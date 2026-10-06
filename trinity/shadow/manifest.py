"""Closed-run manifest and checksum generation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .atomic import atomic_bytes, atomic_json


EXCLUDED_SUFFIXES = ("-wal", "-shm", "-journal", ".tmp")


def eligible_files(root: str | Path) -> list[Path]:
    base = Path(root)
    return [
        path for path in sorted(base.rglob("*"))
        if path.is_file() and not path.name.endswith(EXCLUDED_SUFFIXES)
        and path.name not in {"manifest.json", "checksums.sha256"}
    ]


def generate_manifest(root: str | Path, *, run_id: str) -> dict[str, object]:
    base = Path(root)
    entries = []
    checksum_lines = []
    for path in eligible_files(base):
        relative = path.relative_to(base).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append({"path": relative, "bytes": path.stat().st_size, "sha256": digest})
        checksum_lines.append(f"{digest}  {relative}")
    value = {
        "schema_name": "trinity.shadow-run-manifest", "schema_version": "1",
        "run_id": run_id, "file_count": len(entries),
        "total_bytes": sum(int(item["bytes"]) for item in entries), "files": entries,
    }
    atomic_json(base / "manifest.json", value)
    atomic_bytes(base / "checksums.sha256", ("\n".join(checksum_lines) + "\n").encode())
    return value


def verify_manifest(root: str | Path, manifest: dict[str, object]) -> None:
    base = Path(root)
    for item in manifest["files"]:
        path = base / str(item["path"])
        if not path.is_file() or path.stat().st_size != item["bytes"]:
            raise ValueError(f"manifest size mismatch: {item['path']}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"manifest hash mismatch: {item['path']}")
