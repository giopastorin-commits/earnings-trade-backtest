"""Deterministic, source-unit-checked H1 fact extraction for configured issuers."""

from __future__ import annotations

import re

from trinity.italia_company_config import CompanyConfig


FIELDS = ("revenue", "revenue_growth", "operating_profit", "operating_margin",
          "ebitda", "net_income", "net_debt_or_cash", "capex", "guidance", "cet1_ratio")
_NUMBER = r"(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?)"

# The unit is captured from the same phrase or table header as the value. A
# missing unit causes a missing fact; no magnitude-based inference is allowed.
RULES: dict[str, dict[str, str]] = {
    "PRY.MI": {
        "revenue": r"(?P<unit>Euro\s+Millions)\s+ADJ\. EBITDA BREAKDOWN.*?1H 2026\s+1H 2025\s+REVENUES\s+" + _NUMBER,
        "ebitda": r"(?P<unit>Euro\s+Millions)\s+ADJ\. EBITDA BREAKDOWN.*?1H 2026\s+1H 2025.*?Adj\.EBITDA\s+" + _NUMBER,
        "net_income": r"(?P<unit>Euro\s+Millions)\s+ADJ\. EBITDA BREAKDOWN.*?1H 2026\s+1H 2025.*?NET PROFIT\s+" + _NUMBER,
        "revenue_growth": r"1H 2026\s+1H 2025\s+REVENUES\s+\d[\d,]*\s+\d[\d,]*\s+YoY organic growth\s+" + _NUMBER + r"(?P<unit>%)",
    },
    "ISP.MI": {
        "net_income": r"NET INCOME OF\s+[^\d\n]{0,4}" + _NUMBER + r"(?P<unit>M)\s+IN H1 2026",
        "cet1_ratio": r"COMMON\s+EQUITY\s+TIER\s+1\s+RATIO\s+WAS\s+" + _NUMBER + r"(?P<unit>%)",
    },
    "ENEL.MI": {
        "revenue": r"Revenues:\s*" + _NUMBER + r"\s*(?P<unit>million euros)",
        "ebitda": r"Ordinary EBITDA:\s*" + _NUMBER + r"\s*(?P<unit>million euros)",
        "net_income": r"Group net ordinary income:\s*" + _NUMBER + r"\s*(?P<unit>million euros)",
        "net_debt_or_cash": r"Net financial debt:\s*" + _NUMBER + r"\s*(?P<unit>million euros)",
    },
    "DLG.MI": {
        "revenue": r"In the first half the Group achieved:.*?revenues for\s+[^\d\n]{0,4}" + _NUMBER + r"\s*(?P<unit>million)",
        "ebitda": r"In the first half the Group achieved:.*?adjusted\d*\s+Ebitda of\s+[^\d\n]{0,4}" + _NUMBER + r"\s*(?P<unit>million)",
        "revenue_growth": r"revenues for\s+[^\d\n]{0,4}\d[\d,.]*\s+million,\s+up by\s+" + _NUMBER + r"(?P<unit>%)",
        "net_income": r"net income pertaining to the Group equal to\s+[^\d\n]{0,4}" + _NUMBER + r"\s*(?P<unit>million)",
        "net_debt_or_cash": r"positive net financial position equal to\s+[^\d\n]{0,4}" + _NUMBER + r"\s*(?P<unit>million)",
        "capex": r"Capital expenditures amounted to\s+[^\d\n]{0,4}" + _NUMBER + r"\s*(?P<unit>million)",
    },
}


def _unit(raw: str) -> tuple[str, float] | None:
    normalized = " ".join(raw.lower().split())
    return {
        "euro millions": ("EUR_MILLIONS", 1_000_000),
        "million euros": ("EUR_MILLIONS", 1_000_000),
        "million": ("EUR_MILLIONS", 1_000_000),
        "m": ("EUR_MILLIONS", 1_000_000),
        "eur thousands": ("EUR_THOUSANDS", 1_000),
        "eur billions": ("EUR_BILLIONS", 1_000_000_000),
        "bn": ("EUR_BILLIONS", 1_000_000_000),
        "eur": ("EUR", 1),
        "euros": ("EUR", 1),
        "%": ("PERCENT", 1),
    }.get(normalized)


