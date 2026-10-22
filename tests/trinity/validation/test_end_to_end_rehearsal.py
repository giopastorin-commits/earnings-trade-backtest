"""Offline, ephemeral proof of the complete prospective-validation chain."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil
import socket

import exchange_calendars
import pytest

from tests.trinity.validation.test_collector import _fixture as collector_fixture
from trinity.shadow.r2 import (
    archive_stock_observation_snapshot,
    restore_stock_observation_snapshot,
)
from trinity.twelvedata_prices import ApiUsageCapture, ProviderCapture, canonical_bytes
from trinity.validation.baseline_v1 import BASELINE_HASH, BASELINE_ID
from trinity.validation.baskets import AVAILABLE, build_official_weekly_baskets
from trinity.validation.cli import protocol_dict
from trinity.validation.collector import collect_run
from trinity.validation.contracts import OFFICIAL_ELIGIBLE
from trinity.validation.cycle_registry import (
    load_registered_cycles,
    register_observation_cycle,
)
from trinity.validation.market_calendar import session_times
from trinity.validation.observation_cycle import (
    ObservationCycleError,
    create_observation_cycle,
    expectation_references,
    expectation_set_sha256,
    verify_observation_cycle,
)
from trinity.validation.observation_slots import first_eligible_observation_slot
from trinity.validation.outcome_expectations import append_outcome_expectations
from trinity.validation.outcome_finalization import (
    COMPLETE,
    CycleSourceLocator,
    finalize_outcomes,
    load_outcome_finals,
)
from trinity.validation.protocol_v1 import (
    PROTOCOL_EPOCH,
    PROTOCOL_HASH,
    PROTOCOL_ID,
)
from trinity.validation.run_observation import (
    append_run_observation,
    load_run_observations,
)
from trinity.validation.scorecard import (
    INSUFFICIENT_PRIMARY_USABLE_WEEKS,
    build_final_scorecard,
)
from trinity.validation.simple_quant_baseline import (
    INSUFFICIENT_USABLE_WEEKS,
    build_quant_baseline_weeks,
    build_simple_quant_comparator,
)
from trinity.validation.slot_assignment import (
    ASSIGNED_EXACT_CYCLE,
    close_observation_slot,
    load_current_expectations,
    load_expectation_assignments,
)
from trinity.validation.spy_benchmark import acquire_spy_benchmark
from trinity.validation.stock_observation import (
    expected_ticker_set_sha256,
    load_stock_observation_snapshot,
)
from trinity.validation.stock_observation_acquisition import acquire_stock_observation
from trinity.validation.storage import ValidationStore


EXPECTED_PROTOCOL_HASH = "c04e4009962e2d93265b725e0c3213000151202e8ecd3dd45e60cf9f92ed59d3"
EXPECTED_BASELINE_HASH = "2c67a7375e27dbd8eebaef3b14bec8f057fc773083db59242e305117b2fac57a"
RUN_ID = "REHEARSAL_ONLY_OFFICIAL_2026W41"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _refresh_source_fixture(root: Path) -> None:
    """Make the shared collector fixture unmistakably rehearsal-only before freezing it."""

    report_path = root / "artifacts" / "final_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["run_id"] = RUN_ID
    _write_json(report_path, report)
    _write_json(root / "run_state.json", {"run_id": RUN_ID, "state": "COMPLETE"})

    setups_path = root / "artifacts" / "setups.json"
    setups = json.loads(setups_path.read_text(encoding="utf-8"))
    aaa = next(item for item in setups if item["ticker"] == "AAA")
    aaa.update(tp1=125, tp2=130, rr_tp1=5, rr_tp2=6)
    _write_json(setups_path, setups)

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = RUN_ID
    for item in manifest["files"]:
        path = root / Path(item["path"])
        raw = path.read_bytes()
        item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    _write_json(manifest_path, manifest)


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def _sessions(target: date) -> tuple[str, ...]:
    calendar = exchange_calendars.get_calendar(
        "XNYS", start=target - timedelta(days=450), end=target,
    )
    return tuple(item.date().isoformat() for item in calendar.sessions)


def _market_payload(
    symbol: str, target: date, closes: dict[str, float], *, base_open: float = 100.0,
) -> bytes:
    values = []
    for session in _sessions(target):
        close = closes.get(session, base_open)
        values.append({
            "datetime": session,
            "open": f"{base_open:.6f}",
            "high": f"{max(base_open, close) + 1:.6f}",
            "low": f"{min(base_open, close) - 1:.6f}",
            "close": f"{close:.6f}",
            "volume": "1000000",
        })
    return canonical_bytes({
        "meta": {"symbol": symbol, "interval": "1day"},
        "values": list(reversed(values)), "status": "ok",
    })


class _Clock:
    def __init__(self, started: datetime) -> None:
        self.started = started
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.started if self.calls == 1 else self.started + timedelta(seconds=5)


class _StockProvider:
    api_key = "REHEARSAL_FAKE_SECRET"

    def __init__(self, raws: dict[str, bytes], requested_at: datetime) -> None:
        self.raws = raws
        self.requested_at = requested_at
        self.daily_remaining = 1000
        self.credit_events: list[dict[str, object]] = []

    def fetch_api_usage(self) -> ApiUsageCapture:
        return ApiUsageCapture(
            usage={"daily_usage": 1, "plan_daily_limit": 1000},
            daily_usage=1, plan_daily_limit=1000, daily_remaining=1000,
            retrieved_at_utc=self.requested_at.isoformat(), response_headers={},
        )

    def fetch(self, symbol, *, start_date, target_session, remaining_tickers=None):
        del start_date, target_session, remaining_tickers
        self.credit_events.append({
            "endpoint": "/time_series", "symbol": symbol,
            "retrieved_at_utc": self.requested_at.isoformat(), "http_status": 200,
        })
        return ProviderCapture(self.raws[symbol], ({
            "attempt": 1, "requested_at_utc": self.requested_at.isoformat(),
            "http_status": 200, "elapsed_seconds": 0.01,
            "provider_code": None, "provider_message": None,
        },))


class _SpyProvider:
    api_key = "REHEARSAL_FAKE_SECRET"

    def __init__(self, raw: bytes, requested_at: datetime) -> None:
        self.raw = raw
        self.requested_at = requested_at

    def fetch(self, symbol, *, start_date, target_session, remaining_tickers=None):
        del start_date, target_session, remaining_tickers
        assert symbol == "SPY"
        return ProviderCapture(self.raw, ({
            "attempt": 1, "requested_at_utc": self.requested_at.isoformat(),
            "http_status": 200, "elapsed_seconds": 0.01,
        },))


class _FakeR2:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        del bucket
        metadata = dict((ExtraArgs or {}).get("Metadata", {}))
        self.objects[key] = (Path(filename).read_bytes(), metadata)

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        del Bucket, ContinuationToken
        return {"Contents": [
            {"Key": key, "Size": len(raw)}
            for key, (raw, _metadata) in sorted(self.objects.items())
            if key.startswith(Prefix)
        ], "IsTruncated": False}

    def head_object(self, *, Bucket, Key):
        del Bucket
        raw, metadata = self.objects[Key]
        return {"ContentLength": len(raw), "Metadata": metadata}

    def get_object(self, *, Bucket, Key):
        del Bucket
        return {"Body": BytesIO(self.objects[Key][0])}


def _record_count(store: ValidationStore) -> int:
    return int(store.connection.execute("SELECT COUNT(*) FROM validation_record").fetchone()[0])


def test_true_end_to_end_rehearsal_is_ephemeral_connected_and_fail_closed(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted during rehearsal")
    ))
    assert PROTOCOL_HASH == EXPECTED_PROTOCOL_HASH
    assert BASELINE_HASH == EXPECTED_BASELINE_HASH

    rehearsal_root = tmp_path / "REHEARSAL_ONLY"
    source_root = collector_fixture(
        rehearsal_root / "source_run", mode="OFFICIAL", official=True,
        scanner_tickers=("AAA", "BBB"),
    )
    _refresh_source_fixture(source_root)
    source_before = _file_hashes(source_root)
    collected = collect_run(source_root)
    assert collected.run_id == RUN_ID
    assert collected.eligibility == OFFICIAL_ELIGIBLE
    assert tuple(item.ticker for item in collected.selections) == ("AAA", "BBB")
    assert source_before == _file_hashes(source_root)

    store_path = rehearsal_root / "validation" / "rehearsal.sqlite3"
    store_path.parent.mkdir(parents=True)
    with ValidationStore(store_path) as store:
        assert store.append("PROTOCOL", f"{PROTOCOL_ID}:1", {
            **protocol_dict(), "protocol_hash": PROTOCOL_HASH,
        })
        assert append_run_observation(store, collected)
        assert not append_run_observation(store, collected)
        prefix = f"{PROTOCOL_ID}:{PROTOCOL_EPOCH}:{collected.run_id}"
        for item in collected.selections:
            assert store.append("SELECTION", f"{prefix}:{item.ticker}", item.to_dict())
        for item in collected.setups:
            assert store.append("SETUP", f"{prefix}:{item.ticker}", item.to_dict())
        for item in collected.executions:
            assert store.append(
                "EXECUTION", f"{prefix}:{item.ticker}:{item.freshness_timestamp}",
                item.to_dict(),
            )

        run = load_run_observations(store)[0]
        assert (run.scanner_tickers, run.luna_tickers, run.sol_tickers) == (
            ("AAA", "BBB"), ("BBB",), ("BBB",),
        )
        assert append_outcome_expectations(store, collected) == 12
        assert append_outcome_expectations(store, collected) == 0
        expectations = load_current_expectations(store)
        assert len(expectations) == 12
        assert {item.horizon_sessions for item in expectations} == {5, 20, 60}
        assert sum(item.cohort == "SCANNER" for item in expectations) == 6
        assert sum(item.cohort == "LUNA" for item in expectations) == 3
        assert sum(item.cohort == "SOL" for item in expectations) == 3

        slot = first_eligible_observation_slot(max(item.maturity_session for item in expectations))
        mature = tuple(item for item in expectations
                       if item.maturity_session <= slot.slot_target_session)
        assert mature == expectations
        refs = expectation_references(mature)
        expectation_sha = expectation_set_sha256(refs)
        expected_tickers = tuple(sorted({item.ticker for item in mature}))
        ticker_sha = expected_ticker_set_sha256(expected_tickers)

        by_horizon = {
            horizon: next(item for item in expectations
                          if item.ticker == "AAA" and item.cohort == "SCANNER"
                          and item.horizon_sessions == horizon)
            for horizon in (5, 20, 60)
        }
        closes = {
            "AAA": {
                by_horizon[5].maturity_session: 115.0,
                by_horizon[20].maturity_session: 125.0,
                by_horizon[60].maturity_session: 140.0,
            },
            "BBB": {
                by_horizon[5].maturity_session: 105.0,
                by_horizon[20].maturity_session: 108.0,
                by_horizon[60].maturity_session: 115.0,
            },
        }
        spy_closes = {
            by_horizon[5].maturity_session: 102.0,
            by_horizon[20].maturity_session: 105.0,
            by_horizon[60].maturity_session: 110.0,
        }
        target = date.fromisoformat(slot.slot_target_session)
        finality = session_times(slot.slot_target_session)[1] + timedelta(minutes=20)

        stock_original = rehearsal_root / "stock_original"
        stock_manifest = acquire_stock_observation(
            target_session=target, expected_tickers=expected_tickers,
            expectation_set_sha256=expectation_sha, output_root=stock_original,
            acquisition_attempt=1,
            client=_StockProvider({
                ticker: _market_payload(ticker, target, closes[ticker])
                for ticker in expected_tickers
            }, finality + timedelta(seconds=1)),
            now=_Clock(finality),
        )
        fake_r2 = _FakeR2()
        archived = archive_stock_observation_snapshot(
            fake_r2, "rehearsal-only-bucket", stock_original,
        )
        stock_restored = rehearsal_root / "stock_restored"
        restored = restore_stock_observation_snapshot(
            fake_r2, "rehearsal-only-bucket", archived["prefix"], stock_restored,
            expected_snapshot_id=stock_manifest["snapshot_id"],
            expected_snapshot_sha256=stock_manifest["snapshot_sha256"],
            expected_target_session=slot.slot_target_session,
            expected_tickers=expected_tickers,
            expected_expectation_set_sha256=expectation_sha,
        )
        loaded_stock = load_stock_observation_snapshot(
            stock_restored, expected_tickers=expected_tickers,
            expected_observation_target_session=slot.slot_target_session,
        )
        assert loaded_stock.source_snapshot_id == stock_manifest["snapshot_id"]
        assert loaded_stock.source_snapshot_sha256 == stock_manifest["snapshot_sha256"]
        assert loaded_stock.expected_ticker_set_sha256 == ticker_sha
        assert restored["expectation_set_sha256"] == expectation_sha
        assert all((stock_restored / "raw" / f"{ticker}.json").is_file()
                   for ticker in expected_tickers)

        spy_root = rehearsal_root / "spy"
        spy_manifest = acquire_spy_benchmark(
            target_session=target, run_id=RUN_ID, output_root=spy_root,
            client=_SpyProvider(
                _market_payload("SPY", target, spy_closes), finality + timedelta(seconds=1),
            ), now=_Clock(finality),
        )
        assert spy_manifest["target_market_session"] == slot.slot_target_session

        cycle_root = rehearsal_root / "cycle"
        cycle = create_observation_cycle(
            expectations=mature, observation_target_session=slot.slot_target_session,
            stock_root=stock_restored, spy_root=spy_root, output_root=cycle_root,
        )
        assert verify_observation_cycle(
            cycle_root, expectations=mature, stock_root=stock_restored, spy_root=spy_root,
        ) == cycle
        assert cycle["expectation_set_sha256"] == expectation_sha
        assert cycle["expected_ticker_set_sha256"] == ticker_sha
        assert cycle["stock_snapshot"]["snapshot_id"] == stock_manifest["snapshot_id"]
        assert cycle["spy_snapshot"]["snapshot_id"] == spy_manifest["benchmark_snapshot_id"]

        assert register_observation_cycle(
            store, cycle_root=cycle_root, expectations=mature,
            stock_root=stock_restored, spy_root=spy_root,
        )
        assert not register_observation_cycle(
            store, cycle_root=cycle_root, expectations=mature,
            stock_root=stock_restored, spy_root=spy_root,
        )
        registrations = load_registered_cycles(store)
        assert len(registrations) == 1
        assert registrations[0].observation_cycle_id == cycle["observation_cycle_id"]

        closed_at = datetime.fromisoformat(
            slot.finality_eligible_at_utc.replace("Z", "+00:00")
        ) + timedelta(seconds=1)
        closure = close_observation_slot(
            store, iso_year=slot.iso_year, iso_week=slot.iso_week,
            closed_at_utc=closed_at.isoformat(),
        )
        assert closure.closure_state == ASSIGNED_EXACT_CYCLE
        assert closure.authoritative_cycle_id == cycle["observation_cycle_id"]
        assert closure.assignment_count == len(mature)
        assignments = load_expectation_assignments(store, expectations)
        assert len(assignments) == len(mature)
        assert {item.expectation_key for item in assignments} == {item.key for item in mature}
        assert {item.observation_cycle_id for item in assignments} == {
            cycle["observation_cycle_id"],
        }

        locator = CycleSourceLocator(
            cycle["observation_cycle_id"], cycle_root, stock_restored, spy_root,
        )
        summary = finalize_outcomes(store, cycle_sources=(locator,))
        assert (summary.completed, summary.pending_unassigned,
                summary.pending_source_unavailable, summary.data_quality_failure) == (12, 0, 0, 0)
        replay = finalize_outcomes(store, cycle_sources=(locator,))
        assert replay.already_final == 12 and replay.new_final_records == 0
        finals = load_outcome_finals(store, expectations)
        assert len(finals) == 12 and all(item.status == COMPLETE for item in finals)
        assert all(item.quality_flags == () for item in finals)
        assert all(item.observation_cycle_id == cycle["observation_cycle_id"] for item in finals)
        assert all(
            (item.protocol_id, item.protocol_hash, item.protocol_epoch,
             item.run_id, item.eligibility, item.decision_timestamp_utc,
             item.decision_target_session, item.observation_slot_id,
             item.slot_target_session, item.stock_snapshot_id,
             item.stock_snapshot_sha256, item.spy_snapshot_id,
             item.spy_snapshot_sha256)
            == (PROTOCOL_ID, PROTOCOL_HASH, PROTOCOL_EPOCH,
                collected.run_id, OFFICIAL_ELIGIBLE, collected.run_timestamp_utc,
                collected.decision_target_session, slot.observation_slot_id,
                slot.slot_target_session, stock_manifest["snapshot_id"],
                stock_manifest["snapshot_sha256"], spy_manifest["benchmark_snapshot_id"],
                spy_manifest["benchmark_snapshot_sha256"])
            for item in finals
        )

        final_map = {(item.ticker, item.cohort, item.horizon_sessions): item for item in finals}
        expected_active = {
            ("AAA", 5): 13.0, ("AAA", 20): 20.0, ("AAA", 60): 30.0,
            ("BBB", 5): 3.0, ("BBB", 20): 3.0, ("BBB", 60): 5.0,
        }
        expected_stock = {
            ("AAA", 5): 15.0, ("AAA", 20): 25.0, ("AAA", 60): 40.0,
            ("BBB", 5): 5.0, ("BBB", 20): 8.0, ("BBB", 60): 15.0,
        }
        expected_spy = {5: 2.0, 20: 5.0, 60: 10.0}
        for (ticker, horizon), active in expected_active.items():
            final = final_map[(ticker, "SCANNER", horizon)]
            assert final.first_tradable_price == pytest.approx(100.0)
            assert final.future_price == pytest.approx(closes[ticker][final.maturity_session])
            assert final.benchmark_first_tradable_price == pytest.approx(100.0)
            assert final.benchmark_future_price == pytest.approx(
                spy_closes[final.maturity_session]
            )
            assert final.stock_return_pct == pytest.approx(expected_stock[(ticker, horizon)])
            assert final.benchmark_return_pct == pytest.approx(expected_spy[horizon])
            assert final.active_return_pct == pytest.approx(active)
            assert final.expected_first_tradable_session == by_horizon[horizon].expected_first_tradable_session
            assert final.maturity_session == by_horizon[horizon].maturity_session

        before_analytics = _record_count(store)
        weekly = build_official_weekly_baskets(store)
        assert len(weekly) == 1
        assert all(weekly[0].basket(horizon, cohort).status == AVAILABLE
                   for horizon in (5, 20, 60) for cohort in ("SCANNER", "LUNA", "SOL"))
        scanner20 = weekly[0].basket(20, "SCANNER")
        luna20 = weekly[0].basket(20, "LUNA")
        sol20 = weekly[0].basket(20, "SOL")
        assert scanner20.gross_active_return_pct == pytest.approx(11.5)
        assert luna20.gross_active_return_pct == pytest.approx(3.0)
        assert sol20.gross_active_return_pct == pytest.approx(3.0)

        scorecard = build_final_scorecard(store)
        assert scorecard.status == INSUFFICIENT_PRIMARY_USABLE_WEEKS
        assert (scorecard.n_total_official_weeks, scorecard.n_primary_usable_weeks,
                scorecard.n_primary_pending_weeks,
                scorecard.n_primary_dqf_unavailable_weeks) == (1, 1, 0, 0)
        assert scorecard.primary_confidence_interval_95 is None
        assert scorecard.primary_mean_delta_ar20_pct == pytest.approx(-8.5)
        assert scorecard.luna_minus_scanner.mean_delta_ar20_pct == pytest.approx(-8.5)
        assert scorecard.sol_minus_luna.mean_delta_ar20_pct == pytest.approx(0.0)
        assert tuple(item.mean_delta_ar20_pct for item in scorecard.cost_sensitivity) \
            == pytest.approx((-8.5, -8.5, -8.5))
        winners = scorecard.diagnostics.missed_winner_20d
        assert (winners.scanner_winners, winners.luna_promoted, winners.sol_promoted) == (1, 0, 0)
        assert (winners.missed_by_luna_rate, winners.missed_by_sol_rate) == (1.0, 1.0)

        baseline_weeks = build_quant_baseline_weeks(store)
        assert len(baseline_weeks) == 1
        baseline = baseline_weeks[0]
        assert (baseline.baseline_id, baseline.baseline_hash) == (BASELINE_ID, BASELINE_HASH)
        assert baseline.selection_count == 1 and baseline.members == ("AAA",)
        assert baseline.members != run.sol_tickers
        assert baseline.gross_active_return_pct == pytest.approx(20.0)
        comparator = build_simple_quant_comparator(store)
        assert comparator.status == INSUFFICIENT_USABLE_WEEKS
        assert (comparator.n_total_official_weeks, comparator.n_usable,
                comparator.n_pending, comparator.n_dqf) == (1, 1, 0, 0)
        assert comparator.confidence_interval_95 is None
        assert comparator.mean_delta_ar20_pct == pytest.approx(-17.0)
        assert tuple(item.mean_delta_ar20_pct for item in comparator.cost_sensitivity) \
            == pytest.approx((-17.0, -17.0, -17.0))
        assert _record_count(store) == before_analytics

        tampered_stock = rehearsal_root / "stock_tampered"
        shutil.copytree(stock_restored, tampered_stock)
        raw_path = tampered_stock / "raw" / "AAA.json"
        raw_path.write_bytes(raw_path.read_bytes() + b" ")
        with pytest.raises(ObservationCycleError, match="stock observation verification failed"):
            create_observation_cycle(
                expectations=mature, observation_target_session=slot.slot_target_session,
                stock_root=tampered_stock, spy_root=spy_root,
                output_root=rehearsal_root / "tampered_cycle",
            )
        assert len(load_registered_cycles(store)) == 1
        assert len(load_expectation_assignments(store, expectations)) == 12
        assert len(load_outcome_finals(store, expectations)) == 12
        persisted_payloads = b"".join(
            bytes(row[0]) for row in store.connection.execute(
                "SELECT payload_json FROM validation_record"
            ).fetchall()
        )
        assert str(rehearsal_root).encode() not in persisted_payloads

    assert source_before == _file_hashes(source_root)
    assert all(path == rehearsal_root or rehearsal_root in path.parents for path in (
        source_root, store_path, stock_original, stock_restored, spy_root, cycle_root,
    ))
    assert PROTOCOL_HASH == EXPECTED_PROTOCOL_HASH
    assert BASELINE_HASH == EXPECTED_BASELINE_HASH
