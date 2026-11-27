# FlagRewind Live Demo Script (<= 3 Minutes)

> **Recording Rules:** Live Flagsmith Cloud run only (`https://app.flagsmith.com/` + `https://api.flagsmith.com/api/v1/`). Never show `.env` or API keys on screen. Keep total runtime under `3:00` (`~2:45` target).

---

## Shot-by-Shot Checklist (0:00 – 2:45)

| Shot | Time | Terminal / Window Action | What Viewers Must See |
|---|---|---|---|
| **1. Problem & Preflight** | `0:00 – 0:25` | Browser tab on **Flagsmith Cloud** (`Acme Checkout` production flags & segment overrides) + terminal running `flagrewind doctor` | Live Flagsmith Cloud org check (`PASS`), `production`/`staging` environments, dry-run write guard, and `gemini-flash-lite-latest` schema check (`PASS`). |
| **2. Alert + Live Fault** | `0:25 – 0:50` | Terminal A: `uvicorn demo_service.app:app --port 8001` + `python -m demo_service.traffic --rate 20 --seed 42`; Terminal B: `flagrewind journal --port 8787`; Terminal C: `python -m drills.runner --scenario S01` | Production traffic running; `S01` enables `tax_engine_v2` for `eu-customers` plus decoys on Flagsmith Cloud; `/metrics` error rate crosses `5%` (`VATCalculationError`). |
| **3. Ranked Culprits & Blast Radius** | `0:50 – 1:25` | Terminal D: `flagrewind watch --once` (or live capture window from `flagrewind watch`) | 80-column Rich incident brief (`INC-...`): Alert rate (`>5%` DE/FR), Top suspect `tax_engine_v2` (`eu-customers` override) with confidence, code frame `demo_service/faults.py:apply_tax_engine`, blast radius, and dry-run `PUT` plan (`PLN-...`) + required approval phrase. |
| **4. Typed Approval & Revert** | `1:25 – 1:55` | Terminal D: `flagrewind apply INC-0001`, first type `n` (or wrong phrase) briefly OR proceed cleanly to type operator name + `apply PLN-0001 option 1` | Agent halts at prompt -> **no write until exact phrase typed** -> executor preconditions verified -> Flagsmith Cloud `PUT` revert applied (`0` unapproved writes). Show Flagsmith Cloud tab refreshed showing `tax_engine_v2` reverted. |
| **5. Recovery & Report** | `1:55 – 2:20` | Terminal D output + open `.flagrewind/incidents/INC-0001/report.md` (and `drills/results/live_e2e_proof.json`) | Three consecutive `<5%` 10s recovery windows (`error_rate -> 0.0`), written Markdown report, and redacted audit hash record. |
| **6. Honest Benchmark Table** | `2:20 – 2:45` | Show `docs/benchmark.md` Summary Tables & `docs/verification/latest.md` | State honestly: live E2E (`n=1`, `S01`): `31.9s` alert -> revert applied (automated approval, excluding typing delay; `81.0s` full recovery), same endpoint as `B1` manual baseline (`n=3`, indicative — pending operator recording; do not cite any unrecorded `~178s` number). Other runs: Cloud 14-scenario suite (`n=14` rules-only: `10/10` core + `4/4` held-out frozen at `drills-heldout-v1`, `1.8s` median alert -> revert/advisory); LLM ablation (`n=14` synthetic, **advisory only**: `14/14` top-1, `11/14` action, `0` fallbacks, held-out frozen at `drills-heldout-v1` before ablation); local `ruff` + `pytest` verified in `docs/verification/latest.md` (**local run; GitHub Actions blocked by an account billing hold**); `0` unapproved writes across all runs. |

---

## 60-Word Voiceover Script (~28s core narration — pair across Shots 2–5)

> When checkout breaks overnight, dangerous flag edits hide inside noisy changes. FlagRewind watches Flagsmith through the Admin API, webhooks, and live error metrics, correlates failing segments with source code, ranks suspects, and prepares the exact inverse rollback with blast radius. Nothing mutates without my explicit typed approval. Once confirmed, FlagRewind reverts the flag, verifies three healthy windows, and logs recovery.

*(Word count: exactly 60 words.)*

---

## Pre-Recording Safety Check
- Ensure `.env` stays closed off-screen (`python -m drills.runner --reset` sets Flagsmith Cloud back to clean baseline before starting Shot 1).
- Never display `.flagrewind/audit.jsonl` before verifying redaction, though `audit.py` automatically strips all keys and token prefixes (`AQ.`, `ser.`, loaded `.env` secrets).
