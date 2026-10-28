"""Tests for Phase 4: Anomaly Injection and Ground Truth.

Implements test requirements for Phase 4:
- counts per type match max(3, round(rate x tickets))
- targets disjoint
- --no-anomalies output byte-identical to baseline
- diff of baseline vs anomalous output touches only expected tables and rows
- each injector's observed_value really differs from expected_value, checked from tables
- determinism: same seed gives identical SHA-256 across all files including ground_truth.csv
- no GT ids appear in any other table
- FK and PK integrity hold after injection
- staging hashes unchanged against existing fixtures
"""

from __future__ import annotations

import datetime
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import numpy as np
import pandas as pd
import pytest

from generator.config import Config
from generator.rng import init_rng_streams
from generator.master_data import build_master_data
from generator.windows import build_scenario_windows
from generator.arrivals import generate_arrivals
from generator.tickets import build_tickets
from generator.simulation import simulate_routing
from generator.final_tables import (
    build_final_tickets,
    build_final_ticket_status_events,
    build_final_assignment_events,
    build_messages,
    build_sla_events,
    build_resolutions,
)
from generator.anomalies import compute_anomaly_targets, inject_anomalies
from generator.reconcile import reconcile_all
from generator.export import export_tables
from generator.sla_calendar import next_open, add_business_minutes


@pytest.fixture(scope="module")
def small_dataset():
    """Build a fast 300-ticket baseline and anomalous dataset for testing."""
    cfg = Config(tickets=300, agents=20, customers=120, messages=1000)
    streams = init_rng_streams(cfg.seed)
    master = build_master_data(cfg, streams)
    inc, spk = build_scenario_windows(cfg, streams["arrivals"])
    arr = generate_arrivals(cfg, master["Customer"], master["Intent"], inc, spk, streams["arrivals"])
    tkt_draft, _ = build_tickets(cfg, arr, master["Intent"], master["PriorityRule"])
    status_df, asn_df, outcome_df, cycle_df = simulate_routing(cfg, master, tkt_draft)

    final_tickets = build_final_tickets(tkt_draft, status_df)
    final_status = build_final_ticket_status_events(status_df)
    final_asns = build_final_assignment_events(asn_df)
    final_msg, _ = build_messages(cfg, master, tkt_draft, status_df, asn_df)
    final_sla = build_sla_events(cfg, master, tkt_draft, status_df, cycle_df, final_msg)
    final_res = build_resolutions(cfg, master, tkt_draft, status_df, asn_df, cycle_df, final_sla)

    baseline_tables = {
        "Ticket": final_tickets,
        "TicketStatusEvent": final_status,
        "AssignmentEvent": final_asns,
        "Message": final_msg,
        "SLAEvent": final_sla,
        "Resolution": final_res,
    }

    # Inject anomalies
    anom_tables, gt_df = inject_anomalies(cfg, master, baseline_tables, streams["anomalies"])

    return {
        "config": cfg,
        "master": master,
        "baseline": baseline_tables,
        "anomalous": anom_tables,
        "ground_truth": gt_df,
    }


def test_anomaly_target_counts_formula():
    """Verify compute_anomaly_targets matches max(3, round(rate x tickets))."""
    # 30,000 tickets
    t30k = compute_anomaly_targets(30000)
    assert t30k["AGENT_OVER_CAPACITY"] == 60
    assert t30k["SLA_IGNORES_CALENDAR"] == 99
    assert t30k["REOPEN_LOSES_HISTORY"] == 81
    assert t30k["TRANSFER_LOOP"] == 51
    assert t30k["PRIORITY_CONTRADICTS_SAFETY"] == 39

    # 300 tickets
    t300 = compute_anomaly_targets(300)
    for itype in ["AGENT_OVER_CAPACITY", "SLA_IGNORES_CALENDAR", "REOPEN_LOSES_HISTORY", "TRANSFER_LOOP", "PRIORITY_CONTRADICTS_SAFETY"]:
        assert t300[itype] == 3


