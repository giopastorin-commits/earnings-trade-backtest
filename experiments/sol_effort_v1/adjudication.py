"""Blind factual-adjudication dataset builder.

This module creates review rows only from claims explicitly emitted by an
analysis. It never assigns correctness itself; reviewers must supply the
primary source and verdict independently.
"""

from __future__ import annotations

from typing import Any, Mapping

from .canonical import sha256_json


def build_adjudication_dataset(results: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    rows: list[dict[str, Any]] = []
    key: dict[str, str] = {}
    for arm in results.get("arms") or []:
        critic_hash = arm.get("critic_output_hash")
        if not critic_hash:
            continue
        blind_id = "analysis-" + sha256_json({
            "record_id": arm.get("record_id"), "arm_id": arm.get("arm_id"),
            "critic_output_hash": critic_hash,
        })[:20]
        key[blind_id] = str(arm.get("arm_id"))
        parsed = next((
            call.get("parsed") for call in results.get("calls") or []
            if call.get("call_id") == arm.get("critic_call_id")
        ), None)
        claims = parsed.get("claims") if isinstance(parsed, Mapping) else None
        if not isinstance(claims, list):
            continue
        for index, claim in enumerate(claims):
            claim_text = claim if isinstance(claim, str) else claim.get("text") if isinstance(claim, Mapping) else None
            if not isinstance(claim_text, str) or not claim_text.strip():
                continue
            rows.append({
                "adjudication_id": f"{blind_id}:{index}", "blind_analysis_id": blind_id,
                "claim_text": claim_text, "primary_source": None, "verdict": None,
                "severity": None, "corrected_by_critic": None, "introduced_by_critic": None,
                "reviewer_disagreement": None, "review_status": "UNREVIEWED",
            })
    return {
        "schema_name": "trinity.sol-effort-v1.blind-adjudication",
        "schema_version": "1", "rows": rows, "factual_scores_assigned": False,
        "reason_if_empty": "No structured claims were emitted by the current response schema" if not rows else None,
    }, key
