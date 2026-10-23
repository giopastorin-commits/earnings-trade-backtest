"""Fail-fast infrastructure readiness gate for supervised weekly smoke runs."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from trinity.twelvedata_prices import (
    DAILY_CREDIT_RESERVE,
    TwelveDataClient,
    required_daily_credits,
)
from trinity.usa_issuer_registry import REGISTRY

from .r2 import client_from_environment, restore_baseline


REQUIRED_CONFIGURATION = (
    "TRINITY_R2_ENDPOINT",
    "TRINITY_R2_ACCESS_KEY_ID",
    "TRINITY_R2_SECRET_ACCESS_KEY",
    "TRINITY_BASELINE_PREFIX",
    "TWELVEDATA_API_KEY",
    "OPENAI_API_KEY",
    "EODHD_API_KEY",
)
DEFAULT_R2_BUCKET = "trinity-raw"
PROBE_BYTES = b"TRINITY_P1_LIVE_READINESS_PROBE_V1\n"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
SEC_COMPACT_ABSENT_OPTIONAL = "ABSENT_OPTIONAL"
SEC_COMPACT_EMPTY_OPTIONAL = "EMPTY_OPTIONAL"
SEC_COMPACT_PRESENT_VALID = "PRESENT_VALID"


class LivePreflightError(RuntimeError):
    """Required infrastructure is not ready for a Full Shadow smoke run."""


@dataclass(frozen=True)
class LivePreflightResult:
    config_ready: bool
    r2_ready: bool
    baseline_ready: bool
    twelvedata_quota_ready: bool
    required_daily_credits: int
    available_daily_credits: int
    analytical_sha: str
    sec_compact_status: str

    def outputs(self) -> dict[str, str]:
        return {
            "config_ready": str(self.config_ready).lower(),
            "r2_ready": str(self.r2_ready).lower(),
            "baseline_ready": str(self.baseline_ready).lower(),
            "twelvedata_quota_ready": str(self.twelvedata_quota_ready).lower(),
            "required_daily_credits": str(self.required_daily_credits),
            "available_daily_credits": str(self.available_daily_credits),
            "analytical_sha": self.analytical_sha,
            "sec_compact_status": self.sec_compact_status,
        }


def _required_environment(environ: Mapping[str, str]) -> None:
    missing = [name for name in REQUIRED_CONFIGURATION if not environ.get(name, "").strip()]
    if missing:
        raise LivePreflightError("missing required configuration: " + ", ".join(missing))


def _probe_key(environ: Mapping[str, str]) -> str:
    run_id = environ.get("GITHUB_RUN_ID", "").strip()
    attempt = environ.get("GITHUB_RUN_ATTEMPT", "").strip()
    if not run_id.isdigit() or not attempt.isdigit() or int(attempt) < 1:
        raise LivePreflightError("invalid GitHub run identity for R2 preflight probe")
    return f"preflight/github_run_id={run_id}/attempt={attempt}/probe.bin"


def _verify_r2_probe(client: Any, bucket: str, key: str) -> None:
    digest = hashlib.sha256(PROBE_BYTES).hexdigest()
    wrote = False
    primary_error: Exception | None = None
    try:
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        client.put_object(
            Bucket=bucket, Key=key, Body=PROBE_BYTES,
            Metadata={"sha256": digest}, ContentType="application/octet-stream",
        )
        wrote = True
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head.get("ContentLength", -1)) != len(PROBE_BYTES) \
                or head.get("Metadata", {}).get("sha256") != digest:
            raise LivePreflightError("R2 preflight probe HEAD verification failed")
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        if body != PROBE_BYTES or hashlib.sha256(body).hexdigest() != digest:
            raise LivePreflightError("R2 preflight probe read verification failed")
    except Exception as exc:
        primary_error = exc
    finally:
        if wrote:
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except Exception as cleanup_exc:
                detail = f"R2 preflight probe cleanup failed: {type(cleanup_exc).__name__}"
                if primary_error is not None:
                    detail += f" (after {type(primary_error).__name__})"
                raise LivePreflightError(detail) from cleanup_exc
    if primary_error is not None:
        if isinstance(primary_error, LivePreflightError):
            raise primary_error
        raise LivePreflightError(
            f"R2 read/write preflight failed: {type(primary_error).__name__}"
        ) from primary_error


def _verify_sec_compact(root: Path) -> str:
    """Validate an optional cached SEC tree without making it a dependency."""

    sec_root = root / "sec_compact"
    if not sec_root.exists():
        return SEC_COMPACT_ABSENT_OPTIONAL
    if not sec_root.is_dir():
        raise LivePreflightError("present sec_compact path is not a directory")
    manifests = sorted(sec_root.glob("*/sec/manifest.json"))
    if not manifests:
        if any(path.is_file() for path in sec_root.rglob("*")):
            raise LivePreflightError("present sec_compact contains files but no ticker manifests")
        return SEC_COMPACT_EMPTY_OPTIONAL
    for path in manifests:
        ticker = path.parents[1].name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LivePreflightError(f"invalid sec_compact evidence for {ticker}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("companies"), dict):
            raise LivePreflightError(f"invalid sec_compact companies for {ticker}")
        company = value["companies"].get(ticker)
        if not isinstance(company, dict):
            raise LivePreflightError(f"invalid sec_compact company for {ticker}")
        if "company" in company and (
            not isinstance(company["company"], str) or not company["company"].strip()
        ):
            raise LivePreflightError(f"invalid sec_compact company name for {ticker}")
        if "documents" in company and not isinstance(company["documents"], list):
            raise LivePreflightError(f"invalid sec_compact documents for {ticker}")
    return SEC_COMPACT_PRESENT_VALID


def run_live_preflight(
    output_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    r2_factory: Callable[..., tuple[Any, str]] = client_from_environment,
    baseline_restorer: Callable[..., dict[str, Any]] = restore_baseline,
    twelvedata_factory: Callable[..., Any] = TwelveDataClient,
) -> LivePreflightResult:
    """Verify configuration, R2/baseline integrity, and full-run TD quota."""

    env = os.environ if environ is None else environ
    _required_environment(env)
    analytical_sha = env.get("TRINITY_ANALYTICAL_REF", "").strip()
    if not _SHA40.fullmatch(analytical_sha):
        raise LivePreflightError("TRINITY_ANALYTICAL_REF must be an immutable Git SHA")
    prefix = env["TRINITY_BASELINE_PREFIX"].strip()
    if "version=" not in prefix or "latest" in prefix.lower():
        raise LivePreflightError("TRINITY_BASELINE_PREFIX must be immutable and contain version=")
    root = Path(output_root)
    if root.exists() and any(root.iterdir()):
        raise LivePreflightError("preflight output root must be new or empty")
    root.mkdir(parents=True, exist_ok=True)

    client, bucket = r2_factory()
    if bucket != env.get("TRINITY_R2_BUCKET", DEFAULT_R2_BUCKET):
        raise LivePreflightError("R2 client bucket does not match configured bucket")
    baseline_root = root / "baseline"
    baseline_restorer(client, bucket, prefix, baseline_root)
    sec_compact_status = _verify_sec_compact(baseline_root)
    _verify_r2_probe(client, bucket, _probe_key(env))

    ticker_count = len(REGISTRY.supported)
    required = required_daily_credits(ticker_count)
    usage = twelvedata_factory(api_key=env["TWELVEDATA_API_KEY"]).fetch_api_usage()
    available = int(usage.daily_remaining)
    if available < required:
        raise LivePreflightError(
            f"insufficient Twelve Data daily credits: available={available} required={required}"
        )
    return LivePreflightResult(
        config_ready=True,
        r2_ready=True,
        baseline_ready=True,
        twelvedata_quota_ready=True,
        required_daily_credits=required,
        available_daily_credits=available,
        analytical_sha=analytical_sha,
        sec_compact_status=sec_compact_status,
    )


def _write_outputs(values: Mapping[str, str], path: str | None) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args(argv)
    for name in REQUIRED_CONFIGURATION:
        print(f"{name}=CONFIGURED" if os.getenv(name, "").strip() else f"{name}=MISSING")
    result = run_live_preflight(args.output_root)
    print(f"SEC_COMPACT={result.sec_compact_status}")
    print(
        "TWELVEDATA_QUOTA="
        f"available={result.available_daily_credits} "
        f"required={result.required_daily_credits} reserve={DAILY_CREDIT_RESERVE} READY"
    )
    _write_outputs(result.outputs(), os.getenv("GITHUB_OUTPUT"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
