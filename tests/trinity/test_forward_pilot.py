from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

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
from trinity.usa_v2 import AS_OF, _read_prices, load_company


def _llm(ticker: str) -> RecordingUSAProvider:
    thesis = _load_latest_thesis(ticker, AS_OF, THESIS_CACHE)
    analyst, critic, _ = _role_values(thesis)
    responses = iter((analyst, critic))
    return RecordingUSAProvider(lambda _prompt, _schema: next(responses))


def _responses_llm(ticker: str) -> RecordingUSAProvider:
    provider = _llm(ticker)
    provider.provenance = {
        "provider": "OPENAI_RESPONSES_API", "transport": "RESPONSES_API_HTTPS",
        "model": "gpt-5.6-sol", "model_version": "responses-api:gpt-5.6-sol",
        "timeout_seconds": 300, "sandbox": "not-applicable",
        "skip_git_repo_check": False, "ignore_user_config": False,
    }
    return provider


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
            price_snapshot_id="TWELVEDATA_2026-09-12_RUN_fixture",
            price_snapshot_session=AS_OF,
            price_snapshot_sha256="a" * 64,
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


class _Snapshot:
    snapshot_id = "TWELVEDATA_2026-10-01_RUN_test"
    target_session = "2026-10-01"
    snapshot_sha256 = "a" * 64
    acquisition_completed_at = "2026-10-02T12:00:00.000000Z"
    def bars(self, _ticker):
        return _bars(date.fromisoformat(self.target_session))
    def normalized_bytes(self, ticker):
        return json.dumps(self.bars(ticker)).encode()


class _RetryResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _RetrySession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _retry_provider(tmp_path, outcomes):
    session = _RetrySession(outcomes)
    return EODHDSECForwardProvider(
        tmp_path, price_snapshot=_Snapshot(), api_key="test-only",
        session=session, continuity_path=None,
    ), session


def test_eodhd_get_retries_read_timeout_once_without_changing_request(
    monkeypatch, tmp_path,
):
    raw = b"exact-response-bytes\x00\xff"
    provider, session = _retry_provider(
        tmp_path, [requests.ReadTimeout(), _RetryResponse(raw)],
    )
    sleeps = []
    monkeypatch.setattr("trinity.usa_forward.time.sleep", sleeps.append)
    params = {"api_token": "test-only", "s": "ABBV.US", "limit": 1000}

    assert provider._get("https://example.test/news", params, "ABBV", "news") == raw
    assert sleeps == [1]
    assert len(session.calls) == 2
    assert all(call == ("https://example.test/news", {"params": params, "timeout": 45})
               for call in session.calls)


def test_eodhd_get_retries_two_connection_errors_then_succeeds(monkeypatch, tmp_path):
    provider, session = _retry_provider(
        tmp_path,
        [requests.ConnectionError(), requests.ConnectionError(), _RetryResponse(b"ok")],
    )
    sleeps = []
    monkeypatch.setattr("trinity.usa_forward.time.sleep", sleeps.append)

    assert provider._get("https://example.test/eod", {"fmt": "json"}, "DE", "price") == b"ok"
    assert sleeps == [1, 2]
    assert len(session.calls) == 3
    assert all(call[1]["timeout"] == 45 for call in session.calls)


