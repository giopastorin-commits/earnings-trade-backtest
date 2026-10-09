from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trinity.ledger import setup_result_value
from trinity.notifications import telegram
from trinity.notifications.telegram import (
    TelegramNotification,
    TelegramNotificationError,
    deliver_completed_run,
    load_committed_setup,
    render_telegram_message,
    send_telegram_message,
)
from trinity.pilots.jnj_telegram import materialize_jnj_telegram_pilot
from trinity.usa_setup_v1 import build_setup


SETUP_ID = "11111111-1111-4111-8111-111111111111"
RUN_ID = "22222222-2222-4222-8222-222222222222"
ATTEMPT_ID = "33333333-3333-4333-8333-333333333333"
RESEARCH_ID = "44444444-4444-4444-8444-444444444444"
SETUP_NODE_ID = "setup-node"
RESEARCH_NODE_ID = "research-node"


def _result(setup_type="PULLBACK"):
    operational = setup_type != "NO_SETUP"
    return {
        "ticker": "JNJ",
        "setup_type": setup_type,
        "technical_regime": "UPTREND",
        "research_status": "INVESTIGATE",
        "evidence_confidence": "HIGH",
        "thesis_strength": "MEDIUM",
        "entry_level": "264.022" if operational else None,
        "stop_level": "256.1417" if operational else None,
        "tp1": "281.07" if operational else None,
        "tp2": "287.6628" if operational else None,
        "rr_tp1": "2.1634" if operational else None,
        "rr_tp2": "3" if operational else None,
        "reason_codes": ["UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY"],
    }


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, storage):
        self.storage = storage

    def execute(self, _sql, parameters):
        assert parameters == (SETUP_ID,)
        return _Rows(self.storage.lineage_rows)


