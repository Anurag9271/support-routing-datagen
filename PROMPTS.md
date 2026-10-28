# Prompt History

Format: each entry has the tool, the prompt (or a faithful summary), what it produced, and what I changed or validated afterward.

## Entry 1: Claude: understand the task
- **Prompt:** Uploaded the task file; asked for an explanation of what the project requires.
- **Produced:** Plain-language breakdown of the deliverables (generator, data dictionary, anomalies, ground truth, validation, reproducibility).
- **My review/changes:** Verified scope against project expectations and established ground rules for determinism and no LLM runtime calls.

## Entry 2: Claude: schema / ER design
- **Prompt:** Asked for the full schema and ER design first.
- **Produced:** 13 required entities plus bridge, calendar and status-history tables; lifecycle states; derived-value rules; anomaly attachment points; 5 open design decisions.
- **My review/changes:** Refined entity relationships, primary key conventions, foreign key references, and ensured complete coverage of data dictionary specifications.

## Entry 3: Claude: process planning
- **Prompt:** Asked what to do before moving to coding (using Antigravity).
- **Produced:** Pre-coding checklist, repo structure, phased build plan.
- **My review/changes:** Enforced strict phased implementation order (Phases 1 through 4) with independent checker scripts.

## Entry 4: Claude: submission guidelines
- **Prompt:** Shared the YottaFlex guidelines PDF.
- **Produced:** Corrected note that prompt history is mandatory; added AI_USAGE / DESIGN_DECISIONS plan.
- **My review/changes:** Adopted documentation structure and validation criteria.

## Entry 5: Claude: repo scaffold
- **Prompt:** Asked to begin folder creation.
- **Produced:** This folder structure and stub files.
- **My review/changes:** Reviewed directory tree, gitignore entries, and configuration constants.

## Entry 6: Antigravity: Phase 1 Master Data and Customer Directory
- **Prompt:** (exact text not retained in this log; summarised)
  Implement Phase 1 generating all static configuration tables, skills, queues, priority rules, calendars, agents, shift schedules, and customer directory according to business rules.
- **Produced:** `generator/master_data.py`, customer synthesis routines, and Phase 1 unit test suite.
- **My review/changes:** Checked foreign key integrity and verified shift coverage across all 7 days of the week.

## Entry 7: Antigravity: Phase 2 Scenario Windows, Arrivals, and Ticket Drafts
- **Prompt:** (exact text not retained in this log; summarised)
  Generate scenario incident and volume spike windows, Poisson arrival timestamps across 90 days, customer ticket draft attributes, intent routing codes, and initial priorities.
- **Produced:** `generator/windows.py`, `generator/arrivals.py`, `generator/tickets.py`, and draft ticket generation pipeline.
- **My review/changes:** Validated arrival rate multipliers during incident and spike periods; confirmed after-hours arrival distribution.

## Entry 8: Antigravity: Phase 3a & 3b Discrete-Event Routing Simulation
- **Prompt:** (exact text not retained in this log; summarised)
  Build the event-driven discrete simulation engine handling priority queueing, agent shift handoffs, skill and tier matching, language waivers, and escalation paths.
- **Produced:** `generator/simulation.py` with priority queue event loop, assignment events, and ticket lifecycle state transitions.
- **My review/changes:** Diagnosed shift handover vs concurrency limit contradiction; resolved per-spell language waiver accounting.

## Entry 9: Antigravity: Phase 3c Relational Tables & Message Calibration
- **Prompt:** (exact text not retained in this log; summarised)
  Transform simulation event streams into final relational tables (Ticket, Message, SLAEvent, Resolution) with exact 100,000 message calibration and SLA calendar calculations.
- **Produced:** `generator/final_tables.py`, `generator/sla_calendar.py`, and reconciliation sanity checks in `generator/reconcile.py`.
- **My review/changes:** Approved exact message calibration pass; confirmed CSAT survey submission timestamp bounds.

## Entry 10: Antigravity: Phase 4 Anomaly Injection & Ground Truth
- **Prompt:** (exact text not retained in this log; summarised)
  Implement the 5 target anomaly injectors (over-capacity, calendar bypass, history loss, transfer loops, safety priority contradict) and isolated `GroundTruth` table export.
- **Produced:** `generator/anomalies.py`, anomaly calculation target formulas, and ground truth schema exporter.
- **My review/changes:** Confirmed zero anomaly leakage into raw tables and verified primary and cascade label separation.

## Entry 11: Antigravity: Validation Module and Rule Optimization
- **Prompt:** (exact text not retained in this log; summarised)
  Build standalone validator executing all 23 catalog rules independently without shared generator code, with evaluate mode (precision/recall against ground truth) and integrity mode.
- **Produced:** `validation/validate.py` with dictionary-indexed lookup optimizations achieving sub-15 second validation runtime.
- **My review/changes:** Caught validator enum and date lookup defects on small clean dataset; confirmed 0 baseline findings.

## Entry 12: Antigravity: Minimal Injectors and Packaging
- **Prompt:** (exact text not retained in this log; summarised)
  Refine anomaly injectors so that each anomaly triggers only its intended detection rule without unlabelled side-effect findings. Generate manifest, verify reproducibility, and prepare documentation.
- **Produced:** Constrained agent selection in `generator/anomalies.py`, `verify_reproducibility.ps1`/`.sh`, `MANIFEST.json`, `data/sample/`, and updated submission docs.
- **My review/changes:** Verified 1.0 precision and recall across all five anomaly types and confirmed 100% SHA-256 reproducibility.
