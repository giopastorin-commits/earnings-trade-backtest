# Ledger Foundation V1.4.2 Corrective Amendment

Freeze identifier: `LEDGER_FOUNDATION_V1_SCHEMA_FREEZE_v1.4.2`

Scope: `SETUP_POLICY_DECIMAL_SERIALIZATION_ERRATUM`

## 1. Status and scope

This is a narrow corrective amendment to the immutable Ledger Foundation
V1.4.0 and V1.4.1 freezes. The authoritative contract is V1.4.0 plus V1.4.1
plus this amendment. This amendment wins only for the persisted representation
of
`ledger.usa-setup-v1-policy-definition.v1.pullback.minimum_rr_tp1`.

V1.4.0 and V1.4.1 otherwise remain byte-semantically unchanged. This amendment
does not alter the Setup V1 algorithm, any Setup threshold, the Ledger
canonicalizer, a canonicalization version, PIT semantics, derivation semantics,
classification semantics, or Manifest V1/V2/V3 semantics.

## 2. Corrected Setup-policy singleton

In the closed canonical payload of
`ledger.usa-setup-v1-policy-definition.v1`, the persisted JSON property:

```json
"minimum_rr_tp1": 1.5
```

is superseded by exactly:

```json
"minimum_rr_tp1": "1.5"
```

The corrected value is a required JSON string and a canonical DECIMAL under:

```text
^-?(?:0|[1-9]\d*)(?:\.\d*[1-9])?$
```

The persisted Artifact envelope remains exactly:

```text
artifact_kind            = ledger.usa-setup-v1-policy-definition.v1
schema_name              = ledger.usa-setup-v1-policy-definition
schema_version           = "1"
canonicalization_version = JCS-LEDGER-SUBSET-V1
media_type               = application/json
```

Every other property, value, type, order-sensitive array, closed-object rule,
and semantic constraint in the V1.4.0 singleton remains unchanged. Extra
properties remain forbidden.

## 3. Mathematical semantics

The mathematical threshold remains exactly 1.5. Policy validation and Setup
evaluation SHALL parse the canonical string as an exact base-10 Decimal before
comparison. The rule remains:

```text
rr_tp1 >= Decimal("1.5")
```

Decimal strings SHALL NOT be compared lexicographically. Binary floating-point
comparison SHALL NOT replace exact Decimal comparison where the persisted
policy threshold is evaluated. Current Setup V1 calculations and all expected
results, including the JNJ result, remain unchanged.

## 4. Canonicalization preservation

`JCS-LEDGER-SUBSET-V1` remains unchanged. This amendment does not authorize
nonzero fractional JSON numbers, a new canonicalization version, or changes to
Artifact hashing. Integer-valued policy properties remain JSON integers.
Numeric literals embedded inside formula strings remain strings and are not
rewritten.

## 5. Exhaustive policy-payload audit

The complete frozen
`ledger.usa-setup-v1-policy-definition.v1` singleton was audited. Before this
amendment, `pullback.minimum_rr_tp1` was the only actual nonzero fractional JSON
number in the payload. The other actual JSON numeric values are integers:
`minimum_bars`, `sma_windows`, `atr_window`, `rsi_window`,
`average_volume_window`, `return_windows`, `range_windows`, and
`rounding_decimal_places`.

Constants such as `0.99`, `1.2`, `0.10`, `2.0`, `3.0`, `1.5`, `1.02`, and
`0.25` occur only within frozen formula strings and require no persistence
change.

## 6. Supersession and preservation

V1.4.2 supersedes V1.4.0/V1.4.1 only for the JSON representation of
`ledger.usa-setup-v1-policy-definition.v1.pullback.minimum_rr_tp1`.

All other V1.0-V1.4.1 clauses remain authoritative and unchanged, including:

- `TRINITY_USA_RESEARCH / USA_V2` behavior;
- `SETUP / USA_SETUP_V1` calculations and thresholds;
- `MAX_REQUIRED_PARENTS_V1`;
- `REQUIRED_ANCESTRY_PIT_V1` and `REQUIRED_ANCESTRY_PIT_V2`;
- PIT and record-class propagation;
- Research/Setup persistence and lineage contracts;
- Manifest V1/V2/V3 semantics;
- the Setup-result canonical DECIMAL-string correction in V1.4.1; and
- the frozen JNJ mathematical result.

Migrations 0001-0005 remain immutable. This amendment does not create or modify
migration 0006, production code, or tests. Future migration 0006 remains the
next implementation migration under the V1.4.0/V1.4.1 authorized scope, with
this corrected policy singleton applied by implementation validation.

## 7. Frozen implementation-test delta

Implementation tests SHALL prove:

1. the Setup-policy Artifact with `"minimum_rr_tp1":"1.5"` is accepted;
2. JSON numeric `1.5` is rejected;
3. strings `"1.50"`, `"01.5"`, and `"-0"` are rejected;
4. malformed DECIMAL strings are rejected;
5. the threshold is parsed and compared as exact `Decimal("1.5")`;
6. Setup V1 results remain mathematically unchanged; and
7. the frozen JNJ Setup result remains unchanged.
