from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from trinity.ledger import LedgerStorage
from trinity.pilots.forward import render_forward_summary, run_forward_pilot
from trinity.pilots.jnj_telegram import _role_values
from trinity.pilots.multiticker import RecordingUSAProvider
from trinity.usa_forward import (
    EODHDSECForwardProvider,
    ForwardAcquisitionError,
    ForwardTickerSources,
    validate_forward_bars,
)
from trinity.usa_setup_v1 import PRICE_CACHE, THESIS_CACHE, _load_latest_thesis
from trinity.usa_v2 import AS_OF, load_company


def _llm(ticker: str) -> RecordingUSAProvider:
    thesis = _load_latest_thesis(ticker, AS_OF, THESIS_CACHE)
    analyst, critic, _ = _role_values(thesis)
    responses = iter((analyst, critic))
    return RecordingUSAProvider(lambda _prompt, _schema: next(responses))


class _FixtureSource:
    def acquire(self, ticker: str) -> ForwardTickerSources:
        if ticker == "JPM":
            raise ForwardAcquisitionError("JPM: injected missing fresh source")
        pack = load_company(ticker, AS_OF)
        company_input = deepcopy(pack.company_input)
        for event in company_input["events"]:
            facts = event["facts"]
            facts["source_class"] = (
                "PRIMARY_SEC" if facts.get("primary_source") is True else "ISSUER_RELEASE"
            )
            facts["confirmation_state"] = "CONFIRMED"
        pack = replace(pack, company_input=company_input)
        raw = (PRICE_CACHE / f"{ticker}.json").read_bytes()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        return ForwardTickerSources(
            ticker=ticker, as_of=AS_OF, pack=pack, bars=json.loads(raw),
            price_raw=raw, research_archive=f"fresh:{ticker}".encode(),
            price_retrieved_at=now, research_retrieved_at=now,
            fresh_price_timestamp=AS_OF,
            freshest_evidence_timestamp=max(
                str(item.get("accepted_at") or item.get("published_at") or "")
                for item in pack.company_input["evidence"]
            ),
        )


@pytest.fixture(scope="module")
def forward_batch(tmp_path_factory):
    database = tmp_path_factory.mktemp("forward") / "pilot.sqlite3"
    started = datetime.now(timezone.utc)
    batch = run_forward_pilot(
        database, ["AAPL", "JPM", "JNJ"], dry_run=True,
        source_provider_factory=lambda _root: _FixtureSource(),
        llm_provider_factory=_llm,
    )
    return batch, started


def _bars(latest: date, count: int = 201):
    first = latest - timedelta(days=count - 1)
    return [
        {"date": (first + timedelta(days=index)).isoformat(), "open": 100 + index,
         "high": 102 + index, "low": 99 + index, "close": 101 + index,
         "adjusted_close": 101 + index, "volume": 1_000_000 + index}
        for index in range(count)
    ]


def test_stale_and_missing_price_data_are_rejected():
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    with pytest.raises(ForwardAcquisitionError, match="fewer than 201"):
        validate_forward_bars("AAPL", [], retrieved_at=now)
    with pytest.raises(ForwardAcquisitionError, match="stale"):
        validate_forward_bars("AAPL", _bars(date(2026, 9, 20)), retrieved_at=now)


def test_forward_mode_is_dry_run_only_and_makes_no_provider_call(tmp_path):
    calls = []
    with pytest.raises(ValueError, match="dry-run only"):
        run_forward_pilot(
            tmp_path / "never.sqlite3", ["AAPL"], dry_run=False,
            source_provider_factory=lambda root: calls.append(root),
        )
    assert calls == []


def test_forward_continuity_preserves_every_luna_selected_evidence_id(tmp_path):
    continuity = tmp_path / "luna.json"
    continuity.write_text(json.dumps({"results": [{
        "ticker": "AAPL", "decision": "ESCALATE",
        "selected_evidence_ids": [
            "news:AAPL:0123456789abcdef",
            "earnings-calendar:AAPL.US:2026-10-29",
        ],
    }]}), encoding="utf-8")
    provider = EODHDSECForwardProvider(
        tmp_path / "sources", api_key="test-only", continuity_path=continuity,
    )
    assert provider._continuity_evidence_ids("AAPL") == (
        "news:AAPL:0123456789abcdef",
        "earnings-calendar:AAPL.US:2026-10-29",
    )


