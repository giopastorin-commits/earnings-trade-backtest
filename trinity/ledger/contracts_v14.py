"""Frozen V1.4/V1.4.1 Artifact contracts and persistence projections."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from .canonical import CANONICAL_JSON_V1, IDENTITY, canonicalize_json
from .errors import ArtifactIntegrityError

ARTIFACT_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
UUID4_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
DECIMAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d*[1-9])?\Z")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

ANALYSIS_FIELDS = (
    "fundamental_analysis", "earnings_and_news_analysis", "price_context",
    "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation",
)
STATUSES = ("PASS", "WATCH", "INVESTIGATE")
LEVELS = ("HIGH", "LOW", "MEDIUM")
EVENT_CLASSES = (
    "NEW_INFORMATION", "EXPECTATION_CHANGE", "CONFIRMATION", "REITERATION",
    "ALREADY_KNOWN", "LOW_RELEVANCE",
)

RESEARCH_METHOD_DEFINITION = {
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
    "event_classes": list(EVENT_CLASSES),
    "analysis_fields": list(ANALYSIS_FIELDS),
    "claim_materialities": ["MATERIAL", "SUPPORTING"],
    "analyst_response_contract": "USA_V2_ANALYST_RESPONSE_V1",
    "critic_response_contract": "USA_V2_CRITIC_RESPONSE_V1",
    "event_validation": "USA_V2_COMPLETE_UNIQUE_EVENT_ASSESSMENT_V1",
    "claim_validation": "USA_V2_CLAIM_FACT_EVIDENCE_VALIDATION_V1",
    "evidence_confidence_bounding": "USA_V2_EVIDENCE_CONFIDENCE_CEILING_V1",
    "decision_calibration": "USA_V2_EVENT_CLASS_AND_THESIS_STRENGTH_V1",
    "citation_cleaning": "USA_V2_KNOWN_EVIDENCE_IDENTIFIER_ONLY_V1",
    "memory_policy": "CURRENT_INVOCATION_EXPLICIT_CONTEXT_ONLY",
    "outcome_history_allowed": False,
    "implicit_latest_input_allowed": False,
}

SETUP_POLICY_DEFINITION = {
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
        "otherwise": "NEUTRAL",
    },
    "preconditions": {
        "pass_result": "NO_SETUP", "downtrend": "NO_SETUP",
        "non_uptrend": "NO_SETUP",
    },
    "breakout": {
        "near_breakout": "close >= 0.99 * prior_high_20d",
        "volume_confirmed": "relative_volume >= 1.2",
        "entry": "prior_high_20d + 0.10 * atr14",
        "stop": "entry - 2.0 * atr14",
        "tp1": "entry + 2.0 * risk_per_share",
        "tp2": "entry + 3.0 * risk_per_share",
    },
    "pullback": {
        "support_candidates": ["sma20", "sma50"],
        "support_band": "0.99 * support <= close <= 1.02 * support",
        "support_selection": "minimum absolute distance from close",
        "entry": "support + 0.25 * atr14",
        "stop": "min(low_20d, entry - 1.5 * atr14)",
        "tp1": "high_20d",
        "minimum_rr_tp1": "1.5",
        "tp2_initial": "max(high_60d, entry + 2.0 * risk_per_share)",
        "tp2_fallback": "entry + 3.0 * risk_per_share when tp2_initial <= tp1",
    },
    "rounding_decimal_places": 4,
    "setup_types": ["BREAKOUT", "NO_SETUP", "PULLBACK"],
    "research_statuses": ["INVESTIGATE", "PASS", "WATCH"],
    "decision_levels": ["HIGH", "LOW", "MEDIUM"],
    "technical_regimes": ["DOWNTREND", "NEUTRAL", "UPTREND"],
    "implicit_latest_research_allowed": False,
}
MINIMUM_RR_TP1 = Decimal(SETUP_POLICY_DEFINITION["pullback"]["minimum_rr_tp1"])

CONFIG_DEFINITION = {
    "schema_name": "ledger.usa-v2-invocation-config",
    "schema_version": "1",
    "config_identity": "USA_V2_FROZEN_INVOCATION_CONFIG_V1",
}

SETUP_RULE_TYPES = {
    "BREAKOUT": (
        "USA_SETUP_V1_BREAKOUT_ENTRY", "USA_SETUP_V1_BREAKOUT_STOP",
        "USA_SETUP_V1_BREAKOUT_TARGETS",
    ),
    "PULLBACK": (
        "USA_SETUP_V1_PULLBACK_ENTRY", "USA_SETUP_V1_PULLBACK_STOP",
        "USA_SETUP_V1_PULLBACK_TARGETS",
    ),
    "NO_SETUP": (
        "NO_OPERATIONAL_ENTRY", "NO_OPERATIONAL_STOP", "NO_OPERATIONAL_TARGETS",
    ),
}

SETUP_DECIMAL_FIELDS = (
    "entry_level", "stop_level", "stop_distance_pct", "tp1", "tp2",
    "risk_per_share", "reward_tp1", "reward_tp2", "rr_tp1", "rr_tp2",
    "close", "atr14", "rsi14", "sma20", "sma50", "sma200",
    "distance_sma20_pct", "distance_sma50_pct", "distance_sma200_pct",
    "return_5d", "return_20d", "return_60d", "high_20d", "low_20d",
    "average_volume_20d", "relative_volume",
)


def canonical_decimal(value: object) -> str:
    """Return the frozen finite canonical decimal string."""
    if isinstance(value, bool) or value is None:
        raise ArtifactIntegrityError("decimal value must be a finite number or string")
    if isinstance(value, float) and not math.isfinite(value):
        raise ArtifactIntegrityError("decimal value must be finite")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ArtifactIntegrityError("invalid decimal value") from exc
    if not decimal.is_finite() or (decimal.is_zero() and decimal.is_signed()):
        raise ArtifactIntegrityError("non-finite and negative-zero decimals are forbidden")
    if decimal == decimal.to_integral_value():
        result = str(decimal.quantize(Decimal(1)))
    else:
        result = format(decimal.normalize(), "f")
    if not DECIMAL_RE.fullmatch(result):
        raise ArtifactIntegrityError("decimal does not have canonical lexical form")
    return result


def setup_rule_values(setup_kind: str) -> tuple[str, str, str]:
    try:
        rule_types = SETUP_RULE_TYPES[setup_kind]
    except KeyError as exc:
        raise ArtifactIntegrityError("unsupported Setup kind") from exc
    values = tuple(
        canonicalize_json({"schema_version": "1", "rule_type": item, "parameters": {}}).decode()
        for item in rule_types
    )
    return values  # type: ignore[return-value]


def setup_result_value(record: object) -> dict[str, Any]:
    """Project an unchanged SetupRecord into the frozen semantic payload."""
    if is_dataclass(record):
        value = asdict(record)
    elif isinstance(record, Mapping):
        value = dict(record)
    else:
        raise ArtifactIntegrityError("Setup result must be a record or mapping")
    for field in SETUP_DECIMAL_FIELDS:
        if field not in value:
            raise ArtifactIntegrityError(f"Setup result missing {field}")
        if value[field] is not None:
            value[field] = canonical_decimal(value[field])
    if isinstance(value.get("reason_codes"), tuple):
        value["reason_codes"] = list(value["reason_codes"])
    validate_setup_result(value)
    return value


def research_result_value(record: object) -> dict[str, Any]:
    """Project a current USA V2 ThesisRecord into its frozen semantic payload."""
    if is_dataclass(record):
        source = asdict(record)
    elif isinstance(record, Mapping):
        source = dict(record)
    else:
        raise ArtifactIntegrityError("Research result must be a record or mapping")
    value = json.loads(json.dumps(source))
    value.pop("thesis_id", None)
    value.pop("created_at", None)
    facts = value["facts"]
    price = facts["price"]
    for field in ("current_price", "return_5d", "return_20d", "return_60d"):
        if price[field] is not None:
            price[field] = canonical_decimal(price[field])
    acquisition = price["acquisition"]
    for field in ("open", "high", "low", "close", "adjusted_close", "volume"):
        if acquisition["last_bar"][field] is not None:
            acquisition["last_bar"][field] = canonical_decimal(acquisition["last_bar"][field])
    for item in acquisition["reference_bars"].values():
        item["close"] = canonical_decimal(item["close"])
    fundamentals = facts["fundamentals"]
    for field in ("revenue", "revenue_growth", "operating_margin", "net_income", "free_cash_flow", "net_debt"):
        if fundamentals[field] is not None:
            fundamentals[field] = canonical_decimal(fundamentals[field])
    fact_keys = {
        "evidence_identifier", "novelty", "issuer_release", "primary_source",
        "reported_metrics", "guidance_ranges", "guidance_language",
        "guidance_changes", "structured_sec_facts",
    }
    if facts.get("facts_contract_version") == "2":
        fact_keys.update({"source_class", "confirmation_state"})
    for event in facts["events"]:
        branch = event["facts"]
        for key in fact_keys:
            branch.setdefault(key, [] if key in {
                "reported_metrics", "guidance_ranges", "guidance_changes", "structured_sec_facts"
            } else None)
        for item in branch["reported_metrics"]:
            item["value"] = canonical_decimal(item["value"])
        for item in branch["guidance_ranges"]:
            for field in ("previous_range", "current_range", "value"):
                if item[field] is not None:
                    item[field] = [canonical_decimal(x) for x in item[field]]
        for item in branch["guidance_changes"]:
            for field in ("previous_range", "current_range"):
                item[field] = [canonical_decimal(x) for x in item[field]]
        for item in branch["structured_sec_facts"]:
            item["value"] = canonical_decimal(item["value"])
            item.setdefault("xbrl_tag", None)
            item.setdefault("comparison_period", None)
            item.setdefault("components", None)
    evidence_keys = {
        "source", "published_at", "identifier", "url", "excerpt", "title",
        "retrieved_at", "content_sha256", "raw_path", "published_at_precision",
        "document_kind", "filing_date", "accepted_at", "form", "accession_number",
    }
    for item in facts["evidence"]:
        for key in evidence_keys:
            item.setdefault(key, None)
    value["evidence"] = facts["evidence"]
    _validate_research_result(value)
    return value


def validate_setup_result(value: Any) -> None:
    expected = {
        "ticker", "as_of", "research_status", "evidence_confidence",
        "thesis_strength", "technical_regime", "setup_type", "entry_condition",
        *SETUP_DECIMAL_FIELDS, "reason_codes", "invalidation",
    }
    _closed(value, expected, "Setup result")
    _text(value["ticker"], "ticker")
    if not isinstance(value["as_of"], str) or not DATE_RE.fullmatch(value["as_of"]):
        _fail("Setup as_of must be an ISO date")
    _enum(value["research_status"], STATUSES, "research_status")
    _enum(value["evidence_confidence"], LEVELS, "evidence_confidence")
    _enum(value["thesis_strength"], LEVELS, "thesis_strength")
    _enum(value["technical_regime"], ("DOWNTREND", "NEUTRAL", "UPTREND"), "technical_regime")
    kind = _enum(value["setup_type"], tuple(SETUP_RULE_TYPES), "setup_type")
    for field in SETUP_DECIMAL_FIELDS:
        item = value[field]
        if item is not None and (not isinstance(item, str) or not DECIMAL_RE.fullmatch(item)):
            _fail(f"{field} must be a canonical decimal string or null")
        if isinstance(item, str):
            canonical_decimal(item)
    nullable = {
        "entry_level", "stop_level", "stop_distance_pct", "tp1", "tp2",
        "risk_per_share", "reward_tp1", "reward_tp2", "rr_tp1", "rr_tp2",
    }
    if kind == "NO_SETUP":
        if value["entry_condition"] is not None or any(value[x] is not None for x in nullable):
            _fail("NO_SETUP cannot contain operational values")
    else:
        _text(value["entry_condition"], "entry_condition")
        if any(value[x] is None for x in nullable):
            _fail("operational Setup requires every level")
        decimal = {x: Decimal(value[x]) for x in nullable}
        if not decimal["stop_level"] < decimal["entry_level"] < decimal["tp1"] < decimal["tp2"]:
            _fail("Setup levels are not numerically ordered")
        if decimal["rr_tp1"] < MINIMUM_RR_TP1:
            _fail("Setup RR1 is below 1.5")
    reasons = value["reason_codes"]
    if not isinstance(reasons, list) or not reasons or len(reasons) != len(set(reasons)):
        _fail("reason_codes must be a non-empty unique ordered array")
    for item in reasons:
        _text(item, "reason_code")
    _text(value["invalidation"], "invalidation")


def validate_json_artifact(
    artifact: Any, *, resolver: Callable[[str], Any] | None = None
) -> Any:
    """Validate every V1.4 structured Artifact and return its decoded value."""
    kind = artifact.artifact_kind
    if kind == "ledger.llm-raw-response.v1":
        _envelope(artifact, kind, IDENTITY, "application/json")
        return artifact.payload
    media = "application/schema+json" if kind == "ledger.llm-response-schema.v1" else "application/json"
    _envelope(artifact, kind, CANONICAL_JSON_V1, media)
    try:
        value = json.loads(artifact.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactIntegrityError(f"{kind} payload is invalid JSON") from exc
    validators = {
        "ledger.llm-context.v1": _validate_context,
        "ledger.llm-response-schema.v1": _validate_response_schema,
        "ledger.llm-invocation-parameters.v1": _validate_parameters,
        "ledger.code-manifest.v1": _validate_code_manifest,
        "ledger.usa-v2-invocation-config.v1": lambda item: _equal(item, CONFIG_DEFINITION, "config"),
        "ledger.llm-parsed-response.v1": _validate_parsed,
        "ledger.llm-invocation-request.v1": _validate_request,
        "ledger.llm-invocation-success.v1": _validate_success,
        "ledger.llm-error.v1": _validate_error,
        "ledger.usa-v2-stage-result.v1": _validate_stage,
        "ledger.usa-v2-facts.v2": _validate_facts_v2,
        "ledger.usa-v2-research-method-definition.v1": lambda item: _equal(item, RESEARCH_METHOD_DEFINITION, "Research method"),
        "ledger.usa-setup-v1-policy-definition.v1": lambda item: _equal(item, SETUP_POLICY_DEFINITION, "Setup policy"),
        "ledger.usa-setup-v1-result.v1": validate_setup_result,
        "ledger.usa-v2-research-result.v1": _validate_research_result,
    }
    validator = validators.get(kind)
    if validator is None:
        raise ArtifactIntegrityError(f"unsupported V1.4 Artifact kind: {kind}")
    validator(value)
    if resolver is not None:
        _validate_references(kind, value, resolver)
    return value


def validate_effective_prompt(artifact: Any) -> None:
    _envelope(
        artifact, "ledger.llm-effective-prompt.v1", IDENTITY,
        "text/plain;charset=utf-8",
    )
    try:
        artifact.payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactIntegrityError("effective prompt must be exact UTF-8 bytes") from exc


def _validate_context(value: Any) -> None:
    _closed(value, {"schema_name", "schema_version", "interaction_role", "items"}, "LLM context")
    _identity(value, "ledger.llm-context")
    role = _enum(value["interaction_role"], ("ANALYST", "CRITIC"), "interaction_role")
    items = value["items"]
    if not isinstance(items, list):
        _fail("context items must be an array")
    expected = ["FACTS"] if role == "ANALYST" else ["FACTS", "ANALYST_EFFECTIVE_STAGE_RESULT"]
    if [item.get("context_role") if isinstance(item, dict) else None for item in items] != expected:
        _fail("context roles/order differ from frozen contract")
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        _closed(item, {"context_role", "artifact_id", "derivation_node_id"}, "context item")
        _artifact_id(item["artifact_id"])
        _text(item["derivation_node_id"], "derivation_node_id")
        key = (item["context_role"], item["artifact_id"], item["derivation_node_id"])
        if key in seen:
            _fail("duplicate context item")
        seen.add(key)


def _validate_response_schema(value: Any) -> None:
    if value not in (analyst_response_schema(), critic_response_schema()):
        _fail("response schema is not an exact frozen Analyst/Critic singleton")


def _validate_parameters(value: Any) -> None:
    keys = {
        "schema_name", "schema_version", "provider", "transport", "model",
        "model_version", "timeout_seconds", "sandbox", "ephemeral",
        "skip_git_repo_check", "ignore_user_config", "response_schema_artifact_id",
        "temperature", "top_p", "seed", "max_output_tokens", "reasoning_effort",
        "tool_mode",
    }
    _closed(value, keys, "invocation parameters")
    _identity(value, "ledger.llm-invocation-parameters")
    expected = {
        "provider": "OPENAI_CODEX_CLI", "transport": "CODEX_CLI_STDIN",
        "model": "gpt-5.6-sol", "model_version": "codex-cli:gpt-5.6-sol",
        "timeout_seconds": 240, "sandbox": "read-only", "ephemeral": True,
        "skip_git_repo_check": True, "ignore_user_config": True,
        "temperature": None, "top_p": None, "seed": None,
        "max_output_tokens": None, "reasoning_effort": None, "tool_mode": "NONE",
    }
    if any(value[key] != item for key, item in expected.items()):
        _fail("invocation parameters differ from frozen Codex profile")
    _artifact_id(value["response_schema_artifact_id"])


def _validate_code_manifest(value: Any) -> None:
    _closed(value, {"schema_name", "schema_version", "repository", "git_commit", "source_files"}, "code manifest")
    _identity(value, "ledger.code-manifest")
    if value["repository"] != "earnings-trade-backtest" or not re.fullmatch(r"[0-9a-f]{40}", value["git_commit"] or ""):
        _fail("invalid code manifest identity")
    paths = ["trinity/italia_real.py", "trinity/italia_v1.py", "trinity/usa_v2.py"]
    files = value["source_files"]
    if not isinstance(files, list) or [x.get("path") if isinstance(x, dict) else None for x in files] != paths:
        _fail("code manifest source paths/order differ")
    for item in files:
        _closed(item, {"path", "sha256"}, "source file")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            _fail("invalid source-file SHA-256")


def _validate_parsed(value: Any) -> None:
    _closed(value, {"schema_name", "schema_version", "interaction_role", "raw_response_artifact_id", "response_schema_artifact_id", "parsed_value"}, "parsed response")
    _identity(value, "ledger.llm-parsed-response")
    role = _enum(value["interaction_role"], ("ANALYST", "CRITIC"), "interaction_role")
    _artifact_id(value["raw_response_artifact_id"])
    _artifact_id(value["response_schema_artifact_id"])
    _validate_role_value(role, value["parsed_value"])


def _validate_request(value: Any) -> None:
    keys = {
        "schema_name", "schema_version", "interaction_role", "provider", "model",
        "model_version", "client_effective_input_artifact_id", "context_artifact_id",
        "response_schema_artifact_id", "invocation_parameters_artifact_id",
        "code_manifest_artifact_id", "config_artifact_id",
    }
    _closed(value, keys, "invocation request")
    _identity(value, "ledger.llm-invocation-request")
    _enum(value["interaction_role"], ("ANALYST", "CRITIC"), "interaction_role")
    if (value["provider"], value["model"], value["model_version"]) != (
        "OPENAI_CODEX_CLI", "gpt-5.6-sol", "codex-cli:gpt-5.6-sol"
    ):
        _fail("invocation provider/model identity differs")
    for key in keys - {"schema_name", "schema_version", "interaction_role", "provider", "model", "model_version"}:
        _artifact_id(value[key])


def _validate_success(value: Any) -> None:
    _closed(value, {"schema_name", "schema_version", "llm_interaction_id", "raw_response_artifact_id", "parsed_response_artifact_id", "effective_stage_result_artifact_id"}, "LLM success")
    _identity(value, "ledger.llm-invocation-success")
    if not isinstance(value["llm_interaction_id"], str) or not UUID4_RE.fullmatch(value["llm_interaction_id"]):
        _fail("success interaction ID must be UUIDv4")
    for key in ("raw_response_artifact_id", "parsed_response_artifact_id", "effective_stage_result_artifact_id"):
        _artifact_id(value[key])


def _validate_error(value: Any) -> None:
    keys = {"schema_name", "schema_version", "llm_interaction_id", "failure_phase", "error_code", "exit_code", "raw_response_artifact_id", "diagnostic_artifact_ids"}
    _closed(value, keys, "LLM error")
    _identity(value, "ledger.llm-error")
    if not isinstance(value["llm_interaction_id"], str) or not UUID4_RE.fullmatch(value["llm_interaction_id"]):
        _fail("error interaction ID must be UUIDv4")
    _enum(value["failure_phase"], ("PROVIDER_EXECUTION", "RAW_RESPONSE_READ", "JSON_PARSE", "SCHEMA_VALIDATION", "DETERMINISTIC_POST_VALIDATION"), "failure_phase")
    _enum(value["error_code"], ("PROVIDER_NONZERO_EXIT", "PROVIDER_TIMEOUT", "RAW_RESPONSE_MISSING", "RAW_RESPONSE_NOT_UTF8", "MALFORMED_JSON", "SCHEMA_INVALID", "POST_VALIDATION_FAILED"), "error_code")
    if value["exit_code"] is not None and (isinstance(value["exit_code"], bool) or not isinstance(value["exit_code"], int)):
        _fail("exit_code must be integer or null")
    if value["raw_response_artifact_id"] is not None:
        _artifact_id(value["raw_response_artifact_id"])
    _sorted_artifact_ids(value["diagnostic_artifact_ids"], "diagnostic_artifact_ids")


def _validate_stage(value: Any) -> None:
    _closed(value, {"schema_name", "schema_version", "llm_interaction_id", "interaction_role", "parsed_response_artifact_id", "result"}, "stage result")
    _identity(value, "ledger.usa-v2-stage-result")
    if not isinstance(value["llm_interaction_id"], str) or not UUID4_RE.fullmatch(value["llm_interaction_id"]):
        _fail("stage interaction ID must be UUIDv4")
    _artifact_id(value["parsed_response_artifact_id"])
    role = _enum(value["interaction_role"], ("ANALYST", "CRITIC"), "interaction_role")
    _validate_effective_stage(role, value["result"])


def _validate_role_value(role: str, value: Any) -> None:
    schema = analyst_response_schema() if role == "ANALYST" else critic_response_schema()
    required = set(schema["required"])
    _closed(value, required, f"{role} parsed value")
    if role == "ANALYST":
        _validate_analysis(value, includes_decision=True)
    else:
        _string_array(value["notes"], "notes")
        _enum(value["status"], STATUSES, "status")
        _enum(value["evidence_confidence"], LEVELS, "evidence_confidence")
        _enum(value["thesis_strength"], LEVELS, "thesis_strength")
        _event_assessments(value["event_assessments"])
        _validate_analysis(value["revised_analysis"], includes_decision=False)


def _validate_effective_stage(role: str, value: Any) -> None:
    if role == "ANALYST":
        keys = set(ANALYSIS_FIELDS) | {"proposed_status", "claim_refs", "evidence_confidence", "thesis_strength", "event_assessments"}
        _closed(value, keys, "Analyst stage")
        _validate_analysis(value, includes_decision=True)
    else:
        keys = {"notes", "status", "evidence_confidence", "thesis_strength", "event_assessments", "revised_analysis", "critic_status", "critic_thesis_strength", "critic_evidence_confidence"}
        _closed(value, keys, "Critic stage")
        _string_array(value["notes"], "notes")
        for field in ("status", "critic_status"):
            _enum(value[field], STATUSES, field)
        for field in ("evidence_confidence", "thesis_strength", "critic_thesis_strength", "critic_evidence_confidence"):
            _enum(value[field], LEVELS, field)
        _event_assessments(value["event_assessments"])
        _validate_analysis(value["revised_analysis"], includes_decision=False)


def _validate_analysis(value: Any, *, includes_decision: bool) -> None:
    for field in ANALYSIS_FIELDS[:5] + (ANALYSIS_FIELDS[7],):
        _text(value[field], field)
    for field in ("catalysts", "risks"):
        _string_array(value[field], field)
    _claim_refs(value["claim_refs"], include_errors=False)
    if includes_decision:
        _enum(value["proposed_status"], STATUSES, "proposed_status")
        _enum(value["evidence_confidence"], LEVELS, "evidence_confidence")
        _enum(value["thesis_strength"], LEVELS, "thesis_strength")
        _event_assessments(value["event_assessments"])


def _validate_research_result(value: Any) -> None:
    keys = {
        "company_name", "ticker", "isin", "as_of", "facts", *ANALYSIS_FIELDS,
        "status", "confidence", "evidence", "critic_notes", "model_version",
        "prompt_version", "claim_refs", "rejected_claim_refs",
        "evidence_confidence", "thesis_strength", "analyst_status",
        "analyst_thesis_strength", "analyst_evidence_confidence", "critic_status",
        "critic_thesis_strength", "critic_evidence_confidence", "event_assessments",
    }
    _closed(value, keys, "Research result")
    if "thesis_id" in value or "created_at" in value:
        _fail("runtime Research identity is forbidden in semantic content")
    for field in ("company_name", "ticker", "model_version"):
        _text(value[field], field)
    if value["isin"] is not None:
        _text(value["isin"], "isin")
    if not isinstance(value["as_of"], str) or not DATE_RE.fullmatch(value["as_of"]):
        _fail("Research as_of must be ISO date")
    for field in ANALYSIS_FIELDS[:5] + (ANALYSIS_FIELDS[7],):
        _text(value[field], field)
    for field in ("catalysts", "risks", "critic_notes"):
        _string_array(value[field], field)
    for field in ("status", "analyst_status", "critic_status"):
        _enum(value[field], STATUSES, field)
    for field in ("confidence", "evidence_confidence", "thesis_strength", "analyst_thesis_strength", "analyst_evidence_confidence", "critic_thesis_strength", "critic_evidence_confidence"):
        _enum(value[field], LEVELS, field)
    if value["confidence"] != value["evidence_confidence"] or value["prompt_version"] != "TRINITY_ITALIA_V1_CONTRACT":
        _fail("Research confidence/prompt contract differs")
    _claim_refs(value["claim_refs"], include_errors=True, errors_empty=True)
    _claim_refs(value["rejected_claim_refs"], include_errors=True, errors_empty=False)
    if value["facts"].get("facts_contract_version") == "2":
        _validate_facts_v2(value["facts"])
    else:
        _validate_facts(value["facts"])
    if value["evidence"] != value["facts"]["evidence"]:
        _fail("Research evidence must exactly copy facts.evidence")
    assessments = _event_assessments(value["event_assessments"])
    event_ids = [event["facts"]["evidence_identifier"] for event in value["facts"]["events"]]
    if [item["event_id"] for item in assessments] != event_ids:
        _fail("event assessments must exactly follow facts.events")


def _validate_facts(value: Any) -> None:
    _validate_facts_contract(value, v2=False)


def _validate_facts_v2(value: Any) -> None:
    _validate_facts_contract(value, v2=True)


def _validate_facts_contract(value: Any, *, v2: bool) -> None:
    keys = {"identity", "price", "fundamentals", "financial_facts", "events", "evidence", "missing_fields"}
    if v2:
        keys.add("facts_contract_version")
    _closed(value, keys, "USA facts")
    if v2 and value["facts_contract_version"] != "2":
        _fail("USA facts V2 version marker differs")
    identity = value["identity"]
    _closed(identity, {"company_name", "ticker", "provider_symbol", "isin", "as_of", "schema_type"}, "identity")
    _text(identity["company_name"], "company_name"); _text(identity["ticker"], "ticker")
    for field in ("provider_symbol", "isin", "schema_type"):
        if identity[field] is not None: _text(identity[field], field)
    if not isinstance(identity["as_of"], str) or not DATE_RE.fullmatch(identity["as_of"]): _fail("identity as_of invalid")
    price = value["price"]
    _closed(price, {"current_price", "return_5d", "return_20d", "return_60d", "trend", "indicators", "published_at", "acquisition"}, "price")
    for field in ("current_price", "return_5d", "return_20d", "return_60d"):
        _decimal_or_null(price[field], field)
    if price["trend"] is not None: _text(price["trend"], "trend")
    if price["indicators"] != {}: _fail("USA V2 indicators must be empty")
    if price["published_at"] is not None and (not isinstance(price["published_at"], str) or not DATE_RE.fullmatch(price["published_at"])): _fail("price published_at invalid")
    _validate_acquisition(price["acquisition"])
    fundamentals = value["fundamentals"]
    _closed(fundamentals, {"revenue", "revenue_growth", "operating_margin", "net_income", "free_cash_flow", "net_debt", "valuation_metrics", "published_at"}, "fundamentals")
    for field in ("revenue", "revenue_growth", "operating_margin", "net_income", "free_cash_flow", "net_debt"): _decimal_or_null(fundamentals[field], field)
    if fundamentals["valuation_metrics"] != {} or value["financial_facts"] != {}: _fail("USA V2 financial maps must be empty")
    if fundamentals["published_at"] is not None and (not isinstance(fundamentals["published_at"], str) or not DATE_RE.fullmatch(fundamentals["published_at"])): _fail("fundamentals published_at invalid")
    if not isinstance(value["events"], list): _fail("events must be an array")
    fact_ids: set[str] = set()
    for event in value["events"]:
        (_validate_event_v2 if v2 else _validate_event)(event, fact_ids)
    _validate_evidence(value["evidence"])
    _unique_strings(value["missing_fields"], "missing_fields")


def _validate_acquisition(value: Any) -> None:
    _closed(value, {"currency", "market", "provider_symbol", "last_bar", "reference_bars", "raw_path", "sha256", "fact_metadata"}, "price acquisition")
    if (value["currency"], value["market"]) != ("USD", "USA"): _fail("price acquisition market differs")
    for field in ("provider_symbol", "raw_path"): _text(value[field], field)
    if not isinstance(value["sha256"], str) or not SHA256_RE.fullmatch(value["sha256"]): _fail("price SHA-256 invalid")
    bar = value["last_bar"]
    _closed(bar, {"date", "open", "high", "low", "close", "adjusted_close", "volume"}, "last_bar")
    if not isinstance(bar["date"], str) or not DATE_RE.fullmatch(bar["date"]): _fail("bar date invalid")
    for field in ("open", "high", "low", "close", "volume"): _decimal(bar[field], field)
    _decimal_or_null(bar["adjusted_close"], "adjusted_close")
    refs = value["reference_bars"]
    _closed(refs, {"5", "20", "60"}, "reference_bars")
    for item in refs.values():
        _closed(item, {"date", "close"}, "reference bar"); _decimal(item["close"], "reference close")
        if not isinstance(item["date"], str) or not DATE_RE.fullmatch(item["date"]): _fail("reference date invalid")
    metadata = value["fact_metadata"]
    if not isinstance(metadata, list): _fail("fact_metadata must be array")
    expected_metrics = ["current_price", "return_5d", "return_20d", "return_60d"]
    if [item.get("metric") if isinstance(item, dict) else None for item in metadata] != expected_metrics: _fail("fact_metadata order differs")
    for item in metadata:
        _closed(item, {"fact_id", "ticker", "metric", "value_path", "unit", "scale", "period", "evidence_id", "extraction_method", "display_precision"}, "fact metadata")
        for field in ("fact_id", "ticker", "metric", "value_path", "unit", "period", "evidence_id", "extraction_method"): _text(item[field], field)
        if item["value_path"] != f"price.{item['metric']}" or item["extraction_method"] != "deterministic_cached_ohlcv": _fail("fact metadata semantics differ")
        _nonnegative_int(item["display_precision"], "display_precision")
        if not isinstance(item["scale"], int) or isinstance(item["scale"], bool) or item["scale"] <= 0: _fail("scale invalid")


def _validate_event(event: Any, fact_ids: set[str]) -> None:
    _closed(event, {"kind", "published_at", "summary", "facts", "guidance"}, "event")
    _text(event["kind"], "event kind"); _text(event["summary"], "event summary")
    if not isinstance(event["published_at"], str) or not DATE_RE.fullmatch(event["published_at"]): _fail("event date invalid")
    if event["guidance"] is not None: _text(event["guidance"], "guidance")
    facts = event["facts"]
    keys = {"evidence_identifier", "novelty", "issuer_release", "primary_source", "reported_metrics", "guidance_ranges", "guidance_language", "guidance_changes", "structured_sec_facts"}
    _closed(facts, keys, "event facts"); _text(facts["evidence_identifier"], "evidence_identifier")
    novelty = facts["novelty"]
    if novelty is not None: _enum(novelty, ("NEW_RELEASE", "REITERATION", "NEW_PRIMARY_DOCUMENT"), "novelty")
    if facts["issuer_release"] not in (True, None) or facts["primary_source"] not in (True, None): _fail("event source flags invalid")
    if facts["issuer_release"] is True:
        if novelty not in ("NEW_RELEASE", "REITERATION") or facts["primary_source"] is not None or facts["structured_sec_facts"] != []: _fail("cached-release branch invalid")
    elif event["kind"] == "RESULTS":
        if novelty != "NEW_PRIMARY_DOCUMENT" or facts["primary_source"] is not True: _fail("SEC earnings branch invalid")
    elif event["kind"] == "SEC_PERIODIC_REPORT":
        if novelty is not None or facts["primary_source"] is not True or any(facts[x] for x in ("reported_metrics", "guidance_ranges", "guidance_changes")) or facts["guidance_language"] is not None: _fail("SEC periodic branch invalid")
    else:
        if novelty is not None or facts["primary_source"] is not True or any(facts[x] for x in ("reported_metrics", "guidance_ranges", "guidance_changes", "structured_sec_facts")) or facts["guidance_language"] is not None: _fail("SEC material branch invalid")
    for name, validator in (("reported_metrics", _reported_metric), ("guidance_ranges", _guidance_range), ("guidance_changes", _guidance_change), ("structured_sec_facts", _structured_fact)):
        items = facts[name]
        if not isinstance(items, list): _fail(f"{name} must be array")
        for item in items:
            validator(item); fact_id = item["fact_id"]
            if fact_id in fact_ids: _fail("duplicate fact_id")
            fact_ids.add(fact_id)
    language = facts["guidance_language"]
    if language is not None:
        _closed(language, {"source_excerpt", "evidence_identifier", "extraction_method"}, "guidance language")
        for field in language: _text(language[field], field)
        if language["extraction_method"] != "literal_guidance_language": _fail("guidance-language method invalid")


def _validate_event_v2(event: Any, fact_ids: set[str]) -> None:
    facts = event.get("facts") if isinstance(event, dict) else None
    if not isinstance(facts, dict):
        _fail("event facts must be an object")
    keys = {
        "evidence_identifier", "novelty", "issuer_release", "primary_source",
        "reported_metrics", "guidance_ranges", "guidance_language",
        "guidance_changes", "structured_sec_facts", "source_class",
        "confirmation_state",
    }
    _closed(facts, keys, "event facts V2")
    source_class = _enum(
        facts["source_class"],
        ("PRIMARY_SEC", "ISSUER_RELEASE", "THIRD_PARTY_REPORT"),
        "source_class",
    )
    confirmation = _enum(
        facts["confirmation_state"],
        ("CONFIRMED", "REPORTED", "RUMORED", "UNKNOWN"),
        "confirmation_state",
    )
    if source_class == "THIRD_PARTY_REPORT":
        _closed(event, {"kind", "published_at", "summary", "facts", "guidance"}, "event")
        if event["kind"] != "CORPORATE_EVENT":
            _fail("third-party report must be a corporate event")
        if confirmation not in {"REPORTED", "RUMORED"}:
            _fail("third-party report confirmation state invalid")
        if facts["issuer_release"] is not None or facts["primary_source"] is not None:
            _fail("third-party report source flags invalid")
        if facts["novelty"] not in {"NEW_RELEASE", "REITERATION"}:
            _fail("third-party report novelty invalid")
        if any(facts[name] for name in (
            "reported_metrics", "guidance_ranges", "guidance_changes",
            "structured_sec_facts",
        )) or facts["guidance_language"] is not None or event["guidance"] is not None:
            _fail("third-party report may not create structured primary facts")
        _text(facts["evidence_identifier"], "evidence_identifier")
        _text(event["summary"], "event summary")
        if not isinstance(event["published_at"], str) or not DATE_RE.fullmatch(event["published_at"]):
            _fail("event date invalid")
        return
    if confirmation != "CONFIRMED":
        _fail("primary or issuer event must be confirmed")
    if source_class == "PRIMARY_SEC" and facts["primary_source"] is not True:
        _fail("PRIMARY_SEC event lacks primary source flag")
    if source_class == "ISSUER_RELEASE" and facts["issuer_release"] is not True:
        _fail("ISSUER_RELEASE event lacks issuer release flag")
    legacy = json.loads(json.dumps(event))
    legacy["facts"].pop("source_class")
    legacy["facts"].pop("confirmation_state")
    _validate_event(legacy, fact_ids)


def _reported_metric(item: Any) -> None:
    keys = {"fact_id", "ticker", "metric", "metric_label", "period", "evidence_id", "evidence_identifier", "source_excerpt", "extraction_method", "value", "unit", "measurement_type", "scale", "display_precision"}
    _closed(item, keys, "reported metric")
    for field in keys - {"scale", "display_precision", "value"}: _text(item[field], field)
    _decimal(item["value"], "reported value")
    _enum(item["unit"], ("USD_MILLIONS", "USD_BILLIONS", "USD_TRILLIONS"), "unit")
    _enum(item["measurement_type"], ("REPORTED_AMOUNT", "CHANGE_AMOUNT", "ANNUALIZED_RUN_RATE"), "measurement_type")
    if item["metric"] != item["metric_label"] or item["evidence_id"] != item["evidence_identifier"] or item["extraction_method"] != "literal_regex_explicit_currency_and_scale": _fail("reported metric linkage differs")
    if item["scale"] not in (1_000_000, 1_000_000_000, 1_000_000_000_000): _fail("reported scale invalid")
    _nonnegative_int(item["display_precision"], "display_precision")


def _guidance_range(item: Any) -> None:
    keys = {"fact_id", "ticker", "metric", "period", "unit", "evidence_id", "evidence_identifier", "source_excerpt", "extraction_method", "previous_range", "current_range", "value", "scale", "display_precision"}
    _closed(item, keys, "guidance range")
    for field in keys - {"previous_range", "current_range", "value", "scale", "display_precision"}: _text(item[field], field)
    _enum(item["metric"], ("ADJUSTED_EPS", "CORE_FFO"), "metric")
    if item["unit"] != "USD_PER_SHARE" or item["evidence_id"] != item["evidence_identifier"] or item["extraction_method"] != "literal_guidance_range_explicit_usd_per_share" or item["scale"] != 1 or item["display_precision"] is not None: _fail("guidance range semantics differ")
    _range(item["current_range"])
    if item["previous_range"] is not None: _range(item["previous_range"])
    if item["value"] != item["current_range"]: _fail("guidance range value differs")


def _guidance_change(item: Any) -> None:
    keys = {"fact_id", "ticker", "metric", "period", "unit", "evidence_id", "evidence_identifier", "previous_evidence_identifier", "current_evidence_identifier", "extraction_method", "previous_range", "current_range", "direction", "value", "scale", "display_precision"}
    _closed(item, keys, "guidance change")
    for field in keys - {"previous_range", "current_range", "scale", "display_precision"}: _text(item[field], field)
    _range(item["previous_range"]); _range(item["current_range"])
    _enum(item["direction"], ("RAISED", "LOWERED", "UNCHANGED", "MIXED"), "direction")
    if item["unit"] != "USD_PER_SHARE" or item["value"] != item["direction"] or item["evidence_id"] != item["current_evidence_identifier"] or item["evidence_identifier"] != item["current_evidence_identifier"] or item["extraction_method"] != "deterministic_same_metric_period_unit_range_comparison" or item["scale"] != 1 or item["display_precision"] is not None: _fail("guidance-change semantics differ")


def _structured_fact(item: Any) -> None:
    keys = {"fact_id", "ticker", "metric", "period", "unit", "source", "evidence_id", "evidence_identifier", "accession_number", "extraction_method", "value", "scale", "display_precision", "xbrl_tag", "comparison_period", "components"}
    _closed(item, keys, "structured SEC fact")
    for field in keys - {"value", "scale", "display_precision", "xbrl_tag", "comparison_period", "components"}: _text(item[field], field)
    _decimal(item["value"], "SEC value"); _nonnegative_int(item["display_precision"], "display_precision")
    if not isinstance(item["scale"], int) or isinstance(item["scale"], bool) or item["scale"] <= 0 or item["source"] != "SEC XBRL companyfacts" or item["evidence_id"] != item["evidence_identifier"]: _fail("SEC fact provenance invalid")
    method = item["extraction_method"]
    direct = {"revenue", "operating_income", "net_income", "eps_diluted", "operating_cash_flow", "capex", "cash", "debt"}
    if method == "sec_xbrl_exact_accession":
        if item["metric"] not in direct or not item["xbrl_tag"] or item["comparison_period"] is not None or item["components"] is not None: _fail("direct XBRL shape invalid")
    elif method == "deterministic_same_tag_same_duration_year_over_year":
        if item["metric"] != "revenue_growth" or item["unit"] != "PERCENT" or not item["xbrl_tag"] or not item["comparison_period"] or item["components"] is not None: _fail("growth fact shape invalid")
    elif method in ("deterministic_operating_income_divided_by_revenue", "deterministic_operating_cash_flow_minus_capex"):
        expected = "operating_margin" if "divided" in method else "free_cash_flow"
        if item["metric"] != expected or item["xbrl_tag"] is not None or item["comparison_period"] is not None or not isinstance(item["components"], list) or len(item["components"]) != 2 or len(set(item["components"])) != 2: _fail("derived SEC fact shape invalid")
        for component in item["components"]: _text(component, "component")
    else: _fail("unsupported SEC extraction method")


def _validate_evidence(items: Any) -> None:
    keys = {"source", "published_at", "identifier", "url", "excerpt", "title", "retrieved_at", "content_sha256", "raw_path", "published_at_precision", "document_kind", "filing_date", "accepted_at", "form", "accession_number"}
    if not isinstance(items, list): _fail("evidence must be array")
    seen: set[str] = set()
    for item in items:
        _closed(item, keys, "evidence")
        _text(item["source"], "source")
        if not isinstance(item["published_at"], str) or not DATE_RE.fullmatch(item["published_at"]): _fail("evidence date invalid")
        if item["identifier"] is None and item["url"] is None: _fail("evidence needs identity")
        for field in keys - {"source", "published_at"}:
            if item[field] is not None: _text(item[field], field)
        if item["identifier"] is not None:
            if item["identifier"] in seen: _fail("duplicate evidence identifier")
            seen.add(item["identifier"])
        if item["content_sha256"] is not None and not SHA256_RE.fullmatch(item["content_sha256"]): _fail("evidence hash invalid")


def _validate_references(kind: str, value: Any, resolver: Callable[[str], Any]) -> None:
    mapping: dict[str, dict[str, str]] = {
        "ledger.llm-invocation-request.v1": {
            "client_effective_input_artifact_id": "ledger.llm-effective-prompt.v1",
            "context_artifact_id": "ledger.llm-context.v1",
            "response_schema_artifact_id": "ledger.llm-response-schema.v1",
            "invocation_parameters_artifact_id": "ledger.llm-invocation-parameters.v1",
            "code_manifest_artifact_id": "ledger.code-manifest.v1",
            "config_artifact_id": "ledger.usa-v2-invocation-config.v1",
        },
        "ledger.llm-invocation-success.v1": {
            "raw_response_artifact_id": "ledger.llm-raw-response.v1",
            "parsed_response_artifact_id": "ledger.llm-parsed-response.v1",
            "effective_stage_result_artifact_id": "ledger.usa-v2-stage-result.v1",
        },
        "ledger.llm-parsed-response.v1": {
            "raw_response_artifact_id": "ledger.llm-raw-response.v1",
            "response_schema_artifact_id": "ledger.llm-response-schema.v1",
        },
        "ledger.usa-v2-stage-result.v1": {
            "parsed_response_artifact_id": "ledger.llm-parsed-response.v1",
        },
    }
    for field, expected in mapping.get(kind, {}).items():
        if resolver(value[field]).artifact_kind != expected: _fail(f"{field} targets wrong Artifact kind")
    if kind == "ledger.llm-context.v1":
        for item in value["items"]: resolver(item["artifact_id"])
    if kind == "ledger.llm-invocation-parameters.v1" and resolver(value["response_schema_artifact_id"]).artifact_kind != "ledger.llm-response-schema.v1": _fail("parameter schema target invalid")
    if kind == "ledger.llm-error.v1":
        if value["raw_response_artifact_id"] is not None and resolver(value["raw_response_artifact_id"]).artifact_kind != "ledger.llm-raw-response.v1": _fail("error raw response target invalid")
        for item in value["diagnostic_artifact_ids"]: resolver(item)


def analyst_response_schema() -> dict[str, Any]:
    claim = _claim_schema()
    event = _event_schema()
    return {
        "type": "object", "additionalProperties": False,
        "required": [*ANALYSIS_FIELDS, "proposed_status", "claim_refs", "evidence_confidence", "thesis_strength", "event_assessments"],
        "properties": {
            **{field: ({"type": "array", "items": {"type": "string"}} if field in {"catalysts", "risks"} else {"type": "string"}) for field in ANALYSIS_FIELDS},
            "proposed_status": {"type": "string", "enum": list(STATUSES)},
            "claim_refs": {"type": "array", "items": claim},
            "evidence_confidence": {"type": "string", "enum": list(LEVELS)},
            "thesis_strength": {"type": "string", "enum": list(LEVELS)},
            "event_assessments": {"type": "array", "items": event},
        },
    }


def critic_response_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["notes", "status", "evidence_confidence", "thesis_strength", "event_assessments", "revised_analysis"],
        "properties": {
            "notes": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": list(STATUSES)},
            "evidence_confidence": {"type": "string", "enum": list(LEVELS)},
            "thesis_strength": {"type": "string", "enum": list(LEVELS)},
            "event_assessments": {"type": "array", "items": _event_schema()},
            "revised_analysis": {
                "type": "object", "additionalProperties": False,
                "required": [*ANALYSIS_FIELDS, "claim_refs"],
                "properties": {
                    **{field: ({"type": "array", "items": {"type": "string"}} if field in {"catalysts", "risks"} else {"type": "string"}) for field in ANALYSIS_FIELDS},
                    "claim_refs": {"type": "array", "items": _claim_schema()},
                },
            },
        },
    }


def _claim_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["claim_id", "field", "text", "fact_ids", "materiality", "period"], "properties": {"claim_id": {"type": "string"}, "field": {"type": "string", "enum": list(ANALYSIS_FIELDS)}, "text": {"type": "string"}, "fact_ids": {"type": "array", "items": {"type": "string"}}, "materiality": {"type": "string", "enum": ["MATERIAL", "SUPPORTING"]}, "period": {"type": ["string", "null"]}}}


def _event_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["event_id", "classification", "material", "rationale"], "properties": {"event_id": {"type": "string"}, "classification": {"type": "string", "enum": list(EVENT_CLASSES)}, "material": {"type": "boolean"}, "rationale": {"type": "string"}}}


def _claim_refs(items: Any, *, include_errors: bool, errors_empty: bool | None = None) -> None:
    if not isinstance(items, list): _fail("claim_refs must be an array")
    seen: set[str] = set()
    expected = {"claim_id", "field", "text", "fact_ids", "materiality", "period"}
    if include_errors: expected.add("validation_errors")
    for item in items:
        _closed(item, expected, "claim")
        for field in ("claim_id", "field", "text", "materiality"): _text(item[field], field)
        _enum(item["field"], ANALYSIS_FIELDS, "claim field"); _enum(item["materiality"], ("MATERIAL", "SUPPORTING"), "materiality")
        _string_array(item["fact_ids"], "fact_ids", unique=False)
        if item["period"] is not None: _text(item["period"], "period")
        if include_errors:
            errors = _unique_strings(item["validation_errors"], "validation_errors")
            if errors != sorted(errors): _fail("validation_errors must be ASCII sorted")
            if errors_empty is True and errors: _fail("valid claim has errors")
            if errors_empty is False and not errors: _fail("rejected claim needs errors")
        if include_errors and errors_empty is True:
            if item["claim_id"] in seen: _fail("duplicate valid claim ID")
            seen.add(item["claim_id"])


def _event_assessments(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list): _fail("event_assessments must be array")
    seen: set[str] = set()
    for item in items:
        _closed(item, {"event_id", "classification", "material", "rationale"}, "event assessment")
        _text(item["event_id"], "event_id"); _enum(item["classification"], EVENT_CLASSES, "classification"); _text(item["rationale"], "rationale")
        if not isinstance(item["material"], bool): _fail("event material must be boolean")
        if item["event_id"] in seen: _fail("duplicate event assessment")
        seen.add(item["event_id"])
    return items


def _envelope(artifact: Any, kind: str, canonicalization: str, media_type: str) -> None:
    if (artifact.artifact_kind, artifact.canonicalization_version, artifact.media_type) != (kind, canonicalization, media_type):
        _fail(f"{kind} envelope differs from frozen contract")


def _closed(value: Any, keys: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{name} must contain exactly {sorted(keys)}")


def _identity(value: Mapping[str, Any], schema_name: str) -> None:
    if value.get("schema_name") != schema_name or value.get("schema_version") != "1":
        _fail(f"{schema_name} identity differs")


def _equal(value: Any, expected: Any, name: str) -> None:
    if value != expected: _fail(f"{name} differs from frozen singleton")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value: _fail(f"{name} must be non-empty text")
    return value


def _enum(value: Any, values: tuple[str, ...], name: str) -> str:
    if value not in values: _fail(f"{name} has unsupported value")
    return value


def _artifact_id(value: Any) -> str:
    if not isinstance(value, str) or not ARTIFACT_ID_RE.fullmatch(value): _fail("invalid Artifact ID")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value): _fail(f"{name} must be DECIMAL")
    try: return Decimal(value)
    except InvalidOperation as exc: raise ArtifactIntegrityError(f"{name} invalid") from exc


def _decimal_or_null(value: Any, name: str) -> None:
    if value is not None: _decimal(value, name)


def _range(value: Any) -> None:
    if not isinstance(value, list) or len(value) != 2: _fail("range must have two DECIMAL values")
    low, high = (_decimal(item, "range value") for item in value)
    if low > high: _fail("range low exceeds high")


def _nonnegative_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0: _fail(f"{name} must be nonnegative integer")


def _string_array(value: Any, name: str, *, unique: bool = False) -> list[str]:
    if not isinstance(value, list): _fail(f"{name} must be array")
    for item in value: _text(item, name)
    if unique and len(value) != len(set(value)): _fail(f"{name} must be unique")
    return value


def _unique_strings(value: Any, name: str) -> list[str]:
    return _string_array(value, name, unique=True)


def _sorted_artifact_ids(value: Any, name: str) -> None:
    if not isinstance(value, list) or value != sorted(set(value)): _fail(f"{name} must be sorted and unique")
    for item in value: _artifact_id(item)


def _fail(message: str) -> None:
    raise ArtifactIntegrityError(message)
