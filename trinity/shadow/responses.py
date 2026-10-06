"""Checkpoint-first OpenAI Responses API transport for Shadow Production."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping

import requests

from .atomic import atomic_bytes, atomic_json


API_URL = "https://api.openai.com/v1/responses"
PROVIDER = "OPENAI_RESPONSES_API"
TRANSPORT = "RESPONSES_API_HTTPS"
PRICE_VERSION = "shadow-v1-2026-10-06"
# USD per million tokens: ordinary input, cached input, cache write, output.
MODEL_PRICES = {
    "gpt-5.6-luna": (Decimal("0.20"), Decimal("0.02"), Decimal("0.25"), Decimal("1.20")),
    "gpt-5.6-sol": (Decimal("4.00"), Decimal("0.40"), Decimal("5.00"), Decimal("20.00")),
}


class ResponsesTransportError(RuntimeError):
    pass


class CostGuardStop(RuntimeError):
    pass


@dataclass(frozen=True)
class ResponseCheckpoint:
    parsed: dict[str, Any]
    response_id: str
    raw_path: Path
    usage: dict[str, int]
    cost_usd: Decimal
    duration_seconds: float
    raw_sha256: str


class PersistentCostLedger:
    def __init__(self, path: str | Path, hard_limit_usd: Decimal = Decimal("5.00")) -> None:
        self.path = Path(path)
        self.hard_limit_usd = hard_limit_usd

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_name": "trinity.shadow-api-usage", "schema_version": "1",
                "pricing_version": PRICE_VERSION, "hard_limit_usd": str(self.hard_limit_usd),
                "total_cost_usd": "0", "calls": [],
            }
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("schema_name") != "trinity.shadow-api-usage":
            raise CostGuardStop("invalid persistent API usage state")
        return value

    def assert_call_allowed(self) -> None:
        measured = Decimal(str(self.read()["total_cost_usd"]))
        if measured >= self.hard_limit_usd:
            raise CostGuardStop(
                f"measured API cost {measured} reached hard guard {self.hard_limit_usd}"
            )

    def append(self, record: Mapping[str, Any]) -> None:
        value = self.read()
        calls = [*value["calls"], dict(record)]
        total = sum((Decimal(str(item["calculated_cost_usd"])) for item in calls), Decimal("0"))
        value.update({"calls": calls, "total_cost_usd": str(total)})
        atomic_json(self.path, value)


class ResponsesAPI:
    """A dependency-injectable transport; no credential is ever serialized."""

    def __init__(
        self, *, run_root: str | Path, cost_ledger: PersistentCostLedger,
        api_key: str | None = None, session: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.run_root = Path(run_root)
        self.cost_ledger = cost_ledger
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ResponsesTransportError("OPENAI_API_KEY is not configured")
        self.session = session or requests.Session()
        self.clock = clock

    def invoke(
        self, *, model: str, role: str, ticker: str, prompt: str,
        schema: Mapping[str, Any], call_ordinal: int,
    ) -> ResponseCheckpoint:
        if model not in MODEL_PRICES:
            raise ResponsesTransportError(f"unsupported frozen model: {model}")
        self.cost_ledger.assert_call_allowed()
        body = {
            "model": model, "service_tier": "default", "store": False,
            "input": prompt,
            "text": {"format": {"type": "json_schema", "name": f"trinity_{role.lower()}",
                                 "strict": True, "schema": dict(schema)}},
        }
        started = time.perf_counter()
        response = None
        for attempt in range(2):
            try:
                response = self.session.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=body, timeout=300,
                )
            except requests.RequestException as exc:
                if attempt == 0:
                    continue
                raise ResponsesTransportError(f"Responses API transport failure: {type(exc).__name__}") from exc
            if int(response.status_code) >= 500 and attempt == 0:
                continue
            if int(response.status_code) >= 400:
                raise ResponsesTransportError(f"Responses API HTTP {response.status_code}")
            break
        assert response is not None
        duration = time.perf_counter() - started
        raw = bytes(response.content)
        stamp = f"{call_ordinal:04d}_{ticker}_{role.lower()}"
        raw_path = self.run_root / "model_responses" / f"{stamp}.json"
        # Durability boundary: provider success is on disk before JSON parsing.
        atomic_bytes(raw_path, raw)
        digest = hashlib.sha256(raw).hexdigest()
        try:
            envelope = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ResponsesTransportError("checkpointed Responses API body is not JSON") from exc
        usage = _usage(envelope)
        cost = _cost(model, usage)
        record = {
            "provider": PROVIDER, "model": model, "role": role, "ticker": ticker,
            "response_id": str(envelope.get("id") or ""),
            "service_tier": str(envelope.get("service_tier") or "default"),
            "requested_service_tier": "default", "store": False,
            **usage, "duration_seconds": duration, "calculated_cost_usd": str(cost),
            "raw_sha256": digest, "raw_path": raw_path.relative_to(self.run_root).as_posix(),
            "recorded_at": self.clock().astimezone(timezone.utc).isoformat(),
        }
        self.cost_ledger.append(record)
        parsed = _extract_output_json(envelope)
        return ResponseCheckpoint(
            parsed=parsed, response_id=record["response_id"], raw_path=raw_path,
            usage=usage, cost_usd=cost, duration_seconds=duration, raw_sha256=digest,
        )


def _usage(value: Mapping[str, Any]) -> dict[str, int]:
    source = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    input_details = source.get("input_tokens_details") if isinstance(source.get("input_tokens_details"), dict) else {}
    output_details = source.get("output_tokens_details") if isinstance(source.get("output_tokens_details"), dict) else {}
    return {
        "input_tokens": int(source.get("input_tokens") or 0),
        "cached_input_tokens": int(input_details.get("cached_tokens") or 0),
        "cache_write_tokens": int(input_details.get("cache_write_tokens") or 0),
        "output_tokens": int(source.get("output_tokens") or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
        "total_tokens": int(source.get("total_tokens") or 0),
    }


def _cost(model: str, usage: Mapping[str, int]) -> Decimal:
    input_rate, cached_rate, cache_write_rate, output_rate = MODEL_PRICES[model]
    cached = Decimal(usage["cached_input_tokens"])
    cache_write = Decimal(usage["cache_write_tokens"])
    ordinary = max(Decimal(usage["input_tokens"]) - cached - cache_write, Decimal("0"))
    return (
        ordinary * input_rate + cached * cached_rate
        + cache_write * cache_write_rate + Decimal(usage["output_tokens"]) * output_rate
    ) / Decimal(1_000_000)


def _extract_output_json(value: Mapping[str, Any]) -> dict[str, Any]:
    for item in value.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                try:
                    parsed = json.loads(str(content.get("text") or ""))
                except json.JSONDecodeError as exc:
                    raise ResponsesTransportError("checkpointed output_text is invalid JSON") from exc
                if not isinstance(parsed, dict):
                    raise ResponsesTransportError("structured output must be a JSON object")
                return parsed
    raise ResponsesTransportError("Responses API body lacks output_text")
