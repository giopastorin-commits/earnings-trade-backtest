"""Production ordering from READY screening through Setup, Luna, and Sol routing."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trinity.pilots.luna_triage import (
    DEFAULT_SOURCE_ROOT as DEFAULT_LUNA_SOURCE_ROOT,
    run_triage,
    validate_ready_entries,
)
from trinity.pilots.pre_research import DEFAULT_CACHE, DEFAULT_OUTPUT
from trinity.usa_setup_v1 import SetupRecord, build_setup
from trinity.twelvedata_prices import ValidatedPriceSnapshot


DEFAULT_LUNA_OUTPUT = Path("data/local/production_funnel_luna_latest.json")
DEFAULT_WATCHLIST = Path("data/local/trinity_active_watchlist_latest.json")
DEFAULT_LEDGER = Path("data/local/trinity_production_funnel.sqlite3")


class ProductionFunnelError(RuntimeError):
    """A frozen screening input or downstream routing result was invalid."""


@dataclass(frozen=True)
class ProductionFunnelResult:
    ready_count: int
    operational_setup_count: int
    no_setup_count: int
    luna_eligible_count: int
    watch_tickers: tuple[str, ...]
    escalate_tickers: tuple[str, ...]
    drop_tickers: tuple[str, ...]
    failed_tickers: tuple[str, ...]
    setups: tuple[SetupRecord, ...]
    watchlist: dict[str, Any]
    luna_result: dict[str, Any]
    sol_result: object | None


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_funnel(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionFunnelError(
            f"invalid pre-research funnel artifact: {type(exc).__name__}"
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("survivors"), list):
        raise ProductionFunnelError("pre-research funnel artifact lacks survivors")
    rows = [row for row in value["survivors"] if isinstance(row, dict)
            and row.get("screening_state") == "READY_TECHNICALLY"]
    return value, validate_ready_entries(rows, expected_count=None)


def _load_bars(
    ticker: str, *, price_dir: Path, inputs: Mapping[str, object],
) -> list[dict[str, Any]]:
    path = price_dir / f"{ticker}.json"
    try:
        bars = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionFunnelError(f"{ticker}: invalid captured OHLCV") from exc
    if not isinstance(bars, list):
        raise ProductionFunnelError(f"{ticker}: captured OHLCV is not an array")
    metadata = inputs.get(ticker)
    expected = metadata.get("sha256") if isinstance(metadata, dict) else None
    if not isinstance(expected, str) or _canonical_sha256(bars) != expected:
        raise ProductionFunnelError(f"{ticker}: captured OHLCV SHA-256 mismatch")
    return bars


def _research_shell(ticker: str, as_of: str) -> dict[str, object]:
    return {
        "ticker": ticker, "as_of": as_of, "status": "INVESTIGATE",
        "evidence_confidence": "HIGH", "thesis_strength": "MEDIUM",
        "event_assessments": [],
    }


def _watchlist_value(
    setups: Mapping[str, SetupRecord], luna_rows: Sequence[Mapping[str, object]],
    *, generated_at: str,
) -> dict[str, Any]:
    entries = []
    for row in sorted(luna_rows, key=lambda item: str(item.get("ticker", ""))):
        if row.get("status") != "SUCCESS" or row.get("decision") != "WATCH":
            continue
        ticker = str(row["ticker"])
        setup = setups[ticker]
        entries.append({
            "ticker": ticker, "as_of_session": setup.as_of,
            "setup_type": setup.setup_type, "entry": setup.entry_level,
            "stop": setup.stop_level, "tp1": setup.tp1, "tp2": setup.tp2,
            "rr1": setup.rr_tp1, "rr2": setup.rr_tp2,
            "luna_priority": row["qualitative_priority"],
            "luna_primary_reason": row["primary_reason"],
            "state": "ACTIVE_WATCH",
        })
    return {
        "schema_name": "trinity.production-funnel-active-watchlist",
        "schema_version": "1", "generated_at": generated_at,
        "entries": entries,
    }


def run_production_funnel(
    *, funnel_path: str | Path = DEFAULT_OUTPUT,
    price_dir: str | Path = DEFAULT_CACHE,
    luna_output_path: str | Path | None = DEFAULT_LUNA_OUTPUT,
    watchlist_path: str | Path | None = DEFAULT_WATCHLIST,
    luna_source_root: str | Path = DEFAULT_LUNA_SOURCE_ROOT,
    ledger_db: str | Path = DEFAULT_LEDGER,
    setup_builder: Callable[..., SetupRecord] = build_setup,
    triage_runner: Callable[..., dict[str, Any]] = run_triage,
    sol_runner: Callable[..., object] | None = None,
    price_snapshot: ValidatedPriceSnapshot | None = None,
) -> ProductionFunnelResult:
    """Run Setup first, then Luna, routing only ESCALATE names to Sol."""

    funnel, ready = _load_funnel(funnel_path)
    inputs = funnel.get("inputs")
    if not isinstance(inputs, dict):
        raise ProductionFunnelError("pre-research funnel artifact lacks input provenance")
    if price_snapshot is not None:
        expected = price_snapshot.provenance()
        actual = funnel.get("price_snapshot")
        normalized = {
            "price_provider": actual.get("provider"),
            "price_snapshot_id": actual.get("snapshot_id"),
            "price_snapshot_session": actual.get("session"),
            "price_snapshot_sha256": actual.get("sha256"),
        } if isinstance(actual, dict) else None
        if normalized != expected:
            raise ProductionFunnelError("funnel price snapshot provenance mismatch")
    setups: list[SetupRecord] = []
    operational_rows: list[dict[str, Any]] = []
    for row in ready:
        ticker, as_of = str(row["ticker"]), str(row.get("latest_session") or "")
        bars = _load_bars(ticker, price_dir=Path(price_dir), inputs=inputs)
        setup = setup_builder(_research_shell(ticker, as_of), bars, as_of)
        if setup.ticker != ticker or setup.as_of != as_of:
            raise ProductionFunnelError(f"{ticker}: Setup identity mismatch")
        setups.append(setup)
        if setup.setup_type != "NO_SETUP":
            operational_rows.append(row)

    if operational_rows:
        luna = triage_runner(
            funnel_path=funnel_path, output_path=luna_output_path,
            source_root=luna_source_root, expected_count=len(operational_rows),
            ready_entries=operational_rows,
        )
    else:
        luna = {
            "schema_name": "trinity.luna-triage", "schema_version": "1",
            "prompt_version": "luna-triage-v1", "finished_at": "-",
            "counts": {"ready_input": 0, "drop": 0, "watch": 0,
                       "escalate": 0, "failed": 0},
            "escalate_tickers": [], "results": [],
        }
    rows = luna.get("results")
    if not isinstance(rows, list):
        raise ProductionFunnelError("Luna result lacks results list")
    eligible = {str(row["ticker"]) for row in operational_rows}
    returned = {str(row.get("ticker") or "") for row in rows}
    if returned != eligible or len(rows) != len(eligible):
        raise ProductionFunnelError("Luna result ticker set differs from operational setups")
    invalid = [row for row in rows if row.get("status") == "SUCCESS" and
               row.get("decision") not in {"DROP", "WATCH", "ESCALATE"}]
    if invalid:
        raise ProductionFunnelError("Luna result contains an invalid decision")

    setup_by_ticker = {item.ticker: item for item in setups if item.setup_type != "NO_SETUP"}
    watchlist = _watchlist_value(
        setup_by_ticker, rows, generated_at=str(luna.get("finished_at") or "-"),
    )
    if watchlist_path is not None:
        path = Path(watchlist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(watchlist, ensure_ascii=False, indent=2), encoding="utf-8")

    successful = [row for row in rows if row.get("status") == "SUCCESS"]
    escalate = tuple(sorted(str(row["ticker"]) for row in successful
                            if row.get("decision") == "ESCALATE"))
    watch = tuple(sorted(str(row["ticker"]) for row in successful
                         if row.get("decision") == "WATCH"))
    drop = tuple(sorted(str(row["ticker"]) for row in successful
                        if row.get("decision") == "DROP"))
    failed = tuple(sorted(str(row.get("ticker") or "") for row in rows
                          if row.get("status") != "SUCCESS"))
    sol_result = None
    if escalate:
        if sol_runner is not None:
            sol_result = sol_runner(ledger_db, escalate, dry_run=True)
        else:
            if luna_output_path is None:
                raise ProductionFunnelError(
                    "production Sol routing requires the current Luna output artifact"
                )
            from trinity.pilots.forward import run_forward_pilot
            from trinity.usa_forward import EODHDNewsSECForwardProvider

            continuity_path = Path(luna_output_path)
            if price_snapshot is None:
                raise ProductionFunnelError(
                    "production Sol routing requires the validated price snapshot"
                )
            sol_result = run_forward_pilot(
                ledger_db, escalate, dry_run=True,
                source_provider_factory=lambda root: EODHDNewsSECForwardProvider(
                    root, price_snapshot=price_snapshot,
                    continuity_path=continuity_path,
                ),
            )
    return ProductionFunnelResult(
        ready_count=len(ready), operational_setup_count=len(operational_rows),
        no_setup_count=len(ready) - len(operational_rows),
        luna_eligible_count=len(operational_rows), watch_tickers=watch,
        escalate_tickers=escalate, drop_tickers=drop, failed_tickers=failed,
        setups=tuple(setups), watchlist=watchlist,
        luna_result=luna, sol_result=sol_result,
    )


def render_summary(result: ProductionFunnelResult) -> str:
    return "\n".join((
        f"READY: {result.ready_count}",
        f"Operational Setup: {result.operational_setup_count}",
        f"NO_SETUP: {result.no_setup_count}",
        f"Luna eligible: {result.luna_eligible_count}",
        f"WATCH: {len(result.watch_tickers)} ({' '.join(result.watch_tickers) or '-'})",
        f"ESCALATE: {len(result.escalate_tickers)} ({' '.join(result.escalate_tickers) or '-'})",
        f"DROP: {len(result.drop_tickers)} ({' '.join(result.drop_tickers) or '-'})",
        f"FAILED: {len(result.failed_tickers)} ({' '.join(result.failed_tickers) or '-'})",
    ))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--funnel", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--price-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--luna-output", type=Path, default=DEFAULT_LUNA_OUTPUT)
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--luna-source-root", type=Path, default=DEFAULT_LUNA_SOURCE_ROOT)
    parser.add_argument("--ledger-db", type=Path, default=DEFAULT_LEDGER)
    args = parser.parse_args(argv)
    try:
        result = run_production_funnel(
            funnel_path=args.funnel, price_dir=args.price_dir,
            luna_output_path=args.luna_output, watchlist_path=args.watchlist,
            luna_source_root=args.luna_source_root, ledger_db=args.ledger_db,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(render_summary(result))
    return 1 if result.failed_tickers else 0


if __name__ == "__main__":
    raise SystemExit(main())
