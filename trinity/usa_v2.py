"""Read-only USA thesis slice over the existing frozen EODHD caches.

The cache is a historical observation, not a live feed. News is only admitted
when the issuer is explicit in the title and the cached text is a wire release.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
import hashlib
import html
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from trinity.italia_real import CodexCLIProvider, LLMProviderError, _ANALYST_SCHEMA
from trinity.italia_v1 import analyze_company, build_facts, save_thesis


AS_OF = "2026-09-12"
DATASET = Path("C:/Users/giopa/trinity-scanner-v1/historical/raw")
PRICES = DATASET / "eodhd_prices_518_daily_20220101_20260913_v1/provider_raw"
NEWS = DATASET / "eodhd_news/eodhd_news_historical_20250101_20260912_v2/records"
COMPANIES = {
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
WIRE = re.compile(r"(?:BUSINESS WIRE|PRNewswire|PR NEWSWIRE)", re.I)
RESULT = re.compile(r"(?:first|second|third|fourth)[- ]quarter.{0,35}results|quarterly.{0,20}results|revenue growth", re.I)
SCHEDULED = re.compile(r"to host|conference call|available on|to release|to announce|date for release|to report", re.I)
GUIDANCE = re.compile(r"\b(?:raises?|raised|reiterates?|reaffirm(?:s|ed)?|lowers?|lowered|updates?|provides?|issues?)\b.{0,75}\b(?:guidance|outlook)\b|\b(?:guidance|outlook)\b.{0,60}\b(?:raises?|raised|reiterates?|reaffirm(?:s|ed)?|lowers?|lowered|updates?|provides?|issues?)\b", re.I)
_METRIC_LABEL = r"adjusted operating profit|operating profit|operating income|sales and revenues|net sales|net income|net earnings|cash flow|revenue|earnings|sales"
METRIC = re.compile(
    rf"(?P<label>{_METRIC_LABEL})\b(?:(?!\b(?:{_METRIC_LABEL})\b)[^.!?\n]){{0,65}}?"
    r"(?P<amount>\$[\d,.]+\s*(?:billion|million|trillion))", re.I,
)
GUIDANCE_RANGE = re.compile(r"\$([\d,.]+)\s*(?:-|to|–|—)\s*\$?([\d,.]+)", re.I)
REVISION_FIELDS = ("fundamental_analysis", "earnings_and_news_analysis", "price_context",
                   "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation")
_REVISED_SCHEMA = {"type": "object", "additionalProperties": False,
                   "required": list(REVISION_FIELDS),
                   "properties": {key: _ANALYST_SCHEMA["properties"][key] for key in REVISION_FIELDS}}
_USA_CRITIC_SCHEMA = {"type": "object", "additionalProperties": False,
                      "required": ["notes", "status", "confidence", "revised_analysis"],
                      "properties": {
                          "notes": {"type": "array", "items": {"type": "string"}},
                          "status": {"type": "string", "enum": ["PASS", "WATCH", "INVESTIGATE"]},
                          "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
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


def _read_prices(ticker: str, as_of: str) -> tuple[dict[str, object], dict[str, object]]:
    path = PRICES / f"{ticker}.json"
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
        "source": "EODHD frozen daily USA cache", "published_at": latest["date"],
        "identifier": f"price:{ticker}:{latest['date']}", "url": None,
        "excerpt": json.dumps({"latest": {key: latest[key] for key in
                                            ("date", "open", "high", "low", "close", "volume")},
                               "reference_bars": reference_bars, "returns_percent": returns}),
        "title": f"{ticker} daily OHLCV", "retrieved_at": "2026-09-13",
        "content_sha256": _digest(raw), "raw_path": str(path),
        "published_at_precision": "day", "document_kind": "PRICE",
    }
    price = {"current_price": latest["close"], **returns, "published_at": latest["date"],
             "acquisition": {"currency": "USD", "market": "USA", "provider_symbol": f"{ticker}.US",
                             "last_bar": latest, "reference_bars": reference_bars,
                             "raw_path": str(path), "sha256": evidence["content_sha256"]}}
    return price, evidence


def _issuer_release(record: Mapping[str, object], ticker: str) -> bool:
    title = html.unescape(str(record.get("title") or ""))
    content = html.unescape(str(record.get("content") or ""))
    aliases = COMPANIES[ticker][2]
    return (f"{ticker}.US" in record.get("symbols", [])
            and any(title.casefold().startswith(alias.casefold()) for alias in aliases)
            and bool(WIRE.search(content[:650]))
            and any(alias.casefold() in content[:2000].casefold() for alias in aliases))


def _news_records(ticker: str, as_of: str) -> tuple[list[dict[str, object]], int]:
    accepted: list[dict[str, object]] = []
    rejected = 0
    for path in sorted((NEWS / ticker).glob("2026-*.jsonl")):
        if path.stem < "2026-04" or path.stem > as_of[:7]:
            continue
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                record = json.loads(line)
                published = str(record.get("published_ts") or "")[:10]
                if not published or published > as_of:
                    continue
                if not _issuer_release(record, ticker):
                    rejected += 1
                    continue
                record["_path"] = str(path)
                record["_line"] = line_number
                record["_line_sha256"] = _digest(line.encode("utf-8"))
                accepted.append(record)
    accepted.sort(key=lambda r: str(r["published_ts"]), reverse=True)
    return accepted, rejected


def _record_evidence(record: Mapping[str, object], ticker: str) -> dict[str, object]:
    title = html.unescape(str(record["title"]))
    content = html.unescape(str(record["content"]))
    identifier = f"news:{ticker}:{str(record['canonical_record_sha256'])[:16]}"
    return {
        "source": "EODHD cached issuer wire release", "published_at": str(record["published_ts"])[:10],
        "identifier": identifier, "url": record.get("link"), "excerpt": content[:4500],
        "title": title, "retrieved_at": str(record.get("ingestion_ts") or "2026-09-13")[:10],
        "content_sha256": str(record["_line_sha256"]), "raw_path": f"{record['_path']}:{record['_line']}",
        "published_at_precision": "timestamp", "document_kind": "ISSUER_RELEASE",
    }


def _literal_metrics(text: str, evidence_id: str) -> list[dict[str, object]]:
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
    for match in re.finditer(r"\b(adjusted EPS|Core FFO)\b", text[:4500], re.I):
        context = text[max(0, match.start() - 400):match.end() + 80]
        if not re.search(r"guidance", context, re.I):
            continue
        tail = text[match.end():min(len(text), match.end() + 150)]
        found = list(GUIDANCE_RANGE.finditer(tail))[:2]
        if not found or found[0].start() > 100:
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


def _clean_citations(value: str, known: set[str]) -> str:
    """Keep only actual evidence IDs, splitting combined model citations."""
    def replacement(match: re.Match[str]) -> str:
        parts = [part.strip() for part in match.group(1).split(";")]
        return " ".join(f"[{part}]" for part in parts if part in known)
    cleaned = re.sub(r"\[([^\]]+)\]", replacement, value)
    return re.sub(r"\s+([,.!?])", r"\1", re.sub(r" {2,}", " ", cleaned)).strip()


def load_company(ticker: str, as_of: str = AS_OF) -> EvidencePack:
    """Assemble cached data and source-linked literal facts without network calls."""
    if ticker not in COMPANIES:
        raise ValueError(f"unsupported ticker: {ticker}")
    date.fromisoformat(as_of)
    if as_of > AS_OF:
        raise ValueError("as_of exceeds frozen news coverage")
    company, sector, _ = COMPANIES[ticker]
    price, price_evidence = _read_prices(ticker, as_of)
    records, rejected = _news_records(ticker, as_of)
    results = [r for r in records if RESULT.search(html.unescape(str(r["title"])))
               and not SCHEDULED.search(html.unescape(str(r["title"]))) ]
    announcement = [r for r in records if RESULT.search(html.unescape(str(r["title"])))
                    and r not in results]
    chosen = results[:2] or announcement[:1]
    latest_other = next((r for r in records if r not in chosen and
                         not RESULT.search(html.unescape(str(r["title"]))) and
                         re.search(r"guidance|acqui|approv|trial|dividend|buyback|contract|invest", str(r["title"]), re.I)), None)
    if latest_other:
        chosen.append(latest_other)
    # Retain publication order for novelty comparison, regardless of selection order.
    chosen.sort(key=lambda r: str(r["published_ts"]))
    evidence = [price_evidence] + [_record_evidence(r, ticker) for r in chosen]
    events = []
    prior_titles: set[str] = set()
    for record, item in zip(chosen, evidence[1:]):
        title = item["title"]
        normalized_title = re.sub(r"\W+", " ", title.casefold()).strip()
        novelty = "REITERATION" if normalized_title in prior_titles else "NEW_RELEASE"
        prior_titles.add(normalized_title)
        guidance = _guidance(item["excerpt"], item["identifier"])
        is_result = bool(RESULT.search(title))
        metrics = _literal_metrics(item["excerpt"], item["identifier"]) if is_result else []
        guidance_ranges = _guidance_ranges(item["excerpt"], item["identifier"]) if is_result else []
        events.append({"kind": "RESULTS" if is_result else "CORPORATE_EVENT",
                       "published_at": item["published_at"], "summary": title,
                       "facts": {"novelty": novelty, "reported_metrics": metrics,
                                 "guidance_ranges": guidance_ranges,
                                 "guidance_language": guidance, "evidence_identifier": item["identifier"],
                                 "issuer_release": True},
                       "guidance": guidance["source_excerpt"] if guidance else None})
    company_input = {"company_name": company, "ticker": ticker, "provider_symbol": f"{ticker}.US",
                     "as_of": as_of, "schema_type": sector, "price": price,
                     "fundamentals": {}, "financial_facts": {}, "events": events, "evidence": evidence}
    # Run the reused point-in-time/provenance validator before any LLM call.
    build_facts(company_input, as_of)
    triage = {"issuer_releases_in_cache": len(records), "rejected_non_issuer_news": rejected,
              "selected_release_count": len(chosen), "selected_titles": [r["title"] for r in chosen],
              "limitations": ["No primary SEC/IR financial PDF in frozen cache",
                              "Guidance changes are literal language only; quantitative delta unsupported"]}
    return EvidencePack(ticker, as_of, company_input, triage)


class USAProvider(CodexCLIProvider):
    """Two existing structured LLM calls with a USA evidence contract."""

    def analyze(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        prompt = (
            "You are TRINITY USA V2 Analyst. Return only schema JSON, in Italian. "
            "Use only provided facts and cite [evidence identifier] for every material claim. "
            "Treat reported_metrics as literal issuer claims with explicit USD units, not audited SEC facts. "
            "Respect measurement_type: CHANGE_AMOUNT is a change, ANNUALIZED_RUN_RATE is not quarterly sales; "
            "REPORTED_AMOUNT still needs context from source_excerpt. "
            "Use guidance_ranges when comparing per-share guidance. Describe direction only; do not calculate "
            "an unstated numerical delta. "
            "Do not add arithmetic, figures, causality, valuation or guidance deltas absent from facts. "
            "Distinguish a new disclosure from a repeated statement; a scheduled results call is not a catalyst. "
            "Only list a catalyst if the event is concrete, future-relevant and evidenced. "
            "State uncertainty when the cache lacks underlying results or independent confirmation. "
            "Lower confidence when evidence is thin; status is research triage, not a trade signal. "
            "PASS means insufficient supported thesis and requires LOW confidence; WATCH means monitor; "
            "INVESTIGATE means concrete evidence merits more work. as_of is the historical cutoff.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        return self._request(prompt, _ANALYST_SCHEMA)

    def critique(self, facts: Mapping[str, object], draft: Mapping[str, object]) -> Mapping[str, object]:
        prompt = (
            "You are TRINITY USA V2 Critic. Return only schema JSON, in Italian. Audit each material "
            "claim against evidence identifiers, including every number and unit. Identify repeated news, "
            "weak or irrelevant catalysts, unsupported causal claims, overstrong conclusions, and guidance "
            "changes claimed without a comparable earlier source. Distinguish source-reported metrics from "
            "independently verified facts. In notes give specific corrections with evidence IDs. "
            "Return revised_analysis with all narrative fields corrected; remove unsupported claims and "
            "numbers, preserve source citations, and make invalidation criteria precise. If evidence "
            "is thin or issuer release only, confidence cannot be HIGH. If material unsupported claims remain, "
            "return PASS/LOW. PASS always requires LOW. Status is research triage, not a trade signal.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
            + "\nDRAFT:\n" + json.dumps(draft, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        result = self._request(prompt, _USA_CRITIC_SCHEMA)
        revised = result["revised_analysis"]
        known_ids = {str(item["identifier"]) for item in facts["evidence"]}
        for field in REVISION_FIELDS:
            if isinstance(revised[field], list):
                revised[field] = [_clean_citations(item, known_ids) for item in revised[field]]
            else:
                revised[field] = _clean_citations(revised[field], known_ids)
        result["notes"] = [_clean_citations(item, known_ids) for item in result["notes"]]
        numeric_failures = _numeric_audit(facts, revised)
        if numeric_failures:
            first_source = facts["evidence"][0]["identifier"]
            for failure in numeric_failures:
                field = failure.split(":", 1)[0].split("[", 1)[0]
                if field in ("catalysts", "risks"):
                    index = int(failure.split("[", 1)[1].split("]", 1)[0])
                    revised[field][index] = f"Dettaglio numerico omesso per provenance insufficiente [{first_source}]."
                else:
                    revised[field] = f"Dettaglio numerico omesso per provenance insufficiente [{first_source}]."
            result["status"] = "PASS"
            result["confidence"] = "LOW"
            result["notes"].extend(numeric_failures)
        if _numeric_audit(facts, revised):
            raise LLMProviderError("numeric provenance remediation failed")
        self.revisions = revised
        if result.get("status") == "PASS" and result.get("confidence") != "LOW":
            raise ValueError("PASS requires LOW confidence")
        if result.get("confidence") == "HIGH":
            result["confidence"] = "MEDIUM"
            result["notes"].append("Confidence capped at MEDIUM: cached issuer wire releases lack independently verified filing/IR financial statements.")
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
            thesis = replace(thesis, **provider.revisions)
            thesis_path = save_thesis(thesis, output)
            result = {"ticker": ticker, "last_close": pack.company_input["price"]["current_price"],
                      "price_date": pack.company_input["price"]["published_at"],
                      "evidence_count": len(thesis.evidence), "events": pack.triage["selected_titles"],
                      "literal_metrics": sum(len(e["facts"]["reported_metrics"]) for e in pack.company_input["events"]),
                      "status": thesis.status, "confidence": thesis.confidence,
                      "critic_notes": thesis.critic_notes, "thesis_path": str(thesis_path)}
        except Exception as exc:
            failures += 1
            result = {"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
