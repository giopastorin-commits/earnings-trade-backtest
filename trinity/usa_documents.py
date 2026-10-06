"""SEC EDGAR document coverage for the fixed TRINITY USA V2 universe.

Acquisition is explicit and cached. Analysis reads only the local manifest, so
ordinary thesis runs never make network requests or silently change evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping, Sequence

from bs4 import BeautifulSoup
import requests

from trinity.usa_issuer_registry import REGISTRY, get_issuer


SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
DEFAULT_CACHE = Path("data/trinity_usa_documents")
SEC_COMPANIES: dict[str, tuple[str, str]] = {
    record.ticker: (record.sec_cik, record.schema_type) for record in REGISTRY.supported
}
ORIGINAL_SEC_TICKERS = (
    "AAPL", "JPM", "JNJ", "XOM", "WMT", "CAT", "NEE", "AMZN", "PLD", "LIN",
)
_DURATION_TAGS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
        "SalesRevenueNet", "InterestAndDividendIncomeOperating",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
    ),
}
_INSTANT_TAGS = {
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "debt": (
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermDebt",
    ),
}


@dataclass(frozen=True)
class DocumentRecord:
    company: str
    ticker: str
    source: str
    url: str
    identifier: str
    evidence_id: str
    title: str
    form: str
    accession_number: str
    filing_date: str
    published_at: str
    accepted_at: str
    retrieved_at: str
    content_sha256: str
    local_path: str
    document_kind: str
    excerpt: str


class SECAcquisitionError(RuntimeError):
    """A primary SEC record could not be acquired or parsed."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(session: requests.Session, url: str, *, attempts: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=45)
            response.raise_for_status()
            time.sleep(0.12)
            return response.content
        except requests.RequestException as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.0 + attempt)
    raise SECAcquisitionError(f"SEC request failed: {url}: {type(last).__name__}") from last


def _recent_filings(
    submissions: Mapping[str, Any], as_of: str, decision_cutoff_utc: datetime | None = None,
) -> list[dict[str, str]]:
    recent = submissions["filings"]["recent"]
    records = []
    for index, filing_date in enumerate(recent["filingDate"]):
        if filing_date > as_of:
            continue
        record = {key: str(values[index]) for key, values in recent.items()
                  if isinstance(values, list) and index < len(values)}
        if decision_cutoff_utc is not None and filing_date == as_of:
            accepted = record.get("acceptanceDateTime", "")
            if not accepted:
                continue
            try:
                if accepted.isdigit() and len(accepted) == 14:
                    from zoneinfo import ZoneInfo
                    accepted_at = datetime.strptime(accepted, "%Y%m%d%H%M%S").replace(
                        tzinfo=ZoneInfo("America/New_York")
                    )
                else:
                    accepted_at = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
                if accepted_at.astimezone(timezone.utc) > decision_cutoff_utc.astimezone(timezone.utc):
                    continue
            except ValueError:
                continue
        records.append(record)
    return records


def _selected_filings(records: list[dict[str, str]]) -> list[tuple[str, dict[str, str]]]:
    reports = [row for row in records if row.get("form") in {"10-Q", "10-K"}]
    earnings = [row for row in records if row.get("form") == "8-K" and
                "2.02" in row.get("items", "")]
    material = [row for row in records if row.get("form") == "8-K" and row not in earnings and
                any(item in row.get("items", "") for item in ("1.01", "2.01", "5.02", "8.01"))]
    selected: list[tuple[str, dict[str, str]]] = []
    if reports:
        latest_q = next((row for row in reports if row["form"] == "10-Q"), reports[0])
        selected.append(("PERIODIC_REPORT", latest_q))
    selected.extend(("EARNINGS_CURRENT" if index == 0 else "EARNINGS_PREVIOUS", row)
                    for index, row in enumerate(earnings[:2]))
    if material:
        selected.append(("MATERIAL_EVENT", material[0]))
    return selected


def _submission_documents(raw: bytes) -> list[tuple[str, str, bytes]]:
    text = raw.decode("utf-8", errors="replace")
    result = []
    for block in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", text, re.I | re.S):
        type_match = re.search(r"<TYPE>\s*([^\r\n<]+)", block, re.I)
        file_match = re.search(r"<FILENAME>\s*([^\r\n<]+)", block, re.I)
        text_match = re.search(r"<TEXT>\s*(.*?)\s*</TEXT>", block, re.I | re.S)
        if type_match and file_match and text_match:
            result.append((type_match.group(1).strip(), file_match.group(1).strip(),
                           text_match.group(1).encode("utf-8")))
    return result


