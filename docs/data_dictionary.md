# Data Dictionary

Dataset: omnichannel support-ticket routing with SLA tracking (synthetic).
Companion document: `business_rules.md` (generation logic, routing, SLA, anomalies, validation catalog).

## 0. Conventions

| Topic | Convention |
|---|---|
| Types | `string`, `int`, `decimal`, `bool`, `date` (YYYY-MM-DD), `time` (HH:MM), `timestamp` (UTC, ISO-8601 `YYYY-MM-DDTHH:MM:SSZ`, whole seconds) |
| Time zone | Everything is UTC. No DST. Calendars and shifts are expressed in UTC. |
| Nulls | CSV: empty string. JSON: `null`. SQL: `NULL`. |
| Booleans | CSV/JSON: `true` / `false`. SQL: `1` / `0`. |
| Units | Durations are minutes unless the field name says otherwise. "Business minutes" are minutes inside the applicable SLA calendar. |
| Immutability | IDs never change. State changes are new event rows, never edits. |
| Req | `Y` required, `N` optional (nullable), `C` conditionally required (rule stated). |
| Unique | `PK` primary key, `UQ` unique on its own, `UQ(a,b)` unique as a combination, `-` not unique. |

### ID formats

| Entity | Pattern | Example |
|---|---|---|
| Customer | `^CUST-\d{6}$` | CUST-000042 |
| Ticket | `^TKT-2026-\d{6}$` | TKT-2026-000001 |
| Message | `^MSG-<ticket-id>-\d{3}$` | MSG-TKT-2026-000001-001 |
| Agent | `^AGT-\d{6}$` | AGT-000007 |
| AssignmentEvent | `^ASN-<ticket-id>-\d{3}$` | ASN-TKT-2026-000001-001 |
| SLAEvent | `^SLA-<ticket-id>(-R[1-2])?$` | SLA-TKT-2026-000001, SLA-TKT-2026-000001-R1 |
| TicketStatusEvent | `^TSE-<ticket-id>-\d{3}$` | TSE-TKT-2026-000001-004 |
| Resolution | `^RES-<ticket-id>-\d{2}$` | RES-TKT-2026-000001-01 |
| Incident | `^INC-\d{3}$` | INC-003 |
| Intent | `^INT-\d{3}$` | INT-005 |
| PriorityRule | `^PRL-\d{3}$` | PRL-002 |
| Skill / Queue / Calendar | `^SKL-[A-Z]+$` / `^QUE-[A-Z]+$` / `^CAL-[A-Z0-9]+$` | SKL-BILLING, QUE-INCIDENT, CAL-247 |
| AgentShift / CalendarWindow / GroundTruth | `^SHF-\d{6}$` / `^CWN-\d{3}$` / `^GT-\d{6}$` | SHF-000001 |

`<ticket-id>` is the full ticket ID including its `TKT-2026-` prefix. Counters are zero-padded and gapless. Ticket numbers are assigned in `created_at` order (ties broken by customer ID). Per-ticket sequence numbers are assigned in event-time order.

### Entity inventory

13 required entities: Customer, Channel, Ticket, Message, Intent, Language, PriorityRule, Agent, Skill, Queue, AssignmentEvent, SLAEvent, Resolution.
8 supporting tables: SLACalendar, SLACalendarWindow, AgentSkill, AgentLanguage, QueueMember, AgentShift, TicketStatusEvent, GroundTruth.

(The earlier ER proposal said nine supporting tables; the correct count is seven, and this dictionary adds `SLACalendarWindow` for eight.)

---

## 1. Language

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| language_code | string | Y | `en`, `es`, `hi`, `ar`, `fr` | PK | Matches `^[a-z]{2}$` |
| language_name | string | Y | English, Spanish, Hindi, Arabic, French | UQ | Non-empty |
| is_supported | bool | Y | `true` for all five | - | Must be true for any language used by a ticket |

