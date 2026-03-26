from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
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
    if not shutil.which("gh"):
        raise RuntimeError("GitHub CLI (gh) is required but was not found in PATH.")


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
        run(["git", "pull"], cwd=dest)
        return dest
    run(["git", "clone", repo_url, str(dest)])
    return dest


def list_open_issues(slug: str, limit: int = 10) -> list[Issue]:
    ensure_gh_installed()
    out = run(
        [
            "gh",
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
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            slug,
            "--json",
            "number,title,body,url",
        ]
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


def create_pr(slug: str, title: str, body: str) -> str:
    out = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            slug,
            "--title",
            title,
            "--body",
            body,
        ]
    ).stdout.strip()
    return out
