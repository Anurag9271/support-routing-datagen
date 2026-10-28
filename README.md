# YottaFlex Support-Routing Synthetic Data Generator

A deterministic, high-performance synthetic data generator and validation suite for an omnichannel customer support routing system. Simulates 90 days of realistic contact center operations across chat, email, and voice channels, generating tickets, multi-turn message threads, agent assignments, SLA tracking with operating calendars, lifecycle state transitions, and isolated labelled anomalies for benchmarking routing and audit algorithms.

---

## 1. Project Overview

The YottaFlex generator models an enterprise support operation spanning **90 days** (`2026-01-05T00:00:00Z` to `2026-04-05T00:00:00Z`) with:
- **30,000 support tickets** with realistic Poisson arrivals, volume spikes, and major service outage incidents.
- **100 human agents** across 3 tiers (T1, T2, T3) working scheduled 8-hour rotating shifts with strict concurrency limits (3 to 5 simultaneous tickets).
- **100,000 conversation messages** exactly calibrated across English, Spanish, Hindi, Arabic, and French.
- **13 core relational entities** plus 7 supporting calendar, shift, queue membership, and bridge tables (20 relational tables total).
- **Deterministic simulation**: Bit-for-bit identical outputs from master seed `20261022` using isolated NumPy RNG streams.
- **Zero external dependencies at runtime**: No network access, no API keys, and no LLM inference during generation.

---

## 2. Requirements & Environment

- **Python Version**: Python 3.12.x tested (requires 3.12+).
- **Operating System**: Tested on Windows 11 with PowerShell; Linux and macOS supported via bash scripts.
- **Core Dependencies** (from `requirements.txt`):
  - `pandas>=2.0` (tested with 3.1.0)
  - `numpy>=1.24` (tested with 2.5.4)
  - `pytest>=7.4` (tested with 9.1.1)
- **Hardware & Runtime Estimates**:
  - **RAM**: ~2 GB peak memory usage.
  - **Disk Space**: ~150 MB for full dataset export (CSVs, JSON, and SQLite `seed.sql`).
  - **Generation Runtime**: ~3.5 minutes for default scale (30,000 tickets, 100k messages).
  - **Validator Runtime**: ~15 seconds for complete 23-rule validation catalog on 30,000 tickets.

---

## 3. Installation

### PowerShell (Windows)
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Bash (Linux / macOS)
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

---

## 4. Quickstart

### Small-Scale Run (Starter Scale)
Generate a 300-ticket starter dataset, validate it, and run unit tests in 3 quick commands:

```bash
# 1. Generate small dataset
python -m generator --seed 20261022 --tickets 300 --agents 20 --customers 120 --messages 1000 --out data/_quick

# 2. Run validator in evaluate mode against ground truth
python -m validation.validate --mode evaluate --data data/_quick --ground-truth data/_quick/ground_truth/ground_truth.csv

# 3. Run fast test suite
python -m pytest -q -m "not slow"
```

### Full-Scale Generation (Default: 30,000 Tickets)
```bash
python -m generator --seed 20261022
```

### Clean Baseline Generation (No Anomalies)
```bash
python -m generator --seed 20261022 --no-anomalies --out data/_clean
```

---

## 5. CLI Options & Defaults

All options for `python -m generator`:

| Argument | Type | Default | Description |
|---|---|---|---|
| `--seed` | `int` | `20261022` | Master random seed for deterministic generation. |
| `--tickets` | `int` | `30000` | Total ticket count to simulate. |
| `--agents` | `int` | `100` | Total agent workforce headcount. |
| `--customers` | `int` | `None` (`tickets * 0.4`) | Unique customer pool size (12,000 at default scale). |
| `--messages` | `int` | `None` (`100000` at 30k) | Exact target message count (calibrated post-simulation). |
| `--days` | `int` | `90` | Simulation arrival window in days. |
| `--out` | `str` | `data/raw` | Output directory for generated dataset files. |
| `--no-anomalies` | `flag` | `False` | Disables Phase 4 anomaly injection, producing clean baseline. |
| `--formats` | `str` | `csv,json,sql` | Export formats: `csv` (required), `json`, and SQLite `sql`. |

---

## 6. Validation Suite

The standalone validator (`validation/validate.py`) shares zero code with the generator and verifies all 23 rules from the business rules validation catalog.

