"""Explicit offline commands for TRINITY Live Validation V1."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Sequence

from .collector import collect_run
from .protocol_v1 import PROTOCOL_EPOCH, PROTOCOL_HASH, PROTOCOL_ID, protocol_dict
from .run_observation import append_run_observation
from .storage import ValidationStore


def _acquire_spy(args: argparse.Namespace) -> int:
    # Imported only for the explicit acquisition command; collection remains offline.
    from .spy_benchmark import acquire_spy_benchmark

    manifest = acquire_spy_benchmark(
        target_session=args.target_session, run_id=args.run_id, output_root=args.output_root,
    )
    print(json.dumps({
        "benchmark_snapshot_id": manifest["benchmark_snapshot_id"],
        "benchmark_snapshot_sha256": manifest["benchmark_snapshot_sha256"],
        "target_market_session": manifest["target_market_session"],
        "ready": manifest["coverage_gate"]["ready"],
    }, sort_keys=True))
    return 0


def _collect(args: argparse.Namespace) -> int:
    collected = collect_run(args.run_root)
    key_prefix = f"{PROTOCOL_ID}:{PROTOCOL_EPOCH}:{collected.run_id}"
    with ValidationStore(args.store) as store:
        store.append("PROTOCOL", f"{protocol_dict()['protocol_id']}:1", {
            **protocol_dict(), "protocol_hash": PROTOCOL_HASH,
        })
        run_observation_appended = append_run_observation(store, collected)
        for item in collected.selections:
            store.append("SELECTION", f"{key_prefix}:{item.ticker}", item.to_dict())
        for item in collected.setups:
            store.append("SETUP", f"{key_prefix}:{item.ticker}", item.to_dict())
        for item in collected.executions:
            store.append("EXECUTION", f"{key_prefix}:{item.ticker}:{item.freshness_timestamp}", item.to_dict())
        integrity, foreign_keys = store.integrity()
    print(json.dumps({
        "run_id": collected.run_id, "scanner": len(collected.selections),
        "run_observation_appended": run_observation_appended,
        "setups": len(collected.setups), "execution": len(collected.executions),
        "eligibility": collected.eligibility,
        "protocol_hash": PROTOCOL_HASH, "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
    }, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True)
    show = sub.add_parser("protocol", help="print the frozen machine-readable protocol")
    show.set_defaults(handler=lambda _args: print(json.dumps(
        {**protocol_dict(), "protocol_hash": PROTOCOL_HASH}, sort_keys=True
    )) or 0)
    collect = sub.add_parser("collect", help="collect one immutable Shadow run offline")
    collect.add_argument("--run-root", type=Path, required=True)
    collect.add_argument("--store", type=Path, required=True)
    collect.set_defaults(handler=_collect)
    acquire = sub.add_parser(
        "acquire-spy", help="explicitly acquire one immutable benchmark-only SPY snapshot",
    )
    acquire.add_argument("--target-session", type=date.fromisoformat, required=True)
    acquire.add_argument("--run-id", required=True)
    acquire.add_argument("--output-root", type=Path, required=True)
    acquire.set_defaults(handler=_acquire_spy)
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
