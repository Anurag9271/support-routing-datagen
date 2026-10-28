"""Phase 3c: Final relational tables generation.

Transforms staging draft outputs into final relational tables:
- Ticket (final)
- TicketStatusEvent (final)
- AssignmentEvent (final)
- Message (final, exactly cfg.messages rows)
- SLAEvent (final, one row per cycle)
- Resolution (final, one row per resolved cycle)

Implements:
- docs/data_dictionary.md sections 15, 16, 17, 18, 19, 20
- docs/business_rules.md sections 5, 7.7, 7.8, 8.1, 8.2, 9, 10
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Mapping
import numpy as np
import pandas as pd

from generator.config import Config
from generator import ids
from generator.rng import ticket_rng
from generator import sla_calendar


EPOCH_UTC = datetime.datetime(2026, 1, 5, 0, 0, 0, tzinfo=datetime.timezone.utc)


def _iso_to_dt(iso_str: str) -> datetime.datetime:
    """Parse ISO UTC string with Z into aware datetime."""
    return datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


def _dt_to_iso(dt: datetime.datetime) -> str:
    """Format aware datetime as ISO UTC string with Z."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sec_to_iso(sec: int) -> str:
    return (EPOCH_UTC + datetime.timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_sec(iso_str: str) -> int:
    dt = datetime.datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int((dt - EPOCH_UTC).total_seconds())


# =============================================================================
# 1. Ticket (Final)
# =============================================================================

def build_final_tickets(
    ticket_draft_df: pd.DataFrame,
    status_event_df: pd.DataFrame,
) -> pd.DataFrame:
    """Derive final Ticket table with current_status, last_status_at, and reopen_count."""
    # Find last status event per ticket
    last_events = (
        status_event_df.sort_values(["ticket_id", "seq_no"])
        .groupby("ticket_id")
        .last()
        .reset_index()
    )
    last_status_map = dict(zip(last_events["ticket_id"], last_events["to_status"]))
    last_time_map = dict(zip(last_events["ticket_id"], last_events["event_at"]))

    # Count REOPENED status events per ticket
    reopen_counts = (
        status_event_df[status_event_df["to_status"] == "REOPENED"]
        .groupby("ticket_id")
        .size()
        .to_dict()
    )

    rows: list[dict[str, Any]] = []
    for _, tkt in ticket_draft_df.iterrows():
        tid = str(tkt["ticket_id"])
        c_status = last_status_map.get(tid, "NEW")
        l_status_at = last_time_map.get(tid, str(tkt["created_at"]))
        r_count = reopen_counts.get(tid, 0)

        rows.append({
            "ticket_id": tid,
            "customer_id": str(tkt["customer_id"]),
            "channel_id": str(tkt["channel_id"]),
            "intent_id": str(tkt["intent_id"]),
            "queue_id": str(tkt["queue_id"]),
            "language_code": str(tkt["language_code"]),
            "created_at": str(tkt["created_at"]),
            "subject": str(tkt["subject"]),
            "priority_level": str(tkt["priority_level"]),
            "priority_reason_code": str(tkt["priority_reason_code"]),
            "priority_rule_id": str(tkt["priority_rule_id"]),
            "is_critical_incident": bool(tkt["is_critical_incident"]),
            "incident_id": str(tkt["incident_id"]) if pd.notna(tkt["incident_id"]) and tkt["incident_id"] != "" else "",
            "is_after_hours_arrival": bool(tkt["is_after_hours_arrival"]),
            "current_status": c_status,
            "last_status_at": l_status_at,
            "reopen_count": int(r_count),
        })

    df = pd.DataFrame(rows)
    return df.sort_values("ticket_id").reset_index(drop=True)


# =============================================================================
# 2. TicketStatusEvent & AssignmentEvent (Final)
# =============================================================================

def build_final_ticket_status_events(status_event_df: pd.DataFrame) -> pd.DataFrame:
    """Format status events for final export matching data_dictionary.md 16."""
    cols = [
        "status_event_id",
        "ticket_id",
        "seq_no",
        "cycle_no",
        "from_status",
        "to_status",
        "event_at",
        "actor_type",
        "actor_agent_id",
        "trigger_ref",
    ]
    df = status_event_df[cols].copy()
    df["from_status"] = df["from_status"].fillna("")
    df["actor_agent_id"] = df["actor_agent_id"].fillna("")
    df["trigger_ref"] = df["trigger_ref"].fillna("")
    return df.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True)


