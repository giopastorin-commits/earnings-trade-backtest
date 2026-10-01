# Ledger Foundation V1.4.1 Corrective Amendment

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.4.1`

Scope: `V1_4_IMPLEMENTATION_CLOSURE`

## 1. Status, interpretation, and scope

This is a corrective implementation-closure amendment to the immutable Ledger
Foundation V1.4.0 freeze. The authoritative V1.4 contract is V1.4.0 plus this
amendment. This amendment wins only for the clauses explicitly enumerated
below. It makes no analytical change to USA V2 Research, USA Setup V1,
`MAX_REQUIRED_PARENTS_V1`, either PIT algebra, Research/Setup classification,
Manifest V1/V2/V3 semantics, the JNJ expected result, or Ledger canonicalization.

This amendment corrects or completes exactly:

1. V1.4.0 Sections 5 and 24-26 by authorizing a populated-safe V1/V2 evolution
   of `pit_classification_policy` in migration 0006;
2. V1.4.0 Sections 9-13 and 16 by closing the remaining LLM Artifact contracts;
3. V1.4.0 Section 8 by closing the USA V2 nested structured-fact catalog;
4. V1.4.0 Section 18.1 by closing the three Setup rule-JSON mappings; and
5. V1.4.0 Section 18.2 by requiring canonical decimal strings for every finite
   quantitative decimal in `ledger.usa-setup-v1-result.v1`.

All other V1.4.0 clauses remain unchanged. Migrations 0001-0005 remain
byte-identical. V1.4.1 defines, but does not create, migration 0006.

## 2. Populated-safe PIT-policy table evolution

### 2.1 Resulting table contract

Migration 0006 SHALL replace only the `policy_version` CHECK of the existing
`pit_classification_policy` table. The resulting table has exactly the same
columns, order, types, nullability, primary key, unique constraint, foreign key,
and other CHECK constraints as migration 0005, except:

```sql
policy_version TEXT NOT NULL CHECK (
    policy_version IN (
        'REQUIRED_ANCESTRY_PIT_V1',
        'REQUIRED_ANCESTRY_PIT_V2'
    )
)
```

No semantic column is added, removed, renamed, or transformed. The unique
semantic identity remains `(policy_kind, policy_version)`. Migration does not
register a V2 row. V2 registration remains an explicit authorized operation.

### 2.2 Required SQLite migration procedure

The migration framework SHALL execute the following as one atomic migration
transaction with `PRAGMA foreign_keys = ON`. Before the first DDL statement it
SHALL execute `PRAGMA defer_foreign_keys = ON` for that transaction.

1. Record the original policy-row count. Validate that every existing row has
   `policy_kind = 'PIT_CLASSIFICATION'` and
   `policy_version = 'REQUIRED_ANCESTRY_PIT_V1'`.
2. Create `pit_classification_policy_v141` using the exact migration-0005 table
   DDL with only the two-value `policy_version` CHECK above.
3. Copy explicitly, without coercion or regenerated values:

   ```sql
   INSERT INTO pit_classification_policy_v141 (
       classification_policy_id, policy_kind, policy_version,
       definition_artifact_id, code_commit, created_at
   )
   SELECT classification_policy_id, policy_kind, policy_version,
          definition_artifact_id, code_commit, created_at
   FROM pit_classification_policy;
   ```

4. Before replacement, prove equal row counts and empty bidirectional `EXCEPT`
   results across all six columns. The migration SHALL use a temporary guard
   table with a failing CHECK (or an equivalently transactional SQLite guard)
   so any mismatch aborts rather than merely returning a diagnostic row.
5. Drop exactly `pit_classification_policy_insert_authorized`,
   `pit_classification_policy_no_update`, and
   `pit_classification_policy_no_delete`.
6. Drop the old `pit_classification_policy` table while foreign-key checking is
   deferred, then rename `pit_classification_policy_v141` to
   `pit_classification_policy` before the transaction can commit.
7. Recreate the three policy triggers byte-semantically equivalent to migration
   0005 against the replacement table. No classification-table trigger or
   index is changed merely for this rebuild.
8. Recheck the original row count, the six-column row set, and all
   `classification_policy_id` values. Verify that every
   `derivation_node_classification.classification_policy_id` resolves to the
   replacement table and that every classification/supersession row is
   otherwise unchanged.
9. Execute `PRAGMA foreign_key_check`; any row is fatal. Commit only after every
   guard and the FK check succeeds.

The rebuild preserves all policy IDs, definition Artifact references, code
commits, timestamps, classification FKs, classification versions, and
supersession chains. A populated level-5 database and a fresh 0001-through-0006
database follow the same resulting contract. Migration-framework replay
applies migration 0006 zero additional times.

## 3. Common structured-Artifact rules

Unless a contract below says `IDENTITY`, its persisted envelope is:

```text
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

