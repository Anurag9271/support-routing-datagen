"""Fast tests for independent validator (validation/validate.py).

Implements Part 1 test requirements at small scale (300 tickets, 20 agents), not slow:
- Clean baseline data produces 0 findings under integrity mode
- Injected anomalous data produces Precision = 1.0, Recall = 1.0 per issue type under evaluate mode
- Hand-corrupted scenarios in 5 rule families are correctly flagged
"""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import pytest
import pandas as pd

from generator.config import Config
from generator.rng import init_rng_streams
from generator.master_data import build_master_data
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
from generator.anomalies import inject_anomalies
from generator.export import export_tables
from validation.validate import Validator, evaluate_findings


@pytest.fixture(scope="module")
def small_scale_environments(tmp_path_factory: pytest.TempPathFactory):
    """Generate small scale clean baseline and anomalous datasets (300 tickets, 20 agents)."""
    base_dir = tmp_path_factory.mktemp("test_val_env")
    clean_dir = base_dir / "clean"
    anom_dir = base_dir / "anom"
    gt_dir = base_dir / "gt"

    cfg = Config(seed=20261022, tickets=300, agents=20, customers=120, messages=1000)
    streams = init_rng_streams(cfg.seed)

    master = build_master_data(cfg, streams)
    arrivals = generate_arrivals(cfg, master["Customer"], master["Intent"], [], [], streams["arrivals"])
    tkt_draft, _ = build_tickets(cfg, arrivals, master["Intent"], master["PriorityRule"])

    status_df, asn_df, outcome_df, cyc_df = simulate_routing(cfg, master, tkt_draft)

    final_tkts = build_final_tickets(tkt_draft, status_df)
    final_st = build_final_ticket_status_events(status_df)
    final_asn = build_final_assignment_events(asn_df)
    final_msg, _ = build_messages(cfg, master, tkt_draft, status_df, asn_df)
    final_sla = build_sla_events(cfg, master, tkt_draft, status_df, cyc_df, final_msg)
    final_res = build_resolutions(cfg, master, tkt_draft, status_df, asn_df, cyc_df, final_sla)

    clean_tables = {
        **master,
        "Ticket": final_tkts,
        "TicketStatusEvent": final_st,
        "AssignmentEvent": final_asn,
        "Message": final_msg,
        "SLAEvent": final_sla,
        "Resolution": final_res,
    }
    export_tables(clean_tables, clean_dir)

    # Injected anomalies
    anom_tables, gt_df = inject_anomalies(cfg, master, clean_tables, streams["anomalies"])
    all_anom_tables = {**master, **anom_tables}
    export_tables(all_anom_tables, anom_dir)

    gt_path = gt_dir / "ground_truth.csv"
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_export = gt_df.copy()
    gt_export["is_primary"] = gt_export["is_primary"].map({True: "true", False: "false"})
    gt_export.to_csv(gt_path, index=False, encoding="utf-8", lineterminator="\n")

    return {
        "clean_dir": clean_dir,
        "anom_dir": anom_dir,
        "gt_path": gt_path,
        "gt_df": gt_df,
    }


def test_validator_clean_baseline_gives_zero_findings(small_scale_environments):
    """Clean data generated without anomalies must report 0 findings."""
    clean_dir = small_scale_environments["clean_dir"]
    val = Validator(data_dir=clean_dir)
    findings = val.run_all_checks()

    # Rule counts
    counts = defaultdict(int)
    for f in findings:
        counts[f.rule_id] += 1

    assert len(findings) == 0, f"Expected 0 findings on clean baseline, got {len(findings)}: {dict(counts)}"


def test_validator_anomalous_data_evaluate_metrics(small_scale_environments):
    """Anomalous data must detect every ground truth anomaly with P=1.0 and R=1.0 per issue type."""
    anom_dir = small_scale_environments["anom_dir"]
    gt_path = small_scale_environments["gt_path"]

    val = Validator(data_dir=anom_dir, ground_truth_path=gt_path)
    findings = val.run_all_checks()
    gt_df = pd.read_csv(gt_path, dtype=str, keep_default_na=False)

    metrics, fps, fns = evaluate_findings(findings, gt_df)

    # Per issue type P=R=F1=1.0
    for issue_type, res in metrics["by_issue_type"].items():
        assert res["gt_count"] >= 3, f"Expected at least 3 injected cases for {issue_type}"
        assert res["recall"] == 1.0, f"Recall for {issue_type} is {res['recall']} != 1.0 (FN: {res['fn']})"
        assert res["precision"] == 1.0, f"Precision for {issue_type} is {res['precision']} != 1.0 (FP: {res['fp']})"
        assert res["f1"] == 1.0


def test_hand_corrupted_cases_detected(tmp_path: Path, small_scale_environments):
    """Test 5 hand-corrupted cases across 5 rule families are detected by the validator."""
    clean_dir = small_scale_environments["clean_dir"]
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir(parents=True, exist_ok=True)

    # Copy clean tables to corrupt_dir
    for f in clean_dir.glob("*.csv"):
        (corrupt_dir / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    # Corrupt 1: V-PK-01 (duplicate customer_id)
    cust_df = pd.read_csv(corrupt_dir / "customer.csv", dtype=str, keep_default_na=False)
    dup_row = cust_df.iloc[[0]].copy()
    cust_df = pd.concat([cust_df, dup_row], ignore_index=True)
    cust_df.to_csv(corrupt_dir / "customer.csv", index=False, encoding="utf-8")

    # Corrupt 2: V-FK-01 (ticket references nonexistent customer)
    tkt_df = pd.read_csv(corrupt_dir / "ticket.csv", dtype=str, keep_default_na=False)
    tkt_df.at[0, "customer_id"] = "CUST-999999"
    tkt_df.to_csv(corrupt_dir / "ticket.csv", index=False, encoding="utf-8")

    # Corrupt 3: V-ENUM-01 (invalid priority level)
    tkt_df.at[1, "priority_level"] = "P9"
    tkt_df.to_csv(corrupt_dir / "ticket.csv", index=False, encoding="utf-8")

    # Corrupt 4: V-ID-01 (malformed ticket ID)
    tkt_df.at[2, "ticket_id"] = "INVALID-TKT-ID"
    tkt_df.to_csv(corrupt_dir / "ticket.csv", index=False, encoding="utf-8")

    # Corrupt 5: V-DUP-01 (duplicate customer email)
    cust_df.at[1, "email"] = cust_df.at[0, "email"]
    cust_df.to_csv(corrupt_dir / "customer.csv", index=False, encoding="utf-8")

    val = Validator(data_dir=corrupt_dir)
    findings = val.run_all_checks()

    found_rules = set(f.rule_id for f in findings)
    assert "V-PK-01" in found_rules, "Corrupted PK not detected"
    assert "V-FK-01" in found_rules, "Corrupted FK not detected"
    assert "V-ENUM-01" in found_rules, "Corrupted ENUM not detected"
    assert "V-ID-01" in found_rules, "Corrupted ID not detected"
    assert "V-DUP-01" in found_rules, "Corrupted DUP not detected"
