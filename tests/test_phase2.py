"""Test suite for Phase 2 of the synthetic support routing data generator.

Validates:
- SLA calendar arithmetic (boundary cases, weekend rollovers, property tests)
- Scenario windows (incidents, spikes, duration, non-overlap)
- Ticket arrivals and classification at default scale (30,000 tickets):
  - Exact ticket count
  - Ticket IDs gapless and sorted by created_at
  - All FKs resolve
  - Every created_at inside arrival window
  - Prospects only hold allows_prospect intents
  - Every safety-intent ticket is P1 with SAFETY_OVERRIDE
  - Incident tickets are P1, flagged, and is_critical_incident iff incident_id
  - ENTERPRISE upgrade follows section 5 (never above P2, unchanged => INTENT_DEFAULT)
  - P2 rule changes from PRL-002 to PRL-003 on 2026-02-16
  - is_after_hours_arrival matches independent recomputation
  - After-hours share between 8% and 30%
  - P1 share between 3% and 9%
  - Each spike window ticket count >= 2.5x average count of same clock hours on other weekdays
  - Incident bursts hold their expected ticket counts
  - Subjects exist for all 60 (intent, language) pairs
  - Same seed run twice gives byte-identical CSVs (SHA-256)
  - ground_truth_decisions has one row per ticket
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
from generator.export import export_tables
from generator import sla_calendar
from generator.windows import build_scenario_windows, export_scenario_windows
from generator.arrivals import generate_arrivals
from generator.tickets import (
    build_tickets,
    export_ticket_draft,
    export_ground_truth_decisions,
)


# =============================================================================
# 1. SLA Calendar Unit & Property Tests
# =============================================================================

def test_sla_calendar_boundary_and_rollover_cases() -> None:
    """Test required boundary and rollover cases (business_rules.md 8.1, 8.2)."""
    # 1. Arrival exactly at open: CAL-EMAIL opens at 08:00
    t_open = datetime.datetime(2026, 1, 5, 8, 0, 0, tzinfo=datetime.timezone.utc)
    assert sla_calendar.is_open("CAL-EMAIL", t_open) is True
    assert sla_calendar.next_open("CAL-EMAIL", t_open) == t_open

    # 2. Arrival exactly at close: CAL-EMAIL closes at 20:00 [08:00, 20:00)
    t_close = datetime.datetime(2026, 1, 5, 20, 0, 0, tzinfo=datetime.timezone.utc)
    assert sla_calendar.is_open("CAL-EMAIL", t_close) is False
    assert sla_calendar.next_open("CAL-EMAIL", t_close) == datetime.datetime(
        2026, 1, 6, 8, 0, 0, tzinfo=datetime.timezone.utc
    )

    # 3. Due time landing exactly on close: 08:00 + 12h (720 min) -> exactly 20:00
    due_at_close = sla_calendar.add_business_minutes("CAL-EMAIL", t_open, 720)
    assert due_at_close == t_close
    assert sla_calendar.business_minutes("CAL-EMAIL", t_open, due_at_close) == 720

    # 4. Friday-evening EMAIL rolling to Monday:
    # Friday 2026-01-09 at 19:00:00 + 120 minutes -> 60m on Fri (up to 20:00) + 60m on Mon (08:00 to 09:00)
    t_fri_email = datetime.datetime(2026, 1, 9, 19, 0, 0, tzinfo=datetime.timezone.utc)
    due_fri_email = sla_calendar.add_business_minutes("CAL-EMAIL", t_fri_email, 120)
    expected_mon_email = datetime.datetime(2026, 1, 12, 9, 0, 0, tzinfo=datetime.timezone.utc)
    assert due_fri_email == expected_mon_email
    assert sla_calendar.business_minutes("CAL-EMAIL", t_fri_email, due_fri_email) == 120

    # 5. PHONE weekend rollover:
    # Friday 2026-01-09 at 17:30:00 (closes 18:00) + 60 min -> 30m on Fri + 30m on Mon (opens 09:00 -> 09:30)
    t_fri_phone = datetime.datetime(2026, 1, 9, 17, 30, 0, tzinfo=datetime.timezone.utc)
    due_fri_phone = sla_calendar.add_business_minutes("CAL-PHONE", t_fri_phone, 60)
    expected_mon_phone = datetime.datetime(2026, 1, 12, 9, 30, 0, tzinfo=datetime.timezone.utc)
    assert due_fri_phone == expected_mon_phone
    assert sla_calendar.business_minutes("CAL-PHONE", t_fri_phone, due_fri_phone) == 60

    # 6. CAL-247 never closed
    t_weekend = datetime.datetime(2026, 1, 10, 3, 14, 15, tzinfo=datetime.timezone.utc)
    assert sla_calendar.is_open("CAL-247", t_weekend) is True
    assert sla_calendar.next_open("CAL-247", t_weekend) == t_weekend
    assert sla_calendar.add_business_minutes("CAL-247", t_weekend, 45) == t_weekend + datetime.timedelta(minutes=45)

    # 7. m = 0 returns start
    assert sla_calendar.add_business_minutes("CAL-EMAIL", t_open, 0) == t_open
    assert sla_calendar.add_business_minutes("CAL-EMAIL", t_close, 0) == t_close

    # 8. business_minutes(a, b) == 0 when b <= a
    assert sla_calendar.business_minutes("CAL-EMAIL", t_open, t_open) == 0
    assert sla_calendar.business_minutes("CAL-EMAIL", t_close, t_open) == 0


def test_sla_calendar_property_test_500_cases_per_calendar() -> None:
    """Property test: business_minutes(cal, start, add_business_minutes(cal, start, m)) == m (500 cases/cal)."""
    rng = np.random.default_rng(20261022)
    start_epoch = datetime.datetime(2026, 1, 5, 0, 0, 0, tzinfo=datetime.timezone.utc)
    total_seconds_window = 90 * 86400

    calendars = ["CAL-EMAIL", "CAL-CHAT", "CAL-PHONE", "CAL-247"]

    for cal_code in calendars:
        for _ in range(500):
            # Pick random start within window
            sec_offset = int(rng.integers(0, total_seconds_window))
            start_dt = start_epoch + datetime.timedelta(seconds=sec_offset)

            # Pick m in [1, 2880] (up to 48 hours business minutes)
            m = int(rng.integers(1, 2881))

            end_dt = sla_calendar.add_business_minutes(cal_code, start_dt, m)
            # Starting from next_open(start_dt), the elapsed business minutes must equal m exactly
            clock_start = sla_calendar.next_open(cal_code, start_dt)
            measured_m = sla_calendar.business_minutes(cal_code, clock_start, end_dt)
            assert measured_m == m, (
                f"Mismatch for {cal_code}: start={start_dt}, clock_start={clock_start}, "
                f"m={m}, end={end_dt}, measured={measured_m}"
            )


# =============================================================================
# 2. Phase 2 Full Pipeline Fixture (Default Scale: 30,000 Tickets)
# =============================================================================

@pytest.fixture(scope="module")
def phase2_pipeline_results():
    """Run full Phase 1 + Phase 2 at default scale (30,000 tickets, 100 agents, 12,000 customers)."""
    config = Config(seed=20261022, tickets=30000, agents=100, customers=12000)
    streams = init_rng_streams(config.seed)

    # Phase 1 tables
    master_tables = build_master_data(config, streams)

    # Phase 2 scenario windows
    incidents, spikes = build_scenario_windows(config, streams["arrivals"])

    # Phase 2 arrivals
    raw_arrivals = generate_arrivals(
        config,
        master_tables["Customer"],
        master_tables["Intent"],
        incidents,
        spikes,
        streams["arrivals"],
    )

    # Phase 2 tickets & decisions
    ticket_draft_df, gt_decisions_df = build_tickets(
        config, raw_arrivals, master_tables["Intent"], master_tables["PriorityRule"]
    )

    return {
        "config": config,
        "master_tables": master_tables,
        "incidents": incidents,
        "spikes": spikes,
        "raw_arrivals": raw_arrivals,
        "ticket_draft_df": ticket_draft_df,
        "gt_decisions_df": gt_decisions_df,
    }


def test_exact_ticket_count(phase2_pipeline_results) -> None:
    """Exact ticket count equals 30,000."""
    df = phase2_pipeline_results["ticket_draft_df"]
    assert len(df) == 30000


def test_ticket_ids_gapless_and_sorted_by_created_at(phase2_pipeline_results) -> None:
    """Ticket IDs are gapless TKT-2026-000001..030000 and sorted by created_at."""
    df = phase2_pipeline_results["ticket_draft_df"]

    # Gapless IDs
    expected_ids = [f"TKT-2026-{i:06d}" for i in range(1, 30001)]
    assert df["ticket_id"].tolist() == expected_ids

    # Sorted by created_at (ties broken by customer_id)
    sort_keys = list(zip(df["created_at"], df["customer_id"]))
    assert sort_keys == sorted(sort_keys)


def test_all_fks_resolve(phase2_pipeline_results) -> None:
    """All foreign keys in ticket_draft resolve."""
    df = phase2_pipeline_results["ticket_draft_df"]
    tables = phase2_pipeline_results["master_tables"]

    assert set(df["customer_id"]).issubset(set(tables["Customer"]["customer_id"]))
    assert set(df["channel_id"]).issubset(set(tables["Channel"]["channel_id"]))
    assert set(df["intent_id"]).issubset(set(tables["Intent"]["intent_id"]))
    assert set(df["queue_id"]).issubset(set(tables["Queue"]["queue_id"]))
    assert set(df["language_code"]).issubset(set(tables["Language"]["language_code"]))
    assert set(df["priority_rule_id"]).issubset(set(tables["PriorityRule"]["rule_id"]))


def test_every_created_at_inside_arrival_window(phase2_pipeline_results) -> None:
    """Every ticket created_at is >= 2026-01-05T00:00:00Z and < 2026-04-05T00:00:00Z."""
    df = phase2_pipeline_results["ticket_draft_df"]
    start_bound = "2026-01-05T00:00:00Z"
    end_bound = "2026-04-05T00:00:00Z"

    assert (df["created_at"] >= start_bound).all()
    assert (df["created_at"] < end_bound).all()


def test_prospects_only_hold_allows_prospect_intents(phase2_pipeline_results) -> None:
    """PROSPECT customers only raise tickets with allows_prospect intents."""
    df = phase2_pipeline_results["ticket_draft_df"]
    cust_df = phase2_pipeline_results["master_tables"]["Customer"]

    prospect_ids = set(cust_df[cust_df["customer_type"] == "PROSPECT"]["customer_id"])
    prospect_tickets = df[df["customer_id"].isin(prospect_ids)]

    assert len(prospect_tickets) > 0
    assert set(prospect_tickets["intent_id"]).issubset({"INT-010", "INT-011"})


def test_safety_intents_are_p1_with_safety_override(phase2_pipeline_results) -> None:
    """Safety-intent tickets (INT-008, INT-009) have priority_level=P1 and SAFETY_OVERRIDE."""
    df = phase2_pipeline_results["ticket_draft_df"]
    safety_tickets = df[df["intent_id"].isin(["INT-008", "INT-009"])]

    assert len(safety_tickets) > 0
    assert (safety_tickets["priority_level"] == "P1").all()
    assert (safety_tickets["priority_reason_code"] == "SAFETY_OVERRIDE").all()
    assert (safety_tickets["queue_id"] == "QUE-INCIDENT").all()


def test_incident_tickets_properties(phase2_pipeline_results) -> None:
    """Incident tickets are P1, INCIDENT_OVERRIDE, and is_critical_incident iff incident_id."""
    df = phase2_pipeline_results["ticket_draft_df"]
    inc_tickets = df[df["is_critical_incident"] == True]

    assert len(inc_tickets) > 0
    assert (inc_tickets["incident_id"] != "").all()
    assert (inc_tickets["priority_level"] == "P1").all()
    assert (inc_tickets["priority_reason_code"] == "INCIDENT_OVERRIDE").all()
    assert (inc_tickets["queue_id"] == "QUE-INCIDENT").all()
    assert (inc_tickets["intent_id"] == "INT-005").all()

    # Non-incident tickets have empty incident_id
    non_inc = df[df["is_critical_incident"] == False]
    assert (non_inc["incident_id"] == "").all()


def test_enterprise_upgrade_rules(phase2_pipeline_results) -> None:
    """Enterprise customers receive 1 level upgrade, never above P2, unchanged => INTENT_DEFAULT."""
    df = phase2_pipeline_results["ticket_draft_df"]
    cust_df = phase2_pipeline_results["master_tables"]["Customer"]
    intents_meta = phase2_pipeline_results["master_tables"]["Intent"].set_index("intent_id").to_dict(orient="index")

    ent_cust_ids = set(cust_df[cust_df["segment"] == "ENTERPRISE"]["customer_id"])
    # Exclude incidents and safety intents which take higher precedence
    ent_tickets = df[
        df["customer_id"].isin(ent_cust_ids)
        & (~df["is_critical_incident"])
        & (~df["intent_id"].isin(["INT-008", "INT-009"]))
    ]

    for _, row in ent_tickets.iterrows():
        def_prio = intents_meta[row["intent_id"]]["default_priority"]
        if def_prio == "P4":
            assert row["priority_level"] == "P3"
            assert row["priority_reason_code"] == "SEGMENT_UPGRADE"
        elif def_prio == "P3":
            assert row["priority_level"] == "P2"
            assert row["priority_reason_code"] == "SEGMENT_UPGRADE"
        elif def_prio == "P2":
            # Never above P2; unchanged => INTENT_DEFAULT
            assert row["priority_level"] == "P2"
            assert row["priority_reason_code"] == "INTENT_DEFAULT"


def test_p2_rule_effective_dating(phase2_pipeline_results) -> None:
    """P2 tickets created <= 2026-02-15 use PRL-002; on >= 2026-02-16 use PRL-003."""
    df = phase2_pipeline_results["ticket_draft_df"]
    p2_tickets = df[df["priority_level"] == "P2"]

    p2_before = p2_tickets[p2_tickets["created_at"] < "2026-02-16T00:00:00Z"]
    p2_after = p2_tickets[p2_tickets["created_at"] >= "2026-02-16T00:00:00Z"]

    assert len(p2_before) > 0
    assert len(p2_after) > 0

    assert (p2_before["priority_rule_id"] == "PRL-002").all()
    assert (p2_after["priority_rule_id"] == "PRL-003").all()


def test_after_hours_arrival_matches_independent_recomputation(phase2_pipeline_results) -> None:
    """is_after_hours_arrival matches independent recomputation from channel calendars."""
    df = phase2_pipeline_results["ticket_draft_df"]

    cal_map = {"EMAIL": "CAL-EMAIL", "CHAT": "CAL-CHAT", "PHONE": "CAL-PHONE"}

    for _, row in df.sample(n=1000, random_state=42).iterrows():
        ch = str(row["channel_id"])
        cal = cal_map[ch]
        t = row["created_at"]
        expected_ah = not sla_calendar.is_open(cal, t)
        assert row["is_after_hours_arrival"] == expected_ah


def test_distribution_targets_p1_and_after_hours(phase2_pipeline_results) -> None:
    """Check target distributions: after-hours 8%-30%, P1 share 3%-9%."""
    df = phase2_pipeline_results["ticket_draft_df"]

    ah_share = df["is_after_hours_arrival"].mean()
    assert 0.08 <= ah_share <= 0.30, f"After-hours share was {ah_share:.3f}"

    p1_share = (df["priority_level"] == "P1").mean()
    assert 0.03 <= p1_share <= 0.09, f"P1 share was {p1_share:.3f}"


def test_spike_windows_ticket_concentration(phase2_pipeline_results) -> None:
    """Each spike window ticket count is at least 2.5x the average count of same clock hours on other weekdays."""
    df = phase2_pipeline_results["ticket_draft_df"]
    spikes = phase2_pipeline_results["spikes"]

    # Parse created_at timestamps
    timestamps = [datetime.datetime.fromisoformat(t.replace("Z", "+00:00")) for t in df["created_at"]]
    df = df.copy()
    df["dt"] = timestamps

    for spk in spikes:
        # Spike tickets
        spk_tickets = df[(df["dt"] >= spk.start) & (df["dt"] < spk.end)]
        spk_count = len(spk_tickets)

        # Baseline count in same clock hours on other weekdays
        spk_dur_hours = (spk.end - spk.start).total_seconds() / 3600.0
        start_hour = spk.start.hour
        end_hour = spk.end.hour  # approximate hour range

        # Other weekdays
        other_wd_tickets = df[
            (df["dt"].apply(lambda t: t.isoweekday() <= 5))
            & (df["dt"].apply(lambda t: t.date() != spk.start.date()))
            & (df["dt"].apply(lambda t: start_hour <= t.hour < start_hour + round(spk_dur_hours)))
        ]
        # Number of other weekdays
        other_days_count = len(set(other_wd_tickets["dt"].apply(lambda t: t.date())))
        if other_days_count > 0:
            avg_other_count = len(other_wd_tickets) / other_days_count
            ratio = spk_count / avg_other_count
            assert ratio >= 2.5, f"Spike {spk.id} ratio {ratio:.2f} < 2.5 (count={spk_count}, avg={avg_other_count:.1f})"


def test_incident_bursts_hold_expected_counts(phase2_pipeline_results) -> None:
    """Incident bursts hold their expected ticket counts exactly."""
    df = phase2_pipeline_results["ticket_draft_df"]
    incidents = phase2_pipeline_results["incidents"]

    for inc in incidents:
        actual = len(df[df["incident_id"] == inc.id])
        assert actual == inc.ticket_count, f"Incident {inc.id} count {actual} != expected {inc.ticket_count}"


def test_subjects_exist_for_all_60_pairs() -> None:
    """Subjects template exists for all 12 intents x 5 languages (<= 8 words)."""
    path = Path("generator/templates/subjects.json")
    assert path.exists()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["review_status"] == "machine_drafted"
    subjects = data["subjects"]
    assert len(subjects) == 12

    for intent_id, langs in subjects.items():
        assert len(langs) == 5
        for lang_code, text in langs.items():
            assert len(text.strip().split()) <= 8


def test_ground_truth_decisions_has_one_row_per_ticket(phase2_pipeline_results) -> None:
    """ground_truth_decisions.csv has one row per ticket with expected columns."""
    df = phase2_pipeline_results["ticket_draft_df"]
    gt = phase2_pipeline_results["gt_decisions_df"]

    assert len(gt) == len(df)
    assert gt["ticket_id"].tolist() == df["ticket_id"].tolist()

    expected_cols = [
        "ticket_id",
        "expected_priority_level",
        "expected_queue_id",
        "expected_priority_rule_id",
        "expected_calendar_code",
        "expected_first_response_due",
        "expected_resolution_due",
        "expected_escalation_due_at",
    ]
    assert list(gt.columns) == expected_cols
    assert gt.notna().all().all()


def test_same_seed_run_twice_gives_byte_identical_csvs(tmp_path: Path) -> None:
    """Running full Phase 1 + 2 with same seed produces 100% byte-identical CSVs (SHA-256)."""
    cfg = Config(seed=20261022, tickets=2000, agents=20, customers=400)

    # Run 1
    dir1 = tmp_path / "run1"
    streams1 = init_rng_streams(cfg.seed)
    tables1 = build_master_data(cfg, streams1)
    export_tables(tables1, dir1 / "raw")
    inc1, spk1 = build_scenario_windows(cfg, streams1["arrivals"])
    export_scenario_windows(inc1 + spk1, dir1 / "ground_truth" / "scenario_windows.csv")
    arr1 = generate_arrivals(cfg, tables1["Customer"], tables1["Intent"], inc1, spk1, streams1["arrivals"])
    tkt1, gt1 = build_tickets(cfg, arr1, tables1["Intent"], tables1["PriorityRule"])
    export_ticket_draft(tkt1, dir1 / "staging" / "ticket_draft.csv")
    export_ground_truth_decisions(gt1, dir1 / "ground_truth" / "ground_truth_decisions.csv")

    # Run 2
    dir2 = tmp_path / "run2"
    streams2 = init_rng_streams(cfg.seed)
    tables2 = build_master_data(cfg, streams2)
    export_tables(tables2, dir2 / "raw")
    inc2, spk2 = build_scenario_windows(cfg, streams2["arrivals"])
    export_scenario_windows(inc2 + spk2, dir2 / "ground_truth" / "scenario_windows.csv")
    arr2 = generate_arrivals(cfg, tables2["Customer"], tables2["Intent"], inc2, spk2, streams2["arrivals"])
    tkt2, gt2 = build_tickets(cfg, arr2, tables2["Intent"], tables2["PriorityRule"])
    export_ticket_draft(tkt2, dir2 / "staging" / "ticket_draft.csv")
    export_ground_truth_decisions(gt2, dir2 / "ground_truth" / "ground_truth_decisions.csv")

    files1 = sorted(dir1.rglob("*.csv"))
    files2 = sorted(dir2.rglob("*.csv"))

    assert len(files1) == len(files2) and len(files1) > 0
    for f1, f2 in zip(files1, files2):
        assert f1.name == f2.name
        sha1 = hashlib.sha256(f1.read_bytes()).hexdigest()
        sha2 = hashlib.sha256(f2.read_bytes()).hexdigest()
        assert sha1 == sha2, f"SHA-256 mismatch for {f1.name}: {sha1} vs {sha2}"
