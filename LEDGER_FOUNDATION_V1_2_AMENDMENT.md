# TRINITY Ledger Foundation V1.2.0 — Derivation Closure Amendment

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.2.0`

Parent freeze: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.1.0`

Parent specifications:

- `LEDGER_FOUNDATION_V1_SPEC.md`
- `LEDGER_FOUNDATION_V1_1_AMENDMENT.md`
- `LEDGER_FOUNDATION_V1_IMPLEMENTATION_ERRATA.md`

Status: **FROZEN**

Rule: **Any semantic change after this freeze requires a new specification
version.**

## 1. Authority, purpose, and scope

This document is the authoritative additive semantic amendment for the Ledger
Foundation V1.2.0 temporal derivation DAG. It supersedes V1.0.0 and V1.1.0 only
where this document explicitly narrows or defers a prior requirement. All
unmodified parent-freeze semantics remain authoritative.

This amendment closes only:

- immutable derivation nodes and edges;
- raw observation and normalized-fact mappings;
- transitive temporal availability;
- deterministic recursive graph verification;
- attempt-local construction and committed-run binding;
- graph ancestry and `run_input` integration.

It does not require `SOURCE_PUBLISHED_AT`, `LEGACY_ASSERTED_AT`, PIT or legacy
classification persistence, legacy writers, Research, LLM capture, Setup,
execution eligibility, lifecycle, outcomes, Challenge Cases, Experiment
Registry, or evaluator/export behavior.

Milestone 3B supports only temporal provenance. PIT closure is deferred to
Milestone 3C under §13.

## 2. `derivation_node`

`derivation_node` has exactly these fields:

| Field | Type and nullability | Contract |
|---|---|---|
| `derivation_node_id` | non-null text | Primary identity; non-empty |
| `run_id` | nullable text | FK to `run.run_id`; null while attempt-local |
| `attempt_id` | non-null text | FK to `attempt.attempt_id`; producing/consuming attempt |
| `node_kind` | non-null text | Frozen node-kind enum below |
| `entity_type` | non-null text | Must form an allowed pair with `node_kind` |
| `entity_id` | non-null text | Non-empty exact identity of the represented entity |
| `direct_available_at` | nullable text | Normalized UTC timestamp intrinsic to a raw input |
| `derived_available_at` | non-null text | Normalized UTC checked temporal materialization |
| `derivation_policy_version` | non-null text | Exactly `MAX_REQUIRED_PARENTS_V1` |

The frozen `node_kind` values remain:

- `INPUT_OBSERVATION`;
- `NORMALIZED_FACT`;
- `RESEARCH`;
- `SETUP`;
- `ELIGIBILITY`;
- `OUTCOME`.

No uniqueness constraint collapses nodes representing the same entity. A
distinct derivation history may use a distinct node identity.

Every node is immutable from insertion except for the sole `run_id` binding
permitted by §9. `UPDATE` and `DELETE` are otherwise forbidden.

## 3. Entity mappings and target integrity

The complete frozen mappings are:

| `node_kind` | `entity_type` | Target | V1.2.0 Milestone-3B status |
|---|---|---|---|
| `INPUT_OBSERVATION` | `input_observation` | `input_observation.input_observation_id` | Supported |
| `NORMALIZED_FACT` | `artifact` | `artifact.artifact_id` | Supported |
| `RESEARCH` | `research_record` | `research_record.research_id` | Reserved; not instantiable |
| `SETUP` | `setup` | `setup.setup_id` | Reserved; not instantiable |
| `ELIGIBILITY` | `setup_execution_eligibility` | its `eligibility_id` | Reserved; not instantiable |
| `OUTCOME` | `outcome` | `outcome.outcome_id` | Reserved; not instantiable |

No other pair is valid. Milestone 3B MUST reject creation for every reserved
mapping because its target entity is not yet implemented.

Because the target is polymorphic, writer validation and table-specific
database triggers enforce both the allowed pair and target existence. A
missing target or invalid pair aborts the authoritative transaction.

## 4. Raw `INPUT_OBSERVATION` nodes

A raw node represents exactly one `input_observation_id`; it does not store a
redundant Artifact claim.

Raw-node construction requires:

- `node_kind = INPUT_OBSERVATION`;
- `entity_type = input_observation`;
- an existing `input_observation` identified by `entity_id`;
- no incoming derivation edges;
- `direct_available_at` equal to the observation's
  `effective_available_at`;
