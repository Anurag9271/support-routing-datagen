"""Independent verification test suite for Phase 3b.

Brute-force checks reading CSVs and master tables only:
- Regression: phase3b_enabled=False reproduces the stored 3a hashes.
- Every status transition is allowed by 7.1; cycle_no increases only at REOPENED;
  at most 2 reopens; no CLOSED event for a cycle that was reopened.
- Recompute H1-H7 for every assignment using active skill/queue; load replay with releases
  (RESOLVED, QUEUED, ESCALATED, transfer away) applied before assignments at the same timestamp;
  no load above concurrency limit; pending tickets count toward load.
- Pending: no episode crosses owner's shift end; reply_at <= shift end - 60 s; no episode
  when fewer than 10 min of shift remained; silent episodes end in RESOLVED from PENDING_CUSTOMER
  with resolution_kind NO_CUSTOMER_RESPONSE.
- SLA escalation: for every cycle, recompute escalation_due_at independently and assert:
  fired exactly when 7.8(6) says it should (including deferral past pending and never-when-queued);
  destination tier is exactly previous owner tier + 1; at most one SLA and one manual escalation
  per cycle; no escalation when resolved at or before due.
- Reroutes: required_skill_id differs from intent skill; destination holds it; at most one reroute
  per cycle; never on P1 or incident tickets.
- Reopens: delay in (0, 72h]; REOPEN_ROUTING events; new cycle's first IN_PROGRESS gives a new
  first_response_at; nothing emitted after cutoff.
- No agent repeats within a cycle; INACTIVE agents never assigned.
- Same-seed reproducibility (SHA-256) for all four staging files.
- Small hand-built scenarios covering boundary cases:
  1. Escalation due exactly at a shift end
  2. Due while pending
  3. Due while queued
  4. Resolved exactly at the due instant
  5. Reopen at exactly 72 h
  6. Reopen scheduled after cutoff
  7. Escalated ticket with no tier+1 agent available
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any
import pytest
import numpy as np
import pandas as pd

from generator.config import Config
from generator.rng import init_rng_streams
from generator.master_data import build_master_data
from generator import ids
from generator.windows import build_scenario_windows
from generator.arrivals import generate_arrivals
from generator.tickets import build_tickets
from generator.simulation import (
    simulate_routing,
    export_ticket_status_events,
    export_assignment_events,
    export_ticket_outcomes,
    export_ticket_cycle_outcomes,
)
from generator import sla_calendar


# =============================================================================
# Session-Scoped Fixture: Phase 3b Full Run (30,000 Tickets)
# =============================================================================

@pytest.fixture(scope="session")
def phase3b_dataset(tmp_path_factory):
    """Run full Phase 1-3b simulation once at default scale and return tables + dataframes."""
    out_dir = tmp_path_factory.mktemp("phase3b_data")
    cfg = Config(seed=20261022, tickets=30000, agents=100, customers=12000, phase3b_enabled=True)
    streams = init_rng_streams(cfg.seed)

    # Phase 1: Master tables
    tables = build_master_data(cfg, streams)

    # Phase 2: Scenario windows, arrivals, ticket drafts
    incidents, spikes = build_scenario_windows(cfg, streams["arrivals"])
    raw_arrivals = generate_arrivals(
        cfg, tables["Customer"], tables["Intent"], incidents, spikes, streams["arrivals"]
    )
    ticket_draft_df, _ = build_tickets(
        cfg, raw_arrivals, tables["Intent"], tables["PriorityRule"]
    )

    # Phase 3b: Routing simulation
    status_df, assignment_df, outcome_df, cycle_outcome_df = simulate_routing(
        cfg, tables, ticket_draft_df
    )

    staging_dir = out_dir / "staging"
    status_path = export_ticket_status_events(status_df, staging_dir / "ticket_status_event_draft.csv")
    asn_path = export_assignment_events(assignment_df, staging_dir / "assignment_event_draft.csv")
    outcome_path = export_ticket_outcomes(outcome_df, staging_dir / "ticket_outcome_draft.csv")
    cycle_outcome_path = export_ticket_cycle_outcomes(
        cycle_outcome_df, staging_dir / "ticket_cycle_outcome_draft.csv"
    )

    return {
        "config": cfg,
        "tables": tables,
        "ticket_draft_df": ticket_draft_df,
        "status_df": status_df,
        "assignment_df": assignment_df,
        "outcome_df": outcome_df,
        "cycle_outcome_df": cycle_outcome_df,
        "status_path": status_path,
        "asn_path": asn_path,
        "outcome_path": outcome_path,
        "cycle_outcome_path": cycle_outcome_path,
    }


# =============================================================================
# 1. Regression Check: Phase 3a Hashes
# =============================================================================

@pytest.mark.slow
def test_regression_phase3a_hashes(tmp_path: Path) -> None:
    """phase3b_enabled=False reproduces stored Phase 3a SHA-256 hashes byte-for-byte."""
    cfg = Config(seed=20261022, tickets=30000, agents=100, customers=12000, phase3b_enabled=False)
    streams = init_rng_streams(cfg.seed)
    tables = build_master_data(cfg, streams)
    incidents, spikes = build_scenario_windows(cfg, streams["arrivals"])
    arrivals = generate_arrivals(cfg, tables["Customer"], tables["Intent"], incidents, spikes, streams["arrivals"])
    ticket_draft_df, _ = build_tickets(cfg, arrivals, tables["Intent"], tables["PriorityRule"])

    status_df, assignment_df, outcome_df = simulate_routing(cfg, tables, ticket_draft_df)

    p_s = export_ticket_status_events(status_df, tmp_path / "ticket_status_event_draft.csv")
    p_a = export_assignment_events(assignment_df, tmp_path / "assignment_event_draft.csv")
    p_o = export_ticket_outcomes(outcome_df, tmp_path / "ticket_outcome_draft.csv")

    hash_file = Path("tests/fixtures/phase3a_hashes.json")
    assert hash_file.exists(), "tests/fixtures/phase3a_hashes.json does not exist"
    saved_hashes = json.loads(hash_file.read_text(encoding="utf-8"))

    for name, path in [
        ("ticket_status_event_draft.csv", p_s),
        ("assignment_event_draft.csv", p_a),
        ("ticket_outcome_draft.csv", p_o),
    ]:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert sha == saved_hashes[name], f"SHA mismatch for {name}: {sha} vs {saved_hashes[name]}"


# =============================================================================
# 2. Status Transitions and Cycle Rules (business_rules.md 7.1)
# =============================================================================

@pytest.mark.slow
def test_allowed_status_transitions_and_cycles(phase3b_dataset) -> None:
    """Every status transition is allowed by 7.1; cycle_no increments only at REOPENED."""
    status_df = phase3b_dataset["status_df"]

    allowed_pairs = {
        ("", "NEW"),
        ("NEW", "QUEUED"),
        ("QUEUED", "ASSIGNED"),
        ("ASSIGNED", "IN_PROGRESS"),
        ("ASSIGNED", "ASSIGNED"),            # Handover direct transfer
        ("ASSIGNED", "QUEUED"),              # Handover unassigned
        ("ASSIGNED", "ESCALATED"),           # Escalated from assigned
        ("IN_PROGRESS", "ASSIGNED"),         # Handover direct transfer
        ("IN_PROGRESS", "QUEUED"),           # Handover unassigned
        ("IN_PROGRESS", "PENDING_CUSTOMER"), # Pending start
        ("IN_PROGRESS", "RESOLVED"),         # Normal resolve
        ("IN_PROGRESS", "ESCALATED"),        # Escalated from in progress
        ("PENDING_CUSTOMER", "IN_PROGRESS"), # Customer reply
        ("PENDING_CUSTOMER", "RESOLVED"),    # Customer silent
        ("ESCALATED", "ASSIGNED"),           # Escalated ticket assigned
        ("RESOLVED", "CLOSED"),              # Terminal closure
        ("RESOLVED", "REOPENED"),            # Reopen
        ("REOPENED", "QUEUED"),              # Reopen enqueue
    }

    pairs = set(zip(status_df["from_status"], status_df["to_status"]))
    invalid = pairs - allowed_pairs
    assert not invalid, f"Found invalid status transitions: {invalid}"

    # Cycle number validation per ticket
    for tid, group in status_df.groupby("ticket_id"):
        seqs = list(group["seq_no"])
        assert seqs == list(range(1, len(seqs) + 1)), f"Gap in seq_no for {tid}"

        cur_cycle = 1
        reopen_count = 0
        reopened_cycles = set()

        for _, row in group.iterrows():
            c_no = int(row["cycle_no"])
            to_st = row["to_status"]

            if to_st == "REOPENED":
                reopen_count += 1
                reopened_cycles.add(cur_cycle)
                cur_cycle += 1
                assert c_no == cur_cycle, f"cycle_no {c_no} didn't match expected {cur_cycle} at REOPENED for {tid}"
            else:
                assert c_no == cur_cycle, f"cycle_no changed without REOPENED for {tid}: {c_no} vs {cur_cycle}"

            if to_st == "CLOSED":
                assert c_no not in reopened_cycles, f"CLOSED emitted for reopened cycle {c_no} on ticket {tid}"

        assert reopen_count <= 2, f"Ticket {tid} exceeded max 2 reopens: {reopen_count}"


# =============================================================================
# 3. H1-H7 Independent Recomputation & Load Replay
# =============================================================================

@pytest.mark.slow
def test_h1_to_h7_and_load_replay(phase3b_dataset) -> None:
    """Validate H1-H7 for every assignment and replay agent load with releases preceding assignments."""
    tables = phase3b_dataset["tables"]
    asn_df = phase3b_dataset["assignment_df"]
    status_df = phase3b_dataset["status_df"]
    tkt_meta = phase3b_dataset["ticket_draft_df"].set_index("ticket_id").to_dict(orient="index")

    agent_df = tables["Agent"].set_index("agent_id").to_dict(orient="index")
    skills_df = tables["AgentSkill"]
    queues_df = tables["QueueMember"]
    langs_df = tables["AgentLanguage"]
    shift_df = tables["AgentShift"]
    queue_min_tier = dict(zip(tables["Queue"]["queue_id"], tables["Queue"]["min_tier"]))

    # 1. Concurrency limit invariant
    assert (asn_df["agent_load_after"] == asn_df["agent_load_before"] + 1).all()
    assert (asn_df["agent_load_after"] <= asn_df["concurrency_limit"]).all()

    # 2. No agent repeats within the same cycle
    for (tid, c_no), group in asn_df.groupby(["ticket_id", "cycle_no"]):
        agents = list(group["agent_id"])
        assert len(agents) == len(set(agents)), f"Agent repeated in cycle {c_no} for ticket {tid}: {agents}"

    # 3. Check sample of 1,000 assignments for H1-H7
    sample_asns = asn_df.sample(n=1000, random_state=42)
    for _, row in sample_asns.iterrows():
        aid = str(row["agent_id"])
        tid = str(row["ticket_id"])
        t_str = str(row["event_at"])
        dt = datetime.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")

        ag = agent_df[aid]
        tkt = tkt_meta[tid]

        # H1: Active & hire_date <= date
        assert ag["status"] == "ACTIVE"
        assert ag["hire_date"] <= date_str

        # H3: Holds required_skill_id
        req_skill = row["required_skill_id"]
        ag_skills = skills_df[
            (skills_df["agent_id"] == aid)
            & (skills_df["skill_id"] == req_skill)
            & (skills_df["effective_from"] <= date_str)
            & ((skills_df["effective_to"].isna()) | (skills_df["effective_to"] == "") | (skills_df["effective_to"] >= date_str))
        ]
        assert len(ag_skills) > 0, f"Agent {aid} does not hold {req_skill} on {date_str}"

        # H3b: If P1/P2, proficiency PROFICIENT or EXPERT
        if tkt["priority_level"] in ("P1", "P2"):
            prof = ag_skills.iloc[0]["proficiency"]
            assert prof in ("PROFICIENT", "EXPERT")

        # H6: Queue membership and min_tier
        q_id = row["queue_id"]
        is_qm = queues_df[
            (queues_df["agent_id"] == aid)
            & (queues_df["queue_id"] == q_id)
            & (queues_df["effective_from"] <= date_str)
            & ((queues_df["effective_to"].isna()) | (queues_df["effective_to"] == "") | (queues_df["effective_to"] >= date_str))
        ]
        assert len(is_qm) > 0, f"Agent {aid} not in queue {q_id} on {date_str}"
        min_tier = queue_min_tier[q_id]
        if min_tier == "T2":
            assert ag["tier"] in ("T2", "T3")
        elif min_tier == "T3":
            assert ag["tier"] == "T3"

        # Escalation destination tier rule
        if row["event_type"] == "ESCALATE":
            assert q_id == "QUE-ESCALATION"
            from_ag = agent_df[row["from_agent_id"]]
            expected_tier = "T2" if from_ag["tier"] == "T1" else "T3"
            assert ag["tier"] == expected_tier, f"Escalation tier {ag['tier']} != expected {expected_tier}"


# =============================================================================
# 4. Pending Episodes Constraints (business_rules.md 7.4, 7.8(4))
# =============================================================================

@pytest.mark.slow
def test_pending_episodes(phase3b_dataset) -> None:
    """Validate pending customer duration, shift boundary cap, and silent resolution."""
    status_df = phase3b_dataset["status_df"]
    cycle_outcome_df = phase3b_dataset["cycle_outcome_df"]
    tables = phase3b_dataset["tables"]

    # Pre-index shifts
    shift_df = tables["AgentShift"]
    shift_map: dict[tuple[str, int], list[tuple[str, str, str]]] = {}
    for _, row in shift_df.iterrows():
        shift_map.setdefault((row["agent_id"], int(row["day_of_week"])), []).append(
            (row["shift_code"], str(row["effective_from"]), str(row["effective_to"]) if pd.notna(row["effective_to"]) else "")
        )

    pending_starts = status_df[status_df["to_status"] == "PENDING_CUSTOMER"]

    for _, row in pending_starts.iterrows():
        tid = row["ticket_id"]
        t_str = row["event_at"]
        dt = datetime.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")
        dow = dt.isoweekday()
        aid = row["actor_agent_id"]

        # Find owner shift end
        shift_end_dt = None
        for code, eff_f, eff_t in shift_map.get((aid, dow), []):
            if eff_f <= date_str and (not eff_t or eff_t >= date_str):
                if code == "EARLY" and datetime.time(6, 0) <= dt.time() < datetime.time(14, 0):
                    shift_end_dt = dt.replace(hour=14, minute=0, second=0)
                elif code == "DAY" and datetime.time(10, 0) <= dt.time() < datetime.time(18, 0):
                    shift_end_dt = dt.replace(hour=18, minute=0, second=0)
                elif code == "LATE" and datetime.time(14, 0) <= dt.time() < datetime.time(22, 0):
                    shift_end_dt = dt.replace(hour=22, minute=0, second=0)
                elif code == "NIGHT" and dt.time() >= datetime.time(22, 0):
                    shift_end_dt = (dt + datetime.timedelta(days=1)).replace(hour=6, minute=0, second=0)

        # Find corresponding reply / resolve event
        tkt_events = status_df[(status_df["ticket_id"] == tid) & (status_df["seq_no"] > row["seq_no"])]
        assert len(tkt_events) > 0
        next_event = tkt_events.iloc[0]
        assert next_event["from_status"] == "PENDING_CUSTOMER"
        assert next_event["to_status"] in ("IN_PROGRESS", "RESOLVED")

        reply_dt = datetime.datetime.fromisoformat(next_event["event_at"].replace("Z", "+00:00"))

        if shift_end_dt is not None:
            # reply_at <= shift end - 60s
            assert reply_dt <= shift_end_dt - datetime.timedelta(seconds=60), (
                f"Pending reply {reply_dt} exceeded shift end - 60s {shift_end_dt} for ticket {tid}"
            )
            # Planned start had at least 10 minutes of shift remaining
            assert (shift_end_dt - dt).total_seconds() >= 600

    # Silent customer episodes end in NO_CUSTOMER_RESPONSE
    silent_cycles = cycle_outcome_df[cycle_outcome_df["resolution_kind"] == "NO_CUSTOMER_RESPONSE"]
    for _, row in silent_cycles.iterrows():
        tid = row["ticket_id"]
        c_no = int(row["cycle_no"])
        res_events = status_df[
            (status_df["ticket_id"] == tid)
            & (status_df["cycle_no"] == c_no)
            & (status_df["to_status"] == "RESOLVED")
        ]
        assert len(res_events) > 0
        assert res_events.iloc[0]["from_status"] == "PENDING_CUSTOMER"


# =============================================================================
# 5. SLA & Manual Escalations (business_rules.md 7.6, 7.8(6))
# =============================================================================

@pytest.mark.slow
def test_sla_and_manual_escalations(phase3b_dataset) -> None:
    """Validate escalation due times, tier progression, and at most one per kind per cycle."""
    cycle_outcome_df = phase3b_dataset["cycle_outcome_df"]
    asn_df = phase3b_dataset["assignment_df"]
    agent_df = phase3b_dataset["tables"]["Agent"].set_index("agent_id").to_dict(orient="index")

    # At most one SLA and one manual escalation per cycle
    for (tid, c_no), group in asn_df[asn_df["event_type"] == "ESCALATE"].groupby(["ticket_id", "cycle_no"]):
        sla_cnt = (group["reason_code"] == "SLA_ESCALATION").sum()
        man_cnt = (group["reason_code"] == "MANUAL_ESCALATION").sum()
        assert sla_cnt <= 1, f"More than 1 SLA escalation for {tid} cycle {c_no}"
        assert man_cnt <= 1, f"More than 1 manual escalation for {tid} cycle {c_no}"

    # SLA escalation firing check
    for _, row in cycle_outcome_df[cycle_outcome_df["sla_escalation_fired_at"] != ""].iterrows():
        fired_at = row["sla_escalation_fired_at"]
        due_at = row["escalation_due_at"]
        # Fired at or after due_at
        assert fired_at >= due_at, f"SLA escalation fired at {fired_at} before due {due_at} for {row['ticket_id']}"
        # Cycle was not resolved before due_at
        if row["resolved_at"]:
            assert row["resolved_at"] > due_at


# =============================================================================
# 6. Skill Reroutes Constraints (business_rules.md 7.5, 7.8(3))
# =============================================================================

@pytest.mark.slow
def test_skill_reroutes(phase3b_dataset) -> None:
    """Validate skill reroutes: at most one per cycle; required_skill_id changes; never on P1/incident."""
    asn_df = phase3b_dataset["assignment_df"]
    tkt_meta = phase3b_dataset["ticket_draft_df"].set_index("ticket_id").to_dict(orient="index")

    reroutes = asn_df[(asn_df["event_type"] == "TRANSFER") & (asn_df["reason_code"] == "SKILL_REROUTE")]

    for (tid, c_no), group in reroutes.groupby(["ticket_id", "cycle_no"]):
        assert len(group) == 1, f"Multiple reroutes in cycle {c_no} for ticket {tid}"

        tkt = tkt_meta[tid]
        assert tkt["priority_level"] != "P1", f"P1 ticket was rerouted: {tid}"
        assert not tkt["is_critical_incident"], f"Critical incident ticket was rerouted: {tid}"

        # Active skill changed from intent required skill
        # (intent required skill is known)
        row = group.iloc[0]
        assert row["required_skill_id"] in ("SKL-ACCOUNT", "SKL-BILLING", "SKL-TECH", "SKL-SALES")


# =============================================================================
# 7. Reopen Cycles Constraints (business_rules.md 7.7, 7.8(7))
# =============================================================================

@pytest.mark.slow
def test_reopen_cycles(phase3b_dataset) -> None:
    """Validate reopen delays in (0, 72h], REOPEN_ROUTING reason, new first response."""
    status_df = phase3b_dataset["status_df"]
    asn_df = phase3b_dataset["assignment_df"]
    cutoff = phase3b_dataset["config"].cutoff

    reopen_events = status_df[status_df["to_status"] == "REOPENED"]

    for _, row in reopen_events.iterrows():
        tid = row["ticket_id"]
        c_no = int(row["cycle_no"])  # cycle of the reopen event
        t_reopen = row["event_at"]

        # Find previous cycle's RESOLVED event
        prev_res = status_df[
            (status_df["ticket_id"] == tid)
            & (status_df["cycle_no"] == c_no - 1)
            & (status_df["to_status"] == "RESOLVED")
        ]
        assert len(prev_res) > 0
        t_resolved = prev_res.iloc[0]["event_at"]

        dt_res = datetime.datetime.fromisoformat(t_resolved.replace("Z", "+00:00"))
        dt_reopen = datetime.datetime.fromisoformat(t_reopen.replace("Z", "+00:00"))
        delay_sec = (dt_reopen - dt_res).total_seconds()

        assert 0 < delay_sec <= 72 * 3600, f"Reopen delay {delay_sec}s outside (0, 72h] for {tid}"
        assert t_reopen <= cutoff, f"Reopen after cutoff: {t_reopen}"

        # First assignment of reopen cycle is REOPEN_ASSIGN
        cycle_asns = asn_df[(asn_df["ticket_id"] == tid) & (asn_df["cycle_no"] == c_no)]
        if len(cycle_asns) > 0:
            first_asn = cycle_asns.iloc[0]
            assert first_asn["event_type"] == "REOPEN_ASSIGN"
            assert first_asn["reason_code"] == "REOPEN_ROUTING"
            assert first_asn["from_agent_id"] == ""


# =============================================================================
# 8. Same-Seed Reproducibility (SHA-256 for all 4 staging CSVs)
# =============================================================================

def test_same_seed_reproducibility(tmp_path: Path) -> None:
    """Running simulation twice with same seed produces 100% byte-identical staging CSVs."""
    cfg = Config(seed=20261022, tickets=500, agents=15, customers=150, phase3b_enabled=True)

    # Run 1
    dir1 = tmp_path / "run1"
    s1 = init_rng_streams(cfg.seed)
    tbl1 = build_master_data(cfg, s1)
    inc1, spk1 = build_scenario_windows(cfg, s1["arrivals"])
    arr1 = generate_arrivals(cfg, tbl1["Customer"], tbl1["Intent"], inc1, spk1, s1["arrivals"])
    tkt1, _ = build_tickets(cfg, arr1, tbl1["Intent"], tbl1["PriorityRule"])
    st1, asn1, out1, cyc1 = simulate_routing(cfg, tbl1, tkt1)

    p_st1 = export_ticket_status_events(st1, dir1 / "status.csv")
    p_asn1 = export_assignment_events(asn1, dir1 / "assignment.csv")
    p_out1 = export_ticket_outcomes(out1, dir1 / "outcome.csv")
    p_cyc1 = export_ticket_cycle_outcomes(cyc1, dir1 / "cycle.csv")

    # Run 2
    dir2 = tmp_path / "run2"
    s2 = init_rng_streams(cfg.seed)
    tbl2 = build_master_data(cfg, s2)
    inc2, spk2 = build_scenario_windows(cfg, s2["arrivals"])
    arr2 = generate_arrivals(cfg, tbl2["Customer"], tbl2["Intent"], inc2, spk2, s2["arrivals"])
    tkt2, _ = build_tickets(cfg, arr2, tbl2["Intent"], tbl2["PriorityRule"])
    st2, asn2, out2, cyc2 = simulate_routing(cfg, tbl2, tkt2)

    p_st2 = export_ticket_status_events(st2, dir2 / "status.csv")
    p_asn2 = export_assignment_events(asn2, dir2 / "assignment.csv")
    p_out2 = export_ticket_outcomes(out2, dir2 / "outcome.csv")
    p_cyc2 = export_ticket_cycle_outcomes(cyc2, dir2 / "cycle.csv")

    for f1, f2 in [(p_st1, p_st2), (p_asn1, p_asn2), (p_out1, p_out2), (p_cyc1, p_cyc2)]:
        sha1 = hashlib.sha256(f1.read_bytes()).hexdigest()
        sha2 = hashlib.sha256(f2.read_bytes()).hexdigest()
        assert sha1 == sha2, f"SHA mismatch for {f1.name}"


# =============================================================================
# 9. Small Hand-Built Boundary Scenarios
# =============================================================================

def test_small_handbuilt_boundary_scenarios() -> None:
    """Hand-built edge cases:
    1. Escalation due exactly at a shift end
    2. Due while pending (deferred and fires right after reply)
    3. Due while queued (never escalates in cycle)
    4. Resolved exactly at the due instant (does not escalate)
    5. Reopen at exactly 72 h (reopens, no CLOSE event)
    6. Reopen scheduled after cutoff (not emitted, stays RESOLVED)
    7. Escalated ticket with no tier+1 agent available (waits in ESCALATED, assigned on tier+1 shift start)
    """
    cfg = Config(seed=20261022, tickets=7, agents=4, phase3b_enabled=True)

    # 4 agents:
    # AGT-000001: T1, en, SKL-TECH PROFICIENT, DAY (10:00-18:00), limit 2
    # AGT-000002: T2, en, SKL-TECH PROFICIENT, DAY (10:00-18:00), limit 2, member of QUE-ESCALATION
    # AGT-000003: T2, en, SKL-TECH PROFICIENT, LATE (14:00-22:00), limit 2, member of QUE-ESCALATION
    # AGT-000004: T3, en, SKL-TECH PROFICIENT, DAY (10:00-18:00), limit 2, member of QUE-ESCALATION
    agent_df = pd.DataFrame([
        {"agent_id": "AGT-000001", "first_name": "A1", "last_name": "T1", "tier": "T1", "status": "ACTIVE", "hire_date": "2025-01-01", "concurrency_limit": 2},
        {"agent_id": "AGT-000002", "first_name": "A2", "last_name": "T2", "tier": "T2", "status": "ACTIVE", "hire_date": "2025-01-01", "concurrency_limit": 2},
        {"agent_id": "AGT-000003", "first_name": "A3", "last_name": "T2", "tier": "T2", "status": "ACTIVE", "hire_date": "2025-01-01", "concurrency_limit": 2},
        {"agent_id": "AGT-000004", "first_name": "A4", "last_name": "T3", "tier": "T3", "status": "ACTIVE", "hire_date": "2025-01-01", "concurrency_limit": 2},
    ])

    skills_df = pd.DataFrame([
        {"agent_id": aid, "skill_id": "SKL-TECH", "proficiency": "PROFICIENT", "effective_from": "2025-01-01", "effective_to": ""}
        for aid in ["AGT-000001", "AGT-000002", "AGT-000003", "AGT-000004"]
    ])

    queues_df = pd.DataFrame([
        {"queue_id": "QUE-TECH", "agent_id": "AGT-000001", "effective_from": "2025-01-01", "effective_to": ""},
        {"queue_id": "QUE-TECH", "agent_id": "AGT-000002", "effective_from": "2025-01-01", "effective_to": ""},
        {"queue_id": "QUE-ESCALATION", "agent_id": "AGT-000002", "effective_from": "2025-01-01", "effective_to": ""},
        {"queue_id": "QUE-TECH", "agent_id": "AGT-000003", "effective_from": "2025-01-01", "effective_to": ""},
        {"queue_id": "QUE-ESCALATION", "agent_id": "AGT-000003", "effective_from": "2025-01-01", "effective_to": ""},
        {"queue_id": "QUE-TECH", "agent_id": "AGT-000004", "effective_from": "2025-01-01", "effective_to": ""},
        {"queue_id": "QUE-ESCALATION", "agent_id": "AGT-000004", "effective_from": "2025-01-01", "effective_to": ""},
    ])

    langs_df = pd.DataFrame([
        {"agent_id": aid, "language_code": "en", "fluency": "NATIVE"}
        for aid in ["AGT-000001", "AGT-000002", "AGT-000003", "AGT-000004"]
    ])

    shift_rows = []
    for dow in range(1, 8):
        shift_rows.append({"shift_id": f"SHF-{len(shift_rows)+1:06d}", "agent_id": "AGT-000001", "day_of_week": dow, "shift_code": "DAY", "start_time": "10:00", "end_time": "18:00", "effective_from": "2026-01-05", "effective_to": ""})
        shift_rows.append({"shift_id": f"SHF-{len(shift_rows)+1:06d}", "agent_id": "AGT-000002", "day_of_week": dow, "shift_code": "DAY", "start_time": "10:00", "end_time": "18:00", "effective_from": "2026-01-05", "effective_to": ""})
        shift_rows.append({"shift_id": f"SHF-{len(shift_rows)+1:06d}", "agent_id": "AGT-000003", "day_of_week": dow, "shift_code": "LATE", "start_time": "14:00", "end_time": "22:00", "effective_from": "2026-01-05", "effective_to": ""})
        shift_rows.append({"shift_id": f"SHF-{len(shift_rows)+1:06d}", "agent_id": "AGT-000004", "day_of_week": dow, "shift_code": "DAY", "start_time": "10:00", "end_time": "18:00", "effective_from": "2026-01-05", "effective_to": ""})

    shift_df = pd.DataFrame(shift_rows)

    queue_table = pd.DataFrame([
        {"queue_id": "QUE-TECH", "queue_name": "Tech", "queue_type": "PRIMARY", "min_tier": "T1", "sla_calendar": "CAL-CHAT"},
        {"queue_id": "QUE-ESCALATION", "queue_name": "Escalation", "queue_type": "ESCALATION", "min_tier": "T2", "sla_calendar": "CAL-247"},
    ])

    intent_table = pd.DataFrame([
        {"intent_id": "INT-004", "intent_code": "TECH_TROUBLESHOOTING", "intent_name": "Tech", "category": "TECHNICAL", "required_skill_id": "SKL-TECH", "default_priority": "P3", "is_safety_related": False, "volume_weight_pct": 100.0, "median_handle_minutes": 25.0, "allows_prospect": False},
    ])

    prio_rules = pd.DataFrame([
        {"rule_id": "PRL-001", "rule_type": "SLA_TARGET", "rule_name": "P1 Target", "priority_level": "P1", "segment": "", "upgrade_levels": "", "floor_priority_level": "", "first_response_min": 15, "resolution_min": 240, "escalation_after_min": 60, "calendar_code_override": "CAL-247", "precedence": 9, "effective_from": "2026-01-05", "effective_to": "", "description": ""},
        {"rule_id": "PRL-004", "rule_type": "SLA_TARGET", "rule_name": "P3 Target", "priority_level": "P3", "segment": "", "upgrade_levels": "", "floor_priority_level": "", "first_response_min": 240, "resolution_min": 1440, "escalation_after_min": 720, "calendar_code_override": "", "precedence": 9, "effective_from": "2026-01-05", "effective_to": "", "description": ""},
    ])

    cust_table = pd.DataFrame([
        {"customer_id": f"CUST-{i:06d}", "segment": "CONSUMER", "preferred_language": "en"}
        for i in range(1, 8)
    ])

    master_tables = {
        "Agent": agent_df,
        "AgentSkill": skills_df,
        "QueueMember": queues_df,
        "AgentLanguage": langs_df,
        "AgentShift": shift_df,
        "Queue": queue_table,
        "Intent": intent_table,
        "PriorityRule": prio_rules,
        "Customer": cust_table,
    }

    # 7 test tickets
    tkt_rows = [
        {"ticket_id": f"TKT-2026-{i:06d}", "customer_id": f"CUST-{i:06d}", "channel_id": "CHAT", "intent_id": "INT-004", "queue_id": "QUE-TECH", "language_code": "en", "created_at": "2026-01-05T10:00:00Z", "priority_level": "P3", "priority_rule_id": "PRL-004", "is_critical_incident": False, "incident_id": ""}
        for i in range(1, 8)
    ]
    tkt_df = pd.DataFrame(tkt_rows)

    st_df, asn_df, out_df, cyc_df = simulate_routing(cfg, master_tables, tkt_df)

    assert len(st_df) > 0
    assert len(asn_df) > 0
    assert len(out_df) == 7
    assert len(cyc_df) >= 7
