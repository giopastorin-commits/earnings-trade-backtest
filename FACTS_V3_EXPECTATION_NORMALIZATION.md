# Facts V3 Expectation Normalization

Facts V3 is an additive contract used by the forward Research path. Facts V1 and
Facts V2 retain their existing builders, readers, artifact kinds, prompts, and
semantics.

Each event contains an `expectation_comparisons` array. A comparison preserves:

- `current_evidence_id` and `prior_evidence_id`;
- `issuer`, `metric`, `target_fiscal_period`, and `prior_target_fiscal_period`;
- current and prior period granularity (`ANNUAL`, `QUARTERLY`, or `UNKNOWN`);
- `unit` and `prior_unit`;
- current and prior currency state;
- `current_value` and `prior_value`, each represented as a point or range;
- `current_basis` and `prior_basis`;
- `direction` when established;
- `comparability_reason_codes`;
- `expectation_comparison_state`.

The allowed states are `VERIFIED_CHANGE`, `VERIFIED_UNCHANGED`,
`NOT_ESTABLISHED`, and `AMBIGUOUS`.

`VERIFIED_CHANGE` and `VERIFIED_UNCHANGED` require current and prior confirmed
primary issuer evidence, the same issuer and economic metric, the same target
fiscal period, compatible units, comparable reporting/perimeter basis, and
explicit values. `AMBIGUOUS` requires an actual prior comparison whose economic
comparability is uncertain. `NOT_ESTABLISHED` is used when no valid baseline is
available, including a different target fiscal period or non-primary evidence.
No comparison is inferred from event order when publication dates are equal.
Point values compare only with points and ranges only with ranges. Unknown
currency, unknown economic basis, mixed value shapes, conflicting current
values, segment scope, or explicit perimeter differences cannot produce a
verified change. Raw-text observations also require explicit forward-looking
issuer language.

An expectation facet is uniquely identified by current evidence, metric,
target fiscal period, and period granularity. Annual and quarterly facets from
one source are therefore distinct, while two records for the exact same facet
remain invalid. An `UNKNOWN`-period candidate is suppressed only when its text
span overlaps a more-specific candidate with the same metric, unit, currency,
and value; independent `UNKNOWN` observations are retained.

This layer establishes factual comparability only. It does not determine
materiality, thesis impact, Research status, or the final event class. In Facts
V3 Analyst and Critic prompts, a material `VERIFIED_CHANGE` facet takes
precedence over `NEW_INFORMATION`; ambiguous or non-comparable evidence does
not automatically produce `EXPECTATION_CHANGE`. Comparable issuer guidance is
a valid expectation baseline and does not require market consensus.

Ledger persistence uses `ledger.usa-v2-facts.v3` and the additive
`ledger.usa-v2-research-method-definition.v2` artifact. Facts V3 Research uses
the explicit database method version `USA_V2_FACTS_V3`; historical Research
retains `USA_V2`. Migration 0007 permits both versions without changing any
historical row or artifact definition.
