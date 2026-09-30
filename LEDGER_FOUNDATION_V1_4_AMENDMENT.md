# Ledger Foundation V1.4.0 Amendment

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.4.0`

Scope: `RESEARCH_LLM_SETUP_INTEGRATION_CLOSURE`

## 1. Version and purpose

This additive semantic amendment activates the previously reserved `RESEARCH`
and `SETUP` derivation mappings, operationalizes Research, Analyst/Critic LLM,
and Setup persistence, freezes the Artifact contracts required by that
integration, extends derived classification to Research and Setup, introduces
`REQUIRED_ANCESTRY_PIT_V2`, and introduces
`ledger.run-result-manifest.v3`.

This amendment is limited to the existing TRINITY USA V2 Research method,
Codex CLI Analyst/Critic interaction path, USA Setup V1 policy, and one
deterministic JNJ vertical slice. It does not add execution eligibility, trade
execution, lifecycle, outcomes, learning, scheduling, or new analytical rules.

## 2. RESEARCH derivation activation

The operational mapping is exactly:

```text
node_kind   = RESEARCH
entity_type = research_record
entity_id   = research_record.research_id
```

`direct_available_at` is null. The temporal policy is exactly
`DERIVATION / MAX_REQUIRED_PARENTS_V1`. A RESEARCH node has at least one
REQUIRED parent and:

```text
derived_available_at = max(REQUIRED parent derived_available_at)
```

Optional parents are full provenance only. The node and target Research record
belong to the same consuming attempt. The target exists in the same atomic
construction transaction. Exactly one RESEARCH node represents a Research
record. Entity and node begin with `run_id = NULL`, bind exactly once and
atomically to the same successful same-attempt run, and are otherwise
immutable.

## 3. SETUP derivation activation

The operational mapping is exactly:

```text
node_kind   = SETUP
entity_type = setup
entity_id   = setup.setup_id
```

The temporal, ownership, atomicity, run-binding, and immutability rules are the
same as for RESEARCH. For `SETUP / USA_SETUP_V1`, REQUIRED parents are exactly:

1. the RESEARCH node selected by the single PRIMARY
   `setup_research_lineage` row; and
2. the NORMALIZED_FACT node for the exact OHLCV input independently consumed
   by Setup V1.

Implicit latest-Research selection is forbidden.

## 4. RESEARCH and SETUP classification

For both node kinds:

```text
intrinsic_record_class = LEDGER_NATIVE
classification_basis   = REQUIRED_PARENT_PROPAGATION
evidence_artifact_id   = NULL
```

`resolved_record_class` is `LEGACY_NON_LEDGER_ARTIFACT` if any REQUIRED parent
resolves legacy and is otherwise `LEDGER_NATIVE`. `pit_class` is the exact
REQUIRED-parent fold under the selected PIT-classification policy. Optional
parents do not participate. Research and Setup have no intrinsic PIT source
and may neither weaken nor strengthen PIT independently.

## 5. PIT classification policy V2

The policy identity is exactly:

```text
policy_kind    = PIT_CLASSIFICATION
policy_version = REQUIRED_ANCESTRY_PIT_V2
```

The definition Artifact envelope is:

```text
artifact_kind            = ledger.pit-classification-policy-definition.v1
schema_name              = ledger.pit-classification-policy-definition
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

Every property in the following canonical object is required. Extra
properties are forbidden. Arrays use the displayed order. Propagation pairs
are normalized so `left <= right` and ordered by `(left, right)`.

```json
{
  "schema_name": "ledger.pit-classification-policy-definition",
  "schema_version": "1",
  "policy_kind": "PIT_CLASSIFICATION",
  "policy_version": "REQUIRED_ANCESTRY_PIT_V2",
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
  "not_applicable_artifact_kinds": ["ledger.llm-raw-response.v1", "ledger.pit-classification-policy-definition.v1"],
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

There is no `definition_sha256`. V1 remains immutable and valid for historical
runs. V2 preserves V1 and extends only the NOT_APPLICABLE allowlist with
`ledger.llm-raw-response.v1` under the existing
`PIT_IRRELEVANT_CONTENT` evidence/verifier mechanism.

## 6. Research method

### 6.1 `research_method`

| Column | Storage contract |
|---|---|
| `research_method_id` | TEXT primary key; canonical lowercase UUIDv4 |
| `research_kind` | TEXT non-null; exactly `TRINITY_USA_RESEARCH` |
| `method_version` | TEXT non-null; exactly `USA_V2` |
| `definition_artifact_id` | TEXT non-null FK to Artifact |
| `code_commit` | TEXT non-null; 40 lowercase hexadecimal characters |
| `created_at` | TEXT non-null; normalized Ledger UTC timestamp |

`(research_kind, method_version)` is unique. Rows are immutable and have no
mutable current-method pointer.

### 6.2 USA V2 method-definition Artifact

```text
artifact_kind            = ledger.usa-v2-research-method-definition.v1
schema_name              = ledger.usa-v2-research-method-definition
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The payload is the following closed singleton. Every property is required;
extra properties are forbidden; arrays use the displayed order and prohibit
duplicates.

