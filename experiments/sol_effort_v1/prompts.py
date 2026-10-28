"""Prompt construction with explicit draft immutability."""

from __future__ import annotations

from .canonical import canonical_text
from .dataset import TickerInput


def render_critic_prompt(record: TickerInput, analyst_output_text: str) -> str:
    """Substitute the exact analyst text once; never rewrite the draft."""

    if not isinstance(analyst_output_text, str) or not analyst_output_text:
        raise ValueError("analyst output text must be non-empty")
    template = record.critic_prompt_template
    rendered = template
    for marker, replacement in {
        "{{TICKER}}": record.ticker,
        "{{FACTS_JSON}}": canonical_text(record.facts),
        "{{ANALYST_DRAFT_JSON}}": analyst_output_text,
    }.items():
        rendered = rendered.replace(marker, replacement)
    if "{{ANALYST_DRAFT_JSON}}" in rendered:
        raise ValueError("critic template substitution failed")
    return rendered

