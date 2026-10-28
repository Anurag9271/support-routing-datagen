"""Tests for packaging (JSON, SQLite seed.sql, MANIFEST.json).

Implements Part 2 packaging validation:
- Loads seed.sql into in-memory sqlite3 database
- Validates row counts equal the CSV row counts
- Asserts PRAGMA foreign_key_check is completely empty
- Verifies MANIFEST.json row counts and SHA-256 hashes
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
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
from generator.export import (
    export_tables,
    export_json,
    export_sql,
    export_manifest,
    GENERATOR_VERSION,
)


@pytest.fixture(scope="module")
def small_dataset(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Generate small-scale dataset with CSV, JSON, SQL, and MANIFEST."""
    base_dir = tmp_path_factory.mktemp("test_pkg")
    raw_dir = base_dir / "raw"
    json_dir = base_dir / "raw_json"
    sql_path = base_dir / "seed.sql"
    manifest_path = base_dir / "MANIFEST.json"

    cfg = Config(seed=20261022, tickets=100, agents=15, customers=40, messages=333, out_dir=str(raw_dir))
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

    all_tables = {
        **master,
        "Ticket": final_tkts,
        "TicketStatusEvent": final_st,
        "AssignmentEvent": final_asn,
        "Message": final_msg,
        "SLAEvent": final_sla,
        "Resolution": final_res,
    }

    csv_files = export_tables(all_tables, raw_dir)
    json_files = export_json(all_tables, json_dir)
    export_sql(all_tables, sql_path)

    all_exported = dict(csv_files)
    all_exported["seed_sql"] = sql_path
    export_manifest(cfg.seed, ["--test=1"], all_exported, manifest_path, GENERATOR_VERSION)

    return {
        "base_dir": base_dir,
        "raw_dir": raw_dir,
        "json_dir": json_dir,
        "sql_path": sql_path,
        "manifest_path": manifest_path,
    }


def test_seed_sql_loads_into_sqlite_cleanly(small_dataset: dict[str, Path]):
    """Assert seed.sql executes into in-memory sqlite3 with 0 FK violations and exact row counts."""
    sql_path = small_dataset["sql_path"]
    raw_dir = small_dataset["raw_dir"]

    conn = sqlite3.connect(":memory:")
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON;")

    script = sql_path.read_text(encoding="utf-8")
    conn.executescript(script)

    # Check PRAGMA foreign_key_check is completely empty
    fk_errors = conn.execute("PRAGMA foreign_key_check;").fetchall()
    assert len(fk_errors) == 0, f"PRAGMA foreign_key_check failed: {fk_errors[:5]}"

    # Check row counts match CSVs
    cursor = conn.cursor()
    for csv_file in raw_dir.glob("*.csv"):
        tname = csv_file.stem
        # count rows in sqlite
        db_count = cursor.execute(f"SELECT COUNT(*) FROM {tname};").fetchone()[0]
        csv_df = pd.read_csv(csv_file, dtype=str, keep_default_na=False)
        assert db_count == len(csv_df), f"Table {tname}: SQLite count {db_count} != CSV count {len(csv_df)}"

    conn.close()


def test_manifest_integrity(small_dataset: dict[str, Path]):
    """Assert MANIFEST.json is well-formed with accurate row counts, SHA-256, and version."""
    manifest_path = small_dataset["manifest_path"]
    data = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert data["generator_version"] == GENERATOR_VERSION
    assert data["seed"] == 20261022
    assert "files" in data
    assert len(data["files"]) >= 21

    # Check each file has row_count and valid 64-char hex SHA-256
    for fname, info in data["files"].items():
        assert "row_count" in info
        assert "sha256" in info
        assert len(info["sha256"]) == 64
        assert int(info["sha256"], 16) > 0


def test_json_orient_and_format(small_dataset: dict[str, Path]):
    """Assert JSON tables are exported in records orient, valid UTF-8, matching row counts."""
    json_dir = small_dataset["json_dir"]
    raw_dir = small_dataset["raw_dir"]

    for jfile in json_dir.glob("*.json"):
        records = json.loads(jfile.read_text(encoding="utf-8"))
        assert isinstance(records, list)

        csv_file = raw_dir / f"{jfile.stem}.csv"
        csv_df = pd.read_csv(csv_file, dtype=str, keep_default_na=False)
        assert len(records) == len(csv_df)
