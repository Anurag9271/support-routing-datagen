"""Scenario windows generation for critical incidents and queue arrival spikes.

Implements:
- business_rules.md section 4.2: Queue spikes
- business_rules.md section 4.3: Critical incidents
- data_dictionary.md section 21: GroundTruth scenario windows schema
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence
import numpy as np
import pandas as pd

from generator.config import Config
from generator import ids


@dataclass(frozen=True)
class ScenarioWindow:
    """Represents a time window for an incident or queue spike (business_rules.md 4.2, 4.3)."""

    kind: str  # 'incident' or 'spike'
    id: str  # 'INC-001'... or 'SPK-001'...
    start: datetime.datetime
    end: datetime.datetime
    skew: str  # 'OUTAGE' for incident; 'BILLING' or 'TECH' for spike
    ticket_count: int = 0  # Number of tickets allocated (for incidents)

    @property
    def start_iso(self) -> str:
        """Start timestamp in UTC ISO-8601 format."""
        return self.start.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def end_iso(self) -> str:
        """End timestamp in UTC ISO-8601 format."""
        return self.end.strftime("%Y-%m-%dT%H:%M:%SZ")

    def contains(self, dt: datetime.datetime) -> bool:
        """Return True if dt falls within [start, end)."""
        return self.start <= dt < self.end

    def overlaps(self, other_start: datetime.datetime, other_end: datetime.datetime) -> bool:
        """Return True if this window overlaps with [other_start, other_end]."""
        return self.start < other_end and other_start < self.end


def _split_incident_tickets(
    total_tickets: int, n_incidents: int, rng: np.random.Generator
) -> list[int]:
    """Split total incident tickets across incidents with +/-30% jitter summing exactly."""
    if n_incidents <= 0 or total_tickets <= 0:
        return [0] * n_incidents
    if n_incidents == 1:
        return [total_tickets]

    # Draw jitter in [-0.30, +0.30] for each incident
    jitters = [1.0 + float(rng.uniform(-0.30, 0.30)) for _ in range(n_incidents)]
    sum_j = sum(jitters)
    shares = [j / sum_j for j in jitters]

    counts = [int(round(s * total_tickets)) for s in shares]
    diff = total_tickets - sum(counts)

    # Adjust residual so sum is exact
    idx = 0
    while diff != 0:
        step = 1 if diff > 0 else -1
        counts[idx % n_incidents] += step
        diff -= step
        idx += 1

    return counts


def build_scenario_windows(
    config: Config, rng: np.random.Generator
) -> tuple[list[ScenarioWindow], list[ScenarioWindow]]:
    """Build scenario windows from the 'arrivals' RNG stream (business_rules.md 4.2-4.3).

    Args:
        config: Generator configuration.
        rng: The 'arrivals' child RNG stream.

    Returns:
        tuple of (incidents, spikes).
    """
    tickets = config.tickets
    window_start_dt = datetime.datetime.fromisoformat(config.window_start.replace("Z", "+00:00"))
    total_days = config.days

    n_incidents = min(config.incident_window_count, max(1, tickets // 2000))
    n_spikes = min(config.spike_window_count, max(1, tickets // 2000))

    # 1. Critical incidents (business_rules.md 4.3):
    # Different days, duration 90-240 min, fully inside the arrival window, ids INC-001...
    incident_days = rng.choice(total_days, size=n_incidents, replace=False)
    incident_days = sorted(int(d) for d in incident_days)

    total_incident_tickets = round(0.04 * tickets)
    incident_ticket_counts = _split_incident_tickets(total_incident_tickets, n_incidents, rng)

    incidents: list[ScenarioWindow] = []
    for i, (day_idx, count) in enumerate(zip(incident_days, incident_ticket_counts)):
        dur_min = int(rng.integers(
            config.incident_duration_minutes_min, config.incident_duration_minutes_max + 1
        ))
        dur_sec = dur_min * 60

        # Start within the day so it does not spill past midnight
        max_start_sec = 86400 - dur_sec
        start_sec = int(rng.integers(0, max_start_sec + 1))

        start_dt = window_start_dt + datetime.timedelta(days=day_idx, seconds=start_sec)
        end_dt = start_dt + datetime.timedelta(seconds=dur_sec)

        incidents.append(
            ScenarioWindow(
                kind="incident",
                id=ids.incident_id(i + 1),
                start=start_dt,
                end=end_dt,
                skew="OUTAGE",
                ticket_count=count,
            )
        )

    # 2. Queue spikes (business_rules.md 4.2):
    # Weekdays only, 2-3 h (120-180 min), start hour 08-16 UTC,
    # non-overlapping each other and incidents.
    # First 3 are BILLING-skewed, remaining are TECH-skewed.
    weekday_indices = [
        d for d in range(total_days)
        if (window_start_dt + datetime.timedelta(days=d)).isoweekday() <= 5
    ]

    spikes: list[ScenarioWindow] = []
    max_attempts = 1000

    for i in range(n_spikes):
        skew = "BILLING" if i < 3 else "TECH"
        spk_id = f"SPK-{i + 1:03d}"

        placed = False
        attempts = 0
        while not placed and attempts < max_attempts:
            attempts += 1
            day_idx = int(rng.choice(weekday_indices))
            dur_min = int(rng.integers(
                config.spike_duration_hours_min * 60, config.spike_duration_hours_max * 60 + 1
            ))
            dur_sec = dur_min * 60

            # Start hour 08..16 UTC
            start_hour = int(rng.integers(8, 17))
            start_min = int(rng.integers(0, 60))
            day_start_sec = start_hour * 3600 + start_min * 60

            cand_start = window_start_dt + datetime.timedelta(days=day_idx, seconds=day_start_sec)
            cand_end = cand_start + datetime.timedelta(seconds=dur_sec)

            # Check overlap with all incidents and existing spikes
            overlap = False
            for inc in incidents:
                if inc.overlaps(cand_start, cand_end):
                    overlap = True
                    break
            if not overlap:
                for spk in spikes:
                    if spk.overlaps(cand_start, cand_end):
                        overlap = True
                        break

            if not overlap:
                spikes.append(
                    ScenarioWindow(
                        kind="spike",
                        id=spk_id,
                        start=cand_start,
                        end=cand_end,
                        skew=skew,
                        ticket_count=0,
                    )
                )
                placed = True

        if not placed:
            raise RuntimeError(f"Could not place non-overlapping spike window {spk_id} after {max_attempts} attempts.")

    # Sort windows deterministically by start time
    incidents = sorted(incidents, key=lambda w: (w.start, w.id))
    spikes = sorted(spikes, key=lambda w: (w.start, w.id))

    return incidents, spikes


def export_scenario_windows(
    windows: Sequence[ScenarioWindow], out_path: str | Path
) -> Path:
    """Export scenario windows to CSV (data_dictionary.md 21).

    Columns: kind, id, start, end, skew.
    """
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = [
        {
            "kind": w.kind,
            "id": w.id,
            "start": w.start_iso,
            "end": w.end_iso,
            "skew": w.skew,
        }
        for w in sorted(windows, key=lambda x: (x.start, x.id))
    ]

    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n", na_rep="")
    return path
