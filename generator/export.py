"""Exporters for synthetic data tables (CSV, JSON, SQL, and MANIFEST).

Implements data_dictionary.md section 0 and Part 2 packaging requirements:
- CSV: data/raw/<table>.csv
- JSON: data/raw_json/<table>.json (records orient, UTF-8, LF)
- SQL: data/seed.sql (CREATE TABLE with SQLite types/PKs/FKs and INSERT in dependency order)
- MANIFEST: MANIFEST.json (seed, CLI args, version constant, per-file row count and SHA-256)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
import numpy as np
import pandas as pd

GENERATOR_VERSION = "1.0.0"

# Dependency order for relational integrity
TABLE_DEPENDENCY_ORDER = [
    "Language",
    "SLACalendar",
    "SLACalendarWindow",
    "Channel",
    "Customer",
    "Skill",
    "Intent",
    "PriorityRule",
    "Queue",
    "Agent",
    "AgentShift",
    "AgentSkill",
    "AgentLanguage",
    "QueueMember",
    "Ticket",
    "TicketStatusEvent",
    "AssignmentEvent",
    "Message",
    "SLAEvent",
    "Resolution",
]

SQL_SCHEMAS: dict[str, str] = {
    "Language": """CREATE TABLE IF NOT EXISTS language (
    language_code TEXT PRIMARY KEY,
    language_name TEXT NOT NULL UNIQUE,
    is_supported INTEGER NOT NULL
);""",
    "SLACalendar": """CREATE TABLE IF NOT EXISTS sla_calendar (
    calendar_code TEXT PRIMARY KEY,
    calendar_name TEXT NOT NULL UNIQUE,
    timezone TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT
);""",
    "SLACalendarWindow": """CREATE TABLE IF NOT EXISTS sla_calendar_window (
    window_id TEXT PRIMARY KEY,
    calendar_code TEXT NOT NULL REFERENCES sla_calendar(calendar_code),
    day_of_week INTEGER NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL
);""",
    "Channel": """CREATE TABLE IF NOT EXISTS channel (
    channel_id TEXT PRIMARY KEY,
    channel_name TEXT NOT NULL UNIQUE,
    is_realtime INTEGER NOT NULL,
    calendar_code TEXT NOT NULL REFERENCES sla_calendar(calendar_code),
    message_type TEXT NOT NULL
);""",
    "Customer": """CREATE TABLE IF NOT EXISTS customer (
    customer_id TEXT PRIMARY KEY,
    customer_type TEXT NOT NULL,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    phone TEXT,
    segment TEXT NOT NULL,
    country_code TEXT NOT NULL,
    preferred_language TEXT NOT NULL REFERENCES language(language_code),
    created_at TEXT NOT NULL
);""",
    "Skill": """CREATE TABLE IF NOT EXISTS skill (
    skill_id TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL UNIQUE,
    description TEXT
);""",
    "Intent": """CREATE TABLE IF NOT EXISTS intent (
    intent_id TEXT PRIMARY KEY,
    intent_code TEXT NOT NULL UNIQUE,
    intent_name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    required_skill_id TEXT NOT NULL REFERENCES skill(skill_id),
    default_priority TEXT NOT NULL,
    is_safety_related INTEGER NOT NULL,
    allows_prospect INTEGER NOT NULL,
    median_handle_minutes INTEGER NOT NULL,
    volume_weight_pct NUMERIC NOT NULL
);""",
    "PriorityRule": """CREATE TABLE IF NOT EXISTS priority_rule (
    rule_id TEXT PRIMARY KEY,
    rule_type TEXT NOT NULL,
    rule_name TEXT NOT NULL UNIQUE,
    priority_level TEXT,
    segment TEXT,
    upgrade_levels INTEGER,
    floor_priority_level TEXT,
    first_response_min INTEGER,
    resolution_min INTEGER,
    escalation_after_min INTEGER,
    calendar_code_override TEXT REFERENCES sla_calendar(calendar_code),
    precedence INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    description TEXT NOT NULL
);""",
    "Queue": """CREATE TABLE IF NOT EXISTS queue (
    queue_id TEXT PRIMARY KEY,
    queue_name TEXT NOT NULL UNIQUE,
    queue_type TEXT NOT NULL,
    required_skill_id TEXT REFERENCES skill(skill_id),
    min_tier TEXT NOT NULL
);""",
    "Agent": """CREATE TABLE IF NOT EXISTS agent (
    agent_id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    tier TEXT NOT NULL,
    concurrency_limit INTEGER NOT NULL,
    status TEXT NOT NULL,
    hire_date TEXT NOT NULL
);""",
    "AgentShift": """CREATE TABLE IF NOT EXISTS agent_shift (
    shift_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agent(agent_id),
    day_of_week INTEGER NOT NULL,
    shift_code TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT
);""",
    "AgentSkill": """CREATE TABLE IF NOT EXISTS agent_skill (
    agent_id TEXT NOT NULL REFERENCES agent(agent_id),
    skill_id TEXT NOT NULL REFERENCES skill(skill_id),
    proficiency TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    PRIMARY KEY (agent_id, skill_id, effective_from)
);""",
    "AgentLanguage": """CREATE TABLE IF NOT EXISTS agent_language (
    agent_id TEXT NOT NULL REFERENCES agent(agent_id),
    language_code TEXT NOT NULL REFERENCES language(language_code),
    fluency TEXT NOT NULL,
    PRIMARY KEY (agent_id, language_code)
);""",
    "QueueMember": """CREATE TABLE IF NOT EXISTS queue_member (
    queue_id TEXT NOT NULL REFERENCES queue(queue_id),
    agent_id TEXT NOT NULL REFERENCES agent(agent_id),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    PRIMARY KEY (queue_id, agent_id, effective_from)
);""",
    "Ticket": """CREATE TABLE IF NOT EXISTS ticket (
    ticket_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customer(customer_id),
    channel_id TEXT NOT NULL REFERENCES channel(channel_id),
    intent_id TEXT NOT NULL REFERENCES intent(intent_id),
    queue_id TEXT NOT NULL REFERENCES queue(queue_id),
    language_code TEXT NOT NULL REFERENCES language(language_code),
    created_at TEXT NOT NULL,
    subject TEXT NOT NULL,
    priority_level TEXT NOT NULL,
    priority_reason_code TEXT NOT NULL,
    priority_rule_id TEXT NOT NULL REFERENCES priority_rule(rule_id),
    is_critical_incident INTEGER NOT NULL,
    incident_id TEXT,
    is_after_hours_arrival INTEGER NOT NULL,
    current_status TEXT NOT NULL,
    last_status_at TEXT NOT NULL,
    reopen_count INTEGER NOT NULL
);""",
    "TicketStatusEvent": """CREATE TABLE IF NOT EXISTS ticket_status_event (
    status_event_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES ticket(ticket_id),
    seq_no INTEGER NOT NULL,
    cycle_no INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    event_at TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_agent_id TEXT REFERENCES agent(agent_id),
    trigger_ref TEXT
);""",
    "AssignmentEvent": """CREATE TABLE IF NOT EXISTS assignment_event (
    assignment_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES ticket(ticket_id),
    seq_no INTEGER NOT NULL,
    cycle_no INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    event_at TEXT NOT NULL,
    agent_id TEXT NOT NULL REFERENCES agent(agent_id),
    from_agent_id TEXT REFERENCES agent(agent_id),
    queue_id TEXT NOT NULL REFERENCES queue(queue_id),
    required_skill_id TEXT NOT NULL REFERENCES skill(skill_id),
    language_match INTEGER NOT NULL,
    wait_minutes NUMERIC NOT NULL,
    agent_load_before INTEGER NOT NULL,
    agent_load_after INTEGER NOT NULL,
    concurrency_limit INTEGER NOT NULL
);""",
    "Message": """CREATE TABLE IF NOT EXISTS message (
    message_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES ticket(ticket_id),
    seq_no INTEGER NOT NULL,
    cycle_no INTEGER NOT NULL,
    sender_type TEXT NOT NULL,
    sender_agent_id TEXT REFERENCES agent(agent_id),
    message_type TEXT NOT NULL,
    in_reply_to_message_id TEXT REFERENCES message(message_id),
    sent_at TEXT NOT NULL,
    language_code TEXT NOT NULL REFERENCES language(language_code),
    template_id TEXT,
    body TEXT NOT NULL
);""",
    "SLAEvent": """CREATE TABLE IF NOT EXISTS sla_event (
    sla_event_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES ticket(ticket_id),
    cycle_no INTEGER NOT NULL,
    priority_rule_id TEXT NOT NULL REFERENCES priority_rule(rule_id),
    calendar_code_used TEXT NOT NULL REFERENCES sla_calendar(calendar_code),
    clock_start TEXT NOT NULL,
    first_response_target_min INTEGER NOT NULL,
    resolution_target_min INTEGER NOT NULL,
    escalation_after_min INTEGER NOT NULL,
    first_response_due TEXT NOT NULL,
    resolution_due TEXT NOT NULL,
    escalation_due_at TEXT NOT NULL,
    first_response_at TEXT,
    resolved_at TEXT,
    frt_business_minutes NUMERIC,
    resolution_business_minutes NUMERIC,
    frt_breached INTEGER NOT NULL,
    resolution_breached INTEGER NOT NULL
);""",
    "Resolution": """CREATE TABLE IF NOT EXISTS resolution (
    resolution_id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES ticket(ticket_id),
    cycle_no INTEGER NOT NULL,
    resolved_at TEXT NOT NULL,
    resolution_code TEXT NOT NULL,
    resolved_by_agent_id TEXT NOT NULL REFERENCES agent(agent_id),
    time_to_resolve_minutes NUMERIC NOT NULL,
    transfer_count INTEGER NOT NULL,
    escalation_count INTEGER NOT NULL,
    csat_responded INTEGER NOT NULL,
    csat_score INTEGER,
    csat_submitted_at TEXT
);""",
}


def table_to_filename(table_name: str, ext: str = "csv") -> str:
    """Convert table name (e.g. 'AgentShift') to filename (e.g. 'agent_shift.csv')."""
    clean = table_name.removesuffix(f".{ext}").removesuffix(".csv")
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", clean)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()
    return f"{snake}.{ext}"


def format_dataframe_for_export(df: pd.DataFrame) -> pd.DataFrame:
    """Prepare DataFrame for CSV serialization."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == "bool":
            out[col] = out[col].map({True: "true", False: "false"})
        elif out[col].dtype == "object":
            out[col] = out[col].map(
                lambda x: "true" if x is True else ("false" if x is False else x)
            )
        if pd.api.types.is_float_dtype(out[col]):
            out[col] = out[col].map(
                lambda x: "" if pd.isna(x) else f"{x:.1f}"
            )
    return out