The named `schema_name` is Artifact metadata/payload contract terminology, not
a new Artifact-table column. Each structured payload is a JSON object with all
listed properties required, even when nullable. Extra properties and duplicate
object keys are forbidden. Artifact IDs match `^sha256:[0-9a-f]{64}$` and must
resolve to immutable Artifacts having the stipulated kind, canonicalization,
media type, and payload contract. Arrays preserve their declared order;
unordered arrays are sorted as stated and reject duplicates.

`DECIMAL` means a string matching:

```text
^-?(?:0|[1-9]\d*)(?:\.\d*[1-9])?$
```

It denotes a finite base-10 value, forbids exponent notation, a leading plus,
unnecessary leading zeroes, trailing fractional zeroes, and negative zero, and
uses integer lexical form for integer-valued decimals. Decimal conversion from
current engine numbers is a persistence projection and does not alter the
engine calculation.

## 4. `ledger.llm-context.v1`

Envelope:

```text
artifact_kind = ledger.llm-context.v1
schema_name   = ledger.llm-context
```

Payload, closed:

```text
schema_name      string, exactly "ledger.llm-context"
schema_version   string, exactly "1"
interaction_role enum: ANALYST | CRITIC
items            non-empty ordered array of context items
```

Each context item has exactly:

```text
context_role       enum: FACTS | ANALYST_EFFECTIVE_STAGE_RESULT
artifact_id        Artifact ID
derivation_node_id non-empty derivation-node ID
```

For `ANALYST`, `items` contains exactly one `FACTS` item. Its Artifact is the
exact USA V2 factual context supplied to the Analyst, and its node is the exact
NORMALIZED_FACT node representing that Artifact.

For `CRITIC`, `items` contains exactly two items in this order: `FACTS`, then
`ANALYST_EFFECTIVE_STAGE_RESULT`. The first is the exact factual context sent to
the Critic. The second identifies the exact
`ledger.usa-v2-stage-result.v1` Artifact and NORMALIZED_FACT node produced by
the exact Analyst interaction. The pair `(context_role, artifact_id,
derivation_node_id)` is unique, and neither Artifact nor node may appear twice.
Every node must represent its paired Artifact. Outcome history, prior-trade
memory, implicit caches, latest selection, and undeclared context are forbidden.

## 5. `ledger.llm-response-schema.v1`

Envelope:

```text
artifact_kind            = ledger.llm-response-schema.v1
schema_name              = ledger.llm-response-schema
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/schema+json
```

The payload is the exact JSON Schema object written to the Codex CLI
`--output-schema` file; it has no Ledger wrapper. Role binding is explicit in
the invocation request and the schema Artifact must equal the corresponding
singleton below byte-semantically after Ledger canonicalization.

Definitions used by both singleton schemas:

