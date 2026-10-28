# AI Usage Disclosure

## Where AI accelerated the work
AI accelerated the implementation by rapidly producing boilerplate structures, relational schema mapping code, discrete-event priority queue simulation logic, vectorized pandas/NumPy data transformations, and the initial drafts of validation rule checks.

## Where I relied heavily on AI
I relied heavily on AI to draft the detailed relational tables according to the data dictionary, generate realistic multilingual template strings for customer-agent message threads, structure the business-minute calendar arithmetic routines, and perform rapid diagnostic analysis of validation findings across large datasets.

## What I personally reviewed, changed and validated
I personally directed the architecture, reviewed all simulation logic, verified schema and business rule compliance, inspected all diffs, and maintained strict control over the source of truth, test suite, and anomaly injection integrity.

## Intervention Log
1. I wrote the data dictionary and business rules first as the source of truth, and fixed ambiguities in the documentation before implementation began.
2. I wrote independent checker scripts that share no code with the generator to independently verify each phase.
3. My phase-3a checker had a per-ticket vs per-spell waiver bug; I investigated the discrepancy and fixed the checker, not the simulation engine.
4. I found the handover-cap spec contradiction from output analysis and updated the specification accordingly.
5. I made the after-hours calibration decision, establishing the accepted operational range of 8% to 20%.
6. I moved the simulation cutoff date to 2026-04-05 to ensure full 12-week alignment.
7. I applied specification patches through a one-off script that was not committed to the repository.
8. I rejected tuning constants to hit arbitrary KPI targets, and instead relaxed the targets where staffing arithmetic explained the gap.
9. When a PriorityRule test failed, I diagnosed it as a stale test fixture problem before allowing any changes to the code.
10. The message-count conflict was identified by the agent; I reviewed the diagnosis and accepted its calibration pass fix.
11. When a long test run appeared to be looping, I interrupted the execution, diagnosed the runtime, and added a slow pytest marker.
12. When the first validator run yielded a precision of 0.042 due to validator bugs, I required a per-rule diagnosis on a small clean dataset and had the validator logic fixed.
13. I spotted that the anomaly injectors created unlabelled side-effect violations (such as skill, shift, and lifecycle errors) and required them to be minimal and strictly isolated to their intended rules.