- `derived_available_at` equal to the same instant;
- `derivation_policy_version = MAX_REQUIRED_PARENTS_V1`.

The writer derives both timestamps. A caller cannot supply or override either
timestamp.

The raw node's Artifact ancestry is resolved authoritatively as:

```text
derivation_node.entity_id
  -> input_observation.input_observation_id
  -> input_observation.artifact_id
  -> artifact.artifact_id
```

No independent node-level Artifact identity exists to mismatch.

## 5. Derived nodes

Milestone 3B creates derived nodes only as `NORMALIZED_FACT` nodes representing
an existing immutable Artifact.

Under `MAX_REQUIRED_PARENTS_V1`, every non-input node requires:

- `direct_available_at IS NULL`;
- at least one incoming edge with `required = 1`;
- all parents to exist before construction;
- all parents to belong to the same attempt lineage;
- a complete parent edge set supplied in the atomic construction transaction.

The caller cannot supply `derived_available_at`. The writer computes it under
§7 before inserting the authoritative node.

## 6. `derivation_edge`

`derivation_edge` has exactly these fields:

| Field | Type and nullability | Contract |
|---|---|---|
| `derivation_edge_id` | non-null text | Primary identity; non-empty |
| `parent_node_id` | non-null text | FK to `derivation_node.derivation_node_id` |
| `child_node_id` | non-null text | FK to `derivation_node.derivation_node_id` |
| `edge_role` | non-null text | Non-empty, case-sensitive stable role without NUL |
| `required` | non-null integer | Exactly `0` or `1` |

`(parent_node_id, child_node_id, edge_role)` is unique.
`parent_node_id` and `child_node_id` MUST differ.

An edge is immutable. `UPDATE` and `DELETE` are forbidden. Adding an edge after
the child's atomic construction completes is also forbidden; reparenting
requires a new node.

### 6.1 Required and optional parents

`required = 1` means the parent influenced the child's content. It participates
in temporal propagation, required ancestry, cutoff validation, and
`run_input` coverage.

`required = 0` is provenance only. It MUST NOT influence content,
`derived_available_at`, required ancestry, or semantic result-Artifact
identity. It remains part of full provenance ancestry and committed audit
closure. Its inclusion may distinguish the result-manifest audit closure; the
result manifest is closure identity, not the derived content hash.

Every content-influencing parent MUST use a required edge.

### 6.2 Deterministic ordering

For recomputation and evidence traversal, parents are ordered by:

```text
(edge_role COLLATE BINARY, parent_node_id COLLATE BINARY)
```

No parent ordinal is stored.

## 7. Frozen temporal derivation policy

The sole supported temporal policy identity is:

```text
policy_kind    = DERIVATION
policy_version = MAX_REQUIRED_PARENTS_V1
```

`policy_kind` is fixed by the `derivation_node` domain and is not redundantly
stored. Every node persists `derivation_policy_version`.

For `MAX_REQUIRED_PARENTS_V1` only:

- no generic derivation-policy registry is required;
- no policy-definition Artifact is required;
- no mutable current/default policy exists;
- no policy selection exists.

This is a narrow exception to V1.1 §§2.3 and 8.2. It applies to no other
policy. A future temporal algorithm requires a new explicit policy version and
semantic amendment.

The canonical result-manifest policy reference for this policy contains
exactly:

```json
{"policy_kind":"DERIVATION","policy_version":"MAX_REQUIRED_PARENTS_V1"}
```

### 7.1 Raw-node rule

```text
derived_available_at =
    input_observation.effective_available_at
```

### 7.2 Derived-node rule

```text
derived_available_at = max(
    parent.derived_available_at
    for every parent edge where required = 1
)
```

Optional parents are excluded. A derived node without a required parent is
invalid. V1.0 §6.2 is narrowed for this policy: non-input nodes have no
intrinsic direct-availability term.

## 8. Authoritative proof and recursive verification

For temporal derivation, the proof is the immutable DAG, its authoritative
entities and parent relationships, the stored policy version, the authoritative
InputObservation for every raw node, and the stored temporal result.

No separate derivation-proof entity, proof Artifact, availability proof hash,
or proof-hash preimage exists. This does not alter unrelated proof mechanisms.

### 8.1 Deterministic validator

The validator accepts one or more root node IDs. It traverses parents in the
order fixed by §6.2, memoizes validated results, and maintains `UNVISITED`,
`VISITING`, and `VALIDATED` states.

