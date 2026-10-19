from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket

import exchange_calendars
import pytest

from trinity.twelvedata_prices import (
    ADJUSTMENT, INTERVAL, PRICE_PROVIDER, ProviderCapture, canonical_bytes, snapshot_hash,
)
from trinity.validation.contracts import CollectedRun, OFFICIAL_ELIGIBLE, SelectionObservation
from trinity.validation.market_calendar import derive_outcome_sessions, session_times
from trinity.validation.maturity import (
    MaturityError, OUTCOME_EXPECTATION, OUTCOME_FINAL, ObservationCatalog,
    append_outcome_expectations, build_observation_catalog, build_outcome_expectations,
    catalog_spy_snapshot, catalog_stock_snapshot, mature_outcomes,
    resolve_first_eligible_observation,
)
from trinity.validation.cli import main as validation_cli
from trinity.validation.protocol_v1 import PROTOCOL_HASH
from trinity.validation.spy_benchmark import acquire_spy_benchmark
from trinity.validation.storage import ValidationStorageConflict, ValidationStore


SHA = "a" * 64
GIT_SHA = "b" * 40
DECISION = "2026-10-09T20:00:00Z"


def _sessions(through: str) -> list[str]:
    calendar = exchange_calendars.get_calendar(
        "XNYS", start=date(2026, 10, 12), end=date.fromisoformat(through),
    )
    return [label.date().isoformat() for label in calendar.schedule.index]


def _bars(through: str, *, base: float = 100.0, omit: set[str] | None = None):
    omitted = omit or set()
    return [
        {
            "date": session, "open": base + index, "high": base + index + 2,
            "low": base + index - 1, "close": base + index + 1, "volume": 1000.0,
        }
        for index, session in enumerate(_sessions(through)) if session not in omitted
    ]


def _completion(target: str, *, before_close: bool = False) -> tuple[datetime, datetime]:
    _opened, closed = session_times(target)
    completed = closed - timedelta(minutes=1) if before_close else closed + timedelta(minutes=2)
    return completed - timedelta(minutes=1), completed


def _stock_snapshot(
    root: Path, target: str, cycle: str, *, ticker: str = "AAA", base: float = 100,
    omit: set[str] | None = None, before_close: bool = False, ready: bool = True,
) -> Path:
    bars = _bars(target, base=base, omit=omit)
    raw = canonical_bytes(bars)
    path = root / "normalized" / f"{ticker}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    started, completed = _completion(target, before_close=before_close)
    entry = {
        "canonical_ticker": ticker, "provider_symbol": ticker,
        "classification": "ACTIVE_COMPLETE", "first_date": bars[0]["date"] if bars else None,
        "last_valid_date": bars[-1]["date"] if bars else None, "bar_count": len(bars),
        "content_sha256": hashlib.sha256(raw).hexdigest(), "raw_sha256": None,
        "excluded_rows": [], "attempts": [], "retry_count": 0, "error": None,
    }
    manifest = {
        "schema_name": "trinity.twelvedata-price-snapshot", "schema_version": "1",
        "snapshot_id": f"TWELVEDATA_{target}_RUN_{cycle}", "provider": PRICE_PROVIDER,
        "interval": INTERVAL, "adjust": ADJUSTMENT, "target_market_session": target,
        "acquisition_started_at_utc": started.isoformat(),
        "acquisition_completed_at_utc": completed.isoformat(),
        "canonical_ticker_count": 1, "active_complete_count": 1,
        "explicit_exception_count": 0, "provider_failure_count": 0, "invalid_data_count": 0,
        "classification_counts": {"ACTIVE_COMPLETE": 1},
        "coverage_gate": {
            "ready": ready, "allowed_states": ["ACTIVE_COMPLETE", "CORPORATE_ACTION_NO_LONGER_TRADING"],
            "blocking_tickers": [] if ready else [ticker],
        },
        "tickers": [entry],
    }
    manifest["snapshot_sha256"] = snapshot_hash(manifest)
    (root / "snapshot_manifest.json").write_bytes(canonical_bytes(manifest))
    return root


class _Spy:
    api_key = "fixture-secret"

    def __init__(self, target: str, base: float = 400) -> None:
        self.target = target
        self.base = base

    def fetch(self, symbol, *, start_date, target_session, remaining_tickers=None):
        assert symbol == "SPY" and target_session.isoformat() == self.target
        values = [
            {
                "datetime": row["date"], "open": str(row["open"]), "high": str(row["high"]),
                "low": str(row["low"]), "close": str(row["close"]), "volume": str(row["volume"]),
            }
            for row in reversed(_bars(self.target, base=self.base))
        ]
        raw = json.dumps({"meta": {"symbol": "SPY"}, "values": values}).encode()
        return ProviderCapture(raw=raw, attempts=({"attempt": 1, "status_code": 200},))


