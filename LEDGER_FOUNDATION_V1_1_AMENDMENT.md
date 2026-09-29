# TRINITY Ledger Foundation V1.1 — Additive Amendment

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.1.0`

Parent freeze: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.0.0`

Parent specification: `LEDGER_FOUNDATION_V1_SPEC.md`

Parent specification SHA-256:
`ddbc36f7795cf7f8e28e1a78415f5a28809bd68b325e0bcb8624d52702b030f9`

Status: **FROZEN**

Rule: **Any semantic change after this freeze requires a new specification
version.**

## 1. Authority and compatibility

This document is the authoritative additive semantic amendment to Ledger
Foundation V1.0.0. It supersedes V1.0.0 only where it adds or clarifies missing
semantics identified by the frozen-spec internal consistency audit. All V1.0.0
entities, names, fields, enum values, relationships, temporal rules, lifecycle
transitions, and invariants remain authoritative unless this amendment
explicitly adds a constraint or definition.

This amendment does not authorize an alternate architecture. It closes the
request-to-outcome path without requiring distributed coordination, an ORM,
cloud services, broker execution, or an ML/memory subsystem.

The implementation erratum in
`LEDGER_FOUNDATION_V1_IMPLEMENTATION_ERRATA.md` is incorporated normatively.
Migration `0001_ledger_core.sql` remains byte-for-byte immutable.

## 2. Common additive conventions

### 2.1 Immutability and checked projections

Historical identity, artifacts, links, and events are append-only and reject
`UPDATE` and `DELETE` at the SQLite level. A **checked projection** is mutable
only through a named Ledger transaction that simultaneously appends the
authoritative history event. Direct projection mutation is forbidden.

V1.1 has only three checked projections:

- terminal fields on `attempt`;
- `run_request_control`; and
- state/quantity projection fields on `trade_instance`.

A projection MUST replay exactly from its append-only event stream. Projection
mutation without its corresponding event, or an event without its projection
mutation, is invalid and MUST roll back atomically.

Separately, the nullable finalization FKs `derivation_node.run_id`,
`research_record.run_id`, and `setup.run_id` may undergo exactly one checked
null-to-value binding in the atomic run-commit transaction. Before binding,
the research or setup belongs to its producing attempt through its required
derivation node. This is not general projection mutability; each field is
immutable after binding. No other nullable FK gains this exception.

### 2.2 Decimal, JSON, and timestamp values

Decision-relevant decimal values are canonical decimal strings. Binary
floating-point values MUST NOT cross an artifact hashing or persisted decision
boundary. Decimal strings use optional leading `-`, one or more integer digits,
an optional fractional part without trailing zeroes, no exponent, and `0`
rather than negative zero.

Timestamps and canonical JSON retain the V1.0.0 rules. Unordered collections
MUST be sorted by the owning domain schema before canonicalization; the generic
canonicalizer never guesses that an array is unordered.

### 2.3 Lightweight policy identity

Every policy that can change a historical result is identified by:

- `policy_kind`;
- immutable `policy_version`;
- `definition_artifact_id`, referencing canonical policy JSON;
- `definition_sha256`;
- `code_commit`; and
- `created_at`.

`(policy_kind, policy_version)` is unique. The definition artifact and code
commit are immutable. There is no implicit `latest` policy. Callers select a
policy explicitly, and persisted results store that exact identity. Separate
policy tables defined below MAY share implementation code, but not identity.

## 3. Core canonicalization and artifact identity

### 3.1 Structured artifacts

The canonicalization version is `JCS-LEDGER-SUBSET-V1`. It is the implemented
RFC 8785/JCS-compatible restricted subset:

- output is deterministic UTF-8 without a byte-order mark;
- object keys are ordered by UTF-16 code units;
- strings use JSON escaping and lone Unicode surrogates are rejected;
- null, booleans, strings, arrays, objects, and integers in the interoperable
  range `[-9007199254740991, 9007199254740991]` are supported;
- arrays preserve caller-supplied semantic order;
- duplicate object keys are rejected;
- NaN, positive/negative Infinity, and non-zero binary floats are rejected;
- every signed floating zero accepted at an input boundary canonicalizes to
  integer token `0`; and
- exact non-integer decimals are canonical decimal strings.

For opaque artifacts, the canonicalization version is `IDENTITY` and the exact
supplied bytes are hashed without normalization.

### 3.2 Artifact hash

The artifact content-address preimage is exactly:

```text
UTF8("TRINITY-LEDGER-V1") || 0x00 ||
UTF8(artifact_type) || 0x00 ||
UTF8(canonicalization_version) || 0x00 ||
payload_bytes
```

