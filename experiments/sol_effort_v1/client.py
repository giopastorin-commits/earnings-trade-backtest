"""Offline mock and explicitly gated Responses API client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from decimal import ROUND_CEILING
import json
import os
from time import perf_counter
from typing import Any, Callable, Mapping

from .canonical import canonical_bytes, canonical_text, sha256_bytes, sha256_json
from .config import (
    API_URL,
    AUTHORIZATION_SENTINEL,
    MODEL,
    TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL,
    Effort,
)
from .schema import validate_object


INPUT_RATE = Decimal("4.00")
CACHED_INPUT_RATE = Decimal("0.40")
CACHE_WRITE_RATE = Decimal("5.00")
OUTPUT_RATE = Decimal("20.00")
MAX_OUTPUT_TOKENS = 4096
SERVICE_TIER = "default"
VALIDATOR_VERSION = "schema-v1"
PRICING_VERSION = "openai-gpt-5.6-sol-standard-2026-10-28"
PRICING_RATES_PER_MILLION = {
    "input_usd": str(INPUT_RATE),
    "cached_input_usd": str(CACHED_INPUT_RATE),
    "cache_write_usd": str(CACHE_WRITE_RATE),
    "output_usd": str(OUTPUT_RATE),
}
BUDGET_ESTIMATOR_VERSION = "provider-input-token-preflight-v1"
INPUT_TOKEN_SAFETY_MULTIPLIER = Decimal("1.25")
REQUEST_OVERHEAD_TOKENS = 256
RESERVATION_SAFETY_MULTIPLIER = Decimal("1.10")
SHORT_CONTEXT_INPUT_LIMIT = 272_000
PRICING_VALID_THROUGH = date(2026, 11, 21)
INPUT_TOKEN_URL = "https://api.openai.com/v1/responses/input_tokens"


def build_request_body(*, role: str, effort: Effort, prompt: str,
                       schema: Mapping[str, Any], max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict[str, Any]:
    """Build the exact Responses API request body used for hashing and HTTP."""
    return {
        "model": MODEL,
        "reasoning": {"effort": effort},
        "service_tier": SERVICE_TIER,
        "store": False,
        "input": prompt,
        "max_output_tokens": max_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": f"trinity_sol_effort_{role.lower()}",
                "strict": True,
                "schema": dict(schema),
            }
        },
    }


@dataclass(frozen=True)
class ProviderResponse:
    response_id: str
    output_text: str
    parsed: dict[str, Any]
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    total_tokens: int
    duration_seconds: float
    cost_usd: str
    raw_sha256: str
    request_hash: str
    visible_output_tokens: int = 0


class ProviderCallError(RuntimeError):
    """A provider response was received but could not become a valid result."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        status: str = "FAILED",
        raw_sha256: str | None = None,
        accounting: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.call_status = status
        self.raw_sha256 = raw_sha256
        self.accounting = dict(accounting or {})


