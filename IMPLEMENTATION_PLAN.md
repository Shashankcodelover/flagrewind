# FlagRewind — Implementation Plan

- **GOAL:** Deliver a fully tested, reproducible, human-approved Flagsmith incident triage and rollback agent (`FlagRewind`), synthetic demo service ("Acme Checkout"), 10-scenario benchmark suite, CI pipeline, and complete hackathon documentation inside `d:\users\Shashank J\Desktop\track 2\flagrewind` ready for Track 02 submission before 29 Nov 2026, 18:00 IST.
- **WHY:** Track 02 judges evaluate real Flagsmith usage (API/CLI/MCP), measurable time saved, explicit human approval, and clear output; FlagRewind fills an uncrowded incident-triage niche with deterministic safety guarantees.
- **SCOPE:**
  - *Included:* P0 and P1 Python CLI (`src/flagrewind/`), FastAPI synthetic demo service (`demo_service/`), pinned self-hosted Flagsmith Docker compose & idempotent bootstrap (`docker/`, `scripts/bootstrap.py`), 10-scenario drill runner & scorer (`drills/`), unit + integration tests (`tests/`), GitHub Actions CI, and documentation (`README.md`, `WALKTHROUGH.md`, `DEMO_SCRIPT.md`, `docs/*`).
  - *Excluded:* Paid APIs, agent frameworks, vector DBs, Feature Versioning v2 on P0 demo environments, creating public repos/pushing/submitting without explicit user approval, and P2 stretch items unless P0+P1 are green by Sat 28 Nov 13:00 IST.
- **ASSUMPTIONS:**
  - `[CONFIRM]` User will start Docker Desktop so self-hosted Flagsmith (`v2.29x`) can run locally on port `8000` (otherwise unit tests + `respx` integration suite cover offline verification while live Docker checks remain pending).
  - `[CONFIRM]` User will choose between creating a free Google AI Studio Gemini key (`LLM_API_KEY`), installing local Ollama, or building/verifying rules-only (`--no-llm`) first and wiring the LLM key at M3.
  - Python `3.11.9` (`uv` / `.venv`) runs locally inside `track 2` without needing external drive access.
- **RISKS:**
  - *Docker Desktop currently offline (`npipe:////./pipe/dockerDesktopLinuxEngine` not found):* delays live Flagsmith Swagger/webhook capture (M1/M2); mitigated by building testable modular core + `respx` mocks first and running live Flagsmith checks as soon as Docker starts.
  - *Upstream `flagsmith-task-processor` port collision (`8001:8000` vs `demo_service` `8001`):* mitigated by omitting host port `8001` binding on `flagsmith-task-processor` (or mapping `8002:8000`).
  - *No LLM key currently configured:* mitigated by rules-only mode (`--no-llm`), deterministic schema tests with mocked OpenAI responses, and late binding via env vars.
  - *Terminal TLS / sandbox restriction (`curl` `SEC_E_UNTRUSTED_ROOT`):* installing dependencies into `.venv` or running `docker` commands may require `BypassSandbox: true` approval when touching host network/named pipes.
  - *Dual GitHub account conflict on Windows:* global Git email belongs to another account; mitigated by strict `--local` Git config for `Shashankcodelover` inside `flagrewind` only.
- **APPROACH:**
  - *Chosen design:* Modular Python 3.11 package (`src/flagrewind/`) separating deterministic evidence/planning/execution/verification from an isolated OpenAI-compatible LLM ranker behind strict Pydantic v2 contracts and a typed human approval gate (`apply <plan_id> option <n>`).
  - *Rejected alternative:* Using an autonomous agent framework (LangGraph/CrewAI) giving the LLM direct Flagsmith API tool calls — rejected because it violates the brief ("no agent framework") and creates prompt-injection and unapproved-mutation risks.

## MODULES (Small, Independent, Testable, Reversible)

1. **Module 1: Repo Foundation, Strict Config & CLI Skeleton** (`chore/init-foundation`)
   - Initialize isolated git repo (`--local` `Shashankcodelover`), `.gitignore`, `.editorconfig`, `LICENSE` (Apache-2.0), `.env.example`, `pyproject.toml`, `flagrewind.yaml`, `config.py`, `cli.py` skeleton, `.venv` install, and config unit tests.
2. **Module 2: Docker Flagsmith Stack, Docs Contracts & Bootstrap** (`feat/flagsmith-stack` — M1)
   - Pinned `docker/docker-compose.yml` (avoiding `8001` collision), `scripts/bootstrap.py`, `docs/fault-model.md`, `docs/autonomy-contract.md`; verify live Swagger + Flagsmith bootstrap when Docker is running.
