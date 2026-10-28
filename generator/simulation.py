"""Discrete-event routing simulation engine for Phase 3a & 3b.

Implements:
- business_rules.md section 6: Routing (eligibility H1-H7, scoring, waiting, language fallback, handovers)
- business_rules.md section 6.6: Simulation mechanics (ranks, epochs, dispatch, handover limbo, work accounting)
- business_rules.md section 7.1-7.8: Complete status transitions, timeline, pending customer,
  skill reroutes, manual & SLA escalations, reopen cycles
- business_rules.md section 3.4: Shifts and on-shift intervals
- business_rules.md section 8.1-8.2: SLA calculations and due dates
- data_dictionary.md sections 13, 16, 18: AgentShift, TicketStatusEvent, AssignmentEvent schemas

Discrete-Event Rank Order at One Timestamp (business_rules.md 7.8(8)):
------------------------------------------------------------------------
Events occurring at the exact same timestamp are processed in rank order:
  Rank 1: RESOLVE      - Agent finishes ticket work; or silent resolve from PENDING_CUSTOMER.
  Rank 2: SHIFT_END    - Agent shift ends; uncompleted tickets enter handover limbo; releases load.
  Rank 3: SHIFT_START  - Agent shift begins; agent enters on-shift pool.
  Rank 4: PENDING_REPLY- Customer reply (IN_PROGRESS) or silent timeout (RESOLVED).
  Rank 5: FALLBACK_DUE - Waiting ticket reaches language fallback threshold; language waiver activated.
  Rank 6: PICKUP       - Agent begins work; moves ASSIGNED -> IN_PROGRESS. First pickup is first_response.
  Rank 7: MID_EVENT    - Mid-handling event starts: PENDING_CUSTOMER, SKILL_REROUTE, or MANUAL_ESCALATION.
  Rank 8: REOPEN       - RESOLVED -> REOPENED (CUSTOMER) -> QUEUED (SYSTEM); starts cycle k > 1.
  Rank 9: ENQUEUE      - Ticket arrives / classified; moves NEW -> QUEUED; starts wait clock.
  Rank 10: CLOSE       - Ticket closure at resolved_at + 72 hours; moves RESOLVED -> CLOSED.

After all events at a timestamp are processed:
DISPATCH and ESCALATION_DUE checks repeat at the same timestamp until nothing changes (7.8(8)).

Per-Cycle Draw-Vector Layout (business_rules.md 6.6, 7.8):
-----------------------------------------------------------
Cycle 1 draws from ticket_rng(seed, ticket_number, "handle_times").
Cycle k > 1 draws from ticket_rng(seed, ticket_number, f"handle_times_c{k}").

Positions 0 to 7 (preserved from Phase 3a):
  Index 0: work (lognormal draw with mean=log(median_eff), sigma=0.5; reopen cycles use 0.5 * median_eff)
  Index 1: pickup_delay assignment 1 (raw draw: lognormal for CHAT/EMAIL, uniform for PHONE)
  Index 2: pickup_delay assignment 2
  Index 3: pickup_delay assignment 3
  Index 4: pickup_delay assignment 4
  Index 5: pickup_delay assignment 5
  Index 6: pickup_delay assignment 6
  Index 7: classification_delay (seconds in [2, 30], uniform draw; only cycle 1 uses this)

Positions 8 to 16 (appended for Phase 3b):
  Index 8: mid_pending_roll (uniform in [0, 1]; tested against intent pending probability)
  Index 9: mid_reroute_roll (uniform in [0, 1]; tested against 0.04 reroute probability)
  Index 10: mid_manual_roll (uniform in [0, 1]; tested against 0.06 ENTERPRISE / 0.02 other probability)
  Index 11: u_draw (uniform in [0, 1]; scaled to [0.4, 0.6] for pending, [0.2, 0.8] for reroute, [0.3, 0.9] for manual)
  Index 12: reroute_target_draw (uniform in [0, 1]; selects target skill for SKILL_REROUTE)
  Index 13: pending_duration_min (lognormal draw with mean=log(45.0), sigma=0.7; whole seconds, >= 60s)
  Index 14: pending_silent_roll (uniform in [0, 1]; tested against 0.03 customer silent probability)
  Index 15: reopen_prob_roll (uniform in [0, 1]; tested against per-cycle reopen probability formula)
  Index 16: reopen_delay_u (uniform in [0, 1]; inverse-CDF truncated lognormal median 6h, sigma 0.8 in (0, 72h])
"""

from __future__ import annotations

import datetime
import heapq
import statistics
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

# Event ranks matching business_rules.md 7.8(8)
RANK_RESOLVE: Final[int] = 1        # RESOLVE and silent resolve
RANK_SHIFT_END: Final[int] = 2      # Agent shift ends; handover limbo
RANK_SHIFT_START: Final[int] = 3    # Agent shift begins
RANK_PENDING_REPLY: Final[int] = 4  # Customer reply or silent resolution
RANK_FALLBACK_DUE: Final[int] = 5   # Language waiver threshold reached
RANK_PICKUP: Final[int] = 6         # First agent action; ASSIGNED -> IN_PROGRESS
RANK_MID_EVENT: Final[int] = 7      # Pending start, reroute, manual escalation
RANK_REOPEN: Final[int] = 8         # RESOLVED -> REOPENED -> QUEUED
RANK_ENQUEUE: Final[int] = 9        # Initial classification: NEW -> QUEUED
RANK_CLOSE: Final[int] = 10         # Ticket closure at resolved_at + 72 hours
RANK_ESCALATION_WAKEUP: Final[int] = 11  # Wakeup for SLA escalation check

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

# Intent pending probabilities (business_rules.md 7.4)
INTENT_PENDING_PROB: Final[Mapping[str, float]] = {
    "TECH_TROUBLESHOOTING": 0.25,
    "REFUND_REQUEST": 0.20,
    "SECURITY_BREACH": 0.20,
    "ACCOUNT_ACCESS": 0.15,
}
DEFAULT_PENDING_PROB: Final[float] = 0.08