```json
{
  "schema_name": "ledger.usa-v2-research-method-definition",
  "schema_version": "1",
  "research_kind": "TRINITY_USA_RESEARCH",
  "method_version": "USA_V2",
  "facts_builder": "trinity.italia_v1.build_facts",
  "provider_class": "trinity.usa_v2.USAProvider",
  "finalizer": "trinity.italia_v1.analyze_company",
  "stage_order": ["ANALYST", "CRITIC", "FINALIZE"],
  "statuses": ["PASS", "WATCH", "INVESTIGATE"],
  "decision_levels": ["HIGH", "LOW", "MEDIUM"],
  "event_classes": ["NEW_INFORMATION", "EXPECTATION_CHANGE", "CONFIRMATION", "REITERATION", "ALREADY_KNOWN", "LOW_RELEVANCE"],
  "analysis_fields": ["fundamental_analysis", "earnings_and_news_analysis", "price_context", "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation"],
  "claim_materialities": ["MATERIAL", "SUPPORTING"],
  "analyst_response_contract": "USA_V2_ANALYST_RESPONSE_V1",
  "critic_response_contract": "USA_V2_CRITIC_RESPONSE_V1",
  "event_validation": "USA_V2_COMPLETE_UNIQUE_EVENT_ASSESSMENT_V1",
  "claim_validation": "USA_V2_CLAIM_FACT_EVIDENCE_VALIDATION_V1",
  "evidence_confidence_bounding": "USA_V2_EVIDENCE_CONFIDENCE_CEILING_V1",
  "decision_calibration": "USA_V2_EVENT_CLASS_AND_THESIS_STRENGTH_V1",
  "citation_cleaning": "USA_V2_KNOWN_EVIDENCE_IDENTIFIER_ONLY_V1",
  "memory_policy": "CURRENT_INVOCATION_EXPLICIT_CONTEXT_ONLY",
  "outcome_history_allowed": false,
  "implicit_latest_input_allowed": false
}
```

## 7. Research record

| Column | Storage contract |
|---|---|
| `research_id` | TEXT primary key; canonical lowercase UUIDv4 |
| `attempt_id` | TEXT non-null FK to attempt |
| `run_id` | TEXT nullable FK to run; authorized one-time binding only |
| `research_kind` | TEXT non-null; `TRINITY_USA_RESEARCH` |
| `subject_key` | TEXT non-null stable identity; JNJ uses `ticker:JNJ` |
| `content_artifact_id` | TEXT non-null FK to the Research-result Artifact |
| `research_method_id` | TEXT non-null FK to research_method |
| `method_version` | TEXT non-null; must match the method row |
| `as_of_at` | TEXT non-null normalized UTC reference instant |
| `derivation_node_id` | TEXT non-null unique, deferred FK to derivation_node |
| `supersedes_research_id` | TEXT nullable self-FK |
| `created_at` | TEXT non-null normalized Ledger UTC timestamp |

For `TRINITY_USA_RESEARCH / USA_V2`, `supersedes_research_id IS NULL`.
Research supersession and a current/latest Research chain are deferred.
`research_id` and `research_record.created_at` are authoritative. All fields
except authorized `run_id: NULL -> same-attempt committed run` are immutable.

## 8. Research content Artifact