def _spy_snapshot(
    root: Path, target: str, cycle: str, *, base: float = 400,
    before_close: bool = False,
) -> Path:
    started, completed = _completion(target, before_close=before_close)
    moments = iter((started, completed))
    acquire_spy_benchmark(
        target_session=date.fromisoformat(target), run_id=cycle, output_root=root,
        client=_Spy(target, base), now=lambda: next(moments),
    )
    return root


def _selection(*, eligibility: str = OFFICIAL_ELIGIBLE) -> SelectionObservation:
    return SelectionObservation(
        run_id="RUN_WEEK_0", intended_session="2026-10-09", run_timestamp_utc=DECISION,
        ticker="AAA", universe_version="registry-v1", universe_sha256=SHA,
        scanner_selected=True, scanner_state="READY_TECHNICALLY", scanner_reason="fixture",
        luna_evaluated=True, luna_status="COMPLETED", luna_decision="ESCALATE",
        luna_model="luna", luna_prompt_version="v1", luna_input_sha256=None,
        luna_output_sha256=None, luna_cost_usd="0", sol_evaluated=True,
        sol_status="PROMOTED", sol_decision="INVESTIGATE", sol_model="sol",
        sol_prompt_version="v1", sol_input_sha256=None, sol_output_sha256=None,
        sol_cost_usd="0", analytical_commit_sha=GIT_SHA,
        snapshot_id="TWELVEDATA_2026-10-09_RUN_WEEK_0", snapshot_sha256=SHA,
        eligibility=eligibility,
    )


def _collected(*, eligibility: str = OFFICIAL_ELIGIBLE) -> CollectedRun:
    return CollectedRun(
        run_id="RUN_WEEK_0", source_root="fixture", source_manifest_sha256=SHA,
        selections=(_selection(eligibility=eligibility),), setups=(), executions=(),
        total_api_cost_usd="0", eligibility=eligibility,
    )


def _catalog(tmp_path: Path, target: str, cycle: str, **kwargs) -> ObservationCatalog:
    stock = _stock_snapshot(tmp_path / f"stock-{cycle}", target, cycle, **kwargs)
    spy = _spy_snapshot(tmp_path / f"spy-{cycle}", target, cycle)
    return build_observation_catalog(
        stock=[(stock, f"stock:{cycle}")], spy=[(spy, f"spy:{cycle}")],
    )


def _merge(*catalogs: ObservationCatalog) -> ObservationCatalog:
    return ObservationCatalog(
        stock=tuple(item for catalog in catalogs for item in catalog.stock),
        spy=tuple(item for catalog in catalogs for item in catalog.spy),
    )


def test_expectations_freeze_three_cohort_identities_and_calendar_lineage():
    expectations = build_outcome_expectations(_collected())
    assert len(expectations) == 9
    assert {item.cohort for item in expectations} == {"SCANNER", "LUNA", "SOL"}
    assert {item.horizon_sessions for item in expectations} == {5, 20, 60}
    assert all(item.decision_target_session == "2026-10-09" for item in expectations)
    assert all(item.expected_first_tradable_session == "2026-10-12" for item in expectations)
    assert len({item.key for item in expectations}) == 9


def test_multi_week_append_only_maturation_never_restates_final_prices(tmp_path):
    expectations = build_outcome_expectations(_collected())
    scanner = {item.horizon_sessions: item for item in expectations if item.cohort == "SCANNER"}
    d5, d20, d60 = (scanner[value].maturity_session for value in (5, 20, 60))
    before_d5 = _sessions(d5)[-2]
    catalogs = [_catalog(tmp_path, before_d5, "EARLY")]
    database = tmp_path / "validation.sqlite3"
    with ValidationStore(database) as store:
        append_outcome_expectations(store, _collected())
        first = mature_outcomes(store, _merge(*catalogs))
        assert first.new_records == 0 and first.pending == 9

        catalogs.append(_catalog(tmp_path, d5, "D5"))
        second = mature_outcomes(store, _merge(*catalogs))
        assert second.completed == 3 and second.pending == 6
        d5_before = {
            key: payload for key, payload in store.record_items(OUTCOME_FINAL)
            if key.endswith(":5")
        }

        catalogs.append(_catalog(tmp_path, d20, "D20"))
        third = mature_outcomes(store, _merge(*catalogs))
        assert third.completed == 3 and third.pending == 3 and third.already_final == 3

        catalogs.append(_catalog(tmp_path, d60, "D60"))
        fourth = mature_outcomes(store, _merge(*catalogs))
        assert fourth.completed == 3 and fourth.pending == 0 and fourth.already_final == 6

        later = _catalog(tmp_path, d60, "ZZ_LATER", base=900)
        fifth = mature_outcomes(store, _merge(*catalogs, later))
        assert fifth.new_records == 0 and fifth.already_final == 9
        assert d5_before == {
            key: payload for key, payload in store.record_items(OUTCOME_FINAL)
            if key.endswith(":5")
        }
        assert all(payload["status"] == "COMPLETE" for payload in store.records(OUTCOME_FINAL))