## 2. SLACalendar

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| calendar_code | string | Y | `CAL-EMAIL`, `CAL-CHAT`, `CAL-PHONE`, `CAL-247` | PK | Matches ID pattern |
| calendar_name | string | Y | Free text | UQ | Non-empty |
| timezone | string | Y | `UTC` only | - | Must equal `UTC` |
| effective_from | date | Y | `2026-01-05` | - | <= window start |
| effective_to | date | N | null = open-ended | - | If present, >= effective_from |

## 3. SLACalendarWindow

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| window_id | string | Y | `CWN-001`... | PK | Matches ID pattern |
| calendar_code | string | Y | FK -> SLACalendar | - | Must exist |
| day_of_week | int | Y | 1-7 (1 = Monday) | UQ(calendar_code, day_of_week) | One window per calendar per day; a closed day has no row |
| open_time | time | Y | `00:00`-`23:59` | - | < close_time |
| close_time | time | Y | `00:01`-`24:00` | - | `24:00` allowed only here. Windows are half-open: `[open, close)` |

## 4. Channel

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| channel_id | string | Y | `EMAIL`, `CHAT`, `PHONE` | PK | In enum |
| channel_name | string | Y | Free text | UQ | Non-empty |
| is_realtime | bool | Y | CHAT, PHONE = true; EMAIL = false | - | Matches the listed values |
| calendar_code | string | Y | FK -> SLACalendar | - | EMAIL -> CAL-EMAIL, CHAT -> CAL-CHAT, PHONE -> CAL-PHONE |
| message_type | string | Y | `EMAIL`, `CHAT`, `VOICE_TRANSCRIPT` | - | One per channel |

## 5. Skill

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| skill_id | string | Y | `SKL-BILLING`, `SKL-TECH`, `SKL-ACCOUNT`, `SKL-SECURITY`, `SKL-SALES`, `SKL-GENERAL` | PK | Matches ID pattern |
| skill_name | string | Y | Free text | UQ | Non-empty |
| description | string | N | Free text | - | - |

## 6. Intent

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| intent_id | string | Y | `INT-001`...`INT-012` | PK | Matches ID pattern |
| intent_code | string | Y | e.g. `BILLING_INQUIRY` | UQ | `^[A-Z_]+$` |
| intent_name | string | Y | Free text | UQ | Non-empty |
| category | string | Y | `BILLING`, `TECHNICAL`, `ACCOUNT`, `SECURITY_SAFETY`, `SALES`, `GENERAL` | - | In enum |
| required_skill_id | string | Y | FK -> Skill | - | Must exist |
| default_priority | string | Y | `P1`-`P4` | - | In enum |
| is_safety_related | bool | Y | true for INT-008, INT-009 only | - | If true, the safety rule forces P1 (see PriorityRule) |
| allows_prospect | bool | Y | true for INT-010, INT-011 only | - | Prospects may only raise tickets with these intents |
| median_handle_minutes | int | Y | > 0 | - | Generation parameter |
| volume_weight_pct | decimal | Y | 0-100 | - | Sum over all intents = 100.0 |

## 7. PriorityRule

One table holds SLA targets (versioned) and priority-classification rules.

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| rule_id | string | Y | `PRL-001`... | PK | Matches ID pattern |
| rule_type | string | Y | `SLA_TARGET`, `INCIDENT_OVERRIDE`, `SAFETY_OVERRIDE`, `SEGMENT_UPGRADE` | - | In enum |
| rule_name | string | Y | Free text | UQ | Non-empty |
| priority_level | string | C | `P1`-`P4` | - | Required for SLA_TARGET (the level it defines) and for the two override types (forced level = `P1`); null for SEGMENT_UPGRADE |
| segment | string | C | `ENTERPRISE` | - | Required only for SEGMENT_UPGRADE |
| upgrade_levels | int | C | 1 | - | Required only for SEGMENT_UPGRADE |
| floor_priority_level | string | C | `P2` | - | Required only for SEGMENT_UPGRADE; an upgrade never goes above this level |
| first_response_min | int | C | > 0 (business minutes) | - | Required for SLA_TARGET; < resolution_min |
| resolution_min | int | C | > 0 (business minutes) | - | Required for SLA_TARGET |
| escalation_after_min | int | C | > 0 (business minutes) | - | Required for SLA_TARGET; < resolution_min |
| calendar_code_override | string | N | FK -> SLACalendar | - | P1 SLA_TARGET row = `CAL-247`; null otherwise |
| precedence | int | Y | 1 = highest | - | INCIDENT 1, SAFETY 2, SEGMENT 3; SLA_TARGET rows use 9 |
| effective_from | date | Y | >= `2026-01-05` | - | - |
| effective_to | date | N | null = open-ended | - | >= effective_from; no two SLA_TARGET rows for the same priority_level overlap in time, and none leaves a gap |
| description | string | Y | Human-readable rule text | - | Non-empty |