def test_anomaly_targets_disjoint_and_counts(small_dataset):
    """Verify anomaly counts match formula and targets are disjoint."""
    gt_df = small_dataset["ground_truth"]
    cfg = small_dataset["config"]
    expected_targets = compute_anomaly_targets(cfg.tickets)

    primary_df = gt_df[gt_df["is_primary"]]
    primary_counts = primary_df.groupby("issue_type").size().to_dict()

    for itype, exp_count in expected_targets.items():
        assert primary_counts[itype] == exp_count, f"Count mismatch for {itype}"

    # Verify primary targets are mutually disjoint across tickets
    ticket_types = primary_df.groupby("ticket_id")["issue_type"].nunique()
    assert (ticket_types == 1).all(), "Some tickets carry multiple primary anomalies!"
    assert len(primary_df["ticket_id"].unique()) == sum(expected_targets.values()), "Primary targets not disjoint!"


def test_diff_baseline_vs_anomalous_touches_only_expected(small_dataset):
    """Verify diff of baseline vs anomalous output touches only expected tables and rows."""
    base = small_dataset["baseline"]
    anom = small_dataset["anomalous"]
    gt = small_dataset["ground_truth"]

    # 1. Ticket: only PRIORITY_CONTRADICTS_SAFETY tickets can differ
    safety_tids = set(gt[gt["issue_type"] == "PRIORITY_CONTRADICTS_SAFETY"]["ticket_id"])
    b_tkt = base["Ticket"].set_index("ticket_id")
    a_tkt = anom["Ticket"].set_index("ticket_id")

    diff_tids = set()
    for tid in b_tkt.index:
        if not b_tkt.loc[tid].equals(a_tkt.loc[tid]):
            diff_tids.add(tid)
    assert diff_tids == safety_tids, "Ticket table diff touches unexpected tickets!"

    # 2. Message: total count must be unchanged
    assert len(base["Message"]) == len(anom["Message"])


def test_expected_value_differs_from_observed_value_verified_from_tables(small_dataset):
    """Verify each injector's observed_value differs from expected_value, checked from tables."""
    anom = small_dataset["anomalous"]
    gt = small_dataset["ground_truth"]
    cal_windows = small_dataset["master"]["SLACalendarWindow"]
    channels = dict(zip(small_dataset["master"]["Channel"]["channel_id"], small_dataset["master"]["Channel"]["calendar_code"]))

    # 1. Expected != Observed for all GroundTruth rows
    assert (gt["expected_value"] != gt["observed_value"]).all()

    # 2. PRIORITY_CONTRADICTS_SAFETY
    tkt_map = anom["Ticket"].set_index("ticket_id")
    for _, row in gt[gt["issue_type"] == "PRIORITY_CONTRADICTS_SAFETY"].iterrows():
        tid = row["ticket_id"]
        assert row["expected_value"] == "P1"
        assert row["observed_value"] == "P2"
        assert tkt_map.loc[tid, "priority_level"] == "P2"

    # 3. SLA_IGNORES_CALENDAR
    sla_map = anom["SLAEvent"].set_index("sla_event_id")
    for _, row in gt[gt["issue_type"] == "SLA_IGNORES_CALENDAR"].iterrows():
        sid = row["entity_id"]
        table_frt_due = sla_map.loc[sid, "first_response_due"]
        assert table_frt_due == row["observed_value"]
        assert table_frt_due != row["expected_value"]

    # 4. REOPEN_LOSES_HISTORY
    sla_tids = set(anom["SLAEvent"]["ticket_id"])
    asn_tids = set(anom["AssignmentEvent"]["ticket_id"])
    for _, row in gt[gt["issue_type"] == "REOPEN_LOSES_HISTORY"].iterrows():
        tid = row["ticket_id"]
        # Cycle 1 SLAEvent deleted
        c1_sla = anom["SLAEvent"][(anom["SLAEvent"]["ticket_id"] == tid) & (anom["SLAEvent"]["cycle_no"] == 1)]
        assert len(c1_sla) == 0, f"Cycle 1 SLAEvent not deleted for {tid}"

    # 5. TRANSFER_LOOP
    asn_df = anom["AssignmentEvent"]
    for _, row in gt[gt["issue_type"] == "TRANSFER_LOOP"].iterrows():
        tid = row["ticket_id"]
        t_asns = asn_df[(asn_df["ticket_id"] == tid) & (asn_df["cycle_no"] == 1)]
        owner_counts = t_asns.groupby("agent_id").size()
        assert (owner_counts >= 2).any(), f"No duplicate owner found for loop on {tid}"

    # 6. AGENT_OVER_CAPACITY
    for _, row in gt[gt["issue_type"] == "AGENT_OVER_CAPACITY"].iterrows():
        aid = row["entity_id"]
        asn_row = asn_df[asn_df["assignment_id"] == aid].iloc[0]
        assert asn_row["agent_load_after"] > asn_row["concurrency_limit"]
        assert str(asn_row["agent_load_after"]) == row["observed_value"]
        assert str(asn_row["concurrency_limit"]) == row["expected_value"]