def test_no_lookahead_rejects_snapshot_acquired_before_maturity_close(tmp_path):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    stock = _stock_snapshot(tmp_path / "stock", expectation.maturity_session, "EARLY", before_close=True)
    spy = _spy_snapshot(tmp_path / "spy", expectation.maturity_session, "EARLY", before_close=True)
    catalog = build_observation_catalog(stock=[(stock, "stock:early")], spy=[(spy, "spy:early")])
    assert resolve_first_eligible_observation(expectation, catalog).state == "NOT_MATURE_YET"


def test_first_bundle_missing_stock_ticker_is_final_dq_and_not_replaced(tmp_path):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    missing = _catalog(tmp_path, expectation.maturity_session, "FIRST", ticker="BBB")
    later = _catalog(tmp_path, expectation.maturity_session, "SECOND", base=900)
    with ValidationStore(tmp_path / "v.sqlite3") as store:
        store.append(OUTCOME_EXPECTATION, expectation.key, expectation.to_dict())
        summary = mature_outcomes(store, _merge(missing, later))
        assert summary.data_quality_failure == 1
        final = store.records(OUTCOME_FINAL)[0]
        assert final["quality_flags"] == ["STOCK_TICKER_MISSING_FROM_OBSERVATION_SNAPSHOT"]
        assert final["stock_observation_snapshot_id"].endswith("RUN_FIRST")


def test_missing_spy_or_target_mismatch_is_data_quality_failure(tmp_path):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    stock_root = _stock_snapshot(tmp_path / "stock", expectation.maturity_session, "ONE")
    stock = catalog_stock_snapshot(stock_root, archive_identity="stock:one")
    for catalog in (
        ObservationCatalog((stock,), ()),
        ObservationCatalog((stock,), (
            catalog_spy_snapshot(
                _spy_snapshot(tmp_path / "spy-other", _sessions(expectation.maturity_session)[-2], "ONE"),
                archive_identity="spy:other",
            ),
        )),
    ):
        resolution = resolve_first_eligible_observation(expectation, catalog)
        assert resolution.state == "DATA_QUALITY_FAILURE"
        assert resolution.quality_flags == ("SPY_OBSERVATION_BUNDLE_MISSING",)


def test_missing_d1_or_dn_in_first_bundle_is_data_quality_failure(tmp_path):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    for label, omitted, expected in (
        ("d1", {expectation.expected_first_tradable_session}, "FIRST_TRADABLE_SESSION_MISSING"),
        ("dn", {expectation.maturity_session}, "MATURITY_SESSION_MISSING"),
    ):
        catalog = _catalog(tmp_path / label, expectation.maturity_session, label.upper(), omit=omitted)
        with ValidationStore(tmp_path / f"{label}.sqlite3") as store:
            store.append(OUTCOME_EXPECTATION, expectation.key, expectation.to_dict())
            mature_outcomes(store, catalog)
            assert store.records(OUTCOME_FINAL)[0]["quality_flags"] == [expected]


def test_not_ready_and_corrupted_snapshots_fail_closed_during_catalog(tmp_path):
    target = derive_outcome_sessions(DECISION, 5)[1]
    not_ready = _stock_snapshot(tmp_path / "not-ready", target, "NR", ready=False)
    with pytest.raises(MaturityError, match="not catalogable"):
        catalog_stock_snapshot(not_ready, archive_identity="stock:nr")
    corrupt = _stock_snapshot(tmp_path / "corrupt", target, "BAD")
    (corrupt / "normalized" / "AAA.json").write_text("[]", encoding="utf-8")
    with pytest.raises(MaturityError, match="hash mismatch"):
        catalog_stock_snapshot(corrupt, archive_identity="stock:bad")
    spy = _spy_snapshot(tmp_path / "spy", target, "SPY")
    (spy / "READY").unlink()
    with pytest.raises(MaturityError, match="SPY snapshot is not catalogable"):
        catalog_spy_snapshot(spy, archive_identity="spy:bad")