The artifact ID is `sha256:` followed by the lowercase SHA-256 of that
preimage. `artifact.sha256` remains the lowercase SHA-256 of `payload_bytes`.
`artifact.content_hash` is the domain-separated digest contained in
`artifact_id`. `artifact_kind` stores `artifact_type`; the canonicalization
version and immutable payload bytes are persisted with the artifact.

`(sha256, byte_length)` remains unique. Reusing identical payload bytes with
incompatible immutable metadata is an error, not an overwrite. Reads verify
byte length, raw payload SHA-256, domain-separated ID, canonical form, and
storage address.

## 4. Forward migration semantics

- `schema_metadata.schema_version = 1` is the frozen logical schema family.
- `schema_migration.schema_version` is the monotonic migration sequence
  `1, 2, 3, ...`.
- `schema_metadata.applied_migration_id` identifies the bootstrap migration.
- `schema_metadata` remains immutable after bootstrap.
- Current migration level is the maximum verified, gap-free migration sequence.
- The ordered registry MUST reject gaps, duplicates, reordering, unknown
  applied migrations, identity drift, and hash drift before applying pending
  migrations.
- Each migration and its history row commit in one transaction.
- Later migrations MUST NOT recreate or reinsert `schema_metadata` unless a
  future frozen specification explicitly requires it.

## 5. Execution coordination

### 5.1 Request idempotency

`run_request` retains all V1.0.0 fields. The idempotency constraint is:

```text
UNIQUE(requested_by, idempotency_key)
```

`requested_by` is the caller scope. The semantic request fingerprint is the
SHA-256 of canonical JSON containing exactly:

- `request_kind`;
- `analysis_cutoff_at`;
- parsed canonical `parameters_json`; and
- `baseline_commit`.

`requested_at`, `run_request_id`, and operational metadata are excluded.
Replaying the same caller/key with the same fingerprint returns the original
request without inserting a row. Reuse with a different fingerprint raises an
idempotency conflict. A request is otherwise immutable.

### 5.2 `run_request_control`

`run_request_control` is the mutable operational projection for one request:

| Field | Contract |
|---|---|
| `run_request_id` | Primary key and FK to `run_request` |
| `current_attempt_id` | Nullable unique FK to the currently authorized `attempt` |
| `current_fence_token` | Non-negative monotonic integer; initially `0` |
| `request_state` | `ACTIVE` or `COMMITTED` |
| `committed_run_id` | Nullable unique FK to `run`; required only when `COMMITTED` |
| `updated_at` | Projection update time |

It has exactly one row per request. It is not historical truth. Its state MUST
be reproducible from attempts, attempt events, and the committed run.

### 5.3 Attempt allocation and authority

`attempt` retains all V1.0.0 fields and adds `fence_token`. Within one
`BEGIN IMMEDIATE` transaction, allocation MUST:

1. require request state `ACTIVE`;
2. revoke any current attempt;
3. if that attempt is still `RUNNING`, terminalize it as `ABORTED` with reason
   code `SUPERSEDED_BY_RETRY` and append the required events;
4. allocate `attempt_ordinal = max(existing ordinal) + 1`;
5. allocate `fence_token = current_fence_token + 1`;
6. insert the new `RUNNING` attempt;
7. append its `ALLOCATED` event; and
8. update `run_request_control` to the new attempt and token.

Both `(run_request_id, attempt_ordinal)` and `(run_request_id, fence_token)` are
unique. Attempt and request identifiers MUST be unequal.

An attempt is authorized only when all are true in the same transaction as an
authoritative write:

- request state is `ACTIVE`;
- control `current_attempt_id` equals the supplied `attempt_id`;
- control `current_fence_token` equals the attempt's token; and
- attempt status is `RUNNING`, except that successful run commit performs its
  final authorization check immediately before the atomic success transition.

Every attempt-scoped authoritative write MUST execute this check. A stale
worker may place deduplicated bytes in `artifact`, but it MUST NOT attach those
bytes, create observations, create derivation records, append attempt events,
or commit a run. No distributed lease is implied.

### 5.4 Attempt status and events

Allowed status transitions are:

| From | To |
|---|---|
| `RUNNING` | `SUCCEEDED` |
| `RUNNING` | `FAILED` |
| `RUNNING` | `ABORTED` |

Terminal statuses have no outgoing transitions. Successful terminalization is
performed only by the committed-run transaction in §6.4.

Terminal constraints:

- `RUNNING`: `finished_at`, `failure_class`, and `failure_message` are null.
- `SUCCEEDED`: `finished_at` is required; failure fields are null.
- `FAILED`: `finished_at` and `failure_class` are required; message is nullable.
- `ABORTED`: `finished_at` is required; failure fields are null, preserving the
  V1.0.0 field contract. Its reason is carried by the terminal event.

