"""Arrival model for generating support tickets across time, channels, and intents.

Implements:
- business_rules.md section 4.1: Baseline arrival rate and channel mix
- business_rules.md section 4.2: Queue spikes
- business_rules.md section 4.3: Critical incidents
"""

from __future__ import annotations

import datetime
from typing import Mapping, Sequence
import numpy as np
import pandas as pd

from generator.config import Config
from generator import ids
from generator.sla_calendar import is_open
from generator.windows import ScenarioWindow


def _get_hour_weight(hour: int) -> float:
    """Return hour_weight based on business_rules.md 4.1.

    Peaks 10:00-14:00 (1.0), shoulders 07:00-10:00 and 14:00-19:00 (0.7),
    19:00-23:00 (0.35), 23:00-07:00 (0.15).
    """
    if 10 <= hour < 14:
        return 1.0
    if 7 <= hour < 10 or 14 <= hour < 19:
        return 0.7
    if 19 <= hour < 23:
        return 0.35
    return 0.15


def _get_dow_weight(dow: int) -> float:
    """Return dow_weight based on business_rules.md 4.1.

    Mon-Fri 1.0, Sat 0.55, Sun 0.45 (1=Mon..7=Sun).
    """
    if dow <= 5:
        return 1.0
    if dow == 6:
        return 0.55
    return 0.45


