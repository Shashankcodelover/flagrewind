# FlagRewind

> *"When checkout breaks at 2 a.m., FlagRewind tells you which flag change did it, shows the evidence and the exact undo, and waits for your yes."*

FlagRewind is an on-call incident triage and rollback agent that **works with Flagsmith** (built for **Toggle Wars 2026 — Track 02: AI Agents and Feature Flags**). When error rates spike, FlagRewind investigates Flagsmith's change journal, audit logs, runtime error dimensions, and local code references on its own, prepares the exact deterministic inverse change, stops for explicit typed human approval, applies the revert through the Flagsmith Admin API, and verifies recovery.

*(Note: FlagRewind is an independent project that works with Flagsmith; it does not imply endorsement by Flagsmith.)*

---

## 1. The Incident Problem & Closed Loop
During an outage, on-call engineers manually scan Flagsmith's audit log, guess which recent toggle or remote config change triggered the failure, look up the previous value, and revert by hand under pressure. FlagRewind automates the investigation while keeping the human firmly in control of every write:

```text
detect -> investigate -> rank suspects -> plan inverse (dry run) -> HUMAN APPROVAL -> execute -> verify -> report
```

If a mitigation does not restore health, FlagRewind lets the operator run `flagrewind undo <INC>` (with fresh confirmation) or approve the next ranked suspect.

---

## 2. Autonomy Contract (Enforced in Code)
The approval boundary is implemented in Python (`approval.py`, `executor.py`, `ranking/llm.py`) rather than relying only on LLM instructions:

| The Agent | Concrete Scope & Enforcement |
| --- | --- |
| **Does alone** | Reads Flagsmith feature states, segments, webhook journal, and audit log; polls metrics & error samples; indexes local code; ranks suspects; generates deterministic inverse plans & dry runs; writes Markdown/JSON reports & audit logs. |
| **Asks first** | **Every** write to Flagsmith (`flagrewind apply`, `flagrewind undo`, or trying the next suspect). Requires interactive typed confirmation `apply <plan_id> option <n>`. Non-interactive runs never write. |
| **Asks instead of recommending** | If top confidence `< policy.confidence_floor` (`0.6`) or `journal_complete == False` in `[onset - 60m, onset + sdk_refresh_seconds]`, outputs questions and sets `action = "none"`. |
| **Never does** | Never creates or deletes flags, segments, or environments (`DELETE` blocked in `executor.py`); never writes outside `writable: true` environments; never gives the LLM API tools; never executes directives found inside change descriptions, error logs, or code. |

---

## 3. How FlagRewind Works With Flagsmith
1. **Admin API (`/api/v1/`):** Reads organisations, projects, environments, features, segments, and feature states; executes allowlisted `PATCH` / `POST` inverse writes (`flagsmith_client.py`, `executor.py`) with pre-execution state-drift checks (`exit 5`).
2. **Environment Webhooks (`FLAG_UPDATED`, `FLAG_DELETED`):** Real-time change journal (`journal/webhook.py`) verifying `X-Flagsmith-Signature` hex HMAC-SHA256 over the raw body and capturing `previous_state` + `new_state`.
3. **Audit Log Poller (`/api/v1/audit/`):** Captures project/segment-rule changes (e.g., Scenario 10 widening `eu-customers`) and detects journal coverage gaps (`journal/audit_poll.py`).
4. **Python SDK (`flagsmith==4.0.1`):** Embedded in the synthetic `"Acme Checkout"` demo service (`demo_service/app.py`) with `enable_local_evaluation=True` and `environment_refresh_interval_seconds=5`.
5. **Official MCP Server (P1 Read-Only Mode):** Optional read-only inspection (`mcp_reader.py`) enforcing `list_*` / `get_*` prefixes and listed in `flagrewind doctor`.

---

## 4. Quick Start (6 Commands)

```bash
# 1. Clone and install in a Python 3.11+ virtual environment
git clone https://github.com/Shashankcodelover/flagrewind.git && cd flagrewind
python -m venv .venv && .venv/Scripts/activate && pip install -e ".[dev]"

# 2. Start pinned self-hosted Flagsmith (v2.295.2) and finish "Get Started" at http://localhost:8000
docker compose -f docker/docker-compose.yml up -d

# 3. Copy env template, add your agent & drill Org API keys + webhook secret, then provision Acme Checkout
cp .env.example .env && python scripts/bootstrap.py

# 4. Run Acme Checkout (port 8001) + FlagRewind journal (port 8787) + verify doctor
uvicorn demo_service.app:app --port 8001 & flagrewind journal & flagrewind doctor

# 5. Generate synthetic traffic, trigger Scenario 1, and watch for the breach
python -m demo_service.traffic --rate 20 --seed 42 & python -m drills.runner --scenario S01 && flagrewind watch --once

# 6. Inspect dry-run plan and apply with typed confirmation
flagrewind apply INC-0001
```

