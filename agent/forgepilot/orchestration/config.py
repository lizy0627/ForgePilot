"""Dormant orchestration feature flag; legacy remains the runtime default."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Literal

from ..errors import ForgePilotError


@dataclass(frozen=True, slots=True)
class OrchestrationConfig:
    mode: Literal["legacy", "multi_agent"] = "legacy"

    def __post_init__(self) -> None:
        if self.mode not in {"legacy", "multi_agent"}:
            raise ValueError("orchestration.mode must be legacy or multi_agent")


def load_orchestration_config(path: str | Path | None = None) -> OrchestrationConfig:
    """Read only [orchestration]. The flag is intentionally not wired to API v1."""
    if path is None:
        return OrchestrationConfig()
    try:
        source = Path(path)
        if not source.is_absolute() or source.stat().st_size > 65_536:
            raise ValueError()
        document = tomllib.loads(source.read_text(encoding="utf-8"))
        section = document.get("orchestration", {})
        if not isinstance(section, dict) or set(section) - {"mode"}:
            raise ValueError()
        mode = section.get("mode", "legacy")
        if not isinstance(mode, str):
            raise ValueError()
        return OrchestrationConfig(mode=mode)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
        raise ForgePilotError("orchestration_config", "Invalid orchestration configuration") from None


__all__ = ["OrchestrationConfig", "load_orchestration_config"]
