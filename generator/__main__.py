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
    """Run Phase 1 generator: master data, population, and CSV export."""
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
        f"Support Routing Generator (Phase 1) | Seed: {config.seed} | "
        f"Agents: {config.agents} | Customers: {config.customers} | Out: {config.out_dir}"
    )

    streams = init_rng_streams(config.seed)
    tables = build_master_data(config, streams)
    exported = export_tables(tables, config.out_dir)

    print(f"Successfully generated and exported {len(exported)} tables:")
    for t_name in sorted(exported.keys()):
        row_cnt = len(tables[t_name])
        print(f"  - {t_name:20s}: {row_cnt:6d} rows -> {exported[t_name]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
