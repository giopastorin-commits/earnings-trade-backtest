"""USA V2 normalization over captured EODHD and SEC evidence.

Historical V1 keeps its issuer-release-only contract. Forward Facts Contract
V2 also admits narrowly validated third-party corporate-action reporting while
preserving its non-primary provenance and uncertainty.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import html
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from trinity.italia_real import CodexCLIProvider, _ANALYST_SCHEMA
from trinity.italia_v1 import (
    analyze_company, build_facts, build_facts_v2, save_thesis,
)
from trinity.usa_issuer_registry import REGISTRY, get_issuer
from trinity.usa_documents import DEFAULT_CACHE as DOCUMENT_CACHE, load_document_coverage
from trinity.paths import baseline_root, news_cache, price_cache


AS_OF = "2026-09-12"
DATASET = baseline_root()
PRICES = price_cache()
NEWS = news_cache()
_ORIGINAL_COMPANIES = {
    "AAPL": ("Apple", "TECHNOLOGY", ("Apple",)),
    "JPM": ("JPMorgan Chase", "BANK", ("JPMorganChase", "JPMorgan Chase")),
    "JNJ": ("Johnson & Johnson", "HEALTHCARE", ("Johnson & Johnson",)),
    "XOM": ("Exxon Mobil", "ENERGY", ("ExxonMobil",)),
    "WMT": ("Walmart", "CONSUMER_STAPLES", ("Walmart",)),
    "CAT": ("Caterpillar", "INDUSTRIAL", ("Caterpillar",)),
    "NEE": ("NextEra Energy", "UTILITY", ("NextEra Energy",)),
    "AMZN": ("Amazon", "CONSUMER_DISCRETIONARY", ("Amazon.com",)),
    "PLD": ("Prologis", "REAL_ESTATE", ("Prologis",)),
    "LIN": ("Linde", "MATERIALS", ("Linde",)),
}
ORIGINAL_TICKERS = tuple(_ORIGINAL_COMPANIES)
# Compatibility view retained for callers; values now come from one validated
# canonical registry.  The original ten entries are identical to USA V2 V1.
COMPANIES = {
    record.ticker: (record.company_name, record.schema_type, record.aliases)
    for record in REGISTRY.supported
}
WIRE = re.compile(r"(?:BUSINESS WIRE|PRNewswire|PR NEWSWIRE)", re.I)
CORPORATE_ACTION = re.compile(
    r"\b(?:takeover|take[- ]private|go[- ]private|acquisition (?:offer|proposal|approach)|"
    r"merger (?:talks?|negotiations?|agreement)|definitive (?:merger|transaction) agreement|"
    r"regulatory (?:approval|clearance|review)|shareholder approval|tender offer)\b",
    re.I,
)
REPORTED_LANGUAGE = re.compile(
    r"\b(?:reports?|reported|according to|citing|people familiar|sources?|talks?|approach|"
    r"financing efforts?)\b",
    re.I,
)
RUMORED_LANGUAGE = re.compile(r"\b(?:rumou?rs?|chatter|speculation|unconfirmed)\b", re.I)
RESULT = re.compile(r"(?:first|second|third|fourth)[- ]quarter.{0,35}results|quarterly.{0,20}results|revenue growth", re.I)
SCHEDULED = re.compile(r"to host|conference call|available on|to release|to announce|date for release|to report", re.I)
GUIDANCE = re.compile(r"\b(?:raises?|raised|reiterates?|reaffirm(?:s|ed)?|lowers?|lowered|updates?|provides?|issues?)\b.{0,75}\b(?:guidance|outlook)\b|\b(?:guidance|outlook)\b.{0,60}\b(?:raises?|raised|reiterates?|reaffirm(?:s|ed)?|lowers?|lowered|updates?|provides?|issues?)\b", re.I)
_METRIC_LABEL = r"adjusted operating profit|operating profit|operating income|sales and revenues|net sales|net income|net earnings|cash flow|revenue|earnings|sales"
METRIC = re.compile(
    rf"(?P<label>{_METRIC_LABEL})\b(?:(?!\b(?:{_METRIC_LABEL})\b)[^.!?\n]){{0,65}}?"
    r"(?P<amount>\$[\d,.]+\s*(?:billion|million|trillion))", re.I,
)
GUIDANCE_RANGE = re.compile(
    r"\$([\d,]+(?:\.\d+)?)\s*(?:-|to|–|—)\s*\$?([\d,]+(?:\.\d+)?)", re.I
)
GUIDANCE_TABLE_RANGE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s+\$\s*([\d,]+(?:\.\d+)?)", re.I
)
REVISION_FIELDS = ("fundamental_analysis", "earnings_and_news_analysis", "price_context",
                   "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation")
CLAIM_FIELDS = frozenset(REVISION_FIELDS)
DECISION_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})
EVENT_CLASSES = (
    "NEW_INFORMATION", "EXPECTATION_CHANGE", "CONFIRMATION", "REITERATION",
    "ALREADY_KNOWN", "LOW_RELEVANCE",
)
_CLAIM_REF_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["claim_id", "field", "text", "fact_ids", "materiality", "period"],
    "properties": {
        "claim_id": {"type": "string"},
        "field": {"type": "string", "enum": list(REVISION_FIELDS)},
        "text": {"type": "string"},
        "fact_ids": {"type": "array", "items": {"type": "string"}},
        "materiality": {"type": "string", "enum": ["MATERIAL", "SUPPORTING"]},
        "period": {"type": ["string", "null"]},
    },
}
_EVENT_ASSESSMENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["event_id", "classification", "material", "rationale"],
    "properties": {
        "event_id": {"type": "string"},
        "classification": {"type": "string", "enum": list(EVENT_CLASSES)},
        "material": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
}
_USA_ANALYST_SCHEMA = json.loads(json.dumps(_ANALYST_SCHEMA))
_USA_ANALYST_SCHEMA["required"].remove("confidence")
del _USA_ANALYST_SCHEMA["properties"]["confidence"]
_USA_ANALYST_SCHEMA["required"].extend(
    ["claim_refs", "evidence_confidence", "thesis_strength", "event_assessments"]
)
_USA_ANALYST_SCHEMA["properties"]["claim_refs"] = {
    "type": "array", "items": _CLAIM_REF_SCHEMA,
}
_USA_ANALYST_SCHEMA["properties"]["evidence_confidence"] = {
    "type": "string", "enum": list(DECISION_LEVELS),
}
_USA_ANALYST_SCHEMA["properties"]["thesis_strength"] = {
    "type": "string", "enum": list(DECISION_LEVELS),
}
_USA_ANALYST_SCHEMA["properties"]["event_assessments"] = {
    "type": "array", "items": _EVENT_ASSESSMENT_SCHEMA,
}
_REVISED_SCHEMA = {"type": "object", "additionalProperties": False,
                   "required": [*REVISION_FIELDS, "claim_refs"],
                   "properties": {
                       **{key: _ANALYST_SCHEMA["properties"][key] for key in REVISION_FIELDS},
                       "claim_refs": {"type": "array", "items": _CLAIM_REF_SCHEMA},
                   }}
_USA_CRITIC_SCHEMA = {"type": "object", "additionalProperties": False,
                      "required": ["notes", "status", "evidence_confidence",
                                   "thesis_strength", "event_assessments", "revised_analysis"],
                      "properties": {
                          "notes": {"type": "array", "items": {"type": "string"}},
                          "status": {"type": "string", "enum": ["PASS", "WATCH", "INVESTIGATE"]},
                          "evidence_confidence": {"type": "string", "enum": list(DECISION_LEVELS)},
                          "thesis_strength": {"type": "string", "enum": list(DECISION_LEVELS)},
                          "event_assessments": {"type": "array", "items": _EVENT_ASSESSMENT_SCHEMA},
                          "revised_analysis": _REVISED_SCHEMA,
                      }}


@dataclass(frozen=True)
class EvidencePack:
    ticker: str
    as_of: str
    company_input: dict[str, object]
    triage: dict[str, object]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slug(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_") or "unknown"


def _period_key(period: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-\d{2}/(\d{4})-(\d{2})-\d{2}", period)
    if match and match.group(1) == match.group(3):
        quarter = (int(match.group(4)) - 1) // 3 + 1
        return f"{match.group(3)}Q{quarter}"
    return period.replace("/", "_")


def _fact_identity(
    ticker: str, fact: dict[str, object], *, fallback_period: str, discriminator: str = "",
) -> dict[str, object]:
    """Attach stable identity/provenance metadata without copying the fact value."""
    metric = str(fact.get("metric") or fact.get("metric_label") or "unknown")
    period = str(fact.get("period") or fallback_period)
    evidence_id = str(fact.get("evidence_id") or fact.get("evidence_identifier") or "")
    base = f"fact:{ticker}:{_period_key(period)}:{_slug(metric)}"
    if discriminator:
        digest = hashlib.sha256(
            f"{evidence_id}|{metric}|{fact.get('value')}|{fact.get('unit')}|{discriminator}".encode()
        ).hexdigest()[:10]
        base = f"{base}:{digest}"
    fact["fact_id"] = base
    fact["ticker"] = ticker
    fact["metric"] = metric
    fact["period"] = period
    fact["evidence_id"] = evidence_id
    unit = str(fact.get("unit") or "")
    fact["scale"] = {
        "USD": 1, "USD_THOUSANDS": 1_000, "USD_MILLIONS": 1_000_000,
        "USD_BILLIONS": 1_000_000_000, "USD_TRILLIONS": 1_000_000_000_000,
        "PERCENT": 1, "USD_PER_SHARE": 1, "USD/shares": 1,
    }.get(unit, 1)
    value = fact.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        decimal_value = Decimal(str(value))
        fact["display_precision"] = max(0, -decimal_value.as_tuple().exponent)
    else:
        fact["display_precision"] = None
    return fact


def _attach_fact_ids(
    ticker: str, price: dict[str, object], price_evidence_id: str,
    events: list[dict[str, object]],
) -> None:
    price["acquisition"]["fact_metadata"] = [
        {
            "fact_id": f"fact:{ticker}:{price['published_at']}:{metric}",
            "ticker": ticker, "metric": metric, "value_path": f"price.{metric}",
            "unit": "USD" if metric == "current_price" else "PERCENT",
            "period": price["published_at"], "evidence_id": price_evidence_id,
            "extraction_method": "deterministic_cached_ohlcv",
            "scale": 1, "display_precision": 4 if metric != "current_price" else 2,
        }
        for metric in ("current_price", "return_5d", "return_20d", "return_60d")
    ]
    for event in events:
        published = str(event["published_at"])
        groups = (
            ("reported_metrics", "reported"),
            ("guidance_ranges", "guidance_range"),
            ("guidance_changes", "guidance_change"),
            ("structured_sec_facts", ""),
        )
        for key, discriminator in groups:
            for fact in event["facts"].get(key, []):
                if key == "guidance_ranges":
                    fact.setdefault("value", fact.get("current_range"))
                elif key == "guidance_changes":
                    fact.setdefault("value", fact.get("direction"))
                    fact.setdefault("evidence_identifier", fact.get("current_evidence_identifier"))
                    fact.setdefault("unit", "DIRECTION")
                _fact_identity(
                    ticker, fact, fallback_period=published,
                    discriminator=f"{discriminator}:{published}" if discriminator else "",
                )


def _read_prices(
    ticker: str, as_of: str, *, prices_dir: Path = PRICES,
    source_name: str = "EODHD frozen daily USA cache",
    retrieved_at: str = "2026-09-13",
) -> tuple[dict[str, object], dict[str, object]]:
    issuer = get_issuer(ticker)
    path = prices_dir / f"{ticker}.json"
    raw = path.read_bytes()
    bars = json.loads(raw)
    bars = [bar for bar in bars if bar["date"] <= as_of]
    if len(bars) < 61 or any(bars[-1][field] is None for field in ("open", "high", "low", "close", "volume")):
        raise ValueError(f"{ticker}: insufficient OHLCV")
    latest = bars[-1]
    if latest["close"] <= 0 or latest["volume"] < 0:
        raise ValueError(f"{ticker}: invalid latest OHLCV")
    returns = {f"return_{period}d": round(100 * (latest["close"] / bars[-period - 1]["close"] - 1), 4)
               for period in (5, 20, 60)}
    reference_bars = {str(period): {"date": bars[-period - 1]["date"],
                                    "close": bars[-period - 1]["close"]}
                      for period in (5, 20, 60)}
    evidence = {
        "source": source_name, "published_at": latest["date"],
        "identifier": f"price:{ticker}:{latest['date']}", "url": None,
        "excerpt": json.dumps({"latest": {key: latest[key] for key in
                                            ("date", "open", "high", "low", "close", "volume")},
                               "reference_bars": reference_bars, "returns_percent": returns}),
        "title": f"{ticker} daily OHLCV", "retrieved_at": retrieved_at,
        "content_sha256": _digest(raw), "raw_path": str(path),
        "published_at_precision": "day", "document_kind": "PRICE",
    }
    # The frozen Facts/Ledger acquisition contract has an optional
    # adjusted_close slot. Twelve Data production snapshots deliberately carry
    # only raw OHLCV (adjust=none), so represent the absent provider field as
    # null without adding it to the analytical bar series used by Setup V1.
    acquisition_last_bar = {
        "date": latest["date"],
        **{field: latest.get(field) for field in (
            "open", "high", "low", "close", "adjusted_close", "volume",
        )},
    }
    price = {"current_price": latest["close"], **returns, "published_at": latest["date"],
             "acquisition": {"currency": "USD", "market": "USA", "provider_symbol": issuer.provider_symbol,
                             "last_bar": acquisition_last_bar, "reference_bars": reference_bars,
                             "raw_path": str(path), "sha256": evidence["content_sha256"]}}
    return price, evidence


def _issuer_release(record: Mapping[str, object], ticker: str) -> bool:
    title = html.unescape(str(record.get("title") or ""))
    content = html.unescape(str(record.get("content") or ""))
    issuer = get_issuer(ticker)
    aliases = issuer.aliases
    return (issuer.provider_symbol in record.get("symbols", [])
            and any(title.casefold().startswith(alias.casefold()) for alias in aliases)
            and bool(WIRE.search(content[:650]))
            and any(alias.casefold() in content[:2000].casefold() for alias in aliases))


def _news_evidence_id(record: Mapping[str, object], ticker: str) -> str:
    return f"news:{ticker}:{str(record.get('canonical_record_sha256') or '')[:16]}"


def _third_party_corporate_action(
    record: Mapping[str, object], ticker: str,
) -> tuple[bool, str, str]:
    """Validate a narrow company-specific corporate-action report."""

    issuer = get_issuer(ticker)
    symbols = [str(item).upper() for item in (record.get("symbols") or [])]
    if issuer.provider_symbol not in symbols:
        return False, "UNKNOWN", "PROVIDER_TICKER_MISMATCH"
    title = html.unescape(str(record.get("title") or "")).strip()
    content = html.unescape(str(record.get("content") or "")).strip()
    published = str(record.get("published_ts") or record.get("date") or "")
    if not published:
        return False, "UNKNOWN", "MISSING_PUBLICATION_TIMESTAMP"
    aliases = issuer.aliases
    matching_aliases = [alias for alias in aliases if alias.casefold() in title.casefold()]
    if not matching_aliases:
        return False, "UNKNOWN", "ISSUER_NOT_SPECIFIC_IN_TITLE"
    if not any(title.casefold().startswith(alias.casefold()) for alias in matching_aliases) and not (
        CORPORATE_ACTION.search(title)
    ):
        return False, "UNKNOWN", "ISSUER_NOT_PRIMARY_SUBJECT"
    # Only the headline and opening report establish the subject. Related-link
    # modules and company-history boilerplate later in an article must not turn
    # an otherwise unrelated story into a corporate-action event.
    material_text = f"{title}\n{content[:650]}"
    if not CORPORATE_ACTION.search(material_text):
        return False, "UNKNOWN", "NO_SUPPORTED_CORPORATE_ACTION"
    if not REPORTED_LANGUAGE.search(material_text) and not RUMORED_LANGUAGE.search(material_text):
        return False, "UNKNOWN", "NO_REPORTING_ATTRIBUTION"
    if not str(record.get("link") or "").strip():
        return False, "UNKNOWN", "MISSING_SOURCE_URL"
    # Explicit rumor/unconfirmed language is the conservative controlling state,
    # even when the prose also calls the underlying communication an approach.
    state = "RUMORED" if RUMORED_LANGUAGE.search(material_text) else "REPORTED"
    return True, state, "ADMITTED_MATERIAL_THIRD_PARTY_REPORT"


def _third_party_event_summary(record: Mapping[str, object]) -> str:
    """Preserve report uncertainty and the material sentence in event facts."""

    title = html.unescape(str(record.get("title") or "")).strip()
    content = html.unescape(str(record.get("content") or "")).strip()[:650]
    sentence = title if CORPORATE_ACTION.search(title) else next(
        (part.strip() for part in re.split(r"(?<=[.!?])\s+", content)
         if CORPORATE_ACTION.search(part)), title,
    )
    sentence = re.sub(r"\s+", " ", sentence)[:320].rstrip()
    state = str(record.get("_confirmation_state") or "REPORTED")
    return f"Third-party {state.lower()} information: {sentence}"


def _news_records(
    ticker: str, as_of: str, *, news_dir: Path = NEWS, forward: bool = False,
) -> tuple[list[dict[str, object]], int, dict[str, str]]:
    accepted: list[dict[str, object]] = []
    rejected = 0
    decisions: dict[str, str] = {}
    pattern = "*.jsonl" if forward else "2026-*.jsonl"
    for path in sorted((news_dir / ticker).glob(pattern)):
        if ((not forward and path.stem < "2026-04") or
                path.stem > as_of[:7]):
            continue
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                record = json.loads(line)
                published = str(record.get("published_ts") or "")[:10]
                if not published or published > as_of:
                    continue
                evidence_id = _news_evidence_id(record, ticker)
                if _issuer_release(record, ticker):
                    record["_source_class"] = "ISSUER_RELEASE"
                    record["_confirmation_state"] = "CONFIRMED"
                elif forward:
                    admitted, state, reason = _third_party_corporate_action(record, ticker)
                    decisions[evidence_id] = reason
                    if not admitted:
                        rejected += 1
                        continue
                    record["_source_class"] = "THIRD_PARTY_REPORT"
                    record["_confirmation_state"] = state
                else:
                    rejected += 1
                    continue
                record["_path"] = str(path)
                record["_line"] = line_number
                record["_line_sha256"] = _digest(line.encode("utf-8"))
                accepted.append(record)
    accepted.sort(key=lambda r: str(r["published_ts"]), reverse=True)
    return accepted, rejected, decisions


def _record_evidence(
    record: Mapping[str, object], ticker: str,
    *, source_name: str = "EODHD cached issuer wire release",
) -> dict[str, object]:
    title = html.unescape(str(record["title"]))
    content = html.unescape(str(record["content"]))
    identifier = _news_evidence_id(record, ticker)
    return {
        "source": source_name, "published_at": str(record["published_ts"])[:10],
        "identifier": identifier, "url": record.get("link"), "excerpt": content[:4500],
        "title": title, "retrieved_at": str(record.get("ingestion_ts") or "2026-09-13")[:10],
        "content_sha256": str(record["_line_sha256"]), "raw_path": f"{record['_path']}:{record['_line']}",
        "published_at_precision": "timestamp",
        "document_kind": str(record.get("_source_class") or "ISSUER_RELEASE"),
    }


def _material_8k_items(document: Mapping[str, object]) -> tuple[str, ...]:
    """Return material non-earnings items supported by a mixed-purpose 8-K."""

    if document.get("document_kind") != "SEC_8_K_EARNINGS":
        return ()
    return tuple(dict.fromkeys(re.findall(
        r"\bItem\s+(1\.01|2\.01|2\.05|5\.02|8\.01)\b",
        str(document.get("excerpt") or ""), re.I,
    )))


def _apply_material_evidence_continuity(
    required_ids: tuple[str, ...], *, ticker: str,
    records_by_id: Mapping[str, dict[str, object]],
    decisions: Mapping[str, str], chosen: list[dict[str, object]],
) -> list[dict[str, str]]:
    """Admit validated upstream evidence or record why it was excluded."""

    continuity: list[dict[str, str]] = []
    expected_prefix = f"news:{ticker}:"
    for evidence_id in dict.fromkeys(required_ids):
        if not evidence_id.startswith(expected_prefix):
            continuity.append({
                "evidence_id": evidence_id, "status": "EXCLUDED",
                "reason": (
                    "INVALID_TICKER_EVIDENCE_IDENTIFIER"
                    if evidence_id.startswith("news:")
                    else "UNSUPPORTED_CONTINUITY_EVIDENCE_CLASS"
                ),
            })
            continue
        record = records_by_id.get(evidence_id)
        if record is not None:
            if record not in chosen:
                chosen.append(record)
            continuity.append({
                "evidence_id": evidence_id, "status": "ADMITTED",
                "reason": decisions.get(evidence_id, "ADMITTED_VALID_ISSUER_RELEASE"),
            })
        else:
            continuity.append({
                "evidence_id": evidence_id, "status": "EXCLUDED",
                "reason": decisions.get(evidence_id, "NOT_PRESENT_IN_CAPTURE"),
            })
    return continuity


def _reported_period(text: str) -> str:
    months = {name: index for index, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"), 1)}
    ended = re.search(
        r"quarter ended\s+(" + "|".join(months) + r")\s+(\d{1,2}),\s*(20\d{2})",
        text[:1200], re.I,
    )
    if ended:
        return f"{ended.group(3)}-{months[ended.group(1).lower()]:02d}-{int(ended.group(2)):02d}"
    quarter = re.search(
        r"\b(first|second|third|fourth)[- ]quarter\s+(20\d{2})\b|"
        r"\b(20\d{2})\s+(first|second|third|fourth)[- ]quarter\b",
        text[:1200], re.I,
    )
    if quarter:
        name = (quarter.group(1) or quarter.group(4)).lower()
        year = quarter.group(2) or quarter.group(3)
        return f"{year}Q{('first', 'second', 'third', 'fourth').index(name) + 1}"
    return "UNSPECIFIED"


def _literal_metrics(
    text: str, evidence_id: str, period: str | None = None,
) -> list[dict[str, object]]:
    claims: list[dict[str, object]] = []
    # The opening results section carries current-period highlights. Later
    # boilerplate often repeats prior-year company-wide figures.
    for match in METRIC.finditer(text[:1700]):
        bridge = text[match.end("label"):match.start("amount")]
        if re.match(r"\s+volume\b", bridge, re.I):
            continue
        amount = match.group("amount")
        unit = "USD_" + re.search(r"billion|million|trillion", amount, re.I).group().upper() + "S"
        value = float(re.search(r"[\d,.]+", amount).group().replace(",", ""))
        excerpt = text[max(0, match.start() - 35):min(len(text), match.end() + 45)].strip()
        suffix = text[match.end():match.end() + 45]
        measurement = ("ANNUALIZED_RUN_RATE" if re.search(r"annualized|run rate", suffix, re.I)
                       else "CHANGE_AMOUNT" if re.search(r"\b(?:up|down|increased|decreased|higher|lower|by)\b", bridge, re.I)
                       else "REPORTED_AMOUNT")
        claim = {"metric_label": match.group("label").lower(), "value": value, "unit": unit,
                 "period": period or _reported_period(text),
                 "measurement_type": measurement,
                 "source_excerpt": excerpt, "evidence_identifier": evidence_id,
                 "extraction_method": "literal_regex_explicit_currency_and_scale"}
        if not any((old["metric_label"], old["value"], old["unit"]) ==
                   (claim["metric_label"], claim["value"], claim["unit"]) for old in claims):
            claims.append(claim)
        if len(claims) == 5:
            break
    return claims


def _guidance(text: str, evidence_id: str) -> dict[str, str] | None:
    match = GUIDANCE.search(text[:4500])
    if not match:
        return None
    excerpt = text[max(0, match.start() - 50):min(len(text), match.end() + 70)].strip()
    return {"source_excerpt": excerpt, "evidence_identifier": evidence_id,
            "extraction_method": "literal_guidance_language"}


def _guidance_ranges(text: str, evidence_id: str) -> list[dict[str, object]]:
    """Capture explicit per-share guidance ranges; no inferred scale or delta."""
    ranges: list[dict[str, object]] = []
    for match in re.finditer(r"\b(adjusted EPS|Core FFO)\b", text[:6500], re.I):
        context = text[max(0, match.start() - 400):match.end() + 80]
        if not re.search(r"guidance", context, re.I):
            continue
        tail = text[match.end():min(len(text), match.end() + 150)]
        found = list(GUIDANCE_RANGE.finditer(tail))[:2]
        if not found:
            found = list(GUIDANCE_TABLE_RANGE.finditer(tail))[:2]
        if not found or found[0].start() > 100:
            continue
        if re.search(r"\bby\s*$", tail[:found[0].start()], re.I):
            continue
        # Rows can contain both Previous and Current; a highlight has Current only.
        def pair(item: re.Match[str]) -> list[float]:
            return [float(item.group(1).replace(",", "")),
                    float(item.group(2).replace(",", ""))]
        if any(low > high for low, high in (pair(item) for item in found)):
            continue
        year = re.search(r"20\d{2}", context)
        if year is None:
            continue
        previous = pair(found[0]) if len(found) == 2 else None
        current = pair(found[-1])
        excerpt = text[max(0, match.start() - 45):match.end() + found[-1].end()].strip()
        metric = "ADJUSTED_EPS" if match.group(1).lower().startswith("adjusted") else "CORE_FFO"
        claim = {"metric": metric, "period": year.group(), "unit": "USD_PER_SHARE",
                 "previous_range": previous, "current_range": current,
                 "source_excerpt": excerpt, "evidence_identifier": evidence_id,
                 "extraction_method": "literal_guidance_range_explicit_usd_per_share"}
        if not any(old["metric"] == metric and old["period"] == claim["period"] for old in ranges):
            ranges.append(claim)
    return ranges


_EXPECTATION_RANGE_PATTERNS = (
    (
        "SUBSCRIPTION_REVENUE", "DOLLAR_SCALED",
        re.compile(
            r"subscription\s+revenues?(?:(?!\.).){0,120}?"
            r"\$\s*(?P<low>[\d,.]+)\s*(?P<low_scale>billion|million)\s+"
            r"(?:and|to|[-–—])\s+\$?\s*(?P<high>[\d,.]+)\s*"
            r"(?P<high_scale>billion|million)",
            re.I,
        ),
    ),
    (
        "SALES", "DOLLAR_SCALED",
        re.compile(
            r"(?:worldwide\s+)?sales(?:\s+range)?(?:(?!\.).){0,120}?"
            r"\$\s*(?P<low>[\d,.]+)\s*(?P<low_scale>billion|million)\s+"
            r"(?:and|to|[-–—])\s+\$?\s*(?P<high>[\d,.]+)\s*"
            r"(?P<high_scale>billion|million)",
            re.I,
        ),
    ),
    (
        "NON_GAAP_EPS", "DOLLAR_PER_SHARE",
        re.compile(
            r"non-gaap\s+eps(?:(?!\.).){0,120}?\$\s*(?P<low>[\d,.]+)\s+"
            r"(?:and|to|[-–—])\s+\$?\s*(?P<high>[\d,.]+)",
            re.I,
        ),
    ),
    (
        "ADJUSTED_EPS", "DOLLAR_PER_SHARE",
        re.compile(
            r"adjusted\s+eps(?:(?!\.).){0,120}?\$\s*(?P<low>[\d,.]+)\s*"
            r"(?:and|to|[-–—])\s*\$?\s*(?P<high>[\d,.]+)",
            re.I,
        ),
    ),
    (
        "CORE_FFO", "DOLLAR_PER_SHARE",
        re.compile(
            r"core\s+ffo(?:(?!\.).){0,160}?\$\s*(?P<low>[\d,.]+)\s*"
            r"(?:and|to|[-–—])\s*\$?\s*(?P<high>[\d,.]+)",
            re.I,
        ),
    ),
)

_FORWARD_EXPECTATION_INTENT = re.compile(
    r"\b(?:expects?|expected|outlook|guidance|forecasts?|projects?|anticipates?|"
    r"raises?|lowers?|narrows?|reaffirms?|now\s+expects?)\b",
    re.I,
)
_QUARTERS = {"first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4"}

_EXPECTATION_POINT_PATTERNS = (
    (
        "NON_GAAP_OPERATING_MARGIN", "PERCENT",
        re.compile(
            r"non-gaap\s+operating\s+margin(?:\s+guidance)?(?:(?!\.).){0,80}?"
            r"(?P<value>[\d,.]+)\s*%",
            re.I,
        ),
    ),
)


def _expectation_period(text: str, position: int) -> tuple[str, str] | None:
    context = text[max(0, position - 500):position]
    candidates: list[tuple[int, str, str]] = []
    annual_patterns = (
        r"full[- ]year\s+(?:fiscal\s+)?(?P<year>20\d{2})",
        r"fiscal\s+(?P<year>20\d{2}).{0,45}?\bfull\s+year\b",
    )
    quarter_patterns = (
        r"fiscal\s+(?P<year>20\d{2})\s+(?P<quarter>first|second|third|fourth|q[1-4])(?:[- ]quarter)?",
        r"(?P<quarter>first|second|third|fourth|q[1-4])[- ]quarter(?:\s+of)?\s+fiscal\s+(?P<year>20\d{2})",
        r"(?P<quarter>q[1-4])\s+(?:fiscal\s+)?(?P<year>20\d{2})",
    )
    for pattern in annual_patterns:
        for match in re.finditer(pattern, context, re.I):
            candidates.append((match.start(), f"FY{match.group('year')}", "ANNUAL"))
    for pattern in quarter_patterns:
        for match in re.finditer(pattern, context, re.I):
            quarter = match.group("quarter").lower()
            quarter = quarter.upper() if quarter.startswith("q") else _QUARTERS[quarter]
            candidates.append((match.start(), f"{match.group('year')}{quarter}", "QUARTERLY"))
    if not candidates:
        fiscal_years = list(re.finditer(r"\bfiscal\s+(20\d{2})\b", context, re.I))
        if not fiscal_years:
            return None
        return fiscal_years[-1].group(1), "UNKNOWN"
    _, target, granularity = max(candidates, key=lambda item: item[0])
    return target, granularity


def _expectation_basis(segment: str) -> str:
    if re.search(
        r"(?:outlook|guidance).{0,100}?(?:does\s+not\s+reflect|excludes?|excluding)"
        r".{0,120}?(?:acquisition|transaction)",
        segment, re.I,
    ):
        return "EXCLUDES_TRANSACTION_IMPACT"
    if re.search(
        r"(?:outlook|guidance).{0,100}?(?:includes?|including)"
        r".{0,120}?(?:acquisition|transaction)",
        segment, re.I,
    ):
        return "INCLUDES_TRANSACTION_IMPACT"
    if re.search(r"\bdiscontinued\s+operations\b", segment, re.I):
        return "DISCONTINUED_OPERATIONS"
    if re.search(r"\bcontinuing\s+operations\b", segment, re.I):
        return "CONTINUING_OPERATIONS"
    if re.search(r"\borganic\s+basis\b|\borganic\s+(?:sales|revenue|growth)\b", segment, re.I):
        return "ORGANIC_BASIS"
    if re.search(r"\breported\s+basis\b", segment, re.I):
        return "REPORTED_BASIS"
    if re.search(r"\bsegment\s+(?:sales|revenue|guidance|outlook)\b", segment, re.I):
        return "SEGMENT_SPECIFIC"
    if re.search(r"\bworldwide\s+sales\b", segment, re.I):
        return "WORLDWIDE"
    if re.search(r"\bconsolidated\b", segment, re.I):
        return "CONSOLIDATED"
    return "UNSPECIFIED"


def _decimal_text(value: str) -> float:
    return float(value.rstrip(".").replace(",", ""))


def _has_forward_expectation_intent(text: str, start: int, end: int) -> bool:
    return bool(_FORWARD_EXPECTATION_INTENT.search(text[max(0, start - 350):end + 80]))


def _candidate_role(text: str, start: int, end: int) -> str:
    context = text[max(0, start - 100):end + 30]
    if re.search(r"\b(?:previously|prior\s+(?:range|guidance|outlook)|formerly)\b", context, re.I):
        return "PRIOR_REFERENCE"
    if re.search(
        r"\b(?:now|current|updated|updating|raises?|lowers?|narrows?|reaffirms?)\b",
        context, re.I,
    ):
        return "CURRENT"
    return "UNSPECIFIED"


def _literal_expectation_ranges(text: str) -> list[dict[str, object]]:
    """Extract explicit expectation ranges without inferring a period, unit, or perimeter."""

    normalized = re.sub(r"\s+", " ", text)
    found: list[dict[str, object]] = []
    for metric, unit_kind, pattern in _EXPECTATION_RANGE_PATTERNS:
        for match in pattern.finditer(normalized):
            if not _has_forward_expectation_intent(normalized, match.start(), match.end()):
                continue
            period = _expectation_period(normalized, match.start())
            if period is None:
                continue
            target_period, granularity = period
            unit = unit_kind
            if unit_kind == "DOLLAR_SCALED":
                low_scale = match.group("low_scale").upper()
                high_scale = match.group("high_scale").upper()
                if low_scale != high_scale:
                    continue
                unit = f"DOLLAR_{low_scale}S"
            found.append({
                "metric": metric,
                "target_fiscal_period": target_period,
                "period_granularity": granularity,
                "unit": unit,
                "value": [_decimal_text(match.group("low")), _decimal_text(match.group("high"))],
                "origin": "RAW_TEXT",
                "candidate_role": _candidate_role(normalized, match.start(), match.end()),
                "start": match.start(),
                "end": match.end(),
            })
    for metric, unit, pattern in _EXPECTATION_POINT_PATTERNS:
        for match in pattern.finditer(normalized):
            if not _has_forward_expectation_intent(normalized, match.start(), match.end()):
                continue
            period = _expectation_period(normalized, match.start())
            if period is None:
                continue
            target_period, granularity = period
            found.append({
                "metric": metric,
                "target_fiscal_period": target_period,
                "period_granularity": granularity,
                "unit": unit,
                "value": [_decimal_text(match.group("value"))],
                "origin": "RAW_TEXT",
                "candidate_role": _candidate_role(normalized, match.start(), match.end()),
                "start": match.start(),
                "end": match.end(),
            })
    found.sort(key=lambda item: (int(item["start"]), str(item["metric"])))
    for index, item in enumerate(found):
        segment_end = int(found[index + 1]["start"]) if index + 1 < len(found) else min(
            len(normalized), int(item["end"]) + 500
        )
        item["basis"] = _expectation_basis(
            normalized[max(0, int(item["start"]) - 140):segment_end]
        )
        item["source_start"] = item.pop("start")
        item["source_end"] = item.pop("end")
    return found


def _structured_period(item: Mapping[str, object]) -> tuple[str, str]:
    raw = str(item.get("period") or "")
    if match := re.fullmatch(r"FY(20\d{2})", raw, re.I):
        return f"FY{match.group(1)}", "ANNUAL"
    if match := re.fullmatch(r"(20\d{2})Q([1-4])", raw, re.I):
        return f"{match.group(1)}Q{match.group(2)}", "QUARTERLY"
    if match := re.fullmatch(r"Q([1-4])\s+(20\d{2})", raw, re.I):
        return f"{match.group(2)}Q{match.group(1)}", "QUARTERLY"
    excerpt = str(item.get("source_excerpt") or "")
    if re.fullmatch(r"20\d{2}", raw):
        if re.search(r"\b(?:full[- ]year|annual)\b", excerpt, re.I):
            return f"FY{raw}", "ANNUAL"
        quarter = re.search(r"\b(?:q([1-4])|(first|second|third|fourth)[- ]quarter)\b", excerpt, re.I)
        if quarter:
            number = quarter.group(1) or _QUARTERS[quarter.group(2).lower()][1]
            return f"{raw}Q{number}", "QUARTERLY"
    return raw, "UNKNOWN"


def _unit_currency(unit: str) -> str:
    if unit == "PERCENT":
        return "NONE"
    match = re.match(r"^(USD|EUR|GBP|CAD|AUD)_", unit)
    return match.group(1) if match else "UNKNOWN"


def _unique_excerpt_span(text: str, excerpt: str) -> tuple[int | None, int | None]:
    normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip()
    if not normalized_excerpt or text.count(normalized_excerpt) != 1:
        return None, None
    start = text.index(normalized_excerpt)
    return start, start + len(normalized_excerpt)


def _suppress_dominated_unknown_candidates(
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Drop only UNKNOWN candidates textually contained by the same specific observation."""

    result: list[dict[str, object]] = []
    for candidate in candidates:
        if candidate["period_granularity"] != "UNKNOWN":
            result.append(candidate)
            continue
        start = candidate.get("source_start")
        end = candidate.get("source_end")
        dominated = any(
            specific["period_granularity"] != "UNKNOWN"
            and specific["metric"] == candidate["metric"]
            and specific["unit"] == candidate["unit"]
            and specific["currency"] == candidate["currency"]
            and specific["value"] == candidate["value"]
            and isinstance(start, int) and isinstance(end, int)
            and isinstance(specific.get("source_start"), int)
            and isinstance(specific.get("source_end"), int)
            and max(start, int(specific["source_start"]))
            < min(end, int(specific["source_end"]))
            for specific in candidates
        )
        if not dominated:
            result.append(candidate)
    return result


