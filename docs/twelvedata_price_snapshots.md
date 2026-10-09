# Twelve Data production price snapshots

TRINITY production OHLCV is acquired independently from the analytical run.
The provider contract is fixed to `interval=1day` and `adjust=none`; canonical
TRINITY tickers are submitted directly, including `BF.B` and `BRK.B`.

The manual `trinity-price-refresh.yml` workflow acquires the complete canonical
registry using at most seven request starts per rolling 61 seconds. It retains
exact raw responses under `raw/`, writes ascending normalized OHLCV under
`normalized/`, and creates `snapshot_manifest.json`. The manifest records every
ticker's classification, dates, bar count, content hash, attempts, retries, and
the deterministic snapshot-level hash.

Before the ticker loop, the acquisition calls Twelve Data `/api_usage` once and
requires at least 550 daily credits remaining: 518 ticker requests plus a
32-credit operational reserve. Sanitized provider fields and credit headers are
written to `provider_credit_telemetry.json`; the manifest hashes this file and
the immutable R2 archive preserves it. `INSUFFICIENT_DAILY_CREDITS` stops before
the first ticker request. A 429 body that explicitly reports daily exhaustion
stops the whole acquisition as `DAILY_QUOTA_EXHAUSTED` without retry, while a
temporary minute-rate 429 retains the bounded retry policy. Credit headroom is
rechecked continuously so retries cannot make full-universe completion
impossible. Neither quota failure is converted into ticker DQ or an analytical
exclusion, and no partial snapshot manifest is produced.

Only `ACTIVE_COMPLETE` and explicitly justified non-provider states such as
`CORPORATE_ACTION_NO_LONGER_TRADING` pass the hard coverage gate. WBD's known
2026-10-06 flat, zero-volume carry-forward row is excluded and recorded as
`CORPORATE_ACTION_ARTIFACT_STALE_CARRY_FORWARD`.

Ready snapshots are archived without overwrite at:

```text
prices/twelvedata/session=YYYY-MM-DD/run_id=<github-run-id>-A<attempt>/
```

For `full-shadow`, dispatch `trinity-shadow-manual.yml` with both the immutable
R2 prefix and its expected snapshot SHA-256. The runner restores and verifies
that exact snapshot before creating analytical artifacts or model calls. The
pre-research funnel, final report, and Ledger record the provider, snapshot ID,
target session, and snapshot hash.

EODHD remains configured in the analytical workflow only for News and calendar
evidence. SEC behavior is unchanged.