```json
{
  "claim_ref": {
    "type": "object",
    "additionalProperties": false,
    "required": ["claim_id", "field", "text", "fact_ids", "materiality", "period"],
    "properties": {
      "claim_id": {"type": "string"},
      "field": {"type": "string", "enum": ["fundamental_analysis", "earnings_and_news_analysis", "price_context", "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation"]},
      "text": {"type": "string"},
      "fact_ids": {"type": "array", "items": {"type": "string"}},
      "materiality": {"type": "string", "enum": ["MATERIAL", "SUPPORTING"]},
      "period": {"type": ["string", "null"]}
    }
  },
  "event_assessment": {
    "type": "object",
    "additionalProperties": false,
    "required": ["event_id", "classification", "material", "rationale"],
    "properties": {
      "event_id": {"type": "string"},
      "classification": {"type": "string", "enum": ["NEW_INFORMATION", "EXPECTATION_CHANGE", "CONFIRMATION", "REITERATION", "ALREADY_KNOWN", "LOW_RELEVANCE"]},
      "material": {"type": "boolean"},
      "rationale": {"type": "string"}
    }
  }
}
```

The exact ANALYST schema is:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["fundamental_analysis", "earnings_and_news_analysis", "price_context", "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation", "proposed_status", "claim_refs", "evidence_confidence", "thesis_strength", "event_assessments"],
  "properties": {
    "fundamental_analysis": {"type": "string"},
    "earnings_and_news_analysis": {"type": "string"},
    "price_context": {"type": "string"},
    "bull_case": {"type": "string"},
    "bear_case": {"type": "string"},
    "catalysts": {"type": "array", "items": {"type": "string"}},
    "risks": {"type": "array", "items": {"type": "string"}},
    "thesis_invalidation": {"type": "string"},
    "proposed_status": {"type": "string", "enum": ["PASS", "WATCH", "INVESTIGATE"]},
    "claim_refs": {"type": "array", "items": {"type":"object","additionalProperties":false,"required":["claim_id","field","text","fact_ids","materiality","period"],"properties":{"claim_id":{"type":"string"},"field":{"type":"string","enum":["fundamental_analysis","earnings_and_news_analysis","price_context","bull_case","bear_case","catalysts","risks","thesis_invalidation"]},"text":{"type":"string"},"fact_ids":{"type":"array","items":{"type":"string"}},"materiality":{"type":"string","enum":["MATERIAL","SUPPORTING"]},"period":{"type":["string","null"]}}}},
    "evidence_confidence": {"type": "string", "enum": ["HIGH", "LOW", "MEDIUM"]},
    "thesis_strength": {"type": "string", "enum": ["HIGH", "LOW", "MEDIUM"]},
    "event_assessments": {"type": "array", "items": {"type":"object","additionalProperties":false,"required":["event_id","classification","material","rationale"],"properties":{"event_id":{"type":"string"},"classification":{"type":"string","enum":["NEW_INFORMATION","EXPECTATION_CHANGE","CONFIRMATION","REITERATION","ALREADY_KNOWN","LOW_RELEVANCE"]},"material":{"type":"boolean"},"rationale":{"type":"string"}}}}
  }
}
```

The exact CRITIC schema is:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["notes", "status", "evidence_confidence", "thesis_strength", "event_assessments", "revised_analysis"],
  "properties": {
    "notes": {"type": "array", "items": {"type": "string"}},
    "status": {"type": "string", "enum": ["PASS", "WATCH", "INVESTIGATE"]},
    "evidence_confidence": {"type": "string", "enum": ["HIGH", "LOW", "MEDIUM"]},
    "thesis_strength": {"type": "string", "enum": ["HIGH", "LOW", "MEDIUM"]},
    "event_assessments": {"type": "array", "items": {"type":"object","additionalProperties":false,"required":["event_id","classification","material","rationale"],"properties":{"event_id":{"type":"string"},"classification":{"type":"string","enum":["NEW_INFORMATION","EXPECTATION_CHANGE","CONFIRMATION","REITERATION","ALREADY_KNOWN","LOW_RELEVANCE"]},"material":{"type":"boolean"},"rationale":{"type":"string"}}}},
    "revised_analysis": {
      "type": "object",
      "additionalProperties": false,
      "required": ["fundamental_analysis", "earnings_and_news_analysis", "price_context", "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation", "claim_refs"],
      "properties": {
        "fundamental_analysis": {"type": "string"},
        "earnings_and_news_analysis": {"type": "string"},
        "price_context": {"type": "string"},
        "bull_case": {"type": "string"},
        "bear_case": {"type": "string"},
        "catalysts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "thesis_invalidation": {"type": "string"},
        "claim_refs": {"type": "array", "items": {"type":"object","additionalProperties":false,"required":["claim_id","field","text","fact_ids","materiality","period"],"properties":{"claim_id":{"type":"string"},"field":{"type":"string","enum":["fundamental_analysis","earnings_and_news_analysis","price_context","bull_case","bear_case","catalysts","risks","thesis_invalidation"]},"text":{"type":"string"},"fact_ids":{"type":"array","items":{"type":"string"}},"materiality":{"type":"string","enum":["MATERIAL","SUPPORTING"]},"period":{"type":["string","null"]}}}}
      }
    }
  }
}
```