@pytest.mark.parametrize("status", [503, 429])
def test_eodhd_get_retries_transient_http_status(monkeypatch, tmp_path, status):
    provider, session = _retry_provider(
        tmp_path, [_RetryResponse(b"temporary", status), _RetryResponse(b"ok")],
    )
    sleeps = []
    monkeypatch.setattr("trinity.usa_forward.time.sleep", sleeps.append)

    assert provider._get("https://example.test/news", {}, "ABBV", "news") == b"ok"
    assert sleeps == [1]
    assert len(session.calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_eodhd_get_does_not_retry_permanent_http_status(monkeypatch, tmp_path, status):
    provider, session = _retry_provider(
        tmp_path, [_RetryResponse(b"permanent", status)],
    )
    sleeps = []
    monkeypatch.setattr("trinity.usa_forward.time.sleep", sleeps.append)

    with pytest.raises(ForwardAcquisitionError, match=rf"HTTP {status}"):
        provider._get("https://example.test/news", {}, "ABBV", "news")
    assert sleeps == []
    assert len(session.calls) == 1


def test_eodhd_get_stops_after_three_read_timeouts(monkeypatch, tmp_path):
    provider, session = _retry_provider(
        tmp_path, [requests.ReadTimeout(), requests.ReadTimeout(), requests.ReadTimeout()],
    )
    sleeps = []
    monkeypatch.setattr("trinity.usa_forward.time.sleep", sleeps.append)

    with pytest.raises(ForwardAcquisitionError, match=r"request failed \(ReadTimeout\)"):
        provider._get("https://example.test/news", {}, "ABBV", "news")
    assert sleeps == [1, 2]
    assert len(session.calls) == 3


def test_stale_and_missing_price_data_are_rejected():
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    with pytest.raises(ForwardAcquisitionError, match="fewer than 201"):
        validate_forward_bars("AAPL", [], retrieved_at=now)
    with pytest.raises(ForwardAcquisitionError, match="stale"):
        validate_forward_bars("AAPL", _bars(date(2026, 9, 20)), retrieved_at=now)


def test_sol_news_filter_uses_research_cutoff_independently_of_price_cutoff(tmp_path):
    technical = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
    research = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    provider = EODHDSECForwardProvider(
        tmp_path, price_snapshot=_Snapshot(), api_key="test-only",
        session=SimpleNamespace(), decision_cutoff_utc=technical,
        research_cutoff_utc=research,
    )
    raw = json.dumps([
        {"date": "2026-10-10T14:00:00Z", "title": "weekend"},
        {"date": "2026-10-12T11:59:00Z", "title": "premarket"},
        {"date": "2026-10-12T12:00:01Z", "title": "future"},
    ]).encode()
    normalized = provider._normalize_news(
        "AAPL", raw, "2026-10-12T12:00:00Z", provider.research_cutoff_utc,
    )
    assert [item["date"] for item in normalized] == [
        "2026-10-10T14:00:00Z", "2026-10-12T11:59:00Z",
    ]
    assert provider.decision_cutoff_utc == technical
    assert provider.research_cutoff_utc == research


def test_raw_twelve_data_bars_keep_setup_fields_and_null_optional_provenance_slot(tmp_path):
    prices = tmp_path / "prices"
    prices.mkdir()
    bars = _bars(date(2026, 10, 7), count=61)
    for bar in bars:
        bar.pop("adjusted_close")
    (prices / "AAPL.json").write_text(json.dumps(bars), encoding="utf-8")
    price, _evidence = _read_prices("AAPL", "2026-10-07", prices_dir=prices)
    assert set(bars[-1]) == {"date", "open", "high", "low", "close", "volume"}
    assert price["acquisition"]["last_bar"]["adjusted_close"] is None


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
        tmp_path / "sources", price_snapshot=_Snapshot(),
        api_key="test-only", continuity_path=continuity,
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

    calls = []

    class Session:
        headers = {}
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response(news)

    def sec_acquirer(as_of, *, cache_dir, tickers, now, **_kwargs):
        assert tickers == ("AAPL",) and as_of == "2026-10-02" and now == fixed
        assert _kwargs["decision_cutoff_utc"] == fixed
        path = Path(cache_dir) / "manifest.json"
        path.write_text("{}", encoding="utf-8")
        return path

    historical = load_company("AAPL", AS_OF)
    seen = {}

    def loader(ticker, as_of, **kwargs):
        seen.update(kwargs)
        assert ticker == "AAPL" and as_of == "2026-10-01"
        assert kwargs["forward"] is True
        assert kwargs["research_as_of"] == "2026-10-02"
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
        tmp_path, price_snapshot=_Snapshot(), api_key="test-only", session=Session(),
        clock=lambda: fixed, sec_acquirer=sec_acquirer,
        decision_cutoff_utc=datetime(2026, 10, 1, 20, tzinfo=timezone.utc),
        research_cutoff_utc=fixed,
    )
    acquired = provider.acquire("AAPL")
    assert acquired.price_retrieved_at == "2026-10-02T12:00:00.000000Z"
    assert acquired.research_retrieved_at == "2026-10-02T12:00:00.000000Z"
    assert acquired.as_of == "2026-10-01"
    assert acquired.research_as_of == "2026-10-02"
    assert acquired.research_cutoff_utc == "2026-10-02T12:00:00.000000Z"
    assert json.loads(acquired.price_raw) == _Snapshot().bars("AAPL")
    assert [item[0] for item in calls] == ["https://eodhistoricaldata.com/api/news"]
    assert calls[0][1]["params"]["to"] == "2026-10-02"
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
        tmp_path, price_snapshot=_Snapshot(), api_key="test-only",
        session=Session(), clock=lambda: fixed,
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
        methods = storage.connection.execute(
            "SELECT method_version FROM research_method"
        ).fetchall()
        records = storage.connection.execute(
            "SELECT DISTINCT method_version FROM research_record"
        ).fetchall()
        assert [row[0] for row in methods] == ["USA_V2_FACTS_V3"]
        assert [row[0] for row in records] == ["USA_V2_FACTS_V3"]


def test_ledger_ohlcv_observation_records_twelve_data_snapshot_provenance(forward_batch):
    batch, _started = forward_batch
    with LedgerStorage.open(batch.ledger_db) as storage:
        rows = storage.connection.execute(
            "SELECT source_id, source_metadata_json FROM input_observation "
            "WHERE source_id LIKE 'twelve_data:%' ORDER BY source_id"
        ).fetchall()
    assert len(rows) == 2
    for source_id, metadata_raw in rows:
        assert source_id.startswith("twelve_data:")
        metadata = json.loads(metadata_raw)
        assert metadata["acquisition_method"] == "validated-immutable-snapshot"
        assert metadata["provider_metadata"] == {
            "snapshot_id": "TWELVEDATA_2026-09-12_RUN_fixture",
            "snapshot_session": AS_OF,
            "snapshot_sha256": "a" * 64,
        }


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


def test_forward_pipeline_keeps_setup_on_price_session_with_later_research_date(tmp_path):
    research_day = (date.fromisoformat(AS_OF) + timedelta(days=2)).isoformat()

    class SplitCutoffSource:
        def acquire(self, ticker):
            sources = _FixtureSource().acquire(ticker)
            pack = replace(
                sources.pack, as_of=research_day,
                company_input={**sources.pack.company_input, "as_of": research_day},
            )
            return replace(
                sources, pack=pack, research_as_of=research_day,
                research_cutoff_utc=f"{research_day}T12:00:00.000000Z",
            )

    batch = run_forward_pilot(
        tmp_path / "split-cutoff.sqlite3", ["AAPL"], dry_run=True,
        source_provider_factory=lambda _root: SplitCutoffSource(),
        llm_provider_factory=_llm,
    )
    assert batch.results[0].run_status != "FAILED"
    assert batch.results[0].fresh_price_timestamp == AS_OF
    with LedgerStorage.open(batch.ledger_db) as storage:
        setup_payload = storage.connection.execute(
            "SELECT a.payload FROM setup s JOIN artifact a "
            "ON a.artifact_id=s.result_artifact_id"
        ).fetchone()[0]
        research_at = storage.connection.execute(
            "SELECT as_of_at FROM research_record"
        ).fetchone()[0]
    assert json.loads(setup_payload)["as_of"] == AS_OF
    assert research_at == f"{research_day}T12:00:00.000000Z"


def test_responses_api_provider_provenance_is_persisted_without_method_change(tmp_path):
    database = tmp_path / "responses.sqlite3"
    batch = run_forward_pilot(
        database, ["AAPL"], dry_run=True,
        source_provider_factory=lambda _root: _FixtureSource(),
        llm_provider_factory=_responses_llm,
    )
    assert batch.results[0].run_status != "FAILED"
    with LedgerStorage.open(database) as storage:
        identities = storage.connection.execute(
            "SELECT DISTINCT provider, model, model_version FROM llm_interaction"
        ).fetchall()
        assert [tuple(row) for row in identities] == [(
            "OPENAI_RESPONSES_API", "gpt-5.6-sol", "responses-api:gpt-5.6-sol",
        )]
        methods = storage.connection.execute(
            "SELECT DISTINCT method_version FROM research_record"
        ).fetchall()
        assert [row[0] for row in methods] == ["USA_V2_FACTS_V3"]
