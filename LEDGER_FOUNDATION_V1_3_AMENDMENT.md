# Ledger Foundation V1.3.0 — PIT and Record Classification Closure

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.3.0`

Parent freeze: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.2.0`

Scope: `PIT_AND_RECORD_CLASSIFICATION_CLOSURE`

## 1. Version, purpose, and boundary

This is an additive semantic amendment to Ledger Foundation V1.2.0. It is
version `1.3.0`, not a V1.2 patch, because it adds persisted classification
entities, classification behavior, a policy, classification-aware request
modes, and result manifest V2.

V1.3 closes append-only derivation-node classification, independent record and
PIT axes, exact raw evidence, required-ancestry propagation, reconstruction,
supersession, classified-run closure, and downstream gating. It does not
implement migration `0005` or alter the V1.2 temporal DAG.

## 2. Independent classification axes

`record_class` has exactly:

- `LEDGER_NATIVE`;
- `LEGACY_NON_LEDGER_ARTIFACT`.

`pit_class` has exactly:

- `ARCHIVED_POINT_IN_TIME`;
- `RECONSTRUCTED_NOT_ARCHIVED`;
- `NOT_APPLICABLE`;
- `UNKNOWN`.

The axes are independent and MUST NOT be collapsed or renamed.

## 3. Persistence entities

### 3.1 Identifier format

`classification_policy_id` and `derivation_node_classification_id` are opaque
RFC 4122 UUID version 4 identities in lowercase hyphenated form, matching:

```text
[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}
```

### 3.2 `pit_classification_policy`

| Field | Type and nullability | Contract |
|---|---|---|
| `classification_policy_id` | non-null text | UUIDv4 primary key |
| `policy_kind` | non-null text | Exactly `PIT_CLASSIFICATION` |
| `policy_version` | non-null text | Initially exactly `REQUIRED_ANCESTRY_PIT_V1` |
| `definition_artifact_id` | non-null text | FK to the immutable policy-definition Artifact |
| `code_commit` | non-null text | Non-empty immutable implementation provenance |
| `created_at` | non-null text | Normalized UTC timestamp |

`(policy_kind, policy_version)` is unique. `UPDATE` and `DELETE` are forbidden.
There is no `definition_sha256`, replacement digest, or mutable current-policy
pointer.

Semantic policy identity is `(policy_kind, policy_version)`. Row identity is
`classification_policy_id`. Exact definition bytes are identified solely by
`definition_artifact_id` through existing Artifact integrity. `code_commit` is
implementation provenance, not semantic policy identity.

### 3.3 `derivation_node_classification`

| Field | Type and nullability | Contract |
|---|---|---|
| `derivation_node_classification_id` | non-null text | UUIDv4 primary key |
| `derivation_node_id` | non-null text | FK to `derivation_node.derivation_node_id` |
| `pit_reference_at` | non-null text | Normalized UTC PIT reference instant |
| `classification_version` | non-null integer | Positive ordinal in the node/reference chain |
| `record_class` | non-null text | Intrinsic record-origin enum |
| `resolved_record_class` | non-null text | Conservatively propagated record-origin enum |
| `pit_class` | non-null text | Frozen PIT enum |
| `classification_basis` | non-null text | Frozen basis enum |
| `classification_policy_id` | non-null text | FK to `pit_classification_policy` |
| `evidence_artifact_id` | nullable text | FK to raw evidence; null for propagated derived classification |
| `classified_by_attempt_id` | non-null text | FK to the authorized classifying attempt |
| `created_at` | non-null text | Actual normalized UTC classification time |
| `supersedes_classification_id` | nullable text | Unique self-FK to the immediate prior version |

`classification_basis` has exactly:

- `RAW_ARCHIVE_EVIDENCE`;
- `RAW_RECONSTRUCTION_EVIDENCE`;
- `RAW_UNKNOWN_EVIDENCE`;
- `RAW_NOT_APPLICABLE_EVIDENCE`;
- `REQUIRED_PARENT_PROPAGATION`.

