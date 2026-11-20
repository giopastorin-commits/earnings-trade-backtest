# H2 V2.1 — Phase 1 input build report

**Status:** Phase 1 completed; Phase 2 not started

**Branch:** `research/h2-v2-1-build-inputs`

**Starting commit:** `bee6463372e541519aae88cd98481bba27839875`

**Final analytical-code commit:** `421396a3c773c9131ca9dcedc8c18b47b42419f0`

**Immutable input-snapshot commit:** `546ea38f7376adf9480a0da07048ed8ecc561ea3`

**Build completed:** `2026-11-20T14:45:15.451Z`

This report covers only the construction and sealing of the H2 V2.1 Phase 1
inputs. No outcome was loaded or calculated, no model was estimated, and no
prediction was generated.

## 1. Governing specification and provenance

- Governing specification: `H2_V2_1_BUILD_SPEC.md`.
- Protocol precedence: `H2_PROTOCOL_V2_1.md` over `H2_PROTOCOL_V2.md`.
- R-001 and D-001 source: `giopastorin-commits/trinity-online`, commit
  `40ae6cb`, branch `maintenance/pausa-ingest-notturno`,
  `research/REGISTRO_ESPERIMENTI.md`.
- Universe: 518 supported records from
  `trinity/usa_issuer_registry_v2.fixture`.
- Universe SHA-256:
  `957f2686fcb5b4beace10c9dbe8b88e6383ecd1fc423cf6e2e8c836f3a4c1097`.
- SEC cutoff/run start: `2026-11-18T17:23:11.000Z`.
- SEC corpus: 10,577 hash-inventoried source/cache files; ensemble SHA-256
  `d0b8b29ae0aa645c08046e681a89e15a4c59872e0e5d9302e143aebabdb773ef`.
- Normalized SEC text ensemble SHA-256:
  `f122d7b0fd2be3dc91febddd576bc7f5d8e85470e3fb252a4c33e8a1e0b100d4`.

## 2. Price source and immutable acquisition

One new Twelve Data ensemble was used for DEV, PROSP_BATCH1 and SPY. No file
from the earlier A1 run or the 2026-10-26 production snapshot was reused.

| Field | Recorded value |
| --- | --- |
| Snapshot ID | `TWELVEDATA_2026-11-17_RUN_H2V21_PHASE1_20261120_A2_PSKY` |
| Endpoint | `https://api.twelvedata.com/time_series` |
| Parameters | `interval=1day`, `start_date=2025-08-24`, `end_date=2026-11-18`, `outputsize=5000`, `adjust=none` |
| Adjustment | unadjusted; no split or dividend adjustment requested |
| Acquisition start | `2026-11-20T12:49:22.148Z` |
| Acquisition end | `2026-11-20T14:04:46.559Z` |
| Target session | `2026-11-17` |
| Issuer snapshot SHA-256 | `394c89cb192a89f5c61fc2eab8c2c1b6768f4a0e59925ada7fabce9e456ab4a9` |
| SPY normalized SHA-256 | `2303f9d5f2af1fda5a896dab3068c9b32083109d465dc51f42afddcdb146fc81` |
| Ensemble SHA-256 | `8dd59aae14c92f9d8dd9a6c055c166157e8b49b0d6ce0b48591080277ec7a54f` |
| Explicit provider override | canonical `SKYD` → requested `PSKY` |

Source verification after the build exited successfully and reproduced the
price and SEC ensemble hashes above. The reader boundary is applied while
reading JSONL: it stops at each event's `reference_session_date`; synthetic
sentinel tests verify that a later price row is neither decoded nor returned.

Twelve Data returned HTTP 400 for requested symbols PSKY and WBD. No alternate
symbol, predecessor, old snapshot, or other provider was substituted. WBD has
no DEV or PROSP_BATCH1 event in this build. The one PROSP_BATCH1 event for CIK
`0002041610` remains canonical ticker `SKYD`, was requested only as provider
symbol `PSKY`, and is excluded as `PRICE_COVERAGE` (accession
`0002041610-26-000062`, reference session `2026-11-09`).

## 3. Permitted Phase 1 counts

Candidate events are unique accessions in the corresponding input CSV unioned
with same-sample non-alias exclusion rows. `DUPLICATE_SHARE_CLASS` rows are not
candidate events and are excluded from these counts.

| Sample | Candidate events | EXACT | FALLBACK | NO_MATCH | AMBIGUOUS | sigma_pre_60 missing | sigma_pre_60 == 0 | Final primary-eligible |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| DEV | 90 | 81 | 3 | 0 | 0 | 0 | 0 | 81 |
| PROSP_BATCH1 | 389 | 355 | 33 | 0 | 0 | 1 | 0 | 354 |

The prospective batch contains **5 distinct ISO reference weeks**.

### INTRADAY and UNKNOWN outside the samples

These events remain `sample=UNASSIGNED`; `release_period` is descriptive only.

| release_period | TIMING_INTRADAY | TIMING_UNKNOWN |
| --- | ---: | ---: |
| DEV_PERIOD | 2 | 0 |
| PROSP_PERIOD | 7 | 0 |
| OTHER | 0 | 0 |