Required-array, enum-array, and method-field order is exactly the displayed
order. Object member serialization is governed by JCS. The definitions shown
before the singleton schemas are descriptive aliases only; the supplied schema
objects contain the displayed inline definitions and no `$defs` member.

## 6. `ledger.llm-invocation-parameters.v1`

Envelope:

```text
artifact_kind = ledger.llm-invocation-parameters.v1
schema_name   = ledger.llm-invocation-parameters
```

The closed payload has exactly these required properties:

```text
schema_name                 exactly "ledger.llm-invocation-parameters"
schema_version              exactly "1"
provider                    exactly "OPENAI_CODEX_CLI"
transport                   exactly "CODEX_CLI_STDIN"
model                       exactly "gpt-5.6-sol"
model_version               exactly "codex-cli:gpt-5.6-sol"
timeout_seconds             integer, exactly 240
sandbox                     exactly "read-only"
ephemeral                   boolean, exactly true
skip_git_repo_check         boolean, exactly true
ignore_user_config          boolean, exactly true
response_schema_artifact_id Artifact ID for the exact role schema
temperature                 null
top_p                       null
seed                        null
max_output_tokens           null
reasoning_effort            null
tool_mode                   exactly "NONE"
```

Null means TRINITY did not supply that parameter; it does not assert a provider
default. The response-schema ID must equal the ID in the invocation request.

## 7. Code and configuration Artifacts

### 7.1 `ledger.code-manifest.v1`

Envelope:

```text
artifact_kind = ledger.code-manifest.v1
schema_name   = ledger.code-manifest
```

The closed payload is:

```text
schema_name    exactly "ledger.code-manifest"
schema_version exactly "1"
repository     exactly "earnings-trade-backtest"
git_commit     40 lowercase hexadecimal characters
source_files   array sorted lexicographically by path
```

Each source-file item has exactly `path` and `sha256`; `path` is one of, in
this exact order, `trinity/italia_real.py`, `trinity/italia_v1.py`, and
`trinity/usa_v2.py`, and `sha256` is the lowercase SHA-256 of that file's exact
bytes at `git_commit`. Paths and hashes are unique. Dirty or untracked source
cannot be represented by this manifest.

### 7.2 `ledger.usa-v2-invocation-config.v1`

Envelope:

```text
artifact_kind = ledger.usa-v2-invocation-config.v1
schema_name   = ledger.usa-v2-invocation-config
```

The closed singleton payload is exactly:

```json
{"schema_name":"ledger.usa-v2-invocation-config","schema_version":"1","config_identity":"USA_V2_FROZEN_INVOCATION_CONFIG_V1"}
```

