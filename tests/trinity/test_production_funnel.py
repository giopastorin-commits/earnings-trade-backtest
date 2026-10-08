from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from trinity.pilots.luna_triage import OUTPUT_SCHEMA, PROMPT_TEMPLATE, canonical_bytes
from trinity.pilots.production_funnel import run_production_funnel


READY_TICKERS = (
    "AAPL", "ABBV", "AES", "BBY", "BIIB", "CRL", "CVX", "DELL", "DHR",
    "DVN", "DXCM", "EXPD", "GDDY", "GILD", "KEYS", "LITE", "MCK", "MRK",
    "NBIS", "NOW", "PFE", "PSX", "TECH", "TGT", "TMO", "VTRS", "WAT",
    "WDAY", "WST", "XOM",
)
OPERATIONAL = {
    "BBY": "PULLBACK", "DVN": "PULLBACK", "GDDY": "PULLBACK",
    "KEYS": "BREAKOUT", "LITE": "BREAKOUT", "MRK": "PULLBACK",
    "WDAY": "PULLBACK",
}
DECISIONS = {
    "BBY": "WATCH", "DVN": "WATCH", "GDDY": "ESCALATE",
    "KEYS": "WATCH", "LITE": "WATCH", "MRK": "ESCALATE",
    "WDAY": "ESCALATE",
}


def _files(root: Path, tickers=READY_TICKERS):
    price_dir = root / "prices"
    price_dir.mkdir()
    inputs = {}
    survivors = []
    for ticker in tickers:
        bars = [{"date": "2026-10-01", "ticker": ticker}]
        (price_dir / f"{ticker}.json").write_text(json.dumps(bars), encoding="utf-8")
        inputs[ticker] = {"sha256": hashlib.sha256(canonical_bytes(bars)).hexdigest()}
        survivors.append({
            "ticker": ticker, "latest_session": "2026-10-01",
            "screening_state": "READY_TECHNICALLY", "primary_reason": "READY",
        })
    funnel = root / "funnel.json"
    funnel.write_text(json.dumps({"survivors": survivors, "inputs": inputs}), encoding="utf-8")
    return funnel, price_dir


def _setup_builder_for(setup_types):
    def builder(thesis, _bars, as_of):
        ticker = thesis["ticker"]
        setup_type = setup_types.get(ticker, "NO_SETUP")
        operational = setup_type != "NO_SETUP"
        return SimpleNamespace(
            ticker=ticker, as_of=as_of, setup_type=setup_type,
            entry_level=100.0 if operational else None,
            stop_level=90.0 if operational else None,
            tp1=120.0 if operational else None,
            tp2=130.0 if operational else None,
            rr_tp1=2.0 if operational else None,
            rr_tp2=3.0 if operational else None,
        )
    return builder


class FakeTriage:
    def __init__(self, decisions):
        self.decisions = decisions
        self.seen = []

    def __call__(self, **kwargs):
        self.seen = [row["ticker"] for row in kwargs["ready_entries"]]
        assert kwargs["expected_count"] == len(self.seen)
        return {
            "finished_at": "2026-10-04T12:00:00Z",
            "results": [{
                "ticker": ticker, "status": "SUCCESS", "decision": self.decisions[ticker],
                "qualitative_priority": "HIGH" if self.decisions[ticker] == "ESCALATE" else "MEDIUM",
                "primary_reason": f"Frozen snapshot rationale for {ticker}.",
            } for ticker in self.seen],
        }


class FakeSol:
    def __init__(self):
        self.calls = []

    def __call__(self, database, tickers, *, dry_run):
        self.calls.append((Path(database), tuple(tickers), dry_run))
        return {"tickers": tuple(tickers)}


def test_validated_30_name_order_filters_before_luna_and_routes_only_escalate(tmp_path):
    funnel, prices = _files(tmp_path)
    triage, sol = FakeTriage(DECISIONS), FakeSol()
    watchlist = tmp_path / "watchlist.json"
    with patch("subprocess.run", side_effect=AssertionError("real model forbidden")), \
         patch("requests.Session.get", side_effect=AssertionError("network forbidden")):
        result = run_production_funnel(
            funnel_path=funnel, price_dir=prices, luna_output_path=None,
            watchlist_path=watchlist, ledger_db=tmp_path / "ledger.sqlite3",
            setup_builder=_setup_builder_for(OPERATIONAL),
            triage_runner=triage, sol_runner=sol,
        )

    assert result.ready_count == 30
    assert result.operational_setup_count == result.luna_eligible_count == 7
    assert result.no_setup_count == 23
    assert set(triage.seen) == set(OPERATIONAL)
    assert result.watch_tickers == ("BBY", "DVN", "KEYS", "LITE")
    assert result.escalate_tickers == ("GDDY", "MRK", "WDAY")
    assert result.drop_tickers == ()
    assert sol.calls == [(tmp_path / "ledger.sqlite3", ("GDDY", "MRK", "WDAY"), True)]
    assert not (tmp_path / "ledger.sqlite3").exists()

    saved = json.loads(watchlist.read_text(encoding="utf-8"))
    assert [row["ticker"] for row in saved["entries"]] == ["BBY", "DVN", "KEYS", "LITE"]
    assert all(row["state"] == "ACTIVE_WATCH" for row in saved["entries"])
    assert all(set(row) == {
        "ticker", "as_of_session", "setup_type", "entry", "stop", "tp1", "tp2",
        "rr1", "rr2", "luna_priority", "luna_primary_reason", "state",
    } for row in saved["entries"])


