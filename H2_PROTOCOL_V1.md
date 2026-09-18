# TRINITY/H2 Statistical Protocol V1

## Protocol identity and freeze

**Protocol:** `H2_PROTOCOL_V1`

**Status:** frozen before analysis. No historical H2/outcome relationship may be
inspected before this version is frozen.

**Frozen components:**

- Outcome Engine V0.2
- H2 TFIDF V0.1
- Earnings Pairing Engine V0.2
- Experimental Dataset Builder V0.1

This document defines the confirmatory test of H2A. It does not define or
authorize a trading strategy.

## Research hypothesis

H2 is textual novelty between a current earnings release and its previous
comparable earnings release:

```text
H2 = 1 - cosine_similarity(TF-IDF(current), TF-IDF(previous))
```

The confirmatory hypothesis H2A is:

> Greater textual novelty in an earnings release is associated with a greater
> absolute magnitude of the subsequent stock-price movement.

The directional statistical alternative is `Spearman rho > 0`. The null for
the primary test is `rho <= 0`.

## Unit of analysis and data freeze

The observational unit is one current earnings release represented by one flat
row from Experimental Dataset Builder V0.1. Each `current_release_id` may appear
at most once. Pair selection, H2, and sample eligibility must use only metadata
and text available no later than the current release. Future price observations
may be used only to construct outcome targets.

Before analysis, the dataset snapshot, code revision, protocol revision, and
execution configuration must be recorded. The complete audit dataset is
retained, including fallback, excluded, ambiguous, and pending rows.

No record may be selected or removed because of its H2 value or realized
outcome.

## Primary outcome

The sole primary outcome is `absolute_return_pct_20`, defined as the absolute
value of the raw return from the event reference price to the closing price at
`+20` trading sessions.

No other horizon or outcome may replace it after results are observed.

## Primary sample

The primary sample contains only complete cases satisfying all of the following
pre-specified conditions:

- `row_status == "INCLUDED"`;
- `pairing_quality == "EXACT"`;
- `h2_method == "TFIDF_COSINE_V1"`;
- `h2_novelty` is not null;
- `outcome_status_20 == "COMPLETE"`;
- `absolute_return_pct_20` is not null;
- `timing_ambiguous == false`;
- `reference_resolution != "AMBIGUOUS"`.

The final two guards make the temporal-validity requirement explicit.
`INTRADAY` and `UNKNOWN` events cannot enter the primary sample when the frozen
outcome engine cannot establish an unambiguous reference. No filter is applied
to the numerical value of H2, raw return, absolute return, or any other outcome.

The primary analysis is EXACT-only. Fallback pairs never enter the primary
sample.

## Primary analysis

The primary statistic is Spearman rank correlation between:

- predictor: `h2_novelty`;
- outcome: `absolute_return_pct_20`.

Ranks use average ranks for tied values. The following must be reported:

- Spearman `rho`;
- one-sided p-value for the alternative `rho > 0`;
- complete-case sample size `N`;
- number of distinct tickers;
- bootstrap 95% confidence interval for `rho`.

Pearson correlation is not a primary test.

The confirmatory primary p-value is cluster-aware and is defined below. A
standard tie-aware Spearman p-value may be reported only as a clearly labeled
naive diagnostic. It is not the confirmatory p-value and cannot be used in the
support criteria.

### Cluster-aware primary p-value

The confirmatory p-value uses a one-sided wild cluster bootstrap-t test on the
rank regression corresponding to Spearman association:

```text
rank(absolute_return_pct_20) = alpha + beta * rank(h2_novelty) + error
```

Average ranks are used for ties. The reported primary effect statistic remains
Spearman rho. The rank-regression slope has the same direction as rho; its
cluster-aware bootstrap-t test supplies the confirmatory p-value for the
pre-specified alternative `rho > 0` (equivalently, `beta > 0`).

The procedure is frozen as follows:

1. Fit the full rank regression and calculate the observed statistic
   `t_observed = beta_hat / SE_CR1(beta_hat)`. `SE_CR1` is the ticker-clustered
   sandwich standard error with finite-sample correction
   `G/(G-1) * (N-1)/(N-K)`, where `G` is the number of distinct tickers,
   `N` is the number of rows, and `K=2` is the number of regression parameters
   including the intercept.
2. Fit the restricted null model with `beta=0`, consisting of an intercept
   only, and retain its fitted values and residuals.
3. Generate exactly 10,000 requested wild-bootstrap replicates using a separate
   deterministic pseudorandom-number-generator instance initialized with seed
   `20260918`.
