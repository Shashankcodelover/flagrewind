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

## 3. Known Limitations & Bias Disclosure
- All scenarios were constructed around the synthetic Acme Checkout fault model (`docs/fault-model.md`).
- Even with seed-randomized timings and hidden ground-truth files, familiarity with the demo service creates author advantage in B1; report `n` and include an independent volunteer baseline wherever possible.
- Live Docker + Gemini/Ollama benchmark runs are recorded under `drills/results/` only after actual execution (never fabricated).