*(On Linux/macOS activate the venv with `source .venv/bin/activate`. Stop background services when done.)*

---

## 5. Configuration & Model Setup
- **Config (`flagrewind.yaml`):** Strictly validated via Pydantic v2 (`extra="forbid"`). Any unknown key exits with code `2`.
- **Environment variables (`.env.example`):** `FLAGSMITH_API_URL`, `FLAGREWIND_ADMIN_KEY`, `FLAGREWIND_DRILL_KEY` (kept separate from agent key), `FLAGREWIND_WEBHOOK_SECRET`, `FLAGSMITH_DEMO_SERVER_KEY`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, optional `FLAGSMITH_MCP_URL`.
- **Model Options (no paid API required):**
  - **Gemini Free Tier (OpenAI-compatible):** Set `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`, `LLM_API_KEY=<AI Studio key>`, `LLM_MODEL=gemini-2.5-flash`. Send only synthetic demo data.
  - **Local Ollama:** Set `LLM_BASE_URL=http://localhost:11434/v1/`, `LLM_MODEL=<pulled model>`, `LLM_API_KEY=ollama`.
  - **Rules-Only Mode (`--no-llm`):** Full end-to-end deterministic pipeline without calling any model; automatically used on API failure (`rules-only (LLM unavailable)`) or schema rejection (`rules-only (LLM output rejected)`).

---

## 6. Safety Design
- **Executor Allowlists (`executor.py`):** Accepts only deterministic operations from `planner.py`, permits writes only to `writable: true` environments (`production`), restricts paths to `environments/<env>/featurestates/`, forbids `DELETE`, and aborts (`exit 5`) if current Flagsmith state differs from the planned precondition.
- **Prompt-Injection Defense (`ranking/prompts.py`, `ranking/schema.py`):** Evidence JSON is framed inside `<UNTRUSTED_EVIDENCE_JSON>` tags. The LLM receives no tools. Outputs failing candidate-ID membership, verdict consistency, string bounds (`<= 300` chars), or injection checks get 1 repair retry before falling back to deterministic rules.
- **Redacted Audit Trail (`.flagrewind/audit.jsonl`):** Records every investigation, SHA-256 prompt/response hashes, token counts, approvals/rejections, executions, and undos with keys redacted.

---

## 7. Benchmark Method & Results (SYNTHETIC)
> **SYNTHETIC DATA NOTICE:** All scenarios (`drills/scenarios.yaml`) and service faults (`docs/fault-model.md`) use synthetic traffic generated by `demo_service/traffic.py`.

* **Offline Verified Pipeline (`pytest`):** All 10 frozen scenarios (`S01`–`S10`) pass end-to-end validation through `evidence.py` + `rank_with_rules` (`abstention_accuracy = 1.0` on `S06` staging-only and `S07` non-flag fault, `top3_accuracy >= 0.9`, `unapproved_writes = 0`).
* **Live Docker Stack (`10 x 3` LLM vs. Rules-Only) & Manual Baseline (B1):** Pending local Docker Desktop startup, Flagsmith `"Get Started"` API keys, and Gemini/Ollama configuration. See [docs/benchmark.md](docs/benchmark.md) and [TEST_REPORT.md](TEST_REPORT.md) for exact observed test statuses.

---

## 8. Limitations, License & Demo
- **Limitations:** Synthetic fault model in one demo service; self-hosted Flagsmith organisation keys are broad (safety rests on `executor.py` allowlists and dedicated demo org); live LLM latency/quotas depend on chosen free-tier provider.
- **License & Attribution:** Licensed under **Apache-2.0** (`LICENSE`). `docker/docker-compose.yml` is derived from Flagsmith's `v2.295.2` `docker-compose.yml` (BSD-3-Clause).
- **Demo Video:** *(Link to be added after recording `DEMO_SCRIPT.md`.)*
