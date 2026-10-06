"""Fresh, closed SQLite lifecycle for immutable run archives."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from trinity.ledger import LedgerStorage


def initialize_fresh_ledger(path: str | Path) -> None:
    target = Path(path)
    if target.exists():
        raise FileExistsError(f"shadow Ledger already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with LedgerStorage.open(target):
        pass


def close_and_verify(path: str | Path) -> None:
    target = Path(path)
    connection = sqlite3.connect(target)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Ledger integrity_check failed")
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign:
            raise RuntimeError(f"Ledger foreign_key_check failed: {len(foreign)} row(s)")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            raise RuntimeError(f"SQLite sidecar remains after close: {sidecar.name}")