`(derivation_node_id, pit_reference_at, classification_version)` is unique.
Raw `INPUT_OBSERVATION` classifications require a non-null evidence Artifact
and matching raw basis. `NORMALIZED_FACT` classifications require
`REQUIRED_PARENT_PROPAGATION` and null evidence. `UPDATE` and `DELETE` are
forbidden.

### 3.4 `derivation_node_classification_parent`

| Field | Type and nullability | Contract |
|---|---|---|
| `child_classification_id` | non-null text | FK to the derived classification |
| `parent_classification_id` | non-null text | FK to the exact parent classification version used |

The composite primary key is
`(child_classification_id, parent_classification_id)`. The IDs MUST differ.
Every distinct direct REQUIRED DAG parent must appear exactly once. Each parent
classification must classify that parent node. Optional parents MUST NOT
appear. Parent and child classifications must share `pit_reference_at` and
policy identity. `UPDATE` and `DELETE` are forbidden.

The complete parent-classification set and child classification are inserted
atomically. A deferred child FK may be used if no orphan or partial closure can
commit.

## 4. Policy identity

The only V1.3 policy is:

```text
policy_kind    = PIT_CLASSIFICATION
policy_version = REQUIRED_ANCESTRY_PIT_V1
```

No generic policy framework or mutable default is introduced. A future
semantic algorithm requires a new explicit policy version and amendment.

## 5. Policy-definition Artifact

`artifact_type` is logical terminology for an Artifact's semantic type. Its
persisted representation is `artifact.artifact_kind`. The persisted envelope
is exactly:

