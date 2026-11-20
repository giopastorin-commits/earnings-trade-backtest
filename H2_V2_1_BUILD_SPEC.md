# H2 V2.1 — Phase 1 input build specification

**Specification ID:** `H2_V2_1_BUILD_INPUTS_V1`  
**Status:** frozen before SEC-document or price-bar acquisition  
**Starting branch:** `research/h2-pre-event-covariates-v0-1`  
**Starting commit:** `bee6463372e541519aae88cd98481bba27839875`  
**Build branch:** `research/h2-v2-1-build-inputs`

This document fixes the complete Phase 1 construction contract. Phase 1 creates
input snapshots and hashes only. It must not call `calculate_event_outcomes` or
`build_experimental_dataset`, must not read an event's price rows after its
`reference_session_date`, and must not estimate a model, generate a prediction,
or calculate, load, display, or summarize an outcome.

## 1. Governing documents and recorded decision

`H2_PROTOCOL_V2_1.md` prevails over `H2_PROTOCOL_V2.md`.

| Document | SHA-256 in the starting commit |
| --- | --- |
| `H2_PROTOCOL_V2_1.md` | `b29dd07407293cccecd2be8187593f749a5827657420c972c99a918ebb60a572` |
| `H2_PROTOCOL_V2.md` | `02fd5790d47714a6fe273553e4ddac1d579bb20833ff54c69879ed4c7108b728` |

R-001 and D-001 were read from
`giopastorin-commits/trinity-online`, branch
`maintenance/pausa-ingest-notturno`, file
`research/REGISTRO_ESPERIMENTI.md`, commit `40ae6cb`. D-001 applies the V2.1
fallback early: the development sample is made from otherwise admissible events
with `release_at >= 2026-09-21T00:00:00Z` and reference session no later than
2026-10-20. There is no holdout Part B. No file in `trinity-online` is modified.

## 2. Frozen universe

The universe is the set of records for which `supported == true` in
`trinity/usa_issuer_registry_v2.fixture`, which is `DEFAULT_REGISTRY` in the
starting commit.

- Supported tickers: **518**.
- Fixture SHA-256:
  `957f2686fcb5b4beace10c9dbe8b88e6383ecd1fc423cf6e2e8c836f3a4c1097`.
- The iteration order is ascending canonical ticker. No ticker may be added,
  removed, or substituted after this specification is committed.
- SPY is not a member of the issuer universe. It is acquired only as the future
  benchmark, from the same price provider and in the same immutable price
  ensemble described in section 10.

## 3. SEC access contract

- User-Agent: `Gioele Pastorin giopastorin@gmail.com`.
- Maximum request rate: **8 requests/second**, below the SEC ceiling of 10.
- Every HTTP request uses a 45-second timeout and at most three attempts for
  transient HTTP 429/5xx or connection/timeout failures, with deterministic
  backoff of 0.5, 1.0, then 2.0 seconds.
- Raw responses are cached outside the Git repository under
  `C:\Users\giopa\h2-v2-1-phase1-private\sec` and are never silently replaced.
  A cached response is reused only when its URL and SHA-256 are recorded in the
  acquisition manifest.
- Endpoints are limited to:
  `https://data.sec.gov/submissions/CIK##########.json`, additional submissions
  files referenced by that response,
  `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`, and complete
  filing submissions at
  `https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_without_dashes}/{accession_with_dashes}.txt`.
- Acquisition records URL, UTC retrieval time, byte length, HTTP metadata needed
  for audit, and SHA-256. Credentials do not exist in this flow.

The SEC acquisition cutoff is captured once as `run_started_at_utc` before the
first SEC request. Filings with `acceptanceDateTime` later than that cutoff are
not included in this Phase 1 run.

## 4. Candidate events and EX-99 selection

An initial candidate is a filing for a supported issuer that meets all of the
following:

1. form is exactly `8-K`; `8-K/A` and every other form are excluded;
2. the SEC `items` field contains item `2.02` as a comma-delimited item, not as
   a substring of another item;
3. `acceptanceDateTime <= run_started_at_utc`;
4. the filing is not a duplicate accession already seen for that issuer.

One accession number is one event. `release_id` and `current_release_id` are the
accession number including dashes. `release_at` is the SEC
`acceptanceDateTime`, parsed as an aware instant and serialized in UTC with a
trailing `Z`. `acceptanceDateTime` is preserved verbatim in a separate column.
The build records that SEC acceptance can occur after the first public
dissemination of the release.

The complete filing submission is parsed in document order. Document `TYPE` is
trimmed and upper-cased. The selected exhibit is:

1. the first document whose normalized type is exactly `EX-99.1`;
2. otherwise the first document whose normalized type begins with `EX-99`;
3. if neither exists, the event is written only to the exclusions file with
   reason `NO_EX99`.

No filename heuristic overrides the `TYPE` rule. The selected document type,
filename, source URL, raw SHA-256, and document ordinal are recorded.

### Amendment 2026-11-19T19:58:29.815+01:00 — multiple share classes

This dated amendment was committed before recalculating or rebuilding any
Phase 1 output after the duplicate-share-class condition was discovered.  It
supersedes any earlier implementation behavior that emitted one copy of an
SEC event for every supported security of the same issuer.

1. An accession number is one event across the entire build.  Event and
   historical-pairing rows are deduplicated globally by accession number; the
   same filing text and H2 value must never be repeated for multiple share
   classes.
2. For each CIK, the event ticker is the first ticker in the root `tickers`
   array of that CIK's cached SEC submissions JSON that is present in
   `REGISTRY.supported`.  If the first array member is not supported, selection
   proceeds in the SEC-provided order until the first supported ticker.  No
   alphabetical, price, volume, liquidity, coverage, or other data-based rule
   may override this order.  The build fails closed if the SEC array has no
   supported ticker.