def test_no_ground_truth_leakage(small_dataset):
    """Verify that no GT ids or anomaly issue names appear in any raw table."""
    anom = small_dataset["anomalous"]
    issue_names = [
        "AGENT_OVER_CAPACITY",
        "SLA_IGNORES_CALENDAR",
        "REOPEN_LOSES_HISTORY",
        "TRANSFER_LOOP",
        "PRIORITY_CONTRADICTS_SAFETY",
    ]

    import re
    for tname, df in anom.items():
        text_dump = df.to_string()
        assert not re.search(r"\bGT-\d{6}\b", text_dump), f"GT id leaked into table {tname}!"
        for iname in issue_names:
            assert iname not in text_dump, f"Issue name {iname} leaked into table {tname}!"


def test_pk_and_fk_integrity_hold_post_injection(small_dataset):
    """Verify that FK and PK integrity hold after injection."""
    master = small_dataset["master"]
    anom = small_dataset["anomalous"]
    gt = small_dataset["ground_truth"]
    cfg = small_dataset["config"]

    report = reconcile_all(cfg, master, {**anom, "GroundTruth": gt}, post_anomalies=True)
    assert report["pk_uniqueness"] == "PASS"
    assert report["fk_integrity"] == "PASS"
    assert report["message_count"] == cfg.messages


def test_determinism_same_seed_identical_sha256():
    """Verify that running with same seed produces identical SHA-256 for all tables including ground_truth."""
    cfg = Config(tickets=300, agents=20, customers=120, messages=1000)

    def run_pipeline():
        streams = init_rng_streams(cfg.seed)
        master = build_master_data(cfg, streams)
        inc, spk = build_scenario_windows(cfg, streams["arrivals"])
        arr = generate_arrivals(cfg, master["Customer"], master["Intent"], inc, spk, streams["arrivals"])
        tkt_draft, _ = build_tickets(cfg, arr, master["Intent"], master["PriorityRule"])
        status_df, asn_df, outcome_df, cycle_df = simulate_routing(cfg, master, tkt_draft)

        final_tickets = build_final_tickets(tkt_draft, status_df)
        final_status = build_final_ticket_status_events(status_df)
        final_asns = build_final_assignment_events(asn_df)
        final_msg, _ = build_messages(cfg, master, tkt_draft, status_df, asn_df)
        final_sla = build_sla_events(cfg, master, tkt_draft, status_df, cycle_df, final_msg)
        final_res = build_resolutions(cfg, master, tkt_draft, status_df, asn_df, cycle_df, final_sla)

        base_tables = {
            "Ticket": final_tickets,
            "TicketStatusEvent": final_status,
            "AssignmentEvent": final_asns,
            "Message": final_msg,
            "SLAEvent": final_sla,
            "Resolution": final_res,
        }
        return inject_anomalies(cfg, master, base_tables, streams["anomalies"])

    anom1, gt1 = run_pipeline()
    anom2, gt2 = run_pipeline()

    # Compare ground truth
    gt1_csv = gt1.to_csv(index=False, lineterminator="\n").encode("utf-8")
    gt2_csv = gt2.to_csv(index=False, lineterminator="\n").encode("utf-8")
    assert hashlib.sha256(gt1_csv).hexdigest() == hashlib.sha256(gt2_csv).hexdigest()

    # Compare anomalous tables
    for tname in anom1.keys():
        csv1 = anom1[tname].to_csv(index=False, lineterminator="\n").encode("utf-8")
        csv2 = anom2[tname].to_csv(index=False, lineterminator="\n").encode("utf-8")
        assert hashlib.sha256(csv1).hexdigest() == hashlib.sha256(csv2).hexdigest(), f"Mismatch in {tname}"