def build_final_assignment_events(assignment_event_df: pd.DataFrame) -> pd.DataFrame:
    """Format assignment events for final export matching data_dictionary.md 18."""
    cols = [
        "assignment_id",
        "ticket_id",
        "seq_no",
        "cycle_no",
        "event_type",
        "reason_code",
        "event_at",
        "agent_id",
        "from_agent_id",
        "queue_id",
        "required_skill_id",
        "language_match",
        "wait_minutes",
        "agent_load_before",
        "agent_load_after",
        "concurrency_limit",
    ]
    df = assignment_event_df[cols].copy()
    df["from_agent_id"] = df["from_agent_id"].fillna("")
    return df.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True)


# =============================================================================
# 3. Message (Final, exactly cfg.messages)
# =============================================================================

def _load_message_templates() -> dict[str, Any]:
    path = Path(__file__).parent / "templates" / "bodies.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


CHANNEL_MESSAGE_TYPE: Mapping[str, str] = {
    "EMAIL": "EMAIL",
    "CHAT": "CHAT",
    "PHONE": "VOICE_TRANSCRIPT",
}


def build_messages(
    config: Config,
    master_tables: Mapping[str, pd.DataFrame],
    ticket_draft_df: pd.DataFrame,
    status_event_df: pd.DataFrame,
    assignment_event_df: pd.DataFrame,
) -> tuple[pd.DataFrame, int]:
    """Generate final Message table matching data_dictionary.md 17 and business_rules.md 9.

    Calibrates to exactly config.messages rows using optional follow-up pairs.
    Returns (message_df, natural_message_count).
    """
    templates = _load_message_templates()
    cutoff_dt = _iso_to_dt(config.cutoff)
    cutoff_sec = _iso_to_sec(config.cutoff)

    # Pre-index events by ticket_id
    status_by_ticket: dict[str, list[dict[str, Any]]] = {}
    for row in status_event_df.to_dict(orient="records"):
        status_by_ticket.setdefault(str(row["ticket_id"]), []).append(row)

    asn_by_ticket: dict[str, list[dict[str, Any]]] = {}
    for row in assignment_event_df.to_dict(orient="records"):
        asn_by_ticket.setdefault(str(row["ticket_id"]), []).append(row)

    # Helper: find active owner at given sec
    def get_owner_at_sec(tid: str, at_sec: int) -> tuple[str, bool]:
        asns = asn_by_ticket.get(tid, [])
        owner = ""
        lang_match = True
        for a in asns:
            ev_sec = _iso_to_sec(a["event_at"])
            if ev_sec <= at_sec:
                owner = str(a["agent_id"])
                lm = a["language_match"]
                lang_match = (lm is True or lm == "true" or lm == 1)
            else:
                break
        return owner, lang_match

    # Structure per ticket:
    # ticket_messages: list of raw msg dicts
    # candidate_followups: list of dicts for potential optional follow-up pairs
    ticket_base_messages: dict[str, list[dict[str, Any]]] = {}
    candidate_followups: list[dict[str, Any]] = []

    for t_row in ticket_draft_df.to_dict(orient="records"):
        tid = str(t_row["ticket_id"])
        tnum = int(tid.split("-")[-1])
        ch = str(t_row["channel_id"])
        intent = str(t_row["intent_id"])
        tkt_lang = str(t_row["language_code"])
        created_sec = _iso_to_sec(str(t_row["created_at"]))
        created_str = str(t_row["created_at"])
        m_type = CHANNEL_MESSAGE_TYPE[ch]

        t_msgs: list[dict[str, Any]] = []

        # 1. Opening customer message (seq 1)
        m1 = {
            "ticket_id": tid,
            "cycle_no": 1,
            "sender_type": "CUSTOMER",
            "sender_agent_id": "",
            "message_type": m_type,
            "in_reply_to_message_id": "",
            "sent_at_sec": created_sec,
            "sent_at": created_str,
            "language_code": tkt_lang,
            "template_id": f"TPL-{intent}-CUSTOMER-{tkt_lang}",
            "body": templates["bodies"][intent]["CUSTOMER"][tkt_lang],
        }
        t_msgs.append(m1)

        # 2. EMAIL auto-acknowledgement (seq 2 for EMAIL)
        if ch == "EMAIL":
            ack_sec = min(created_sec + 15, cutoff_sec)
            t_msgs.append({
                "ticket_id": tid,
                "cycle_no": 1,
                "sender_type": "SYSTEM",
                "sender_agent_id": "",
                "message_type": "SYSTEM_NOTE",
                "in_reply_to_tag": "m1",
                "sent_at_sec": ack_sec,
                "sent_at": _sec_to_iso(ack_sec),
                "language_code": tkt_lang,
                "template_id": f"TPL-SYSTEM-EMAIL_ACK-{tkt_lang}",
                "body": templates["system_notes"]["EMAIL_ACK"][tkt_lang],
            })

        # Process status events for cycles, first response, escalations, resolutions
        events = status_by_ticket.get(tid, [])
        first_resp_cycles: set[int] = set()
        escalated_cycles: set[int] = set()

        for ev in events:
            cyc = int(ev["cycle_no"])
            to_st = ev["to_status"]
            ev_sec = _iso_to_sec(ev["event_at"])

            if to_st == "IN_PROGRESS" and cyc not in first_resp_cycles:
                first_resp_cycles.add(cyc)
                # First agent response
                owner, lang_match = get_owner_at_sec(tid, ev_sec)
                agent_lang = tkt_lang if lang_match else "en"
                t_msgs.append({
                    "ticket_id": tid,
                    "cycle_no": cyc,
                    "sender_type": "AGENT",
                    "sender_agent_id": owner,
                    "message_type": m_type,
                    "in_reply_to_tag": "latest_customer",
                    "sent_at_sec": ev_sec,
                    "sent_at": ev["event_at"],
                    "language_code": agent_lang,
                    "template_id": f"TPL-{intent}-AGENT-{agent_lang}",
                    "body": templates["bodies"][intent]["AGENT"][agent_lang],
                })

            elif to_st == "ESCALATED":
                escalated_cycles.add(cyc)
                # System note at escalation
                t_msgs.append({
                    "ticket_id": tid,
                    "cycle_no": cyc,
                    "sender_type": "SYSTEM",
                    "sender_agent_id": "",
                    "message_type": "SYSTEM_NOTE",
                    "in_reply_to_tag": "latest_any",
                    "sent_at_sec": ev_sec,
                    "sent_at": ev["event_at"],
                    "language_code": tkt_lang,
                    "template_id": f"TPL-SYSTEM-ESCALATION-{tkt_lang}",
                    "body": templates["system_notes"]["ESCALATION"][tkt_lang],
                })

            elif to_st == "RESOLVED":
                # System note at resolution on escalated tickets
                if cyc in escalated_cycles:
                    t_msgs.append({
                        "ticket_id": tid,
                        "cycle_no": cyc,
                        "sender_type": "SYSTEM",
                        "sender_agent_id": "",
                        "message_type": "SYSTEM_NOTE",
                        "in_reply_to_tag": "latest_any",
                        "sent_at_sec": ev_sec,
                        "sent_at": ev["event_at"],
                        "language_code": tkt_lang,
                        "template_id": f"TPL-SYSTEM-RESOLUTION-{tkt_lang}",
                        "body": templates["system_notes"]["RESOLUTION"][tkt_lang],
                    })

        ticket_base_messages[tid] = t_msgs

        # Check candidate for follow-up pair
        # Find first response time and resolve time in cycle 1
        c1_in_prog = [e for e in events if e["cycle_no"] == 1 and e["to_status"] == "IN_PROGRESS"]
        c1_res = [e for e in events if e["cycle_no"] == 1 and e["to_status"] == "RESOLVED"]
        if c1_in_prog and c1_res:
            t1 = _iso_to_sec(c1_in_prog[0]["event_at"])
            t2 = _iso_to_sec(c1_res[0]["event_at"])
            span = t2 - t1
            if span >= 180:
                t_cust = t1 + int(span * 0.35)
                t_agent = t1 + int(span * 0.70)
                owner_cust, _ = get_owner_at_sec(tid, t_cust)
                owner_agent, lang_match = get_owner_at_sec(tid, t_agent)
                if owner_agent != "":
                    agent_lang = tkt_lang if lang_match else "en"
                    candidate_followups.append({
                        "ticket_id": tid,
                        "ticket_number": tnum,
                        "cycle_no": 1,
                        "t_cust": t_cust,
                        "t_agent": t_agent,
                        "owner_agent": owner_agent,
                        "agent_lang": agent_lang,
                        "m_type": m_type,
                        "tkt_lang": tkt_lang,
                        "intent": intent,
                    })

    # Base count before optional follow-ups
    base_count = sum(len(msgs) for msgs in ticket_base_messages.values())

    # Deterministic natural draw for candidate follow-ups
    # Draw uniform in [0, 1) per candidate using ticket_rng
    natural_selected_cands: set[str] = set()
    cand_scores: list[tuple[float, str, dict[str, Any]]] = []

    for cand in candidate_followups:
        tid = cand["ticket_id"]
        tnum = cand["ticket_number"]
        rng = ticket_rng(config.seed, tnum, "messages")
        score = rng.random()
        cand_scores.append((score, tid, cand))
        # Natural threshold: target is cfg.messages
        # We need approx (cfg.messages - base_count) / 2 candidates
        needed_pairs = max(0, (config.messages - base_count) // 2)
        prob = min(1.0, max(0.0, needed_pairs / max(1, len(candidate_followups))))
        if score < prob:
            natural_selected_cands.add(tid)

    natural_count = base_count + len(natural_selected_cands) * 2

    # Check 10% tolerance on natural count
    diff_pct = abs(natural_count - config.messages) / config.messages
    if diff_pct > 0.10:
        raise ValueError(
            f"Natural message total {natural_count} differs from target {config.messages} "
            f"by {diff_pct * 100:.2f}% (> 10% limit)"
        )

    # Calibration pass: adjust selected candidates deterministically to hit exact target
    # Sort candidates by score
    cand_scores.sort(key=lambda x: (x[0], x[1]))
    target_pairs = max(0, (config.messages - base_count) // 2)
    if target_pairs > len(cand_scores):
        raise ValueError(
            f"Cannot calibrate to {config.messages} messages: required {target_pairs} follow-up pairs "
            f"but only {len(cand_scores)} candidate tickets available."
        )
    has_single = ((config.messages - base_count) % 2 == 1) and (len(cand_scores) > target_pairs)
    selected_tids = {cand_scores[i][1] for i in range(min(len(cand_scores), target_pairs))}
    single_tid = cand_scores[target_pairs][1] if has_single else ""

    # Build final message list
    all_final_messages: list[dict[str, Any]] = []
    cand_map = {c["ticket_id"]: c for c in candidate_followups}

    for tid in sorted(ticket_base_messages.keys()):
        msgs = list(ticket_base_messages[tid])

        # If selected for follow-up pair or single follow-up, insert follow-up message(s)
        if tid in selected_tids or tid == single_tid:
            cand_info = cand_map[tid]
            tkt_lang = cand_info["tkt_lang"]
            agent_lang = cand_info["agent_lang"]

            m_cust = {
                "ticket_id": tid,
                "cycle_no": 1,
                "sender_type": "CUSTOMER",
                "sender_agent_id": "",
                "message_type": cand_info["m_type"],
                "in_reply_to_tag": "latest_agent",
                "sent_at_sec": cand_info["t_cust"],
                "sent_at": _sec_to_iso(cand_info["t_cust"]),
                "language_code": tkt_lang,
                "template_id": f"TPL-FOLLOWUP-CUSTOMER-{tkt_lang}",
                "body": templates["followups"]["CUSTOMER"][tkt_lang],
            }
            if tid in selected_tids:
                m_agent = {
                    "ticket_id": tid,
                    "cycle_no": 1,
                    "sender_type": "AGENT",
                    "sender_agent_id": cand_info["owner_agent"],
                    "message_type": cand_info["m_type"],
                    "in_reply_to_tag": "latest_customer",
                    "sent_at_sec": cand_info["t_agent"],
                    "sent_at": _sec_to_iso(cand_info["t_agent"]),
                    "language_code": agent_lang,
                    "template_id": f"TPL-FOLLOWUP-AGENT-{agent_lang}",
                    "body": templates["followups"]["AGENT"][agent_lang],
                }
                msgs.extend([m_cust, m_agent])
            else:
                msgs.append(m_cust)

        # Sort messages by sent_at_sec, breaking ties by sender type (CUSTOMER, then AGENT, then SYSTEM)
        sender_order = {"CUSTOMER": 0, "AGENT": 1, "SYSTEM": 2}
        msgs.sort(key=lambda m: (m["sent_at_sec"], sender_order.get(m["sender_type"], 9)))

        # Assign seq_no, IDs, and resolve in_reply_to threading
        seq = 0
        last_customer_id = ""
        last_agent_id = ""
        last_any_id = ""

        for m in msgs:
            seq += 1
            mid = ids.message_id(tid, seq)
            m["message_id"] = mid
            m["seq_no"] = seq

            # Determine in_reply_to_message_id
            if seq == 1:
                reply_id = ""
            else:
                tag = m.get("in_reply_to_tag", "")
                if tag == "m1":
                    reply_id = ids.message_id(tid, 1)
                elif tag == "latest_customer":
                    reply_id = last_customer_id if last_customer_id else last_any_id
                elif tag == "latest_agent":
                    reply_id = last_agent_id if last_agent_id else last_any_id
                else:  # latest_any
                    reply_id = last_any_id

            m["in_reply_to_message_id"] = reply_id

            if m["sender_type"] == "CUSTOMER":
                last_customer_id = mid
            elif m["sender_type"] == "AGENT":
                last_agent_id = mid
            last_any_id = mid

            all_final_messages.append(m)

    if len(all_final_messages) != config.messages:
        raise ValueError(
            f"Message calibration failed to reach exact target: "
            f"produced {len(all_final_messages)} messages, expected {config.messages}."
        )

    cols = [
        "message_id",
        "ticket_id",
        "seq_no",
        "cycle_no",
        "sender_type",
        "sender_agent_id",
        "message_type",
        "in_reply_to_message_id",
        "sent_at",
        "language_code",
        "template_id",
        "body",
    ]
    df = pd.DataFrame(all_final_messages)[cols]
    return df.sort_values(["ticket_id", "seq_no"]).reset_index(drop=True), natural_count


# =============================================================================
# 4. SLAEvent (Final, one row per cycle)
# =============================================================================

def build_sla_events(
    config: Config,
    master_tables: Mapping[str, pd.DataFrame],
    ticket_draft_df: pd.DataFrame,
    status_event_df: pd.DataFrame,
    cycle_outcome_df: pd.DataFrame,
    message_df: pd.DataFrame,
) -> pd.DataFrame:
    """Generate final SLAEvent table matching data_dictionary.md 19 and business_rules.md 8."""
    cutoff_dt = _iso_to_dt(config.cutoff)

    # Pre-index ticket metadata
    tkt_meta = ticket_draft_df.set_index("ticket_id").to_dict(orient="index")

    # Find first agent message per (ticket_id, cycle_no)
    agent_msgs = message_df[message_df["sender_type"] == "AGENT"]
    first_agent_msgs = (
        agent_msgs.sort_values(["ticket_id", "seq_no"])
        .groupby(["ticket_id", "cycle_no"])
        .first()
        .reset_index()
    )
    first_resp_map: dict[tuple[str, int], str] = {
        (str(row["ticket_id"]), int(row["cycle_no"])): str(row["sent_at"])
        for _, row in first_agent_msgs.iterrows()
    }

    # Find cycle start times and resolved times from status events
    # Cycle 1 start is ticket created_at
    # Cycle 2 start is first REOPENED event
    # Cycle 3 start is second REOPENED event
    reopens = (
        status_event_df[status_event_df["to_status"] == "REOPENED"]
        .sort_values(["ticket_id", "seq_no"])
        .groupby("ticket_id")
    )
    reopen_times: dict[str, list[str]] = {}
    for tid, grp in reopens:
        reopen_times[str(tid)] = list(grp["event_at"])

    # Resolved times per (ticket_id, cycle_no)
    resolved_events = (
        status_event_df[status_event_df["to_status"] == "RESOLVED"]
        .sort_values(["ticket_id", "seq_no"])
        .groupby(["ticket_id", "cycle_no"])
        .first()
        .reset_index()
    )
    resolved_map: dict[tuple[str, int], str] = {
        (str(row["ticket_id"]), int(row["cycle_no"])): str(row["event_at"])
        for _, row in resolved_events.iterrows()
    }

    prio_rules_df = master_tables["PriorityRule"]

    rows: list[dict[str, Any]] = []

    # Iterate over every cycle in cycle_outcome_df
    for c_row in cycle_outcome_df.to_dict(orient="records"):
        tid = str(c_row["ticket_id"])
        cyc = int(c_row["cycle_no"])
        meta = tkt_meta[tid]

        sla_id = ids.sla_event_id(tid, cyc)

        # Cycle start time
        if cyc == 1:
            cycle_start_str = str(meta["created_at"])
        else:
            reopen_list = reopen_times.get(tid, [])
            idx = cyc - 2
            if idx < len(reopen_list):
                cycle_start_str = reopen_list[idx]
            else:
                cycle_start_str = str(c_row.get("cycle_start_at", meta["created_at"]))

        cycle_start_dt = _iso_to_dt(cycle_start_str)
        cycle_start_date = cycle_start_dt.strftime("%Y-%m-%d")

        prio_lvl = str(meta["priority_level"])

        # Find effective SLA_TARGET rule on cycle start date
        sla_targets = prio_rules_df[
            (prio_rules_df["rule_type"] == "SLA_TARGET")
            & (prio_rules_df["priority_level"] == prio_lvl)
            & (prio_rules_df["effective_from"] <= cycle_start_date)
            & (
                prio_rules_df["effective_to"].isna()
                | (prio_rules_df["effective_to"] == "")
                | (prio_rules_df["effective_to"] >= cycle_start_date)
            )
        ]
        rule = sla_targets.iloc[0]
        rule_id = str(rule["rule_id"])

        frt_min = int(rule["first_response_min"])
        res_min = int(rule["resolution_min"])
        esc_min = int(rule["escalation_after_min"])

        # Reopen halving
        if cyc > 1:
            frt_min = int(np.ceil(frt_min * 0.5))
            res_min = int(np.ceil(res_min * 0.5))

        cal = "CAL-247" if prio_lvl == "P1" else f"CAL-{meta['channel_id']}"
        clock_start = sla_calendar.next_open(cal, cycle_start_dt)
        frt_due = sla_calendar.add_business_minutes(cal, clock_start, frt_min)
        res_due = sla_calendar.add_business_minutes(cal, clock_start, res_min)
        esc_due = sla_calendar.add_business_minutes(cal, clock_start, esc_min)

        # Actuals
        first_resp_str = first_resp_map.get((tid, cyc), "")
        res_str = resolved_map.get((tid, cyc), "")

        # Business minutes
        if first_resp_str:
            resp_dt = _iso_to_dt(first_resp_str)
            frt_bmin = sla_calendar.business_minutes(cal, clock_start, resp_dt)
            frt_bmin_str = f"{frt_bmin:.1f}"
            frt_breached = resp_dt > frt_due
        else:
            frt_bmin_str = ""
            frt_breached = cutoff_dt > frt_due

        if res_str:
            res_dt = _iso_to_dt(res_str)
            res_bmin = sla_calendar.business_minutes(cal, clock_start, res_dt)
            res_bmin_str = f"{res_bmin:.1f}"
            res_breached = res_dt > res_due
        else:
            res_bmin_str = ""
            res_breached = cutoff_dt > res_due

        rows.append({
            "sla_event_id": sla_id,
            "ticket_id": tid,
            "cycle_no": cyc,
            "priority_rule_id": rule_id,
            "calendar_code_used": cal,
            "clock_start": _dt_to_iso(clock_start),
            "first_response_target_min": frt_min,
            "resolution_target_min": res_min,
            "escalation_after_min": esc_min,
            "first_response_due": _dt_to_iso(frt_due),
            "resolution_due": _dt_to_iso(res_due),
            "escalation_due_at": _dt_to_iso(esc_due),
            "first_response_at": first_resp_str,
            "resolved_at": res_str,
            "frt_business_minutes": frt_bmin_str,
            "resolution_business_minutes": res_bmin_str,
            "frt_breached": frt_breached,
            "resolution_breached": res_breached,
        })

    cols = [
        "sla_event_id",
        "ticket_id",
        "cycle_no",
        "priority_rule_id",
        "calendar_code_used",
        "clock_start",
        "first_response_target_min",
        "resolution_target_min",
        "escalation_after_min",
        "first_response_due",
        "resolution_due",
        "escalation_due_at",
        "first_response_at",
        "resolved_at",
        "frt_business_minutes",
        "resolution_business_minutes",
        "frt_breached",
        "resolution_breached",
    ]
    df = pd.DataFrame(rows)[cols]
    return df.sort_values(["ticket_id", "cycle_no"]).reset_index(drop=True)


# =============================================================================
# 5. Resolution (Final, one row per resolved cycle)
# =============================================================================

def build_resolutions(
    config: Config,
    master_tables: Mapping[str, pd.DataFrame],
    ticket_draft_df: pd.DataFrame,
    status_event_df: pd.DataFrame,
    assignment_event_df: pd.DataFrame,
    cycle_outcome_df: pd.DataFrame,
    sla_event_df: pd.DataFrame,
) -> pd.DataFrame:
    """Generate final Resolution table matching data_dictionary.md 20 and business_rules.md 10."""
    cutoff_sec = _iso_to_sec(config.cutoff)

    # Pre-index metadata
    tkt_meta = ticket_draft_df.set_index("ticket_id").to_dict(orient="index")
    sla_map = sla_event_df.set_index(["ticket_id", "cycle_no"]).to_dict(orient="index")

    # Cycle counts of transfers and escalations
    asn_by_ticket_cyc = assignment_event_df.groupby(["ticket_id", "cycle_no"])
    transfer_counts = (
        assignment_event_df[assignment_event_df["event_type"] == "TRANSFER"]
        .groupby(["ticket_id", "cycle_no"])
        .size()
        .to_dict()
    )
    escalate_counts = (
        assignment_event_df[assignment_event_df["event_type"] == "ESCALATE"]
        .groupby(["ticket_id", "cycle_no"])
        .size()
        .to_dict()
    )

    # First assignment language match per (ticket_id, cycle_no)
    first_asns = (
        assignment_event_df.sort_values(["ticket_id", "seq_no"])
        .groupby(["ticket_id", "cycle_no"])
        .first()
        .reset_index()
    )
    lang_mismatch_map: dict[tuple[str, int], int] = {}
    for _, a in first_asns.iterrows():
        lm = a["language_match"]
        is_match = (lm is True or lm == "true" or lm == 1)
        lang_mismatch_map[(str(a["ticket_id"]), int(a["cycle_no"]))] = 0 if is_match else 1

    # Cycle start times
    reopens = (
        status_event_df[status_event_df["to_status"] == "REOPENED"]
        .sort_values(["ticket_id", "seq_no"])
        .groupby("ticket_id")
    )
    reopen_times: dict[str, list[str]] = {}
    for tid, grp in reopens:
        reopen_times[str(tid)] = list(grp["event_at"])

    # Find owner at resolved_at from status_event_df actor_agent_id
    res_events = (
        status_event_df[status_event_df["to_status"] == "RESOLVED"]
        .sort_values(["ticket_id", "seq_no"])
        .groupby(["ticket_id", "cycle_no"])
        .first()
        .reset_index()
    )

    # Map cycle outcome info
    cyc_outcome_map = cycle_outcome_df.set_index(["ticket_id", "cycle_no"]).to_dict(orient="index")

    rows: list[dict[str, Any]] = []

    for _, r_ev in res_events.iterrows():
        tid = str(r_ev["ticket_id"])
        cyc = int(r_ev["cycle_no"])
        tnum = int(tid.split("-")[-1])
        meta = tkt_meta[tid]
        sla_info = sla_map.get((tid, cyc), {})
        cyc_out = cyc_outcome_map.get((tid, cyc), {})

        res_id = ids.resolution_id(tid, cyc)
        resolved_at_str = str(r_ev["event_at"])
        resolved_sec = _iso_to_sec(resolved_at_str)

        # Cycle start sec
        if cyc == 1:
            cycle_start_sec = _iso_to_sec(str(meta["created_at"]))
        else:
            reopen_list = reopen_times.get(tid, [])
            idx = cyc - 2
            if idx < len(reopen_list):
                cycle_start_sec = _iso_to_sec(reopen_list[idx])
            else:
                cycle_start_sec = _iso_to_sec(str(cyc_out.get("cycle_start_at", meta["created_at"])))

        wall_minutes = max(0.0, (resolved_sec - cycle_start_sec) / 60.0)

        # Resolution code (business_rules.md 7.7)
        res_kind = str(cyc_out.get("resolution_kind", ""))
        if res_kind == "NO_CUSTOMER_RESPONSE":
            res_code = "NO_CUSTOMER_RESPONSE"
        else:
            intent = str(meta["intent_id"])
            rng_code = ticket_rng(config.seed, tnum, f"res_code_c{cyc}").random()
            if intent == "INT-002":  # REFUND_REQUEST
                if rng_code < 0.60:
                    res_code = "REFUNDED"
                elif rng_code < 0.90:
                    res_code = "SOLVED"
                else:
                    res_code = "INFO_PROVIDED"
            elif intent == "INT-003":  # PAYMENT_FAILURE
                if rng_code < 0.15:
                    res_code = "REFUNDED"
                elif rng_code < 0.85:
                    res_code = "SOLVED"
                else:
                    res_code = "INFO_PROVIDED"
            else:
                if rng_code < 0.55:
                    res_code = "SOLVED"
                elif rng_code < 0.80:
                    res_code = "INFO_PROVIDED"
                elif rng_code < 0.90:
                    res_code = "WORKAROUND"
                else:
                    res_code = "DUPLICATE"

        # Resolved by agent
        res_agent = str(r_ev["actor_agent_id"])
        if not res_agent:
            res_agent = str(cyc_out.get("resolved_by_agent_id", ""))

        # Transfer & escalation counts
        transfers = transfer_counts.get((tid, cyc), 0)
        escalations = escalate_counts.get((tid, cyc), 0)

        # CSAT survey (business_rules.md 10)
        ch = str(meta["channel_id"])
        resp_rate = 0.45 if ch == "CHAT" else (0.35 if ch == "PHONE" else 0.30)

        rng_resp = ticket_rng(config.seed, tnum, f"csat_resp_c{cyc}").random()
        csat_responded = rng_resp < resp_rate

        if csat_responded:
            frt_b = 1 if sla_info.get("frt_breached", False) else 0
            res_b = 1 if sla_info.get("resolution_breached", False) else 0
            esc_b = 1 if escalations > 0 else 0
            lang_m = lang_mismatch_map.get((tid, cyc), 0)
            cyc_b = 1 if cyc > 1 else 0

            norm_noise = ticket_rng(config.seed, tnum, f"csat_score_c{cyc}").normal(0.0, 0.6)
            latent = (
                4.3
                - 0.9 * frt_b
                - 0.9 * res_b
                - 0.25 * transfers
                - 0.35 * esc_b
                - 0.5 * lang_m
                - 0.5 * cyc_b
                + norm_noise
            )
            csat_score_val = max(1, min(5, int(round(latent))))
            csat_score_str = str(csat_score_val)

            delay_sec = max(
                1,
                int(round(ticket_rng(config.seed, tnum, f"csat_time_c{cyc}").lognormal(mean=np.log(1800), sigma=1.0))),
            )
            sub_sec = min(resolved_sec + delay_sec, cutoff_sec)
            csat_sub_str = _sec_to_iso(sub_sec)
        else:
            csat_score_str = ""
            csat_sub_str = ""

        rows.append({
            "resolution_id": res_id,
            "ticket_id": tid,
            "cycle_no": cyc,
            "resolved_at": resolved_at_str,
            "resolution_code": res_code,
            "resolved_by_agent_id": res_agent,
            "time_to_resolve_minutes": f"{wall_minutes:.1f}",
            "transfer_count": transfers,
            "escalation_count": escalations,
            "csat_responded": csat_responded,
            "csat_score": csat_score_str,
            "csat_submitted_at": csat_sub_str,
        })

    cols = [
        "resolution_id",
        "ticket_id",
        "cycle_no",
        "resolved_at",
        "resolution_code",
        "resolved_by_agent_id",
        "time_to_resolve_minutes",
        "transfer_count",
        "escalation_count",
        "csat_responded",
        "csat_score",
        "csat_submitted_at",
    ]
    df = pd.DataFrame(rows)[cols]
    return df.sort_values(["ticket_id", "cycle_no"]).reset_index(drop=True)
