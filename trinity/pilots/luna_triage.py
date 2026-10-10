"""Compact, upstream Luna triage for READY_TECHNICALLY funnel entries.

This module deliberately does not import or invoke USA V2, Setup, Ledger, or
Telegram.  Its output is a local screening artifact, not Research or a signal.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import requests

from trinity.pilots.pre_research import CANONICAL_UNIVERSE, load_canonical_universe
from trinity.shadow.atomic import atomic_bytes


MODEL = "gpt-5.6-luna"
PROMPT_VERSION = "luna-triage-v1"
DEFAULT_FUNNEL = Path("data/local/pre_research_funnel_latest.json")
DEFAULT_OUTPUT = Path("data/local/luna_triage_v1_latest.json")
DEFAULT_SOURCE_ROOT = Path("data/local/luna_triage_v1_sources")
FORWARD_SEC_ROOT = Path("data/local/trinity_forward_sources/trinity_forward_pilot")
EODHD_NEWS = "https://eodhistoricaldata.com/api/news"
EODHD_EARNINGS = "https://eodhd.com/api/calendar/earnings"
EXPECTED_READY_COUNT = 30
MAX_NEWS_ITEMS = 3

OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ticker", "decision", "qualitative_priority", "fresh_information",
        "material_catalyst_present", "material_risk_present", "primary_reason",
        "catalysts", "risks", "evidence_ids",
    ],
    "properties": {
        "ticker": {"type": "string", "minLength": 1},
        "decision": {"type": "string", "enum": ["DROP", "WATCH", "ESCALATE"]},
        "qualitative_priority": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
        "fresh_information": {"type": "boolean"},
        "material_catalyst_present": {"type": "boolean"},
        "material_risk_present": {"type": "boolean"},
        "primary_reason": {"type": "string", "minLength": 1, "maxLength": 300},
        "catalysts": {
            "type": "array", "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "risks": {
            "type": "array", "maxItems": 3,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
    },
}

PROMPT_TEMPLATE = """You are the TRINITY Luna Triage V1 screening stage.
Return only JSON matching the supplied schema. Evaluate one ticker independently.

Question: Does this technically relevant ticker contain enough current qualitative
information to justify escalation to the expensive full Research pipeline?

DROP means current information materially weakens the case for spending Research
resources. WATCH means potentially interesting, but current evidence is mixed,
non-urgent, or lacks a meaningful fresh catalyst. ESCALATE means full Research is
justified now by a meaningful fresh catalyst, expectation change, strong primary
evidence, or an unusually important uncertainty requiring deeper analysis.

ESCALATE is not BUY. Do not predict price. Do not give an entry, stop, target,
probability, recommendation, or trading signal. Use only the packet. Never invent
facts or citations. evidence_ids must contain only identifiers supplied in
available_evidence_ids. Give at most three short factual catalysts and three short
factual risks. Keep primary_reason concise.

