"""Create and strongly verify the immutable Week 0 golden baseline in R2."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .atomic import atomic_json
from .golden_replay import ANALYTICAL_COMMIT, canonical_bytes, canonical_sha256
from .r2 import R2Error


RUN_ID = "SHADOW_USA_2026-10-06_W00"
DECISION_CUTOFF = "2026-10-05T20:00:00Z"
EXPECTED_FUNNEL_SHA256 = "444c55be62496ab6fd48d3d6bd497617c74f18dc981d680e89e7c695cc6602ec"
EXPECTED_SETUPS_SHA256 = "894c2f6b2eb003ab6261c248ddaa88aab78126b72a2627a023066c9813066547"
EXPECTED_INPUT_MANIFEST_SHA256 = "bfd0cc4d291439db09d9edbb7194bde235eab5cfccefdaf56bd1fd1adbf66ad2"
SOURCE_PREFIX = f"runs/shadow/run_id={RUN_ID}/artifacts/"
TARGET_PREFIX = f"baseline/shadow-week0/version={EXPECTED_INPUT_MANIFEST_SHA256}/"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _manifest_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2,
    ) + "\n").encode("utf-8")


def _list_objects(client: Any, bucket: str, prefix: str) -> dict[str, dict[str, Any]]:
    objects: dict[str, dict[str, Any]] = {}
    token = None
    while True:
        request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            request["ContinuationToken"] = token
        page = client.list_objects_v2(**request)
        objects.update({item["Key"]: item for item in page.get("Contents", [])})
        if not page.get("IsTruncated"):
            return objects
        token = page["NextContinuationToken"]


def _minimal_price_payload(raw: bytes, through: date) -> bytes:
    try:
        rows = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise R2Error("Week 0 source OHLCV object is invalid JSON") from exc
    if not isinstance(rows, list) or not rows:
        raise R2Error("Week 0 source OHLCV object is empty")
    clipped = [row for row in rows if str(row.get("date") or "") <= through.isoformat()]
    return canonical_bytes(clipped[-201:])


def _get(client: Any, bucket: str, key: str) -> bytes:
    try:
        return bytes(client.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception as exc:
        raise R2Error(f"R2 object unavailable: {key} ({type(exc).__name__})") from exc


def _verify_object(client: Any, bucket: str, key: str, payload: bytes) -> None:
    remote = _get(client, bucket, key)
    if len(remote) != len(payload) or _sha256(remote) != _sha256(payload):
        raise R2Error(f"full remote SHA-256 verification failed: {key}")
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head["ContentLength"]) != len(payload):
        raise R2Error(f"remote size verification failed: {key}")
    if head.get("Metadata", {}).get("sha256") != _sha256(payload):
        raise R2Error(f"remote SHA-256 metadata verification failed: {key}")


def create_week0_baseline(
    client: Any, bucket: str, *, reference_root: str | Path,
    receipt_path: str | Path | None = None,
) -> dict[str, object]:
    reference = Path(reference_root)
    input_manifest = json.loads((reference / "input_manifest.json").read_text(encoding="utf-8"))
    if canonical_sha256(input_manifest) != EXPECTED_INPUT_MANIFEST_SHA256:
        raise R2Error("golden input manifest differs from the frozen hash")
    entries = input_manifest.get("files")
    if not isinstance(entries, list) or len(entries) != 517:
        raise R2Error("golden input manifest must describe exactly 517 files")
    mapping_entry = entries[0]
    mapping = (reference / "canonical_ticker_mapping.csv").read_bytes()
    if len(mapping) != int(mapping_entry["byte_length"]) or _sha256(mapping) != mapping_entry["sha256"]:
        raise R2Error("canonical ticker mapping does not match the golden manifest")

    payloads: dict[str, bytes] = {"canonical_ticker_mapping.csv": mapping}
    price_entries = [item for item in entries if str(item["relative_path"]).startswith("prices/")]

    def fetch_price(item: Mapping[str, object]) -> tuple[str, bytes]:
        relative = str(item["relative_path"])
        source_key = SOURCE_PREFIX + relative
        payload = _minimal_price_payload(_get(client, bucket, source_key), date(2026, 10, 5))
        if len(payload) != int(item["byte_length"]) or _sha256(payload) != item["sha256"]:
            raise R2Error(f"source-derived minimal input mismatch: {relative}")
        return relative, payload

    with ThreadPoolExecutor(max_workers=12) as pool:
        for relative, payload in pool.map(fetch_price, price_entries):
            payloads[relative] = payload

    records = []
    entries_by_path = {str(item["relative_path"]): item for item in entries}
    for relative in sorted(payloads):
        item = entries_by_path[relative]
        source = (
            "tests/golden/shadow_week0/canonical_ticker_mapping.csv"
            if relative == "canonical_ticker_mapping.csv" else SOURCE_PREFIX + relative
        )
        records.append({
            "relative_path": relative,
            "semantic_role": item["semantic_role"],
            "size": len(payloads[relative]),
            "sha256": _sha256(payloads[relative]),
            "source_week0_object": source,
            "source_provenance": item["source_provenance"],
        })
    manifest = {
        "schema_name": "trinity.shadow-week0-r2-baseline",
        "schema_version": "1",
        "run_id": RUN_ID,
        "decision_cutoff_utc": DECISION_CUTOFF,
        "decision_cutoff_america_new_york": "2026-10-05T16:00:00-04:00",
        "analytical_base_commit": ANALYTICAL_COMMIT,
        "golden_input_manifest_sha256": EXPECTED_INPUT_MANIFEST_SHA256,
        "expected_funnel_sha256": EXPECTED_FUNNEL_SHA256,
        "expected_setups_sha256": EXPECTED_SETUPS_SHA256,
        "selection_rule": input_manifest["selection_rule"],
        "expected_absent_inputs": input_manifest["expected_absent_inputs"],
        "files": records,
        "file_count": len(records),
        "total_bytes": sum(len(value) for value in payloads.values()),
    }
    manifest_payload = _manifest_bytes(manifest)
    all_payloads = {
        **{TARGET_PREFIX + relative: payload for relative, payload in payloads.items()},
        TARGET_PREFIX + "manifest.json": manifest_payload,
    }
    existing = _list_objects(client, bucket, TARGET_PREFIX)
    unexpected = set(existing) - set(all_payloads)
    if unexpected:
        raise R2Error(f"immutable baseline collision: {len(unexpected)} unexpected objects")

    data_payloads = {key: value for key, value in all_payloads.items() if not key.endswith("/manifest.json")}
    reused = 0
    uploaded = 0

    def put_or_reuse(item: tuple[str, bytes]) -> str:
        key, payload = item
        if key in existing:
            head = client.head_object(Bucket=bucket, Key=key)
            if (int(head["ContentLength"]) != len(payload)
                    or head.get("Metadata", {}).get("sha256") != _sha256(payload)):
                raise R2Error(f"immutable baseline object collision: {key}")
            return "reused"
        client.put_object(
            Bucket=bucket, Key=key, Body=payload,
            ContentType="text/csv" if key.endswith(".csv") else "application/json",
            Metadata={"sha256": _sha256(payload)},
        )
        return "uploaded"

    with ThreadPoolExecutor(max_workers=12) as pool:
        for action in pool.map(put_or_reuse, data_payloads.items()):
            if action == "reused":
                reused += 1
            else:
                uploaded += 1
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(
            lambda item: _verify_object(client, bucket, item[0], item[1]),
            data_payloads.items(),
        ))

    manifest_key = TARGET_PREFIX + "manifest.json"
    if manifest_key in existing:
        _verify_object(client, bucket, manifest_key, manifest_payload)
        reused += 1
    else:
        client.put_object(
            Bucket=bucket, Key=manifest_key, Body=manifest_payload,
            ContentType="application/json", Metadata={"sha256": _sha256(manifest_payload)},
        )
        uploaded += 1

    final_objects = _list_objects(client, bucket, TARGET_PREFIX)
    if set(final_objects) != set(all_payloads):
        raise R2Error("remote baseline object set differs after upload")
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(
            lambda item: _verify_object(client, bucket, item[0], item[1]),
            all_payloads.items(),
        ))
    receipt: dict[str, object] = {
        "status": "BASELINE_COMPLETE",
        "bucket": bucket,
        "prefix": TARGET_PREFIX,
        "data_object_count": len(records),
        "object_count": len(all_payloads),
        "data_bytes": manifest["total_bytes"],
        "total_bytes": sum(len(value) for value in all_payloads.values()),
        "manifest_sha256": _sha256(manifest_payload),
        "golden_input_manifest_sha256": EXPECTED_INPUT_MANIFEST_SHA256,
        "uploaded_objects": uploaded,
        "reused_objects": reused,
        "full_sha256_verified_objects": len(all_payloads),
    }
    if receipt_path is not None:
        atomic_json(receipt_path, receipt)
    return receipt
