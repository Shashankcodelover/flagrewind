# FlagRewind Benchmark & Manual Baseline Runbook (SYNTHETIC)

> **SYNTHETIC DATA NOTICE:** All drills and benchmark timings measure synthetic incidents in the local "Acme Checkout" service. Raw runs (including failures) must be published alongside means and medians.

## 1. Baselines
- **B1 — Manual Runbook:** An engineer opens Flagsmith (`http://localhost:8000`) audit log + `http://localhost:8001/metrics` + `/errors`, identifies the suspect manually, looks up the previous state, reverts it in the Flagsmith UI, and confirms recovery—timed with a stopwatch on hidden seeded scenarios (`n >= 4`, with a volunteer if available).
- **B2 — Rules-Only FlagRewind:** `flagrewind investigate --no-llm` + `flagrewind apply` (isolates deterministic scoring vs LLM ranking).
- **LLM Mode:** `flagrewind investigate` + `flagrewind apply` (Gemini free tier or local Ollama at `temperature=0`).

## 2. Manual Runbook Procedure (B1)
1. Start the synthetic traffic generator with a hidden seed (`python -m demo_service.traffic --seed <SEED>`) and have a second script/helper trigger one randomized scenario from `drills/scenarios.yaml` without showing `drills/answers/ground_truth.json`.
2. Start the stopwatch when `http://localhost:8001/metrics` error rate crosses `5%`.
3. Inspect `/errors` and `/metrics` by country/plan/endpoint, then scroll Flagsmith's Project Audit Log to locate candidate changes in the last 60 minutes.
4. Open the candidate feature/segment in Flagsmith, verify its previous value, apply the manual inverse, and wait until `/metrics` stays below `5%` for 3 consecutive 10s windows.
5. Stop the timer and record `scenario_id`, `time_to_mitigation_s`, `time_to_recovery_s`, and whether the first guess was right.

## 3. Live Benchmark Results (Flagsmith Cloud Free Tier & Paced LLM Ablation)

> **Execution Environment:** Flagsmith Cloud (`https://api.flagsmith.com/api/v1/`, Free tier) + local FastAPI `demo_service` (`Flagsmith(enable_local_evaluation=True)`).  
> **Held-Out Freeze Disclosure:** `drills/heldout.yaml` (`H01`–`H04`) and `drills/answers/ground_truth_heldout.json` were committed and tagged `drills-heldout-v1` at `8f35022` (`2026-11-26 14:17:06 +0530`) **before** both the live Cloud suite and the live LLM ablation. No rules in `src/flagrewind/evidence.py` or `src/flagrewind/ranking/rules.py` were tuned after the held-out freeze.  
> **Audit Log Behavior Note:** Flagsmith Cloud Free tier returns `count: 0` on `/audit/`; live runs capture every flag, segment override, segment rule, and multivariate diff via Admin API state-snapshot diff polling (`AuditPoller.poll_state_diffs`). Signed HMAC-SHA256 webhook verification was additionally proved in loopback (`drills/results/live_e2e_proof.json`: `401` invalid signature, `200` valid signature, `1` journal event recorded).  
> **CI / Local Verification Status:** Automated lint/unit checks are captured in [`docs/verification/latest.md`](verification/latest.md) via `scripts/verify.ps1` (**local run; GitHub Actions blocked by an account billing hold**).

### A. Single Live End-to-End Closed-Loop Proof (`n=1`, Scenario `S01`, Live Flagsmith Cloud)
From `drills/results/live_e2e_proof.json` (`python drills/live_drill.py`):
- **Sample Size & Target Scenario:** **`n=1`**, `S01` (`tax_engine_v2` enabled for `eu-customers`, ranked by advisory live `gemini-flash-lite-latest` with deterministic inverse planning)
- **Alert -> Plan Ready:** **12.406s** (continuous `AuditPoller` + `/metrics` & `/errors` analysis + live Gemini advisory ranking + deterministic inverse `PUT` plan)
- **Alert -> Revert Applied (Automated Simulated Approval):** **31.859s** (`31.9s`, `n=1`; includes live Cloud Admin API `PUT` revert; confirmation phrase was injected programmatically by the script without human reading/typing delay)
- **Alert -> 3 Healthy Recovery Windows:** **81.046s** (`state_verified=True`, `recovered=True`, `consecutive_healthy_windows=3`, observed rates: `0.0831 -> 0.0968 -> 0.07 -> 0.048 -> 0.0 -> 0.0`)
- **Unapproved Writes:** **0**