def test_mocked_fresh_provider_uses_only_execution_paths(monkeypatch, tmp_path):
    fixed = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    price = json.dumps(_bars(date(2026, 10, 1))).encode()
    news = json.dumps([{
        "date": "2026-10-01T10:00:00+00:00", "title": "Example",
        "content": "Example", "symbols": ["AAPL.US"], "link": "https://example.test",
    }]).encode()

    class Response:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class Session:
        headers = {}
        def get(self, url, **_kwargs):
            return Response(price if "/api/eod/" in url else news)

    def sec_acquirer(as_of, *, cache_dir, tickers, now, **_kwargs):
        assert tickers == ("AAPL",) and as_of == "2026-10-01" and now == fixed
        path = Path(cache_dir) / "manifest.json"
        path.write_text("{}", encoding="utf-8")
        return path

    historical = load_company("AAPL", AS_OF)
    seen = {}

    def loader(ticker, as_of, **kwargs):
        seen.update(kwargs)
        assert ticker == "AAPL" and as_of == "2026-10-01"
        assert kwargs["forward"] is True
        assert str(kwargs["prices_dir"]).startswith(str(tmp_path.resolve()))
        assert str(kwargs["news_dir"]).startswith(str(tmp_path.resolve()))
        assert str(kwargs["documents_dir"]).startswith(str(tmp_path.resolve()))
        evidence = [
            {**historical.company_input["evidence"][0]},
            {"source": "SEC EDGAR filing", "published_at": "2026-09-30",
             "accepted_at": "2026-09-30T20:00:00Z"},
        ]
        company_input = {**historical.company_input, "as_of": as_of, "evidence": evidence}
        return replace(historical, as_of=as_of, company_input=company_input)

    monkeypatch.setattr("trinity.usa_forward.load_company", loader)
    provider = EODHDSECForwardProvider(
        tmp_path, api_key="test-only", session=Session(),
        clock=lambda: fixed, sec_acquirer=sec_acquirer,
    )
    acquired = provider.acquire("AAPL")
    assert acquired.price_retrieved_at == "2026-10-02T12:00:00.000000Z"
    assert acquired.research_retrieved_at == "2026-10-02T12:00:00.000000Z"
    assert acquired.price_raw == price
    assert seen and PRICE_CACHE not in Path(seen["prices_dir"]).parents


def test_missing_required_source_data_is_safe(monkeypatch, tmp_path):
    fixed = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    price = json.dumps(_bars(date(2026, 10, 1))).encode()

    class Response:
        def __init__(self, content):
            self.content = content
        def raise_for_status(self):
            return None

    class Session:
        headers = {}
        def get(self, url, **_kwargs):
            return Response(price if "/api/eod/" in url else b"{}")

    provider = EODHDSECForwardProvider(
        tmp_path, api_key="test-only", session=Session(), clock=lambda: fixed,
        sec_acquirer=lambda *args, **kwargs: Path(kwargs["cache_dir"]) / "manifest.json",
    )
    with pytest.raises(ForwardAcquisitionError, match="news response has invalid shape"):
        provider.acquire("AAPL")


def test_mocked_forward_success_has_independent_native_lineage_and_real_timestamps(
    forward_batch,
):
    batch, started = forward_batch
    assert [item.ticker for item in batch.results] == ["AAPL", "JPM", "JNJ"]
    successful = [item for item in batch.results if item.run_status != "FAILED"]
    failed = next(item for item in batch.results if item.ticker == "JPM")
    assert failed.run_status == "FAILED"
    assert "injected missing fresh source" in failed.error
    assert all(item.run_status in {"SUCCESS", "NO_SETUP"} for item in successful)
    assert len({item.run_id for item in successful}) == 2
    assert len({item.research_id for item in successful}) == 2
    assert len({item.setup_id for item in successful}) == 2
    assert all(item.resolved_record_class == "LEDGER_NATIVE" for item in successful)
    assert all(item.resolved_pit_class == "ARCHIVED_POINT_IN_TIME" for item in successful)
    assert batch.telegram_render_count == 2
    assert not any(item.telegram_sent for item in batch.results)

    with LedgerStorage.open(batch.ledger_db) as storage:
        observations = storage.list_input_observations()
        source_observations = [item for item in observations if item.source_record_key in {
            "research-source-bundle", "ohlcv",
        }]
        assert len(source_observations) == 4
        assert all(
            datetime.fromisoformat(item.retrieved_at.replace("Z", "+00:00")) >= started
            for item in source_observations
        )
        artifacts = {item.artifact_id for item in source_observations}
        assert len(artifacts) == 4


def test_forward_summary_and_historical_defaults_remain_available(forward_batch):
    batch, _ = forward_batch
    summary = render_forward_summary(batch.results)
    for heading in (
        "Fresh price timestamp", "Freshest evidence timestamp", "Record class",
        "PIT class", "Run status",
    ):
        assert heading in summary
    historical = load_company("JNJ", AS_OF)
    assert historical.as_of == AS_OF
    assert "frozen" in historical.company_input["evidence"][0]["source"].lower()
