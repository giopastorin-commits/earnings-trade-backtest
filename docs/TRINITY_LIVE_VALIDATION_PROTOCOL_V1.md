# TRINITY Live Validation Protocol V1

Status: frozen prospective protocol

Freeze note: `FROZEN FOR FIRST OFFICIAL OBSERVATION`

Protocol ID: `TRINITY_LIVE_VALIDATION_V1`
Protocol epoch: `1`

This layer measures TRINITY. It does not alter Scanner, Luna, Sol, Setup,
Entry Freshness, retries, costs, notifications, the operational Ledger, or
historical recovery. Inputs are immutable run artifacts; collection is
offline and read-only. Validation records are written to a separate,
append-only store. Identical replays are no-ops and identity conflicts fail
closed.

No `OFFICIAL_ELIGIBLE` observation existed when this hardening was frozen.
The earlier protocol hash
`074ced65bbdef589ff94ce31a5214982ffd482f830c508f15143638cc7bf30f5`
and the pre-slot-governance hash
`38980fc7bdc0ab8912f23cbc8ce67325f0d0ffe896a8d29838fe81ed1d094a1a`
and the pre-finalization-policy hash
`134d9d6a3751c27952a3848baeff4d12642bfcccebada68b6984e95c2c8449a8`
and the pre-weekly-analytics hash
`0a2e4add27ba15507235f04d4a307c21a70c93920f5a7c02cd9e97803a469035`
are retired for future official observations. Existing smoke/test identities
remain under their original hashes and are never upgraded retroactively. The
current hash, computed from the machine-readable contract after freezing the
weekly analytics policy, is
`c04e4009962e2d93265b725e0c3213000151202e8ecd3dd45e60cf9f92ed59d3`.

## Run eligibility

- `VALIDATION` plus `official_benchmark_observation=false` is `SMOKE_ONLY`.
- `OFFICIAL` plus `official_benchmark_observation=true` is `OFFICIAL_ELIGIBLE`.
- Every other combination is `INELIGIBLE` and fails closed.

Collection supports both coherent categories. Official cohorts, endpoints,
bootstrap, confidence intervals, missed-winner diagnostics, and scheduled
review windows accept only `OFFICIAL_ELIGIBLE` inputs. Mixed or smoke inputs
to official analytics fail closed.

## Frozen selection cohorts

- Scanner: every `READY_TECHNICALLY` ticker.
- Luna promoted: `ESCALATE`.
- Luna rejected: `DROP`.
- Luna `WATCH`: retained as a distinct state.
- Sol evaluated: a ticker actually routed to Sol.
- Sol promoted: `INVESTIGATE`.
- Sol rejected: evaluated successfully but not promoted.
- Sol `NOT_EVALUATED`: never routed to Sol.
- Sol `EVALUATED_FAILED`: routed but failed; a failure is never a rejection.

Membership is frozen from the source run and is never conditioned on whether
an entry later triggers.

## Endpoints

The statistical unit is the weekly run. Stage baskets are equal-weight.
The primary horizon is 20 trading sessions; secondary horizons are 5 and 60.
The benchmark is SPY price return supplied through a separate immutable,
point-in-time series using Twelve Data, interval `1day`, adjustment `none`.
This matches the non-adjusted price methodology of the investible series.
SPY is benchmark-only and is not part of the investible 518-ticker universe.

Primary endpoint:

`AR20(Sol) - AR20(Scanner)`

Confirmatory secondary endpoints:

- `AR20(Luna) - AR20(Scanner)`
- `AR20(Sol) - AR20(Luna)`

All other measurements are `DIAGNOSTIC` or `EXPLORATORY`. In particular,
`missed_winner_rate_20d` is diagnostic and counts Scanner observations with
mature 20-session active return of at least +10%, then reports how many were
promoted by Luna, evaluated by Sol, and promoted by Sol.

