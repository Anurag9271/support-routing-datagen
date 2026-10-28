# Business Rules

Defines how the data is generated, how decisions are made, how derived values are computed, how anomalies are injected, and how the output is validated. Field definitions live in `data_dictionary.md`. Every number below is a default that lives in `generator/config.py`.

## 1. Scope and conventions

| Item | Value |
|---|---|
| Seed | `20261022` |
| Arrival window | `2026-01-05T00:00:00Z` (Monday) up to but excluding `2026-04-05T00:00:00Z` = 90 days |
| Dataset cutoff | `2026-04-05T00:00:00Z` (= window end). The dataset is a snapshot at the end of the 90-day window: tickets still in flight stay open, tickets resolved less than 72 h earlier stay RESOLVED, and no event after the cutoff is emitted. |
| Time zone | UTC everywhere, no DST |
| Time arithmetic | Integer seconds. Minutes are converted to seconds first. No floating-point date math. |
| Default scale | 30,000 tickets, 100 agents, 100,000 messages (exact), about 12,000 customers (`tickets x 0.4`) |
| Scale parameters | `--tickets --agents --messages --customers --days --seed --out`. The same code produces the starter and full datasets. |

All volume figures, rates and targets below scale with `--tickets`.

## 2. Reference data

### 2.1 Languages

`en, es, hi, ar, fr`. Customer shares: en 60%, es 15%, hi 12%, fr 7%, ar 6%.

Country by language (sampled by weight): en -> US 40, GB 20, IN 20, CA 10, AU 10. es -> ES 50, MX 50. hi -> IN 100. fr -> FR 70, CA 30. ar -> AE 50, SA 50.

### 2.2 Channels and calendars (UTC)

| Channel | Calendar | Open windows |
|---|---|---|
| EMAIL | CAL-EMAIL | Mon-Fri 08:00-20:00 |
| CHAT | CAL-CHAT | Mon-Sun 06:00-22:00 |
| PHONE | CAL-PHONE | Mon-Fri 09:00-18:00 |
| (P1 override) | CAL-247 | Mon-Sun 00:00-24:00 |

Windows are half-open `[open, close)`. There are no holidays (see section 16).

A ticket is an **after-hours arrival** when `created_at` falls outside its channel's calendar. Phone after-hours tickets are voicemail callbacks. Chat after-hours tickets are offline messages. Note that after-hours uses the channel's calendar, not the P1 override.

### 2.3 Skills and intents

Skills: `SKL-BILLING, SKL-TECH, SKL-ACCOUNT, SKL-SECURITY, SKL-SALES, SKL-GENERAL`.

| Intent | Code | Skill | Default priority | Safety | Weight % | Median handle (min) | Prospect |
|---|---|---|---|---|---|---|---|
| INT-001 | BILLING_INQUIRY | BILLING | P3 | no | 14.0 | 25 | no |
| INT-002 | REFUND_REQUEST | BILLING | P3 | no | 10.0 | 40 | no |
| INT-003 | PAYMENT_FAILURE | BILLING | P2 | no | 8.0 | 30 | no |
| INT-004 | TECH_TROUBLESHOOTING | TECH | P3 | no | 16.0 | 70 | no |
| INT-005 | OUTAGE_REPORT | TECH | P2 | no | 3.0 | 120 | no |
| INT-006 | ACCOUNT_ACCESS | ACCOUNT | P2 | no | 11.0 | 20 | no |
| INT-007 | ACCOUNT_UPDATE | ACCOUNT | P4 | no | 9.0 | 15 | no |
| INT-008 | SECURITY_BREACH | SECURITY | P2 | **yes** | 1.5 | 90 | no |
| INT-009 | PRODUCT_SAFETY_HAZARD | SECURITY | P2 | **yes** | 1.0 | 120 | no |
| INT-010 | SALES_PRESALES | SALES | P4 | no | 10.0 | 30 | yes |
| INT-011 | GENERAL_QUESTION | GENERAL | P4 | no | 10.0 | 12 | yes |
| INT-012 | COMPLAINT_FEEDBACK | GENERAL | P3 | no | 6.5 | 45 | no |

Weights sum to 100. Safety intents default to P2 on purpose: the safety rule lifts them to P1, and an injected rule failure then looks like a plausible "rule did not fire".

### 2.4 Queues

| Queue | Type | Skill | Min tier | Receives |
|---|---|---|---|---|
| QUE-BILLING / TECH / ACCOUNT / SALES / GENERAL | SKILL | matching skill | T1 | Non-P1 tickets whose intent uses that skill |
| QUE-INCIDENT | INCIDENT | none | T2 | Every P1 ticket |
| QUE-ESCALATION | ESCALATION | none | T2 | Used by ESCALATE assignment events |

Because every safety intent is P1, SKL-SECURITY work always flows through QUE-INCIDENT. There is no security skill queue.

### 2.5 PriorityRule rows

| Rule | Type | Level | First response | Resolution | Escalate after | Calendar | Effective |
|---|---|---|---|---|---|---|---|
| PRL-001 | SLA_TARGET | P1 | 15 | 240 | 60 | CAL-247 | 2026-01-05 - open |
| PRL-002 | SLA_TARGET | P2 | 60 | 480 | 240 | channel | 2026-01-05 - 2026-02-15 |
| PRL-003 | SLA_TARGET | P2 | **45** | 480 | 240 | channel | 2026-02-16 - open |
| PRL-004 | SLA_TARGET | P3 | 240 | 1440 | 720 | channel | 2026-01-05 - open |
| PRL-005 | SLA_TARGET | P4 | 480 | 2880 | 1440 | channel | 2026-01-05 - open |
| PRL-006 | INCIDENT_OVERRIDE | P1 | | | | | precedence 1 |
| PRL-007 | SAFETY_OVERRIDE | P1 | | | | | precedence 2 |
| PRL-008 | SEGMENT_UPGRADE | floor P2 | | | | | precedence 3, ENTERPRISE, +1 level |

