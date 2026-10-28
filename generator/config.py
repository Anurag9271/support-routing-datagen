"""Configuration for synthetic support routing data generator.

Implements business_rules.md sections 1-4.
Holds all parameters, distributions, and default constants in a frozen dataclass.
No magic numbers elsewhere in the generator code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Config:
    """Master configuration holding every default from business_rules.md sections 1-4.

    Citations:
        - business_rules.md section 1: Scope and conventions
        - business_rules.md section 2: Reference data
        - business_rules.md section 3: Population generation
        - business_rules.md section 4: Arrival model
    """

    # --- Section 1: Scope and conventions ---
    seed: int = 20261022
    window_start: str = "2026-01-05T00:00:00Z"
    window_end: str = "2026-04-05T00:00:00Z"
    cutoff: str = "2026-04-05T00:00:00Z"
    timezone: str = "UTC"
    days: int = 90
    phase3b_enabled: bool = True
    anomalies_enabled: bool = True

    # Scale parameters
    tickets: int = 30000
    agents: int = 100
    messages: int = 100000
    customers: int = 12000
    out_dir: str = "data/raw"

    # --- Section 2: Reference data ---
    # Languages (2.1)
    languages: tuple[str, ...] = ("en", "es", "hi", "ar", "fr")
    language_names: tuple[str, ...] = ("English", "Spanish", "Hindi", "Arabic", "French")
    customer_language_weights: tuple[float, ...] = (0.60, 0.15, 0.12, 0.06, 0.07)  # en, es, hi, ar, fr
    country_weights_by_language: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: {
            "en": {"US": 40.0, "GB": 20.0, "IN": 20.0, "CA": 10.0, "AU": 10.0},
            "es": {"ES": 50.0, "MX": 50.0},
            "hi": {"IN": 100.0},
            "fr": {"FR": 70.0, "CA": 30.0},
            "ar": {"AE": 50.0, "SA": 50.0},
        }
    )

    # Channels and calendars (2.2)
    calendar_effective_from: str = "2026-01-05"
    channel_ids: tuple[str, ...] = ("EMAIL", "CHAT", "PHONE")

    # Skills (2.3)
    skills: tuple[str, ...] = (
        "SKL-BILLING",
        "SKL-TECH",
        "SKL-ACCOUNT",
        "SKL-SECURITY",
        "SKL-SALES",
        "SKL-GENERAL",
    )
    core_skills: tuple[str, ...] = (
        "SKL-BILLING",
        "SKL-TECH",
        "SKL-ACCOUNT",
        "SKL-GENERAL",
    )

    # Queues (2.4)
    skill_queues: tuple[str, ...] = (
        "QUE-BILLING",
        "QUE-TECH",
        "QUE-ACCOUNT",
        "QUE-SALES",
        "QUE-GENERAL",
    )
    special_queues: tuple[str, ...] = ("QUE-INCIDENT", "QUE-ESCALATION")

    # PriorityRule rows (2.5)
    p2_target_v2_effective_date: str = "2026-02-16"

    # --- Section 3: Population generation ---
    # Agents (3.1)
    tier_proportions: Mapping[str, float] = field(
        default_factory=lambda: {"T1": 0.70, "T2": 0.22, "T3": 0.08}
    )
    concurrency_limits: Mapping[str, int] = field(
        default_factory=lambda: {"T1": 4, "T2": 5, "T3": 3}
    )
    new_hire_proportion: float = 0.15
    new_hire_concurrency_reduction: int = 1
    concurrency_limit_floor: int = 2
    new_hire_start_date: str = "2025-11-06"
    new_hire_end_date: str = "2026-01-04"
    tenured_hire_start_date: str = "2019-01-01"
    tenured_hire_end_date: str = "2025-11-05"
    inactive_agents_count: int = 3

    # Skills held (3.2)
    t1_skill_weights: Mapping[str, float] = field(
        default_factory=lambda: {
            "SKL-BILLING": 25.0,
            "SKL-TECH": 30.0,
            "SKL-ACCOUNT": 20.0,
            "SKL-GENERAL": 15.0,
            "SKL-SALES": 10.0,
        }
    )
    t1_second_skill_prob: float = 0.30
    t1_proficiency_weights: Mapping[str, float] = field(
        default_factory=lambda: {"BASIC": 0.40, "PROFICIENT": 0.60}
    )
    t2_skill_counts: tuple[int, ...] = (2, 3)
    t2_tech_prob: float = 0.70
    t2_security_prob: float = 0.40
    t2_proficiency_weights: Mapping[str, float] = field(
        default_factory=lambda: {"PROFICIENT": 0.50, "EXPERT": 0.50}
    )
    t3_skill_counts: tuple[int, ...] = (3, 4)
    t3_proficiency: str = "EXPERT"
    skill_acquisitions_count: int = 8

    # Agent languages (3.3)
    agent_en_fluency_weights: Mapping[str, float] = field(
        default_factory=lambda: {"NATIVE": 0.40, "FLUENT": 0.60}
    )
    additional_language_proportions: Mapping[str, float] = field(
        default_factory=lambda: {"es": 0.24, "hi": 0.20, "fr": 0.12, "ar": 0.10}
    )
    additional_language_fluency_weights: Mapping[str, float] = field(
        default_factory=lambda: {"NATIVE": 0.50, "FLUENT": 0.50}
    )

    # Shifts (3.4)
    shift_definitions: Mapping[str, tuple[str, str, float]] = field(
        default_factory=lambda: {
            # shift_code: (start_time, end_time, agent_share)
            "EARLY": ("06:00", "14:00", 0.25),
            "DAY": ("10:00", "18:00", 0.30),
            "LATE": ("14:00", "22:00", 0.30),
            "NIGHT": ("22:00", "06:00", 0.15),
        }
    )
    work_day_patterns: Mapping[str, tuple[tuple[int, ...], float]] = field(
        default_factory=lambda: {
            # pattern_code: (days_of_week 1=Mon, share)
            "A": ((1, 2, 3, 4, 5), 0.70),
            "B": ((7, 1, 2, 3, 4), 0.15),
            "C": ((2, 3, 4, 5, 6), 0.15),
        }
    )
    night_tier_counts: Mapping[str, int] = field(
        default_factory=lambda: {"T1": 8, "T2": 5, "T3": 2}
    )
    shift_effective_from: str = "2026-01-05"

    # Customers (3.6)
    customer_type_weights: Mapping[str, float] = field(
        default_factory=lambda: {"CUSTOMER": 0.90, "PROSPECT": 0.10}
    )
    customer_segment_weights: Mapping[str, float] = field(
        default_factory=lambda: {"CONSUMER": 0.70, "SMB": 0.22, "ENTERPRISE": 0.08}
    )
    customer_ticket_propensity: Mapping[str, int] = field(
        default_factory=lambda: {"CONSUMER": 1, "SMB": 2, "ENTERPRISE": 3}
    )
    customer_created_at_start: str = "2024-01-01T00:00:00Z"
    customer_email_domain: str = "example.test"
    customer_phone_prefix: str = "+999"

    # --- Section 4: Arrival model ---
    dow_arrival_weights: Mapping[int, float] = field(
        default_factory=lambda: {
            1: 1.0,
            2: 1.0,
            3: 1.0,
            4: 1.0,
            5: 1.0,
            6: 0.55,
            7: 0.45,
        }
    )
    hour_arrival_weights: Mapping[str, tuple[tuple[int, ...], float]] = field(
        default_factory=lambda: {
            "peak": ((10, 11, 12, 13), 1.0),
            "shoulder": ((7, 8, 9, 14, 15, 16, 17, 18), 0.7),
            "evening": ((19, 20, 21, 22), 0.35),
            "night": ((23, 0, 1, 2, 3, 4, 5, 6), 0.15),
        }
    )
    channel_after_hours_mask: Mapping[str, float] = field(
        default_factory=lambda: {"EMAIL": 0.35, "CHAT": 0.25, "PHONE": 0.10}
    )
    channel_mix_by_segment: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: {
            "CONSUMER": {"EMAIL": 0.50, "CHAT": 0.40, "PHONE": 0.10},
            "SMB": {"EMAIL": 0.50, "CHAT": 0.40, "PHONE": 0.10},
            "ENTERPRISE": {"EMAIL": 0.40, "CHAT": 0.30, "PHONE": 0.30},
        }
    )

    # Queue spikes (4.2)
    spike_window_count: int = 5
    spike_duration_hours_min: int = 2
    spike_duration_hours_max: int = 3
    spike_arrival_multiplier: float = 4.0

    # Critical incidents (4.3)
    incident_window_count: int = 6
    incident_duration_minutes_min: int = 90
    incident_duration_minutes_max: int = 240
    incident_tickets_min: int = 100
    incident_tickets_max: int = 250
    incident_median_handle_minutes: int = 15
