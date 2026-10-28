"""Read-only adapter from frozen TRINITY Ledger SQLite artifacts.

The adapter extracts existing effective prompts, response schemas, facts, and
Analyst/Critic lineage. It never writes to the database and never calls a
provider. Missing temporal or lineage evidence is a hard failure.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from .dataset import Dataset, DatasetError, TickerInput


class ArchiveDatasetError(DatasetError):
    pass


def _json_payload(artifacts: Mapping[str, sqlite3.Row], artifact_id: str) -> Any:
    row = artifacts.get(artifact_id)
    if row is None:
        raise ArchiveDatasetError(f"missing artifact {artifact_id}")
    _verify_artifact_row(artifact_id, row)
    try:
        return json.loads(bytes(row["payload"]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveDatasetError(f"artifact {artifact_id} is not JSON") from exc


def _text_payload(artifacts: Mapping[str, sqlite3.Row], artifact_id: str) -> str:
    row = artifacts.get(artifact_id)
    if row is None:
        raise ArchiveDatasetError(f"missing prompt artifact {artifact_id}")
    _verify_artifact_row(artifact_id, row)
    try:
        return bytes(row["payload"]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveDatasetError(f"prompt artifact {artifact_id} is not UTF-8") from exc


def _verify_artifact_row(artifact_id: str, row: Mapping[str, Any]) -> None:
    """Mirror Ledger artifact integrity checks without writing to SQLite."""
    payload = bytes(row["payload"])
    keys = set(row.keys()) if hasattr(row, "keys") else set(row)
    if "byte_length" not in keys or "sha256" not in keys:
        raise ArchiveDatasetError(f"artifact {artifact_id} lacks integrity metadata")
    if int(row["byte_length"]) != len(payload):
        raise ArchiveDatasetError(f"artifact {artifact_id} byte length mismatch")
    digest = hashlib.sha256(payload).hexdigest()
    if str(row["sha256"]) != digest:
        raise ArchiveDatasetError(f"artifact {artifact_id} sha256 mismatch")
    if "content_hash" in keys:
        expected = str(row["content_hash"])
        if expected != digest and expected != artifact_id.removeprefix("sha256:"):
            raise ArchiveDatasetError(f"artifact {artifact_id} content hash mismatch")


def _authoritative_run(row: Mapping[str, Any], requested_run_id: str | None) -> str:
    if row is None:
        raise ArchiveDatasetError("requested run is absent from the Ledger")
    keys = set(row.keys())
    run_id = str(row["run_id"] if "run_id" in keys else "")
    if requested_run_id is not None and run_id != requested_run_id:
        raise ArchiveDatasetError("Ledger run identity mismatch")
    status = next((row[name] for name in ("status", "run_status", "state") if name in keys), None)
    if status is None:
        raise ArchiveDatasetError("Ledger run lacks a verifiable commit status")
    if status is not None and str(status).upper() not in {"COMMITTED", "COMPLETED", "SUCCESS", "SUCCEEDED", "OFFICIAL", "OFFICIAL_COMPLETE"}:
        raise ArchiveDatasetError(f"Ledger run is not committed: {status}")
    official = next((row[name] for name in ("official", "is_official", "run_type", "cycle_type") if name in keys), None)
    if official is not None and str(official).upper() not in {"1", "TRUE", "YES", "OFFICIAL", "OFFICIAL_COMPLETE"}:
        raise ArchiveDatasetError("Ledger run is not OFFICIAL")
    if not run_id:
        raise ArchiveDatasetError("Ledger run has no run_id")
    return run_id


def _official_request(row: Mapping[str, Any]) -> None:
    request_kind = str(row["request_kind"] if "request_kind" in row.keys() else "").upper()
    parameters_text = row["parameters_json"] if "parameters_json" in row.keys() else ""
    try:
        parameters = json.loads(parameters_text) if parameters_text else {}
    except (TypeError, json.JSONDecodeError) as exc:
        raise ArchiveDatasetError("run request parameters are not valid JSON") from exc
    mode = str(parameters.get("mode") or parameters.get("run_mode") or "").upper() if isinstance(parameters, Mapping) else ""
    if request_kind not in {"PIT_SAFE_DECISION", "OFFICIAL"} and mode not in {"OFFICIAL", "PRODUCTION"}:
        raise ArchiveDatasetError("Ledger run request is not marked OFFICIAL")


def _historical_json_text(value: Any) -> str:
    """Match USAProvider's historical json.dumps formatting exactly."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
    )


