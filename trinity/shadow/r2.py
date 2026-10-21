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


def price_snapshot_prefix(session: str, run_id: str) -> str:
    if not session or "/" in session or not run_id or "/" in run_id:
        raise R2Error("invalid price snapshot namespace identity")
    return f"prices/twelvedata/session={session}/run_id={run_id}/"


def archive_price_snapshot(
    client: Any, bucket: str, snapshot_root: str | Path, *, run_id: str,
    enforce_canonical_universe: bool = True,
) -> dict[str, Any]:
    from trinity.twelvedata_prices import verify_canonical_universe, verify_snapshot

    root = Path(snapshot_root)
    manifest = verify_snapshot(root, require_ready=True, require_raw=True)
    if enforce_canonical_universe:
        verify_canonical_universe(manifest)
    expected_id = f"TWELVEDATA_{manifest['target_market_session']}_RUN_{run_id}"
    if manifest.get("snapshot_id") != expected_id:
        raise R2Error("price snapshot run identity mismatch")
    prefix = price_snapshot_prefix(str(manifest["target_market_session"]), run_id)
    objects: list[tuple[str, Path, str]] = []
    authoritative = [root / "snapshot_manifest.json"]
    if isinstance(manifest.get("credit_guard"), dict):
        authoritative.append(root / "provider_credit_telemetry.json")
    authoritative.extend(sorted((root / "raw").glob("*.json")))
    authoritative.extend(sorted((root / "normalized").glob("*.json")))
    for path in authoritative:
        if not path.is_file():
            raise R2Error(f"missing authoritative price snapshot file: {path.name}")
        if path.name.endswith((".tmp", "-wal", "-shm", "-journal")):
            continue
        relative = path.relative_to(root).as_posix()
        objects.append((
            prefix + relative, path, hashlib.sha256(path.read_bytes()).hexdigest(),
        ))
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
    expected_keys = {key for key, _path, _digest in objects}
    if set(existing) - expected_keys:
        raise R2Error("immutable price snapshot prefix contains unexpected objects")
    for key, path, digest in objects:
        if key in existing:
            head = client.head_object(Bucket=bucket, Key=key)
            if int(head["ContentLength"]) != path.stat().st_size \
                    or head.get("Metadata", {}).get("sha256") != digest:
                raise R2Error(f"immutable price snapshot collision: {key}")
            continue
        client.upload_file(
            str(path), bucket, key,
            ExtraArgs={"Metadata": {"sha256": digest}, "ContentType": _content_type(path)},
        )
    total = 0
    for key, path, digest in objects:
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head["ContentLength"]) != path.stat().st_size \
                or head.get("Metadata", {}).get("sha256") != digest:
            raise R2Error(f"remote price snapshot verification failed: {key}")
        total += path.stat().st_size
    return {
        "prefix": prefix, "object_count": len(objects), "total_bytes": total,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_sha256": manifest["snapshot_sha256"],
    }


