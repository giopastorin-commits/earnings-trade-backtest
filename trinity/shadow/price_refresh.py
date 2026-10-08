"""Manual acquisition and immutable R2 archival for Twelve Data price snapshots."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
from typing import Sequence

from trinity.shadow.atomic import atomic_json
from trinity.shadow.r2 import R2Error, archive_price_snapshot, client_from_environment
from trinity.twelvedata_prices import PriceSnapshotError, acquire_snapshot, verify_snapshot


def acquire_command(target_session: date, output_root: Path, run_id: str) -> int:
    manifest = acquire_snapshot(
        target_session=target_session, output_root=output_root, run_id=run_id,
    )
    _write_summary(manifest, output_root)
    return 0 if manifest["coverage_gate"]["ready"] else 2


def archive_command(output_root: Path, run_id: str) -> int:
    manifest = verify_snapshot(output_root, require_ready=True, require_raw=True)
    client, bucket = client_from_environment()
    result = archive_price_snapshot(client, bucket, output_root, run_id=run_id)
    atomic_json(output_root / "r2_archive_result.json", result)
    _write_summary({**manifest, "r2_archive": result}, output_root)
    return 0


def _write_summary(manifest: dict, output_root: Path) -> None:
    gate = manifest["coverage_gate"]
    counts = manifest["classification_counts"]
    lines = [
        "## TRINITY Twelve Data price snapshot",
        "",
        f"- Snapshot ID: `{manifest['snapshot_id']}`",
        f"- Session: `{manifest['target_market_session']}`",
        f"- SHA-256: `{manifest['snapshot_sha256']}`",
        f"- Coverage ready: `{gate['ready']}`",
        f"- ACTIVE_COMPLETE: `{counts['ACTIVE_COMPLETE']}`",
        f"- Explicit exceptions: `{counts['CORPORATE_ACTION_NO_LONGER_TRADING']}`",
        f"- Blocking tickers: `{', '.join(gate['blocking_tickers']) or '-'}`",
    ]
    if isinstance(manifest.get("r2_archive"), dict):
        lines.append(f"- R2 prefix: `{manifest['r2_archive']['prefix']}`")
    summary = "\n".join(lines) + "\n"
    (output_root / "summary.md").write_text(summary, encoding="utf-8")
    github_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_summary:
        with Path(github_summary).open("a", encoding="utf-8") as stream:
            stream.write(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    acquire = subparsers.add_parser("acquire")
    acquire.add_argument("--target-session", type=date.fromisoformat, required=True)
    acquire.add_argument("--output-root", type=Path, required=True)
    acquire.add_argument("--run-id", required=True)
    archive = subparsers.add_parser("archive")
    archive.add_argument("--output-root", type=Path, required=True)
    archive.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "acquire":
            return acquire_command(args.target_session, args.output_root, args.run_id)
        return archive_command(args.output_root, args.run_id)
    except (OSError, ValueError, PriceSnapshotError, R2Error) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