3. The selected ticker is used for every release of that CIK in DEV,
   PROSP_BATCH1, and the history from 2025-01-01 used for pairing.  Pairing is
   therefore performed once per issuer, under the same selected ticker.
4. The committed `h2_v2_1_share_class_resolution.json` records every CIK with
   more than one supported registry ticker, the complete SEC `tickers` array
   in its original order, the selected ticker, all non-selected supported
   tickers, the cached submissions source path and SHA-256, and the
   `retrieved_at_utc` acquisition timestamp from that source's cache metadata.
5. For each relevant accession and each non-selected supported class, the
   exclusions CSV contains a row whose `ticker` is the non-selected class,
   whose reason is `DUPLICATE_SHARE_CLASS`, and whose detail identifies the CIK
   and selected ticker.  This applies to historical releases as well as DEV
   and PROSP releases.  These rows audit security aliases: they are reported
   separately and never count as candidate events or as excluded events in
   sample eligibility counts.
6. Price coverage is evaluated only for the SEC-selected ticker.  Insufficient
   coverage yields `PRICE_COVERAGE`; it never triggers substitution of another
   share class.

The Phase 1 report separately gives duplicate-share-class alias-row and unique
accession counts for DEV, PROSP_BATCH1, and historical pairing input, and
identifies whether the condition occurs in each scope.

#### Registry-field clarification 2026-11-19T20:08:11.833+01:00

The SEC array is compared to the `sec_ticker` field of supported registry
records for the same CIK, because that is the registry's explicit SEC symbol
(for example SEC `BF-B` maps to canonical event ticker `BF.B`).  The selected
event ticker written to outputs is the corresponding canonical registry
`ticker`.  This is an identity mapping supplied by the frozen registry, not a
symbol heuristic or a price-based substitution.  The share-class resolution
manifest records both the selected SEC ticker and the selected canonical
ticker.  The build fails closed on a missing, conflicting, or non-unique
registry mapping.

#### Missing-SEC-ticker fallback 2026-11-20T09:46:32.021+01:00

This fallback applies only when no element of the SEC submissions `tickers`
array maps through `sec_ticker` to a supported registry record for the same
CIK.  It covers both an empty SEC array and a non-empty array with no registry
match.

1. If exactly one `REGISTRY.supported` record has that CIK, its canonical
   `ticker` is used for the event.  The resolution manifest records the CIK,
   complete SEC array (including an empty array), canonical registry ticker,
   cached-submissions acquisition timestamp, and reason
   `SEC_TICKERS_EMPTY` or `SEC_TICKERS_NO_REGISTRY_MATCH`.
2. If more than one supported registry record has that CIK, no ticker is
   selected.  Every release of that CIK, including historical pairing input,
   is written once to exclusions with reason `AMBIGUOUS_TICKER_MAPPING` and is
   not processed for text, H2, pairing, covariates, or sample eligibility.
3. The rule applies identically to DEV, PROSP_BATCH1, unassigned events, and
   history from 2025-01-01.  The report gives, for each fallback CIK, unique
   event counts by DEV, PROSP_BATCH1, UNASSIGNED, outside-window, and historical
   pairing scope.
4. A registry fallback never authorizes joining or substituting time series.
   In particular, CIK `0002041610` uses only the explicitly required PSKY
   price series; PARA or any predecessor ticker is forbidden.  Insufficient
   PSKY history yields `PRICE_COVERAGE`.
5. Pairing remains confined to releases of the exact same CIK.  Releases of a
   predecessor CIK are never admitted as prior releases, including for PSKY.

The resolution manifest distinguishes the normal SEC-order rule, unique-CIK
registry fallback, and ambiguous mapping.  No rule consults price availability,
volume, liquidity, H2, or any other analytical value.

#### PSKY provider-symbol clarification 2026-11-20T10:34:04.071+01:00

The frozen registry record for CIK `0002041610` has canonical event ticker
`SKYD` but the approved price-series identity is `PSKY`.  Accordingly:

- event rows retain canonical registry ticker `SKYD`;
- the Twelve Data request for that canonical event ticker uses provider symbol
  `PSKY`, and the source manifest records the explicit `SKYD -> PSKY` override;
- normalized/bounded storage may remain keyed by canonical ticker `SKYD`, but
  its raw response, returned provider symbol, and hashes must all derive only
  from the PSKY request;
- no SKYD, PARA, or other predecessor price series may be concatenated,
  substituted, or used to fill PSKY coverage;
- the earlier A1 price ensemble, which requested SKYD, is not eligible for the
  final build.  A new complete immutable 518-ticker-plus-SPY ensemble is
  acquired from Twelve Data with the same 2026-11-17 target and fixed request
  parameters.  No file from A1 is reused or mixed into the new ensemble.

## 5. Timing classification and descriptive release period

`release_at` is converted to `America/New_York`. The XNYS calendar comes from
`exchange_calendars==4.13.2`.

- On an XNYS session day, local time before 09:30 is `PRE_MARKET`.
- On an XNYS session day, local time from 09:30 inclusive to 16:00 exclusive is
  `INTRADAY`.
- On an XNYS session day, local time at or after 16:00 is `AFTER_MARKET`.
- On a non-session day, timing is `PRE_MARKET`.
- A missing, naive, or invalid acceptance timestamp is `UNKNOWN`.

`INTRADAY` and `UNKNOWN` events are never assigned to DEV or PROSP. They appear
only in `h2_v2_1_exclusions.csv`, with `sample=UNASSIGNED` and respectively
`TIMING_INTRADAY` or `TIMING_UNKNOWN`.