def restore_price_snapshot(
    client: Any, bucket: str, prefix: str, destination: str | Path,
    *, expected_snapshot_sha256: str,
    enforce_canonical_universe: bool = True,
) -> dict[str, Any]:
    from trinity.twelvedata_prices import verify_canonical_universe, verify_snapshot

    normalized = prefix.strip("/") + "/"
    if not normalized.startswith("prices/twelvedata/session=") \
            or "/run_id=" not in normalized or "latest" in normalized.lower():
        raise R2Error("price snapshot prefix must identify an immutable session and run_id")
    if not expected_snapshot_sha256:
        raise R2Error("expected price snapshot SHA-256 is required")
    manifest_raw = client.get_object(
        Bucket=bucket, Key=normalized + "snapshot_manifest.json",
    )["Body"].read()
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise R2Error("invalid remote price snapshot manifest") from exc
    if manifest.get("snapshot_sha256") != expected_snapshot_sha256:
        raise R2Error("remote price snapshot identity hash mismatch")
    session_component = normalized.split("session=", 1)[1].split("/", 1)[0]
    run_component = normalized.split("/run_id=", 1)[1].split("/", 1)[0]
    expected_id = f"TWELVEDATA_{session_component}_RUN_{run_component}"
    if manifest.get("target_market_session") != session_component \
            or manifest.get("snapshot_id") != expected_id:
        raise R2Error("remote price snapshot namespace identity mismatch")
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    (root / "snapshot_manifest.json").write_bytes(manifest_raw)
    if isinstance(manifest.get("credit_guard"), dict):
        telemetry = client.get_object(
            Bucket=bucket, Key=normalized + "provider_credit_telemetry.json",
        )["Body"].read()
        if hashlib.sha256(telemetry).hexdigest() != manifest["credit_guard"].get("telemetry_sha256"):
            raise R2Error("remote price credit telemetry hash mismatch")
        (root / "provider_credit_telemetry.json").write_bytes(telemetry)
    entries = manifest.get("tickers")
    if not isinstance(entries, list):
        raise R2Error("remote price snapshot manifest lacks tickers")
    for item in entries:
        ticker = str(item.get("canonical_ticker") or "")
        if not ticker or "/" in ticker or "\\" in ticker:
            raise R2Error("unsafe price snapshot ticker")
        body = client.get_object(
            Bucket=bucket, Key=normalized + f"normalized/{ticker}.json",
        )["Body"].read()
        if hashlib.sha256(body).hexdigest() != item.get("content_sha256"):
            raise R2Error(f"remote normalized price hash mismatch: {ticker}")
        target = root / "normalized" / f"{ticker}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    try:
        restored = verify_snapshot(root, require_ready=True, require_raw=False)
        if enforce_canonical_universe:
            verify_canonical_universe(restored)
        return restored
    except Exception as exc:
        raise R2Error(f"restored price snapshot verification failed: {exc}") from exc


def stock_observation_prefix(
    protocol_hash: str, session: str, snapshot_id: str,
) -> str:
    values = (protocol_hash, session, snapshot_id)
    if any(not value or "/" in value or "\\" in value for value in values) \
            or len(protocol_hash) != 64 \
            or any(char not in "0123456789abcdef" for char in protocol_hash) \
            or "latest" in snapshot_id.lower():
        raise R2Error("invalid stock observation namespace identity")
    return (
        f"observations/stocks/protocol={protocol_hash}/"
        f"session={session}/snapshot_id={snapshot_id}/"
    )