def _expectation_direction(previous: list[float], current: list[float]) -> str | None:
    if len(previous) != len(current) or len(current) not in {1, 2}:
        return None
    if current == previous:
        return "UNCHANGED"
    if len(current) == 1:
        return "RAISED" if current[0] > previous[0] else "LOWERED"
    if current[0] >= previous[0] and current[-1] >= previous[-1]:
        return "RAISED"
    if current[0] <= previous[0] and current[-1] <= previous[-1]:
        return "LOWERED"
    return "MIXED"


def _is_primary_expectation_event(event: Mapping[str, object]) -> bool:
    facts = event.get("facts")
    if not isinstance(facts, Mapping):
        return False
    return (
        facts.get("confirmation_state") == "CONFIRMED"
        and facts.get("source_class") in {"PRIMARY_SEC", "ISSUER_RELEASE"}
    )


def _expectation_observations(
    events: list[dict[str, object]], evidence: list[dict[str, object]], issuer: str,
    provider_symbol: str,
) -> list[dict[str, object]]:
    evidence_text = {
        str(item.get("identifier")): str(item.get("excerpt") or "") for item in evidence
    }
    observations: list[dict[str, object]] = []
    for event in events:
        facts = event["facts"]
        evidence_id = str(facts.get("evidence_identifier") or "")
        normalized_text = re.sub(r"\s+", " ", evidence_text.get(evidence_id, ""))
        candidates = _literal_expectation_ranges(normalized_text)
        for item in facts.get("guidance_ranges", []):
            target_period, granularity = _structured_period(item)
            source_start, source_end = _unique_excerpt_span(
                normalized_text, str(item.get("source_excerpt") or ""),
            )
            candidates.append({
                "metric": item["metric"], "target_fiscal_period": target_period,
                "period_granularity": granularity,
                "unit": item["unit"], "value": list(item["current_range"]),
                "basis": _expectation_basis(str(item.get("source_excerpt") or "")),
                "origin": "STRUCTURED_GUIDANCE",
                "candidate_role": "CURRENT",
                "source_start": source_start,
                "source_end": source_end,
            })
        primary = _is_primary_expectation_event(event)
        for item in candidates:
            currency = _unit_currency(str(item["unit"]))
            if str(item["unit"]).startswith("DOLLAR_"):
                currency = "USD" if primary and provider_symbol.upper().endswith(".US") else "UNKNOWN"
                if currency == "USD":
                    item["unit"] = str(item["unit"]).replace("DOLLAR_", "USD_", 1)
            item["currency"] = currency
        candidates = _suppress_dominated_unknown_candidates(candidates)
        grouped: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
        for item in candidates:
            key = (
                str(item["metric"]), str(item["target_fiscal_period"]),
                str(item["period_granularity"]), str(item["unit"]),
            )
            grouped.setdefault(key, []).append(item)
        for group in grouped.values():
            unique_values = {json.dumps(item["value"]) for item in group}
            current_items = [item for item in group if item["candidate_role"] == "CURRENT"]
            if len(unique_values) == 1:
                item = next((value for value in group if value["origin"] == "RAW_TEXT"), group[0])
                candidate_ambiguity = False
            elif len(current_items) == 1 and all(
                value is current_items[0] or value["candidate_role"] == "PRIOR_REFERENCE"
                for value in group
            ):
                item = current_items[0]
                candidate_ambiguity = False
            else:
                item = dict(group[0])
                item["value"] = None
                candidate_ambiguity = True
            observations.append({
                **item,
                "issuer": issuer,
                "evidence_id": evidence_id,
                "published_at": str(event["published_at"]),
                "primary": primary,
                "currency": item["currency"],
                "candidate_ambiguity": candidate_ambiguity,
            })
    return observations


