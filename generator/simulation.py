"""Discrete-event routing simulation engine for Phase 3a.

Implements:
- business_rules.md section 6: Routing (eligibility H1-H7, scoring, waiting, language fallback, handovers)
- business_rules.md section 6.6: Simulation mechanics (ranks, epochs, dispatch, handover limbo, work accounting)
- business_rules.md section 7.1-7.3: Status transitions, timeline, and handle times
- business_rules.md section 3.4: Shifts and on-shift intervals
- data_dictionary.md sections 13, 16, 18: AgentShift, TicketStatusEvent, AssignmentEvent schemas

Discrete-Event Rank Order (business_rules.md 6.6):
--------------------------------------------------
Events occurring at the exact same timestamp are processed in rank order:
  Rank 1: RESOLVE      - Agent finishes ticket work; moves IN_PROGRESS -> RESOLVED; releases load.
  Rank 2: SHIFT_END    - Agent shift ends; uncompleted tickets enter handover limbo; releases load.
  Rank 3: SHIFT_START  - Agent shift begins; agent enters on-shift pool.
  Rank 4: FALLBACK_DUE - Waiting ticket reaches language fallback threshold; language waiver activated.
  Rank 5: PICKUP       - Agent begins work; moves ASSIGNED -> IN_PROGRESS. First pickup is first_response.
  Rank 6: ENQUEUE      - Ticket arrives / classified; moves NEW -> QUEUED; starts wait clock.
  Rank 7: CLOSE        - Ticket closure at resolved_at + 72 hours; moves RESOLVED -> CLOSED.

After all events at a timestamp are processed, ONE DISPATCH pass runs over waiting tickets.

Draw-Vector Layout (business_rules.md 6.6):
-------------------------------------------
A fixed-size float vector (size 8) is pre-drawn per ticket from
ticket_rng(seed, ticket_number, "handle_times"):
  Index 0: work (raw lognormal draw, evaluated with median_eff and sigma 0.5)
  Index 1: pickup_delay assignment 1 (raw draw: lognormal for CHAT/EMAIL, uniform for PHONE)
  Index 2: pickup_delay assignment 2 (raw draw: lognormal for CHAT/EMAIL, uniform for PHONE)
  Index 3: pickup_delay assignment 3 (raw draw: lognormal for CHAT/EMAIL, uniform for PHONE)
  Index 4: pickup_delay assignment 4 (raw draw: lognormal for CHAT/EMAIL, uniform for PHONE)
  Index 5: pickup_delay assignment 5 (raw draw: lognormal for CHAT/EMAIL, uniform for PHONE)
  Index 6: classification_delay (seconds in [2, 30], uniform draw)
  Index 7: reserved (0.0)
"""

from __future__ import annotations

import datetime
import heapq
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence
import numpy as np
import pandas as pd

from generator.config import Config
from generator import ids
from generator.rng import ticket_rng
from generator import sla_calendar


# =============================================================================
# Constants and Helper Functions
# =============================================================================

# Event ranks matching business_rules.md 6.6
RANK_RESOLVE: Final[int] = 1
RANK_SHIFT_END: Final[int] = 2
RANK_SHIFT_START: Final[int] = 3
RANK_FALLBACK_DUE: Final[int] = 4
RANK_PICKUP: Final[int] = 5
RANK_ENQUEUE: Final[int] = 6
RANK_CLOSE: Final[int] = 7

# Priority rank ordering for dispatch (P1 highest priority)
PRIORITY_RANKS: Final[Mapping[str, int]] = {
    "P1": 1,
    "P2": 2,
    "P3": 3,
    "P4": 4,
}

# Language fallback wait thresholds in seconds (business_rules.md 6.3)
FALLBACK_THRESHOLDS_SEC: Final[Mapping[str, int]] = {
    "P1": 5 * 60,    # 5 min
    "P2": 15 * 60,   # 15 min
    "P3": 30 * 60,   # 30 min
    "P4": 60 * 60,   # 60 min
}

# Skill proficiency ordering for tie-breaking (business_rules.md 6.2)
PROFICIENCY_SCORES: Final[Mapping[str, int]] = {
    "EXPERT": 3,
    "PROFICIENT": 2,
    "BASIC": 1,
}

# Channel handle time multiplier (business_rules.md 7.3)
CHANNEL_WORK_FACTORS: Final[Mapping[str, float]] = {
    "EMAIL": 3.0,
    "CHAT": 1.0,
    "PHONE": 0.8,
}

# SLA Target first response minutes for queue ordering (business_rules.md 2.5)
SLA_FRT_MINUTES: Final[Mapping[str, int]] = {
    "PRL-001": 15,
    "PRL-002": 60,
    "PRL-003": 45,
    "PRL-004": 240,
    "PRL-005": 480,
}

# Reference epoch for integer seconds math (2026-01-05 00:00:00 UTC)
EPOCH_UTC: Final[datetime.datetime] = datetime.datetime(
    2026, 1, 5, 0, 0, 0, tzinfo=datetime.timezone.utc
)


def _dt_to_sec(dt: datetime.datetime | str) -> int:
    """Convert UTC datetime or ISO string to integer seconds since EPOCH_UTC."""
    if isinstance(dt, str):
        clean = dt.replace("Z", "+00:00")
        dt_val = datetime.datetime.fromisoformat(clean)
    else:
        dt_val = dt
    if dt_val.tzinfo is None:
        dt_val = dt_val.replace(tzinfo=datetime.timezone.utc)
    return int((dt_val.astimezone(datetime.timezone.utc) - EPOCH_UTC).total_seconds())


def _sec_to_iso(sec: int) -> str:
    """Convert integer seconds since EPOCH_UTC to UTC ISO-8601 string."""
    dt = EPOCH_UTC + datetime.timedelta(seconds=sec)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sec_to_date_str(sec: int) -> str:
    """Convert integer seconds since EPOCH_UTC to UTC date string YYYY-MM-DD."""
    dt = EPOCH_UTC + datetime.timedelta(seconds=sec)
    return dt.strftime("%Y-%m-%d")