### Evaluation Mode (Benchmarking Anomaly Detection)
Evaluates findings against `ground_truth.csv`, calculating Precision, Recall, and F1 per anomaly type:
```bash
python -m validation.validate --mode evaluate --data data/raw --ground-truth data/ground_truth/ground_truth.csv --out docs/validation_summary.md
```
*Expected Result on Default Scale*:
- **Overall Precision**: `1.0000`
- **Overall Recall**: `1.0000`
- **F1 Score**: `1.0000`

### Integrity Mode (Audit Clean Baseline)
Audits a dataset for compliance; expects **0 findings** on clean data:
```bash
python -m validation.validate --mode integrity --data data/_clean
```
*Expected Result on Clean Data*: `[INTEGRITY PASS] 0 findings across all 23 rules.`

---

## 7. Testing & Reproducibility Verification

### Automated Reproducibility Scripts
Generates the dataset twice into temporary directories, performs SHA-256 hash comparison across all files in `MANIFEST.json`, runs the validator on both, and reports `PASS` or `FAIL`:

```powershell
# PowerShell (Windows)
powershell -ExecutionPolicy Bypass -File scripts/verify_reproducibility.ps1

# Optional full 30,000-ticket verification:
powershell -ExecutionPolicy Bypass -File scripts/verify_reproducibility.ps1 -full
```

```bash
# Bash (Linux / macOS)
bash scripts/verify_reproducibility.sh
bash scripts/verify_reproducibility.sh --full
```

### Pytest Test Suite
Tests are categorized by speed using pytest markers:
```bash
# Run fast test suite (59 tests, ~26 seconds)
python -m pytest -q -m "not slow"

# Run full test suite including slow full-scale fixtures
python -m pytest -q
```

---

## 8. Repository Layout & Outputs

```
support-routing-datagen/
├── generator/                 # Discrete-event simulation and export engine
│   ├── __main__.py            # CLI entry point
│   ├── config.py              # Configuration dataclass and defaults
│   ├── master_data.py         # Static configuration, skills, queues, calendars, agents
│   ├── arrivals.py            # Poisson arrivals, scenario windows, ticket drafts
│   ├── simulation.py          # Priority-queue discrete-event routing simulation
│   ├── final_tables.py        # Relational table construction and message calibration
│   ├── sla_calendar.py        # Exact business-minute SLA calendar arithmetic
│   ├── anomalies.py           # Anomaly injectors and GroundTruth generator
│   ├── export.py              # CSV, JSON, SQL seed script, and MANIFEST exporter
│   └── reconcile.py           # Relational integrity and reconciliation checks
├── validation/
│   └── validate.py            # Independent 23-rule validator (evaluate & integrity modes)
├── tests/                     # Unit and integration test suite
├── scripts/                   # Reproducibility verification scripts (.ps1 and .sh)
├── docs/                      # Specification source of truth and reports
│   ├── data_dictionary.md     # Relational schema and column constraints
│   ├── business_rules.md      # State transitions, routing heuristics, SLA calendars
│   ├── DESIGN_DECISIONS.md    # Architecture trade-offs, spec decisions, limitations
│   ├── validation_summary.md  # Validator performance report (Precision/Recall)
│   └── source_manifest.md     # 100% synthetic data declaration
├── data/
│   ├── raw/                   # Default scale generated tables (30,000 tickets)
│   ├── staging/               # Intermediate simulation drafts (reproducible, ignored)
│   ├── ground_truth/          # ground_truth.csv (isolated from input features)
│   └── sample/                # Committed sample dataset (300 tickets, ~678 KB)
├── AI_USAGE.md                # AI usage disclosure and intervention log
└── PROMPTS.md                 # Chronological prompt history
```

### Relational Tables & Default-Scale Row Counts (data/raw)

