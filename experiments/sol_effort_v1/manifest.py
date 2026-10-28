"""Deterministic experiment manifest construction."""

from __future__ import annotations

from typing import Any, Iterable

from .canonical import sha256_json
from .config import ARMS, MODEL
from .dataset import Dataset, TickerInput


def build_manifest(dataset: Dataset, records: Iterable[TickerInput]) -> dict[str, Any]:
    selected = tuple(records)
    payload = {
        "experiment_id": "TRINITY_SOL_EFFORT_COMPARISON_V1",
        "experiment_version": "1",
        "model": MODEL,
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
    }
    manifest = dict(payload)
    manifest["manifest_hash"] = sha256_json(payload)
    return manifest

