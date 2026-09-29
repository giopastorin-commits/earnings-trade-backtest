# TRINITY Ledger Foundation V1 — Frozen Specification

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0`

Baseline:

- Branch: `trinity-italia-v1`
- Commit: `0a7bfbaa87dac94db9f35b26ab1c6f1df8672f45`

Status: **FROZEN**

Rule: **Any semantic change after this freeze requires a new specification version.**

## 1. Authority and scope

This document is the sole authoritative Ledger Foundation V1 specification. It
consolidates the approved Final Technical Design, Delta Conformance Review,
schema-freeze errata, and final trade-lifecycle correction. Earlier drafts and
definitions that conflict with this document are obsolete and non-normative.

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. V1
defines persistence, identity, point-in-time (PIT) knowledge, derivation,
execution eligibility, trade lifecycle, outcomes, challenges, experiments, and
legacy-boundary semantics. It does not change the behavior of any existing
TRINITY engine or H2 frozen module.

## 2. Frozen architectural decisions

1. V1 uses one SQLite database as its storage engine. SQLite is the system of
   record, not an export cache. Schema migrations are explicit and monotonic;
   V1 starts at schema version `1`.
2. Ledger facts are append-only. Corrections supersede prior records by explicit
   lineage; they never update historical meaning in place.
3. Every timestamp is an RFC 3339 UTC instant with microsecond precision,
   serialized with `Z`. Calendar dates use ISO 8601 `YYYY-MM-DD`.
4. All identifiers are opaque, stable text identifiers. Foreign keys are
   enabled. Referential integrity and enumerated-value checks are mandatory.
5. `run_request_id`, `attempt_id`, and `run_id` represent different entities
   and MUST be stored in different columns. They MUST NOT be inferred from one
   another or reused as one another.
6. The authoritative analysis boundary is `analysis_cutoff_at`. No Ledger V1
   entity has an authoritative field named `decision_cutoff_at`.
7. Payload identity and temporal observation are separate: `artifact` stores
   immutable bytes and content identity; `input_observation` records when and
   how an input became knowable to a run.
8. Temporal availability propagates through explicit `derivation_node` and
   `derivation_edge` records. A derived fact can never become available before
   its latest required ancestor.
9. Missing trustworthy source-publication time may use
   `RETRIEVED_AT_FALLBACK`; the fallback is visible and queryable, never silently
   treated as source publication time.
10. Execution eligibility is derived, reproducible, and policy-versioned. It is
    materialized as `execution_eligible_at` with the exact eligibility policy
    version that produced it; it is not a free-form analyst assertion.
11. V1 supports two distinct outcome families: `FIXED_HORIZON` and
    `TRADE_LIFECYCLE`. Results from the two families MUST NOT be conflated.
12. Unfilled setups terminate as `EXPIRED_UNFILLED` or
    `INVALIDATED_UNFILLED`. The lifecycle is monotonic; there is no transition
    from `TP1_HIT` back to `OPEN`.
13. Research-to-setup provenance is explicit and many-to-many. A setup without
    its required research lineage is invalid.
14. Failed attempts and their artifacts are first-class audit evidence. Failure
    MUST NOT be represented by deleting an attempt or its emitted artifacts.
15. Challenge cases and experiment registrations are durable, versioned Ledger
    records, not annotations in reports.
16. Pre-Ledger data is classified explicitly by legacy and PIT quality; unknown
    provenance is never upgraded by assumption.

## 3. Storage contract

### 3.1 SQLite V1

The V1 database MUST:

- use SQLite with `PRAGMA foreign_keys = ON` for every connection;
- use UTF-8 text, deterministic column types, and no application-dependent
  implicit type coercion;
- store timestamps as canonical UTC text and validate their format;
- use transactions so a successful run and its required graph, lineage, and
  artifacts become visible atomically;
- reject schema versions other than the version understood by the writer;
- maintain a `schema_metadata` singleton with `schema_version = 1`, the freeze
  identifier, creation time, and applied migration identifier;
- use unique constraints for natural idempotency keys in addition to primary
  keys; and
- never depend on row order, SQLite `rowid`, local time, or host path for
  semantic identity.

Binary payloads MAY be stored as SQLite BLOBs or in a content-addressed object
store referenced by the database. In either case, `artifact.sha256` and
`artifact.byte_length` are mandatory, the referenced bytes MUST remain
immutable and retrievable, and the database remains authoritative for metadata
and lineage.

### 3.2 Common conventions

All tables have an opaque text primary key unless described as a singleton.
JSON values are canonical UTF-8 JSON: object keys sorted lexicographically, no
insignificant whitespace, finite numbers only, and no duplicate keys. Hashes
are lowercase hexadecimal SHA-256. Version strings are immutable identifiers,
not mutable labels such as `latest`.

Producers MUST persist timestamps supplied by upstream sources separately from
Ledger ingestion timestamps. They MUST NOT substitute file modification time,
database insertion time, or run completion time for source availability.

## 4. Identity and execution model

### 4.1 `run_request`

A run request is the immutable declaration of intended work. Required fields:

| Field | Meaning |
|---|---|
| `run_request_id` | Primary identity of the request |
| `request_kind` | Versioned operation kind |
| `requested_at` | Time the request entered the system |
| `analysis_cutoff_at` | Latest knowledge instant permitted for analysis |
| `parameters_json` | Canonical requested parameters |
| `idempotency_key` | Caller-scoped unique request key |
| `requested_by` | Actor or system identity |
| `baseline_commit` | Code baseline requested |

`analysis_cutoff_at` is inclusive: an input with effective availability exactly
equal to the cutoff may be used. A request is immutable after insertion.

### 4.2 `attempt`

An attempt is one physical try to satisfy a request. Required fields:

| Field | Meaning |
|---|---|
| `attempt_id` | Primary identity of this try |
| `run_request_id` | Request being attempted |
| `attempt_ordinal` | Positive, unique ordinal within the request |
| `started_at` | Try start time |
| `finished_at` | Nullable until terminal |
| `status` | `RUNNING`, `SUCCEEDED`, `FAILED`, or `ABORTED` |
| `worker_identity` | Executing worker identity |
| `code_commit` | Exact code commit executed |
| `environment_fingerprint` | Deterministic environment/config fingerprint |
| `failure_class` | Required for `FAILED`; otherwise null |
| `failure_message` | Sanitized diagnostic; nullable |

There may be many attempts for one request. `(run_request_id,
attempt_ordinal)` is unique. An attempt's terminal status is immutable. Failure
does not create a successful run, but all artifacts emitted before failure MUST
remain linked to the attempt through `attempt_artifact` with role
`FAILED_ATTEMPT_OUTPUT`, `LOG`, `DIAGNOSTIC`, or `PARTIAL_OUTPUT`.

### 4.3 `run`

A run is the atomically committed, successful Ledger result of exactly one
successful attempt. Required fields:

| Field | Meaning |
|---|---|
| `run_id` | Primary identity of the committed result |
| `run_request_id` | Originating request |
| `attempt_id` | Unique successful attempt that produced it |
| `committed_at` | Atomic Ledger commit time |
| `status` | V1 value is `COMMITTED` |
| `result_manifest_artifact_id` | Canonical result-manifest artifact |

`run.attempt_id` is unique and MUST reference an attempt with `SUCCEEDED`
status and the same `run_request_id`. A successful attempt creates at most one
run. Failed or aborted attempts create none. The three ID values are distinct
namespaces and their strings MUST also be pairwise unequal within a run chain.

## 5. Artifacts and input observations

### 5.1 `artifact`

An artifact identifies immutable bytes; it does not, by itself, say when those
bytes were publicly available, observed, selected as input, or produced.
Required fields:

| Field | Meaning |
|---|---|
| `artifact_id` | Opaque artifact identity |
| `sha256` | Content hash |
| `byte_length` | Exact byte count |
| `media_type` | Registered or explicit media type |
| `storage_uri` | Immutable content address |
| `created_at` | Ledger metadata creation time |
| `artifact_kind` | Versioned semantic kind |

Content identity `(sha256, byte_length)` is unique. Equal bytes may be reused
by multiple observations and attempts. `created_at` has no PIT meaning.

### 5.2 `input_observation`

An input observation is the auditable claim that a particular artifact or
external fact was observed from a source. Required fields:

| Field | Meaning |
|---|---|
| `input_observation_id` | Observation identity |
| `artifact_id` | Observed immutable payload |
| `source_id` | Stable source identity |
| `source_record_key` | Source-native record/version key |
| `source_published_at` | Nullable trustworthy source publication instant |
| `retrieved_at` | Time payload was actually retrieved |
| `availability_basis` | How effective availability is established |
| `effective_available_at` | Derived effective availability instant |
| `observed_by_attempt_id` | Attempt that retrieved or registered it |
| `source_metadata_json` | Canonical source/version metadata |

Allowed `availability_basis` values are:

- `SOURCE_PUBLISHED_AT`: a trustworthy, immutable source publication instant
  exists; `effective_available_at = source_published_at`.
- `RETRIEVED_AT_FALLBACK`: no trustworthy source publication instant exists;
  `source_published_at` is null and `effective_available_at = retrieved_at`.
- `LEGACY_ASSERTED_AT`: used only for explicitly classified legacy imports with
  a separately evidenced asserted availability instant.

For all bases, `retrieved_at` MUST NOT precede `effective_available_at` unless
the source supplies a separately verified future-effective record type and the
observation is barred from use until that future instant. An attempt may use an
observation only when `effective_available_at <= analysis_cutoff_at` and all
derivation propagation rules also pass. The same artifact observed at different
times or from different sources produces distinct observations.

`run_input` records actual use with `(run_id, input_observation_id, input_role,
derivation_node_id)`. Mere presence in storage is not usage.

## 6. Temporal derivation graph

### 6.1 Nodes and edges

Every input, normalized fact, research item, setup, eligibility result, and
outcome that can influence a result MUST be represented by a `derivation_node`.
Required fields:

| Field | Meaning |
|---|---|
| `derivation_node_id` | Node identity |
| `run_id` | Owning committed run; nullable while attempt-local |
| `attempt_id` | Producing attempt |
| `node_kind` | `INPUT_OBSERVATION`, `NORMALIZED_FACT`, `RESEARCH`, `SETUP`, `ELIGIBILITY`, or `OUTCOME` |
| `entity_type` / `entity_id` | Exact Ledger entity represented |
| `direct_available_at` | Availability intrinsic to the entity, if any |
| `derived_available_at` | Computed propagated availability |
| `derivation_policy_version` | Exact propagation implementation version |

`derivation_edge` contains `derivation_edge_id`, `parent_node_id`,
`child_node_id`, `edge_role`, and `required`. Parent and child MUST belong to the
same attempt/run lineage. `(parent_node_id, child_node_id, edge_role)` is unique.
Self-edges and cycles are forbidden.

### 6.2 Temporal propagation

For a node `n`, V1 computes:

```text
derived_available_at(n) = max(
  direct_available_at(n),
  derived_available_at(p) for every required parent p
)
```

Null terms are omitted, but a non-input node with neither a direct availability
instant nor a required parent is invalid. For an `INPUT_OBSERVATION` node,
`direct_available_at` and `derived_available_at` both equal the observation's
`effective_available_at`. Optional parents MAY add provenance but MUST NOT
silently influence content; if they influence content, the edge is required.

The graph MUST be evaluated in deterministic topological order. Persisted
`derived_available_at` is a checked materialization: readers MUST be able to
recompute it from the versioned policy and graph. A child cannot predate a
required parent. All nodes used by a run MUST have
`derived_available_at <= run_request.analysis_cutoff_at`.

## 7. Research and setup lineage

### 7.1 Research records

`research_record` represents a versioned research conclusion. It contains
`research_id`, `run_id`, `research_kind`, `subject_key`, `content_artifact_id`,
`method_version`, `as_of_at`, `derivation_node_id`, `supersedes_research_id`
(nullable), and `created_at`. `as_of_at` is the conclusion's market-time scope;
it does not replace propagated availability.

A correction creates a new record and points to the superseded record. Existing
runs retain their original lineage.

### 7.2 Setups

`setup` is an immutable proposed trading setup. It contains:

- `setup_id`, `run_id`, `instrument_id`, `direction`, and `setup_kind`;
- `signal_at`, `entry_rule_json`, `stop_rule_json`, and `target_rule_json`;
- `valid_from`, `expires_at`, and optional `invalidates_at`;
- `setup_policy_version`, `derivation_node_id`, and `created_at`; and
- canonical units, currency, venue/calendar identity, and price-adjustment
  convention where applicable.

`setup_research_lineage` is the explicit many-to-many join with `setup_id`,
`research_id`, `lineage_role`, and `research_derivation_node_id`. Every setup
MUST have at least one `PRIMARY` research lineage row. Every research item that
influences the setup MUST be linked. The setup derivation node has required
edges from all influencing research nodes. A report-level hyperlink or matching
symbol is not lineage.

## 8. Versioned execution eligibility

### 8.1 Policy registry

`eligibility_policy` contains:

- `eligibility_policy_id` and immutable `policy_version`;
- canonical `policy_definition_json` and its SHA-256;
- calendar, session, latency, data-completeness, and instrument-universe rule
  versions;
- `valid_from` and optional `valid_to`; and
- the code commit and artifact implementing the policy.

Policy versions are append-only and uniquely identify semantics. Changing any
input rule requires a new policy version.

### 8.2 Derived eligibility

`setup_execution_eligibility` contains `eligibility_id`, `setup_id`,
`eligibility_policy_id`, `derivation_node_id`, `evaluated_at`,
`execution_eligible_at`, `eligible`, and canonical `reason_codes_json`.

`execution_eligible_at` is a derived, versioned value. When `eligible = 1`, it
is the earliest instant at which all of the following are true:

```text
execution_eligible_at = policy_next_eligible_instant(max(
  setup.signal_at,
  setup_derivation_node.derived_available_at,
  all policy-required input availability instants,
  policy-required processing/market-session constraints
))
```

When `eligible = 0`, `execution_eligible_at` is null and at least one stable
reason code is required. It MUST be at or after the setup's propagated
availability and MUST be reproducible from the referenced policy version.
Manual overrides create a new explicit policy result; they do not mutate the
derived value or omit its policy lineage. Eligibility is separate from fill:
being eligible does not assert that an order traded.

## 9. Trade lifecycle

### 9.1 Event model

`trade_instance` contains `trade_id`, `setup_id`, `eligibility_id`,
`lifecycle_policy_version`, `initial_state`, `current_state`,
`opened_at` (nullable), `closed_at` (nullable), and `derivation_node_id`.
`trade_event` is append-only and contains `trade_event_id`, `trade_id`,
`sequence_number`, `event_at`, `observed_at`, `event_type`, `from_state`,
`to_state`, price/quantity fields where relevant, `market_data_observation_id`,
and `rule_version`. `(trade_id, sequence_number)` is unique.

The event stream, ordered by `(event_at, deterministic intrabar precedence,
sequence_number)`, is authoritative. `current_state`, quantities, and realized
values are checked projections. An event MUST NOT use market data unavailable
to the outcome run at its applicable cutoff.

### 9.2 Frozen states

V1 lifecycle states are:

- `PENDING_ENTRY`
- `OPEN`
- `TP1_HIT`
- `CLOSED_TARGET`
- `CLOSED_STOP`
- `CLOSED_TIME`
- `EXPIRED_UNFILLED`
- `INVALIDATED_UNFILLED`

The last five states are terminal except that `TP1_HIT` is non-terminal.
`TP1_HIT` means the first target was reached and the lifecycle policy's
post-TP1 position/risk rules now apply; it is not equivalent to a fresh open
position.

### 9.3 Allowed transition matrix

Only these transitions are legal:

| From | To | Cause |
|---|---|---|
| `PENDING_ENTRY` | `OPEN` | Valid entry fill |
| `PENDING_ENTRY` | `EXPIRED_UNFILLED` | Entry window expires without fill |
| `PENDING_ENTRY` | `INVALIDATED_UNFILLED` | Setup invalidates before any fill |
| `OPEN` | `TP1_HIT` | First target is filled and residual position remains |
| `OPEN` | `CLOSED_TARGET` | Target policy closes the whole position |
| `OPEN` | `CLOSED_STOP` | Stop closes the whole position |
| `OPEN` | `CLOSED_TIME` | Time/forced-exit rule closes the whole position |
| `TP1_HIT` | `CLOSED_TARGET` | Remaining position closes at a later target |
| `TP1_HIT` | `CLOSED_STOP` | Post-TP1 stop closes the remainder |
| `TP1_HIT` | `CLOSED_TIME` | Time/forced-exit rule closes the remainder |

All other transitions are forbidden. Terminal states have no outgoing
transitions. In particular, a lifecycle that has reached `TP1_HIT` MUST NOT
transition back to `OPEN`. There is no fill event after an unfilled terminal
state. Partial fills, gaps, same-bar target/stop collisions, corporate actions,
session boundaries, and price rounding are resolved only by the referenced
versioned lifecycle policy, with deterministic reason codes and event evidence.

## 10. Outcome families

`outcome` contains `outcome_id`, `setup_id`, `outcome_family`,
`outcome_policy_version`, `outcome_run_id`, `derivation_node_id`,
`measurement_start_at`, `measurement_end_at`, canonical `metrics_json`,
`status`, and optional `trade_id`. Unmeasurable results persist with a stable
status/reason rather than disappearing.

### 10.1 `FIXED_HORIZON`

This family measures instrument performance at one or more predefined horizons
from a versioned anchor and pricing/calendar convention. It is independent of
entry-fill, stop, target, or trade-state simulation. Required policy semantics
include anchor selection, horizons, session/calendar mapping, adjusted/unadjusted
price convention, missing-price behavior, and return formula. `trade_id` is
null.

### 10.2 `TRADE_LIFECYCLE`

This family measures the path-dependent result produced by the frozen lifecycle
state machine under a versioned execution/lifecycle policy. It references one
`trade_id` and includes fill evidence, terminal state, realized and residual
quantity accounting, costs/slippage convention, holding interval, and return or
R-multiple definitions.

The two families may share a setup but MUST have separate outcome rows,
policies, derivation nodes, and metrics namespaces. A fixed-horizon return MUST
NOT be presented as a simulated trade result, and a lifecycle result MUST NOT be
used as a fixed-horizon observation.

## 11. Challenge Cases

`challenge_case` is a durable registry of ambiguity, adversarial temporal data,
or an expected edge condition. It contains `challenge_case_id`, immutable
`challenge_version`, `title`, `category`, `fixture_artifact_id`,
`expected_assertions_json`, `status`, `introduced_by_run_id`, and optional
`supersedes_challenge_case_id`. Executions are recorded in
`challenge_case_result` with the exact code, schema, and policy versions,
pass/fail status, and evidence artifacts.

The V1 conformance corpus MUST cover at least:

1. source publication exactly at `analysis_cutoff_at` (included) and one
   microsecond after it (excluded);
2. absent source time using `RETRIEVED_AT_FALLBACK`, including a later retrieval
   of identical bytes without retroactive availability;
3. a multi-level derivation whose late required ancestor propagates through
   research, setup, eligibility, and outcome;
4. a corrected source record that supersedes but does not mutate the original;
5. retries proving one request, multiple attempts, one committed run, and three
   distinct ID namespaces;
6. a failed attempt that retains partial, diagnostic, and log artifacts but has
   no run;
7. explicit many-to-many research-to-setup lineage;
8. an ineligible setup with null `execution_eligible_at` and stable reason;
9. a policy-version change that yields a new eligibility record without
   rewriting the old record;
10. no-fill expiry to `EXPIRED_UNFILLED`;
11. pre-fill invalidation to `INVALIDATED_UNFILLED`;
12. TP1 followed by stop, target, and time exits, each proving no return to
    `OPEN`;
13. deterministic same-bar collision and gap handling;
14. distinct `FIXED_HORIZON` and `TRADE_LIFECYCLE` outcomes for one setup;
15. legacy records in each permitted PIT classification, including rejection of
    unknown data from PIT-safe cohorts; and
16. cycle rejection and recomputation of every materialized derivation time.

Challenge fixtures and expected assertions are immutable. A changed expectation
creates a new challenge version.

## 12. Experiment Registry

`experiment` is the immutable declaration of an analysis. Required fields are
`experiment_id`, `experiment_version`, `name`, `hypothesis`, `registered_at`,
`registered_by`, `cohort_definition_json`, `code_commit`, `schema_version`,
`input_snapshot_artifact_id`, `configuration_artifact_id`, all referenced
research/setup/eligibility/lifecycle/outcome policy versions, primary metrics,
and canonical exclusion rules.

`experiment_run` links `experiment_id` to one or more `run_id` values and stores
`role` (`PRIMARY`, `REPLICATION`, `SENSITIVITY`, or `CHALLENGE`), start/finish
times, status, and result-manifest artifact. `experiment_result` stores immutable
metric outputs and evidence artifacts. A rerun creates a new experiment run; it
does not overwrite results. A semantic change to hypothesis, cohort, exclusions,
metric definition, or policy set creates a new experiment version.

Registration MUST make the analysis reproducible without consulting mutable
defaults. Failed experiment attempts remain visible through the attempt model
and may be linked as failed executions, but they are not successful
`experiment_run` records.

## 13. Legacy and PIT classifications

Every imported pre-Ledger record has a `legacy_record_classification` containing
`legacy_record_id`, source locator, import artifact, classifier version,
classification, evidence JSON, `classified_at`, and optional mapped
`input_observation_id`.

Allowed classifications are:

- `NATIVE_PIT`: generated under Ledger rules with complete observation and
  derivation evidence; not normally used for legacy imports.
- `LEGACY_PIT_VERIFIED`: pre-Ledger data with independently verifiable source
  availability and complete enough lineage for the intended use.
- `LEGACY_PIT_BOUNDED`: exact availability is unknown but a conservative upper
  bound is evidenced; usability begins at that bound and uses
  `LEGACY_ASSERTED_AT`.
- `LEGACY_NON_PIT`: known to contain look-ahead, mutable-current-state data, or
  otherwise non-PIT construction.
- `LEGACY_PIT_UNKNOWN`: insufficient evidence to decide.

Only `NATIVE_PIT`, `LEGACY_PIT_VERIFIED`, and—when the experiment explicitly
permits conservative bounds—`LEGACY_PIT_BOUNDED` may enter a PIT-safe cohort.
`LEGACY_NON_PIT` and `LEGACY_PIT_UNKNOWN` MUST be excluded from PIT claims and
may be used only in explicitly labeled exploratory/non-PIT work. Classification
never manufactures a source publication time. Reclassification is append-only,
versioned, justified by new evidence, and does not rewrite prior experiments.

## 14. Global invariants

The following invariants are mandatory:

1. **Immutable identity:** primary IDs never change or get reassigned.
2. **Execution identity:** request, attempt, and run columns and values are
   distinct; status/foreign-key consistency is enforced.
3. **One successful commit:** one attempt produces at most one run; a run comes
   only from a successful attempt.
4. **Failure retention:** failed/aborted attempts and their artifacts remain
   queryable; no run is fabricated for them.
5. **Content integrity:** every stored/referenced artifact matches its SHA-256
   and byte length.
6. **Observation separation:** artifacts carry no implied availability or use;
   observations and `run_input` carry those claims.
7. **Cutoff safety:** every required input and ancestor used by a run is
   available no later than its `analysis_cutoff_at`.
8. **Visible fallback:** unavailable source-publication time uses the explicit
   `RETRIEVED_AT_FALLBACK` basis and retrieval instant.
9. **Acyclic provenance:** derivation graphs are acyclic, connected to their
   semantic entities, and reproducible.
10. **Temporal monotonicity:** a child's derived availability is not earlier
    than any required parent.
11. **Lineage completeness:** all content-influencing parents use required
    edges; every setup explicitly links all influencing research.
12. **Versioned derivation:** every materialized derived value references the
    immutable policy version used to compute it.
13. **Eligibility correctness:** eligible times are derived, never earlier than
    setup availability, and null for ineligible results.
14. **Lifecycle legality:** event sequences match the allowed transition matrix,
    terminal states have no successors, and state projections replay exactly.
15. **No lifecycle rewind:** `TP1_HIT` never transitions to `OPEN`.
16. **Unfilled finality:** expiry and pre-fill invalidation use their distinct
    terminal states and can never later fill.
17. **Outcome separation:** each outcome has exactly one family; policy and
    metric semantics are family-specific.
18. **PIT honesty:** legacy classification gates cohort use and is never inferred
    optimistically.
19. **Experiment reproducibility:** a registered experiment pins inputs, code,
    schema, policies, cohort, exclusions, and metric definitions.
20. **Atomic visibility:** committed runs never expose a partial required graph
    or incomplete manifest.
21. **Determinism:** identical canonical inputs and pinned versions produce the
    same semantic result and hashes, excluding explicitly recorded operational
    metadata.
22. **Supersession, not mutation:** corrections, policy changes, and revised
    registrations create new linked versions.

## 15. Required conformance tests

An implementation is not Ledger Foundation V1 conformant until automated tests
prove:

- schema creation at version 1, foreign-key enforcement, all enum/check/unique
  constraints, and rejection of an unsupported schema version;
- canonical timestamp/JSON/hash handling and deterministic serialization;
- request idempotency, retry ordinals, ID namespace separation, attempt/run
  cardinality, atomic commit, and rollback of partial run commits;
- retention and retrieval of artifacts from failed and aborted attempts;
- artifact deduplication without observation deduplication, byte/hash
  verification, and explicit actual-use records;
- all availability bases, cutoff equality boundaries, fallback behavior, and
  prohibition of post-cutoff ancestors;
- graph cycle rejection, required/optional edge semantics, deterministic
  topological propagation, and recomputation equality for materialized times;
- research supersession, mandatory setup lineage, and rejection of missing
  content-influencing edges;
- eligibility reproduction under its exact version, calendar/session boundary
  behavior, explicit ineligibility, and preservation across policy upgrades;
- every allowed lifecycle transition, rejection of every other pair, no events
  after terminal states, no `TP1_HIT` rewind, unfilled terminal behavior, event
  replay, quantity conservation, and deterministic collision/gap precedence;
- independent fixed-horizon and lifecycle policies/results, missing market-data
  handling, and prevention of cross-family metric substitution;
- registration/versioning and repeatable execution of all Challenge Cases;
- immutable Experiment Registry definitions/results and exact dependency
  pinning; and
- each legacy/PIT classification, cohort gating, conservative-bound behavior,
  and append-only reclassification.

Tests MUST include database-level negative cases, application-level property
tests for graph and lifecycle invariants, deterministic golden fixtures, and
restart/retry tests. Documentation string matching alone is not conformance.

## 16. Freeze boundary and change control

This freeze fixes the entities, meanings, enum semantics, temporal rules,
identity separation, lineage requirements, lifecycle transition matrix,
outcome-family separation, legacy/PIT gates, and registry responsibilities in
this document. Implementation details may be chosen only where this document
explicitly permits alternatives and only if all invariants remain true.

Clarifications that do not change observable meaning may be recorded as
non-semantic notes. Any change to a field's meaning, allowed value, required
relationship, availability rule, transition, outcome definition, policy
versioning rule, or invariant is semantic and therefore requires a new
specification version and freeze identifier.