## 8. Queue

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| queue_id | string | Y | `QUE-BILLING`, `QUE-TECH`, `QUE-ACCOUNT`, `QUE-SALES`, `QUE-GENERAL`, `QUE-INCIDENT`, `QUE-ESCALATION` | PK | Matches ID pattern |
| queue_name | string | Y | Free text | UQ | Non-empty |
| queue_type | string | Y | `SKILL`, `INCIDENT`, `ESCALATION` | - | In enum |
| required_skill_id | string | C | FK -> Skill | - | Required for SKILL queues; null for INCIDENT and ESCALATION |
| min_tier | string | Y | `T1`, `T2`, `T3` | - | SKILL = T1; INCIDENT = T2; ESCALATION = T2 |

## 9. Agent

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| agent_id | string | Y | `AGT-000001`... | PK | Matches ID pattern |
| full_name | string | Y | Synthetic name | - | Non-empty; not a real individual |
| tier | string | Y | `T1`, `T2`, `T3` | - | In enum |
| concurrency_limit | int | Y | 2-6 | - | T1 = 4, T2 = 5, T3 = 3, minus 1 for new hires (floor 2) |
| status | string | Y | `ACTIVE`, `INACTIVE` | - | INACTIVE agents never appear in AssignmentEvent |
| hire_date | date | Y | <= `2026-01-04` | - | New hire = hire_date >= `2025-11-06` |

## 10. AgentSkill (bridge, Agent N:M Skill)

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| agent_id | string | Y | FK -> Agent | PK part | Must exist |
| skill_id | string | Y | FK -> Skill | PK part | Must exist |
| proficiency | string | Y | `BASIC`, `PROFICIENT`, `EXPERT` | - | In enum |
| effective_from | date | Y | >= agent hire_date | PK part | PK = (agent_id, skill_id, effective_from) |
| effective_to | date | N | null = open-ended | - | >= effective_from; periods for the same agent+skill do not overlap |

## 11. AgentLanguage (bridge, Agent N:M Language)

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| agent_id | string | Y | FK -> Agent | PK part | Must exist |
| language_code | string | Y | FK -> Language | PK part | Must exist |
| fluency | string | Y | `BASIC`, `FLUENT`, `NATIVE` | - | Every agent has `en` at FLUENT or NATIVE. Routing treats FLUENT and NATIVE as "speaks". |

## 12. QueueMember (bridge, Queue N:M Agent)

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| queue_id | string | Y | FK -> Queue | PK part | Must exist |
| agent_id | string | Y | FK -> Agent | PK part | Must exist |
| effective_from | date | Y | >= agent hire_date | PK part | PK = (queue_id, agent_id, effective_from) |
| effective_to | date | N | null = open-ended | - | >= effective_from |

## 13. AgentShift

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| shift_id | string | Y | `SHF-000001`... | PK | Matches ID pattern |
| agent_id | string | Y | FK -> Agent | UQ(agent_id, day_of_week, effective_from) | Must exist |
| day_of_week | int | Y | 1-7 (1 = Monday) | - | A NIGHT shift belongs to the day it starts |
| shift_code | string | Y | `EARLY` 06:00-14:00, `DAY` 10:00-18:00, `LATE` 14:00-22:00, `NIGHT` 22:00-06:00 | - | start/end must match the shift_code definition |
| start_time | time | Y | HH:MM | - | - |
| end_time | time | Y | HH:MM | - | NIGHT ends the following day (end_time < start_time only for NIGHT) |
| effective_from | date | Y | >= `2026-01-05` | - | - |
| effective_to | date | N | null = open-ended | - | >= effective_from |