4. The resampling unit is the ticker cluster. In each replicate, independently
   draw one Rademacher weight per ticker (`-1` or `+1`, each with probability
   0.5) and apply that same weight to every restricted-model residual belonging
   to that ticker. Construct the bootstrap outcome ranks as restricted fitted
   values plus weighted residuals. Predictor ranks and cluster membership remain
   fixed.
5. Refit the full rank regression to each bootstrap sample and calculate
   `t_bootstrap = beta_bootstrap / SE_CR1(beta_bootstrap)` using the same CR1
   ticker-clustered variance definition.
6. With `B_valid` valid replicates, calculate the one-sided confirmatory
   p-value as
   `(1 + count(t_bootstrap >= t_observed)) / (1 + B_valid)`. The plus-one rule
   prevents a zero Monte Carlo p-value.

A replicate is invalid if the regression is singular, its cluster-robust
standard error is zero or non-finite, or its t-statistic is non-finite. Invalid
replicates are omitted from the p-value denominator and their count is reported.
If fewer than 9,500 of the 10,000 requested replicates are valid, the p-value is
declared not estimable. The test is also not estimable when `G < 2`, `N <= K`,
the predictor or outcome ranks are constant, or the observed cluster-robust
standard error is zero or non-finite. In every not-estimable case, H2A is
classified as `NOT CONFIRMED` under V1.

The implementation and library versions used for ranking, regression,
clustered covariance, and random-number generation must be recorded. This
method is fixed before results are inspected and may not be replaced according
to which inferential method yields a smaller p-value.

## Bootstrap confidence interval

The primary 95% confidence interval is a percentile cluster bootstrap with:

- cluster unit: ticker;
- resamples: 10,000;
- pseudorandom seed: `20260918`;
- sampling: tickers sampled with replacement, retaining all eligible rows for
  every sampled ticker;
- statistic: Spearman rho recalculated on each resample;
- interval: empirical 2.5th and 97.5th percentiles.

Ticker clustering is pre-specified because multiple releases from one company
are not assumed to be independent. When a ticker is drawn multiple times, its
rows are included once per draw as separate bootstrap clusters.

Undefined bootstrap replicates, such as resamples with constant H2 or outcome,
are recorded and omitted from percentile calculation. If fewer than 95% of the
10,000 requested replicates are valid, the confidence interval is declared not
estimable and H2A is `NOT CONFIRMED` under V1. The count of valid and invalid
replicates must be reported.

## Secondary outcomes

The following outcomes are pre-specified as secondary:

- `absolute_return_pct_1`;
- `absolute_return_pct_5`;
- `absolute_return_pct_60`;
- `abs(excess_return_pct_1)`;
- `abs(excess_return_pct_5)`;
- `abs(excess_return_pct_20)`;
- `abs(excess_return_pct_60)`;
- `max_favorable_excursion_pct_20`;
- `abs(max_adverse_excursion_pct_20)`;
- `realized_volatility_20`.

The key secondary outcome is `abs(excess_return_pct_20)`.

Absolute excess returns and absolute adverse excursion are derived only by
applying the absolute-value function to their frozen dataset fields. No other
transformation is selected after inspecting results.

For each secondary outcome, analysis uses rows satisfying the primary pairing,
H2, and temporal-validity criteria plus `outcome_status_<horizon> == "COMPLETE"`
and a non-null relevant outcome. Spearman rho, two-sided descriptive p-value,
N, and a 95% cluster-bootstrap interval are reported. Secondary hypotheses are
not substitutes for the primary hypothesis.

## Multiple testing

There is exactly one primary outcome and one confirmatory primary test. The
primary p-value is not included in a multiple-testing adjustment.

The ten pre-specified secondary Spearman p-values form one family. Their raw
p-values and Benjamini-Hochberg false-discovery-rate adjusted q-values are both
reported. The FDR level is `0.05`. Outcomes with unavailable data remain listed
as not estimable and are not silently removed from the protocol.

All subgroup, timing, quintile, winsorized, and other sensitivity results are
secondary or exploratory regardless of their nominal p-values.

## H2 quintile description

H2 quintiles are descriptive only. If the eligible sample permits exactly five
non-empty groups, rows are assigned to Q1 through Q5 using ascending H2 ranks,
and the mean, median, and N of each outcome are reported for each quintile.

Tied H2 values are not split by release ID, ticker, outcome, or input order. If
ties make five non-empty groups impossible, quintile analysis is reported as
not estimable; the number of buckets is not changed. No alternative bucket
count may be selected after viewing results.

## Sensitivity and subgroup analyses

