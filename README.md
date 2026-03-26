# cursor_hackathon

Backend-only CLI that:

1. Takes a GitHub repo URL
2. Finds an issue
3. Attempts an automated code fix
4. Commits changes and opens a PR

## Requirements

- Python 3.10+
- `git`
- `gh` (GitHub CLI), authenticated (`gh auth login`)
- `OPENAI_API_KEY` in `.env` or exported in shell
- Optional: `AUTOFIX_GH_BIN` if GitHub CLI is installed under a different command/path

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Then edit `.env` and set `OPENAI_API_KEY`.

## Usage

```bash
autofix doctor
autofix run https://github.com/owner/repo
```

Optional flags:

- `--issue 123` pick a specific issue
- `--workspace .autofix-workspace` clone location
- `--no-create-pr` run fix + commit only

## Notes

- The default behavior selects the first open issue from `gh issue list`.
- Patch generation is done through OpenAI and applied as a git diff.
- Validation is best-effort and auto-detects simple test commands.
- Docker is optional. Use it only if you want environment isolation/reproducibility.