## 14. Customer

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| customer_id | string | Y | `CUST-000001`... | PK | Matches ID pattern |
| customer_type | string | Y | `CUSTOMER`, `PROSPECT` | - | PROSPECT only on tickets with allows_prospect intents |
| full_name | string | Y | Synthetic name | - | Not a real individual |
| email | string | Y | `<handle>@example.test` | UQ | Reserved test domain only |
| phone | string | N | `+999...` | - | Unallocated `+999` prefix, so numbers are never real |
| segment | string | Y | `CONSUMER`, `SMB`, `ENTERPRISE` | - | In enum |
| country_code | string | Y | ISO 3166-1 alpha-2 | - | Consistent with preferred_language (see business_rules.md) |
| preferred_language | string | Y | FK -> Language | - | Must exist |
| created_at | timestamp | Y | <= first ticket created_at | - | Customers may predate the window |

## 15. Ticket

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| ticket_id | string | Y | `TKT-2026-000001`... | PK | Matches pattern; numbers gapless 1..N in created_at order |
| customer_id | string | Y | FK -> Customer | - | Must exist |
| channel_id | string | Y | FK -> Channel | - | Must exist |
| intent_id | string | Y | FK -> Intent | - | If customer_type = PROSPECT, intent.allows_prospect must be true |
| queue_id | string | Y | FK -> Queue | - | Initial queue; never changes. P1 -> QUE-INCIDENT, else the queue of the intent's required skill |
| language_code | string | Y | FK -> Language | - | = customer.preferred_language, except 5% of tickets use `en` |
| created_at | timestamp | Y | 2026-01-05T00:00:00Z to < 2026-04-05T00:00:00Z | - | First TicketStatusEvent (NEW) has the same timestamp |
| subject | string | Y | Template text | - | Non-empty |
| priority_level | string | Y | `P1`-`P4` | - | Must equal the level derived by the classification rules (see business_rules.md section 5) |
| priority_reason_code | string | Y | `INTENT_DEFAULT`, `INCIDENT_OVERRIDE`, `SAFETY_OVERRIDE`, `SEGMENT_UPGRADE` | - | Consistent with priority_level |
| priority_rule_id | string | Y | FK -> PriorityRule | - | The SLA_TARGET row for priority_level effective on created_at |
| is_critical_incident | bool | Y | - | - | true if and only if incident_id is not null |
| incident_id | string | N | `INC-001`... | - | Set only for tickets generated inside an incident burst |
| is_after_hours_arrival | bool | Y | - | - | Derived: created_at falls outside the channel's calendar windows |
| current_status | string | Y | Status enum (section 16) | - | Equals to_status of the ticket's last TicketStatusEvent |
| last_status_at | timestamp | Y | - | - | Equals event_at of the last TicketStatusEvent |
| reopen_count | int | Y | 0-2 | - | Derived: count of REOPENED status events |

## 16. TicketStatusEvent

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| status_event_id | string | Y | `TSE-<ticket-id>-001`... | PK | Matches pattern |
| ticket_id | string | Y | FK -> Ticket | UQ(ticket_id, seq_no) | Must exist |
| seq_no | int | Y | 1..n | - | Gapless; ordered by event_at, then seq_no |
| cycle_no | int | Y | 1-3 | - | Increases by 1 only at a REOPENED event |
| from_status | string | C | Status enum | - | Null only for seq_no = 1; else equals previous row's to_status |
| to_status | string | Y | `NEW`, `QUEUED`, `ASSIGNED`, `IN_PROGRESS`, `PENDING_CUSTOMER`, `ESCALATED`, `RESOLVED`, `CLOSED`, `REOPENED` | - | (from_status, to_status) must be an allowed transition (business_rules.md section 7.1) |
| event_at | timestamp | Y | - | - | >= previous event_at; <= dataset cutoff |
| actor_type | string | Y | `SYSTEM`, `AGENT`, `CUSTOMER` | - | REOPENED = CUSTOMER; CLOSED = SYSTEM |
| actor_agent_id | string | C | FK -> Agent | - | Required if actor_type = AGENT; else null |
| trigger_ref | string | N | AssignmentEvent / Resolution ID | - | If present, must exist and share the ticket_id |