```text
artifact_kind            = ledger.pit-classification-policy-definition.v1
schema_name              = ledger.pit-classification-policy-definition
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The base Artifact table remains unchanged. The canonical payload is the
following closed object. Every property is required; extra properties are
forbidden. Arrays use the displayed ASCII-binary order. Propagation pairs are
normalized so `left <= right` and ordered by `(left, right)`.

```json
{
  "schema_name": "ledger.pit-classification-policy-definition",
  "schema_version": "1",
  "policy_kind": "PIT_CLASSIFICATION",
  "policy_version": "REQUIRED_ANCESTRY_PIT_V1",
  "record_classes": ["LEDGER_NATIVE", "LEGACY_NON_LEDGER_ARTIFACT"],
  "pit_classes": ["ARCHIVED_POINT_IN_TIME", "NOT_APPLICABLE", "RECONSTRUCTED_NOT_ARCHIVED", "UNKNOWN"],
  "classification_bases": ["RAW_ARCHIVE_EVIDENCE", "RAW_NOT_APPLICABLE_EVIDENCE", "RAW_RECONSTRUCTION_EVIDENCE", "RAW_UNKNOWN_EVIDENCE", "REQUIRED_PARENT_PROPAGATION"],
  "record_origin_evidence_kinds": ["LEDGER_AUTHORIZED_CAPTURE", "LEGACY_IMPORT"],
  "pit_evidence_kinds": ["INDEPENDENT_ARCHIVE_PROOF", "INSUFFICIENT_PIT_EVIDENCE", "LEDGER_CONTEMPORANEOUS_CAPTURE", "PIT_IRRELEVANT_CONTENT", "POST_REFERENCE_RECONSTRUCTION"],
  "recognized_verifiers": [
    {"verifier_id":"INDEPENDENT_ARCHIVE_PROOF_V1","pit_evidence_kind":"INDEPENDENT_ARCHIVE_PROOF"},
    {"verifier_id":"LEDGER_AUTHORIZED_CAPTURE_V1","pit_evidence_kind":"LEDGER_CONTEMPORANEOUS_CAPTURE"},
    {"verifier_id":"LEDGER_INSUFFICIENT_EVIDENCE_V1","pit_evidence_kind":"INSUFFICIENT_PIT_EVIDENCE"},
    {"verifier_id":"LEDGER_PIT_IRRELEVANCE_V1","pit_evidence_kind":"PIT_IRRELEVANT_CONTENT"},
    {"verifier_id":"LEDGER_RECONSTRUCTION_EVIDENCE_V1","pit_evidence_kind":"POST_REFERENCE_RECONSTRUCTION"}
  ],
  "not_applicable_artifact_kinds": ["ledger.pit-classification-policy-definition.v1"],
  "record_origin_results": [
    {"record_origin_evidence_kind":"LEDGER_AUTHORIZED_CAPTURE","record_class":"LEDGER_NATIVE"},
    {"record_origin_evidence_kind":"LEGACY_IMPORT","record_class":"LEGACY_NON_LEDGER_ARTIFACT"}
  ],
  "raw_evidence_results": [
    {"pit_evidence_kind":"INDEPENDENT_ARCHIVE_PROOF","classification_basis":"RAW_ARCHIVE_EVIDENCE","pit_class":"ARCHIVED_POINT_IN_TIME","verifier_id":"INDEPENDENT_ARCHIVE_PROOF_V1"},
    {"pit_evidence_kind":"INSUFFICIENT_PIT_EVIDENCE","classification_basis":"RAW_UNKNOWN_EVIDENCE","pit_class":"UNKNOWN","verifier_id":"LEDGER_INSUFFICIENT_EVIDENCE_V1"},
    {"pit_evidence_kind":"LEDGER_CONTEMPORANEOUS_CAPTURE","classification_basis":"RAW_ARCHIVE_EVIDENCE","pit_class":"ARCHIVED_POINT_IN_TIME","verifier_id":"LEDGER_AUTHORIZED_CAPTURE_V1"},
    {"pit_evidence_kind":"PIT_IRRELEVANT_CONTENT","classification_basis":"RAW_NOT_APPLICABLE_EVIDENCE","pit_class":"NOT_APPLICABLE","verifier_id":"LEDGER_PIT_IRRELEVANCE_V1"},
    {"pit_evidence_kind":"POST_REFERENCE_RECONSTRUCTION","classification_basis":"RAW_RECONSTRUCTION_EVIDENCE","pit_class":"RECONSTRUCTED_NOT_ARCHIVED","verifier_id":"LEDGER_RECONSTRUCTION_EVIDENCE_V1"}
  ],
  "parent_rules": {
    "include_required_edges": true,
    "include_optional_edges": false,
    "require_complete_direct_parent_set": true,
    "require_same_pit_reference_at": true,
    "require_same_policy_identity": true
  },
  "pit_propagation": [
    {"left":"ARCHIVED_POINT_IN_TIME","right":"ARCHIVED_POINT_IN_TIME","result":"ARCHIVED_POINT_IN_TIME"},
    {"left":"ARCHIVED_POINT_IN_TIME","right":"NOT_APPLICABLE","result":"ARCHIVED_POINT_IN_TIME"},
    {"left":"ARCHIVED_POINT_IN_TIME","right":"RECONSTRUCTED_NOT_ARCHIVED","result":"RECONSTRUCTED_NOT_ARCHIVED"},
    {"left":"ARCHIVED_POINT_IN_TIME","right":"UNKNOWN","result":"UNKNOWN"},
    {"left":"NOT_APPLICABLE","right":"NOT_APPLICABLE","result":"NOT_APPLICABLE"},
    {"left":"NOT_APPLICABLE","right":"RECONSTRUCTED_NOT_ARCHIVED","result":"RECONSTRUCTED_NOT_ARCHIVED"},
    {"left":"NOT_APPLICABLE","right":"UNKNOWN","result":"UNKNOWN"},
    {"left":"RECONSTRUCTED_NOT_ARCHIVED","right":"RECONSTRUCTED_NOT_ARCHIVED","result":"RECONSTRUCTED_NOT_ARCHIVED"},
    {"left":"RECONSTRUCTED_NOT_ARCHIVED","right":"UNKNOWN","result":"UNKNOWN"},
    {"left":"UNKNOWN","right":"UNKNOWN","result":"UNKNOWN"}
  ],
  "record_class_propagation": [
    {"left":"LEDGER_NATIVE","right":"LEDGER_NATIVE","result":"LEDGER_NATIVE"},
    {"left":"LEDGER_NATIVE","right":"LEGACY_NON_LEDGER_ARTIFACT","result":"LEGACY_NON_LEDGER_ARTIFACT"},
    {"left":"LEGACY_NON_LEDGER_ARTIFACT","right":"LEGACY_NON_LEDGER_ARTIFACT","result":"LEGACY_NON_LEDGER_ARTIFACT"}
  ],
  "supersession_rules": {
    "chain_scope": ["derivation_node_id", "pit_reference_at"],
    "first_classification_version": 1,
    "classification_version_increment": 1,
    "parallel_policy_heads_allowed": false,
    "policy_change_supersedes_current_head": true,
    "implicit_current_policy_allowed_for_commit": false
  },
  "run_gating": [
    {"request_kind":"EXPLORATORY_NON_PIT","allowed_resolved_pit_classes":["ARCHIVED_POINT_IN_TIME","NOT_APPLICABLE","RECONSTRUCTED_NOT_ARCHIVED","UNKNOWN"]},
    {"request_kind":"PIT_SAFE_DECISION","allowed_resolved_pit_classes":["ARCHIVED_POINT_IN_TIME","NOT_APPLICABLE"]},
    {"request_kind":"RECONSTRUCTION","allowed_resolved_pit_classes":["ARCHIVED_POINT_IN_TIME","NOT_APPLICABLE","RECONSTRUCTED_NOT_ARCHIVED"]}
  ]
}
```

There is no `definition_sha256` or redundant digest. The existing
content-addressed `definition_artifact_id` and normal Artifact verification are
the complete exact-definition identity.

## 6. Classification-evidence Artifact

Logical `artifact_type` is persisted in `artifact.artifact_kind`. The evidence
envelope is exactly:

```text
artifact_kind            = ledger.pit-classification-evidence.v1
schema_name              = ledger.pit-classification-evidence
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The payload has exactly the following required properties. Nullable fields
must appear as JSON `null`; extra fields are forbidden. Displayed UUID,
Artifact, and timestamp values illustrate their types rather than literal
required values.

