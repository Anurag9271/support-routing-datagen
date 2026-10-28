"""Command-line interface for the support routing data generator.

Usage:
    python -m generator --seed 20261022 --tickets 300 --agents 20 --customers 120 --out data/raw
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from generator.config import Config
from generator.rng import init_rng_streams
from generator.master_data import build_master_data
from generator.export import export_tables
from generator.windows import build_scenario_windows, export_scenario_windows
from generator.arrivals import generate_arrivals
from generator.tickets import (
    build_tickets,
    export_ticket_draft,
    export_ground_truth_decisions,
)
from generator.simulation import (
    simulate_routing,
    export_ticket_status_events,
    export_assignment_events,
    export_ticket_outcomes,
    generate_kpi_report,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Omnichannel Support Routing Synthetic Data Generator"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=20261022,
        help="Master random seed (default: 20261022)",
    )
    parser.add_argument(
        "--tickets",
        type=int,
        default=30000,
        help="Total ticket volume to generate (default: 30000)",
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=100,
        help="Total agent headcount (default: 100)",
    )
    parser.add_argument(
        "--messages",
        type=int,
        default=100000,
        help="Total messages count (default: 100000)",
    )
    parser.add_argument(
        "--customers",
        type=int,
        default=None,
        help="Total customer headcount (default: tickets x 0.4)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Arrival window duration in days (default: 90)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="data/raw",
        help="Output directory for raw CSV tables (default: data/raw)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run Phase 1 + Phase 2 generator."""
    args = parse_args(argv)

    customer_count = args.customers
    if customer_count is None:
        customer_count = round(args.tickets * 0.4)

    config = Config(
        seed=args.seed,
        tickets=args.tickets,
        agents=args.agents,
        messages=args.messages,
        customers=customer_count,
        days=args.days,
        out_dir=args.out,
    )

    print(
        f"Support Routing Generator (Phase 1 + 2) | Seed: {config.seed} | "
        f"Tickets: {config.tickets} | Agents: {config.agents} | Customers: {config.customers} | Out: {config.out_dir}"
    )

    # Isolated RNG streams
    streams = init_rng_streams(config.seed)

    # --- Phase 1: Master data & Customers ---
    tables = build_master_data(config, streams)
    exported = export_tables(tables, config.out_dir)

    print(f"Phase 1: Generated and exported {len(exported)} master data tables to {config.out_dir}:")
    for t_name in sorted(exported.keys()):
        row_cnt = len(tables[t_name])
        print(f"  - {t_name:20s}: {row_cnt:6d} rows -> {exported[t_name]}")

    # Base directory for staging and ground_truth
    out_dir_path = Path(config.out_dir)
    base_dir = out_dir_path.parent if out_dir_path.name == "raw" else out_dir_path
    staging_dir = base_dir / "staging"
    ground_truth_dir = base_dir / "ground_truth"

    # --- Phase 2: Scenario windows, Arrivals, Tickets & Ground Truth ---
    incidents, spikes = build_scenario_windows(config, streams["arrivals"])
    all_windows = incidents + spikes
    windows_path = export_scenario_windows(
        all_windows, ground_truth_dir / "scenario_windows.csv"
    )
    print(f"Phase 2: Generated {len(incidents)} incidents and {len(spikes)} spikes -> {windows_path}")

    raw_arrivals = generate_arrivals(
        config,
        tables["Customer"],
        tables["Intent"],
        incidents,
        spikes,
        streams["arrivals"],
    )

    ticket_draft_df, gt_decisions_df = build_tickets(
        config, raw_arrivals, tables["Intent"], tables["PriorityRule"]
    )

    draft_path = export_ticket_draft(ticket_draft_df, staging_dir / "ticket_draft.csv")
    decisions_path = export_ground_truth_decisions(
        gt_decisions_df, ground_truth_dir / "ground_truth_decisions.csv"
    )

    print(f"Phase 2: Generated {len(ticket_draft_df)} ticket drafts -> {draft_path}")
    print(f"Phase 2: Generated {len(gt_decisions_df)} ground truth decisions -> {decisions_path}")

    # --- Phase 3a: Discrete-event routing simulation and staging drafts ---
    print("\nPhase 3a: Running discrete-event routing simulation...")
    status_df, assignment_df, outcome_df = simulate_routing(config, tables, ticket_draft_df)

    status_path = export_ticket_status_events(
        status_df, staging_dir / "ticket_status_event_draft.csv"
    )
    asn_path = export_assignment_events(
        assignment_df, staging_dir / "assignment_event_draft.csv"
    )
    outcome_path = export_ticket_outcomes(
        outcome_df, staging_dir / "ticket_outcome_draft.csv"
    )

    print(f"Phase 3a: Exported {len(status_df)} status events -> {status_path}")
    print(f"Phase 3a: Exported {len(assignment_df)} assignment events -> {asn_path}")
    print(f"Phase 3a: Exported {len(outcome_df)} ticket outcomes -> {outcome_path}")

    # Print Phase 3a KPI report
    kpi_report = generate_kpi_report(
        ticket_draft_df, status_df, assignment_df, outcome_df, tables, all_windows
    )
    print(kpi_report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