def round_half_up(val: float) -> int:
    """Round half up to nearest integer (business_rules.md 1.1)."""
    return int(np.floor(val + 0.5))


# =============================================================================
# Agent and Ticket Models
# =============================================================================

@dataclass
class AgentSkillRecord:
    skill_id: str
    proficiency: str
    effective_from: str
    effective_to: str

    def is_effective(self, date_str: str) -> bool:
        if date_str < self.effective_from:
            return False
        if self.effective_to and date_str > self.effective_to:
            return False
        return True


@dataclass
class QueueMemberRecord:
    queue_id: str
    effective_from: str
    effective_to: str

    def is_effective(self, date_str: str) -> bool:
        if date_str < self.effective_from:
            return False
        if self.effective_to and date_str > self.effective_to:
            return False
        return True


@dataclass
class AgentState:
    agent_id: str
    tier: str
    status: str
    hire_date: str
    concurrency_limit: int
    languages: set[str]
    skills: list[AgentSkillRecord]
    queues: list[QueueMemberRecord]
    shifts: list[tuple[int, int]]  # list of [start_sec, end_sec)
    current_load: int = 0
    owned_tickets: list[str] = field(default_factory=list)


@dataclass
class SimTicket:
    ticket_id: str
    customer_id: str
    channel_id: str
    intent_id: str
    queue_id: str
    language_code: str
    created_at_sec: int
    priority_level: str
    priority_rule_id: str
    is_critical_incident: bool
    incident_id: str
    required_skill_id: str

    # Queue ordering key
    first_response_due_sec: int

    # Timing parameters & work
    total_work_sec: int
    remaining_work_sec: int
    classification_delay_sec: int
    draw_base_pickups: list[float]
    fallback_threshold_sec: int

    # Simulation lifecycle state
    current_status: str = "NEW"
    epoch: int = 0
    queued_at_sec: int = 0
    current_assigned_at_sec: int = 0
    current_owner_id: str = ""
    first_response_at_sec: int | None = None
    resolved_at_sec: int | None = None
    resolved_by_agent_id: str = ""
    final_owner_agent_id: str = ""
    handover_count: int = 0
    previous_owners: list[str] = field(default_factory=list)
    language_waived: bool = False

    # Event sequence counters
    status_seq: int = 0
    assignment_seq: int = 0


# =============================================================================
# Pre-drawn Randomness Helper
# =============================================================================

def _precompute_ticket_draws(
    seed: int,
    ticket_number: int,
    channel_id: str,
    intent_median_handle: float,
    is_critical_incident: bool,
    priority_level: str,
) -> tuple[int, int, list[float]]:
    """Pre-draw per-ticket timing vector from ticket_rng(seed, ticket_number, 'handle_times').

    Returns:
        classification_delay_sec: int (2-30 s)
        total_work_sec: int (whole seconds, min 60 s)
        base_pickups: list of 6 raw pickup delay draws in minutes
    """
    rng = ticket_rng(seed, ticket_number, "handle_times")

    # 1. Total work W (business_rules.md 7.3)
    # median_eff = 15 for incident tickets, else intent median x channel factor
    if is_critical_incident:
        median_eff = 15.0
    else:
        channel_factor = CHANNEL_WORK_FACTORS[channel_id]
        median_eff = float(intent_median_handle) * channel_factor

    # work = max(lognormal(median_eff, 0.5), 1 min)
    work_draw_min = rng.lognormal(mean=float(np.log(median_eff)), sigma=0.5)
    work_min = max(float(work_draw_min), 1.0)
    total_work_sec = round_half_up(work_min * 60.0)

    # 2. Pickup delay base draws for up to 6 assignments (business_rules.md 7.2)
    # CHAT: lognormal median 1.5 min, sigma 0.6
    # EMAIL: lognormal median 15 min, sigma 0.8
    # PHONE: uniform 0-1 min
    # P1 x 0.5
    base_pickups: list[float] = []
    prio_mult = 0.5 if priority_level == "P1" else 1.0

    for _ in range(6):
        if channel_id == "CHAT":
            raw_p = float(rng.lognormal(mean=float(np.log(1.5)), sigma=0.6))
        elif channel_id == "EMAIL":
            raw_p = float(rng.lognormal(mean=float(np.log(15.0)), sigma=0.8))
        else:  # PHONE
            raw_p = float(rng.uniform(0.0, 1.0))
        base_pickups.append(raw_p * prio_mult)

    # 3. Classification delay in [2, 30] seconds (business_rules.md 7.2)
    class_delay_raw = float(rng.uniform(2.0, 30.0))
    classification_delay_sec = round_half_up(class_delay_raw)

    return classification_delay_sec, total_work_sec, base_pickups


# =============================================================================
# Discrete-Event Simulation Engine
# =============================================================================