It records the absence of any additional decision-relevant invocation config;
method semantics belong to the Research method Artifact and provider settings
belong to the invocation-parameters Artifact.

## 8. Raw and parsed LLM response Artifacts

### 8.1 `ledger.llm-raw-response.v1`

```text
artifact_kind            = ledger.llm-raw-response.v1
schema_name              = ledger.llm-raw-response
schema_version           = "1"
canonicalization_version = IDENTITY
media_type               = application/json
```

The payload is exactly the provider `result.json` byte sequence, without BOM
removal, newline change, whitespace normalization, Unicode normalization, JSON
parsing, or reserialization. Persistence occurs before UTF-8 or JSON validity
is asserted. UTF-8 decoding is attempted only during parsing. Invalid UTF-8,
malformed JSON, or non-object JSON fails the interaction but does not alter the
already observed raw bytes.

### 8.2 `ledger.llm-parsed-response.v1`

Envelope:

```text
artifact_kind = ledger.llm-parsed-response.v1
schema_name   = ledger.llm-parsed-response
```

The closed payload has exactly:

```text
schema_name                 exactly "ledger.llm-parsed-response"
schema_version              exactly "1"
interaction_role            ANALYST | CRITIC
raw_response_artifact_id    Artifact ID targeting the exact raw response
response_schema_artifact_id Artifact ID targeting the exact role schema
parsed_value                object satisfying the role schema below
```

For ANALYST, `parsed_value` has exactly the fields and nested contracts of the
ANALYST singleton in Section 5. For CRITIC, it has exactly those of the CRITIC
singleton. In addition to JSON Schema validity, strings consumed by deterministic
USA V2 validation must be non-empty where V1.4.0 requires semantic text;
event IDs must cover the explicit factual event set exactly once; claim and
fact references are explicit and never resolved through latest lookup.

The authoritative chain remains exact raw bytes -> schema-valid parsed value ->
deterministic effective USA V2 stage result. A raw parse failure, schema
failure, or deterministic post-validation failure creates a FAILED interaction
and no parsed/effective success bundle as applicable. No failed interaction can
create finalized Research.

## 9. Complete USA V2 structured-fact catalog

This section completes V1.4.0 Section 8. It defines the persistence projection;
it adds no extractor or fact. Every object is closed and every listed key is
required. Nullable members appear as explicit null. All decimal quantities use
`DECIMAL`; counters, scales, and display precision are JSON integers within the
JCS safe-integer range.

### 9.1 Price acquisition

`price.current_price`, `return_5d`, `return_20d`, and `return_60d` are DECIMAL
or null. `last_bar` has exactly:

```text
date                          ISO date
open, high, low, close        DECIMAL
adjusted_close                DECIMAL or null
volume                        DECIMAL, numeric value >= 0
```

`reference_bars` has exactly keys `5`, `20`, and `60`. Each value has exactly
`date` (ISO date) and `close` (positive DECIMAL). `fact_metadata` retains engine
order; fact IDs are unique. Each item has exactly:

```text
fact_id, ticker, metric, value_path, unit, period, evidence_id,
extraction_method             non-empty strings
scale                         positive integer
display_precision             nonnegative integer
```

For current USA V2, metric order is `current_price`, `return_5d`, `return_20d`,
`return_60d`; `value_path` is `price.` plus metric; unit is USD for current
price and PERCENT otherwise; extraction method is
`deterministic_cached_ohlcv`.

### 9.2 Normalized event-facts envelope

Every persisted event `facts` object has exactly:

```text
evidence_identifier string
novelty              NEW_RELEASE | REITERATION | NEW_PRIMARY_DOCUMENT | null
issuer_release       true | null
primary_source       true | null
reported_metrics     array
guidance_ranges      array
guidance_language    object or null
guidance_changes     array
structured_sec_facts array
```