All minutes are business minutes. The P2 first-response change on 2026-02-16 exercises effective dating: the target applies by ticket creation date (or reopen date for a reopen cycle).

## 3. Population generation

### 3.1 Agents (100)

| Tier | Count | Concurrency limit |
|---|---|---|
| T1 | 70 | 4 |
| T2 | 22 | 5 |
| T3 | 8 | 3 |

- 15% of agents are new hires (hire_date >= 2025-11-06). Their limit is reduced by 1 (floor 2). All other hire dates are uniform 2019-01-01 to 2025-11-05.
- 3 agents are `INACTIVE` (all T1). They get shifts and skills but must never receive work.
- Hire dates are all before the window, so every agent exists for the full window.

### 3.2 Skills

| Tier | Skills held | Proficiency |
|---|---|---|
| T1 | 1 primary (BILLING 25, TECH 30, ACCOUNT 20, GENERAL 15, SALES 10 weights) plus a second skill with 30% probability | BASIC 40%, PROFICIENT 60% |
| T2 | 2-3 skills; TECH with 70%, SECURITY with 40% | PROFICIENT 50%, EXPERT 50% |
| T3 | 3-4 skills; always TECH and SECURITY | EXPERT |

Eight skill acquisitions are scheduled on random days of the window (an `AgentSkill` row with a later `effective_from`, plus a matching `QueueMember` row). They exercise effective dating.

### 3.3 Languages

Every agent has `en` (NATIVE 40%, FLUENT 60%). Additional languages: es 24 agents, hi 20, fr 12, ar 10 (NATIVE 50%, FLUENT 50%). Agents are chosen at random per language, so the same agent can hold several.

### 3.4 Shifts

| Code | Hours (UTC) | Agents |
|---|---|---|
| EARLY | 06:00-14:00 | 25 |
| DAY | 10:00-18:00 | 30 |
| LATE | 14:00-22:00 | 30 |
| NIGHT | 22:00-06:00 | 15 |

Work-day patterns: A = Mon-Fri (70% of agents), B = Sun-Thu (15%), C = Tue-Sat (15%). Each agent gets one `AgentShift` row per work day (500 rows). Within each shift code, patterns are spread evenly, so every day has some coverage. Tier mix within NIGHT: 8 T1, 5 T2, 2 T3.

Feasibility check at generation time: for BILLING, TECH, ACCOUNT and GENERAL there must be at least one `en` speaking, active agent on shift in every hour of the week. SALES and SECURITY may have gaps (this produces realistic after-hours waits).

### 3.5 Queue membership

An agent belongs to each skill queue for each skill they hold. All T2 and T3 agents belong to QUE-INCIDENT and QUE-ESCALATION. Membership starts at `hire_date` or at the skill's `effective_from`.

### 3.6 Customers

| Attribute | Rule |
|---|---|
| Type | 10% PROSPECT, 90% CUSTOMER |
| Segment | CONSUMER 70%, SMB 22%, ENTERPRISE 8% |
| Ticket propensity | Tickets per customer are drawn proportional to weight CONSUMER 1, SMB 2, ENTERPRISE 3. This makes ticket volume heavy-tailed. |
| Language and country | Per section 2.1 |
| Name | Seeded draw from lists of generic first and last names. No real individuals. |
| Email / phone | `<handle>@example.test`; `+999` prefix numbers. Never real. |
| Created at | Uniform from 2024-01-01 to the customer's first ticket |

## 4. Arrival model

### 4.1 Baseline arrival rate

Expected tickets at time `t` for channel `c` = `base_daily x dow_weight x hour_weight x channel_mask`.

- `dow_weight`: Mon-Fri 1.0, Sat 0.55, Sun 0.45.
- `hour_weight` (UTC): peaks 10:00-14:00 (1.0), shoulders 07:00-10:00 and 14:00-19:00 (0.7), 19:00-23:00 (0.35), 23:00-07:00 (0.15).
- `channel_mask`: 1.0 inside the channel's open windows; outside it is the after-hours factor: EMAIL 0.35, CHAT 0.25, PHONE 0.10.
- With the factors above, after-hours arrivals are about 11% overall (EMAIL about 16%, PHONE about 14%, CHAT about 4%). Accepted range: 8-20%. Weekend ticket shares (about 6% and 5%) are lower than `dow_weight` alone suggests, because EMAIL and PHONE are closed at weekends and the after-hours factor applies on top.

Channel mix: CONSUMER and SMB: EMAIL 50%, CHAT 40%, PHONE 10%. ENTERPRISE: EMAIL 40%, CHAT 30%, PHONE 30%.

Intent is drawn from the intent weights (section 2.3), restricted to `allows_prospect` intents for PROSPECT customers. The intent weights are renormalised after that restriction.

Because prospects (10% of customers) draw only from allows_prospect intents, the realised SALES_PRESALES and GENERAL_QUESTION shares are higher than their weights (SALES about 14%), and other intents are slightly lower. This is accepted.

### 4.2 Queue spikes

Up to 5 spike windows chosen by seed on weekdays (scaled as in 4.3), each 2-3 hours long. Within a window the arrival rate is multiplied by 4. During a spike the intent mix is skewed (BILLING and PAYMENT_FAILURE x3 for 3 spikes; TECH_TROUBLESHOOTING x3 for 2). The x4 multiplier is applied to the hourly arrival weights, so spike tickets emerge from the single allocation of all tickets (about 2% of the total)

### 4.3 Critical incidents

6 incident windows (`INC-001`..`INC-006`), each 90-240 minutes, on different days. Each generates 100-250 `OUTAGE_REPORT` tickets, skewed to CHAT and PHONE. Incident tickets have `incident_id` set and `is_critical_incident = true`, and are P1. Their median handle time is 15 minutes (known-incident macro replies). Incident tickets total about 4% of volume.

Baseline tickets are scaled so that baseline + spike + incident = the `--tickets` total exactly.

Scaled runs: `n_incidents = min(6, max(1, tickets // 2000))` and `n_spikes = min(5, max(1, tickets // 2000))`. Total incident tickets = `round(0.04 x tickets)`, split across incidents with +/-30% jitter that still sums exactly (about 200 each at 30,000 tickets).

## 5. Priority classification

Evaluate in this order and stop at the first match (lowest `precedence`).

| # | Condition | Priority | `priority_reason_code` |
|---|---|---|---|
| 1 | `incident_id` is not null | P1 | INCIDENT_OVERRIDE |
| 2 | `intent.is_safety_related` | P1 | SAFETY_OVERRIDE |
| 3 | `segment = ENTERPRISE` | `intent.default_priority` raised by one level, never above P2 (P4->P3, P3->P2, P2 stays P2). If the level did not change, the reason is INTENT_DEFAULT. | SEGMENT_UPGRADE |
| 4 | otherwise | `intent.default_priority` | INTENT_DEFAULT |

Queue: P1 -> `QUE-INCIDENT`; otherwise the queue for `intent.required_skill_id`.

`priority_rule_id` = the SLA_TARGET row for the final priority effective on `created_at`.

**Safety rule (SR-1).** Any ticket whose intent has `is_safety_related = true` must have `priority_level = P1`. This rule is deliberately stated so the validator can check it without knowing anything about injected cases.

Prospect tickets: `segment` of the prospect customer applies as normal.

## 6. Routing

Routing is an event-driven simulation. Events are processed in order of `(timestamp, event_type_rank, ticket_id)` so ties are deterministic. Triggers for an assignment attempt: ticket enters QUEUED, an agent releases capacity, a shift starts, a ticket re-enters QUEUED.

### 6.1 Hard eligibility (all must hold)

| ID | Constraint |
|---|---|
| H1 | Agent `status = ACTIVE` and `hire_date` <= date |
| H2 | Agent is on shift at that instant (`AgentShift` effective that day; half-open interval; NIGHT crosses midnight) |
| H3 | Agent holds `required_skill_id` on that date (`AgentSkill` effective) |
| H3b | For P1 and P2 tickets, proficiency is PROFICIENT or EXPERT |
| H4 | Agent speaks the ticket language at FLUENT or NATIVE (waived only by the fallback rule 6.3) |
| H5 | `agent_load_before < concurrency_limit` |
| H6 | Agent is a `QueueMember` of the target queue on that date, and `tier >= queue.min_tier` |
| H7 | Agent has not already owned this ticket in the same cycle (prevents loops) |

Agent **load** = number of tickets the agent currently owns in status ASSIGNED, IN_PROGRESS or PENDING_CUSTOMER. It drops when the ticket leaves the agent (transfer, escalation, shift handover) or is RESOLVED.

### 6.2 Choosing among eligible agents

Sort by: (1) `load / concurrency_limit` ascending, (2) proficiency descending (EXPERT, PROFICIENT, BASIC), (3) assignments received that day ascending, (4) `agent_id` ascending. Take the first.

### 6.3 Waiting and language fallback

If no agent is eligible, the ticket stays QUEUED. Waiting tickets are served in order of `(priority, SLA first_response_due, ticket_id)`. If a ticket has waited longer than the language threshold and a language match is the only unmet condition, H4 is waived and the ticket goes to the best `en` agent. The assignment gets `reason_code = LANGUAGE_FALLBACK` and `language_match = false`.

| Priority | Fallback threshold (min) |
|---|---|
| P1 | 5 |
| P2 | 15 |
| P3 | 30 |
| P4 | 60 |

Capacity and skill are never waived. Fallback is valid baseline behaviour, not an anomaly. The threshold is measured within a single uninterrupted QUEUED spell; the waiver activates the first time such a spell reaches the threshold and then holds for the rest of the cycle. Expected fallback share: about 5-12% of tickets (realised about 9%), concentrated in ar, fr and hi tickets because few agents combine those languages with the required skill and shift.

The waiver is per cycle: once a ticket's wait exceeds its threshold, H4 stays waived for the rest of that cycle, including SHIFT_HANDOVER assignments. While waived, candidates are ranked language-match first, then by the normal score (6.2). The reason code is LANGUAGE_FALLBACK only on an ASSIGN event whose chosen agent lacks the language.

### 6.4 Transfers, handovers and reopen routing

All use the same rules, with these differences:

