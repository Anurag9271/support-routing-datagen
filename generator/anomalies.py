"""Anomaly injection and ground-truth table generation.

Implements business_rules.md section 11 and data_dictionary.md section 21:
- Injects 5 labelled anomalies on in-memory copies of final tables:
  1. AGENT_OVER_CAPACITY (0.20%, V-ROUT-CAP-01)
  2. SLA_IGNORES_CALENDAR (0.33%, V-SLA-01)
  3. REOPEN_LOSES_HISTORY (0.27%, V-HIST-01)
  4. TRANSFER_LOOP (0.17%, V-LOOP-01)
  5. PRIORITY_CONTRADICTS_SAFETY (0.13%, V-SAFE-01)
- Re-derives all dependent fields (breach flags, loads, actors, resolvers).
- Generates GroundTruth table (data_dictionary.md section 21) with primary and cascade rows.
- No ground truth leak into raw tables.
- Disjoint target tickets across anomaly types.
"""

from __future__ import annotations

import datetime
from datetime import timedelta
from typing import Any, Final, Mapping
import numpy as np
import pandas as pd

from generator.config import Config
from generator.ids import ground_truth_id
from generator.sla_calendar import next_open, add_business_minutes

# Target rates per business_rules.md section 11
ANOMALY_RATES: Final[dict[str, float]] = {
    "AGENT_OVER_CAPACITY": 0.0020,
    "SLA_IGNORES_CALENDAR": 0.0033,
    "REOPEN_LOSES_HISTORY": 0.0027,
    "TRANSFER_LOOP": 0.0017,
    "PRIORITY_CONTRADICTS_SAFETY": 0.0013,
}

DETECTION_RULES: Final[dict[str, str]] = {
    "AGENT_OVER_CAPACITY": "V-ROUT-CAP-01",
    "SLA_IGNORES_CALENDAR": "V-SLA-01",
    "REOPEN_LOSES_HISTORY": "V-HIST-01",
    "TRANSFER_LOOP": "V-LOOP-01",
    "PRIORITY_CONTRADICTS_SAFETY": "V-SAFE-01",
}


def compute_anomaly_targets(tickets: int) -> dict[str, int]:
    """Compute target count per anomaly type: max(3, round(rate x tickets))."""
    return {
        issue_type: max(3, round(rate * tickets))
        for issue_type, rate in ANOMALY_RATES.items()
    }


def _iso_to_dt(iso_str: str) -> datetime.datetime:
    """Parse UTC ISO string to datetime."""
    return datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


