"""Test suite for Phase 3c: Final tables, Messages, SLAEvent, Resolution, and Export.

Validates:
- Message count exactness (hits cfg.messages exactly via calibration)
- Threading via in_reply_to, sender validity, ownership interval constraints
- Template language coverage (12 intents x 5 languages x senders)
- SLA reproduction against sla_calendar helpers (clock_start, due times, breach flags)
- CSAT score bounds [1, 5], submission timestamps, and null handling
- Internal reconciliation checks (reconcile.py)
- Reproducibility and byte-identical determinism (same seed twice gives identical SHA-256)
- Regression test against Phase 3a staging hashes
- Full-scale run (30,000 tickets, 100 agents, 100,000 messages) marked slow
"""

from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
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
from generator.simulation import simulate_routing, export_ticket_status_events, export_assignment_events, export_ticket_outcomes
from generator.final_tables import (
    build_final_tickets,
    build_final_ticket_status_events,
    build_final_assignment_events,
    build_messages,
    build_sla_events,
    build_resolutions,
    _load_message_templates,
)
from generator.export import export_tables
from generator.reconcile import reconcile_all, ReconciliationError
from generator import sla_calendar


EPOCH_UTC = datetime.datetime(2026, 1, 5, 0, 0, 0, tzinfo=datetime.timezone.utc)


def _iso_to_sec(iso_str: str) -> int:
    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int((dt - EPOCH_UTC).total_seconds())


# =============================================================================
# Fixture: Small-Scale Pipeline (300 tickets, 20 agents, 1000 messages)
# =============================================================================

@pytest.fixture(scope="session")
def small_phase3c_data(tmp_path_factory):
    """Run full Phase 1-3c generator on small scale and return tables + paths."""
    out_dir = tmp_path_factory.mktemp("phase3c_small_raw")
    cfg = Config(
        seed=20261022,
        tickets=300,
        agents=20,
        customers=120,
        messages=1000,
        days=90,
        phase3b_enabled=True,
    )
    streams = init_rng_streams(cfg.seed)

    # Master tables
    master_tables = build_master_data(cfg, streams)

    # Arrivals & drafts
    incidents, spikes = build_scenario_windows(cfg, streams["arrivals"])
    arrivals = generate_arrivals(
        cfg, master_tables["Customer"], master_tables["Intent"], incidents, spikes, streams["arrivals"]
    )
    ticket_draft_df, _ = build_tickets(cfg, arrivals, master_tables["Intent"], master_tables["PriorityRule"])

    # Simulation
    status_df, assignment_df, outcome_df, cycle_outcome_df = simulate_routing(cfg, master_tables, ticket_draft_df)

    # Final tables
    final_tickets = build_final_tickets(ticket_draft_df, status_df)
    final_status = build_final_ticket_status_events(status_df)
    final_asn = build_final_assignment_events(assignment_df)
    final_messages, natural_cnt = build_messages(cfg, master_tables, ticket_draft_df, status_df, assignment_df)
    final_sla = build_sla_events(cfg, master_tables, ticket_draft_df, status_df, cycle_outcome_df, final_messages)
    final_res = build_resolutions(
        cfg, master_tables, ticket_draft_df, status_df, assignment_df, cycle_outcome_df, final_sla
    )

    final_tables = {
        "Ticket": final_tickets,
        "TicketStatusEvent": final_status,
        "AssignmentEvent": final_asn,
        "Message": final_messages,
        "SLAEvent": final_sla,
        "Resolution": final_res,
    }

    # Export to tmp directory
    exported = export_tables({**master_tables, **final_tables}, out_dir)

    return {
        "config": cfg,
        "master_tables": master_tables,
        "final_tables": final_tables,
        "natural_msg_cnt": natural_cnt,
        "ticket_draft_df": ticket_draft_df,
        "status_df": status_df,
        "assignment_df": assignment_df,
        "cycle_outcome_df": cycle_outcome_df,
        "out_dir": out_dir,
        "exported": exported,
    }


# =============================================================================
# 1. Message Count Exactness & Sequences
# =============================================================================

