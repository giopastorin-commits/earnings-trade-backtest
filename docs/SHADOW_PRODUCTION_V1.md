# TRINITY Shadow Production V1

This runner is manual-only. It uses `workflow_dispatch` with `golden-replay`
(the default) and `full-shadow` modes, never submits broker
orders, and renders Telegram output without sending it. Analytical prompts,
schemas, formulas, thresholds, ordering, and method versions remain owned by
the frozen production modules.

## GitHub configuration

Required secrets:

- `OPENAI_API_KEY`
- `EODHD_API_KEY`
- `TRINITY_R2_ENDPOINT`
- `TRINITY_R2_ACCESS_KEY_ID`
- `TRINITY_R2_SECRET_ACCESS_KEY`

Golden replay deliberately does not receive the OpenAI, EODHD, Telegram, or
R2 write secrets. It requires only:

- `TRINITY_R2_ENDPOINT`
- `TRINITY_R2_READ_ACCESS_KEY_ID` (R2 Object Read only)
- `TRINITY_R2_READ_SECRET_ACCESS_KEY` (R2 Object Read only)

Required variable:

- `TRINITY_BASELINE_PREFIX`: immutable prefix containing `version=`

Optional variable:

- `TRINITY_R2_BUCKET` (defaults to `trinity-raw`)
- `TRINITY_GOLDEN_BASELINE_PREFIX` (defaults to the immutable Week 0 content version)

The Week 0 golden prefix is
`baseline/shadow-week0/version=bfd0cc4d291439db09d9edbb7194bde235eab5cfccefdaf56bd1fd1adbf66ad2/`.
Its `manifest.json` is the completion marker and has SHA-256
`4109518d3c3b46036e0cbbdf21586846f2572f950d6c8b5f7297e18c07f0036a`.

The baseline prefix must contain `manifest.json`. Its `files` array lists only
the canonical ticker mapping, the 518 historical OHLCV JSON captures, and any
deterministic reference/cache files required before fresh acquisition. Every
entry has `path`, `bytes`, and `sha256`. The runner downloads only those listed
objects and verifies them before execution; it never mounts or downloads the
complete raw archive.

## Runtime and durability

GitHub uses Ubuntu 24.04 and exact Python 3.13.15. Golden replay installs the
complete Linux dependency closure from `requirements-golden.lock` with
`--require-hashes`; direct and transitive versions and wheel hashes are fixed.
Full Shadow uses direct dependencies pinned in `requirements-shadow.txt`; its
transitive dependencies are resolver-selected and therefore less strictly
reproducible than golden replay. The direct list was derived from the locally working
Python 3.13 environment, with boto3 pinned for the S3-compatible R2 transport.

All workflow actions are pinned to immutable commit SHAs with their readable
release version in adjacent comments. Golden replay restores 517 data objects,
verifies every SHA-256 locally, executes only the deterministic funnel and
Setup V1, and uploads attempt-specific comparison diagnostics with `if: always()`.

Every model success is saved atomically before parsing. Usage and calculated
cost are then appended to a durable per-run ledger. A measured total at or over
USD 5.00 prevents the next request. Raw responses use `store:false` and no
request headers or credentials are persisted.

Each run uses a fresh SQLite file, applies every registered migration,
checkpoints WAL, runs `integrity_check` and `foreign_key_check`, closes the
database, and excludes sidecars from the archive. R2 keys are immutable under
`runs/shadow/run_id=<run_id>/`; existing objects must match both size and the
stored SHA-256 metadata.

Failure runs retain completed artifacts and are archived by an `always()`
workflow step. There is no schedule and the concurrency group permits only one
Shadow Production job at a time.
