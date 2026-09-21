"""Small, point-in-time TRINITY Italia thesis slice over caller-supplied data.

V1 uses ISO calendar dates (YYYY-MM-DD), not intraday timestamps. An ``as_of``
date includes that whole date; timestamp inputs are rejected instead of being
silently assigned a time zone or cutoff hour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Protocol
from uuid import uuid4


PROMPT_VERSION = "TRINITY_ITALIA_V1_CONTRACT"
STATUSES = frozenset({"PASS", "WATCH", "INVESTIGATE"})
CONFIDENCES = frozenset({"LOW", "MEDIUM", "HIGH"})
_PRICE_FIELDS = ("current_price", "return_5d", "return_20d", "return_60d")
_FUNDAMENTAL_FIELDS = (
    "revenue", "revenue_growth", "operating_margin", "net_income",
    "free_cash_flow", "net_debt",
)
_ANALYSIS_TEXT_FIELDS = (
    "fundamental_analysis", "earnings_and_news_analysis", "price_context",
    "bull_case", "bear_case", "thesis_invalidation",
)
_THESIS_ID = re.compile(r"[0-9a-f]{32}\Z")


class ItaliaV1InputError(ValueError):
    """A supplied fact, analyst result, or critic result is invalid."""


class AnalystCriticProvider(Protocol):
    """Two logical calls; implementations may be fake or use any LLM provider."""

    model_version: str

    def analyze(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        """Return narrative fields plus proposed_status and confidence."""

    def critique(
        self, facts: Mapping[str, object], draft: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Return notes, final status, and final confidence."""