def generate_arrivals(
    config: Config,
    customer_df: pd.DataFrame,
    intent_df: pd.DataFrame,
    incidents: Sequence[ScenarioWindow],
    spikes: Sequence[ScenarioWindow],
    rng: np.random.Generator,
) -> list[dict[str, object]]:
    """Generate arrivals according to business_rules.md sections 4.1-4.3.

    Args:
        config: Generator configuration.
        customer_df: Customer table DataFrame.
        intent_df: Intent table DataFrame.
        incidents: List of critical incident windows.
        spikes: List of queue spike windows.
        rng: The 'arrivals' child RNG stream.

    Returns:
        List of raw arrival dictionaries sorted by (created_at, customer_id)
        with gapless ticket numbers 1..N and ticket IDs.
    """
    tickets_target = config.tickets
    window_start_dt = datetime.datetime.fromisoformat(config.window_start.replace("Z", "+00:00"))
    total_days = config.days
    total_hours = total_days * 24

    # 1. Total incident tickets vs baseline tickets
    total_incident_tickets = sum(inc.ticket_count for inc in incidents)
    n_baseline = tickets_target - total_incident_tickets

    # 2. Assign each baseline ticket to a customer by weighted draw with replacement (4.1)
    # Weights: CONSUMER 1, SMB 2, ENTERPRISE 3
    propensity_map = config.customer_ticket_propensity
    cust_weights_arr = np.array([propensity_map[s] for s in customer_df["segment"]], dtype=float)
    cust_weights_arr /= cust_weights_arr.sum()

    baseline_cust_indices = rng.choice(len(customer_df), size=n_baseline, p=cust_weights_arr)
    chosen_customers = customer_df.iloc[baseline_cust_indices].to_dict(orient="records")

    # 3. Channel draw by segment mix (4.1)
    # CONSUMER & SMB: EMAIL 50%, CHAT 40%, PHONE 10%
    # ENTERPRISE: EMAIL 40%, CHAT 30%, PHONE 30%
    tickets_by_channel: dict[str, list[dict[str, object]]] = {"EMAIL": [], "CHAT": [], "PHONE": []}
    channel_mix = config.channel_mix_by_segment

    for cust in chosen_customers:
        seg = str(cust["segment"])
        mix_dict = channel_mix[seg]
        channels = list(mix_dict.keys())
        p_channels = [mix_dict[ch] for ch in channels]
        chosen_channel = str(rng.choice(channels, p=p_channels))
        tickets_by_channel[chosen_channel].append(cust)

    # 4. Hourly bin weights over whole window for each channel (4.1)
    # Channel calendars
    channel_cals = {"EMAIL": "CAL-EMAIL", "CHAT": "CAL-CHAT", "PHONE": "CAL-PHONE"}
    after_hours_mask = config.channel_after_hours_mask

    # Precalculate start times of all 2160 hourly bins
    bin_starts = [
        window_start_dt + datetime.timedelta(hours=h) for h in range(total_hours)
    ]
    bin_dows = [b.isoweekday() for b in bin_starts]
    bin_hours = [b.hour for b in bin_starts]
    bin_dow_weights = np.array([_get_dow_weight(dow) for dow in bin_dows], dtype=float)
    bin_hour_weights = np.array([_get_hour_weight(h) for h in bin_hours], dtype=float)

    # Check which bins overlap spike windows
    bin_spike_multipliers = np.ones(total_hours, dtype=float)
    bin_spike_windows: list[ScenarioWindow | None] = [None] * total_hours

    for h_idx, b_start in enumerate(bin_starts):
        b_end = b_start + datetime.timedelta(hours=1)
        for spk in spikes:
            if spk.overlaps(b_start, b_end):
                bin_spike_multipliers[h_idx] = config.spike_arrival_multiplier  # 4.0
                bin_spike_windows[h_idx] = spk
                break

    # Prepare intent weighting lookups
    intents_meta = intent_df.to_dict(orient="records")
    all_intent_ids = [str(r["intent_id"]) for r in intents_meta]
    base_intent_weights = {str(r["intent_id"]): float(r["volume_weight_pct"]) for r in intents_meta}
    allows_prospect_intents = [
        str(r["intent_id"]) for r in intents_meta if bool(r["allows_prospect"])
    ]

    def _sample_intent(
        cust_type: str, spike_win: ScenarioWindow | None
    ) -> str:
        """Sample intent respecting prospect limits and spike skews."""
        w_dict = dict(base_intent_weights)

        # Apply spike skew if inside a spike window (4.2)
        if spike_win is not None:
            if spike_win.skew == "BILLING":
                # INT-001 and INT-003 weights x3
                w_dict["INT-001"] *= 3.0
                w_dict["INT-003"] *= 3.0
            elif spike_win.skew == "TECH":
                # INT-004 weight x3
                w_dict["INT-004"] *= 3.0

        if cust_type == "PROSPECT":
            # Restricted to allows_prospect intents (INT-010, INT-011) and renormalised
            candidates = allows_prospect_intents
            weights = np.array([w_dict[i_id] for i_id in candidates], dtype=float)
        else:
            candidates = all_intent_ids
            weights = np.array([w_dict[i_id] for i_id in candidates], dtype=float)

        weights /= weights.sum()
        return str(rng.choice(candidates, p=weights))

    raw_tickets: list[dict[str, object]] = []

    # Allocate baseline tickets per channel across bins with multinomial draw
    for ch_name in ("EMAIL", "CHAT", "PHONE"):
        ch_custs = tickets_by_channel[ch_name]
        n_ch = len(ch_custs)
        if n_ch == 0:
            continue

        cal_code = channel_cals[ch_name]
        ah_factor = after_hours_mask[ch_name]

        # Calculate channel mask for all bins
        ch_masks = np.array([
            1.0 if is_open(cal_code, b_start) else ah_factor
            for b_start in bin_starts
        ], dtype=float)

        raw_bin_weights = bin_dow_weights * bin_hour_weights * ch_masks * bin_spike_multipliers
        p_bins = raw_bin_weights / raw_bin_weights.sum()

        # ONE multinomial draw so totals are exact (3. generator/arrivals.py)
        bin_counts = rng.multinomial(n_ch, p_bins)

        # Assign tickets into bins
        cust_ptr = 0
        for b_idx, count in enumerate(bin_counts):
            if count == 0:
                continue
            b_start = bin_starts[b_idx]
            spk_win = bin_spike_windows[b_idx]

            # Place each ticket uniformly at whole-second resolution inside its bin
            sec_offsets = rng.integers(0, 3600, size=count)
            for offset in sec_offsets:
                cust = ch_custs[cust_ptr]
                cust_ptr += 1

                t_created = b_start + datetime.timedelta(seconds=int(offset))
                intent_id = _sample_intent(str(cust["customer_type"]), spk_win)

                raw_tickets.append({
                    "customer": cust,
                    "customer_id": cust["customer_id"],
                    "channel_id": ch_name,
                    "intent_id": intent_id,
                    "created_at": t_created,
                    "is_critical_incident": False,
                    "incident_id": None,
                })

    # 5. Incident tickets (4.3):
    # CUSTOMER-type customers only, intent INT-005, channel CHAT 50 / PHONE 35 / EMAIL 15,
    # timestamps uniform inside their incident window, incident_id set.
    customer_only_df = customer_df[customer_df["customer_type"] == "CUSTOMER"]
    inc_cust_w = np.array([propensity_map[s] for s in customer_only_df["segment"]], dtype=float)
    inc_cust_w /= inc_cust_w.sum()

    for inc in incidents:
        if inc.ticket_count <= 0:
            continue
        inc_cust_indices = rng.choice(len(customer_only_df), size=inc.ticket_count, p=inc_cust_w)
        inc_custs = customer_only_df.iloc[inc_cust_indices].to_dict(orient="records")

        # Channel mix: CHAT 50%, PHONE 35%, EMAIL 15%
        inc_channels = rng.choice(
            ["CHAT", "PHONE", "EMAIL"], size=inc.ticket_count, p=[0.50, 0.35, 0.15]
        )

        dur_sec = int((inc.end - inc.start).total_seconds())
        time_offsets = rng.integers(0, dur_sec, size=inc.ticket_count)

        for cust, ch, offset in zip(inc_custs, inc_channels, time_offsets):
            t_created = inc.start + datetime.timedelta(seconds=int(offset))
            raw_tickets.append({
                "customer": cust,
                "customer_id": cust["customer_id"],
                "channel_id": ch,
                "intent_id": "INT-005",
                "created_at": t_created,
                "is_critical_incident": True,
                "incident_id": inc.id,
            })

    # Total check: must equal tickets_target exactly
    assert len(raw_tickets) == tickets_target, (
        f"Total tickets {len(raw_tickets)} != target {tickets_target}"
    )

    # 6. Ticket numbers are assigned after sorting by (created_at, customer_id), gapless from 1
    raw_tickets.sort(key=lambda t: (t["created_at"], str(t["customer_id"])))

    for i, t in enumerate(raw_tickets, start=1):
        t["ticket_number"] = i
        t["ticket_id"] = ids.ticket_id(i)

    return raw_tickets
