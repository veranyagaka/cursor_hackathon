from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import shutil

from openai import OpenAI

from .config import Settings
from .github_ops import Issue
from .shell import run


@dataclass
class FixResult:
    patch_text: str
    validation_log: str


def _detect_validation_command(repo_dir: Path) -> list[str] | None:
    if (repo_dir / "pytest.ini").exists() or (repo_dir / "pyproject.toml").exists():
        if shutil.which("pytest"):
            return ["pytest", "-q"]
    if (repo_dir / "package.json").exists():
        return ["npm", "test", "--", "--runInBand"]
    return None


def _repo_snapshot(repo_dir: Path, max_files: int = 60) -> str:
    tracked = run(["git", "ls-files"], cwd=repo_dir).stdout.strip().splitlines()
    selected = tracked[:max_files]
    tree = "\n".join(selected)
    readme = ""
    for name in ("README.md", "readme.md", "README.rst"):
        p = repo_dir / name
        if p.exists():
            readme = p.read_text(encoding="utf-8", errors="ignore")[:8000]
            break
    return (
        "Repository file list (truncated):\n"
        f"{tree}\n\n"
        "README excerpt:\n"
        f"{readme}"
    )


def _generate_patch(
    repo_dir: Path,
    issue: Issue,
    settings: Settings,
    extra_instructions: str = "",
) -> str:
    if not settings.openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Set it to enable automated code fixing."
        )
    client = OpenAI(api_key=settings.openai_api_key)
    snapshot = _repo_snapshot(repo_dir)

    prompt = f"""
You are a senior software engineer creating a minimal fix for a GitHub issue.
Return ONLY a valid git unified diff patch.
Do not include markdown fences.
Do not modify lockfiles unless strictly necessary.

Issue #{issue.number}: {issue.title}
Issue URL: {issue.url}
Issue body:
{issue.body}

{snapshot}

Additional instructions:
{extra_instructions}
"""

    response = client.responses.create(
        model=settings.openai_model,
        input=prompt,
    )
    patch = response.output_text.strip()
    if "diff --git" not in patch:
        raise RuntimeError("Model did not return a valid unified diff patch.")
    return patch


def _try_apply_patch(repo_dir: Path, patch_arg: str) -> str | None:
    attempts = [
        ["git", "apply", "--index", patch_arg],
        ["git", "apply", "--3way", "--index", patch_arg],
        ["git", "apply", patch_arg],
        ["git", "apply", "--3way", patch_arg],
    ]
    errors: list[str] = []
    for cmd in attempts:
        result = run(cmd, cwd=repo_dir, check=False)
        if result.returncode == 0:
            return None
        errors.append(f"{' '.join(cmd)}\n{result.stderr.strip()}")
    return "\n\n".join(errors)


def _apply_patch(repo_dir: Path, patch_text: str) -> str | None:
    patch_file = repo_dir / ".autofix.patch"
    patch_arg = patch_file.name
    patch_file.write_text(patch_text, encoding="utf-8")
    try:
        return _try_apply_patch(repo_dir, patch_arg)
    finally:
        if patch_file.exists():
            patch_file.unlink()


def _validate(repo_dir: Path) -> str:
    cmd = _detect_validation_command(repo_dir)
    if not cmd:
        return "No validation command auto-detected."
    result = run(cmd, cwd=repo_dir, check=False)
    if result.returncode == 0:
        return f"Validation passed: {' '.join(cmd)}\n{result.stdout}"
    return (
        f"Validation failed ({' '.join(cmd)}):\n"
        f"{result.stdout}\n{result.stderr}"
    )


def fix_issue(
    repo_dir: Path,
    issue: Issue,
    settings: Settings,
    progress: Callable[[str], None] | None = None,
) -> FixResult:
    if progress:
        progress("Generating patch with OpenAI")
    patch_text = _generate_patch(repo_dir, issue, settings)
    if progress:
        progress("Applying generated patch")
    apply_error = _apply_patch(repo_dir, patch_text)
    if apply_error:
        if progress:
            progress("Patch apply failed, retrying with fresh context")
        retry_instructions = (
            "The previous patch did not apply cleanly to the current repository state. "
            "Regenerate the patch so every hunk applies to the exact current files. "
            "Prefer minimal, targeted edits."
        )
        patch_text = _generate_patch(
            repo_dir, issue, settings, extra_instructions=retry_instructions
        )
        if progress:
            progress("Applying regenerated patch")
        apply_error = _apply_patch(repo_dir, patch_text)
        if apply_error:
            raise RuntimeError(
                "Failed to apply generated patch after retry.\n\n"
                f"Apply errors:\n{apply_error}"
            )
    if progress:
        progress("Running validation")
    validation_log = _validate(repo_dir)
    if progress:
        progress("Validation finished")
    return FixResult(patch_text=patch_text, validation_log=validation_log)