Arrays preserve engine order and reject duplicate `fact_id`. Branches are:

- cached issuer release: non-null novelty, `issuer_release=true`,
  `primary_source=null`, structured facts empty;
- SEC earnings release: `novelty=NEW_PRIMARY_DOCUMENT`,
  `issuer_release=null`, `primary_source=true`;
- SEC material event: novelty and issuer flag null, `primary_source=true`, all
  four fact arrays empty and guidance language null;
- SEC periodic report: novelty and issuer flag null, `primary_source=true`,
  only `structured_sec_facts` may be non-empty.

### 9.3 Reported metric

Each `reported_metrics` item has exactly:

```text
fact_id, ticker, metric, metric_label, period, evidence_id,
evidence_identifier, source_excerpt, extraction_method  non-empty strings
value                                                    DECIMAL
unit                                                     USD_MILLIONS | USD_BILLIONS | USD_TRILLIONS
measurement_type                                         REPORTED_AMOUNT | CHANGE_AMOUNT | ANNUALIZED_RUN_RATE
scale                                                     1000000 | 1000000000 | 1000000000000
display_precision                                         nonnegative integer
```

`metric` equals `metric_label`; `evidence_id` equals `evidence_identifier`; the
extraction method is `literal_regex_explicit_currency_and_scale`.

### 9.4 Guidance range, language, and change

A `guidance_ranges` item has exactly:

```text
fact_id, ticker, metric, period, unit, evidence_id,
evidence_identifier, source_excerpt, extraction_method  non-empty strings
previous_range                                            null or two-element DECIMAL array [low, high]
current_range                                             two-element DECIMAL array [low, high]
value                                                     exact duplicate of current_range
scale                                                     integer 1
display_precision                                         null
```

`metric` is `ADJUSTED_EPS` or `CORE_FFO`; unit is `USD_PER_SHARE`; each range
is numerically nondecreasing; extraction method is
`literal_guidance_range_explicit_usd_per_share`; evidence IDs are equal.

`guidance_language`, when non-null, has exactly non-empty strings
`source_excerpt`, `evidence_identifier`, and `extraction_method`, with method
`literal_guidance_language`.

A `guidance_changes` item has exactly:

```text
fact_id, ticker, metric, period, unit, evidence_id,
evidence_identifier, previous_evidence_identifier,
current_evidence_identifier, extraction_method          non-empty strings
previous_range, current_range                             two-element DECIMAL arrays [low, high]
direction                                                 RAISED | LOWERED | UNCHANGED | MIXED
value                                                     exactly direction
scale                                                     integer 1
display_precision                                         null
```

Unit is `USD_PER_SHARE`; `evidence_identifier` and `evidence_id` both equal
`current_evidence_identifier`; method is
`deterministic_same_metric_period_unit_range_comparison`.

### 9.5 Structured SEC fact

Each `structured_sec_facts` item has exactly:

```text
fact_id, ticker, metric, period, unit, source, evidence_id,
evidence_identifier, accession_number, extraction_method non-empty strings
value                                                      DECIMAL
scale                                                      positive integer
display_precision                                          nonnegative integer
xbrl_tag                                                   non-empty string or null
comparison_period                                          non-empty string or null
components                                                  ordered array of non-empty strings or null
```

`source` is `SEC XBRL companyfacts`; evidence IDs are equal. Exactly one shape
applies:

- direct XBRL: non-null `xbrl_tag`, null comparison/components, method
  `sec_xbrl_exact_accession`; metric is one of `revenue`, `operating_income`,
  `net_income`, `eps_diluted`, `operating_cash_flow`, `capex`, `cash`, `debt`;
- year-over-year revenue growth: non-null `xbrl_tag` and
  `comparison_period`, null components, metric `revenue_growth`, unit PERCENT,
  method `deterministic_same_tag_same_duration_year_over_year`;