| Table | File | Rows (30k Scale) | Description |
|---|---|---:|---|
| `Agent` | `agent.csv` | 100 | Support agents with tier and concurrency limit |
| `AgentLanguage` | `agent_language.csv` | 166 | Spoken languages and fluency levels |
| `AgentShift` | `agent_shift.csv` | 500 | Weekly rotating 8-hour shift schedules |
| `AgentSkill` | `agent_skill.csv` | 188 | Skill proficiencies with effective dates |
| `AssignmentEvent` | `assignment_event.csv` | 38,189 | Routing assignments, transfers, and escalations |
| `Channel` | `channel.csv` | 3 | Communication channels (CHAT, EMAIL, PHONE) |
| `Customer` | `customer.csv` | 12,000 | Customers with segment and language preferences |
| `Intent` | `intent.csv` | 12 | Support intents, categories, and default priority |
| `Language` | `language.csv` | 5 | Supported languages (en, es, hi, ar, fr) |
| `Message` | `message.csv` | 100,000 | Calibrated conversation turns and timestamps |
| `PriorityRule` | `priority_rule.csv` | 8 | Classification and escalation rules |
| `Queue` | `queue.csv` | 7 | Skill-based queues and escalation tier gates |
| `QueueMember` | `queue_member.csv` | 225 | Agent queue memberships and effective windows |
| `Resolution` | `resolution.csv` | 31,314 | Ticket cycle resolutions, handle times, CSAT |
| `SLACalendar` | `sla_calendar.csv` | 4 | Operating calendar definitions |
| `SLACalendarWindow`| `sla_calendar_window.csv` | 24 | Operating hours per day of week |
| `SLAEvent` | `sla_event.csv` | 31,320 | SLA clocks, targets, due times, breach flags |
| `Skill` | `skill.csv` | 6 | Core technical and domain skills |
| `Ticket` | `ticket.csv` | 30,000 | Support tickets and lifecycle states |
| `TicketStatusEvent`| `ticket_status_event.csv` | 208,743 | State transition history with actor references |
| **GroundTruth** | `ground_truth/ground_truth.csv`| 338 | Labelled anomaly instances (separate folder) |

---

## 9. Injected Anomalies & Detection Rules

Five targeted operational anomalies are injected post-simulation into specific entities. Injectors select candidate tickets and agents that satisfy all baseline constraints so each anomaly triggers **only** its intended detection rule:

| Issue Type | Detection Rule | Target Formula | Default Count | Intended Violation |
|---|---|---|---:|---|
| `AGENT_OVER_CAPACITY` | `V-ROUT-CAP-01` | `round(tickets * 0.002)` | 60 prim. (68 tot.) | Agent assigned ticket exceeding concurrency limit |
| `SLA_IGNORES_CALENDAR` | `V-SLA-01` | `round(tickets * 0.0033)` | 99 | Due times computed using wall clock, ignoring channel calendar |
| `REOPEN_LOSES_HISTORY` | `V-HIST-01` | `round(tickets * 0.0027)` | 81 | Cycle 1 assignments and SLA records deleted upon reopening |
| `TRANSFER_LOOP` | `V-LOOP-01` | `round(tickets * 0.0017)` | 51 | Ping-pong transfers (A -> B -> A -> B) assigning agent twice |
| `PRIORITY_CONTRADICTS_SAFETY` | `V-SAFE-01` | `round(tickets * 0.0013)` | 39 | Safety-intent tickets downgraded to P2 violating rule SR-1 |

---

## 10. Troubleshooting

1. **Wrong Python on PATH**: Always execute commands using the virtual environment interpreter (`.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on Linux/macOS) to avoid missing package errors.
2. **Side-Run Overwrites**: Executing `python -m generator` without `--out` will overwrite `data/raw`. For experimental or smaller runs, always specify an isolated output folder (e.g., `--out data/_quick`).
3. **First-Time Generation Runtime**: Full generation produces ~380,000 total rows across 20 tables and takes ~3.5 minutes. Do not terminate the process prematurely.

---

## 11. Known Limitations

- **No Holidays / DST**: Operating calendars follow standard UTC operating hours without regional public holidays or daylight saving shifts.
- **SLA Clock Pausing**: SLA clocks continue to tick during `PENDING_CUSTOMER` status.
- **Age-Based Escalation**: Queued tickets do not escalate solely by waiting time unless breaching SLA escalation limits.
- **Unplanned Absences**: Agent availability is determined strictly by scheduled shifts; sickness and unplanned absences are not modeled.
- **Initial Window Ramp-Up**: The first 6 hours of `2026-01-05` (00:00–06:00 UTC) have no agents on shift (first shift begins at 06:00 UTC); early arrivals queue until shifts open.
- **Specialized Queue Weekend Gaps**: `QUE-SALES` and `QUE-SECURITY` operate Monday–Friday; weekend arrivals remain queued until Monday morning.
- **Multilingual Templates**: Non-English customer messages use realistic domain-specific synthetic translation templates.
- **Customer Directory**: All customer profiles predate the simulation start date.
- **Agent Utilization & Breaches**: 100 agents handling 30,000 tickets across 90 days yields realistic baseline utilization (~48%) and SLA breach rates (~3.6%), reflecting operational staffing for peak demand.