```json
{
  "schema_name": "ledger.pit-classification-evidence",
  "schema_version": "1",
  "derivation_node_id": "00000000-0000-4000-8000-000000000000",
  "input_observation_id": "00000000-0000-4000-8000-000000000000",
  "artifact_id": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "pit_reference_at": "2000-01-01T00:00:00.000000Z",
  "record_origin_evidence_kind": "LEDGER_AUTHORIZED_CAPTURE",
  "legacy_namespace": null,
  "legacy_record_key": null,
  "legacy_record_id": null,
  "pit_evidence_kind": "LEDGER_CONTEMPORANEOUS_CAPTURE",
  "verifier_id": "LEDGER_AUTHORIZED_CAPTURE_V1",
  "archive_captured_at": "2000-01-01T00:00:00.000000Z",
  "reconstruction_completed_at": null,
  "supporting_artifact_ids": [],
  "rationale_code": "CONTEMPORANEOUS_CAPTURE_VERIFIED"
}
```

The node must be `INPUT_OBSERVATION`; `input_observation_id` must equal its
`entity_id`; `artifact_id` must equal the observation's Artifact; and
`pit_reference_at` must equal the classification reference.

`supporting_artifact_ids` contains unique existing Artifact IDs in ASCII-binary
order. It cannot contain the evidence Artifact itself. Every supporting
Artifact and the evidence Artifact must be attached to the classifying attempt
as `DIAGNOSTIC` or `OUTPUT` before classification.