Every output row also has the purely descriptive `release_period`, based only
on the New York calendar date of `release_at`:

- `DEV_PERIOD`: 2026-09-21 through 2026-10-20 inclusive;
- `PROSP_PERIOD`: 2026-10-21 or later;
- `OTHER`: every earlier date or an unavailable valid local date.

`release_period` never assigns a sample, changes eligibility, or supplies a
reference session. The Phase 1 report gives INTRADAY/UNKNOWN counts separately
from DEV and PROSP, split by `release_period`.

## 6. Reference session

Reference sessions are resolved from the XNYS schedule, not from price bars.
The rule is the calendar equivalent of Outcome Engine V0.2
`_resolve_reference`:

- `PRE_MARKET` on a session day: the immediately preceding XNYS session;
- `AFTER_MARKET` on a session day: that XNYS session;
- a non-session day (classified `PRE_MARKET`): the last XNYS session before
  that day;
- `INTRADAY` or `UNKNOWN`: no reference session and
  `reference_resolution=AMBIGUOUS`.

Resolution labels are respectively `PREVIOUS_SESSION_CLOSE`,
`EVENT_SESSION_CLOSE`, and `LAST_SESSION_BEFORE_NON_TRADING_EVENT`.

Synthetic tests must prove equivalence with `_resolve_reference` for
PRE_MARKET, AFTER_MARKET, a weekend, and an XNYS holiday. Tests may call
`_resolve_reference` only with synthetic bars and may not call
`calculate_event_outcomes`.

## 7. Sample assignment

`S_last` for this build is fixed as **2026-11-17**, the last completed XNYS
session before execution on 2026-11-18.

- `DEV`: valid timing, `release_at >= 2026-09-21T00:00:00Z`, and
  `reference_session_date <= 2026-10-20`.
- `PROSP_BATCH1`: valid timing and `reference_session_date` from 2026-10-21
  through 2026-11-17 inclusive.
- Valid-timing events that meet neither definition are excluded as
  `OUTSIDE_SAMPLE_WINDOW`.
- INTRADAY/UNKNOWN remain `UNASSIGNED` as fixed in section 5.

Later prospective batches through reference 2026-12-11 are not built in this
run. They must reuse this code and contract in separate commits.

## 8. Point-in-time fiscal period

The same rule is applied to every current event and to every historical release
used for pairing.

For a release, select the accepted filing with the greatest
`acceptanceDateTime < release_at` whose form is exactly `10-Q` or `10-K`.
Amendments are not eligible. Preserve its accession, form, acceptance time, and
SEC submissions `reportDate`.

### Recorded technical deviation

The SEC submissions JSON contains `reportDate` but does not contain `fy` or
`fp`. Therefore the requested fields cannot be obtained from submissions alone.
Before data acquisition this specification fixes the following deviation:

1. `reportDate`, form, accession, and acceptance time come from SEC
   submissions.
2. `fy` and `fp` come from SEC Company Facts entries matched to the same
   accession. Candidate metadata tuples must have an integer `fy`, an `fp` in
   `{Q1,Q2,Q3,FY}`, and `end == reportDate`.
3. If exactly one distinct `(fy, fp, end)` tuple survives, use it. If none or
   more than one survives, fiscal metadata is null. No arbitrary majority or
   lexical tie-break is allowed.

The release fiscal period is the immediate successor:

| Latest periodic filing metadata | Release period |
| --- | --- |
| `fy=N, fp=Q1` | `fiscal_year=N`, `fiscal_quarter=2`, `period_type=QUARTERLY` |
| `fy=N, fp=Q2` | `fiscal_year=N`, `fiscal_quarter=3`, `period_type=QUARTERLY` |
| `fy=N, fp=Q3` | `fiscal_year=N`, `fiscal_quarter=4`, `period_type=QUARTERLY`, label `FY/Q4` |
| `fy=N, fp=FY` | `fiscal_year=N+1`, `fiscal_quarter=1`, `period_type=QUARTERLY` |

If the predecessor or its unique SEC metadata tuple is unavailable, fiscal
year and quarter are null and `period_type=UNKNOWN`. Pairing may then use its
frozen chronological fallback; the row cannot be primary `EXACT`.

Synthetic tests cover Q1→Q2, Q2→Q3, Q3→FY/Q4, FY→next-year Q1, missing
metadata, an ambiguous Company Facts match, and the strict point-in-time
acceptance boundary.

## 9. Historical releases, text, pairing, and H2

Historical pairing candidates are eligible 8-K earnings releases with
`acceptanceDateTime >= 2025-01-01T00:00:00Z` and earlier than the current
release. They use the same event, exhibit, timing-independent text, and fiscal
rules as current releases. `document_type` is always `EARNINGS_RELEASE`.

The selected EX-99 bytes are converted to text using the same algorithm as
`trinity/usa_documents.py::_plain_text`: BeautifulSoup `html.parser`, removal
of `script`, `style`, and `ix:hidden`, newline extraction, horizontal-space
collapse, three-or-more-newline collapse, and outer trim. The extracted text
is then normalized by `trinity.h2_novelty.normalize_text`. The normalized UTF-8
bytes are the text artifact hashed in the snapshots.

- Extraction version: `USA_DOCUMENTS_PLAIN_TEXT_V1`.
- H2 normalization/method: `TFIDF_COSINE_V1`.
- Pairing component: the frozen `trinity/earnings_pairing.py`.
- Current and previous normalized text SHA-256 values and component file hashes
  are recorded per row.

Component hashes at the starting commit:

| Component | SHA-256 |
| --- | --- |
| `trinity/usa_documents.py` | `24dd2f46a8187d2ca71be85ccb0c92dc33ba31de859fc4d2cd961b5db867b160` |
| `trinity/h2_novelty.py` | `2c53f69075925afb02a024c4e03b3f6a3ecbde292f49366e3e9ba19c63d06e9e` |
| `trinity/earnings_pairing.py` | `e93c2a2da32a05f5f2db1b590625869870ae505c43fd4c43cd649ff7fe0980f8` |
| `trinity/pre_event_covariates.py` | `3e1267ccf83076b443ec6f300ad9a8f73e5daec141708403f2dd5a0a96aadaf3` |

Pairing quality is mapped without discretion:

- `EXACT`: matched with `EXACT_PREVIOUS_FISCAL_PERIOD`;
- `FALLBACK`: matched with `CHRONOLOGICAL_COMPATIBLE_FALLBACK`;
- `NO_MATCH`: pairing status `NO_MATCH`;
- `AMBIGUOUS`: pairing status `AMBIGUOUS`.

H2 is computed only for a matched pair with both normalized texts. No H2 value
is imputed.

## 10. Single immutable price source

### R2 check and deviation fixed before acquisition

Read-only R2 credentials are not available in the execution environment. The
latest locally verifiable immutable production snapshot is
`TWELVEDATA_2026-10-26_RUN_40432096545-A1`, snapshot SHA-256
`dfe144f423945270d3671a9501f6f79198f3ffdab5a511d7dad678f0dd3b8fe8`.
It cannot cover PROSP_BATCH1 through 2026-11-17 and therefore is not used at
all. No price file from it may be mixed into this build.

The approved alternative is one new local immutable Twelve Data ensemble with
target session **2026-11-17**. It supplies every DEV and PROSP ticker and SPY.
It is kept outside Git at
`C:\Users\giopa\h2-v2-1-phase1-private\prices\TWELVEDATA_2026-11-17_RUN_H2V21_PHASE1_20261118_A1`.
There is no R2 write.

### Request contract

- Provider: `TWELVE_DATA`.
- Endpoint: `GET https://api.twelvedata.com/time_series`.
- Parameters for every issuer and SPY:
  `interval=1day`, `start_date=2025-08-24`,
  `end_date=2026-11-18` (exclusive), `outputsize=5000`, `adjust=none`, and the
  symbol (`provider_symbol` for issuers, `SPY` for the benchmark).
- `adjust=none` means unadjusted provider OHLCV: no split adjustment and no
  dividend adjustment is requested. The same contract is used for all symbols.
- Throttle and retry contract is the frozen `trinity/twelvedata_prices.py`
  implementation: at most 7 credits per rolling 61 seconds, three attempts,
  and the production credit guard.
- The issuer acquisition uses `acquire_snapshot`; SPY is acquired through the
  same `TwelveDataClient.fetch` and `normalize_response` contract before the
  ensemble is sealed.

Immediately after all downloads, before any event covariate is built, the
acquisition records the UTC start/end times and creates a sorted inventory of
every raw, normalized, telemetry, benchmark, and manifest file with relative
path, byte length, and SHA-256. The ensemble SHA-256 is the SHA-256 of the
canonical UTF-8 JSON serialization of that sorted inventory. The source
manifest records endpoint parameters and adjustment policy. Every source file
is then marked read-only. Hash verification must pass before and after the
Phase 1 build. Raw price files and secrets are never committed.

Coverage is verified without computing price statistics:

- every event needs its exact reference session and 60 preceding XNYS sessions,
  hence 61 closes ending at reference;
- the snapshot as a whole must cover at least 61 XNYS sessions before the first
  DEV event;
- a missing ticker, missing reference session, fewer than 61 required sessions,
  or invalid required OHLCV is `PRICE_COVERAGE` for that event;
- no alternate provider, old snapshot, or per-ticker substitution is allowed.

## 11. Price-read boundary and covariates

The immutable normalized price arrays are converted once, before analysis, to
an external hash-bound JSONL mirror ordered by ascending session. The mirror is
part of the same ensemble inventory. Each event reader opens that ticker's
JSONL file and stops immediately after reading the exact
`reference_session_date`. It does not read or decode a subsequent line. If the
reference is absent, date tokens may be inspected to establish the gap, but
OHLCV fields after the reference may not be decoded or returned.

Only this bounded reader may supply bars to
`compute_pre_event_covariates`. The function receives an already bounded
sequence; it remains a second defensive truncation. Tests use a sentinel row
after the reference that raises if accessed and assert that changing any
post-reference row cannot change output or hashes.

The only price-derived Phase 1 fields are those returned by
`trinity/pre_event_covariates.py`: `sigma_pre_60`,
`dollar_volume_pre_60`, status fields, `price_window_start`, and
`price_window_sha256`. No future-price field and no outcome field is created.

## 12. Output rows, eligibility, and exclusions

`h2_v2_1_inputs_dev.csv` and `h2_v2_1_inputs_prosp_batch1.csv` contain one
row for every valid-timing event that passed the EX-99 selection rule and was
assigned to that sample, including rows later found ineligible. `NO_EX99`,
INTRADAY, and UNKNOWN rows are not present in these two files and remain only in
the exclusions file.

The fixed leading columns are:

`sample`, `release_period`, `row_status`, `exclusion_reasons`,
`current_release_id`, `ticker`, `release_at`, `acceptanceDateTime`,
`event_timing`, `timing_ambiguous`, `reference_resolution`,
`reference_session_date`, `previous_release_id`, `pairing_quality`,
`current_text_sha256`, `previous_text_sha256`, `h2_similarity`, `h2_novelty`,
`current_word_count`, `previous_word_count`, `sigma_pre_60`,
`dollar_volume_pre_60`.

