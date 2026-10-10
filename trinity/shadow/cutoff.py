"""Decision cutoff derived from the completed session established by the funnel."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


LIVE_RESEARCH_CUTOFF_METHOD = "LIVE_RESEARCH_CUTOFF_V1"


@dataclass(frozen=True)
class DecisionCutoff:
    session_date: str
    utc: str
    america_new_york: str


@dataclass(frozen=True)
class LiveResearchCutoff:
    verified_price_session: str
    technical_cutoff_utc: str
    research_cutoff_utc: str
    captured_at_utc: str
    method_version: str = LIVE_RESEARCH_CUTOFF_METHOD


def capture_live_research_cutoff(
    *, verified_price_session: str, technical_cutoff_utc: str,
    now_utc: datetime | None = None,
) -> LiveResearchCutoff:
    """Capture the single immutable source boundary for one live research run."""

    captured = now_utc or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        raise ValueError("live research cutoff must be timezone-aware")
    captured_text = captured.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return LiveResearchCutoff(
        verified_price_session=verified_price_session,
        technical_cutoff_utc=technical_cutoff_utc,
        research_cutoff_utc=captured_text,
        captured_at_utc=captured_text,
    )


def cutoff_for_verified_session(
    session_date: str, latest_dates: list[str], *, minimum_consensus: float = 0.90,
) -> DecisionCutoff:
    parsed = date.fromisoformat(session_date)
    if not latest_dates:
        raise ValueError("latest-session determination has no price observations")
    frequency = Counter(latest_dates)
    if frequency[session_date] / len(latest_dates) < minimum_consensus:
        raise ValueError("latest-session determination lacks 90% universe consensus")
    eastern = datetime.combine(parsed, time(16, 0), ZoneInfo("America/New_York"))
    utc = eastern.astimezone(timezone.utc)
    return DecisionCutoff(
        session_date=session_date,
        utc=utc.isoformat().replace("+00:00", "Z"),
        america_new_york=eastern.isoformat(),
    )


def filter_timestamped_records(
    records: list[dict[str, object]], cutoff_utc: str, *, fields: tuple[str, ...],
) -> tuple[list[dict[str, object]], int]:
    cutoff = datetime.fromisoformat(cutoff_utc.replace("Z", "+00:00"))
    kept, excluded = [], 0
    for record in records:
        stamp = next((record.get(field) for field in fields if record.get(field)), None)
        if stamp is None:
            kept.append(record)
            continue
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.astimezone(timezone.utc) <= cutoff:
            kept.append(record)
        else:
            excluded += 1
    return kept, excluded