Identity, request, ordinal, token, start, worker, code, and environment fields
are immutable. Status and terminal fields are a checked one-way projection.

`attempt_event` is authoritative append-only history:

| Field | Contract |
|---|---|
| `attempt_event_id` | Primary identity |
| `attempt_id` | FK to `attempt` |
| `sequence_number` | Positive ordinal, unique per attempt |
| `event_type` | `ALLOCATED`, `AUTHORITY_REVOKED`, `SUCCEEDED`, `FAILED`, or `ABORTED` |
| `event_at` | Event time |
| `from_status` | Nullable only for `ALLOCATED` |
| `to_status` | Resulting status; unchanged for `AUTHORITY_REVOKED` |
| `reason_code` | Nullable stable coordinator/worker reason; required for `ABORTED` |
| `failure_class` / `failure_message` | Terminal diagnostic fields where applicable |
| `actor_identity` | Coordinator or worker identity |
| `details_artifact_id` | Nullable canonical diagnostic artifact |
| `created_at` | Ledger insertion time |

`(attempt_id, sequence_number)` is unique. Allocation creates sequence `1`.
Revocation and automatic abort are separate ordered events in the same
allocation transaction. Direct event UPDATE/DELETE is forbidden.

Repeating the identical terminalization request returns the existing terminal
state without a new event. A conflicting repeat raises an invalid-transition
error. Failed and aborted attempts remain queryable permanently.

## 6. Execution artifacts and committed run closure

### 6.1 `attempt_artifact`

| Field | Contract |
|---|---|
| `attempt_artifact_id` | Primary identity |
| `attempt_id` | FK to `attempt` |
| `artifact_id` | FK to `artifact` |
| `role` | Enum below |
| `attached_at` | Attachment time |
| `ordinal` | Positive deterministic ordering within attempt and role |

Allowed roles are `OUTPUT`, `RESULT_MANIFEST`, `FAILED_ATTEMPT_OUTPUT`, `LOG`,
`DIAGNOSTIC`, and `PARTIAL_OUTPUT`. `(attempt_id, role, ordinal)` and
`(attempt_id, artifact_id, role)` are unique. Rows are immutable.

An authorized running attempt may attach any non-manifest role. A failed or
aborted attempt retains every existing attachment. Terminalization MUST NOT
delete or relabel artifacts. `RESULT_MANIFEST` is attached only by successful
run commit. A failure/abort transaction may attach pending diagnostic or
partial-output artifacts before appending the terminal event and changing the
status projection.

### 6.2 `run_input`

| Field | Contract |
|---|---|
| `run_input_id` | Primary identity |
| `run_id` | FK to `run` |
| `input_observation_id` | FK to `input_observation` |
| `input_role` | Non-empty, policy-owned stable role |
| `derivation_node_id` | FK to the run's `INPUT_OBSERVATION` derivation node |
| `created_at` | Atomic run-commit time |

`(run_id, input_observation_id, input_role, derivation_node_id)` is unique.
Rows are immutable. Every observation that influenced a committed result MUST
appear, and a stored-but-unused observation MUST NOT appear.

### 6.3 Canonical result manifest

The artifact kind is `ledger.run-result-manifest.v1` and canonicalization is
`JCS-LEDGER-SUBSET-V1`. Its canonical JSON contains exactly:

```text
manifest_version                 = "1"
run_id
run_request_id
attempt_id
analysis_cutoff_at
request_kind
baseline_commit
attempt_code_commit
environment_fingerprint
policy_references[]              sorted by (policy_kind, policy_version)
input_observation_ids[]          sorted lexicographically
derivation_node_ids[]            sorted lexicographically
output_artifact_ids[]            sorted lexicographically; excludes manifest
research_ids[]                   sorted lexicographically
setup_ids[]                      sorted lexicographically
eligibility_ids[]                sorted lexicographically
trade_ids[]                      sorted lexicographically
outcome_ids[]                    sorted lexicographically
```

Absent entity families use empty arrays. The manifest contains no commit time,
host path, secret, or mutable locator. Its artifact ID is stable for identical
semantic closure.

### 6.4 Successful commit transaction

Successful commit executes in one `BEGIN IMMEDIATE` transaction:

1. verify the request, attempt, and run IDs are pairwise unequal;
2. perform the current-attempt/fence authorization check;
3. require attempt status `RUNNING`;
4. validate all required derivation availability against
   `analysis_cutoff_at`;
5. validate referenced entities, `run_input`, policies, and manifest closure;
6. create and verify the result-manifest artifact;
7. attach it as `RESULT_MANIFEST`;
8. insert `run` with status `COMMITTED`;
9. append `SUCCEEDED` and update the attempt terminal projection;
10. bind all manifest-listed attempt-local derivation nodes, research records,
    and setups to this run as permitted by §§2.1 and 8.3;