Allowed `record_origin_evidence_kind` values are exactly
`LEDGER_AUTHORIZED_CAPTURE` and `LEGACY_IMPORT`.

Allowed `pit_evidence_kind` values are exactly
`LEDGER_CONTEMPORANEOUS_CAPTURE`, `INDEPENDENT_ARCHIVE_PROOF`,
`POST_REFERENCE_RECONSTRUCTION`, `INSUFFICIENT_PIT_EVIDENCE`, and
`PIT_IRRELEVANT_CONTENT`.

Allowed `rationale_code` values are exactly:

- `CONTEMPORANEOUS_CAPTURE_VERIFIED`;
- `INDEPENDENT_ARCHIVE_VERIFIED`;
- `POST_REFERENCE_RECONSTRUCTION_VERIFIED`;
- `PIT_EVIDENCE_INSUFFICIENT`;
- `PIT_NOT_APPLICABLE_BY_ARTIFACT_KIND`.

Free-form rationale is forbidden.

### 6.1 Evidence validation matrix

| PIT evidence kind | Verifier and rationale | Timestamp rules | Supporting Artifacts | Result | Valid origin |
|---|---|---|---|---|---|
| `LEDGER_CONTEMPORANEOUS_CAPTURE` | `LEDGER_AUTHORIZED_CAPTURE_V1`; `CONTEMPORANEOUS_CAPTURE_VERIFIED` | `archive_captured_at = retrieved_at <= pit_reference_at`; reconstruction null | Zero or more | `ARCHIVED_POINT_IN_TIME` | Ledger authorized only |
| `INDEPENDENT_ARCHIVE_PROOF` | `INDEPENDENT_ARCHIVE_PROOF_V1`; `INDEPENDENT_ARCHIVE_VERIFIED` | Non-null `archive_captured_at <= pit_reference_at`; reconstruction null | At least one independently authenticating Artifact | `ARCHIVED_POINT_IN_TIME` | Either |
| `POST_REFERENCE_RECONSTRUCTION` | `LEDGER_RECONSTRUCTION_EVIDENCE_V1`; `POST_REFERENCE_RECONSTRUCTION_VERIFIED` | Archive null; `pit_reference_at < reconstruction_completed_at <= retrieved_at` | At least one reconstruction/source-evidence Artifact | `RECONSTRUCTED_NOT_ARCHIVED` | Either |
| `INSUFFICIENT_PIT_EVIDENCE` | `LEDGER_INSUFFICIENT_EVIDENCE_V1`; `PIT_EVIDENCE_INSUFFICIENT` | Both timestamps null | Zero or more | `UNKNOWN` | Either |
| `PIT_IRRELEVANT_CONTENT` | `LEDGER_PIT_IRRELEVANCE_V1`; `PIT_NOT_APPLICABLE_BY_ARTIFACT_KIND` | Both timestamps null | Zero or more | `NOT_APPLICABLE` | Either |

Independent proof must authenticate the exact subject Artifact or exact
source-native immutable version. A locator, filename, modification time, or
unsupported publication assertion is insufficient. PIT-irrelevant content
must use a policy-allowlisted Artifact kind. Kind, verifier, rationale,
timestamps, support, and origin must match the matrix.

Classification never changes `effective_available_at`.
`RETRIEVED_AT_FALLBACK` remains retrieval-based even when independent archive
evidence establishes PIT quality.

## 7. Legacy identity

For `LEGACY_IMPORT`, all of the following are non-null:

- `legacy_namespace`, matching `[a-z0-9][a-z0-9._-]*`;
- `legacy_record_key`, non-empty UTF-8 without NUL and stable in that namespace;
- `legacy_record_id`, equal to `legacy:sha256:<digest>`.

`<digest>` is lowercase SHA-256 of the canonical JSON bytes of:

```json
{"legacy_namespace":"<namespace>","legacy_record_key":"<record-key>"}
```

For `LEDGER_AUTHORIZED_CAPTURE`, all three legacy fields are null. Mutable
locators and payload bytes do not enter legacy identity. This does not
implement a full legacy importer, automatically upgrade existing legacy data,
or enable `LEGACY_ASSERTED_AT`.

## 8. Record-class semantics

`record_class` is intrinsic origin. A Ledger wrapper does not prove the
underlying evidence native. Raw class is computed from validated origin
evidence.

For `NORMALIZED_FACT`, intrinsic `record_class = LEDGER_NATIVE` and:

```text
resolved_record_class = LEGACY_NON_LEDGER_ARTIFACT
    if self or any REQUIRED ancestry resolves legacy
otherwise LEDGER_NATIVE
```

Optional parents do not participate. A derived record can be Ledger-native
while resolved legacy and therefore excluded from native-only populations.

## 9. PIT semantics

- `ARCHIVED_POINT_IN_TIME`: qualifying evidence proves the exact immutable
  version existed no later than `pit_reference_at`.
- `RECONSTRUCTED_NOT_ARCHIVED`: the historical representation was reconstructed
  after that reference without qualifying exact archive proof.
- `NOT_APPLICABLE`: policy proves no time-varying historical-fact semantics for
  the use.
- `UNKNOWN`: evidence is insufficient to prove another class.

`UNKNOWN` is not PIT-safe. A valid temporal DAG never upgrades reconstructed
evidence to archived. Classification never changes `effective_available_at` or
`derived_available_at`.

## 10. PIT propagation algebra

Let `A = ARCHIVED_POINT_IN_TIME`, `R = RECONSTRUCTED_NOT_ARCHIVED`,
`U = UNKNOWN`, and `N = NOT_APPLICABLE`:

| `⊗` | A | R | U | N |
|---|---|---|---|---|
| **A** | A | R | U | A |
| **R** | R | R | U | R |
| **U** | U | U | U | U |
| **N** | A | R | U | N |

The operation is associative, commutative, and idempotent. Only REQUIRED
parents participate. Optional parents are excluded. `UNKNOWN` dominates
PIT-relevant classes. `NOT_APPLICABLE` is neutral with a PIT-relevant class;
all-N ancestry resolves `NOT_APPLICABLE`.

## 11. Intrinsic PIT boundary

V1.3 prohibits intrinsic PIT weakening on `NORMALIZED_FACT`. Derived PIT class
comes only from complete REQUIRED parent classification. Every
content-influencing input must already be REQUIRED; omission is a DAG-integrity
violation. Future intrinsic PIT semantics require a new policy version and
amendment.

## 12. Classification-aware request kinds

V1.3 uses existing immutable `run_request.request_kind`; no purpose column is
added. Request kind and canonical `parameters_json` remain in the semantic
request fingerprint.

Classification-aware kinds are exactly `PIT_SAFE_DECISION`, `RECONSTRUCTION`,
and `EXPLORATORY_NON_PIT`.

For `PIT_SAFE_DECISION`, classification `pit_reference_at` equals
`analysis_cutoff_at`; `historical_as_of_at` and an explicit `pit_reference_at`
parameter are forbidden. Only resolved `ARCHIVED_POINT_IN_TIME` or
`NOT_APPLICABLE` is accepted.

For `RECONSTRUCTION`, `parameters_json.historical_as_of_at` is required,
normalized UTC, and no later than `analysis_cutoff_at`. An explicit
`pit_reference_at` parameter is forbidden. Classification reference equals
`historical_as_of_at`. Archived, reconstructed, and not-applicable results are
allowed; `UNKNOWN` is forbidden. It never claims historical PIT-safe execution.

For `EXPLORATORY_NON_PIT`, `parameters_json.pit_reference_at` is required,
normalized UTC, and no later than `analysis_cutoff_at`.
`historical_as_of_at` is forbidden. Any PIT enum is permitted, but no PIT-safe
claim is possible.

Other existing request kinds remain valid only for unclassified temporal
behavior and cannot create manifest V2.