## 17. Message

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| message_id | string | Y | `MSG-<ticket-id>-001`... | PK | Matches pattern |
| ticket_id | string | Y | FK -> Ticket | UQ(ticket_id, seq_no) | Must exist |
| seq_no | int | Y | 1..n | - | Gapless; ordered by sent_at; seq 1 is always the customer's opening message |
| cycle_no | int | Y | 1-3 | - | Matches the ticket's cycle at sent_at |
| sender_type | string | Y | `CUSTOMER`, `AGENT`, `SYSTEM` | - | In enum |
| sender_agent_id | string | C | FK -> Agent | - | Required if sender_type = AGENT, else null. Must be the ticket's owner at sent_at |
| message_type | string | Y | `EMAIL`, `CHAT`, `VOICE_TRANSCRIPT`, `SYSTEM_NOTE` | - | SYSTEM sender = SYSTEM_NOTE; others = channel's message_type |
| in_reply_to_message_id | string | N | FK -> Message | - | If present: same ticket, lower seq_no. Null for seq 1 |
| sent_at | timestamp | Y | >= ticket.created_at | - | Message 1 sent_at = ticket.created_at |
| language_code | string | Y | FK -> Language | - | Customer messages = ticket language; agent messages = a language the agent speaks |
| template_id | string | Y | Template key | - | Exists in the template library |
| body | string | Y | Rendered template text | - | Non-empty; contains no real personal data |

## 18. AssignmentEvent

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| assignment_id | string | Y | `ASN-<ticket-id>-001`... | PK | Matches pattern |
| ticket_id | string | Y | FK -> Ticket | UQ(ticket_id, seq_no) | Must exist |
| seq_no | int | Y | 1..n | - | Gapless; ordered by event_at |
| cycle_no | int | Y | 1-3 | - | Matches the ticket's cycle at event_at |
| event_type | string | Y | `ASSIGN`, `TRANSFER`, `ESCALATE`, `REOPEN_ASSIGN` | - | First event of cycle 1 = ASSIGN; first of cycle k>1 = REOPEN_ASSIGN |
| reason_code | string | Y | `INITIAL_ROUTING`, `LANGUAGE_FALLBACK`, `SHIFT_HANDOVER`, `SKILL_REROUTE`, `SLA_ESCALATION`, `MANUAL_ESCALATION`, `REOPEN_ROUTING` | - | Must be valid for event_type (business_rules.md section 6.5) |
| event_at | timestamp | Y | - | - | >= the ticket's latest QUEUED/ESCALATED status event |
| agent_id | string | Y | FK -> Agent | - | The agent who owns the ticket after this event |
| from_agent_id | string | C | FK -> Agent | - | Null for ASSIGN and REOPEN_ASSIGN; required for TRANSFER and ESCALATE; must differ from agent_id and equal the previous event's agent_id |
| queue_id | string | Y | FK -> Queue | - | ESCALATE = QUE-ESCALATION; others = ticket.queue_id |
| required_skill_id | string | Y | FK -> Skill | - | The skill evaluated for this decision. Equals intent.required_skill_id except for SKILL_REROUTE (a different skill) |
| language_match | bool | Y | - | - | true if the agent speaks the ticket language at FLUENT or better; false only for LANGUAGE_FALLBACK |
| wait_minutes | decimal | Y | >= 0, 1 dp | - | event_at minus the time the ticket entered QUEUED/ESCALATED |
| agent_load_before | int | Y | >= 0 | - | Derived by replay: tickets the agent owned in ASSIGNED, IN_PROGRESS or PENDING_CUSTOMER just before event_at |
| agent_load_after | int | Y | >= 1 | - | = agent_load_before + 1 |
| concurrency_limit | int | Y | 2-6 | - | Equals Agent.concurrency_limit |

