# FlagRewind — Progress Log

## Current Status
- **Completed Modules:** Modules 0–9 (Full FlagRewind P0 + P1 codebase, Acme Checkout demo service, pinned Flagsmith Docker stack & bootstrap, 10-scenario synthetic drill suite, CI workflows, and documentation)
- **Target Workspace:** `d:\users\Shashank J\Desktop\track 2\flagrewind`
- **GitHub Scope:** `Shashankcodelover` (configured `--local` inside `flagrewind` only)

## Milestone Summary
- **Module 1 (`chore/init-foundation`):** Repo setup, Apache-2.0 `LICENSE`, `.env.example`, `pyproject.toml`, strict `config.py`, `cli.py`, isolated `.venv`.
- **Module 2 (`feat/flagsmith-stack`):** Pinned `docker/docker-compose.yml` (`v2.295.2`, `8002:8000` for task-processor so `demo_service` keeps `8001`), idempotent `scripts/bootstrap.py`, `docs/fault-model.md`, `docs/autonomy-contract.md`.
- **Module 3 (`feat/demo-service`):** FastAPI `"Acme Checkout"` (`demo_service/{app.py, faults.py, traffic.py}`).
- **Module 4 (`feat/journal-evidence`):** `flagsmith_client.py`, HMAC webhook receiver, audit poller with gap detection, symptom reader, AST `codeindex.py`, `evidence.py`.
- **Module 5 (`feat/core-mitigation-loop`):** `rules.py`, `planner.py`, `approval.py`, `executor.py` (`exit 5` on drift, allowlists, never `DELETE`), `verifier.py`, `report.py`, `audit.py`, `capabilities.py` (`doctor`), safety test (`0` unapproved writes).
- **Module 6 (`feat/llm-ranking`):** `llm_client.py`, untrusted-data `prompts.py`, validated `ranking/llm.py` with 1 repair retry and labelled fallbacks.
- **Module 7 (`feat/watch-undo-mcp`):** `journal`, `watch`, `undo`, next-suspect flow, P1 read-only `mcp_reader.py`, `notifier.py`, integration suite.
- **Module 8 (`feat/drills-benchmark`):** Frozen 10-scenario `drills/scenarios.yaml`, isolated `answers/ground_truth.json`, `runner.py`, `score.py`, `docs/benchmark.md`.
- **Module 9 (`docs/ci-and-release`):** GitHub Actions CI (pinned actions, least privilege, Gitleaks, CodeQL), PR/issue templates, `README.md`, `WALKTHROUGH.md`, `DEMO_SCRIPT.md`, `TEST_REPORT.md`, ADR-0001.

## Remaining Human-Only Actions (For Live M1/M2/M5/M6 & Submission)
1. Start **Docker Desktop** on Windows and run `docker compose -f docker/docker-compose.yml up -d`.
2. Open `http://localhost:8000`, complete Flagsmith `"Get Started"`, create organisation `"FlagRewind Demo"`, and create two Org API keys (`FLAGREWIND_ADMIN_KEY` and `FLAGREWIND_DRILL_KEY`).
3. Create a free Google AI Studio key (`LLM_API_KEY`) or install Ollama locally (`LLM_MODEL`).
4. Run `python scripts/bootstrap.py` and copy `.env.example` -> `.env` with the keys.
5. Run live `10 x 3` drills + timed B1 manual baseline (`docs/benchmark.md`), approve creating public repo `Shashankcodelover/flagrewind` & `git push`, record <= 3 min video (`DEMO_SCRIPT.md`), and submit before `29 Nov 2026, 20:00 IST`.
