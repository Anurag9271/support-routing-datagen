# Design Decisions, Assumptions, Trade-offs, and Limitations

This document captures the architectural trade-offs, modeling assumptions, known limitations, and concrete specification decisions established during the design and implementation of the YottaFlex omnichannel support-routing synthetic data generator.

---

## 1. Core Architecture & Trade-Offs

### 1.1 Deterministic Rule Engine Instead of Runtime LLM
- **Decision**: All tickets, routing assignments, status transitions, SLA calculations, and message threads are generated via deterministic state-machine simulation and templated language generation, rather than prompting an LLM at runtime.
- **Rationale**: Guarantees bit-for-bit reproducibility from seed `20261022`, runs without API keys or network calls, completes full 30,000-ticket generation in under 4 minutes, and strictly enforces relational schema and foreign key constraints without hallucinations.

### 1.2 Discrete-Event Simulation Engine
- **Decision**: Routing is modeled as an event-driven discrete simulation with a priority event queue advancing across the 90-day window (`2026-01-05` to `2026-04-05`).
- **Rationale**: Accurately reflects real-world contact center dynamics (shift changes, queue wait times, agent concurrency saturation, skill matching, after-hours arrivals, reopen cycles, and escalation handoffs).

### 1.3 Exact Business-Minute SLA Calendar
- **Decision**: SLA targets, elapsed durations, and due timestamps are computed using minute-level operating calendars (`CAL-247`, `CAL-PHONE`, `CAL-CHAT`, `CAL-EMAIL`).
- **Rationale**: Wall-clock calculation produces massive false SLA breaches during off-hours and weekends. Calendar arithmetic accurately models SLA clocks that only tick when channels are active.

### 1.4 Exact Message-Count Calibration Pass
- **Decision**: Natural simulation produces ~97,000 to ~99,000 messages. A targeted post-simulation calibration pass adds valid customer/agent conversation messages to open or multi-turn tickets to hit exactly 100,000 messages.
- **Rationale**: Meets the hard target of 100,000 messages while maintaining conversation coherence, timestamp order, cycle affiliation, and status event synchronization.

### 1.5 Clean Baseline with Post-Simulation Labelled Anomaly Injection
- **Decision**: The simulation generates a 100% valid, rule-compliant baseline dataset first. The five target anomalies are then injected in-memory into specific entities, and tracked in an isolated `GroundTruth` table.
- **Rationale**: Clean separation ensures that baseline business rules are never relaxed to accommodate anomalies. Ground truth labels reflect exact injections with verified primary and cascade relationships.

### 1.6 Minimal Injected Anomalies (Rule Isolation)
- **Decision**: Anomaly injectors are constrained to select candidate tickets and agents that satisfy all other rules (H1–H7: active status, valid shift, skill proficiency, queue membership, and tier).
- **Rationale**: Ensures each injected anomaly triggers strictly its designated detection rule (`V-ROUT-CAP-01`, `V-SLA-01`, `V-HIST-01`, `V-LOOP-01`, `V-SAFE-01`) without generating unlabelled side-effect false positives (e.g., `V-ROUT-SKILL-01`, `V-ROUT-SHIFT-01`, `V-LIFE-01`).

---

## 2. Key Specification Decisions Made During Development

1. **After-Hours Calibration via Channel Operating Calendar (Decision 1)**:
   - *Decision*: A ticket is marked `is_after_hours_arrival = True` if its arrival timestamp falls outside the operating window of its channel's specific SLA calendar (`CAL-PHONE`, `CAL-CHAT`, or `CAL-EMAIL`).
   - *Result*: Produces an after-hours arrival rate of ~11.1%, fitting cleanly within the accepted 8%–20% operational range.

2. **Shift Handover vs Concurrency Limit Contradiction (Decision 2)**:
   - *Decision*: When an agent's shift ends while handling a ticket, the handover to the next on-shift agent occurs only if the receiving agent has available concurrency capacity (`agent_load < concurrency_limit`). If no capacity is available, the ticket queues in its primary queue.
   - *Rationale*: Resolves the contradiction where mandatory handover would force receiving agents over capacity, creating unlabelled concurrency violations.

3. **Per-Spell Language Waiver Tracking (Decision 3)**:
   - *Decision*: Language matching waivers (allowing routing to an agent without the customer's language) are calculated per queuing spell in the current cycle, not cumulatively across the entire ticket lifecycle.
   - *Rationale*: A customer who waited 30 minutes in cycle 1 should not automatically have language requirements waived instantly upon reopening in cycle 2.

4. **Window Cutoff Adjusted to `2026-04-05T00:00:00Z` (Decision 4)**:
   - *Decision*: The 90-day simulation window begins on Monday `2026-01-05T00:00:00Z` and ends on Sunday `2026-04-05T00:00:00Z` (exactly 90 full days, 12 full weeks).
   - *Rationale*: Avoids partial-week cutoff artifacts and maintains consistent weekly shift rotations.

5. **SYSTEM Resolution Notes Capped to Escalated Tickets (Decision 5)**:
   - *Decision*: Internal SYSTEM notes are generated for SLA and tier escalations rather than on every resolved ticket.
   - *Rationale*: Generating notes on all 30,000 tickets would inflate message volume beyond the 100,000 target.

6. **CSAT Submission Timestamps Capped at Cutoff (Decision 6)**:
   - *Decision*: Survey response timestamps (`csat_submitted_at`) for tickets resolved shortly before the cutoff date are constrained to `min(resolved_at + response_delay, cutoff)`.
   - *Rationale*: Prevents future-dated CSAT timestamps beyond the simulation observation window.

7. **Relaxed KPI Targets Explained by Staffing Arithmetic (Decision 7)**:
   - *Decision*: Maintain realistic routing simulation without artificial inflation or parameter overfitting.
   - *Rationale*: 100 agents handling 30,000 tickets over 90 days with 8-hour shifts yields an average agent utilization of ~48% and an SLA breach rate of ~3.6%, reflecting realistic staffing for peak-hour coverage.

---

## 3. Assumptions

1. **Customer Base**: All customers were registered before the simulation window start date (`2026-01-05`).
2. **First Contact**: Every ticket begins with an initial customer message (`seq_no=1`, `sender_type=CUSTOMER`).
3. **Queue Capacities**: Queues have infinite FIFO buffering capacity; agents pull from queues based on priority and wait time.
4. **Resolution Finality**: Once a ticket reaches `CLOSED`, no further events, status transitions, or messages may occur.

---

## 4. Known Limitations

1. **Holidays & Daylight Saving Time**: Simulation calendar operates on standard UTC business hours without regional public holidays or DST clock shifts.
2. **SLA Pausing**: SLA clocks do not pause during `PENDING_CUSTOMER` status.
3. **Age-Based Escalation**: Queued tickets that are not assigned do not automatically escalate by age alone unless reaching the SLA escalation threshold.
4. **Agent Absences**: Agent availability is determined strictly by scheduled shifts; unscheduled sick leave and unplanned absences are not modeled.
5. **Initial Window Ramp-Up**: The first 6 hours of `2026-01-05` (00:00–06:00 UTC) have no agents on shift (first shift begins at 06:00 UTC), so tickets arriving in this window queue until morning shifts open.
6. **Weekend Support Coverage**: Specialized queues (`QUE-SALES` and `QUE-SECURITY`) operate Monday–Friday; weekend arrivals remain queued until Monday 08:00 UTC.
7. **Multilingual Templates**: Non-English customer messages and responses use realistic synthetic domain-specific translation templates.
