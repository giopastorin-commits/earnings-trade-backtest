"""Materialize the frozen JNJ V1.4 vertical slice for a Telegram smoke test."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from trinity.ledger import (
    CONFIG_DEFINITION,
    RESEARCH_METHOD_DEFINITION,
    SETUP_POLICY_DEFINITION,
    DerivationParent,
    LedgerStorage,
    RunInputBinding,
    analyst_response_schema,
    canonical_decimal,
    canonicalize_json,
    critic_response_schema,
    pit_classification_policy_definition_v2,
    research_result_value,
    setup_result_value,
)
from trinity.ledger.canonical import IDENTITY
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
V14_IMPLEMENTATION_COMMIT = "eddd0495639e2d3c2cec53bc0f0b72e44654bdce"
ANALYSIS_FIELDS = (
    "fundamental_analysis", "earnings_and_news_analysis", "price_context",
    "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation",
)
DEFAULT_DATABASE = Path("data/local/jnj_telegram_pilot.sqlite3")


@dataclass(frozen=True)
class PilotIdentities:
    ledger_db: Path
    run_id: str
    research_id: str
    setup_id: str


def materialize_jnj_telegram_pilot(
    database: str | Path = DEFAULT_DATABASE,
    *,
    repository_root: str | Path | None = None,
) -> PilotIdentities:
    """Create exactly one committed deterministic JNJ reconstruction run."""

    root = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    database_path = Path(database).resolve()
    if database_path.exists():
        raise FileExistsError(f"pilot Ledger already exists: {database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)

    thesis = json.loads(
        (root / "data/trinity_usa_v2/9d870f12247e425f9dc234baf78c4454.json")
        .read_text(encoding="utf-8")
    )
    bars_path = root / "tests/trinity/ledger/fixtures/jnj_ohlcv_20260912.json"
    bars_bytes = bars_path.read_bytes()
    bars = json.loads(bars_bytes)
    setup_record = build_setup(thesis, bars, "2026-09-12")

    try:
        with LedgerStorage.open(database_path) as storage:
            identities = _materialize(storage, root, thesis, bars, bars_bytes, setup_record)
            notification = load_committed_setup(
                storage, setup_id=identities.setup_id, run_id=identities.run_id
            )
            render_telegram_message(notification)
            if storage.connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("pilot Ledger failed foreign_key_check")
        return PilotIdentities(database_path, identities.run_id, identities.research_id, identities.setup_id)
    except Exception:
        if database_path.exists():
            database_path.unlink()
        raise


def _materialize(storage, root, thesis, bars, bars_bytes, setup_record):
    attempt = _attempt(storage)
    method_definition = _artifact(
        storage, "ledger.usa-v2-research-method-definition.v1",
        RESEARCH_METHOD_DEFINITION,
    )
    method = storage.register_research_method(
        definition_artifact_id=method_definition.artifact_id,
        code_commit=V14_IMPLEMENTATION_COMMIT,
    )
    setup_definition = _artifact(
        storage, "ledger.usa-setup-v1-policy-definition.v1", SETUP_POLICY_DEFINITION
    )
    setup_policy = storage.register_setup_policy(
        definition_artifact_id=setup_definition.artifact_id,
        code_commit=V14_IMPLEMENTATION_COMMIT,
    )
    pit_definition = _artifact(
        storage, "ledger.pit-classification-policy-definition.v1",
        pit_classification_policy_definition_v2(),
    )
    pit_policy = storage.register_pit_classification_policy(
        definition_artifact_id=pit_definition.artifact_id,
        code_commit=V14_IMPLEMENTATION_COMMIT,
    )

    source = storage.insert_opaque_artifact(
        artifact_type="legacy.jnj-ohlcv.v1", payload=bars_bytes,
        media_type="application/json",
    )
    source_observation, source_raw = _observe(
        storage, attempt, source, "jnj-ohlcv", SOURCE_TIME
    )
    proof = _artifact(storage, "fixture.reconstruction-proof.v1", {"reference": REFERENCE})
    source_class = _classify_raw(
        storage, attempt, pit_policy, source, source_observation, source_raw,
        "POST_REFERENCE_RECONSTRUCTION", (proof.artifact_id,),
    )

    research_value = research_result_value(thesis)
    facts_artifact = _artifact(storage, "ledger.usa-v2-facts.v1", research_value["facts"])
    facts_node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=facts_artifact.artifact_id,
        parents=(DerivationParent(source_raw.derivation_node_id, "SOURCE_FACTS"),),
    )
    facts_class = _classify_derived(storage, attempt, pit_policy, facts_node, (source_class,))
    ohlcv_artifact = _artifact(storage, "ledger.usa-setup-v1-ohlcv.v1", {
        "ticker": "JNJ",
        "bars": [
            {"date": item["date"], **{
                field: canonical_decimal(item[field])
                for field in ("open", "high", "low", "close", "volume")
            }}
            for item in bars
        ],
    })
    ohlcv_node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=ohlcv_artifact.artifact_id,
        parents=(DerivationParent(source_raw.derivation_node_id, "OHLCV"),),
    )
    ohlcv_class = _classify_derived(storage, attempt, pit_policy, ohlcv_node, (source_class,))

    analyst_value, critic_value, critic_stage = _role_values(thesis)
    analyst = _interaction(
        storage, root, attempt, role="ANALYST", ordinal=1,
        facts_artifact=facts_artifact, facts_node=facts_node,
        parsed_value=analyst_value, stage_value=analyst_value,
        finished_at=ANALYST_TIME,
    )
    critic = _interaction(
        storage, root, attempt, role="CRITIC", ordinal=2,
        facts_artifact=facts_artifact, facts_node=facts_node,
        parsed_value=critic_value, stage_value=critic_stage,
        finished_at=CRITIC_TIME, analyst=analyst,
    )
    analyst_raw_class = _classify_raw(
        storage, attempt, pit_policy, analyst["raw"], analyst["observation"],
        analyst["raw_node"], "PIT_IRRELEVANT_CONTENT",
    )
    analyst_stage_class = _classify_derived(
        storage, attempt, pit_policy, analyst["stage_node"],
        (analyst_raw_class, facts_class),
    )
    critic_raw_class = _classify_raw(
        storage, attempt, pit_policy, critic["raw"], critic["observation"],
        critic["raw_node"], "PIT_IRRELEVANT_CONTENT",
    )
    critic_stage_class = _classify_derived(
        storage, attempt, pit_policy, critic["stage_node"],
        (critic_raw_class, facts_class, analyst_stage_class),
    )

    research_artifact = _artifact(
        storage, "ledger.usa-v2-research-result.v1", research_value
    )
    research = storage.create_research_record(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        subject_key="ticker:JNJ", content_artifact_id=research_artifact.artifact_id,
        research_method_id=method.research_method_id, as_of_at=REFERENCE,
        parents=(
            DerivationParent(facts_node.derivation_node_id, "FACTS"),
            DerivationParent(analyst["stage_node"].derivation_node_id, "ANALYST"),
            DerivationParent(critic["stage_node"].derivation_node_id, "CRITIC"),
        ),
        llm_links=(
            (analyst["interaction"].llm_interaction_id, "PRIMARY", 1),
            (critic["interaction"].llm_interaction_id, "CRITIQUE", 1),
        ),
    )
    research_node = storage.get_derivation_node(research.derivation_node_id)
    research_class = _classify_derived(
        storage, attempt, pit_policy, research_node,
        (facts_class, analyst_stage_class, critic_stage_class),
    )
    setup_artifact = _artifact(
        storage, "ledger.usa-setup-v1-result.v1", setup_result_value(setup_record)
    )
    setup = storage.create_setup(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        instrument_id="JNJ", setup_kind="PULLBACK", signal_at=CRITIC_TIME,
        setup_policy_id=setup_policy.setup_policy_id,
        result_artifact_id=setup_artifact.artifact_id,
        research_id=research.research_id,
        research_derivation_node_id=research.derivation_node_id,
        ohlcv_derivation_node_id=ohlcv_node.derivation_node_id,
    )
    setup_node = storage.get_derivation_node(setup.derivation_node_id)
    setup_class = _classify_derived(
        storage, attempt, pit_policy, setup_node, (research_class, ohlcv_class)
    )
    for artifact in (research_artifact, setup_artifact):
        storage.attach_attempt_artifact(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=artifact.artifact_id, role="OUTPUT",
        )
    classifications = (
        source_class, facts_class, ohlcv_class, analyst_raw_class,
        analyst_stage_class, critic_raw_class, critic_stage_class,
        research_class, setup_class,
    )
    run = storage.commit_run(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        output_artifact_ids=(research_artifact.artifact_id, setup_artifact.artifact_id),
        run_inputs=(
            RunInputBinding(
                source_observation.input_observation_id, "SOURCE",
                source_raw.derivation_node_id,
            ),
            RunInputBinding(
                analyst["observation"].input_observation_id, "ANALYST_RESPONSE",
                analyst["raw_node"].derivation_node_id,
            ),
            RunInputBinding(
                critic["observation"].input_observation_id, "CRITIC_RESPONSE",
                critic["raw_node"].derivation_node_id,
            ),
        ),
        derivation_node_ids=(setup_node.derivation_node_id,),
        derivation_node_classification_ids=tuple(
            item.derivation_node_classification_id for item in classifications
        ),
    )
    return PilotIdentities(Path(), run.run_id, research.research_id, setup.setup_id)


def _artifact(storage, kind, value, *, schema=False):
    return storage.insert_json_artifact(
        artifact_type=kind, value=value,
        media_type="application/schema+json" if schema else "application/json",
    )


def _attempt(storage):
    request = storage.create_run_request(
        request_kind="RECONSTRUCTION", analysis_cutoff_at=CUTOFF,
        parameters={"historical_as_of_at": REFERENCE},
        idempotency_key="jnj-v14-telegram-pilot",
        requested_by="jnj-telegram-pilot",
        baseline_commit=V14_IMPLEMENTATION_COMMIT,
    )
    return storage.allocate_attempt(
        run_request_id=request.run_request_id,
        worker_identity="jnj-telegram-pilot",
        code_commit=V14_IMPLEMENTATION_COMMIT,
        environment_fingerprint="pilot:jnj:telegram:v1",
    )


def _observe(storage, attempt, artifact, record, when):
    observation = storage.create_input_observation(
        artifact_id=artifact.artifact_id, source_id="fixture:jnj:v1",
        source_record_key=record, source_published_at=None, retrieved_at=when,
        availability_basis="RETRIEVED_AT_FALLBACK", effective_available_at=when,
        observed_by_attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        source_metadata={
            "schema_version": "1", "provider": "fixture", "dataset_name": "jnj",
            "source_record_key": record,
            "acquisition_method": "checked-in-fixture",
            "availability_rule_id": "retrieval-fallback",
            "availability_rule_version": "1", "evidence_artifact_ids": [],
            "provider_metadata": {}, "future_effective_at": None,
        },
    )
    node = storage.create_raw_derivation_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        input_observation_id=observation.input_observation_id,
    )
    return observation, node


def _classify_raw(storage, attempt, policy, artifact, observation, node, kind, support=()):
    if kind == "POST_REFERENCE_RECONSTRUCTION":
        verifier = "LEDGER_RECONSTRUCTION_EVIDENCE_V1"
        rationale = "POST_REFERENCE_RECONSTRUCTION_VERIFIED"
        reconstruction = SOURCE_TIME
        origin = "LEGACY_IMPORT"
        namespace, key = "legacy.jnj", "ohlcv-2026-09-12"
        legacy_id = "legacy:sha256:" + hashlib.sha256(canonicalize_json({
            "legacy_namespace": namespace, "legacy_record_key": key,
        })).hexdigest()
    else:
        verifier = "LEDGER_PIT_IRRELEVANCE_V1"
        rationale = "PIT_NOT_APPLICABLE_BY_ARTIFACT_KIND"
        reconstruction = None
        origin = "LEDGER_AUTHORIZED_CAPTURE"
        namespace = key = legacy_id = None
    evidence = _artifact(storage, "ledger.pit-classification-evidence.v1", {
        "schema_name": "ledger.pit-classification-evidence",
        "schema_version": "1", "derivation_node_id": node.derivation_node_id,
        "input_observation_id": observation.input_observation_id,
        "artifact_id": artifact.artifact_id, "pit_reference_at": REFERENCE,
        "record_origin_evidence_kind": origin, "legacy_namespace": namespace,
        "legacy_record_key": key, "legacy_record_id": legacy_id,
        "pit_evidence_kind": kind, "verifier_id": verifier,
        "archive_captured_at": None,
        "reconstruction_completed_at": reconstruction,
        "supporting_artifact_ids": sorted(support), "rationale_code": rationale,
    })
    for artifact_id in (*support, evidence.artifact_id):
        storage.attach_attempt_artifact(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=artifact_id, role="DIAGNOSTIC",
        )
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
        parent_classification_ids=tuple(
            item.derivation_node_classification_id for item in parents
        ),
    )


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
    revised["claim_refs"] = [
        _claim_without_errors(item) for item in thesis["claim_refs"]
    ]
    critic = {
        "notes": thesis["critic_notes"], "status": thesis["status"],
        "evidence_confidence": thesis["evidence_confidence"],
        "thesis_strength": thesis["thesis_strength"],
        "event_assessments": thesis["event_assessments"],
        "revised_analysis": revised,
    }
    return analyst, critic, {
        **critic, "critic_status": thesis["critic_status"],
        "critic_thesis_strength": thesis["critic_thesis_strength"],
        "critic_evidence_confidence": thesis["critic_evidence_confidence"],
    }


def _claim_without_errors(item):
    return {key: value for key, value in item.items() if key != "validation_errors"}


def _interaction(
    storage, root, attempt, *, role, ordinal, facts_artifact, facts_node,
    parsed_value, stage_value, finished_at, analyst=None,
):
    schema = _artifact(
        storage, "ledger.llm-response-schema.v1",
        analyst_response_schema() if role == "ANALYST" else critic_response_schema(),
        schema=True,
    )
    prompt = storage.insert_artifact(
        artifact_type="ledger.llm-effective-prompt.v1",
        canonicalization_version=IDENTITY,
        payload=f"{role} JNJ deterministic fixture\n".encode(),
        media_type="text/plain;charset=utf-8",
    )
    items = [{
        "context_role": "FACTS", "artifact_id": facts_artifact.artifact_id,
        "derivation_node_id": facts_node.derivation_node_id,
    }]
    if analyst is not None:
        items.append({
            "context_role": "ANALYST_EFFECTIVE_STAGE_RESULT",
            "artifact_id": analyst["stage_artifact"].artifact_id,
            "derivation_node_id": analyst["stage_node"].derivation_node_id,
        })
    context = _artifact(storage, "ledger.llm-context.v1", {
        "schema_name": "ledger.llm-context", "schema_version": "1",
        "interaction_role": role, "items": items,
    })
    parameters = _artifact(storage, "ledger.llm-invocation-parameters.v1", {
        "schema_name": "ledger.llm-invocation-parameters", "schema_version": "1",
        "provider": "OPENAI_CODEX_CLI", "transport": "CODEX_CLI_STDIN",
        "model": "gpt-5.6-sol", "model_version": "codex-cli:gpt-5.6-sol",
        "timeout_seconds": 240, "sandbox": "read-only", "ephemeral": True,
        "skip_git_repo_check": True, "ignore_user_config": True,
        "response_schema_artifact_id": schema.artifact_id,
        "temperature": None, "top_p": None, "seed": None,
        "max_output_tokens": None, "reasoning_effort": None, "tool_mode": "NONE",
    })
    paths = ["trinity/italia_real.py", "trinity/italia_v1.py", "trinity/usa_v2.py"]
    code = _artifact(storage, "ledger.code-manifest.v1", {
        "schema_name": "ledger.code-manifest", "schema_version": "1",
        "repository": "earnings-trade-backtest",
        "git_commit": V14_IMPLEMENTATION_COMMIT,
        "source_files": [
            {"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
            for path in paths
        ],
    })
    config = _artifact(
        storage, "ledger.usa-v2-invocation-config.v1", CONFIG_DEFINITION
    )
    request = _artifact(storage, "ledger.llm-invocation-request.v1", {
        "schema_name": "ledger.llm-invocation-request", "schema_version": "1",
        "interaction_role": role, "provider": "OPENAI_CODEX_CLI",
        "model": "gpt-5.6-sol", "model_version": "codex-cli:gpt-5.6-sol",
        "client_effective_input_artifact_id": prompt.artifact_id,
        "context_artifact_id": context.artifact_id,
        "response_schema_artifact_id": schema.artifact_id,
        "invocation_parameters_artifact_id": parameters.artifact_id,
        "code_manifest_artifact_id": code.artifact_id,
        "config_artifact_id": config.artifact_id,
    })
    interaction_id = str(uuid.uuid4())
    raw = storage.insert_artifact(
        artifact_type="ledger.llm-raw-response.v1",
        canonicalization_version=IDENTITY,
        payload=canonicalize_json(parsed_value), media_type="application/json",
    )
    observation, raw_node = _observe(
        storage, attempt, raw, f"{role.lower()}-result.json", finished_at
    )
    parsed = _artifact(storage, "ledger.llm-parsed-response.v1", {
        "schema_name": "ledger.llm-parsed-response", "schema_version": "1",
        "interaction_role": role, "raw_response_artifact_id": raw.artifact_id,
        "response_schema_artifact_id": schema.artifact_id,
        "parsed_value": parsed_value,
    })
    stage = _artifact(storage, "ledger.usa-v2-stage-result.v1", {
        "schema_name": "ledger.usa-v2-stage-result", "schema_version": "1",
        "llm_interaction_id": interaction_id, "interaction_role": role,
        "parsed_response_artifact_id": parsed.artifact_id, "result": stage_value,
    })
    parents = [
        DerivationParent(raw_node.derivation_node_id, "RAW_RESPONSE"),
        DerivationParent(facts_node.derivation_node_id, "FACTS"),
    ]
    if analyst is not None:
        parents.append(DerivationParent(
            analyst["stage_node"].derivation_node_id, "ANALYST_RESULT"
        ))
    stage_node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=stage.artifact_id, parents=tuple(parents),
    )
    success = _artifact(storage, "ledger.llm-invocation-success.v1", {
        "schema_name": "ledger.llm-invocation-success", "schema_version": "1",
        "llm_interaction_id": interaction_id,
        "raw_response_artifact_id": raw.artifact_id,
        "parsed_response_artifact_id": parsed.artifact_id,
        "effective_stage_result_artifact_id": stage.artifact_id,
    })
    interaction = storage.create_llm_interaction(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        ordinal=ordinal, request_artifact_id=request.artifact_id,
        response_artifact_id=success.artifact_id, error_artifact_id=None,
        status="SUCCEEDED", started_at=SOURCE_TIME, finished_at=finished_at,
        llm_interaction_id=interaction_id,
    )
    return {
        "interaction": interaction, "raw": raw, "observation": observation,
        "raw_node": raw_node, "stage_artifact": stage, "stage_node": stage_node,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Materialize the deterministic committed JNJ Telegram pilot Ledger"
    )
    parser.add_argument("--ledger-db", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args(argv)
    try:
        result = materialize_jnj_telegram_pilot(args.ledger_db)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"ledger_db={result.ledger_db}")
    print(f"run_id={result.run_id}")
    print(f"research_id={result.research_id}")
    print(f"setup_id={result.setup_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