Every immutable official weekly run is retained as a statistical unit through
an append-only `RUN_OBSERVATION`, including runs with no Scanner candidates.
Its frozen membership is derived from run lineage: Luna is the completed
`ESCALATE` subset of Scanner, and Sol is the `PROMOTED`/`INVESTIGATE` subset
of Luna. A run with an empty Scanner sleeve has empty Luna and Sol sleeves but
does not disappear from the denominator.

An empty Scanner, Luna, or Sol sleeve is retained for the week as a benchmark
sleeve and therefore has active return zero. A nonempty stage is available
only when every member has a COMPLETE final outcome. If any member is pending,
the stage is `PENDING` with no return. If any member has
`DATA_QUALITY_FAILURE`, the stage is `UNAVAILABLE_DQF` with no return: members
are never dropped, remaining members are never reweighted, and DQF is never
imputed as zero. Member, complete, pending, and DQF counts remain explicit.

The fixed denominator `n_total_official_weeks` counts every immutable official
weekly run. At D20, `n_primary_usable_weeks` counts only weeks whose Scanner
and Sol stages are both available; pending and DQF-unavailable primary weeks
are reported separately. The primary mean and circular block bootstrap use
only primary-usable weeks, while all fixed-denominator counts and rates remain
visible. Formal inference begins only at 52 primary-usable weeks, not merely
52 calendar weeks or runs. The frozen bootstrap uses 10,000 replicates, a
four-week D20 block, and a 95% interval.

The primary endpoint is gross active-return delta. Trading costs are
sensitivity-only at 0, 10, and 25 bps per side. A nonempty stock sleeve pays
the round-trip cost `2 * bps_per_side / 100` percentage points; an empty
benchmark sleeve remains at zero active return and bears zero cost. Selection
quality flags fail closed for official analytics until a future protocol
explicitly classifies any benign flag, so
`SOL_CRITIC_LINEAGE_NOT_COMMITTED` cannot silently enter the Sol endpoint.

The existing `baskets.py` and `scorecard.py` remain legacy relative to the
append-only `OUTCOME_FINAL` pipeline. They do not implement this frozen weekly
policy and are not evidence of its correctness; replacement is a separate
task.

## Weekly observation slots

Official observation cadence is weekly and weeks use the ISO-week definition.
The deterministic target is the first actual XNYS trading session in that ISO
week. This is normally Monday, but when Monday is not an XNYS session the
target is the next session in the same ISO week, such as Tuesday after a
Monday market holiday. Weekday names and UTC close times are never hardcoded.

A target becomes eligible for acquisition only at its calendar-derived XNYS
close plus 20 minutes, matching the stock-observation and SPY finality gates.
For an immutable outcome expectation whose maturity session is DN, the first
eligible observation slot is the first weekly target session on or after DN.
Monday maturity therefore uses that Monday slot when it is the target;
Tuesday-through-Friday maturity after a normal Monday target moves to the next
week. A Tuesday maturity after a Monday holiday uses Tuesday when Tuesday is
that week's first XNYS session. Slots affect acquisition timing only: D1, DN,
and the D1-open-to-DN-close return definition remain unchanged.

A slot with no `CLOSED_VERIFIED` cycle is an operational observation missing,
not `DATA_QUALITY_FAILURE`. The expectation remains pending and eligible for a
later weekly slot; no zero return, DQF, removal, horizon change, or
complete-case dropping is allowed. Future slot-closure infrastructure must
make such misses auditable.

For each official slot, the future assignment layer must derive the exact set
of all V1 expectations mature by the slot target and not previously assigned.
A generic cycle containing an arbitrary subset may remain in the generic
cycle registry, but it cannot serve as the authoritative official slot cycle.
If multiple candidate cycles claim the same exact slot set, registration order,
cycle ID, and observed result cannot choose a winner: the future assignment
layer must report an ambiguous operational error. The generic cycle registry
is an inventory and is not proof of authoritative assignment or completeness.

## Point-in-time outcomes

The return begins at D1, the first XNYS session whose open is strictly later
than the decision timestamp. D1 and inclusive DN are frozen in the immutable
`OUTCOME_EXPECTATION`; future bars never rederive or shift those endpoints.
The close that created the signal is never treated as an executable fill.

An N-session outcome is measured from the open of the first actually tradable
session after the decision to the close of the Nth tradable session,
inclusively. Thus 5, 20, and 60 sessions use D1 open to D5, D20, and D60 close,
respectively; D21 is never used for the 20-session endpoint.

### Outcome finalization

`PENDING` is derived runtime state and is never persisted as `OUTCOME_FINAL`.
An unassigned expectation remains `PENDING_UNASSIGNED`. An assigned expectation
whose authoritative cycle artifacts have not been supplied locally remains
`PENDING_SOURCE_NOT_AVAILABLE`. A supplied but corrupt, unverifiable, or
lineage-inconsistent artifact is an operational contract error: finalization
fails closed and writes no final record. A missed or ambiguous observation
slot therefore remains pending rather than becoming a zero return or a data
quality failure.

Finalization verifies the complete immutable chain from expectation through
exact-set assignment, terminal slot closure, cycle registration, the full
`CLOSED_VERIFIED` cycle, stock observation, and SPY observation. Runtime source
locators contain local paths only for retrieval; paths never enter identities
or persisted final records. The complete cycle expectation set is reloaded
and verified, rather than verifying only the ticker currently being finalized.

A `COMPLETE` outcome uses exactly stock D1 OPEN to DN CLOSE and SPY D1 OPEN to
DN CLOSE. Stock and benchmark returns are gross percentage price returns, and
active return is stock return minus benchmark return. There is no forward
fill, nearest-session substitution, observation-day return, trading-cost
adjustment, or horizon rediscovery.

`DATA_QUALITY_FAILURE` requires positive verified evidence: an authoritative
assignment and fully verified sources whose stock classification is explicitly
`CORPORATE_ACTION_NO_LONGER_TRADING`, with D1 or DN absent from the real bars.
The deterministic flags are `TERMINAL_STOCK_D1_UNAVAILABLE` and
`TERMINAL_STOCK_DN_UNAVAILABLE`. A terminal ticker with both endpoints is
calculated normally. Missing endpoints for `ACTIVE_COMPLETE`, or missing SPY
endpoints, are contract errors and produce no final record. Artifact corruption
or a local restore gap is never converted into DQF.

`OUTCOME_FINAL` contains only `COMPLETE` or `DATA_QUALITY_FAILURE`, is keyed by
the immutable expectation identity, preserves the full cycle/snapshot lineage,
and is append-only. Identical replay is idempotent; divergent content conflicts.
Neither COMPLETE nor DQF may be restated using a later cycle.

The current canonical 518-name snapshot does not contain SPY. V1 therefore
defines the benchmark interface but makes no data request. A future, separately
reviewed acquisition must archive SPY under an immutable benchmark namespace
with symbol `SPY`, provider `TWELVE_DATA`, interval `1day`, adjustment `none`,
return kind `PRICE_RETURN`, explicit session coverage, snapshot ID, and SHA-256.
The future acquisition must use the same target market session as its TRINITY
run, preserve immutable raw and normalized objects, generate and verify a
manifest, and pass a hard coverage gate before outcomes can become complete.
It must not modify the canonical issuer registry or price snapshot: SPY is not
the 519th ticker.

### Immutable SPY benchmark snapshot

The acquisition implementation lives only in `trinity.validation` and writes
a separate benchmark root containing `raw/SPY.json`, `normalized/SPY.json`,
`benchmark_manifest.json`, and a final `READY` marker. Snapshot IDs use
`SPY_TWELVEDATA_<SESSION>_RUN_<RUN_ID>`. Existing/non-empty roots are never
overwritten. The verifier checks the manifest and marker hashes, raw and
normalized hashes, canonical reproducibility, ordered unique XNYS sessions,
valid OHLCV, and exact target-session coverage before constructing a
`BenchmarkSeries`.

