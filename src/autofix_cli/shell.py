from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def raise_for_error(self) -> "CommandResult":
        if self.returncode != 0:
            joined = " ".join(self.command)
            raise RuntimeError(f"Command failed: {joined}\n{self.stderr.strip()}")
        return self


def run(
    command: list[str],
    cwd: Path | None = None,
    check: bool = True,
) -> CommandResult:
    proc = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    result = CommandResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    if check:
        result.raise_for_error()
    return result