3. **Module 3: Demo Service ("Acme Checkout") & Fault Model** (`feat/demo-service` — M2a)
   - `demo_service/{app.py, faults.py, traffic.py}` on port `8001` using Flagsmith local evaluation (`5s` refresh), `/metrics`, `/errors`, deterministic faults + multivariate + non-flag fault switch, seedable traffic generator, and unit tests.
4. **Module 4: Flagsmith Client, Webhook/Audit Journal, Symptoms & Evidence** (`feat/journal-evidence` — M2b)
   - `flagsmith_client.py`, `journal/{store.py, webhook.py, audit_poll.py}` (HMAC-SHA256 + gap detection), `symptoms/demo.py`, `codeindex.py`, `evidence.py`, captured webhook fixture tests & unit tests.
5. **Module 5: Rules Ranking, Planner, Approval Gate, Executor, Verifier & Reports** (`feat/core-mitigation-loop` — M2c)
   - `ranking/{schema.py, rules.py}`, `planner.py`, `approval.py`, `executor.py` (allowlists, never DELETE, precondition exit 5), `verifier.py`, `report.py`, `audit.py`, `capabilities.py` (`doctor`), Rich 80-col/NO_COLOR brief, and safety tests proving zero unapproved writes.
6. **Module 6: LLM Ranker, Injection Defense & Repair/Fallback** (`feat/llm-ranking` — M3)
   - `llm_client.py`, `ranking/{prompts.py, llm.py}` with untrusted delimiters, candidate-ID/verdict/length validation, 1 repair retry, labelled fallback, and adversarial unit tests.
7. **Module 7: Watch Loop, Undo, Next-Suspect & P1 Features** (`feat/watch-undo-mcp` — M4/M8)
   - `watch` command, `undo` + next-suspect loop, `mcp_reader.py` (P1 read-only MCP + `doctor` tool listing), `disable_for_segment`, `--notify-url`, and integration suite (`respx` + live Docker when available).
8. **Module 8: 10-Scenario Drill Suite & Benchmark Harness** (`feat/drills-benchmark` — M5/M6)
   - `drills/{scenarios.yaml, runner.py, score.py, answers/}`, `docs/benchmark.md` manual baseline runbook, deterministic scorer tests, and live benchmark runs once stack + model are ready.
9. **Module 9: CI, Repo Hygiene, Docs & Final Audit** (`docs/ci-and-release` — M7/M9)
   - `.github/workflows/ci.yml` (pinned actions, minimal permissions, `ruff` + `pytest`), `.github/` templates & `dependabot.yml`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `README.md`, `WALKTHROUGH.md`, `DEMO_SCRIPT.md`, `TEST_REPORT.md`, final audit.

## ORDER AND DEPENDENCIES
- `Module 1 -> Module 2 (live boot needs Docker) & Module 3 (parallel-safe, depends only on Module 1)`
- `Module 1 + 3 -> Module 4 -> Module 5` (completes the rules-only vertical slice!)
- `Module 5 -> Module 6` (adds LLM layer over validated contracts)
- `Module 5 + 6 -> Module 7 -> Module 8 -> Module 9`
- Live end-to-end Docker verification (M1/M2/M5) runs as soon as Docker Desktop is active; live Gemini/Ollama benchmark runs as soon as a free model key/Ollama is configured.

## DONE CRITERIA
- Every module passes `ruff check`, `pytest` unit tests (no network), and checklist verification in `.agent/plan.md`.
- All 17 checklist gates in `TASK_BRIEF.md` §15 and §17 pass: live scenario 1 in both modes (when Docker + LLM ready), safety test (0 unapproved writes), drift abort (exit 5), prompt-injection/hallucination rejection, clean fresh-clone quick start, CI green, and complete docs.

## ESTIMATE
- **Module 1:** Small (~15 min) · Light (`pip`/`uv` install into `.venv`)
- **Module 2:** Medium (~25 min) · Heavy when pulling/booting Docker Flagsmith + Postgres containers (RAM monitored `<80%`)
- **Module 3:** Medium (~25 min) · Light (unit tests)
- **Module 4:** Medium (~30 min) · Light (`respx` HTTP mocks)
- **Module 5:** Large-split (~35 min) · Light (`pytest` + safety patch tests)
- **Module 6:** Small-Medium (~20 min) · Light (mocked OpenAI client + live smoke test when key exists)
- **Module 7:** Medium (~30 min) · Medium (integration test harness)
- **Module 8:** Medium (~30 min) · Medium-Heavy (10 scenarios live drill runs)
- **Module 9:** Small-Medium (~25 min) · Light (`ruff`, full test suite, fresh-clone check)