11. insert immutable `run_input` rows; and
12. set request control to `COMMITTED` and its `committed_run_id`.

Any failure rolls back every step. A repeated commit by the same attempt with
the same run ID and manifest returns the existing run. Reuse with another run
ID, another manifest, another attempt, or after authority loss raises a
finalization conflict. No attempt may create more than one run; no committed
request may allocate another attempt.

## 7. Observation provenance

### 7.1 `source_id`

`source_id` is a stable logical identifier, not a foreign key and not a machine
locator. It has canonical form `provider:dataset[:contract-version]`, using
lowercase ASCII components separated by `:`. It MUST NOT contain credentials,
filesystem paths, host-specific directories, query secrets, or mutable
endpoints. This is an explicit exception to the usual entity-ID foreign-key
rule because the source namespace is externally governed.

Operational source bindings MAY exist outside Ledger history. Changing one has
no effect on source, artifact, observation, or derivation identity.

### 7.2 Minimum `source_metadata_json`

Canonical metadata contains:

```text
schema_version                   = "1"
provider
dataset_name
source_record_key
acquisition_method
availability_rule_id
availability_rule_version
evidence_artifact_ids[]          sorted lexicographically
provider_metadata               canonical object; may be empty
future_effective_at              nullable RFC 3339 timestamp
```

It MUST NOT contain secrets or mutable local paths. Evidence artifacts are
immutable and MUST be attached to the observing attempt as `DIAGNOSTIC` or
`OUTPUT` before observation insertion.

### 7.3 Availability bases

`SOURCE_PUBLISHED_AT` requires:

- non-null `source_published_at`;
- a recognized, immutable availability rule version;
- at least one evidence artifact containing or independently authenticating
  that timestamp; and
- `effective_available_at = source_published_at`, except for the proven
  future-effective rule below.

`RETRIEVED_AT_FALLBACK` requires `source_published_at = null` and
`effective_available_at = retrieved_at` exactly. A caller-supplied earlier
instant is ignored and rejected. Source publication text without qualifying
evidence does not upgrade this basis.

`LEGACY_ASSERTED_AT` requires a `legacy_record_classification` of
`LEGACY_PIT_BOUNDED` or `LEGACY_PIT_VERIFIED`, its evidence artifact, and the
asserted conservative instant. `effective_available_at` equals that asserted
instant. It is unavailable to native acquisition paths.

A future-effective observation is permitted only under
`SOURCE_PUBLISHED_AT` or `LEGACY_ASSERTED_AT`, with non-null
`future_effective_at` authenticated by the same evidence. Then:

```text
effective_available_at = max(proven_availability_at, future_effective_at)
```

This is the only case where retrieval may precede effective availability.
`RETRIEVED_AT_FALLBACK` never uses this exception; an unproved future-effective
record must be re-observed no earlier than its effective instant.

Observation creation is an authorized attempt-scoped write. It is immutable,
and the same artifact may have arbitrarily many distinct observations.

## 8. Derivation closure

### 8.1 Entity mapping

The allowed node-to-entity mappings are:

| `node_kind` | `entity_type` | Target |
|---|---|---|
| `INPUT_OBSERVATION` | `input_observation` | `input_observation.input_observation_id` |
| `NORMALIZED_FACT` | `artifact` | `artifact.artifact_id` |
| `RESEARCH` | `research_record` | `research_record.research_id` |
| `SETUP` | `setup` | `setup.setup_id` |
| `ELIGIBILITY` | `setup_execution_eligibility` | its `eligibility_id` |
| `OUTCOME` | `outcome` | `outcome.outcome_id` |

No other pair is valid in V1.1. Because SQLite cannot express a polymorphic
FK, table-specific triggers plus writer validation MUST enforce target
existence, and conformance tests MUST exercise every mapping negatively.

### 8.2 Derivation policy

`derivation_policy` implements the common policy identity of §2.3 with
`policy_kind = DERIVATION`. V1.1 policy version
`MAX_REQUIRED_PARENTS_V1` implements the unchanged V1.0.0 formula. Every node
stores both its version and definition artifact ID.

### 8.3 Attempt/run lineage

All nodes connected by an edge have the same producing `attempt_id`. A raw node
may represent an observation originally recorded by another attempt; its own
`attempt_id` identifies the current consuming/deriving attempt. Cross-attempt
edges are forbidden. This preserves reusable observations without merging
execution graphs.

