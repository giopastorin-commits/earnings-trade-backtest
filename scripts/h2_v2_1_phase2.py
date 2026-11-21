#!/usr/bin/env python3
"""CLI for the frozen H2 V2.1 Phase 2 seal and DEV-only Part A."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trinity.h2_v2_1_phase2 import (  # noqa: E402
    build_dev_outcomes,
    build_model,
    build_predictions,
    build_seal_manifest,
    run_part_a,
    verify_frozen_sources,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seal H2 V2.1 Phase 2 without PROSP outcomes")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seal = subparsers.add_parser("build-seal")
    seal.add_argument("--dev-input", type=Path, required=True)
    seal.add_argument("--prosp-input", type=Path, required=True)
    seal.add_argument("--price-root", type=Path, required=True)
    seal.add_argument("--split-root", type=Path, required=True)
    seal.add_argument("--output-root", type=Path, required=True)
    part_a = subparsers.add_parser("part-a")
    part_a.add_argument("--dev-input", type=Path, required=True)
    part_a.add_argument("--outcomes", type=Path, required=True)
    part_a.add_argument("--model", type=Path, required=True)
    part_a.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "part-a":
        result = run_part_a(
            dev_path=args.dev_input, outcomes_path=args.outcomes,
            model_path=args.model, output_path=args.output,
        )
        print(json.dumps({
            "report_sha256": result["report_sha256"],
            "M1_valid_reps": result["M1"]["valid_reps"],
            "M1x_valid_reps": result["M1x"]["valid_reps"],
        }, indent=2, sort_keys=True))
        return 0
    if args.output_root.exists() and any(args.output_root.iterdir()):
        raise SystemExit("Phase 2 output root must be new and empty")
    args.output_root.mkdir(parents=True, exist_ok=True)
    price_manifest, split_manifest = verify_frozen_sources(
        dev_path=args.dev_input, prosp_path=args.prosp_input,
        price_root=args.price_root, split_root=args.split_root,
    )
    price_manifest = dict(price_manifest)
    split_manifest = dict(split_manifest)
    price_manifest["_manifest_path"] = str(args.price_root / "price_ensemble_manifest.json")
    split_manifest["_manifest_path"] = str(
        args.split_root / "split_adjusted_snapshot_manifest.json"
    )
    outcomes_path = args.output_root / "h2_v2_1_dev_outcomes.csv"
    model_path = args.output_root / "h2_v2_1_model.json"
    predictions_path = args.output_root / "h2_v2_1_predictions_batch1.csv"
    outcome_summary = build_dev_outcomes(
        dev_path=args.dev_input, price_root=args.price_root,
        split_root=args.split_root, output_path=outcomes_path,
    )
    build_model(
        dev_path=args.dev_input, outcomes_path=outcomes_path, output_path=model_path,
    )
    prediction_summary = build_predictions(
        prosp_path=args.prosp_input, model_path=model_path, output_path=predictions_path,
    )
    manifest = build_seal_manifest(
        repo_root=REPO_ROOT, dev_path=args.dev_input, prosp_path=args.prosp_input,
        outcomes_path=outcomes_path, model_path=model_path,
        predictions_path=predictions_path, price_manifest=price_manifest,
        split_manifest=split_manifest, outcome_summary=outcome_summary,
        prediction_summary=prediction_summary,
        output_path=REPO_ROOT / "H2_V2_1_SEAL_MANIFEST.json",
    )
    dev_tickers = {row["ticker"] for row in _read_identity_rows(args.dev_input)}
    prosp_tickers = {row["ticker"] for row in _read_identity_rows(args.prosp_input)}
    result = {
        "dev_outcomes_sha256": outcome_summary["sha256"],
        "dev_admitted_count": outcome_summary["admitted_count"],
        "dev_excluded_count": outcome_summary["excluded_count"],
        "dev_exclusion_counts": outcome_summary["exclusion_counts"],
        "dev_prosp_overlap_ticker_count": len(dev_tickers & prosp_tickers),
        "model_sha256": _sha256(model_path),
        "predictions_sha256": prediction_summary["sha256"],
        "predicted_count": prediction_summary["predicted_count"],
        "seal_manifest_sha256": _sha256(REPO_ROOT / "H2_V2_1_SEAL_MANIFEST.json"),
        "s_seal": manifest["prosp_batch1"]["s_seal"],
        "prosp_d20_on_or_before_s_seal_count": manifest["prosp_batch1"][
            "d20_on_or_before_s_seal_count"
        ],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _read_identity_rows(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            {"ticker": str(row.get("ticker") or "")}
            for row in csv.DictReader(handle)
        ]


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
