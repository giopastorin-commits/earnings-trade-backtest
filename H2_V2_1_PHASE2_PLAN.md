# H2 V2.1 — Phase 2 implementation plan

**Frozen at:** 2026-11-21T12:22:41.4931125+01:00

**Governing documents:** `H2_PROTOCOL_V2_1.md` (prevails) and
`H2_PROTOCOL_V2.md`, especially V2.1 sections 4, 5-bis and 11 and V2 sections
7 and 8.

**Scope:** DEV outcomes, one frozen estimation of the six pre-specified
models, outcome-free PROSP_BATCH1 predictions, the first-lot seal, and—only
after that seal has been pushed—Part A on DEV. Part C and every PROSP outcome
are outside scope.

## 1. Frozen inputs and fail-closed checks

Execution first requires byte-for-byte SHA-256 equality for:

- DEV input:
  `research/h2_v2_1_phase1_1_split_factors/h2_v2_1_inputs_dev.csv`,
  `e1f3e7671422ece25cc6a0ffba9c4bbca4e11ad84d7e080bc349932466817501`;
- PROSP_BATCH1 input:
  `research/h2_v2_1_phase1_1_split_factors/h2_v2_1_inputs_prosp_batch1.csv`,
  `2a01fb5d0cbf8b110779bd61d2f94116877dde15fbef1dd4a91d757d3e9a1493`;
- unadjusted Twelve Data ensemble:
  `8dd59aae14c92f9d8dd9a6c055c166157e8b49b0d6ce0b48591080277ec7a54f`;
- `adjust=splits` Twelve Data ensemble:
  `44dc22079a57f4e60a07822c259503377bd80f72a04929bffc25db9a802c50d2`.

Any mismatch stops execution before an outcome is calculated. No source is
downloaded or substituted. The code rejects duplicate accessions.

## 2. DEV-only outcome boundary

Only rows in the frozen DEV file whose `row_status` is `INCLUDED` are passed
to Outcome Engine V0.2 (`trinity.outcomes.calculate_event_outcomes`) with
`horizons=(20,)`. The issuer series is the frozen unadjusted snapshot and the
benchmark is SPY from that same snapshot. The engine's resolved reference must
equal the frozen `reference_session_date`; a mismatch stops execution.

The price reader is DEV-specific and bounded: for each DEV event it decodes
the exact reference row and at most the next 20 issuer sessions. SPY is read
only through that event's D20. The PROSP prediction path accepts no price-root
argument and never imports or calls the outcome builder. No price level is
written to an artifact or log.

For every calculated DEV event the outcome file stores identifiers,
reference and D20 dates, `outcome_status_20`, `raw_return_pct_20`,
`benchmark_return_pct_20`, `excess_return_pct_20`, and
`absolute_return_pct_20`; it does not store reference or future prices. DEV
rows not calculated retain blank outcome fields and their existing reasons so
the file remains an audit dataset.

The D20 split screen uses the same decimal factor method as Phase 1.1-B on
common dates: `r_t = close_splits_t / close_none_t`, with a change when
`abs(r_t / r_(t-1) - 1) > 0.01`. Both series are bounded at the DEV event's
D20. A detected date in
`reference_session_date < date <= future_session_date_20` adds
`CORPORATE_ACTION_SPLIT` and makes the event ineligible. Missing adjusted
coverage through D20 adds `SPLIT_CHECK_UNAVAILABLE`. No return or unadjusted
price jump is used for this decision.

Final primary eligibility follows V2.1 section 4 exactly: included row, EXACT
pairing, frozen H2 method, non-null novelty, complete D20 outcome, non-null
absolute return, unambiguous timing/reference, and available `sigma_pre_60`.
`dollar_volume_pre_60 > 0` is required only for M0x/M1x. Since the specified
models require logarithms, a non-positive or non-finite `sigma_pre_60` in an
otherwise eligible row is a contract error rather than a new discretionary
exclusion.

The output is `research/h2_v2_1_phase2/h2_v2_1_dev_outcomes.csv`. Execution
prints only hashes and permitted counts. The overlap report is only the count
of distinct tickers having at least one DEV and one PROSP input event.

## 3. OLS implementation and matrices

OLS is implemented by `numpy.linalg.lstsq(X, y, rcond=None)` using Python
3.13.17 and NumPy 2.5.3. Inputs are converted to IEEE-754 `float64`. Each
matrix is checked for finite values, `N > K`, and full column rank before its
single fit. There is no regularization, variable selection, interaction,
weighting, imputation, winsorization, or refit.

Column order is frozen as follows:

- M0: `intercept`, `ln_sigma_pre_60`;
- M1: `intercept`, `ln_sigma_pre_60`, `h2_novelty`;
- M0x: `intercept`, `ln_sigma_pre_60`, `ln_dollar_volume_pre_60`,
  `log_word_ratio`, `log_word_count`, `after_market`;
- M1x: the M0x columns followed by `h2_novelty`;
- M0e: the M0 columns;
- M1e: the M1 columns.

For M0/M1/M0x/M1x,
`y = ln(1 + absolute_return_pct_20)`. For M0e/M1e,
`y = ln(1 + abs(excess_return_pct_20))`. Outcome values are in percentage
points, as required by V2. M0x/M1x use only the primary-eligible subset with
finite, strictly positive `dollar_volume_pre_60`; M0e/M1e use the primary
sample and require a complete benchmark-derived excess return.

For each model the estimation-data hash is SHA-256 over canonical UTF-8 JSON
containing, in sorted accession order, the accession, ordered matrix row and
transformed response. This exact payload is not written separately; its hash
and row count are stored in the model.

## 4. Frozen model format

`research/h2_v2_1_phase2/h2_v2_1_model.json` is canonical JSON with:

- schema name/version and build timestamp;
- the six named model objects;
- ordered columns and coefficient mapping;
- N, K, matrix rank, response definition and transformation;
- estimation-data SHA-256 and sorted estimation accessions;
- OLS function and `rcond`;
- DEV input/outcome hashes and governing commit hashes;
- Python, NumPy and pandas versions.

The model contains no inferential selection result. Its SHA-256 is recorded
before predictions are sealed.

## 5. PROSP_BATCH1 predictions without outcomes

Predictions are a pure function of the frozen model JSON and the frozen
PROSP_BATCH1 input CSV. For each `row_status == INCLUDED` event the applicable
ordered input vector is multiplied by the already frozen coefficient vector.
For every model, `pred = exp(y_hat) - 1` in percentage points. `pred_0x` and
`pred_1x` are blank unless `dollar_volume_pre_60` is finite and strictly
positive. The other four predictions follow their corresponding frozen
models and require only their specified input columns.

`research/h2_v2_1_phase2/h2_v2_1_predictions_batch1.csv` has exactly:
`current_release_id`, `ticker`, `reference_session_date`, `pred_0`, `pred_1`,
`pred_0x`, `pred_1x`, `pred_0e`, `pred_1e`. Rows are sorted by accession. It
contains neither outcomes nor prices. Non-included PROSP rows are not
predicted; their permitted count is reported.

## 6. Seal manifest

`H2_V2_1_SEAL_MANIFEST.json` is canonical JSON and records:

1. D-001 fallback provenance: trinity-online commit `40ae6cb`, provenance
   report SHA-256 supplied by the execution instruction, and frozen DEV hash;
2. the commit SHA that froze `trinity/pre_event_covariates.py` and its tests,
   plus their file SHA-256 values;
3. model SHA-256;
4. PROSP_BATCH1 input SHA-256;
5. PROSP_BATCH1 prediction SHA-256;
6. each PROSP event's already-frozen `price_window_sha256`, and a canonical
   ensemble hash over the sorted accession/hash pairs;
7. the unadjusted and split-adjusted source ensemble/manifest hashes;
8. the DEV outcome SHA-256 and permitted counts;
9. generation time, intended seal branch and the calendar-only count of
   PROSP events whose XNYS D20 is on or before the planned `S_seal`.

Because the seal commit is created on Saturday 2026-11-21, the last completed
XNYS session cannot change during the commit operation: `S_seal` is
2026-11-20. D20 dates for the seal count are obtained only from the XNYS
calendar, never from PROSP price data. The actual commit SHA and Git author
timestamp are reported immediately after the commit and push.

## 7. Mandatory order and Part A

The plan is committed before any outcome execution. Code/tests are then
implemented and validated on synthetic data. DEV outcomes, model,
outcome-free predictions and seal manifest are committed together and pushed.
Only after the remote seal exists may Part A run.

Part A uses the already frozen DEV estimation samples and performs exactly V2
section 8: M1 and M1x novelty coefficients with ticker-clustered CR1 standard
errors; restricted-null Rademacher wild cluster bootstrap-t with 10,000
replicates and seed 20261118; M1 calendar-quarter fixed-effect sensitivity;
calendar-year release subperiod coefficients; and descriptive in-sample
delta R-squared and delta MAE for M0 versus M1. Its report heading is
`Analisi di robustezza, non confermativa (N piccolo)`. Part A neither reads nor
analyzes PROSP outcomes.
