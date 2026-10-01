"""Read-only Telegram output adapter for committed Ledger V1.4 Setups."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests

from trinity.ledger import LedgerStorage


BOT_TOKEN_ENV = "TRINITY_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "TRINITY_TELEGRAM_CHAT_ID"
TELEGRAM_TIMEOUT_SECONDS = 15


class TelegramNotificationError(RuntimeError):
    """A safe, credential-free notification validation or transport error."""


@dataclass(frozen=True)
class TelegramNotification:
    setup_id: str
    run_id: str
    research_id: str
    ticker: str
    setup_type: str
    technical_regime: str
    research_status: str
    evidence_confidence: str
    thesis_strength: str
    entry: str | None
    stop: str | None
    tp1: str | None
    tp2: str | None
    rr1: str | None
    rr2: str | None
    reason_codes: tuple[str, ...]
    resolved_pit_class: str
    resolved_record_class: str


def load_committed_setup(
    storage: LedgerStorage,
    *,
    setup_id: str,
    run_id: str | None = None,
) -> TelegramNotification:
    """Load one explicit Setup and fail closed unless its committed closure verifies."""

    try:
        setup = storage.get_setup(setup_id)
    except Exception as exc:
        raise TelegramNotificationError("Setup does not exist or failed validation") from exc
    if setup.run_id is None:
        raise TelegramNotificationError("Setup is not committed")
    if run_id is not None and run_id != setup.run_id:
        raise TelegramNotificationError("Setup is not committed to the requested run")

    try:
        node = storage.get_derivation_node(setup.derivation_node_id)
        run = storage.get_run(setup.run_id)
        manifest_artifact = storage.get_artifact(run.result_manifest_artifact_id)
        manifest = json.loads(manifest_artifact.payload)
    except Exception as exc:
        raise TelegramNotificationError("Committed Setup lineage failed validation") from exc

    if (
        node.node_kind != "SETUP"
        or node.entity_type != "setup"
        or node.entity_id != setup.setup_id
        or node.run_id != setup.run_id
        or node.attempt_id != setup.attempt_id
    ):
        raise TelegramNotificationError("SETUP derivation node does not match committed Setup")
    if manifest_artifact.artifact_kind != "ledger.run-result-manifest.v3":
        raise TelegramNotificationError("Setup notification requires Manifest V3")
    if getattr(run, "run_id", setup.run_id) != setup.run_id:
        raise TelegramNotificationError("Setup and committed run identity differ")
    if not isinstance(manifest.get("setup_ids"), list) or setup.setup_id not in manifest["setup_ids"]:
        raise TelegramNotificationError("Setup is absent from committed Manifest V3")
    if setup.derivation_node_id not in manifest.get("derivation_node_ids", []):
        raise TelegramNotificationError("Setup node is absent from committed derivation closure")

    rows = storage.connection.execute(
        """
        SELECT research_id, research_derivation_node_id
        FROM setup_research_lineage
        WHERE setup_id = ? AND lineage_role = 'PRIMARY'
        """,
        (setup.setup_id,),
    ).fetchall()
    if len(rows) != 1:
        raise TelegramNotificationError("Setup does not have exact PRIMARY Research lineage")
    research_id, research_node_id = rows[0]
    try:
        research = storage.get_research_record(research_id)
        research_node = storage.get_derivation_node(research_node_id)
    except Exception as exc:
        raise TelegramNotificationError("Referenced Research failed validation") from exc
    if (
        research.run_id != setup.run_id
        or research.attempt_id != setup.attempt_id
        or research.derivation_node_id != research_node_id
        or research_node.node_kind != "RESEARCH"
        or research_node.entity_type != "research_record"
        or research_node.entity_id != research_id
        or research_node.run_id != setup.run_id
        or research_node_id not in manifest.get("derivation_node_ids", [])
        or research_id not in manifest.get("research_ids", [])
    ):
        raise TelegramNotificationError("Research is not in the same committed closure")
    required_parent_ids = {
        edge.parent_node_id
        for edge in storage.list_derivation_edges(setup.derivation_node_id)
        if edge.required
    }
    if research_node_id not in required_parent_ids:
        raise TelegramNotificationError("Setup does not require its referenced Research node")

    try:
        result = storage.validate_v14_artifact(setup.result_artifact_id)
    except Exception as exc:
        raise TelegramNotificationError("Setup result Artifact failed validation") from exc
    if not isinstance(result, Mapping):
        raise TelegramNotificationError("Setup result Artifact is malformed")

    return TelegramNotification(
        setup_id=setup.setup_id,
        run_id=setup.run_id,
        research_id=research_id,
        ticker=result["ticker"],
        setup_type=result["setup_type"],
        technical_regime=result["technical_regime"],
        research_status=result["research_status"],
        evidence_confidence=result["evidence_confidence"],
        thesis_strength=result["thesis_strength"],
        entry=result["entry_level"],
        stop=result["stop_level"],
        tp1=result["tp1"],
        tp2=result["tp2"],
        rr1=result["rr_tp1"],
        rr2=result["rr_tp2"],
        reason_codes=tuple(result["reason_codes"]),
        resolved_pit_class=manifest["resolved_pit_class"],
        resolved_record_class=manifest["resolved_record_class"],
    )


def render_telegram_message(notification: TelegramNotification) -> str:
    """Render deterministic plain text without changing authoritative values."""

    lines = [
        f"TRINITY — {notification.ticker}",
        "",
        f"Setup: {notification.setup_type}",
        f"Regime: {notification.technical_regime}",
        f"Research: {notification.research_status}",
        f"Evidence confidence: {notification.evidence_confidence}",
        f"Thesis strength: {notification.thesis_strength}",
        "",
    ]
    if notification.setup_type != "NO_SETUP":
        levels = (
            ("Entry", notification.entry),
            ("Stop", notification.stop),
            ("TP1", notification.tp1),
            ("TP2", notification.tp2),
        )
        if any(value is None for _, value in levels) or notification.rr1 is None or notification.rr2 is None:
            raise TelegramNotificationError("Operational Setup has incomplete levels")
        lines.extend(f"{label}: {_display_level(value)}" for label, value in levels)
        lines.extend(("", f"RR1: {_display_rr(notification.rr1)}", f"RR2: {_display_rr(notification.rr2)}", ""))
    lines.append("Motivi:")
    lines.extend(f"• {reason}" for reason in notification.reason_codes)
    lines.extend(("", f"PIT: {notification.resolved_pit_class}", f"Record: {notification.resolved_record_class}"))
    return "\n".join(lines)


def send_telegram_message(
    text: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    transport: Callable[..., Any] | None = None,
    timeout: int = TELEGRAM_TIMEOUT_SECONDS,
) -> None:
    """Send through the Bot API; all failures are sanitized of credentials."""

    effective_token = token if token is not None else os.environ.get(BOT_TOKEN_ENV)
    effective_chat = chat_id if chat_id is not None else os.environ.get(CHAT_ID_ENV)
    if not effective_token or not effective_chat:
        raise TelegramNotificationError("Telegram credentials are missing")
    sender = transport or requests.post
    url = f"https://api.telegram.org/bot{effective_token}/sendMessage"
    try:
        response = sender(
            url,
            json={"chat_id": effective_chat, "text": text},
            timeout=timeout,
        )
        status_code = int(response.status_code)
        body = response.json()
        if status_code < 200 or status_code >= 300 or not isinstance(body, Mapping) or body.get("ok") is not True:
            raise TelegramNotificationError(
                f"Telegram API rejected the message (HTTP {status_code})"
            )
    except TelegramNotificationError:
        raise
    except Exception:
        raise TelegramNotificationError("Telegram API request failed") from None


def _display_level(value: str) -> str:
    _parse_decimal(value)
    return value


def _display_rr(value: str) -> str:
    return format(_parse_decimal(value).quantize(Decimal("0.01")), ".2f")


def _parse_decimal(value: str) -> Decimal:
    try:
        decimal = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TelegramNotificationError("Setup contains an invalid decimal value") from exc
    if not decimal.is_finite():
        raise TelegramNotificationError("Setup contains a non-finite decimal value")
    return decimal


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send one explicitly identified committed TRINITY Setup to Telegram"
    )
    parser.add_argument("--ledger-db", required=True, type=Path)
    parser.add_argument("--setup-id", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        with LedgerStorage.open(args.ledger_db) as storage:
            notification = load_committed_setup(
                storage, setup_id=args.setup_id, run_id=args.run_id
            )
        text = render_telegram_message(notification)
        if args.dry_run:
            print(text)
        else:
            send_telegram_message(text)
            print(f"Telegram notification sent for setup {notification.setup_id}")
    except TelegramNotificationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