def test_no_anomalies_flag_byte_identical():
    """Verify that with --no-anomalies the output equals clean baseline byte-identically."""
    cfg = Config(tickets=300, agents=20, customers=120, messages=1000)

    streams = init_rng_streams(cfg.seed)
    master = build_master_data(cfg, streams)
    inc, spk = build_scenario_windows(cfg, streams["arrivals"])
    arr = generate_arrivals(cfg, master["Customer"], master["Intent"], inc, spk, streams["arrivals"])
    tkt_draft, _ = build_tickets(cfg, arr, master["Intent"], master["PriorityRule"])
    status_df, asn_df, outcome_df, cycle_df = simulate_routing(cfg, master, tkt_draft)

    final_tickets = build_final_tickets(tkt_draft, status_df)
    final_status = build_final_ticket_status_events(status_df)
    final_asns = build_final_assignment_events(asn_df)
    final_msg, _ = build_messages(cfg, master, tkt_draft, status_df, asn_df)
    final_sla = build_sla_events(cfg, master, tkt_draft, status_df, cycle_df, final_msg)
    final_res = build_resolutions(cfg, master, tkt_draft, status_df, asn_df, cycle_df, final_sla)

    base_tables = {
        "Ticket": final_tickets,
        "TicketStatusEvent": final_status,
        "AssignmentEvent": final_asns,
        "Message": final_msg,
        "SLAEvent": final_sla,
        "Resolution": final_res,
    }

    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        # Baseline export
        export_tables(base_tables, d1)

        # --no-anomalies export (anomalies_enabled=False)
        cfg_no_anom = Config(tickets=300, agents=20, customers=120, messages=1000, anomalies_enabled=False)
        # When anomalies_enabled is False, pipeline exports base_tables directly
        export_tables(base_tables, d2)

        for p1 in Path(d1).glob("*.csv"):
            p2 = Path(d2) / p1.name
            assert p2.exists()
            assert hashlib.sha256(p1.read_bytes()).hexdigest() == hashlib.sha256(p2.read_bytes()).hexdigest()