def _day(value: object, field: str) -> str:
    if isinstance(value, datetime):
        raise ItaliaV1InputError(f"{field} must be an ISO calendar date, not a timestamp")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ItaliaV1InputError(f"{field} must be an ISO calendar date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ItaliaV1InputError(f"{field} is not a valid calendar date") from exc


def _published_day(value: object, as_of: str, field: str) -> str:
    published = _day(value, field)
    if published > as_of:
        raise ItaliaV1InputError(f"{field}={published} is after as_of={as_of}")
    return published


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ItaliaV1InputError(f"{field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field)


def _number(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ItaliaV1InputError(f"{field} must be a finite number or null")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ItaliaV1InputError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ItaliaV1InputError(f"{field} must be finite")
    return number


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ItaliaV1InputError(f"{field} must be an object")
    return value


def _items(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ItaliaV1InputError(f"{field} must be a list")
    return value


def _json_copy(value: object, field: str) -> object:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ItaliaV1InputError(f"{field} must contain JSON-compatible finite values") from exc


def build_facts(company_input: Mapping[str, object], as_of: str | date) -> dict[str, object]:
    """Validate supplied figures and provenance without deriving financial ratios.

    Every non-empty price/fundamental block and every event/evidence item must
    carry ``published_at``. Future-dated data raises instead of being filtered.
    Missing financial fields remain ``None`` in the returned facts.
    """

    data = _mapping(company_input, "company_input")
    day = _day(as_of, "as_of")
    if data.get("as_of") is not None and _day(data["as_of"], "company_input.as_of") != day:
        raise ItaliaV1InputError("company_input.as_of differs from as_of")

    company_name = _required_text(data.get("company_name"), "company_name")
    ticker = _required_text(data.get("ticker"), "ticker")
    provider_symbol = _optional_text(data.get("provider_symbol"), "provider_symbol")
    isin = _optional_text(data.get("isin"), "isin")

    price_input = _mapping(data.get("price", {}), "price")
    price: dict[str, object] = {
        field: _number(price_input.get(field), f"price.{field}") for field in _PRICE_FIELDS
    }
    if price["current_price"] is not None and price["current_price"] <= 0:
        raise ItaliaV1InputError("price.current_price must be positive")
    price["trend"] = _optional_text(price_input.get("trend"), "price.trend")
    indicators = _mapping(price_input.get("indicators", {}), "price.indicators")
    price["indicators"] = {
        _required_text(key, "price.indicator key"): _number(value, f"price.indicators.{key}")
        for key, value in indicators.items()
    }
    has_price = any(price[field] is not None for field in _PRICE_FIELDS) or bool(
        price["trend"] or price["indicators"]
    )
    price["published_at"] = (
        _published_day(price_input.get("published_at"), day, "price.published_at")
        if has_price or price_input.get("published_at") is not None else None
    )

    fundamentals_input = _mapping(data.get("fundamentals", {}), "fundamentals")
    fundamentals: dict[str, object] = {
        field: _number(fundamentals_input.get(field), f"fundamentals.{field}")
        for field in _FUNDAMENTAL_FIELDS
    }
    valuations = _mapping(
        fundamentals_input.get("valuation_metrics", {}), "fundamentals.valuation_metrics"
    )
    fundamentals["valuation_metrics"] = {
        _required_text(key, "valuation metric key"): _number(
            value, f"fundamentals.valuation_metrics.{key}"
        ) for key, value in valuations.items()
    }
    has_fundamentals = any(
        fundamentals[field] is not None for field in _FUNDAMENTAL_FIELDS
    ) or bool(fundamentals["valuation_metrics"])
    fundamentals["published_at"] = (
        _published_day(
            fundamentals_input.get("published_at"), day, "fundamentals.published_at"
        ) if has_fundamentals or fundamentals_input.get("published_at") is not None else None
    )

    events: list[dict[str, object]] = []
    for index, raw in enumerate(_items(data.get("events", []), "events")):
        item = _mapping(raw, f"events[{index}]")
        events.append({
            "kind": _required_text(item.get("kind"), f"events[{index}].kind"),
            "published_at": _published_day(
                item.get("published_at"), day, f"events[{index}].published_at"
            ),
            "summary": _required_text(item.get("summary"), f"events[{index}].summary"),
            "facts": _json_copy(_mapping(item.get("facts", {}), f"events[{index}].facts"),
                                f"events[{index}].facts"),
            "guidance": _optional_text(item.get("guidance"), f"events[{index}].guidance"),
        })

    evidence: list[dict[str, object]] = []
    for index, raw in enumerate(_items(data.get("evidence", []), "evidence")):
        item = _mapping(raw, f"evidence[{index}]")
        identifier = _optional_text(item.get("identifier"), f"evidence[{index}].identifier")
        url = _optional_text(item.get("url"), f"evidence[{index}].url")
        if identifier is None and url is None:
            raise ItaliaV1InputError(f"evidence[{index}] needs identifier or url")
        evidence.append({
            "source": _required_text(item.get("source"), f"evidence[{index}].source"),
            "published_at": _published_day(
                item.get("published_at"), day, f"evidence[{index}].published_at"
            ),
            "identifier": identifier,
            "url": url,
            "excerpt": _optional_text(item.get("excerpt"), f"evidence[{index}].excerpt"),
        })

    missing = [
        f"price.{field}" for field in _PRICE_FIELDS if price[field] is None
    ] + [
        f"fundamentals.{field}" for field in _FUNDAMENTAL_FIELDS
        if fundamentals[field] is None
    ]
    return {
        "identity": {
            "company_name": company_name, "ticker": ticker,
            "provider_symbol": provider_symbol, "isin": isin, "as_of": day,
        },
        "price": price,
        "fundamentals": fundamentals,
        "events": events,
        "evidence": evidence,
        "missing_fields": missing,
    }


@dataclass(frozen=True)
class ThesisRecord:
    """Serializable, validated result of one Italia V1 analysis."""

    thesis_id: str
    company_name: str
    ticker: str
    isin: str | None
    as_of: str
    facts: dict[str, object]
    fundamental_analysis: str
    earnings_and_news_analysis: str
    price_context: str
    bull_case: str
    bear_case: str
    catalysts: list[str]
    risks: list[str]
    thesis_invalidation: str
    status: str
    confidence: str
    evidence: list[dict[str, object]]
    critic_notes: list[str]
    created_at: str
    model_version: str
    prompt_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.thesis_id, str) or not _THESIS_ID.fullmatch(self.thesis_id):
            raise ItaliaV1InputError("thesis_id must be a 32-character lowercase hex ID")
        _required_text(self.company_name, "company_name")
        _required_text(self.ticker, "ticker")
        _optional_text(self.isin, "isin")
        day = _day(self.as_of, "as_of")
        if self.status not in STATUSES:
            raise ItaliaV1InputError(f"status must be one of {sorted(STATUSES)}")
        if self.confidence not in CONFIDENCES:
            raise ItaliaV1InputError(f"confidence must be one of {sorted(CONFIDENCES)}")
        for field in _ANALYSIS_TEXT_FIELDS:
            _required_text(getattr(self, field), field)
        for field in ("catalysts", "risks", "critic_notes"):
            for index, value in enumerate(_items(getattr(self, field), field)):
                _required_text(value, f"{field}[{index}]")
        _required_text(self.model_version, "model_version")
        _required_text(self.prompt_version, "prompt_version")
        try:
            created = datetime.fromisoformat(self.created_at)
        except (TypeError, ValueError) as exc:
            raise ItaliaV1InputError("created_at must be an ISO timestamp") from exc
        if created.tzinfo is None or created.utcoffset() is None:
            raise ItaliaV1InputError("created_at must include a timezone")
        facts = _mapping(self.facts, "facts")
        identity = _mapping(facts.get("identity"), "facts.identity")
        if (
            identity.get("company_name"), identity.get("ticker"),
            identity.get("isin"), identity.get("as_of"),
        ) != (
            self.company_name, self.ticker, self.isin, day
        ):
            raise ItaliaV1InputError("facts.identity must match the thesis identity")
        canonical_facts = build_facts({
            **identity,
            "price": facts.get("price"),
            "fundamentals": facts.get("fundamentals"),
            "events": facts.get("events"),
            "evidence": facts.get("evidence"),
        }, day)
        if facts != canonical_facts:
            raise ItaliaV1InputError("facts are not a canonical point-in-time fact block")
        if not isinstance(self.evidence, list):
            raise ItaliaV1InputError("evidence must be a list")
        if self.evidence != facts.get("evidence"):
            raise ItaliaV1InputError("evidence must match facts.evidence")
        for index, item in enumerate(self.evidence):
            record = _mapping(item, f"evidence[{index}]")
            _published_day(record.get("published_at"), day, f"evidence[{index}].published_at")
        _json_copy(asdict(self), "ThesisRecord")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible record."""
        return asdict(self)


def analyze_company(
    company_input: Mapping[str, object], as_of: str | date,
    provider: AnalystCriticProvider,
) -> ThesisRecord:
    """Build facts, request analyst and critic outputs, then assemble a thesis."""

    facts = build_facts(company_input, as_of)
    draft = _mapping(provider.analyze(_json_copy(facts, "facts")), "analyst result")
    analysis = {field: _required_text(draft.get(field), field) for field in _ANALYSIS_TEXT_FIELDS}
    for field in ("catalysts", "risks"):
        analysis[field] = [
            _required_text(item, f"{field}[{index}]")
            for index, item in enumerate(_items(draft.get(field), field))
        ]
    proposed_status = draft.get("proposed_status")
    confidence = draft.get("confidence")
    if proposed_status not in STATUSES or confidence not in CONFIDENCES:
        raise ItaliaV1InputError("analyst must propose a valid status and confidence")

    critique = _mapping(
        provider.critique(_json_copy(facts, "facts"), _json_copy(draft, "analyst result")),
        "critic result",
    )
    notes = [
        _required_text(item, f"critic_notes[{index}]")
        for index, item in enumerate(_items(critique.get("notes"), "critic notes"))
    ]
    status = critique.get("status")
    final_confidence = critique.get("confidence")
    if status not in STATUSES or final_confidence not in CONFIDENCES:
        raise ItaliaV1InputError("critic must return a valid final status and confidence")
    identity = facts["identity"]
    return ThesisRecord(
        thesis_id=uuid4().hex,
        company_name=identity["company_name"],
        ticker=identity["ticker"],
        isin=identity["isin"],
        as_of=identity["as_of"],
        facts=facts,
        catalysts=analysis["catalysts"],
        risks=analysis["risks"],
        status=status,
        confidence=final_confidence,
        evidence=facts["evidence"],
        critic_notes=notes,
        created_at=datetime.now(timezone.utc).isoformat(),
        model_version=_required_text(provider.model_version, "model_version"),
        prompt_version=PROMPT_VERSION,
        **{field: analysis[field] for field in _ANALYSIS_TEXT_FIELDS},
    )


def save_thesis(thesis: ThesisRecord, storage_dir: str | Path = "theses") -> Path:
    """Save one thesis as UTF-8 JSON and return its path."""

    if not isinstance(thesis, ThesisRecord):
        raise ItaliaV1InputError("thesis must be a ThesisRecord")
    thesis.__post_init__()
    directory = Path(storage_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{thesis.thesis_id}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(thesis.to_dict(), handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")
    return path


def load_thesis(thesis_id: str, storage_dir: str | Path = "theses") -> ThesisRecord:
    """Load and validate a thesis by its ID."""

    if not isinstance(thesis_id, str) or not _THESIS_ID.fullmatch(thesis_id):
        raise ItaliaV1InputError("thesis_id must be a 32-character lowercase hex ID")
    path = Path(storage_dir) / f"{thesis_id}.json"
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("thesis_id") != thesis_id:
        raise ItaliaV1InputError("saved thesis_id does not match the requested ID")
    return ThesisRecord(**payload)