## 19. SLAEvent (one row per SLA cycle)

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| sla_event_id | string | Y | `SLA-<ticket-id>` (cycle 1), `-R1`, `-R2` | PK | Matches pattern; suffix = cycle_no - 1 |
| ticket_id | string | Y | FK -> Ticket | UQ(ticket_id, cycle_no) | Must exist |
| cycle_no | int | Y | 1-3 | - | Equals the ticket's cycle |
| priority_rule_id | string | Y | FK -> PriorityRule | - | SLA_TARGET row effective at the cycle's start (created_at, or the REOPENED time) |
| calendar_code_used | string | Y | FK -> SLACalendar | - | P1 -> `CAL-247`; otherwise the channel's calendar |
| clock_start | timestamp | Y | >= cycle start | - | First open instant of the calendar at or after the cycle start |
| first_response_target_min | int | Y | > 0 | - | From the rule; reopen cycles use 50% of the rule value, rounded up |
| resolution_target_min | int | Y | > 0 | - | Same reopen adjustment |
| escalation_after_min | int | Y | > 0 | - | From the rule |
| first_response_due | timestamp | Y | - | - | clock_start + first_response_target_min business minutes |
| resolution_due | timestamp | Y | - | - | clock_start + resolution_target_min business minutes |
| escalation_due_at | timestamp | Y | - | - | clock_start + escalation_after_min business minutes |
| first_response_at | timestamp | N | - | - | sent_at of the first AGENT message in the cycle; null if none by cutoff |
| resolved_at | timestamp | N | - | - | Matches the cycle's RESOLVED status event; null if unresolved by cutoff |
| frt_business_minutes | decimal | N | >= 0, 1 dp | - | Business minutes from clock_start to first_response_at |
| resolution_business_minutes | decimal | N | >= 0, 1 dp | - | Business minutes from clock_start to resolved_at |
| frt_breached | bool | Y | - | - | Derived: first_response_at > first_response_due, or, if null, cutoff > first_response_due |
| resolution_breached | bool | Y | - | - | Derived: resolved_at > resolution_due, or, if null, cutoff > resolution_due |

## 20. Resolution (one row per resolved cycle)

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| resolution_id | string | Y | `RES-<ticket-id>-01`... | PK | Matches pattern; suffix = cycle_no |
| ticket_id | string | Y | FK -> Ticket | UQ(ticket_id, cycle_no) | Must exist |
| cycle_no | int | Y | 1-3 | - | Has a matching RESOLVED status event |
| resolved_at | timestamp | Y | - | - | Equals the RESOLVED status event time |
| resolution_code | string | Y | `SOLVED`, `WORKAROUND`, `REFUNDED`, `INFO_PROVIDED`, `DUPLICATE`, `NO_CUSTOMER_RESPONSE` | - | REFUNDED only for refund/payment intents |
| resolved_by_agent_id | string | Y | FK -> Agent | - | Equals the ticket's owner at resolved_at |
| time_to_resolve_minutes | decimal | Y | >= 0, 1 dp | - | Wall-clock minutes from cycle start to resolved_at |
| transfer_count | int | Y | >= 0 | - | Count of TRANSFER events in the cycle |
| escalation_count | int | Y | >= 0 | - | Count of ESCALATE events in the cycle |
| csat_responded | bool | Y | - | - | - |
| csat_score | int | C | 1-5 | - | Required iff csat_responded = true |
| csat_submitted_at | timestamp | C | - | - | Required iff csat_responded = true; >= resolved_at |

## 21. GroundTruth (separate file, never an input feature)

Written to `data/ground_truth/ground_truth.csv`, not to `data/raw/`.