```text
artifact_kind            = ledger.usa-v2-research-result.v1
schema_name              = ledger.usa-v2-research-result
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The closed payload contains exactly these required properties:

| Property | Contract |
|---|---|
| `company_name` | non-empty string |
| `ticker` | non-empty string |
| `isin` | non-empty string or null |
| `as_of` | ISO date `YYYY-MM-DD` |
| `facts` | closed USA V2 fact block below |
| `fundamental_analysis` | non-empty string |
| `earnings_and_news_analysis` | non-empty string |
| `price_context` | non-empty string |
| `bull_case` | non-empty string |
| `bear_case` | non-empty string |
| `catalysts` | ordered array of non-empty strings |
| `risks` | ordered array of non-empty strings |
| `thesis_invalidation` | non-empty string |
| `status` | `PASS`, `WATCH`, or `INVESTIGATE` |
| `confidence` | `LOW`, `MEDIUM`, or `HIGH` |
| `evidence` | exact ordered copy of `facts.evidence` |
| `critic_notes` | ordered array of non-empty strings |
| `model_version` | non-empty string |
| `prompt_version` | exactly `TRINITY_ITALIA_V1_CONTRACT` |
| `claim_refs` | ordered array of validated claim objects |
| `rejected_claim_refs` | ordered array of rejected claim objects |
| `evidence_confidence` | `LOW`, `MEDIUM`, or `HIGH` |
| `thesis_strength` | `LOW`, `MEDIUM`, or `HIGH` |
| `analyst_status` | `PASS`, `WATCH`, or `INVESTIGATE` |
| `analyst_thesis_strength` | `LOW`, `MEDIUM`, or `HIGH` |
| `analyst_evidence_confidence` | `LOW`, `MEDIUM`, or `HIGH` |
| `critic_status` | `PASS`, `WATCH`, or `INVESTIGATE` |
| `critic_thesis_strength` | `LOW`, `MEDIUM`, or `HIGH` |
| `critic_evidence_confidence` | `LOW`, `MEDIUM`, or `HIGH` |
| `event_assessments` | ordered complete event-assessment array |

`thesis_id` and `created_at` are forbidden. Consequently, engine UUID and
persistence time cannot change semantic content identity.

A claim object has exactly `claim_id`, `field`, `text`, `fact_ids`,
`materiality`, `period`, and `validation_errors`. Text fields are non-empty;
`field` is one of the eight method analysis fields; `materiality` is
`MATERIAL` or `SUPPORTING`; `period` is non-empty string or null;
`validation_errors` is an ASCII-sorted unique string array. Valid claims have
an empty error array and unique claim ID. Rejected claims have a non-empty
error array. Claim and fact-ID array order is preserved; duplicate fact IDs
remain permitted because the current validator permits them.

An event assessment has exactly `event_id`, `classification`, `material`, and
`rationale`. Classification is one of the six method event classes. Each
facts event occurs exactly once, event IDs are unique, and order follows
`facts.events`.

The USA V2 fact block has exactly `identity`, `price`, `fundamentals`,
`financial_facts`, `events`, `evidence`, and `missing_fields`.

`identity` has exactly `company_name`, `ticker`, `provider_symbol`, `isin`,
`as_of`, and `schema_type`. Nullable fields are `provider_symbol`, `isin`, and
`schema_type`; the others are non-empty strings and `as_of` is an ISO date.

`price` has exactly `current_price`, `return_5d`, `return_20d`, `return_60d`,
`trend`, `indicators`, `published_at`, and `acquisition`. Numeric values and
trend/publication date are nullable. For USA V2 `indicators` is exactly `{}`.
`acquisition` has exactly `currency`, `market`, `provider_symbol`, `last_bar`,
`reference_bars`, `raw_path`, `sha256`, and `fact_metadata`. `last_bar` has
exactly `date`, `open`, `high`, `low`, `close`, `adjusted_close`, and `volume`.
`reference_bars` has exactly keys `5`, `20`, and `60`, each containing exactly
`date` and `close`. `sha256` is 64 lowercase hexadecimal characters.
Each ordered `fact_metadata` item has exactly `fact_id`, `ticker`, `metric`,
`value_path`, `unit`, `scale`, `period`, `evidence_id`, `extraction_method`,
and nonnegative integer `display_precision`.

`fundamentals` has exactly `revenue`, `revenue_growth`, `operating_margin`,
`net_income`, `free_cash_flow`, `net_debt`, `valuation_metrics`, and
`published_at`. Numeric values and publication date are nullable;
`valuation_metrics` is exactly `{}`. For current USA V2 `financial_facts` is
exactly `{}`.

Each event has exactly `kind`, `published_at`, `summary`, `facts`, and
`guidance`. Event `facts` is the closed normalized object with exactly
`evidence_identifier`, `novelty`, `issuer_release`, `primary_source`,
`reported_metrics`, `guidance_ranges`, `guidance_language`,
`guidance_changes`, and `structured_sec_facts`. Absent engine branches are
explicit nulls or empty arrays. Each structured fact uses the exact current
USA V2 normalized fact keys; no key outside the USA V2 fact catalog is
permitted. Fact IDs are unique and arrays retain engine order.

Each evidence object has exactly `source`, `published_at`, `identifier`,
`url`, `excerpt`, `title`, `retrieved_at`, `content_sha256`, `raw_path`,
`published_at_precision`, `document_kind`, `filing_date`, `accepted_at`,
`form`, and `accession_number`. Optional properties appear as explicit null.
At least one of `identifier` and `url` is non-null. Non-null identifiers are
unique. Evidence order is engine order. `missing_fields` is an ordered unique
string array.

Every object above forbids extra properties. JSON numbers are finite, are not
negative zero, and use JCS shortest round-trippable encoding. NaN and
infinities are forbidden.

## 9. LLM interaction entities

### 9.1 `llm_interaction`

| Column | Storage contract |
|---|---|
| `llm_interaction_id` | TEXT primary key; canonical lowercase UUIDv4 |
| `attempt_id` | TEXT non-null FK to attempt |
| `ordinal` | INTEGER positive; unique per attempt |
| `provider` | TEXT non-null; `OPENAI_CODEX_CLI` |
| `model` | TEXT non-null; `gpt-5.6-sol` |
| `model_version` | TEXT non-null; `codex-cli:gpt-5.6-sol` |
| `request_artifact_id` | TEXT non-null FK to invocation-request Artifact |
| `response_artifact_id` | TEXT nullable FK to invocation-success Artifact |
| `error_artifact_id` | TEXT nullable FK to error Artifact |
| `status` | TEXT; `SUCCEEDED` or `FAILED` |
| `started_at` | TEXT non-null normalized UTC |
| `finished_at` | TEXT non-null normalized UTC, not before start |
| `input_tokens` | INTEGER nullable and nonnegative |
| `output_tokens` | INTEGER nullable and nonnegative |
| `created_at` | TEXT non-null normalized UTC |

Success requires response and forbids error. Failure requires error and
forbids response. Rows are immutable.

### 9.2 `research_llm_interaction`

Columns are non-null `research_id`, `llm_interaction_id`,
`interaction_role`, and positive `ordinal`. The primary key is
`(research_id, llm_interaction_id, interaction_role)`; the table additionally
has unique `(research_id, interaction_role, ordinal)` and
`(research_id, llm_interaction_id)`. Roles are `PRIMARY`, `SUPPORTING`, and
`CRITIQUE`. The pilot uses Analyst `PRIMARY/1` and Critic `CRITIQUE/1`.
Research and interaction share attempt identity. Failed interactions cannot
be linked. Rows are immutable.

## 10. Client effective input

`ledger.llm-effective-prompt.v1` means exactly
`CLIENT_EFFECTIVE_INPUT_BYTES`: the exact UTF-8 bytes TRINITY supplied to
Codex CLI stdin, including whitespace and serialization. Its
`canonicalization_version` is `IDENTITY` and media type is
`text/plain;charset=utf-8`.

It does not claim to contain hidden provider system prompts, undocumented
provider instructions, server-side transformations, or internal routing and
preprocessing. Provider/model provenance is recorded separately.

## 11. LLM invocation request

Envelope:

```text
artifact_kind            = ledger.llm-invocation-request.v1
schema_name              = ledger.llm-invocation-request
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The closed payload has exactly these required non-null properties:

