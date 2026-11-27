# FlagRewind Test & Verification Report

**Generated:** `2026-11-27` · **Mode:** Local verification (`scripts/verify.ps1` -> `docs/verification/latest.md`: **local run; GitHub Actions blocked by an account billing hold**) + live Flagsmith Cloud + live advisory Gemini (`gemini-flash-lite-latest`) verified.

---

## 1. Automated & Live Verification Ledger (Observed Results)

| Check | Command | Result | Scope |
| --- | --- | --- | --- |
| Local Verify Script (Ruff + Pytest) | `powershell -ExecutionPolicy Bypass -File scripts/verify.ps1` | **PASS** (`docs/verification/latest.md`: **local run; GitHub Actions blocked by an account billing hold**) | Runs `ruff check .` (`All checks passed!`) and `pytest --tb=short` (`33 passed`) and saves raw output |
| Live Flagsmith Cloud Doctor | `.venv\Scripts\flagrewind.exe doctor` | **PASS** (`exit 0`) | Live Cloud org (`1 org`), `production` & `staging` envs, audit read, write-scope dry-run, live `gemini-flash-lite-latest` schema check |
| Live End-to-End Closed-Loop Proof (`n=1`, `S01`) | `.venv\Scripts\python.exe -m drills.live_drill` | **PASS** (`exit 0`, `drills/results/live_e2e_proof.json`) | **`n=1`**: Live Cloud fault injection via drill key, `AuditPoller` diff capture, advisory Gemini ranking, `12.406s` alert->plan, `31.859s` alert->revert applied (simulated phrase), `81.046s` `3` healthy recovery windows, loopback HMAC webhook proof (`401`/`200`), `0` unapproved writes |
| Live Flagsmith Cloud 14-Scenario Suite (`n=14`) | `.venv\Scripts\python.exe -m drills.live_suite_runner` | **PASS** (`drills/results/live-20261127T022911Z/score_summary.json`) | `10` core (`10/10` top-1, `10/10` action, `1.8s` median) + `4` held-out (`H01`–`H04`, frozen at `drills-heldout-v1` prior to run: `4/4` top-1, `4/4` action, `2.0s` median), `1.0` abstention, `0` unapproved writes (`S10` `segment_rule` manual advisory only) |
| Live Gemini vs. Rules Ablation (`n=14`, synthetic) | `.venv\Scripts\python.exe -m drills.ablation_live` | **PASS** (`drills/results/llm_vs_rules_synthetic.json`) | **LLM is advisory only**; held-out scenarios frozen at `drills-heldout-v1` before ablation. `gemini-flash-lite-latest` completed `14/14` with `0` fallbacks (`14/14` top-1 = `1.0`, `3/3` abstention = `1.0`, **`11/14` action correctness = `0.7857`** (chose `disable_for_segment` on `S04`, `S05`, `H01`), `mean_tokens=1397.5`, `4.11s` median latency, `0` unapproved writes) |
| Docker Compose Validation | `docker compose -f docker/docker-compose.yml config --quiet` | **PASS** (`exit 0`) | Validates pinned `v2.295.2` Flagsmith compose file |
| Drill Runner Dry Run | `.venv\Scripts\python.exe -m drills.runner --dry-run --seed 42` | **PASS** (`exit 0`) | Verified seeded permutation (`S08, S04, S03, S09, S06, S07, S10, S05, S01, S02`) |
| Manual Triage Baseline (`B1`, `n=3`, indicative only) | `.venv\Scripts\python.exe -m drills.manual_stopwatch --count 3 --seed 42` | **Pending human run** (`drills/manual_stopwatch.py`) | Same endpoint (`alert -> revert applied`) in Flagsmith UI (`drills/results/manual_baseline_n3.json`); no unrecorded timing claimed |

---

## 2. P0 / Track 02 Acceptance Criteria Matrix

| Criterion | Status | Evidence |
| --- | --- | --- |
| Strict `flagrewind.yaml` validation (`extra="forbid"`, exit 2) | **PASS** | `tests/unit/test_config.py`, `tests/unit/test_cli_skeleton.py` |
| Synthetic Acme Checkout deterministic faults & 50-error cap | **PASS** | `tests/unit/test_demo_service.py` |
| HMAC-SHA256 webhook verification, audit poller & snapshot-diff fallback | **PASS** | `tests/unit/test_journal_evidence.py`, `drills/results/live_e2e_proof.json` |
| Staging-only (`env=0`) & time-window filtering (`[onset-60m, onset+5s]`) | **PASS** | `tests/unit/test_journal_evidence.py`, `tests/unit/test_drills.py` |
| Zero unapproved writes & key redaction | **PASS** | `tests/unit/test_safety_no_unapproved_writes.py`, `tests/unit/test_core_mitigation.py` |
| Precondition drift aborts with exit `5` | **PASS** | `tests/unit/test_core_mitigation.py` |
| Hallucinated IDs, bad confidence, inconsistent verdict, and prompt injections rejected + repair retry + fallback | **PASS** | `tests/unit/test_llm_ranking.py` |
| End-to-end `watch` -> `apply` -> `verify` -> `undo` + `exit 3` on Flagsmith auth failure | **PASS (mocked HTTP + live Cloud E2E)** | `tests/integration/test_mitigation_integration.py`, `drills/results/live_e2e_proof.json` |
| 10-scenario synthetic suite (`rules-only`: `1.0` accuracy, `0` unapproved writes) | **PASS (offline synthetic)** | `tests/unit/test_drills.py` |
| Live Flagsmith Cloud bootstrap, project reset, and 14-scenario benchmark (10 core + 4 pre-tagged held-out) | **PASS (live Flagsmith Cloud)** | `scripts/bootstrap.py`, `drills/live_suite_runner.py`, `drills/results/live-20261127T022911Z/score_summary.json` |
| Live Gemini paced ablation (14 scenarios, zero fallbacks) | **PASS (synthetic + live Gemini)** | `drills/ablation_live.py`, `drills/results/llm_vs_rules_synthetic.json` |
| B1 manual UI triage baseline (`n=3`, same endpoint) | **Pending human run** | `drills/manual_stopwatch.py` (awaiting operator interactive session) |
| Fresh-clone end-to-end demo & video recording | **Ready for video** | Follow `DEMO_SCRIPT.md` (<= 3 min live run + 60-word voiceover) |
