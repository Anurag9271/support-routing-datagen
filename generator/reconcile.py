"""Internal reconciliation sanity checks for Phase 3c.

Implements fast sanity checks run at the end of generation:
- PK uniqueness for every table (including composite PKs)
- FK integrity for every foreign key in data_dictionary.md section 22
- Message total equals cfg.messages
- Ticket.current_status matches the last status event's to_status
- Ticket.last_status_at matches the last status event's event_at
- SLAEvent.first_response_at equals the first agent message time in that cycle
- Cycle counts equal tickets + reopens
- Exactly one SLAEvent per cycle
- Exactly one Resolution per resolved cycle
- The sum of per-ticket message counts equals the Message row count
"""

from __future__ import annotations

from typing import Any, Mapping
import pandas as pd

from generator.config import Config


class ReconciliationError(Exception):
    """Raised when an internal reconciliation check fails."""
    pass


def reconcile_all(
    config: Config,
    master_tables: Mapping[str, pd.DataFrame],
    final_tables: Mapping[str, pd.DataFrame],
    post_anomalies: bool = False,
) -> dict[str, Any]:
    """Execute all Phase 3c internal reconciliation checks.

    Raises:
        ReconciliationError: if any check fails.

    Returns:
        Summary statistics dictionary.
    """
    report: dict[str, Any] = {}

    all_tables: dict[str, pd.DataFrame] = {**master_tables, **final_tables}

    # 1. PK Uniqueness for every table
    pk_specs: dict[str, list[str]] = {
        "Customer": ["customer_id"],
        "Ticket": ["ticket_id"],
        "Message": ["message_id"],
        "Agent": ["agent_id"],
        "AssignmentEvent": ["assignment_id"],
        "SLAEvent": ["sla_event_id"],
        "TicketStatusEvent": ["status_event_id"],
        "Resolution": ["resolution_id"],
        "Intent": ["intent_id"],
        "PriorityRule": ["rule_id"],
        "Skill": ["skill_id"],
        "Queue": ["queue_id"],
        "Channel": ["channel_id"],
        "Language": ["language_code"],
        "SLACalendar": ["calendar_code"],
        "SLACalendarWindow": ["window_id"],
        "AgentShift": ["shift_id"],
        "AgentSkill": ["agent_id", "skill_id", "effective_from"],
        "AgentLanguage": ["agent_id", "language_code"],
        "QueueMember": ["queue_id", "agent_id", "effective_from"],
        "GroundTruth": ["gt_id"],
    }

    for tname, pk_cols in pk_specs.items():
        if tname not in all_tables:
            continue
        df = all_tables[tname]
        dups = df.duplicated(subset=pk_cols, keep=False)
        if dups.any():
            dup_keys = df.loc[dups, pk_cols].head(5).to_dict(orient="records")
            raise ReconciliationError(
                f"PK uniqueness check failed for table {tname} on columns {pk_cols}. Duplicates: {dup_keys}"
            )

    report["pk_uniqueness"] = "PASS"

    # 2. FK Integrity (data_dictionary.md section 22)
    tkt_df = final_tables["Ticket"]
    st_df = final_tables["TicketStatusEvent"]
    asn_df = final_tables["AssignmentEvent"]
    msg_df = final_tables["Message"]
    sla_df = final_tables["SLAEvent"]
    res_df = final_tables["Resolution"]

    # Ticket FKs
    cust_ids = set(master_tables["Customer"]["customer_id"])
    chan_ids = set(master_tables["Channel"]["channel_id"])
    intent_ids = set(master_tables["Intent"]["intent_id"])
    queue_ids = set(master_tables["Queue"]["queue_id"])
    lang_codes = set(master_tables["Language"]["language_code"])
    prio_rule_ids = set(master_tables["PriorityRule"]["rule_id"])
    agent_ids = set(master_tables["Agent"]["agent_id"])
    skill_ids = set(master_tables["Skill"]["skill_id"])
    cal_codes = set(master_tables["SLACalendar"]["calendar_code"])
    tkt_ids = set(tkt_df["ticket_id"])
    msg_ids = set(msg_df["message_id"])

    # Ticket FK checks
    invalid = set(tkt_df["customer_id"]) - cust_ids
    if invalid:
        raise ReconciliationError(f"Ticket.customer_id FK invalid: {list(invalid)[:5]}")
    invalid = set(tkt_df["channel_id"]) - chan_ids
    if invalid:
        raise ReconciliationError(f"Ticket.channel_id FK invalid: {list(invalid)[:5]}")
    invalid = set(tkt_df["intent_id"]) - intent_ids
    if invalid:
        raise ReconciliationError(f"Ticket.intent_id FK invalid: {list(invalid)[:5]}")
    invalid = set(tkt_df["queue_id"]) - queue_ids
    if invalid:
        raise ReconciliationError(f"Ticket.queue_id FK invalid: {list(invalid)[:5]}")
    invalid = set(tkt_df["language_code"]) - lang_codes
    if invalid:
        raise ReconciliationError(f"Ticket.language_code FK invalid: {list(invalid)[:5]}")
    invalid = set(tkt_df["priority_rule_id"]) - prio_rule_ids
    if invalid:
        raise ReconciliationError(f"Ticket.priority_rule_id FK invalid: {list(invalid)[:5]}")

    # TicketStatusEvent FK checks
    invalid = set(st_df["ticket_id"]) - tkt_ids
    if invalid:
        raise ReconciliationError(f"TicketStatusEvent.ticket_id FK invalid: {list(invalid)[:5]}")
    agents_in_st = set(st_df.loc[st_df["actor_agent_id"].notna() & (st_df["actor_agent_id"] != ""), "actor_agent_id"])
    invalid = agents_in_st - agent_ids
    if invalid:
        raise ReconciliationError(f"TicketStatusEvent.actor_agent_id FK invalid: {list(invalid)[:5]}")

    # AssignmentEvent FK checks
    invalid = set(asn_df["ticket_id"]) - tkt_ids
    if invalid:
        raise ReconciliationError(f"AssignmentEvent.ticket_id FK invalid: {list(invalid)[:5]}")
    invalid = set(asn_df["agent_id"]) - agent_ids
    if invalid:
        raise ReconciliationError(f"AssignmentEvent.agent_id FK invalid: {list(invalid)[:5]}")
    from_agents = set(asn_df.loc[asn_df["from_agent_id"] != "", "from_agent_id"])
    invalid = from_agents - agent_ids
    if invalid:
        raise ReconciliationError(f"AssignmentEvent.from_agent_id FK invalid: {list(invalid)[:5]}")
    invalid = set(asn_df["queue_id"]) - queue_ids
    if invalid:
        raise ReconciliationError(f"AssignmentEvent.queue_id FK invalid: {list(invalid)[:5]}")
    invalid = set(asn_df["required_skill_id"]) - skill_ids
    if invalid:
        raise ReconciliationError(f"AssignmentEvent.required_skill_id FK invalid: {list(invalid)[:5]}")

    # Message FK checks
    invalid = set(msg_df["ticket_id"]) - tkt_ids
    if invalid:
        raise ReconciliationError(f"Message.ticket_id FK invalid: {list(invalid)[:5]}")
    senders = set(msg_df.loc[msg_df["sender_agent_id"].notna() & (msg_df["sender_agent_id"] != ""), "sender_agent_id"])
    invalid = senders - agent_ids
    if invalid:
        raise ReconciliationError(f"Message.sender_agent_id FK invalid: {list(invalid)[:5]}")
    invalid = set(msg_df["language_code"]) - lang_codes
    if invalid:
        raise ReconciliationError(f"Message.language_code FK invalid: {list(invalid)[:5]}")
    replies = set(msg_df.loc[msg_df["in_reply_to_message_id"].notna() & (msg_df["in_reply_to_message_id"] != ""), "in_reply_to_message_id"])
    invalid = replies - msg_ids
    if invalid:
        raise ReconciliationError(f"Message.in_reply_to_message_id FK invalid: {list(invalid)[:5]}")

    # SLAEvent FK checks
    invalid = set(sla_df["ticket_id"]) - tkt_ids
    if invalid:
        raise ReconciliationError(f"SLAEvent.ticket_id FK invalid: {list(invalid)[:5]}")
    invalid = set(sla_df["priority_rule_id"]) - prio_rule_ids
    if invalid:
        raise ReconciliationError(f"SLAEvent.priority_rule_id FK invalid: {list(invalid)[:5]}")
    invalid = set(sla_df["calendar_code_used"]) - cal_codes
    if invalid:
        raise ReconciliationError(f"SLAEvent.calendar_code_used FK invalid: {list(invalid)[:5]}")

    # Resolution FK checks
    invalid = set(res_df["ticket_id"]) - tkt_ids
    if invalid:
        raise ReconciliationError(f"Resolution.ticket_id FK invalid: {list(invalid)[:5]}")
    invalid = set(res_df["resolved_by_agent_id"]) - agent_ids
    if invalid:
        raise ReconciliationError(f"Resolution.resolved_by_agent_id FK invalid: {list(invalid)[:5]}")

    # GroundTruth FK checks (if present)
    if "GroundTruth" in all_tables:
        gt_df = all_tables["GroundTruth"]
        invalid = set(gt_df["ticket_id"]) - tkt_ids
        if invalid:
            raise ReconciliationError(f"GroundTruth.ticket_id FK invalid: {list(invalid)[:5]}")

    report["fk_integrity"] = "PASS"

    # 3. Message total equals cfg.messages
    if len(msg_df) != config.messages:
        raise ReconciliationError(
            f"Message row count {len(msg_df)} does not match config.messages {config.messages}"
        )
    report["message_count"] = len(msg_df)

    # 4. Ticket.current_status and last_status_at match last status event
    last_st = (
        st_df.sort_values(["ticket_id", "seq_no"])
        .groupby("ticket_id")
        .last()
        .reset_index()
    )
    last_st_map = dict(zip(last_st["ticket_id"], last_st["to_status"]))
    last_dt_map = dict(zip(last_st["ticket_id"], last_st["event_at"]))

    mismatch_status = []
    mismatch_time = []
    for _, t in tkt_df.iterrows():
        tid = str(t["ticket_id"])
        if t["current_status"] != last_st_map.get(tid):
            mismatch_status.append((tid, t["current_status"], last_st_map.get(tid)))
        if t["last_status_at"] != last_dt_map.get(tid):
            mismatch_time.append((tid, t["last_status_at"], last_dt_map.get(tid)))

    if mismatch_status:
        raise ReconciliationError(
            f"Ticket.current_status mismatch with last status event (showing 5): {mismatch_status[:5]}"
        )
    if mismatch_time:
        raise ReconciliationError(
            f"Ticket.last_status_at mismatch with last status event (showing 5): {mismatch_time[:5]}"
        )
    report["ticket_status_reconciliation"] = "PASS"

    # 5. first_response_at equals first agent message time
    agent_msgs = msg_df[msg_df["sender_type"] == "AGENT"]
    first_agent_msgs = (
        agent_msgs.sort_values(["ticket_id", "seq_no"])
        .groupby(["ticket_id", "cycle_no"])
        .first()
        .reset_index()
    )
    first_agent_map = {
        (str(row["ticket_id"]), int(row["cycle_no"])): str(row["sent_at"])
        for _, row in first_agent_msgs.iterrows()
    }

    mismatch_frt = []
    for _, s in sla_df.iterrows():
        tid = str(s["ticket_id"])
        cyc = int(s["cycle_no"])
        frt_at = str(s["first_response_at"]) if pd.notna(s["first_response_at"]) else ""
        expected_frt = first_agent_map.get((tid, cyc), "")
        if frt_at != expected_frt:
            mismatch_frt.append((tid, cyc, frt_at, expected_frt))

    if mismatch_frt:
        raise ReconciliationError(
            f"SLAEvent.first_response_at mismatch with first agent message (showing 5): {mismatch_frt[:5]}"
        )
    report["first_response_at_reconciliation"] = "PASS"

    # 6. Cycle counts equal tickets + reopens
    reopen_total = int(tkt_df["reopen_count"].sum())
    expected_cycles = len(tkt_df) + reopen_total
    if post_anomalies:
        report["cycle_count_reconciliation"] = f"SKIPPED_POST_ANOMALIES ({len(sla_df)} cycles remaining)"
    else:
        if len(sla_df) != expected_cycles:
            raise ReconciliationError(
                f"SLAEvent count {len(sla_df)} != tickets ({len(tkt_df)}) + reopens ({reopen_total}) = {expected_cycles}"
            )
        report["cycle_count_reconciliation"] = f"{len(sla_df)} == {len(tkt_df)} + {reopen_total}"

    # 7. One SLAEvent per cycle
    sla_keys = sla_df["ticket_id"].astype(str) + "_" + sla_df["cycle_no"].astype(str)
    if not sla_keys.is_unique:
        dups = sla_df[sla_keys.duplicated(keep=False)]
        raise ReconciliationError(f"Duplicate SLAEvent per ticket cycle: {dups.head(5).to_dict(orient='records')}")
    report["sla_event_uniqueness"] = "PASS"

    # 8. One Resolution per resolved cycle
    resolved_count = (st_df["to_status"] == "RESOLVED").sum()
    if post_anomalies:
        report["resolution_count_reconciliation"] = f"SKIPPED_POST_ANOMALIES ({len(res_df)} resolutions remaining)"
    else:
        if len(res_df) != resolved_count:
            raise ReconciliationError(
                f"Resolution count {len(res_df)} does not match RESOLVED status event count {resolved_count}"
            )
        report["resolution_count_reconciliation"] = f"{len(res_df)} == {resolved_count}"

    res_keys = res_df["ticket_id"].astype(str) + "_" + res_df["cycle_no"].astype(str)
    if not res_keys.is_unique:
        dups = res_df[res_keys.duplicated(keep=False)]
        raise ReconciliationError(f"Duplicate Resolution per ticket cycle: {dups.head(5).to_dict(orient='records')}")
    report["resolution_uniqueness"] = "PASS"

    # 9. Sum of per-ticket message counts equals Message row count
    tkt_msg_sum = int(msg_df.groupby("ticket_id").size().sum())
    if tkt_msg_sum != len(msg_df):
        raise ReconciliationError(
            f"Sum of per-ticket message counts ({tkt_msg_sum}) != total message count ({len(msg_df)})"
        )
    report["message_sum_reconciliation"] = "PASS"

    return report