```text
schema_name = ledger.llm-invocation-request
schema_version = "1"
interaction_role = ANALYST | CRITIC
provider = OPENAI_CODEX_CLI
model = gpt-5.6-sol
model_version = codex-cli:gpt-5.6-sol
client_effective_input_artifact_id
context_artifact_id
response_schema_artifact_id
invocation_parameters_artifact_id
code_manifest_artifact_id
config_artifact_id
```

All Artifact IDs match `^sha256:[0-9a-f]{64}$`. Extra properties are
forbidden. Context is an ordered closed Artifact reference list matching the
actual prompt order. Invocation parameters record only applicable settings;
unsupported or omitted provider settings are explicit nulls, not fabricated
values. No hidden decision-relevant input is permitted.

## 12. LLM success and error

### 12.1 Success

```text
artifact_kind            = ledger.llm-invocation-success.v1
schema_name              = ledger.llm-invocation-success
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The closed payload has exactly required `schema_name`, `schema_version`,
canonical lowercase UUIDv4 `llm_interaction_id`, and Artifact IDs
`raw_response_artifact_id`, `parsed_response_artifact_id`, and
`effective_stage_result_artifact_id`. The three IDs respectively target
`ledger.llm-raw-response.v1`, `ledger.llm-parsed-response.v1`, and
`ledger.usa-v2-stage-result.v1`. `llm_interaction.response_artifact_id`
references only this success Artifact.

### 12.2 Error

```text
artifact_kind            = ledger.llm-error.v1
schema_name              = ledger.llm-error
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The closed payload has exactly required `schema_name`, `schema_version`,
canonical lowercase UUIDv4 `llm_interaction_id`, `failure_phase`,
`error_code`, nullable `exit_code`, nullable `raw_response_artifact_id`, and
lexicographically sorted unique `diagnostic_artifact_ids`.