The provider response must explicitly identify `meta.symbol=SPY` and
`meta.interval=1day`; missing or divergent metadata fails closed. Coverage is
the exact uninterrupted XNYS session sequence from the first XNYS session on
or after the requested start date (`target_market_session - 450` calendar
days) through the target session, with no truncated suffix, filling,
nearest-session substitution, or repair. The manifest records the expected
coverage endpoints and expected/actual counts; the verifier recomputes them
from the requested window and XNYS calendar.

Acquisition must start no earlier than 20 minutes after the calendar-derived
XNYS close of the target session. Every persisted provider attempt must carry
a timezone-aware `requested_at_utc` within the interval from acquisition start
through acquisition completion, at or after the finality cutoff, and attempt
timestamps must be nondecreasing. Provider-attempt provenance is persisted
through an allowlist. Payloads and allowlisted strings containing credentials,
token assignments, credential-bearing URLs, or bearer tokens are rejected
before READY can be created.

Snapshot identity is the pair `benchmark_snapshot_id` plus
`benchmark_snapshot_sha256`. The snapshot ID alone is not sufficient to
identify content.

Each normalized bar contains `date`, `open`, `high`, `low`, `close`, `volume`,
and `tradable_at`. The latter is the authoritative XNYS session open from the
versioned `exchange-calendars` schedule; it is not a fixed UTC assumption, so
daylight-saving changes remain explicit. A TRINITY/SPY target-session mismatch
fails closed. The explicit future CLI is `python -m trinity.validation.cli
acquire-spy`; it is not called by any workflow.

## Execution analytics

Selection and execution performance are separate. A Setup that never triggers
is `NOT_TRIGGERED`, not a loss. Other lifecycle states include `EXPIRED`,
`INVALIDATED_BEFORE_ENTRY`, `ENTRY_TRIGGERED`, `STOPPED`, `TP1`, and `TP2`.
For long trades, `R0 = fill_price - stop`. MFE and MAE are reported in R and
are diagnostic only. If daily OHLC indicates both stop and a target in the
same bar, the record is marked `AMBIGUOUS_OHLC` and the primary V1 result uses
the frozen worst-case `STOP_FIRST` policy.

BREAKOUT uses stop-entry semantics distinct from PULLBACK. If the next bar
opens at or above entry, the actual fill is the open and `R0` uses that fill.
If open is below entry but high reaches entry, fill is the frozen entry. A gap
opening at or beyond TP1/TP2 is `GAP_BEYOND_TARGET`, has no result R, and is
never counted as a win. Same-bar ordering ambiguity remains flagged and uses
the conservative `STOP_FIRST` primary policy. PULLBACK semantics are unchanged.

## Costs and inference

- Base shadow trading cost: 10 bps per side.
- Sensitivities: 0 and 25 bps per side.
- Confidence interval: 95%.
- Circular block bootstrap: 10,000 replicates.
- Mean blocks: 1 week (5d), 4 weeks (20d), 12 weeks (60d).
- Full-pipeline governance MVE: +0.50 percentage points per 20 sessions.
- Stage governance MVE: +0.25 percentage points per 20 sessions.

These MVE values are governance thresholds, not scientific truths. Formal
inference is withheld before the 12-month/52-week review. Reviews at 3 months
are operational QA, 6 months preliminary, 12 months the first formal economic
review, and 24 months the more robust review.

Allowed scorecard statuses are `MONITOR`, `INSUFFICIENT_SAMPLE`,
`INVESTIGATE_DATA_ISSUE`, and `FORMAL_REVIEW_DUE`. Early reports must not use
claims such as “winning”, “losing”, or “edge proven”.

The canonical machine-readable contract is
`trinity/validation/protocol_v1.json`; its canonical SHA-256 is the protocol
hash. Any future change to analytical logic, models/prompts, thresholds,
benchmark, horizons, execution policy, or this contract requires a new epoch
or protocol version. Existing observations must never be rewritten.
