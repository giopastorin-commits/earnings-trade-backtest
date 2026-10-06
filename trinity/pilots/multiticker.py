"""Explicit, isolated multi-ticker pilot over the frozen TRINITY USA V2 slice."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping, Sequence
import uuid

from trinity.italia_v1 import analyze_company, build_facts
from trinity.ledger import (
    CONFIG_DEFINITION,
    RESEARCH_METHOD_DEFINITION,
    RESEARCH_METHOD_DEFINITION_V3,
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
    TelegramNotification,
    load_committed_setup,
    render_telegram_message,
    send_telegram_message,
)
from trinity.usa_issuer_registry import REGISTRY
from trinity.usa_setup_v1 import PRICE_CACHE, build_setup
from trinity.usa_v2 import AS_OF, COMPANIES, USAProvider, load_company


DEFAULT_DATABASE = Path("data/local/trinity_multiticker_pilot.sqlite3")
REFERENCE = f"{AS_OF}T00:00:00.000000Z"
ANALYSIS_FIELDS = (
    "fundamental_analysis", "earnings_and_news_analysis", "price_context",
    "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation",
)
CODE_PATHS = (
    "trinity/italia_real.py", "trinity/italia_v1.py", "trinity/usa_v2.py",
    "trinity/usa_forward.py",
)


@dataclass(frozen=True)
class InvocationTrace:
    role: str
    prompt: str
    schema: Mapping[str, object]
    parsed_value: dict[str, object]
    started_at: str
    finished_at: str
    provenance: Mapping[str, object] | None = None


class RecordingUSAProvider(USAProvider):
    """Existing USA provider with an audit-only recorder around its LLM calls."""

    def __init__(
        self,
        requester: Callable[[str, Mapping[str, object]], dict[str, object]] | None = None,
    ) -> None:
        super().__init__()
        self._requester = requester
        self.invocations: list[InvocationTrace] = []
        self.analyst_stage: dict[str, object] | None = None
        self.critic_stage: dict[str, object] | None = None
        self.provenance: dict[str, object] = {
            "provider": "OPENAI_CODEX_CLI", "transport": "CODEX_CLI_STDIN",
            "model": "gpt-5.6-sol", "model_version": "codex-cli:gpt-5.6-sol",
            "timeout_seconds": 240, "sandbox": "read-only",
            "skip_git_repo_check": True, "ignore_user_config": True,
        }

    def _request(
        self, prompt: str, schema: Mapping[str, object]
    ) -> dict[str, object]:
        started = _utc_now()
        result = (
            self._requester(prompt, schema)
            if self._requester is not None
            else super()._request(prompt, schema)
        )
        finished = _utc_now()
        role = "ANALYST" if len(self.invocations) == 0 else "CRITIC"
        self.invocations.append(InvocationTrace(
            role, prompt, deepcopy(schema), deepcopy(result), started, finished,
            deepcopy(self.provenance),
        ))
        return result

    def analyze(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        result = super().analyze(facts)
        self.analyst_stage = deepcopy(dict(result))
        return result

    def critique(
        self, facts: Mapping[str, object], draft: Mapping[str, object]
    ) -> Mapping[str, object]:
        result = super().critique(facts, draft)
        self.critic_stage = deepcopy(dict(result))
        return result


@dataclass(frozen=True)
class TickerResult:
    ticker: str
    run_status: str
    research_status: str = "-"
    evidence_confidence: str = "-"
    thesis_strength: str = "-"
    technical_regime: str = "-"
    setup_type: str = "-"
    entry: str = "-"
    stop: str = "-"
    tp1: str = "-"
    tp2: str = "-"
    rr1: str = "-"
    rr2: str = "-"
    resolved_record_class: str = "-"
    resolved_pit_class: str = "-"
    run_id: str | None = None
    research_id: str | None = None
    setup_id: str | None = None
    telegram_message: str | None = None
    telegram_sent: bool = False
    error: str | None = None
    fresh_price_timestamp: str = "-"
    freshest_evidence_timestamp: str = "-"
    price_provider: str = "-"
    news_provider: str = "-"
    primary_evidence_provider: str = "-"


@dataclass(frozen=True)
class BatchResult:
    ledger_db: Path
    tickers: tuple[str, ...]
    results: tuple[TickerResult, ...]

    @property
    def telegram_render_count(self) -> int:
        return sum(item.telegram_message is not None for item in self.results)


def run_multiticker_pilot(
    database: str | Path,
    tickers: Sequence[str],
    *,
    dry_run: bool,
    repository_root: str | Path | None = None,
    provider_factory: Callable[[str], RecordingUSAProvider] = lambda _ticker: RecordingUSAProvider(),
    company_loader: Callable[[str, str], Any] = load_company,
    telegram_sender: Callable[[str], None] = send_telegram_message,
) -> BatchResult:
    """Run only the explicitly supplied tickers; each ticker owns one Ledger run."""

    selected = _validate_tickers(tickers)
    root = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[2]
    )
    database_path = Path(database).resolve()
    if database_path.exists():
        raise FileExistsError(f"pilot Ledger already exists: {database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    code_commit = _git_head(root)
    sent_setup_ids: set[str] = set()
    results: list[TickerResult] = []

    with LedgerStorage.open(database_path) as storage:
        shared = _register_shared_definitions(storage, code_commit)
        for ticker in selected:
            attempt = None
            try:
                attempt = _new_attempt(storage, ticker, code_commit)
                pack = company_loader(ticker, AS_OF)
                if (
                    pack.ticker != ticker
                    or pack.as_of != AS_OF
                    or pack.company_input.get("ticker") != ticker
                    or pack.company_input.get("as_of") != AS_OF
                ):
                    raise ValueError(f"{ticker}: source bundle identity mismatch")
                bars_path = PRICE_CACHE / f"{ticker}.json"
                bars_bytes = bars_path.read_bytes()
                bars = json.loads(bars_bytes)
                facts = build_facts(pack.company_input, AS_OF)
                persisted = _persist_inputs(
                    storage, attempt, ticker, pack, facts, bars, bars_bytes,
                    shared["pit_policy"],
                )

                provider = provider_factory(ticker)
                thesis = analyze_company(pack.company_input, AS_OF, provider)
                thesis = replace(
                    thesis,
                    **provider.revisions,
                    claim_refs=provider.validated_claim_refs,
                    rejected_claim_refs=provider.rejected_claim_refs,
                )
                if (
                    len(provider.invocations) != 2
                    or provider.analyst_stage is None
                    or provider.critic_stage is None
                ):
                    raise RuntimeError("Analyst/Critic invocation trace is incomplete")
                setup_record = build_setup(thesis.to_dict(), bars, AS_OF)
                identities = _persist_completed_pipeline(
                    storage, root, code_commit, attempt, ticker, thesis,
                    setup_record, provider, persisted,
                    shared["research_method"], shared["setup_policy"],
                )
                notification = load_committed_setup(
                    storage,
                    setup_id=identities["setup_id"],
                    run_id=identities["run_id"],
                )
                message = render_telegram_message(notification)
                sent = False
                if not dry_run:
                    _send_once(
                        notification.setup_id, message, sent_setup_ids, telegram_sender,
                    )
                    sent = True
                results.append(_ticker_result(notification, message, sent))
            except Exception as exc:  # ticker boundary intentionally isolates failures
                if attempt is not None and storage.is_attempt_authorized(
                    attempt.attempt_id, attempt.fence_token
                ):
                    _record_failure(storage, attempt, ticker, exc)
                results.append(TickerResult(
                    ticker=ticker,
                    run_status="FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                ))
        if storage.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("pilot Ledger failed foreign_key_check")
    return BatchResult(database_path, selected, tuple(results))


def _validate_tickers(tickers: Sequence[str]) -> tuple[str, ...]:
    if not tickers:
        raise ValueError("an explicit ticker list is required")
    selected = tuple(str(item).strip().upper() for item in tickers)
    if any(not item for item in selected):
        raise ValueError("ticker values must be non-empty")
    if len(selected) != len(set(selected)):
        raise ValueError("duplicate tickers are not allowed")
    records = {item: REGISTRY.get(item) for item in selected}
    unsupported = [item for item, record in records.items()
                   if record is None or not record.supported]
    if unsupported:
        raise ValueError(f"unsupported ticker(s): {', '.join(unsupported)}")
    return selected


def _new_attempt(storage: LedgerStorage, ticker: str, code_commit: str):
    cutoff = _utc_after(minutes=15)
    request = storage.create_run_request(
        request_kind="RECONSTRUCTION",
        analysis_cutoff_at=cutoff,
        parameters={"historical_as_of_at": REFERENCE, "ticker": ticker},
        idempotency_key=f"multiticker-v1:{ticker}:{uuid.uuid4()}",
        requested_by="trinity-multiticker-pilot-v1",
        baseline_commit=code_commit,
    )
    return storage.allocate_attempt(
        run_request_id=request.run_request_id,
        worker_identity=f"multiticker-pilot:{ticker.lower()}",
        code_commit=code_commit,
        environment_fingerprint="pilot:usa-v2:multiticker:v1",
    )


def _persist_inputs(
    storage, attempt, ticker, pack, facts, bars, bars_bytes, pit_policy,
    *, as_of: str = AS_OF, reference: str = REFERENCE,
    research_source_bytes: bytes | None = None,
    research_retrieved_at: str | None = None,
    price_retrieved_at: str | None = None,
    forward: bool = False,
):
    observed_at = _utc_now()
    generated_source_payload = json.dumps(
        {
            "ticker": ticker,
            "as_of": as_of,
            "company_input": pack.company_input,
            "triage": pack.triage,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    source_payload = research_source_bytes or generated_source_payload
    source = storage.insert_opaque_artifact(
        artifact_type=("ledger.forward-source-archive.v1" if forward
                       else "local.usa-v2-source-bundle.v1"),
        payload=source_payload,
        media_type="application/zip" if forward else "application/json",
    )
    source_observation, source_raw = _observe(
        storage, attempt, source, ticker.lower(), "research-source-bundle",
        research_retrieved_at or observed_at,
        provider="eodhd-sec-edgar" if forward else "local",
        acquisition_method="live-api-capture" if forward else "checked-in-or-frozen-local",
    )
    ohlcv_source = storage.insert_opaque_artifact(
        artifact_type="local.eodhd-ohlcv.v1",
        payload=bars_bytes,
        media_type="application/json",
    )
    ohlcv_observation, ohlcv_raw = _observe(
        storage, attempt, ohlcv_source, ticker.lower(), "ohlcv",
        price_retrieved_at or observed_at,
        provider="eodhd" if forward else "local",
        acquisition_method="live-api-capture" if forward else "checked-in-or-frozen-local",
    )
    proof = _artifact(storage, "local.multiticker-source-proof.v1", {
        "ticker": ticker,
        "as_of": as_of,
        "research_source_sha256": hashlib.sha256(source_payload).hexdigest(),
        "ohlcv_sha256": hashlib.sha256(bars_bytes).hexdigest(),
        "evidence": [
            {"identifier": item.get("identifier"), "raw_path": item.get("raw_path"),
             "content_sha256": item.get("content_sha256")}
            for item in pack.company_input["evidence"]
        ],
    })
    source_class = _classify_raw(
        storage, attempt, pit_policy, source, source_observation, source_raw,
        ticker, observed_at, (proof.artifact_id,), reference=reference, forward=forward,
    )
    ohlcv_source_class = _classify_raw(
        storage, attempt, pit_policy, ohlcv_source, ohlcv_observation, ohlcv_raw,
        ticker, observed_at, (proof.artifact_id,), reference=reference, forward=forward,
    )
    facts_version = facts.get("facts_contract_version")
    facts_kind = (
        "ledger.usa-v2-facts.v3" if facts_version == "3" else
        "ledger.usa-v2-facts.v2" if facts_version == "2" else
        "ledger.usa-v2-facts.v1"
    )
    facts_artifact = _artifact(storage, facts_kind, _canonical_fact_numbers(facts))
    facts_node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        artifact_id=facts_artifact.artifact_id,
        parents=(DerivationParent(source_raw.derivation_node_id, "SOURCE_FACTS"),),
    )
    facts_class = _classify_derived(
        storage, attempt, pit_policy, facts_node, (source_class,), reference=reference,
    )
    ohlcv_artifact = _artifact(storage, "ledger.usa-setup-v1-ohlcv.v1", {
        "ticker": ticker,
        "bars": [
            {"date": item["date"], **{
                field: canonical_decimal(item[field])
                for field in ("open", "high", "low", "close", "volume")
            }}
            for item in bars
        ],
    })
    ohlcv_node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        artifact_id=ohlcv_artifact.artifact_id,
        parents=(DerivationParent(ohlcv_raw.derivation_node_id, "OHLCV"),),
    )
    ohlcv_class = _classify_derived(
        storage, attempt, pit_policy, ohlcv_node, (ohlcv_source_class,), reference=reference,
    )
    return {
        "pit_policy": pit_policy,
        "facts_artifact": facts_artifact,
        "facts_node": facts_node,
        "facts_class": facts_class,
        "ohlcv_node": ohlcv_node,
        "ohlcv_class": ohlcv_class,
        "source_observation": source_observation,
        "source_raw": source_raw,
        "ohlcv_observation": ohlcv_observation,
        "ohlcv_raw": ohlcv_raw,
        "classifications": [source_class, ohlcv_source_class, facts_class, ohlcv_class],
    }


def _persist_completed_pipeline(
    storage, root, code_commit, attempt, ticker, thesis, setup_record, provider,
    persisted, method, setup_policy, *, reference: str = REFERENCE,
    research_as_of_at: str | None = None,
):
    analyst = _interaction(
        storage, root, code_commit, attempt,
        trace=provider.invocations[0],
        stage_value=provider.analyst_stage,
        facts_artifact=persisted["facts_artifact"],
        facts_node=persisted["facts_node"],
        ordinal=1,
    )
    critic_stage = {
        "notes": list(thesis.critic_notes),
        "status": thesis.status,
        "evidence_confidence": thesis.evidence_confidence,
        "thesis_strength": thesis.thesis_strength,
        "event_assessments": list(thesis.event_assessments),
        "revised_analysis": {
            **{field: getattr(thesis, field) for field in ANALYSIS_FIELDS},
            "claim_refs": [
                {key: value for key, value in item.items()
                 if key != "validation_errors"}
                for item in thesis.claim_refs
            ],
        },
        "critic_status": thesis.critic_status,
        "critic_thesis_strength": thesis.critic_thesis_strength,
        "critic_evidence_confidence": thesis.critic_evidence_confidence,
    }
    critic = _interaction(
        storage, root, code_commit, attempt,
        trace=provider.invocations[1],
        stage_value=critic_stage,
        facts_artifact=persisted["facts_artifact"],
        facts_node=persisted["facts_node"],
        ordinal=2,
        analyst=analyst,
    )
    pit_policy = persisted["pit_policy"]
    analyst_raw_class = _classify_llm_raw(
        storage, attempt, pit_policy, analyst, reference=reference,
    )
    analyst_stage_class = _classify_derived(
        storage, attempt, pit_policy, analyst["stage_node"],
        (analyst_raw_class, persisted["facts_class"]), reference=reference,
    )
    critic_raw_class = _classify_llm_raw(
        storage, attempt, pit_policy, critic, reference=reference,
    )
    critic_stage_class = _classify_derived(
        storage, attempt, pit_policy, critic["stage_node"],
        (critic_raw_class, persisted["facts_class"], analyst_stage_class), reference=reference,
    )
    research_artifact = _artifact(
        storage, "ledger.usa-v2-research-result.v1", research_result_value(thesis),
    )
    research = storage.create_research_record(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        subject_key=f"ticker:{ticker}",
        content_artifact_id=research_artifact.artifact_id,
        research_method_id=method.research_method_id,
        as_of_at=research_as_of_at or reference,
        parents=(
            DerivationParent(persisted["facts_node"].derivation_node_id, "FACTS"),
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
        (persisted["facts_class"], analyst_stage_class, critic_stage_class), reference=reference,
    )
    setup_artifact = _artifact(
        storage, "ledger.usa-setup-v1-result.v1", setup_result_value(setup_record),
    )
    setup = storage.create_setup(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        instrument_id=ticker,
        setup_kind=setup_record.setup_type,
        signal_at=provider.invocations[1].finished_at,
        setup_policy_id=setup_policy.setup_policy_id,
        result_artifact_id=setup_artifact.artifact_id,
        research_id=research.research_id,
        research_derivation_node_id=research.derivation_node_id,
        ohlcv_derivation_node_id=persisted["ohlcv_node"].derivation_node_id,
    )
    setup_node = storage.get_derivation_node(setup.derivation_node_id)
    setup_class = _classify_derived(
        storage, attempt, pit_policy, setup_node,
        (research_class, persisted["ohlcv_class"]), reference=reference,
    )
    for artifact in (research_artifact, setup_artifact):
        storage.attach_attempt_artifact(
            attempt_id=attempt.attempt_id,
            fence_token=attempt.fence_token,
            artifact_id=artifact.artifact_id,
            role="OUTPUT",
        )
    classifications = [
        *persisted["classifications"], analyst_raw_class, analyst_stage_class,
        critic_raw_class, critic_stage_class, research_class, setup_class,
    ]
    run = storage.commit_run(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        output_artifact_ids=(research_artifact.artifact_id, setup_artifact.artifact_id),
        run_inputs=(
            RunInputBinding(
                persisted["source_observation"].input_observation_id,
                "RESEARCH_SOURCE", persisted["source_raw"].derivation_node_id,
            ),
            RunInputBinding(
                persisted["ohlcv_observation"].input_observation_id,
                "OHLCV_SOURCE", persisted["ohlcv_raw"].derivation_node_id,
            ),
            RunInputBinding(
                analyst["observation"].input_observation_id,
                "ANALYST_RESPONSE", analyst["raw_node"].derivation_node_id,
            ),
            RunInputBinding(
                critic["observation"].input_observation_id,
                "CRITIC_RESPONSE", critic["raw_node"].derivation_node_id,
            ),
        ),
        derivation_node_ids=(setup_node.derivation_node_id,),
        derivation_node_classification_ids=tuple(
            item.derivation_node_classification_id for item in classifications
        ),
    )
    return {"run_id": run.run_id, "research_id": research.research_id,
            "setup_id": setup.setup_id}


def _interaction(
    storage, root, code_commit, attempt, *, trace, stage_value,
    facts_artifact, facts_node, ordinal, analyst=None,
):
    expected_schema = (
        analyst_response_schema() if trace.role == "ANALYST" else critic_response_schema()
    )
    schema = _artifact(
        storage, "ledger.llm-response-schema.v1",
        expected_schema,
        schema=True,
    )
    prompt = storage.insert_artifact(
        artifact_type="ledger.llm-effective-prompt.v1",
        canonicalization_version=IDENTITY,
        payload=trace.prompt.encode("utf-8"),
        media_type="text/plain;charset=utf-8",
    )
    items = [{
        "context_role": "FACTS",
        "artifact_id": facts_artifact.artifact_id,
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
        "interaction_role": trace.role, "items": items,
    })
    provenance = dict(trace.provenance or {
        "provider": "OPENAI_CODEX_CLI", "transport": "CODEX_CLI_STDIN",
        "model": "gpt-5.6-sol", "model_version": "codex-cli:gpt-5.6-sol",
        "timeout_seconds": 240, "sandbox": "read-only",
        "skip_git_repo_check": True, "ignore_user_config": True,
    })
    parameters = _artifact(storage, "ledger.llm-invocation-parameters.v1", {
        "schema_name": "ledger.llm-invocation-parameters", "schema_version": "1",
        **provenance, "ephemeral": True,
        "response_schema_artifact_id": schema.artifact_id,
        "temperature": None, "top_p": None, "seed": None,
        "max_output_tokens": None, "reasoning_effort": None, "tool_mode": "NONE",
    })
    code = _artifact(storage, "ledger.code-manifest.v1", {
        "schema_name": "ledger.code-manifest", "schema_version": "1",
        "repository": "earnings-trade-backtest", "git_commit": code_commit,
        "source_files": [
            {"path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
            for path in CODE_PATHS
        ],
    })
    config = _artifact(
        storage, "ledger.usa-v2-invocation-config.v1", CONFIG_DEFINITION,
    )
    request = _artifact(storage, "ledger.llm-invocation-request.v1", {
        "schema_name": "ledger.llm-invocation-request", "schema_version": "1",
        "interaction_role": trace.role, "provider": provenance["provider"],
        "model": provenance["model"], "model_version": provenance["model_version"],
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
        payload=canonicalize_json(trace.parsed_value),
        media_type="application/json",
    )
    observation, raw_node = _observe(
        storage, attempt, raw, "llm", f"{trace.role.lower()}-result", trace.finished_at,
    )
    parsed = _artifact(storage, "ledger.llm-parsed-response.v1", {
        "schema_name": "ledger.llm-parsed-response", "schema_version": "1",
        "interaction_role": trace.role,
        "raw_response_artifact_id": raw.artifact_id,
        "response_schema_artifact_id": schema.artifact_id,
        "parsed_value": trace.parsed_value,
    })
    stage = _artifact(storage, "ledger.usa-v2-stage-result.v1", {
        "schema_name": "ledger.usa-v2-stage-result", "schema_version": "1",
        "llm_interaction_id": interaction_id,
        "interaction_role": trace.role,
        "parsed_response_artifact_id": parsed.artifact_id,
        "result": stage_value,
    })
    parents = [
        DerivationParent(raw_node.derivation_node_id, "RAW_RESPONSE"),
        DerivationParent(facts_node.derivation_node_id, "FACTS"),
    ]
    if analyst is not None:
        parents.append(DerivationParent(
            analyst["stage_node"].derivation_node_id, "ANALYST_RESULT",
        ))
    stage_node = storage.create_normalized_fact_node(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        artifact_id=stage.artifact_id,
        parents=tuple(parents),
    )
    success = _artifact(storage, "ledger.llm-invocation-success.v1", {
        "schema_name": "ledger.llm-invocation-success", "schema_version": "1",
        "llm_interaction_id": interaction_id,
        "raw_response_artifact_id": raw.artifact_id,
        "parsed_response_artifact_id": parsed.artifact_id,
        "effective_stage_result_artifact_id": stage.artifact_id,
    })
    interaction = storage.create_llm_interaction(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        ordinal=ordinal,
        request_artifact_id=request.artifact_id,
        response_artifact_id=success.artifact_id,
        error_artifact_id=None,
        status="SUCCEEDED",
        started_at=trace.started_at,
        finished_at=trace.finished_at,
        llm_interaction_id=interaction_id,
        provider=str(provenance["provider"]), model=str(provenance["model"]),
        model_version=str(provenance["model_version"]),
    )
    return {
        "interaction": interaction, "raw": raw, "observation": observation,
        "raw_node": raw_node, "stage_artifact": stage, "stage_node": stage_node,
    }


def _artifact(storage, kind, value, *, schema=False):
    return storage.insert_json_artifact(
        artifact_type=kind,
        value=value,
        media_type="application/schema+json" if schema else "application/json",
    )


def _canonical_fact_numbers(value):
    """Make normalized pre-LLM facts acceptable to canonical JSON storage."""
    if isinstance(value, float):
        return canonical_decimal(value)
    if isinstance(value, list):
        return [_canonical_fact_numbers(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_fact_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_fact_numbers(item) for key, item in value.items()}
    return value


def _observe(
    storage, attempt, artifact, dataset, record, when, *, provider="local",
    acquisition_method="checked-in-or-frozen-local",
):
    source_id = f"{provider}:{dataset}:v1"
    observation = storage.create_input_observation(
        artifact_id=artifact.artifact_id,
        source_id=source_id,
        source_record_key=record,
        source_published_at=None,
        retrieved_at=when,
        availability_basis="RETRIEVED_AT_FALLBACK",
        effective_available_at=when,
        observed_by_attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        source_metadata={
            "schema_version": "1", "provider": provider, "dataset_name": dataset,
            "source_record_key": record, "acquisition_method": acquisition_method,
            "availability_rule_id": "retrieval-fallback",
            "availability_rule_version": "1", "evidence_artifact_ids": [],
            "provider_metadata": {}, "future_effective_at": None,
        },
    )
    node = storage.create_raw_derivation_node(
        attempt_id=attempt.attempt_id,
        fence_token=attempt.fence_token,
        input_observation_id=observation.input_observation_id,
    )
    return observation, node


def _register_shared_definitions(storage, code_commit, *, facts_version="1"):
    v3 = facts_version == "3"
    method_definition = _artifact(
        storage,
        "ledger.usa-v2-research-method-definition.v2" if v3
        else "ledger.usa-v2-research-method-definition.v1",
        RESEARCH_METHOD_DEFINITION_V3 if v3 else RESEARCH_METHOD_DEFINITION,
    )
    method = storage.register_research_method(
        definition_artifact_id=method_definition.artifact_id,
        code_commit=code_commit,
    )
    setup_definition = _artifact(
        storage, "ledger.usa-setup-v1-policy-definition.v1", SETUP_POLICY_DEFINITION,
    )
    setup_policy = storage.register_setup_policy(
        definition_artifact_id=setup_definition.artifact_id,
        code_commit=code_commit,
    )
    pit_definition = _artifact(
        storage, "ledger.pit-classification-policy-definition.v1",
        pit_classification_policy_definition_v2(),
    )
    pit_policy = storage.register_pit_classification_policy(
        definition_artifact_id=pit_definition.artifact_id,
        code_commit=code_commit,
    )
    return {
        "research_method": method,
        "setup_policy": setup_policy,
        "pit_policy": pit_policy,
    }


def _classify_raw(
    storage, attempt, policy, artifact, observation, node, ticker,
    reconstruction_at, support, *, reference=REFERENCE, forward=False,
):
    namespace = f"local.usa-v2.{ticker.lower()}"
    key = observation.source_record_key
    legacy_id = "legacy:sha256:" + hashlib.sha256(canonicalize_json({
        "legacy_namespace": namespace, "legacy_record_key": key,
    })).hexdigest()
    evidence_value = {
        "schema_name": "ledger.pit-classification-evidence", "schema_version": "1",
        "derivation_node_id": node.derivation_node_id,
        "input_observation_id": observation.input_observation_id,
        "artifact_id": artifact.artifact_id, "pit_reference_at": reference,
        "record_origin_evidence_kind": ("LEDGER_AUTHORIZED_CAPTURE" if forward else "LEGACY_IMPORT"),
        "legacy_namespace": None if forward else namespace,
        "legacy_record_key": None if forward else key,
        "legacy_record_id": None if forward else legacy_id,
        "pit_evidence_kind": ("LEDGER_CONTEMPORANEOUS_CAPTURE" if forward
                              else "POST_REFERENCE_RECONSTRUCTION"),
        "verifier_id": ("LEDGER_AUTHORIZED_CAPTURE_V1" if forward
                        else "LEDGER_RECONSTRUCTION_EVIDENCE_V1"),
        "archive_captured_at": observation.retrieved_at if forward else None,
        "reconstruction_completed_at": None if forward else reconstruction_at,
        "supporting_artifact_ids": [] if forward else sorted(support),
        "rationale_code": ("CONTEMPORANEOUS_CAPTURE_VERIFIED" if forward
                           else "POST_REFERENCE_RECONSTRUCTION_VERIFIED"),
    }
    evidence = _artifact(storage, "ledger.pit-classification-evidence.v1", evidence_value)
    for artifact_id in (*support, evidence.artifact_id):
        storage.attach_attempt_artifact(
            attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
            artifact_id=artifact_id, role="DIAGNOSTIC",
        )
    return storage.create_raw_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=node.derivation_node_id, pit_reference_at=reference,
        classification_policy_id=policy.classification_policy_id,
        evidence_artifact_id=evidence.artifact_id,
    )


def _classify_llm_raw(storage, attempt, policy, interaction, *, reference=REFERENCE):
    observation = interaction["observation"]
    node = interaction["raw_node"]
    artifact = interaction["raw"]
    evidence = _artifact(storage, "ledger.pit-classification-evidence.v1", {
        "schema_name": "ledger.pit-classification-evidence", "schema_version": "1",
        "derivation_node_id": node.derivation_node_id,
        "input_observation_id": observation.input_observation_id,
        "artifact_id": artifact.artifact_id, "pit_reference_at": reference,
        "record_origin_evidence_kind": "LEDGER_AUTHORIZED_CAPTURE",
        "legacy_namespace": None, "legacy_record_key": None, "legacy_record_id": None,
        "pit_evidence_kind": "PIT_IRRELEVANT_CONTENT",
        "verifier_id": "LEDGER_PIT_IRRELEVANCE_V1",
        "archive_captured_at": None, "reconstruction_completed_at": None,
        "supporting_artifact_ids": [],
        "rationale_code": "PIT_NOT_APPLICABLE_BY_ARTIFACT_KIND",
    })
    storage.attach_attempt_artifact(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=evidence.artifact_id, role="DIAGNOSTIC",
    )
    return storage.create_raw_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=node.derivation_node_id, pit_reference_at=reference,
        classification_policy_id=policy.classification_policy_id,
        evidence_artifact_id=evidence.artifact_id,
    )


def _classify_derived(storage, attempt, policy, node, parents, *, reference=REFERENCE):
    return storage.create_derived_node_classification(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        derivation_node_id=node.derivation_node_id, pit_reference_at=reference,
        classification_policy_id=policy.classification_policy_id,
        parent_classification_ids=tuple(
            item.derivation_node_classification_id for item in parents
        ),
    )


def _record_failure(storage, attempt, ticker, exc):
    details = _artifact(storage, "local.multiticker-failure.v1", {
        "ticker": ticker,
        "failure_class": type(exc).__name__,
        "failure_message": str(exc)[:1000],
    })
    storage.attach_attempt_artifact(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        artifact_id=details.artifact_id, role="FAILED_ATTEMPT_OUTPUT",
    )
    storage.terminalize_attempt(
        attempt_id=attempt.attempt_id, fence_token=attempt.fence_token,
        status="FAILED", actor_identity="trinity-multiticker-pilot-v1",
        failure_class=type(exc).__name__, failure_message=str(exc)[:1000],
        details_artifact_id=details.artifact_id,
    )


def _ticker_result(
    notification: TelegramNotification, message: str, sent: bool,
) -> TickerResult:
    operational = notification.setup_type != "NO_SETUP"
    return TickerResult(
        ticker=notification.ticker,
        run_status="SUCCESS" if operational else "NO_SETUP",
        research_status=notification.research_status,
        evidence_confidence=notification.evidence_confidence,
        thesis_strength=notification.thesis_strength,
        technical_regime=notification.technical_regime,
        setup_type=notification.setup_type,
        entry=notification.entry if operational else "-",
        stop=notification.stop if operational else "-",
        tp1=notification.tp1 if operational else "-",
        tp2=notification.tp2 if operational else "-",
        rr1=notification.rr1 if operational else "-",
        rr2=notification.rr2 if operational else "-",
        resolved_record_class=notification.resolved_record_class,
        resolved_pit_class=notification.resolved_pit_class,
        run_id=notification.run_id,
        research_id=notification.research_id,
        setup_id=notification.setup_id,
        telegram_message=message,
        telegram_sent=sent,
    )


def _send_once(
    setup_id: str,
    message: str,
    sent_setup_ids: set[str],
    sender: Callable[[str], None],
) -> None:
    if setup_id in sent_setup_ids:
        raise RuntimeError("duplicate Telegram send suppressed")
    sender(message)
    sent_setup_ids.add(setup_id)


def render_summary(results: Sequence[TickerResult]) -> str:
    headers = (
        "Ticker", "Research status", "Evidence confidence", "Thesis strength",
        "Technical regime", "Setup type", "Entry", "Stop", "TP1", "TP2",
        "RR1", "RR2", "Resolved record class", "Resolved PIT class", "Run status",
    )
    rows = [headers]
    rows.extend((
        item.ticker, item.research_status, item.evidence_confidence,
        item.thesis_strength, item.technical_regime, item.setup_type,
        item.entry, item.stop, item.tp1, item.tp2, item.rr1, item.rr2,
        item.resolved_record_class, item.resolved_pit_class, item.run_status,
    ) for item in results)
    widths = [max(len(str(row[index])) for row in rows) for index in range(len(headers))]
    return "\n".join(
        " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )


def _git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        capture_output=True, check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 40:
        raise RuntimeError("unable to resolve repository HEAD")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _utc_after(*, minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an explicit TRINITY multi-ticker pilot")
    parser.add_argument("--ledger-db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument(
        "--forward", action="store_true",
        help="acquire fresh EODHD/SEC data; V1 permits dry-run Telegram only",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="render without Telegram API calls")
    mode.add_argument("--send-telegram", action="store_true", help="send each committed ticker")
    args = parser.parse_args(argv)
    try:
        if args.forward:
            if not args.dry_run:
                raise ValueError("forward pilot V1 is dry-run only")
            from trinity.pilots.forward import run_forward_pilot
            batch = run_forward_pilot(
                args.ledger_db, args.tickers, dry_run=True,
            )
        else:
            batch = run_multiticker_pilot(
                args.ledger_db, args.tickers, dry_run=args.dry_run,
            )
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    for item in batch.results:
        if item.telegram_message is not None:
            print(item.telegram_message)
            print()
        elif item.error:
            print(f"{item.ticker}: FAILED: {item.error}")
    if args.forward:
        from trinity.pilots.forward import render_forward_summary
        print(render_forward_summary(batch.results))
        print("Prices: EODHD | News: EODHD | Primary evidence: SEC EDGAR")
    else:
        print(render_summary(batch.results))
    return 1 if any(item.run_status == "FAILED" for item in batch.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
