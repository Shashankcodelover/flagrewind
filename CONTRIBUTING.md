# Contributing to FlagRewind

Thank you for helping improve FlagRewind!

## Local Development Setup
1. Create a virtual environment with Python 3.11+:
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # Linux/macOS: source .venv/bin/activate
   ```
2. Install package with dev dependencies:
   ```bash
   pip install -e ".[dev]"
   ```
3. Run lint and tests before opening a PR:
   ```bash
   ruff check .
   pytest
   ```

## Workflow Standards
- Work on a branch (`feat/<name>`, `fix/<name>`, `docs/<name>`) — never commit directly to `main`.
- Use Conventional Commits (`feat(planner): ...`, `fix(executor): ...`).
- Never commit `.env`, API keys, `.flagrewind/` runtime logs, or synthetic secrets.
- Every code path touching Flagsmith writes must pass `tests/unit/test_safety_no_unapproved_writes.py`.
