"""Durable, single-terminal-state lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .atomic import atomic_json


TERMINAL_STATES = frozenset({
    "COMPLETE", "FAILED_DATA", "FAILED_SETUP", "FAILED_LUNA", "FAILED_SOL",
    "FAILED_LEDGER", "FAILED_ARCHIVE", "COST_GUARD_STOP",
})


class RunState:
    def __init__(self, path: str | Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.current = "STARTING"
        self.write("RUNNING")

    def write(self, state: str, error: str | None = None) -> None:
        allowed = state == "RUNNING" or state in TERMINAL_STATES
        if not allowed:
            raise ValueError(f"unsupported run state: {state}")
        if self.current in TERMINAL_STATES:
            raise RuntimeError("terminal run state is immutable")
        self.current = state
        atomic_json(self.path, {
            "schema_name": "trinity.shadow-run-state", "schema_version": "1",
            "run_id": self.run_id, "state": state,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "error": error,
        })