| Field | Type | Req | Allowed values / format | Unique | Validation rule |
|---|---|---|---|---|---|
| gt_id | string | Y | `GT-000001`... | PK | Matches pattern |
| issue_type | string | Y | `AGENT_OVER_CAPACITY`, `SLA_IGNORES_CALENDAR`, `REOPEN_LOSES_HISTORY`, `TRANSFER_LOOP`, `PRIORITY_CONTRADICTS_SAFETY` | - | In enum |
| entity_type | string | Y | `ASSIGNMENT_EVENT`, `SLA_EVENT`, `TICKET` | - | In enum |
| entity_id | string | Y | ID of the affected row | - | Must exist in the input data (except REOPEN_LOSES_HISTORY, where it is the ticket ID) |
| ticket_id | string | Y | FK -> Ticket | - | Must exist |
| is_primary | bool | Y | - | - | true for the deliberately injected row; false for a cascade row |
| detection_rule | string | Y | Validation rule ID (e.g. `V-ROUT-CAP-01`) | - | Exists in the validation catalog |
| expected_value | string | N | Correct value | - | Per anomaly type |
| observed_value | string | N | Injected value | - | Per anomaly type |
| description | string | Y | Human-readable summary | - | Non-empty |

Companion file `data/ground_truth/ground_truth_decisions.csv`: one row per ticket with the values the documented rules produce *before* injection.

| Field | Type | Req | Validation rule |
|---|---|---|---|
| ticket_id | string | Y | PK; FK -> Ticket |
| expected_priority_level | string | Y | Output of the classification rules |
| expected_queue_id | string | Y | Output of the queue rule |
| expected_priority_rule_id | string | Y | SLA_TARGET row effective at created_at |
| expected_calendar_code | string | Y | P1 -> CAL-247, else the channel's calendar |
| expected_first_response_due | timestamp | Y | Cycle 1 |
| expected_resolution_due | timestamp | Y | Cycle 1 |
| expected_escalation_due_at | timestamp | Y | Cycle 1 |

---

## 22. Relationships and cardinality

| Parent | Child | Cardinality | Optional? | FK in child |
|---|---|---|---|---|
| Language | Customer | 1:N | Required | preferred_language |
| Language | Ticket, Message | 1:N | Required | language_code |
| Language | Agent (via AgentLanguage) | N:M | Agent requires at least `en` | bridge |
| SLACalendar | SLACalendarWindow | 1:N | Required | calendar_code |
| SLACalendar | Channel | 1:N | Required | calendar_code |
| SLACalendar | SLAEvent / PriorityRule | 1:N | SLAEvent required; PriorityRule override optional | calendar_code_used / calendar_code_override |
| Channel | Ticket | 1:N | Required | channel_id |
| Customer | Ticket | 1:N | Required | customer_id |
| Skill | Intent / Queue | 1:N | Intent required; Queue optional (null for INCIDENT, ESCALATION) | required_skill_id |
| Skill | AssignmentEvent | 1:N | Required | required_skill_id |
| Skill <-> Agent | AgentSkill | N:M | Agent has 1-4 skills | bridge |
| Intent | Ticket | 1:N | Required | intent_id |
| PriorityRule | Ticket / SLAEvent | 1:N | Required | priority_rule_id |
| Queue | Ticket / AssignmentEvent | 1:N | Required | queue_id |
| Queue <-> Agent | QueueMember | N:M | Effective-dated | bridge |
| Agent | AgentShift | 1:N | Required (5 rows per agent) | agent_id |
| Ticket | Message | 1:N | At least 1 | ticket_id |
| Message | Message | 1:N (self) | Optional | in_reply_to_message_id |
| Ticket | TicketStatusEvent | 1:N | At least 1 | ticket_id |
| Ticket | AssignmentEvent | 1:N | 0 for tickets never assigned by cutoff | ticket_id |
| Agent | AssignmentEvent | 1:N | Required (agent_id); optional (from_agent_id) | agent_id, from_agent_id |
| Ticket | SLAEvent | 1:N | One per cycle (1-3) | ticket_id |
| Ticket | Resolution | 1:N | One per resolved cycle; 0 if unresolved | ticket_id |
| Agent | Resolution | 1:N | Required | resolved_by_agent_id |
| Ticket | GroundTruth | 1:N | Only for injected cases | ticket_id |

Lifecycle states: Ticket `NEW -> QUEUED -> ASSIGNED -> IN_PROGRESS <-> PENDING_CUSTOMER -> ESCALATED -> RESOLVED -> CLOSED` (and `REOPENED`). Allowed transitions are in `business_rules.md` section 7.1.