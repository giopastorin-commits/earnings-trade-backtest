# TRINITY feedback completion audit

**Working branch:** `codex/feedback-validation-v1`  
**Base:** `origin/main` at `925cfce478ac2dd512739de25bc31fddb74df9de`  
**Frozen analytical commit:** `eb15c45b041b8890194d2f98b025aa98ed7fa527`  
**Audit date:** 27 October 2026

## Initial evidence

| Ref | Observed state |
|---|---|
| `main` | Production workflow active; archives price/run artifacts and commits the official cycle marker. No feedback ingestion or outcome workflow is present. |
| `trinity-p3-automatic-outcomes-v1` at `62393a0` | Adds `ingest_official_run_r2`, outcome scheduling, R2 commit publication and an inline production ingestion step, but its workflow is not on the default branch. |
| `trinity-live-validation-v1` at `8221ff6` | Contains the frozen validation primitives: collector, XNYS calendar, D5/D20/D60 expectations, stock/SPY acquisition, finalization, baskets and V1 scorecard. |
| `trinity-maturity-wip` at `4339a66` | Contains maturity/scorecard development and ledger work; it is not the production registration branch and is not used as an implementation base. |
| production run `40432096545` | GitHub API shows success on `main`; jobs include production archive and completion, but no feedback/reconciliation job. |

The local environment has no R2 credentials, so the remote bucket was not read or modified. Repository evidence and the production run job list show no verified real `OUTCOME_FINAL` publication from the official 26 October run. Existing P3 fixtures and tests are synthetic/rehearsal evidence, not production outcomes.

## Changes on this branch

1. Reused the P3 orchestration on top of `main`, without copying or modifying the frozen analytical implementation.
2. Removed the feedback ingestion step from the production workflow. Production completion is no longer coupled to feedback availability.
3. Added `TRINITY Official Feedback Reconciliation`, triggered only after a successful official production workflow and also manually dispatchable with an exact archived run ID.
4. Added immutable R2 archive discovery and restore with run-ID validation, path traversal protection, archive checksum verification and no-overwrite semantics.
5. Added `reconcile-official-r2`, which restores the archived run and retries ingestion only; it does not call Scanner, Luna, Sol, OpenAI, EODHD or a broker.
6. Made scorecard persistence explicit. Every outcome pass, including `NO_MATURE_EXPECTATIONS` and `ALREADY_COMPLETED`, publishes a deterministic scorecard keyed by the authoritative record digest.
7. Added descriptive D5/D20/D60 summaries, weekly distributions, API-cost totals, missed-winner diagnostics and promoted-underperformer diagnostics alongside the frozen V1 scorecard. The frozen protocol and its formal 52-week inference gate remain unchanged.
8. Added tests for deterministic scorecard output, no-op scorecard persistence, archived-run discovery/restore and separated workflow ordering.

## Corrective review of PR #1

The independent findings against commit `e01c250` were corrected on the same
feature branch without changing Scanner, Luna, Sol, the validation protocol, or
the frozen analytical commit:

1. Automatic reconciliation now reports `NO_OFFICIAL_CYCLE` for a successful
   production workflow that produced no OFFICIAL archive; manual absence and
   ambiguous discovery remain errors.
2. `OFFICIAL_INGESTION_RECEIPT` no longer contains the registration workflow's
   `GITHUB_SHA`, so retries over the same source run produce identical content.
3. Every authoritative R2 ingestion validates the immutable OFFICIAL completion
   marker and the complete archived source before opening or mutating the local
   validation store.
4. Immutable validation objects use atomic R2 create-if-absent semantics via
   `If-None-Match: *`; a 409/412 conflict is accepted only for identical bytes
   and verified SHA-256 metadata.
5. Workflow-dispatch input is carried through environment variables and checked
   against strict run-ID/integer patterns before use.

The tests now include two concurrent immutable writers, missing/divergent marker
rejection, retry-deterministic receipts, clean skipped-cycle handling, and an
end-to-end path that archives with the real frozen `archive_run`, restores the
archive through `FakeR2`, and imports it with the frozen collector. Selection
diagnostics separately identify whole-pipeline non-promotion, Sol rejection,
Sol evaluation failure, and no Sol routing.

Validation executed on 27 October 2026:

- Corrected P3 suite: `29 passed`.
- Frozen `tests/trinity/validation` suite at analytical commit
  `eb15c45b041b8890194d2f98b025aa98ed7fa527`: `400 passed`.
- Python compilation and workflow YAML parse: passed.
- Production R2 integration, workflow dispatch, deployment and merge: not run
  by design; the review used only local fixtures, `FakeR2`, and the detached
  frozen analytical checkout.

## R2 layout added

```text
prospective-validation/protocol=TRINITY_LIVE_VALIDATION_V1/epoch=1/
  commits/official-ingestion/identity=<sha256>.json
  commits/observation-slot/identity=<sha256>.json
  stock-observations/identity=<cycle-id>/...
  spy-benchmarks/identity=<cycle-id>/...
  observation-cycles/identity=<cycle-id>/...
  scorecards/identity=<authoritative-record-digest>/scorecard.json
  scorecards/identity=<authoritative-record-digest>/ARCHIVE_MANIFEST.json
```

The scorecard is a deterministic derivative of authoritative validation records; it is not a mutable `latest` pointer and does not replace the append-only commit bundles.

## Activation and rollback

Nothing has been deployed, merged, scheduled or written to production R2.

After review:

1. Open and review the PR from `codex/feedback-validation-v1`.
2. Run the offline P3 suite against the frozen analytical checkout.
3. Merge only after explicit approval.
4. Confirm the first production run completes and that the separate reconciliation workflow publishes an official-ingestion receipt.
5. Confirm the first outcome workflow publishes stock/SPY/cycle commits and a scorecard.
6. If rollback is required, disable the two feedback workflows or revert this branch; existing immutable R2 objects are not deleted or rewritten.

Formal inference remains unavailable until the frozen protocol has at least 52 primary-usable official weeks. Before that threshold, reports must remain descriptive.