`failure_phase` is one of `PROVIDER_EXECUTION`, `RAW_RESPONSE_READ`,
`JSON_PARSE`, `SCHEMA_VALIDATION`, or `DETERMINISTIC_POST_VALIDATION`.
`error_code` is one of `PROVIDER_NONZERO_EXIT`, `PROVIDER_TIMEOUT`,
`RAW_RESPONSE_MISSING`, `RAW_RESPONSE_NOT_UTF8`, `MALFORMED_JSON`,
`SCHEMA_INVALID`, or `POST_VALIDATION_FAILED`. Diagnostics are sanitized
immutable Artifacts and contain no credentials.

## 13. Raw, parsed, and effective chain

The authoritative chain is:

```text
CLIENT_EFFECTIVE_INPUT_BYTES -> provider invocation

provider result bytes
  -> ledger.llm-raw-response.v1
  -> InputObservation
  -> INPUT_OBSERVATION raw node

raw response
  -> UTF-8 decode / JSON parse / exact role schema validation
  -> ledger.llm-parsed-response.v1

parsed response
  -> deterministic current USA V2 role validation/transformation
  -> ledger.usa-v2-stage-result.v1
  -> NORMALIZED_FACT node
```

The parsed-response closed payload has exactly `schema_name`,
`schema_version`, `interaction_role`, `raw_response_artifact_id`,
`response_schema_artifact_id`, and `parsed_value`. Role is `ANALYST` or
`CRITIC`; parsed value exactly satisfies the referenced role schema.

The raw response observation uses `RETRIEVED_AT_FALLBACK`, with
`retrieved_at = llm_interaction.finished_at` and
`effective_available_at = retrieved_at`. Its raw node and every
decision-relevant context node are REQUIRED parents of the effective stage;
the Critic additionally requires the exact Analyst effective-stage node.

Malformed JSON, schema failure, or deterministic post-validation failure
creates a FAILED interaction and no success or effective-stage Artifact.
Failed interactions cannot finalize Research. Latest/timestamp lookup is
forbidden.

## 14. LLM temporal and PIT rules

If source evidence is available at T1, Analyst completes at T2, and Critic
completes at T3, Research availability is at least T3. Source publication
cannot backdate generated LLM information.

Under `REQUIRED_ANCESTRY_PIT_V2`, a `ledger.llm-raw-response.v1` raw node may
be classified NOT_APPLICABLE using the exact frozen
`PIT_IRRELEVANT_CONTENT` evidence mechanism. Creation time affects temporal
availability, not the PIT quality of its historical factual parents. Thus
`A x N = A`, `R x N = R`, and `U x N = U` under the frozen algebra.

## 15. Analyst and Critic linkage

Analyst facts/context plus its exact raw response produce its parsed response
and effective stage. Critic explicitly consumes the exact Analyst interaction,
exact Analyst effective-stage Artifact/node, and exact factual context. Latest
selection, timestamp selection, and implicit shared caches are forbidden.

## 16. USA V2 stage-result Artifact