def _plain_text(raw: bytes) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    for element in soup(["script", "style", "ix:hidden"]):
        element.decompose()
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))).strip()


def _excerpt(text: str, kind: str) -> str:
    if kind == "SEC_PERIODIC_REPORT":
        anchors = ("CONSOLIDATED STATEMENTS OF OPERATIONS", "CONSOLIDATED STATEMENTS OF INCOME",
                   "MANAGEMENT'S DISCUSSION", "MANAGEMENT’S DISCUSSION")
        starts = [text.upper().find(anchor) for anchor in anchors]
        start = min((value for value in starts if value >= 0), default=0)
        return text[start:start + 6500]
    return text[:6500]


def _archive_url(cik: str, accession: str, filename: str) -> str:
    return (f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{filename}")


def _write_document(
    root: Path, ticker: str, company: str, cik: str, role: str,
    filing: Mapping[str, str], doc_type: str, filename: str, content: bytes,
    retrieved_at: str,
) -> DocumentRecord:
    accession = filing["accessionNumber"]
    is_exhibit = doc_type.upper().startswith("EX-99")
    if is_exhibit:
        kind = "SEC_EARNINGS_RELEASE"
    elif role == "PERIODIC_REPORT":
        kind = "SEC_PERIODIC_REPORT"
    elif role.startswith("EARNINGS"):
        kind = "SEC_8_K_EARNINGS"
    else:
        kind = "SEC_8_K_MATERIAL"
    suffix = re.sub(r"[^a-z0-9]+", "-", doc_type.lower()).strip("-")
    evidence_id = f"sec:{ticker}:{accession}:{suffix}"
    path = root / ticker / f"{accession}_{suffix}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    text = _plain_text(content)
    title = (f"{ticker} {doc_type} {role.lower().replace('_', ' ')} "
             f"filed {filing['filingDate']}")
    return DocumentRecord(
        company=company, ticker=ticker, source="SEC EDGAR", url=_archive_url(cik, accession, filename),
        identifier=evidence_id, evidence_id=evidence_id, title=title, form=filing["form"],
        accession_number=accession, filing_date=filing["filingDate"],
        published_at=filing["filingDate"], accepted_at=filing.get("acceptanceDateTime", ""),
        retrieved_at=retrieved_at, content_sha256=_sha(content), local_path=str(path),
        document_kind=kind, excerpt=_excerpt(text, kind),
    )


def _entry_days(entry: Mapping[str, Any]) -> int:
    if not entry.get("start"):
        return 0
    return (date.fromisoformat(entry["end"]) - date.fromisoformat(entry["start"])).days


def _xbrl_value(
    companyfacts: Mapping[str, Any], tags: tuple[str, ...], accession: str,
    *, duration: bool,
) -> tuple[str, str, dict[str, Any]] | None:
    us_gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        concept = us_gaap.get(tag)
        if not concept:
            continue
        for unit, entries in concept.get("units", {}).items():
            eligible = [entry for entry in entries if entry.get("accn") == accession and
                        entry.get("form") in {"10-Q", "10-K"} and
                        bool(entry.get("start")) == duration]
            if duration:
                quarter = [entry for entry in eligible if 70 <= _entry_days(entry) <= 110]
                eligible = quarter or eligible
            if eligible:
                eligible.sort(key=lambda entry: (entry.get("end", ""), -_entry_days(entry)), reverse=True)
                return tag, unit, eligible[0]
    return None


def _fact(metric: str, found: tuple[str, str, dict[str, Any]], evidence_id: str) -> dict[str, Any]:
    tag, unit, entry = found
    period = (f"{entry.get('start')}/{entry['end']}" if entry.get("start") else entry["end"])
    return {
        "metric": metric, "value": entry["val"], "unit": unit, "period": period,
        "source": "SEC XBRL companyfacts", "evidence_identifier": evidence_id,
        "accession_number": entry["accn"], "xbrl_tag": tag,
        "extraction_method": "sec_xbrl_exact_accession",
    }


def extract_xbrl_facts(
    companyfacts: Mapping[str, Any], accession: str, evidence_id: str, sector: str,
) -> list[dict[str, Any]]:
    """Extract only explicitly tagged facts for one periodic filing accession."""
    facts: list[dict[str, Any]] = []
    found: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for metric, tags in _DURATION_TAGS.items():
        if sector == "BANK" and metric in {"operating_income", "operating_cash_flow", "capex"}:
            continue
        item = _xbrl_value(companyfacts, tags, accession, duration=True)
        if item:
            found[metric] = item
            facts.append(_fact(metric, item, evidence_id))
    for metric, tags in _INSTANT_TAGS.items():
        item = _xbrl_value(companyfacts, tags, accession, duration=False)
        if item:
            found[metric] = item
            facts.append(_fact(metric, item, evidence_id))
    if "revenue" in found:
        tag, unit, current = found["revenue"]
        concept = companyfacts["facts"]["us-gaap"][tag]
        comparables = [entry for entry in concept["units"][unit]
                       if entry.get("accn") == accession and entry.get("start") and
                       abs(_entry_days(entry) - _entry_days(current)) <= 5 and
                       340 <= (date.fromisoformat(current["end"]) - date.fromisoformat(entry["end"])).days <= 390]
        if comparables and comparables[0]["val"]:
            previous = comparables[0]
            growth = 100.0 * (current["val"] / previous["val"] - 1.0)
            facts.append({
                "metric": "revenue_growth", "value": round(growth, 4), "unit": "PERCENT",
                "period": f"{current['start']}/{current['end']}", "source": "SEC XBRL companyfacts",
                "evidence_identifier": evidence_id, "accession_number": accession,
                "xbrl_tag": tag, "comparison_period": f"{previous['start']}/{previous['end']}",
                "extraction_method": "deterministic_same_tag_same_duration_year_over_year",
            })
    values = {fact["metric"]: fact for fact in facts}
    if sector != "BANK" and values.get("revenue", {}).get("value") and "operating_income" in values:
        margin = 100.0 * values["operating_income"]["value"] / values["revenue"]["value"]
        facts.append({
            "metric": "operating_margin", "value": round(margin, 4), "unit": "PERCENT",
            "period": values["revenue"]["period"], "source": "SEC XBRL companyfacts",
            "evidence_identifier": evidence_id, "accession_number": accession,
            "components": [values["operating_income"]["xbrl_tag"], values["revenue"]["xbrl_tag"]],
            "extraction_method": "deterministic_operating_income_divided_by_revenue",
        })
    if sector != "BANK" and "operating_cash_flow" in values and "capex" in values and (
        values["operating_cash_flow"]["period"] == values["capex"]["period"]
    ):
        facts.append({
            "metric": "free_cash_flow", "value": values["operating_cash_flow"]["value"] - values["capex"]["value"],
            "unit": values["operating_cash_flow"]["unit"], "period": values["capex"]["period"],
            "source": "SEC XBRL companyfacts", "evidence_identifier": evidence_id,
            "accession_number": accession,
            "components": [values["operating_cash_flow"]["xbrl_tag"], values["capex"]["xbrl_tag"]],
            "extraction_method": "deterministic_operating_cash_flow_minus_capex",
        })
    return facts


def acquire_sec_documents(
    as_of: str, *, cache_dir: str | Path = DEFAULT_CACHE,
    user_agent: str = "TRINITY research giopa@example.com",
    session: requests.Session | None = None,
    tickers: Sequence[str] | None = None,
    now: datetime | None = None, decision_cutoff_utc: datetime | None = None,
) -> Path:
    """Acquire the fixed universe's primary filings and write an audited manifest."""
    date.fromisoformat(as_of)
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    client = session or requests.Session()
    if session is None:
        import truststore
        truststore.inject_into_ssl()
    client.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
    # Preserve the original no-argument ten-ticker acquisition behavior. The
    # production forward path always supplies its explicit ticker list.
    requested = tuple(tickers) if tickers is not None else ORIGINAL_SEC_TICKERS
    unsupported = sorted(set(requested) - set(SEC_COMPANIES))
    if unsupported:
        raise ValueError(f"unsupported SEC tickers: {', '.join(unsupported)}")
    retrieved_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    manifest: dict[str, Any] = {"as_of": as_of, "retrieved_at": retrieved_at,
                                "source": "SEC EDGAR", "companies": {}}
    for ticker in requested:
        issuer = get_issuer(ticker)
        cik, sector = issuer.sec_cik, issuer.schema_type
        submissions_url = f"{SEC_DATA}/submissions/CIK{cik}.json"
        companyfacts_url = f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik}.json"
        submissions_raw = _fetch(client, submissions_url)
        companyfacts_raw = _fetch(client, companyfacts_url)
        (root / ticker).mkdir(exist_ok=True)
        (root / ticker / "submissions.json").write_bytes(submissions_raw)
        (root / ticker / "companyfacts.json").write_bytes(companyfacts_raw)
        submissions = json.loads(submissions_raw)
        companyfacts = json.loads(companyfacts_raw)
        all_recent = _recent_filings(submissions, as_of)
        eligible_recent = _recent_filings(submissions, as_of, decision_cutoff_utc)
        selected = _selected_filings(eligible_recent)
        documents: list[DocumentRecord] = []
        periodic_accession = None
        periodic_evidence = None
        for role, filing in selected:
            accession = filing["accessionNumber"]
            url = (f"{SEC_BASE}/Archives/edgar/data/{int(cik)}/"
                   f"{accession.replace('-', '')}/{accession}.txt")
            submission_raw = _fetch(client, url)
            docs = _submission_documents(submission_raw)
            primary = next((item for item in docs if item[1] == filing["primaryDocument"]), None)
            if primary is None:
                raise SECAcquisitionError(f"{ticker} {accession}: primary document missing")
            record = _write_document(root, ticker, submissions["name"], cik, role, filing,
                                     primary[0], primary[1], primary[2], retrieved_at)
            documents.append(record)
            if role == "PERIODIC_REPORT":
                periodic_accession, periodic_evidence = accession, record.evidence_id
            if role.startswith("EARNINGS"):
                exhibit = next((item for item in docs if item[0].upper().startswith("EX-99")), None)
                if exhibit:
                    documents.append(_write_document(
                        root, ticker, submissions["name"], cik, role, filing,
                        exhibit[0], exhibit[1], exhibit[2], retrieved_at,
                    ))
        xbrl_evidence_id = f"sec:{ticker}:companyfacts:{as_of}"
        facts = (extract_xbrl_facts(companyfacts, periodic_accession, xbrl_evidence_id, sector)
                 if periodic_accession and periodic_evidence else [])
        if periodic_accession and facts:
            periodic_filing = next(row for role, row in selected if role == "PERIODIC_REPORT")
            companyfacts_path = root / ticker / "companyfacts.json"
            documents.append(DocumentRecord(
                company=submissions["name"], ticker=ticker, source="SEC EDGAR XBRL companyfacts",
                url=companyfacts_url, identifier=xbrl_evidence_id, evidence_id=xbrl_evidence_id,
                title=f"{ticker} SEC XBRL facts for {periodic_accession}", form="XBRL",
                accession_number=periodic_accession, filing_date=periodic_filing["filingDate"],
                published_at=periodic_filing["filingDate"],
                accepted_at=periodic_filing.get("acceptanceDateTime", ""),
                retrieved_at=retrieved_at, content_sha256=_sha(companyfacts_raw),
                local_path=str(companyfacts_path), document_kind="SEC_XBRL_COMPANYFACTS",
                excerpt="STRUCTURED SEC XBRL FACTS:\n" + json.dumps(
                    facts, ensure_ascii=False, allow_nan=False, sort_keys=True
                ),
            ))
        manifest["companies"][ticker] = {
            "cik": cik, "company": submissions["name"], "sector": sector,
            "submissions_url": submissions_url, "companyfacts_url": companyfacts_url,
            "documents": [asdict(item) for item in documents], "structured_facts": facts,
            "post_cutoff_excluded": len(all_recent) - len(eligible_recent),
        }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def load_document_coverage(
    ticker: str, as_of: str, *, cache_dir: str | Path = DEFAULT_CACHE,
) -> dict[str, Any] | None:
    """Load only a complete, matching cached manifest; never fetch implicitly."""
    path = Path(cache_dir) / "manifest.json"
    if not path.is_file():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("as_of") != as_of:
        return None
    company = manifest.get("companies", {}).get(ticker)
    if not company:
        return None
    for document in company.get("documents", []):
        local = Path(document["local_path"])
        if not local.is_file() or _sha(local.read_bytes()) != document["content_sha256"]:
            raise SECAcquisitionError(f"cached document hash mismatch: {local}")
        if document["published_at"] > as_of or document["filing_date"] > as_of:
            raise SECAcquisitionError(f"future document in cache: {document['evidence_id']}")
    return company
