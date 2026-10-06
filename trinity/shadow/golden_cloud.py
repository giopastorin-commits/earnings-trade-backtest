"""Network-isolated GitHub entry point for the Week 0 golden replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from .atomic import atomic_json
from .golden_baseline import (
    DECISION_CUTOFF,
    EXPECTED_FUNNEL_SHA256,
    EXPECTED_INPUT_MANIFEST_SHA256,
    EXPECTED_SETUPS_SHA256,
)
from .golden_replay import GoldenReplayError, run_golden_replay
from .r2 import R2Error, client_from_environment, restore_baseline


def _git_head() -> str:
    if os.getenv("GITHUB_SHA"):
        return str(os.environ["GITHUB_SHA"])
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()


def restore_command(args: argparse.Namespace) -> int:
    diagnostics = Path(args.diagnostics_root)
    diagnostics.mkdir(parents=True, exist_ok=True)
    try:
        client, bucket = client_from_environment(read_only=True)
        manifest = restore_baseline(
            client, bucket, args.prefix, args.baseline_root,
            expected_manifest_sha256=args.manifest_sha256,
        )
        if manifest.get("golden_input_manifest_sha256") != EXPECTED_INPUT_MANIFEST_SHA256:
            raise R2Error("baseline identifies a different golden input manifest")
        if manifest.get("expected_funnel_sha256") != EXPECTED_FUNNEL_SHA256:
            raise R2Error("baseline identifies a different expected funnel")
        if manifest.get("expected_setups_sha256") != EXPECTED_SETUPS_SHA256:
            raise R2Error("baseline identifies different expected setups")
        result = {
            "status": "BASELINE_RESTORED_AND_VERIFIED", "bucket": bucket,
            "prefix": args.prefix, "manifest_sha256": manifest["restored_manifest_sha256"],
            "file_count": manifest["file_count"], "total_bytes": manifest["total_bytes"],
        }
        atomic_json(diagnostics / "baseline_restore.json", result)
        return 0
    except Exception as exc:
        atomic_json(diagnostics / "restore_failure.json", {
            "status": "FAILED_DATA", "classification": "INPUT_MISMATCH",
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise


def replay_command(args: argparse.Namespace) -> int:
    output = Path(args.output_root)
    output.mkdir(parents=True, exist_ok=True)
    manifest_raw = (Path(args.baseline_root) / "manifest.json").read_bytes()
    provenance = {
        "git_commit_sha": _git_head(),
        "github_run_id": os.getenv("GITHUB_RUN_ID", "local"),
        "github_run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", "1"),
        "attempt_identity": (
            f"{os.getenv('GITHUB_RUN_ID', 'local')}-attempt-"
            f"{os.getenv('GITHUB_RUN_ATTEMPT', '1')}"
        ),
        "golden_cutoff": DECISION_CUTOFF,
        "baseline_version": args.baseline_version,
        "baseline_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "python_version": platform.python_version(),
        "os_runtime": platform.platform(),
        "python_implementation": platform.python_implementation(),
    }
    atomic_json(output / "cloud_replay_provenance.json", provenance)
    try:
        comparison = run_golden_replay(
            decision_cutoff=DECISION_CUTOFF,
            baseline_root=args.baseline_root,
            universe_path=Path(args.baseline_root) / "canonical_ticker_mapping.csv",
            reference_root=args.reference_root,
            output_root=output,
        )
    except Exception as exc:
        if not (output / "comparison.json").is_file():
            atomic_json(output / "failure.json", {
                "status": "GOLDEN_MISMATCH", "mismatch_classification": "OTHER",
                "error": f"{type(exc).__name__}: {exc}",
            })
        raise
    atomic_json(output / "cloud_replay_result.json", {
        "status": comparison["status"], "provenance": provenance,
        "funnel": comparison["artifacts"]["funnel"],
        "setups": comparison["artifacts"]["setups"],
        "setup_records": comparison["setup_record_hashes"],
    })
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TRINITY offline golden cloud replay")
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--prefix", required=True)
    restore.add_argument("--manifest-sha256", required=True)
    restore.add_argument("--baseline-root", type=Path, required=True)
    restore.add_argument("--diagnostics-root", type=Path, required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--baseline-root", type=Path, required=True)
    replay.add_argument("--reference-root", type=Path, required=True)
    replay.add_argument("--output-root", type=Path, required=True)
    replay.add_argument("--baseline-version", required=True)
    args = parser.parse_args(argv)
    try:
        return restore_command(args) if args.command == "restore" else replay_command(args)
    except (GoldenReplayError, R2Error, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
