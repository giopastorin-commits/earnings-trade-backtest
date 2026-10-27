# TRINITY P3 Automatic Prospective Outcomes

Status: feature-branch implementation for review; not deployed or enabled on
the production default branch.

## Frozen analytical contract

P3 consumes, and does not modify, `TRINITY_LIVE_VALIDATION_V1`, epoch `1`,
protocol hash
`c04e4009962e2d93265b725e0c3213000151202e8ecd3dd45e60cf9f92ed59d3`,
from analytical commit `eb15c45b041b8890194d2f98b025aa98ed7fa527`.
Scanner, Luna, Sol, ranking, trade plans, benchmark, SimpleQuant, horizons,
costs, cohort weights, bootstrap rules, and universe logic remain frozen.

## Architecture

The feature adds orchestration only. After a successfully archived OFFICIAL
weekly run and its OFFICIAL completion marker, the separate reconciliation
command uses the frozen collector and storage primitives to append `RUN_OBSERVATION`,
`SELECTION`, `SETUP`, `EXECUTION`, and D5/D20/D60 `OUTCOME_EXPECTATION`
records. VALIDATION is rejected. One immutable ingestion commit makes the
entire append set authoritative; identical replay is idempotent and divergent
replay fails closed.

The outcome collector restores all authoritative commit bundles, calls the
frozen `weekly_observation_slot`, and derives exactly the mature, unassigned V1
expectations whose DN session is on or before that slot target. An empty set
returns `NO_MATURE_EXPECTATIONS`, writes no cycle or assignment, and makes no
provider call. A non-empty set is hashed and reduced to its exact ticker
subset. A single preflight requires one credit per stock ticker, one for SPY,
and the existing 32-credit reserve before acquisition starts.

Production and feedback are intentionally separate. The official production
workflow archives and commits its completion marker without invoking the
feedback collector. A successful `TRINITY Weekly Production` run then triggers
`TRINITY Official Feedback Reconciliation`, which restores the immutable
`runs/shadow/run_id=.../` archive and calls `reconcile-official-r2`. A manual
retry accepts the exact archived run identity, so an ingestion failure never
reruns Scanner, Luna, or Sol.

The reconciliation command is idempotent. It discovers exactly one archived
run for a workflow-run ID/attempt, verifies the run archive checksums and the
collector's complete-run contract, hydrates existing authoritative commits,
and publishes the official-ingestion commit only after all records validate.

Stock acquisition uses the frozen
`trinity.validation.stock_observation_acquisition.acquire_stock_observation`.
SPY uses the frozen `acquire_spy_benchmark`. The frozen cycle builder verifies
both READY snapshots and the identical target session. Only then are the cycle
registered, exact-set assignments appended, outcomes finalized, and frozen
baskets, scorecard, and SimpleQuant analytics produced. Existing final records
are never restated; missing or non-authoritative observations remain PENDING.

## Future schedule

The outcome workflow registers 04:00/05:00 UTC cron twins Tuesday through
Saturday. Its gate permits only the twin corresponding to 06:00 Europe/Rome
on the calendar day after the frozen slot target. Thus a normal Monday slot is
collected Tuesday morning; a Labor Day Tuesday slot is collected Wednesday
morning. The target itself always comes from
`trinity.validation.observation_slots`, never from weekday or close-time
assumptions. Non-target days are no-ops, early closes use XNYS calendar data,
and runner delay cannot retarget the slot. This separates outcome acquisition
from the approximately 23:00 Europe/Rome OFFICIAL production window. The
workflow has its own `trinity-prospective-outcomes-v1` concurrency group.

Scheduled workflows run only from the default branch. This workflow is
therefore inactive while it remains on the P3 review branch and must not be
promoted without explicit approval after the first OFFICIAL review.

## Immutable durability

The proposed authoritative R2 root is:

`prospective-validation/protocol=TRINITY_LIVE_VALIDATION_V1/epoch=1/`

It contains:

- `commits/official-ingestion/identity=<sha256>.json`
- `commits/observation-slot/identity=<sha256>.json`
- `stock-observations/identity=<cycle-id>/...`
- `spy-benchmarks/identity=<cycle-id>/...`
- `observation-cycles/identity=<cycle-id>/...`

Each commit is a single canonical JSON object containing the exact new
append-only validation records and their payload hashes. Commit publication is
the authoritative atomic boundary. Snapshot and cycle trees have immutable
per-file hashes plus an archive manifest and are fully verified before the
slot commit is published. Create-or-verify semantics accept identical replay
and reject divergent content. No mutable `latest.json` participates in
authority. GitHub artifacts are diagnostics only.

This design lets a partial publication retry safely: records are materialized
only from complete immutable commit bundles. Orphaned pre-commit evidence is
non-authoritative and cannot manufacture an outcome. Provider, quota, partial
stock, SPY, target mismatch, corruption, ambiguous-cycle, or endpoint failures
all fail closed. DQF remains limited to the frozen positive terminal-stock
evidence rules.

The outcome collector also publishes a deterministic scorecard artifact under
`scorecards/identity=<authoritative-record-digest>/`. It contains the frozen
V1 scorecard, descriptive D5/D20/D60 cohort summaries, weekly distributions,
API-cost totals, and diagnostics for missed Scanner winners and promoted
underperformers. A no-mature-expectations run still publishes the current
scorecard and makes no market-data provider call.

No OpenAI, EODHD, Scanner, Luna, Sol, Telegram, broker, order, or trade
execution path exists in the outcome workflow. Formal inference remains
blocked by the frozen 52 primary-usable-week minimum; earlier reviews are
descriptive only.