```text
artifact_kind            = ledger.usa-v2-stage-result.v1
schema_name              = ledger.usa-v2-stage-result
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The closed payload has exactly required `schema_name`, `schema_version`,
canonical UUIDv4 `llm_interaction_id`, `interaction_role`,
`parsed_response_artifact_id`, and `result`.

For ANALYST, result has exactly the eight method analysis fields plus
`proposed_status`, `claim_refs`, `evidence_confidence`, `thesis_strength`, and
`event_assessments`. For CRITIC, result has exactly `notes`, `status`,
`evidence_confidence`, `thesis_strength`, `event_assessments`,
`revised_analysis`, `critic_status`, `critic_thesis_strength`, and
`critic_evidence_confidence`. `revised_analysis` has exactly the eight method
analysis fields and `claim_refs`. Status, level, claim, and event contracts
are those in Sections 6 and 8. Extra properties are forbidden.

## 17. Setup policy

### 17.1 `setup_policy`

The table contains exactly `setup_policy_id`, `policy_kind`,
`policy_version`, `definition_artifact_id`, `code_commit`, and `created_at`.
The ID is canonical lowercase UUIDv4. Kind is `SETUP`; version is
`USA_SETUP_V1`; definition is an Artifact FK; code commit is 40 lowercase
hexadecimal characters; creation time is normalized UTC. Kind/version is
unique. Rows are immutable. There is no `definition_sha256` or replacement
digest.

### 17.2 Definition Artifact

```text
artifact_kind            = ledger.usa-setup-v1-policy-definition.v1
schema_name              = ledger.usa-setup-v1-policy-definition
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The payload is the following closed singleton. Every property is required;
extra properties and duplicate array elements are forbidden; array order is
displayed order.

```json
{
  "schema_name": "ledger.usa-setup-v1-policy-definition",
  "schema_version": "1",
  "policy_kind": "SETUP",
  "policy_version": "USA_SETUP_V1",
  "direction": "LONG",
  "price_adjustment": "RAW",
  "minimum_bars": 201,
  "sma_windows": [20, 50, 200],
  "atr_window": 14,
  "rsi_window": 14,
  "average_volume_window": 20,
  "return_windows": [5, 20, 60],
  "range_windows": [20, 60],
  "regime_rules": {
    "uptrend": "close > sma50 > sma200 AND sma20 >= sma50",
    "downtrend": "close < sma50 < sma200 AND sma20 <= sma50",
    "otherwise": "NEUTRAL"
  },
  "preconditions": {
    "pass_result": "NO_SETUP",
    "downtrend": "NO_SETUP",
    "non_uptrend": "NO_SETUP"
  },
  "breakout": {
    "near_breakout": "close >= 0.99 * prior_high_20d",
    "volume_confirmed": "relative_volume >= 1.2",
    "entry": "prior_high_20d + 0.10 * atr14",
    "stop": "entry - 2.0 * atr14",
    "tp1": "entry + 2.0 * risk_per_share",
    "tp2": "entry + 3.0 * risk_per_share"
  },
  "pullback": {
    "support_candidates": ["sma20", "sma50"],
    "support_band": "0.99 * support <= close <= 1.02 * support",
    "support_selection": "minimum absolute distance from close",
    "entry": "support + 0.25 * atr14",
    "stop": "min(low_20d, entry - 1.5 * atr14)",
    "tp1": "high_20d",
    "minimum_rr_tp1": 1.5,
    "tp2_initial": "max(high_60d, entry + 2.0 * risk_per_share)",
    "tp2_fallback": "entry + 3.0 * risk_per_share when tp2_initial <= tp1"
  },
  "rounding_decimal_places": 4,
  "setup_types": ["BREAKOUT", "NO_SETUP", "PULLBACK"],
  "research_statuses": ["INVESTIGATE", "PASS", "WATCH"],
  "decision_levels": ["HIGH", "LOW", "MEDIUM"],
  "technical_regimes": ["DOWNTREND", "NEUTRAL", "UPTREND"],
  "implicit_latest_research_allowed": false
}
```

## 18. Setup record and result

### 18.1 `setup`

The exact columns are `setup_id`, `attempt_id`, nullable `run_id`,
`instrument_id`, `direction`, `setup_kind`, `signal_at`, `entry_rule_json`,
`stop_rule_json`, `target_rule_json`, nullable `valid_from`, `expires_at`, and
`invalidates_at`, `setup_policy_id`, `setup_policy_version`,
`result_artifact_id`, unique deferred `derivation_node_id`, `currency`,
`venue_id`, `calendar_id`, `price_adjustment`, and `created_at`.