Nodes begin attempt-local with `run_id = null`. During the successful commit
transaction, the complete ancestor-closed set of manifest-listed nodes MUST
undergo exactly one checked binding from null to that attempt's new `run_id`.
Any other `run_id` update is forbidden. After binding, every edge endpoint in
that committed graph has the same run ID. An unlisted attempt-local node is not
silently adopted by the run.

### 8.4 Atomic creation and parent semantics

A raw input node is created atomically with no parents and copies the
observation's effective availability into both direct and derived availability.

A derived node and its complete edge set are created in one transaction. All
parents must already exist. `required = 1` means the parent influenced content
and participates in temporal and PIT propagation. `required = 0` is provenance
only and MUST NOT influence content, availability, PIT class, or result hashes.
Adding an edge after node creation is forbidden.

Before insertion, a recursive CTE checks that no proposed parent is the child
or a descendant of the child. Self-edges and cycles abort the transaction.
Parents are ordered deterministically by `(edge_role, parent_node_id)` for
recomputation and evidence serialization.

The writer computes `derived_available_at`; callers cannot supply it. Reads and
run commit recompute it from the graph and policy. A mismatch is an integrity
error. Optional parents are excluded. All content-influencing parents must have
required edges.

## 9. PIT closure

### 9.1 Node PIT class

Every derivation node stores one frozen PIT classification. Raw native nodes
are `NATIVE_PIT`; mapped legacy observations inherit the current classification
record used at creation. Derived nodes combine required parents only:

1. any `LEGACY_NON_PIT` → `LEGACY_NON_PIT`;
2. otherwise any `LEGACY_PIT_UNKNOWN` → `LEGACY_PIT_UNKNOWN`;
3. otherwise any `LEGACY_PIT_BOUNDED` → `LEGACY_PIT_BOUNDED`;
4. otherwise any `LEGACY_PIT_VERIFIED` → `LEGACY_PIT_VERIFIED`;
5. otherwise all parents are `NATIVE_PIT` → `NATIVE_PIT`.

Direct intrinsic content may only weaken the result. No policy or manual
override may strengthen it. Optional parents do not participate. PIT-safe
cohort gates retain the V1.0.0 rules.

### 9.2 Legacy reclassification

`legacy_record_classification` is append-only:

| Field | Contract |
|---|---|
| `classification_id` | Primary identity |
| `legacy_record_id` | Stable imported-record identity |
| `classification_version` | Positive ordinal unique per legacy record |
| `classification` | Frozen V1.0.0 PIT enum |
| `classifier_version` | Immutable classifier identity |
| `evidence_artifact_id` | FK to immutable evidence |
| `classified_at` | Classification time |
| `supersedes_classification_id` | Nullable unique FK to immediate prior version |
| `mapped_input_observation_id` | Nullable FK to imported observation |

Reclassification creates the next version and immediate supersession link.
Prior experiments and nodes retain the classification ID originally used.

**DEFINED IN V1.1; IMPLEMENTATION DEFERRED:** legacy import and reclassification
writers may be implemented after the native end-to-end path. Native acquisition
does not depend on them.

## 10. Research, LLM, and setup closure

### 10.1 Research method identity

`research_method` contains `research_method_id`, `research_kind`,
`method_version`, `definition_artifact_id`, `code_commit`, and `created_at`.
`(research_kind, method_version)` is unique and immutable. A
`research_record.method_version` MUST resolve to the matching method record;
the record also stores `research_method_id` as an FK. While attempt-local,
`research_record.run_id` is null and its derivation node MUST belong to the
producing attempt; successful commit performs the sole permitted run binding.

### 10.2 `llm_interaction`

| Field | Contract |
|---|---|
| `llm_interaction_id` | Primary identity |
| `attempt_id` | FK to authorized producing attempt |
| `interaction_ordinal` | Positive ordinal unique per attempt |
| `provider` / `model` / `model_version` | Non-secret immutable model identity |
| `request_artifact_id` | Canonical prompt/messages/tools/config artifact |
| `response_artifact_id` | Nullable exact response artifact |
| `error_artifact_id` | Nullable sanitized error artifact |
| `status` | `SUCCEEDED` or `FAILED` |
| `started_at` / `finished_at` | Required timestamps |
| `input_tokens` / `output_tokens` | Nullable non-negative provider counts |
| `created_at` | Ledger insertion time |

Exactly one of response or error artifact is required according to status.
Rows are immutable. Failures are persisted even if the surrounding attempt
continues or retries the call. Credentials, transient headers, and secrets are
never artifacts.

`research_llm_interaction` has composite primary key
`(research_id, llm_interaction_id, interaction_role)`, plus a positive
`ordinal` unique within `(research_id, interaction_role)`. It is inserted only
after both immutable records exist; both must belong to the same attempt.
Roles are `PRIMARY`, `SUPPORTING`, and `CRITIQUE`. Failed interactions MAY be
linked for audit but cannot be `PRIMARY`. Every successful LLM interaction
that influences research MUST be linked, and the research derivation node has
a required edge from each influencing response artifact's normalized node.

