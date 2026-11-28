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
  - **Gemini Free Tier (OpenAI-compatible):** Set `LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/`, `LLM_API_KEY=<AI Studio key>`, `LLM_MODEL=gemini-flash-lite-latest` *(note: `gemini-flash-latest` has a 20 RPD free quota limit and `gemini-2.5-flash` may return 404 on new keys)*. Send only synthetic demo data.
  - **Local Ollama:** Set `LLM_BASE_URL=http://localhost:11434/v1/`, `LLM_MODEL=<pulled model>`, `LLM_API_KEY=ollama`.
  - **Rules-Only Mode (`--no-llm`):** Full end-to-end deterministic pipeline without calling any model; automatically used on API failure (`rules-only (LLM unavailable)`) or schema rejection (`rules-only (LLM output rejected)`).

---

## 6. Safety Design
- **Executor Allowlists (`executor.py`):** Accepts only deterministic operations from `planner.py`, permits writes only to `writable: true` environments (`production`), restricts paths to `environments/<env>/featurestates/`, forbids `DELETE`, and aborts (`exit 5`) if current Flagsmith state differs from the planned precondition.
- **Prompt-Injection Defense (`ranking/prompts.py`, `ranking/schema.py`):** Evidence JSON is framed inside `<UNTRUSTED_EVIDENCE_JSON>` tags. The LLM receives no tools. Outputs failing candidate-ID membership, verdict consistency, string bounds (`<= 300` chars), or injection checks get 1 repair retry before falling back to deterministic rules.
- **Redacted Audit Trail (`.flagrewind/audit.jsonl`):** Records every investigation, SHA-256 prompt/response hashes, token counts, approvals/rejections, executions, and undos with keys redacted.

---

## 7. Benchmark Method, Verification & Results (SYNTHETIC + LIVE FLAGSMITH CLOUD)
> **SYNTHETIC DATA NOTICE:** All scenarios (`drills/scenarios.yaml`, `drills/heldout.yaml`) and service faults (`docs/fault-model.md`) use synthetic traffic generated by `demo_service/traffic.py`. Held-out scenarios (`H01`–`H04`) were committed and tagged `drills-heldout-v1` (`8f35022`, `2026-11-26 14:17 +0530`) **before** both the live Cloud suite and the live LLM ablation, with zero post-hoc rule tuning.
> **Verification & CI Status:** Local `ruff check .` and `pytest` outputs are recorded by `scripts/verify.ps1` in [`docs/verification/latest.md`](docs/verification/latest.md) (**local run; GitHub Actions blocked by an account billing hold**).

All recorded runs are listed explicitly below with their exact sample sizes and measured endpoints:

* **Run 1 — Live Flagsmith Cloud Single E2E Proof (`n=1`, Scenario `S01`, `drills/results/live_e2e_proof.json`):**
  * Mode: **advisory** live Gemini (`gemini-flash-lite-latest`, re-ranking only) + deterministic `planner.py` inverse `PUT` + live Flagsmith Cloud + live `demo_service` (`:8001`).
  * Signed webhook HMAC-SHA256 verification proved in loopback (`401` rejected / `200` accepted) + Cloud Free-tier diff capture via `AuditPoller.poll_state_diffs`.
  * Measured (`n=1`): `12.406s` alert -> plan ready, **`31.859s` (`31.9s`) alert -> revert applied** (automated approval script; interactive `flagrewind apply` adds operator reading/typing time), and `81.046s` alert -> `3` consecutive `<5%` recovery windows (`0` unapproved writes).