## 13. Reconstruction

`historical_as_of_at` is the historical subject/reference instant.
`analysis_cutoff_at` is the actual maximum knowledge instant allowed to the
run. V1.2 cutoff enforcement remains unchanged. Evidence retrieved after the
historical reference can be used only when available by the actual run cutoff.
Reconstruction never backdates evidence or moves a cutoff.

## 14. Append-only supersession

The chain scope is `(derivation_node_id, pit_reference_at)`. Version 1 has no
superseded predecessor. Each later version increments exactly by one and
supersedes the unique current head. A row can be superseded only once.

There is one head across all policy versions; parallel policy-specific heads
are forbidden. A policy change continues the same ordinal chain. Old rows
remain queryable. Current head means the unsuperseded row for an explicit
node/reference and always carries its policy identity; it does not imply a
current policy. Runs must explicitly specify a supported policy.

No update, delete, DAG/timestamp change, or retroactive knowledge claim is
permitted.

## 15. Classification transaction

Classification rows must exist before run commit. Creation is one authorized
`BEGIN IMMEDIATE` transaction containing:

1. active request/current attempt/fence validation;
2. `RUNNING` attempt validation;
3. policy and definition-Artifact validation;
4. evidence and node/reference validation;
5. supersession validation;
6. complete direct-REQUIRED-parent classification validation;
7. classification-parent insertion; and
8. classification insertion.

Failure rolls back all classification state. A complete classification is
authoritative historical evidence and survives later attempt failure or
supersession. Failed attempts may retain complete classifications and evidence,
but never partial construction.

## 16. Classified run commit

A classification-aware successful commit does not create classifications. It
revalidates the complete closure, exact supported policy, and common
`pit_reference_at`; requires adopted classifications to have
`classified_by_attempt_id` equal to the successful consuming attempt;
preserves every V1.2 temporal, input, authority, and binding rule; and
atomically creates manifest V2 and finalizes the run. No partial classified run
may commit.

## 17. Result manifest V2

Manifest V1 remains valid. Classification-aware runs use persisted Artifact
kind `ledger.run-result-manifest.v2` (logical artifact type of the same value)
and `manifest_version = "2"`.

V2 retains all V1 fields and adds exactly:

- `pit_reference_at`;
- `derivation_node_classification_ids`;
- `resolved_record_class`;
- `resolved_pit_class`.

Classification IDs are the exact selected-root and complete REQUIRED-node
classification closure, lexicographically sorted. Optional-only
classifications are excluded.

`policy_references`, sorted by `(policy_kind, policy_version)`, contains exactly:

```json
{"policy_kind":"DERIVATION","policy_version":"MAX_REQUIRED_PARENTS_V1"}
```

and:

```json
{"policy_kind":"PIT_CLASSIFICATION","policy_version":"REQUIRED_ANCESTRY_PIT_V1"}
```

No run-table classification columns are added. The immutable V2 manifest is
authoritative.

## 18. Manifest V1 compatibility

Existing V1 runs remain permanently unclassified. Later classifications do
not upgrade them; no V1 manifest is rewritten as V2. An API may report
`UNCLASSIFIED` status, but that value is not in either classification enum. A
classified evaluation requires a new classification-aware request, attempt,
classification closure, run, and V2 manifest.

## 19. Downstream gate

Before future Research, LLM, or Setup consumption, the temporal DAG, every
REQUIRED classification, classification-parent closure, policy/reference, and
temporal cutoff must verify, and request mode must be explicit.

`PIT_SAFE_DECISION` requires archived/not-applicable resolution. Native-only
cohorts additionally require resolved native record class.
`RECONSTRUCTION` permits archived, reconstructed, and not-applicable evidence
without a historical PIT-safe execution claim. `EXPLORATORY_NON_PIT` may use
unknown, reconstructed, or legacy evidence but cannot enter PIT-safe or
native-only cohorts.

## 20. Migration boundary

