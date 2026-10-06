"""Offline deterministic replay and canonical Week 0 reference support."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import platform
import socket
from typing import Any, Iterator, Mapping, Sequence
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests

from trinity.pilots.pre_research import (
    FunnelDataError,
    PriceCapture,
    UniverseMember,
    load_canonical_universe,
    run_funnel,
)
from trinity.pilots.production_funnel import _research_shell
from trinity.usa_setup_v1 import build_setup

from .atomic import atomic_bytes, atomic_json


MINIMAL_PRICE_BARS = 201
ANALYTICAL_COMMIT = "808f5afeeab44ba34ae8ef1cb76abd704f4a260b"
METHOD_VERSIONS = {
    "pre_research": "trinity.pre-research-funnel/1",
    "setup": "SETUP_V1",
}


class GoldenReplayError(RuntimeError):
    """The offline reference is incomplete or differs analytically."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_cutoff(value: str) -> tuple[datetime, date, str]:
    if not value or not value.endswith("Z"):
        raise GoldenReplayError("--decision-cutoff must be an explicit UTC timestamp ending in Z")
    try:
        cutoff = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GoldenReplayError("--decision-cutoff is not a valid ISO-8601 timestamp") from exc
    eastern = cutoff.astimezone(ZoneInfo("America/New_York"))
    if eastern.hour != 16 or eastern.minute != 0 or eastern.second != 0:
        raise GoldenReplayError("decision cutoff must identify the completed 16:00 New York close")
    return cutoff, eastern.date(), eastern.isoformat()


def _read_price_slice(path: Path, ticker: str, through: date) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FunnelDataError(f"{ticker}: invalid frozen OHLCV input") from exc
    if not isinstance(value, list) or not value:
        raise FunnelDataError(f"{ticker}: empty frozen OHLCV input")
    clipped = [row for row in value if str(row.get("date") or "") <= through.isoformat()]
    return clipped[-MINIMAL_PRICE_BARS:]