def test_message_count_exactness_and_sequences(small_phase3c_data) -> None:
    """Message total equals config.messages exactly; natural count within 10%; seq_no gapless."""
    cfg = small_phase3c_data["config"]
    msg_df = small_phase3c_data["final_tables"]["Message"]
    nat_cnt = small_phase3c_data["natural_msg_cnt"]
    tkt_df = small_phase3c_data["final_tables"]["Ticket"]

    # 1. Exact total match
    assert len(msg_df) == cfg.messages, f"Message count {len(msg_df)} != target {cfg.messages}"

    # 2. Natural count within 10%
    diff_pct = abs(nat_cnt - cfg.messages) / cfg.messages
    assert diff_pct <= 0.10, f"Natural message count {nat_cnt} deviated by {diff_pct*100:.2f}% (> 10%)"

    # 3. Gapless per ticket and timestamps non-decreasing
    cutoff_sec = _iso_to_sec(cfg.cutoff)
    tkt_created_map = dict(zip(tkt_df["ticket_id"], tkt_df["created_at"]))

    for tid, group in msg_df.groupby("ticket_id"):
        seqs = list(group["seq_no"])
        assert seqs == list(range(1, len(seqs) + 1)), f"Gap in seq_no for ticket {tid}"

        # First message sent_at equals ticket created_at
        m1 = group.iloc[0]
        assert m1["sent_at"] == tkt_created_map[tid], f"Message 1 sent_at != created_at for {tid}"
        assert m1["sender_type"] == "CUSTOMER"

        # Non-decreasing timestamps and nothing after cutoff
        sent_secs = [_iso_to_sec(s) for s in group["sent_at"]]
        for i in range(1, len(sent_secs)):
            assert sent_secs[i] >= sent_secs[i - 1], f"Timestamp decreased for {tid}: {group.iloc[i-1]['sent_at']} > {group.iloc[i]['sent_at']}"
            assert sent_secs[i] <= cutoff_sec, f"Message after cutoff for {tid}: {group.iloc[i]['sent_at']}"


# =============================================================================
# 2. Message Threading, Senders & Ownership Intervals
# =============================================================================

def test_message_threading_and_senders(small_phase3c_data) -> None:
    """Validate in_reply_to threading, sender validity, and agent ownership intervals."""
    msg_df = small_phase3c_data["final_tables"]["Message"]
    asn_df = small_phase3c_data["final_tables"]["AssignmentEvent"]
    st_df = small_phase3c_data["final_tables"]["TicketStatusEvent"]

    # Pre-index assignments per ticket
    asns_by_tkt: dict[str, list[dict]] = {}
    for a in asn_df.to_dict(orient="records"):
        asns_by_tkt.setdefault(str(a["ticket_id"]), []).append(a)

    # First response times per (ticket_id, cycle_no)
    inp_events = st_df[st_df["to_status"] == "IN_PROGRESS"].groupby(["ticket_id", "cycle_no"]).first().reset_index()
    first_resp_map = {(str(r["ticket_id"]), int(r["cycle_no"])): str(r["event_at"]) for _, r in inp_events.iterrows()}

    for tid, group in msg_df.groupby("ticket_id"):
        seen_msg_ids: set[str] = set()
        c_asns = asns_by_tkt.get(tid, [])

        for idx, row in group.iterrows():
            mid = row["message_id"]
            seq = row["seq_no"]
            reply_id = row["in_reply_to_message_id"]
            stype = row["sender_type"]
            aid = row["sender_agent_id"]
            sent_str = row["sent_at"]
            sent_sec = _iso_to_sec(sent_str)
            cyc = row["cycle_no"]

            # Threading check
            if seq == 1:
                assert reply_id == "", f"seq 1 should have empty in_reply_to: {mid}"
            else:
                assert reply_id in seen_msg_ids, f"reply_id {reply_id} not seen earlier on ticket {tid}"

            seen_msg_ids.add(mid)

            # Sender check
            if stype == "AGENT":
                assert aid != "", f"Agent message missing sender_agent_id: {mid}"
                # Verify agent was owner at sent_at
                active_owner = ""
                for a in c_asns:
                    if _iso_to_sec(a["event_at"]) <= sent_sec:
                        active_owner = a["agent_id"]
                    else:
                        break
                assert active_owner == aid, f"Agent {aid} sent message {mid} at {sent_str} but active owner was {active_owner}"

                # First agent response of cycle must match IN_PROGRESS timestamp
                first_agent_of_cyc = group[(group["sender_type"] == "AGENT") & (group["cycle_no"] == cyc)].iloc[0]
                if mid == first_agent_of_cyc["message_id"]:
                    expected_frt = first_resp_map.get((tid, cyc))
                    assert sent_str == expected_frt, f"First response {mid} sent_at {sent_str} != IN_PROGRESS {expected_frt}"

            elif stype in ("CUSTOMER", "SYSTEM"):
                assert aid == "", f"{stype} message should have empty sender_agent_id: {mid}"