This freeze defines but does not create migration `0005`. It will create the
three classification tables and required indexes/triggers, populate no
synthetic classifications, support populated level-4 databases, and leave
`derivation_node`, `derivation_edge`, the base Artifact schema, and migrations
`0001`–`0004` unchanged.

Migration `0005` may replace only current manifest-validation trigger behavior
as necessary to accept frozen V1 and V2 manifests without editing migration
`0002`.

## 21. Explicit supersession and preservation

V1.3 supersedes or narrows the old combined native/legacy/PIT taxonomy, V1.1
direct node PIT persistence, the assumption that native raw nodes are
automatically PIT-safe, intrinsic PIT weakening for current supported node
kinds, and the manifest contract only by adding V2 while preserving V1.

It preserves the V1.2 DAG, `derived_available_at`, cutoff enforcement,
optional-edge semantics, authority/fencing, immutable nodes/edges,
InputObservation behavior, `RETRIEVED_AT_FALLBACK`, and unsupported
`SOURCE_PUBLISHED_AT` and `LEGACY_ASSERTED_AT`.

## 22. Frozen test matrix

| Area | Required cases |
|---|---|
| Raw archived | Authorized contemporaneous capture; exact node/observation/Artifact/reference linkage |
| Independent archive | Exact immutable version authenticated; locator-only or missing proof rejected |
| Reconstruction | Correct post-reference timestamps/evidence; temporal validity cannot upgrade it |
| Unknown | Insufficient evidence resolves unknown and fails PIT-safe/reconstruction gates |
| Not applicable | Only allowlisted Artifact kinds; required null timestamps |
| Evidence | Invalid verifier, rationale, timestamp, origin, support order/duplicates, missing/extra JSON properties rejected |
| Claim authority | Caller cannot self-assert a stronger class |
| PIT algebra | Every cell; associative, commutative, idempotent; all-N result |
| Propagation | Multiple parents; multi-level and ten-level chains; no strengthening |
| Optional parents | Excluded from PIT, record propagation, and classification closure |
| Record class | Native-only, legacy raw, native reconstruction, derived-native with resolved legacy ancestry |
| Legacy | Stable ID; incomplete identity rejected; no automatic upgrade or native masquerade |
| Existing V1.2 nodes | Classification without node, edge, timestamp, or Artifact mutation |
| Supersession | Version 1; exact next ordinal; immediate head; old version retained; fork/gap rejected |
| Policy change | Same ordinal chain; parallel heads rejected; explicit commit policy |
| Immutability | Policy, classification, and parent rows reject update/delete |
| Authority/atomicity | Current writer succeeds; stale/forged/terminal fails; no partial closure |
| Closure | Missing, extra, optional, wrong-node/reference/policy parent rejected |
| PIT-safe gate | Archived/not-applicable accepted; unknown/reconstructed rejected; native cohort excludes legacy |
| Reconstruction gate | Historical reference required; allowed classes enforced; no historical-execution claim |
| Exploratory gate | Explicit reference; every class allowed; no PIT-safe claim |
| Temporal separation | Classification never changes observation or derived timestamps |
| Cutoff | Inclusive V1.2 cutoff retained; reconstruction cannot bypass it |
| Manifest V2 | Exact additions, required closure, two policy references, deterministic ordering |
| Manifest V1 | Permanently unclassified; no rewrite/upgrade; API-only status |
| Idempotency | Identical V2 finalization replays; incompatible closure fails |
| Migration | Fresh through level 5; populated level-4 upgrade; rerun zero; FK check; 0001–0004 unchanged |
| Regression | Prior Ledger/TRINITY tests; no network, production DB, or protected-module changes |

## 23. Freeze boundary

This amendment freezes the two axes, three persistence entities, policy and
definition, evidence and legacy identity, propagation, reconstruction,
request kinds, supersession, transaction boundaries, manifest V2 and V1
compatibility, downstream gate, test matrix, and future migration-0005
boundary. Implementation remains a later milestone.
