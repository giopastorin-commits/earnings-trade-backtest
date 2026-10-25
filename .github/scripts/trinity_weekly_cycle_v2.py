"""Bind the frozen weekly-cycle implementation to the V2 marker namespace."""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Sequence

sys.path.insert(0, str(Path.cwd()))
from trinity.shadow import weekly_cycle


CONTRACT_VERSION = "TRINITY_WEEKLY_ORCHESTRATOR_V2"


def main(argv: Sequence[str] | None = None) -> int:
    configured = os.environ.get("TRINITY_ORCHESTRATOR_METHOD")
    if configured != CONTRACT_VERSION:
        raise SystemExit("TRINITY_ORCHESTRATOR_METHOD does not match the V2 registration")
    weekly_cycle.CONTRACT_VERSION = CONTRACT_VERSION
    return weekly_cycle.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