The following are pre-specified, subject to data availability:

1. **Primary:** EXACT-only.
2. **Pairing sensitivity:** EXACT plus FALLBACK, with pairing quality retained
   and separately reported. This analysis cannot confirm H2A if the primary
   EXACT-only analysis fails.
3. **Sector-stratified description:** report N and descriptive association by
   a sector classification fixed independently of outcomes.
4. **Size-stratified description:** permitted only if point-in-time market
   capitalization, or a proxy defined and frozen before outcome inspection, is
   available. Contemporary or future market capitalization may not be used
   retroactively.

H2 is not recalculated, sector-adjusted, or assigned sector-relative
percentiles in V1. Sector and size results are descriptive and do not redefine
the primary test.

## Event timing analysis

PRE_MARKET and AFTER_MARKET observations are reported separately as descriptive
timing strata, using the reference established by the frozen outcome engine.
The combined eligible sample remains the primary analysis unless changed in a
future protocol version before that future analysis.

INTRADAY and UNKNOWN observations are excluded from analyses requiring an
outcome whenever their temporal reference is ambiguous. They remain in the
audit dataset.

## Missing data and pending outcomes

- H2 values are never imputed.
- Outcomes are never imputed.
- Benchmark returns and excess returns are never imputed.
- A PENDING record is excluded only from analyses requiring that specific
  horizon; it may remain eligible for another completed horizon.
- Pairing exclusions, ambiguous pairs, invalid-text rows, and missing-OHLCV
  rows remain in the audit dataset but do not enter the primary sample.
- Complete-case N is reported separately for every analyzed outcome.
- Missingness summaries are reported by exclusion reason, pairing quality,
  outcome horizon, and timing category without using them to alter eligibility.

## Robustness and reproducibility

The following rules are frozen:

- use the fixed bootstrap seed and resampling plan above;
- record code, dependency, dataset, and protocol versions;
- do not tune H2 thresholds;
- do not alter TF-IDF, tokenization, text normalization, or pairing after seeing
  results;
- do not manually remove boilerplate after seeing results;
- do not remove observations based on return magnitude;
- do not select horizons, transformations, subgroups, or covariates because
  they improve results;
- preserve enough intermediate counts and identifiers to reproduce every
  inclusion and exclusion.

## Outlier reporting

Extreme observations remain in the primary analysis and are listed separately
for audit using release ID, ticker, H2, outcome, and provenance fields.

A winsorized sensitivity may be reported separately using a rule fixed here:
winsorize the analyzed outcome, but not H2, at the empirical 1st and 99th
percentiles of that analysis sample. This result is always labeled sensitivity,
never replaces the unwinsorized primary result, and cannot rescue a failed
primary test. If the sample is too small for meaningful distinct percentile
cutoffs, the winsorized sensitivity is not reported.

## Success and failure criteria

H2A receives support under V1 only if all of the following hold in the primary
EXACT-only analysis:

- primary Spearman `rho > 0`;
- the bootstrap 95% confidence interval excludes zero and is wholly positive;
- the one-sided primary p-value is `< 0.05`;
- the key secondary `abs(excess_return_pct_20)` has a positive Spearman rho,
  although it need not be statistically significant.

H2A is not considered confirmed if an apparent signal:

- occurs only in subgroups selected after inspection;
- occurs only after changing the H2 method;
- occurs only after post-hoc removal of outliers;
- occurs only among FALLBACK pairs;
- depends on replacing the primary outcome or test.

If the primary test does not meet every stated support criterion, H2A is
classified as **NOT CONFIRMED in V1**. A not-confirmed result must be reported
and must not trigger post-hoc redefinition of V1.

## Interpretation and no-trading claim

Even a positive result demonstrates at most an association between textual
novelty and the magnitude of a future price movement. It does not establish
causality, direction of the price move, economic profitability, or a tradable
edge.

This protocol does not authorize or support:

- buy or sell signals;
- position sizing;
- stop-loss or take-profit rules;
- portfolio construction;
- execution rules;
- any trading strategy.

## Change control

Any subsequent change to any of the following requires a new protocol version,
such as `H2_PROTOCOL_V2`:

- H2 definition or normalization;
- earnings-release pairing;
- primary or sensitivity sample construction;
- primary outcome;
- primary statistical test or inferential direction.

A new version must describe and date the change and must coexist with V1. It
must not overwrite, relabel, or retroactively replace `H2_PROTOCOL_V1` or its
results. Corrections of typographical errors that do not change operational
meaning must still be logged; any ambiguity about substantive impact is
resolved in favor of issuing a new protocol version.