def export_tables(
    tables: Mapping[str, pd.DataFrame], out_dir: str | Path
) -> dict[str, Path]:
    """Export each table DataFrame to a CSV file."""
    dest_dir = Path(out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    exported: dict[str, Path] = {}
    for table_name in sorted(tables.keys()):
        df = tables[table_name]
        filename = table_to_filename(table_name, "csv")
        file_path = dest_dir / filename

        formatted_df = format_dataframe_for_export(df)
        formatted_df.to_csv(
            file_path,
            index=False,
            encoding="utf-8",
            lineterminator="\n",
            na_rep="",
        )
        exported[table_name] = file_path

    return exported


def export_json(
    tables: Mapping[str, pd.DataFrame], out_dir: str | Path
) -> dict[str, Path]:
    """Export each table DataFrame to a JSON file (records orient, UTF-8, sorted)."""
    dest_dir = Path(out_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    exported: dict[str, Path] = {}
    for table_name in sorted(tables.keys()):
        df = tables[table_name]
        filename = table_to_filename(table_name, "json")
        file_path = dest_dir / filename

        # Format records: booleans as bool, empty strings/None/NaN as None
        records = df.to_dict(orient="records")
        for rec in records:
            for k, v in rec.items():
                if pd.isna(v) or v == "":
                    rec[k] = None
                elif isinstance(v, (bool, np.bool_)):
                    rec[k] = bool(v)

        content = json.dumps(records, indent=2, ensure_ascii=False)
        file_path.write_text(content + "\n", encoding="utf-8")
        exported[table_name] = file_path

    return exported


def export_sql(
    tables: Mapping[str, pd.DataFrame], out_path: str | Path
) -> Path:
    """Export all tables into a SQLite-compatible seed.sql script in relational dependency order."""
    dest_path = Path(out_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = [
        "-- YottaFlex SQLite Seed Database Script",
        "-- Generated automatically in relational dependency order",
        "PRAGMA foreign_keys = OFF;",
        "",
    ]

    # CREATE TABLE statements in dependency order
    for tname in TABLE_DEPENDENCY_ORDER:
        if tname in SQL_SCHEMAS:
            lines.append(SQL_SCHEMAS[tname])
            lines.append("")

    lines.append("BEGIN TRANSACTION;")
    lines.append("")

    # INSERT statements in dependency order
    for tname in TABLE_DEPENDENCY_ORDER:
        if tname not in tables:
            continue
        df = tables[tname]
        sql_table_name = table_to_filename(tname, "").removesuffix(".")
        cols = list(df.columns)
        cols_str = ", ".join(cols)

        for _, row in df.iterrows():
            vals: list[str] = []
            for c in cols:
                v = row[c]
                if pd.isna(v) or v == "" or v is None:
                    vals.append("NULL")
                elif isinstance(v, bool) or v in (True, False, "true", "false"):
                    b_val = 1 if v in (True, "true") else 0
                    vals.append(str(b_val))
                elif isinstance(v, (int, np.integer)):
                    vals.append(str(int(v)))
                elif isinstance(v, (float, np.floating)):
                    vals.append(f"{float(v):.1f}")
                else:
                    s_val = str(v).replace("'", "''")
                    vals.append(f"'{s_val}'")
            vals_str = ", ".join(vals)
            lines.append(f"INSERT INTO {sql_table_name} ({cols_str}) VALUES ({vals_str});")

    lines.append("")
    lines.append("COMMIT;")
    lines.append("PRAGMA foreign_keys = ON;")
    lines.append("")

    dest_path.write_text("\n".join(lines), encoding="utf-8")
    return dest_path


def compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def count_file_rows(path: Path) -> int:
    """Count rows (excluding header) for CSV or JSON files."""
    if path.suffix == ".csv":
        with path.open("r", encoding="utf-8") as f:
            lines = sum(1 for line in f if line.strip())
        return max(0, lines - 1)
    elif path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return len(data) if isinstance(data, list) else 1
        except Exception:
            return 0
    elif path.suffix == ".sql":
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for line in f if line.startswith("INSERT INTO "))
    return 0


def export_manifest(
    seed: int,
    cli_args: list[str] | dict[str, Any],
    file_paths: Mapping[str, Path],
    out_path: str | Path,
    version: str = GENERATOR_VERSION,
) -> Path:
    """Write reproducible MANIFEST.json listing file row counts and SHA-256 hashes without timestamps."""
    dest_path = Path(out_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    files_info: dict[str, dict[str, Any]] = {}
    for name in sorted(file_paths.keys()):
        p = file_paths[name]
        if p.exists():
            files_info[name] = {
                "filename": p.name,
                "row_count": count_file_rows(p),
                "sha256": compute_file_sha256(p),
            }

    manifest_data = {
        "generator_version": version,
        "seed": seed,
        "cli_arguments": cli_args if isinstance(cli_args, list) else [f"--{k}={v}" for k, v in cli_args.items()],
        "files": files_info,
    }

    content = json.dumps(manifest_data, indent=2, sort_keys=True)
    dest_path.write_text(content + "\n", encoding="utf-8")
    return dest_path