IDs are canonical lowercase UUIDv4 where applicable; timestamps are normalized
UTC; direction is LONG; setup kind is BREAKOUT, PULLBACK, or NO_SETUP;
currency is USD; JNJ venue is XNYS; calendar is
`XNYS_PROVIDED_SESSIONS_V1`; price adjustment is RAW. For USA_SETUP_V1,
`valid_from`, `expires_at`, and `invalidates_at` are null. Existence does not
grant execution eligibility. All fields except authorized one-time same-attempt
run binding are immutable.

### 18.2 Result Artifact

```text
artifact_kind            = ledger.usa-setup-v1-result.v1
schema_name              = ledger.usa-setup-v1-result
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The closed payload has exactly required `ticker`, `as_of`, `research_status`,
`evidence_confidence`, `thesis_strength`, `technical_regime`, `setup_type`,
nullable `entry_condition`, `entry_level`, `stop_level`,
`stop_distance_pct`, `tp1`, `tp2`, `risk_per_share`, `reward_tp1`,
`reward_tp2`, `rr_tp1`, and `rr_tp2`, and non-null numeric `close`, `atr14`,
`rsi14`, `sma20`, `sma50`, `sma200`, `distance_sma20_pct`,
`distance_sma50_pct`, `distance_sma200_pct`, `return_5d`, `return_20d`,
`return_60d`, `high_20d`, `low_20d`, `average_volume_20d`, and
`relative_volume`, plus ordered unique `reason_codes` and non-empty
`invalidation`.

All numbers are finite, not negative zero, and have at most four decimal
places. NO_SETUP has null operational levels and non-empty reasons.
Operational results require all levels, risk, rewards, and ratios; require
`stop < entry < tp1 < tp2`; and require `rr_tp1 >= 1.5`. Extra properties are
forbidden.

## 19. Setup/Research lineage

`setup_research_lineage` has non-null `setup_id`, `research_id`,
`lineage_role`, and `research_derivation_node_id`, with primary key
`(setup_id, research_id, lineage_role)`. For USA_SETUP_V1 there is exactly one
row and its role is PRIMARY. It identifies the exact Research and its exact
RESEARCH node, shares the consuming attempt, and is immutable. Latest
selection is forbidden.

## 20. Required lineage

Research REQUIRED ancestry includes every decision-relevant normalized input:
facts consumed by Analyst/Critic, exact Analyst effective result, exact Critic
effective result, and factual normalized input directly consumed during
deterministic finalization. Setup REQUIRED ancestry includes the exact
finalized RESEARCH node and exact independently consumed normalized OHLCV
node. Optional provenance is diagnostic only.

## 21. Entity/node atomicity and run binding

Research plus its RESEARCH node and Setup plus its SETUP node are each created
in one authorized transaction using deferred foreign-key validation. Entity,
complete incoming edges, and node must all validate before commit. Failure
leaves no orphan. Exactly one node represents each entity.

Entities and nodes begin with `run_id = NULL`. Successful classified commit
binds only adopted same-attempt entities/nodes exactly once to the same run.
Unrelated entities, second binding, and foreign binding are forbidden.
Classifications exist before commit.

## 22. Manifest V3

```text
artifact_kind            = ledger.run-result-manifest.v3
manifest_version         = "3"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

Manifest V3 has exactly the frozen V2 field set: `manifest_version`, `run_id`,
`run_request_id`, `attempt_id`, `analysis_cutoff_at`, `request_kind`,
`baseline_commit`, `attempt_code_commit`, `environment_fingerprint`,
`policy_references`, `input_observation_ids`, `derivation_node_ids`,
`output_artifact_ids`, `research_ids`, `setup_ids`, `eligibility_ids`,
`trade_ids`, `outcome_ids`, `pit_reference_at`,
`derivation_node_classification_ids`, `resolved_record_class`, and
`resolved_pit_class`. Extra properties are forbidden.

Policy references, sorted by `(policy_kind, policy_version)`, are exactly:

```json
[
  {"policy_kind":"DERIVATION","policy_version":"MAX_REQUIRED_PARENTS_V1"},
  {"policy_kind":"PIT_CLASSIFICATION","policy_version":"REQUIRED_ANCESTRY_PIT_V2"}
]
```

ID arrays are lexicographically sorted and duplicate-free. `research_ids` and
`setup_ids` are the exact adopted and run-bound entity sets. For V1.4,
`eligibility_ids`, `trade_ids`, and `outcome_ids` are empty. Research/Setup IDs
must agree exactly with adopted derivation-node targets. Missing, extra,
unrelated, or mismatched IDs reject commit. Manifest V1 and V2, including the
V2 V1-policy reference, remain unchanged.

