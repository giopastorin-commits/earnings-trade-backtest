#!/usr/bin/env python3
"""CLI for the outcome-free H2 V2.1 Phase 1 input build."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trinity.h2_v2_1_inputs import (  # noqa: E402
    acquire_price_ensemble,
    acquire_sec_corpus,
    build_phase1_inputs,
    parse_utc_datetime,
    verify_price_ensemble,
    verify_sec_corpus,
)
from trinity.twelvedata_split_factors import (  # noqa: E402
    acquire_split_adjusted_snapshot,
    apply_split_factor_screen,
    verify_split_adjusted_snapshot,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build H2 V2.1 Phase 1 inputs without outcomes or predictions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prices = subparsers.add_parser("acquire-prices")
    prices.add_argument("--price-root", type=Path, required=True)

    sec = subparsers.add_parser("acquire-sec")
    sec.add_argument("--sec-root", type=Path, required=True)
    sec.add_argument(
        "--cutoff-utc",
        help="Optional aware ISO-8601 acquisition cutoff; defaults to start time.",
    )

    verify = subparsers.add_parser("verify-sources")
    verify.add_argument("--price-root", type=Path, required=True)
    verify.add_argument("--sec-root", type=Path, required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    build.add_argument("--sec-root", type=Path, required=True)
    build.add_argument("--price-root", type=Path, required=True)
    build.add_argument("--text-root", type=Path, required=True)
    build.add_argument("--output-root", type=Path, required=True)

    split_factors = subparsers.add_parser("acquire-split-factors")
    split_factors.add_argument("--phase1-root", type=Path, required=True)
    split_factors.add_argument("--snapshot-root", type=Path, required=True)

    verify_split_factors = subparsers.add_parser("verify-split-factors")
    verify_split_factors.add_argument("--snapshot-root", type=Path, required=True)

    apply_split_factors = subparsers.add_parser("apply-split-factors")
    apply_split_factors.add_argument("--phase1-root", type=Path, required=True)
    apply_split_factors.add_argument("--price-root", type=Path, required=True)
    apply_split_factors.add_argument("--snapshot-root", type=Path, required=True)
    apply_split_factors.add_argument("--output-root", type=Path, required=True)
    return parser


def _small_price_result(manifest: dict[str, object]) -> dict[str, object]:
    return {
        key: manifest.get(key)
        for key in (
            "snapshot_id", "target_session", "issuer_snapshot_sha256",
            "ensemble_sha256", "manifest_file_sha256",
            "acquisition_started_at_utc", "acquisition_completed_at_utc",
        )
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "acquire-prices":
        result = _small_price_result(acquire_price_ensemble(args.price_root))
    elif args.command == "acquire-sec":
        cutoff: datetime | None = None
        if args.cutoff_utc:
            cutoff = parse_utc_datetime(args.cutoff_utc)
            if cutoff is None:
                raise SystemExit("--cutoff-utc must be an aware ISO-8601 timestamp")
        manifest = acquire_sec_corpus(args.sec_root, cutoff=cutoff)
        result = {
            key: manifest.get(key)
            for key in (
                "run_started_at_utc", "index_sha256", "ensemble_sha256",
                "manifest_file_sha256",
            )
        }
    elif args.command == "verify-sources":
        price = verify_price_ensemble(args.price_root)
        sec, _index = verify_sec_corpus(args.sec_root)
        result = {
            "price_ensemble_sha256": price.get("ensemble_sha256"),
            "sec_ensemble_sha256": sec.get("ensemble_sha256"),
        }
    elif args.command == "acquire-split-factors":
        manifest = acquire_split_adjusted_snapshot(
            parent_root=args.phase1_root, output_root=args.snapshot_root,
        )
        result = {
            key: manifest.get(key)
            for key in (
                "complete", "completed_ticker_count", "remaining_ticker_count",
                "acquisition_started_at_utc", "acquisition_completed_at_utc",
                "canonical_ticker_count", "ensemble_sha256",
                "manifest_file_sha256", "checkpoint_sha256",
            )
        }
    elif args.command == "verify-split-factors":
        manifest = verify_split_adjusted_snapshot(args.snapshot_root)
        result = {
            "canonical_ticker_count": manifest.get("canonical_ticker_count"),
            "ensemble_sha256": manifest.get("ensemble_sha256"),
        }
    elif args.command == "apply-split-factors":
        result = apply_split_factor_screen(
            parent_root=args.phase1_root, none_price_root=args.price_root,
            adjusted_root=args.snapshot_root, output_root=args.output_root,
        )
    else:
        result = build_phase1_inputs(
            repo_root=args.repo_root, sec_root=args.sec_root,
            price_root=args.price_root, text_root=args.text_root,
            output_root=args.output_root,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