def _cutoff(facts: Mapping[str, Any], attempt_cutoff: str | None) -> str:
    candidates = (
        facts.get("as_of"),
        facts.get("as_of_at"),
        facts.get("analysis_cutoff_at"),
        attempt_cutoff,
    )
    for value in candidates:
        if not isinstance(value, str) or not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise ArchiveDatasetError("historical record has no verified temporal cutoff")


def _week(value: str, run_id: str) -> str:
    match = re.search(r"(20\d\d-\d\d-\d\d)", run_id)
    date_text = match.group(1) if match else value[:10]
    try:
        date_value = datetime.fromisoformat(date_text).date()
    except ValueError as exc:
        raise ArchiveDatasetError("historical record has no usable week identity") from exc
    iso = date_value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _sources(facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    evidence = facts.get("evidence")
    if not isinstance(evidence, list):
        return []
    result: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        source = {
            key: item[key]
            for key in ("identifier", "url", "title", "excerpt", "published_at", "retrieved_at")
            if key in item
        }
        if source:
            result.append(source)
    return result


def load_ledger_dataset(
    path: str | Path,
    *,
    run_id: str | None = None,
    analytical_commit: str | None = None,
    immutable: bool = False,
) -> Dataset:
    """Export usable historical Sol inputs from a frozen Ledger SQLite file."""

    database = Path(path).resolve()
    if not database.is_file():
        raise ArchiveDatasetError(f"Ledger SQLite file does not exist: {database}")
    uri = database.as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise ArchiveDatasetError("cannot open Ledger SQLite read-only") from exc
    try:
        artifacts = {
            row["artifact_id"]: row
            for row in connection.execute(
                "SELECT * FROM artifact"
            )
        }
        attempt_rows = {
            row["attempt_id"]: row
            for row in connection.execute(
                """
                SELECT a.attempt_id, a.code_commit, rr.analysis_cutoff_at,
                       rr.request_kind, rr.parameters_json,
                       r.*
                FROM attempt AS a
                JOIN run_request AS rr ON rr.run_request_id = a.run_request_id
                LEFT JOIN run AS r ON r.attempt_id = a.attempt_id
                """
            )
        }
        interactions = list(connection.execute(
            """
            SELECT llm_interaction_id, attempt_id, request_artifact_id,
                   response_artifact_id, status
            FROM llm_interaction
            WHERE status = 'SUCCEEDED'
            ORDER BY attempt_id, llm_interaction_id
            """
        ))
    except sqlite3.Error as exc:
        raise ArchiveDatasetError("Ledger schema is not compatible with the adapter") from exc
    finally:
        connection.close()

    requests: dict[str, dict[str, Any]] = {}
    for interaction in interactions:
        request = _json_payload(artifacts, interaction["request_artifact_id"])
        if not isinstance(request, Mapping):
            raise ArchiveDatasetError("invocation request artifact is not an object")
        requests[interaction["llm_interaction_id"]] = {
            "interaction": interaction,
            "request": dict(request),
        }

    records: list[TickerInput] = []
    for analyst_id, item in requests.items():
        request = item["request"]
        if request.get("interaction_role") != "ANALYST":
            continue
        interaction = item["interaction"]
        attempt = attempt_rows.get(interaction["attempt_id"])
        if attempt is None:
            raise ArchiveDatasetError(f"missing attempt metadata for {analyst_id}")
        effective_run_id = _authoritative_run(attempt, run_id)
        _official_request(attempt)
        if analytical_commit is not None and str(attempt["code_commit"]) != analytical_commit:
            raise ArchiveDatasetError("Ledger analytical commit does not match requested commit")
        if run_id is not None and effective_run_id != run_id:
            continue
        context = _json_payload(artifacts, request["context_artifact_id"])
        if not isinstance(context, Mapping) or not context.get("items"):
            raise ArchiveDatasetError(f"Analyst context missing for {analyst_id}")
        facts_id = context["items"][0]["artifact_id"]
        facts = _json_payload(artifacts, facts_id)
        if not isinstance(facts, Mapping):
            raise ArchiveDatasetError(f"facts artifact is not an object for {analyst_id}")
        identity = facts.get("identity") if isinstance(facts.get("identity"), Mapping) else {}
        ticker = str(facts.get("ticker") or identity.get("ticker") or "").strip().upper()
        if not ticker:
            raise ArchiveDatasetError(f"cannot derive ticker for {analyst_id}")
        cutoff = _cutoff(facts, attempt["analysis_cutoff_at"])
        analyst_prompt = _text_payload(
            artifacts, request["client_effective_input_artifact_id"]
        )
        analyst_schema = _json_payload(
            artifacts, request["response_schema_artifact_id"]
        )
        success = _json_payload(artifacts, interaction["response_artifact_id"])
        parsed = _json_payload(artifacts, success["parsed_response_artifact_id"])
        analyst_output_text = _historical_json_text(parsed["parsed_value"])
        analyst_stage_id = success["effective_stage_result_artifact_id"]

        critic_candidates = []
        for candidate_id, candidate in requests.items():
            if candidate["interaction"]["attempt_id"] != interaction["attempt_id"]:
                continue
            candidate_request = candidate["request"]
            if candidate_request.get("interaction_role") != "CRITIC":
                continue
            candidate_context = _json_payload(
                artifacts, candidate_request["context_artifact_id"]
            )
            items = candidate_context.get("items") if isinstance(candidate_context, Mapping) else None
            if isinstance(items, list) and len(items) == 2 and items[1].get("artifact_id") == analyst_stage_id:
                critic_candidates.append(candidate)
        if len(critic_candidates) > 1:
            raise ArchiveDatasetError(f"ambiguous historical Critic lineage for {analyst_id}")
        critic_candidate = critic_candidates[0] if critic_candidates else None
        if critic_candidate is None:
            raise ArchiveDatasetError(
                f"no verifiable historical Critic linked to Analyst {analyst_id}"
            )
        critic_request = critic_candidate["request"]
        critic_prompt = _text_payload(
            artifacts, critic_request["client_effective_input_artifact_id"]
        )
        if analyst_output_text not in critic_prompt:
            raise ArchiveDatasetError(
                f"historical Critic prompt does not contain exact Analyst draft for {analyst_id}"
            )
        critic_template = critic_prompt.replace(
            analyst_output_text, "{{ANALYST_DRAFT_JSON}}", 1
        )
        critic_schema = _json_payload(
            artifacts, critic_request["response_schema_artifact_id"]
        )
        records.append(TickerInput.from_mapping({
            "record_id": f"{effective_run_id}:{ticker}:{analyst_id}",
            "ticker": ticker,
            "week": _week(cutoff, effective_run_id),
            "snapshot_id": effective_run_id,
            "data_cutoff_utc": cutoff,
            "snapshot": {
                "kind": "LEDGER_ARCHIVE_LINEAGE",
                "source": "TRINITY_LEDGER",
                "provenance": f"{effective_run_id}:{interaction['attempt_id']}",
                "captured_at": cutoff,
                "source_reference": str(database),
                "run_id": effective_run_id,
                "attempt_id": interaction["attempt_id"],
            },
            "facts": dict(facts),
            "sources": _sources(facts),
            "analyst_prompt": analyst_prompt,
            "critic_prompt_template": critic_template,
            "analyst_schema": analyst_schema,
            "critic_schema": critic_schema,
            "metadata": {
                "historical_analyst_interaction_id": analyst_id,
                "historical_critic_interaction_id": critic_candidate["interaction"]["llm_interaction_id"],
                "historical_reasoning_effort": (
                    _json_payload(
                        artifacts,
                        request["invocation_parameters_artifact_id"],
                    ).get("reasoning_effort")
                ),
                "analytical_commit": analytical_commit or attempt["code_commit"],
            },
        }))
    if not records:
        raise ArchiveDatasetError("no eligible linked Analyst/Critic records found")
    return Dataset(
        schema_name="trinity.sol-effort-v1.input-dataset",
        schema_version="1",
        source_type="REAL_LEDGER_ARCHIVE",
        source_reference=str(database),
        analytical_commit=analytical_commit,
        records=tuple(records),
    )

