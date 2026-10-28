"""Strict loader for point-in-time experiment input datasets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from .canonical import sha256_json


class DatasetError(ValueError):
    """The experiment input is incomplete or temporally unsafe."""


_TEMPORAL_KEY_PARTS = (
    "published", "retrieved", "as_of", "available", "observed", "timestamp",
    "effective", "event_date", "captured", "snapshot", "cutoff",
)


def _utc(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise DatasetError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _temporal_value(value: Any, field: str) -> datetime:
    if isinstance(value, str) and len(value) == 10 and value[4] == "-" and value[7] == "-":
        try:
            # Date-only provenance is accepted at end-of-day, conservatively.
            return datetime.fromisoformat(value).replace(
                hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
            )
        except ValueError as exc:
            raise DatasetError(f"{field} is not a valid date") from exc
    return _utc(str(value), field)


def _validate_nested_temporal(value: Any, cutoff: datetime, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if (item is not None and any(part in key_text for part in _TEMPORAL_KEY_PARTS)
                    and "precision" not in key_text and not key_text.endswith("_id")):
                if isinstance(item, (str, int, float)):
                    try:
                        if (isinstance(item, str) and len(item) == 10 and "as_of" in key_text
                                and datetime.fromisoformat(item).date() <= cutoff.date()):
                            pass
                        elif _temporal_value(item, f"{path}.{key}") > cutoff:
                            raise DatasetError(f"future temporal field detected: {path}.{key}")
                    except DatasetError:
                        raise
                    except (TypeError, ValueError):
                        # Non-date labels such as `snapshot_id` are provenance
                        # identifiers, not timestamps.
                        pass
            _validate_nested_temporal(item, cutoff, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_nested_temporal(item, cutoff, f"{path}[{index}]")


@dataclass(frozen=True)
class TickerInput:
    record_id: str
    ticker: str
    week: str
    snapshot_id: str
    data_cutoff_utc: str
    snapshot: Mapping[str, Any]
    facts: Mapping[str, Any]
    sources: tuple[Mapping[str, Any], ...]
    analyst_prompt: str
    critic_prompt_template: str
    analyst_schema: Mapping[str, Any]
    critic_schema: Mapping[str, Any]
    metadata: Mapping[str, Any]
    # Kept as a compatibility field, but deliberately excluded from the
    # point-in-time input payload. Loaded datasets store outcomes separately.
    outcomes: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TickerInput":
        required = (
            "ticker", "week", "snapshot_id", "data_cutoff_utc", "snapshot",
            "facts", "sources", "analyst_prompt", "critic_prompt_template",
            "analyst_schema", "critic_schema",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise DatasetError(f"record missing required fields: {missing}")
        ticker = str(value["ticker"]).strip().upper()
        week = str(value["week"]).strip()
        if not ticker or not week:
            raise DatasetError("ticker and week must be non-empty")
        cutoff = _utc(str(value["data_cutoff_utc"]), "data_cutoff_utc")
        sources = value["sources"]
        if not isinstance(sources, list):
            raise DatasetError("sources must be a list")
        if not sources:
            raise DatasetError("sources must contain temporal provenance")
        normalized_sources: list[Mapping[str, Any]] = []
        for index, source in enumerate(sources):
            if not isinstance(source, Mapping):
                raise DatasetError(f"sources[{index}] must be an object")
            temporal_values = [
                ("published_at", source.get("published_at")),
                ("retrieved_at", source.get("retrieved_at")),
            ]
            if not any(value is not None for _, value in temporal_values):
                raise DatasetError(f"sources[{index}] has no temporal provenance")
            for field, source_value in temporal_values:
                if source_value is not None and _temporal_value(source_value, f"sources[{index}].{field}") > cutoff:
                    raise DatasetError(
                        f"future source detected for {ticker}: sources[{index}].{field}"
                    )
            normalized_sources.append(dict(source))
        if "outcomes" in value:
            raise DatasetError("outcomes must be stored separately at dataset level")
        for field in ("snapshot", "facts", "analyst_schema", "critic_schema"):
            if not isinstance(value[field], Mapping):
                raise DatasetError(f"{field} must be an object")
        _validate_nested_temporal(value["facts"], cutoff, f"{ticker}.facts")
        _validate_nested_temporal(value["snapshot"], cutoff, f"{ticker}.snapshot")
        snapshot = value["snapshot"]
        snapshot_source = snapshot.get("source") or snapshot.get("source_uri") or snapshot.get("provenance")
        snapshot_time = next((snapshot.get(key) for key in ("captured_at", "snapshot_at", "as_of_at", "retrieved_at", "timestamp", "date") if snapshot.get(key) is not None), None)
        if not snapshot_source or snapshot_time is None:
            raise DatasetError(f"snapshot for {ticker} lacks verifiable provenance and timestamp")
        analyst_prompt = value["analyst_prompt"]
        critic_template = value["critic_prompt_template"]
        if not isinstance(analyst_prompt, str) or not analyst_prompt:
            raise DatasetError("analyst_prompt must be non-empty text")
        if not isinstance(critic_template, str) or "{{ANALYST_DRAFT_JSON}}" not in critic_template:
            raise DatasetError("critic_prompt_template must contain {{ANALYST_DRAFT_JSON}}")
        record_id = str(value.get("record_id") or f"{ticker}:{week}:{value['snapshot_id']}")
        return cls(
            record_id=record_id,
            ticker=ticker,
            week=week,
            snapshot_id=str(value["snapshot_id"]),
            data_cutoff_utc=cutoff.isoformat().replace("+00:00", "Z"),
            snapshot=dict(value["snapshot"]),
            facts=dict(value["facts"]),
            sources=tuple(normalized_sources),
            analyst_prompt=analyst_prompt,
            critic_prompt_template=critic_template,
            analyst_schema=dict(value["analyst_schema"]),
            critic_schema=dict(value["critic_schema"]),
            metadata=dict(value.get("metadata") or {}),
            outcomes=dict(value.get("outcomes") or {}),
        )

    def input_payload(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "ticker": self.ticker,
            "week": self.week,
            "snapshot_id": self.snapshot_id,
            "data_cutoff_utc": self.data_cutoff_utc,
            "snapshot": self.snapshot,
            "facts": self.facts,
            "sources": list(self.sources),
            "analyst_prompt": self.analyst_prompt,
            "critic_prompt_template": self.critic_prompt_template,
            "analyst_schema": self.analyst_schema,
            "critic_schema": self.critic_schema,
            "metadata": self.metadata,
        }

    @property
    def input_hash(self) -> str:
        return sha256_json(self.input_payload())


@dataclass(frozen=True)
class Dataset:
    schema_name: str
    schema_version: str
    source_type: str
    source_reference: str
    analytical_commit: str | None
    records: tuple[TickerInput, ...]
    outcomes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def _read_source(path: Path) -> tuple[dict[str, Any], str]:
    if path.is_dir():
        for candidate in ("sol_effort_dataset.json", "records.json", "dataset.json"):
            possible = path / candidate
            if possible.is_file():
                path = possible
                break
        else:
            raise DatasetError(
                "dataset directory must contain sol_effort_dataset.json, records.json, or dataset.json"
            )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read dataset {path}") from exc
    if isinstance(value, list):
        value = {"records": value}
    if not isinstance(value, Mapping) or not isinstance(value.get("records"), list):
        raise DatasetError("dataset must be an object containing a records list")
    return dict(value), str(path)


def load_dataset(path: str | Path) -> Dataset:
    value, source_reference = _read_source(Path(path))
    records = tuple(TickerInput.from_mapping(item) for item in value["records"])
    if not records:
        raise DatasetError("dataset contains no records")
    record_ids = [item.record_id for item in records]
    if len(set(record_ids)) != len(record_ids):
        raise DatasetError("dataset contains duplicate record_id values")
    return Dataset(
        schema_name=str(value.get("schema_name", "trinity.sol-effort-v1.input-dataset")),
        schema_version=str(value.get("schema_version", "1")),
        source_type=str(value.get("source_type", "REAL_ARCHIVE")),
        source_reference=source_reference,
        analytical_commit=(
            None if value.get("analytical_commit") is None
            else str(value["analytical_commit"])
        ),
        records=records,
        outcomes={
            str(key): dict(value)
            for key, value in (value.get("outcomes") or {}).items()
            if isinstance(value, Mapping)
        },
    )


def select_records(dataset: Dataset, limit: int) -> tuple[TickerInput, ...]:
    """Select before any model call using immutable identity fields only."""

    if type(limit) is not int or limit < 1:
        raise DatasetError("limit must be a positive integer")
    ordered = sorted(dataset.records, key=lambda item: (item.input_hash, item.record_id))
    return tuple(ordered[:limit])