def test_drop_and_watch_never_reach_sol_but_watch_is_preserved(tmp_path):
    tickers = ("DROP1", "WATCH1", "ESC1")
    funnel, prices = _files(tmp_path, tickers)
    setup_types = {ticker: "PULLBACK" for ticker in tickers}
    decisions = {"DROP1": "DROP", "WATCH1": "WATCH", "ESC1": "ESCALATE"}
    triage, sol = FakeTriage(decisions), FakeSol()
    result = run_production_funnel(
        funnel_path=funnel, price_dir=prices, luna_output_path=None,
        watchlist_path=tmp_path / "watch.json", ledger_db=tmp_path / "ledger.sqlite3",
        setup_builder=_setup_builder_for(setup_types),
        triage_runner=triage, sol_runner=sol,
    )
    assert result.drop_tickers == ("DROP1",)
    assert result.watch_tickers == ("WATCH1",)
    assert result.escalate_tickers == ("ESC1",)
    assert [row["ticker"] for row in result.watchlist["entries"]] == ["WATCH1"]
    assert sol.calls[0][1] == ("ESC1",)


def test_all_no_setup_makes_zero_luna_and_sol_calls(tmp_path):
    funnel, prices = _files(tmp_path, ("NONE1", "NONE2"))
    triage_calls = []
    sol = FakeSol()
    result = run_production_funnel(
        funnel_path=funnel, price_dir=prices, luna_output_path=None,
        watchlist_path=tmp_path / "watch.json",
        setup_builder=_setup_builder_for({}),
        triage_runner=lambda **kwargs: triage_calls.append(kwargs), sol_runner=sol,
    )
    assert result.ready_count == result.no_setup_count == 2
    assert result.operational_setup_count == result.luna_eligible_count == 0
    assert triage_calls == [] and sol.calls == []
    assert result.watchlist["entries"] == []


def test_default_sol_route_uses_current_luna_artifact_for_facts_v2_continuity(tmp_path):
    funnel, prices = _files(tmp_path, ("ESC1",))
    triage = FakeTriage({"ESC1": "ESCALATE"})
    luna_output = tmp_path / "current-luna.json"
    seen = {}

    class Snapshot:
        def provenance(self):
            return {
                "price_provider": "TWELVE_DATA", "price_snapshot_id": "snap",
                "price_snapshot_session": "2026-10-01",
                "price_snapshot_sha256": "a" * 64,
            }
    snapshot = Snapshot()
    value = json.loads(funnel.read_text(encoding="utf-8"))
    value["price_snapshot"] = {
        "provider": "TWELVE_DATA", "snapshot_id": "snap",
        "session": "2026-10-01", "sha256": "a" * 64,
    }
    funnel.write_text(json.dumps(value), encoding="utf-8")

    class Provider:
        def __init__(self, root, *, price_snapshot, continuity_path):
            seen["root"] = Path(root)
            seen["price_snapshot"] = price_snapshot
            seen["continuity_path"] = Path(continuity_path)

    def forward(database, tickers, *, dry_run, source_provider_factory):
        seen["database"] = Path(database)
        seen["tickers"] = tuple(tickers)
        seen["dry_run"] = dry_run
        source_provider_factory(tmp_path / "sources")
        return "forward-result"

    with patch("trinity.pilots.forward.run_forward_pilot", side_effect=forward), \
         patch("trinity.usa_forward.EODHDNewsSECForwardProvider", Provider):
        result = run_production_funnel(
            funnel_path=funnel, price_dir=prices, luna_output_path=luna_output,
            watchlist_path=tmp_path / "watch.json", ledger_db=tmp_path / "ledger.sqlite3",
            setup_builder=_setup_builder_for({"ESC1": "PULLBACK"}),
            triage_runner=triage, sol_runner=None, price_snapshot=snapshot,
        )
    assert result.sol_result == "forward-result"
    assert seen == {
        "database": tmp_path / "ledger.sqlite3", "tickers": ("ESC1",),
        "dry_run": True, "root": tmp_path / "sources",
        "price_snapshot": snapshot,
        "continuity_path": luna_output,
    }


def test_luna_prompt_and_schema_are_frozen():
    assert hashlib.sha256(PROMPT_TEMPLATE.encode()).hexdigest() == \
        "a3562d14bca089cefee0d3c7bbfc8ff06851dea2615217275dfc1a5dfb6eaf22"
    assert hashlib.sha256(canonical_bytes(OUTPUT_SCHEMA)).hexdigest() == \
        "5b10b1693e31e1e659d083c88b3b574c5175b649428b6f51bdc6f73e519bc5c1"
