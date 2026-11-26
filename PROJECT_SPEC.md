# FlagRewind — Project Specification

- **Event / Track:** Toggle Wars 2026 (WeMakeDevs x Flagsmith) · Track 02 "AI Agents and Feature Flags"
- **Participant:** P3 (solo) · GitHub scope: `Shashankcodelover`
- **Pitch:** *"When checkout breaks at 2 a.m., FlagRewind tells you which flag change did it, shows the evidence and the exact undo, and waits for your yes."*
- **Deadline:** 29 Nov 2026, 20:00 IST (internal target: 18:00 IST).

## 1. Core Loop & Autonomy Contract
`detect -> investigate -> rank suspects -> plan inverse (dry run) -> HUMAN APPROVAL -> execute -> verify -> report`

| Boundary | Permitted Actions |
| --- | --- |
| **Does alone** | Reads Flagsmith state, change journal, audit log, metrics, error samples, and local code; ranks suspects; drafts plans/inverses; runs dry runs; writes reports. |
| **Asks first** | Any write to Flagsmith; undoing a mitigation; trying the next suspect (requires typed `apply <plan_id> option <n>`). |
| **Asks instead of recommending** | Top confidence `< policy.confidence_floor` (`0.6`) or change journal has gaps during the incident window. |
| **Never does** | Creates or deletes flags, segments, or environments; writes outside allowlisted writable environments; follows instructions found inside change descriptions, logs, code, or tool output; writes in non-interactive mode. |

## 2. CLI Commands (`flagrewind`)
- `flagrewind doctor` — verifies Flagsmith API, key, project/environments, journal/audit readability, dry-run write check (no mutation), webhook port/receiver reachability, MCP read tools (P1), and LLM schema test; writes `.flagrewind/capabilities.json`.
- `flagrewind journal` — runs FastAPI/uvicorn webhook receiver + Flagsmith audit-log poller; appends `CHG-XXXXXX` events to `.flagrewind/journal.jsonl`.
- `flagrewind watch` — polls symptom source; opens incident and runs read-only investigation on threshold breach.
- `flagrewind investigate [--since 30m] [--symptom TEXT]` — builds evidence bundle, ranks suspects (`--no-llm` for rules-only), builds deterministic plan, renders one-screen Rich brief.
- `flagrewind plan <INC>` — displays dry-run plan preview again.
- `flagrewind apply <INC> [--option N]` — interactive approval gate -> precondition re-read -> Admin API execute -> state & recovery verification -> Markdown/JSON report.
- `flagrewind undo <INC>` — restores pre-mitigation state with fresh approval.
- `flagrewind report <INC>` — writes Markdown + JSON incident reports under `.flagrewind/incidents/<id>/`.
- **Exit codes:** `0` ok · `1` unresolved/rejected · `2` config error · `3` Flagsmith/LLM unreachable or unauthorized · `4` internal error · `5` precondition failed (state drifted).

## 3. Configuration (`flagrewind.yaml`, Pydantic v2 `extra="forbid"`)
- `flagsmith`: `api_url_env`, `admin_key_env`, `project`
- `environments`: `{production: {writable: true}, staging: {writable: false}}`
- `journal`: `webhook_port` (`8787`), `webhook_secret_env`, `audit_poll_seconds` (`15`)
- `symptoms`: `source` (`demo`), `demo_url` (`http://localhost:8001`), `error_rate_threshold` (`0.05`), `baseline_window_s` (`120`), `detect_window_s` (`30`)
- `code`: `paths: [demo_service]`
- `llm`: `base_url_env`, `api_key_env`, `model_env`, `temperature: 0`, `max_calls_per_incident: 3`, `timeout_s: 30`
- `policy`: `allowed_actions: [revert_change, disable_for_segment]`, `confidence_floor: 0.6`, `verify_seconds: 60`, `sdk_refresh_seconds: 5`

## 4. Key Contracts
- **Journal event:** `id` (`CHG-XXXXXX`), `source` (`webhook` | `audit`), `occurred_at`, `received_at`, `environment`, `feature`, `kind` (`feature_state` | `segment_override` | `segment_rule` | `multivariate` | `other`), `segment`, `previous` (`{enabled, value}` | `null`), `new` (`{enabled, value}` | `null`), `actor`, `raw_ref`.
- **Evidence window:** `[onset - 60m, onset + sdk_refresh_seconds]`; staging kept with `env=0`; `scope` measures overlap of affected dimensions vs segment rules; `code` matches flag keys in error stack frames.
- **LLM contract:** OpenAI-compatible client (`openai` SDK) with Gemini free tier or local Ollama; untrusted-data tags around evidence JSON; Pydantic validation (`change_id` in candidates, confidence `0..1`, non-`flag_change` forces `action="none"`, `<=5` ranked, strings `<=300` chars); 1 repair retry then labelled `"rules-only (LLM output rejected)"` fallback.
- **Executor safety:** Runs only deterministic `planner.py` output, only on writable environments, only feature states & segment overrides, never `DELETE`, re-reads Flagsmith state before applying (`exit 5` on drift).
- **Synthetic Demo World ("Acme Checkout", port `8001`):** Flagsmith SDK local evaluation (`5s` refresh), `/checkout`, `/search`, `/profile`, `/metrics`, `/errors`, deterministic faults (`tax_engine_v2`, `payment_timeout_ms`, `profile_v2`, `search_ranker_v2`, multivariate weights, 4 decoys, non-flag fault switch), and 10 seeded scenarios in `drills/`. All benchmark metrics labelled `SYNTHETIC`.

## 5. Priority Tiers
- **P0:** Pinned Flagsmith + bootstrap, `demo_service` + faults + traffic, webhook journal, evidence, rules ranking, LLM ranking + repair/fallback, planner, approval gate, executor, verifier, report, audit trail, `doctor`, unit + integration tests, README with autonomy contract.
- **P1:** `watch`, `undo` + next-suspect loop, audit-log poller, 10-scenario drills + scorer + manual baseline, MCP read-only mode, `disable_for_segment`, `--notify-url`, CI.
- **P2 (only if P0+P1 green by Sat 28 Nov 13:00 IST):** Minimal local approval page, Slack-compatible webhook, Prometheus symptom adapter.