They are followed by audit metadata: fiscal-period source fields, pairing
method/status, H2 method, covariate statuses, price-window hash, selected
EX-99 identity, price/universe/source identifiers and hashes, component
versions and component SHA-256 values. There are no outcome, model, or
prediction columns.

Primary Phase 1 eligibility is exactly:

- assigned to DEV or PROSP_BATCH1;
- selected EX-99 and normalized current text available;
- `pairing_quality == EXACT`;
- H2 method `TFIDF_COSINE_V1` and H2 value available;
- timing is PRE_MARKET or AFTER_MARKET and resolution is not ambiguous;
- `sigma_pre_60` is available.

`dollar_volume_pre_60` is recorded but is not required for primary
eligibility under V2.1. It remains required only for future secondary models,
which are outside Phase 1.

`h2_v2_1_exclusions.csv` has one row per event per exclusion reason, so no
reason is lost. Reasons are from this closed set:

`NO_EX99`, `TIMING_INTRADAY`, `TIMING_UNKNOWN`, `OUTSIDE_SAMPLE_WINDOW`,
`FISCAL_PERIOD_UNKNOWN`, `PAIRING_FALLBACK`, `PAIRING_NO_MATCH`,
`PAIRING_AMBIGUOUS`, `TEXT_UNAVAILABLE`, `H2_UNAVAILABLE`, `PRICE_COVERAGE`,
`SIGMA_PRE_60_MISSING`, `DUPLICATE_SHARE_CLASS`,
`AMBIGUOUS_TICKER_MAPPING`.

`FISCAL_PERIOD_UNKNOWN` is auditable but is not a second exclusion when the
same row is already classified as pairing fallback/no-match/ambiguous; all
applicable reasons are nevertheless retained as separate rows.

## 13. Determinism and hashing

- Universe iteration: ascending ticker.
- Release history: ascending `(release_at, release_id)`.
- Input rows: ascending `(reference_session_date, release_at, ticker,
  current_release_id)`.
- Unassigned exclusions: ascending `(release_at, ticker, current_release_id,
  reason)`.
- CSV: UTF-8 without BOM, LF line endings, RFC 4180 quoting, fixed column order,
  empty string for null, booleans `true`/`false`, finite floats serialized with
  Python's deterministic shortest round-trip representation.
- JSON manifests: UTF-8, sorted keys, no insignificant whitespace, final LF.
- The builder refuses to overwrite a non-empty output directory.
- A repeated synthetic build with shuffled input order must produce byte-for-byte
  identical outputs and hashes.

Every output file, SEC normalized-text artifact, price-source file, source
ensemble, universe fixture, and relevant component receives a SHA-256. The
committed price-source manifest contains only identifiers, request metadata,
hashes, and coverage status—not raw prices or credentials.

## 14. Tests and execution gates

Before real acquisition, tests must cover:

1. timing boundaries and non-session handling;
2. XNYS reference resolution and synthetic equivalence with
   `_resolve_reference` for PRE_MARKET, AFTER_MARKET, weekend, and holiday;
3. fiscal-period successor rules and ambiguous/missing metadata;
4. EX-99.1 preference and first-EX-99 fallback;
5. strict price-read truncation with an access sentinel after reference;
6. `PRICE_COVERAGE` without provider substitution;
7. deterministic output and hashes under shuffled input order;
8. absence of outcome/prediction columns and imports/calls to
   `calculate_event_outcomes` or `build_experimental_dataset`.

All existing `tests/trinity` tests must continue to pass. Real execution is
allowed only after the specification commit exists and the new tests pass.

## 15. Phase 1 report and permitted counts

`PHASE1_REPORT.md` records the final commit SHA, hashes of all committed files,
the universe and source-price hashes, acquisition timestamps, component
versions, and deviations. It reports only these permitted counts:

- candidate events;
- pairing `EXACT`, `FALLBACK`, `NO_MATCH`, `AMBIGUOUS`;
- INTRADAY and UNKNOWN separately, split by `release_period`, outside the DEV
  and PROSP sample tables;
- exclusions by reason;
- missing `sigma_pre_60`;
- `sigma_pre_60 == 0`;
- final primary-eligible rows;
- distinct ISO reference weeks in PROSP_BATCH1.

`DUPLICATE_SHARE_CLASS` audit rows and unique accessions are shown separately
for DEV, PROSP_BATCH1, and historical pairing input.  They do not enter the
candidate-event or excluded-event counts above.

No means, quantiles, distributions, correlations, regressions, price summaries,
H2 summaries, covariate summaries, outcome summaries, models, or predictions are
permitted. Phase 1 stops after committing and pushing the specification, code,
tests, input CSVs, exclusions, manifests/hashes, and report on
`research/h2-v2-1-build-inputs`.

## 16. Amendment 2026-11-20T16:23:34.6062791+01:00 — Phase 1.1 corporate-action split screen

This amendment is frozen and committed before the first request for split
data. It extends the Phase 1 input snapshot with a provider-reported corporate
action screen, without calculating an outcome, return, model, or prediction.
All Phase 1 prohibitions and the price-read boundary remain in force.

### 16.1 Frozen acquisition universe and provider contract

The split-acquisition universe is the sorted union of canonical event tickers
from:

1. `h2_v2_1_inputs_dev.csv`;
2. `h2_v2_1_inputs_prosp_batch1.csv`; and
3. `h2_v2_1_exclusions.csv` rows whose `sample` is `DEV` or
   `PROSP_BATCH1`, excluding `DUPLICATE_SHARE_CLASS` alias rows.