- operating margin: null tag/comparison, components exactly the operating
  income and revenue XBRL tags in that order, metric `operating_margin`, unit
  PERCENT, method `deterministic_operating_income_divided_by_revenue`;
- free cash flow: null tag/comparison, components exactly operating-cash-flow
  and capex tags in that order, metric `free_cash_flow`, method
  `deterministic_operating_cash_flow_minus_capex`.

Components contain no duplicates. Unit/scale is the current extractor's exact
unit and frozen scale mapping. Engine order is preserved.

### 9.6 Evidence and remaining facts

Each evidence item has exactly `source`, `published_at`, `identifier`, `url`,
`excerpt`, `title`, `retrieved_at`, `content_sha256`, `raw_path`,
`published_at_precision`, `document_kind`, `filing_date`, `accepted_at`,
`form`, and `accession_number`. Nullable values appear explicitly as null;
`source` and `published_at` are non-empty, and at least one of identifier/url is
non-null. Non-null identifiers are unique. `content_sha256`, when non-null, is
64 lowercase hexadecimal characters. Evidence retains engine order.

The remaining fact-block contracts from V1.4.0 are unchanged, except that all
finite decimal quantities in the persisted semantic Research Artifact use
DECIMAL rather than binary-float JSON numbers. `financial_facts` and
`valuation_metrics` remain exactly empty objects for USA V2. `missing_fields`
retains engine order and rejects duplicates. This is serialization only and
does not change extraction or analysis.

## 10. Setup rule JSON contracts

`setup.entry_rule_json`, `stop_rule_json`, and `target_rule_json` are canonical
JCS JSON text values representing semantic rule identity, not evaluated levels.
Each is a closed object with exactly `schema_version`, `rule_type`, and
`parameters`. `schema_version` is always `"1"`; `parameters` is exactly `{}`.
Concrete evaluated values remain solely in the immutable
`ledger.usa-setup-v1-result.v1` Artifact. The Setup row's explicit
`setup_policy_id`/`setup_policy_version` fixes the policy; no current-policy
lookup is permitted.

For BREAKOUT:

```json
{"schema_version":"1","rule_type":"USA_SETUP_V1_BREAKOUT_ENTRY","parameters":{}}
{"schema_version":"1","rule_type":"USA_SETUP_V1_BREAKOUT_STOP","parameters":{}}
{"schema_version":"1","rule_type":"USA_SETUP_V1_BREAKOUT_TARGETS","parameters":{}}
```

For PULLBACK:

```json
{"schema_version":"1","rule_type":"USA_SETUP_V1_PULLBACK_ENTRY","parameters":{}}
{"schema_version":"1","rule_type":"USA_SETUP_V1_PULLBACK_STOP","parameters":{}}
{"schema_version":"1","rule_type":"USA_SETUP_V1_PULLBACK_TARGETS","parameters":{}}
```

For NO_SETUP:

```json
{"schema_version":"1","rule_type":"NO_OPERATIONAL_ENTRY","parameters":{}}
{"schema_version":"1","rule_type":"NO_OPERATIONAL_STOP","parameters":{}}
{"schema_version":"1","rule_type":"NO_OPERATIONAL_TARGETS","parameters":{}}
```

The three values must match `setup_kind`, the referenced USA_SETUP_V1 policy,
and the result Artifact. Any mixed family, unknown member, extra property,
nonempty parameters, or operational rule paired with NO_SETUP is invalid.

## 11. Setup-result decimal-string correction

This section supersedes only the numeric encoding statements in V1.4.0
Section 18.2. In `ledger.usa-setup-v1-result.v1`, every finite quantitative
decimal is a canonical DECIMAL string. These properties are DECIMAL when
non-null:

```text
entry_level, stop_level, stop_distance_pct, tp1, tp2,
risk_per_share, reward_tp1, reward_tp2, rr_tp1, rr_tp2,
close, atr14, rsi14, sma20, sma50, sma200,
distance_sma20_pct, distance_sma50_pct, distance_sma200_pct,
return_5d, return_20d, return_60d, high_20d, low_20d,
average_volume_20d, relative_volume
```