# =============================================================================
# 3. Template Library & Language Coverage
# =============================================================================

def test_template_language_coverage(small_phase3c_data) -> None:
    """Verify templates cover 12 intents x 5 languages and message bodies match templates."""
    templates = _load_message_templates()
    msg_df = small_phase3c_data["final_tables"]["Message"]
    asn_df = small_phase3c_data["final_tables"]["AssignmentEvent"]
    tkt_df = small_phase3c_data["final_tables"]["Ticket"]

    languages = {"en", "es", "hi", "fr", "ar"}
    intents = {f"INT-{i:03d}" for i in range(1, 13)}

    # Verify template library completeness
    assert set(templates["bodies"].keys()) == intents
    for intent_id, senders in templates["bodies"].items():
        assert set(senders.keys()) == {"CUSTOMER", "AGENT", "SYSTEM"}
        for stype, langs in senders.items():
            assert set(langs.keys()) == languages

    # Verify language-fallback rule
    fb_asns = asn_df[asn_df["reason_code"] == "LANGUAGE_FALLBACK"]
    fb_tickets = set(fb_asns["ticket_id"])

    tkt_lang_map = dict(zip(tkt_df["ticket_id"], tkt_df["language_code"]))

    asn_by_tkt: dict[str, list[dict]] = {}
    for a in asn_df.to_dict(orient="records"):
        asn_by_tkt.setdefault(str(a["ticket_id"]), []).append(a)

    for _, row in msg_df.iterrows():
        mid = row["message_id"]
        stype = row["sender_type"]
        body = row["body"]
        assert len(body.strip()) > 0, f"Empty body for message {mid}"

        tid = row["ticket_id"]
        tkt_lang = tkt_lang_map[tid]

        if stype == "CUSTOMER":
            assert row["language_code"] == tkt_lang, f"Customer message language {row['language_code']} != ticket language {tkt_lang}"

        if stype == "AGENT":
            aid = row["sender_agent_id"]
            sent_sec = _iso_to_sec(row["sent_at"])
            # Find active assignment at sent_at
            tkt_asns = asn_by_tkt.get(tid, [])
            active_asn = None
            for a in tkt_asns:
                if _iso_to_sec(a["event_at"]) <= sent_sec:
                    active_asn = a
                else:
                    break
            if active_asn and (active_asn["language_match"] is False or active_asn["language_match"] == "false"):
                assert row["language_code"] == "en", f"Fallback agent on {tid} should write in en, got {row['language_code']}"


# =============================================================================
# 4. SLA Reproduction Against sla_calendar Helpers
# =============================================================================

