from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from trinity.ledger import (
    CONFIG_DEFINITION,
    RESEARCH_METHOD_DEFINITION,
    SETUP_POLICY_DEFINITION,
    ArtifactIntegrityError,
    DerivationParent,
    LedgerStorage,
    RunInputBinding,
    UnsupportedCanonicalValue,
    analyst_response_schema,
    canonical_decimal,
    canonicalize_json,
    critic_response_schema,
    pit_classification_policy_definition_v2,
    pit_classification_policy_definition_v1,
    research_result_value,
    setup_result_value,
    setup_rule_values,
)
from trinity.ledger.canonical import IDENTITY
from trinity.ledger.schema import (
    MigrationRegistry,
    core_migration,
    execution_coordination_migration,
    input_observation_migration,
    pit_classification_migration,
    research_llm_setup_migration,
    temporal_derivation_dag_migration,
)
from trinity.notifications.telegram import (
    load_committed_setup,
    render_telegram_message,
)
from trinity.usa_setup_v1 import build_setup


REFERENCE = "2026-09-12T00:00:00.000000Z"
SOURCE_TIME = "2026-09-13T08:00:00.000000Z"
ANALYST_TIME = "2026-09-13T09:00:00.000000Z"
CRITIC_TIME = "2026-09-13T10:00:00.000000Z"
CUTOFF = "2026-09-14T00:00:00.000000Z"
COMMIT = "eddd0495639e2d3c2cec53bc0f0b72e44654bdce"
ANALYSIS_FIELDS = (
    "fundamental_analysis", "earnings_and_news_analysis", "price_context",
    "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation",
)


def _artifact(storage: LedgerStorage, kind: str, value: object, *, schema=False):
    return storage.insert_json_artifact(
        artifact_type=kind, value=value,
        media_type="application/schema+json" if schema else "application/json",
    )


def _attempt(storage: LedgerStorage):
    request = storage.create_run_request(
        request_kind="RECONSTRUCTION", analysis_cutoff_at=CUTOFF,
        parameters={"historical_as_of_at": REFERENCE},
        idempotency_key="jnj-v14-fixture", requested_by="v14-integration-test",
        baseline_commit=COMMIT,
    )
    return storage.allocate_attempt(
        run_request_id=request.run_request_id, worker_identity="v14-fixture-worker",
        code_commit=COMMIT, environment_fingerprint="fixture:jnj:v1",
    )


def _observe(storage, attempt, artifact, record, when):
    observation = storage.create_input_observation(
        artifact_id=artifact.artifact_id, source_id="fixture:jnj:v1",
        source_record_key=record, source_published_at=None, retrieved_at=when,
        availability_basis="RETRIEVED_AT_FALLBACK", effective_available_at=when,
        observed_by_attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        source_metadata={
            "schema_version": "1", "provider": "fixture", "dataset_name": "jnj",
            "source_record_key": record, "acquisition_method": "checked-in-fixture",
            "availability_rule_id": "retrieval-fallback", "availability_rule_version": "1",
            "evidence_artifact_ids": [], "provider_metadata": {}, "future_effective_at": None,
        },
    )
    node = storage.create_raw_derivation_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        input_observation_id=observation.input_observation_id,
    )
    return observation, node


def _evidence(storage, attempt, artifact, observation, node, kind, support=()):
    if kind == "POST_REFERENCE_RECONSTRUCTION":
        verifier, rationale = "LEDGER_RECONSTRUCTION_EVIDENCE_V1", "POST_REFERENCE_RECONSTRUCTION_VERIFIED"
        reconstruction = SOURCE_TIME
        origin = "LEGACY_IMPORT"
        namespace, key = "legacy.jnj", "ohlcv-2026-09-12"
        legacy_id = "legacy:sha256:" + hashlib.sha256(canonicalize_json({
            "legacy_namespace": namespace, "legacy_record_key": key,
        })).hexdigest()
    else:
        verifier, rationale = "LEDGER_PIT_IRRELEVANCE_V1", "PIT_NOT_APPLICABLE_BY_ARTIFACT_KIND"
        reconstruction = None
        origin = "LEDGER_AUTHORIZED_CAPTURE"
        namespace = key = legacy_id = None
    value = {
        "schema_name": "ledger.pit-classification-evidence", "schema_version": "1",
        "derivation_node_id": node.derivation_node_id,
        "input_observation_id": observation.input_observation_id,
        "artifact_id": artifact.artifact_id, "pit_reference_at": REFERENCE,
        "record_origin_evidence_kind": origin, "legacy_namespace": namespace,
        "legacy_record_key": key, "legacy_record_id": legacy_id,
        "pit_evidence_kind": kind, "verifier_id": verifier,
        "archive_captured_at": None, "reconstruction_completed_at": reconstruction,
        "supporting_artifact_ids": sorted(support), "rationale_code": rationale,
    }
    evidence = _artifact(storage, "ledger.pit-classification-evidence.v1", value)
    for artifact_id in (*support, evidence.artifact_id):
        storage.attach_attempt_artifact(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=artifact_id, role="DIAGNOSTIC",
        )
    return evidence