Empty tickers are rejected. The resulting frozen universe contains **458
canonical tickers**. It is derived from the input snapshot committed at
`546ea38f7376adf9480a0da07048ed8ecc561ea3`; the manifest records the sorted
canonical ticker list and its canonical-JSON SHA-256.

The provider is the same provider as the price snapshot, `TWELVE_DATA`. Each
canonical ticker is requested separately from:

`GET https://api.twelvedata.com/splits?symbol={provider_symbol}`

The provider symbol equals the canonical ticker except for the already frozen
override `SKYD -> PSKY`. No request for SKYD, PARA, or another predecessor is
permitted for canonical SKYD. No alternate symbol or provider may replace a
failed response.

The official endpoint cost recorded before acquisition is 20 API credits per
symbol. The implementation checks provider API usage before the first split
request and requires at least **9,192 available daily credits**: 9,160 for the
458 symbols plus the already frozen 32-credit safety reserve. If the guard
fails, no split request is made and no partial snapshot is eligible. A later
retry must use a new empty run directory.

The `/splits` endpoint returns the available history for one symbol and does
not use a client-supplied date range in this contract. Raw responses are
preserved unchanged. Normalized rows are filtered locally to split date from
**2025-08-24 through 2026-11-20 inclusive**, where 2026-11-20 is the frozen
“today” date for this Phase 1.1 run. The normalized schema preserves, when
present, `date`, `description`, `ratio`, `from_factor`, `to_factor`, and any
provider-reported action/type field. Rows are sorted deterministically by
date and their canonical JSON representation.

The immutable snapshot root is:

`C:\Users\giopa\h2-v2-1-phase1-private\splits\TWELVEDATA_SPLITS_2025-08-24_2026-11-20_H2V21_PHASE1_1_A1`

It must be new and empty. The manifest records endpoint, request parameters,
canonical and provider symbols, acquisition start/end timestamps in UTC,
response classification/error, raw and normalized file SHA-256 values, the
complete sorted inventory, and an ensemble SHA-256. Raw files and credentials
remain outside Git. The snapshot is made read-only after sealing and its
hashes must verify before and after the regenerated input build.

### 16.2 Phase 1/2 pre-reference exclusion

For every DEV or PROSP_BATCH1 input row with both a valid
`price_window_start` and `reference_session_date`, add exclusion reason
`CORPORATE_ACTION_SPLIT` if at least one normalized provider action has an
effective/split date in the interval:

`price_window_start < split_date <= reference_session_date`

The lower bound is strictly excluded and the reference-session bound is
included. The decision uses only ticker identity, the provider action date,
and the two already committed calendar dates. It never uses a price, return,
H2 value, covariate value, price jump, volume, or any other data-dependent
signal. A matching row becomes `row_status=EXCLUDED`; the exclusions CSV gains
one `CORPORATE_ACTION_SPLIT` row per affected accession, with all matching
action dates recorded in deterministic order in `detail`. An already excluded
row retains all existing reasons and also receives this reason.

If either boundary is missing, the interval is not defined and no split
classification is asserted; the row retains its existing missing-price or
other exclusion reasons. Events that exist only as `NO_EX99`, INTRADAY, or
UNKNOWN exclusion rows are not reintroduced into the input CSVs.

Phase 1.1 creates a new, non-overwriting output directory
`research/h2_v2_1_phase1_1_split`. The regenerated CSVs preserve the Phase 1
column order and add no outcome, model, prediction, or future-price column.
Their manifest binds the Phase 1 parent snapshot commit and hashes, the split
ensemble hash, and all regenerated file hashes.

### 16.3 Post-reference rule reserved for Phase 2

For DEV in Phase 2, and for PROSP only when its outcome batch is authorized
later, exclude an event with `CORPORATE_ACTION_SPLIT` if a provider action date
falls in:

`reference_session_date < split_date <= D20_session_date`

The reference-session bound is strictly excluded and D20 is included. D20 is
the XNYS session selected by the frozen outcome-engine horizon rule. Phase 1.1
does not calculate D20 outcomes and does not invoke the outcome engine. A
Phase 2 batch may apply this rule only if its immutable split snapshot covers
the batch's entire D20 interval; otherwise it must acquire and seal a later
snapshot before calculating outcomes. The decision is calendar-only and may
not inspect returns.

### 16.4 Other corporate actions and dividends

Every row returned by the Twelve Data `/splits` endpoint is screened under the
same interval rules, including provider-reported split and reverse-split rows.
If that endpoint explicitly reports a spin-off or extraordinary distribution,
it is screened identically and the provider description/type is preserved.
No such action may be inferred from a price or volume discontinuity.

The `/splits` endpoint may not provide a complete spin-off or extraordinary-
distribution calendar. Any absence is a known limitation, not evidence that
no such action occurred. Ordinary dividends do not cause an exclusion and are
a separate known limitation: future outcomes are price returns, not total
returns.

### 16.5 Tests, report, and permitted counts

Before real split acquisition, synthetic tests must cover both interval
boundaries, split and reverse-split rows, multiple action dates, no match,
provider failure without substitution, the SKYD-to-PSKY request mapping,
deterministic hashing, and preservation of existing reasons. They must also
prove that Phase 1.1 imports or calls neither `calculate_event_outcomes` nor
`build_experimental_dataset`.

The Phase 1.1 report records source and output hashes and, per DEV and
PROSP_BATCH1, only the number of unique events newly carrying
`CORPORATE_ACTION_SPLIT` and the resulting primary-eligible count. It may also
report acquisition-status counts as source-provenance metadata. It must not
summarize split ratios, H2, covariates, prices, returns, or outcomes.