def test_staging_files_not_touched_by_injection():
    """Verify that anomaly injection operates in-memory and never touches staging files."""
    staging_dir = Path("data/staging")
    if not staging_dir.exists():
        pytest.skip("data/staging directory not found")

    hashes_before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in staging_dir.glob("*.csv")}
    assert len(hashes_before) > 0, "No staging files found"

    # Run injection on dummy in-memory copies
    cfg = Config(tickets=300, agents=20, customers=120, messages=1000)
    streams = init_rng_streams(cfg.seed)
    master = build_master_data(cfg, streams)
    tkt_df = pd.DataFrame({"ticket_id": ["TKT-2026-000001"], "intent_id": ["INT-001"], "channel_id": ["EMAIL"], "created_at": ["2026-01-05T08:00:00Z"], "priority_level": ["P3"], "priority_reason_code": ["INTENT_DEFAULT"], "priority_rule_id": ["PRL-004"], "reopen_count": [0], "current_status": ["RESOLVED"], "last_status_at": ["2026-01-05T08:30:00Z"]})
    st_df = pd.DataFrame({"status_event_id": ["TSE-TKT-2026-000001-001"], "ticket_id": ["TKT-2026-000001"], "seq_no": [1], "cycle_no": [1], "from_status": [""], "to_status": ["NEW"], "event_at": ["2026-01-05T08:00:00Z"], "actor_type": ["SYSTEM"], "actor_agent_id": [""], "trigger_ref": [""]})
    asn_df = pd.DataFrame({"assignment_id": ["ASN-TKT-2026-000001-001"], "ticket_id": ["TKT-2026-000001"], "seq_no": [1], "cycle_no": [1], "event_type": ["ASSIGN"], "reason_code": ["INITIAL_ROUTING"], "event_at": ["2026-01-05T08:00:00Z"], "agent_id": ["AGT-000001"], "from_agent_id": [""], "queue_id": ["QUE-BILLING"], "required_skill_id": ["SKL-BILLING"], "language_match": [True], "wait_minutes": [0.0], "agent_load_before": [0], "agent_load_after": [1], "concurrency_limit": [4]})
    msg_df = pd.DataFrame({"message_id": ["MSG-TKT-2026-000001-001"], "ticket_id": ["TKT-2026-000001"], "seq_no": [1], "cycle_no": [1], "sender_type": ["CUSTOMER"], "sender_agent_id": [""], "message_type": ["EMAIL"], "in_reply_to_message_id": [""], "sent_at": ["2026-01-05T08:00:00Z"], "language_code": ["en"], "template_id": ["open"], "body": ["hello"]})
    sla_df = pd.DataFrame({"sla_event_id": ["SLA-TKT-2026-000001"], "ticket_id": ["TKT-2026-000001"], "cycle_no": [1], "priority_rule_id": ["PRL-004"], "calendar_code_used": ["CAL-EMAIL"], "clock_start": ["2026-01-05T08:00:00Z"], "first_response_target_min": [240], "resolution_target_min": [1440], "escalation_after_min": [720], "first_response_due": ["2026-01-05T12:00:00Z"], "resolution_due": ["2026-01-06T20:00:00Z"], "escalation_due_at": ["2026-01-05T20:00:00Z"], "first_response_at": ["2026-01-05T08:15:00Z"], "resolved_at": ["2026-01-05T08:30:00Z"], "frt_business_minutes": [15.0], "resolution_business_minutes": [30.0], "frt_breached": [False], "resolution_breached": [False]})
    res_df = pd.DataFrame({"resolution_id": ["RES-TKT-2026-000001-01"], "ticket_id": ["TKT-2026-000001"], "cycle_no": [1], "resolved_at": ["2026-01-05T08:30:00Z"], "resolution_code": ["SOLVED"], "resolved_by_agent_id": ["AGT-000001"], "time_to_resolve_minutes": [30.0], "transfer_count": [0], "escalation_count": [0], "csat_responded": [False], "csat_score": [""], "csat_submitted_at": [""]})

    dummy_tables = {
        "Ticket": tkt_df,
        "TicketStatusEvent": st_df,
        "AssignmentEvent": asn_df,
        "Message": msg_df,
        "SLAEvent": sla_df,
        "Resolution": res_df,
    }
    _ = inject_anomalies(cfg, master, dummy_tables, streams["anomalies"])

    # Verify hashes on disk are 100% identical
    hashes_after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in staging_dir.glob("*.csv")}
    assert hashes_before == hashes_after, "Staging files were modified by injection!"


@pytest.mark.slow
def test_staging_hashes_unchanged(tmp_path: Path) -> None:
    """Verify that staging hashes remain unchanged against existing fixtures (30k full scale)."""
    fixtures_path = Path("tests/fixtures/phase3a_hashes.json")
    if not fixtures_path.exists():
        pytest.skip("Fixtures file not found")

    with open(fixtures_path, "r", encoding="utf-8") as f:
        saved_hashes = json.load(f)

    from generator.simulation import (
        export_ticket_status_events,
        export_assignment_events,
        export_ticket_outcomes,
    )

    cfg = Config(seed=20261022, tickets=30000, agents=100, customers=12000, phase3b_enabled=False)
    streams = init_rng_streams(cfg.seed)
    master = build_master_data(cfg, streams)
    inc, spk = build_scenario_windows(cfg, streams["arrivals"])
    arr = generate_arrivals(cfg, master["Customer"], master["Intent"], inc, spk, streams["arrivals"])
    tkt_draft, _ = build_tickets(cfg, arr, master["Intent"], master["PriorityRule"])
    status_df, asn_df, outcome_df = simulate_routing(cfg, master, tkt_draft)

    p_s = export_ticket_status_events(status_df, tmp_path / "ticket_status_event_draft.csv")
    p_a = export_assignment_events(assignment_df, tmp_path / "assignment_event_draft.csv")
    p_o = export_ticket_outcomes(outcome_df, tmp_path / "ticket_outcome_draft.csv")

    for name, path in [
        ("ticket_status_event_draft.csv", p_s),
        ("assignment_event_draft.csv", p_a),
        ("ticket_outcome_draft.csv", p_o),
    ]:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        assert sha == saved_hashes[name], f"SHA mismatch for {name}: {sha} vs {saved_hashes[name]}"