class _Storage:
    def __init__(self, *, committed=True, manifest_setup=True, lineage=True):
        self.setup = SimpleNamespace(
            setup_id=SETUP_ID,
            run_id=RUN_ID if committed else None,
            attempt_id=ATTEMPT_ID,
            derivation_node_id=SETUP_NODE_ID,
            result_artifact_id="setup-result",
        )
        self.setup_node = SimpleNamespace(
            node_kind="SETUP", entity_type="setup", entity_id=SETUP_ID,
            run_id=RUN_ID, attempt_id=ATTEMPT_ID,
        )
        self.research = SimpleNamespace(
            research_id=RESEARCH_ID, run_id=RUN_ID, attempt_id=ATTEMPT_ID,
            derivation_node_id=RESEARCH_NODE_ID,
        )
        self.research_node = SimpleNamespace(
            node_kind="RESEARCH", entity_type="research_record",
            entity_id=RESEARCH_ID, run_id=RUN_ID, attempt_id=ATTEMPT_ID,
        )
        self.manifest = {
            "setup_ids": [SETUP_ID] if manifest_setup else [],
            "research_ids": [RESEARCH_ID],
            "derivation_node_ids": [RESEARCH_NODE_ID, SETUP_NODE_ID],
            "resolved_pit_class": "RECONSTRUCTED_NOT_ARCHIVED",
            "resolved_record_class": "LEGACY_NON_LEDGER_ARTIFACT",
        }
        self.run = SimpleNamespace(run_id=RUN_ID, result_manifest_artifact_id="manifest")
        self.manifest_artifact = SimpleNamespace(
            artifact_kind="ledger.run-result-manifest.v3",
            payload=json.dumps(self.manifest).encode(),
        )
        self.lineage_rows = [(RESEARCH_ID, RESEARCH_NODE_ID)] if lineage else []
        self.connection = _Connection(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get_setup(self, setup_id):
        if setup_id != SETUP_ID:
            raise KeyError(setup_id)
        return self.setup

    def get_derivation_node(self, node_id):
        return {
            SETUP_NODE_ID: self.setup_node,
            RESEARCH_NODE_ID: self.research_node,
        }[node_id]

    def get_run(self, run_id):
        if run_id != RUN_ID:
            raise KeyError(run_id)
        return self.run

    def get_artifact(self, artifact_id):
        if artifact_id != "manifest":
            raise KeyError(artifact_id)
        return self.manifest_artifact

    def get_research_record(self, research_id):
        if research_id != RESEARCH_ID:
            raise KeyError(research_id)
        return self.research

    def list_derivation_edges(self, node_id):
        assert node_id == SETUP_NODE_ID
        return [SimpleNamespace(parent_node_id=RESEARCH_NODE_ID, required=True)]

    def validate_v14_artifact(self, artifact_id):
        assert artifact_id == "setup-result"
        return _result()


def _notification(**changes):
    values = dict(
        setup_id=SETUP_ID, run_id=RUN_ID, research_id=RESEARCH_ID,
        ticker="JNJ", setup_type="PULLBACK", technical_regime="UPTREND",
        research_status="INVESTIGATE", evidence_confidence="HIGH",
        thesis_strength="MEDIUM", entry="264.022", stop="256.1417",
        tp1="281.07", tp2="287.6628", rr1="2.1634", rr2="3",
        reason_codes=("UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY"),
        resolved_pit_class="RECONSTRUCTED_NOT_ARCHIVED",
        resolved_record_class="LEGACY_NON_LEDGER_ARTIFACT",
    )
    values.update(changes)
    return TelegramNotification(**values)


def test_valid_committed_setup_loads_and_renders():
    notification = load_committed_setup(_Storage(), setup_id=SETUP_ID, run_id=RUN_ID)
    text = render_telegram_message(notification)
    assert notification.research_id == RESEARCH_ID
    assert "TRINITY — JNJ" in text
    assert "Entry: 264.022" in text
    assert "RR1: 2.1634" in text
    assert "RR2: 3" in text


def test_uncommitted_setup_is_rejected():
    with pytest.raises(TelegramNotificationError, match="not committed"):
        load_committed_setup(_Storage(committed=False), setup_id=SETUP_ID)


def test_setup_absent_from_manifest_is_rejected():
    with pytest.raises(TelegramNotificationError, match="absent"):
        load_committed_setup(_Storage(manifest_setup=False), setup_id=SETUP_ID)


def test_wrong_run_is_rejected():
    with pytest.raises(TelegramNotificationError, match="requested run"):
        load_committed_setup(_Storage(), setup_id=SETUP_ID, run_id="wrong-run")


def test_malformed_lineage_is_rejected():
    with pytest.raises(TelegramNotificationError, match="PRIMARY Research"):
        load_committed_setup(_Storage(lineage=False), setup_id=SETUP_ID)


def test_no_setup_omits_operational_levels():
    text = render_telegram_message(_notification(
        setup_type="NO_SETUP", entry=None, stop=None, tp1=None, tp2=None,
        rr1=None, rr2=None, reason_codes=("NO_VALID_SETUP",),
    ))
    assert "Setup: NO_SETUP" in text
    assert "• NO_VALID_SETUP" in text
    for forbidden in ("Entry:", "Stop:", "TP1:", "TP2:", "RR1:", "RR2:"):
        assert forbidden not in text


def test_reason_order_and_decimal_display_are_deterministic():
    text = render_telegram_message(_notification(reason_codes=("SECOND", "FIRST")))
    assert text.index("• SECOND") < text.index("• FIRST")
    assert "Entry: 264.022" in text
    assert "Stop: 256.1417" in text
    assert "RR1: 2.1634" in text and "RR2: 3" in text


def test_dry_run_performs_no_network(monkeypatch, capsys, tmp_path):
    storage = _Storage()
    monkeypatch.setattr(telegram.LedgerStorage, "open", lambda _path: storage)
    monkeypatch.setattr(
        telegram,
        "send_telegram_message",
        lambda _text: pytest.fail("dry-run attempted network send"),
    )
    assert telegram.main([
        "--ledger-db", str(tmp_path / "ledger.sqlite3"),
        "--setup-id", SETUP_ID,
        "--dry-run",
    ]) == 0
    assert "TRINITY — JNJ" in capsys.readouterr().out


def test_live_mode_requires_credentials(monkeypatch):
    monkeypatch.delenv(telegram.BOT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(telegram.CHAT_ID_ENV, raising=False)
    with pytest.raises(TelegramNotificationError, match="credentials are missing"):
        send_telegram_message("safe")


def test_http_failure_is_sanitized_and_never_exposes_credentials():
    token = "highly-secret-token"

    def failure(url, **_kwargs):
        raise RuntimeError(f"failed URL {url}")

    with pytest.raises(TelegramNotificationError) as caught:
        send_telegram_message(
            "safe", token=token, chat_id="123", transport=failure
        )
    assert token not in str(caught.value)
    assert "123" not in str(caught.value)


def test_telegram_api_rejection_is_explicit_and_sanitized():
    response = SimpleNamespace(status_code=403, json=lambda: {"ok": False})
    with pytest.raises(TelegramNotificationError, match="HTTP 403") as caught:
        send_telegram_message(
            "safe", token="secret", chat_id="chat", transport=lambda *_a, **_k: response
        )
    assert "secret" not in str(caught.value)


def test_jnj_exact_message_content_uses_existing_deterministic_fixture():
    root = Path(__file__).resolve().parents[2]
    thesis = json.loads(
        (root / "data/trinity_usa_v2/9d870f12247e425f9dc234baf78c4454.json").read_text(
            encoding="utf-8"
        )
    )
    bars = json.loads(
        (root / "tests/trinity/ledger/fixtures/jnj_ohlcv_20260912.json").read_text(
            encoding="utf-8"
        )
    )
    projected = setup_result_value(build_setup(thesis, bars, "2026-09-12"))
    notification = _notification(
        setup_type=projected["setup_type"],
        technical_regime=projected["technical_regime"],
        research_status=projected["research_status"],
        evidence_confidence=projected["evidence_confidence"],
        thesis_strength=projected["thesis_strength"],
        entry=projected["entry_level"], stop=projected["stop_level"],
        tp1=projected["tp1"], tp2=projected["tp2"],
        rr1=projected["rr_tp1"], rr2=projected["rr_tp2"],
        reason_codes=tuple(projected["reason_codes"]),
    )
    text = render_telegram_message(notification)
    for expected in (
        "JNJ", "PULLBACK", "UPTREND", "INVESTIGATE", "HIGH", "MEDIUM",
        "264.022", "256.1417", "281.07", "287.6628", "2.1634", "3",
        "UPTREND_CONFIRMED", "SMA_SUPPORT_NEARBY",
        "RECONSTRUCTED_NOT_ARCHIVED", "LEGACY_NON_LEDGER_ARTIFACT",
    ):
        assert expected in text


def test_jnj_pilot_materializer_creates_committed_no_network_ledger(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        telegram.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("pilot materializer attempted network"),
    )
    path = tmp_path / "jnj-telegram-pilot.sqlite3"
    identities = materialize_jnj_telegram_pilot(path)
    assert identities.ledger_db == path.resolve()
    assert path.exists()
    from trinity.ledger import LedgerStorage

    with LedgerStorage.open(path) as storage:
        notification = load_committed_setup(
            storage,
            setup_id=identities.setup_id,
            run_id=identities.run_id,
        )
        assert notification.research_id == identities.research_id
        assert notification.resolved_pit_class == "RECONSTRUCTED_NOT_ARCHIVED"
        assert notification.resolved_record_class == "LEGACY_NON_LEDGER_ARTIFACT"
        assert "Setup: PULLBACK" in render_telegram_message(notification)
        assert storage.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(FileExistsError):
        materialize_jnj_telegram_pilot(path)


FIXED_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def _delivery_root(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    (root / "artifacts").mkdir(parents=True)
    (root / "ledger").mkdir()
    (root / "run_state.json").write_text(json.dumps({
        "run_id": "SHADOW_RUN", "state": "COMPLETE",
    }), encoding="utf-8")
    return root


def _candidate(setup_id=SETUP_ID, ticker="JNJ"):
    return {"ticker": ticker, "setup_id": setup_id, "ledger_run_id": RUN_ID}


def _mock_delivery_gates(monkeypatch, candidates):
    monkeypatch.setattr(telegram, "_verify_post_archive_run", lambda _root: "SHADOW_RUN")
    monkeypatch.setattr(telegram, "_select_sol_candidates", lambda _root: candidates)


def test_sol_candidate_selection_is_structured_deterministic_and_exclusive(tmp_path):
    root = _delivery_root(tmp_path)
    (root / "artifacts" / "luna_results.json").write_text(json.dumps({
        "results": [{"ticker": "LUNA_ONLY", "decision": "WATCH"}],
    }), encoding="utf-8")
    (root / "artifacts" / "sol_results.json").write_text(json.dumps([
        {"ticker": "ZZZ", "setup_id": "z", "run_id": "r", "setup_type": "PULLBACK",
         "run_status": "SUCCESS", "error": None},
        {"ticker": "DROP", "setup_id": "drop", "run_id": "r", "setup_type": "BREAKOUT",
         "run_status": "FAILED", "error": "model failure"},
        {"ticker": "NOPE", "setup_id": "none", "run_id": "r", "setup_type": "NO_SETUP",
         "run_status": "SUCCESS", "error": None},
        {"ticker": "AAA", "setup_id": "a", "run_id": "r", "setup_type": "BREAKOUT",
         "run_status": "SUCCESS", "error": None},
    ]), encoding="utf-8")
    assert telegram._select_sol_candidates(root) == [
        {"ticker": "AAA", "setup_id": "a", "ledger_run_id": "r"},
        {"ticker": "ZZZ", "setup_id": "z", "ledger_run_id": "r"},
    ]


def test_post_archive_gate_blocks_transport_before_complete(tmp_path):
    root = _delivery_root(tmp_path)
    (root / "run_state.json").write_text(json.dumps({
        "run_id": "SHADOW_RUN", "state": "RUNNING",
    }), encoding="utf-8")
    calls = []
    with pytest.raises(TelegramNotificationError, match="not COMPLETE"):
        deliver_completed_run(
            root, token="secret", chat_id="chat",
            transport=lambda *_a, **_k: calls.append(True), now=lambda: FIXED_NOW,
        )
    assert calls == []
    result = json.loads((root / "artifacts" / telegram.DELIVERY_RESULT_NAME).read_text())
    assert result["notification_attempted"] is False
    assert result["telegram_sent"] is False
    assert "secret" not in json.dumps(result) and "chat" not in json.dumps(result)


def test_real_post_archive_gate_verifies_manifest_archive_and_ledger(tmp_path):
    from trinity.shadow.manifest import generate_manifest
    from trinity.shadow.sqlite_lifecycle import initialize_fresh_ledger

    root = _delivery_root(tmp_path)
    (root / "artifacts" / "sol_results.json").write_text("[]", encoding="utf-8")
    initialize_fresh_ledger(root / "ledger" / "trinity.sqlite3")
    generate_manifest(root, run_id="SHADOW_RUN")
    (root / "r2_archive_result.json").write_text(json.dumps({
        "object_count": 4, "total_bytes": 100,
    }), encoding="utf-8")
    result = deliver_completed_run(root, now=lambda: FIXED_NOW)
    assert result["candidate_count"] == 0
    assert result["telegram_sent"] is False


def test_delivery_uses_committed_values_and_persists_sanitized_success(
    tmp_path, monkeypatch,
):
    root = _delivery_root(tmp_path)
    _mock_delivery_gates(monkeypatch, [_candidate()])
    requests = []

    def transport(url, **kwargs):
        requests.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    result = deliver_completed_run(
        root, token="unit-bot-token", chat_id="unit-chat", transport=transport,
        now=lambda: FIXED_NOW, storage_factory=lambda _path: _Storage(),
    )
    assert result["telegram_sent"] is True
    assert result["candidate_tickers"] == ["JNJ"]
    assert result["sent_count"] == 1 and result["failed_count"] == 0
    message = requests[0][1]["json"]["text"]
    for exact in (
        "Entry: 264.022", "Stop: 256.1417", "TP1: 281.07", "TP2: 287.6628",
        "RR1: 2.1634", "RR2: 3",
    ):
        assert exact in message
    persisted = (root / "artifacts" / telegram.DELIVERY_RESULT_NAME).read_text()
    assert "unit-bot-token" not in persisted and "unit-chat" not in persisted


def test_committed_ledger_validation_gates_each_send(tmp_path, monkeypatch):
    root = _delivery_root(tmp_path)
    _mock_delivery_gates(monkeypatch, [_candidate()])
    calls = []
    result = deliver_completed_run(
        root, token="token", chat_id="chat", transport=lambda *_a, **_k: calls.append(1),
        now=lambda: FIXED_NOW,
        storage_factory=lambda _path: _Storage(manifest_setup=False),
    )
    assert calls == []
    assert result["sent_count"] == 0 and result["failed_count"] == 1
    assert result["telegram_sent"] is False


def test_missing_credentials_fail_safely_without_network(tmp_path, monkeypatch):
    root = _delivery_root(tmp_path)
    _mock_delivery_gates(monkeypatch, [_candidate()])
    monkeypatch.delenv(telegram.BOT_TOKEN_ENV, raising=False)
    monkeypatch.delenv(telegram.CHAT_ID_ENV, raising=False)
    calls = []
    result = deliver_completed_run(
        root, transport=lambda *_a, **_k: calls.append(1), now=lambda: FIXED_NOW,
        storage_factory=lambda _path: _Storage(),
    )
    assert calls == [] and result["failed_count"] == 1
    assert result["deliveries"][0]["error"] == "Telegram credentials are missing"


def test_transport_failure_is_isolated_and_duplicate_is_refused(tmp_path, monkeypatch):
    root = _delivery_root(tmp_path)
    _mock_delivery_gates(monkeypatch, [_candidate()])
    attempts = []

    def failure(url, **_kwargs):
        attempts.append(url)
        raise RuntimeError(f"failed {url}")

    result = deliver_completed_run(
        root, token="do-not-persist", chat_id="private-chat", transport=failure,
        now=lambda: FIXED_NOW, storage_factory=lambda _path: _Storage(),
    )
    assert len(attempts) == 1
    assert result["failed_count"] == 1 and result["telegram_sent"] is False
    assert json.loads((root / "run_state.json").read_text())["state"] == "COMPLETE"
    persisted = (root / "artifacts" / telegram.DELIVERY_RESULT_NAME).read_text()
    assert "do-not-persist" not in persisted and "private-chat" not in persisted
    with pytest.raises(TelegramNotificationError, match="duplicate invocation"):
        deliver_completed_run(
            root, token="do-not-persist", chat_id="private-chat", transport=failure,
            now=lambda: FIXED_NOW, storage_factory=lambda _path: _Storage(),
        )
    assert len(attempts) == 1


def test_zero_candidates_produces_audit_record_and_zero_sends(tmp_path, monkeypatch):
    root = _delivery_root(tmp_path)
    _mock_delivery_gates(monkeypatch, [])
    calls = []
    result = deliver_completed_run(
        root, transport=lambda *_a, **_k: calls.append(1), now=lambda: FIXED_NOW,
        storage_factory=lambda _path: pytest.fail("zero-candidate run opened Ledger"),
    )
    assert calls == []
    assert result["candidate_count"] == 0
    assert result["notification_attempted"] is False
    assert result["telegram_sent"] is False


def test_workflow_scopes_telegram_secrets_to_post_archive_full_shadow_only():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/trinity-shadow-manual.yml").read_text()
    archive_at = workflow.index("- name: Archive and verify run in R2")
    telegram_at = workflow.index("- name: Deliver committed post-archive Telegram notifications")
    diagnostics_at = workflow.index("- name: Preserve local diagnostics", telegram_at)
    assert archive_at < telegram_at < diagnostics_at
    assert "continue-on-error: true" in workflow[telegram_at:diagnostics_at]
    assert "TRINITY_TELEGRAM_BOT_TOKEN" not in workflow[:telegram_at]
    telegram_step = workflow[telegram_at:diagnostics_at]
    assert "secrets.TRINITY_TELEGRAM_BOT_TOKEN" in telegram_step
    assert "secrets.TRINITY_TELEGRAM_CHAT_ID" in telegram_step
    price_workflow = (root / ".github/workflows/trinity-price-refresh.yml").read_text()
    assert "TRINITY_TELEGRAM" not in price_workflow
    shadow_cli = (root / "trinity/shadow/cli.py").read_text()
    assert '"broker_execution": False' in shadow_cli