### Exclusion rows by reason

The exclusions file contains one row per accession/reason. Multiple reasons
may therefore refer to the same event.

| Reason | DEV | PROSP_BATCH1 | UNASSIGNED | Alias/outside-history rows | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| `DUPLICATE_SHARE_CLASS` | 0 | 3 | 0 | 22 | 25 |
| `FISCAL_PERIOD_UNKNOWN` | 2 | 21 | 0 | 0 | 23 |
| `NO_EX99` | 6 | 1 | 0 | 0 | 7 |
| `PAIRING_FALLBACK` | 3 | 33 | 0 | 0 | 36 |
| `PRICE_COVERAGE` | 0 | 1 | 0 | 0 | 1 |
| `SIGMA_PRE_60_MISSING` | 0 | 1 | 0 | 0 | 1 |
| `TIMING_INTRADAY` | 0 | 0 | 9 | 0 | 9 |
| `TIMING_UNKNOWN` | 0 | 0 | 0 | 0 | 0 |
| `PAIRING_NO_MATCH` | 0 | 0 | 0 | 0 | 0 |
| `PAIRING_AMBIGUOUS` | 0 | 0 | 0 | 0 | 0 |
| `AMBIGUOUS_TICKER_MAPPING` | 0 | 0 | 0 | 0 | 0 |

## 4. Multiple share classes and missing SEC ticker fallback

An accession is emitted once. The non-selected securities below are audit
aliases only and do not count as excluded events.

| CIK | Full SEC ticker array | Selected canonical | Non-selected supported | Historical events | DEV | PROSP_BATCH1 | Alias rows |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| `0001564708` | `NWSA, NWS, NWSLL` | `NWSA` | `NWS` | 8 | 0 | 1 | 9 |
| `0001652044` | `GOOGL, GOOG, GOOGM, GOOGN` | `GOOGL` | `GOOG` | 7 | 0 | 1 | 8 |
| `0001754301` | `FOXA, FOX` | `FOXA` | `FOX` | 7 | 0 | 1 | 8 |
| **Total** | — | — | — | **22** | **0** | **3** | **25** |

The approved unique-CIK registry fallback affected exactly these three CIKs:

| CIK | SEC ticker array | Registry ticker used | Reason | Acquired at UTC | Historical | DEV | PROSP_BATCH1 | UNASSIGNED | Outside window |
| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| `0000034088` | empty | `XOM` | `SEC_TICKERS_EMPTY` | `2026-11-19T15:43:29.971Z` | 6 | 0 | 0 | 0 | 0 |
| `0001437107` | empty | `WBD` | `SEC_TICKERS_EMPTY` | `2026-11-19T15:35:24.376Z` | 7 | 0 | 0 | 0 | 0 |
| `0002041610` | `SKYD, SKYD-WT` | `SKYD` | `SEC_TICKERS_NO_REGISTRY_MATCH` | `2026-11-19T15:16:57.525Z` | 4 | 0 | 1 | 0 | 0 |

For CIK `0002041610`, historical pairing uses only releases of that same CIK.
No PARA or predecessor-CIK release is admitted, and no predecessor price series
is joined.

## 5. File hashes

### Committed construction files

| File | SHA-256 |
| --- | --- |
| `H2_V2_1_BUILD_SPEC.md` | `dc81207e3318645459b616c4d55acc723627a96aac676ff8c25396b6122e5ab5` |
| `requirements.txt` | `aa332642d650023599fed6bd51d144d0deb441f47ebf264f4eb467abdcb69ba5` |
| `scripts/h2_v2_1_build_inputs.py` | `08e7836c825ff8409661e81a5536375a1d7eb6980c1adbbf8ee5737a7c0f6b22` |
| `trinity/h2_v2_1_inputs.py` | `aee41924991bdd9b6ebc0a608a6ae6f0429e5317a6d526ab8291299ea020f657` |
| `trinity/twelvedata_prices.py` | `b66e58277fc93563d4d39ba7a254e1d34aa4bf689bd28bc1c1dcbeea4d7b938b` |
| `tests/trinity/test_h2_v2_1_inputs.py` | `95e29023a0b8b3b9ffb188c2843c7fc76cfd1020116e6d4beee4c911ec9c8e39` |
| `tests/trinity/test_twelvedata_prices.py` | `f01778968b4c5dec7b46de8c6240912ca7a1d673df0fc5cfa2bfe26418598bf0` |

### Committed snapshot files