class FrozenPriceProvider:
    """Read only cutoff-clipped Week 0 bars; never acquire or write data."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.request_count = 0
        self.downloaded_bytes = 0
        self.cache_hits = 0

    def acquire(self, member: UniverseMember, through: date) -> PriceCapture:
        path = self.root / f"{member.ticker}.json"
        if not path.is_file():
            raise FunnelDataError(f"{member.ticker}: no local OHLCV baseline")
        bars = _read_price_slice(path, member.ticker, through)
        self.cache_hits += 1
        return PriceCapture(
            member.ticker, bars, "FROZEN_WEEK0_MINIMAL_201", False, 0,
        )


def _blocked_network(*_args: object, **_kwargs: object) -> None:
    raise GoldenReplayError("network access is prohibited in golden-replay mode")


@contextmanager
def prohibit_network() -> Iterator[None]:
    """Fail closed if a replay path attempts HTTP, urllib, or a socket connection."""

    with ExitStack() as stack:
        stack.enter_context(patch.object(requests.sessions.Session, "request", _blocked_network))
        stack.enter_context(patch("urllib.request.urlopen", _blocked_network))
        stack.enter_context(patch.object(socket, "create_connection", _blocked_network))
        stack.enter_context(patch.object(socket.socket, "connect", _blocked_network))
        yield


def build_input_manifest(
    universe_path: str | Path, price_root: str | Path, through: date,
) -> dict[str, Any]:
    mapping = Path(universe_path)
    prices = Path(price_root)
    members = load_canonical_universe(mapping)
    files: list[dict[str, Any]] = [{
        "relative_path": "canonical_ticker_mapping.csv",
        "semantic_role": "canonical 518 ticker-to-provider mapping",
        "byte_length": mapping.stat().st_size,
        "sha256": _sha256_file(mapping),
        "source_provenance": "Week 0 canonical universe source; byte-identical compact copy",
    }]
    absent: list[str] = []
    for member in members:
        source = prices / f"{member.ticker}.json"
        if not source.is_file():
            absent.append(f"prices/{member.ticker}.json")
            continue
        minimal = _read_price_slice(source, member.ticker, through)
        payload = canonical_bytes(minimal)
        files.append({
            "relative_path": f"prices/{member.ticker}.json",
            "semantic_role": "cutoff-clipped OHLCV; final 201 rows (or all rows when fewer)",
            "byte_length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "row_count": len(minimal),
            "source_provenance": "frozen Week 0 merged OHLCV capture",
        })
    manifest = {
        "schema_name": "trinity.shadow-week0-minimal-inputs",
        "schema_version": "1",
        "decision_session": through.isoformat(),
        "selection_rule": "rows dated on/before cutoff; retain final 201 rows",
        "files": files,
        "expected_absent_inputs": sorted(absent),
        "file_count": len(files),
        "total_bytes": sum(int(item["byte_length"]) for item in files),
    }
    return manifest


def _input_hashes(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for item in manifest["files"]:
        path = str(item["relative_path"])
        if path.startswith("prices/"):
            values[Path(path).stem] = {
                "bar_count": item["row_count"], "sha256": item["sha256"],
            }
    return values


def canonical_funnel(
    value: Mapping[str, Any], universe: Sequence[UniverseMember],
    input_manifest: Mapping[str, Any], cutoff_utc: str,
) -> dict[str, Any]:
    rejection_by_ticker = {
        str(row["ticker"]): row for row in value["rejections"]
    }
    survivor_by_ticker = {
        str(row["ticker"]): row for row in value["survivors"]
    }
    dispositions = []
    for member in universe:
        rejection = rejection_by_ticker.get(member.ticker)
        survivor = survivor_by_ticker.get(member.ticker)
        if survivor is not None:
            dispositions.append({
                "ticker": member.ticker, "data_quality": "VALID",
                "regime": survivor["regime"],
                "screening_state": survivor["screening_state"],
                "primary_reason": survivor["primary_reason"],
            })
        elif rejection is not None and rejection["reason"] == "DOWNTREND":
            dispositions.append({
                "ticker": member.ticker, "data_quality": "VALID",
                "regime": "DOWNTREND", "screening_state": "DOWNTREND_REJECTED",
                "primary_reason": rejection["reason"],
            })
        elif rejection is not None:
            dispositions.append({
                "ticker": member.ticker, "data_quality": "REJECTED",
                "regime": None, "screening_state": "DATA_QUALITY_REJECTED",
                "primary_reason": rejection["reason"],
            })
        else:
            raise GoldenReplayError(f"{member.ticker}: no canonical disposition")
    return {
        "decision_cutoff_utc": cutoff_utc,
        "latest_completed_session": value["latest_completed_session"],
        "method_versions": METHOD_VERSIONS,
        "policy": value["policy"],
        "universe": [asdict(item) for item in universe],
        "counts": value["counts"],
        "control_group": value["control_group"],
        "dispositions": dispositions,
        "shortlist": value["shortlist"],
        "survivors": value["survivors"],
        "rejections": value["rejections"],
        "minimal_input_manifest_sha256": canonical_sha256(input_manifest),
        "minimal_price_inputs": _input_hashes(input_manifest),
    }


def canonical_setups(value: Mapping[str, Any], cutoff_utc: str) -> dict[str, Any]:
    # SetupRecord.to_dict retains tuple-valued reason codes in memory while
    # JSON artifacts necessarily store arrays.  Normalize through canonical
    # JSON so representation type cannot create a false analytical mismatch.
    records = json.loads(canonical_bytes(value["records"]))
    return {
        "decision_cutoff_utc": cutoff_utc,
        "method": value["method"],
        "decision_date": value["decision_date"],
        "ready_count": value["ready_count"],
        "operational_count": value["operational_count"],
        "no_setup_count": value["no_setup_count"],
        "records": records,
    }


def _first_difference(
    expected: object, actual: object, path: str = "$",
) -> dict[str, object] | None:
    if type(expected) is not type(actual):
        return {"path": path, "expected": expected, "actual": actual,
                "detail": f"type {type(expected).__name__} != {type(actual).__name__}"}
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return {"path": path, "expected": sorted(expected), "actual": sorted(actual),
                    "detail": "object keys differ"}
        for key in expected:
            result = _first_difference(expected[key], actual[key], f"{path}.{key}")
            if result:
                return result
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return {"path": path, "expected": len(expected), "actual": len(actual),
                    "detail": "array lengths differ"}
        for index, (left, right) in enumerate(zip(expected, actual)):
            result = _first_difference(left, right, f"{path}[{index}]")
            if result:
                return result
        return None
    return None if expected == actual else {
        "path": path, "expected": expected, "actual": actual, "detail": "values differ",
    }


def _setup_hashes(value: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(row["ticker"]): canonical_sha256(row)
        for row in value["records"]
    }


def _freeze_reference(
    root: Path, universe_path: Path, input_manifest: Mapping[str, Any],
    expected_funnel: Mapping[str, Any], expected_setups: Mapping[str, Any],
    cutoff_utc: str, cutoff_new_york: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    atomic_bytes(root / "canonical_ticker_mapping.csv", universe_path.read_bytes())
    atomic_json(root / "input_manifest.json", input_manifest)
    atomic_json(root / "expected_funnel.json", expected_funnel)
    atomic_json(root / "expected_setups.json", expected_setups)
    atomic_json(root / "canonicalization_rules.json", {
        "schema_version": "1",
        "serialization": "UTF-8 JSON, sorted keys, compact separators, allow_nan=false",
        "canonical": [
            "ticker mapping and membership", "minimal input hashes", "cutoff and session",
            "method versions and policy", "all funnel counts/dispositions/metrics",
            "READY and NO_SETUP membership", "all Setup V1 analytical fields",
        ],
        "excluded_volatile": [
            "run_id", "generation/completion timestamps", "wall-clock duration",
            "absolute paths", "temporary directories", "GitHub run metadata", "process id",
            "acquisition request/cache counters and source path labels",
        ],
    })
    output_manifest = {
        "schema_name": "trinity.shadow-week0-expected-outputs",
        "schema_version": "1",
        "expected_funnel_sha256": canonical_sha256(expected_funnel),
        "expected_setups_sha256": canonical_sha256(expected_setups),
        "setup_record_sha256": _setup_hashes(expected_setups),
        "ready_tickers": [
            row["ticker"] for row in expected_funnel["survivors"]
            if row["screening_state"] == "READY_TECHNICALLY"
        ],
        "operational_setup_tickers": [
            row["ticker"] for row in expected_setups["records"]
            if row["setup_type"] != "NO_SETUP"
        ],
        "no_setup_tickers": [
            row["ticker"] for row in expected_setups["records"]
            if row["setup_type"] == "NO_SETUP"
        ],
    }
    atomic_json(root / "expected_output_manifest.json", output_manifest)
    atomic_json(root / "provenance.json", {
        "run_id": "SHADOW_USA_2026-10-06_W00",
        "decision_cutoff_utc": cutoff_utc,
        "decision_cutoff_america_new_york": cutoff_new_york,
        "analytical_commit": ANALYTICAL_COMMIT,
        "frozen_artifacts": ["pre_research_funnel.json", "setups.json"],
        "description": (
            "Compact golden records derived from the locally verified frozen Week 0 artifacts. "
            "OHLCV payloads are external and identified by input_manifest.json."
        ),
    })


def run_golden_replay(
    *, decision_cutoff: str, baseline_root: str | Path,
    universe_path: str | Path, output_root: str | Path,
    freeze_reference_root: str | Path | None = None,
    reference_root: str | Path | None = None,
) -> dict[str, Any]:
    cutoff, session, cutoff_new_york = parse_cutoff(decision_cutoff)
    baseline = Path(baseline_root)
    universe_source = Path(universe_path)
    output = Path(output_root)
    price_root = baseline / "prices"
    reference = Path(reference_root) if reference_root is not None else None
    frozen_funnel_path = baseline / "pre_research_funnel.json"
    frozen_setups_path = baseline / "setups.json"
    required_paths = [universe_source, price_root]
    if reference is None:
        required_paths.extend((frozen_funnel_path, frozen_setups_path))
    else:
        required_paths.extend((
            reference / "input_manifest.json", reference / "expected_funnel.json",
            reference / "expected_setups.json",
        ))
    for required in required_paths:
        if not required.exists():
            raise GoldenReplayError(f"required frozen input is missing: {required}")
    input_manifest = build_input_manifest(universe_source, price_root, session)
    universe = load_canonical_universe(universe_source)
    provider = FrozenPriceProvider(price_root)
    replay_funnel_path = output / "pre_research_funnel.json"
    with prohibit_network():
        result = run_funnel(
            universe_path=universe_source, output_path=replay_funnel_path,
            # The production ceiling deliberately waits 15 minutes after the
            # close.  Derive that guard time from the explicit cutoff; never
            # consult the current clock.
            provider=provider, now=cutoff + timedelta(minutes=15),
        )
        replay_funnel = json.loads(replay_funnel_path.read_text(encoding="utf-8"))
        ready = [
            row for row in replay_funnel["survivors"]
            if row["screening_state"] == "READY_TECHNICALLY"
        ]
        records = []
        for row in ready:
            ticker = str(row["ticker"])
            bars = _read_price_slice(price_root / f"{ticker}.json", ticker, session)
            records.append(build_setup(
                _research_shell(ticker, str(row["latest_session"])),
                bars, str(row["latest_session"]),
            ).to_dict())
    replay_setups = {
        "run_id": "GOLDEN_REPLAY",
        "method": "SETUP_V1",
        "decision_date": session.isoformat(),
        "ready_count": len(records),
        "operational_count": sum(row["setup_type"] != "NO_SETUP" for row in records),
        "no_setup_count": sum(row["setup_type"] == "NO_SETUP" for row in records),
        "records": records,
    }
    atomic_json(output / "setups.json", replay_setups)
    input_difference = None
    if reference is None:
        frozen_funnel = json.loads(frozen_funnel_path.read_text(encoding="utf-8"))
        frozen_setups = json.loads(frozen_setups_path.read_text(encoding="utf-8"))
        expected_funnel = canonical_funnel(
            frozen_funnel, universe, input_manifest, decision_cutoff,
        )
        expected_setups = canonical_setups(frozen_setups, decision_cutoff)
    else:
        expected_inputs = json.loads((reference / "input_manifest.json").read_text(encoding="utf-8"))
        input_difference = _first_difference(expected_inputs, input_manifest)
        expected_funnel = json.loads((reference / "expected_funnel.json").read_text(encoding="utf-8"))
        expected_setups = json.loads((reference / "expected_setups.json").read_text(encoding="utf-8"))
    actual_funnel = canonical_funnel(
        replay_funnel, universe, input_manifest, decision_cutoff,
    )
    actual_setups = canonical_setups(replay_setups, decision_cutoff)
    funnel_difference = _first_difference(expected_funnel, actual_funnel)
    setup_difference = _first_difference(expected_setups, actual_setups)
    setup_expected_hashes = _setup_hashes(expected_setups)
    setup_actual_hashes = _setup_hashes(actual_setups)
    first_difference = input_difference or funnel_difference or setup_difference
    mismatch_classification = (
        "INPUT_MISMATCH" if input_difference else
        "SERIALIZATION_MISMATCH" if first_difference and str(first_difference.get("detail", "")).startswith("type ") else
        "PORTABILITY_MISMATCH" if first_difference and platform.system() != "Windows" else
        "ANALYTICAL_MISMATCH" if first_difference else None
    )
    comparison = {
        "status": "MATCH" if not first_difference else "GOLDEN_MISMATCH",
        "decision_cutoff_utc": decision_cutoff,
        "decision_cutoff_america_new_york": cutoff_new_york,
        "counts": result.counts,
        "expected_ready": [row["ticker"] for row in expected_funnel["survivors"]
                           if row["screening_state"] == "READY_TECHNICALLY"],
        "actual_ready": [row["ticker"] for row in actual_funnel["survivors"]
                         if row["screening_state"] == "READY_TECHNICALLY"],
        "expected_operational_setups": [row["ticker"] for row in expected_setups["records"]
                                        if row["setup_type"] != "NO_SETUP"],
        "actual_operational_setups": [row["ticker"] for row in actual_setups["records"]
                                      if row["setup_type"] != "NO_SETUP"],
        "expected_no_setup": [row["ticker"] for row in expected_setups["records"]
                              if row["setup_type"] == "NO_SETUP"],
        "actual_no_setup": [row["ticker"] for row in actual_setups["records"]
                            if row["setup_type"] == "NO_SETUP"],
        "artifacts": {
            "funnel": {"expected_sha256": canonical_sha256(expected_funnel),
                       "actual_sha256": canonical_sha256(actual_funnel),
                       "result": "MATCH" if not funnel_difference else "DIFFERENT"},
            "setups": {"expected_sha256": canonical_sha256(expected_setups),
                       "actual_sha256": canonical_sha256(actual_setups),
                       "result": "MATCH" if not setup_difference else "DIFFERENT"},
            "input_manifest": {"sha256": canonical_sha256(input_manifest)},
        },
        "setup_record_hashes": {
            ticker: {"expected_sha256": setup_expected_hashes[ticker],
                     "actual_sha256": setup_actual_hashes.get(ticker),
                     "result": "MATCH" if setup_expected_hashes[ticker] == setup_actual_hashes.get(ticker)
                     else "DIFFERENT"}
            for ticker in setup_expected_hashes
        },
        "first_mismatch": first_difference,
        "mismatch_classification": mismatch_classification,
        "network_calls": 0,
        "model_calls": 0,
    }
    atomic_json(output / "input_manifest.json", input_manifest)
    atomic_json(output / "canonical_funnel.json", actual_funnel)
    atomic_json(output / "canonical_setups.json", actual_setups)
    atomic_json(output / "comparison.json", comparison)
    if comparison["status"] != "MATCH":
        raise GoldenReplayError(
            "GOLDEN_MISMATCH " + str(mismatch_classification) + ": "
            + json.dumps(first_difference, ensure_ascii=False, sort_keys=True)
        )
    if freeze_reference_root is not None:
        _freeze_reference(
            Path(freeze_reference_root), universe_source, input_manifest,
            expected_funnel, expected_setups, decision_cutoff, cutoff_new_york,
        )
    return comparison