### 10.3 Setup policy and exact setup persistence

`setup_policy` implements §2.3 with `policy_kind = SETUP`. `setup` retains all
V1.0.0 fields and adds exact identity fields:

- `currency`: uppercase ISO 4217 code;
- `venue_id`: stable external venue code;
- `calendar_id`: immutable versioned trading-calendar identity; and
- `price_adjustment`: `RAW`, `SPLIT_ADJUSTED`, or `TOTAL_RETURN_ADJUSTED`.

`instrument_id` is stable external identity `namespace:symbol` and, like
`source_id`, is not a machine locator. `direction` is `LONG` or `SHORT`.
`entry_rule_json`, `stop_rule_json`, and `target_rule_json` each contain exactly
`schema_version`, `rule_type`, and canonical `parameters`. Their interpretation
is owned by the referenced setup policy. `valid_from <= expires_at`; an
`invalidates_at`, when present, must be within that interval.

While attempt-local, `setup.run_id` is null and its derivation node MUST belong
to the producing attempt. Successful commit performs the sole permitted run
binding. This allows lifecycle evaluation before a successful `run` exists
without fabricating a committed run or weakening lineage.

### 10.4 Research/setup lineage

`setup_research_lineage` has composite primary key
`(setup_id, research_id, lineage_role)`. Roles are `PRIMARY`, `SUPPORTING`,
`RISK`, and `CONTRARY`. `research_derivation_node_id` MUST be the node belonging
to that research record and the same attempt/run graph as the setup node.
Every influencing record is linked; at least one `PRIMARY` row is mandatory.
Rows and referenced records are immutable.

## 11. Execution eligibility

`eligibility_policy` retains V1.0.0 fields and conforms to §2.3 with
`policy_kind = ELIGIBILITY`. `(policy_version, definition_sha256)` identifies
one immutable definition. Policy selection is explicit; validity intervals
must contain `evaluated_at`, and overlapping applicable versions are rejected
rather than resolved by recency.

Reason codes are owned and versioned by the policy definition. Stored codes are
canonical arrays sorted lexicographically and use
`<policy-version>:<reason-name>`.

An eligibility result has required derivation edges from the setup and every
policy-required availability input. The definition artifact, calendar/session
versions, computed base instant, reason codes, and resulting
`execution_eligible_at` form its reproducible evidence.

Manual overrides add `supersedes_eligibility_id`, `override_authority`, and
`override_reason_artifact_id`. They create a new result under an explicit
override policy version. They never update, delete, or relabel the derived
result. An override cannot strengthen PIT classification.

## 12. Trade lifecycle closure

### 12.1 Lifecycle policy

`lifecycle_policy` conforms to §2.3 with `policy_kind = TRADE_LIFECYCLE`. Its
definition owns entry/fill rules, costs, rounding, gap handling, same-bar
precedence, partial-fill aggregation, post-TP1 behavior, and time exits. Every
trade stores the exact policy version and definition artifact.

`trade_instance` adds `initial_quantity`, `current_quantity`, `currency`, and
`price_adjustment`, all checked projections or immutable identity as
appropriate. Quantities are non-negative canonical decimal strings.

### 12.2 Exact `trade_event`

In addition to V1.0.0 fields, every event stores:

| Field | Contract |
|---|---|
| `event_type` | `ENTRY_FILL`, `TP1_FILL`, `TARGET_FILL`, `STOP_FILL`, `TIME_EXIT`, `EXPIRE_UNFILLED`, or `INVALIDATE_UNFILLED` |
| `execution_price` | Nullable canonical decimal; required for fill/exit events |
| `quantity_delta` | Canonical decimal; zero only for unfilled terminal events |
| `remaining_quantity` | Non-negative canonical decimal after the event |
| `fees` / `slippage` | Non-negative canonical decimals, default `0` |
| `execution_components_json` | Canonical ordered list of component fills |
| `interpretation_artifact_id` | Canonical evidence explaining raw-data interpretation |
| `created_at` | Ledger insertion time |

Event types map one-to-one to the frozen transition causes. Component fills do
not create unlisted state self-transitions; they are aggregated into the event
that crosses a frozen state boundary. Each component contains exactly
`ordinal`, `execution_price`, `quantity`, `observed_at`, and
`source_record_key`, ordered by ordinal. Sequence allocation, event insertion, and
projection update occur atomically. Terminal states reject further events.

Quantity conservation is checked after every event:

```text
prior remaining quantity - executed exit quantity = remaining_quantity
```