For each node it MUST:

1. resolve the node or fail;
2. reject a node already marked `VISITING` as a cycle;
3. require policy version `MAX_REQUIRED_PARENTS_V1`;
4. validate the `node_kind`/`entity_type` pair and resolve the target entity;
5. load and structurally validate every incoming edge;
6. reject duplicate, self-referential, cross-attempt, or cross-run structure;
7. recursively validate every parent, including optional provenance parents;
8. compute the expected timestamp under §7;
9. compare it with stored `derived_available_at`;
10. mark the node `VALIDATED` and memoize its verified timestamp.

For a raw node the validator additionally requires zero parents and exact
equality among observation availability, `direct_available_at`, and
`derived_available_at`.

For a derived node the validator additionally requires
`direct_available_at IS NULL`, at least one required parent, and completely
resolvable required ancestry.

Validation fails closed if any node, entity, required parent, policy, edge,
timestamp, cycle check, or ancestry segment cannot be validated. A read returns
no partial verified result. A commit-time validation failure rolls back the
entire transaction.

The authorized construction transaction's complete required-parent set is the
authoritative assertion of content influence. The Ledger can detect structural
omission or tampering after that assertion; it cannot infer undisclosed caller
intent.

## 9. Attempt and run lineage

Node and edge construction is attempt-authoritative. The authorization check
and write occur in one transaction and require:

- request state `ACTIVE`;
- current control attempt equal to the supplied attempt;
- current fence token equal to the attempt token; and
- attempt status `RUNNING`.

A stale or forged attempt cannot create nodes or edges.

A raw node MAY consume an observation originally recorded by another attempt.
The raw node's `attempt_id` is the current consuming attempt. Every pair of
nodes connected by an edge MUST have the same `attempt_id`; cross-attempt edges
are forbidden.

Nodes begin attempt-local with `run_id = null`. During successful run commit,
the complete all-edge ancestor closure of the selected result nodes undergoes
the sole permitted null-to-value binding to that attempt's new `run_id`.

The binding requires:

- the run belongs to the node's attempt;
- every edge endpoint in the closure is bound to the same run;
- the closure is complete across required and optional edges;
- no unlisted attempt-local node is silently adopted;
- no node is rebound or unbound.

## 10. Atomic construction, acyclicity, and immutability

A raw node is created in one transaction containing authority validation,
observation resolution, timestamp computation, and node insertion.

A derived node and its complete edge set are created in one transaction
containing authority validation, target validation, parent validation, cycle
validation, timestamp computation, and all node/edge insertions.

All parents MUST already exist. Before insertion, recursive graph validation
rejects a proposed parent that is the child or a descendant of the child.
Self-edges, direct cycles, and indirect cycles abort the transaction.

A conforming database implementation MAY insert edges before the new child
using a deferred child FK, then insert and validate the child before commit.
The parent FK remains resolvable at insertion. This construction ensures that:

- an edge can target only a child currently under construction;
- an edge cannot be appended to an existing child;
- an orphan edge cannot commit; and
- any failure leaves neither a partial node nor partial edges.

Database constraints/triggers and writer validation enforce the invariants.
Dropping or bypassing those controls is corruption, not an alternate write
path.

## 11. Ancestry traversal

Two ancestry views are normative:

- **required ancestry** traverses only `required = 1` edges and represents
  content and temporal influence;
- **full provenance ancestry** traverses every edge and includes optional
  provenance.

Both return unique ancestors excluding the queried root in deterministic
parent-before-child topological order. When more than one node is eligible at
the same topological position, `derivation_node_id COLLATE BINARY` breaks the
tie.

Raw observations and Artifacts are obtained by traversing required or full
ancestry to `INPUT_OBSERVATION` nodes and then following the mapping in §4.

## 12. `run_input`, cutoff, and result-manifest closure

The existing `run_input` schema remains unchanged.

At successful run commit:

- every raw `INPUT_OBSERVATION` node in required ancestry MUST have at least
  one matching `run_input` row;
- each `run_input.input_observation_id` MUST equal the `entity_id` of its
  referenced raw node;
- the referenced raw node MUST be bound to the same run;
- optional-only observations MUST NOT appear as used inputs;
- an observation outside required ancestry MUST NOT appear in `run_input`;
- `input_role` remains a non-empty, policy-owned stable role;
- `run_input` rows remain immutable.