def calculate_cost(
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> Decimal:
    ordinary = max(input_tokens - cached_input_tokens - cache_write_tokens, 0)
    return (
        Decimal(ordinary) * INPUT_RATE
        + Decimal(cached_input_tokens) * CACHED_INPUT_RATE
        + Decimal(cache_write_tokens) * CACHE_WRITE_RATE
        + Decimal(output_tokens) * OUTPUT_RATE
    ) / Decimal(1_000_000)


def prudent_input_token_bound(request_body: Mapping[str, Any]) -> int:
    """Return an offline-only estimate; never authorize a LIVE request.

    This remains available for DRY RUN reporting. LIVE reservations require
    the provider input-token preflight and never use this estimate.
    """
    byte_length = len(canonical_bytes(request_body))
    if byte_length <= 0:
        raise LiveShadowDisabled("cannot establish a positive input budget bound")
    with_margin = (Decimal(byte_length) * INPUT_TOKEN_SAFETY_MULTIPLIER).to_integral_value(
        rounding=ROUND_CEILING
    )
    return int(with_margin) + REQUEST_OVERHEAD_TOKENS


def calculate_prudent_reservation(input_tokens: int, max_output_tokens: int) -> Decimal:
    if input_tokens < 1:
        raise LiveShadowDisabled("provider input-token count must be positive")
    if input_tokens > SHORT_CONTEXT_INPUT_LIMIT:
        raise LiveShadowDisabled(
            f"LIVE SHADOW rejects inputs above {SHORT_CONTEXT_INPUT_LIMIT} tokens"
        )
    if date.today() > PRICING_VALID_THROUGH:
        raise LiveShadowDisabled("configured model pricing requires revalidation")
    # Cache writes are the highest configured input-side rate. Reserving all
    # counted input at that rate is conservative even when caching is absent.
    prudent_input_rate = max(INPUT_RATE, CACHED_INPUT_RATE, CACHE_WRITE_RATE)
    base = (
        Decimal(input_tokens) * prudent_input_rate
        + Decimal(max_output_tokens) * OUTPUT_RATE
    ) / Decimal(1_000_000)
    return (base * RESERVATION_SAFETY_MULTIPLIER).quantize(
        Decimal("0.000001"), rounding=ROUND_CEILING
    )


def _sample(schema: Mapping[str, Any], seed: str) -> Any:
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties") or {}
        return {
            key: _sample(properties[key], seed + ":" + key)
            for key in schema.get("required", [])
            if key in properties
        }
    if kind == "array":
        return []
    if "enum" in schema and schema["enum"]:
        return schema["enum"][int(seed[:8], 16) % len(schema["enum"])]
    if kind in ("number", "integer"):
        return 0
    if kind == "boolean":
        return False
    return "MOCK_" + seed[:12]


class MockResponsesClient:
    """Deterministic, non-networking client used by tests and MOCK mode."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(
        self, *, call_id: str, role: str, ticker: str, effort: Effort,
        prompt: str, schema: Mapping[str, Any], checkpoint: Callable[..., Any] | None = None,
    ) -> ProviderResponse:
        request_hash = sha256_json(build_request_body(
            role=role, effort=effort, prompt=prompt, schema=schema,
        ))
        parsed = validate_object(_sample(schema, request_hash), schema)
        output_text = canonical_text(parsed)
        raw = canonical_bytes({
            "id": "mock-" + request_hash[:24],
            "output": [{"content": [{"type": "output_text", "text": output_text}]}],
        })
        input_tokens = max(1, len(prompt.encode("utf-8")) // 4)
        visible_output_tokens = max(1, len(output_text.encode("utf-8")) // 4)
        reasoning_tokens = max(
            1, (int(request_hash[:6], 16) % 80) + (40 if effort == "high" else 10)
        )
        # Responses usage.output_tokens includes reasoning tokens. Keep the
        # visible component separately so MOCK and LIVE use the same billing
        # convention.
        output_tokens = visible_output_tokens + reasoning_tokens
        raw = canonical_bytes({
            "id": "mock-" + request_hash[:24],
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": output_text}]}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
            },
        })
        if checkpoint is not None:
            checkpoint(
                call_id=call_id,
                raw=raw,
                accounting={
                    "input_tokens": input_tokens,
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                    "output_tokens": output_tokens,
                    "visible_output_tokens": visible_output_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    "cost_usd": str(calculate_cost(input_tokens, 0, 0, output_tokens)),
                },
            )
        response = ProviderResponse(
            response_id="mock-" + request_hash[:24],
            output_text=output_text,
            parsed=parsed,
            input_tokens=input_tokens,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=output_tokens,
            visible_output_tokens=visible_output_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=input_tokens + output_tokens,
            duration_seconds=0.0,
            cost_usd=str(calculate_cost(input_tokens, 0, 0, output_tokens)),
            raw_sha256=sha256_bytes(raw),
            request_hash=request_hash,
        )
        self.calls.append({
            "call_id": call_id,
            "role": role,
            "ticker": ticker,
            "effort": effort,
            "prompt_hash": sha256_bytes(prompt.encode("utf-8")),
            "schema_hash": sha256_json(schema),
            "response_id": response.response_id,
        })
        return response


class LiveShadowDisabled(RuntimeError):
    pass


class OpenAIResponsesClient:
    """Network client that refuses to run without explicit authorization."""

    def __init__(
        self, *, max_budget_usd: Decimal, authorization: str,
        api_key: str | None = None, session: Any | None = None,
        max_output_tokens: int = MAX_OUTPUT_TOKENS, journal: Any | None = None,
        token_preflight_authorization: str = "",
        token_count_session: Any | None = None,
    ) -> None:
        if authorization != AUTHORIZATION_SENTINEL:
            raise LiveShadowDisabled(
                "LIVE SHADOW requires the explicit authorization sentinel"
            )
        if max_budget_usd <= 0:
            raise LiveShadowDisabled(
                "LIVE SHADOW requires a positive maximum budget"
            )
        self.max_budget_usd = max_budget_usd
        self.is_live_shadow = True
        self.total_cost = Decimal("0")
        self.max_output_tokens = int(max_output_tokens)
        if self.max_output_tokens < 1:
            raise LiveShadowDisabled("max_output_tokens must be positive")
        self.journal = journal
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise LiveShadowDisabled("OPENAI_API_KEY is not configured")
        self.session = session
        self.token_preflight_authorization = token_preflight_authorization
        self.token_count_session = token_count_session

    def offline_estimate_max_cost(
        self, *, role: str, effort: Effort, prompt: str,
        schema: Mapping[str, Any],
    ) -> Decimal:
        body = build_request_body(
            role=role,
            effort=effort,
            prompt=prompt,
            schema=schema,
            max_output_tokens=self.max_output_tokens,
        )
        input_bound = prudent_input_token_bound(body)
        return calculate_prudent_reservation(input_bound, self.max_output_tokens)

    def prepare_reservation(
        self, *, role: str, effort: Effort, prompt: str,
        schema: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self.token_preflight_authorization != TOKEN_PREFLIGHT_AUTHORIZATION_SENTINEL:
            raise LiveShadowDisabled(
                "LIVE SHADOW requires explicit input-token preflight authorization"
            )
        body = build_request_body(
            role=role,
            effort=effort,
            prompt=prompt,
            schema=schema,
            max_output_tokens=self.max_output_tokens,
        )
        request_hash = sha256_json(body)
        session = self.token_count_session
        if session is None:
            if self.session is None:
                import requests
                self.session = requests.Session()
            session = self.session
        response = session.post(
            INPUT_TOKEN_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=300,
        )
        if int(response.status_code) >= 400:
            raise LiveShadowDisabled(
                f"input-token preflight failed with HTTP {response.status_code}"
            )
        raw = bytes(response.content)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LiveShadowDisabled("input-token preflight returned invalid JSON") from exc
        if payload.get("object") != "response.input_tokens":
            raise LiveShadowDisabled("input-token preflight returned an unexpected object")
        try:
            input_tokens = int(payload["input_tokens"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveShadowDisabled("input-token preflight lacks a valid count") from exc
        reservation = calculate_prudent_reservation(
            input_tokens, self.max_output_tokens
        )
        return {
            "model": MODEL,
            "request_hash": request_hash,
            "input_tokens": input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "reservation_usd": str(reservation),
            "pricing_version": PRICING_VERSION,
            "pricing_rates_per_million": dict(PRICING_RATES_PER_MILLION),
            "pricing_valid_through": PRICING_VALID_THROUGH.isoformat(),
            "estimator_version": BUDGET_ESTIMATOR_VERSION,
            "count_response_sha256": sha256_bytes(raw),
            "counted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def invoke(
        self, *, call_id: str, role: str, ticker: str, effort: Effort,
        prompt: str, schema: Mapping[str, Any], checkpoint: Callable[..., Any] | None = None,
        token_preflight: Mapping[str, Any] | None = None,
    ) -> ProviderResponse:
        if self.journal is not None:
            self.total_cost = max(self.total_cost, self.journal.consumed_cost())
        if self.total_cost >= self.max_budget_usd:
            raise LiveShadowDisabled("LIVE SHADOW budget exhausted before next call")
        body = build_request_body(role=role, effort=effort, prompt=prompt,
                                  schema=schema, max_output_tokens=self.max_output_tokens)
        request_hash = sha256_json(body)
        if not isinstance(token_preflight, Mapping):
            raise LiveShadowDisabled("generation requires a verified input-token preflight")
        if token_preflight.get("request_hash") != request_hash:
            raise LiveShadowDisabled("generation request differs from token-counted request")
        if token_preflight.get("model") != MODEL:
            raise LiveShadowDisabled("token preflight model does not match generation model")
        worst_case = Decimal(str(token_preflight.get("reservation_usd")))
        if self.total_cost + worst_case > self.max_budget_usd:
            raise LiveShadowDisabled(
                "LIVE SHADOW budget preflight rejected the next call"
            )
        if self.session is None:
            import requests
            self.session = requests.Session()
        started = perf_counter()
        response = self.session.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=300,
        )
        if int(response.status_code) >= 400:
            accounting = {
                "cost_usd": None,
                "cost_unknown": True,
                "usage_unavailable": True,
                "http_status": int(response.status_code),
            }
            raise ProviderCallError(
                f"Responses API failed with HTTP {response.status_code}",
                error_type="HTTP",
                status="INDETERMINATE",
                accounting=accounting,
            )
        raw = bytes(response.content)
        raw_sha256 = sha256_bytes(raw)
        unknown_accounting = {
            "cost_usd": None,
            "cost_unknown": True,
            "usage_unavailable": True,
        }
        # This is intentionally the first operation after HTTP 200. No JSON
        # parsing, usage extraction or status interpretation occurs before it.
        if checkpoint is not None:
            checkpoint(call_id=call_id, raw=raw, accounting=unknown_accounting)
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderCallError(
                "Responses API returned invalid JSON after HTTP 200",
                error_type="PARSE",
                status="INDETERMINATE",
                raw_sha256=raw_sha256,
                accounting=unknown_accounting,
            ) from exc
        usage = envelope.get("usage") or {}
        input_details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        cached = int(input_details.get("cached_tokens") or 0)
        cache_write = int(input_details.get("cache_write_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        reasoning_tokens = int(output_details.get("reasoning_tokens") or 0)
        normalized_total_tokens = int(
            usage.get("total_tokens") or input_tokens + output_tokens
        )
        cost = calculate_cost(input_tokens, cached, cache_write, output_tokens)
        accounting = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "cache_write_tokens": cache_write,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "total_tokens": normalized_total_tokens,
            "cost_usd": str(cost),
            "cost_unknown": False,
        }
        if checkpoint is not None:
            checkpoint(call_id=call_id, raw=raw, accounting=accounting)
        if (not envelope.get("status") or envelope.get("status") in {"incomplete", "failed", "cancelled"}
                or envelope.get("incomplete_details")):
            raise ProviderCallError(
                "Responses API returned an incomplete response",
                error_type="INCOMPLETE",
                status="FAILED",
                raw_sha256=raw_sha256,
                accounting=accounting,
            )
        output_text = ""
        for item in envelope.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    output_text = str(content.get("text") or "")
                    break
            if output_text:
                break
        if not output_text:
            raise ProviderCallError(
                "Responses API response lacks output_text",
                error_type="PARSE",
                raw_sha256=raw_sha256,
                accounting=accounting,
            )
        try:
            parsed = validate_object(json.loads(output_text), schema)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderCallError(
                "Responses API output is invalid for the requested schema",
                error_type="SCHEMA",
                raw_sha256=raw_sha256,
                accounting=accounting,
            ) from exc
        self.total_cost += cost
        return ProviderResponse(
            response_id=str(envelope.get("id") or ""),
            output_text=output_text,
            parsed=parsed,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_tokens=cache_write,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=normalized_total_tokens,
            duration_seconds=perf_counter() - started,
            cost_usd=str(cost),
            raw_sha256=raw_sha256,
            request_hash=request_hash,
            visible_output_tokens=max(output_tokens - reasoning_tokens, 0),
        )