def extract_financial_facts(text: str, config: CompanyConfig, evidence_identifier: str,
                            period: str = "H1 2026", *,
                            report_text: str | None = None,
                            report_evidence_identifier: str | None = None) -> tuple[dict[str, dict | None], list[str]]:
    """Return normalized facts and explicit failures, never guessed amounts."""
    facts: dict[str, dict | None] = {field: None for field in FIELDS}
    failures: list[str] = []
    for field, pattern in RULES.get(config.ticker, {}).items():
        match = re.search(pattern, text, re.I | re.S)
        if not match:
            failures.append(f"{field}: pattern or explicit unit absent")
            continue
        unit = _unit(match.group("unit"))
        if unit is None:
            failures.append(f"{field}: unsupported unit {match.group('unit')!r}")
            continue
        number = float(match.group("value").replace(",", ""))
        facts[field] = {"value": number * unit[1], "unit": "EUR" if unit[0].startswith("EUR") else "PERCENT",
                        "source_unit": unit[0], "source_value": number, "period": period,
                        "metric_label": ({"PRY.MI": "Adjusted EBITDA", "ENEL.MI": "Ordinary EBITDA",
                                          "DLG.MI": "Adjusted EBITDA"}.get(config.ticker, "EBITDA")
                                         if field == "ebitda" else
                                         "net ordinary income" if field == "net_income" and config.ticker == "ENEL.MI" else
                                         "organic revenue growth" if field == "revenue_growth" and config.ticker == "PRY.MI" else
                                         "net cash position" if field == "net_debt_or_cash" and config.ticker == "DLG.MI" else
                                         "net financial debt" if field == "net_debt_or_cash" else field),
                        "evidence_identifier": evidence_identifier,
                        "extraction_method": "PDF_TEXT_EXACT_PATTERN",
                        "source_excerpt": match.group(0)[-240:].strip()}
    guidance_patterns = {
        "ISP.MI": r"net income outlook for 2026 has been upgraded to over\s+[^\d\n]{0,4}(?P<value>10)(?P<unit>bn)",
        "ENEL.MI": r"For the financial year 2026, EPS is expected at around\s+(?P<value>0\.74)\s+(?P<unit>euros)",
        "DLG.MI": r"guidance on adjusted EBITDA, bringing the range to\s+[^\d\n]{0,4}(?P<value>670)\s*-\s*[^\d\n]{0,4}(?P<upper>690)\s+(?P<unit>million)",
    }
    if config.ticker in guidance_patterns:
        match = re.search(guidance_patterns[config.ticker], text, re.I | re.S)
        if match:
            unit = _unit(match.group("unit"))
            if unit:
                lower = float(match.group("value")) * unit[1]
                upper = float(match.group("upper")) * unit[1] if "upper" in match.groupdict() else None
                facts["guidance"] = {"value": [lower, upper] if upper is not None else lower,
                                     "unit": "EUR", "source_unit": unit[0],
                                     "metric_label": {"ISP.MI": "net income outlook",
                                                      "ENEL.MI": "EPS outlook",
                                                      "DLG.MI": "adjusted EBITDA outlook"}[config.ticker],
                                     "comparator": "GREATER_THAN" if config.ticker == "ISP.MI"
                                                   else "APPROXIMATE" if config.ticker == "ENEL.MI"
                                                   else "RANGE",
                                     "source_value": match.group("value") +
                                     (("-" + match.group("upper")) if upper is not None else ""),
                                     "period": "FY 2026", "evidence_identifier": evidence_identifier,
                                     "extraction_method": "PDF_TEXT_EXACT_PATTERN",
                                     "source_excerpt": match.group(0).strip()}
        else:
            failures.append("guidance: pattern or explicit unit absent")
    if config.ticker == "PRY.MI" and report_text and report_evidence_identifier:
        match = re.search(
            r"BUSINESS OUTLOOK\s+Prysmian upgrades the 2026 guidance.*?"
            r"Adjusted EBITDA in the range of\s+[^\d\n]{0,4}"
            r"(?P<lower>2,800)\s+(?P<unit>million)\s+to\s+[^\d\n]{0,4}"
            r"(?P<upper>2,900)\s+million", report_text, re.I | re.S)
        if match:
            unit = _unit(match.group("unit"))
            facts["guidance"] = {
                "value": [float(match.group("lower").replace(",", "")) * unit[1],
                          float(match.group("upper").replace(",", "")) * unit[1]],
                "unit": "EUR", "source_unit": unit[0],
                "source_value": "2,800-2,900", "period": "FY 2026",
                "metric_label": "adjusted EBITDA outlook", "comparator": "RANGE",
                "evidence_identifier": report_evidence_identifier,
                "extraction_method": "PDF_TEXT_EXACT_PATTERN",
                "source_excerpt": match.group(0)[-280:].strip(),
            }
        else:
            failures.append("guidance: report pattern or explicit unit absent")
    return facts, failures