## 23. JNJ vertical slice

The explicit expected-output reference is
`data/trinity_usa_v2/9d870f12247e425f9dc234baf78c4454.json`; it is never
selected through latest lookup. Existing pre-Ledger material is expected-output
reference only and is not retroactively complete LLM provenance.

An honest historical pilot based on pre-Ledger local inputs without archival
PIT proof uses `request_kind = RECONSTRUCTION`, resolves PIT as
`RECONSTRUCTED_NOT_ARCHIVED`, and resolves record origin as
`LEGACY_NON_LEDGER_ARTIFACT`. Newly captured LLM responses are Ledger-native
and PIT NOT_APPLICABLE; they do not erase legacy/reconstructed ancestry.

## 24. Migration boundary

This freeze defines but does not create migration 0006. That migration will
create only `research_method`, `llm_interaction`, `research_record`,
`research_llm_interaction`, `setup_policy`, `setup`, and
`setup_research_lineage`, with required indexes and triggers. It may replace
target/manifest validation triggers only to activate RESEARCH/SETUP and accept
Manifest V3 while preserving V1/V2. Migrations 0001-0005 remain byte-identical.
No synthetic history is backfilled.

## 25. Explicit activation, extension, narrowing, and preservation

V1.4 activates V1.2's reserved RESEARCH and SETUP mappings and V1.0/V1.1
Research/LLM/Setup intentions; extends derived classification from
NORMALIZED_FACT to RESEARCH and SETUP; and adds
`REQUIRED_ANCESTRY_PIT_V2` and Manifest V3.

It narrows/clarifies that Setup policy has no `definition_sha256`, USA_V2
Research supersession is disabled, engine thesis ID is not Ledger identity,
Research semantic content excludes `thesis_id` and `created_at`, and
CLIENT_EFFECTIVE_INPUT_BYTES means only bytes sent by TRINITY.

It preserves `MAX_REQUIRED_PARENTS_V1`, the V1.3 PIT algebra, optional-parent
rules, InputObservation semantics, analysis-cutoff enforcement, fencing,
immutable history, Manifest V1/V2, `REQUIRED_ANCESTRY_PIT_V1`, and unsupported
`SOURCE_PUBLISHED_AT` and `LEGACY_ASSERTED_AT` operational paths.

## 26. Frozen test matrix

- Research method: exact definition, wrong identity/schema rejection,
  immutability.
- Research: valid atomic creation, exact lineage, missing required parent,
  orphan rollback, one-time run binding, and null supersession.
- LLM request: exact client bytes, context, schema, parameters, code/config,
  and no hidden outcome context.
- LLM success/failure: raw-byte retention, parsed validation, success/error
  bundles, malformed JSON, schema failure, and post-validation failure.
- Analyst/Critic: exact linkage, exact Analyst stage consumed by Critic, and no
  implicit latest selection.
- Temporal: T1/T2/T3 ordering, Research no earlier than Critic, Setup no
  earlier than Research/OHLCV, and no LLM backdating.
- PIT: V2 registration, raw response NOT_APPLICABLE, A+N=A, R+N=R, U+N=U,
  Research/Setup propagation, and optional exclusion.
- Setup policy: exact singleton, current thresholds, immutability, and absence
  of `definition_sha256`.
- Setup: exact Research and OHLCV, deterministic result, no latest selection,
  and atomic entity/node construction.
- Manifest V3: exact schema and policy references, exact Research/Setup IDs,
  empty eligibility/trade/outcome IDs, DAG agreement, deterministic sorting,
  idempotency, and incompatible-finalization rejection.
- JNJ: deterministic explicit fixture mapping through observations, normalized
  facts, Analyst, Critic, Research, Setup, temporal/classification closure, and
  Manifest V3.
- Migration: fresh level 6, populated level 5 upgrade, rerun zero, clean FK
  check, and unchanged migrations 0001-0005.
- Regression: all Ledger/TRINITY tests, unchanged USA V2/Setup V1 behavior,
  H2 untouched, no network, no production database.

## 27. Out-of-scope boundary

V1.4 does not implement or define execution eligibility, trade execution,
lifecycle, outcomes, challenge cases, experiments, evaluator/export,
scheduling, alerts, portfolio logic, ML, new providers, full legacy import, or
automatic learning. It does not operationalize `SOURCE_PUBLISHED_AT` or
`LEGACY_ASSERTED_AT`.