def _dt_to_iso(dt: datetime.datetime) -> str:
    """Format UTC datetime to ISO string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def inject_anomalies(
    config: Config,
    master_tables: Mapping[str, pd.DataFrame],
    final_tables: Mapping[str, pd.DataFrame],
    rng: np.random.Generator,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Inject 5 labelled anomalies into deep copies of final tables and build GroundTruth.

    Args:
        config: Generator configuration.
        master_tables: Master reference data tables.
        final_tables: Clean baseline final tables from Phase 3c.
        rng: Dedicated 'anomalies' RNG stream.

    Returns:
        (anomalous_tables, ground_truth_df)
    """
    targets = compute_anomaly_targets(config.tickets)

    # In-memory copies
    tkt_df = final_tables["Ticket"].copy(deep=True)
    st_df = final_tables["TicketStatusEvent"].copy(deep=True)
    asn_df = final_tables["AssignmentEvent"].copy(deep=True)
    msg_df = final_tables["Message"].copy(deep=True)
    sla_df = final_tables["SLAEvent"].copy(deep=True)
    res_df = final_tables["Resolution"].copy(deep=True)

    # Master tables references
    agent_df = master_tables["Agent"]
    agent_skill_df = master_tables["AgentSkill"]
    agent_lang_df = master_tables["AgentLanguage"]
    agent_shift_df = master_tables["AgentShift"]
    cal_windows_df = master_tables["SLACalendarWindow"]
    channel_df = master_tables["Channel"]

    channel_cal_map = dict(zip(channel_df["channel_id"], channel_df["calendar_code"]))
    agent_concurrency_map = dict(zip(agent_df["agent_id"], agent_df["concurrency_limit"]))

    used_tickets: set[str] = set()
    raw_gt_records: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # 1. PRIORITY_CONTRADICTS_SAFETY (V-SAFE-01)
    # Pick safety-intent tickets. Set priority_level = P2 and
    # priority_reason_code = INTENT_DEFAULT; re-derive priority_rule_id and
    # SLAEvent targets and due times. Queue and assignments are left as they were.
    # -------------------------------------------------------------------------
    safety_cands = sorted(
        tkt_df[
            tkt_df["intent_id"].isin(["INT-008", "INT-009"])
            & (~tkt_df["ticket_id"].isin(used_tickets))
        ]["ticket_id"].unique()
    )
    prio_target = min(targets["PRIORITY_CONTRADICTS_SAFETY"], len(safety_cands))
    prio_perm = rng.permutation(len(safety_cands))
    selected_prio = [safety_cands[i] for i in prio_perm[:prio_target]]
    used_tickets.update(selected_prio)

    tkt_idx_map = {tid: idx for idx, tid in enumerate(tkt_df["ticket_id"])}
    sla_tkt_cycle_map = {
        (row["ticket_id"], int(row["cycle_no"])): idx
        for idx, row in sla_df.iterrows()
    }

    for tid in selected_prio:
        t_idx = tkt_idx_map[tid]
        created_at_str = str(tkt_df.at[t_idx, "created_at"])
        channel_id = str(tkt_df.at[t_idx, "channel_id"])
        intent_id = str(tkt_df.at[t_idx, "intent_id"])

        # Determine effective P2 rule at created_at
        effective_p2_rule = "PRL-002" if created_at_str < config.p2_target_v2_effective_date else "PRL-003"
        frt_target_m = 60 if effective_p2_rule == "PRL-002" else 45
        res_target_m = 480
        esc_after_m = 240
        chan_cal = channel_cal_map[channel_id]

        tkt_df.at[t_idx, "priority_level"] = "P2"
        tkt_df.at[t_idx, "priority_reason_code"] = "INTENT_DEFAULT"
        tkt_df.at[t_idx, "priority_rule_id"] = effective_p2_rule

        # Update SLAEvent for cycle 1
        sla_key = (tid, 1)
        if sla_key in sla_tkt_cycle_map:
            sla_idx = sla_tkt_cycle_map[sla_key]
            sla_df.at[sla_idx, "priority_rule_id"] = effective_p2_rule
            sla_df.at[sla_idx, "calendar_code_used"] = chan_cal
            sla_df.at[sla_idx, "first_response_target_min"] = frt_target_m
            sla_df.at[sla_idx, "resolution_target_min"] = res_target_m
            sla_df.at[sla_idx, "escalation_after_min"] = esc_after_m

            # Recompute SLA clock start and due times using channel calendar
            cycle_start_dt = _iso_to_dt(created_at_str)
            clock_start_dt = next_open(chan_cal, cycle_start_dt)
            frt_due_dt = add_business_minutes(chan_cal, clock_start_dt, frt_target_m)
            res_due_dt = add_business_minutes(chan_cal, clock_start_dt, res_target_m)
            esc_due_dt = add_business_minutes(chan_cal, clock_start_dt, esc_after_m)

            sla_df.at[sla_idx, "clock_start"] = _dt_to_iso(clock_start_dt)
            sla_df.at[sla_idx, "first_response_due"] = _dt_to_iso(frt_due_dt)
            sla_df.at[sla_idx, "resolution_due"] = _dt_to_iso(res_due_dt)
            sla_df.at[sla_idx, "escalation_due_at"] = _dt_to_iso(esc_due_dt)

            # Recompute breach flags
            frt_at_str = sla_df.at[sla_idx, "first_response_at"]
            if pd.notna(frt_at_str) and str(frt_at_str):
                frt_at_dt = _iso_to_dt(str(frt_at_str))
                sla_df.at[sla_idx, "frt_breached"] = frt_at_dt > frt_due_dt
            else:
                cutoff_dt = _iso_to_dt(config.cutoff)
                sla_df.at[sla_idx, "frt_breached"] = cutoff_dt > frt_due_dt

            res_at_str = sla_df.at[sla_idx, "resolved_at"]
            if pd.notna(res_at_str) and str(res_at_str):
                res_at_dt = _iso_to_dt(str(res_at_str))
                sla_df.at[sla_idx, "resolution_breached"] = res_at_dt > res_due_dt
            else:
                cutoff_dt = _iso_to_dt(config.cutoff)
                sla_df.at[sla_idx, "resolution_breached"] = cutoff_dt > res_due_dt

        raw_gt_records.append({
            "issue_type": "PRIORITY_CONTRADICTS_SAFETY",
            "entity_type": "TICKET",
            "entity_id": tid,
            "ticket_id": tid,
            "is_primary": True,
            "detection_rule": DETECTION_RULES["PRIORITY_CONTRADICTS_SAFETY"],
            "expected_value": "P1",
            "observed_value": "P2",
            "description": f"Safety intent {intent_id} on ticket {tid} set to P2 instead of P1",
        })

    # -------------------------------------------------------------------------
    # 2. SLA_IGNORES_CALENDAR (V-SLA-01)
    # Pick non-P1 tickets whose clock start or due time crosses a closed period.
    # Recompute first_response_due, resolution_due and escalation_due_at using
    # wall-clock minutes from created_at, and recompute breach flags from them.
    # calendar_code_used still shows the channel calendar.
    # -------------------------------------------------------------------------
    tkt_created_map = dict(zip(tkt_df["ticket_id"], tkt_df["created_at"]))
    non_p1_cands = []
    for _, s_row in sla_df[
        (sla_df["cycle_no"] == 1)
        & (sla_df["priority_rule_id"].isin(["PRL-002", "PRL-003", "PRL-004", "PRL-005"]))
    ].iterrows():
        t_id = str(s_row["ticket_id"])
        if t_id in used_tickets:
            continue
        c_at = _iso_to_dt(str(tkt_created_map[t_id]))
        frt_m = int(s_row["first_response_target_min"])
        wall_frt = _dt_to_iso(c_at + timedelta(minutes=frt_m))
        if wall_frt != str(s_row["first_response_due"]):
            non_p1_cands.append(t_id)

    non_p1_cands = sorted(set(non_p1_cands))
    sla_target = min(targets["SLA_IGNORES_CALENDAR"], len(non_p1_cands))
    sla_perm = rng.permutation(len(non_p1_cands))
    selected_sla = [non_p1_cands[i] for i in sla_perm[:sla_target]]
    used_tickets.update(selected_sla)

    for tid in selected_sla:
        sla_idx = sla_tkt_cycle_map[(tid, 1)]
        row = sla_df.loc[sla_idx]
        orig_frt_due = str(row["first_response_due"])
        cycle_start_dt = _iso_to_dt(str(row["clock_start"]))
        t_idx = tkt_idx_map[tid]
        created_at_dt = _iso_to_dt(str(tkt_df.at[t_idx, "created_at"]))

        frt_target = int(row["first_response_target_min"])
        res_target = int(row["resolution_target_min"])
        esc_target = int(row["escalation_after_min"])

        # Wall-clock due times directly from created_at
        wall_frt_dt = created_at_dt + timedelta(minutes=frt_target)
        wall_res_dt = created_at_dt + timedelta(minutes=res_target)
        wall_esc_dt = created_at_dt + timedelta(minutes=esc_target)

        sla_df.at[sla_idx, "first_response_due"] = _dt_to_iso(wall_frt_dt)
        sla_df.at[sla_idx, "resolution_due"] = _dt_to_iso(wall_res_dt)
        sla_df.at[sla_idx, "escalation_due_at"] = _dt_to_iso(wall_esc_dt)

        # Recompute breach flags
        frt_at_str = row["first_response_at"]
        if pd.notna(frt_at_str) and str(frt_at_str):
            sla_df.at[sla_idx, "frt_breached"] = _iso_to_dt(str(frt_at_str)) > wall_frt_dt
        else:
            cutoff_dt = _iso_to_dt(config.cutoff)
            sla_df.at[sla_idx, "frt_breached"] = cutoff_dt > wall_frt_dt

        res_at_str = row["resolved_at"]
        if pd.notna(res_at_str) and str(res_at_str):
            sla_df.at[sla_idx, "resolution_breached"] = _iso_to_dt(str(res_at_str)) > wall_res_dt
        else:
            cutoff_dt = _iso_to_dt(config.cutoff)
            sla_df.at[sla_idx, "resolution_breached"] = cutoff_dt > wall_res_dt

        raw_gt_records.append({
            "issue_type": "SLA_IGNORES_CALENDAR",
            "entity_type": "SLA_EVENT",
            "entity_id": str(row["sla_event_id"]),
            "ticket_id": tid,
            "is_primary": True,
            "detection_rule": DETECTION_RULES["SLA_IGNORES_CALENDAR"],
            "expected_value": orig_frt_due,
            "observed_value": _dt_to_iso(wall_frt_dt),
            "description": f"SLAEvent {row['sla_event_id']} due times computed using wall-clock time ignoring calendar {row['calendar_code_used']}",
        })

    # -------------------------------------------------------------------------
    # 3. REOPEN_LOSES_HISTORY (V-HIST-01)
    # Pick a ticket with reopen_count >= 1. Delete the previous cycle's
    # AssignmentEvents, SLAEvent and Resolution rows. Status events and messages kept.
    # -------------------------------------------------------------------------
    reopen_cands = sorted(
        tkt_df[
            (tkt_df["reopen_count"] >= 1)
            & (~tkt_df["ticket_id"].isin(used_tickets))
        ]["ticket_id"].unique()
    )
    reopen_target = min(targets["REOPEN_LOSES_HISTORY"], len(reopen_cands))
    reopen_perm = rng.permutation(len(reopen_cands))
    selected_reopen = [reopen_cands[i] for i in reopen_perm[:reopen_target]]
    used_tickets.update(selected_reopen)

    # Delete cycle 1 rows for selected reopened tickets
    asn_df = asn_df[~(asn_df["ticket_id"].isin(selected_reopen) & (asn_df["cycle_no"] == 1))].copy()
    sla_df = sla_df[~(sla_df["ticket_id"].isin(selected_reopen) & (sla_df["cycle_no"] == 1))].copy()
    res_df = res_df[~(res_df["ticket_id"].isin(selected_reopen) & (res_df["cycle_no"] == 1))].copy()

    for tid in selected_reopen:
        raw_gt_records.append({
            "issue_type": "REOPEN_LOSES_HISTORY",
            "entity_type": "TICKET",
            "entity_id": tid,
            "ticket_id": tid,
            "is_primary": True,
            "detection_rule": DETECTION_RULES["REOPEN_LOSES_HISTORY"],
            "expected_value": "cycle 1 history present",
            "observed_value": "cycle 1 history missing",
            "description": f"Reopened ticket {tid} cycle 1 AssignmentEvents, SLAEvent, and Resolution deleted",
        })

    # -------------------------------------------------------------------------
    # 4. TRANSFER_LOOP (V-LOOP-01)
    # Pick a ticket with handle time >= 60 min, reopen_count == 0, resolved.
    # Insert A -> B -> A -> B transfers (reason SKILL_REROUTE) 5-20 minutes apart.
    # -------------------------------------------------------------------------
    res_time_num = pd.to_numeric(res_df["time_to_resolve_minutes"], errors="coerce")
    loop_cands = sorted(
        res_df[
            (res_df["cycle_no"] == 1)
            & (res_time_num >= 60.0)
            & (~res_df["ticket_id"].isin(used_tickets))
        ]["ticket_id"].unique()
    )
    loop_target = min(targets["TRANSFER_LOOP"], len(loop_cands))
    loop_perm = rng.permutation(len(loop_cands))
    selected_loop = [loop_cands[i] for i in loop_perm[:loop_target]]
    used_tickets.update(selected_loop)

    # Active agent pool
    active_agent_ids = sorted(agent_df[agent_df["status"] == "ACTIVE"]["agent_id"].unique())

    new_asns: list[dict[str, Any]] = []
    new_statuses: list[dict[str, Any]] = []

    for tid in selected_loop:
        t_asns = asn_df[asn_df["ticket_id"] == tid].sort_values("seq_no")
        first_asn = t_asns.iloc[0]
        agent_a = str(first_asn["agent_id"])
        queue_id = str(first_asn["queue_id"])
        required_skill = str(first_asn["required_skill_id"])
        t_assign_dt = _iso_to_dt(str(first_asn["event_at"]))

        # Pick eligible agent B != A
        possible_b = [ag for ag in active_agent_ids if ag != agent_a]
        b_idx = int(rng.integers(0, len(possible_b)))
        agent_b = possible_b[b_idx]

        # 3 transfers: t1 (A->B), t2 (B->A), t3 (A->B)
        t1_dt = t_assign_dt + timedelta(minutes=10)
        t2_dt = t1_dt + timedelta(minutes=10)
        t3_dt = t2_dt + timedelta(minutes=10)

        # Create transfer AssignmentEvents
        asn_2_id = f"ASN-{tid}-002"
        asn_3_id = f"ASN-{tid}-003"
        asn_4_id = f"ASN-{tid}-004"

        transfer_asns = [
            {
                "assignment_id": asn_2_id,
                "ticket_id": tid,
                "seq_no": 2,
                "cycle_no": 1,
                "event_type": "TRANSFER",
                "reason_code": "SKILL_REROUTE",
                "event_at": _dt_to_iso(t1_dt),
                "agent_id": agent_b,
                "from_agent_id": agent_a,
                "queue_id": queue_id,
                "required_skill_id": required_skill,
                "language_match": True,
                "wait_minutes": 0.0,
                "agent_load_before": 1,
                "agent_load_after": 2,
                "concurrency_limit": agent_concurrency_map.get(agent_b, 4),
            },
            {
                "assignment_id": asn_3_id,
                "ticket_id": tid,
                "seq_no": 3,
                "cycle_no": 1,
                "event_type": "TRANSFER",
                "reason_code": "SKILL_REROUTE",
                "event_at": _dt_to_iso(t2_dt),
                "agent_id": agent_a,
                "from_agent_id": agent_b,
                "queue_id": queue_id,
                "required_skill_id": required_skill,
                "language_match": True,
                "wait_minutes": 0.0,
                "agent_load_before": 1,
                "agent_load_after": 2,
                "concurrency_limit": agent_concurrency_map.get(agent_a, 4),
            },
            {
                "assignment_id": asn_4_id,
                "ticket_id": tid,
                "seq_no": 4,
                "cycle_no": 1,
                "event_type": "TRANSFER",
                "reason_code": "SKILL_REROUTE",
                "event_at": _dt_to_iso(t3_dt),
                "agent_id": agent_b,
                "from_agent_id": agent_a,
                "queue_id": queue_id,
                "required_skill_id": required_skill,
                "language_match": True,
                "wait_minutes": 0.0,
                "agent_load_before": 1,
                "agent_load_after": 2,
                "concurrency_limit": agent_concurrency_map.get(agent_b, 4),
            },
        ]
        new_asns.extend(transfer_asns)

        # Status events for transfers
        new_statuses.extend([
            {
                "status_event_id": "",
                "ticket_id": tid,
                "seq_no": 0,
                "cycle_no": 1,
                "from_status": "IN_PROGRESS",
                "to_status": "ASSIGNED",
                "event_at": _dt_to_iso(t1_dt),
                "actor_type": "SYSTEM",
                "actor_agent_id": "",
                "trigger_ref": asn_2_id,
            },
            {
                "status_event_id": "",
                "ticket_id": tid,
                "seq_no": 0,
                "cycle_no": 1,
                "from_status": "ASSIGNED",
                "to_status": "IN_PROGRESS",
                "event_at": _dt_to_iso(t1_dt + timedelta(seconds=60)),
                "actor_type": "AGENT",
                "actor_agent_id": agent_b,
                "trigger_ref": "",
            },
            {
                "status_event_id": "",
                "ticket_id": tid,
                "seq_no": 0,
                "cycle_no": 1,
                "from_status": "IN_PROGRESS",
                "to_status": "ASSIGNED",
                "event_at": _dt_to_iso(t2_dt),
                "actor_type": "SYSTEM",
                "actor_agent_id": "",
                "trigger_ref": asn_3_id,
            },
            {
                "status_event_id": "",
                "ticket_id": tid,
                "seq_no": 0,
                "cycle_no": 1,
                "from_status": "ASSIGNED",
                "to_status": "IN_PROGRESS",
                "event_at": _dt_to_iso(t2_dt + timedelta(seconds=60)),
                "actor_type": "AGENT",
                "actor_agent_id": agent_a,
                "trigger_ref": "",
            },
            {
                "status_event_id": "",
                "ticket_id": tid,
                "seq_no": 0,
                "cycle_no": 1,
                "from_status": "IN_PROGRESS",
                "to_status": "ASSIGNED",
                "event_at": _dt_to_iso(t3_dt),
                "actor_type": "SYSTEM",
                "actor_agent_id": "",
                "trigger_ref": asn_4_id,
            },
            {
                "status_event_id": "",
                "ticket_id": tid,
                "seq_no": 0,
                "cycle_no": 1,
                "from_status": "ASSIGNED",
                "to_status": "IN_PROGRESS",
                "event_at": _dt_to_iso(t3_dt + timedelta(seconds=60)),
                "actor_type": "AGENT",
                "actor_agent_id": agent_b,
                "trigger_ref": "",
            },
        ])

        # Update Resolution row
        res_matches = res_df[(res_df["ticket_id"] == tid) & (res_df["cycle_no"] == 1)].index
        if len(res_matches) > 0:
            res_idx = res_matches[0]
            res_df.at[res_idx, "resolved_by_agent_id"] = agent_b
            res_df.at[res_idx, "transfer_count"] = int(res_df.at[res_idx, "transfer_count"]) + 3

        # Update Message owner if agent message sent after t3
        t3_iso = _dt_to_iso(t3_dt)
        agent_msgs = msg_df[
            (msg_df["ticket_id"] == tid)
            & (msg_df["sender_type"] == "AGENT")
            & (msg_df["sent_at"] >= t3_iso)
        ].index
        for m_idx in agent_msgs:
            msg_df.at[m_idx, "sender_agent_id"] = agent_b

        raw_gt_records.append({
            "issue_type": "TRANSFER_LOOP",
            "entity_type": "ASSIGNMENT_EVENT",
            "entity_id": asn_3_id,
            "ticket_id": tid,
            "is_primary": True,
            "detection_rule": DETECTION_RULES["TRANSFER_LOOP"],
            "expected_value": "1",
            "observed_value": "2",
            "description": f"Transfer loop A->B->A->B on ticket {tid} assigns agent {agent_a} twice in cycle 1",
        })

    # Append new assignment and status events and re-sequence for touched tickets
    if new_asns:
        asn_df = pd.concat([asn_df, pd.DataFrame(new_asns)], ignore_index=True)
        asn_df = asn_df.sort_values(["ticket_id", "cycle_no", "event_at"]).reset_index(drop=True)
        # Re-sequence seq_no and assignment_id
        asn_df["seq_no"] = asn_df.groupby("ticket_id").cumcount() + 1
        asn_df["assignment_id"] = (
            "ASN-" + asn_df["ticket_id"] + "-" + asn_df["seq_no"].map(lambda s: f"{s:03d}")
        )

    if new_statuses:
        st_df = pd.concat([st_df, pd.DataFrame(new_statuses)], ignore_index=True)
        st_df = st_df.sort_values(["ticket_id", "cycle_no", "event_at"]).reset_index(drop=True)
        # Re-sequence seq_no and status_event_id
        st_df["seq_no"] = st_df.groupby("ticket_id").cumcount() + 1
        st_df["status_event_id"] = (
            "TSE-" + st_df["ticket_id"] + "-" + st_df["seq_no"].map(lambda s: f"{s:03d}")
        )

    # -------------------------------------------------------------------------
    # 5. AGENT_OVER_CAPACITY (V-ROUT-CAP-01)
    # Pick an ASSIGN event where the best eligible agent already has load_before = limit.
    # Reassign the event to that agent and recompute loads. Choose cases where
    # the ticket's open interval is short.
    # -------------------------------------------------------------------------
    res_time_num = pd.to_numeric(res_df["time_to_resolve_minutes"], errors="coerce")
    cap_cands = sorted(
        res_df[
            (res_df["cycle_no"] == 1)
            & (res_time_num <= 20.0)
            & (~res_df["ticket_id"].isin(used_tickets))
        ]["ticket_id"].unique()
    )
    cap_target = min(targets["AGENT_OVER_CAPACITY"], len(cap_cands))
    cap_perm = rng.permutation(len(cap_cands))
    selected_cap = [cap_cands[i] for i in cap_perm[:cap_target]]
    used_tickets.update(selected_cap)

    for tid in selected_cap:
        asn_matches = asn_df[(asn_df["ticket_id"] == tid) & (asn_df["event_type"] == "ASSIGN")].index
        if len(asn_matches) == 0:
            continue
        asn_idx = asn_matches[0]
        orig_agent = str(asn_df.at[asn_idx, "agent_id"])

        # Choose agent X != orig_agent
        possible_x = [ag for ag in active_agent_ids if ag != orig_agent]
        x_idx = int(rng.integers(0, len(possible_x)))
        agent_x = possible_x[x_idx]
        limit_x = agent_concurrency_map.get(agent_x, 4)

        asn_id = str(asn_df.at[asn_idx, "assignment_id"])

        # Set over-capacity load
        asn_df.at[asn_idx, "agent_id"] = agent_x
        asn_df.at[asn_idx, "concurrency_limit"] = limit_x
        asn_df.at[asn_idx, "agent_load_before"] = limit_x
        asn_df.at[asn_idx, "agent_load_after"] = limit_x + 1

        # Update status events actor
        st_matches = st_df[(st_df["ticket_id"] == tid) & (st_df["actor_type"] == "AGENT")].index
        for s_idx in st_matches:
            st_df.at[s_idx, "actor_agent_id"] = agent_x

        # Update message sender
        m_matches = msg_df[(msg_df["ticket_id"] == tid) & (msg_df["sender_type"] == "AGENT")].index
        for m_idx in m_matches:
            msg_df.at[m_idx, "sender_agent_id"] = agent_x

        # Update resolution
        r_matches = res_df[(res_df["ticket_id"] == tid) & (res_df["cycle_no"] == 1)].index
        for r_idx in r_matches:
            res_df.at[r_idx, "resolved_by_agent_id"] = agent_x

        # Check cascades: later assignments at agent_x while this ticket is open
        t_assign_str = str(asn_df.at[asn_idx, "event_at"])
        t_res_str = str(res_df.at[r_matches[0], "resolved_at"]) if len(r_matches) > 0 else config.cutoff
        later_asns = asn_df[
            (asn_df["agent_id"] == agent_x)
            & (asn_df["event_at"] > t_assign_str)
            & (asn_df["event_at"] < t_res_str)
            & (asn_df["ticket_id"] != tid)
        ].index
        for l_idx in later_asns:
            asn_df.at[l_idx, "agent_load_before"] = int(asn_df.at[l_idx, "agent_load_before"]) + 1
            l_after = int(asn_df.at[l_idx, "agent_load_after"]) + 1
            asn_df.at[l_idx, "agent_load_after"] = l_after
            if l_after > limit_x:
                raw_gt_records.append({
                    "issue_type": "AGENT_OVER_CAPACITY",
                    "entity_type": "ASSIGNMENT_EVENT",
                    "entity_id": str(asn_df.at[l_idx, "assignment_id"]),
                    "ticket_id": str(asn_df.at[l_idx, "ticket_id"]),
                    "is_primary": False,
                    "detection_rule": DETECTION_RULES["AGENT_OVER_CAPACITY"],
                    "expected_value": str(limit_x),
                    "observed_value": str(l_after),
                    "description": f"Cascade: Agent {agent_x} assignment exceeds concurrency limit {limit_x} due to prior over-capacity assignment on {tid}",
                })

        raw_gt_records.append({
            "issue_type": "AGENT_OVER_CAPACITY",
            "entity_type": "ASSIGNMENT_EVENT",
            "entity_id": asn_id,
            "ticket_id": tid,
            "is_primary": True,
            "detection_rule": DETECTION_RULES["AGENT_OVER_CAPACITY"],
            "expected_value": str(limit_x),
            "observed_value": str(limit_x + 1),
            "description": f"Agent {agent_x} assigned ticket {tid} exceeding concurrency limit {limit_x} (load_after={limit_x + 1})",
        })

    # Build final GroundTruth DataFrame sorted by ticket_id, sequence gt_id
    raw_gt_records.sort(key=lambda r: (r["ticket_id"], r["entity_id"], not r["is_primary"]))
    for i, r in enumerate(raw_gt_records, start=1):
        r["gt_id"] = ground_truth_id(i)

    gt_df = pd.DataFrame(
        raw_gt_records,
        columns=[
            "gt_id",
            "issue_type",
            "entity_type",
            "entity_id",
            "ticket_id",
            "is_primary",
            "detection_rule",
            "expected_value",
            "observed_value",
            "description",
        ],
    )

    st_df["actor_agent_id"] = st_df["actor_agent_id"].fillna("")
    st_df["trigger_ref"] = st_df["trigger_ref"].fillna("")
    asn_df["from_agent_id"] = asn_df["from_agent_id"].fillna("")

    anomalous_tables = {
        "Ticket": tkt_df.sort_values("ticket_id").reset_index(drop=True),
        "TicketStatusEvent": st_df.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True),
        "AssignmentEvent": asn_df.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True),
        "Message": msg_df.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True),
        "SLAEvent": sla_df.sort_values(["ticket_id", "cycle_no"]).reset_index(drop=True),
        "Resolution": res_df.sort_values(["ticket_id", "cycle_no"]).reset_index(drop=True),
    }

    return anomalous_tables, gt_df
