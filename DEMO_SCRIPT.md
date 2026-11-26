# FlagRewind Demo Script (Target <= 3 Minutes)

> **Rule:** Use only real recorded terminal/UI runs. Keep a pre-recorded backup of Scene 3 alongside saved `.flagrewind/incidents/` reports.

## Scene 1 — The Problem (0:00 – 0:15)
- **Visual:** Show Flagsmith audit log with 10+ recent changes and Acme Checkout `/metrics` spiking past 5% error rate.
- **Voiceover:** *"At 2 a.m., when checkout fails, engineers waste precious minutes scrolling Flagsmith's audit log, guessing the culprit, and typing manual reverts."*

## Scene 2 — Setup & Doctor (0:15 – 0:35)
- **Visual:** Terminal split showing Flagsmith (`:8000`), Acme Checkout (`:8001`), and `flagrewind journal` (`:8787`), then run:
  ```bash
  flagrewind doctor
  ```
- **Voiceover:** *"FlagRewind checks Flagsmith API connectivity, audit logs, writable allowlists (zero writes), webhook listener, and model schema support in one command."*

## Scene 3 — The Live Mitigation Moment (0:35 – 1:35)
- **Visual:** Traffic running at 20 req/s; run `python -m drills.runner --scenario S01` (`tax_engine_v2` enabled for `eu-customers` among decoys). Watch `flagrewind watch --once` open `INC-0001` and display the 80-column brief:
  - Suspect `CHG-XXXXXX`: `tax_engine_v2` enabled for `eu-customers`
  - Evidence: DE/FR checkout failures, `VATCalculationError` in `faults.py`, blast radius, exact confirmation phrase.
- Run `flagrewind apply INC-0001`, type operator name and `apply PLN-0001 option 1`.
- Show Flagsmith state verified (`enabled=False`), error rate returning below 5% across 3 windows, and `.flagrewind/incidents/INC-0001/report.md`.
- **Voiceover:** *"FlagRewind investigates autonomously, stops strictly at the approval gate, applies the exact inverse through the Admin API, and confirms service recovery."*

## Scene 4 — Safety & Abstention (1:35 – 2:00)
- **Visual:** Show (a) typing a wrong confirmation phrase or rejecting leaves Flagsmith state unchanged, and (b) Scenario 7 (`FLAGREWIND_NON_FLAG_FAULT=1`), where FlagRewind answers `NOT_FLAG_RELATED` and proposes `NO WRITE`.
- **Voiceover:** *"Safety is enforced in code: rejected approvals write nothing, state drift aborts cleanly, and non-flag incidents never trigger blind rollbacks."*

## Scene 5 — Benchmark Evidence (2:00 – 2:35)
- **Visual:** Show `drills/score.py` summary table comparing manual runbook (B1), rules-only (B2), and LLM mode across all 10 synthetic scenarios (top-1 accuracy, abstention on S06/S07, time to mitigation, `0` unapproved writes).
- **Voiceover:** *"Across 10 seeded synthetic scenarios, FlagRewind delivers accurate triage, 100% abstention on staging-only and non-flag faults, and zero unapproved writes."*

## Scene 6 — Architecture & Reuse (2:35 – 3:00)
- **Visual:** Show the Autonomy Contract table in `README.md` + `src/flagrewind/` module layout.
- **Voiceover:** *"Deterministic core, untrusted-data LLM isolation, and clean Flagsmith Admin API + webhook + SDK integration—that's FlagRewind."*
