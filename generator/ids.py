"""Identifier builders and validators for all entities.

Implements data_dictionary.md section 0 (ID formats and conventions).
Pure functions, compiled regex patterns, and validation helper.
"""

from __future__ import annotations

import re
from typing import Final

# Regex constants matching data_dictionary.md section 0
CUSTOMER_ID_REGEX: Final[str] = r"^CUST-\d{6}$"
TICKET_ID_REGEX: Final[str] = r"^TKT-2026-\d{6}$"
MESSAGE_ID_REGEX: Final[str] = r"^MSG-TKT-2026-\d{6}-\d{3}$"
AGENT_ID_REGEX: Final[str] = r"^AGT-\d{6}$"
ASSIGNMENT_ID_REGEX: Final[str] = r"^ASN-TKT-2026-\d{6}-\d{3}$"
SLA_EVENT_ID_REGEX: Final[str] = r"^SLA-TKT-2026-\d{6}(-R[1-2])?$"
STATUS_EVENT_ID_REGEX: Final[str] = r"^TSE-TKT-2026-\d{6}-\d{3}$"
RESOLUTION_ID_REGEX: Final[str] = r"^RES-TKT-2026-\d{6}-\d{2}$"
INCIDENT_ID_REGEX: Final[str] = r"^INC-\d{3}$"
INTENT_ID_REGEX: Final[str] = r"^INT-\d{3}$"
PRIORITY_RULE_ID_REGEX: Final[str] = r"^PRL-\d{3}$"
SKILL_ID_REGEX: Final[str] = r"^SKL-[A-Z]+$"
QUEUE_ID_REGEX: Final[str] = r"^QUE-[A-Z]+$"
CALENDAR_CODE_REGEX: Final[str] = r"^CAL-[A-Z0-9]+$"
SHIFT_ID_REGEX: Final[str] = r"^SHF-\d{6}$"
WINDOW_ID_REGEX: Final[str] = r"^CWN-\d{3}$"
GROUND_TRUTH_ID_REGEX: Final[str] = r"^GT-\d{6}$"

_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "customer": re.compile(CUSTOMER_ID_REGEX),
    "ticket": re.compile(TICKET_ID_REGEX),
    "message": re.compile(MESSAGE_ID_REGEX),
    "agent": re.compile(AGENT_ID_REGEX),
    "assignment": re.compile(ASSIGNMENT_ID_REGEX),
    "sla_event": re.compile(SLA_EVENT_ID_REGEX),
    "status_event": re.compile(STATUS_EVENT_ID_REGEX),
    "resolution": re.compile(RESOLUTION_ID_REGEX),
    "incident": re.compile(INCIDENT_ID_REGEX),
    "intent": re.compile(INTENT_ID_REGEX),
    "priority_rule": re.compile(PRIORITY_RULE_ID_REGEX),
    "skill": re.compile(SKILL_ID_REGEX),
    "queue": re.compile(QUEUE_ID_REGEX),
    "calendar": re.compile(CALENDAR_CODE_REGEX),
    "shift": re.compile(SHIFT_ID_REGEX),
    "window": re.compile(WINDOW_ID_REGEX),
    "ground_truth": re.compile(GROUND_TRUTH_ID_REGEX),
}


def customer_id(n: int) -> str:
    """Build customer ID: CUST-\\d{6} (data_dictionary.md 0)."""
    return f"CUST-{n:06d}"


def ticket_id(n: int, year: int = 2026) -> str:
    """Build ticket ID: TKT-2026-\\d{6} (data_dictionary.md 0)."""
    return f"TKT-{year}-{n:06d}"


def message_id(ticket: str, seq: int) -> str:
    """Build message ID: MSG-<ticket-id>-\\d{3} (data_dictionary.md 0)."""
    return f"MSG-{ticket}-{seq:03d}"


def agent_id(n: int) -> str:
    """Build agent ID: AGT-\\d{6} (data_dictionary.md 0)."""
    return f"AGT-{n:06d}"


def assignment_id(ticket: str, seq: int) -> str:
    """Build assignment event ID: ASN-<ticket-id>-\\d{3} (data_dictionary.md 0)."""
    return f"ASN-{ticket}-{seq:03d}"


def sla_event_id(ticket: str, cycle: int = 1) -> str:
    """Build SLA event ID: SLA-<ticket-id> optionally with -R1 or -R2 (data_dictionary.md 0, 19)."""
    if cycle == 1:
        return f"SLA-{ticket}"
    if cycle in (2, 3):
        return f"SLA-{ticket}-R{cycle - 1}"
    raise ValueError(f"Cycle must be 1, 2, or 3, got: {cycle}")


def status_event_id(ticket: str, seq: int) -> str:
    """Build ticket status event ID: TSE-<ticket-id>-\\d{3} (data_dictionary.md 0)."""
    return f"TSE-{ticket}-{seq:03d}"


def resolution_id(ticket: str, cycle: int = 1) -> str:
    """Build resolution ID: RES-<ticket-id>-\\d{2} where suffix is cycle_no (data_dictionary.md 0, 20)."""
    return f"RES-{ticket}-{cycle:02d}"


def incident_id(n: int) -> str:
    """Build incident ID: INC-\\d{3} (data_dictionary.md 0)."""
    return f"INC-{n:03d}"


def intent_id(n: int) -> str:
    """Build intent ID: INT-\\d{3} (data_dictionary.md 0)."""
    return f"INT-{n:03d}"


def priority_rule_id(n: int) -> str:
    """Build priority rule ID: PRL-\\d{3} (data_dictionary.md 0)."""
    return f"PRL-{n:03d}"


def skill_id(name: str) -> str:
    """Build skill ID: SKL-[A-Z]+ (data_dictionary.md 0)."""
    clean = name.upper()
    if clean.startswith("SKL-"):
        return clean
    return f"SKL-{clean}"


def queue_id(name: str) -> str:
    """Build queue ID: QUE-[A-Z]+ (data_dictionary.md 0)."""
    clean = name.upper()
    if clean.startswith("QUE-"):
        return clean
    return f"QUE-{clean}"


def calendar_code(name: str) -> str:
    """Build calendar code: CAL-[A-Z0-9]+ (data_dictionary.md 0)."""
    clean = name.upper()
    if clean.startswith("CAL-"):
        return clean
    return f"CAL-{clean}"


def shift_id(n: int) -> str:
    """Build agent shift ID: SHF-\\d{6} (data_dictionary.md 0)."""
    return f"SHF-{n:06d}"


def window_id(n: int) -> str:
    """Build calendar window ID: CWN-\\d{3} (data_dictionary.md 0)."""
    return f"CWN-{n:03d}"


def ground_truth_id(n: int) -> str:
    """Build ground truth ID: GT-\\d{6} (data_dictionary.md 0)."""
    return f"GT-{n:06d}"


def validate_id(kind: str, value: str) -> bool:
    """Validate that value matches the ID pattern for kind (data_dictionary.md 0).

    Args:
        kind: Entity name (e.g. 'ticket', 'customer', 'agent', 'sla_event', etc.).
        value: ID string to validate.

    Returns:
        True if value matches the pattern, False otherwise.

    Raises:
        ValueError if kind is unrecognized.
    """
    key = kind.lower().strip().replace(" ", "_").replace("-", "_")
    pattern = _PATTERNS.get(key)
    if pattern is None:
        raise ValueError(
            f"Unknown ID kind '{kind}'. Allowed kinds: {sorted(_PATTERNS.keys())}"
        )
    return bool(pattern.match(value))