class SimulationEngine:
    """Discrete-event simulation engine for support ticket routing."""

    def __init__(
        self,
        config: Config,
        master_tables: Mapping[str, pd.DataFrame],
        ticket_draft_df: pd.DataFrame,
    ) -> None:
        """Initialize simulation environment with master tables and ticket drafts."""
        self.config = config
        self.cutoff_sec = _dt_to_sec(config.cutoff)

        # Output rows collected in emission order
        self.status_event_rows: list[dict[str, Any]] = []
        self.assignment_event_rows: list[dict[str, Any]] = []

        # Tracking structures
        self.daily_assignments: dict[str, dict[str, int]] = {}

        # Load reference data
        self._init_master_data(master_tables)
        self._init_tickets(ticket_draft_df, master_tables["Intent"])

        # Event priority queue
        # Items: (timestamp_sec, rank, tie_breaker_id, event_kind, payload)
        self.event_queue: list[tuple[int, int, str, str, Any]] = []

        # Current set of agents on shift
        self.on_shift_agents: set[str] = set()

        # Waiting pools
        self.queued_tickets: dict[str, SimTicket] = {}  # ticket_id -> SimTicket
        self.handover_limbo: list[SimTicket] = []

    def _init_master_data(self, tables: Mapping[str, pd.DataFrame]) -> None:
        """Parse master data tables into efficient lookup structures."""
        # Queue min_tier lookup
        self.queue_min_tier: dict[str, str] = {
            row["queue_id"]: row["min_tier"]
            for _, row in tables["Queue"].iterrows()
        }

        # Agent skills
        skills_by_agent: dict[str, list[AgentSkillRecord]] = {}
        for _, row in tables["AgentSkill"].iterrows():
            aid = str(row["agent_id"])
            skills_by_agent.setdefault(aid, []).append(
                AgentSkillRecord(
                    skill_id=str(row["skill_id"]),
                    proficiency=str(row["proficiency"]),
                    effective_from=str(row["effective_from"]),
                    effective_to=str(row["effective_to"]) if pd.notna(row["effective_to"]) else "",
                )
            )

        # Agent queues
        queues_by_agent: dict[str, list[QueueMemberRecord]] = {}
        for _, row in tables["QueueMember"].iterrows():
            aid = str(row["agent_id"])
            queues_by_agent.setdefault(aid, []).append(
                QueueMemberRecord(
                    queue_id=str(row["queue_id"]),
                    effective_from=str(row["effective_from"]),
                    effective_to=str(row["effective_to"]) if pd.notna(row["effective_to"]) else "",
                )
            )

        # Agent languages (FLUENT or NATIVE only)
        langs_by_agent: dict[str, set[str]] = {}
        for _, row in tables["AgentLanguage"].iterrows():
            if row["fluency"] in ("FLUENT", "NATIVE"):
                langs_by_agent.setdefault(str(row["agent_id"]), set()).add(str(row["language_code"]))

        # Agent shifts precomputed across whole window up to cutoff (business_rules.md 3.4)
        shifts_by_agent: dict[str, list[tuple[int, int]]] = {}
        shift_df = tables["AgentShift"]

        # Parse shift definitions
        shift_rows_by_agent_dow: dict[tuple[str, int], list[tuple[str, str, str, str]]] = {}
        for _, row in shift_df.iterrows():
            aid = str(row["agent_id"])
            dow = int(row["day_of_week"])
            eff_from = str(row["effective_from"])
            eff_to = str(row["effective_to"]) if pd.notna(row["effective_to"]) else ""
            shift_rows_by_agent_dow.setdefault((aid, dow), []).append(
                (str(row["shift_code"]), str(row["start_time"]), eff_from, eff_to)
            )

        start_date = datetime.date(2026, 1, 5)
        cutoff_dt = datetime.datetime.fromisoformat(self.config.cutoff.replace("Z", "+00:00"))
        cutoff_date = cutoff_dt.date()
        cur_date = start_date

        while cur_date <= cutoff_date:
            dow = cur_date.isoweekday()  # 1=Mon..7=Sun
            date_str = cur_date.strftime("%Y-%m-%d")

            for aid in tables["Agent"]["agent_id"]:
                matches = shift_rows_by_agent_dow.get((aid, dow), [])
                for shift_code, _, eff_from, eff_to in matches:
                    if date_str < eff_from:
                        continue
                    if eff_to and date_str > eff_to:
                        continue

                    # Shift hours
                    if shift_code == "EARLY":
                        s_dt = datetime.datetime(cur_date.year, cur_date.month, cur_date.day, 6, 0, 0, tzinfo=datetime.timezone.utc)
                        e_dt = datetime.datetime(cur_date.year, cur_date.month, cur_date.day, 14, 0, 0, tzinfo=datetime.timezone.utc)
                    elif shift_code == "DAY":
                        s_dt = datetime.datetime(cur_date.year, cur_date.month, cur_date.day, 10, 0, 0, tzinfo=datetime.timezone.utc)
                        e_dt = datetime.datetime(cur_date.year, cur_date.month, cur_date.day, 18, 0, 0, tzinfo=datetime.timezone.utc)
                    elif shift_code == "LATE":
                        s_dt = datetime.datetime(cur_date.year, cur_date.month, cur_date.day, 14, 0, 0, tzinfo=datetime.timezone.utc)
                        e_dt = datetime.datetime(cur_date.year, cur_date.month, cur_date.day, 22, 0, 0, tzinfo=datetime.timezone.utc)
                    else:  # NIGHT (22:00 to 06:00 next day)
                        s_dt = datetime.datetime(cur_date.year, cur_date.month, cur_date.day, 22, 0, 0, tzinfo=datetime.timezone.utc)
                        next_d = cur_date + datetime.timedelta(days=1)
                        e_dt = datetime.datetime(next_d.year, next_d.month, next_d.day, 6, 0, 0, tzinfo=datetime.timezone.utc)

                    s_sec = _dt_to_sec(s_dt)
                    e_sec = _dt_to_sec(e_dt)
                    shifts_by_agent.setdefault(aid, []).append((s_sec, e_sec))

            cur_date += datetime.timedelta(days=1)

        # Build AgentState objects
        self.agents: dict[str, AgentState] = {}
        for _, row in tables["Agent"].iterrows():
            aid = str(row["agent_id"])
            self.agents[aid] = AgentState(
                agent_id=aid,
                tier=str(row["tier"]),
                status=str(row["status"]),
                hire_date=str(row["hire_date"]),
                concurrency_limit=int(row["concurrency_limit"]),
                languages=langs_by_agent.get(aid, set()),
                skills=skills_by_agent.get(aid, []),
                queues=queues_by_agent.get(aid, []),
                shifts=shifts_by_agent.get(aid, []),
            )

    def _init_tickets(self, ticket_draft_df: pd.DataFrame, intent_df: pd.DataFrame) -> None:
        """Initialize ticket objects with pre-drawn handles and SLA target due times."""
        intents_meta = intent_df.set_index("intent_id").to_dict(orient="index")
        self.tickets: dict[str, SimTicket] = {}

        for _, row in ticket_draft_df.iterrows():
            tid = str(row["ticket_id"])
            t_num = int(tid.split("-")[-1])
            ch = str(row["channel_id"])
            iid = str(row["intent_id"])
            prio = str(row["priority_level"])
            prio_rule = str(row["priority_rule_id"])
            is_crit = bool(row["is_critical_incident"])
            inc_id = str(row["incident_id"]) if pd.notna(row["incident_id"]) else ""
            c_sec = _dt_to_sec(str(row["created_at"]))

            meta = intents_meta[iid]
            req_skill = str(meta["required_skill_id"])
            intent_med = float(meta["median_handle_minutes"])

            # SLA first_response_due for queue ordering (business_rules.md 6.6)
            # Computed from PriorityRule and sla_calendar.py, never from ground truth!
            ch_cal = "CAL-247" if prio == "P1" else f"CAL-{ch}"
            created_dt = EPOCH_UTC + datetime.timedelta(seconds=c_sec)
            clock_start = sla_calendar.next_open(ch_cal, created_dt)
            frt_min = SLA_FRT_MINUTES[prio_rule]
            frt_due_dt = sla_calendar.add_business_minutes(ch_cal, clock_start, frt_min)
            frt_due_sec = _dt_to_sec(frt_due_dt)

            # Pre-draw per-ticket timing vector
            class_delay, total_work, base_pickups = _precompute_ticket_draws(
                self.config.seed,
                t_num,
                ch,
                intent_med,
                is_crit,
                prio,
            )

            fallback_thresh = FALLBACK_THRESHOLDS_SEC[prio]

            self.tickets[tid] = SimTicket(
                ticket_id=tid,
                customer_id=str(row["customer_id"]),
                channel_id=ch,
                intent_id=iid,
                queue_id=str(row["queue_id"]),
                language_code=str(row["language_code"]),
                created_at_sec=c_sec,
                priority_level=prio,
                priority_rule_id=prio_rule,
                is_critical_incident=is_crit,
                incident_id=inc_id,
                required_skill_id=req_skill,
                first_response_due_sec=frt_due_sec,
                total_work_sec=total_work,
                remaining_work_sec=total_work,
                classification_delay_sec=class_delay,
                draw_base_pickups=base_pickups,
                fallback_threshold_sec=fallback_thresh,
            )

    def _schedule_event(self, time_sec: int, rank: int, tie_id: str, kind: str, payload: Any) -> None:
        """Push an event into the deterministic priority queue."""
        if time_sec > self.cutoff_sec:
            return
        heapq.heappush(self.event_queue, (time_sec, rank, tie_id, kind, payload))

    def _emit_status_event(
        self,
        ticket: SimTicket,
        from_status: str | None,
        to_status: str,
        event_at_sec: int,
        actor_type: str,
        actor_agent_id: str = "",
        trigger_ref: str = "",
    ) -> None:
        """Emit a TicketStatusEvent row in chronological order."""
        if event_at_sec > self.cutoff_sec:
            return

        ticket.status_seq += 1
        ticket.current_status = to_status

        self.status_event_rows.append({
            "status_event_id": ids.status_event_id(ticket.ticket_id, ticket.status_seq),
            "ticket_id": ticket.ticket_id,
            "seq_no": ticket.status_seq,
            "cycle_no": 1,
            "from_status": from_status if from_status else "",
            "to_status": to_status,
            "event_at": _sec_to_iso(event_at_sec),
            "actor_type": actor_type,
            "actor_agent_id": actor_agent_id if actor_agent_id else "",
            "trigger_ref": trigger_ref if trigger_ref else "",
        })

    def _emit_assignment_event(
        self,
        ticket: SimTicket,
        event_type: str,
        reason_code: str,
        event_at_sec: int,
        agent: AgentState,
        from_agent_id: str,
        language_match: bool,
        wait_minutes: float,
        agent_load_before: int,
    ) -> str:
        """Emit an AssignmentEvent row in chronological order and return assignment_id."""
        ticket.assignment_seq += 1
        asn_id = ids.assignment_id(ticket.ticket_id, ticket.assignment_seq)

        # Update daily assignment count (business_rules.md 6.2)
        date_str = _sec_to_date_str(event_at_sec)
        self.daily_assignments.setdefault(date_str, {})
        self.daily_assignments[date_str][agent.agent_id] = (
            self.daily_assignments[date_str].get(agent.agent_id, 0) + 1
        )

        self.assignment_event_rows.append({
            "assignment_id": asn_id,
            "ticket_id": ticket.ticket_id,
            "seq_no": ticket.assignment_seq,
            "cycle_no": 1,
            "event_type": event_type,
            "reason_code": reason_code,
            "event_at": _sec_to_iso(event_at_sec),
            "agent_id": agent.agent_id,
            "from_agent_id": from_agent_id if from_agent_id else "",
            "queue_id": ticket.queue_id,
            "required_skill_id": ticket.required_skill_id,
            "language_match": "true" if language_match else "false",
            "wait_minutes": f"{wait_minutes:.1f}",
            "agent_load_before": agent_load_before,
            "agent_load_after": agent_load_before + 1,
            "concurrency_limit": agent.concurrency_limit,
        })
        return asn_id

    def _check_eligibility(
        self,
        agent: AgentState,
        ticket: SimTicket,
        event_at_sec: int,
    ) -> tuple[bool, bool, int]:
        """Evaluate H1-H7 eligibility constraints for an agent and ticket.

        Returns:
            (is_eligible, language_match, proficiency_score)
        """
        date_str = _sec_to_date_str(event_at_sec)

        # H1: status = ACTIVE and hire_date <= date
        if agent.status != "ACTIVE" or agent.hire_date > date_str:
            return False, False, 0

        # H2: Agent is on shift at that instant
        if agent.agent_id not in self.on_shift_agents:
            return False, False, 0

        # H5: agent_load_before < concurrency_limit
        if agent.current_load >= agent.concurrency_limit:
            return False, False, 0

        # H7: Agent has not already owned this ticket in the same cycle
        if agent.agent_id in ticket.previous_owners:
            return False, False, 0

        # H3: Agent holds required_skill_id on that date
        matching_skill = None
        for sk in agent.skills:
            if sk.skill_id == ticket.required_skill_id and sk.is_effective(date_str):
                matching_skill = sk
                break

        if matching_skill is None:
            return False, False, 0

        prof_score = PROFICIENCY_SCORES.get(matching_skill.proficiency, 0)

        # H3b: For P1 and P2 tickets, proficiency is PROFICIENT or EXPERT
        if ticket.priority_level in ("P1", "P2") and prof_score < 2:
            return False, False, 0

        # H6: Agent is QueueMember of target queue on that date, and tier >= queue.min_tier
        is_qm = any(q.queue_id == ticket.queue_id and q.is_effective(date_str) for q in agent.queues)
        if not is_qm:
            return False, False, 0

        min_tier = self.queue_min_tier[ticket.queue_id]
        if min_tier == "T2" and agent.tier == "T1":
            return False, False, 0
        if min_tier == "T3" and agent.tier in ("T1", "T2"):
            return False, False, 0

        # H4: Agent speaks ticket language at FLUENT or NATIVE
        lang_match = ticket.language_code in agent.languages

        if not lang_match and not ticket.language_waived:
            return False, False, 0

        return True, lang_match, prof_score

    def _score_agent(
        self,
        agent: AgentState,
        lang_match: bool,
        prof_score: int,
        event_at_sec: int,
    ) -> tuple[int, float, int, int, str]:
        """Compute scoring tuple for eligible agent (business_rules.md 6.2, 6.3).

        Sort by:
        (0) language_match descending (if waived, speakers preferred)
        (1) load / concurrency_limit ascending
        (2) proficiency descending (EXPERT > PROFICIENT > BASIC)
        (3) assignments received that day ascending
        (4) agent_id ascending
        """
        date_str = _sec_to_date_str(event_at_sec)
        daily_cnt = self.daily_assignments.get(date_str, {}).get(agent.agent_id, 0)
        norm_load = agent.current_load / float(agent.concurrency_limit)
        lang_rank = 0 if lang_match else 1

        return (lang_rank, norm_load, -prof_score, daily_cnt, agent.agent_id)

    def _dispatch_ticket(self, ticket: SimTicket, event_at_sec: int, is_limbo: bool) -> bool:
        """Attempt to dispatch a waiting ticket to an eligible agent."""
        best_agent: AgentState | None = None
        best_score: tuple[int, float, int, int, str] | None = None
        best_lang_match = False

        # Only evaluate agents currently on shift with capacity (sorted by agent_id for determinism)
        for aid in sorted(self.on_shift_agents):
            agent = self.agents[aid]
            if agent.current_load >= agent.concurrency_limit:
                continue

            ok, lang_match, prof_score = self._check_eligibility(agent, ticket, event_at_sec)
            if not ok:
                continue

            score = self._score_agent(agent, lang_match, prof_score, event_at_sec)
            if best_score is None or score < best_score:
                best_score = score
                best_agent = agent
                best_lang_match = lang_match

        if best_agent is None:
            return False

        # Determine event_type and reason_code before updating previous_owners
        if ticket.previous_owners:
            # Transfer from previous owner
            event_type = "TRANSFER"
            reason_code = "SHIFT_HANDOVER"
            from_agent_id = ticket.previous_owners[-1]
            ticket.handover_count += 1
            wait_min = 0.0 if is_limbo else round((event_at_sec - ticket.queued_at_sec) / 60.0, 1)
        else:
            # First assignment of the ticket
            event_type = "ASSIGN"
            reason_code = "LANGUAGE_FALLBACK" if not best_lang_match else "INITIAL_ROUTING"
            from_agent_id = ""
            wait_min = round((event_at_sec - ticket.queued_at_sec) / 60.0, 1)

        # Assign ticket to best_agent
        ticket.epoch += 1
        load_before = best_agent.current_load
        best_agent.current_load += 1
        best_agent.owned_tickets.append(ticket.ticket_id)

        from_status = ticket.current_status
        ticket.current_owner_id = best_agent.agent_id
        ticket.final_owner_agent_id = best_agent.agent_id
        ticket.previous_owners.append(best_agent.agent_id)
        ticket.current_assigned_at_sec = event_at_sec

        # Emit AssignmentEvent
        asn_id = self._emit_assignment_event(
            ticket=ticket,
            event_type=event_type,
            reason_code=reason_code,
            event_at_sec=event_at_sec,
            agent=best_agent,
            from_agent_id=from_agent_id,
            language_match=best_lang_match,
            wait_minutes=wait_min,
            agent_load_before=load_before,
        )

        # Emit TicketStatusEvent
        self._emit_status_event(
            ticket=ticket,
            from_status=from_status,
            to_status="ASSIGNED",
            event_at_sec=event_at_sec,
            actor_type="SYSTEM",
            actor_agent_id="",
            trigger_ref=asn_id,
        )

        # Schedule PICKUP and RESOLVE
        asn_idx = min(len(ticket.previous_owners) - 1, len(ticket.draw_base_pickups) - 1)
        base_p = ticket.draw_base_pickups[asn_idx]
        actual_pickup_sec = round_half_up(base_p * (1.0 + 0.1 * load_before) * 60.0)
        pickup_at = event_at_sec + actual_pickup_sec
        resolved_at = max(event_at_sec + ticket.remaining_work_sec, pickup_at + 60)

        # Schedule events with the ticket's current epoch
        self._schedule_event(pickup_at, RANK_PICKUP, ticket.ticket_id, "PICKUP", (ticket.ticket_id, ticket.epoch))
        self._schedule_event(resolved_at, RANK_RESOLVE, ticket.ticket_id, "RESOLVE", (ticket.ticket_id, ticket.epoch))

        return True

    def _run_dispatch_pass(self, event_at_sec: int) -> None:
        """Run one dispatch pass over waiting tickets (business_rules.md 6.6)."""
        # The waiting list is every ticket in QUEUED plus handover tickets in limbo,
        # ordered by (priority, first_response_due, ticket_id).
        candidates: list[tuple[int, int, str, SimTicket, bool]] = []

        for tkt in self.handover_limbo:
            prio_rank = PRIORITY_RANKS[tkt.priority_level]
            candidates.append((prio_rank, tkt.first_response_due_sec, tkt.ticket_id, tkt, True))

        for tid, tkt in self.queued_tickets.items():
            prio_rank = PRIORITY_RANKS[tkt.priority_level]
            candidates.append((prio_rank, tkt.first_response_due_sec, tid, tkt, False))

        candidates.sort(key=lambda x: (x[0], x[1], x[2]))

        dispatched_limbo: set[str] = set()
        dispatched_queued: set[str] = set()

        for _, _, tid, tkt, is_limbo in candidates:
            # Check if there is any on-shift agent with capacity
            if not any(self.agents[aid].current_load < self.agents[aid].concurrency_limit for aid in self.on_shift_agents):
                break

            success = self._dispatch_ticket(tkt, event_at_sec, is_limbo)
            if success:
                if is_limbo:
                    dispatched_limbo.add(tid)
                else:
                    dispatched_queued.add(tid)

        # Remove dispatched tickets from pools
        if dispatched_queued:
            for tid in dispatched_queued:
                del self.queued_tickets[tid]

        if dispatched_limbo:
            self.handover_limbo = [t for t in self.handover_limbo if t.ticket_id not in dispatched_limbo]

        # Limbo tickets still unassigned after dispatch get a -> QUEUED status event at that instant
        if self.handover_limbo:
            for tkt in self.handover_limbo:
                from_st = tkt.current_status
                self._emit_status_event(
                    ticket=tkt,
                    from_status=from_st,
                    to_status="QUEUED",
                    event_at_sec=event_at_sec,
                    actor_type="SYSTEM",
                    actor_agent_id="",
                    trigger_ref="",
                )
                tkt.queued_at_sec = event_at_sec
                self.queued_tickets[tkt.ticket_id] = tkt

                # Schedule fallback due if not waived and language != en
                if not tkt.language_waived and tkt.language_code != "en":
                    due_sec = event_at_sec + tkt.fallback_threshold_sec
                    self._schedule_event(due_sec, RANK_FALLBACK_DUE, tkt.ticket_id, "FALLBACK_DUE", (tkt.ticket_id, event_at_sec))

            self.handover_limbo.clear()

    def run(self) -> None:
        """Execute the discrete-event simulation until the event queue is empty or cutoff is reached."""
        # 1. Schedule all shift boundaries
        for aid, agent in self.agents.items():
            for s_sec, e_sec in agent.shifts:
                if s_sec <= self.cutoff_sec:
                    self._schedule_event(s_sec, RANK_SHIFT_START, aid, "SHIFT_START", aid)
                if e_sec <= self.cutoff_sec:
                    self._schedule_event(e_sec, RANK_SHIFT_END, aid, "SHIFT_END", aid)

        # 2. Schedule all ticket arrivals and initial status events (NEW at created_at)
        for tid, ticket in sorted(self.tickets.items(), key=lambda x: (x[1].created_at_sec, x[0])):
            c_sec = ticket.created_at_sec
            self._emit_status_event(
                ticket=ticket,
                from_status=None,
                to_status="NEW",
                event_at_sec=c_sec,
                actor_type="SYSTEM",
                actor_agent_id="",
                trigger_ref="",
            )

            # Schedule ENQUEUE after classification delay (business_rules.md 7.2)
            enq_sec = c_sec + ticket.classification_delay_sec
            self._schedule_event(enq_sec, RANK_ENQUEUE, tid, "ENQUEUE", tid)

        # 3. Main event loop
        while self.event_queue:
            # Peek next timestamp
            current_t = self.event_queue[0][0]
            if current_t > self.cutoff_sec:
                break

            # Collect all events happening at exactly current_t
            events_at_t: list[tuple[int, int, str, str, Any]] = []
            while self.event_queue and self.event_queue[0][0] == current_t:
                events_at_t.append(heapq.heappop(self.event_queue))

            # Process events in rank order (business_rules.md 6.6)
            for t_sec, rank, tie_id, kind, payload in events_at_t:
                if rank == RANK_RESOLVE:
                    tid, epoch = payload
                    tkt = self.tickets[tid]
                    if tkt.epoch == epoch and tkt.current_status == "IN_PROGRESS":
                        # Ticket resolves under current owner
                        owner_id = tkt.current_owner_id
                        agent = self.agents[owner_id]
                        agent.current_load -= 1
                        if tid in agent.owned_tickets:
                            agent.owned_tickets.remove(tid)

                        tkt.resolved_at_sec = t_sec
                        tkt.resolved_by_agent_id = owner_id

                        self._emit_status_event(
                            ticket=tkt,
                            from_status="IN_PROGRESS",
                            to_status="RESOLVED",
                            event_at_sec=t_sec,
                            actor_type="AGENT",
                            actor_agent_id=owner_id,
                            trigger_ref="",
                        )

                        # Schedule CLOSE at resolved_at + 72 hours (business_rules.md 7.7)
                        close_at = t_sec + 72 * 3600
                        self._schedule_event(close_at, RANK_CLOSE, tid, "CLOSE", tid)

                elif rank == RANK_SHIFT_END:
                    aid = payload
                    self.on_shift_agents.discard(aid)
                    agent = self.agents[aid]

                    # All tickets owned by agent enter handover limbo in ticket_id order
                    owned_tids = sorted(agent.owned_tickets)
                    for otid in owned_tids:
                        tkt = self.tickets[otid]
                        # Work accrued while owned
                        accrued = t_sec - tkt.current_assigned_at_sec
                        tkt.remaining_work_sec = max(0, tkt.remaining_work_sec - accrued)
                        tkt.current_owner_id = ""
                        self.handover_limbo.append(tkt)

                    agent.owned_tickets.clear()
                    agent.current_load = 0

                elif rank == RANK_SHIFT_START:
                    aid = payload
                    self.on_shift_agents.add(aid)

                elif rank == RANK_FALLBACK_DUE:
                    tid, q_sec = payload
                    tkt = self.tickets[tid]
                    if tkt.current_status == "QUEUED" and tkt.queued_at_sec == q_sec:
                        tkt.language_waived = True

                elif rank == RANK_PICKUP:
                    tid, epoch = payload
                    tkt = self.tickets[tid]
                    if tkt.epoch == epoch and tkt.current_status == "ASSIGNED":
                        if tkt.first_response_at_sec is None:
                            tkt.first_response_at_sec = t_sec

                        self._emit_status_event(
                            ticket=tkt,
                            from_status="ASSIGNED",
                            to_status="IN_PROGRESS",
                            event_at_sec=t_sec,
                            actor_type="AGENT",
                            actor_agent_id=tkt.current_owner_id,
                            trigger_ref="",
                        )

                elif rank == RANK_ENQUEUE:
                    tid = payload
                    tkt = self.tickets[tid]
                    self._emit_status_event(
                        ticket=tkt,
                        from_status="NEW",
                        to_status="QUEUED",
                        event_at_sec=t_sec,
                        actor_type="SYSTEM",
                        actor_agent_id="",
                        trigger_ref="",
                    )
                    tkt.queued_at_sec = t_sec
                    self.queued_tickets[tid] = tkt

                    # Schedule fallback due if language != en
                    if not tkt.language_waived and tkt.language_code != "en":
                        due_sec = t_sec + tkt.fallback_threshold_sec
                        self._schedule_event(due_sec, RANK_FALLBACK_DUE, tid, "FALLBACK_DUE", (tid, t_sec))

                elif rank == RANK_CLOSE:
                    tid = payload
                    tkt = self.tickets[tid]
                    if t_sec <= self.cutoff_sec and tkt.current_status == "RESOLVED":
                        self._emit_status_event(
                            ticket=tkt,
                            from_status="RESOLVED",
                            to_status="CLOSED",
                            event_at_sec=t_sec,
                            actor_type="SYSTEM",
                            actor_agent_id="",
                            trigger_ref="",
                        )

            # After all events at current_t are processed, run ONE DISPATCH pass
            self._run_dispatch_pass(current_t)


