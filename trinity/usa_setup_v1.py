"""Deterministic long-only setup engine for frozen USA V2 research theses."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date
import json
from pathlib import Path
from typing import Mapping, Sequence

from trinity.paths import price_cache


PRICE_CACHE = price_cache()
THESIS_CACHE = Path("data/trinity_usa_v2")
TICKERS = ("AAPL", "JPM", "JNJ", "XOM", "WMT", "CAT", "NEE", "AMZN", "PLD", "LIN")
RESEARCH_STATUSES = frozenset({"PASS", "WATCH", "INVESTIGATE"})
LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})
REGIMES = frozenset({"UPTREND", "NEUTRAL", "DOWNTREND"})
SETUP_TYPES = frozenset({"PULLBACK", "BREAKOUT", "NO_SETUP"})


@dataclass(frozen=True)
class SetupRecord:
    ticker: str
    as_of: str
    research_status: str
    evidence_confidence: str
    thesis_strength: str
    technical_regime: str
    setup_type: str
    entry_condition: str | None
    entry_level: float | None
    stop_level: float | None
    stop_distance_pct: float | None
    tp1: float | None
    tp2: float | None
    risk_per_share: float | None
    reward_tp1: float | None
    reward_tp2: float | None
    rr_tp1: float | None
    rr_tp2: float | None
    close: float
    atr14: float
    rsi14: float
    sma20: float
    sma50: float
    sma200: float
    distance_sma20_pct: float
    distance_sma50_pct: float
    distance_sma200_pct: float
    return_5d: float
    return_20d: float
    return_60d: float
    high_20d: float
    low_20d: float
    average_volume_20d: float
    relative_volume: float
    reason_codes: tuple[str, ...]
    invalidation: str

    def __post_init__(self) -> None:
        if not self.ticker or self.research_status not in RESEARCH_STATUSES:
            raise ValueError("invalid setup research identity")
        if self.evidence_confidence not in LEVELS or self.thesis_strength not in LEVELS:
            raise ValueError("invalid setup decision level")
        if self.technical_regime not in REGIMES or self.setup_type not in SETUP_TYPES:
            raise ValueError("invalid setup classification")
        if self.setup_type == "NO_SETUP":
            if any(value is not None for value in (
                self.entry_level, self.stop_level, self.tp1, self.tp2,
                self.risk_per_share, self.rr_tp1, self.rr_tp2,
            )):
                raise ValueError("NO_SETUP cannot contain operational levels")
            if not self.reason_codes:
                raise ValueError("NO_SETUP requires reason codes")
        else:
            if any(value is None for value in (
                self.entry_condition, self.entry_level, self.stop_level,
                self.tp1, self.tp2, self.risk_per_share, self.rr_tp1, self.rr_tp2,
            )):
                raise ValueError("operational setup requires complete levels")
            if not (self.stop_level < self.entry_level < self.tp1 < self.tp2):
                raise ValueError("setup levels must be ordered stop < entry < TP1 < TP2")
            if self.rr_tp1 < 1.5:
                raise ValueError("operational setup requires RR1 >= 1.5")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _prepare_bars(
    bars: Sequence[Mapping[str, object]], as_of: str,
) -> list[dict[str, float | str]]:
    cutoff = date.fromisoformat(as_of)
    prepared: list[dict[str, float | str]] = []
    seen: set[str] = set()
    for raw in bars:
        bar_date = str(raw.get("date") or "")
        parsed_date = date.fromisoformat(bar_date)
        if parsed_date > cutoff:
            continue
        if bar_date in seen:
            raise ValueError(f"duplicate OHLCV date: {bar_date}")
        seen.add(bar_date)
        values = {field: float(raw[field]) for field in ("open", "high", "low", "close", "volume")}
        if (min(values[field] for field in ("open", "high", "low", "close")) <= 0
                or values["volume"] < 0
                or values["high"] < max(values["open"], values["close"])
                or values["low"] > min(values["open"], values["close"])):
            raise ValueError(f"invalid OHLCV bar: {bar_date}")
        prepared.append({"date": bar_date, **values})
    prepared.sort(key=lambda item: str(item["date"]))
    return prepared


def _sma(values: Sequence[float], window: int) -> float:
    return sum(values[-window:]) / window


def _atr14(bars: Sequence[Mapping[str, float | str]]) -> float:
    ranges: list[float] = []
    for index in range(len(bars) - 14, len(bars)):
        current = bars[index]
        previous_close = float(bars[index - 1]["close"])
        ranges.append(max(
            float(current["high"]) - float(current["low"]),
            abs(float(current["high"]) - previous_close),
            abs(float(current["low"]) - previous_close),
        ))
    return sum(ranges) / 14


def _rsi14(closes: Sequence[float]) -> float:
    changes = [closes[index] - closes[index - 1]
               for index in range(len(closes) - 14, len(closes))]
    average_gain = sum(max(change, 0.0) for change in changes) / 14
    average_loss = sum(max(-change, 0.0) for change in changes) / 14
    if average_gain == 0 and average_loss == 0:
        return 50.0
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)


def _technical_metrics(
    bars: Sequence[Mapping[str, float | str]],
) -> dict[str, float]:
    closes = [float(item["close"]) for item in bars]
    latest = bars[-1]
    close = closes[-1]
    sma20, sma50, sma200 = (_sma(closes, window) for window in (20, 50, 200))
    atr14 = _atr14(bars)
    previous_volumes = [float(item["volume"]) for item in bars[-21:-1]]
    average_volume = sum(previous_volumes) / 20
    high20 = max(float(item["high"]) for item in bars[-20:])
    low20 = min(float(item["low"]) for item in bars[-20:])
    prior_high20 = max(float(item["high"]) for item in bars[-21:-1])
    high60 = max(float(item["high"]) for item in bars[-60:])
    metrics = {
        "close": close,
        "sma20": sma20,
        "sma50": sma50,
        "sma200": sma200,
        "distance_sma20_pct": 100 * (close / sma20 - 1),
        "distance_sma50_pct": 100 * (close / sma50 - 1),
        "distance_sma200_pct": 100 * (close / sma200 - 1),
        "return_5d": 100 * (close / closes[-6] - 1),
        "return_20d": 100 * (close / closes[-21] - 1),
        "return_60d": 100 * (close / closes[-61] - 1),
        "high_20d": high20,
        "low_20d": low20,
        "prior_high_20d": prior_high20,
        "high_60d": high60,
        "atr14": atr14,
        "rsi14": _rsi14(closes),
        "average_volume_20d": average_volume,
        "relative_volume": float(latest["volume"]) / average_volume if average_volume else 0.0,
    }
    return metrics


def _regime(metrics: Mapping[str, float]) -> str:
    close = metrics["close"]
    sma20, sma50, sma200 = metrics["sma20"], metrics["sma50"], metrics["sma200"]
    if close > sma50 > sma200 and sma20 >= sma50:
        return "UPTREND"
    if close < sma50 < sma200 and sma20 <= sma50:
        return "DOWNTREND"
    return "NEUTRAL"


def _base_record(
    thesis: Mapping[str, object], as_of: str, metrics: Mapping[str, float], regime: str,
    *, setup_type: str, reason_codes: tuple[str, ...], entry_condition: str | None = None,
    entry: float | None = None, stop: float | None = None, tp1: float | None = None,
    tp2: float | None = None,
) -> SetupRecord:
    risk = entry - stop if entry is not None and stop is not None else None
    reward1 = tp1 - entry if tp1 is not None and entry is not None else None
    reward2 = tp2 - entry if tp2 is not None and entry is not None else None
    rr1 = reward1 / risk if reward1 is not None and risk and risk > 0 else None
    rr2 = reward2 / risk if reward2 is not None and risk and risk > 0 else None
    stop_distance = 100 * risk / entry if risk is not None and entry else None
    operational = setup_type != "NO_SETUP"
    invalidation = (
        f"Setup invalid below {stop:.4f} on a closing basis."
        if operational and stop is not None else
        "No operational setup; recompute only with OHLCV available at a later as_of."
    )
    return SetupRecord(
        ticker=str(thesis["ticker"]), as_of=as_of,
        research_status=str(thesis["status"]),
        evidence_confidence=str(thesis["evidence_confidence"]),
        thesis_strength=str(thesis["thesis_strength"]),
        technical_regime=regime, setup_type=setup_type,
        entry_condition=entry_condition,
        entry_level=_rounded(entry), stop_level=_rounded(stop),
        stop_distance_pct=_rounded(stop_distance), tp1=_rounded(tp1), tp2=_rounded(tp2),
        risk_per_share=_rounded(risk), reward_tp1=_rounded(reward1),
        reward_tp2=_rounded(reward2), rr_tp1=_rounded(rr1), rr_tp2=_rounded(rr2),
        close=_rounded(metrics["close"]), atr14=_rounded(metrics["atr14"]),
        rsi14=_rounded(metrics["rsi14"]), sma20=_rounded(metrics["sma20"]),
        sma50=_rounded(metrics["sma50"]), sma200=_rounded(metrics["sma200"]),
        distance_sma20_pct=_rounded(metrics["distance_sma20_pct"]),
        distance_sma50_pct=_rounded(metrics["distance_sma50_pct"]),
        distance_sma200_pct=_rounded(metrics["distance_sma200_pct"]),
        return_5d=_rounded(metrics["return_5d"]),
        return_20d=_rounded(metrics["return_20d"]),
        return_60d=_rounded(metrics["return_60d"]),
        high_20d=_rounded(metrics["high_20d"]), low_20d=_rounded(metrics["low_20d"]),
        average_volume_20d=_rounded(metrics["average_volume_20d"]),
        relative_volume=_rounded(metrics["relative_volume"]),
        reason_codes=reason_codes, invalidation=invalidation,
    )


def build_setup(
    thesis: Mapping[str, object], bars: Sequence[Mapping[str, object]], as_of: str,
) -> SetupRecord:
    """Return one deterministic setup using only bars dated on or before ``as_of``."""
    date.fromisoformat(as_of)
    status = str(thesis.get("status") or "")
    if status not in RESEARCH_STATUSES:
        raise ValueError("invalid research status")
    if str(thesis.get("as_of") or "") != as_of:
        raise ValueError("thesis as_of does not match setup as_of")
    if str(thesis.get("evidence_confidence") or "") not in LEVELS:
        raise ValueError("invalid evidence_confidence")
    if str(thesis.get("thesis_strength") or "") not in LEVELS:
        raise ValueError("invalid thesis_strength")
    if not isinstance(thesis.get("event_assessments"), list):
        raise ValueError("event_assessments must be present")
    prepared = _prepare_bars(bars, as_of)
    if len(prepared) < 201:
        zero = {
            "close": float(prepared[-1]["close"]) if prepared else 0.0,
            **{name: 0.0 for name in (
                "sma20", "sma50", "sma200", "distance_sma20_pct", "distance_sma50_pct",
                "distance_sma200_pct", "return_5d", "return_20d", "return_60d",
                "high_20d", "low_20d", "atr14", "rsi14", "average_volume_20d",
                "relative_volume",
            )},
        }
        return _base_record(thesis, as_of, zero, "NEUTRAL", setup_type="NO_SETUP",
                            reason_codes=("INSUFFICIENT_OHLCV",))
    metrics = _technical_metrics(prepared)
    regime = _regime(metrics)
    if status == "PASS":
        return _base_record(thesis, as_of, metrics, regime, setup_type="NO_SETUP",
                            reason_codes=("RESEARCH_STATUS_PASS",))
    if regime == "DOWNTREND":
        return _base_record(thesis, as_of, metrics, regime, setup_type="NO_SETUP",
                            reason_codes=("DOWNTREND_NO_LONG_SETUP",))
    if regime != "UPTREND":
        return _base_record(thesis, as_of, metrics, regime, setup_type="NO_SETUP",
                            reason_codes=("REGIME_NOT_UPTREND",))

    near_breakout = metrics["close"] >= 0.99 * metrics["prior_high_20d"]
    volume_confirmed = metrics["relative_volume"] >= 1.2
    if near_breakout and volume_confirmed:
        entry = metrics["prior_high_20d"] + 0.10 * metrics["atr14"]
        stop = entry - 2.0 * metrics["atr14"]
        if stop >= entry or metrics["atr14"] <= 0:
            return _base_record(thesis, as_of, metrics, regime, setup_type="NO_SETUP",
                                reason_codes=("STOP_NOT_DETERMINABLE",))
        risk = entry - stop
        return _base_record(
            thesis, as_of, metrics, regime, setup_type="BREAKOUT",
            reason_codes=("UPTREND_CONFIRMED", "BREAKOUT_VOLUME_CONFIRMED"),
            entry_condition=f"BUY ABOVE {entry:.4f}", entry=entry, stop=stop,
            tp1=entry + 2.0 * risk, tp2=entry + 3.0 * risk,
        )

    supports = [value for value in (metrics["sma20"], metrics["sma50"])
                if 0.99 * value <= metrics["close"] <= 1.02 * value]
    if supports:
        support = min(supports, key=lambda value: abs(metrics["close"] - value))
        entry = support + 0.25 * metrics["atr14"]
        stop = min(metrics["low_20d"], entry - 1.5 * metrics["atr14"])
        tp1 = metrics["high_20d"]
        risk = entry - stop
        if risk <= 0 or tp1 <= entry:
            return _base_record(thesis, as_of, metrics, regime, setup_type="NO_SETUP",
                                reason_codes=("STOP_OR_TARGET_NOT_DETERMINABLE",))
        rr1 = (tp1 - entry) / risk
        if rr1 < 1.5:
            return _base_record(thesis, as_of, metrics, regime, setup_type="NO_SETUP",
                                reason_codes=("RR_TP1_BELOW_1_5",))
        tp2 = max(metrics["high_60d"], entry + 2.0 * risk)
        if tp2 <= tp1:
            tp2 = entry + 3.0 * risk
        return _base_record(
            thesis, as_of, metrics, regime, setup_type="PULLBACK",
            reason_codes=("UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY"),
            entry_condition=f"BUY ON PULLBACK {support:.4f}-{entry:.4f}",
            entry=entry, stop=stop, tp1=tp1, tp2=tp2,
        )

    reasons = []
    if not near_breakout:
        reasons.append("NOT_NEAR_20D_BREAKOUT")
    if not volume_confirmed:
        reasons.append("RELATIVE_VOLUME_BELOW_1_2")
    reasons.append("NOT_NEAR_SMA20_OR_SMA50_SUPPORT")
    return _base_record(thesis, as_of, metrics, regime, setup_type="NO_SETUP",
                        reason_codes=tuple(reasons))


def _load_latest_thesis(ticker: str, as_of: str, directory: Path) -> dict[str, object]:
    candidates: list[dict[str, object]] = []
    for path in directory.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (payload.get("ticker") == ticker and payload.get("as_of") == as_of
                and "evidence_confidence" in payload and "event_assessments" in payload):
            candidates.append(payload)
    if not candidates:
        raise ValueError(f"{ticker}: no calibrated thesis for {as_of}")
    return max(candidates, key=lambda item: str(item["created_at"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic USA Setup Engine V1")
    parser.add_argument("--as-of", default="2026-09-12")
    parser.add_argument("--ticker", choices=TICKERS, nargs="+")
    parser.add_argument("--price-cache", type=Path, default=PRICE_CACHE)
    parser.add_argument("--thesis-cache", type=Path, default=THESIS_CACHE)
    args = parser.parse_args()
    failures = 0
    for ticker in (args.ticker or TICKERS):
        try:
            thesis = _load_latest_thesis(ticker, args.as_of, args.thesis_cache)
            bars = json.loads((args.price_cache / f"{ticker}.json").read_text(encoding="utf-8"))
            record = build_setup(thesis, bars, args.as_of)
            print(json.dumps(record.to_dict(), ensure_ascii=False, allow_nan=False))
        except Exception as exc:  # pragma: no cover - CLI boundary
            failures += 1
            print(json.dumps({"ticker": ticker, "error": f"{type(exc).__name__}: {exc}"}))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
