# Support Ticket Routing: Synthetic Data Generator

Generates a reproducible synthetic dataset for omnichannel support-ticket routing
(intent, urgency, language, skill, agent capacity) with SLA tracking, labelled anomalies,
a separate ground-truth file, validation and tests.

> Status: scaffold. Sections below are filled in as each build phase lands.

## Quickstart (placeholder)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m generator --seed 20261022 --tickets 300 --agents 10 --messages 1000 --days 10 --out data/raw   # starter scale
python -m validation.validate --data data/raw
pytest
```

## Repository layout
| Path | Purpose |
|---|---|
| `generator/` | Config, master data, tickets, routing, SLA, anomaly injection, export |
| `validation/` | Integrity, lifecycle and reconciliation checks |
| `tests/` | Unit and integration tests |
| `data/raw/` | Generated input tables (no labels) |
| `data/ground_truth/` | `ground_truth.csv`, never read by the generator's input features |
| `data/sql/` | SQL seed data |
| `docs/` | Data dictionary, business rules, design decisions, validation summary |
| `PROMPTS.md` | Prompt history |
| `AI_USAGE.md` | AI usage disclosure |

## Seed and reproducibility
Fixed seed: `20261022`. See `docs/reproducibility.md` (to be added).

## Assumptions, trade-offs, limitations
See `docs/DESIGN_DECISIONS.md`.
