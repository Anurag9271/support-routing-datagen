"""Command-line interface for the support routing data generator.

Usage:
    python -m generator --seed 20261022 --tickets 300 --agents 20 --customers 120 --messages 1000 --out data/raw
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
    export_ticket_cycle_outcomes,
    generate_kpi_report,
)
from generator.final_tables import (
    build_final_tickets,
    build_final_ticket_status_events,
    build_final_assignment_events,
    build_messages,
    build_sla_events,
    build_resolutions,
)
from generator.reconcile import reconcile_all
from generator.anomalies import inject_anomalies


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Omnichannel Support Routing Synthetic Data Generator (Phases 1 - 3c)"
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
    parser.add_argument(
        "--no-anomalies",
        action="store_true",
        default=False,
        help="Disable anomaly injection and produce clean baseline data (Phase 3c)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run full generator pipeline (Phases 1, 2, 3a, 3b, 3c)."""
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
        anomalies_enabled=not args.no_anomalies,
    )

    print("=" * 76)
    print("      YOTTAFLEX OMNICHANNEL SUPPORT-ROUTING SYNTHETIC DATA GENERATOR        ")
    print("                       Phases 1, 2, 3a, 3b, 3c, 4                           ")
    print("=" * 76)
    print(
        f"Configuration: Seed={config.seed} | Tickets={config.tickets} | Agents={config.agents} | "
        f"Customers={config.customers} | Target Messages={config.messages} | "
        f"Anomalies={'ENABLED' if config.anomalies_enabled else 'DISABLED'} | Out={config.out_dir}"
    )

    # Isolated RNG streams
    streams = init_rng_streams(config.seed)

    # Base directory for staging and ground_truth
    out_dir_path = Path(config.out_dir)
    base_dir = out_dir_path.parent if out_dir_path.name == "raw" else out_dir_path
    staging_dir = base_dir / "staging"
    ground_truth_dir = base_dir / "ground_truth"

    # --- Phase 1: Master data & Customers ---
    print("\n[Phase 1] Building master data tables and customer directory...")
    master_tables = build_master_data(config, streams)
    exported_master = export_tables(master_tables, config.out_dir)
    print(f"  Exported {len(exported_master)} master tables to {config.out_dir}")

    # --- Phase 2: Scenario windows, Arrivals, Tickets & Ground Truth ---
    print("\n[Phase 2] Generating scenario windows, arrivals, and ticket drafts...")
    incidents, spikes = build_scenario_windows(config, streams["arrivals"])
    all_windows = incidents + spikes
    windows_path = export_scenario_windows(
        all_windows, ground_truth_dir / "scenario_windows.csv"
    )
    print(f"  Generated {len(incidents)} incidents and {len(spikes)} spikes -> {windows_path}")

    raw_arrivals = generate_arrivals(
        config,
        master_tables["Customer"],
        master_tables["Intent"],
        incidents,
        spikes,
        streams["arrivals"],
    )

    ticket_draft_df, gt_decisions_df = build_tickets(
        config, raw_arrivals, master_tables["Intent"], master_tables["PriorityRule"]
    )

    draft_path = export_ticket_draft(ticket_draft_df, staging_dir / "ticket_draft.csv")
    decisions_path = export_ground_truth_decisions(
        gt_decisions_df, ground_truth_dir / "ground_truth_decisions.csv"
    )
    print(f"  Generated {len(ticket_draft_df)} ticket drafts -> {draft_path}")
    print(f"  Generated {len(gt_decisions_df)} ground truth decisions -> {decisions_path}")

    # --- Phase 3a/3b: Discrete-event routing simulation and staging drafts ---
    print("\n[Phase 3a/3b] Running discrete-event routing simulation...")
    res = simulate_routing(config, master_tables, ticket_draft_df)
    if config.phase3b_enabled:
        status_df, assignment_df, outcome_df, cycle_outcome_df = res
        cycle_path = export_ticket_cycle_outcomes(
            cycle_outcome_df, staging_dir / "ticket_cycle_outcome_draft.csv"
        )
        print(f"  Exported {len(cycle_outcome_df)} ticket cycle outcomes -> {cycle_path}")
    else:
        status_df, assignment_df, outcome_df = res
        cycle_outcome_df = None

    status_path = export_ticket_status_events(
        status_df, staging_dir / "ticket_status_event_draft.csv"
    )
    asn_path = export_assignment_events(
        assignment_df, staging_dir / "assignment_event_draft.csv"
    )
    outcome_path = export_ticket_outcomes(
        outcome_df, staging_dir / "ticket_outcome_draft.csv"
    )
    print(f"  Exported {len(status_df)} status events -> {status_path}")
    print(f"  Exported {len(assignment_df)} assignment events -> {asn_path}")
    print(f"  Exported {len(outcome_df)} ticket outcomes -> {outcome_path}")

    # --- Phase 3c: Final relational tables, messages, SLA, Resolution, and export ---
    print("\n[Phase 3c] Building final relational tables...")
    final_tickets = build_final_tickets(ticket_draft_df, status_df)
    final_status_events = build_final_ticket_status_events(status_df)
    final_assignment_events = build_final_assignment_events(assignment_df)

    print("  Generating Messages with count calibration...")
    final_messages, natural_msg_cnt = build_messages(
        config, master_tables, ticket_draft_df, status_df, assignment_df
    )
    print(f"  Messages: Natural Total = {natural_msg_cnt}, Final Total = {len(final_messages)}")

    print("  Generating SLAEvents per cycle...")
    final_sla_events = build_sla_events(
        config, master_tables, ticket_draft_df, status_df, cycle_outcome_df, final_messages
    )

    print("  Generating Resolutions per resolved cycle...")
    final_resolutions = build_resolutions(
        config, master_tables, ticket_draft_df, status_df, assignment_df, cycle_outcome_df, final_sla_events
    )

    final_tables = {
        "Ticket": final_tickets,
        "TicketStatusEvent": final_status_events,
        "AssignmentEvent": final_assignment_events,
        "Message": final_messages,
        "SLAEvent": final_sla_events,
        "Resolution": final_resolutions,
    }

    # --- Baseline Internal Reconciliation ---
    print("\n[Baseline Reconciliation] Running generator/reconcile.py sanity checks on clean baseline...")
    rec_report = reconcile_all(config, master_tables, final_tables, post_anomalies=False)
    print("  All baseline reconciliation checks PASSED:")
    for k, v in rec_report.items():
        print(f"    - {k:35s}: {v}")

    # --- Phase 4: Anomaly Injection & Ground Truth ---
    if config.anomalies_enabled:
        print("\n[Phase 4] Injecting labelled anomalies into in-memory final tables...")
        tables_to_export, ground_truth_df = inject_anomalies(
            config, master_tables, final_tables, streams["anomalies"]
        )
        print(f"  Injected {len(ground_truth_df)} total anomaly records.")

        # Re-run reconciliation on anomalous tables
        print("\n[Post-Anomaly Reconciliation] Running sanity checks on anomalous tables...")
        rec_post = reconcile_all(
            config, master_tables, {**tables_to_export, "GroundTruth": ground_truth_df}, post_anomalies=True
        )
        print("  All post-anomaly reconciliation checks PASSED:")
        for k, v in rec_post.items():
            print(f"    - {k:35s}: {v}")
    else:
        print("\n[Phase 4] Anomaly injection disabled (--no-anomalies). Exporting clean baseline...")
        tables_to_export = final_tables
        ground_truth_df = pd.DataFrame(
            columns=[
                "gt_id",
                "issue_type",
                "entity_type",
                "entity_id",
                "ticket_id",
                "is_primary",
                "detection_rule",
                "expected_value",
                "observed_value",
                "description",
            ]
        )

    # Export final tables to data/raw
    exported_final = export_tables(tables_to_export, config.out_dir)
    print(f"\n  Exported {len(exported_final)} final tables to {config.out_dir}:")
    for t_name in sorted(exported_final.keys()):
        print(f"    - {t_name:20s}: {len(tables_to_export[t_name]):6d} rows -> {exported_final[t_name]}")

    # Export ground_truth.csv
    gt_path = ground_truth_dir / "ground_truth.csv"
    gt_path.parent.mkdir(parents=True, exist_ok=True)
    # Format boolean is_primary as true/false
    gt_export_df = ground_truth_df.copy()
    if len(gt_export_df) > 0 and "is_primary" in gt_export_df.columns:
        gt_export_df["is_primary"] = gt_export_df["is_primary"].map({True: "true", False: "false"})
    gt_export_df.to_csv(gt_path, index=False, lineterminator="\n", encoding="utf-8")
    print(f"  Exported GroundTruth ({len(ground_truth_df)} rows) -> {gt_path}")

    # Summary table row counts for ALL tables in data/raw
    all_raw_tables = {**master_tables, **tables_to_export}
    print("\n" + "=" * 76)
    print("                     FINAL DATASET SUMMARY (data/raw)                      ")
    print("=" * 76)
    for t_name in sorted(all_raw_tables.keys()):
        print(f"  {t_name:25s}: {len(all_raw_tables[t_name]):7d} rows")
    print("=" * 76)

    # Detailed KPI Report
    kpi_report = generate_kpi_report(
        ticket_draft_df, status_df, assignment_df, outcome_df, master_tables, all_windows, cycle_outcome_df
    )
    print(kpi_report)

    # Print Phase 3c Message, SLA, and CSAT Metrics
    print("\n" + "=" * 76)
    print("                         PHASE 3c METRICS REPORT                           ")
    print("=" * 76)

    # 1. Messages by channel
    msg_with_ch = final_messages.merge(final_tickets[["ticket_id", "channel_id"]], on="ticket_id")
    msg_counts_by_ch = msg_with_ch.groupby("channel_id").size()
    tkts_by_ch = final_tickets.groupby("channel_id").size()

    print("\n1. Message Metrics:")
    print(f"   - Target Messages     : {config.messages:7d}")
    print(f"   - Natural Messages    : {natural_msg_cnt:7d} ({((natural_msg_cnt - config.messages) / config.messages) * 100:+.2f}%)")
    print(f"   - Final Messages      : {len(final_messages):7d}")
    print(f"   - Mean per Ticket     : {len(final_messages) / len(final_tickets):.2f}")
    for ch in sorted(tkts_by_ch.keys()):
        m_cnt = msg_counts_by_ch.get(ch, 0)
        t_cnt = tkts_by_ch[ch]
        print(f"     * {ch:6s} Messages   : {m_cnt:6d} across {t_cnt:5d} tickets (Mean: {m_cnt / t_cnt:.2f})")

    # 2. SLA Breaches
    frt_breaches = final_sla_events["frt_breached"].sum()
    res_breaches = final_sla_events["resolution_breached"].sum()
    total_cycles = len(final_sla_events)

    print("\n2. SLA Breach Metrics:")
    print(f"   - Total Cycles        : {total_cycles:7d}")
    print(f"   - Overall FRT Breach  : {frt_breaches:6d} / {total_cycles} ({frt_breaches / total_cycles * 100:.2f}%)")
    print(f"   - Overall Res Breach  : {res_breaches:6d} / {total_cycles} ({res_breaches / total_cycles * 100:.2f}%)")

    sla_with_prio = final_sla_events.merge(final_tickets[["ticket_id", "priority_level"]], on="ticket_id")
    for prio in ["P1", "P2", "P3", "P4"]:
        sub = sla_with_prio[sla_with_prio["priority_level"] == prio]
        if len(sub) > 0:
            p_frt = sub["frt_breached"].sum()
            p_res = sub["resolution_breached"].sum()
            print(
                f"     * {prio} Cycles: {len(sub):5d} | "
                f"FRT Breach: {p_frt:4d} ({p_frt / len(sub) * 100:5.2f}%) | "
                f"Res Breach: {p_res:4d} ({p_res / len(sub) * 100:5.2f}%)"
            )

    # 3. CSAT Metrics
    res_with_ch = final_resolutions.merge(final_tickets[["ticket_id", "channel_id"]], on="ticket_id")
    total_res = len(final_resolutions)
    csat_responded_total = final_resolutions["csat_responded"].sum()
    valid_scores = final_resolutions.loc[final_resolutions["csat_responded"], "csat_score"].astype(int)

    print("\n3. CSAT Survey Metrics:")
    print(f"   - Total Resolutions   : {total_res:7d}")
    print(f"   - Overall Response    : {csat_responded_total:6d} / {total_res} ({csat_responded_total / total_res * 100:.2f}%)")
    print(f"   - Mean CSAT Score     : {valid_scores.mean():.2f} (Target: 3.6 - 4.1)")
    for ch in sorted(res_with_ch["channel_id"].unique()):
        sub_ch = res_with_ch[res_with_ch["channel_id"] == ch]
        resp_cnt = sub_ch["csat_responded"].sum()
        ch_scores = sub_ch.loc[sub_ch["csat_responded"], "csat_score"].astype(int)
        print(
            f"     * {ch:6s} Response : {resp_cnt:5d} / {len(sub_ch)} ({resp_cnt / len(sub_ch) * 100:5.2f}%) | "
            f"Mean Score: {ch_scores.mean():.2f}"
        )

    # 4. Phase 4 Anomaly Metrics Report
    print("\n" + "=" * 76)
    print("                         PHASE 4 ANOMALIES REPORT                          ")
    print("=" * 76)
    if config.anomalies_enabled and len(ground_truth_df) > 0:
        primary_counts = ground_truth_df[ground_truth_df["is_primary"]].groupby("issue_type").size()
        cascade_counts = ground_truth_df[~ground_truth_df["is_primary"]].groupby("issue_type").size()
        all_issues = sorted(set(ground_truth_df["issue_type"]))
        print(f"  Total Injected Records : {len(ground_truth_df):6d} (Primary: {ground_truth_df['is_primary'].sum()}, Cascade: {(~ground_truth_df['is_primary']).sum()})")
        print("  Breakdown by Issue Type:")
        for itype in all_issues:
            p_cnt = int(primary_counts.get(itype, 0))
            c_cnt = int(cascade_counts.get(itype, 0))
            print(f"    - {itype:30s}: Primary = {p_cnt:3d} | Cascade = {c_cnt:3d} | Total = {p_cnt + c_cnt:3d}")
    else:
        print("  Anomalies disabled (--no-anomalies). Clean baseline data generated.")
    print("=" * 76 + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