* **Run 2 — Live Flagsmith Cloud Suite (`n=14`: 10 Core + 4 Held-Out, `rules-only`, `drills/results/live-20261127T022911Z/score_summary.json`):** Executed against Flagsmith Cloud (`https://api.flagsmith.com/api/v1/`) + local-eval SDK; measures alert -> Admin API revert/advisory decision after propagation (without 3-window recovery waits):
  * **Core 10 (`S01`–`S10`):** `top1_accuracy = 1.0`, `abstention_accuracy = 1.0` (`2/2`), `action_correctness = 1.0`, `0` unapproved writes (`1.8s` median alert -> revert/advisory; note `S10` `segment_rule` produces a manual advisory option with `reverted=False`).
  * **Held-Out 4 (`H01`–`H04`, frozen at `drills-heldout-v1`):** `top1_accuracy = 1.0`, `abstention_accuracy = 1.0` (`H02` non-flag memory fault), `action_correctness = 1.0`, `0` unapproved writes (`2.0s` median).
  * **Combined 14:** `14/14` top-1, `14/14` action correctness, `3/3` abstention, `1.8s` median, `0` unapproved writes.
* **Run 3 — Live Gemini (`gemini-flash-lite-latest`) vs. Rules-Only Ablation (`n=14` Synthetic Scenarios, `drills/results/llm_vs_rules_synthetic.json`):**
  * **The LLM is advisory only:** it holds zero Flagsmith API tools, never builds write payloads, and can never bypass typed human approval; held-out scenarios (`H01`–`H04`) were frozen at tag `drills-heldout-v1` before running this ablation.
  * `rules-only` (`n=14`, planning latency only): `top1_accuracy = 1.0` (`14/14`), `abstention_accuracy = 1.0` (`3/3`), `action_correctness = 1.0` (`14/14`), `0` unapproved writes.
  * `llm (gemini-flash-lite-latest)` (`n=14`, `min_interval_s=4.5`, planning latency only): **`14/14` top-1 (`1.0`), `3/3` abstention (`1.0`), `11/14` exact action (`0.7857`; proposed `disable_for_segment` instead of `revert_change` on `S04`, `S05`, `H01`), `0` fallbacks** (`mean_calls = 1`, `mean_tokens = 1397.5`, median ranking latency `4.11s`, `0` unapproved writes).
* **Run 4 — Automated Offline Suite (`pytest` + `ruff`):** Verifiable locally via `.\scripts\verify.ps1` -> [`docs/verification/latest.md`](docs/verification/latest.md) (**local run; GitHub Actions blocked by an account billing hold**).
* **Read-Only Public Showcase (`deploy/cloudflare/`):** Interactive incident simulator, typed-approval preview, and live Cloud benchmark explorer deployed on Cloudflare Workers (zero write keys exposed): **<https://flagrewind.shashankshashi831431.workers.dev>** (`/health`, `/api/proof`, `/api/scenarios`, `/api/ablation`, `/api/runs`).
* **"Saves Real Time" Same-Endpoint Comparison (`n=3` Manual Baseline vs. `n=1` Live Run):**
  * Both sides must share the identical endpoint: **`alert (>5% breach) -> revert applied to Flagsmith`**.
  * **FlagRewind live Cloud run (`n=1`, `S01`):** `31.859s` (`31.9s`) alert -> revert applied under automated confirmation (`12.406s` alert -> plan, excluding human reading/typing pause).
  * **Manual UI baseline (`B1`, `n=3`, indicative only):** Timed via `python -m drills.manual_stopwatch --count 3 --seed 42` (`drills/results/manual_baseline_n3.json`). **Status: Pending operator recording** — no manual timing (`~178s` or otherwise) is claimed because `manual_baseline_n3.json` has not been recorded yet. Full breakdown in [`docs/benchmark.md`](docs/benchmark.md).

---

## 8. Limitations, License & Demo
- **Limitations:** Synthetic fault model in one demo service; self-hosted Flagsmith organisation keys are broad (safety rests on `executor.py` allowlists and dedicated demo org); live LLM latency/quotas depend on chosen free-tier provider.
- **License & Attribution:** Licensed under **Apache-2.0** (`LICENSE`). `docker/docker-compose.yml` is derived from Flagsmith's `v2.295.2` `docker-compose.yml` (BSD-3-Clause).
- **Demo Video:** *(Link to be added after recording `DEMO_SCRIPT.md`.)*