Before commitment, the writer recursively verifies the complete graph and
requires every node in required ancestry to have:

```text
derived_available_at <= run_request.analysis_cutoff_at
```

The result manifest contains:

- `derivation_node_ids`: the complete all-edge ancestor-closed node set,
  sorted lexicographically;
- `input_observation_ids`: distinct observations represented by `run_input`,
  sorted lexicographically;
- the exact derivation policy reference from §7.

Graph verification, cutoff validation, run insertion, one-time node binding,
manifest creation, `run_input` insertion, attempt success, and request-control
commit occur in the existing atomic successful-run transaction. Any failure
rolls back all of them.

## 13. PIT and legacy deferral to Milestone 3C

Milestone 3B MUST NOT:

- persist node PIT or legacy classification;
- infer `NATIVE_PIT` or any other class;
- persist intrinsic weakening fields;
- implement legacy classification writers; or
- claim that temporal validity establishes PIT safety.

V1.1 §9.1 node-classification persistence and propagation are deferred to a
separate Milestone 3C amendment. A V1.2.0 temporal node proves lineage and
availability only.

Until 3C is frozen and implemented, these nodes cannot support a PIT-safe
claim or enter a PIT-labelled cohort. Research, LLM, Setup, eligibility,
lifecycle, and outcome integration remains blocked. Milestone 3C MUST define
an append-only-compatible classification mechanism for existing temporal nodes
before those integrations proceed.

This deferral does not weaken temporal cutoff enforcement.

## 14. Frozen conformance matrix

| Area | Required cases |
|---|---|
| Raw node | Valid observation mapping; copied availability; no parents; Artifact traversal; no redundant Artifact field; unsupported policy rejected |
| Derived node | One required parent; multiple required parents; exact maximum; no required parent rejected |
| Temporal propagation | `T1 < T2 < T3`; ten-level chain; late ancestor propagation; no backdating |
| Optional edges | Included in full provenance and audit closure; excluded from availability and required-input closure |
| Entity integrity | Missing observation; missing Artifact; invalid node-kind/entity-type pair; reserved future target rejected |
| DAG integrity | Self-edge; edge to completed child; attempted direct and indirect reparenting; cycle detection during verification |
| Atomicity | Invalid parent, target, policy, or timestamp leaves no node or edge |
| Verification | Valid recomputation; stored timestamp mismatch; missing parent; unsupported policy; incomplete ancestry; cycle |
| Immutability | Node update/delete rejected; edge insert-after-construction/update/delete rejected |
| Authority | Current attempt accepted; stale attempt rejected; forged fence rejected; historical graph survives supersession |
| Lineage | Cross-attempt edge rejected; foreign-run binding rejected; complete closure bound exactly once |
| `run_input` | Required raw ancestors covered; optional-only observations excluded; mismatched raw node rejected; unused observation rejected |
| Regression | Fresh migration path; level upgrade; rerun; migration hashes; FK check; prior Ledger and TRINITY suites |

## 15. Explicit supersession and compatibility

This amendment makes only these narrow changes:

1. For `MAX_REQUIRED_PARENTS_V1`, it replaces V1.1's generic
   policy-definition Artifact requirement with the fixed identity and normative
   algorithm in §7.
2. It clarifies that the temporal derivation proof is the authoritative graph,
   entities, policy version, and recomputation; it adds no proof entity,
   Artifact, or hash.
3. It narrows V1.0 §6.2 for this policy so non-input nodes have no intrinsic
   direct-availability term.
4. It defers V1.1 §9.1 PIT persistence and propagation to Milestone 3C.
5. It confirms that raw Artifact identity is traversed through
   `input_observation` rather than duplicated on the node.

Migrations `0001_ledger_core`, `0002_execution_coordination`, and
`0003_input_observation` predate this amendment and remain byte-for-byte
unchanged. Existing `InputObservation`, attempt/fencing, run, manifest, and
`run_input` semantics remain authoritative except for the additive derivation
closure defined here.

## 16. Freeze boundary

This freeze fixes the two derivation schemas, supported mappings, policy
identity and algorithm, parent semantics and order, temporal recomputation,
atomic construction, acyclicity, immutability, attempt/run lineage, ancestry,
`run_input` integration, result-manifest graph closure, and the Milestone 3C
PIT boundary.

Changing any field, constraint, mapping, temporal formula, policy identity,
lineage rule, ancestry rule, commit requirement, or deferral boundary requires
a new semantic specification version.
