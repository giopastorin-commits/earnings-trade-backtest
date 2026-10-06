"""Minimal, versioned R2 baseline restore and collision-safe run archive."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any


class R2Error(RuntimeError):
    pass


def client_from_environment(*, read_only: bool = False) -> tuple[Any, str]:
    if os.name == "nt":
        try:
            import truststore
            truststore.inject_into_ssl()
        except ImportError:
            pass
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise R2Error("boto3 is required for R2 access") from exc
    access_name = (
        "TRINITY_R2_READ_ACCESS_KEY_ID" if read_only
        else "TRINITY_R2_ACCESS_KEY_ID"
    )
    secret_name = (
        "TRINITY_R2_READ_SECRET_ACCESS_KEY" if read_only
        else "TRINITY_R2_SECRET_ACCESS_KEY"
    )
    required = {
        "TRINITY_R2_ENDPOINT": os.getenv("TRINITY_R2_ENDPOINT"),
        access_name: os.getenv(access_name), secret_name: os.getenv(secret_name),
        "TRINITY_R2_BUCKET": os.getenv("TRINITY_R2_BUCKET"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise R2Error("missing R2 configuration: " + ", ".join(missing))
    client = boto3.client(
        "s3", endpoint_url=required["TRINITY_R2_ENDPOINT"],
        aws_access_key_id=required[access_name],
        aws_secret_access_key=required[secret_name],
        region_name="auto", config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )
    return client, str(required["TRINITY_R2_BUCKET"])


def restore_baseline(
    client: Any, bucket: str, prefix: str, destination: str | Path,
    *, expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    normalized = prefix.strip("/") + "/"
    if "version=" not in normalized or "latest" in normalized.lower():
        raise R2Error("TRINITY_BASELINE_PREFIX must be immutable and contain version=")
    manifest_key = normalized + "manifest.json"
    manifest_raw = client.get_object(Bucket=bucket, Key=manifest_key)["Body"].read()
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    if expected_manifest_sha256 and manifest_sha256 != expected_manifest_sha256:
        raise R2Error("baseline manifest SHA-256 mismatch")
    manifest = json.loads(manifest_raw)
    files = manifest.get("files")
    if not isinstance(files, list):
        raise R2Error("baseline manifest lacks files")
    root = Path(destination)
    for item in files:
        relative = PurePosixPath(str(item.get("relative_path") or item.get("path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise R2Error("unsafe baseline manifest path")
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = client.get_object(Bucket=bucket, Key=normalized + relative.as_posix())["Body"].read()
        expected_bytes = item.get("size", item.get("bytes"))
        if expected_bytes is None:
            raise R2Error(f"baseline manifest lacks size: {relative}")
        if len(body) != int(expected_bytes) or hashlib.sha256(body).hexdigest() != item["sha256"]:
            raise R2Error(f"baseline integrity mismatch: {relative}")
        target.write_bytes(body)
    if manifest.get("file_count") not in (None, len(files)):
        raise R2Error("baseline manifest file count mismatch")
    total = sum(int(item.get("size", item.get("bytes", 0))) for item in files)
    if manifest.get("total_bytes") not in (None, total):
        raise R2Error("baseline manifest total bytes mismatch")
    (root / "manifest.json").write_bytes(manifest_raw)
    return {**manifest, "restored_manifest_sha256": manifest_sha256}


def archive_run(client: Any, bucket: str, run_id: str, run_root: str | Path) -> dict[str, int]:
    prefix = f"runs/shadow/run_id={run_id}/"
    root = Path(run_root)
    objects: list[tuple[str, Path, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name.endswith(("-wal", "-shm", "-journal", ".tmp")):
            continue
        relative = path.relative_to(root).as_posix()
        group = "ledger" if path.suffix == ".sqlite3" else (
            "manifests" if path.name in {"manifest.json", "checksums.sha256"} else
            "reports" if path.suffix in {".txt", ".md"} else "artifacts"
        )
        key = f"{prefix}{group}/{relative}"
        objects.append((key, path, hashlib.sha256(path.read_bytes()).hexdigest()))
    existing: dict[str, dict[str, Any]] = {}
    token = None
    while True:
        args = {"Bucket": bucket, "Prefix": prefix}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        existing.update({item["Key"]: item for item in page.get("Contents", [])})
        if not page.get("IsTruncated"):
            break
        token = page["NextContinuationToken"]
    expected_keys = {item[0] for item in objects}
    unexpected = set(existing) - expected_keys
    if unexpected:
        raise R2Error(f"immutable run prefix collision ({len(unexpected)} unexpected objects)")
    for key, path, digest in objects:
        if key in existing:
            head = client.head_object(Bucket=bucket, Key=key)
            if int(head["ContentLength"]) != path.stat().st_size or head.get("Metadata", {}).get("sha256") != digest:
                raise R2Error(f"immutable run object collision: {key}")
            continue
        client.upload_file(
            str(path), bucket, key,
            ExtraArgs={"Metadata": {"sha256": digest}, "ContentType": _content_type(path)},
        )
    total = 0
    for key, path, digest in objects:
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head["ContentLength"]) != path.stat().st_size or head.get("Metadata", {}).get("sha256") != digest:
            raise R2Error(f"remote verification failed: {key}")
        total += path.stat().st_size
    return {"object_count": len(objects), "total_bytes": total}


def _content_type(path: Path) -> str:
    return {".json": "application/json", ".txt": "text/plain", ".sqlite3": "application/vnd.sqlite3"}.get(
        path.suffix, "application/octet-stream"
    )