Entry establishes initial/current quantity. `CLOSED_*` states require zero
remaining quantity. `TP1_HIT` requires positive remaining quantity. Unfilled
terminal states require no entry quantity.

### 12.3 Raw evidence to interpreted event

`market_data_observation_id` references the raw observation. The interpretation
artifact contains policy identity, observation IDs, raw bar/tick values,
rounding, collision/gap decision, component fills, and selected event. Its
derivation node has required edges from every raw observation used. This
artifact—not an unexplained projected price—is the replay evidence.

## 13. Outcome closure

### 13.1 Outcome policy and status

`outcome_policy` conforms to §2.3 with `policy_kind` equal to
`FIXED_HORIZON` or `TRADE_LIFECYCLE`. `outcome` adds:

- `status`: `MEASURED`, `UNMEASURABLE`, or `INVALID`;
- `reason_code`: null only for `MEASURED`, otherwise a policy-owned stable code;
- `policy_definition_artifact_id`; and
- `metrics_schema_version`.

No unmeasurable setup disappears. Outcomes of different families remain
separate rows with separate policies and metric namespaces.

### 13.2 Fixed-horizon metrics

`metrics_json` has schema `ledger.fixed-horizon-metrics.v1`:

```text
anchor_at
price_adjustment
currency
horizons[] ordered by policy horizon ordinal:
  label
  target_at
  observed_at nullable
  price nullable canonical decimal
  return nullable canonical decimal
  status = MEASURED | UNMEASURABLE
  reason_code nullable
```

The outcome derivation node has required edges from the setup and all price
observations used.

### 13.3 Trade-lifecycle metrics

`metrics_json` has schema `ledger.trade-lifecycle-metrics.v1`:

```text
terminal_state
entry_quantity
entry_value
exit_quantity
exit_value
gross_pnl
fees
slippage
net_pnl
return
r_multiple nullable
holding_seconds
currency
price_adjustment
```

All numeric values except integer seconds are canonical decimal strings. The
metrics MUST reconcile exactly to the event stream and quantity conservation.

## 14. Deferred-but-defined boundaries

### 14.1 Challenge Cases

`challenge_case_result` contains `challenge_case_result_id`,
`challenge_case_id`, `run_id`, `code_commit`, `schema_version`, canonical
`policy_references_json`, `status` (`PASSED`, `FAILED`, or `ERROR`),
`evidence_artifact_id`, `started_at`, and `finished_at`.
`(challenge_case_id, run_id)` is unique. Challenge case status is `ACTIVE` or
`SUPERSEDED`; category is a non-empty version-owned string.

**DEFINED IN V1.1; IMPLEMENTATION DEFERRED:** registry and execution code may be
implemented after the native outcome path.

### 14.2 Experiment Registry

The V1.0.0 `experiment` record stores `policy_references_json` as a canonical
array of common policy identities sorted by kind/version,
`primary_metrics_json` as an ordered array of metric names plus definition
artifact IDs, and `exclusion_rules_json` as an ordered array of stable rule
codes. These fields concretize the corresponding V1.0.0 prose fields and are
immutable with the experiment version.

`experiment_run` contains `experiment_run_id`, `experiment_id`, `run_id`,
`role`, `status` (`RUNNING`, `SUCCEEDED`, `FAILED`, or `ABORTED`),
`started_at`, nullable `finished_at`, nullable `result_manifest_artifact_id`,
and nullable `failed_attempt_id`. Exactly one manifest or failed attempt is
required for a terminal successful or failed record respectively.

`experiment_result` contains `experiment_result_id`, `experiment_run_id`,
`metric_name`, `metric_definition_artifact_id`, `value_json`,
`evidence_artifact_id`, and `created_at`. `(experiment_run_id, metric_name)` is
unique. Rows are immutable. Every policy reference resolves through the common
identity contract in §2.3.

**DEFINED IN V1.1; IMPLEMENTATION DEFERRED:** experiment authoring and execution
may follow Challenge Cases.

### 14.3 Evaluator/export boundary

Evaluator views, reports, exports, and datasets are derived, non-authoritative
products. They MUST reference immutable run, experiment, policy, outcome, and
artifact identities and MUST NOT mutate Ledger facts. An export reintroduced as
input requires a new artifact and input observation; its former provenance is
metadata, not inherited availability.

**DEFINED IN V1.1; IMPLEMENTATION DEFERRED:** no evaluator table, view, or export
format is required for the core native path.

## 15. Atomic end-to-end path

The now-closed path is:

```text
run_request
  → authorized attempt
  → input_observation
  → derivation graph
  → research_record / llm_interaction as applicable
  → setup + research lineage
  → execution eligibility
  → fixed-horizon outcome and/or trade lifecycle + outcome
  → atomic committed run + canonical result manifest
```