## 17. Amendment 2026-11-20T18:49:21.9071987+01:00 — Phase 1.1-B split-adjustment factors

This amendment supersedes section 16 in full. The `/splits` endpoint and every
normalization or credit rule specific to it are no longer authorized. The
failed section-16 A1 attempt made exactly one `/api_usage` request, acquired no
split data, and is not a source for Phase 1.1-B.

Before this amendment, the official Twelve Data documentation was checked and
confirmed both conditions required to proceed:

- `/time_series` supports `adjust=splits` (along with `all`, `dividends`, and
  `none`); and
- `/time_series` costs **1 API credit per symbol**.

Documentation checked:
`https://twelvedata.com/docs/ai/ai-data-assistant` and
`https://support.twelvedata.com/en/articles/5615854-credits`.

No `adjust=splits` data was requested before this amendment was committed.

### 17.1 Frozen universe, request, and multi-day acquisition

The canonical ticker universe remains the 458-ticker sorted union defined in
section 16.1, derived from the Phase 1 snapshot commit
`546ea38f7376adf9480a0da07048ed8ecc561ea3`. The provider symbol equals the
canonical ticker except for the already frozen `SKYD -> PSKY` override. No
SKYD, PARA, predecessor, alternate symbol, or alternate provider may replace
PSKY.

Each ticker is requested from the same endpoint and with the same parameters
as price snapshot A2, changing only `adjust` from `none` to `splits`:

`GET https://api.twelvedata.com/time_series`

| Parameter | Frozen value |
| --- | --- |
| `symbol` | the frozen provider symbol |
| `interval` | `1day` |
| `start_date` | `2025-08-24` |
| `end_date` | `2026-11-18` |
| `outputsize` | `5000` |
| `adjust` | `splits` |

The immutable completed snapshot root is:

`C:\Users\giopa\h2-v2-1-phase1-private\split_adjusted_prices\TWELVEDATA_2026-11-17_ADJUST_SPLITS_H2V21_PHASE1_1_B_A1`

The Basic-plan daily cap may require acquisition over multiple UTC days. A
multi-day run is allowed only as a resumable acquisition with the exact frozen
parameters above:

1. Before every daily batch, `/api_usage` is called and the batch size is no
   larger than available daily credits minus the frozen 32-credit reserve.
2. Every completed ticker file is written once. A resume verifies its path,
   parameters, provider symbol, byte length, and SHA-256 and never replaces it.
3. Each request records its own UTC acquisition timestamp. The checkpoint
   records completed and remaining canonical tickers and is not an eligible
   source snapshot.
4. A resume with different parameters, ticker universe, parent snapshot, or
   an unexpected pre-existing file fails closed.
5. Only after all 458 tickers have a terminal recorded response is the sorted
   inventory sealed with per-file SHA-256 values and an ensemble SHA-256. The
   final snapshot is then read-only. Provider failures are recorded without
   symbol substitution.

At one credit per symbol, a one-day acquisition requires 490 available daily
credits (458 ticker credits plus the 32-credit reserve). A partial daily batch
does not change the request parameters and does not become an analytical
snapshot.

The final manifest records endpoint, full parameter template, canonical and
provider symbols, UTC acquisition time per response, raw and normalized file
hashes, retry metadata, checkpoint lineage, acquisition start/end timestamps,
the sorted inventory, and the ensemble hash. Raw licensed price files and
credentials remain outside Git.

### 17.2 Frozen unadjusted comparator

The unadjusted comparator is only the already sealed A2 price ensemble:

- snapshot ID:
  `TWELVEDATA_2026-11-17_RUN_H2V21_PHASE1_20261120_A2_PSKY`;
- `adjust=none`;
- ensemble SHA-256:
  `8dd59aae14c92f9d8dd9a6c055c166157e8b49b0d6ce0b48591080277ec7a54f`.

No unadjusted series is re-downloaded, repaired, joined, or replaced. Both
sources must pass their existing hash verification before and after the
Phase 1.1-B screen.

### 17.3 Bounded factor detection

For each canonical ticker, define the Phase 1.1 cutoff as the latest valid
`reference_session_date` among that ticker's DEV and PROSP_BATCH1 input rows.
If a ticker has more than one event, only that latest reference is used as the
read boundary for detecting its factor changes.

Both `adjust=none` and `adjust=splits` series are read through bounded JSONL
readers that stop immediately after the ticker cutoff. No later row is decoded
or returned. A ticker with no input row having both a valid
`price_window_start` and reference session has no Phase 1.1 interval to screen;
its provider response remains in the acquisition manifest, but no split
classification is asserted for that ticker.

On the ascending intersection of dates present in both bounded series, define
with deterministic decimal arithmetic:

`r_t = close_splits_t / close_none_t`

For each common date after the first common date, it is a detected split date
exactly when:

`abs((r_t / r_previous_common_date) - 1) > 0.01`

The threshold is strict: equality to 0.01 is not a detection. A zero,
non-finite, missing, duplicate, or otherwise invalid required close fails the
ticker screen; it is never imputed. The detection artifact records only the
canonical ticker, provider symbol, detected date, previous common date, and
the decimal factor-change ratio `r_t / r_previous_common_date`. It does not
record or report the close values and does not calculate an unadjusted return
or price jump.

The detection artifact is deterministically sorted by `(ticker, date,
previous_common_date, factor_change_ratio)` and receives a SHA-256. Detection
after a PROSP event's reference is deferred to its separately authorized
outcome phase.

### 17.4 Event exclusion and Phase 2 boundary