Existing nullable operational fields remain DECIMAL-or-null. `ticker`, `as_of`,
`research_status`, `evidence_confidence`, `thesis_strength`,
`technical_regime`, `setup_type`, `entry_condition`, `reason_codes`, and
`invalidation`, and every cross-field semantic rule remain unchanged.

Validation parses DECIMAL strings as exact base-10 decimals before comparison.
It never compares them lexicographically and does not use binary float when
exact decimal comparison is practical. Thus `stop_level < entry_level < tp1 <
tp2` and `rr_tp1 >= 1.5` retain their numeric meanings.

Current Setup V1 may calculate internally exactly as before. Persistence maps
each current finite result to its canonical decimal string. Four-decimal
display formatting is presentation only. The JNJ values map exactly:

```text
264.0220 -> "264.022"
256.1417 -> "256.1417"
281.0700 -> "281.07"
287.6628 -> "287.6628"
2.1634   -> "2.1634"
3.0000   -> "3"
```

Mathematically equal decimals therefore yield identical semantic payload bytes
and Artifact identity irrespective of input formatting.

## 12. Corrected migration 0006 scope

Future migration 0006 is authorized to do exactly:

1. perform the Section 2 populated-safe policy-table rebuild;
2. create the seven V1.4.0 integration tables `research_method`,
   `llm_interaction`, `research_record`, `research_llm_interaction`,
   `setup_policy`, `setup`, and `setup_research_lineage`;
3. create or recreate only their required indexes and triggers;
4. activate RESEARCH and SETUP target validation; and
5. accept Manifest V3 while preserving V1/V2 behavior.

It does not modify migrations 0001-0005, synthesize history or a V2 policy row,
alter existing temporal/classification records, or change the canonicalizer.

## 13. Frozen test delta

Migration tests SHALL cover populated V1 policy preservation, unchanged policy
IDs and classification FKs, intact supersession chains, explicit V2
registration, duplicate semantic-policy rejection, fresh and populated
upgrade paths, rerun zero, and clean `foreign_key_check`.

LLM tests SHALL cover every exact envelope and closed payload, extra-property
and wrong-kind rejection, exact raw bytes, invalid UTF-8/malformed JSON,
ANALYST and CRITIC parsed schemas, context ordering and exact Analyst linkage,
and exact code/config identity.

Structured-fact tests SHALL exercise every allowed nested shape, missing and
extra-key rejection, malformed items, branch normalization, canonical decimal
projection, and acceptance of the current USA V2 fixture without changing its
mathematical content.

Setup-rule tests SHALL cover exact BREAKOUT, PULLBACK, and NO_SETUP triples,
malformed rules, mixed families, and result/policy inconsistency.

Setup-result tests SHALL cover canonical decimal persistence, trailing-zero
and integer normalization, negative-zero rejection, NaN/Infinity rejection,
malformed strings, nullable-field preservation, exact Decimal comparisons,
mathematically unchanged BREAKOUT/PULLBACK projections, unchanged NO_SETUP null
levels, the six exact JNJ strings in Section 11, and identical Artifact identity
for repeated mathematically identical decimals.

## 14. Preservation

V1.4.1 does not change TRINITY_USA_RESEARCH/USA_V2 behavior, SETUP/USA_SETUP_V1
calculations, `MAX_REQUIRED_PARENTS_V1`, either frozen PIT algebra, the approved
V2 PIT-policy payload, V1 PIT policy, Research/Setup propagation, Manifest
V1/V2/V3 semantics, thesis/runtime-field exclusions, Research supersession,
JNJ expected mathematical values, scanner, H2, alerts, or trading logic.
V1.0-V1.4.0 frozen documents and migrations 0001-0005 remain immutable.