def test_corrupted_spy_hash_fails_closed(tmp_path):
    target = derive_outcome_sessions(DECISION, 5)[1]
    spy = _spy_snapshot(tmp_path / "spy", target, "BAD")
    (spy / "normalized" / "SPY.json").write_text("[]", encoding="utf-8")
    with pytest.raises(MaturityError, match="hash mismatch"):
        catalog_spy_snapshot(spy, archive_identity="spy:bad")


def test_duplicate_catalog_and_deterministic_tie_break(tmp_path):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    first = _catalog(tmp_path / "a", expectation.maturity_session, "A")
    with pytest.raises(MaturityError, match="duplicate"):
        _merge(first, first)
    second = _catalog(tmp_path / "b", expectation.maturity_session, "B")
    resolution = resolve_first_eligible_observation(expectation, _merge(second, first))
    assert resolution.bundle is not None
    assert resolution.bundle.stock.snapshot_id.endswith("RUN_A")


def test_final_lineage_is_explicit_idempotent_and_divergence_fails_closed(tmp_path):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    catalog = _catalog(tmp_path, expectation.maturity_session, "FINAL")
    with ValidationStore(tmp_path / "v.sqlite3") as store:
        store.append(OUTCOME_EXPECTATION, expectation.key, expectation.to_dict())
        first = mature_outcomes(store, catalog)
        assert first.new_records == 1 and first.completed == 1
        payload = store.records(OUTCOME_FINAL)[0]
        assert payload["decision_snapshot_id"] == "TWELVEDATA_2026-10-09_RUN_WEEK_0"
        assert payload["stock_observation_snapshot_id"].endswith("RUN_FINAL")
        assert payload["decision_snapshot_id"] != payload["stock_observation_snapshot_id"]
        assert payload["spy_observation_target_session"] == payload["stock_observation_target_session"]
        assert payload["protocol_hash"] == PROTOCOL_HASH
        second = mature_outcomes(store, catalog)
        assert second.already_final == 1 and second.new_records == 0
        changed = dict(payload, future_price=float(payload["future_price"]) + 1)
        with pytest.raises(ValidationStorageConflict, match="divergent"):
            store.append(OUTCOME_FINAL, expectation.key, changed)


def test_future_spy_matches_observation_not_decision_session(tmp_path):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 20)
    catalog = _catalog(tmp_path, expectation.maturity_session, "NOVEMBER")
    resolution = resolve_first_eligible_observation(expectation, catalog)
    assert resolution.bundle is not None
    assert resolution.bundle.observation_snapshot_target_session == expectation.maturity_session
    assert resolution.bundle.observation_snapshot_target_session != expectation.decision_target_session


def test_maturity_engine_has_no_network_dependency(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("network attempted")
    ))
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    catalog = _catalog(tmp_path, expectation.maturity_session, "OFFLINE")
    with ValidationStore(tmp_path / "v.sqlite3") as store:
        store.append(OUTCOME_EXPECTATION, expectation.key, expectation.to_dict())
        assert mature_outcomes(store, catalog).completed == 1


def test_explicit_offline_mature_cli_reports_summary(tmp_path, capsys):
    expectation = next(item for item in build_outcome_expectations(_collected())
                       if item.cohort == "SCANNER" and item.horizon_sessions == 5)
    target = expectation.maturity_session
    stock = _stock_snapshot(tmp_path / "stock", target, "CLI")
    spy = _spy_snapshot(tmp_path / "spy", target, "CLI")
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps({
        "stock": [{"root": str(stock), "archive_identity": "r2:stock/CLI"}],
        "spy": [{"root": str(spy), "archive_identity": "r2:spy/CLI"}],
    }), encoding="utf-8")
    database = tmp_path / "validation.sqlite3"
    with ValidationStore(database) as store:
        store.append(OUTCOME_EXPECTATION, expectation.key, expectation.to_dict())
    assert validation_cli([
        "mature-outcomes", "--store", str(database), "--catalog", str(catalog_path),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "already_final": 0, "completed": 1, "data_quality_failure": 0,
        "expectations_total": 1, "foreign_key_violations": 0,
        "integrity_check": "ok", "new_records": 1, "pending": 0,
    }