For each DEV or PROSP_BATCH1 input event with valid boundaries, add
`CORPORATE_ACTION_SPLIT` exactly when a detected date for its canonical ticker
falls in:

`price_window_start < detected_split_date <= reference_session_date`

The lower bound is excluded and the reference bound is included. All matching
dates and their factor-change ratios are recorded in deterministic order in
the exclusion detail. The row becomes `EXCLUDED`; existing exclusion reasons
are preserved. Events without a defined interval retain their existing
reasons and receive no asserted split classification.

For an authorized Phase 2 DEV run, and later for an authorized PROSP outcome
batch, apply the same factor method with bounded reads through D20 and exclude
when:

`reference_session_date < detected_split_date <= D20_session_date`

The reference bound is excluded and D20 is included. Phase 1.1-B neither
computes D20 nor calls the outcome engine. A Phase 2 batch must first prove
that both immutable series cover its D20 boundary.

### 17.5 Scope limits and output

The factor method is designed to detect adjustments represented by Twelve
Data under `adjust=splits`, including split and reverse-split factor changes.
It does not infer an action from a discontinuity in the unadjusted price
series. If spin-offs or extraordinary distributions are not represented in
the provider's split-adjustment factor, their absence is a known limitation.
Ordinary dividends do not cause an exclusion and remain a known limitation:
future outcomes are price returns, not total returns.

Phase 1.1-B writes to a new, non-overwriting directory:

`research/h2_v2_1_phase1_1_split_factors`

It regenerates `h2_v2_1_inputs_dev.csv`,
`h2_v2_1_inputs_prosp_batch1.csv`, and `h2_v2_1_exclusions.csv`, and commits a
source manifest, detection artifact, and build manifest. No outcome, model,
prediction, D20 value, post-reference price, or return column is added.

The report records all source/output hashes, the count of unique events newly
carrying `CORPORATE_ACTION_SPLIT` by DEV and PROSP_BATCH1, the resulting
primary-eligible counts, and the complete detected list of
`ticker / date / factor-change ratio`. No price, return, H2, or covariate
summary is permitted.

### 17.6 Required synthetic tests

Before the first `adjust=splits` download, tests must cover:

1. exact request parity with A2 except `adjust=splits`;
2. one-credit accounting, daily batching, safe resume, and hash refusal on a
   changed completed file;
3. the `SKYD -> PSKY` request with no fallback;
4. bounded reads of both series through the latest ticker reference;
5. common-date alignment and deterministic decimal factor calculation;
6. strict threshold behavior below, equal to, and above 0.01;
7. lower-exclusive and reference-inclusive event intervals;
8. multiple events for one ticker and preservation of existing reasons;
9. deterministic artifacts and output hashes; and
10. absence of calls/imports to `calculate_event_outcomes`,
    `build_experimental_dataset`, or the outcome engine.

## 18. Amendment 2026-11-20T20:00:54.3094290+01:00 — event-level split coverage

This amendment is committed before any Phase 1.1-B CSV regeneration. It adds
an event-level coverage gate to section 17 and prevails wherever a common-date
factor could otherwise be calculated from an incomplete adjusted series.

For every DEV or PROSP_BATCH1 input event with valid
`price_window_start` and `reference_session_date`, define the inclusive set of
unadjusted source dates:

`D_none = {d in adjust=none : price_window_start <= d <= reference_session_date}`

The event is split-verifiable exactly when all of the following hold:

1. `D_none` is non-empty;
2. the unadjusted series contains the event's `price_window_start` and
   `reference_session_date`; and
3. the `adjust=splits` series contains every date in `D_none`.

The coverage comparison reads both sources through the event ticker's latest
DEV/PROSP reference boundary and never decodes a later row. It compares dates
only; it does not inspect or summarize prices, returns, H2, or covariates.

If any required date is absent from `adjust=splits`, the event is not used for
factor-based split classification. It receives exclusion reason
`SPLIT_CHECK_UNAVAILABLE`, becomes `row_status=EXCLUDED`, and retains all
pre-existing reasons. Its exclusion detail records only the source status,
last adjusted date available at or before the ticker cutoff, and the missing
required dates. No alternate source, symbol, predecessor series, interpolation,
imputation, or estimate is permitted.

`CORPORATE_ACTION_SPLIT` may be asserted only for an event that passes this
coverage gate. For a passing event, the interval rule remains strictly:

`price_window_start < detected_split_date <= reference_session_date`

The lower-bound date is required for coverage even though a factor change on
that date is outside the exclusion interval; it supplies a valid baseline for
the next common-date factor comparison.

Phase 1.1-B additionally commits
`h2_v2_1_split_coverage_audit.json`. For every ticker whose adjusted-source
classification is not `ACTIVE_COMPLETE`, the audit records:

- canonical and provider symbol;
- provider classification;
- last adjusted date available in the normalized source;
- each DEV/PROSP accession and sample;
- its `price_window_start` and reference session;
- whether the event is split-verifiable; and
- the missing-date count and dates when it is not verifiable.

The Phase 1.1-B report reproduces this audit for KVUE and every other
non-`ACTIVE_COMPLETE` ticker, including the last adjusted date, DEV/PROSP
events, and verifiability. Permitted event counts add
`SPLIT_CHECK_UNAVAILABLE` by sample and the resulting primary-eligible rows.
No values of either close series are written to the audit or report.

Synthetic tests must cover complete coverage, a missing interior adjusted
date, a missing reference date, preservation of existing reasons, precedence
over `CORPORATE_ACTION_SPLIT`, bounded reads with a malformed future sentinel,
and the non-`ACTIVE_COMPLETE` ticker audit.
