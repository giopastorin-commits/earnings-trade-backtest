"""Adapters from frozen analytical providers to the Responses API transport."""

from __future__ import annotations

import json
from typing import Mapping

from trinity.pilots.luna_triage import LunaReply, MODEL as LUNA_MODEL, OUTPUT_SCHEMA
from trinity.pilots.multiticker import RecordingUSAProvider

from .responses import ResponsesAPI


class ResponsesLuna:
    model = LUNA_MODEL

    def __init__(self, transport: ResponsesAPI, ticker_order: list[str]) -> None:
        self.transport = transport
        self.ticker_order = iter(ticker_order)
        self.ordinal = 0

    def invoke(self, prompt: str) -> LunaReply:
        self.ordinal += 1
        ticker = next(self.ticker_order)
        result = self.transport.invoke(
            model=LUNA_MODEL, role="LUNA_TRIAGE", ticker=ticker, prompt=prompt,
            schema=OUTPUT_SCHEMA, call_ordinal=self.ordinal,
        )
        raw = json.dumps(result.parsed, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return LunaReply(
            result.parsed, raw, result.usage["input_tokens"],
            result.usage["output_tokens"], result.usage["total_tokens"],
        )


class ResponsesSolProvider(RecordingUSAProvider):
    """Preserves USAProvider prompts/schemas/post-processing; replaces only transport."""

    def __init__(self, transport: ResponsesAPI, ticker: str, ordinal_base: int = 0) -> None:
        self.transport = transport
        self.ticker = ticker
        self.ordinal_base = ordinal_base
        self._api_ordinal = 0
        super().__init__(requester=self._invoke)
        self.provenance = {
            "provider": "OPENAI_RESPONSES_API", "transport": "RESPONSES_API_HTTPS",
            "model": "gpt-5.6-sol", "model_version": "responses-api:gpt-5.6-sol",
            "timeout_seconds": 300, "sandbox": "not-applicable",
            "skip_git_repo_check": False, "ignore_user_config": False,
        }

    def _invoke(self, prompt: str, schema: Mapping[str, object]) -> dict[str, object]:
        self._api_ordinal += 1
        role = "ANALYST" if self._api_ordinal == 1 else "CRITIC"
        result = self.transport.invoke(
            model="gpt-5.6-sol", role=role, ticker=self.ticker, prompt=prompt,
            schema=schema, call_ordinal=self.ordinal_base + self._api_ordinal,
        )
        return result.parsed
