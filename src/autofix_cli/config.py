from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None
    openai_model: str
    workspace: Path

    @staticmethod
    def from_env(workspace: Path | None = None) -> "Settings":
        selected_workspace = workspace or Path.cwd() / ".autofix-workspace"
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("AUTOFIX_OPENAI_MODEL", "gpt-4.1"),
            workspace=selected_workspace,
        )
