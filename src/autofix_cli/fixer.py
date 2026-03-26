from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import time

from openai import OpenAI

from .config import Settings
from .github_ops import Issue
from .shell import run


@dataclass
class FixResult:
    patch_text: str
    validation_log: str


@dataclass
class DoctorPatchResult:
    probe_name: str


def doctor_patch(repo_dir: Path, keep_changes: bool = False) -> DoctorPatchResult:
    """Validate that git can parse and apply a minimal synthetic patch."""
    repo_check = run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=repo_dir, check=False
    )
    if repo_check.returncode != 0:
        raise RuntimeError(f"Not a git repository: {repo_dir}")

    probe_name = f".autofix-doctor-probe-{int(time.time() * 1000)}.txt"
    patch_text = (
        f"diff --git a/{probe_name} b/{probe_name}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{probe_name}\n"
        "@@ -0,0 +1 @@\n"
        f"+autofix-doctor-probe-{int(time.time())}\n"
    )
    patch_file = repo_dir / ".autofix-doctor.patch"
    patch_file.write_text(patch_text, encoding="utf-8")

    try:
        check = run(
            ["git", "apply", "--check", patch_file.name], cwd=repo_dir, check=False
        )
        if check.returncode != 0:
            raise RuntimeError(
                "Synthetic patch failed git apply --check.\n"
                f"{check.stderr.strip() or check.stdout.strip()}"
            )

        apply_result = run(
            ["git", "apply", patch_file.name], cwd=repo_dir, check=False
        )
        if apply_result.returncode != 0:
            raise RuntimeError(
                "Synthetic patch failed git apply.\n"
                f"{apply_result.stderr.strip() or apply_result.stdout.strip()}"
            )

        if not keep_changes:
            reverse = run(
                ["git", "apply", "-R", patch_file.name], cwd=repo_dir, check=False
            )
            if reverse.returncode != 0:
                raise RuntimeError(
                    "Synthetic patch applied but rollback failed.\n"
                    f"{reverse.stderr.strip() or reverse.stdout.strip()}"
                )
    finally:
        if patch_file.exists():
            patch_file.unlink()
    return DoctorPatchResult(probe_name=probe_name)


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

    invalid_outputs: list[str] = []
    max_attempts = 3
    for attempt in range(max_attempts):
        retry_context = ""
        if invalid_outputs:
            previous = invalid_outputs[-1][:2000]
            retry_context = (
                "\nYour last output was invalid because it was not parseable as a unified diff.\n"
                "Return ONLY patch text. No explanations, no markdown prose.\n"
                f"Previous invalid output excerpt:\n{previous}\n"
            )
        response = client.responses.create(
            model=settings.openai_model,
            input=f"{prompt}{retry_context}",
        )
        raw = response.output_text.strip()
        patch = _extract_patch(raw)
        if patch and _looks_like_unified_diff(patch):
            return patch
        invalid_outputs.append(raw)

    debug_file = repo_dir / ".autofix-last-invalid-response.txt"
    debug_file.write_text(
        "\n\n--- INVALID OUTPUT SEPARATOR ---\n\n".join(invalid_outputs),
        encoding="utf-8",
    )
    raise RuntimeError(
        "Model did not return a valid unified diff patch after retries. "
        f"Saved model output to: {debug_file}"
    )


def _extract_patch(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None

    if stripped.startswith("```"):
        match = re.search(r"```(?:diff)?\s*(.*?)```", stripped, flags=re.DOTALL)
        if match:
            stripped = match.group(1).strip()

    # If the model prepends prose, keep only content from the first diff header.
    first_header_patterns = [
        r"^diff --git\s",
        r"^---\s+a/",
        r"^---\s+",
    ]
    first_start: int | None = None
    for pattern in first_header_patterns:
        match = re.search(pattern, stripped, flags=re.MULTILINE)
        if match:
            start = match.start()
            first_start = start if first_start is None else min(first_start, start)
    if first_start is not None and first_start > 0:
        stripped = stripped[first_start:].lstrip()

    return stripped or None


def _looks_like_unified_diff(text: str) -> bool:
    has_git_header = "diff --git " in text
    has_file_headers = ("--- a/" in text and "+++ b/" in text) or (
        text.startswith("--- ") and "\n+++ " in text
    )
    # Most patch edits include hunks; this avoids accepting prose with file header-like text.
    has_hunks = "\n@@ " in text or text.startswith("@@ ")
    if has_git_header and (has_hunks or "new file mode" in text or "deleted file mode" in text):
        return True
    # Some tools emit plain unified diff without diff --git header.
    return has_file_headers and has_hunks


def _is_no_valid_patch_error(apply_error: str | None) -> bool:
    if not apply_error:
        return False
    return "No valid patches in input" in apply_error


def _retry_instructions_from_apply_error(apply_error: str) -> str:
    if _is_no_valid_patch_error(apply_error):
        return (
            "The previous output was not an actual git patch. "
            "Return only a valid unified diff with real hunks. "
            "Use standard patch structure: diff --git headers, ---/+++ file headers, and @@ hunks. "
            "Do not include explanations or any text outside the patch."
        )
    return (
        "The previous patch did not apply cleanly to the current repository state. "
        "Regenerate the patch so every hunk applies to the exact current files. "
        "Prefer minimal, targeted edits."
    )


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
    max_apply_retries: int = 3,
) -> FixResult:
    if progress:
        progress("Generating patch with OpenAI")
    patch_text = _generate_patch(repo_dir, issue, settings)

    apply_error: str | None = None
    for attempt in range(max_apply_retries):
        if attempt == 0:
            if progress:
                progress("Applying generated patch")
        else:
            if progress:
                progress(f"Patch apply failed (attempt {attempt}), retrying with fresh context")
            retry_instructions = _retry_instructions_from_apply_error(apply_error or "")
            # On the final attempt, escalate to very explicit instructions
            if attempt == max_apply_retries - 1:
                retry_instructions = (
                    "CRITICAL: Your previous outputs were NOT valid git patches. "
                    "You MUST return ONLY a unified diff. "
                    "Start immediately with 'diff --git a/...' or '--- a/...'. "
                    "No explanations, no markdown, no prose of any kind. "
                    "If you cannot produce a real patch, output a no-op diff that touches a comment. "
                    "Structure: diff --git header, --- / +++ file headers, then @@ hunks with context lines."
                )
            patch_text = _generate_patch(
                repo_dir, issue, settings, extra_instructions=retry_instructions
            )
            if progress:
                progress(f"Applying regenerated patch (attempt {attempt + 1})")

        apply_error = _apply_patch(repo_dir, patch_text)
        if apply_error is None:
            break
    else:
        # Save the last invalid patch for debugging
        debug_patch = repo_dir / ".autofix-last-failed.patch"
        debug_patch.write_text(patch_text, encoding="utf-8")
        raise RuntimeError(
            f"Failed to apply generated patch after {max_apply_retries} attempts.\n\n"
            f"Last patch saved to: {debug_patch}\n\n"
            f"Apply errors:\n{apply_error}"
        )

    if progress:
        progress("Running validation")
    validation_log = _validate(repo_dir)
    if progress:
        progress("Validation finished")
    return FixResult(patch_text=patch_text, validation_log=validation_log)