### B. 14 Scenarios Live on Flagsmith Cloud (`n=14`, `rules-only`: 10 Core + 4 Held-Out)
From `drills/results/live-20261127T022911Z/score_summary.json` (`python -m drills.live_suite_runner`):

| Slice | Scenarios | Top-1 Acc. | Top-3 Acc. | Abstention Acc. | Action Correctness | Median Alert -> Revert/Decision | Unapproved Writes |
|---|---|---|---|---|---|---|---|
| Core 10 (Live Cloud) | `S01`–`S10` | **1.0** (`10/10`) | **1.0** | **1.0** (`2/2`) | **1.0** (`10/10`) | **1.8s** | **0** |
| Held-Out 4 (`drills-heldout-v1`, Live Cloud) | `H01`–`H04` | **1.0** (`4/4`) | **1.0** | **1.0** (`1/1`) | **1.0** (`4/4`) | **2.0s** | **0** |
| Combined 14 (Live Cloud) | `S01`–`H04` | **1.0** (`14/14`) | **1.0** | **1.0** (`3/3`) | **1.0** (`14/14`) | **1.8s** | **0** |

Honest caveats from the raw live suite output:
- `S06` (`staging`-only decoy) produced `0.0%` production error rate (`breached=False`), so FlagRewind returned `not_flag_related` / `none` (`reverted=False`).
- `S10` (`eu-customers` `segment_rule` widening) correctly identified `eu-customers` top-1 with `revert_change`, and generated an **advisory** (manual) option (`reverted=False`, `0.016s`) because `executor.py` does not auto-mutate segment rules.
- `drills/results/live-20261127T022911Z/score_summary.json` measures alert -> Admin API revert (or abstention/advisory decision) after fault propagation, without adding separate per-scenario recovery windows (`live_drill.py` proved the full `81.046s` 3-window recovery loop on `S01`).

### C. Paced Live LLM Ablation Across All 14 Scenarios (`n=14`, `gemini-flash-lite-latest`, SYNTHETIC)
From `drills/results/llm_vs_rules_synthetic.json` (`python -m drills.ablation_live`, `min_interval_s=4.5`, Google AI Studio free tier):
- **Role & Freeze Disclosure:** **The LLM is advisory only** — it receives read-only redacted evidence, re-ranks candidates, and suggests an allowlisted action label (`revert_change`, `disable_for_segment`, or `none`), with zero Flagsmith API tools and zero authority over writes (`planner.py` constructs deterministic payloads and `approval.py` blocks unapproved writes). The held-out scenarios (`H01`–`H04`) were committed and frozen at tag **`drills-heldout-v1`** (`8f35022`) **before** running this ablation.
- *(Quota note: `gemini-flash-latest` hit its 20 RPD free-tier quota limit and `gemini-2.5-flash` returned HTTP 404 for new keys; re-running with `gemini-flash-lite-latest` completed all 14 scenarios with zero fallbacks.)*

| Mode | Runs | Top-1 Acc. | Top-3 Acc. | Abstention Acc. | Action Correctness | Median Ranking Latency | Mean Calls | Mean Tokens | Fallbacks Fired | Unapproved Writes |
|---|---|---|---|---|---|---|---|---|---|---|
| `rules-only` (synthetic) | 14 | **1.0** (`14/14`) | **1.0** | **1.0** (`3/3`) | **1.0** (`14/14`) | **0.0s** | `0` | `—` | `0` | **0** |
| `llm (gemini-flash-lite-latest)` | 14 | **1.0** (`14/14`) | **1.0** | **1.0** (`3/3`) | **0.7857** (`11/14`) | **4.11s** | `1` | `1397.5` | `0 / 14` | **0** |

