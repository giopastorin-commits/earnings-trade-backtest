"""Small, point-in-time TRINITY Italia thesis slice over caller-supplied data.

V1 uses ISO calendar dates (YYYY-MM-DD), not intraday timestamps. An ``as_of``
date includes that whole date; timestamp inputs are rejected instead of being
silently assigned a time zone or cutoff hour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Callable, Protocol
from uuid import uuid4


PROMPT_VERSION = "TRINITY_ITALIA_V1_CONTRACT"
STATUSES = frozenset({"PASS", "WATCH", "INVESTIGATE"})
CONFIDENCES = frozenset({"LOW", "MEDIUM", "HIGH"})
EVENT_CLASSIFICATIONS = frozenset({
    "NEW_INFORMATION", "EXPECTATION_CHANGE", "CONFIRMATION", "REITERATION",
    "ALREADY_KNOWN", "LOW_RELEVANCE",
})
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
    if "acquisition" in price_input:
        price["acquisition"] = _json_copy(
            _mapping(price_input["acquisition"], "price.acquisition"), "price.acquisition"
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
    if "provenance" in fundamentals_input:
        fundamentals["provenance"] = _json_copy(
            _mapping(fundamentals_input["provenance"], "fundamentals.provenance"),
            "fundamentals.provenance",
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
        record = {
            "source": _required_text(item.get("source"), f"evidence[{index}].source"),
            "published_at": _published_day(
                item.get("published_at"), day, f"evidence[{index}].published_at"
            ),
            "identifier": identifier,
            "url": url,
            "excerpt": _optional_text(item.get("excerpt"), f"evidence[{index}].excerpt"),
        }
        for field in (
            "title", "retrieved_at", "content_sha256", "raw_path", "published_at_precision",
            "document_kind", "filing_date", "accepted_at", "form", "accession_number",
        ):
            if field in item:
                record[field] = _required_text(item[field], f"evidence[{index}].{field}")
        evidence.append(record)

    financial_facts = _mapping(data.get("financial_facts", {}), "financial_facts")
    known_evidence = {item["identifier"] for item in evidence if item["identifier"]}
    for field, raw in financial_facts.items():
        if raw is None:
            continue
        fact = _mapping(raw, f"financial_facts.{field}")
        if fact.get("unit") not in {"EUR", "PERCENT"} or fact.get("source_unit") not in {
            "EUR", "EUR_THOUSANDS", "EUR_MILLIONS", "EUR_BILLIONS", "PERCENT"
        }:
            raise ItaliaV1InputError(f"financial_facts.{field} has unverified unit")
        if fact.get("evidence_identifier") not in known_evidence:
            raise ItaliaV1InputError(f"financial_facts.{field} lacks matching evidence")
        _required_text(fact.get("period"), f"financial_facts.{field}.period")
        _required_text(fact.get("extraction_method"), f"financial_facts.{field}.extraction_method")
        _required_text(fact.get("source_excerpt"), f"financial_facts.{field}.source_excerpt")
        values = fact.get("value") if isinstance(fact.get("value"), list) else [fact.get("value")]
        for value in values:
            _number(value, f"financial_facts.{field}.value")

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
            "schema_type": _optional_text(data.get("schema_type"), "schema_type"),
        },
        "price": price,
        "fundamentals": fundamentals,
        "financial_facts": _json_copy(financial_facts, "financial_facts"),
        "events": events,
        "evidence": evidence,
        "missing_fields": missing,
    }


def build_facts_v2(company_input: Mapping[str, object], as_of: str | date) -> dict[str, object]:
    """Build additive USA facts V2 while preserving the complete V1 projection."""

    facts = build_facts(company_input, as_of)
    source_classes = {"PRIMARY_SEC", "ISSUER_RELEASE", "THIRD_PARTY_REPORT"}
    confirmation_states = {"CONFIRMED", "REPORTED", "RUMORED", "UNKNOWN"}
    for index, event in enumerate(facts["events"]):
        event_facts = _mapping(event["facts"], f"events[{index}].facts")
        source_class = _required_text(
            event_facts.get("source_class"), f"events[{index}].facts.source_class"
        )
        confirmation_state = _required_text(
            event_facts.get("confirmation_state"),
            f"events[{index}].facts.confirmation_state",
        )
        if source_class not in source_classes:
            raise ItaliaV1InputError(
                f"events[{index}].facts.source_class must be one of {sorted(source_classes)}"
            )
        if confirmation_state not in confirmation_states:
            raise ItaliaV1InputError(
                "events[{}].facts.confirmation_state must be one of {}".format(
                    index, sorted(confirmation_states)
                )
            )
        issuer_release = event_facts.get("issuer_release")
        primary_source = event_facts.get("primary_source")
        if source_class == "PRIMARY_SEC":
            if primary_source is not True or issuer_release is True or confirmation_state != "CONFIRMED":
                raise ItaliaV1InputError(
                    f"events[{index}] PRIMARY_SEC provenance is inconsistent"
                )
        elif source_class == "ISSUER_RELEASE":
            if issuer_release is not True or primary_source is True or confirmation_state != "CONFIRMED":
                raise ItaliaV1InputError(
                    f"events[{index}] ISSUER_RELEASE provenance is inconsistent"
                )
        elif (
            event["kind"] != "CORPORATE_EVENT"
            or issuer_release is not None
            or primary_source is not None
            or confirmation_state not in {"REPORTED", "RUMORED"}
        ):
            raise ItaliaV1InputError(
                f"events[{index}] THIRD_PARTY_REPORT provenance is inconsistent"
            )
    facts["facts_contract_version"] = "2"
    return facts


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
    claim_refs: list[dict[str, object]] = field(default_factory=list)
    rejected_claim_refs: list[dict[str, object]] = field(default_factory=list)
    evidence_confidence: str = "LOW"
    thesis_strength: str = "LOW"
    analyst_status: str = "PASS"
    analyst_thesis_strength: str = "LOW"
    analyst_evidence_confidence: str = "LOW"
    critic_status: str = "PASS"
    critic_thesis_strength: str = "LOW"
    critic_evidence_confidence: str = "LOW"
    event_assessments: list[dict[str, object]] = field(default_factory=list)

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
        for field_name in ("evidence_confidence", "thesis_strength",
                           "analyst_thesis_strength", "analyst_evidence_confidence",
                           "critic_thesis_strength", "critic_evidence_confidence"):
            if getattr(self, field_name) not in CONFIDENCES:
                raise ItaliaV1InputError(
                    f"{field_name} must be one of {sorted(CONFIDENCES)}"
                )
        for field_name in ("analyst_status", "critic_status"):
            if getattr(self, field_name) not in STATUSES:
                raise ItaliaV1InputError(f"{field_name} must be one of {sorted(STATUSES)}")
        if self.confidence != self.evidence_confidence:
            raise ItaliaV1InputError("confidence must mirror evidence_confidence")
        for field in _ANALYSIS_TEXT_FIELDS:
            _required_text(getattr(self, field), field)
        for field in ("catalysts", "risks", "critic_notes"):
            for index, value in enumerate(_items(getattr(self, field), field)):
                _required_text(value, f"{field}[{index}]")
        for collection_name in ("claim_refs", "rejected_claim_refs"):
            for index, raw in enumerate(_items(getattr(self, collection_name), collection_name)):
                claim = _mapping(raw, f"{collection_name}[{index}]")
                for key in ("claim_id", "field", "text", "materiality"):
                    _required_text(claim.get(key), f"{collection_name}[{index}].{key}")
                for fact_index, fact_id in enumerate(_items(
                    claim.get("fact_ids"), f"{collection_name}[{index}].fact_ids"
                )):
                    _required_text(fact_id, f"{collection_name}[{index}].fact_ids[{fact_index}]")
        for index, raw in enumerate(_items(self.event_assessments, "event_assessments")):
            assessment = _mapping(raw, f"event_assessments[{index}]")
            for key in ("event_id", "classification", "rationale"):
                _required_text(assessment.get(key), f"event_assessments[{index}].{key}")
            if assessment.get("classification") not in EVENT_CLASSIFICATIONS:
                raise ItaliaV1InputError(
                    f"event_assessments[{index}].classification must be one of "
                    f"{sorted(EVENT_CLASSIFICATIONS)}"
                )
            if not isinstance(assessment.get("material"), bool):
                raise ItaliaV1InputError(f"event_assessments[{index}].material must be boolean")
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
        facts_builder = (
            build_facts_v2 if facts.get("facts_contract_version") == "2" else build_facts
        )
        canonical_facts = facts_builder({
            **identity,
            "price": facts.get("price"),
            "fundamentals": facts.get("fundamentals"),
            "financial_facts": facts.get("financial_facts"),
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
    *, facts_builder: Callable[[Mapping[str, object], str | date], dict[str, object]] = build_facts,
) -> ThesisRecord:
    """Build facts, request analyst and critic outputs, then assemble a thesis."""

    facts = facts_builder(company_input, as_of)
    draft = _mapping(provider.analyze(_json_copy(facts, "facts")), "analyst result")
    analysis = {field: _required_text(draft.get(field), field) for field in _ANALYSIS_TEXT_FIELDS}
    for field in ("catalysts", "risks"):
        analysis[field] = [
            _required_text(item, f"{field}[{index}]")
            for index, item in enumerate(_items(draft.get(field), field))
        ]
    proposed_status = draft.get("proposed_status")
    analyst_evidence_confidence = draft.get("evidence_confidence", draft.get("confidence"))
    analyst_thesis_strength = draft.get("thesis_strength", "LOW")
    if (proposed_status not in STATUSES or analyst_evidence_confidence not in CONFIDENCES
            or analyst_thesis_strength not in CONFIDENCES):
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
    final_confidence = critique.get("evidence_confidence", critique.get("confidence"))
    thesis_strength = critique.get("thesis_strength", analyst_thesis_strength)
    critic_status = critique.get("critic_status", status)
    critic_evidence_confidence = critique.get("critic_evidence_confidence", final_confidence)
    critic_thesis_strength = critique.get("critic_thesis_strength", thesis_strength)
    if (status not in STATUSES or final_confidence not in CONFIDENCES
            or thesis_strength not in CONFIDENCES or critic_status not in STATUSES
            or critic_evidence_confidence not in CONFIDENCES
            or critic_thesis_strength not in CONFIDENCES):
        raise ItaliaV1InputError("critic must return a valid final status and confidence")
    event_assessments = critique.get("event_assessments", draft.get("event_assessments", []))
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
        evidence_confidence=final_confidence,
        thesis_strength=thesis_strength,
        analyst_status=proposed_status,
        analyst_thesis_strength=analyst_thesis_strength,
        analyst_evidence_confidence=analyst_evidence_confidence,
        critic_status=critic_status,
        critic_thesis_strength=critic_thesis_strength,
        critic_evidence_confidence=critic_evidence_confidence,
        event_assessments=[dict(_mapping(item, "event assessment"))
                           for item in _items(event_assessments, "event_assessments")],
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
