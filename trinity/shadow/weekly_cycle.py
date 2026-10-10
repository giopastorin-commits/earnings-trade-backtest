"""Durable weekly-cycle idempotency and same-run handoff validation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Sequence

from .r2 import R2Error, client_from_environment
from .weekly_schedule import CONTRACT_VERSION


class WeeklyCycleError(RuntimeError):
    pass


SHA256 = re.compile(r"^[0-9a-f]{64}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def completion_marker_key(intended_monday: str, target_session: str) -> str:
    if not DATE.fullmatch(intended_monday) or not DATE.fullmatch(target_session):
        raise WeeklyCycleError("invalid weekly cycle date identity")
    return (
        "orchestration/weekly/"
        f"method={CONTRACT_VERSION}/intended_monday={intended_monday}/"
        f"target_session={target_session}/official-complete.json"
    )


def marker_exists(client: Any, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except Exception as exc:
        response = getattr(exc, "response", {})
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = str(response.get("Error", {}).get("Code", ""))
        if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def check_cycle(
    client: Any, bucket: str, *, mode: str, intended_monday: str, target_session: str,
) -> dict[str, Any]:
    mode = mode.upper()
    if mode not in {"VALIDATION", "OFFICIAL"}:
        raise WeeklyCycleError("invalid orchestrator mode")
    key = completion_marker_key(intended_monday, target_session)
    duplicate = mode == "OFFICIAL" and marker_exists(client, bucket, key)
    return {
        "mode": mode, "eligible": not duplicate, "duplicate_official": duplicate,
        "completion_marker_key": key, "method_version": CONTRACT_VERSION,
    }


def validate_snapshot_handoff(
    *, target_session: str, snapshot_id: str, snapshot_sha256: str, prefix: str,
    github_run_id: str, github_run_attempt: str,
) -> dict[str, str]:
    run_identity = f"{github_run_id}-A{github_run_attempt}"
    expected_id = f"TWELVEDATA_{target_session}_RUN_{run_identity}"
    expected_prefix = f"prices/twelvedata/session={target_session}/run_id={run_identity}/"
    if snapshot_id != expected_id:
        raise WeeklyCycleError("same-run price snapshot ID mismatch")
    if prefix != expected_prefix or "latest" in prefix.lower():
        raise WeeklyCycleError("same-run immutable price snapshot prefix mismatch")
    if not SHA256.fullmatch(snapshot_sha256):
        raise WeeklyCycleError("invalid price snapshot SHA-256")
    return {
        "target_market_session": target_session, "price_snapshot_id": snapshot_id,
        "price_snapshot_sha256": snapshot_sha256, "price_snapshot_r2_prefix": prefix,
    }


def write_official_completion_marker(
    client: Any, bucket: str, *, mode: str, intended_monday: str,
    target_session: str, analytical_commit: str, workflow_commit: str,
    snapshot_id: str, snapshot_sha256: str, snapshot_prefix: str,
    analytical_run_id: str, analytical_archive: dict[str, Any],
) -> dict[str, Any]:
    if mode != "OFFICIAL":
        return {"created": False, "reason": "VALIDATION_MODE"}
    key = completion_marker_key(intended_monday, target_session)
    if marker_exists(client, bucket, key):
        raise WeeklyCycleError("OFFICIAL weekly cycle is already complete")
    payload = {
        "schema": "trinity.weekly-cycle-completion.v1", "mode": mode,
        "method_version": CONTRACT_VERSION,
        "intended_monday_europe_rome": intended_monday,
        "target_market_session": target_session,
        "analytical_commit": analytical_commit,
        "workflow_registration_commit": workflow_commit,
        "price_snapshot_id": snapshot_id, "price_snapshot_sha256": snapshot_sha256,
        "price_snapshot_r2_prefix": snapshot_prefix,
        "analytical_run_id": analytical_run_id, "analytical_archive": analytical_archive,
        "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    body = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(body).hexdigest()
    try:
        client.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType="application/json",
            Metadata={"sha256": digest}, IfNoneMatch="*",
        )
    except Exception as exc:
        status = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status in {409, 412}:
            raise WeeklyCycleError("OFFICIAL completion marker collision") from exc
        raise
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head["ContentLength"]) != len(body) or head.get("Metadata", {}).get("sha256") != digest:
        raise WeeklyCycleError("OFFICIAL completion marker verification failed")
    return {"created": True, "key": key, "sha256": digest, "payload": payload}


def _output(values: dict[str, Any]) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            for key, value in values.items():
                if isinstance(value, (str, int, float, bool)):
                    stream.write(f"{key}={str(value).lower() if isinstance(value, bool) else value}\n")
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--mode", required=True)
    check.add_argument("--intended-monday", required=True)
    check.add_argument("--target-session", required=True)
    handoff = sub.add_parser("validate-handoff")
    for name in ("target-session", "snapshot-id", "snapshot-sha256", "prefix", "github-run-id", "github-run-attempt"):
        handoff.add_argument("--" + name, required=True)
    complete = sub.add_parser("complete")
    for name in ("mode", "intended-monday", "target-session", "analytical-commit", "workflow-commit",
                 "snapshot-id", "snapshot-sha256", "snapshot-prefix", "run-root"):
        complete.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            client, bucket = client_from_environment()
            result = check_cycle(client, bucket, mode=args.mode, intended_monday=args.intended_monday,
                                 target_session=args.target_session)
        elif args.command == "validate-handoff":
            result = validate_snapshot_handoff(
                target_session=args.target_session, snapshot_id=args.snapshot_id,
                snapshot_sha256=args.snapshot_sha256, prefix=args.prefix,
                github_run_id=args.github_run_id, github_run_attempt=args.github_run_attempt,
            )
        else:
            root = Path(args.run_root)
            pointer = json.loads((root / "current_run.json").read_text(encoding="utf-8"))
            run_path = Path(pointer["path"])
            state = json.loads((run_path / "run_state.json").read_text(encoding="utf-8"))
            archive = json.loads((run_path / "r2_archive_result.json").read_text(encoding="utf-8"))
            if state.get("state") != "COMPLETE":
                raise WeeklyCycleError("analytical run is not COMPLETE")
            client, bucket = client_from_environment()
            result = write_official_completion_marker(
                client, bucket, mode=args.mode, intended_monday=args.intended_monday,
                target_session=args.target_session, analytical_commit=args.analytical_commit,
                workflow_commit=args.workflow_commit, snapshot_id=args.snapshot_id,
                snapshot_sha256=args.snapshot_sha256, snapshot_prefix=args.snapshot_prefix,
                analytical_run_id=pointer["run_id"], analytical_archive=archive,
            )
        _output(result)
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, R2Error, WeeklyCycleError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
