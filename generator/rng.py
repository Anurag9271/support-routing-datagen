"""Reproducible random number generation streams.

Implements business_rules.md section 15.
Spawns one SeedSequence into named child streams:
customers, agents, arrivals, intents, handle_times, pending,
transfers, escalations, reopens, messages, csat, anomalies.
Provides ticket_rng(seed, ticket_number, module) for per-ticket streams.
No global random state.
"""

from __future__ import annotations

import zlib
from typing import Final, Mapping
import numpy as np

# Named child streams in business_rules.md section 15
STREAM_NAMES: Final[tuple[str, ...]] = (
    "customers",
    "agents",
    "arrivals",
    "intents",
    "handle_times",
    "pending",
    "transfers",
    "escalations",
    "reopens",
    "messages",
    "csat",
    "anomalies",
)


def init_rng_streams(seed: int) -> dict[str, np.random.Generator]:
    """Spawn a SeedSequence into the 12 named child streams (business_rules.md 15).

    Args:
        seed: Master seed integer (default 20261022).

    Returns:
        Mapping from stream name to independent np.random.Generator.
    """
    master_seq = np.random.SeedSequence(seed)
    child_seqs = master_seq.spawn(len(STREAM_NAMES))
    return {
        name: np.random.default_rng(child_seq)
        for name, child_seq in zip(STREAM_NAMES, child_seqs)
    }


def ticket_rng(seed: int, ticket_number: int, module: str | int) -> np.random.Generator:
    """Generate an independent per-ticket child stream (business_rules.md 15).

    Per-ticket randomness (handle time, delays, CSAT noise) is keyed by
    ticket number and module identifier so it does not depend on simulation order.

    Args:
        seed: Master seed integer.
        ticket_number: Gapless ticket sequence number (1..N).
        module: Name or integer ID of the calling module (e.g. 'handle_times', 'delays').

    Returns:
        Deterministic, independent np.random.Generator for this ticket and module.
    """
    if isinstance(module, str):
        mod_id = zlib.crc32(module.encode("utf-8"))
    else:
        mod_id = int(module)

    seq = np.random.SeedSequence(entropy=seed, spawn_key=(ticket_number, mod_id))
    return np.random.default_rng(seq)
