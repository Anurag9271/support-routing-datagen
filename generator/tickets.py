"""Ticket classification, SLA targets, and staging export.

Implements:
- business_rules.md section 5: Priority classification and queue rules
- business_rules.md section 8.1 & 8.2: SLA due times calculation
- data_dictionary.md section 15: Ticket schema
- data_dictionary.md section 21: ground_truth_decisions schema
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Mapping, Sequence
import pandas as pd

from generator.config import Config
from generator.rng import ticket_rng
from generator import sla_calendar

# Channel calendar mapping (business_rules.md 2.2)
CHANNEL_CALENDAR_MAP: Mapping[str, str] = {
    "EMAIL": "CAL-EMAIL",
    "CHAT": "CAL-CHAT",
    "PHONE": "CAL-PHONE",
}

# Skill to Queue mapping for non-P1 tickets (business_rules.md 2.4)
SKILL_QUEUE_MAP: Mapping[str, str] = {
    "SKL-BILLING": "QUE-BILLING",
    "SKL-TECH": "QUE-TECH",
    "SKL-ACCOUNT": "QUE-ACCOUNT",
    "SKL-SALES": "QUE-SALES",
    "SKL-GENERAL": "QUE-GENERAL",
}

# SLA Target parameters by rule (business_rules.md 2.5)
# rule_id: (first_response_min, resolution_min, escalation_after_min)
SLA_TARGET_MINUTES: Mapping[str, tuple[int, int, int]] = {
    "PRL-001": (15, 240, 60),      # P1
    "PRL-002": (60, 480, 240),     # P2 v1 (<= 2026-02-15)
    "PRL-003": (45, 480, 240),     # P2 v2 (>= 2026-02-16)
    "PRL-004": (240, 1440, 720),   # P3
    "PRL-005": (480, 2880, 1440),  # P4
}


def _load_subjects_template() -> dict[str, dict[str, str]]:
    """Load pre-drafted subject templates from generator/templates/subjects.json."""
    path = Path(__file__).parent / "templates" / "subjects.json"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["subjects"]


_SUBJECTS_MAP: dict[str, dict[str, str]] = _load_subjects_template()


def build_tickets(
    config: Config,
    raw_arrivals: Sequence[dict[str, object]],
    intent_df: pd.DataFrame,
    priority_rule_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Process raw arrivals into tickets and ground truth decisions.

    Args:
        config: Generator configuration.
        raw_arrivals: Raw arrival records from generator.arrivals.
        intent_df: Intent reference table.
        priority_rule_df: PriorityRule reference table.

    Returns:
        tuple of (ticket_draft_df, ground_truth_decisions_df).
    """
    intents_by_id = intent_df.set_index("intent_id").to_dict(orient="index")

    ticket_rows: list[dict[str, object]] = []
    decision_rows: list[dict[str, object]] = []

    for arrival in raw_arrivals:
        ticket_num = int(arrival["ticket_number"])
        ticket_id = str(arrival["ticket_id"])
        cust = arrival["customer"]
        cust_id = str(arrival["customer_id"])
        channel_id = str(arrival["channel_id"])
        intent_id = str(arrival["intent_id"])
        created_dt: datetime.datetime = arrival["created_at"]
        incident_id = arrival["incident_id"]

        intent_meta = intents_by_id[intent_id]
        is_safety = bool(intent_meta["is_safety_related"])
        default_priority = str(intent_meta["default_priority"])
        required_skill = str(intent_meta["required_skill_id"])
        cust_pref_lang = str(cust["preferred_language"])
        cust_segment = str(cust["segment"])

        # 1. Language: customer's preferred language, except 5% of non-English tickets use 'en'
        # Uses per-ticket RNG stream (business_rules.md 15)
        if cust_pref_lang == "en":
            ticket_lang = "en"
        else:
            tkt_rng = ticket_rng(config.seed, ticket_num, "language")
            if tkt_rng.random() < 0.05:
                ticket_lang = "en"
            else:
                ticket_lang = cust_pref_lang

        # 2. Priority classification (business_rules.md section 5):
        # 1: incident_id is not null -> P1, INCIDENT_OVERRIDE
        # 2: intent.is_safety_related -> P1, SAFETY_OVERRIDE
        # 3: segment = ENTERPRISE -> raise by one level, never above P2
        #    unchanged => INTENT_DEFAULT
        # 4: otherwise -> intent.default_priority, INTENT_DEFAULT
        if incident_id is not None:
            priority_level = "P1"
            priority_reason_code = "INCIDENT_OVERRIDE"
        elif is_safety:
            priority_level = "P1"
            priority_reason_code = "SAFETY_OVERRIDE"
        elif cust_segment == "ENTERPRISE":
            if default_priority == "P4":
                priority_level = "P3"
                priority_reason_code = "SEGMENT_UPGRADE"
            elif default_priority == "P3":
                priority_level = "P2"
                priority_reason_code = "SEGMENT_UPGRADE"
            elif default_priority == "P2":
                priority_level = "P2"
                priority_reason_code = "INTENT_DEFAULT"
            else:
                priority_level = "P1"
                priority_reason_code = "INTENT_DEFAULT"
        else:
            priority_level = default_priority
            priority_reason_code = "INTENT_DEFAULT"

        # 3. Queue rule (section 5):
        # P1 -> QUE-INCIDENT, else queue of intent.required_skill_id
        if priority_level == "P1":
            queue_id = "QUE-INCIDENT"
        else:
            queue_id = SKILL_QUEUE_MAP[required_skill]

        # 4. PriorityRule lookup (effective on created_at UTC date)
        created_date_str = created_dt.strftime("%Y-%m-%d")
        if priority_level == "P1":
            rule_id = "PRL-001"
        elif priority_level == "P2":
            if created_date_str < config.p2_target_v2_effective_date:  # < 2026-02-16
                rule_id = "PRL-002"
            else:
                rule_id = "PRL-003"
        elif priority_level == "P3":
            rule_id = "PRL-004"
        else:  # P4
            rule_id = "PRL-005"

        # 5. Flags
        is_crit = bool(incident_id is not None)
        ch_cal = CHANNEL_CALENDAR_MAP[channel_id]
        # After-hours uses channel's calendar, not the P1 override (business_rules.md 2.2)
        is_after_hours = not sla_calendar.is_open(ch_cal, created_dt)

        # 6. Subject
        subject = _SUBJECTS_MAP[intent_id][ticket_lang]

        created_iso = created_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        ticket_rows.append({
            "ticket_id": ticket_id,
            "customer_id": cust_id,
            "channel_id": channel_id,
            "intent_id": intent_id,
            "queue_id": queue_id,
            "language_code": ticket_lang,
            "created_at": created_iso,
            "subject": subject,
            "priority_level": priority_level,
            "priority_reason_code": priority_reason_code,
            "priority_rule_id": rule_id,
            "is_critical_incident": is_crit,
            "incident_id": incident_id if incident_id else "",
            "is_after_hours_arrival": is_after_hours,
        })

        # 7. Ground Truth Decisions (data_dictionary.md section 21, business_rules.md 8.2)
        expected_cal = "CAL-247" if priority_level == "P1" else ch_cal
        clock_start = sla_calendar.next_open(expected_cal, created_dt)

        frt_min, res_min, esc_min = SLA_TARGET_MINUTES[rule_id]
        due_frt = sla_calendar.add_business_minutes(expected_cal, clock_start, frt_min)
        due_res = sla_calendar.add_business_minutes(expected_cal, clock_start, res_min)
        due_esc = sla_calendar.add_business_minutes(expected_cal, clock_start, esc_min)

        decision_rows.append({
            "ticket_id": ticket_id,
            "expected_priority_level": priority_level,
            "expected_queue_id": queue_id,
            "expected_priority_rule_id": rule_id,
            "expected_calendar_code": expected_cal,
            "expected_first_response_due": due_frt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_resolution_due": due_res.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "expected_escalation_due_at": due_esc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

    ticket_draft_df = pd.DataFrame(ticket_rows)
    ground_truth_decisions_df = pd.DataFrame(decision_rows)

    return ticket_draft_df, ground_truth_decisions_df


def export_ticket_draft(df: pd.DataFrame, out_path: str | Path) -> Path:
    """Export ticket draft DataFrame to CSV (data/staging/ticket_draft.csv)."""
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    out = df.copy()
    for col in out.columns:
        if out[col].dtype == "bool":
            out[col] = out[col].map({True: "true", False: "false"})

    out.to_csv(dest, index=False, encoding="utf-8", lineterminator="\n", na_rep="")
    return dest


def export_ground_truth_decisions(df: pd.DataFrame, out_path: str | Path) -> Path:
    """Export ground truth decisions to CSV (data/ground_truth/ground_truth_decisions.csv)."""
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest, index=False, encoding="utf-8", lineterminator="\n", na_rep="")
    return dest