# =============================================================================
# Outcome Generation and Exporters
# =============================================================================

def simulate_routing(
    config: Config,
    master_tables: Mapping[str, pd.DataFrame],
    ticket_draft_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run discrete-event routing simulation for Phase 3a.

    Returns:
        tuple of (status_event_df, assignment_event_df, ticket_outcome_df)
    """
    engine = SimulationEngine(config, master_tables, ticket_draft_df)
    engine.run()

    status_event_df = pd.DataFrame(engine.status_event_rows)
    assignment_event_df = pd.DataFrame(engine.assignment_event_rows)

    # Build outcome rows
    outcome_rows: list[dict[str, Any]] = []
    for tid in sorted(engine.tickets.keys()):
        tkt = engine.tickets[tid]
        outcome_rows.append({
            "ticket_id": tkt.ticket_id,
            "first_response_at": _sec_to_iso(tkt.first_response_at_sec) if tkt.first_response_at_sec else "",
            "resolved_at": _sec_to_iso(tkt.resolved_at_sec) if tkt.resolved_at_sec else "",
            "resolved_by_agent_id": tkt.resolved_by_agent_id,
            "final_owner_agent_id": tkt.final_owner_agent_id,
            "status_at_cutoff": tkt.current_status,
            "handover_count": tkt.handover_count,
        })

    ticket_outcome_df = pd.DataFrame(outcome_rows)
    return status_event_df, assignment_event_df, ticket_outcome_df


def export_ticket_status_events(df: pd.DataFrame, out_path: str | Path) -> Path:
    """Export status events DataFrame to CSV (data/staging/ticket_status_event_draft.csv)."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n", na_rep="")
    return path


