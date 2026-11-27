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
> **Held-Out Freeze Disclosure:** `drills/heldout.yaml` (`H01`–`H04`) and `drills/answers/ground_truth_heldout.json` were committed and tagged `drills-heldout-v1` at `8f35022` (`2026-11-26 14:17:06 +0530`) **before** any live runs against Flagsmith Cloud. No rules in `src/flagrewind/evidence.py` or `src/flagrewind/ranking/rules.py` were tuned after running held-out scenarios.  
> **Audit Log Behavior Note:** Flagsmith Cloud Free tier returns `count: 0` on `/audit/`; live runs capture every flag, segment override, segment rule, and multivariate diff via Admin API state-snapshot diff polling (`AuditPoller.poll_state_diffs`). Signed HMAC-SHA256 webhook verification was additionally proved in loopback (`drills/results/live_e2e_proof.json`: `401` invalid signature, `200` valid signature, `1` journal event recorded).

### A. Single Live End-to-End Closed-Loop Proof (`S01`, Live Flagsmith Cloud)
From `drills/results/live_e2e_proof.json` (`python drills/live_drill.py`):
- **Target Scenario:** `S01` (`tax_engine_v2` enabled for `eu-customers`, ranked by live `gemini-flash-lite-latest`)
- **Alert -> Plan Ready:** **12.406s** (continuous `AuditPoller` + `/metrics` & `/errors` analysis + live Gemini ranking + deterministic inverse `PUT` plan)
- **Alert -> Revert Applied (Automated Simulated Approval):** **31.859s** (includes live Cloud Admin API `PUT` revert; confirmation phrase was injected programmatically by the script without human reading/typing delay)
- **Alert -> 3 Healthy Recovery Windows:** **81.046s** (`state_verified=True`, `recovered=True`, `consecutive_healthy_windows=3`, observed rates: `0.0831 -> 0.0968 -> 0.07 -> 0.048 -> 0.0 -> 0.0`)
- **Unapproved Writes:** **0**

### B. 14 Scenarios Live on Flagsmith Cloud (`rules-only`, 10 Core + 4 Held-Out)
From `drills/results/live-20261127T022911Z/score_summary.json` (`python -m drills.live_suite_runner`):

| Slice | Scenarios | Top-1 Acc. | Top-3 Acc. | Abstention Acc. | Action Correctness | Median Alert -> Revert Applied | Unapproved Writes |
|---|---|---|---|---|---|---|---|
| Core 10 (Live Cloud) | `S01`–`S10` | **1.0** (`10/10`) | **1.0** | **1.0** (`2/2`) | **1.0** (`10/10`) | **1.8s** | **0** |
| Held-Out 4 (`drills-heldout-v1`, Live Cloud) | `H01`–`H04` | **1.0** (`4/4`) | **1.0** | **1.0** (`1/1`) | **1.0** (`4/4`) | **2.0s** | **0** |
| Combined 14 (Live Cloud) | `S01`–`H04` | **1.0** (`14/14`) | **1.0** | **1.0** (`3/3`) | **1.0** (`14/14`) | **1.8s** | **0** |

Honest caveats from the raw live suite output:
- `S06` (`staging`-only decoy) produced `0.0%` production error rate (`breached=False`), so FlagRewind returned `not_flag_related` / `none` (`reverted=False`).
- `S10` (`eu-customers` `segment_rule` widening) correctly identified `eu-customers` top-1 with `revert_change`, and generated an **advisory** (manual) option (`reverted=False`, `0.016s`) because `executor.py` does not auto-mutate segment rules.
- `drills/results/live-20261127T022911Z/score_summary.json` measures alert -> Admin API revert (or abstention/advisory completion) after fault propagation, without adding separate per-scenario recovery windows (`live_drill.py` proved the full `81.046s` 3-window recovery loop on `S01`).

### C. Paced Live LLM Ablation Across All 14 Scenarios (`gemini-flash-lite-latest`, SYNTHETIC)
From `drills/results/llm_vs_rules_synthetic.json` (`python -m drills.ablation_live`, `min_interval_s=4.5`, Google AI Studio free tier):
*(Note: `gemini-flash-latest` hit its 20 RPD free-tier quota limit and `gemini-2.5-flash` returned HTTP 404 for new keys; re-running with `gemini-flash-lite-latest` completed all 14 scenarios with zero fallbacks.)*

| Mode | Runs | Top-1 Acc. | Top-3 Acc. | Abstention Acc. | Action Correctness | Median Latency | Mean Calls | Mean Tokens | Fallbacks Fired | Unapproved Writes |
|---|---|---|---|---|---|---|---|---|---|---|
| `rules-only` (synthetic) | 14 | **1.0** (`14/14`) | **1.0** | **1.0** (`3/3`) | **1.0** (`14/14`) | **0.0s** | `0` | `—` | `0` | **0** |
| `llm (gemini-flash-lite-latest)` | 14 | **1.0** (`14/14`) | **1.0** | **1.0** (`3/3`) | **0.7857** (`11/14`) | **4.11s** | `1` | `1397.5` | `0 / 14` | **0** |

Honest breakdown of the 3 `action_correctness` misses (`11/14`):
- On `S04` (`tax_engine_v2` global rollout with DE/FR failures), `S05` (`tax_engine_v2` multi-change with DE/FR failures), and `H01` (`profile_v2` global rollout with `free` plan failures), `gemini-flash-lite-latest` ranked the exact culprit `#1` (`top1_correct = True`), but recommended the allowlisted action `disable_for_segment` instead of full `revert_change`, while `ground_truth.json` / `ground_truth_heldout.json` expects `revert_change`. Every scenario completed cleanly with `calls=1`, `fallback_fired=False`, and `0` unapproved writes (`917`–`2420` tokens per scenario, `2.954s`–`5.672s` latency).

### D. "Saves Real Time" Same-Endpoint Comparison (`n=3`, Indicative Only)
Both sides **must** use the identical endpoint: **alert breach detected -> revert applied to Flagsmith** (including human review and typed confirmation on FlagRewind's side, not automated planning latency alone).

- **Manual UI Baseline (`B1`, `n=3`, indicative only):** Run blindly with `python -m drills.manual_stopwatch --count 3 --seed 42` (`drills/results/manual_baseline_n3.json`). **Status: Pending operator execution** (not fabricated in this repo; run by a human navigating the Flagsmith UI + `/metrics` + `/errors`).
- **FlagRewind Measured Endpoints (Live Cloud `S01` proof):**
  - Alert -> Plan Ready (including live Cloud polling & LLM call): **12.406s**
  - Alert -> Revert Applied (automated confirmation script, excluding human reading/typing pause): **31.859s** (`flagrewind apply` human typed phrase adds ~5–15s of operator typing in interactive runs)
  - Alert -> Verified Recovery (`3` healthy 10s windows): **81.046s**

## 4. Known Limitations & Bias Disclosure
- All scenarios were constructed around the synthetic Acme Checkout fault model (`docs/fault-model.md`).
- Even with seed-randomized timings and hidden ground-truth files, familiarity with the demo service creates author advantage in `B1`; report `n=3` strictly as **indicative only**.
- Live Cloud runs executed against Flagsmith Cloud Free tier (`https://api.flagsmith.com/api/v1/`), where CDN `/environment-document/` propagation takes ~30–40s per mutation and Free tier audit endpoints return `0` entries (covered via Admin API state-snapshot diff polling).
