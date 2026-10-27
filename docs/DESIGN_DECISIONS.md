# Design Decisions, Assumptions, Trade-offs, Limitations

## Open decisions (defaults proposed, confirm or change)
| Decision | Default | Rationale |
|---|---|---|
| SLA ID for reopen cycles | `SLA-<ticket-id>` cycle 1; `SLA-<ticket-id>-R1`, `-R2`... | One SLA record per cycle while keeping the required base format |
| Priority naming | P1 to P4 (P1 critical) | Compact, standard |
| Languages | en, es, hi, ar, fr (~60% en) | Enough for mismatch logic without bloat |
| Extra IDs | `CUST-000001`, `TSE-<ticket-id>-001` | Brief gives no format |
| Message bodies | Templates only | No real personal data, cheap to generate |

## Assumptions
_to fill_

## Trade-offs
_to fill_

## Known limitations
_to fill_