- **SHIFT_HANDOVER:** when an agent's shift ends, each ticket they own in ASSIGNED or IN_PROGRESS is reassigned at that exact instant. If no eligible agent exists the ticket goes to QUEUED and is assigned later (still reason SHIFT_HANDOVER). Tickets in PENDING_CUSTOMER never cross a shift end (see 7.4).
- **SKILL_REROUTE:** the new agent must hold a different skill (`required_skill_id` on the event is that skill, not the intent's skill).
- **ESCALATE:** destination tier must be exactly the owner's tier + 1, and the queue is `QUE-ESCALATION`.
- **REOPEN_ASSIGN:** routed like a first assignment. The previous agents of earlier cycles may be chosen again.

### 6.5 Event type and reason code combinations

| event_type | Allowed reason_code | from_agent_id |
|---|---|---|
| ASSIGN | INITIAL_ROUTING, LANGUAGE_FALLBACK | null |
| TRANSFER | SHIFT_HANDOVER, SKILL_REROUTE | required |
| ESCALATE | SLA_ESCALATION, MANUAL_ESCALATION | required |
| REOPEN_ASSIGN | REOPEN_ROUTING | null |

### 6.6 Simulation mechanics

- **Ordering.** The event queue is ordered by `(timestamp, rank, ticket_id)`. Ranks: 1 RESOLVE, 2 SHIFT_END, 3 SHIFT_START, 4 FALLBACK_DUE, 5 PICKUP (first agent action, moves ASSIGNED to IN_PROGRESS), 6 ENQUEUE (NEW to QUEUED). After all events at one timestamp are processed, one DISPATCH pass runs. A ticket resolved at exactly a shift-end instant is resolved before the handover.
- **Epochs.** Each assignment increments the ticket's epoch. Scheduled RESOLVE and PICKUP events carry the epoch they were created in and are discarded if stale.
- **Dispatch.** The waiting list is every ticket in QUEUED plus handover tickets in limbo (below), ordered by `(priority, first_response_due, ticket_id)`. `first_response_due` is computed from the PriorityRule and `sla_calendar.py`, never read from ground truth. There is no head-of-line blocking: each waiting ticket is tried in order against the eligible agents.
- **Shift boundaries.** An agent is on shift in `[start, end)`. At SHIFT_END, every ticket the agent owns (ASSIGNED or IN_PROGRESS), in ticket_id order, goes into handover limbo. During the same timestamp's dispatch, a limbo ticket that finds an eligible agent is assigned directly (status IN_PROGRESS or ASSIGNED to ASSIGNED, `wait_minutes` = 0). Limbo tickets still unassigned after dispatch get a `-> QUEUED` status event at that instant, and their wait clock starts there.
- **Work accounting.** Each ticket draws total work `W` once. Work accrues in wall-clock seconds while the ticket is owned (ASSIGNED or IN_PROGRESS); time spent QUEUED does not count. On any assignment at time `t` with remaining work `R`: `pickup_at = t + pickup_delay` (drawn as in 7.2; the first assignment's pickup is the first response), and `resolved_at = max(t + R, pickup_at + 60 s)`.
- **Work floor.** Remaining work is floored at 0 at handover; this can happen when `resolved_at` was clamped to `pickup_at + 60 s`.
- **Draws.** All per-ticket randomness comes from a fixed-size vector pre-drawn from `ticket_rng(seed, ticket_number, "handle_times")`, indexed by purpose (work, pickup delay by assignment number, classification delay), so draws never depend on simulation order.
- **Sequencing.** `seq_no` for status and assignment events follows emission order, which is chronological per ticket. IDs are assigned from `seq_no`.
- **Load replay.** A validator reconstructs load from the event tables. Releases at timestamp `t` (RESOLVED, `-> QUEUED` from an owned state, or a TRANSFER away) apply before assignments at `t`. For assignments to one agent at one timestamp, sorting by `agent_load_before` must give `base, base+1, base+2...`.

## 7. Ticket lifecycle simulation

### 7.1 Allowed status transitions

| From | To |
|---|---|
| (none) | NEW |
| NEW | QUEUED |
| QUEUED | ASSIGNED |
| ASSIGNED | IN_PROGRESS, ASSIGNED (transfer), QUEUED (handover with no agent), ESCALATED |
| IN_PROGRESS | PENDING_CUSTOMER, RESOLVED, ESCALATED, ASSIGNED (transfer), QUEUED (handover with no agent) |
| PENDING_CUSTOMER | IN_PROGRESS, RESOLVED |
| ESCALATED | ASSIGNED |
| RESOLVED | CLOSED, REOPENED |
| REOPENED | QUEUED |
| CLOSED | (terminal) |

Every other transition is invalid. A new cycle starts only at REOPENED.

### 7.2 Timeline of a ticket

1. `NEW` at `created_at`.
2. `QUEUED` after a classification delay of 2-30 seconds.
3. `ASSIGNED` when routing succeeds (immediately if an agent is eligible). Writes an `ASSIGN` event.
4. First agent action: `IN_PROGRESS` and the first AGENT message at `assigned_at + first_response_delay`.
5. Optional `PENDING_CUSTOMER` and back (7.4).
6. Optional transfers and escalations (7.5, 7.6).
7. `RESOLVED`, then `CLOSED` 72 hours later, or `REOPENED` within 72 hours (7.7).

First-response delay (minutes after assignment): CHAT lognormal median 1.5, sigma 0.6. EMAIL lognormal median 15, sigma 0.8. PHONE uniform 0-1. P1 x 0.5. All x `(1 + 0.1 x agent_load_before)`.

### 7.3 Handle time

Resolution happens at `assigned_at + handle`, where `handle = max(lognormal(median, 0.5), first_response_delay + 1)`.

Median = `intent.median_handle_minutes x channel factor` (EMAIL 3.0, CHAT 1.0, PHONE 0.8). Incident tickets use median 15.

The time a ticket spends QUEUED does not count as handle time. Handle time restarts at each transfer or escalation with the remaining work (the original draw minus time already spent).

### 7.4 Pending customer

Probability of one pending episode: TECH_TROUBLESHOOTING 25%, REFUND_REQUEST 20%, SECURITY_BREACH 20%, ACCOUNT_ACCESS 15%, all other intents 8%. Starts at 40-60% of the handle time. Duration is lognormal median 45 min, sigma 0.7, **capped so the customer reply (or closure) occurs at least one minute before the owner's shift ends.** If fewer than 10 minutes of shift remain at the planned start, no episode occurs.

3% of episodes end with the customer silent: the agent resolves from PENDING_CUSTOMER with `resolution_code = NO_CUSTOMER_RESPONSE`.

Pending time still counts toward agent load (see 6.1) and does not pause the SLA clock (section 8).

### 7.5 Transfers

- **SKILL_REROUTE:** 4% of tickets per cycle, once per cycle, at a random point during handling. Target skill is drawn from the ticket category's related skills (BILLING <-> ACCOUNT, TECH <-> ACCOUNT, GENERAL -> any).
- **SHIFT_HANDOVER:** deterministic, from shift ends (6.4).
- Baseline rules per cycle: at most 1 SKILL_REROUTE, and no agent appears twice. SHIFT_HANDOVER is not capped (a long ticket can legitimately cross several shifts; the observed maximum is reported in the KPI summary).

Expected: 8-15% of tickets have at least one transfer.

### 7.6 Escalations

**SLA escalation.** For each cycle, the SLA record fixes `escalation_due_at`. Escalate when all hold:

- the cycle is not yet resolved at `escalation_due_at`,
- the ticket's owner tier is T1 or T2,
- no SLA escalation has happened in this cycle.

Timing: the escalation event fires at the first instant `>= escalation_due_at` at which the ticket is in ASSIGNED or IN_PROGRESS. If it is PENDING_CUSTOMER, the escalation waits until the ticket returns to IN_PROGRESS. If the ticket is still QUEUED (unowned) at that instant, it is not escalated (see section 16).

Sequence: status `-> ESCALATED` at that instant (actor SYSTEM, previous owner releases load), then `ASSIGNED` plus an `ESCALATE` AssignmentEvent when the tier+1 agent is found (`wait_minutes` measures the gap).

**Manual escalation.** ENTERPRISE 6% of tickets, others 2%. Occurs at a random point after first response, at most one per cycle, same mechanics, `reason_code = MANUAL_ESCALATION`. Not subject to the timing rule.

### 7.7 Resolution, closure, reopen

- Resolution code by intent: REFUND_REQUEST -> REFUNDED 60%, SOLVED 30%, INFO_PROVIDED 10%. PAYMENT_FAILURE -> REFUNDED 15%, SOLVED 70%, INFO_PROVIDED 15%. All others -> SOLVED 55%, INFO_PROVIDED 25%, WORKAROUND 10%, DUPLICATE 10%.
- Closure: `CLOSED` at exactly `resolved_at + 72h`, only if no reopen and `<= cutoff`.
- Reopen probability, computed per resolved cycle: `0.04 + 0.03 x resolution_breached + 0.015 x min(transfer_count, 3) + 0.02 x escalated`. Reopen delay is lognormal median 6 hours, truncated to `(0, 72h]`. At most 2 reopens per ticket.
- Reopen sequence: `RESOLVED -> REOPENED` (actor CUSTOMER) `-> QUEUED` (SYSTEM) `-> ASSIGNED` (REOPEN_ASSIGN). A new SLAEvent (suffix `-R1`, `-R2`) and a new cycle's Resolution follow.
- Reopen-cycle SLA targets: first response and resolution are 50% of the rule value (rounded up). Escalation timer is the rule value.

Expected reopen rate: 4-8% of tickets.

### 7.8 Mechanics added in Phase 3b (supersedes conflicting wording in 6.3, 6.6, 7.4-7.7)

1. **Waiting spells.** A waiting spell is an uninterrupted period in status QUEUED or ESCALATED. The language waiver (6.3) activates when one spell reaches the priority threshold and then holds for the rest of the cycle.
2. **Active skill and queue.** The active skill is `intent.required_skill_id` and the active queue is `ticket.queue_id`, until a SKILL_REROUTE; after it, the active skill is the reroute skill and the active queue is that skill's SKILL queue, for the rest of the cycle. A reopen cycle resets both. H3, H3b and H6 are evaluated against the active skill and the event's queue (QUE-ESCALATION for ESCALATE, otherwise the active queue). AssignmentEvent `required_skill_id` and `queue_id` record them.
3. **One mid-handling event per cycle.** At cycle start, draw in this order from fixed positions of the per-cycle draw vector: (a) PENDING with the intent's pending probability (7.4); else (b) SKILL_REROUTE with 4%, only if the active skill is BILLING, TECH, ACCOUNT or GENERAL, the priority is not P1 and the ticket has no incident_id; else (c) MANUAL_ESCALATION with 6% for ENTERPRISE and 2% for others; else none. The effective reroute rate is therefore about 2.8%.
   The event is planned at every assignment of the cycle until it occurs: `planned_at = max(pickup_at + 60 s, t + u x R)`, where `t` is the assignment time, `R` the remaining work, and `u` is drawn once per cycle (uniform: pending 0.4-0.6, reroute 0.2-0.8, manual 0.3-0.9). It is valid only if `planned_at` is before the scheduled resolution; otherwise it is skipped for this assignment and retried at the next. A planned event whose epoch is stale is discarded.
   Reroute target (drawn once per cycle): BILLING -> ACCOUNT; TECH -> ACCOUNT; ACCOUNT -> BILLING or TECH with equal probability; GENERAL -> BILLING, TECH, ACCOUNT or SALES uniformly. A manual escalation planned while the owner is T3 is consumed with no effect.
4. **Pending customer.** At `planned_at`: IN_PROGRESS -> PENDING_CUSTOMER (actor AGENT, the owner). It is not planned if fewer than 10 minutes of the owner's shift remain. `reply_at = min(planned_at + D, owner_shift_end - 60 s)`, with D lognormal (median 45 min, sigma 0.7), at least 1 minute. Work does not accrue while pending. With probability 3% per episode the customer stays silent: PENDING_CUSTOMER -> RESOLVED at `reply_at` by the owner (resolution kind NO_CUSTOMER_RESPONSE). Otherwise PENDING_CUSTOMER -> IN_PROGRESS at `reply_at` (actor CUSTOMER), and `resolved_at = max(reply_at + remaining work, reply_at + 60 s)`. A pending ticket counts toward the owner's load.
5. **SKILL_REROUTE and MANUAL_ESCALATION mechanics.** At `planned_at` the ticket leaves the owner exactly as in a shift handover (release load, reassign in the same dispatch pass, or go to QUEUED/ESCALATED and wait). Reroute: TRANSFER, reason SKILL_REROUTE, active skill changes. Manual escalation: actor AGENT, same mechanics as SLA escalation below, reason MANUAL_ESCALATION.
6. **SLA escalation.** `escalation_due_at` comes from 8.2. The ESCALATION_DUE event is evaluated after the dispatch pass at its timestamp. If the cycle was resolved at or before the due instant, nothing happens. If the ticket is owned (ASSIGNED or IN_PROGRESS) and the owner's tier is T1 or T2, it escalates now. If the ticket is QUEUED, ESCALATED or still unassigned after dispatch, it never escalates in this cycle. If it is PENDING_CUSTOMER, the check is repeated right after the customer reply, and cancelled if the pending ends in a silent resolution.
   Escalation: status -> ESCALATED (actor SYSTEM, or AGENT for manual), the owner releases load, and the ticket waits in an ESCALATED spell. Dispatch assigns an agent whose tier is exactly the previous owner's tier + 1, who is a QUE-ESCALATION member, holds the active skill, satisfies language and capacity rules, and was not an owner earlier in the cycle (H7). The ESCALATE event has `from_agent_id`, queue QUE-ESCALATION, and status ESCALATED -> ASSIGNED. Remaining work carries over. At most one SLA escalation and one manual escalation per cycle. Dispatch and escalation checks repeat at the same timestamp until nothing changes.
7. **Reopen.** The probability is evaluated per resolved cycle (7.7), using that cycle's `resolution_breached` (from the cycle's `resolution_due`), its TRANSFER count and whether it had an ESCALATE event. Delay: lognormal (median 6 h, sigma 0.8) truncated to (0, 72 h] by inverse-CDF sampling (`statistics.NormalDist`), whole seconds, at least 1 s. A reopen whose time falls after the cutoff is not emitted and the ticket stays RESOLVED. A reopened ticket never gets a CLOSED event for the earlier cycle. Reopen-cycle work median is 50% of the ticket's normal median. Cycle k > 1 uses RNG module name `handle_times_c<k>`. Cycle 1 keeps `handle_times`, with existing draw positions unchanged.
8. **Event order at one timestamp.** RESOLVE and silent resolve, SHIFT_END, SHIFT_START, PENDING_REPLY, FALLBACK_DUE, PICKUP, MID_EVENT (pending start, reroute, manual escalation), REOPEN, ENQUEUE, then DISPATCH, then ESCALATION_DUE, then repeat DISPATCH and ESCALATION_DUE until no change. Existing relative ordering from Phase 3a is preserved.

## 8. SLA computation

All SLA values are derived from source records by one shared module (`generator/sla.py`). The validator imports the same functions to recompute them.

### 8.1 Calendar functions

- `is_open(cal, t)`: true if `t` falls in a half-open window of `cal` on its weekday.
- `next_open(cal, t)`: `t` if open, else the next window start.
- `add_business_minutes(cal, start, m)`: advance from `start`, consuming only open time. `m = 0` returns `start`.
- `business_minutes(cal, a, b)`: total open time in `[a, b)`; 0 if `b <= a`.
- Boundary rule: if the minutes run out exactly at a close time, `add_business_minutes` returns the close instant and does not roll forward to the next open. A response at exactly the due time is not a breach.

### 8.2 Per cycle

| Quantity | Formula |
|---|---|
| Cycle start | `created_at` (cycle 1) or the REOPENED event time |
| Calendar | `CAL-247` if the cycle's priority is P1 (rule override), else the channel's calendar |
| `clock_start` | `next_open(calendar, cycle_start)` |
| `first_response_due` | `add_business_minutes(calendar, clock_start, first_response_target_min)` |
| `resolution_due` | `add_business_minutes(calendar, clock_start, resolution_target_min)` |
| `escalation_due_at` | `add_business_minutes(calendar, clock_start, escalation_after_min)` |
| `first_response_at` | `sent_at` of the cycle's first AGENT message (system acknowledgements do not count) |
| `frt_breached` | `first_response_at > first_response_due`; if null, `cutoff > first_response_due` |
| `resolution_breached` | `resolved_at > resolution_due`; if null, `cutoff > resolution_due` |

A response exactly at the due time is **not** a breach. The SLA clock does not pause during PENDING_CUSTOMER (section 16).

Boundary cases that tests must cover: arrival exactly at open, exactly at close, due time landing exactly on close, weekend rollover for EMAIL and PHONE, and a P1 ticket on a weekend.

## 9. Messages

- Message 1 is the customer's opening message at `created_at`, seq 1, no reply link.
- EMAIL tickets also get a SYSTEM auto-acknowledgement 5-60 seconds after message 1 (seq 2). It does not count as a first response.
- Each cycle has at least one AGENT message (the first response, at the IN_PROGRESS transition). Agent and customer follow-ups alternate in the thread. `in_reply_to_message_id` points to the latest message from the other party. SYSTEM notes reply to the message that triggered them.
- A SYSTEM note is written at each escalation and at the resolution of an escalated ticket only. The 100,000 message target would otherwise be exceeded by the base count.
- The sender of every AGENT message must be the ticket's owner at that time (checked by the validator).
- The language of an agent message is decided per assignment event; a LANGUAGE_FALLBACK assignment writes in en, and a later owner fluent in the ticket language writes in that language.
- Bodies come from a template library keyed by `(intent, sender_type, language)` and filled with synthetic values. Templates are short and neutral. Non-English templates are machine-drafted and human-reviewed once (see section 16).

**Count calibration.** Natural counts from the rules above give a mean near 3.3 messages per ticket. The total must equal `--messages` exactly. A calibration pass adds or removes optional customer/agent follow-up pairs on tickets (chosen by seeded order, never touching seq 1, first responses, or required notes) until the total matches. If the natural total is more than 10% away from the target, the generator fails with an error instead of distorting the data.

## 10. Customer satisfaction

Each resolved cycle sends a survey. Response probability: CHAT 45%, PHONE 35%, EMAIL 30% (overall about 30-40%).

If the customer responds:

```
latent = 4.3
       - 0.9 x frt_breached
       - 0.9 x resolution_breached
       - 0.25 x transfer_count
       - 0.35 x (escalation_count > 0)
       - 0.5 x (agent language mismatch on the cycle's first assignment)
       - 0.5 x (cycle_no > 1)
       + Normal(0, 0.6)
csat_score = clamp(round(latent), 1, 5)
```

`csat_submitted_at` = min(`resolved_at` + lognormal(median 30 min, sigma 1.0), cutoff). Expected mean CSAT: 3.6-4.1.

## 11. Anomaly injection

Anomalies are injected **after** the valid baseline is complete and validated, on in-memory tables before final ID sequencing and export. The rules for all five:

- Touch the minimum number of rows.
- Re-derive every dependent derived field (loads, breach flags, reopen_count, current_status) with the same functions the baseline used.
- Make the anomalous rows consistent with everything except the single violated rule, so only the intended validator catches them.
- Write each injected row to `ground_truth.csv`. Rows that became violations only as a consequence (cascade) are written with `is_primary = false`.
- Put no flag, marker or special value on the affected rows themselves.

Count per type = `max(3, round(rate x tickets))`. At 30,000 tickets that is about 300 injected cases (about 1%).

| issue_type | Rate | How it is injected | Cascade | Detection rule |
|---|---|---|---|---|
| AGENT_OVER_CAPACITY | 0.20% (60) | Pick an ASSIGN event where the best eligible agent already has `load_before = limit`. Reassign the event to that agent and recompute loads. Choose cases where the ticket's open interval is short. | Later assignments at the same agent that now exceed the limit are labelled non-primary | V-ROUT-CAP-01 |
| SLA_IGNORES_CALENDAR | 0.33% (100) | Pick non-P1 tickets whose clock start or due time crosses a closed period. Recompute `first_response_due`, `resolution_due` and `escalation_due_at` using wall-clock minutes from `created_at`, and recompute breach flags from them. `calendar_code_used` still shows the channel calendar. | none | V-SLA-01 |
| REOPEN_LOSES_HISTORY | 0.27% (80) | Pick a ticket with `reopen_count >= 1`. Delete the previous cycle's AssignmentEvents, SLAEvent and Resolution rows. The status events and messages are kept. | none | V-HIST-01 |
| TRANSFER_LOOP | 0.17% (50) | Pick a ticket with a handle time of at least 60 minutes and two eligible agents A and B. Insert A -> B -> A -> B transfers (reason SKILL_REROUTE) 5-20 minutes apart inside the handling period, with loads and status events re-derived. | none | V-LOOP-01 |
| PRIORITY_CONTRADICTS_SAFETY | 0.13% (40) | Pick safety-intent tickets. Set `priority_level = P2` and `priority_reason_code = INTENT_DEFAULT`; re-derive `priority_rule_id` and the SLAEvent targets and due times. Queue and assignments are left as they were. | none | V-SAFE-01 |

Eligibility notes: AGENT_OVER_CAPACITY and TRANSFER_LOOP candidates must not be reopened tickets, to keep cascades predictable. At small scales the generator chooses fewer candidates only if none are available, and reports that in the validation summary.

## 12. Ground-truth files

| File | Content | Used by |
|---|---|---|
| `data/ground_truth/ground_truth.csv` | One row per injected anomaly (primary and cascade) | `validate --mode evaluate` only |
| `data/ground_truth/ground_truth_decisions.csv` | Expected priority, queue, rule and SLA due times for every ticket, computed from the baseline before injection | Evaluation and tests only |

Neither file is read when generating the input tables after injection, and neither is in `data/raw/`. A test asserts that no ground-truth column or ID list appears in any input table.

## 13. Validation catalog

The validator has two modes. `--mode integrity` runs every check on the input tables alone, with no ground truth. `--mode evaluate` also reads ground truth and scores detection.

| ID | Check | Severity |
|---|---|---|
| V-PK-01 | Primary keys unique in every table, including composite keys | error |
| V-FK-01 | Every foreign key resolves (all FKs listed in data_dictionary.md section 22) | error |
| V-NULL-01 | Required and conditional fields present as defined | error |
| V-ENUM-01 | Every enum value is allowed | error |
| V-ID-01 | IDs match their pattern; ticket numbers 1..N gapless; per-ticket sequences gapless | error |
| V-DUP-01 | No duplicate customer emails, no duplicate (ticket, seq) or (ticket, cycle) pairs, no duplicate message bodies with identical ticket, sender and timestamp | error |
| V-DATE-01 | Date ordering: created <= queued <= assigned <= first response <= resolved <= closed; message and event times >= ticket created_at; due >= clock_start; nothing after cutoff | error |
| V-ARITH-01 | Reconciliation: reopen_count = REOPENED events = max cycle_no - 1; current_status and last_status_at = last status event; transfer_count and escalation_count match events; load_after = load_before + 1; total messages = target; volume weights sum to 100 | error |
| V-LIFE-01 | Every status transition is allowed; one open cycle at a time; no events after CLOSED; cycle_no increases only at REOPENED | error |
| V-ROUT-SKILL-01 | Assigned agent holds `required_skill_id` on event date; P1 and P2 need PROFICIENT or better | error |
| V-ROUT-LANG-01 | Language match, unless the cycle's waiver was active at event time (the ticket waited at least the threshold in a QUEUED spell earlier in this cycle). | error |
| V-ROUT-CAP-01 | Replay assignments and releases; `load_before` equals the replayed value and `load_after <= concurrency_limit` | error |
| V-ROUT-SHIFT-01 | Agent on shift at event time; INACTIVE agents never assigned | error |
| V-ROUT-TIER-01 | Tier and queue-membership constraints; ESCALATE goes to exactly tier + 1 | error |
| V-LOOP-01 | No agent appears twice among owners within one ticket cycle | error |
| V-SLA-01 | Recompute clock_start and all three due times from calendars and the effective rule; must match | error |
| V-SLA-02 | Recompute breach flags and business-minute figures; must match | error |
| V-SLA-03 | `calendar_code_used` = CAL-247 for P1, else the channel's calendar | error |
| V-ESC-01 | SLA_ESCALATION fires at the first eligible instant >= escalation_due_at; every cycle that should escalate does | error |
| V-HIST-01 | Assignment history complete: every status transition into ASSIGNED has an AssignmentEvent at that time; `from_agent_id` chains to the previous owner; every cycle that reached ASSIGNED has events, an SLAEvent, and (if RESOLVED) a Resolution; every earlier cycle of a reopened ticket still exists | error |
| V-SAFE-01 | Safety rule SR-1: safety intent implies P1 | error |
| V-PRIO-01 | Recompute priority and rule from section 5; must match | error |
| V-GT-01 | Evaluate mode: every ground-truth entity exists; no ground-truth data in input tables | error |

Warnings (never block): the distribution targets in section 14.

**Acceptance criteria.**

1. With `--no-anomalies`, `--mode integrity` reports 0 errors.
2. With anomalies, every error maps to a ground-truth row (precision 1.0), and every ground-truth row is reported by its `detection_rule` (recall 1.0).
3. The mapped acceptance criteria are: routing respects skill and capacity (V-ROUT-*), SLA reproducible (V-SLA-*), escalation timing correct (V-ESC-01), assignment history complete (V-HIST-01).

## 14. KPIs and calibration targets

The generator writes `docs/validation_summary.md` and `data/kpi_summary.json`. Targets are tuned through `config.py` constants and checked as warnings.

| Metric | Target |
|---|---|
| Tickets / agents / messages | 30,000 / 100 / 100,000 (exact) |
| After-hours arrivals | 8-20% |
| P1 share | 4-8% |
| Language fallback | 5-12% |
| Tickets with >= 1 transfer | 10-20% |
| Tickets with an SLA escalation | 2-6% of cycles |
| Reopen rate | 4-8% |
| First-response breach rate | 2-6% |
| Resolution breach rate | 1-5% |
| Survey response rate | 30-40% |
| Mean CSAT | 3.6-4.1 |
| Mean agent utilisation (time-weighted load / limit while on shift) | 10-25% (the volume-to-capacity ratio is fixed by 100 agents and 30,000 tickets) |
| Max queue wait during a spike | Visibly higher than the daily median (reported per spike window) |

Note that a P1 FRT breach of 30-50% results from incident bursts and T2+ scarcity, and that P3/P4 breaches are near zero because utilisation is low (staffing arithmetic, fixed volume and capacity).

KPI summary also reports SLA attainment and breach rate by channel and priority, median and 95th percentile wait, and per-intent volumes.

## 15. Reproducibility

- One `numpy.random.SeedSequence(20261022)` is spawned into named child streams: `customers, agents, arrivals, intents, handle_times, pending, transfers, escalations, reopens, messages, csat, anomalies`. Changing one module therefore does not shift the random draws of another.
- Per-ticket randomness (handle time, delays, CSAT noise) comes from a child stream keyed by ticket number, so it does not depend on simulation order.
- No iteration over unordered sets or dicts without sorting. No wall-clock time, run date, hostname or absolute path appears in any output.
- Output files are sorted by primary key, UTF-8, `\n` line endings, fixed decimal places.
- `MANIFEST.json` lists row counts and SHA-256 checksums for every file. `scripts/verify_reproducibility.sh` generates twice into two folders and compares both.
- Starter and full datasets use the same generator and the same seed; only the scale parameters differ.

## 16. Known simplifications and limitations

- No holidays, no daylight saving, one time zone (UTC).
- The SLA clock never pauses for PENDING_CUSTOMER.
- Tickets waiting in a queue are not escalated by queue age (only owned tickets escalate). Waiting tickets are prioritised by SLA due time instead.
- No abandoned calls, no agent absences or breaks, no skill removals.
- Agent handle time is a wall-clock duration that holds one capacity slot; there is no per-channel capacity weighting.
- Non-English message templates are machine-drafted and only lightly reviewed.
- No external data is used. All records are synthetic (see `source_manifest.md`).
- The first six hours of 2026-01-05 have no agents on shift (there is no Sunday-night shift before the window), so a few tickets wait until 06:00