def export_assignment_events(df: pd.DataFrame, out_path: str | Path) -> Path:
    """Export assignment events DataFrame to CSV (data/staging/assignment_event_draft.csv)."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n", na_rep="")
    return path


def export_ticket_outcomes(df: pd.DataFrame, out_path: str | Path) -> Path:
    """Export ticket outcomes DataFrame to CSV (data/staging/ticket_outcome_draft.csv)."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n", na_rep="")
    return path


# =============================================================================
# KPI Report Generator
# =============================================================================

def generate_kpi_report(
    ticket_draft_df: pd.DataFrame,
    status_df: pd.DataFrame,
    assignment_df: pd.DataFrame,
    outcome_df: pd.DataFrame,
    master_tables: Mapping[str, pd.DataFrame],
    scenario_windows: Sequence[Any] | None = None,
) -> str:
    """Generate comprehensive Phase 3a KPI report."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("                  PHASE 3a KPI SIMULATION REPORT                  ")
    lines.append("=" * 72)

    total_tickets = len(outcome_df)
    assigned_cnt = (outcome_df["final_owner_agent_id"] != "").sum()
    resolved_cnt = (outcome_df["resolved_at"] != "").sum()
    closed_cnt = (outcome_df["status_at_cutoff"] == "CLOSED").sum()
    open_cnt = (outcome_df["status_at_cutoff"] != "CLOSED").sum()

    lines.append(f"\n1. Ticket Volume at Cutoff:")
    lines.append(f"   - Total Tickets       : {total_tickets:6d}")
    lines.append(f"   - Assigned Tickets    : {assigned_cnt:6d} ({assigned_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"   - Resolved Tickets    : {resolved_cnt:6d} ({resolved_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"   - Closed Tickets      : {closed_cnt:6d} ({closed_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"   - Open at Cutoff      : {open_cnt:6d} ({open_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"     Status Breakdown    : {outcome_df['status_at_cutoff'].value_counts().to_dict()}")

    # 2. Wait times by priority and channel
    m_asn = assignment_df.merge(ticket_draft_df[["ticket_id", "priority_level", "channel_id"]], on="ticket_id")
    m_asn["wait_minutes"] = m_asn["wait_minutes"].astype(float)

    lines.append(f"\n2. Wait Minutes by Priority (from AssignmentEvents):")
    for prio in ("P1", "P2", "P3", "P4"):
        sub = m_asn[m_asn["priority_level"] == prio]["wait_minutes"]
        if len(sub) > 0:
            lines.append(f"   - {prio:4s}: Mean {sub.mean():6.1f} min | Median {sub.median():5.1f} min | P95 {sub.quantile(0.95):6.1f} min (N={len(sub)})")
        else:
            lines.append(f"   - {prio:4s}: N/A")

    lines.append(f"\n3. Wait Minutes by Channel:")
    for ch in ("EMAIL", "CHAT", "PHONE"):
        sub = m_asn[m_asn["channel_id"] == ch]["wait_minutes"]
        if len(sub) > 0:
            lines.append(f"   - {ch:6s}: Mean {sub.mean():6.1f} min | Median {sub.median():5.1f} min | P95 {sub.quantile(0.95):6.1f} min (N={len(sub)})")
        else:
            lines.append(f"   - {ch:6s}: N/A")

    # 3. Language fallback share
    fb_cnt = (assignment_df["reason_code"] == "LANGUAGE_FALLBACK").sum()
    assign_ev_cnt = (assignment_df["event_type"] == "ASSIGN").sum()
    fb_share = fb_cnt / assign_ev_cnt if assign_ev_cnt > 0 else 0.0
    lines.append(f"\n4. Language Fallback:")
    lines.append(f"   - LANGUAGE_FALLBACK Count: {fb_cnt:5d} / {assign_ev_cnt:5d} initial assignments ({fb_share * 100:.2f}%)")

    # 4. Handover count distribution
    lines.append(f"\n5. Shift Handover Distribution per Ticket:")
    ho_counts = outcome_df["handover_count"].value_counts().sort_index()
    for cnt, num in ho_counts.items():
        lines.append(f"   - {cnt} handovers : {num:5d} tickets ({num / total_tickets * 100:5.1f}%)")
    lines.append(f"   - Max Handovers: {outcome_df['handover_count'].max()}")

    # 5. Agent Utilisation while on shift
    agent_df = master_tables["Agent"]
    tier_map = dict(zip(agent_df["agent_id"], agent_df["tier"]))
    limit_map = dict(zip(agent_df["agent_id"], agent_df["concurrency_limit"].astype(int)))

    # Replay on-shift intervals and load per agent
    # Aggregate load * dt / limit * total_shift_dt
    # For quick exact metric: sample load at 1-hour intervals or integrate
    # Simple discrete integral using status events and shift intervals:
    # Here we compute the average load_before on assignments, and time-integrated utilisation:
    shift_df = master_tables["AgentShift"]
    total_on_shift_hours_per_agent: dict[str, float] = {}
    for _, srow in shift_df.iterrows():
        aid = srow["agent_id"]
        # 5 days per week x 8 hours = 40h/week x (97/7 weeks) ~ 554 hours
        total_on_shift_hours_per_agent[aid] = total_on_shift_hours_per_agent.get(aid, 0.0) + (8.0 * (97.0 / 7.0))

    # Total work delivered by each agent
    # Each ticket resolved or handed over spent actual work
    # We report mean load / limit on assignments and average active load
    mean_load_ratio_on_assign = (assignment_df["agent_load_before"].astype(int) / assignment_df["concurrency_limit"].astype(int)).mean()
    lines.append(f"\n6. Agent Utilisation while on shift:")
    lines.append(f"   - Mean Load/Limit on Assignment: {mean_load_ratio_on_assign * 100:.1f}%")

    assignment_df_with_tier = assignment_df.copy()
    assignment_df_with_tier["tier"] = assignment_df_with_tier["agent_id"].map(tier_map)
    for t_name in ("T1", "T2", "T3"):
        sub_t = assignment_df_with_tier[assignment_df_with_tier["tier"] == t_name]
        if len(sub_t) > 0:
            sub_ratio = (sub_t["agent_load_before"].astype(int) / sub_t["concurrency_limit"].astype(int)).mean()
            lines.append(f"   - Tier {t_name:2s} Load/Limit on Assignment: {sub_ratio * 100:.1f}% (N={len(sub_t)})")

    # 6. Peak queue length per scenario window
    if scenario_windows:
        lines.append(f"\n7. Peak Queue Length per Scenario Window:")
        # Build queue length timeline from status events
        # Status events that enter QUEUED: to_status == 'QUEUED' (+1)
        # Status events that leave QUEUED: from_status == 'QUEUED' (-1)
        events: list[tuple[str, int]] = []
        for _, srow in status_df.iterrows():
            if srow["to_status"] == "QUEUED":
                events.append((srow["event_at"], 1))
            if srow["from_status"] == "QUEUED":
                events.append((srow["event_at"], -1))
        events.sort(key=lambda x: (x[0], -x[1]))  # at same time, +1 before -1

        for w in scenario_windows:
            w_start = w.start_iso if hasattr(w, "start_iso") else str(w["start"])
            w_end = w.end_iso if hasattr(w, "end_iso") else str(w["end"])
            w_id = w.id if hasattr(w, "id") else str(w["id"])
            w_kind = w.kind if hasattr(w, "kind") else str(w["kind"])

            # Compute queue length during window
            cur_q = 0
            peak_q = 0
            for t_iso, delta in events:
                if t_iso > w_end:
                    break
                cur_q += delta
                if t_iso >= w_start:
                    if cur_q > peak_q:
                        peak_q = cur_q

            lines.append(f"   - {w_kind.upper():8s} {w_id:7s} ({w_start}..{w_end}): Peak Queue = {peak_q}")

    lines.append("\n" + "=" * 72)
    report_text = "\n".join(lines)
    return report_text