def _comparison_record(
    current: Mapping[str, object], prior: Mapping[str, object] | None,
) -> dict[str, object]:
    common = {
        "current_evidence_id": current["evidence_id"],
        "prior_evidence_id": prior["evidence_id"] if prior else None,
        "issuer": current["issuer"],
        "metric": current["metric"],
        "target_fiscal_period": current["target_fiscal_period"],
        "prior_target_fiscal_period": prior["target_fiscal_period"] if prior else None,
        "target_period_granularity": current["period_granularity"],
        "prior_target_period_granularity": prior["period_granularity"] if prior else None,
        "unit": current["unit"],
        "prior_unit": prior["unit"] if prior else None,
        "currency": current["currency"],
        "prior_currency": prior["currency"] if prior else None,
        "current_value": current["value"],
        "prior_value": prior["value"] if prior else None,
        "current_basis": current["basis"],
        "prior_basis": prior["basis"] if prior else None,
    }
    if current["candidate_ambiguity"]:
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": ["MULTIPLE_CURRENT_VALUES"]}
    if not current["primary"]:
        return {**common, "expectation_comparison_state": "NOT_ESTABLISHED",
                "direction": None,
                "comparability_reason_codes": ["CURRENT_EVIDENCE_NOT_PRIMARY"]}
    if current["period_granularity"] == "UNKNOWN":
        return {**common, "expectation_comparison_state": "NOT_ESTABLISHED",
                "direction": None,
                "comparability_reason_codes": ["PERIOD_GRANULARITY_NOT_ESTABLISHED"]}
    if prior is None:
        return {**common, "expectation_comparison_state": "NOT_ESTABLISHED",
                "direction": None,
                "comparability_reason_codes": ["NO_PRIOR_PRIMARY_BASELINE"]}
    if prior["period_granularity"] == "UNKNOWN":
        return {**common, "expectation_comparison_state": "NOT_ESTABLISHED",
                "direction": None,
                "comparability_reason_codes": ["PRIOR_PERIOD_GRANULARITY_NOT_ESTABLISHED"]}
    if current["period_granularity"] != prior["period_granularity"]:
        return {**common, "expectation_comparison_state": "NOT_ESTABLISHED",
                "direction": None,
                "comparability_reason_codes": ["PERIOD_GRANULARITY_MISMATCH"]}
    if current["target_fiscal_period"] != prior["target_fiscal_period"]:
        return {**common, "expectation_comparison_state": "NOT_ESTABLISHED",
                "direction": None,
                "comparability_reason_codes": ["TARGET_FISCAL_PERIOD_MISMATCH"]}
    if current["currency"] == "UNKNOWN" or prior["currency"] == "UNKNOWN":
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": ["CURRENCY_NOT_ESTABLISHED"]}
    if current["currency"] != prior["currency"]:
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": ["CURRENCY_MISMATCH"]}
    if current["unit"] != prior["unit"]:
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": ["INCOMPATIBLE_UNITS"]}
    if current["basis"] == "UNSPECIFIED" or prior["basis"] == "UNSPECIFIED":
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": ["BASIS_NOT_ESTABLISHED"]}
    if "SEGMENT_SPECIFIC" in {current["basis"], prior["basis"]}:
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": ["SEGMENT_SCOPE_NOT_ESTABLISHED"]}
    if current["basis"] != prior["basis"]:
        reason = (
            "PERIMETER_BASIS_MISMATCH"
            if {current["basis"], prior["basis"]} <= {
                "INCLUDES_TRANSACTION_IMPACT", "EXCLUDES_TRANSACTION_IMPACT",
            }
            else "ECONOMIC_BASIS_MISMATCH"
        )
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": [reason]}
    direction = _expectation_direction(list(prior["value"]), list(current["value"]))
    if direction is None:
        return {**common, "expectation_comparison_state": "AMBIGUOUS",
                "direction": None,
                "comparability_reason_codes": ["VALUE_SHAPE_MISMATCH"]}
    state = "VERIFIED_UNCHANGED" if direction == "UNCHANGED" else "VERIFIED_CHANGE"
    return {
        **common,
        "expectation_comparison_state": state,
        "direction": direction,
        "comparability_reason_codes": [
            "CURRENT_PRIMARY", "PRIOR_PRIMARY", "SAME_ISSUER", "SAME_METRIC",
            "SAME_TARGET_FISCAL_PERIOD", "SAME_PERIOD_GRANULARITY",
            "COMPATIBLE_CURRENCY", "COMPATIBLE_UNITS", "COMPARABLE_PERIMETER",
            "EXPLICIT_VALUE_UNCHANGED" if direction == "UNCHANGED" else "EXPLICIT_VALUE_CHANGE",
        ],
    }