TRIAGE_PACKET_JSON:
"""


class TriageError(RuntimeError):
    """A triage input, source, or Luna output was unusable."""


class TransientLunaError(TriageError):
    """A Luna call failed for a retryable provider/timeout reason."""


@dataclass(frozen=True)
class LunaReply:
    value: dict[str, Any]
    raw: bytes
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def validate_ready_entries(
    rows: Sequence[Mapping[str, object]], *, expected_count: int | None = None,
) -> list[dict[str, Any]]:
    """Validate an explicit READY-only batch without changing Luna semantics."""

    ready = [dict(row) for row in rows]
    if any(row.get("screening_state") != "READY_TECHNICALLY" for row in ready):
        raise TriageError("explicit Luna input contains a non-READY ticker")
    ready.sort(key=lambda row: str(row.get("ticker", "")))
    tickers = [str(row.get("ticker", "")).upper() for row in ready]
    if any(not ticker for ticker in tickers) or len(set(tickers)) != len(tickers):
        raise TriageError("READY input contains empty or duplicate tickers")
    if expected_count is not None and len(ready) != expected_count:
        raise TriageError(f"expected {expected_count} READY inputs, found {len(ready)}")
    return ready


def load_ready_entries(path: str | Path, *, expected_count: int | None = None) -> list[dict[str, Any]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TriageError(f"invalid pre-research funnel artifact: {type(exc).__name__}") from exc
    rows = value.get("survivors") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise TriageError("pre-research funnel artifact has no survivors list")
    return validate_ready_entries(
        [row for row in rows if isinstance(row, dict)
         and row.get("screening_state") == "READY_TECHNICALLY"],
        expected_count=expected_count,
    )


def _clean_text(value: object, limit: int = 360) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def select_news(ticker: str, value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise TriageError(f"{ticker}: EODHD news response has invalid shape")
    material = re.compile(
        r"earnings|results|guidance|outlook|forecast|acqui|merger|approval|trial|"
        r"contract|order|chief executive|\bceo\b|dividend|buyback|restructur|lawsuit|"
        r"investigation|offering|debt|rating|launch", re.I,
    )
    scored: list[tuple[int, str, str, dict[str, Any]]] = []
    for row in value:
        if not isinstance(row, dict) or not row.get("date") or not row.get("title"):
            continue
        symbols = [str(item).upper() for item in (row.get("symbols") or [])]
        provider = f"{ticker}.US"
        if symbols and ticker not in symbols and provider not in symbols:
            continue
        haystack = " ".join(map(str, [row.get("title", ""), row.get("content", ""),
                                      row.get("tags", "")]))
        score = 2 if material.search(haystack) else 0
        if len(symbols) <= 2:
            score += 1
        digest = hashlib.sha256(canonical_bytes(row)).hexdigest()
        scored.append((score, str(row["date"]), digest, row))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    chosen = [item for item in scored if item[0] >= 2][:MAX_NEWS_ITEMS]
    if not chosen:
        chosen = scored[:1]
    result = []
    for _score, published, digest, row in chosen:
        link = str(row.get("link") or "")
        result.append({
            "evidence_id": f"news:{ticker}:{digest[:16]}",
            "published_at": published,
            "headline": _clean_text(row.get("title"), 240),
            "source": urlparse(link).netloc or _clean_text(row.get("source"), 80) or "EODHD",
            "summary": _clean_text(row.get("content"), 360) or None,
            "url": link or None,
        })
    return result


def compact_sec(ticker: str, root: str | Path = FORWARD_SEC_ROOT) -> tuple[str | None, list[dict[str, Any]]]:
    path = Path(root) / ticker / "sec" / "manifest.json"
    if not path.is_file():
        return None, []
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        company = manifest["companies"][ticker]
        documents = company.get("documents", [])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TriageError(f"{ticker}: invalid cached SEC manifest") from exc
    documents = sorted(
        (row for row in documents if isinstance(row, dict) and row.get("evidence_id")),
        key=lambda row: (str(row.get("accepted_at") or ""), str(row["evidence_id"])),
        reverse=True,
    )[:3]
    compact = [{
        "evidence_id": str(row["evidence_id"]),
        "form": str(row.get("form") or ""),
        "filing_date": str(row.get("filing_date") or ""),
        "accepted_at": str(row.get("accepted_at") or "") or None,
        "material_event": str(row.get("document_kind") or ""),
        "title": _clean_text(row.get("title"), 240),
    } for row in documents]
    return str(company.get("company") or "") or None, compact


class FreshTriageSources:
    """Small EODHD captures plus read-only reuse of compact cached SEC metadata."""

    def __init__(
        self, *, api_key: str | None = None, cache_dir: str | Path = DEFAULT_SOURCE_ROOT,
        sec_root: str | Path = FORWARD_SEC_ROOT, session: requests.Session | None = None,
        clock: Callable[[], datetime] = utc_now,
        decision_cutoff_utc: datetime | None = None,
        research_cutoff_utc: datetime | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("EODHD_API_KEY")
        if not self.api_key:
            raise TriageError("EODHD_API_KEY is not configured")
        self.cache_dir = Path(cache_dir)
        self.sec_root = Path(sec_root)
        self.session = session or requests.Session()
        if session is None:
            import truststore
            truststore.inject_into_ssl()
        self.clock = clock
        if (decision_cutoff_utc is not None and research_cutoff_utc is not None and
                decision_cutoff_utc != research_cutoff_utc):
            raise TriageError("conflicting legacy and research cutoffs")
        # decision_cutoff_utc remains a compatibility alias for frozen callers.
        self.decision_cutoff_utc = research_cutoff_utc or decision_cutoff_utc
        self.research_cutoff_utc = self.decision_cutoff_utc
        self.news_requests = 0
        self.earnings_requests = 0
        self.news_cache_hits = 0
        self.sec_cache_hits = 0
        self.downloaded_bytes = 0
        self.post_cutoff_excluded = 0

    def news(self, ticker: str, provider_ticker: str) -> tuple[list[dict[str, Any]], str]:
        today = (
            self.decision_cutoff_utc.astimezone(timezone.utc).date()
            if self.decision_cutoff_utc is not None else
            self.clock().astimezone(timezone.utc).date()
        )
        start = today - timedelta(days=45)
        path = self.cache_dir / "news" / f"{ticker}_{start}_{today}.json"
        if path.is_file():
            raw = path.read_bytes()
            self.news_cache_hits += 1
        else:
            raw = self._get(EODHD_NEWS, {
                "api_token": self.api_key, "s": provider_ticker,
                "from": start.isoformat(), "to": today.isoformat(), "limit": 50,
                "fmt": "json",
            }, "news")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TriageError(f"{ticker}: EODHD news response is not JSON") from exc
        if self.decision_cutoff_utc is not None and isinstance(value, list):
            eligible = [item for item in value if isinstance(item, dict) and
                        _not_after_cutoff(item.get("date"), self.decision_cutoff_utc)]
            self.post_cutoff_excluded += len(value) - len(eligible)
            value = eligible
        return select_news(ticker, value), utc_text(self.clock())

    def earnings(self, provider_tickers: Sequence[str]) -> dict[str, dict[str, Any]]:
        today = (
            self.decision_cutoff_utc.astimezone(timezone.utc).date()
            if self.decision_cutoff_utc is not None else
            self.clock().astimezone(timezone.utc).date()
        )
        through = today + timedelta(days=180)
        path = self.cache_dir / "earnings" / f"{today}_{through}.json"
        try:
            if path.is_file():
                raw = path.read_bytes()
            else:
                raw = self._get(EODHD_EARNINGS, {
                    "api_token": self.api_key, "from": today.isoformat(),
                    "to": through.isoformat(), "fmt": "json",
                }, "earnings")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            value = json.loads(raw)
            rows = value.get("earnings", []) if isinstance(value, dict) else []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TriageError):
            return {}
        wanted = set(provider_tickers)
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            code, report_date = str(row.get("code") or ""), str(row.get("report_date") or "")
            if code not in wanted or not report_date or report_date < today.isoformat():
                continue
            if code not in result or report_date < result[code]["next_report_date"]:
                result[code] = {
                    "evidence_id": f"earnings-calendar:{code}:{report_date}",
                    "next_report_date": report_date,
                    "status": str(row.get("before_after_market") or "") or None,
                    "source": "EODHD earnings calendar",
                }
        return result

    def sec(self, ticker: str) -> tuple[str | None, list[dict[str, Any]]]:
        company, evidence = compact_sec(ticker, self.sec_root)
        if self.decision_cutoff_utc is not None:
            eligible = [item for item in evidence if _not_after_cutoff(
                item.get("accepted_at") or item.get("published_at"), self.decision_cutoff_utc,
            )]
            self.post_cutoff_excluded += len(evidence) - len(eligible)
            evidence = eligible
        if evidence:
            self.sec_cache_hits += 1
        return company, evidence

    def _get(self, url: str, params: Mapping[str, object], kind: str) -> bytes:
        if kind == "news":
            self.news_requests += 1
        else:
            self.earnings_requests += 1
        try:
            response = self.session.get(url, params=dict(params), timeout=45)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise TriageError(f"EODHD {kind} request failed ({type(exc).__name__})") from exc
        raw = bytes(response.content)
        self.downloaded_bytes += len(raw)
        return raw


def _not_after_cutoff(value: object, cutoff: datetime) -> bool:
    if not value:
        return True
    text = str(value)
    try:
        if len(text) == 10:
            return date.fromisoformat(text) <= cutoff.astimezone(timezone.utc).date()
        if text.isdigit() and len(text) == 14:
            from zoneinfo import ZoneInfo
            parsed = datetime.strptime(text, "%Y%m%d%H%M%S").replace(
                tzinfo=ZoneInfo("America/New_York")
            )
        else:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc) <= cutoff.astimezone(timezone.utc)
    except ValueError:
        return False


def build_packet(
    row: Mapping[str, object], provider_ticker: str, news: Sequence[Mapping[str, object]],
    company_name: str | None, sec: Sequence[Mapping[str, object]],
    earnings: Mapping[str, object] | None, source_snapshot_timestamp: str,
) -> dict[str, Any]:
    ticker = str(row["ticker"])
    technical_keys = (
        "latest_session", "close", "regime", "distance_sma20_pct",
        "distance_sma50_pct", "distance_prior_high20_pct", "rsi14",
        "relative_volume", "primary_reason",
    )
    packet = {
        "ticker_identity": {
            "ticker": ticker, "provider_ticker": provider_ticker,
            "company_name": company_name,
        },
        "technical_snapshot": {key: row.get(key) for key in technical_keys},
        "recent_material_news": [dict(item) for item in news],
        "primary_material_evidence": [dict(item) for item in sec],
        "earnings_proximity": dict(earnings) if earnings else None,
        "source_snapshot_timestamp": source_snapshot_timestamp,
    }
    ids = [str(item["evidence_id"]) for group in (news, sec) for item in group]
    if earnings and earnings.get("evidence_id"):
        ids.append(str(earnings["evidence_id"]))
    packet["available_evidence_ids"] = sorted(set(ids))
    return packet


def make_prompt(packet: Mapping[str, object]) -> str:
    return PROMPT_TEMPLATE + canonical_bytes(packet).decode("utf-8")


def validate_reply(value: object, ticker: str, evidence_ids: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(OUTPUT_SCHEMA["required"]):
        raise TriageError(f"{ticker}: Luna output does not match exact schema")
    if value["ticker"] != ticker:
        raise TriageError(f"{ticker}: Luna returned a different ticker")
    if value["decision"] not in {"DROP", "WATCH", "ESCALATE"}:
        raise TriageError(f"{ticker}: invalid Luna decision")
    if value["qualitative_priority"] not in {"LOW", "MEDIUM", "HIGH"}:
        raise TriageError(f"{ticker}: invalid qualitative priority")
    for key in ("fresh_information", "material_catalyst_present", "material_risk_present"):
        if type(value[key]) is not bool:
            raise TriageError(f"{ticker}: {key} must be boolean")
    if not isinstance(value["primary_reason"], str) or not value["primary_reason"].strip() \
            or len(value["primary_reason"]) > 300:
        raise TriageError(f"{ticker}: invalid primary_reason")
    for key in ("catalysts", "risks"):
        items = value[key]
        if not isinstance(items, list) or len(items) > 3 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 300 for item in items
        ):
            raise TriageError(f"{ticker}: invalid {key}")
    cited = value["evidence_ids"]
    if not isinstance(cited, list) or len(cited) != len(set(cited)) or any(
        not isinstance(item, str) for item in cited
    ):
        raise TriageError(f"{ticker}: invalid evidence_ids")
    invalid = sorted(set(cited) - set(evidence_ids))
    if invalid:
        raise TriageError(f"{ticker}: Luna cited unsupplied evidence IDs: {invalid}")
    return dict(value)


def _usage_from_jsonl(text: str) -> tuple[int | None, int | None, int | None]:
    candidates: list[Mapping[str, object]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if any(key in value for key in ("input_tokens", "output_tokens", "total_tokens")):
                candidates.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in text.splitlines():
        try:
            visit(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not candidates:
        return None, None, None
    value = candidates[-1]
    integer = lambda key: int(value[key]) if isinstance(value.get(key), (int, float)) else None
    return integer("input_tokens"), integer("output_tokens"), integer("total_tokens")


class LunaCLI:
    model = MODEL

    def invoke(self, prompt: str) -> LunaReply:
        with tempfile.TemporaryDirectory(prefix="trinity-luna-triage-") as directory:
            root = Path(directory)
            schema_path, result_path = root / "schema.json", root / "result.json"
            schema_path.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
            command = [
                "codex", "exec", "--json", "--ephemeral", "--sandbox", "read-only",
                "--skip-git-repo-check", "--ignore-user-config", "--model", MODEL,
                "--output-schema", str(schema_path), "--output-last-message",
                str(result_path), "-",
            ]
            environment = os.environ.copy()
            environment.setdefault("HOME", str(Path.home()))
            environment.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
            try:
                completed = subprocess.run(
                    command, input=prompt, text=True, encoding="utf-8", capture_output=True,
                    cwd=root, timeout=240, check=False, env=environment,
                )
            except subprocess.TimeoutExpired as exc:
                raise TransientLunaError("Codex Luna invocation timed out") from exc
            except OSError as exc:
                raise TriageError(f"Codex CLI unavailable: {type(exc).__name__}") from exc
            diagnostic = "\n".join((completed.stdout, completed.stderr)).lower()
            if completed.returncode != 0 or not result_path.is_file():
                diagnostic_lines = [line for line in (completed.stderr + "\n" + completed.stdout).splitlines()
                                    if line.strip()]
                message = diagnostic_lines[-1] if diagnostic_lines else "no diagnostic"
                if any(word in diagnostic for word in (
                    "timeout", "timed out", "rate limit", "temporarily", "connection", "unavailable",
                )):
                    raise TransientLunaError(f"Codex Luna transient failure: {message[:1000]}")
                raise TriageError(f"Codex Luna failed without fallback: {message[:1000]}")
            raw = result_path.read_bytes()
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TriageError("Codex Luna returned invalid JSON") from exc
            input_tokens, output_tokens, total_tokens = _usage_from_jsonl(completed.stdout)
            return LunaReply(value, raw, input_tokens, output_tokens, total_tokens)


def run_triage(
    *, funnel_path: str | Path = DEFAULT_FUNNEL, output_path: str | Path | None = DEFAULT_OUTPUT,
    source_root: str | Path = DEFAULT_SOURCE_ROOT, sources: Any | None = None,
    luna: Any | None = None, universe_path: str | Path = CANONICAL_UNIVERSE,
    expected_count: int | None = EXPECTED_READY_COUNT,
    clock: Callable[[], datetime] = utc_now,
    ready_entries: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, Any]:
    batch_started_wall, batch_started = clock(), time.perf_counter()
    rows = (
        load_ready_entries(funnel_path, expected_count=expected_count)
        if ready_entries is None else
        validate_ready_entries(ready_entries, expected_count=expected_count)
    )
    mapping = {item.ticker: item.provider_ticker for item in load_canonical_universe(universe_path)}
    if any(str(row["ticker"]) not in mapping for row in rows):
        raise TriageError("READY input includes a ticker outside the canonical universe")
    source = sources or FreshTriageSources(cache_dir=source_root, clock=clock)
    client = luna or LunaCLI()
    earnings = source.earnings([mapping[str(row["ticker"])] for row in rows])
    snapshot = utc_text(datetime.fromtimestamp(Path(funnel_path).stat().st_mtime, timezone.utc))
    results: list[dict[str, Any]] = []
    total_retries = 0
    luna_invocations = 0

    for row in rows:
        ticker = str(row["ticker"])
        ticker_started_wall, ticker_started = clock(), time.perf_counter()
        base: dict[str, Any] = {
            "ticker": ticker, "technical_reason": str(row.get("primary_reason") or ""),
            "model": MODEL, "prompt_version": PROMPT_VERSION, "status": "FAILED",
            "retries": 0,
        }
        try:
            news, retrieved_at = source.news(ticker, mapping[ticker])
            company, sec = source.sec(ticker)
            packet = build_packet(
                row, mapping[ticker], news, company, sec, earnings.get(mapping[ticker]),
                retrieved_at,
            )
            prompt = make_prompt(packet)
            encoded = prompt.encode("utf-8")
            base.update({
                "source_snapshot_timestamp": snapshot,
                "input_sha256": hashlib.sha256(encoded).hexdigest(),
                "input_bytes": len(encoded),
                "available_evidence_ids": packet["available_evidence_ids"],
            })
            reply: LunaReply | None = None
            for attempt in range(2):
                try:
                    luna_invocations += 1
                    reply = client.invoke(prompt)
                    break
                except TransientLunaError:
                    if attempt == 1:
                        raise
                    base["retries"] = 1
                    total_retries += 1
            assert reply is not None
            parsed = validate_reply(reply.value, ticker, packet["available_evidence_ids"])
            response_dir = Path(source_root) / "responses"
            response_dir.mkdir(parents=True, exist_ok=True)
            atomic_bytes(response_dir / f"{ticker}.json", reply.raw)
            base.update({
                **parsed, "status": "SUCCESS",
                "raw_response_sha256": hashlib.sha256(reply.raw).hexdigest(),
                "selected_evidence_ids": parsed["evidence_ids"],
                "input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                "total_tokens": reply.total_tokens,
            })
        except (TriageError, OSError, ValueError) as exc:
            base["failure_reason"] = str(exc)[:500]
        ended = clock()
        base.update({
            "started_at": utc_text(ticker_started_wall), "finished_at": utc_text(ended),
            "duration_seconds": time.perf_counter() - ticker_started,
        })
        results.append(base)

    successful = [row for row in results if row["status"] == "SUCCESS"]
    byte_values = [int(row["input_bytes"]) for row in results if row.get("input_bytes") is not None]
    input_tokens_available = bool(successful) and all(
        row.get("input_tokens") is not None for row in successful
    )
    output_tokens_available = bool(successful) and all(
        row.get("output_tokens") is not None for row in successful
    )
    total_tokens_available = bool(successful) and all(
        row.get("total_tokens") is not None for row in successful
    )
    finished = clock()
    value = {
        "schema_name": "trinity.luna-triage", "schema_version": "1",
        "model": MODEL, "model_explicitly_selected": True, "prompt_version": PROMPT_VERSION,
        "funnel_path": str(Path(funnel_path).resolve()), "source_snapshot_timestamp": snapshot,
        "started_at": utc_text(batch_started_wall), "finished_at": utc_text(finished),
        "runtime_seconds": time.perf_counter() - batch_started,
        "counts": {
            "ready_input": len(rows),
            "drop": sum(row.get("decision") == "DROP" for row in successful),
            "watch": sum(row.get("decision") == "WATCH" for row in successful),
            "escalate": sum(row.get("decision") == "ESCALATE" for row in successful),
            "failed": len(results) - len(successful),
        },
        "escalate_tickers": [row["ticker"] for row in successful if row["decision"] == "ESCALATE"],
        "cost": {
            "luna_invocations": luna_invocations,
            "retries": total_retries,
            "total_input_bytes": sum(byte_values),
            "mean_input_bytes": statistics.mean(byte_values) if byte_values else 0,
            "median_input_bytes": statistics.median(byte_values) if byte_values else 0,
            "maximum_input_bytes": max(byte_values, default=0),
            "average_runtime_per_ticker": (time.perf_counter() - batch_started) / len(rows) if rows else 0,
            "input_tokens_available_for_all_successes": input_tokens_available,
            "output_tokens_available_for_all_successes": output_tokens_available,
            "total_tokens_available_for_all_successes": total_tokens_available,
            "input_tokens": sum(int(row["input_tokens"]) for row in successful) if input_tokens_available else None,
            "output_tokens": sum(int(row["output_tokens"]) for row in successful) if output_tokens_available else None,
            "total_tokens": sum(int(row["total_tokens"]) for row in successful) if total_tokens_available else None,
            "sol_calls": 0,
            "eodhd_news_requests": int(getattr(source, "news_requests", 0)),
            "eodhd_earnings_requests": int(getattr(source, "earnings_requests", 0)),
            "sec_requests": 0,
            "news_cache_hits": int(getattr(source, "news_cache_hits", 0)),
            "sec_cache_hits": int(getattr(source, "sec_cache_hits", 0)),
            "downloaded_bytes": int(getattr(source, "downloaded_bytes", 0)),
            "post_cutoff_excluded": int(getattr(source, "post_cutoff_excluded", 0)),
        },
        "results": results,
    }
    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return value


def render_report(value: Mapping[str, Any]) -> str:
    headers = ("Ticker", "Technical reason", "Luna decision", "Priority", "Fresh", "Catalyst",
               "Risk", "Primary reason", "Input bytes", "Duration", "Status")
    rows = []
    for item in value["results"]:
        rows.append((
            item["ticker"], item["technical_reason"], item.get("decision", "-"),
            item.get("qualitative_priority", "-"), str(item.get("fresh_information", "-")),
            str(item.get("material_catalyst_present", "-")),
            str(item.get("material_risk_present", "-")),
            item.get("primary_reason") or item.get("failure_reason", "-"),
            str(item.get("input_bytes", "-")), f"{item['duration_seconds']:.2f}s", item["status"],
        ))
    widths = [max(len(str(row[i])) for row in [headers, *rows]) for i in range(len(headers))]
    lines = [" | ".join(str(value).ljust(widths[i]) for i, value in enumerate(headers))]
    lines.append("-+-".join("-" * width for width in widths))
    lines.extend(" | ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)) for row in rows)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--funnel", default=str(DEFAULT_FUNNEL))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    args = parser.parse_args(argv)
    try:
        result = run_triage(funnel_path=args.funnel, output_path=args.output,
                            source_root=args.source_root)
    except TriageError as exc:
        parser.error(str(exc))
    print(render_report(result))
    counts, cost = result["counts"], result["cost"]
    print(f"\nREADY: {counts['ready_input']} | DROP: {counts['drop']} | WATCH: {counts['watch']} | "
          f"ESCALATE: {counts['escalate']} | FAILED: {counts['failed']}")
    print("ESCALATE tickers: " + (" ".join(result["escalate_tickers"]) or "-"))
    print(f"Luna invocations: {cost['luna_invocations']} | Retries: {cost['retries']} | "
          f"Sol calls: {cost['sol_calls']}")
    print(f"Input bytes total/mean/median/max: {cost['total_input_bytes']}/"
          f"{cost['mean_input_bytes']:.1f}/{cost['median_input_bytes']:.1f}/"
          f"{cost['maximum_input_bytes']}")
    print(f"EODHD News requests: {cost['eodhd_news_requests']} | SEC requests: 0 | "
          f"Cache hits (news/SEC): {cost['news_cache_hits']}/{cost['sec_cache_hits']}")
    print("Measured tokens input/output/total: "
          f"{cost['input_tokens'] if cost['input_tokens'] is not None else 'unavailable'}/"
          f"{cost['output_tokens'] if cost['output_tokens'] is not None else 'unavailable'}/"
          f"{cost['total_tokens'] if cost['total_tokens'] is not None else 'unavailable'}")
    print(f"Output: {Path(args.output).resolve()}")
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