Every arrow is represented by an immutable FK/link and required derivation edge
where content influence exists. Every attempt-scoped write checks authority in
its transaction. Every committed entity is listed in the result manifest.
Every decision-relevant timestamp propagates through the DAG. Every policy is
explicitly versioned and artifact-backed. No deferred registry or evaluator
component is required to execute this path.

## 16. Internal closure review

The 48 gaps from the V1.0.0 internal consistency audit are dispositioned below.

| Audit gap | Disposition |
|---|---|
| Migration-history model | RESOLVED — §4 incorporates the erratum |
| Canonical JSON and artifact identity | RESOLVED — §3 |
| Request idempotency | RESOLVED — §5.1 |
| Attempt ordinal/retry lineage | RESOLVED — §5.3 |
| Attempt status lifecycle | RESOLVED — §5.4 |
| Append-only attempt history | RESOLVED — §§2.1, 5.4 |
| Active/current attempt | RESOLVED — §§5.2–5.3 |
| Fencing/stale writers | RESOLVED — §5.3 |
| Attempt lifecycle events | RESOLVED — §5.4 |
| `attempt_artifact` | RESOLVED — §6.1 |
| Run commitment | RESOLVED — §6.4 |
| Result manifest | RESOLVED — §6.3 |
| `run_input` | RESOLVED — §6.2 |
| Stable source identity | RESOLVED — §7.1 |
| Source metadata | RESOLVED — §7.2 |
| Availability proof | RESOLVED — §7.3 |
| Future-effective exception | RESOLVED — §7.3 |
| Derivation entity reference | RESOLVED — §8.1 |
| Same attempt/run lineage | RESOLVED — §8.3 |
| Derivation policy | RESOLVED — §8.2 |
| DAG creation/finalization | RESOLVED — §8.4 |
| PIT propagation | RESOLVED — §9.1 |
| Legacy reclassification | EXPLICITLY DEFERRED WITHOUT BLOCKING THE CORE END-TO-END PATH — semantics fixed in §9.2 |
| Research method definition | RESOLVED — §10.1 |
| LLM interaction capture | RESOLVED — §10.2 |
| Failed LLM persistence | RESOLVED — §10.2 |
| Setup exact schema | RESOLVED — §10.3 |
| Instrument/calendar identities | RESOLVED — §10.3 |
| Setup/research lineage | RESOLVED — §10.4 |
| Setup/lifecycle/outcome policy definitions | RESOLVED — §§2.3, 10.3, 12.1, 13.1 |
| Eligibility policy | RESOLVED — §11 |
| Eligibility manual overrides | RESOLVED — §11 |
| Eligibility reason codes | RESOLVED — §11 |
| Trade event schema | RESOLVED — §12.2 |
| Lifecycle policy | RESOLVED — §12.1 |
| Trade projection mutation | RESOLVED — §§2.1, 12.2 |
| Raw observation vs interpreted event | RESOLVED — §12.3 |
| Quantity conservation | RESOLVED — §12.2 |
| Outcome status/reason | RESOLVED — §13.1 |
| Outcome policy | RESOLVED — §13.1 |
| Fixed-horizon metrics | RESOLVED — §13.2 |
| Lifecycle metrics | RESOLVED — §13.3 |
| Challenge result | EXPLICITLY DEFERRED WITHOUT BLOCKING THE CORE END-TO-END PATH — semantics fixed in §14.1 |
| Challenge status/category | EXPLICITLY DEFERRED WITHOUT BLOCKING THE CORE END-TO-END PATH — semantics fixed in §14.1 |
| Experiment run | EXPLICITLY DEFERRED WITHOUT BLOCKING THE CORE END-TO-END PATH — semantics fixed in §14.2 |
| Experiment result | EXPLICITLY DEFERRED WITHOUT BLOCKING THE CORE END-TO-END PATH — semantics fixed in §14.2 |
| Experiment policy pinning | RESOLVED — §§2.3, 14.2 |
| Evaluator boundary | EXPLICITLY DEFERRED WITHOUT BLOCKING THE CORE END-TO-END PATH — semantics fixed in §14.3 |

Disposition totals:

- **RESOLVED:** 42
- **EXPLICITLY DEFERRED WITHOUT BLOCKING THE CORE PATH:** 6
- **REMAINING CORE-PATH BLOCKERS:** 0

## 17. Change control

This amendment freezes the added identity rules, schemas, enum semantics,
transition rules, authorization checks, policy identity, evidence contracts,
metric schemas, and deferred boundaries. Implementation may choose indexes,
query structure, and internal code organization only where observable meaning
is unchanged.

Any change to these semantics requires a new specification version and freeze
identifier.
