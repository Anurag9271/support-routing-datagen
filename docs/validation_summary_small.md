# Validation Summary Report

## 1. Run Metadata

- **Random Master Seed**: `20261022`
- **Dataset Source**: `data/raw`

| Table | Row Count |
|---|---:|
| `Agent` | 30 |
| `AgentLanguage` | 50 |
| `AgentShift` | 150 |
| `AgentSkill` | 70 |
| `AssignmentEvent` | 1,190 |
| `Channel` | 3 |
| `Customer` | 400 |
| `Intent` | 12 |
| `Language` | 5 |
| `Message` | 4,400 |
| `PriorityRule` | 8 |
| `Queue` | 7 |
| `QueueMember` | 83 |
| `Resolution` | 1,030 |
| `SLACalendar` | 4 |
| `SLACalendarWindow` | 24 |
| `SLAEvent` | 1,037 |
| `Skill` | 6 |
| `Ticket` | 1,000 |
| `TicketStatusEvent` | 6,732 |

## 2. Validation Catalog Results

| Rule ID | Description | Findings | Status |
|---|---|---:|---|
| `V-ARITH-01` | Reconciliation: status match, counts, loads, volume weights | 0 | PASS |
| `V-DATE-01` | Date ordering and cutoff (2026-04-05T00:00:00Z) | 0 | PASS |
| `V-DUP-01` | No duplicate emails, sequences, cycles, or message bodies | 0 | PASS |
| `V-ENUM-01` | Every enum value is allowed | 0 | PASS |
| `V-ESC-01` | SLA_ESCALATION timing and firing logic | 0 | PASS |
| `V-FK-01` | Every foreign key resolves (all FKs listed in data_dictionary.md 22) | 0 | PASS |
| `V-GT-01` | Ground-truth entity integrity in evaluate mode | 0 | PASS |
| `V-HIST-01` | Assignment history complete across all cycles | 3 | PASS |
| `V-ID-01` | IDs match their pattern; ticket numbers 1..N gapless | 0 | PASS |
| `V-LIFE-01` | Lifecycle transitions follow allowed business rules | 2 | FAIL |
| `V-LOOP-01` | No agent appears twice among owners within one cycle | 3 | PASS |
| `V-NULL-01` | Required and conditional fields present as defined | 0 | PASS |
| `V-PK-01` | Primary keys unique in every table, including composite keys | 0 | PASS |
| `V-PRIO-01` | Priority level and rule match classification rules | 0 | PASS |
| `V-ROUT-CAP-01` | Replay assignments; load_after <= concurrency_limit | 3 | PASS |
| `V-ROUT-LANG-01` | Language match unless cycle waiver was active | 6 | FAIL |
| `V-ROUT-SHIFT-01` | Agent on shift at event time; INACTIVE never assigned | 8 | FAIL |
| `V-ROUT-SKILL-01` | Assigned agent holds skill; P1/P2 require PROFICIENT+ | 2 | FAIL |
| `V-ROUT-TIER-01` | Tier and queue membership; ESCALATE goes to tier + 1 | 2 | FAIL |
| `V-SAFE-01` | Safety rule SR-1: safety intent implies P1 | 3 | PASS |
| `V-SLA-01` | Recompute clock_start and due times from calendars and rule | 3 | PASS |
| `V-SLA-02` | Recompute breach flags and business-minute figures | 0 | PASS |
| `V-SLA-03` | calendar_code_used = CAL-247 for P1, else channel calendar | 0 | PASS |

## 3. Baseline Checks Intentionally Violated by Anomalies

The following 5 validation rules are deliberately triggered by labelled anomalies injected in Phase 4:
1. **`V-ROUT-CAP-01`**: Violated by `AGENT_OVER_CAPACITY` (assigned agent exceeds concurrency limit).
2. **`V-SLA-01`**: Violated by `SLA_IGNORES_CALENDAR` (due times calculated ignoring SLA calendar windows).
3. **`V-HIST-01`**: Violated by `REOPEN_LOSES_HISTORY` (cycle 1 history deleted on reopened tickets).
4. **`V-LOOP-01`**: Violated by `TRANSFER_LOOP` (transfer loops assign agent twice in same cycle).
5. **`V-SAFE-01`**: Violated by `PRIORITY_CONTRADICTS_SAFETY` (safety-intent tickets downgraded to P2).

## 4. Evaluation Performance (Precision, Recall & F1)

### Summary Metrics

- **Overall (All Rows)**: Precision = `0.4286` | Recall = `1.0000` | F1 = `0.6000`
- **Primary Rows Only**: Precision = `0.4286` | Recall = `1.0000` | F1 = `0.6000`

### Per-Issue-Type Breakdown

| Issue Type | Injected (GT) | TP | FP | FN | Precision | Recall | F1 Score |
|---|---:|---:|---:|---:|---:|---:|---:|
| `AGENT_OVER_CAPACITY` | 3 | 3 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| `PRIORITY_CONTRADICTS_SAFETY` | 3 | 3 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| `REOPEN_LOSES_HISTORY` | 3 | 3 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| `SLA_IGNORES_CALENDAR` | 3 | 3 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| `TRANSFER_LOOP` | 3 | 3 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
