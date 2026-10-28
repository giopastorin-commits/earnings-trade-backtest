"""Frozen experimental arms and request-level constants."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Effort = Literal["medium", "high"]
MODEL = "gpt-5.6-sol"
API_URL = "https://api.openai.com/v1/responses"
AUTHORIZATION_SENTINEL = "I_UNDERSTAND_LIVE_SHADOW_IS_PAID_AND_ISOLATED"


@dataclass(frozen=True)
class Arm:
    arm_id: str
    analyst_effort: Effort
    critic_effort: Effort


ARMS: tuple[Arm, ...] = (
    Arm("A", "medium", "medium"),
    Arm("B", "medium", "high"),
    Arm("C", "high", "medium"),
    Arm("D", "high", "high"),
)