def _classify_raw(storage, attempt, policy, artifact, observation, node, kind, support=()):
    evidence = _evidence(storage, attempt, artifact, observation, node, kind, support)
    return storage.create_raw_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=node.derivation_node_id, pit_reference_at=REFERENCE,
        classification_policy_id=policy.classification_policy_id,
        evidence_artifact_id=evidence.artifact_id,
    )


def _classify_derived(storage, attempt, policy, node, parents):
    return storage.create_derived_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=node.derivation_node_id, pit_reference_at=REFERENCE,
        classification_policy_id=policy.classification_policy_id,
        parent_classification_ids=tuple(x.derivation_node_classification_id for x in parents),
    )


def _code_manifest():
    root = Path(__file__).resolve().parents[3]
    paths = ["trinity/italia_real.py", "trinity/italia_v1.py", "trinity/usa_v2.py"]
    return {
        "schema_name": "ledger.code-manifest", "schema_version": "1",
        "repository": "earnings-trade-backtest", "git_commit": COMMIT,
        "source_files": [{"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()} for path in paths],
    }


def _claim_without_errors(item):
    return {key: value for key, value in item.items() if key != "validation_errors"}


def _role_values(thesis):
    analyst = {field: thesis[field] for field in ANALYSIS_FIELDS}
    analyst.update({
        "proposed_status": thesis["analyst_status"],
        "claim_refs": [_claim_without_errors(item) for item in thesis["claim_refs"]],
        "evidence_confidence": thesis["analyst_evidence_confidence"],
        "thesis_strength": thesis["analyst_thesis_strength"],
        "event_assessments": thesis["event_assessments"],
    })
    revised = {field: thesis[field] for field in ANALYSIS_FIELDS}
    revised["claim_refs"] = [_claim_without_errors(item) for item in thesis["claim_refs"]]
    critic = {
        "notes": thesis["critic_notes"], "status": thesis["status"],
        "evidence_confidence": thesis["evidence_confidence"],
        "thesis_strength": thesis["thesis_strength"],
        "event_assessments": thesis["event_assessments"], "revised_analysis": revised,
    }
    critic_stage = {**critic, "critic_status": thesis["critic_status"],
                    "critic_thesis_strength": thesis["critic_thesis_strength"],
                    "critic_evidence_confidence": thesis["critic_evidence_confidence"]}
    return analyst, critic, critic_stage


def _interaction(storage, attempt, *, role, ordinal, facts_artifact, facts_node,
                 parsed_value, stage_value, finished_at, analyst=None,
                 raw_bytes_override=None, error_code=None):
    schema_value = analyst_response_schema() if role == "ANALYST" else critic_response_schema()
    schema = _artifact(storage, "ledger.llm-response-schema.v1", schema_value, schema=True)
    prompt = storage.insert_artifact(
        artifact_type="ledger.llm-effective-prompt.v1", canonicalization_version=IDENTITY,
        payload=f"{role} JNJ deterministic fixture\n".encode(), media_type="text/plain;charset=utf-8",
    )
    context_items = [{"context_role": "FACTS", "artifact_id": facts_artifact.artifact_id,
                      "derivation_node_id": facts_node.derivation_node_id}]
    if analyst is not None:
        context_items.append({"context_role": "ANALYST_EFFECTIVE_STAGE_RESULT",
                              "artifact_id": analyst["stage_artifact"].artifact_id,
                              "derivation_node_id": analyst["stage_node"].derivation_node_id})
    context = _artifact(storage, "ledger.llm-context.v1", {
        "schema_name": "ledger.llm-context", "schema_version": "1",
        "interaction_role": role, "items": context_items,
    })
    parameters = _artifact(storage, "ledger.llm-invocation-parameters.v1", {
        "schema_name": "ledger.llm-invocation-parameters", "schema_version": "1",
        "provider": "OPENAI_CODEX_CLI", "transport": "CODEX_CLI_STDIN",
        "model": "gpt-5.6-sol", "model_version": "codex-cli:gpt-5.6-sol",
        "timeout_seconds": 240, "sandbox": "read-only", "ephemeral": True,
        "skip_git_repo_check": True, "ignore_user_config": True,
        "response_schema_artifact_id": schema.artifact_id, "temperature": None,
        "top_p": None, "seed": None, "max_output_tokens": None,
        "reasoning_effort": None, "tool_mode": "NONE",
    })
    code = _artifact(storage, "ledger.code-manifest.v1", _code_manifest())
    config = _artifact(storage, "ledger.usa-v2-invocation-config.v1", CONFIG_DEFINITION)
    request = _artifact(storage, "ledger.llm-invocation-request.v1", {
        "schema_name": "ledger.llm-invocation-request", "schema_version": "1",
        "interaction_role": role, "provider": "OPENAI_CODEX_CLI",
        "model": "gpt-5.6-sol", "model_version": "codex-cli:gpt-5.6-sol",
        "client_effective_input_artifact_id": prompt.artifact_id,
        "context_artifact_id": context.artifact_id,
        "response_schema_artifact_id": schema.artifact_id,
        "invocation_parameters_artifact_id": parameters.artifact_id,
        "code_manifest_artifact_id": code.artifact_id, "config_artifact_id": config.artifact_id,
    })
    interaction_id = str(uuid.uuid4())
    raw_bytes = canonicalize_json(parsed_value) if raw_bytes_override is None else raw_bytes_override
    raw = storage.insert_artifact(
        artifact_type="ledger.llm-raw-response.v1", canonicalization_version=IDENTITY,
        payload=raw_bytes, media_type="application/json",
    )
    observation, raw_node = _observe(storage, attempt, raw, f"{role.lower()}-result.json", finished_at)
    if error_code is not None:
        phase = {
            "RAW_RESPONSE_NOT_UTF8": "RAW_RESPONSE_READ",
            "MALFORMED_JSON": "JSON_PARSE",
            "SCHEMA_INVALID": "SCHEMA_VALIDATION",
            "POST_VALIDATION_FAILED": "DETERMINISTIC_POST_VALIDATION",
        }[error_code]
        error = _artifact(storage, "ledger.llm-error.v1", {
            "schema_name": "ledger.llm-error", "schema_version": "1",
            "llm_interaction_id": interaction_id, "failure_phase": phase,
            "error_code": error_code, "exit_code": None,
            "raw_response_artifact_id": raw.artifact_id,
            "diagnostic_artifact_ids": [],
        })
        interaction = storage.create_llm_interaction(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token, ordinal=ordinal,
            request_artifact_id=request.artifact_id, response_artifact_id=None,
            error_artifact_id=error.artifact_id, status="FAILED", started_at=SOURCE_TIME,
            finished_at=finished_at, llm_interaction_id=interaction_id,
        )
        return {"interaction": interaction, "raw": raw, "observation": observation,
                "raw_node": raw_node, "error_artifact": error}
    parsed = _artifact(storage, "ledger.llm-parsed-response.v1", {
        "schema_name": "ledger.llm-parsed-response", "schema_version": "1",
        "interaction_role": role, "raw_response_artifact_id": raw.artifact_id,
        "response_schema_artifact_id": schema.artifact_id, "parsed_value": parsed_value,
    })
    stage = _artifact(storage, "ledger.usa-v2-stage-result.v1", {
        "schema_name": "ledger.usa-v2-stage-result", "schema_version": "1",
        "llm_interaction_id": interaction_id, "interaction_role": role,
        "parsed_response_artifact_id": parsed.artifact_id, "result": stage_value,
    })
    stage_parents = [DerivationParent(raw_node.derivation_node_id, "RAW_RESPONSE"),
                     DerivationParent(facts_node.derivation_node_id, "FACTS")]
    if analyst is not None:
        stage_parents.append(DerivationParent(analyst["stage_node"].derivation_node_id, "ANALYST_RESULT"))
    stage_node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=stage.artifact_id, parents=tuple(stage_parents),
    )
    success = _artifact(storage, "ledger.llm-invocation-success.v1", {
        "schema_name": "ledger.llm-invocation-success", "schema_version": "1",
        "llm_interaction_id": interaction_id, "raw_response_artifact_id": raw.artifact_id,
        "parsed_response_artifact_id": parsed.artifact_id,
        "effective_stage_result_artifact_id": stage.artifact_id,
    })
    interaction = storage.create_llm_interaction(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token, ordinal=ordinal,
        request_artifact_id=request.artifact_id, response_artifact_id=success.artifact_id,
        error_artifact_id=None, status="SUCCEEDED", started_at=SOURCE_TIME,
        finished_at=finished_at, llm_interaction_id=interaction_id,
    )
    return {"interaction": interaction, "raw": raw, "observation": observation,
            "raw_node": raw_node, "stage_artifact": stage, "stage_node": stage_node}


def test_decimal_projection_and_strict_validation():
    assert [canonical_decimal(x) for x in ("264.0220", "281.0700", "3.0000", "0.0000")] == [
        "264.022", "281.07", "3", "0"
    ]
    for invalid in ("-0", "NaN", "Infinity"):
        with pytest.raises(ArtifactIntegrityError):
            canonical_decimal(invalid)


def test_setup_policy_decimal_erratum_is_exact(tmp_path):
    with LedgerStorage.open(tmp_path / "policy-decimal.sqlite3") as storage:
        exact = deepcopy(SETUP_POLICY_DEFINITION)
        assert exact["pullback"]["minimum_rr_tp1"] == "1.5"
        artifact = _artifact(storage, "ledger.usa-setup-v1-policy-definition.v1", exact)
        assert storage.register_setup_policy(
            definition_artifact_id=artifact.artifact_id, code_commit=COMMIT
        ).policy_version == "USA_SETUP_V1"
        assert Decimal(exact["pullback"]["minimum_rr_tp1"]) == Decimal("1.5")
        assert Decimal("2.1634") >= Decimal(exact["pullback"]["minimum_rr_tp1"])
        with pytest.raises(UnsupportedCanonicalValue):
            canonicalize_json({"minimum_rr_tp1": 1.5})

    for malformed in ("1.50", "01.5", "-0", "one-point-five"):
        with LedgerStorage.open(tmp_path / f"invalid-{hashlib.sha256(malformed.encode()).hexdigest()}.sqlite3") as storage:
            value = deepcopy(SETUP_POLICY_DEFINITION)
            value["pullback"]["minimum_rr_tp1"] = malformed
            artifact = _artifact(storage, "ledger.usa-setup-v1-policy-definition.v1", value)
            with pytest.raises(ArtifactIntegrityError):
                storage.register_setup_policy(
                    definition_artifact_id=artifact.artifact_id, code_commit=COMMIT
                )


def test_setup_rule_json_families_are_exact_and_closed():
    expected = {
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
    for setup_kind, rule_types in expected.items():
        values = tuple(json.loads(item) for item in setup_rule_values(setup_kind))
        assert tuple(item["rule_type"] for item in values) == rule_types
        assert all(item == {
            "schema_version": "1", "rule_type": rule_type, "parameters": {}
        } for item, rule_type in zip(values, rule_types))
    with pytest.raises(ArtifactIntegrityError):
        setup_rule_values("LATEST")


def test_frozen_jnj_structured_fact_catalog_is_closed(tmp_path):
    root = Path(__file__).resolve().parents[3]
    thesis = json.loads(
        (root / "data/trinity_usa_v2/9d870f12247e425f9dc234baf78c4454.json").read_text(encoding="utf-8")
    )
    valid = research_result_value(thesis)
    with LedgerStorage.open(tmp_path / "structured-facts.sqlite3") as storage:
        artifact = _artifact(storage, "ledger.usa-v2-research-result.v1", valid)
        assert storage.validate_v14_artifact(artifact.artifact_id)["ticker"] == "JNJ"
        extra = deepcopy(valid)
        extra["facts"]["price"]["acquisition"]["last_bar"]["extra"] = True
        malformed = _artifact(storage, "ledger.usa-v2-research-result.v1", extra)
        with pytest.raises(ArtifactIntegrityError):
            storage.validate_v14_artifact(malformed.artifact_id)
        missing = deepcopy(valid)
        del missing["facts"]["events"][-1]["facts"]["structured_sec_facts"][0]["xbrl_tag"]
        malformed = _artifact(storage, "ledger.usa-v2-research-result.v1", missing)
        with pytest.raises(ArtifactIntegrityError):
            storage.validate_v14_artifact(malformed.artifact_id)
        bad_decimal = deepcopy(valid)
        bad_decimal["facts"]["price"]["current_price"] = "265.580"
        malformed = _artifact(storage, "ledger.usa-v2-research-result.v1", bad_decimal)
        with pytest.raises(ArtifactIntegrityError):
            storage.validate_v14_artifact(malformed.artifact_id)


def test_populated_level_five_policy_upgrade_preserves_identity_and_fk(tmp_path):
    path = tmp_path / "populated-level-five.sqlite3"
    level_five = MigrationRegistry((
        core_migration(), execution_coordination_migration(), input_observation_migration(),
        temporal_derivation_dag_migration(), pit_classification_migration(),
    ))
    with LedgerStorage.open(path, registry=level_five) as storage:
        definition = _artifact(storage, "ledger.pit-classification-policy-definition.v1",
                               pit_classification_policy_definition_v1())
        policy = storage.register_pit_classification_policy(
            definition_artifact_id=definition.artifact_id, code_commit=COMMIT
        )
        policy_id = policy.classification_policy_id
        attempt = _attempt(storage)
        source = storage.insert_opaque_artifact(
            artifact_type="legacy.upgrade-fixture.v1", payload=b"level-five",
            media_type="application/octet-stream",
        )
        observation, node = _observe(storage, attempt, source, "level-five", SOURCE_TIME)
        proof = _artifact(storage, "fixture.reconstruction-proof.v1", {"upgrade": True})
        classification = _classify_raw(
            storage, attempt, policy, source, observation, node,
            "POST_REFERENCE_RECONSTRUCTION", (proof.artifact_id,),
        )
        classification_id = classification.derivation_node_classification_id
        evidence_v2 = _evidence(
            storage, attempt, source, observation, node,
            "POST_REFERENCE_RECONSTRUCTION", (proof.artifact_id,),
        )
        successor = storage.create_raw_node_classification(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            derivation_node_id=node.derivation_node_id, pit_reference_at=REFERENCE,
            classification_policy_id=policy.classification_policy_id,
            evidence_artifact_id=evidence_v2.artifact_id,
            supersedes_classification_id=classification_id,
        )
        successor_id = successor.derivation_node_classification_id
    with LedgerStorage.open(path) as storage:
        assert storage.current_migration_level() == 6
        assert storage.get_pit_classification_policy(policy_id).classification_policy_id == policy_id
        assert storage.get_node_classification(classification_id).classification_policy_id == policy_id
        assert storage.get_node_classification(successor_id).supersedes_classification_id == classification_id
        assert storage.get_current_node_classification(
            node.derivation_node_id, REFERENCE
        ).derivation_node_classification_id == successor_id
        assert storage.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        definition = _artifact(storage, "ledger.pit-classification-policy-definition.v1",
                               pit_classification_policy_definition_v2())
        assert storage.register_pit_classification_policy(
            definition_artifact_id=definition.artifact_id, code_commit=COMMIT
        ).policy_version == "REQUIRED_ANCESTRY_PIT_V2"


def test_jnj_v14_end_to_end_vertical_slice(tmp_path):
    root = Path(__file__).resolve().parents[3]
    thesis_source = json.loads((root / "data/trinity_usa_v2/9d870f12247e425f9dc234baf78c4454.json").read_text(encoding="utf-8"))
    bars_path = Path(__file__).with_name("fixtures") / "jnj_ohlcv_20260912.json"
    bars_bytes = bars_path.read_bytes()
    bars = json.loads(bars_bytes)
    setup_record = build_setup(thesis_source, bars, "2026-09-12")
    assert (setup_record.setup_type, setup_record.technical_regime) == ("PULLBACK", "UPTREND")
    assert (setup_record.entry_level, setup_record.stop_level, setup_record.tp1,
            setup_record.tp2, setup_record.rr_tp1, setup_record.rr_tp2) == (
        264.022, 256.1417, 281.07, 287.6628, 2.1634, 3.0,
    )

    with LedgerStorage.open(tmp_path / "jnj-v14.sqlite3") as storage:
        attempt = _attempt(storage)
        method_artifact = _artifact(storage, "ledger.usa-v2-research-method-definition.v1", RESEARCH_METHOD_DEFINITION)
        method = storage.register_research_method(definition_artifact_id=method_artifact.artifact_id, code_commit=COMMIT)
        setup_policy_artifact = _artifact(storage, "ledger.usa-setup-v1-policy-definition.v1", SETUP_POLICY_DEFINITION)
        setup_policy = storage.register_setup_policy(definition_artifact_id=setup_policy_artifact.artifact_id, code_commit=COMMIT)
        pit_artifact = _artifact(storage, "ledger.pit-classification-policy-definition.v1", pit_classification_policy_definition_v2())
        pit_policy = storage.register_pit_classification_policy(definition_artifact_id=pit_artifact.artifact_id, code_commit=COMMIT)

        source = storage.insert_opaque_artifact(
            artifact_type="legacy.jnj-ohlcv.v1", payload=bars_bytes, media_type="application/json",
        )
        source_observation, source_raw = _observe(storage, attempt, source, "jnj-ohlcv", SOURCE_TIME)
        reconstruction_proof = _artifact(storage, "fixture.reconstruction-proof.v1", {"reference": REFERENCE})
        source_class = _classify_raw(storage, attempt, pit_policy, source, source_observation,
                                     source_raw, "POST_REFERENCE_RECONSTRUCTION", (reconstruction_proof.artifact_id,))

        research_value = research_result_value(thesis_source)
        facts_artifact = _artifact(storage, "ledger.usa-v2-facts.v1", research_value["facts"])
        facts_node = storage.create_normalized_fact_node(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=facts_artifact.artifact_id,
            parents=(DerivationParent(source_raw.derivation_node_id, "SOURCE_FACTS"),),
        )
        facts_class = _classify_derived(storage, attempt, pit_policy, facts_node, (source_class,))
        ohlcv_value = {
            "ticker": "JNJ",
            "bars": [
                {"date": item["date"], **{
                    field: canonical_decimal(item[field])
                    for field in ("open", "high", "low", "close", "volume")
                }}
                for item in bars
            ],
        }
        ohlcv_artifact = _artifact(storage, "ledger.usa-setup-v1-ohlcv.v1", ohlcv_value)
        ohlcv_node = storage.create_normalized_fact_node(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=ohlcv_artifact.artifact_id,
            parents=(DerivationParent(source_raw.derivation_node_id, "OHLCV"),),
        )
        ohlcv_class = _classify_derived(storage, attempt, pit_policy, ohlcv_node, (source_class,))

        analyst_value, critic_value, critic_stage = _role_values(thesis_source)
        analyst = _interaction(storage, attempt, role="ANALYST", ordinal=1,
                               facts_artifact=facts_artifact, facts_node=facts_node,
                               parsed_value=analyst_value, stage_value=analyst_value,
                               finished_at=ANALYST_TIME)
        critic = _interaction(storage, attempt, role="CRITIC", ordinal=2,
                              facts_artifact=facts_artifact, facts_node=facts_node,
                              parsed_value=critic_value, stage_value=critic_stage,
                              finished_at=CRITIC_TIME, analyst=analyst)
        analyst_raw_class = _classify_raw(storage, attempt, pit_policy, analyst["raw"], analyst["observation"], analyst["raw_node"], "PIT_IRRELEVANT_CONTENT")
        analyst_stage_class = _classify_derived(storage, attempt, pit_policy, analyst["stage_node"], (analyst_raw_class, facts_class))
        critic_raw_class = _classify_raw(storage, attempt, pit_policy, critic["raw"], critic["observation"], critic["raw_node"], "PIT_IRRELEVANT_CONTENT")
        critic_stage_class = _classify_derived(storage, attempt, pit_policy, critic["stage_node"], (critic_raw_class, facts_class, analyst_stage_class))

        research_artifact = _artifact(storage, "ledger.usa-v2-research-result.v1", research_value)
        assert "thesis_id" not in research_value and "created_at" not in research_value
        research = storage.create_research_record(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            subject_key="ticker:JNJ", content_artifact_id=research_artifact.artifact_id,
            research_method_id=method.research_method_id, as_of_at=REFERENCE,
            parents=(DerivationParent(facts_node.derivation_node_id, "FACTS"),
                     DerivationParent(analyst["stage_node"].derivation_node_id, "ANALYST"),
                     DerivationParent(critic["stage_node"].derivation_node_id, "CRITIC")),
            llm_links=((analyst["interaction"].llm_interaction_id, "PRIMARY", 1),
                       (critic["interaction"].llm_interaction_id, "CRITIQUE", 1)),
        )
        research_node = storage.get_derivation_node(research.derivation_node_id)
        research_class = _classify_derived(storage, attempt, pit_policy, research_node,
                                           (facts_class, analyst_stage_class, critic_stage_class))
        assert research_node.derived_available_at == CRITIC_TIME

        setup_value = setup_result_value(setup_record)
        setup_artifact = _artifact(storage, "ledger.usa-setup-v1-result.v1", setup_value)
        setup = storage.create_setup(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            instrument_id="JNJ", setup_kind="PULLBACK", signal_at=CRITIC_TIME,
            setup_policy_id=setup_policy.setup_policy_id,
            result_artifact_id=setup_artifact.artifact_id, research_id=research.research_id,
            research_derivation_node_id=research.derivation_node_id,
            ohlcv_derivation_node_id=ohlcv_node.derivation_node_id,
        )
        setup_node = storage.get_derivation_node(setup.derivation_node_id)
        setup_class = _classify_derived(storage, attempt, pit_policy, setup_node,
                                        (research_class, ohlcv_class))
        assert setup_value["entry_level"] == "264.022"
        assert setup_value["stop_level"] == "256.1417"
        assert setup_value["tp1"] == "281.07"
        assert setup_value["tp2"] == "287.6628"
        assert setup_value["rr_tp1"] == "2.1634"
        assert setup_value["rr_tp2"] == "3"

        for artifact in (research_artifact, setup_artifact):
            storage.attach_attempt_artifact(attempt_id=attempt.attempt_id,
                                            fence_token=attempt.fence_token,
                                            artifact_id=artifact.artifact_id, role="OUTPUT")
        classifications = (source_class, facts_class, ohlcv_class, analyst_raw_class,
                           analyst_stage_class, critic_raw_class, critic_stage_class,
                           research_class, setup_class)
        commit_arguments = dict(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            output_artifact_ids=(research_artifact.artifact_id, setup_artifact.artifact_id),
            run_inputs=(
                RunInputBinding(source_observation.input_observation_id, "SOURCE", source_raw.derivation_node_id),
                RunInputBinding(analyst["observation"].input_observation_id, "ANALYST_RESPONSE", analyst["raw_node"].derivation_node_id),
                RunInputBinding(critic["observation"].input_observation_id, "CRITIC_RESPONSE", critic["raw_node"].derivation_node_id),
            ),
            derivation_node_ids=(setup_node.derivation_node_id,),
            derivation_node_classification_ids=tuple(x.derivation_node_classification_id for x in classifications),
        )
        run = storage.commit_run(**commit_arguments)
        manifest_artifact = storage.get_artifact(run.result_manifest_artifact_id)
        manifest = json.loads(manifest_artifact.payload)
        assert manifest_artifact.artifact_kind == "ledger.run-result-manifest.v3"
        assert manifest["manifest_version"] == "3"
        assert manifest["research_ids"] == [research.research_id]
        assert manifest["setup_ids"] == [setup.setup_id]
        assert manifest["eligibility_ids"] == manifest["trade_ids"] == manifest["outcome_ids"] == []
        assert manifest["resolved_record_class"] == "LEGACY_NON_LEDGER_ARTIFACT"
        assert manifest["resolved_pit_class"] == "RECONSTRUCTED_NOT_ARCHIVED"
        assert storage.get_research_record(research.research_id).run_id == run.run_id
        assert storage.get_setup(setup.setup_id).run_id == run.run_id
        telegram_text = render_telegram_message(load_committed_setup(
            storage, setup_id=setup.setup_id, run_id=run.run_id
        ))
        for expected in (
            "JNJ", "PULLBACK", "UPTREND", "INVESTIGATE", "HIGH", "MEDIUM",
            "264.022", "256.1417", "281.07", "287.6628", "2.16", "3.00",
            "UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY",
            "RECONSTRUCTED_NOT_ARCHIVED", "LEGACY_NON_LEDGER_ARTIFACT",
        ):
            assert expected in telegram_text
        assert storage.commit_run(**commit_arguments).run_id == run.run_id
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            storage.connection.execute(
                "UPDATE research_record SET subject_key=subject_key WHERE research_id=?",
                (research.research_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            storage.connection.execute(
                "UPDATE setup SET instrument_id=instrument_id WHERE setup_id=?",
                (setup.setup_id,),
            )
        assert storage.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_raw_response_preserves_non_utf8_before_failed_parse(tmp_path):
    with LedgerStorage.open(tmp_path / "raw.sqlite3") as storage:
        raw = storage.insert_artifact(
            artifact_type="ledger.llm-raw-response.v1", canonicalization_version=IDENTITY,
            payload=b"\xff\x00{", media_type="application/json",
        )
        assert storage.validate_v14_artifact(raw.artifact_id) == b"\xff\x00{"
        with pytest.raises(UnicodeDecodeError):
            raw.payload.decode("utf-8")


def test_failed_llm_interactions_preserve_raw_and_create_no_effective_stage(tmp_path):
    with LedgerStorage.open(tmp_path / "failed-interactions.sqlite3") as storage:
        attempt = _attempt(storage)
        source = storage.insert_opaque_artifact(
            artifact_type="fixture.facts-source.v1", payload=b"facts",
            media_type="application/octet-stream",
        )
        _, raw = _observe(storage, attempt, source, "facts", SOURCE_TIME)
        facts = _artifact(storage, "ledger.usa-v2-facts.v1", {"fixture": "facts"})
        facts_node = storage.create_normalized_fact_node(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=facts.artifact_id,
            parents=(DerivationParent(raw.derivation_node_id, "FACTS_SOURCE"),),
        )
        malformed = _interaction(
            storage, attempt, role="ANALYST", ordinal=1,
            facts_artifact=facts, facts_node=facts_node, parsed_value={}, stage_value={},
            finished_at=ANALYST_TIME, raw_bytes_override=b'{"broken":',
            error_code="MALFORMED_JSON",
        )
        non_utf8 = _interaction(
            storage, attempt, role="ANALYST", ordinal=2,
            facts_artifact=facts, facts_node=facts_node, parsed_value={}, stage_value={},
            finished_at=CRITIC_TIME, raw_bytes_override=b"\xff\xfe",
            error_code="RAW_RESPONSE_NOT_UTF8",
        )
        assert malformed["interaction"].status == non_utf8["interaction"].status == "FAILED"
        assert malformed["raw"].payload == b'{"broken":'
        assert non_utf8["raw"].payload == b"\xff\xfe"
        assert storage.connection.execute(
            "SELECT count(*) FROM artifact WHERE artifact_kind='ledger.usa-v2-stage-result.v1'"
        ).fetchone()[0] == 0


def test_v14_entities_are_immutable(tmp_path):
    with LedgerStorage.open(tmp_path / "immutable.sqlite3") as storage:
        definition = _artifact(storage, "ledger.usa-v2-research-method-definition.v1", RESEARCH_METHOD_DEFINITION)
        method = storage.register_research_method(definition_artifact_id=definition.artifact_id, code_commit=COMMIT)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            storage.connection.execute("UPDATE research_method SET code_commit=code_commit WHERE research_method_id=?", (method.research_method_id,))