Honest breakdown of the 3 `action_correctness` misses (`11/14`):
- On `S04` (`tax_engine_v2` global rollout with DE/FR failures), `S05` (`tax_engine_v2` multi-change with DE/FR failures), and `H01` (`profile_v2` global rollout with `free` plan failures), `gemini-flash-lite-latest` ranked the exact culprit `#1` (`top1_correct = True`), but recommended the allowlisted action `disable_for_segment` instead of full `revert_change`, while `ground_truth.json` / `ground_truth_heldout.json` expects `revert_change`. Every scenario completed cleanly with `calls=1`, `fallback_fired=False`, and `0` unapproved writes (`917`–`2420` tokens per scenario, `2.954s`–`5.672s` latency).

### D. Same-Endpoint Speed Comparison (`n=3` Manual Baseline vs. `n=1` Live Run) & Complete Run Ledger
Both sides **must** use the identical start and end points: **alert breach detected (`>5%`) -> revert applied to Flagsmith** (including human review and typed confirmation on FlagRewind's interactive path, not automated planning latency alone).

- **Manual UI Baseline (`B1`, `n=3`, indicative only):** Run blindly with `python -m drills.manual_stopwatch --count 3 --seed 42` (`drills/results/manual_baseline_n3.json`). **Status: Pending operator execution** — no baseline timing (`~178s` or otherwise) is recorded or claimed in this repository because `manual_baseline_n3.json` has not been recorded yet.
- **FlagRewind Live Cloud E2E Proof (`n=1`, `S01`):**
  - Alert -> Plan Ready (Cloud diff polling + advisory LLM call + deterministic plan): **12.406s**
  - Alert -> Revert Applied (automated confirmation script, excluding human reading/typing pause): **31.859s (`31.9s`)** (`flagrewind apply` human typed phrase adds operator reading/typing time in interactive runs)
  - Alert -> Verified Recovery (`3` healthy 10s windows): **81.046s**

**Complete Inventory of Executed & Defined Runs:**

| Run | Sample Size | Environment & Endpoint Measured | Primary Source File |
|---|---|---|---|
| Live Cloud E2E (`S01`) | **`n=1`** | Live Flagsmith Cloud + `:8001` service; alert->plan (`12.406s`), alert->revert applied (`31.859s` automated), alert->recovery (`81.046s`) | `drills/results/live_e2e_proof.json` |
| Live Cloud 14-Scenario Suite | **`n=14`** (`10` core + `4` held-out) | Live Flagsmith Cloud + local-eval SDK; `rules-only`; alert->revert/advisory decision post-propagation (`1.8s` median Core, `2.0s` Held-out) | `drills/results/live-20261127T022911Z/score_summary.json` |
| Live Gemini vs. Rules Ablation | **`n=14`** (`28` total runs across 2 modes) | Synthetic journal/symptoms + live `gemini-flash-lite-latest` (**advisory only**); ranking/planning latency only (`4.11s` LLM median, `0.0s` rules) | `drills/results/llm_vs_rules_synthetic.json` |
| Local Lint & Automated Test Suite | `33` pytest cases + `ruff` | Local repo verification via `scripts/verify.ps1` (**local run; GitHub Actions blocked by an account billing hold**) | `docs/verification/latest.md` |
| Manual UI Baseline (`B1`) | **`n=3`** (target, indicative only) | Blind interactive stopwatch (`alert -> revert applied` in Flagsmith UI) — **Pending human run** | `drills/manual_stopwatch.py` |

## 4. Known Limitations & Bias Disclosure
- All scenarios were constructed around the synthetic Acme Checkout fault model (`docs/fault-model.md`).
- Even with seed-randomized timings and hidden ground-truth files, familiarity with the demo service creates author advantage in `B1`; report `n=3` strictly as **indicative only**.
- Live Cloud runs executed against Flagsmith Cloud Free tier (`https://api.flagsmith.com/api/v1/`), where CDN `/environment-document/` propagation takes ~30–40s per mutation and Free tier audit endpoints return `0` entries (covered via Admin API state-snapshot diff polling).
