import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

from trinity.pilots.luna_triage import (
    LunaReply,
    OUTPUT_SCHEMA,
    PROMPT_TEMPLATE,
    TriageError,
    TransientLunaError,
    build_packet,
    load_ready_entries,
    make_prompt,
    run_triage,
    validate_reply,
)


def _row(ticker, state="READY_TECHNICALLY"):
    return {
        "ticker": ticker, "latest_session": "2026-10-01", "close": 100.0,
        "regime": "UPTREND", "distance_sma20_pct": 0.1,
        "distance_sma50_pct": 1.2, "distance_prior_high20_pct": -2.0,
        "rsi14": 52.0, "relative_volume": 1.3, "primary_reason": "PULLBACK_SUPPORT_BAND_READY",
        "screening_state": state,
    }


def _reply(ticker, evidence=()):
    return {
        "ticker": ticker, "decision": "WATCH", "qualitative_priority": "MEDIUM",
        "fresh_information": True, "material_catalyst_present": False,
        "material_risk_present": False, "primary_reason": "Evidence is not urgent.",
        "catalysts": [], "risks": [], "evidence_ids": list(evidence),
    }


class FakeSources:
    news_requests = 0
    earnings_requests = 0
    news_cache_hits = 0
    sec_cache_hits = 0
    downloaded_bytes = 0

    def earnings(self, provider_tickers):
        self.earnings_seen = list(provider_tickers)
        return {}

    def news(self, ticker, provider_ticker):
        self.news_requests += 1
        return ([{
            "evidence_id": f"news:{ticker}:1", "published_at": "2026-10-02T12:00:00Z",
            "headline": "Compact headline", "source": "example.com",
            "summary": "Compact summary", "url": "https://example.com/item",
        }], "2026-10-04T10:00:00Z")

    def sec(self, ticker):
        return None, []


class FakeLuna:
    def __init__(self, failures=None):
        self.prompts = []
        self.failures = dict(failures or {})

    def invoke(self, prompt):
        self.prompts.append(prompt)
        packet = json.loads(prompt[len(PROMPT_TEMPLATE):])
        ticker = packet["ticker_identity"]["ticker"]
        error = self.failures.get(ticker)
        if isinstance(error, list) and error:
            raised = error.pop(0)
            if raised:
                raise raised
        elif error:
            raise error
        raw = json.dumps(_reply(ticker, [f"news:{ticker}:1"])).encode()
        return LunaReply(json.loads(raw), raw, 10, 2, 12)


def _files(root: Path, rows):
    funnel = root / "funnel.json"
    funnel.write_text(json.dumps({"survivors": rows}), encoding="utf-8")
    universe = root / "universe.csv"
    universe.write_text(
        "canonical_ticker,provider_ticker\n" +
        "".join(f"{row['ticker']},{row['ticker']}.US\n" for row in rows), encoding="utf-8",
    )
    return funnel, universe


def test_only_ready_entries_are_selected_and_near_distant_excluded(tmp_path):
    funnel, _ = _files(tmp_path, [_row("AAA"), _row("BBB", "NEAR"), _row("CCC", "DISTANT")])
    assert [row["ticker"] for row in load_ready_entries(funnel)] == ["AAA"]


def test_exact_output_schema_and_evidence_validation():
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    valid = _reply("AAA", ["news:AAA:1"])
    assert validate_reply(valid, "AAA", ["news:AAA:1"])["decision"] == "WATCH"
    with pytest.raises(TriageError, match="unsupplied"):
        validate_reply({**valid, "evidence_ids": ["invented"]}, "AAA", ["news:AAA:1"])
    with pytest.raises(TriageError, match="exact schema"):
        validate_reply({**valid, "extra": 1}, "AAA", ["news:AAA:1"])


def test_max_three_catalysts_and_risks():
    for key in ("catalysts", "risks"):
        invalid = _reply("AAA")
        invalid[key] = ["x"] * 4
        with pytest.raises(TriageError, match=key):
            validate_reply(invalid, "AAA", [])


def test_packet_and_prompt_are_deterministic_and_compact():
    row = _row("AAA")
    news = [{"evidence_id": "news:AAA:1", "headline": "h"}]
    packet1 = build_packet(row, "AAA.US", news, None, [], None, "2026-10-04T00:00:00Z")
    packet2 = build_packet(row, "AAA.US", news, None, [], None, "2026-10-04T00:00:00Z")
    assert make_prompt(packet1) == make_prompt(packet2)
    prompt = make_prompt(packet1)
    assert "raw_filing" not in prompt and "ohlcv" not in prompt.lower()
    assert "bars" not in packet1 and len(packet1["technical_snapshot"]) == 9


def test_independent_calls_no_sol_and_no_downstream_side_effects(tmp_path):
    rows = [_row("AAA"), _row("BBB")]
    funnel, universe = _files(tmp_path, rows)
    luna, sources = FakeLuna(), FakeSources()
    with patch("subprocess.run", side_effect=AssertionError("real model forbidden in test")):
        result = run_triage(
            funnel_path=funnel, output_path=tmp_path / "out.json", source_root=tmp_path / "source",
            universe_path=universe, sources=sources, luna=luna, expected_count=2,
        )
    assert len(luna.prompts) == 2
    assert luna.prompts[0] != luna.prompts[1]
    assert result["cost"]["sol_calls"] == 0
    assert result["cost"]["input_tokens"] == 20
    assert result["cost"]["output_tokens"] == 4
    assert result["cost"]["total_tokens"] == 24
    assert not list(tmp_path.rglob("*.sqlite3"))
    assert result["counts"] == {"ready_input": 2, "drop": 0, "watch": 2, "escalate": 0, "failed": 0}


def test_failed_ticker_isolated_and_transient_retry_once(tmp_path):
    rows = [_row("AAA"), _row("BBB"), _row("CCC")]
    funnel, universe = _files(tmp_path, rows)
    luna = FakeLuna({
        "AAA": [TransientLunaError("temporary"), None],
        "BBB": TriageError("permanent"),
        "CCC": [TransientLunaError("one"), TransientLunaError("two")],
    })
    result = run_triage(
        funnel_path=funnel, output_path=None, source_root=tmp_path / "source",
        universe_path=universe, sources=FakeSources(), luna=luna, expected_count=3,
    )
    by_ticker = {row["ticker"]: row for row in result["results"]}
    assert by_ticker["AAA"]["status"] == "SUCCESS" and by_ticker["AAA"]["retries"] == 1
    assert by_ticker["BBB"]["status"] == "FAILED" and by_ticker["BBB"]["retries"] == 0
    assert by_ticker["CCC"]["status"] == "FAILED" and by_ticker["CCC"]["retries"] == 1
    assert result["cost"]["retries"] == 2
    assert result["cost"]["luna_invocations"] == 5


def test_ready_count_integrity_is_enforced(tmp_path):
    funnel, _ = _files(tmp_path, [_row("AAA")])
    with pytest.raises(TriageError, match="expected 30"):
        load_ready_entries(funnel, expected_count=30)
