"""One-company, audited Technoprobe snapshot and real LLM adapter for Italia V1.

The snapshot is a dated transcription of primary sources, not a live market feed.
Missing returns and cash-flow metrics intentionally remain null. No API key,
scraping, or implicit source fallback is used by this module.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import date
import json
import math
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from trinity.italia_v1 import (
    ItaliaV1InputError,
    analyze_company,
    build_facts,
    save_thesis,
)


SNAPSHOT = Path(__file__).parent / "technoprobe_2026-09-18.fixture"
MODEL = "gpt-5.6-sol"


def load_real_company(
    company: str, as_of: str | date, *, snapshot_path: str | Path = SNAPSHOT
) -> dict[str, object]:
    """Map a dated, sourced Technoprobe snapshot to a V1 CompanyInput.

    This V1 loader deliberately supports only the exact snapshot date. It must
    never relabel an old price as the current price for a later as-of date.
    """

    if not isinstance(company, str) or company.strip().upper() != "TECHNOPROBE":
        raise ItaliaV1InputError("only TECHNOPROBE is supported by this snapshot loader")
    with Path(snapshot_path).open(encoding="utf-8") as handle:
        snapshot = json.load(handle)
    if not isinstance(snapshot, dict):
        raise ItaliaV1InputError("snapshot must be an object")
    requested_day = as_of.isoformat() if isinstance(as_of, date) else as_of
    if requested_day != snapshot.get("snapshot_as_of"):
        raise ItaliaV1InputError(
            "snapshot as_of mismatch; no live refresh or stale-price fallback is available"
        )
    if snapshot.get("ticker") != "TPRO" or snapshot.get("isin") != "IT0005482333":
        raise ItaliaV1InputError("snapshot identity does not match Technoprobe")
    report = snapshot.get("financial_report")
    if not isinstance(report, dict) or report.get("unit") != "thousands" or report.get("currency") != "EUR":
        raise ItaliaV1InputError("financial report must specify EUR thousands")
    report_fields = (
        "revenue", "revenue_growth_pct_reported", "operating_profit", "net_income", "net_cash"
    )
    for field in report_fields:
        value = report.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ItaliaV1InputError(f"financial_report.{field} must be finite or null")
    if report.get("revenue") is not None and report["revenue"] <= 0:
        raise ItaliaV1InputError("financial_report.revenue must be positive")
    evidence = snapshot.get("evidence")
    if not isinstance(evidence, list):
        raise ItaliaV1InputError("snapshot.evidence must be a list")
    evidence_ids = {item.get("identifier") for item in evidence if isinstance(item, dict)}
    if {"IT0005482333-2026-09-18", "PR-H1-2026", "CHINA-PARTNERSHIP-2026-08-14"} - evidence_ids:
        raise ItaliaV1InputError("snapshot is missing required primary-source evidence")
    events = snapshot.get("events")
    if not isinstance(events, list):
        raise ItaliaV1InputError("snapshot.events must be a list")
    for event in events:
        if not isinstance(event, dict) or event.get("facts", {}).get("evidence_identifier") not in evidence_ids:
            raise ItaliaV1InputError("event must reference an evidence identifier")
    revenue = report.get("revenue")
    operating_profit = report.get("operating_profit")
    net_income = report.get("net_income")
    net_cash = report.get("net_cash")
    company_input: dict[str, object] = {
        "company_name": snapshot.get("company_name"),
        "ticker": snapshot["ticker"],
        "provider_symbol": snapshot.get("provider_symbol"),
        "isin": snapshot["isin"],
        "as_of": requested_day,
        "price": snapshot.get("price", {}),
        "fundamentals": {
            "revenue": revenue * 1000 if revenue is not None else None,
            "revenue_growth": report.get("revenue_growth_pct_reported"),
            "operating_margin": (
                100 * operating_profit / revenue
                if operating_profit is not None and revenue is not None else None
            ),
            "net_income": net_income * 1000 if net_income is not None else None,
            "free_cash_flow": None,
            "net_debt": -net_cash * 1000 if net_cash is not None else None,
            "published_at": report.get("published_at"),
        },
        "events": events,
        "evidence": evidence,
    }
    # The existing point-in-time validator rejects any future-dated source.
    build_facts(company_input, requested_day)
    return company_input


_ANALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "fundamental_analysis", "earnings_and_news_analysis", "price_context",
        "bull_case", "bear_case", "catalysts", "risks", "thesis_invalidation",
        "proposed_status", "confidence",
    ],
    "properties": {
        **{field: {"type": "string"} for field in (
            "fundamental_analysis", "earnings_and_news_analysis", "price_context",
            "bull_case", "bear_case", "thesis_invalidation",
        )},
        "catalysts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "proposed_status": {"type": "string", "enum": ["PASS", "WATCH", "INVESTIGATE"]},
        "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
    },
}
_CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["notes", "status", "confidence"],
    "properties": {
        "notes": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["PASS", "WATCH", "INVESTIGATE"]},
        "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
    },
}


class LLMProviderError(RuntimeError):
    """The configured LLM did not return a usable structured answer."""


class CodexCLIProvider:
    """Provider-agnostic V1 protocol implemented through authenticated Codex CLI.

    Each call runs in an empty temporary directory, read-only and ephemeral.
    No fallback model, provider or synthetic answer is used on failure.
    """

    model_version = f"codex-cli:{MODEL}"

    def _request(self, prompt: str, schema: Mapping[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory(prefix="trinity-italia-llm-") as directory:
            root = Path(directory)
            schema_path = root / "schema.json"
            result_path = root / "result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                "codex", "exec", "--ephemeral", "--sandbox", "read-only",
                "--skip-git-repo-check", "--ignore-user-config", "--model", MODEL,
                "--output-schema", str(schema_path),
                "--output-last-message", str(result_path), "-",
            ]
            try:
                completed = subprocess.run(
                    command, input=prompt, text=True, encoding="utf-8", capture_output=True,
                    cwd=root, timeout=240, check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LLMProviderError(f"Codex CLI unavailable: {type(exc).__name__}") from exc
            if completed.returncode != 0 or not result_path.is_file():
                diagnostic = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "no diagnostic"
                raise LLMProviderError(
                    f"Codex CLI failed without fallback (exit={completed.returncode}; {diagnostic[:240]})"
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise LLMProviderError("Codex CLI returned invalid JSON") from exc
            if not isinstance(result, dict):
                raise LLMProviderError("Codex CLI result must be an object")
            return result

    def analyze(self, facts: Mapping[str, object]) -> Mapping[str, object]:
        """Produce a structured draft from supplied facts; do no arithmetic."""

        prompt = (
            "Sei l'Analyst di TRINITY Italia V1. Rispondi in italiano, solo nello schema JSON. "
            "Usa ESCLUSIVAMENTE i facts forniti; ogni claim materiale deve citare un "
            "identifier di evidence fra parentesi quadre. Distingui fatti, guidance e "
            "inferenze. Non calcolare rendimenti, margini, ratio o valuation: usa soltanto "
            "i numeri presenti nei facts. Se un dato manca, dichiaralo; non inventarlo. "
            "Per la partnership cinese, distingui la sottoscrizione di nuovo capitale "
            "da una cessione di azioni esistenti: non chiamarla cessione. "
            "PASS indica evidenza insufficiente o tesi non supportata, WATCH monitoraggio, "
            "INVESTIGATE ulteriore approfondimento; non sono segnali buy/sell. "
            "Niente dati esterni o successivi ad as_of.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        return self._request(prompt, _ANALYST_SCHEMA)

    def critique(
        self, facts: Mapping[str, object], draft: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Audit draft claims and return notes plus conservative final labels."""

        prompt = (
            "Sei il Critic di TRINITY Italia V1. Rispondi in italiano, solo nello schema JSON. "
            "Controlla claim non supportati, cifre assenti dai facts, contraddizioni, "
            "interpretazioni troppo forti, prove insufficienti e coerenza di status e "
            "confidence. Elenca problemi specifici in notes, citando la frase e l'evidence "
            "pertinente. Se un claim materiale resta non supportato, imposta PASS/LOW; "
            "altrimenti riduci la confidence o usa WATCH se opportuno. Non inventare dati. "
            "PASS richiede sempre confidence LOW; non restituire PASS con MEDIUM o HIGH. "
            "Se la bozza è coerente, puoi mantenere status e confidence proposti. "
            "Le notes devono giustificare il verdetto finale, senza affermare che una "
            "coppia di status/confidence diversa è quella appropriata. Status e "
            "confidence non sono raccomandazioni di trading.\nFACTS:\n"
            + json.dumps(facts, ensure_ascii=False, allow_nan=False, sort_keys=True)
            + "\nDRAFT:\n"
            + json.dumps(draft, ensure_ascii=False, allow_nan=False, sort_keys=True)
        )
        result = self._request(prompt, _CRITIC_SCHEMA)
        if result.get("status") == "PASS" and result.get("confidence") != "LOW":
            raise LLMProviderError("incoherent critic result: PASS requires LOW confidence")
        return result


def main(argv: list[str] | None = None) -> int:
    """Run and save the one supported real-data thesis."""

    parser = argparse.ArgumentParser(description="Analyze one Technoprobe source snapshot")
    parser.add_argument("--as-of", default="2026-09-18")
    parser.add_argument("--storage-dir", default="theses")
    args = parser.parse_args(argv)
    company_input = load_real_company("TECHNOPROBE", args.as_of)
    thesis = analyze_company(company_input, args.as_of, CodexCLIProvider())
    path = save_thesis(thesis, args.storage_dir)
    print(json.dumps(thesis.to_dict(), ensure_ascii=False, indent=2))
    print(f"Saved thesis: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