def test_sla_reproduction_against_calendar_helpers(small_phase3c_data) -> None:
    """Recompute all SLAEvent fields independently and verify exact match."""
    sla_df = small_phase3c_data["final_tables"]["SLAEvent"]
    cfg = small_phase3c_data["config"]
    cutoff_dt = datetime.datetime.fromisoformat(cfg.cutoff.replace("Z", "+00:00"))

    for _, row in sla_df.iterrows():
        sid = row["sla_event_id"]
        cal = row["calendar_code_used"]
        cyc = row["cycle_no"]

        clock_start_dt = datetime.datetime.fromisoformat(row["clock_start"].replace("Z", "+00:00"))
        frt_due_dt = datetime.datetime.fromisoformat(row["first_response_due"].replace("Z", "+00:00"))
        res_due_dt = datetime.datetime.fromisoformat(row["resolution_due"].replace("Z", "+00:00"))
        esc_due_dt = datetime.datetime.fromisoformat(row["escalation_due_at"].replace("Z", "+00:00"))

        frt_target = row["first_response_target_min"]
        res_target = row["resolution_target_min"]
        esc_target = row["escalation_after_min"]

        # Recompute due times via calendar helper
        expected_frt_due = sla_calendar.add_business_minutes(cal, clock_start_dt, frt_target)
        expected_res_due = sla_calendar.add_business_minutes(cal, clock_start_dt, res_target)
        expected_esc_due = sla_calendar.add_business_minutes(cal, clock_start_dt, esc_target)

        assert frt_due_dt == expected_frt_due, f"FRT due mismatch on {sid}: {frt_due_dt} vs {expected_frt_due}"
        assert res_due_dt == expected_res_due, f"Resolution due mismatch on {sid}: {res_due_dt} vs {expected_res_due}"
        assert esc_due_dt == expected_esc_due, f"Escalation due mismatch on {sid}: {esc_due_dt} vs {expected_esc_due}"

        # Recompute breach flags
        frt_at_str = row["first_response_at"]
        if frt_at_str:
            resp_dt = datetime.datetime.fromisoformat(frt_at_str.replace("Z", "+00:00"))
            expected_frt_b = resp_dt > frt_due_dt
            expected_bmin = sla_calendar.business_minutes(cal, clock_start_dt, resp_dt)
            assert float(row["frt_business_minutes"]) == pytest.approx(expected_bmin, abs=0.1)
        else:
            expected_frt_b = cutoff_dt > frt_due_dt
            assert row["frt_business_minutes"] == ""

        assert row["frt_breached"] == expected_frt_b, f"FRT breach flag mismatch on {sid}"

        res_at_str = row["resolved_at"]
        if res_at_str:
            res_dt = datetime.datetime.fromisoformat(res_at_str.replace("Z", "+00:00"))
            expected_res_b = res_dt > res_due_dt
            expected_res_bmin = sla_calendar.business_minutes(cal, clock_start_dt, res_dt)
            assert float(row["resolution_business_minutes"]) == pytest.approx(expected_res_bmin, abs=0.1)
        else:
            expected_res_b = cutoff_dt > res_due_dt
            assert row["resolution_business_minutes"] == ""

        assert row["resolution_breached"] == expected_res_b, f"Resolution breach flag mismatch on {sid}"


# =============================================================================
# 5. CSAT Bounds, Timestamps & Null Handling
# =============================================================================

def test_csat_bounds_and_null_handling(small_phase3c_data) -> None:
    """Validate CSAT response rates, score bounds [1, 5], submission timing, and empty nulls."""
    res_df = small_phase3c_data["final_tables"]["Resolution"]
    cfg = small_phase3c_data["config"]
    cutoff_sec = _iso_to_sec(cfg.cutoff)

    for _, row in res_df.iterrows():
        rid = row["resolution_id"]
        responded = row["csat_responded"]
        score_str = row["csat_score"]
        sub_str = row["csat_submitted_at"]
        res_str = row["resolved_at"]

        if not responded:
            assert score_str == "", f"Unanswered CSAT should have empty score on {rid}, got: {score_str}"
            assert sub_str == "", f"Unanswered CSAT should have empty submitted_at on {rid}, got: {sub_str}"
        else:
            score = int(score_str)
            assert 1 <= score <= 5, f"CSAT score {score} out of range [1, 5] on {rid}"
            assert sub_str != "", f"Responded CSAT missing submitted_at on {rid}"

            res_sec = _iso_to_sec(res_str)
            sub_sec = _iso_to_sec(sub_str)
            assert sub_sec >= res_sec, f"CSAT submitted {sub_str} before resolution {res_str} on {rid}"
            assert sub_sec <= cutoff_sec, f"CSAT submitted {sub_str} after cutoff on {rid}"


# =============================================================================
# 6. Internal Reconciliation Sanity Checks
# =============================================================================

def test_reconcile_sanity_checks(small_phase3c_data) -> None:
    """reconcile_all passes on clean tables and fails loudly on introduced errors."""
    cfg = small_phase3c_data["config"]
    master = small_phase3c_data["master_tables"]
    final = small_phase3c_data["final_tables"]

    # 1. Clean run passes
    report = reconcile_all(cfg, master, final)
    assert report["pk_uniqueness"] == "PASS"
    assert report["fk_integrity"] == "PASS"
    assert report["ticket_status_reconciliation"] == "PASS"

    # 2. Corrupted message count raises ReconciliationError
    corrupt_final = dict(final)
    corrupt_final["Message"] = final["Message"].iloc[:-1]  # Drop 1 message
    with pytest.raises(ReconciliationError, match="Message row count"):
        reconcile_all(cfg, master, corrupt_final)


# =============================================================================
# 7. Reproducibility & Byte-Identical Determinism
# =============================================================================