def _remote_inventory(client: Any, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    token = None
    while True:
        args = {"Bucket": bucket, "Prefix": prefix}
        if token:
            args["ContinuationToken"] = token
        page = client.list_objects_v2(**args)
        result.update({item["Key"]: item for item in page.get("Contents", [])})
        if not page.get("IsTruncated"):
            return result
        token = page["NextContinuationToken"]


def _stock_observation_files(root: Path, manifest: dict[str, Any]) -> tuple[Path, ...]:
    entries = manifest.get("tickers")
    if not isinstance(entries, list):
        raise R2Error("stock observation manifest lacks tickers")
    paths = [root / "snapshot_manifest.json"]
    guard = manifest.get("credit_guard")
    if isinstance(guard, dict):
        if guard.get("telemetry_file") != "provider_credit_telemetry.json":
            raise R2Error("stock observation telemetry path mismatch")
        paths.append(root / "provider_credit_telemetry.json")
    for item in entries:
        ticker = str(item.get("canonical_ticker") or "") if isinstance(item, dict) else ""
        if not ticker or "/" in ticker or "\\" in ticker:
            raise R2Error("unsafe stock observation ticker")
        paths.extend((root / "raw" / f"{ticker}.json", root / "normalized" / f"{ticker}.json"))
    if any(not path.is_file() for path in paths):
        raise R2Error("stock observation authoritative file is missing")
    return tuple(paths)


def archive_stock_observation_snapshot(
    client: Any, bucket: str, snapshot_root: str | Path,
) -> dict[str, Any]:
    """Archive an authoritative READY stock subset under its immutable identity."""

    from trinity.validation.stock_observation import (
        load_stock_observation_snapshot, normalize_expected_tickers,
        verify_stock_observation_manifest_identity,
    )
    from trinity.twelvedata_prices import verify_snapshot

    root = Path(snapshot_root)
    try:
        manifest = verify_snapshot(root, require_ready=True, require_raw=True)
        identity = verify_stock_observation_manifest_identity(manifest)
        snapshot = load_stock_observation_snapshot(
            root, expected_tickers=identity["expected_tickers"],
            expected_observation_target_session=str(manifest["target_market_session"]),
        )
    except Exception as exc:
        raise R2Error(f"invalid local stock observation snapshot: {exc}") from exc
    tickers = normalize_expected_tickers(identity["expected_tickers"])
    if snapshot.expected_tickers != tickers:
        raise R2Error("local stock observation membership mismatch")
    prefix = stock_observation_prefix(
        str(manifest["protocol_hash"]), str(manifest["target_market_session"]),
        str(manifest["snapshot_id"]),
    )
    files = _stock_observation_files(root, manifest)
    objects = tuple(
        (prefix + path.relative_to(root).as_posix(), path, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in files
    )
    existing = _remote_inventory(client, bucket, prefix)
    expected_keys = {key for key, _path, _digest in objects}
    if set(existing) - expected_keys:
        raise R2Error("immutable stock observation prefix contains unexpected objects")
    for key, path, digest in objects:
        if key in existing:
            head = client.head_object(Bucket=bucket, Key=key)
            if int(head["ContentLength"]) != path.stat().st_size \
                    or head.get("Metadata", {}).get("sha256") != digest:
                raise R2Error(f"immutable stock observation collision: {key}")
            continue
        client.upload_file(
            str(path), bucket, key,
            ExtraArgs={"Metadata": {"sha256": digest}, "ContentType": _content_type(path)},
        )
    if set(_remote_inventory(client, bucket, prefix)) != expected_keys:
        raise R2Error("remote stock observation object closure changed during upload")
    total = 0
    for key, path, digest in objects:
        head = client.head_object(Bucket=bucket, Key=key)
        if int(head["ContentLength"]) != path.stat().st_size \
                or head.get("Metadata", {}).get("sha256") != digest:
            raise R2Error(f"remote stock observation verification failed: {key}")
        total += path.stat().st_size
    return {
        "prefix": prefix, "object_count": len(objects), "total_bytes": total,
        "snapshot_id": manifest["snapshot_id"],
        "snapshot_sha256": manifest["snapshot_sha256"],
    }


def _write_restored(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def restore_stock_observation_snapshot(
    client: Any, bucket: str, prefix: str, destination: str | Path, *,
    expected_snapshot_id: str, expected_snapshot_sha256: str,
    expected_target_session: str, expected_tickers: Any,
    expected_expectation_set_sha256: str,
) -> dict[str, Any]:
    """Restore raw and normalized evidence, then prove it with the frozen loader."""

    from trinity.validation.stock_observation import (
        load_stock_observation_snapshot, verify_stock_observation_manifest_identity,
    )
    from trinity.twelvedata_prices import snapshot_hash

    normalized = prefix.strip("/") + "/"
    if not normalized.startswith("observations/stocks/protocol=") \
            or "/session=" not in normalized or "/snapshot_id=" not in normalized \
            or "latest" in normalized.lower():
        raise R2Error("stock observation prefix must be immutable")
    if not expected_snapshot_id or not expected_snapshot_sha256:
        raise R2Error("expected stock observation identity is required")
    manifest_key = normalized + "snapshot_manifest.json"
    try:
        manifest_raw = client.get_object(Bucket=bucket, Key=manifest_key)["Body"].read()
        manifest = json.loads(manifest_raw)
    except Exception as exc:
        raise R2Error("invalid or missing remote stock observation manifest") from exc
    if not isinstance(manifest, dict):
        raise R2Error("remote stock observation manifest is not an object")
    try:
        verify_stock_observation_manifest_identity(
            manifest, expected_tickers=expected_tickers,
            expected_expectation_set_sha256=expected_expectation_set_sha256,
        )
    except Exception as exc:
        raise R2Error(f"remote stock observation identity mismatch: {exc}") from exc
    if manifest.get("snapshot_id") != expected_snapshot_id \
            or manifest.get("snapshot_sha256") != expected_snapshot_sha256 \
            or snapshot_hash(manifest) != expected_snapshot_sha256 \
            or manifest.get("target_market_session") != expected_target_session:
        raise R2Error("remote stock observation requested identity mismatch")
    expected_prefix = stock_observation_prefix(
        str(manifest["protocol_hash"]), expected_target_session, expected_snapshot_id,
    )
    if normalized != expected_prefix:
        raise R2Error("remote stock observation namespace identity mismatch")
    root = Path(destination)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise R2Error("stock observation restore destination must be new or empty")

    entries = manifest.get("tickers")
    if not isinstance(entries, list):
        raise R2Error("remote stock observation manifest lacks tickers")
    expected: dict[str, tuple[str, int | None]] = {
        "snapshot_manifest.json": (hashlib.sha256(manifest_raw).hexdigest(), len(manifest_raw)),
    }
    guard = manifest.get("credit_guard")
    if isinstance(guard, dict):
        expected["provider_credit_telemetry.json"] = (
            str(guard.get("telemetry_sha256") or ""), None,
        )
    for item in entries:
        ticker = str(item.get("canonical_ticker") or "") if isinstance(item, dict) else ""
        if not ticker or "/" in ticker or "\\" in ticker:
            raise R2Error("unsafe remote stock observation ticker")
        expected[f"raw/{ticker}.json"] = (str(item.get("raw_sha256") or ""), None)
        expected[f"normalized/{ticker}.json"] = (str(item.get("content_sha256") or ""), None)
    remote = _remote_inventory(client, bucket, normalized)
    expected_keys = {normalized + relative for relative in expected}
    if set(remote) != expected_keys:
        raise R2Error("remote stock observation object closure mismatch")
    root.mkdir(parents=True, exist_ok=True)
    for relative, (digest, known_size) in expected.items():
        key = normalized + relative
        try:
            body = manifest_raw if relative == "snapshot_manifest.json" else client.get_object(
                Bucket=bucket, Key=key,
            )["Body"].read()
            head = client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            raise R2Error(f"missing remote stock observation object: {relative}") from exc
        actual_digest = hashlib.sha256(body).hexdigest()
        if actual_digest != digest \
                or int(head["ContentLength"]) != len(body) \
                or head.get("Metadata", {}).get("sha256") != digest \
                or (known_size is not None and len(body) != known_size):
            raise R2Error(f"remote stock observation integrity mismatch: {relative}")
        _write_restored(root / Path(PurePosixPath(relative)), body)
    try:
        snapshot = load_stock_observation_snapshot(
            root, expected_tickers=expected_tickers,
            expected_observation_target_session=expected_target_session,
        )
    except Exception as exc:
        raise R2Error(f"restored stock observation verification failed: {exc}") from exc
    if snapshot.source_snapshot_id != expected_snapshot_id \
            or snapshot.source_snapshot_sha256 != expected_snapshot_sha256:
        raise R2Error("restored stock observation source identity mismatch")
    return {
        **manifest,
        "restored_prefix": normalized,
        "restored_object_count": len(expected),
    }


def _content_type(path: Path) -> str:
    return {".json": "application/json", ".txt": "text/plain", ".sqlite3": "application/vnd.sqlite3"}.get(
        path.suffix, "application/octet-stream"
    )
