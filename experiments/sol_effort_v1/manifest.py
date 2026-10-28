"""Deterministic experiment manifest construction."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any, Iterable

from .canonical import sha256_json
from .client import API_URL, MAX_OUTPUT_TOKENS, SERVICE_TIER, VALIDATOR_VERSION
from .config import ARMS, MODEL
from .dataset import Dataset, TickerInput


def _code_commit() -> str:
    explicit = os.getenv("TRINITY_EXPERIMENT_CODE_COMMIT")
    if explicit:
        return explicit
    try:
        root = Path(__file__).resolve().parents[2]
        git = "git"
        for candidate in (Path("C:/Program Files/Git/cmd/git.exe"), Path("C:/Program Files/Git/bin/git.exe")):
            if candidate.is_file():
                git = str(candidate)
                break
        return subprocess.run([git, "rev-parse", "HEAD"], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return "UNKNOWN"


def build_manifest(dataset: Dataset, records: Iterable[TickerInput], *, plan: Iterable[dict[str, Any]] = (),
                   replicate_id: str = "r1", max_output_tokens: int = MAX_OUTPUT_TOKENS,
                   execution_timestamp_utc: str | None = None) -> dict[str, Any]:
    selected = tuple(records)
    payload = {
        "experiment_id": "TRINITY_SOL_EFFORT_COMPARISON_V1",
        "experiment_version": "1",
        "model": MODEL,
        "api_url": API_URL,
        "service_tier": SERVICE_TIER,
        "max_output_tokens": max_output_tokens,
        "validator_version": VALIDATOR_VERSION,
        "code_commit": _code_commit(),
        "execution_timestamp_utc": execution_timestamp_utc,
        "replicate_id": replicate_id,
        "reasoning_efforts": ["medium", "high"],
        "dataset_schema_name": dataset.schema_name,
        "dataset_schema_version": dataset.schema_version,
        "source_type": dataset.source_type,
        "analytical_commit": dataset.analytical_commit,
        "arms": [
            {
                "arm_id": arm.arm_id,
                "analyst_effort": arm.analyst_effort,
                "critic_effort": arm.critic_effort,
            }
            for arm in ARMS
        ],
        "selection_rule": "sha256(input_hash), record_id; selection precedes calls",
        "records": [
            {
                "record_id": record.record_id,
                "ticker": record.ticker,
                "week": record.week,
                "snapshot_id": record.snapshot_id,
                "data_cutoff_utc": record.data_cutoff_utc,
                "input_hash": record.input_hash,
            }
            for record in selected
        ],
        "dataset_identity": sha256_json({
            "schema_name": dataset.schema_name,
            "schema_version": dataset.schema_version,
            "source_type": dataset.source_type,
            "analytical_commit": dataset.analytical_commit,
            "records": [record.input_hash for record in selected],
        }),
        "planned_calls": [
            {
                "call_id": item["call_id"],
                "record_id": item["record_id"],
                "role": item["role"],
                "effort": item["effort"],
                "analyst_effort": item.get("analyst_effort"),
                "sequence_index": item["sequence_index"],
            }
            for item in plan
        ],
    }
    manifest = dict(payload)
    manifest["manifest_hash"] = sha256_json(payload)
    return manifest