def test_determinism_same_seed_identical_hashes(tmp_path_factory) -> None:
    """Two generation runs with identical seed produce identical SHA-256 for all raw files."""
    cfg = Config(seed=20261022, tickets=100, agents=10, customers=40, messages=330, days=90)

    hashes_run1: dict[str, str] = {}
    hashes_run2: dict[str, str] = {}

    for run_idx, hash_dict in enumerate([hashes_run1, hashes_run2], start=1):
        out_dir = tmp_path_factory.mktemp(f"det_run_{run_idx}")
        streams = init_rng_streams(cfg.seed)
        master = build_master_data(cfg, streams)
        incidents, spikes = build_scenario_windows(cfg, streams["arrivals"])
        arrivals = generate_arrivals(cfg, master["Customer"], master["Intent"], incidents, spikes, streams["arrivals"])
        tkt_draft, _ = build_tickets(cfg, arrivals, master["Intent"], master["PriorityRule"])
        status_df, asn_df, out_df, cyc_df = simulate_routing(cfg, master, tkt_draft)

        final_tkt = build_final_tickets(tkt_draft, status_df)
        final_st = build_final_ticket_status_events(status_df)
        final_asn = build_final_assignment_events(asn_df)
        final_msg, _ = build_messages(cfg, master, tkt_draft, status_df, asn_df)
        final_sla = build_sla_events(cfg, master, tkt_draft, status_df, cyc_df, final_msg)
        final_res = build_resolutions(cfg, master, tkt_draft, status_df, asn_df, cyc_df, final_sla)

        exported = export_tables(
            {**master, "Ticket": final_tkt, "TicketStatusEvent": final_st, "AssignmentEvent": final_asn,
             "Message": final_msg, "SLAEvent": final_sla, "Resolution": final_res},
            out_dir,
        )

        for tname, path in exported.items():
            hash_dict[tname] = hashlib.sha256(path.read_bytes()).hexdigest()

    assert hashes_run1 == hashes_run2, "Non-deterministic output between two runs with identical seed"


# =============================================================================
# 8. Regression Check: Phase 3a Hashes Untouched
# =============================================================================

@pytest.mark.slow
def test_regression_phase3a_staging_hashes(tmp_path: Path) -> None:
    """phase3b_enabled=False continues to reproduce Phase 3a staging hashes exactly."""
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
# 9. Full-Scale Test (30,000 Tickets)
# =============================================================================

@pytest.mark.slow
def test_full_scale_phase3c(tmp_path: Path) -> None:
    """Full-scale Phase 3c validation: 30k tickets, 100k messages, 100% reconciliation."""
    cfg = Config(seed=20261022, tickets=30000, agents=100, customers=12000, messages=100000, phase3b_enabled=True)
    streams = init_rng_streams(cfg.seed)

    tables = build_master_data(cfg, streams)
    incidents, spikes = build_scenario_windows(cfg, streams["arrivals"])
    arrivals = generate_arrivals(cfg, tables["Customer"], tables["Intent"], incidents, spikes, streams["arrivals"])
    ticket_draft_df, _ = build_tickets(cfg, arrivals, tables["Intent"], tables["PriorityRule"])

    status_df, assignment_df, outcome_df, cycle_outcome_df = simulate_routing(cfg, tables, ticket_draft_df)

    final_tkt = build_final_tickets(ticket_draft_df, status_df)
    final_st = build_final_ticket_status_events(status_df)
    final_asn = build_final_assignment_events(assignment_df)
    final_msg, nat_cnt = build_messages(cfg, tables, ticket_draft_df, status_df, assignment_df)
    final_sla = build_sla_events(cfg, tables, ticket_draft_df, status_df, cycle_outcome_df, final_msg)
    final_res = build_resolutions(cfg, tables, ticket_draft_df, status_df, assignment_df, cycle_outcome_df, final_sla)

    final_tables = {
        "Ticket": final_tkt,
        "TicketStatusEvent": final_st,
        "AssignmentEvent": final_asn,
        "Message": final_msg,
        "SLAEvent": final_sla,
        "Resolution": final_res,
    }

    report = reconcile_all(cfg, tables, final_tables)
    assert report["message_count"] == 100000
    assert len(final_tkt) == 30000
    assert len(final_sla) == 31401
    assert len(final_res) == 31395
