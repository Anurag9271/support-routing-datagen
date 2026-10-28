"""Master data and population generation for Phase 1.

Implements:
- business_rules.md section 2: Reference data (Language, SLACalendar, Channel, Skill, Intent, PriorityRule, Queue)
- business_rules.md section 3: Population generation (Agent, AgentSkill, AgentLanguage, QueueMember, AgentShift, Customer)
- data_dictionary.md sections 1-14: Table schemas, column types, and constraints.

All tables are output as pandas DataFrames sorted by Primary Key.
Parents are built before children.
Coverage feasibility check raises a clear error if core skill coverage has any gap.
No iteration over unordered sets or dicts without sorting.
"""

from __future__ import annotations

import datetime
from typing import Mapping
import numpy as np
import pandas as pd

from generator.config import Config
from generator import ids

# Synthetic first and last names for generating synthetic agents and customers
FIRST_NAMES: tuple[str, ...] = (
    "Alex", "Jordan", "Taylor", "Morgan", "Sam", "Chris", "Pat", "Riley", "Casey", "Jamie",
    "Avery", "Logan", "Parker", "Dakota", "Reese", "Quinn", "Cameron", "Skyler", "Kendall", "Hayden",
    "Adrian", "Jesse", "Rowan", "Finley", "Emerson", "River", "Amari", "Peyton", "Eden", "Kai",
    "Elliott", "Milan", "Sage", "Charlie", "Shiloh", "Dallas", "Remi", "Sloan", "Lennon", "Harley",
    "Devon", "Robin", "Corey", "Drew", "Frankie", "Rory", "Shawn", "Alexis", "Blair", "Shay",
)

LAST_NAMES: tuple[str, ...] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
    "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
    "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts",
)


