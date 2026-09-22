"""Small, auditable Technoprobe acquisition from Yahoo Chart and official IR.

Only completed sessions and documents published by ``as_of`` enter facts. IR
dates are date-only; no intraday publication time is inferred.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from pypdf import PdfReader
import requests

from trinity.italia_company_config import COMPANIES, CompanyConfig


PRESS_URL = "https://www.technoprobe.com/investors/investor-relations/financial-press-releases"
REPORT_URL = "https://www.technoprobe.com/investors/investor-relations/financial-statements"
MARKET_TZ = ZoneInfo("Europe/Rome")
DOCUMENT_TYPES = {"RESULTS", "FINANCIAL_REPORT", "GUIDANCE", "CORPORATE_EVENT"}
MONTHS = {name: i for i, name in enumerate((
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december"), 1)}


class AcquisitionError(RuntimeError):
    """A source, document, or source-derived value cannot be trusted."""


def _day(value: str | date) -> date:
    if isinstance(value, datetime):
        raise AcquisitionError("as_of must be a calendar date")
    try:
        return value if isinstance(value, date) else date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise AcquisitionError("as_of must be an ISO calendar date") from exc


class CachedHTTP:
    """Allowlisted GETs with SHA-256-verified raw cache and retrieval metadata."""

    def __init__(self, directory: str | Path, session: requests.Session | None = None,
                 *, refresh: bool = False, now: datetime | None = None) -> None:
        self.directory = Path(directory)
        self.session = session or requests.Session()
        if session is None:
            self.session.headers.update({"User-Agent": "Mozilla/5.0 TRINITY-Italia-V1"})
        self.refresh = refresh
        self.now = now or datetime.now(timezone.utc)
        if self.now.tzinfo is None:
            raise AcquisitionError("now must be timezone-aware")
        if session is None:
            import truststore
            truststore.inject_into_ssl()

    def get(self, url: str, kind: str) -> tuple[bytes, dict[str, str]]:
        parsed = urlparse(url)
        allowed_hosts = {"query1.finance.yahoo.com"} | {
            urlparse(config.ir_base_url).hostname for config in COMPANIES.values()
        }
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or kind not in {"json", "html", "pdf"}:
            raise AcquisitionError(f"source not allowed: {url}")
        key = sha256(url.encode("utf-8")).hexdigest()
        raw = self.directory / f"{key}.{kind}"
        meta_path = self.directory / f"{key}.meta.json"
        if raw.is_file() and meta_path.is_file() and not self.refresh:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fresh = kind == "pdf" or meta["retrieved_at"][:10] == self.now.date().isoformat()
            if fresh:
                content = raw.read_bytes()
                if sha256(content).hexdigest() != meta["content_sha256"] or meta["url"] != url:
                    raise AcquisitionError(f"cache integrity failure for {url}")
                return content, meta
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            content = response.content
        except requests.RequestException as exc:
            raise AcquisitionError(f"fetch failed for {url}: {type(exc).__name__}") from exc
        if not content or len(content) > 30_000_000:
            raise AcquisitionError(f"empty or oversized response: {url}")
        if kind == "pdf" and not content.startswith(b"%PDF"):
            raise AcquisitionError(f"response is not PDF: {url}")
        self.directory.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(content)
        meta = {"url": url, "retrieved_at": self.now.astimezone(timezone.utc).isoformat(),
                "content_sha256": sha256(content).hexdigest(), "raw_path": str(raw.resolve())}
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return content, meta


def fetch_prices(cache: CachedHTTP, as_of: str | date, *, ticker: str = "TPRO.MI") -> dict[str, object]:
    """Return definitive Milan closes and 5/20/60-session close returns."""
    if ticker not in COMPANIES:
        raise AcquisitionError("unsupported Milan symbol; foreign listing fallback forbidden")
    day = _day(as_of)
    query = urlencode({"range": "5y", "interval": "1d", "events": "div,splits"})
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?{query}"
    content, audit = cache.get(url, "json")
    try:
        payload = json.loads(content)
        result = payload["chart"]["result"][0]
        metadata = result["meta"]
        if metadata["symbol"] != ticker or metadata["currency"] != "EUR" or metadata["exchangeName"] != "MIL":
            raise AcquisitionError("Yahoo identity/market/currency mismatch")
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        closes, volumes = quote["close"], quote["volume"]
        opens, highs, lows = quote["open"], quote["high"], quote["low"]
        if not all(len(series) == len(timestamps) for series in
                   (closes, volumes, opens, highs, lows)):
            raise AcquisitionError("Yahoo OHLCV arrays have different lengths")
        bars = []
        today = cache.now.astimezone(MARKET_TZ).date()
        for stamp, open_price, high, low, close, volume in zip(
            timestamps, opens, highs, lows, closes, volumes
        ):
            session_day = datetime.fromtimestamp(stamp, MARKET_TZ).date()
            if session_day > day or session_day >= today:
                continue  # Never promote a current intraday bar to a definitive close.
            for label, value in (("open", open_price), ("high", high),
                                 ("low", low), ("close", close)):
                if value is None or isinstance(value, bool) or not math.isfinite(float(value)) or value <= 0:
                    raise AcquisitionError(f"invalid Yahoo {label} for {session_day}")
            if not (low <= open_price <= high and low <= close <= high):
                raise AcquisitionError(f"inconsistent Yahoo OHLC for {session_day}")
            if volume is None or isinstance(volume, bool) or not math.isfinite(float(volume)) or volume < 0:
                raise AcquisitionError(f"invalid Yahoo volume for {session_day}")
            bars.append({"date": session_day.isoformat(), "open": float(open_price),
                         "high": float(high), "low": float(low), "close": float(close),
                         "volume": int(volume)})
        bars.sort(key=lambda bar: bar["date"])
        if not bars or len({bar["date"] for bar in bars}) != len(bars):
            raise AcquisitionError("no completed unique Yahoo session")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AcquisitionError("malformed Yahoo chart payload") from exc
    last = bars[-1]
    returns = {f"return_{h}d": 100 * (last["close"] / bars[-1-h]["close"] - 1)
               if len(bars) > h else None for h in (5, 20, 60)}
    return {"current_price": last["close"], **returns, "published_at": last["date"],
            "volume": last["volume"], "market": "MIL", "currency": "EUR",
            "session_count": len(bars), "ohlcv": bars, "audit": audit}


def _date_from_text(text: str) -> str | None:
    patterns = (
        (r"\b(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?[ ]+(20\d{2})\b", (1, 2, 3)),
        (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(20\d{2})\b", (2, 1, 3)),
    )
    found = []
    for pattern, (month_group, day_group, year_group) in patterns:
        for match in re.finditer(pattern, text, re.I):
            month = MONTHS.get(match.group(month_group).lower())
            if month:
                try:
                    found.append((match.start(), date(int(match.group(year_group)), month,
                                                     int(match.group(day_group))).isoformat()))
                except ValueError:
                    pass
    if found:
        return max(found)[1]
    return None


def discover_documents(press_html: bytes | dict[str, bytes], report_html: bytes | None,
                       as_of: str | date, *, config: CompanyConfig | None = None) -> list[dict[str, str]]:
    """Select latest results/report and up to two recent material IR releases."""
    if config is not None and config.ticker != "TPRO.MI":
        from urllib.parse import unquote, urljoin
        if not isinstance(press_html, dict):
            raise AcquisitionError("configured IR pages are required")
        selected = []
        for source in config.documents:
            if source.document_type not in DOCUMENT_TYPES:
                raise AcquisitionError("unsupported configured document type")
            if source.published_at > _day(as_of).isoformat():
                continue
            if urlparse(source.url).hostname != urlparse(config.ir_base_url).hostname:
                raise AcquisitionError("IR document URL outside configured official site")
            page = press_html.get(source.index_url)
            if page is None:
                raise AcquisitionError("configured IR index page missing")
            links = {unquote(urljoin(source.index_url, a["href"])).lower()
                     for a in BeautifulSoup(page, "html.parser").select("a[href]")}
            if unquote(source.url).lower() not in links:
                raise AcquisitionError(f"configured document absent from IR index: {source.title}")
            selected.append({"company": config.company_name, "title": source.title,
                             "url": source.url, "published_at": source.published_at,
                             "document_type": source.document_type})
        if not {"RESULTS", "FINANCIAL_REPORT"} <= {d["document_type"] for d in selected}:
            raise AcquisitionError("latest results or financial report not found on official IR")
        return selected
    if not isinstance(press_html, bytes) or not isinstance(report_html, bytes):
        raise AcquisitionError("Technoprobe IR pages are required")
    day = _day(as_of).isoformat()
    from urllib.parse import urljoin
    documents: list[dict[str, str]] = []
    for html, page, kind in ((press_html, PRESS_URL, "press"),
                             (report_html, REPORT_URL, "report")):
        soup = BeautifulSoup(html, "html.parser")
        nodes = soup.select("li") if kind == "press" else soup.select(".block-block_listing_download")
        for node in nodes:
            title_node = node.select_one("p.download-title")
            anchor = node.select_one('a[href$=".pdf"]')
            if not title_node or not anchor:
                continue
            title = title_node.get_text(" ", strip=True)
            pub = _date_from_text(title if kind == "press" else node.get_text(" ", strip=True))
            if pub is None or pub > day:
                continue
            url = urljoin(page, anchor["href"])
            if urlparse(url).hostname != "www.technoprobe.com":
                raise AcquisitionError("IR document URL outside official site")
            documents.append({"source": "Technoprobe Investor Relations", "url": url,
                              "title": title, "published_at": pub, "kind": kind,
                              "published_at_precision": "DATE_ONLY"})
    press = sorted((d for d in documents if d["kind"] == "press"),
                   key=lambda d: (d["published_at"], d["url"]), reverse=True)
    results = [d for d in press if re.search(r"(financial results|results at|annual results)", d["title"], re.I)]
    material = [d for d in press if d not in results and
                (date.fromisoformat(day) - date.fromisoformat(d["published_at"])).days <= 180 and re.search(
        r"(partnership|acquisition|guidance|buyback|agreement|capital|governance|contract)", d["title"], re.I)]
    reports = sorted((d for d in documents if d["kind"] == "report"),
                     key=lambda d: (d["published_at"], d["url"]), reverse=True)
    chosen = (results[:1] + reports[:1] + material[:2])
    if not results or not reports:
        raise AcquisitionError("latest results or financial report not found on official IR")
    if len({d["url"] for d in chosen}) != len(chosen):
        raise AcquisitionError("duplicate selected IR document URL")
    return chosen


def extract_pdf_text(content: bytes) -> str:
    """Extract text only; no OCR or inferred figures."""
    try:
        reader = PdfReader(BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise AcquisitionError("official PDF text extraction failed") from exc


def extract_results(text: str, identifier: str) -> tuple[dict[str, object], dict[str, object]]:
    """Read exact H1-style EUR-thousand table labels; missing fields stay null."""
    def table(label: str) -> float | None:
        match = re.search(rf"(?im)^\s*{re.escape(label)}\s+([\d,]+)\s+[\d,]+(?:\s+[\d.]+%)?\s*$", text)
        return float(match.group(1).replace(",", "")) * 1000 if match else None

    revenue = table("Revenue")
    operating = table("Operating profit")
    net_income = table("Net profit")
    ebitda = table("Ebitda*") or table("EBITDA")
    net_position = table("Net Financial Position**")
    growth = re.search(r"(?i)revenues?[^\n]{0,90}\bup\s+([\d.]+)%", text)
    guidance_revenue = re.search(r"(?i)€?950[–-]1,050 million to €?1,050[–-]1,100 million", text)
    guidance_margin = re.search(r"(?i)44%[–-]46% to 46%[–-]48%", text)
    capex = re.search(r"(?i)increased from €250 million to €350 million", text)
    values = {"revenue": revenue, "revenue_growth": float(growth.group(1)) if growth else None,
              "operating_margin": 100 * operating / revenue if operating is not None and revenue else None,
              "net_income": net_income, "free_cash_flow": None,
              "net_debt": -net_position if net_position is not None else None}
    provenance = {field: {"evidence_identifier": identifier, "extraction": "PDF_TEXT_EXACT_LABEL"}
                  for field, value in values.items() if value is not None}
    extras = {"operating_profit": operating, "ebitda": ebitda,
              "net_financial_position": net_position,
              "guidance": ("Revenue guidance raised: " + guidance_revenue.group(0) +
                           ("; EBITDA margin: " + guidance_margin.group(0) if guidance_margin else ""))
                          if guidance_revenue else None,
              "capex_guidance": capex.group(0).strip() if capex else None}
    return {**values, "provenance": provenance}, extras


def acquire_technoprobe(as_of: str | date, *, cache_dir: str | Path = "data/trinity_italia_v1",
                        session: requests.Session | None = None, now: datetime | None = None,
                        refresh: bool = False) -> tuple[dict[str, object], dict[str, object]]:
    """Acquire one PIT CompanyInput and an auditable acquisition summary."""
    day = _day(as_of).isoformat()
    cache = CachedHTTP(cache_dir, session, refresh=refresh, now=now)
    prices = fetch_prices(cache, day)
    press_html, press_audit = cache.get(PRESS_URL, "html")
    report_html, report_audit = cache.get(REPORT_URL, "html")
    docs = discover_documents(press_html, report_html, day)
    evidence = [{"source": "Yahoo Finance Chart", "url": prices["audit"]["url"],
                 "identifier": f"YAHOO-TPRO.MI-{prices['published_at']}",
                 "published_at": prices["published_at"], "excerpt": "Definitive daily close and volume",
                 "title": "TPRO.MI daily OHLCV", "document_kind": "PRICE",
                 "published_at_precision": "SESSION_DATE", **{k: prices["audit"][k]
                 for k in ("retrieved_at", "content_sha256", "raw_path")}}]
    for doc in docs:
        content, audit = cache.get(doc["url"], "pdf")
        doc.update(audit)
        doc["identifier"] = f"IR-{sha256(doc['url'].encode()).hexdigest()[:16]}"
        evidence.append({"source": doc["source"], "url": doc["url"], "title": doc["title"],
                         "identifier": doc["identifier"], "published_at": doc["published_at"],
                         "published_at_precision": "DATE_ONLY", "document_kind": doc["kind"].upper(),
                         "retrieved_at": audit["retrieved_at"], "content_sha256": audit["content_sha256"],
                         "raw_path": audit["raw_path"], "excerpt": doc["title"]})
        doc["_content"] = content
    result_doc = docs[0]
    fundamentals, extras = extract_results(extract_pdf_text(result_doc.pop("_content")),
                                            result_doc["identifier"])
    for doc in docs[1:]:
        doc.pop("_content")
    fundamentals["published_at"] = result_doc["published_at"]
    events = [{"kind": "RESULTS", "published_at": result_doc["published_at"],
               "summary": result_doc["title"],
               "facts": {**extras, "evidence_identifier": result_doc["identifier"]},
               "guidance": extras["guidance"]}]
    for doc in docs[2:]:
        events.append({"kind": "CORPORATE_EVENT", "published_at": doc["published_at"],
                       "summary": doc["title"], "facts": {"evidence_identifier": doc["identifier"]},
                       "guidance": None})
    price = {field: prices[field] for field in ("current_price", "return_5d", "return_20d", "return_60d", "published_at")}
    price["acquisition"] = {field: prices[field] for field in ("volume", "market", "currency", "session_count")}
    price["acquisition"].update(prices["audit"])
    company = {"company_name": "Technoprobe S.p.A.", "ticker": "TPRO", "provider_symbol": "TPRO.MI",
               "isin": "IT0005482333", "as_of": day, "price": price,
               "fundamentals": fundamentals, "events": events, "evidence": evidence}
    from trinity.italia_v1 import build_facts
    build_facts(company, day)
    summary = {"prices": prices, "documents": docs,
               "source_pages": [press_audit, report_audit], "emarket_storage": "NOT_ACCESSED"}
    return company, summary


def acquire_company(ticker: str, as_of: str | date, *,
                    cache_dir: str | Path = "data/trinity_italia_v1",
                    session: requests.Session | None = None, now: datetime | None = None,
                    refresh: bool = False) -> tuple[dict[str, object], dict[str, object]]:
    """Acquire configured Milan prices, listed IR PDFs, and audited H1 facts."""
    if ticker not in COMPANIES:
        raise AcquisitionError("unsupported Milan symbol")
    if ticker == "TPRO.MI":
        company, summary = acquire_technoprobe(as_of, cache_dir=cache_dir, session=session,
                                               now=now, refresh=refresh)
        for index, document in enumerate(summary["documents"]):
            document.update({"company": company["company_name"],
                             "document_type": "RESULTS" if index == 0 else
                             "FINANCIAL_REPORT" if index == 1 else "CORPORATE_EVENT",
                             "sha256": document["content_sha256"],
                             "local_path": document["raw_path"]})
        return company, summary
    from trinity.italia_financial_facts import extract_financial_facts
    from trinity.italia_v1 import build_facts

    config = COMPANIES[ticker]
    day = _day(as_of).isoformat()
    cache = CachedHTTP(cache_dir, session, refresh=refresh, now=now)
    prices = fetch_prices(cache, day, ticker=ticker)
    pages: dict[str, bytes] = {}
    page_audits = []
    for index_url in dict.fromkeys(source.index_url for source in config.documents):
        pages[index_url], audit = cache.get(index_url, "html")
        page_audits.append(audit)
    documents = discover_documents(pages, None, day, config=config)
    evidence = [{"source": "Yahoo Finance Chart", "url": prices["audit"]["url"],
                 "identifier": f"YAHOO-{ticker}-{prices['published_at']}",
                 "published_at": prices["published_at"], "excerpt": "Definitive daily OHLCV",
                 "title": f"{ticker} daily OHLCV", "document_kind": "PRICE",
                 "published_at_precision": "SESSION_DATE", **{k: prices["audit"][k]
                 for k in ("retrieved_at", "content_sha256", "raw_path")}}]
    results_text = None
    result_doc = None
    report_text = None
    report_doc = None
    for document in documents:
        content, audit = cache.get(document["url"], "pdf")
        document.update({"retrieved_at": audit["retrieved_at"],
                         "sha256": audit["content_sha256"], "local_path": audit["raw_path"]})
        identifier = f"IR-{sha256(document['url'].encode()).hexdigest()[:16]}"
        document["identifier"] = identifier
        evidence.append({"source": config.company_name + " Investor Relations",
                         "url": document["url"], "title": document["title"],
                         "identifier": identifier, "published_at": document["published_at"],
                         "published_at_precision": "DATE_ONLY",
                         "document_kind": document["document_type"],
                         "retrieved_at": audit["retrieved_at"],
                         "content_sha256": audit["content_sha256"],
                         "raw_path": audit["raw_path"], "excerpt": document["title"]})
        if document["document_type"] == "RESULTS":
            results_text = extract_pdf_text(content)
            result_doc = document
        if document["document_type"] == "FINANCIAL_REPORT" and config.extract_report_guidance:
            report_text = extract_pdf_text(content)
            report_doc = document
    if results_text is None or result_doc is None:
        raise AcquisitionError("results PDF missing")
    normalized, failures = extract_financial_facts(
        results_text, config, result_doc["identifier"], report_text=report_text,
        report_evidence_identifier=report_doc["identifier"] if report_doc else None)
    fundamentals = {"revenue": None, "revenue_growth": None, "operating_margin": None,
                    "net_income": None, "free_cash_flow": None, "net_debt": None,
                    "published_at": result_doc["published_at"], "provenance": {}}
    for field in ("revenue", "revenue_growth", "operating_margin", "net_income"):
        if normalized[field] is not None:
            fundamentals[field] = normalized[field]["value"]
            fundamentals["provenance"][field] = normalized[field]
    if normalized["net_debt_or_cash"] is not None:
        value = normalized["net_debt_or_cash"]["value"]
        fundamentals["net_debt"] = -value if config.ticker == "DLG.MI" else value
        fundamentals["provenance"]["net_debt"] = normalized["net_debt_or_cash"]
    price = {field: prices[field] for field in
             ("current_price", "return_5d", "return_20d", "return_60d", "published_at")}
    price["acquisition"] = {field: prices[field] for field in
                            ("volume", "market", "currency", "session_count")}
    price["acquisition"].update(prices["audit"])
    company = {"company_name": config.company_name, "ticker": ticker.removesuffix(".MI"),
               "provider_symbol": ticker, "isin": config.isin, "as_of": day,
               "schema_type": config.schema_type, "price": price,
               "fundamentals": fundamentals, "financial_facts": normalized,
               "events": [{"kind": "RESULTS", "published_at": result_doc["published_at"],
                           "summary": result_doc["title"], "facts": {
                               "evidence_identifier": result_doc["identifier"]},
                           "guidance": normalized["guidance"]["source_excerpt"]
                           if normalized["guidance"] else None}], "evidence": evidence}
    build_facts(company, day)
    return company, {"prices": prices, "documents": documents,
                     "source_pages": page_audits, "extraction_failures": failures}
