"""Forward-data execution for the explicit eight-ticker TRINITY pilot."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from typing import Callable, Sequence
import uuid

from trinity.italia_v1 import analyze_company, build_facts_v3
from trinity.ledger import LedgerStorage
from trinity.notifications.telegram import load_committed_setup, render_telegram_message
from trinity.paths import price_snapshot_root
from trinity.pilots.multiticker import (
    BatchResult,
    RecordingUSAProvider,
    TickerResult,
    _git_head,
    _persist_completed_pipeline,
    _persist_inputs,
    _record_failure,
    _register_shared_definitions,
    _ticker_result,
    _validate_tickers,
)
from trinity.twelvedata_prices import PRICE_PROVIDER, ValidatedPriceSnapshot
from trinity.usa_forward import EODHDNewsSECForwardProvider, ForwardTickerSources
from trinity.usa_setup_v1 import build_setup
from trinity.usa_v2 import with_expectation_comparisons


DEFAULT_FORWARD_DATABASE = Path("data/local/trinity_forward_pilot.sqlite3")
DEFAULT_SOURCE_ROOT = Path("data/local/trinity_forward_sources")


def _default_source_provider(root: Path) -> EODHDNewsSECForwardProvider:
    return EODHDNewsSECForwardProvider(
        root, price_snapshot=ValidatedPriceSnapshot(price_snapshot_root()),
    )


def run_forward_pilot(
    database: str | Path,
    tickers: Sequence[str],
    *,
    dry_run: bool,
    repository_root: str | Path | None = None,
    source_provider_factory: Callable[[Path], object] = _default_source_provider,
    llm_provider_factory: Callable[[str], RecordingUSAProvider] = (
        lambda _ticker: RecordingUSAProvider()
    ),
    source_root: str | Path = DEFAULT_SOURCE_ROOT,
) -> BatchResult:
    """Acquire and commit each explicit ticker; Telegram is render-only in V1."""
    if not dry_run:
        raise ValueError("forward pilot V1 is dry-run only; live Telegram sending is disabled")
    selected = _validate_tickers(tickers)
    root = (Path(repository_root).resolve() if repository_root is not None
            else Path(__file__).resolve().parents[2])
    database_path = Path(database).resolve()
    if database_path.exists():
        raise FileExistsError(f"pilot Ledger already exists: {database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    acquisition_root = Path(source_root).resolve() / database_path.stem
    source_provider = source_provider_factory(acquisition_root)
    code_commit = _git_head(root)
    # Match the existing USA V2 production entry point on Windows. Codex CLI
    # needs an explicit home even when USERPROFILE exists in the parent process.
    user_profile = os.environ.get("USERPROFILE") or str(root.parent)
    os.environ.setdefault("HOME", user_profile)
    os.environ.setdefault("CODEX_HOME", str(Path(user_profile) / ".codex"))
    results: list[TickerResult] = []

    with LedgerStorage.open(database_path) as storage:
        shared = _register_shared_definitions(storage, code_commit, facts_version="3")
        for ticker in selected:
            attempt = None
            sources: ForwardTickerSources | None = None
            try:
                reference = _future_reference()
                attempt = _new_forward_attempt(storage, ticker, code_commit, reference)
                sources = source_provider.acquire(ticker)
                pack = sources.pack
                research_as_of = sources.research_as_of or sources.as_of
                if (
                    sources.ticker != ticker
                    or pack.ticker != ticker
                    or pack.as_of != research_as_of
                    or pack.company_input.get("ticker") != ticker
                    or pack.company_input.get("as_of") != research_as_of
                ):
                    raise ValueError(f"{ticker}: fresh source bundle identity mismatch")
                company_input = with_expectation_comparisons(pack.company_input)
                facts = build_facts_v3(company_input, research_as_of)
                persisted = _persist_inputs(
                    storage, attempt, ticker, pack, facts, sources.bars,
                    sources.price_raw, shared["pit_policy"],
                    as_of=research_as_of, reference=reference,
                    research_source_bytes=sources.research_archive,
                    research_retrieved_at=sources.research_retrieved_at,
                    price_retrieved_at=sources.price_retrieved_at,
                    price_provider=sources.price_provider,
                    price_provider_metadata={
                        "snapshot_id": sources.price_snapshot_id,
                        "snapshot_session": sources.price_snapshot_session,
                        "snapshot_sha256": sources.price_snapshot_sha256,
                    },
                    forward=True,
                )
                provider = llm_provider_factory(ticker)
                thesis = analyze_company(
                    company_input, research_as_of, provider,
                    facts_builder=build_facts_v3,
                )
                thesis = replace(
                    thesis, **provider.revisions,
                    claim_refs=provider.validated_claim_refs,
                    rejected_claim_refs=provider.rejected_claim_refs,
                )
                if (
                    len(provider.invocations) != 2
                    or provider.analyst_stage is None
                    or provider.critic_stage is None
                ):
                    raise RuntimeError("Analyst/Critic invocation trace is incomplete")
                setup_input = {**thesis.to_dict(), "as_of": sources.as_of}
                setup_record = build_setup(setup_input, sources.bars, sources.as_of)
                identities = _persist_completed_pipeline(
                    storage, root, code_commit, attempt, ticker, thesis,
                    setup_record, provider, persisted,
                    shared["research_method"], shared["setup_policy"],
                    reference=reference,
                    research_as_of_at=(sources.research_cutoff_utc or
                                       f"{research_as_of}T23:59:59.999999Z"),
                )
                notification = load_committed_setup(
                    storage, setup_id=identities["setup_id"], run_id=identities["run_id"],
                )
                message = render_telegram_message(notification)
                result = _ticker_result(notification, message, False)
                results.append(replace(
                    result,
                    fresh_price_timestamp=sources.fresh_price_timestamp,
                    freshest_evidence_timestamp=sources.freshest_evidence_timestamp,
                    price_provider=sources.price_provider,
                    news_provider=sources.news_provider,
                    primary_evidence_provider=sources.primary_evidence_provider,
                ))
            except Exception as exc:  # one authorized attempt is the isolation boundary
                if attempt is not None and storage.is_attempt_authorized(
                    attempt.attempt_id, attempt.fence_token
                ):
                    _record_failure(storage, attempt, ticker, exc)
                results.append(TickerResult(
                    ticker=ticker, run_status="FAILED",
                    error=f"{type(exc).__name__}: {exc}",
                    fresh_price_timestamp=(sources.fresh_price_timestamp if sources else "-"),
                    freshest_evidence_timestamp=(sources.freshest_evidence_timestamp if sources else "-"),
                    price_provider=PRICE_PROVIDER, news_provider="EODHD",
                    primary_evidence_provider="SEC EDGAR",
                ))
        if storage.connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("forward pilot Ledger failed foreign_key_check")
    return BatchResult(database_path, selected, tuple(results))


def _future_reference() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _new_forward_attempt(storage, ticker: str, code_commit: str, reference: str):
    request = storage.create_run_request(
        request_kind="PIT_SAFE_DECISION",
        analysis_cutoff_at=reference,
        parameters={"forward": True, "ticker": ticker},
        idempotency_key=f"forward-pilot-v1:{ticker}:{uuid.uuid4()}",
        requested_by="trinity-forward-pilot-v1",
        baseline_commit=code_commit,
    )
    return storage.allocate_attempt(
        run_request_id=request.run_request_id,
        worker_identity=f"forward-pilot:{ticker.lower()}",
        code_commit=code_commit,
        environment_fingerprint="pilot:usa-v2:forward:v1",
    )


def render_forward_summary(results: Sequence[TickerResult]) -> str:
    headers = (
        "Ticker", "Fresh price timestamp", "Freshest evidence timestamp",
        "Research status", "Evidence confidence", "Thesis strength", "Regime",
        "Setup", "Entry", "Stop", "TP1", "TP2", "RR1", "RR2",
        "Record class", "PIT class", "Run status",
    )
    rows = [headers]
    rows.extend((
        item.ticker, item.fresh_price_timestamp, item.freshest_evidence_timestamp,
        item.research_status, item.evidence_confidence, item.thesis_strength,
        item.technical_regime, item.setup_type, item.entry, item.stop,
        item.tp1, item.tp2, item.rr1, item.rr2, item.resolved_record_class,
        item.resolved_pit_class, item.run_status,
    ) for item in results)
    widths = [max(len(str(row[index])) for row in rows) for index in range(len(headers))]
    return "\n".join(
        " | ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )
