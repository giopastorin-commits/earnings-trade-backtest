from __future__ import annotations

import json
from pathlib import Path

import pytest

from trinity.ledger import LedgerStorage
from trinity.pilots.jnj_telegram import _role_values
from trinity.pilots.multiticker import (
    RecordingUSAProvider,
    _send_once,
    _validate_tickers,
    render_summary,
    run_multiticker_pilot,
)
from trinity.usa_setup_v1 import PRICE_CACHE, THESIS_CACHE, _load_latest_thesis
from trinity.usa_v2 import AS_OF, load_company


def _provider_factory(ticker: str) -> RecordingUSAProvider:
    thesis = _load_latest_thesis(ticker, AS_OF, THESIS_CACHE)
    analyst, critic, _stage = _role_values(thesis)
    responses = iter((analyst, critic))
    return RecordingUSAProvider(lambda _prompt, _schema: next(responses))


@pytest.fixture(scope="module")
def isolated_batch(tmp_path_factory):
    database = tmp_path_factory.mktemp("multiticker") / "pilot.sqlite3"

    def loader(ticker, as_of):
        if ticker == "JPM":
            raise RuntimeError("injected ticker failure")
        return load_company(ticker, as_of)

    network_calls = []
    batch = run_multiticker_pilot(
        database,
        ["AAPL", "JPM", "JNJ"],
        dry_run=True,
        provider_factory=_provider_factory,
        company_loader=loader,
        telegram_sender=lambda text: network_calls.append(text),
    )
    return batch, network_calls


def test_explicit_ticker_list_only():
    with pytest.raises(ValueError, match="explicit ticker list"):
        _validate_tickers([])
    assert _validate_tickers(["aapl", "JNJ"]) == ("AAPL", "JNJ")


def test_unsupported_ticker_fails_before_database_creation(tmp_path):
    database = tmp_path / "unsupported.sqlite3"
    with pytest.raises(ValueError, match="unsupported ticker.*ZZZZ"):
        run_multiticker_pilot(database, ["ZZZZ"], dry_run=True)
    assert not database.exists()


def test_failed_ticker_is_isolated_and_no_setup_is_valid(isolated_batch):
    batch, _network_calls = isolated_batch
    by_ticker = {item.ticker: item for item in batch.results}
    assert by_ticker["AAPL"].run_status == "NO_SETUP"
    assert by_ticker["AAPL"].setup_type == "NO_SETUP"
    assert by_ticker["AAPL"].entry == by_ticker["AAPL"].stop == "-"
    assert by_ticker["JPM"].run_status == "FAILED"
    assert "injected ticker failure" in by_ticker["JPM"].error
    assert by_ticker["JNJ"].run_status == "SUCCESS"

    with LedgerStorage.open(batch.ledger_db) as storage:
        attempts = storage.connection.execute(
            "SELECT status, COUNT(*) FROM attempt GROUP BY status"
        ).fetchall()
        assert {row[0]: row[1] for row in attempts} == {"FAILED": 1, "SUCCEEDED": 2}
        assert storage.connection.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 2


def test_each_committed_ticker_has_independent_lineage(isolated_batch):
    batch, _network_calls = isolated_batch
    successful = [item for item in batch.results if item.run_id]
    assert len({item.run_id for item in successful}) == 2
    assert len({item.research_id for item in successful}) == 2
    assert len({item.setup_id for item in successful}) == 2

    with LedgerStorage.open(batch.ledger_db) as storage:
        rows = storage.connection.execute(
            """
            SELECT rr.subject_key, rr.attempt_id, rr.content_artifact_id,
                   s.instrument_id, s.result_artifact_id
            FROM research_record rr
            JOIN setup_research_lineage l ON l.research_id = rr.research_id
            JOIN setup s ON s.setup_id = l.setup_id
            ORDER BY rr.subject_key
            """
        ).fetchall()
        assert [(row[0], row[3]) for row in rows] == [
            ("ticker:AAPL", "AAPL"), ("ticker:JNJ", "JNJ")
        ]
        assert len({row[1] for row in rows}) == 2
        assert len({row[2] for row in rows}) == 2
        assert len({row[4] for row in rows}) == 2
        assert storage.connection.execute(
            "SELECT method_version FROM research_method"
        ).fetchone()[0] == "USA_V2"
        assert {
            row[0] for row in storage.connection.execute(
                "SELECT DISTINCT method_version FROM research_record"
            )
        } == {"USA_V2"}


def test_summary_contains_required_columns_and_failed_row(isolated_batch):
    batch, _network_calls = isolated_batch
    summary = render_summary(batch.results)
    for heading in (
        "Ticker", "Research status", "Evidence confidence", "Thesis strength",
        "Technical regime", "Setup type", "Entry", "Stop", "TP1", "TP2",
        "RR1", "RR2", "Resolved record class", "Resolved PIT class", "Run status",
    ):
        assert heading in summary
    assert "JPM" in summary and "FAILED" in summary


def test_telegram_dry_run_renders_every_commit_and_uses_zero_network(isolated_batch):
    batch, network_calls = isolated_batch
    assert network_calls == []
    rendered = [item for item in batch.results if item.telegram_message]
    assert len(rendered) == 2
    assert batch.telegram_render_count == 2
    assert "Setup: NO_SETUP" in next(
        item.telegram_message for item in rendered if item.ticker == "AAPL"
    )


def test_duplicate_send_is_suppressed_within_batch():
    calls = []
    sent = set()
    _send_once("setup-1", "first", sent, calls.append)
    with pytest.raises(RuntimeError, match="duplicate Telegram send suppressed"):
        _send_once("setup-1", "second", sent, calls.append)
    assert calls == ["first"]


def test_jnj_matches_validated_single_ticker_output(isolated_batch):
    batch, _network_calls = isolated_batch
    result = next(item for item in batch.results if item.ticker == "JNJ")
    expected = json.loads(
        (Path(__file__).parent / "ledger/fixtures/jnj_ohlcv_20260912.json").read_text(
            encoding="utf-8"
        )
    )
    actual = json.loads((PRICE_CACHE / "JNJ.json").read_text(encoding="utf-8"))
    assert actual[-len(expected):] == expected
    assert (
        result.research_status, result.evidence_confidence, result.thesis_strength,
        result.technical_regime, result.setup_type, result.entry, result.stop,
        result.tp1, result.tp2, result.rr1, result.rr2,
    ) == (
        "INVESTIGATE", "HIGH", "MEDIUM", "UPTREND", "PULLBACK",
        "264.022", "256.1417", "281.07", "287.6628", "2.1634", "3",
    )
