"""Test suite for Phase 1 of the synthetic support routing data generator.

Validates:
- PK uniqueness for every table (single & composite)
- Every FK resolves
- ID patterns match regex constants
- Enums are in allowed sets
- Intent weights sum to 100
- Exactly one SLA_TARGET rule per priority per date
- Calendar windows have no overlap
- 3 INACTIVE agents at default scale (all T1)
- Every agent has 'en' FLUENT/NATIVE
- Shift rows = 5 per agent
- Coverage check passes at default scale
- Running twice with same seed gives identical DataFrames (SHA-256 of CSV bytes)
- Different seed gives different agents
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path
import pytest
import pandas as pd

from generator.config import Config
from generator import ids
from generator.rng import init_rng_streams, ticket_rng
from generator.master_data import build_master_data, _check_coverage_feasibility
from generator.export import export_tables


@pytest.fixture(scope="module")
def default_config() -> Config:
    """Default generator configuration (scale: 100 agents, 12000 customers)."""
    return Config(seed=20261022, agents=100, customers=12000)


@pytest.fixture(scope="module")
def default_tables(default_config: Config) -> dict[str, pd.DataFrame]:
    """Generated tables at default scale."""
    streams = init_rng_streams(default_config.seed)
    return build_master_data(default_config, streams)


def test_pk_uniqueness(default_tables: dict[str, pd.DataFrame]) -> None:
    """PK uniqueness for every table (data_dictionary.md 0, business_rules.md 13 V-PK-01)."""
    pk_definitions: dict[str, list[str]] = {
        "Language": ["language_code"],
        "SLACalendar": ["calendar_code"],
        "SLACalendarWindow": ["window_id"],
        "Channel": ["channel_id"],
        "Skill": ["skill_id"],
        "Intent": ["intent_id"],
        "PriorityRule": ["rule_id"],
        "Queue": ["queue_id"],
        "Agent": ["agent_id"],
        "AgentSkill": ["agent_id", "skill_id", "effective_from"],
        "AgentLanguage": ["agent_id", "language_code"],
        "QueueMember": ["queue_id", "agent_id", "effective_from"],
        "AgentShift": ["shift_id"],
        "Customer": ["customer_id"],
    }

    for table_name, pk_cols in sorted(pk_definitions.items()):
        df = default_tables[table_name]
        # No nulls in PK
        assert df[pk_cols].notna().all().all(), f"Nulls found in PK of table {table_name}"
        # Unique rows
        duplicates = df.duplicated(subset=pk_cols, keep=False)
        assert not duplicates.any(), f"Duplicate PK found in table {table_name}: {df[duplicates]}"


def test_every_fk_resolves(default_tables: dict[str, pd.DataFrame]) -> None:
    """Every FK resolves (data_dictionary.md 22, business_rules.md 13 V-FK-01)."""
    tables = default_tables

    # 1. SLACalendarWindow.calendar_code -> SLACalendar.calendar_code
    valid_cals = set(tables["SLACalendar"]["calendar_code"])
    assert set(tables["SLACalendarWindow"]["calendar_code"]).issubset(valid_cals)

    # 2. Channel.calendar_code -> SLACalendar.calendar_code
    assert set(tables["Channel"]["calendar_code"]).issubset(valid_cals)

    # 3. Intent.required_skill_id -> Skill.skill_id
    valid_skills = set(tables["Skill"]["skill_id"])
    assert set(tables["Intent"]["required_skill_id"]).issubset(valid_skills)

    # 4. PriorityRule.calendar_code_override -> SLACalendar.calendar_code (nullable)
    prio_cal_overrides = set(tables["PriorityRule"]["calendar_code_override"].dropna())
    assert prio_cal_overrides.issubset(valid_cals)

    # 5. Queue.required_skill_id -> Skill.skill_id (nullable)
    queue_skills = set(tables["Queue"]["required_skill_id"].dropna())
    assert queue_skills.issubset(valid_skills)

    # 6. AgentSkill.agent_id -> Agent.agent_id
    valid_agents = set(tables["Agent"]["agent_id"])
    assert set(tables["AgentSkill"]["agent_id"]).issubset(valid_agents)

    # 7. AgentSkill.skill_id -> Skill.skill_id
    assert set(tables["AgentSkill"]["skill_id"]).issubset(valid_skills)

    # 8. AgentLanguage.agent_id -> Agent.agent_id
    assert set(tables["AgentLanguage"]["agent_id"]).issubset(valid_agents)

    # 9. AgentLanguage.language_code -> Language.language_code
    valid_langs = set(tables["Language"]["language_code"])
    assert set(tables["AgentLanguage"]["language_code"]).issubset(valid_langs)

    # 10. QueueMember.queue_id -> Queue.queue_id
    valid_queues = set(tables["Queue"]["queue_id"])
    assert set(tables["QueueMember"]["queue_id"]).issubset(valid_queues)

    # 11. QueueMember.agent_id -> Agent.agent_id
    assert set(tables["QueueMember"]["agent_id"]).issubset(valid_agents)

    # 12. AgentShift.agent_id -> Agent.agent_id
    assert set(tables["AgentShift"]["agent_id"]).issubset(valid_agents)

    # 13. Customer.preferred_language -> Language.language_code
    assert set(tables["Customer"]["preferred_language"]).issubset(valid_langs)


def test_id_patterns_match(default_tables: dict[str, pd.DataFrame]) -> None:
    """ID patterns match compiled regexes (data_dictionary.md 0, business_rules.md 13 V-ID-01)."""
    tables = default_tables

    for cid in tables["Customer"]["customer_id"]:
        assert ids.validate_id("customer", cid)

    for aid in tables["Agent"]["agent_id"]:
        assert ids.validate_id("agent", aid)

    for shf in tables["AgentShift"]["shift_id"]:
        assert ids.validate_id("shift", shf)

    for cwn in tables["SLACalendarWindow"]["window_id"]:
        assert ids.validate_id("window", cwn)

    for iid in tables["Intent"]["intent_id"]:
        assert ids.validate_id("intent", iid)

    for pid in tables["PriorityRule"]["rule_id"]:
        assert ids.validate_id("priority_rule", pid)

    for sid in tables["Skill"]["skill_id"]:
        assert ids.validate_id("skill", sid)

    for qid in tables["Queue"]["queue_id"]:
        assert ids.validate_id("queue", qid)

    for cal in tables["SLACalendar"]["calendar_code"]:
        assert ids.validate_id("calendar", cal)

    # Also test all pure helper functions in ids.py
    tkt = ids.ticket_id(1)
    assert ids.validate_id("ticket", tkt)

    msg = ids.message_id(tkt, 1)
    assert ids.validate_id("message", msg)

    asn = ids.assignment_id(tkt, 1)
    assert ids.validate_id("assignment", asn)

    sla_c1 = ids.sla_event_id(tkt, 1)
    sla_c2 = ids.sla_event_id(tkt, 2)
    sla_c3 = ids.sla_event_id(tkt, 3)
    assert ids.validate_id("sla_event", sla_c1)
    assert ids.validate_id("sla_event", sla_c2)
    assert ids.validate_id("sla_event", sla_c3)

    tse = ids.status_event_id(tkt, 1)
    assert ids.validate_id("status_event", tse)

    res = ids.resolution_id(tkt, 1)
    assert ids.validate_id("resolution", res)

    inc = ids.incident_id(1)
    assert ids.validate_id("incident", inc)

    gt = ids.ground_truth_id(1)
    assert ids.validate_id("ground_truth", gt)


def test_enums_in_allowed_sets(default_tables: dict[str, pd.DataFrame]) -> None:
    """Enums are in allowed sets (data_dictionary.md, business_rules.md 13 V-ENUM-01)."""
    tables = default_tables

    assert set(tables["Language"]["language_code"]).issubset({"en", "es", "hi", "ar", "fr"})
    assert set(tables["Channel"]["channel_id"]).issubset({"EMAIL", "CHAT", "PHONE"})
    assert set(tables["Channel"]["message_type"]).issubset({"EMAIL", "CHAT", "VOICE_TRANSCRIPT"})
    assert set(tables["Intent"]["category"]).issubset({
        "BILLING", "TECHNICAL", "ACCOUNT", "SECURITY_SAFETY", "SALES", "GENERAL"
    })
    assert set(tables["Intent"]["default_priority"]).issubset({"P1", "P2", "P3", "P4"})
    assert set(tables["PriorityRule"]["rule_type"]).issubset({
        "SLA_TARGET", "INCIDENT_OVERRIDE", "SAFETY_OVERRIDE", "SEGMENT_UPGRADE"
    })
    prio_levels = set(tables["PriorityRule"]["priority_level"].dropna())
    assert prio_levels.issubset({"P1", "P2", "P3", "P4"})

    assert set(tables["Queue"]["queue_type"]).issubset({"SKILL", "INCIDENT", "ESCALATION"})
    assert set(tables["Queue"]["min_tier"]).issubset({"T1", "T2", "T3"})

    assert set(tables["Agent"]["tier"]).issubset({"T1", "T2", "T3"})
    assert set(tables["Agent"]["status"]).issubset({"ACTIVE", "INACTIVE"})

    assert set(tables["AgentSkill"]["proficiency"]).issubset({"BASIC", "PROFICIENT", "EXPERT"})
    assert set(tables["AgentLanguage"]["fluency"]).issubset({"BASIC", "FLUENT", "NATIVE"})
    assert set(tables["AgentShift"]["shift_code"]).issubset({"EARLY", "DAY", "LATE", "NIGHT"})

    assert set(tables["Customer"]["customer_type"]).issubset({"CUSTOMER", "PROSPECT"})
    assert set(tables["Customer"]["segment"]).issubset({"CONSUMER", "SMB", "ENTERPRISE"})


def test_intent_weights_sum_to_100(default_tables: dict[str, pd.DataFrame]) -> None:
    """Intent weights sum to exactly 100.0 (business_rules.md 2.3)."""
    weights_sum = default_tables["Intent"]["volume_weight_pct"].sum()
    assert abs(weights_sum - 100.0) < 1e-9, f"Weights sum was {weights_sum}, expected 100.0"


def test_exactly_one_sla_target_per_priority_per_date(
    default_tables: dict[str, pd.DataFrame]
) -> None:
    """Exactly one SLA_TARGET rule per priority per date (business_rules.md 2.5)."""
    prio_rules = default_tables["PriorityRule"]
    sla_targets = prio_rules[prio_rules["rule_type"] == "SLA_TARGET"]

    # Check from window start 2026-01-05 through cutoff 2026-04-12
    start_dt = datetime.date(2026, 1, 5)
    end_dt = datetime.date(2026, 4, 12)
    cur = start_dt

    while cur <= end_dt:
        date_str = cur.isoformat()
        for p in ["P1", "P2", "P3", "P4"]:
            matching = sla_targets[
                (sla_targets["priority_level"] == p)
                & (sla_targets["effective_from"] <= date_str)
                & (
                    sla_targets["effective_to"].isna()
                    | (sla_targets["effective_to"] >= date_str)
                )
            ]
            assert len(matching) == 1, (
                f"Expected exactly 1 SLA_TARGET rule for priority {p} on {date_str}, "
                f"found {len(matching)}: {matching['rule_id'].tolist()}"
            )
        cur += datetime.timedelta(days=1)


def test_calendar_windows_no_overlap(default_tables: dict[str, pd.DataFrame]) -> None:
    """Calendar windows have no overlap (data_dictionary.md 3, business_rules.md 2.2)."""
    windows = default_tables["SLACalendarWindow"]

    # For each calendar_code and day_of_week, UQ constraint: at most 1 window
    dups = windows.duplicated(subset=["calendar_code", "day_of_week"], keep=False)
    assert not dups.any(), f"Duplicate windows found: {windows[dups]}"

    # Verify open_time < close_time for all windows
    for _, row in windows.iterrows():
        open_time = str(row["open_time"])
        close_time = str(row["close_time"])
        assert open_time < close_time, f"Window open {open_time} not < close {close_time}"


def test_inactive_agents_count_at_default_scale(default_tables: dict[str, pd.DataFrame]) -> None:
    """Exactly 3 INACTIVE agents at default scale, all T1 (business_rules.md 3.1)."""
    agents = default_tables["Agent"]
    inactive = agents[agents["status"] == "INACTIVE"]
    assert len(inactive) == 3, f"Expected 3 INACTIVE agents, found {len(inactive)}"
    assert (inactive["tier"] == "T1").all(), "All INACTIVE agents must be T1"


def test_every_agent_has_en_fluent_or_native(default_tables: dict[str, pd.DataFrame]) -> None:
    """Every agent has 'en' FLUENT or NATIVE (business_rules.md 3.3)."""
    agents = default_tables["Agent"]
    langs = default_tables["AgentLanguage"]

    en_langs = langs[langs["language_code"] == "en"]
    assert len(en_langs) == len(agents), "Every agent must have an 'en' row"

    # Merge to ensure every agent is covered
    merged = pd.merge(agents, en_langs, on="agent_id", how="left")
    assert merged["fluency"].isin(["FLUENT", "NATIVE"]).all(), "All fluency must be FLUENT or NATIVE"


def test_shift_rows_equal_5_per_agent(default_tables: dict[str, pd.DataFrame]) -> None:
    """Shift rows = 5 per agent (business_rules.md 3.4, data_dictionary.md 22)."""
    shifts = default_tables["AgentShift"]
    counts = shifts.groupby("agent_id").size()

    assert len(counts) == len(default_tables["Agent"]), "All agents must have shift rows"
    assert (counts == 5).all(), f"Every agent must have exactly 5 shift rows, found: {counts.value_counts()}"


def test_coverage_feasibility_check_passes(default_tables: dict[str, pd.DataFrame], default_config: Config) -> None:
    """Coverage check passes at default scale (business_rules.md 3.4)."""
    _check_coverage_feasibility(
        default_tables["AgentShift"],
        default_tables["Agent"],
        default_tables["AgentSkill"],
        default_tables["AgentLanguage"],
        default_config.core_skills,
    )


def test_reproducibility_same_seed_gives_identical_csv(tmp_path: Path) -> None:
    """Running twice with the same seed gives identical DataFrames (compare SHA-256 of CSV bytes)."""
    cfg1 = Config(seed=20261022, agents=20, customers=50, tickets=100)
    streams1 = init_rng_streams(cfg1.seed)
    tables1 = build_master_data(cfg1, streams1)

    dir1 = tmp_path / "run1"
    export_tables(tables1, dir1)

    cfg2 = Config(seed=20261022, agents=20, customers=50, tickets=100)
    streams2 = init_rng_streams(cfg2.seed)
    tables2 = build_master_data(cfg2, streams2)

    dir2 = tmp_path / "run2"
    export_tables(tables2, dir2)

    files1 = sorted(dir1.glob("*.csv"))
    files2 = sorted(dir2.glob("*.csv"))

    assert len(files1) == len(files2) and len(files1) > 0

    for f1, f2 in zip(files1, files2):
        assert f1.name == f2.name
        sha1 = hashlib.sha256(f1.read_bytes()).hexdigest()
        sha2 = hashlib.sha256(f2.read_bytes()).hexdigest()
        assert sha1 == sha2, f"Checksum mismatch for {f1.name}: {sha1} vs {sha2}"


def test_different_seed_gives_different_agents() -> None:
    """A different seed gives different agents (business_rules.md 15)."""
    cfg1 = Config(seed=20261022, agents=20, customers=50)
    streams1 = init_rng_streams(cfg1.seed)
    tables1 = build_master_data(cfg1, streams1)

    cfg2 = Config(seed=98765432, agents=20, customers=50)
    streams2 = init_rng_streams(cfg2.seed)
    tables2 = build_master_data(cfg2, streams2)

    agents1 = tables1["Agent"]
    agents2 = tables2["Agent"]

    # Names, hire dates, or limits must differ
    assert not agents1["full_name"].equals(agents2["full_name"])
    assert not agents1["hire_date"].equals(agents2["hire_date"])


def test_ticket_rng_isolation() -> None:
    """ticket_rng returns deterministic independent random streams."""
    rng_a1 = ticket_rng(20261022, 1, "handle_times")
    rng_a2 = ticket_rng(20261022, 1, "handle_times")
    rng_b = ticket_rng(20261022, 2, "handle_times")
    rng_c = ticket_rng(20261022, 1, "delays")

    draw_a1 = rng_a1.random()
    draw_a2 = rng_a2.random()
    draw_b = rng_b.random()
    draw_c = rng_c.random()

    assert draw_a1 == draw_a2
    assert draw_a1 != draw_b
    assert draw_a1 != draw_c