# Skill to Queue mapping (data_dictionary.md section 10)
SKILL_TO_QUEUE: Final[Mapping[str, str]] = {
    "SKL-ACCOUNT": "QUE-ACCOUNT",
    "SKL-BILLING": "QUE-BILLING",
    "SKL-GENERAL": "QUE-GENERAL",
    "SKL-SALES": "QUE-SALES",
    "SKL-TECH": "QUE-TECH",
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
class CycleDraws:
    total_work_sec: int
    classification_delay_sec: int
    base_pickups: list[float]
    mid_event_type: str  # "PENDING", "SKILL_REROUTE", "MANUAL_ESCALATION", "NONE"
    u_scaled: float
    reroute_target_draw: float
    pending_duration_sec: int
    pending_silent: bool
    reopen_prob_roll: float
    reopen_delay_u: float


@dataclass
class SimTicket:
    ticket_id: str
    ticket_number: int
    customer_id: str
    channel_id: str
    intent_id: str
    intent_code: str
    intent_median_handle: float
    initial_queue_id: str
    initial_skill_id: str
    language_code: str
    created_at_sec: int
    priority_level: str
    customer_segment: str
    is_critical_incident: bool
    incident_id: str

    # Simulation lifecycle state (ticket-level)
    current_status: str = "NEW"
    epoch: int = 0
    reopen_count: int = 0
    handover_count: int = 0
    status_seq: int = 0
    assignment_seq: int = 0
    current_assigned_at_sec: int = 0
    current_owner_id: str = ""
    final_owner_agent_id: str = ""
    first_response_at_sec: int | None = None
    resolved_at_sec: int | None = None
    resolved_by_agent_id: str = ""
    previous_owners: list[str] = field(default_factory=list)

    # Per-cycle lifecycle state
    cycle_no: int = 1
    cycle_start_sec: int = 0
    active_skill_id: str = ""
    active_queue_id: str = ""
    cycle_owners: set[str] = field(default_factory=set)
    cycle_first_response_at_sec: int | None = None
    cycle_resolved_at_sec: int | None = None
    cycle_resolved_by_agent_id: str = ""
    cycle_resolution_kind: str = ""
    cycle_transfer_count: int = 0
    cycle_escalation_count: int = 0
    cycle_pending_episodes: int = 0
    cycle_first_response_due_sec: int = 0
    cycle_resolution_due_sec: int = 0
    cycle_escalation_due_sec: int = 0
    cycle_sla_escalation_fired_at_sec: int | None = None
    cycle_sla_escalated: bool = False
    cycle_manual_escalated: bool = False
    cycle_sla_escalation_eligible: bool = True
    cycle_will_reopen: bool = False

    # Timing parameters & work
    first_response_due_sec: int = 0
    total_work_sec: int = 0
    remaining_work_sec: int = 0
    classification_delay_sec: int = 0
    draw_base_pickups: list[float] = field(default_factory=list)
    fallback_threshold_sec: int = 0

    # Waiting spell state
    language_waived: bool = False
    waiting_spell_start_sec: int = 0
    queued_at_sec: int = 0
    escalated_at_sec: int = 0
    escalated_reason_code: str = ""
    escalation_from_agent_id: str = ""
    queued_transfer_reason: str = "SHIFT_HANDOVER"

    # Mid-handling event state
    cycle_mid_event_type: str = "NONE"
    cycle_mid_event_executed: bool = False
    cycle_u_scaled: float = 0.0
    reroute_target_draw: float = 0.0
    cycle_pending_duration_sec: int = 0
    cycle_pending_silent: bool = False
    cycle_reopen_prob_roll: float = 0.0
    cycle_reopen_delay_u: float = 0.0

    # History of cycle outcomes
    cycle_outcomes: list[dict[str, Any]] = field(default_factory=list)


# =============================================================================
# Pre-drawn Randomness Helper
# =============================================================================

def _precompute_cycle_draws(
    seed: int,
    ticket_number: int,
    cycle_no: int,
    channel_id: str,
    intent_code: str,
    intent_median_handle: float,
    is_critical_incident: bool,
    priority_level: str,
    customer_segment: str,
    phase3b_enabled: bool,
) -> CycleDraws:
    """Pre-draw per-cycle timing and decision vector from ticket_rng.

    Cycle 1 uses module 'handle_times'.
    Cycle k > 1 uses module 'handle_times_c<k>'.
    Positions 0 to 7 are preserved from Phase 3a. Positions 8 to 16 are appended for Phase 3b.
    """
    module_name = "handle_times" if cycle_no == 1 else f"handle_times_c{cycle_no}"
    rng = ticket_rng(seed, ticket_number, module_name)

    # 1. Total work W (business_rules.md 7.3, 7.8(7))
    if is_critical_incident:
        median_eff = 15.0
    else:
        channel_factor = CHANNEL_WORK_FACTORS[channel_id]
        median_eff = float(intent_median_handle) * channel_factor

    if cycle_no > 1:
        # Reopen-cycle work median is 50% of the ticket's normal median (7.8(7))
        median_eff = median_eff * 0.5

    work_draw_min = rng.lognormal(mean=float(np.log(median_eff)), sigma=0.5)
    work_min = max(float(work_draw_min), 1.0)
    total_work_sec = round_half_up(work_min * 60.0)

    # 2. Pickup delay base draws for up to 6 assignments (business_rules.md 7.2)
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

    if not phase3b_enabled:
        return CycleDraws(
            total_work_sec=total_work_sec,
            classification_delay_sec=classification_delay_sec,
            base_pickups=base_pickups,
            mid_event_type="NONE",
            u_scaled=0.0,
            reroute_target_draw=0.0,
            pending_duration_sec=0,
            pending_silent=False,
            reopen_prob_roll=1.0,
            reopen_delay_u=0.0,
        )

    # Phase 3b appended draws (Positions 8 to 16)
    mid_pending_roll = float(rng.uniform(0.0, 1.0))
    mid_reroute_roll = float(rng.uniform(0.0, 1.0))
    mid_manual_roll = float(rng.uniform(0.0, 1.0))
    u_draw = float(rng.uniform(0.0, 1.0))
    reroute_target_draw = float(rng.uniform(0.0, 1.0))
    pending_duration_raw = float(rng.lognormal(mean=float(np.log(45.0)), sigma=0.7))
    pending_silent_roll = float(rng.uniform(0.0, 1.0))
    reopen_prob_roll = float(rng.uniform(0.0, 1.0))
    reopen_delay_u = float(rng.uniform(0.0, 1.0))

    # Evaluate mid-event selection (business_rules.md 7.8(3))
    # (a) PENDING with intent's pending probability
    pending_prob = INTENT_PENDING_PROB.get(intent_code, DEFAULT_PENDING_PROB)
    if mid_pending_roll < pending_prob:
        mid_event_type = "PENDING"
        u_scaled = 0.4 + 0.2 * u_draw
    else:
        # (b) SKILL_REROUTE with 4%, only if active skill is BILLING, TECH, ACCOUNT, GENERAL, prio != P1, no incident
        can_reroute = (
            priority_level != "P1"
            and not is_critical_incident
            and intent_code not in ("SECURITY_BREACH", "PRODUCT_SAFETY_HAZARD", "SALES_PRESALES")
        )
        if can_reroute and mid_reroute_roll < 0.04:
            mid_event_type = "SKILL_REROUTE"
            u_scaled = 0.2 + 0.6 * u_draw
        else:
            # (c) MANUAL_ESCALATION with 6% for ENTERPRISE and 2% for others
            manual_prob = 0.06 if customer_segment == "ENTERPRISE" else 0.02
            if mid_manual_roll < manual_prob:
                mid_event_type = "MANUAL_ESCALATION"
                u_scaled = 0.3 + 0.6 * u_draw
            else:
                mid_event_type = "NONE"
                u_scaled = 0.0

    pending_duration_sec = max(60, round_half_up(float(pending_duration_raw) * 60.0))
    pending_silent = (pending_silent_roll < 0.03)

    return CycleDraws(
        total_work_sec=total_work_sec,
        classification_delay_sec=classification_delay_sec,
        base_pickups=base_pickups,
        mid_event_type=mid_event_type,
        u_scaled=u_scaled,
        reroute_target_draw=reroute_target_draw,
        pending_duration_sec=pending_duration_sec,
        pending_silent=pending_silent,
        reopen_prob_roll=reopen_prob_roll,
        reopen_delay_u=reopen_delay_u,
    )


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

        # Reference tables
        self.priority_rules_df = master_tables["PriorityRule"]
        self.customer_segment: dict[str, str] = {
            str(row["customer_id"]): str(row["segment"])
            for _, row in master_tables["Customer"].iterrows()
        }

        # Event priority queue
        # Items: (timestamp_sec, rank, tie_breaker_id, event_kind, payload)
        self.event_queue: list[tuple[int, int, str, str, Any]] = []

        # Current set of agents on shift
        self.on_shift_agents: set[str] = set()

        # Waiting pools
        self.queued_tickets: dict[str, SimTicket] = {}     # ticket_id -> SimTicket
        self.escalated_tickets: dict[str, SimTicket] = {}  # ticket_id -> SimTicket
        self.handover_limbo: list[SimTicket] = []

        # Fast SLA escalation tracking (due_sec -> list of ticket_id)
        self.escalation_due_tickets: dict[int, list[str]] = {}
        self.pending_replied_tickets_at_t: list[str] = []

        # Load reference data
        self._init_master_data(master_tables)
        self._init_tickets(ticket_draft_df, master_tables["Intent"])

    def _init_master_data(self, tables: Mapping[str, pd.DataFrame]) -> None:
        """Parse master data tables into efficient lookup structures."""
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
            dow = cur_date.isoweekday()
            date_str = cur_date.strftime("%Y-%m-%d")

            for aid in tables["Agent"]["agent_id"]:
                matches = shift_rows_by_agent_dow.get((aid, dow), [])
                for shift_code, _, eff_from, eff_to in matches:
                    if date_str < eff_from:
                        continue
                    if eff_to and date_str > eff_to:
                        continue

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

    def _init_cycle_sla_and_draws(self, ticket: SimTicket, cycle_no: int, cycle_start_sec: int) -> None:
        """Initialize SLA targets, due dates, and random draws for a ticket cycle."""
        ticket.cycle_no = cycle_no
        ticket.cycle_start_sec = cycle_start_sec
        ticket.cycle_first_response_at_sec = None
        ticket.cycle_resolved_at_sec = None
        ticket.cycle_resolved_by_agent_id = ""
        ticket.cycle_resolution_kind = ""
        ticket.cycle_transfer_count = 0
        ticket.cycle_escalation_count = 0
        ticket.cycle_pending_episodes = 0
        ticket.cycle_sla_escalation_fired_at_sec = None
        ticket.cycle_sla_escalated = False
        ticket.cycle_manual_escalated = False
        ticket.cycle_sla_escalation_eligible = True
        ticket.cycle_will_reopen = False
        ticket.language_waived = False
        ticket.waiting_spell_start_sec = cycle_start_sec
        ticket.cycle_owners.clear()

        # Reset active skill and queue per 7.8(2)
        ticket.active_skill_id = ticket.initial_skill_id
        ticket.active_queue_id = ticket.initial_queue_id

        # SLA targets (business_rules.md 8.1-8.2, 7.8(7))
        cycle_dt = EPOCH_UTC + datetime.timedelta(seconds=cycle_start_sec)
        date_str = cycle_dt.strftime("%Y-%m-%d")

        # Find effective SLA_TARGET rule
        sla_targets = self.priority_rules_df[
            (self.priority_rules_df["rule_type"] == "SLA_TARGET")
            & (self.priority_rules_df["priority_level"] == ticket.priority_level)
            & (self.priority_rules_df["effective_from"] <= date_str)
            & (
                self.priority_rules_df["effective_to"].isna()
                | (self.priority_rules_df["effective_to"] == "")
                | (self.priority_rules_df["effective_to"] >= date_str)
            )
        ]
        rule = sla_targets.iloc[0]

        frt_min = int(rule["first_response_min"])
        res_min = int(rule["resolution_min"])
        esc_min = int(rule["escalation_after_min"])

        if cycle_no > 1:
            # Reopen cycle SLA targets: FRT and resolution are 50% rounded up; escalation is unchanged (7.7, 7.8(7))
            frt_min = int(np.ceil(frt_min * 0.5))
            res_min = int(np.ceil(res_min * 0.5))

        cal = "CAL-247" if ticket.priority_level == "P1" else f"CAL-{ticket.channel_id}"
        clock_start = sla_calendar.next_open(cal, cycle_dt)
        frt_due_dt = sla_calendar.add_business_minutes(cal, clock_start, frt_min)
        res_due_dt = sla_calendar.add_business_minutes(cal, clock_start, res_min)
        esc_due_dt = sla_calendar.add_business_minutes(cal, clock_start, esc_min)

        ticket.cycle_first_response_due_sec = _dt_to_sec(frt_due_dt)
        ticket.cycle_resolution_due_sec = _dt_to_sec(res_due_dt)
        ticket.cycle_escalation_due_sec = _dt_to_sec(esc_due_dt)
        ticket.first_response_due_sec = ticket.cycle_first_response_due_sec

        # Draw per-cycle random numbers
        draws = _precompute_cycle_draws(
            seed=self.config.seed,
            ticket_number=ticket.ticket_number,
            cycle_no=cycle_no,
            channel_id=ticket.channel_id,
            intent_code=ticket.intent_code,
            intent_median_handle=ticket.intent_median_handle,
            is_critical_incident=ticket.is_critical_incident,
            priority_level=ticket.priority_level,
            customer_segment=ticket.customer_segment,
            phase3b_enabled=self.config.phase3b_enabled,
        )

        ticket.total_work_sec = draws.total_work_sec
        ticket.remaining_work_sec = draws.total_work_sec
        ticket.classification_delay_sec = draws.classification_delay_sec
        ticket.draw_base_pickups = draws.base_pickups
        ticket.cycle_mid_event_type = draws.mid_event_type
        ticket.cycle_mid_event_executed = False
        ticket.cycle_u_scaled = draws.u_scaled
        ticket.reroute_target_draw = draws.reroute_target_draw
        ticket.cycle_pending_duration_sec = draws.pending_duration_sec
        ticket.cycle_pending_silent = draws.pending_silent
        ticket.cycle_reopen_prob_roll = draws.reopen_prob_roll
        ticket.cycle_reopen_delay_u = draws.reopen_delay_u

        # Schedule SLA escalation wakeup event
        if self.config.phase3b_enabled and ticket.cycle_escalation_due_sec <= self.cutoff_sec:
            self._schedule_event(
                ticket.cycle_escalation_due_sec,
                RANK_SHIFT_START,  # wake up engine at this timestamp
                ticket.ticket_id,
                "ESCALATION_CHECK_WAKEUP",
                (ticket.ticket_id, ticket.cycle_no),
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
            c_sec = _dt_to_sec(str(row["created_at"]))
            cust_id = str(row["customer_id"])

            meta = intents_meta[iid]
            req_skill = str(meta["required_skill_id"])
            intent_code = str(meta["intent_code"])
            intent_med = float(meta["median_handle_minutes"])
            is_crit = bool(row["is_critical_incident"])
            inc_id = str(row["incident_id"]) if pd.notna(row["incident_id"]) else ""
            cust_seg = self.customer_segment.get(cust_id, "CONSUMER")

            ticket = SimTicket(
                ticket_id=tid,
                ticket_number=t_num,
                customer_id=cust_id,
                channel_id=ch,
                intent_id=iid,
                intent_code=intent_code,
                intent_median_handle=intent_med,
                initial_queue_id=str(row["queue_id"]),
                initial_skill_id=req_skill,
                language_code=str(row["language_code"]),
                created_at_sec=c_sec,
                priority_level=prio,
                customer_segment=cust_seg,
                is_critical_incident=is_crit,
                incident_id=inc_id,
                fallback_threshold_sec=FALLBACK_THRESHOLDS_SEC[prio],
            )

            # Initialize cycle 1 SLA targets and draws
            self._init_cycle_sla_and_draws(ticket, cycle_no=1, cycle_start_sec=c_sec)
            self.tickets[tid] = ticket
            if self.config.phase3b_enabled:
                self._register_escalation_due(ticket)

    def _register_escalation_due(self, ticket: SimTicket) -> None:
        """Register ticket's cycle_escalation_due_sec for efficient lookup and wake-up."""
        due = ticket.cycle_escalation_due_sec
        if due <= self.cutoff_sec:
            if due not in self.escalation_due_tickets:
                self.escalation_due_tickets[due] = []
                self._schedule_event(due, RANK_ESCALATION_WAKEUP, "SLA_DUE", "ESCALATION_WAKEUP", due)
            self.escalation_due_tickets[due].append(ticket.ticket_id)

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
            "cycle_no": ticket.cycle_no,
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
        target_queue_id: str,
        target_skill_id: str,
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
            "cycle_no": ticket.cycle_no,
            "event_type": event_type,
            "reason_code": reason_code,
            "event_at": _sec_to_iso(event_at_sec),
            "agent_id": agent.agent_id,
            "from_agent_id": from_agent_id if from_agent_id else "",
            "queue_id": target_queue_id,
            "required_skill_id": target_skill_id,
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
        target_queue_id: str,
        target_skill_id: str,
        required_exact_tier: str | None = None,
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

        # H7: Agent has not already owned this ticket in the same cycle (7.8(2))
        if agent.agent_id in ticket.cycle_owners:
            return False, False, 0

        # Optional tier constraint for escalations (exact owner tier + 1 per 7.8(6))
        if required_exact_tier is not None and agent.tier != required_exact_tier:
            return False, False, 0

        # H3: Agent holds active skill on that date
        matching_skill = None
        for sk in agent.skills:
            if sk.skill_id == target_skill_id and sk.is_effective(date_str):
                matching_skill = sk
                break

        if matching_skill is None:
            return False, False, 0

        prof_score = PROFICIENCY_SCORES.get(matching_skill.proficiency, 0)

        # H3b: For P1 and P2 tickets, proficiency is PROFICIENT or EXPERT
        if ticket.priority_level in ("P1", "P2") and prof_score < 2:
            return False, False, 0

        # H6: Agent is QueueMember of target queue on that date, and tier >= queue.min_tier
        is_qm = any(q.queue_id == target_queue_id and q.is_effective(date_str) for q in agent.queues)
        if not is_qm:
            return False, False, 0

        min_tier = self.queue_min_tier[target_queue_id]
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
        """Compute scoring tuple for eligible agent (business_rules.md 6.2, 6.3)."""
        date_str = _sec_to_date_str(event_at_sec)
        daily_cnt = self.daily_assignments.get(date_str, {}).get(agent.agent_id, 0)
        norm_load = agent.current_load / float(agent.concurrency_limit)
        lang_rank = 0 if lang_match else 1

        return (lang_rank, norm_load, -prof_score, daily_cnt, agent.agent_id)

    def _dispatch_ticket(
        self,
        ticket: SimTicket,
        event_at_sec: int,
        source_pool: str,  # "limbo", "escalated", or "queued"
    ) -> bool:
        """Attempt to dispatch a waiting ticket to an eligible agent."""
        if source_pool == "escalated":
            # Escalated ticket assignment
            target_queue_id = "QUE-ESCALATION"
            target_skill_id = ticket.active_skill_id
            from_agent = self.agents[ticket.escalation_from_agent_id]
            required_tier = "T2" if from_agent.tier == "T1" else "T3"
        else:
            # Normal queue or handover limbo
            target_queue_id = ticket.active_queue_id
            target_skill_id = ticket.active_skill_id
            required_tier = None

        best_agent: AgentState | None = None
        best_score: tuple[int, float, int, int, str] | None = None
        best_lang_match = False

        for aid in sorted(self.on_shift_agents):
            agent = self.agents[aid]
            if agent.current_load >= agent.concurrency_limit:
                continue

            ok, lang_match, prof_score = self._check_eligibility(
                agent=agent,
                ticket=ticket,
                event_at_sec=event_at_sec,
                target_queue_id=target_queue_id,
                target_skill_id=target_skill_id,
                required_exact_tier=required_tier,
            )
            if not ok:
                continue

            score = self._score_agent(agent, lang_match, prof_score, event_at_sec)
            if best_score is None or score < best_score:
                best_score = score
                best_agent = agent
                best_lang_match = lang_match

        if best_agent is None:
            return False

        # Determine event_type, reason_code, from_agent_id, wait_minutes
        if source_pool == "escalated":
            event_type = "ESCALATE"
            reason_code = ticket.escalated_reason_code
            from_agent_id = ticket.escalation_from_agent_id
            ticket.cycle_escalation_count += 1
            wait_min = round((event_at_sec - ticket.escalated_at_sec) / 60.0, 1)
        elif source_pool == "limbo":
            event_type = "TRANSFER"
            reason_code = ticket.queued_transfer_reason
            from_agent_id = ticket.previous_owners[-1]
            ticket.handover_count += 1
            ticket.cycle_transfer_count += 1
            wait_min = 0.0
        else:  # "queued"
            if ticket.previous_owners:
                if ticket.cycle_no > 1 and len(ticket.cycle_owners) == 0:
                    # First assignment of cycle k > 1 is REOPEN_ASSIGN (business_rules.md 7.8(1))
                    event_type = "REOPEN_ASSIGN"
                    reason_code = "REOPEN_ROUTING"
                    from_agent_id = ""
                    wait_min = round((event_at_sec - ticket.queued_at_sec) / 60.0, 1)
                else:
                    event_type = "TRANSFER"
                    reason_code = ticket.queued_transfer_reason
                    from_agent_id = ticket.previous_owners[-1]
                    ticket.handover_count += 1
                    ticket.cycle_transfer_count += 1
                    wait_min = round((event_at_sec - ticket.queued_at_sec) / 60.0, 1)
            else:
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
        ticket.cycle_owners.add(best_agent.agent_id)
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
            target_queue_id=target_queue_id,
            target_skill_id=target_skill_id,
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

        self._schedule_event(pickup_at, RANK_PICKUP, ticket.ticket_id, "PICKUP", (ticket.ticket_id, ticket.epoch))
        self._schedule_event(resolved_at, RANK_RESOLVE, ticket.ticket_id, "RESOLVE", (ticket.ticket_id, ticket.epoch))

        # Plan mid-handling event if Phase 3b enabled (business_rules.md 7.8(3))
        if self.config.phase3b_enabled and ticket.cycle_mid_event_type != "NONE" and not ticket.cycle_mid_event_executed:
            planned_at = max(
                pickup_at + 60,
                round_half_up(event_at_sec + ticket.cycle_u_scaled * ticket.remaining_work_sec),
            )
            if planned_at < resolved_at:
                if ticket.cycle_mid_event_type == "PENDING":
                    # Check owner shift end
                    agent_shift_end = self._get_agent_shift_end(best_agent, event_at_sec)
                    if agent_shift_end - planned_at >= 600:
                        self._schedule_event(
                            planned_at,
                            RANK_MID_EVENT,
                            ticket.ticket_id,
                            "MID_EVENT",
                            (ticket.ticket_id, ticket.epoch, "PENDING", agent_shift_end),
                        )
                elif ticket.cycle_mid_event_type == "SKILL_REROUTE":
                    self._schedule_event(
                        planned_at,
                        RANK_MID_EVENT,
                        ticket.ticket_id,
                        "MID_EVENT",
                        (ticket.ticket_id, ticket.epoch, "SKILL_REROUTE", None),
                    )
                elif ticket.cycle_mid_event_type == "MANUAL_ESCALATION":
                    if best_agent.tier == "T3":
                        # Consumed with no effect
                        ticket.cycle_mid_event_executed = True
                    else:
                        self._schedule_event(
                            planned_at,
                            RANK_MID_EVENT,
                            ticket.ticket_id,
                            "MID_EVENT",
                            (ticket.ticket_id, ticket.epoch, "MANUAL_ESCALATION", None),
                        )

        return True

    def _get_agent_shift_end(self, agent: AgentState, current_t: int) -> int:
        """Return the end time of the agent shift active at current_t."""
        for s_sec, e_sec in agent.shifts:
            if s_sec <= current_t < e_sec:
                return e_sec
        return current_t + 8 * 3600

    def _run_dispatch_pass(self, event_at_sec: int) -> bool:
        """Run one dispatch pass over waiting tickets (business_rules.md 6.6, 7.8)."""
        candidates: list[tuple[int, int, str, SimTicket, str]] = []

        for tkt in self.handover_limbo:
            prio_rank = PRIORITY_RANKS[tkt.priority_level]
            candidates.append((prio_rank, tkt.first_response_due_sec, tkt.ticket_id, tkt, "limbo"))

        for tid, tkt in self.escalated_tickets.items():
            prio_rank = PRIORITY_RANKS[tkt.priority_level]
            candidates.append((prio_rank, tkt.first_response_due_sec, tid, tkt, "escalated"))

        for tid, tkt in self.queued_tickets.items():
            prio_rank = PRIORITY_RANKS[tkt.priority_level]
            candidates.append((prio_rank, tkt.first_response_due_sec, tid, tkt, "queued"))

        candidates.sort(key=lambda x: (x[0], x[1], x[2]))

        dispatched_limbo: set[str] = set()
        dispatched_escalated: set[str] = set()
        dispatched_queued: set[str] = set()
        any_dispatched = False

        for _, _, tid, tkt, source_pool in candidates:
            if not any(self.agents[aid].current_load < self.agents[aid].concurrency_limit for aid in self.on_shift_agents):
                break

            success = self._dispatch_ticket(tkt, event_at_sec, source_pool)
            if success:
                any_dispatched = True
                if source_pool == "limbo":
                    dispatched_limbo.add(tid)
                elif source_pool == "escalated":
                    dispatched_escalated.add(tid)
                else:
                    dispatched_queued.add(tid)

        if dispatched_queued:
            for tid in dispatched_queued:
                del self.queued_tickets[tid]

        if dispatched_escalated:
            for tid in dispatched_escalated:
                del self.escalated_tickets[tid]

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
                tkt.waiting_spell_start_sec = event_at_sec
                self.queued_tickets[tkt.ticket_id] = tkt

                if not tkt.language_waived and tkt.language_code != "en":
                    due_sec = event_at_sec + tkt.fallback_threshold_sec
                    self._schedule_event(
                        due_sec,
                        RANK_FALLBACK_DUE,
                        tkt.ticket_id,
                        "FALLBACK_DUE",
                        (tkt.ticket_id, tkt.cycle_no, event_at_sec),
                    )

            self.handover_limbo.clear()

        return any_dispatched

    def _check_escalations(self, current_t: int) -> bool:
        """Evaluate SLA escalations due at current_t (business_rules.md 7.8(6))."""
        due_tids = self.escalation_due_tickets.get(current_t)
        if not due_tids and not self.pending_replied_tickets_at_t:
            return False

        tids_to_check: set[str] = set()
        if due_tids:
            tids_to_check.update(due_tids)
        if self.pending_replied_tickets_at_t:
            for tid in self.pending_replied_tickets_at_t:
                tkt = self.tickets[tid]
                if tkt.cycle_escalation_due_sec <= current_t:
                    tids_to_check.add(tid)

        any_escalated = False
        for tid in sorted(tids_to_check):
            tkt = self.tickets[tid]
            if (
                tkt.cycle_escalation_due_sec <= current_t
                and not tkt.cycle_sla_escalated
                and tkt.cycle_sla_escalation_eligible
                and tkt.cycle_resolved_at_sec is None
            ):
                if tkt.current_status == "PENDING_CUSTOMER":
                    # Evaluated right after customer reply (7.8(6))
                    continue
                elif tkt.current_status in ("QUEUED", "ESCALATED") or tkt in self.handover_limbo:
                    # Unassigned after dispatch: never escalates in this cycle
                    tkt.cycle_sla_escalation_eligible = False
                    continue
                elif tkt.current_status in ("ASSIGNED", "IN_PROGRESS"):
                    owner = self.agents[tkt.current_owner_id]
                    if owner.tier in ("T1", "T2"):
                        # Execute SLA escalation
                        self._execute_sla_escalation(tkt, current_t)
                        any_escalated = True
                    else:
                        # Owner is T3; cannot escalate to T4
                        tkt.cycle_sla_escalation_eligible = False

        return any_escalated

    def _execute_sla_escalation(self, ticket: SimTicket, event_at_sec: int) -> None:
        """Transition ticket to ESCALATED from SLA timeout."""
        ticket.epoch += 1
        owner = self.agents[ticket.current_owner_id]

        if ticket.current_status == "IN_PROGRESS":
            accrued = event_at_sec - ticket.current_assigned_at_sec
            ticket.remaining_work_sec = max(0, ticket.remaining_work_sec - accrued)

        owner.current_load -= 1
        if ticket.ticket_id in owner.owned_tickets:
            owner.owned_tickets.remove(ticket.ticket_id)

        from_agent_id = owner.agent_id
        ticket.escalation_from_agent_id = from_agent_id
        ticket.escalated_reason_code = "SLA_ESCALATION"
        ticket.escalated_at_sec = event_at_sec
        ticket.waiting_spell_start_sec = event_at_sec
        ticket.current_owner_id = ""

        ticket.cycle_sla_escalated = True
        ticket.cycle_sla_escalation_fired_at_sec = event_at_sec

        from_status = ticket.current_status
        self._emit_status_event(
            ticket=ticket,
            from_status=from_status,
            to_status="ESCALATED",
            event_at_sec=event_at_sec,
            actor_type="SYSTEM",
            actor_agent_id="",
            trigger_ref="",
        )

        self.escalated_tickets[ticket.ticket_id] = ticket

        if not ticket.language_waived and ticket.language_code != "en":
            due_sec = event_at_sec + ticket.fallback_threshold_sec
            self._schedule_event(
                due_sec,
                RANK_FALLBACK_DUE,
                ticket.ticket_id,
                "FALLBACK_DUE",
                (ticket.ticket_id, ticket.cycle_no, event_at_sec),
            )

    def _plan_reopen_if_applicable(self, ticket: SimTicket, resolved_at_sec: int) -> None:
        """Evaluate reopen probability and schedule REOPEN if triggered (7.7, 7.8(7))."""
        if ticket.cycle_no >= 3:
            return

        res_breached = 1 if (resolved_at_sec > ticket.cycle_resolution_due_sec) else 0
        transfers = min(ticket.cycle_transfer_count, 3)
        escalated = 1 if (ticket.cycle_escalation_count > 0) else 0

        # Reopen probability formula: 0.04 + 0.03 x res_breached + 0.015 x min(transfer, 3) + 0.02 x escalated
        reopen_prob = 0.04 + 0.03 * res_breached + 0.015 * transfers + 0.02 * escalated

        if ticket.cycle_reopen_prob_roll < reopen_prob:
            # Lognormal median 6h, sigma 0.8 truncated to (0, 72h]
            dist = statistics.NormalDist(mu=float(np.log(6.0)), sigma=0.8)
            cdf_72 = dist.cdf(float(np.log(72.0)))
            u = max(1e-9, min(1.0 - 1e-9, ticket.cycle_reopen_delay_u))
            y = dist.inv_cdf(u * cdf_72)
            delay_sec = max(1, min(72 * 3600, round_half_up(float(np.exp(y)) * 3600.0)))

            reopen_at_sec = resolved_at_sec + delay_sec
            if reopen_at_sec <= self.cutoff_sec:
                ticket.cycle_will_reopen = True
                self._schedule_event(
                    reopen_at_sec,
                    RANK_REOPEN,
                    ticket.ticket_id,
                    "REOPEN",
                    (ticket.ticket_id, ticket.cycle_no),
                )

    def _record_cycle_outcome(self, ticket: SimTicket) -> None:
        """Store the outcome record for the current cycle."""
        ticket.cycle_outcomes.append({
            "ticket_id": ticket.ticket_id,
            "cycle_no": ticket.cycle_no,
            "cycle_start_at": _sec_to_iso(ticket.cycle_start_sec),
            "first_response_at": _sec_to_iso(ticket.cycle_first_response_at_sec) if ticket.cycle_first_response_at_sec else "",
            "resolved_at": _sec_to_iso(ticket.cycle_resolved_at_sec) if ticket.cycle_resolved_at_sec else "",
            "resolved_by_agent_id": ticket.cycle_resolved_by_agent_id,
            "resolution_kind": ticket.cycle_resolution_kind,
            "transfer_count": ticket.cycle_transfer_count,
            "escalation_count": ticket.cycle_escalation_count,
            "pending_episodes": ticket.cycle_pending_episodes,
            "escalation_due_at": _sec_to_iso(ticket.cycle_escalation_due_sec),
            "sla_escalation_fired_at": _sec_to_iso(ticket.cycle_sla_escalation_fired_at_sec) if ticket.cycle_sla_escalation_fired_at_sec else "",
            "mid_event_type": ticket.cycle_mid_event_type,
        })

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

            enq_sec = c_sec + ticket.classification_delay_sec
            self._schedule_event(enq_sec, RANK_ENQUEUE, tid, "ENQUEUE", tid)

        # 3. Main event loop
        while self.event_queue:
            current_t = self.event_queue[0][0]
            if current_t > self.cutoff_sec:
                break

            self.pending_replied_tickets_at_t.clear()

            events_at_t: list[tuple[int, int, str, str, Any]] = []
            while self.event_queue and self.event_queue[0][0] == current_t:
                events_at_t.append(heapq.heappop(self.event_queue))

            for t_sec, rank, tie_id, kind, payload in events_at_t:
                if rank == RANK_RESOLVE:
                    tid, epoch = payload
                    tkt = self.tickets[tid]
                    if tkt.epoch == epoch and tkt.current_status == "IN_PROGRESS":
                        owner_id = tkt.current_owner_id
                        agent = self.agents[owner_id]
                        agent.current_load -= 1
                        if tid in agent.owned_tickets:
                            agent.owned_tickets.remove(tid)

                        tkt.resolved_at_sec = t_sec
                        tkt.resolved_by_agent_id = owner_id
                        tkt.cycle_resolved_at_sec = t_sec
                        tkt.cycle_resolved_by_agent_id = owner_id
                        tkt.cycle_resolution_kind = "NORMAL"

                        self._emit_status_event(
                            ticket=tkt,
                            from_status="IN_PROGRESS",
                            to_status="RESOLVED",
                            event_at_sec=t_sec,
                            actor_type="AGENT",
                            actor_agent_id=owner_id,
                            trigger_ref="",
                        )

                        if self.config.phase3b_enabled:
                            self._plan_reopen_if_applicable(tkt, t_sec)

                        # Schedule CLOSE at resolved_at + 72h
                        close_at = t_sec + 72 * 3600
                        self._schedule_event(close_at, RANK_CLOSE, tid, "CLOSE", (tid, tkt.cycle_no))

                elif rank == RANK_SHIFT_END:
                    aid = payload
                    self.on_shift_agents.discard(aid)
                    agent = self.agents[aid]

                    owned_tids = sorted(agent.owned_tickets)
                    for otid in owned_tids:
                        tkt = self.tickets[otid]
                        accrued = t_sec - tkt.current_assigned_at_sec
                        tkt.remaining_work_sec = max(0, tkt.remaining_work_sec - accrued)
                        tkt.current_owner_id = ""
                        tkt.queued_transfer_reason = "SHIFT_HANDOVER"
                        self.handover_limbo.append(tkt)

                    agent.owned_tickets.clear()
                    agent.current_load = 0

                elif rank == RANK_SHIFT_START:
                    if kind == "SHIFT_START":
                        aid = payload
                        self.on_shift_agents.add(aid)

                elif rank == RANK_PENDING_REPLY:
                    tid, epoch, action = payload
                    tkt = self.tickets[tid]
                    if tkt.epoch == epoch and tkt.current_status == "PENDING_CUSTOMER":
                        owner_id = tkt.current_owner_id
                        owner = self.agents[owner_id]

                        if action == "SILENT":
                            # Customer silent: PENDING_CUSTOMER -> RESOLVED (actor AGENT, NO_CUSTOMER_RESPONSE)
                            owner.current_load -= 1
                            if tid in owner.owned_tickets:
                                owner.owned_tickets.remove(tid)

                            tkt.resolved_at_sec = t_sec
                            tkt.resolved_by_agent_id = owner_id
                            tkt.cycle_resolved_at_sec = t_sec
                            tkt.cycle_resolved_by_agent_id = owner_id
                            tkt.cycle_resolution_kind = "NO_CUSTOMER_RESPONSE"

                            self._emit_status_event(
                                ticket=tkt,
                                from_status="PENDING_CUSTOMER",
                                to_status="RESOLVED",
                                event_at_sec=t_sec,
                                actor_type="AGENT",
                                actor_agent_id=owner_id,
                                trigger_ref="",
                            )

                            self._plan_reopen_if_applicable(tkt, t_sec)

                            close_at = t_sec + 72 * 3600
                            self._schedule_event(close_at, RANK_CLOSE, tid, "CLOSE", (tid, tkt.cycle_no))

                        else:  # "REPLY"
                            # Customer replies: PENDING_CUSTOMER -> IN_PROGRESS (actor CUSTOMER)
                            self._emit_status_event(
                                ticket=tkt,
                                from_status="PENDING_CUSTOMER",
                                to_status="IN_PROGRESS",
                                event_at_sec=t_sec,
                                actor_type="CUSTOMER",
                                actor_agent_id="",
                                trigger_ref="",
                            )

                            tkt.current_assigned_at_sec = t_sec
                            resolved_at = max(t_sec + tkt.remaining_work_sec, t_sec + 60)
                            self._schedule_event(resolved_at, RANK_RESOLVE, tid, "RESOLVE", (tid, tkt.epoch))
                            if self.config.phase3b_enabled:
                                self.pending_replied_tickets_at_t.append(tid)

                elif rank == RANK_FALLBACK_DUE:
                    tid, cycle_no, spell_start = payload
                    tkt = self.tickets[tid]
                    if (
                        tkt.cycle_no == cycle_no
                        and tkt.current_status in ("QUEUED", "ESCALATED")
                        and tkt.waiting_spell_start_sec == spell_start
                    ):
                        tkt.language_waived = True

                elif rank == RANK_PICKUP:
                    tid, epoch = payload
                    tkt = self.tickets[tid]
                    if tkt.epoch == epoch and tkt.current_status == "ASSIGNED":
                        if tkt.first_response_at_sec is None:
                            tkt.first_response_at_sec = t_sec
                        if tkt.cycle_first_response_at_sec is None:
                            tkt.cycle_first_response_at_sec = t_sec

                        self._emit_status_event(
                            ticket=tkt,
                            from_status="ASSIGNED",
                            to_status="IN_PROGRESS",
                            event_at_sec=t_sec,
                            actor_type="AGENT",
                            actor_agent_id=tkt.current_owner_id,
                            trigger_ref="",
                        )

                elif rank == RANK_MID_EVENT:
                    tid, epoch, mid_kind, extra = payload
                    tkt = self.tickets[tid]
                    if tkt.epoch == epoch and tkt.current_status == "IN_PROGRESS" and not tkt.cycle_mid_event_executed:
                        owner_id = tkt.current_owner_id
                        owner = self.agents[owner_id]

                        if mid_kind == "PENDING":
                            agent_shift_end = extra
                            tkt.cycle_mid_event_executed = True
                            tkt.cycle_pending_episodes += 1

                            accrued = t_sec - tkt.current_assigned_at_sec
                            tkt.remaining_work_sec = max(0, tkt.remaining_work_sec - accrued)

                            self._emit_status_event(
                                ticket=tkt,
                                from_status="IN_PROGRESS",
                                to_status="PENDING_CUSTOMER",
                                event_at_sec=t_sec,
                                actor_type="AGENT",
                                actor_agent_id=owner_id,
                                trigger_ref="",
                            )

                            reply_at = min(t_sec + tkt.cycle_pending_duration_sec, agent_shift_end - 60)
                            action = "SILENT" if tkt.cycle_pending_silent else "REPLY"
                            self._schedule_event(reply_at, RANK_PENDING_REPLY, tid, "PENDING_REPLY", (tid, tkt.epoch, action))

                        elif mid_kind == "SKILL_REROUTE":
                            tkt.cycle_mid_event_executed = True
                            accrued = t_sec - tkt.current_assigned_at_sec
                            tkt.remaining_work_sec = max(0, tkt.remaining_work_sec - accrued)

                            # Determine reroute target skill & queue (7.8(3))
                            if tkt.active_skill_id == "SKL-BILLING":
                                target_sk = "SKL-ACCOUNT"
                            elif tkt.active_skill_id == "SKL-TECH":
                                target_sk = "SKL-ACCOUNT"
                            elif tkt.active_skill_id == "SKL-ACCOUNT":
                                target_sk = "SKL-BILLING" if tkt.reroute_target_draw < 0.5 else "SKL-TECH"
                            else:  # SKL-GENERAL
                                if tkt.reroute_target_draw < 0.25:
                                    target_sk = "SKL-BILLING"
                                elif tkt.reroute_target_draw < 0.50:
                                    target_sk = "SKL-TECH"
                                elif tkt.reroute_target_draw < 0.75:
                                    target_sk = "SKL-ACCOUNT"
                                else:
                                    target_sk = "SKL-SALES"

                            tkt.active_skill_id = target_sk
                            tkt.active_queue_id = SKILL_TO_QUEUE[target_sk]
                            tkt.queued_transfer_reason = "SKILL_REROUTE"

                            owner.current_load -= 1
                            if tid in owner.owned_tickets:
                                owner.owned_tickets.remove(tid)
                            tkt.current_owner_id = ""

                            self.handover_limbo.append(tkt)

                        elif mid_kind == "MANUAL_ESCALATION":
                            tkt.cycle_mid_event_executed = True
                            if owner.tier in ("T1", "T2"):
                                tkt.epoch += 1
                                accrued = t_sec - tkt.current_assigned_at_sec
                                tkt.remaining_work_sec = max(0, tkt.remaining_work_sec - accrued)

                                owner.current_load -= 1
                                if tid in owner.owned_tickets:
                                    owner.owned_tickets.remove(tid)

                                from_agent_id = owner.agent_id
                                tkt.escalation_from_agent_id = from_agent_id
                                tkt.escalated_reason_code = "MANUAL_ESCALATION"
                                tkt.escalated_at_sec = t_sec
                                tkt.waiting_spell_start_sec = t_sec
                                tkt.current_owner_id = ""

                                tkt.cycle_manual_escalated = True

                                self._emit_status_event(
                                    ticket=tkt,
                                    from_status="IN_PROGRESS",
                                    to_status="ESCALATED",
                                    event_at_sec=t_sec,
                                    actor_type="AGENT",
                                    actor_agent_id=from_agent_id,
                                    trigger_ref="",
                                )

                                self.escalated_tickets[tid] = tkt

                                if not tkt.language_waived and tkt.language_code != "en":
                                    due_sec = t_sec + tkt.fallback_threshold_sec
                                    self._schedule_event(
                                        due_sec,
                                        RANK_FALLBACK_DUE,
                                        tkt.ticket_id,
                                        "FALLBACK_DUE",
                                        (tkt.ticket_id, tkt.cycle_no, t_sec),
                                    )

                elif rank == RANK_REOPEN:
                    tid, old_cycle_no = payload
                    tkt = self.tickets[tid]
                    if tkt.cycle_no == old_cycle_no and tkt.current_status == "RESOLVED":
                        # Finalize previous cycle outcome before incrementing cycle_no
                        self._record_cycle_outcome(tkt)

                        tkt.epoch += 1
                        tkt.reopen_count += 1
                        new_cycle_no = old_cycle_no + 1

                        # Initialize new cycle state and draws
                        self._init_cycle_sla_and_draws(tkt, cycle_no=new_cycle_no, cycle_start_sec=t_sec)
                        if self.config.phase3b_enabled:
                            self._register_escalation_due(tkt)

                        # RESOLVED -> REOPENED (actor CUSTOMER)
                        self._emit_status_event(
                            ticket=tkt,
                            from_status="RESOLVED",
                            to_status="REOPENED",
                            event_at_sec=t_sec,
                            actor_type="CUSTOMER",
                            actor_agent_id="",
                            trigger_ref="",
                        )

                        # REOPENED -> QUEUED (actor SYSTEM)
                        self._emit_status_event(
                            ticket=tkt,
                            from_status="REOPENED",
                            to_status="QUEUED",
                            event_at_sec=t_sec,
                            actor_type="SYSTEM",
                            actor_agent_id="",
                            trigger_ref="",
                        )

                        tkt.queued_at_sec = t_sec
                        tkt.waiting_spell_start_sec = t_sec
                        self.queued_tickets[tkt.ticket_id] = tkt

                        if not tkt.language_waived and tkt.language_code != "en":
                            due_sec = t_sec + tkt.fallback_threshold_sec
                            self._schedule_event(
                                due_sec,
                                RANK_FALLBACK_DUE,
                                tkt.ticket_id,
                                "FALLBACK_DUE",
                                (tkt.ticket_id, tkt.cycle_no, t_sec),
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
                    tkt.waiting_spell_start_sec = t_sec
                    self.queued_tickets[tid] = tkt

                    if not tkt.language_waived and tkt.language_code != "en":
                        due_sec = t_sec + tkt.fallback_threshold_sec
                        self._schedule_event(
                            due_sec,
                            RANK_FALLBACK_DUE,
                            tid,
                            "FALLBACK_DUE",
                            (tid, tkt.cycle_no, t_sec),
                        )

                elif rank == RANK_CLOSE:
                    tid, close_cycle_no = payload
                    tkt = self.tickets[tid]
                    if (
                        t_sec <= self.cutoff_sec
                        and tkt.current_status == "RESOLVED"
                        and tkt.cycle_no == close_cycle_no
                        and not tkt.cycle_will_reopen
                    ):
                        self._emit_status_event(
                            ticket=tkt,
                            from_status="RESOLVED",
                            to_status="CLOSED",
                            event_at_sec=t_sec,
                            actor_type="SYSTEM",
                            actor_agent_id="",
                            trigger_ref="",
                        )

                elif rank == RANK_ESCALATION_WAKEUP:
                    pass

            # Repeat DISPATCH and ESCALATION_DUE at current_t until no change (7.8(8))
            while True:
                dispatched = self._run_dispatch_pass(current_t)
                escalated = self._check_escalations(current_t) if self.config.phase3b_enabled else False
                if not dispatched and not escalated:
                    break

            if self.config.phase3b_enabled:
                self.escalation_due_tickets.pop(current_t, None)

        # Finalize outcome records for the final cycle of every ticket
        for tid in sorted(self.tickets.keys()):
            tkt = self.tickets[tid]
            self._record_cycle_outcome(tkt)


# =============================================================================
# Outcome Generation and Exporters
# =============================================================================

def simulate_routing(
    config: Config,
    master_tables: Mapping[str, pd.DataFrame],
    ticket_draft_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run discrete-event routing simulation for Phase 3a / 3b.

    If config.phase3b_enabled is False:
        returns (status_event_df, assignment_event_df, ticket_outcome_df)
    If config.phase3b_enabled is True:
        returns (status_event_df, assignment_event_df, ticket_outcome_df, ticket_cycle_outcome_df)
    """
    engine = SimulationEngine(config, master_tables, ticket_draft_df)
    engine.run()

    status_event_df = pd.DataFrame(engine.status_event_rows)
    assignment_event_df = pd.DataFrame(engine.assignment_event_rows)

    outcome_rows: list[dict[str, Any]] = []
    cycle_outcome_rows: list[dict[str, Any]] = []

    for tid in sorted(engine.tickets.keys()):
        tkt = engine.tickets[tid]
        row: dict[str, Any] = {
            "ticket_id": tkt.ticket_id,
            "first_response_at": _sec_to_iso(tkt.first_response_at_sec) if tkt.first_response_at_sec else "",
            "resolved_at": _sec_to_iso(tkt.resolved_at_sec) if tkt.resolved_at_sec else "",
            "resolved_by_agent_id": tkt.resolved_by_agent_id,
            "final_owner_agent_id": tkt.final_owner_agent_id,
            "status_at_cutoff": tkt.current_status,
            "handover_count": tkt.handover_count,
        }
        if config.phase3b_enabled:
            row["reopen_count"] = tkt.reopen_count
            cycle_outcome_rows.extend(tkt.cycle_outcomes)

        outcome_rows.append(row)

    ticket_outcome_df = pd.DataFrame(outcome_rows)

    if config.phase3b_enabled:
        ticket_cycle_outcome_df = pd.DataFrame(cycle_outcome_rows)
        return status_event_df, assignment_event_df, ticket_outcome_df, ticket_cycle_outcome_df
    else:
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


def export_ticket_cycle_outcomes(df: pd.DataFrame, out_path: str | Path) -> Path:
    """Export ticket cycle outcomes DataFrame to CSV (data/staging/ticket_cycle_outcome_draft.csv)."""
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
    cycle_outcome_df: pd.DataFrame | None = None,
) -> str:
    """Generate comprehensive Phase 3a / 3b KPI report."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("                  PHASE 3 KPI SIMULATION REPORT                  ")
    lines.append("=" * 72)

    total_tickets = len(outcome_df)
    assigned_cnt = (outcome_df["final_owner_agent_id"] != "").sum()
    resolved_cnt = (outcome_df["resolved_at"] != "").sum()
    closed_cnt = (outcome_df["status_at_cutoff"] == "CLOSED").sum()
    resolved_at_cutoff = (outcome_df["status_at_cutoff"] == "RESOLVED").sum()

    open_statuses = {"NEW", "QUEUED", "ASSIGNED", "IN_PROGRESS", "PENDING_CUSTOMER", "ESCALATED"}
    open_cnt = outcome_df["status_at_cutoff"].isin(open_statuses).sum()

    lines.append(f"\n1. Ticket Volume at Cutoff:")
    lines.append(f"   - Total Tickets       : {total_tickets:6d}")
    lines.append(f"   - Assigned Tickets    : {assigned_cnt:6d} ({assigned_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"   - Resolved Tickets    : {resolved_cnt:6d} ({resolved_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"   - Closed Tickets      : {closed_cnt:6d} ({closed_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"   - Resolved at Cutoff  : {resolved_at_cutoff:6d} ({resolved_at_cutoff / total_tickets * 100:.1f}%)")
    lines.append(f"   - Open at Cutoff      : {open_cnt:6d} ({open_cnt / total_tickets * 100:.1f}%)")
    lines.append(f"     Status Breakdown    : {outcome_df['status_at_cutoff'].value_counts().to_dict()}")

    if cycle_outcome_df is not None and len(cycle_outcome_df) > 0:
        total_cycles = len(cycle_outcome_df)
        mid_counts = cycle_outcome_df["mid_event_type"].value_counts().to_dict()
        lines.append(f"\n2. Mid-Handling Events by Cycle (N={total_cycles}):")
        for m_type in ["PENDING", "SKILL_REROUTE", "MANUAL_ESCALATION", "NONE"]:
            cnt = mid_counts.get(m_type, 0)
            lines.append(f"   - {m_type:<18}: {cnt:5d} ({cnt / total_cycles * 100:.2f}%)")

        total_pending = cycle_outcome_df["pending_episodes"].sum()
        silent_cnt = (cycle_outcome_df["resolution_kind"] == "NO_CUSTOMER_RESPONSE").sum()
        silent_pct = (silent_cnt / total_pending * 100) if total_pending > 0 else 0.0
        lines.append(f"\n3. Customer Pending & Silence:")
        lines.append(f"   - Total Pending Episodes: {total_pending}")
        lines.append(f"   - Silent Customers      : {silent_cnt} ({silent_pct:.2f}% of pending episodes)")

        total_sla_esc = (cycle_outcome_df["sla_escalation_fired_at"] != "").sum()
        total_man_esc = (cycle_outcome_df["mid_event_type"] == "MANUAL_ESCALATION").sum()
        lines.append(f"\n4. Escalations:")
        lines.append(f"   - SLA Escalations       : {total_sla_esc} ({total_sla_esc / total_cycles * 100:.2f}% of cycles)")
        lines.append(f"   - Manual Escalations    : {total_man_esc} ({total_man_esc / total_cycles * 100:.2f}% of cycles)")

        if "reopen_count" in outcome_df.columns:
            reopen_dist = outcome_df["reopen_count"].value_counts().to_dict()
            reopened_tickets = (outcome_df["reopen_count"] >= 1).sum()
            lines.append(f"\n5. Reopens:")
            lines.append(f"   - Reopened Tickets (>=1): {reopened_tickets:5d} ({reopened_tickets / total_tickets * 100:.2f}%)")
            for r_cnt in [0, 1, 2]:
                cnt = reopen_dist.get(r_cnt, 0)
                lines.append(f"     * {r_cnt} Reopens        : {cnt:5d} ({cnt / total_tickets * 100:.2f}%)")

    # Wait minutes by priority
    merged_asn = assignment_df.merge(
        ticket_draft_df[["ticket_id", "priority_level", "channel_id"]],
        on="ticket_id",
        how="left",
    )
    merged_asn["wait_min_num"] = pd.to_numeric(merged_asn["wait_minutes"], errors="coerce").fillna(0.0)

    lines.append(f"\n6. Wait Minutes by Priority (from AssignmentEvents):")
    for prio in ["P1", "P2", "P3", "P4"]:
        sub = merged_asn[merged_asn["priority_level"] == prio]["wait_min_num"]
        if len(sub) > 0:
            lines.append(
                f"   - {prio:<4}: Mean {sub.mean():6.1f} min | Median {sub.median():5.1f} min | "
                f"P95 {sub.quantile(0.95):6.1f} min (N={len(sub)})"
            )

    # Transfers and Handovers
    transfer_rows = assignment_df[assignment_df["event_type"] == "TRANSFER"]
    tickets_with_transfer = len(transfer_rows["ticket_id"].unique())
    lines.append(f"\n7. Transfers & Shift Handovers:")
    lines.append(f"   - Tickets with >=1 Transfer: {tickets_with_transfer:5d} ({tickets_with_transfer / total_tickets * 100:.1f}%)")
    lines.append(f"   - Max Handovers per Ticket : {outcome_df['handover_count'].max()}")

    # Peak Queue per window
    if scenario_windows:
        lines.append(f"\n8. Peak Queue Length per Scenario Window:")
        q_events = status_df[status_df["to_status"] == "QUEUED"][["event_at"]].copy()
        a_events = status_df[status_df["from_status"] == "QUEUED"][["event_at"]].copy()
        q_events["delta"] = 1
        a_events["delta"] = -1
        q_all = pd.concat([q_events, a_events]).sort_values("event_at")
        q_all["queue_len"] = q_all["delta"].cumsum()

        for w in scenario_windows:
            w_kind = getattr(w, "kind", "WINDOW").upper()
            w_id = getattr(w, "id", getattr(w, "incident_id", getattr(w, "spike_id", "WIN")))
            w_start = getattr(w, "start_iso", getattr(w, "start_time", ""))
            w_end = getattr(w, "end_iso", getattr(w, "end_time", ""))

            sub_q = q_all[(q_all["event_at"] >= w_start) & (q_all["event_at"] <= w_end)]
            peak_q = sub_q["queue_len"].max() if len(sub_q) > 0 else 0
            lines.append(f"   - {w_kind:<8} {w_id} ({w_start}..{w_end}): Peak Queue = {peak_q}")

    lines.append("\n" + "=" * 72)
    return "\n".join(lines)
