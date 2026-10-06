# TRINITY Shadow Week 0 golden reference

This package freezes the canonical deterministic output of
`SHADOW_USA_2026-10-06_W00` at `2026-10-05T20:00:00Z`. It covers the canonical
universe, data-quality and regime dispositions, the technical funnel, and
Setup V1. It deliberately excludes Luna, Sol, Facts V3 execution, and all
network acquisition.

`canonical_ticker_mapping.csv` is the compact, byte-identical mapping used by
Week 0. `input_manifest.json` identifies the external minimal OHLCV payload:
the final 201 cutoff-clipped rows for each of 516 available tickers, with BF.B
and BRK.B explicitly absent. Those large price inputs are not committed here.

Canonical JSON uses UTF-8, sorted object keys, compact separators, and rejects
NaN. Volatile run identity, timestamps other than the decision cutoff,
durations, absolute paths, process/temp/GitHub identifiers, and acquisition
counters are excluded. The exact exclusions and hashes are recorded in the
JSON manifests beside this file.

Replay uses the production `run_funnel`, `screen_ticker`, technical metric,
regime, and `build_setup` functions. The mode blocks Python network transports,
requires an explicit cutoff and baseline, and stops after Setup V1.