def with_expectation_comparisons(
    company_input: Mapping[str, object],
) -> dict[str, object]:
    """Return a JSON copy with deterministic Facts V3 expectation comparisons attached."""

    normalized = json.loads(json.dumps(company_input, ensure_ascii=False, allow_nan=False))
    events = normalized.get("events", [])
    evidence = normalized.get("evidence", [])
    issuer = str(normalized.get("ticker") or "")
    provider_symbol = str(normalized.get("provider_symbol") or "")
    observations = _expectation_observations(events, evidence, issuer, provider_symbol)
    by_event = {str(event["facts"].get("evidence_identifier")): event for event in events}
    for event in events:
        event["facts"]["expectation_comparisons"] = []
    ordered = sorted(
        observations,
        key=lambda item: (str(item["published_at"]), str(item["evidence_id"]), str(item["metric"])),
    )
    for current in ordered:
        prior_candidates = [
            item for item in ordered
            if item["primary"] and item["metric"] == current["metric"]
            and str(item["published_at"]) < str(current["published_at"])
        ]
        exact = [item for item in prior_candidates
                 if item["target_fiscal_period"] == current["target_fiscal_period"]
                 and item["unit"] == current["unit"]]
        same_period = [item for item in prior_candidates
                       if item["target_fiscal_period"] == current["target_fiscal_period"]]
        pool = exact or same_period or prior_candidates
        prior = max(pool, key=lambda item: (str(item["published_at"]), str(item["evidence_id"]))) if pool else None
        by_event[str(current["evidence_id"])]["facts"]["expectation_comparisons"].append(
            _comparison_record(current, prior)
        )
    return normalized