| File | SHA-256 |
| --- | --- |
| `research/h2_v2_1_phase1_final/h2_v2_1_inputs_dev.csv` | `e1f3e7671422ece25cc6a0ffba9c4bbca4e11ad84d7e080bc349932466817501` |
| `research/h2_v2_1_phase1_final/h2_v2_1_inputs_prosp_batch1.csv` | `a4a638452a5f6284ab605a009a651fd95f89c775148b2d09d9a4e59d540f18fe` |
| `research/h2_v2_1_phase1_final/h2_v2_1_exclusions.csv` | `d1d90fa90aaab3e82629897da1f0b74774cf2a16d9544724c8a7a55f491411e4` |
| `research/h2_v2_1_phase1_final/h2_v2_1_build_manifest.json` | `630443730c94d10d2865d657a1668d88c9cbfe360f94c6fb1200d9bb859f6c7e` |
| `research/h2_v2_1_phase1_final/h2_v2_1_price_source_manifest.json` | `17773417921210c107e508db44d3ab43ac267ca422b3b30011bbe528c7cdb793` |
| `research/h2_v2_1_phase1_final/h2_v2_1_sec_text_manifest.json` | `a3b2bce00472d07f81e9a2cad2e789b3518a7701e49046104a52b868e94479b2` |
| `research/h2_v2_1_phase1_final/h2_v2_1_share_class_resolution.json` | `ccb9d6d36146335ba482421e2f33d8d754d58feba20cbb5124e6cca1c487411c` |

### External immutable source indexes

| File/ensemble | SHA-256 |
| --- | --- |
| SEC acquisition manifest | `37eae02347102faadebdf8eb575e3013b326970e929d2669f38a5003d9f9f0df` |
| SEC corpus index | `ea9edaf8a93ceb865ad8bd348bb591e7bcf5e40fede41959308c05e7c35be79c` |
| SEC source ensemble | `d0b8b29ae0aa645c08046e681a89e15a4c59872e0e5d9302e143aebabdb773ef` |
| Twelve Data issuer snapshot | `394c89cb192a89f5c61fc2eab8c2c1b6768f4a0e59925ada7fabce9e456ab4a9` |
| Twelve Data 518-issuer-plus-SPY ensemble | `8dd59aae14c92f9d8dd9a6c055c166157e8b49b0d6ce0b48591080277ec7a54f` |
| Normalized SEC text ensemble | `f122d7b0fd2be3dc91febddd576bc7f5d8e85470e3fb252a4c33e8a1e0b100d4` |

`PHASE1_REPORT.md` cannot contain its own SHA-256 without changing that hash.
Its exact bytes are instead fixed by the Git commit containing this report; its
SHA-256 is recorded in the delivery message after the commit is created.

## 6. Component hashes and verification

| Component | SHA-256 |
| --- | --- |
| `trinity/usa_documents.py` | `24dd2f46a8187d2ca71be85ccb0c92dc33ba31de859fc4d2cd961b5db867b160` |
| `trinity/h2_novelty.py` | `2c53f69075925afb02a024c4e03b3f6a3ecbde292f49366e3e9ba19c63d06e9e` |
| `trinity/earnings_pairing.py` | `e93c2a2da32a05f5f2db1b590625869870ae505c43fd4c43cd649ff7fe0980f8` |
| `trinity/pre_event_covariates.py` | `3e1267ccf83076b443ec6f300ad9a8f73e5daec141708403f2dd5a0a96aadaf3` |

- Targeted Phase 1 suite: **126 passed, 2 skipped**.
- Covered: timing, XNYS reference resolution, fiscal successor, EX-99
  selection, bounded price reads, provider failure/no substitution,
  deterministic synthetic output, share-class resolution, fallback CIKs and
  PSKY provider-symbol override.
- Source verification after the real build: exit code 0.
- DEV input accessions are unique: 84 rows, 84 unique accessions.
- PROSP_BATCH1 input accessions are unique: 388 rows, 388 unique accessions.
- Searches of the Phase 1 entry point and builder found no call/import of
  `calculate_event_outcomes` or `build_experimental_dataset`.

## 7. Deviations and limitations

1. **Price-source fallback approved before acquisition.** Read-only R2
   credentials were unavailable and the locally verifiable 2026-10-26
   production snapshot did not reach the required 2026-11-17 session. The
   approved alternative was the single new Twelve Data A2 ensemble documented
   above. There was no R2 write and no source mixing.
2. **Fiscal metadata deviation fixed before acquisition.** SEC submissions do
   not supply `fy`/`fp`; these fields were matched point-in-time from SEC
   Company Facts using the exact accession and report date, with ambiguous or
   missing matches left null.
3. **Transient SEC archive handling.** The committed implementation defers a
   filing whose complete-submission request remains transiently unavailable
   after the frozen retries, rather than terminating the entire corpus run.
   The final acquired corpus manifest contains no error entries.
4. **PSKY coverage.** The approved PSKY-only request returned HTTP 400. The
   corresponding PROSP event is excluded as `PRICE_COVERAGE`; no SKYD, PARA or
   predecessor series was used.
5. **Test scope.** The complete `tests/trinity` tree was not run because it
   includes outcome-engine tests, while Phase 1 expressly forbids calling the
   outcome engine. The outcome-free Phase 1 suite listed above was run and
   passed.
6. Raw licensed price files and normalized SEC texts remain outside Git. Their
   committed manifests and ensemble hashes bind the external artifacts. No
   raw price, `.env`, credential or API key is committed.

## 8. Stop condition

Phase 1 ends with the committed input snapshot and hashes above. Phase 2 was
not started: there are no model estimates, forecasts, regressions, outcome
calculations or post-reference price reads in this delivery.
