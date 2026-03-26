from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import shutil

from .shell import run


@dataclass
class Issue:
    number: int
    title: str
    body: str
    url: str


def ensure_gh_installed() -> None:
    gh_bin = os.getenv("AUTOFIX_GH_BIN", "gh")
    if not shutil.which(gh_bin):
        raise RuntimeError(
            f"GitHub CLI binary '{gh_bin}' was not found in PATH. "
            "Install GitHub CLI or set AUTOFIX_GH_BIN to its executable name/path."
        )
    version = run([gh_bin, "--version"], check=False)
    text = f"{version.stdout}\n{version.stderr}".strip()
    # Accept common official outputs like:
    # - "gh version X.Y.Z ..."
    # - "GitHub CLI X.Y.Z"
    normalized = text.lower()
    has_expected_version_marker = (
        "gh version" in normalized or "github cli" in normalized
    )
    if version.returncode != 0 or not has_expected_version_marker:
        raise RuntimeError(
            f"Binary '{gh_bin}' is available but does not appear to be GitHub CLI.\n"
            "Install GitHub CLI (https://cli.github.com/) and make sure it is first in PATH, "
            "or set AUTOFIX_GH_BIN to the correct executable."
        )


def _gh_command(args: list[str]) -> list[str]:
    gh_bin = os.getenv("AUTOFIX_GH_BIN", "gh")
    return [gh_bin, *args]


def ensure_git_installed() -> None:
    if not shutil.which("git"):
        raise RuntimeError("git is required but was not found in PATH.")


def repo_slug_from_url(repo_url: str) -> str:
    clean = repo_url.strip().removesuffix(".git")
    match = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+)$", clean)
    if not match:
        raise ValueError("Expected a GitHub URL like https://github.com/owner/repo")
    return match.group("slug")


def clone_repo(repo_url: str, workspace: Path) -> Path:
    ensure_git_installed()
    workspace.mkdir(parents=True, exist_ok=True)
    slug = repo_slug_from_url(repo_url)
    repo_name = slug.split("/")[-1]
    dest = workspace / repo_name
    if dest.exists():
        run(["git", "fetch", "--all"], cwd=dest)
        head_ref = run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=dest,
            check=False,
        ).stdout.strip()
        default_branch = head_ref.split("/", 1)[1] if "/" in head_ref else ""

        if not default_branch:
            for candidate in ("main", "master"):
                has_branch = run(
                    ["git", "show-ref", "--verify", f"refs/remotes/origin/{candidate}"],
                    cwd=dest,
                    check=False,
                )
                if has_branch.returncode == 0:
                    default_branch = candidate
                    break

        if not default_branch:
            raise RuntimeError(
                "Could not determine remote default branch for existing clone."
            )

        run(["git", "checkout", "-B", default_branch, f"origin/{default_branch}"], cwd=dest)
        return dest
    run(["git", "clone", repo_url, str(dest)])
    return dest


def list_open_issues(slug: str, limit: int = 10) -> list[Issue]:
    ensure_gh_installed()
    out = run(
        _gh_command(
            [
                "issue",
                "list",
                "--repo",
                slug,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,url",
            ]
        )
    ).stdout
    raw = json.loads(out)
    issues: list[Issue] = []
    for item in raw:
        issues.append(
            Issue(
                number=item["number"],
                title=item["title"],
                body="",
                url=item["url"],
            )
        )
    return issues


def get_issue(slug: str, number: int) -> Issue:
    out = run(
        _gh_command(
            [
                "issue",
                "view",
                str(number),
                "--repo",
                slug,
                "--json",
                "number,title,body,url",
            ]
        )
    ).stdout
    item = json.loads(out)
    return Issue(
        number=item["number"],
        title=item["title"],
        body=item.get("body", ""),
        url=item["url"],
    )


def create_branch(repo_dir: Path, issue_number: int) -> str:
    branch = f"autofix/issue-{issue_number}"
    run(["git", "checkout", "-B", branch], cwd=repo_dir)
    return branch


def commit_all(repo_dir: Path, message: str) -> None:
    run(["git", "add", "-A"], cwd=repo_dir)
    status = run(["git", "status", "--porcelain"], cwd=repo_dir).stdout.strip()
    if not status:
        raise RuntimeError("No changes were produced by the autofix run.")
    run(["git", "commit", "-m", message], cwd=repo_dir)


def push_branch(repo_dir: Path, branch: str) -> None:
    run(["git", "push", "-u", "origin", branch], cwd=repo_dir)


def create_pr(slug: str, title: str, body: str, head_branch: str | None = None) -> str:
    cmd = [
        "pr",
        "create",
        "--repo",
        slug,
        "--title",
        title,
        "--body",
        body,
    ]
    if head_branch:
        cmd.extend(["--head", head_branch])
    out = run(_gh_command(cmd)).stdout.strip()
    return out