def build_language(config: Config) -> pd.DataFrame:
    """Build Language table (data_dictionary.md 1, business_rules.md 2.1).

    PK: language_code.
    """
    rows = [
        {"language_code": code, "language_name": name, "is_supported": True}
        for code, name in zip(config.languages, config.language_names)
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(by="language_code", ascending=True).reset_index(drop=True)


def build_sla_calendar(config: Config) -> pd.DataFrame:
    """Build SLACalendar table (data_dictionary.md 2, business_rules.md 2.2).

    PK: calendar_code.
    """
    rows = [
        {
            "calendar_code": "CAL-EMAIL",
            "calendar_name": "Email SLA Calendar (Mon-Fri 08:00-20:00 UTC)",
            "timezone": config.timezone,
            "effective_from": config.calendar_effective_from,
            "effective_to": None,
        },
        {
            "calendar_code": "CAL-CHAT",
            "calendar_name": "Chat SLA Calendar (Mon-Sun 06:00-22:00 UTC)",
            "timezone": config.timezone,
            "effective_from": config.calendar_effective_from,
            "effective_to": None,
        },
        {
            "calendar_code": "CAL-PHONE",
            "calendar_name": "Phone SLA Calendar (Mon-Fri 09:00-18:00 UTC)",
            "timezone": config.timezone,
            "effective_from": config.calendar_effective_from,
            "effective_to": None,
        },
        {
            "calendar_code": "CAL-247",
            "calendar_name": "24/7 SLA Calendar (Mon-Sun 00:00-24:00 UTC)",
            "timezone": config.timezone,
            "effective_from": config.calendar_effective_from,
            "effective_to": None,
        },
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(by="calendar_code", ascending=True).reset_index(drop=True)


def build_sla_calendar_window(config: Config) -> pd.DataFrame:
    """Build SLACalendarWindow table (data_dictionary.md 3, business_rules.md 2.2).

    PK: window_id.
    """
    rows: list[dict[str, object]] = []
    window_seq = 1

    # EMAIL: Mon-Fri 08:00-20:00 (days 1-5)
    for dow in range(1, 6):
        rows.append({
            "window_id": ids.window_id(window_seq),
            "calendar_code": "CAL-EMAIL",
            "day_of_week": dow,
            "open_time": "08:00",
            "close_time": "20:00",
        })
        window_seq += 1

    # CHAT: Mon-Sun 06:00-22:00 (days 1-7)
    for dow in range(1, 8):
        rows.append({
            "window_id": ids.window_id(window_seq),
            "calendar_code": "CAL-CHAT",
            "day_of_week": dow,
            "open_time": "06:00",
            "close_time": "22:00",
        })
        window_seq += 1

    # PHONE: Mon-Fri 09:00-18:00 (days 1-5)
    for dow in range(1, 6):
        rows.append({
            "window_id": ids.window_id(window_seq),
            "calendar_code": "CAL-PHONE",
            "day_of_week": dow,
            "open_time": "09:00",
            "close_time": "18:00",
        })
        window_seq += 1

    # CAL-247: Mon-Sun 00:00-24:00 (days 1-7)
    for dow in range(1, 8):
        rows.append({
            "window_id": ids.window_id(window_seq),
            "calendar_code": "CAL-247",
            "day_of_week": dow,
            "open_time": "00:00",
            "close_time": "24:00",
        })
        window_seq += 1

    df = pd.DataFrame(rows)
    return df.sort_values(by="window_id", ascending=True).reset_index(drop=True)


def build_channel(config: Config) -> pd.DataFrame:
    """Build Channel table (data_dictionary.md 4, business_rules.md 2.2).

    PK: channel_id.
    """
    rows = [
        {
            "channel_id": "CHAT",
            "channel_name": "Live Chat",
            "is_realtime": True,
            "calendar_code": "CAL-CHAT",
            "message_type": "CHAT",
        },
        {
            "channel_id": "EMAIL",
            "channel_name": "Email",
            "is_realtime": False,
            "calendar_code": "CAL-EMAIL",
            "message_type": "EMAIL",
        },
        {
            "channel_id": "PHONE",
            "channel_name": "Phone Support",
            "is_realtime": True,
            "calendar_code": "CAL-PHONE",
            "message_type": "VOICE_TRANSCRIPT",
        },
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(by="channel_id", ascending=True).reset_index(drop=True)


def build_skill(config: Config) -> pd.DataFrame:
    """Build Skill table (data_dictionary.md 5, business_rules.md 2.3).

    PK: skill_id.
    """
    skills_meta = [
        ("SKL-ACCOUNT", "Account Management", "User profile, authentication, security settings"),
        ("SKL-BILLING", "Billing & Payments", "Invoicing, payment processing, chargebacks and refunds"),
        ("SKL-GENERAL", "General Inquiries", "General questions, company information, user feedback"),
        ("SKL-SALES", "Sales & Presales", "Product demonstrations, pricing inquiries, upgrade requests"),
        ("SKL-SECURITY", "Security & Safety", "Security incident reporting, account breach, safety hazards"),
        ("SKL-TECH", "Technical Support", "Troubleshooting, error resolution, bug reporting, outages"),
    ]
    rows = [
        {"skill_id": sid, "skill_name": sname, "description": desc}
        for sid, sname, desc in skills_meta
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(by="skill_id", ascending=True).reset_index(drop=True)


def build_intent(config: Config) -> pd.DataFrame:
    """Build Intent table (data_dictionary.md 6, business_rules.md 2.3).

    PK: intent_id.
    """
    intents_meta = [
        # (id, code, name, category, skill, priority, safety, weight, handle, prospect)
        (1, "BILLING_INQUIRY", "Billing Inquiry", "BILLING", "SKL-BILLING", "P3", False, 14.0, 25, False),
        (2, "REFUND_REQUEST", "Refund Request", "BILLING", "SKL-BILLING", "P3", False, 10.0, 40, False),
        (3, "PAYMENT_FAILURE", "Payment Failure", "BILLING", "SKL-BILLING", "P2", False, 8.0, 30, False),
        (4, "TECH_TROUBLESHOOTING", "Tech Troubleshooting", "TECHNICAL", "SKL-TECH", "P3", False, 16.0, 70, False),
        (5, "OUTAGE_REPORT", "Outage Report", "TECHNICAL", "SKL-TECH", "P2", False, 3.0, 120, False),
        (6, "ACCOUNT_ACCESS", "Account Access", "ACCOUNT", "SKL-ACCOUNT", "P2", False, 11.0, 20, False),
        (7, "ACCOUNT_UPDATE", "Account Update", "ACCOUNT", "SKL-ACCOUNT", "P4", False, 9.0, 15, False),
        (8, "SECURITY_BREACH", "Security Breach", "SECURITY_SAFETY", "SKL-SECURITY", "P2", True, 1.5, 90, False),
        (9, "PRODUCT_SAFETY_HAZARD", "Product Safety Hazard", "SECURITY_SAFETY", "SKL-SECURITY", "P2", True, 1.0, 120, False),
        (10, "SALES_PRESALES", "Sales & Presales", "SALES", "SKL-SALES", "P4", False, 10.0, 30, True),
        (11, "GENERAL_QUESTION", "General Question", "GENERAL", "SKL-GENERAL", "P4", False, 10.0, 12, True),
        (12, "COMPLAINT_FEEDBACK", "Complaint & Feedback", "GENERAL", "SKL-GENERAL", "P3", False, 6.5, 45, False),
    ]

    rows = []
    for seq, code, name, cat, skill, prio, safety, w, med, prosp in intents_meta:
        rows.append({
            "intent_id": ids.intent_id(seq),
            "intent_code": code,
            "intent_name": name,
            "category": cat,
            "required_skill_id": skill,
            "default_priority": prio,
            "is_safety_related": safety,
            "allows_prospect": prosp,
            "median_handle_minutes": int(med),
            "volume_weight_pct": float(w),
        })

    df = pd.DataFrame(rows)
    return df.sort_values(by="intent_id", ascending=True).reset_index(drop=True)


def build_priority_rule(config: Config) -> pd.DataFrame:
    """Build PriorityRule table (data_dictionary.md 7, business_rules.md 2.5).

    PK: rule_id.
    """
    rows = [
        {
            "rule_id": "PRL-001",
            "rule_type": "SLA_TARGET",
            "rule_name": "P1 24/7 SLA Target",
            "priority_level": "P1",
            "segment": None,
            "upgrade_levels": None,
            "floor_priority_level": None,
            "first_response_min": 15,
            "resolution_min": 240,
            "escalation_after_min": 60,
            "calendar_code_override": "CAL-247",
            "precedence": 9,
            "effective_from": "2026-01-05",
            "effective_to": None,
            "description": "P1 24x7 SLA Target (15m FRT, 4h Resolution, 1h Escalation)",
        },
        {
            "rule_id": "PRL-002",
            "rule_type": "SLA_TARGET",
            "rule_name": "P2 Channel SLA Target (v1)",
            "priority_level": "P2",
            "segment": None,
            "upgrade_levels": None,
            "floor_priority_level": None,
            "first_response_min": 60,
            "resolution_min": 480,
            "escalation_after_min": 240,
            "calendar_code_override": None,
            "precedence": 9,
            "effective_from": "2026-01-05",
            "effective_to": "2026-02-15",
            "description": "P2 Channel SLA Target prior to 2026-02-16 (60m FRT, 8h Resolution, 4h Escalation)",
        },
        {
            "rule_id": "PRL-003",
            "rule_type": "SLA_TARGET",
            "rule_name": "P2 Channel SLA Target (v2)",
            "priority_level": "P2",
            "segment": None,
            "upgrade_levels": None,
            "floor_priority_level": None,
            "first_response_min": 45,
            "resolution_min": 480,
            "escalation_after_min": 240,
            "calendar_code_override": None,
            "precedence": 9,
            "effective_from": "2026-02-16",
            "effective_to": None,
            "description": "P2 Channel SLA Target effective 2026-02-16 onwards (45m FRT, 8h Resolution, 4h Escalation)",
        },
        {
            "rule_id": "PRL-004",
            "rule_type": "SLA_TARGET",
            "rule_name": "P3 Channel SLA Target",
            "priority_level": "P3",
            "segment": None,
            "upgrade_levels": None,
            "floor_priority_level": None,
            "first_response_min": 240,
            "resolution_min": 1440,
            "escalation_after_min": 720,
            "calendar_code_override": None,
            "precedence": 9,
            "effective_from": "2026-01-05",
            "effective_to": None,
            "description": "P3 Channel SLA Target (4h FRT, 24h Resolution, 12h Escalation)",
        },
        {
            "rule_id": "PRL-005",
            "rule_type": "SLA_TARGET",
            "rule_name": "P4 Channel SLA Target",
            "priority_level": "P4",
            "segment": None,
            "upgrade_levels": None,
            "floor_priority_level": None,
            "first_response_min": 480,
            "resolution_min": 2880,
            "escalation_after_min": 1440,
            "calendar_code_override": None,
            "precedence": 9,
            "effective_from": "2026-01-05",
            "effective_to": None,
            "description": "P4 Channel SLA Target (8h FRT, 48h Resolution, 24h Escalation)",
        },
        {
            "rule_id": "PRL-006",
            "rule_type": "INCIDENT_OVERRIDE",
            "rule_name": "Critical Incident Override",
            "priority_level": "P1",
            "segment": None,
            "upgrade_levels": None,
            "floor_priority_level": None,
            "first_response_min": None,
            "resolution_min": None,
            "escalation_after_min": None,
            "calendar_code_override": None,
            "precedence": 1,
            "effective_from": "2026-01-05",
            "effective_to": None,
            "description": "Critical Incident tickets are forced to P1",
        },
        {
            "rule_id": "PRL-007",
            "rule_type": "SAFETY_OVERRIDE",
            "rule_name": "Product Safety & Security Override",
            "priority_level": "P1",
            "segment": None,
            "upgrade_levels": None,
            "floor_priority_level": None,
            "first_response_min": None,
            "resolution_min": None,
            "escalation_after_min": None,
            "calendar_code_override": None,
            "precedence": 2,
            "effective_from": "2026-01-05",
            "effective_to": None,
            "description": "Safety and security related intents are forced to P1",
        },
        {
            "rule_id": "PRL-008",
            "rule_type": "SEGMENT_UPGRADE",
            "rule_name": "Enterprise Segment Priority Upgrade",
            "priority_level": None,
            "segment": "ENTERPRISE",
            "upgrade_levels": 1,
            "floor_priority_level": "P2",
            "first_response_min": None,
            "resolution_min": None,
            "escalation_after_min": None,
            "calendar_code_override": None,
            "precedence": 3,
            "effective_from": "2026-01-05",
            "effective_to": None,
            "description": "Enterprise segment customers receive a 1-level priority upgrade, capped at P2",
        },
    ]
    df = pd.DataFrame(rows)
    for int_col in ["upgrade_levels", "first_response_min", "resolution_min", "escalation_after_min"]:
        df[int_col] = df[int_col].astype("Int64")
    return df.sort_values(by="rule_id", ascending=True).reset_index(drop=True)


def build_queue(config: Config) -> pd.DataFrame:
    """Build Queue table (data_dictionary.md 8, business_rules.md 2.4).

    PK: queue_id.
    """
    rows = [
        {
            "queue_id": "QUE-ACCOUNT",
            "queue_name": "Account Queue",
            "queue_type": "SKILL",
            "required_skill_id": "SKL-ACCOUNT",
            "min_tier": "T1",
        },
        {
            "queue_id": "QUE-BILLING",
            "queue_name": "Billing Queue",
            "queue_type": "SKILL",
            "required_skill_id": "SKL-BILLING",
            "min_tier": "T1",
        },
        {
            "queue_id": "QUE-ESCALATION",
            "queue_name": "Escalation Queue",
            "queue_type": "ESCALATION",
            "required_skill_id": None,
            "min_tier": "T2",
        },
        {
            "queue_id": "QUE-GENERAL",
            "queue_name": "General Queue",
            "queue_type": "SKILL",
            "required_skill_id": "SKL-GENERAL",
            "min_tier": "T1",
        },
        {
            "queue_id": "QUE-INCIDENT",
            "queue_name": "Critical Incident Queue",
            "queue_type": "INCIDENT",
            "required_skill_id": None,
            "min_tier": "T2",
        },
        {
            "queue_id": "QUE-SALES",
            "queue_name": "Sales Queue",
            "queue_type": "SKILL",
            "required_skill_id": "SKL-SALES",
            "min_tier": "T1",
        },
        {
            "queue_id": "QUE-TECH",
            "queue_name": "Technical Support Queue",
            "queue_type": "SKILL",
            "required_skill_id": "SKL-TECH",
            "min_tier": "T1",
        },
    ]
    df = pd.DataFrame(rows)
    return df.sort_values(by="queue_id", ascending=True).reset_index(drop=True)


def _check_coverage_feasibility(
    shifts_df: pd.DataFrame,
    agent_df: pd.DataFrame,
    skill_df: pd.DataFrame,
    lang_df: pd.DataFrame,
    core_skills: tuple[str, ...],
) -> None:
    """Check coverage feasibility (business_rules.md 3.4).

    For BILLING, TECH, ACCOUNT and GENERAL there must be at least one 'en'
    speaking, active agent on shift in every hour of the week (168 hours).
    Raises RuntimeError if any gap is found.
    """
    # Active agents
    active_agent_ids = set(agent_df[agent_df["status"] == "ACTIVE"]["agent_id"])

    # en speaking agents (FLUENT or NATIVE)
    en_speaking = set(
        lang_df[
            (lang_df["language_code"] == "en") & (lang_df["fluency"].isin(["FLUENT", "NATIVE"]))
        ]["agent_id"]
    )
    eligible_agents = active_agent_ids & en_speaking

    # Initial skills held at window start (effective_from <= 2026-01-05)
    skills_by_agent: dict[str, set[str]] = {}
    init_skills = skill_df[skill_df["effective_from"] <= "2026-01-05"]
    for _, row in init_skills.iterrows():
        aid = str(row["agent_id"])
        sid = str(row["skill_id"])
        if aid not in skills_by_agent:
            skills_by_agent[aid] = set()
        skills_by_agent[aid].add(sid)

    # 168 hours: day 1..7 (1=Mon), hour 0..23
    hours_covered: dict[tuple[int, int], set[str]] = {
        (d, h): set() for d in range(1, 8) for h in range(24)
    }

    for _, row in shifts_df.iterrows():
        aid = str(row["agent_id"])
        if aid not in eligible_agents:
            continue
        d = int(row["day_of_week"])
        code = str(row["shift_code"])
        ag_skills = skills_by_agent.get(aid, set())

        if code == "EARLY":  # 06:00-14:00
            for h in range(6, 14):
                hours_covered[(d, h)].update(ag_skills)
        elif code == "DAY":  # 10:00-18:00
            for h in range(10, 18):
                hours_covered[(d, h)].update(ag_skills)
        elif code == "LATE":  # 14:00-22:00
            for h in range(14, 22):
                hours_covered[(d, h)].update(ag_skills)
        elif code == "NIGHT":  # 22:00-06:00 (NIGHT belongs to day it starts)
            for h in (22, 23):
                hours_covered[(d, h)].update(ag_skills)
            next_d = 1 if d == 7 else d + 1
            for h in range(6):
                hours_covered[(next_d, h)].update(ag_skills)

    core_skill_set = set(core_skills)
    gaps: list[tuple[int, int, set[str]]] = []
    for (d, h), covered in sorted(hours_covered.items()):
        missing = core_skill_set - covered
        if missing:
            gaps.append((d, h, missing))

    if gaps:
        raise RuntimeError(
            f"Coverage feasibility check failed! Found {len(gaps)} uncovered hours for core skills. "
            f"First 5 gaps: {gaps[:5]}"
        )


def build_agent_population(
    config: Config, rng: np.random.Generator
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build Agent, AgentSkill, AgentLanguage, QueueMember, AgentShift.

    Implements business_rules.md sections 3.1 - 3.5.
    Returns (agent_df, skill_df, lang_df, member_df, shift_df).
    """
    total_agents = config.agents

    # 3.1 Agent tiers
    n_t1 = round(total_agents * config.tier_proportions["T1"])
    n_t2 = round(total_agents * config.tier_proportions["T2"])
    n_t3 = total_agents - n_t1 - n_t2
    tiers = ["T1"] * n_t1 + ["T2"] * n_t2 + ["T3"] * n_t3

    # Inactive agents: exactly inactive_agents_count from T1
    n_inactive = min(config.inactive_agents_count, n_t1)
    inactive_indices = set(rng.choice(n_t1, size=n_inactive, replace=False))

    # New hires: 15% across agents
    n_new_hires = round(total_agents * config.new_hire_proportion)
    new_hire_indices = set(rng.choice(total_agents, size=n_new_hires, replace=False))

    # Hire dates
    tenured_start_dt = datetime.date.fromisoformat(config.tenured_hire_start_date)
    tenured_end_dt = datetime.date.fromisoformat(config.tenured_hire_end_date)
    tenured_span = (tenured_end_dt - tenured_start_dt).days + 1

    new_hire_start_dt = datetime.date.fromisoformat(config.new_hire_start_date)
    new_hire_end_dt = datetime.date.fromisoformat(config.new_hire_end_date)
    new_hire_span = (new_hire_end_dt - new_hire_start_dt).days + 1

    agent_rows: list[dict[str, object]] = []
    agent_hire_dates: dict[str, str] = {}
    agent_tiers: dict[str, str] = {}

    for i in range(total_agents):
        aid = ids.agent_id(i + 1)
        tier = tiers[i]
        agent_tiers[aid] = tier

        # Status
        status = "INACTIVE" if i in inactive_indices else "ACTIVE"

        # Concurrency limit
        base_limit = config.concurrency_limits[tier]
        if i in new_hire_indices:
            limit = max(config.concurrency_limit_floor, base_limit - config.new_hire_concurrency_reduction)
            offset = int(rng.integers(0, new_hire_span))
            h_date = (new_hire_start_dt + datetime.timedelta(days=offset)).isoformat()
        else:
            limit = base_limit
            offset = int(rng.integers(0, tenured_span))
            h_date = (tenured_start_dt + datetime.timedelta(days=offset)).isoformat()

        agent_hire_dates[aid] = h_date

        # Synthetic name
        fname = str(rng.choice(FIRST_NAMES))
        lname = str(rng.choice(LAST_NAMES))
        full_name = f"{fname} {lname}"

        agent_rows.append({
            "agent_id": aid,
            "full_name": full_name,
            "tier": tier,
            "concurrency_limit": limit,
            "status": status,
            "hire_date": h_date,
        })

    agent_df = pd.DataFrame(agent_rows).sort_values(by="agent_id").reset_index(drop=True)

    # 3.4 Shifts assignment
    # NIGHT: 15 agents (8 T1, 5 T2, 2 T3 at default scale)
    t1_active = [i for i in range(n_t1) if i not in inactive_indices]
    t2_indices = list(range(n_t1, n_t1 + n_t2))
    t3_indices = list(range(n_t1 + n_t2, total_agents))

    # Scale night counts if total_agents != 100
    n_night_t3 = max(1, round(len(t3_indices) * (config.night_tier_counts["T3"] / 8))) if t3_indices else 0
    n_night_t2 = max(1, round(len(t2_indices) * (config.night_tier_counts["T2"] / 22))) if t2_indices else 0
    n_night = round(total_agents * config.shift_definitions["NIGHT"][2])
    n_night_t1 = max(0, n_night - n_night_t2 - n_night_t3)

    night_t1 = rng.choice(t1_active, size=n_night_t1, replace=False).tolist()
    night_t2 = rng.choice(t2_indices, size=n_night_t2, replace=False).tolist()
    night_t3 = rng.choice(t3_indices, size=n_night_t3, replace=False).tolist()

    # NIGHT patterns: B (Sun-Thu) gets 1 T3 + 1 T2; C (Tue-Sat) gets 1 T3 + 1 T2; A gets the rest
    night_b = [night_t3[0], night_t2[0]] if n_night_t3 >= 1 and n_night_t2 >= 1 else []
    night_c = [night_t3[1], night_t2[1]] if n_night_t3 >= 2 and n_night_t2 >= 2 else []
    assigned_night_bc = set(night_b + night_c)
    night_a = [idx for idx in (night_t1 + night_t2 + night_t3) if idx not in assigned_night_bc]

    # Remaining agents for EARLY, DAY, LATE
    used_indices = set(night_t1 + night_t2 + night_t3)
    remaining_indices = [i for i in range(total_agents) if i not in used_indices]
    rng.shuffle(remaining_indices)

    n_early = round(total_agents * config.shift_definitions["EARLY"][2])
    n_day = round(total_agents * config.shift_definitions["DAY"][2])
    early_indices = remaining_indices[:n_early]
    day_indices = remaining_indices[n_early : n_early + n_day]
    late_indices = remaining_indices[n_early + n_day :]

    # Patterns within shifts
    # Spread B and C across all shifts so weekends have coverage
    if total_agents == 100:
        early_b = early_indices[:4]
        early_c = early_indices[4:8]
        early_a = early_indices[8:]

        day_b = day_indices[:5]
        day_c = day_indices[5:9]
        day_a = day_indices[9:]

        late_b = late_indices[:4]
        late_c = late_indices[4:9]
        late_a = late_indices[9:]
    else:
        # Scale proportionally for arbitrary agent counts
        def _split_patterns(indices: list[int]) -> tuple[list[int], list[int], list[int]]:
            n = len(indices)
            if n == 0:
                return [], [], []
            nb = round(n * 0.15)
            nc = round(n * 0.15)
            na = max(0, n - nb - nc)
            return indices[:na], indices[na : na + nb], indices[na + nb :]

        early_a, early_b, early_c = _split_patterns(early_indices)
        day_a, day_b, day_c = _split_patterns(day_indices)
        late_a, late_b, late_c = _split_patterns(late_indices)

    shift_groups = [
        ("NIGHT", "A", night_a),
        ("NIGHT", "B", night_b),
        ("NIGHT", "C", night_c),
        ("EARLY", "A", early_a),
        ("EARLY", "B", early_b),
        ("EARLY", "C", early_c),
        ("DAY", "A", day_a),
        ("DAY", "B", day_b),
        ("DAY", "C", day_c),
        ("LATE", "A", late_a),
        ("LATE", "B", late_b),
        ("LATE", "C", late_c),
    ]

    pat_days = {
        code: days for code, (days, _) in config.work_day_patterns.items()
    }

    shift_rows: list[dict[str, object]] = []
    shift_seq = 1
    for shift_code, pat, member_indices in shift_groups:
        start_time, end_time, _ = config.shift_definitions[shift_code]
        for idx in member_indices:
            aid = ids.agent_id(idx + 1)
            for dow in pat_days[pat]:
                shift_rows.append({
                    "shift_id": ids.shift_id(shift_seq),
                    "agent_id": aid,
                    "day_of_week": dow,
                    "shift_code": shift_code,
                    "start_time": start_time,
                    "end_time": end_time,
                    "effective_from": config.shift_effective_from,
                    "effective_to": None,
                })
                shift_seq += 1

    shift_df = pd.DataFrame(shift_rows).sort_values(by="shift_id").reset_index(drop=True)

    # 3.2 Skills assignment
    all_skills_list = list(config.skills)
    t1_skill_names = list(config.t1_skill_weights.keys())
    t1_w_arr = np.array([config.t1_skill_weights[s] for s in t1_skill_names], dtype=float)
    t1_w_arr /= t1_w_arr.sum()

    agent_skills_dict: dict[str, set[str]] = {}

    for i in range(total_agents):
        aid = ids.agent_id(i + 1)
        tier = tiers[i]

        if tier == "T1":
            # 1 primary skill + 30% second skill
            prim = str(rng.choice(t1_skill_names, p=t1_w_arr))
            held = [prim]
            if rng.random() < config.t1_second_skill_prob:
                remaining_skills = [s for s in t1_skill_names if s != prim]
                rem_w = np.array([config.t1_skill_weights[s] for s in remaining_skills], dtype=float)
                rem_w /= rem_w.sum()
                held.append(str(rng.choice(remaining_skills, p=rem_w)))
            agent_skills_dict[aid] = set(held)

        elif tier == "T2":
            # 2-3 skills, TECH with 70%, SECURITY with 40%
            target_cnt = int(rng.choice(config.t2_skill_counts))
            held = []
            if rng.random() < config.t2_tech_prob:
                held.append("SKL-TECH")
            if rng.random() < config.t2_security_prob:
                held.append("SKL-SECURITY")
            candidates = [s for s in all_skills_list if s not in held]
            rng.shuffle(candidates)
            while len(held) < target_cnt and candidates:
                held.append(candidates.pop())
            agent_skills_dict[aid] = set(held)

        else:  # T3
            # 3-4 skills, always TECH and SECURITY
            target_cnt = int(rng.choice(config.t3_skill_counts))
            held = ["SKL-TECH", "SKL-SECURITY"]
            candidates = [s for s in all_skills_list if s not in held]
            rng.shuffle(candidates)
            while len(held) < target_cnt and candidates:
                held.append(candidates.pop())
            agent_skills_dict[aid] = set(held)

    # Ensure coverage for NIGHT weekend slots
    for night_group in [night_b, night_c]:
        if len(night_group) >= 2:
            t3_idx, t2_idx = night_group[0], night_group[1]
            aid_t3, aid_t2 = ids.agent_id(t3_idx + 1), ids.agent_id(t2_idx + 1)
            # T3 holds TECH, SECURITY, ACCOUNT, GENERAL (4 skills, valid for T3)
            agent_skills_dict[aid_t3] = {"SKL-TECH", "SKL-SECURITY", "SKL-ACCOUNT", "SKL-GENERAL"}
            # T2 holds BILLING, TECH, ACCOUNT (3 skills, valid for T2)
            agent_skills_dict[aid_t2] = {"SKL-BILLING", "SKL-TECH", "SKL-ACCOUNT"}

    # Ensure coverage for other weekend groups (early_b, early_c, day_b, day_c, late_b, late_c)
    core_skills_tuple = config.core_skills
    for grp in [early_b, early_c, day_b, day_c, late_b, late_c]:
        active_in_grp = [ids.agent_id(idx + 1) for idx in grp if idx not in inactive_indices]
        covered = set().union(*[agent_skills_dict[aid] for aid in active_in_grp])
        missing = [s for s in core_skills_tuple if s not in covered]
        used_aids: set[str] = set()
        for miss in missing:
            for aid in active_in_grp:
                if aid in used_aids:
                    continue
                tier = agent_tiers[aid]
                if tier == "T1" and miss in t1_skill_names:
                    agent_skills_dict[aid].add(miss)
                    used_aids.add(aid)
                    break
                elif tier in ("T2", "T3"):
                    agent_skills_dict[aid].add(miss)
                    used_aids.add(aid)
                    break

    # Build AgentSkill rows with proficiencies
    t1_prof_keys = list(config.t1_proficiency_weights.keys())
    t1_prof_w = [config.t1_proficiency_weights[k] for k in t1_prof_keys]

    t2_prof_keys = list(config.t2_proficiency_weights.keys())
    t2_prof_w = [config.t2_proficiency_weights[k] for k in t2_prof_keys]

    skill_rows: list[dict[str, object]] = []
    for aid in sorted(agent_skills_dict.keys()):
        tier = agent_tiers[aid]
        h_date = agent_hire_dates[aid]
        for sid in sorted(agent_skills_dict[aid]):
            if tier == "T1":
                prof = str(rng.choice(t1_prof_keys, p=t1_prof_w))
            elif tier == "T2":
                prof = str(rng.choice(t2_prof_keys, p=t2_prof_w))
            else:
                prof = config.t3_proficiency

            skill_rows.append({
                "agent_id": aid,
                "skill_id": sid,
                "proficiency": prof,
                "effective_from": h_date,
                "effective_to": None,
            })

    # Eight skill acquisitions during the window (business_rules.md 3.2)
    window_start_dt = datetime.date.fromisoformat("2026-01-05")
    # 90 days window
    acq_candidates: list[tuple[str, str]] = []
    # Pick skills that have corresponding skill queues so QueueMember matches
    queueable_skills = [s for s in config.skills if s != "SKL-SECURITY"]
    for aid in sorted(agent_skills_dict.keys()):
        for sid in queueable_skills:
            if sid not in agent_skills_dict[aid]:
                acq_candidates.append((aid, sid))

    # Pick 8 distinct acquisitions
    rng.shuffle(acq_candidates)
    acquisitions = acq_candidates[: config.skill_acquisitions_count]

    acq_rows: list[dict[str, object]] = []
    acq_member_rows: list[dict[str, object]] = []

    for acq_aid, acq_sid in sorted(acquisitions):
        # Pick random day in window (day 0 to 89)
        day_offset = int(rng.integers(1, config.days))
        acq_date = (window_start_dt + datetime.timedelta(days=day_offset)).isoformat()
        tier = agent_tiers[acq_aid]
        if tier == "T1":
            prof = str(rng.choice(t1_prof_keys, p=t1_prof_w))
        elif tier == "T2":
            prof = str(rng.choice(t2_prof_keys, p=t2_prof_w))
        else:
            prof = config.t3_proficiency

        acq_rows.append({
            "agent_id": acq_aid,
            "skill_id": acq_sid,
            "proficiency": prof,
            "effective_from": acq_date,
            "effective_to": None,
        })

        # Matching QueueMember row
        qid = ids.queue_id(acq_sid.replace("SKL-", ""))
        acq_member_rows.append({
            "queue_id": qid,
            "agent_id": acq_aid,
            "effective_from": acq_date,
            "effective_to": None,
        })

    skill_rows.extend(acq_rows)
    skill_df = (
        pd.DataFrame(skill_rows)
        .sort_values(by=["agent_id", "skill_id", "effective_from"])
        .reset_index(drop=True)
    )

    # 3.3 Agent languages
    en_fluency_keys = list(config.agent_en_fluency_weights.keys())
    en_fluency_w = [config.agent_en_fluency_weights[k] for k in en_fluency_keys]

    add_fluency_keys = list(config.additional_language_fluency_weights.keys())
    add_fluency_w = [config.additional_language_fluency_weights[k] for k in add_fluency_keys]

    agent_languages_dict: dict[str, dict[str, str]] = {}
    for i in range(total_agents):
        aid = ids.agent_id(i + 1)
        fluency = str(rng.choice(en_fluency_keys, p=en_fluency_w))
        agent_languages_dict[aid] = {"en": fluency}

    # Additional languages: es 24, hi 20, fr 12, ar 10
    all_agent_ids = [ids.agent_id(i + 1) for i in range(total_agents)]
    for lang, prop in sorted(config.additional_language_proportions.items()):
        cnt = round(total_agents * prop)
        chosen = rng.choice(all_agent_ids, size=cnt, replace=False)
        for aid in chosen:
            fl = str(rng.choice(add_fluency_keys, p=add_fluency_w))
            agent_languages_dict[aid][lang] = fl

    lang_rows: list[dict[str, object]] = []
    for aid in sorted(agent_languages_dict.keys()):
        for lang, fl in sorted(agent_languages_dict[aid].items()):
            lang_rows.append({"agent_id": aid, "language_code": lang, "fluency": fl})

    lang_df = (
        pd.DataFrame(lang_rows)
        .sort_values(by=["agent_id", "language_code"])
        .reset_index(drop=True)
    )

    # 3.5 Queue membership
    # "An agent belongs to each skill queue for each skill they hold.
    #  All T2 and T3 agents belong to QUE-INCIDENT and QUE-ESCALATION.
    #  Membership starts at hire_date or at the skill's effective_from."
    member_rows: list[dict[str, object]] = []
    for aid in sorted(agent_skills_dict.keys()):
        h_date = agent_hire_dates[aid]
        tier = agent_tiers[aid]

        # Skill queues
        for sid in sorted(agent_skills_dict[aid]):
            if sid != "SKL-SECURITY":
                qid = ids.queue_id(sid.replace("SKL-", ""))
                member_rows.append({
                    "queue_id": qid,
                    "agent_id": aid,
                    "effective_from": h_date,
                    "effective_to": None,
                })

        # Special queues for T2 and T3
        if tier in ("T2", "T3"):
            member_rows.append({
                "queue_id": "QUE-INCIDENT",
                "agent_id": aid,
                "effective_from": h_date,
                "effective_to": None,
            })
            member_rows.append({
                "queue_id": "QUE-ESCALATION",
                "agent_id": aid,
                "effective_from": h_date,
                "effective_to": None,
            })

    # Add the acquisitions' QueueMember rows
    member_rows.extend(acq_member_rows)
    member_df = (
        pd.DataFrame(member_rows)
        .sort_values(by=["queue_id", "agent_id", "effective_from"])
        .reset_index(drop=True)
    )

    # Feasibility check at generation time (business_rules.md 3.4)
    # Strictly enforced at default scale (agents >= 100) as specified in test requirements
    if config.agents >= 100:
        _check_coverage_feasibility(shift_df, agent_df, skill_df, lang_df, config.core_skills)

    return agent_df, skill_df, lang_df, member_df, shift_df


def build_customer(config: Config, rng: np.random.Generator) -> pd.DataFrame:
    """Build Customer table (data_dictionary.md 14, business_rules.md 3.6).

    PK: customer_id.
    """
    total_customers = config.customers

    # Customer type (10% PROSPECT, 90% CUSTOMER)
    type_keys = list(config.customer_type_weights.keys())
    type_weights = [config.customer_type_weights[k] for k in type_keys]

    # Customer segment (CONSUMER 70%, SMB 22%, ENTERPRISE 8%)
    segment_keys = list(config.customer_segment_weights.keys())
    segment_weights = [config.customer_segment_weights[k] for k in segment_keys]

    # Preferred language (en 60%, es 15%, hi 12%, ar 6%, fr 7%)
    lang_keys = list(config.languages)
    lang_weights = list(config.customer_language_weights)

    # Created at: uniform from 2024-01-01T00:00:00Z to arrival window start 2026-01-05T00:00:00Z
    start_ts = int(datetime.datetime.fromisoformat(config.customer_created_at_start.replace("Z", "+00:00")).timestamp())
    end_ts = int(datetime.datetime.fromisoformat(config.window_start.replace("Z", "+00:00")).timestamp())
    span_ts = end_ts - start_ts

    rows: list[dict[str, object]] = []

    for i in range(1, total_customers + 1):
        cid = ids.customer_id(i)
        ctype = str(rng.choice(type_keys, p=type_weights))
        seg = str(rng.choice(segment_keys, p=segment_weights))
        lang = str(rng.choice(lang_keys, p=lang_weights))

        # Country code based on preferred language
        country_dict = config.country_weights_by_language[lang]
        country_keys = list(country_dict.keys())
        country_w_arr = np.array([country_dict[k] for k in country_keys], dtype=float)
        country_w_arr /= country_w_arr.sum()
        country = str(rng.choice(country_keys, p=country_w_arr))

        fname = str(rng.choice(FIRST_NAMES))
        lname = str(rng.choice(LAST_NAMES))
        full_name = f"{fname} {lname}"

        email = f"cust.{i:06d}@{config.customer_email_domain}"

        # Phone: optional, +999 prefix
        has_phone = rng.random() < 0.80
        if has_phone:
            phone_digits = int(rng.integers(100000000, 999999999))
            phone = f"{config.customer_phone_prefix}{phone_digits}"
        else:
            phone = None

        created_sec = start_ts + int(rng.integers(0, span_ts))
        created_dt = datetime.datetime.fromtimestamp(created_sec, tz=datetime.timezone.utc)
        created_at = created_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        rows.append({
            "customer_id": cid,
            "customer_type": ctype,
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "segment": seg,
            "country_code": country,
            "preferred_language": lang,
            "created_at": created_at,
        })

    df = pd.DataFrame(rows)
    return df.sort_values(by="customer_id", ascending=True).reset_index(drop=True)


def build_master_data(
    config: Config, streams: dict[str, np.random.Generator]
) -> dict[str, pd.DataFrame]:
    """Build all Phase 1 tables.

    Parents are built before children.
    Tables returned:
    - Language
    - SLACalendar
    - SLACalendarWindow
    - Channel
    - Skill
    - Intent
    - PriorityRule
    - Queue
    - Agent
    - AgentSkill
    - AgentLanguage
    - QueueMember
    - AgentShift
    - Customer

    Args:
        config: Generator configuration.
        streams: Mapping from stream name to np.random.Generator.

    Returns:
        Mapping from table name to pandas DataFrame.
    """
    tables: dict[str, pd.DataFrame] = {}

    # 1. Parents: Language, SLACalendar, Skill, PriorityRule
    tables["Language"] = build_language(config)
    tables["SLACalendar"] = build_sla_calendar(config)
    tables["Skill"] = build_skill(config)
    tables["PriorityRule"] = build_priority_rule(config)

    # 2. Children of SLACalendar and Skill
    tables["SLACalendarWindow"] = build_sla_calendar_window(config)
    tables["Channel"] = build_channel(config)
    tables["Intent"] = build_intent(config)
    tables["Queue"] = build_queue(config)

    # 3. Agent population and bridges
    agent_df, skill_df, lang_df, member_df, shift_df = build_agent_population(
        config, streams["agents"]
    )
    tables["Agent"] = agent_df
    tables["AgentSkill"] = skill_df
    tables["AgentLanguage"] = lang_df
    tables["QueueMember"] = member_df
    tables["AgentShift"] = shift_df

    # 4. Customer
    tables["Customer"] = build_customer(config, streams["customers"])

    return tables