def _numeric_audit(facts: Mapping[str, object], draft: Mapping[str, object]) -> list[str]:
    """Reject draft numbers absent from the supplied source text or price block."""
    evidence_by_id = {str(item["identifier"]): str(item.get("excerpt") or "")
                      for item in facts["evidence"]}
    price = facts["price"]
    price_text = " ".join(str(price.get(key)) for key in
                          ("current_price", "return_5d", "return_20d", "return_60d"))
    number = re.compile(r"(?<![\w])\d[\d,.]*", re.I)
    quarter = re.compile(r"\b[1-4]\s*[°º]?\s*(?:trimestre|quarter)\b", re.I)
    spelled = {word: str(index) for index, word in enumerate(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"))}
    def source_numbers(source: str) -> set[str]:
        values = {canonical(token) for token in number.findall(source)}
        values.update(digit for word, digit in spelled.items()
                      if re.search(rf"\b{word}\b", source, re.I))
        return values
    def canonical(token: str) -> str:
        raw = re.sub(r"(?:st|nd|rd|th|%)$", "", token, flags=re.I)
        if "." in raw and "," in raw:
            decimal_separator = "." if raw.rfind(".") > raw.rfind(",") else ","
            raw = raw.replace("," if decimal_separator == "." else ".", "")
            raw = raw.replace(decimal_separator, ".")
        elif raw.count(".") > 1 or raw.count(",") > 1:
            raw = raw.replace(".", "").replace(",", "")
        else:
            raw = raw.replace(",", ".")
        return str(Decimal(raw).normalize())
    citation = re.compile(r"\[([^\]]+)\]")
    failures: list[str] = []
    for field in ("fundamental_analysis", "earnings_and_news_analysis", "price_context",
                  "bull_case", "bear_case", "thesis_invalidation"):
        statement = str(draft.get(field) or "")
        ids = citation.findall(statement)
        supplied = " ".join(evidence_by_id.get(item, "") for item in ids)
        if field == "price_context":
            supplied += " " + price_text
        allowed = source_numbers(supplied)
        if field == "price_context":
            allowed.update({"5", "20", "60"})
        if field == "thesis_invalidation":
            # Zero is a prospective break-even threshold, not an observed fact.
            allowed.add("0")
        without_citations = quarter.sub("", citation.sub("", statement))
        unsupported = sorted({canonical(token) for token in number.findall(without_citations)} - allowed)
        if unsupported:
            failures.append(f"{field}: numeric tokens without cited source {unsupported}")
    for field in ("catalysts", "risks"):
        for index, statement in enumerate(draft.get(field) or []):
            ids = citation.findall(statement)
            supplied = " ".join(evidence_by_id.get(item, "") for item in ids)
            allowed = source_numbers(supplied)
            unsupported = sorted({canonical(token) for token in number.findall(quarter.sub("", citation.sub("", statement)))} - allowed)
            if unsupported:
                failures.append(f"{field}[{index}]: numeric tokens without cited source {unsupported}")
    return failures


@dataclass(frozen=True)
class ClaimValidation:
    revised_analysis: dict[str, object]
    valid_claim_refs: list[dict[str, object]]
    rejected_claim_refs: list[dict[str, object]]
    essential_support_removed: bool


_QUANTIFIED_NUMBER = re.compile(
    r"(?<![\w])[-+âˆ’â€“â€”]?(?:\d{1,3}(?:[.,]\d{3})+|\d+)(?:[.,]\d+)?(?![\w])"
)
_CITATION = re.compile(r"\[[^\]]+\]")


def _fact_catalog(facts: Mapping[str, object]) -> dict[str, dict[str, object]]:
    catalog: dict[str, dict[str, object]] = {}
    price = facts["price"]
    for metadata in price.get("acquisition", {}).get("fact_metadata", []):
        item = dict(metadata)
        item["value"] = price[item["metric"]]
        catalog[str(item["fact_id"])] = item
    for event in facts["events"]:
        for value in event["facts"].values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, Mapping) and item.get("fact_id"):
                    catalog[str(item["fact_id"])] = dict(item)
    return catalog


def _decimal_token(raw: str, *, scaled: bool) -> Decimal:
    token = raw.strip().replace("+", "")
    sign = "-" if token[:1] in {"-", "âˆ’", "â€“", "â€”"} else ""
    token = token.lstrip("-âˆ’â€“â€”")
    if "." in token and "," in token:
        decimal_separator = "." if token.rfind(".") > token.rfind(",") else ","
        token = token.replace("," if decimal_separator == "." else ".", "")
        token = token.replace(decimal_separator, ".")
    elif token.count(".") + token.count(",") > 1:
        token = token.replace(".", "").replace(",", "")
    elif "." in token or "," in token:
        separator = "." if "." in token else ","
        left, right = token.split(separator)
        if scaled or len(right) != 3:
            token = f"{left}.{right}"
        else:
            token = left + right
    return Decimal(sign + token)


def _numeric_mentions(text: str) -> list[dict[str, object]]:
    clean = _CITATION.sub("", text)
    clean = re.sub(r"\b20\d{2}-\d{2}-\d{2}\b", "", clean)
    clean = re.sub(
        r"\b\d+\s*,\s*\d+\s+(?:e|and)\s+\d+\s+(?:giorni|sedute|days?)\b",
        "", clean, flags=re.I,
    )
    negative_context = bool(re.search(
        r"\b(?:ha perso|perdita|negativ[oa]|calo|ribasso|diminuzione|decline|declined|negative|loss|lost)\b",
        clean, re.I,
    ))
    mentions: list[dict[str, object]] = []
    for match in _QUANTIFIED_NUMBER.finditer(clean):
        raw = match.group()
        before = clean[max(0, match.start() - 28):match.start()].casefold()
        after = clean[match.end():match.end() + 32].casefold()
        context = before + raw.casefold() + after
        if re.search(r"\b(?:exhibit|allegato)\s*$", before, re.I):
            continue
        unsigned = raw.lstrip("+-\u2212\u2013\u2014")
        if re.search(rf"\b{re.escape(unsigned)}\s*[- ]?[qk]\b", context, re.I):
            continue
        if re.search(rf"\b(?:q|quarter|trimestre)\s*{re.escape(unsigned)}\b", context, re.I):
            continue
        if re.match(r"\s*(?:Â°|Âº|°|º|st|nd|rd|th)?\s*(?:trimestre|quarter)\b", after, re.I):
            continue
        if re.match(
            r"\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|"
            r"ottobre|novembre|dicembre|january|february|march|april|may|june|july|august|"
            r"september|october|november|december)\b", after, re.I,
        ):
            continue
        if re.match(r"\s*(?:giorni|sedute|days?)\b", after, re.I):
            continue
        if unsigned.isdigit() and 1900 <= int(unsigned) <= 2100:
            continue
        scale = 1
        if re.search(r"\b(?:trillion|trilioni?)\b", context):
            scale = 1_000_000_000_000
        elif re.search(r"\b(?:billion|miliard[oi])\b", context):
            scale = 1_000_000_000
        elif re.search(r"\b(?:million|milion[ei])\b", context):
            scale = 1_000_000
        elif re.search(r"\b(?:thousand|migliaia)\b", context):
            scale = 1_000
        unit = None
        if "%" in after[:4] or re.search(r"\bpercent", context):
            unit = "PERCENT"
        elif re.search(r"\b(?:per share|per azione)\b", context):
            unit = "USD_PER_SHARE"
        elif scale != 1 or re.search(r"(?:\$|\busd\b|\bdollar)", context):
            unit = "USD"
        try:
            value = _decimal_token(raw, scaled=scale != 1 or unit in {"PERCENT", "USD_PER_SHARE"})
        except InvalidOperation:
            continue
        precision = max(0, -value.as_tuple().exponent)
        mentions.append({"raw": raw, "value": value, "scale": scale,
                         "unit": unit, "precision": precision,
                         "negative_context": negative_context})
    return mentions


def _fact_values(fact: Mapping[str, object]) -> list[Decimal]:
    value = fact.get("value")
    values = value if isinstance(value, list) else [value]
    result: list[Decimal] = []
    for item in values:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result.append(Decimal(str(item)))
    return result


def _presentation_matches(mention: Mapping[str, object], fact: Mapping[str, object]) -> bool:
    unit = str(fact.get("unit") or "")
    claim_unit = mention["unit"]
    if claim_unit == "PERCENT" and unit != "PERCENT":
        return False
    if claim_unit == "USD_PER_SHARE" and unit not in {"USD_PER_SHARE", "USD/shares"}:
        return False
    if claim_unit == "USD" and not unit.startswith("USD"):
        return False
    scale = Decimal(str(fact.get("scale") or 1))
    display_scale = Decimal(str(mention["scale"]))
    precision = int(mention["precision"])
    quantum = Decimal(1).scaleb(-precision)
    for fact_value in _fact_values(fact):
        base_value = fact_value * scale
        displayed = base_value / display_scale
        if displayed.quantize(quantum, rounding=ROUND_HALF_UP) == mention["value"]:
            return True
        if (mention.get("negative_context") and displayed < 0 and mention["value"] >= 0 and
                abs(displayed).quantize(quantum, rounding=ROUND_HALF_UP) == mention["value"]):
            return True
    return False


def _field_texts(analysis: Mapping[str, object], field: str) -> list[str]:
    value = analysis.get(field, [] if field in {"catalysts", "risks"} else "")
    return [str(item) for item in value] if isinstance(value, list) else [str(value)]


def _period_matches(declared: object, fact_period: object) -> bool:
    claim = str(declared).strip()
    period = str(fact_period).strip()
    if claim == period or claim == _period_key(period):
        return True
    if "/" in claim and period == claim.rsplit("/", 1)[-1]:
        return True
    quarter_names = {"first": 1, "second": 2, "third": 3, "fourth": 4,
                     "primo": 1, "secondo": 2, "terzo": 3, "quarto": 4}
    match = re.search(
        r"\b(first|second|third|fourth|primo|secondo|terzo|quarto)[- ](?:quarter|trimestre)\s+(20\d{2})\b",
        claim, re.I,
    )
    if match and period == f"{match.group(2)}Q{quarter_names[match.group(1).lower()]}":
        return True
    return False


def _remove_claim_text(analysis: dict[str, object], field: str, text: str) -> None:
    if field in {"catalysts", "risks"}:
        updated = []
        for item in analysis.get(field, []):
            cleaned = str(item).replace(text, "").strip(" ;,.-")
            if cleaned:
                updated.append(cleaned)
        analysis[field] = updated
        return
    cleaned = str(analysis.get(field, "")).replace(text, "").strip()
    analysis[field] = cleaned or "Claim quantitativo rimosso per provenance insufficiente."


def validate_claim_refs(
    facts: Mapping[str, object], analysis: Mapping[str, object],
) -> ClaimValidation:
    """Validate CLAIM -> FACT_ID -> EVIDENCE_ID and remediate only invalid claims."""
    revised = {field: json.loads(json.dumps(analysis[field])) for field in REVISION_FIELDS}
    catalog = _fact_catalog(facts)
    ticker = str(facts["identity"]["ticker"])
    evidence_ids = {str(item["identifier"]) for item in facts["evidence"]}
    supplied_refs = [dict(item) for item in analysis.get("claim_refs", [])]
    valid: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for claim in supplied_refs:
        errors: list[str] = []
        claim_id = str(claim.get("claim_id") or "")
        field = str(claim.get("field") or "")
        text = str(claim.get("text") or "")
        fact_ids = [str(item) for item in claim.get("fact_ids", [])]
        materiality = str(claim.get("materiality") or "MATERIAL")
        if not claim_id or claim_id in seen_ids:
            errors.append("INVALID_CLAIM_ID")
        seen_ids.add(claim_id)
        if field not in CLAIM_FIELDS or not text or not any(text in item for item in _field_texts(analysis, field)):
            errors.append("CLAIM_TEXT_NOT_FOUND")
        linked: list[dict[str, object]] = []
        for fact_id in fact_ids:
            fact = catalog.get(fact_id)
            if fact is None:
                errors.append("UNKNOWN_FACT_ID")
                continue
            linked.append(fact)
            if str(fact.get("ticker")) != ticker or not fact_id.startswith(f"fact:{ticker}:"):
                errors.append("FOREIGN_COMPANY_FACT")
            if str(fact.get("evidence_id") or "") not in evidence_ids:
                errors.append("UNKNOWN_EVIDENCE_ID")
        declared_period = claim.get("period")
        if declared_period is not None and linked and not any(
            _period_matches(declared_period, fact.get("period"))
            for fact in linked
        ):
            errors.append("PERIOD_MISMATCH")
        mentions = _numeric_mentions(text)
        if mentions and not fact_ids:
            errors.append("MISSING_FACT_ID")
        for mention in mentions:
            if not any(_presentation_matches(mention, fact) for fact in linked):
                errors.append("UNREGISTERED_DERIVATION_OR_VALUE")
        record = {**claim, "validation_errors": sorted(set(errors))}
        if errors:
            rejected.append(record)
        else:
            valid.append({**claim, "validation_errors": []})

    referenced_text = [str(item.get("text") or "") for item in supplied_refs]
    for field in REVISION_FIELDS:
        for index, item in enumerate(_field_texts(analysis, field)):
            candidates = [part.strip() for part in re.split(r"(?<=[.!?])\s+", item) if part.strip()]
            for candidate in candidates:
                if not _numeric_mentions(candidate):
                    continue
                if any(candidate in text or text in candidate for text in referenced_text if text):
                    continue
                rejected.append({
                    "claim_id": f"claim:{ticker}:{field}:unbound:{index}", "field": field,
                    "text": candidate, "fact_ids": [], "materiality": "MATERIAL",
                    "period": None, "validation_errors": ["UNBOUND_NUMERIC_CLAIM"],
                })

    for claim in rejected:
        field = str(claim.get("field") or "")
        if field in CLAIM_FIELDS:
            _remove_claim_text(revised, field, str(claim.get("text") or ""))
    valid_material = [item for item in valid if item.get("materiality") == "MATERIAL"]
    material_rejected = any(item.get("materiality") == "MATERIAL" for item in rejected)
    remaining_fact_ids = {fact_id for item in valid_material for fact_id in item.get("fact_ids", [])}
    essential_removed = material_rejected and (
        len(valid_material) < 2 or len(remaining_fact_ids) < 2
    )
    revised["claim_refs"] = valid
    return ClaimValidation(revised, valid, rejected, essential_removed)


def calibrate_decision(
    event_assessments: list[Mapping[str, object]], thesis_strength: str,
) -> str:
    """Map audited event impact to action status without using evidence quality."""
    if thesis_strength not in DECISION_LEVELS:
        raise ValueError(f"invalid thesis_strength: {thesis_strength}")
    material_classes: set[str] = set()
    for index, assessment in enumerate(event_assessments):
        classification = str(assessment.get("classification") or "")
        if classification not in EVENT_CLASSES:
            raise ValueError(f"event_assessments[{index}]: invalid classification")
        if not isinstance(assessment.get("material"), bool):
            raise ValueError(f"event_assessments[{index}]: material must be boolean")
        if assessment["material"]:
            material_classes.add(classification)
    if "EXPECTATION_CHANGE" in material_classes:
        return "INVESTIGATE"
    if "NEW_INFORMATION" in material_classes:
        return "INVESTIGATE" if thesis_strength == "HIGH" else "WATCH"
    return "PASS"


def _event_id(event: Mapping[str, object]) -> str:
    event_facts = event.get("facts")
    if isinstance(event_facts, Mapping):
        direct = event_facts.get("evidence_identifier")
        if isinstance(direct, str) and direct:
            return direct
        for value in event_facts.values():
            if isinstance(value, list):
                for fact in value:
                    if isinstance(fact, Mapping):
                        identifier = fact.get("evidence_id") or fact.get("evidence_identifier")
                        if isinstance(identifier, str) and identifier:
                            return identifier
    return "event:" + _slug(
        f"{event.get('published_at', '')}:{event.get('kind', '')}:{event.get('summary', '')}"
    )


def _validated_event_assessments(
    facts: Mapping[str, object], assessments: object,
) -> list[dict[str, object]]:
    if not isinstance(assessments, list):
        raise ValueError("event_assessments must be a list")
    expected = [_event_id(event) for event in facts.get("events", [])]
    normalized: list[dict[str, object]] = []
    observed: list[str] = []
    for index, raw in enumerate(assessments):
        if not isinstance(raw, Mapping):
            raise ValueError(f"event_assessments[{index}] must be an object")
        event_id = str(raw.get("event_id") or "")
        classification = str(raw.get("classification") or "")
        rationale = str(raw.get("rationale") or "").strip()
        material = raw.get("material")
        if event_id not in expected or event_id in observed:
            raise ValueError(f"event_assessments[{index}]: unknown or duplicate event_id")
        if classification not in EVENT_CLASSES:
            raise ValueError(f"event_assessments[{index}]: invalid classification")
        if not isinstance(material, bool) or not rationale:
            raise ValueError(f"event_assessments[{index}]: material/rationale invalid")
        observed.append(event_id)
        normalized.append({"event_id": event_id, "classification": classification,
                           "material": material, "rationale": rationale})
    if sorted(observed) != sorted(expected):
        raise ValueError("event_assessments must classify every facts.events entry exactly once")
    return normalized


def _evidence_confidence_ceiling(facts: Mapping[str, object]) -> str:
    evidence = facts.get("evidence", [])
    if not isinstance(evidence, list) or not evidence:
        return "LOW"
    evidence_ids = {
        str(item.get("identifier")) for item in evidence
        if isinstance(item, Mapping) and item.get("identifier")
    }
    catalog = _fact_catalog(facts)
    financial_facts = [
        fact for fact in catalog.values()
        if str(fact.get("metric") or "") not in {
            "current_price", "return_5d", "return_20d", "return_60d"
        }
    ]
    provenance_complete = bool(financial_facts) and all(
        str(fact.get("evidence_id") or "") in evidence_ids
        and bool(fact.get("unit")) and bool(fact.get("period"))
        and bool(fact.get("extraction_method"))
        for fact in financial_facts
    )
    kinds = {
        str(item.get("document_kind") or "") for item in evidence
        if isinstance(item, Mapping)
    }
    result_periods = {
        str(event.get("published_at") or "") for event in facts.get("events", [])
        if isinstance(event, Mapping) and event.get("kind") == "RESULTS"
    }
    if (provenance_complete and "SEC_PERIODIC_REPORT" in kinds
            and len(result_periods) >= 2):
        return "HIGH"
    if provenance_complete and any(kind.startswith("SEC_") or kind == "ISSUER_RELEASE"
                                   for kind in kinds):
        return "MEDIUM"
    return "LOW"


def _bounded_evidence_confidence(facts: Mapping[str, object], requested: str) -> str:
    if requested not in DECISION_LEVELS:
        raise ValueError(f"invalid evidence_confidence: {requested}")
    order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    ceiling = _evidence_confidence_ceiling(facts)
    return min((requested, ceiling), key=lambda value: order[value])


def _clean_citations(value: str, known: set[str]) -> str:
    """Keep only actual evidence IDs, splitting combined model citations."""
    def replacement(match: re.Match[str]) -> str:
        parts = [part.strip() for part in match.group(1).split(";")]
        return " ".join(f"[{part}]" for part in parts if part in known)
    cleaned = re.sub(r"\[([^\]]+)\]", replacement, value)
    return re.sub(r"\s+([,.!?])", r"\1", re.sub(r" {2,}", " ", cleaned)).strip()


def load_company(
    ticker: str, as_of: str = AS_OF, *, prices_dir: Path = PRICES,
    news_dir: Path = NEWS, documents_dir: str | Path = DOCUMENT_CACHE,
    forward: bool = False, price_retrieved_at: str = "2026-09-13",
    price_source: str = "EODHD frozen daily USA cache",
    news_source: str = "EODHD cached issuer wire release",
    continuity_evidence_ids: tuple[str, ...] = (),
    research_as_of: str | None = None,
) -> EvidencePack:
    """Assemble cached data and source-linked literal facts without network calls."""
    try:
        issuer = get_issuer(ticker)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    date.fromisoformat(as_of)
    evidence_as_of = research_as_of or as_of
    date.fromisoformat(evidence_as_of)
    if not forward and as_of > AS_OF:
        raise ValueError("as_of exceeds frozen news coverage")
    company, sector = issuer.company_name, issuer.schema_type
    price, price_evidence = _read_prices(
        ticker, as_of, prices_dir=prices_dir, source_name=price_source,
        retrieved_at=price_retrieved_at,
    )
    records, rejected, news_decisions = _news_records(
        ticker, evidence_as_of, news_dir=news_dir, forward=forward
    )
    issuer_records = [r for r in records if r.get("_source_class") == "ISSUER_RELEASE"]
    third_party_records = [
        r for r in records if r.get("_source_class") == "THIRD_PARTY_REPORT"
    ]
    results = [r for r in issuer_records if RESULT.search(html.unescape(str(r["title"])))
               and not SCHEDULED.search(html.unescape(str(r["title"]))) ]
    announcement = [r for r in issuer_records if RESULT.search(html.unescape(str(r["title"])))
                    and r not in results]
    chosen = results[:2] or announcement[:1]
    latest_other = next((r for r in issuer_records if r not in chosen and
                         not RESULT.search(html.unescape(str(r["title"]))) and
                         re.search(r"guidance|acqui|approv|trial|dividend|buyback|contract|invest", str(r["title"]), re.I)), None)
    if latest_other:
        chosen.append(latest_other)
    required = tuple(dict.fromkeys(str(item) for item in continuity_evidence_ids))
    by_evidence_id = {_news_evidence_id(record, ticker): record for record in records}
    continuity = _apply_material_evidence_continuity(
        required, ticker=ticker, records_by_id=by_evidence_id,
        decisions=news_decisions, chosen=chosen,
    )
    latest_corporate = next(
        (r for r in third_party_records if r not in chosen), None
    ) if not required else None
    if latest_corporate:
        chosen.append(latest_corporate)
    # Retain publication order for novelty comparison, regardless of selection order.
    chosen.sort(key=lambda r: str(r["published_ts"]))
    evidence = [price_evidence] + [
        _record_evidence(r, ticker, source_name=news_source) for r in chosen
    ]
    events = []
    prior_titles: set[str] = set()
    for record, item in zip(chosen, evidence[1:]):
        title = item["title"]
        normalized_title = re.sub(r"\W+", " ", title.casefold()).strip()
        novelty = "REITERATION" if normalized_title in prior_titles else "NEW_RELEASE"
        prior_titles.add(normalized_title)
        guidance = _guidance(item["excerpt"], item["identifier"])
        is_result = bool(RESULT.search(title) and not SCHEDULED.search(title))
        metrics = _literal_metrics(item["excerpt"], item["identifier"]) if is_result else []
        guidance_ranges = _guidance_ranges(item["excerpt"], item["identifier"]) if is_result else []
        event_facts = {"novelty": novelty, "reported_metrics": metrics,
                       "guidance_ranges": guidance_ranges,
                       "guidance_language": guidance, "evidence_identifier": item["identifier"],
                       "issuer_release": True}
        if forward:
            event_facts.update({
                "source_class": str(record["_source_class"]),
                "confirmation_state": str(record["_confirmation_state"]),
            })
            if record["_source_class"] == "THIRD_PARTY_REPORT":
                event_facts["issuer_release"] = None
        summary = (
            _third_party_event_summary(record)
            if record.get("_source_class") == "THIRD_PARTY_REPORT" else title
        )
        events.append({"kind": "RESULTS" if is_result else "CORPORATE_EVENT",
                       "published_at": item["published_at"], "summary": summary,
                       "facts": event_facts,
                       "guidance": guidance["source_excerpt"] if guidance else None})
    document_coverage = load_document_coverage(
        ticker, evidence_as_of, cache_dir=documents_dir,
    )
    if document_coverage:
        documents = document_coverage["documents"]
        structured = document_coverage.get("structured_facts", [])
        guidance_observations: list[dict[str, object]] = []
        primary_results = [item for item in documents
                           if item["document_kind"] == "SEC_EARNINGS_RELEASE"]
        latest_cached_result = max((event["published_at"] for event in events
                                    if event["kind"] == "RESULTS"), default="")
        latest_primary_result = max((item["published_at"] for item in primary_results), default="")
        if primary_results and latest_primary_result >= latest_cached_result:
            removed_ids = {event["facts"]["evidence_identifier"] for event in events
                           if event["kind"] == "RESULTS"}
            events = [event for event in events if event["kind"] != "RESULTS"]
            evidence = [item for item in evidence if item.get("identifier") not in removed_ids]
        for document in documents:
            evidence.append({
                "source": document["source"], "published_at": document["published_at"],
                "identifier": document["evidence_id"], "url": document["url"],
                "excerpt": document["excerpt"], "title": document["title"],
                "retrieved_at": document["retrieved_at"],
                "content_sha256": document["content_sha256"], "raw_path": document["local_path"],
                "published_at_precision": "day", "document_kind": document["document_kind"],
                "filing_date": document["filing_date"], "accepted_at": document["accepted_at"],
                "form": document["form"], "accession_number": document["accession_number"],
            })
            if document["document_kind"] == "SEC_EARNINGS_RELEASE":
                ranges = _guidance_ranges(document["excerpt"], document["evidence_id"])
                guidance_observations.extend({**item, "published_at": document["published_at"]}
                                             for item in ranges)
                metrics = _literal_metrics(document["excerpt"], document["evidence_id"])
                result_facts = {"novelty": "NEW_PRIMARY_DOCUMENT", "reported_metrics": metrics,
                                "guidance_ranges": ranges, "guidance_language": None,
                                "evidence_identifier": document["evidence_id"],
                                "primary_source": True}
                if forward:
                    result_facts.update({"source_class": "PRIMARY_SEC",
                                         "confirmation_state": "CONFIRMED"})
                events.append({
                    "kind": "RESULTS", "published_at": document["published_at"],
                    "summary": document["title"],
                    "facts": result_facts,
                    "guidance": (ranges[0]["source_excerpt"] if ranges else None),
                })
            elif document["document_kind"] == "SEC_8_K_MATERIAL":
                material_facts = {"evidence_identifier": document["evidence_id"],
                                  "primary_source": True}
                if forward:
                    material_facts.update({"source_class": "PRIMARY_SEC",
                                           "confirmation_state": "CONFIRMED"})
                events.append({
                    "kind": "CORPORATE_EVENT", "published_at": document["published_at"],
                    "summary": document["title"],
                    "facts": material_facts, "guidance": None,
                })
            elif forward and (material_items := _material_8k_items(document)):
                events.append({
                    "kind": "CORPORATE_EVENT", "published_at": document["published_at"],
                    "summary": (
                        f"{document['title']}; material SEC "
                        + ", ".join(f"Item {item}" for item in material_items)
                    ),
                    "facts": {"evidence_identifier": document["evidence_id"],
                              "primary_source": True, "source_class": "PRIMARY_SEC",
                              "confirmation_state": "CONFIRMED"},
                    "guidance": None,
                })
        for current in sorted(guidance_observations,
                              key=lambda item: str(item["published_at"]), reverse=True):
            previous = next((item for item in guidance_observations
                             if item["metric"] == current["metric"] and
                             item["period"] == current["period"] and
                             item["unit"] == current["unit"] and
                             item["published_at"] < current["published_at"]), None)
            if previous is None:
                continue
            old_range = previous["current_range"]
            new_range = current["current_range"]
            direction = ("RAISED" if new_range[0] >= old_range[0] and
                         new_range[1] >= old_range[1] and new_range != old_range else
                         "LOWERED" if new_range[0] <= old_range[0] and
                         new_range[1] <= old_range[1] and new_range != old_range else
                         "UNCHANGED" if new_range == old_range else "MIXED")
            change = {"metric": current["metric"], "period": current["period"],
                      "unit": current["unit"], "previous_range": old_range,
                      "current_range": new_range, "direction": direction,
                      "previous_evidence_identifier": previous["evidence_identifier"],
                      "current_evidence_identifier": current["evidence_identifier"],
                      "extraction_method": "deterministic_same_metric_period_unit_range_comparison"}
            target = next(event for event in events if
                          event["facts"].get("evidence_identifier") == current["evidence_identifier"])
            target["facts"].setdefault("guidance_changes", []).append(change)
            break
        periodic = next((item for item in documents
                         if item["document_kind"] == "SEC_PERIODIC_REPORT"), None)
        if periodic:
            periodic_facts = {"structured_sec_facts": structured,
                              "evidence_identifier": periodic["evidence_id"],
                              "primary_source": True}
            if forward:
                periodic_facts.update({"source_class": "PRIMARY_SEC",
                                       "confirmation_state": "CONFIRMED"})
            events.append({
                "kind": "SEC_PERIODIC_REPORT", "published_at": periodic["published_at"],
                "summary": periodic["title"],
                "facts": periodic_facts, "guidance": None,
            })
    _attach_fact_ids(ticker, price, str(price_evidence["identifier"]), events)
    facts_as_of = evidence_as_of if forward and research_as_of is not None else as_of
    company_input = {"company_name": company, "ticker": ticker, "provider_symbol": issuer.provider_symbol,
                     "as_of": facts_as_of, "schema_type": sector, "price": price,
                     "fundamentals": {}, "financial_facts": {}, "events": events, "evidence": evidence}
    # Run the reused point-in-time/provenance validator before any LLM call.
    (build_facts_v2 if forward else build_facts)(company_input, facts_as_of)
    limitations = (["SEC periodic report and earnings filings are primary but issuer facts remain unaudited",
                    "Some non-GAAP and guidance metrics are only available in issuer exhibits"]
                   if document_coverage else
                   ["No primary SEC/IR financial document in frozen cache",
                    "Guidance changes are literal language only; quantitative delta unsupported"])
    triage = {"issuer_releases_in_cache": len(records), "rejected_non_issuer_news": rejected,
              "selected_release_count": len(chosen), "selected_titles": [r["title"] for r in chosen],
              "sec_document_count": len(document_coverage["documents"]) if document_coverage else 0,
              "sec_structured_fact_count": len(document_coverage.get("structured_facts", [])) if document_coverage else 0,
              "material_evidence_continuity": continuity,
              "limitations": limitations}
    return EvidencePack(ticker, facts_as_of, company_input, triage)


_FACTS_V3_EXPECTATION_PRECEDENCE = (
    "Facts V3 expectation_comparisons establish factual comparability, not materiality or final judgment. "
    "Comparable issuer guidance is a valid expectation baseline; market consensus is not required. "
    "When an event contains both a first disclosure and a material expectation change marked "
    "VERIFIED_CHANGE against a comparable earlier primary source, EXPECTATION_CHANGE takes precedence "
    "over NEW_INFORMATION. NEW_INFORMATION applies only when no verified expectation-change facet exists. "
    "AMBIGUOUS, NOT_ESTABLISHED, or otherwise non-comparable expectation evidence must not automatically "
    "produce EXPECTATION_CHANGE. "
)


class USAProvider(CodexCLIProvider):
    """Two existing structured LLM calls with a USA evidence contract."""

    def __init__(self) -> None:
        self.revisions: dict[str, object] = {}
        self.validated_claim_refs: list[dict[str, object]] = []
        self.rejected_claim_refs: list[dict[str, object]] = []
        self.analyst_recommendation: tuple[str, str, str] | None = None
        self.critic_recommendation: tuple[str, str, str] | None = None
        self.legacy_numeric_failures: list[str] = []

    def analyze(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        expectation_precedence = (
            _FACTS_V3_EXPECTATION_PRECEDENCE
            if facts.get("facts_contract_version") == "3" else ""
        )
        prompt = (
            "You are TRINITY USA V2 Analyst. Return only schema JSON, in Italian. "
            "Use only provided facts and cite [evidence identifier] for every material claim. "
            "Treat reported_metrics as literal issuer claims with explicit USD units, not audited SEC facts. "
            "Respect measurement_type: CHANGE_AMOUNT is a change, ANNUALIZED_RUN_RATE is not quarterly sales; "
            "REPORTED_AMOUNT still needs context from source_excerpt. "
            "Use guidance_ranges when comparing per-share guidance. Describe direction only; do not calculate "
            "an unstated numerical delta. "
            "Do not add arithmetic, figures, causality, valuation or guidance deltas absent from facts. "
            "For every quantitative claim add a claim_refs entry whose text is an exact substring of the "
            "narrative, whose fact_ids list contains only supplied fact_id values, and whose period matches "
            "the fact when stated. fact_ids are the authoritative numeric provenance; citations remain for "
            "rendering. Do not create a claim_ref for arithmetic absent from a supplied fact. "
            + expectation_precedence +
            "Classify every facts.events entry exactly once in event_assessments, using its facts.evidence_identifier "
            "as event_id (or the evidence_id of its first structured fact when needed). NEW_INFORMATION is a new "
            "disclosure that can affect the thesis but lacks a verified comparable expectation baseline. "
            "EXPECTATION_CHANGE requires a material change verified against a comparable earlier primary source. "
            "CONFIRMATION is a second primary document for the same disclosed event; it is not a second catalyst. "
            "REITERATION repeats guidance or a statement without changing it. ALREADY_KNOWN is prior baseline "
            "information. LOW_RELEVANCE is routine or immaterial. A scheduled results call is LOW_RELEVANCE. "
            "Only list a catalyst if the event is concrete, future-relevant and evidenced. "
            "State uncertainty when the cache lacks underlying results or independent confirmation. "
            "evidence_confidence measures only source quality, primary filings, pertinent fact completeness, "
            "provenance, temporal comparability, units and extraction reliability. It never measures attractiveness. "
            "HIGH evidence_confidence is allowed when SEC documents and provenance are sufficient. "
            "thesis_strength measures material novelty, verified expectation change, catalyst quality, change from "
            "the prior baseline, bull/bear balance and verifiable invalidation; fact count alone is irrelevant. "
            "PASS means no material new information or thesis change requires immediate work. WATCH means a material "
            "event or hypothesis needs monitoring but lacks sufficient confirmation. INVESTIGATE means material new "
            "information or a verified expectation change can concretely alter the thesis. Status is research triage, "
            "not a trade signal. as_of is the historical cutoff.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        result = self._request(prompt, _USA_ANALYST_SCHEMA)
        result["event_assessments"] = _validated_event_assessments(
            facts, result["event_assessments"]
        )
        result["evidence_confidence"] = _bounded_evidence_confidence(
            facts, str(result["evidence_confidence"])
        )
        self.analyst_recommendation = (
            str(result["proposed_status"]), str(result["thesis_strength"]),
            str(result["evidence_confidence"]),
        )
        return result

    def critique(self, facts: Mapping[str, object], draft: Mapping[str, object]) -> Mapping[str, object]:
        expectation_precedence = (
            _FACTS_V3_EXPECTATION_PRECEDENCE
            if facts.get("facts_contract_version") == "3" else ""
        )
        prompt = (
            "You are TRINITY USA V2 Critic. Return only schema JSON, in Italian. Audit each material "
            "claim against evidence identifiers, including every number and unit. Identify repeated news, "
            "weak or irrelevant catalysts, unsupported causal claims, overstrong conclusions, and guidance "
            "changes claimed without a comparable earlier source. Audit every event_assessments entry and return "
            + expectation_precedence +
            "one corrected assessment for every facts.events entry. EXPECTATION_CHANGE requires current and prior "
            "comparable primary evidence. Earnings release plus 10-Q for the same result is NEW_INFORMATION plus "
            "CONFIRMATION, never two catalysts. Reiterated guidance is REITERATION, not expectation change. "
            "Distinguish source-reported metrics from "
            "independently verified facts. In notes give specific corrections with evidence IDs. "
            "Return revised_analysis with all narrative fields corrected; remove unsupported claims and "
            "numbers, preserve source citations, and make invalidation criteria precise. For every retained "
            "quantitative claim, include a claim_refs entry with exact narrative text and supplied fact_ids. "
            "Never create a fact_id or bind a calculated value to component facts. Mark a claim MATERIAL only "
            "when removing it would materially weaken the recommended status or thesis_strength. "
            "evidence_confidence measures only source quality, filing coverage, pertinent facts, provenance, "
            "comparability, units and extraction reliability; it does not measure thesis attractiveness. "
            "thesis_strength measures material novelty, verified expectation change, catalyst quality, baseline "
            "change, bull/bear balance and invalidation; it must not follow fact count alone. PASS means no material "
            "new information or thesis change requires immediate work. WATCH means a material event or hypothesis "
            "needs monitoring but lacks sufficient confirmation. INVESTIGATE means material new information or a "
            "verified expectation change can concretely alter the thesis. HIGH evidence_confidence is valid when "
            "SEC documentation and provenance are sufficient. Status is research triage, not a trade signal.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
            + "\nDRAFT:\n" + json.dumps(draft, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        result = self._request(prompt, _USA_CRITIC_SCHEMA)
        result["event_assessments"] = _validated_event_assessments(
            facts, result["event_assessments"]
        )
        result["evidence_confidence"] = _bounded_evidence_confidence(
            facts, str(result["evidence_confidence"])
        )
        raw_critic_status = str(result["status"])
        raw_critic_strength = str(result["thesis_strength"])
        raw_critic_evidence = str(result["evidence_confidence"])
        revised = result["revised_analysis"]
        self.critic_recommendation = (
            raw_critic_status, raw_critic_strength, raw_critic_evidence,
        )
        result["critic_status"] = raw_critic_status
        result["critic_thesis_strength"] = raw_critic_strength
        result["critic_evidence_confidence"] = raw_critic_evidence
        known_ids = {str(item["identifier"]) for item in facts["evidence"]}
        for field in REVISION_FIELDS:
            if isinstance(revised[field], list):
                revised[field] = [_clean_citations(item, known_ids) for item in revised[field]]
            else:
                revised[field] = _clean_citations(revised[field], known_ids)
        result["notes"] = [_clean_citations(item, known_ids) for item in result["notes"]]
        self.legacy_numeric_failures = _numeric_audit(facts, revised)
        validation = validate_claim_refs(facts, revised)
        self.validated_claim_refs = validation.valid_claim_refs
        self.rejected_claim_refs = validation.rejected_claim_refs
        for rejected in validation.rejected_claim_refs:
            result["notes"].append(
                f"{rejected['claim_id']}: structured provenance rejected "
                f"{rejected['validation_errors']}"
            )
        if validation.essential_support_removed:
            result["status"] = "PASS"
            result["thesis_strength"] = "LOW"
            result["notes"].append(
                "Status degraded to PASS/LOW thesis strength because material rejected claims left fewer than two "
                "validated material claims backed by distinct facts."
            )
        else:
            calibrated_status = calibrate_decision(
                result["event_assessments"], str(result["thesis_strength"])
            )
            if calibrated_status != result["status"]:
                result["notes"].append(
                    f"Action status calibrated from {result['status']} to {calibrated_status} using "
                    "material event classes and thesis strength; evidence confidence was unchanged."
                )
            result["status"] = calibrated_status
        self.revisions = {field: validation.revised_analysis[field] for field in REVISION_FIELDS}
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ten historical USA V2 thesis slices from frozen caches")
    parser.add_argument("--as-of", default=AS_OF)
    parser.add_argument("--output-dir", default="data/trinity_usa_v2")
    parser.add_argument("--ticker", choices=COMPANIES, nargs="+")
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    # The Windows sandbox may omit the standard home variables inherited by codex.exe.
    os.environ.setdefault("USERPROFILE", str(DATASET.parents[2]))
    os.environ.setdefault("HOME", os.environ["USERPROFILE"])
    os.environ.setdefault("CODEX_HOME", str(Path(os.environ["USERPROFILE"]) / ".codex"))
    failures = 0
    for ticker in (args.ticker or COMPANIES):
        try:
            pack = load_company(ticker, args.as_of)
            provider = USAProvider()
            thesis = analyze_company(pack.company_input, args.as_of, provider)
            thesis = replace(thesis, **provider.revisions,
                             claim_refs=provider.validated_claim_refs,
                             rejected_claim_refs=provider.rejected_claim_refs)
            thesis_path = save_thesis(thesis, output)
            structured_fact_count = sum(
                len(event["facts"].get("structured_sec_facts", [])) +
                len(event["facts"].get("reported_metrics", []))
                for event in pack.company_input["events"]
            )
            guidance_fact_count = sum(
                len(event["facts"].get("guidance_ranges", [])) +
                len(event["facts"].get("guidance_changes", []))
                for event in pack.company_input["events"]
            )
            result = {"ticker": ticker, "last_close": pack.company_input["price"]["current_price"],
                      "price_date": pack.company_input["price"]["published_at"],
                      "evidence_count": len(thesis.evidence), "events": pack.triage["selected_titles"],
                      "structured_facts": structured_fact_count,
                      "guidance_facts": guidance_fact_count,
                      "analyst_status": provider.analyst_recommendation[0],
                      "analyst_thesis_strength": provider.analyst_recommendation[1],
                      "analyst_evidence_confidence": provider.analyst_recommendation[2],
                      "critic_status": provider.critic_recommendation[0],
                      "critic_thesis_strength": provider.critic_recommendation[1],
                      "critic_evidence_confidence": provider.critic_recommendation[2],
                      "status": thesis.status,
                      "thesis_strength": thesis.thesis_strength,
                      "evidence_confidence": thesis.evidence_confidence,
                      "event_assessments": thesis.event_assessments,
                      "validated_claims": len(provider.validated_claim_refs),
                      "rejected_claims": len(provider.rejected_claim_refs),
                      "legacy_numeric_failures": len(provider.legacy_numeric_failures),
                      "blocked_derivations": sum(
                          "UNREGISTERED_DERIVATION_OR_VALUE" in item["validation_errors"]
                          for item in provider.rejected_claim_refs
                      ),
                      "critic_notes": thesis.critic_notes, "thesis_path": str(thesis_path)}
        except Exception as exc:
            failures += 1
            result = {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
