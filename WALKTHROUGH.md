# FlagRewind Architecture Walkthrough & Judge Q&A

This guide explains every module in FlagRewind so you can walk through and defend every architectural choice during judging.

---

## 1. Module-by-Module Map

| File | Purpose | Key Safety / Engineering Guarantee |
| --- | --- | --- |
| `src/flagrewind/config.py` | Strict Pydantic v2 config & env lookup | `extra="forbid"` rejects typos (`exit 2`); `writable_environments()` defines allowed mutation scope. |
| `src/flagrewind/cli.py` | Typer CLI (`doctor`, `journal`, `watch`, `investigate`, `plan`, `apply`, `undo`, `report`) | Exit codes `0–5` match exact states; `--no-llm` always available. |
| `src/flagrewind/flagsmith_client.py` | Thin `httpx` wrapper over `/api/v1/` | Maps HTTP `401/403` and network timeouts to `exit 3`; no hidden state. |
| `src/flagrewind/journal/store.py` | Append-only JSONL (`CHG-XXXXXX`) & heartbeat check | Deduplicates events; detects polling/webhook gaps inside `[onset - 60m, onset + sdk_refresh_seconds]`. |
| `src/flagrewind/journal/webhook.py` | FastAPI receiver for `FLAG_UPDATED` / `FLAG_DELETED` | Constant-time `hmac.compare_digest` on `X-Flagsmith-Signature` (`401` on invalid). |
| `src/flagrewind/journal/audit_poll.py` | Polls `/api/v1/audit/` | Captures segment-rule edits (`S10`) and missed webhooks; records heartbeat ticks. |
| `src/flagrewind/symptoms/demo.py` | Aggregates `/metrics` & `/errors` | Computes onset, baseline vs current error rate, `country`/`plan`/`endpoint` slices, and <= 20 truncated errors. |
| `src/flagrewind/codeindex.py` | AST + regex flag-call scanner | Maps error stack frames back to exact Flagsmith keys in `demo_service/`. |
| `src/flagrewind/evidence.py` | Deterministic `EvidenceBundle` builder | Scores `time`, `env` (`0` for non-writable staging), `scope`, `code`, `magnitude`. |
| `src/flagrewind/ranking/schema.py` | Strict Pydantic ranking contract | Rejects hallucinated `change_id`, out-of-range confidence, inconsistent `verdict != flag_change`, > 300 char strings, and injection phrases. |
| `src/flagrewind/ranking/rules.py` | Deterministic rules ranker | Also serves as Baseline B2 and automatic fallback if LLM fails or is rejected. |
| `src/flagrewind/ranking/prompts.py` & `llm.py` | Untrusted-data framing + 1 repair retry | Wraps bundle in `<UNTRUSTED_EVIDENCE_JSON>`; logs SHA-256 hashes & tokens to `audit.jsonl`. |
| `src/flagrewind/llm_client.py` | OpenAI-compatible client (`openai` SDK) | Switches cleanly between Gemini free tier and local Ollama using env vars only. |
| `src/flagrewind/planner.py` | Deterministic inverse & `disable_for_segment` | Generates exact `PATCH` payloads + inverse undo payloads + canonical SHA-256 `plan_hash`. |
| `src/flagrewind/approval.py` | Interactive typed gate | Blocks non-interactive sessions (`sys.stdin.isatty()`); requires exact `apply <plan_id> option <n>`. |
| `src/flagrewind/executor.py` | Allowlisted Flagsmith writer | Enforces approved `plan_hash`, `writable` env, `PATCH`/`POST`-only, featurestates path regex, and precondition re-read (`exit 5`). |
| `src/flagrewind/verifier.py` | Post-write confirmation | Re-reads Flagsmith target state + checks 3 consecutive healthy windows. |
| `src/flagrewind/report.py` & `audit.py` | Rich 80-col brief + reports + redacted audit | Honours `NO_COLOR`; redacts API keys (`Api-Key ...`, `ser.*`, `AIza*`). |
| `src/flagrewind/capabilities.py` | `flagrewind doctor` | Verifies Flagsmith API, dry-run write scope (0 writes), webhook port, MCP read tools, and LLM schema smoke test. |
| `src/flagrewind/mcp_reader.py` | P1 read-only Flagsmith MCP client | Allows only `list_*` / `get_*` MCP tools; blocks any write tool calls. |
| `demo_service/` | Synthetic "Acme Checkout" service | FastAPI + Flagsmith local eval (`5s` refresh) + frozen faults + seedable `20 req/s` traffic. |
| `scripts/bootstrap.py` | Idempotent Flagsmith demo provisioner | Creates project, `staging`/`production`, 9 flags, 2 segments, webhook, and server-side key. |
| `drills/` | 10 scenarios, runner, ground truth, scorer | Ground truth isolated outside `code.paths`; all outputs labelled `SYNTHETIC`. |

---

## 2. Core Architectural Design Rationales
* **Why no agent framework (LangGraph/CrewAI)?** A custom loop keeps every state transition, error code, and security check explicit in ~2,000 lines of readable Python without hidden framework prompts or opaque tool dispatch.
* **How approval is enforced in code (not prompts):** `executor.py` will not execute unless `ApprovalRecord.approved` is `True` and `ApprovalRecord.plan_hash == compute_plan_hash(plan, option_n)`. `approval.py` refuses approval whenever `stdin` is non-interactive or the exact confirmation phrase fails to match.
* **How hallucinated or injected LLM outputs are stopped:** The LLM never touches Flagsmith or generates HTTP calls. Every string from Flagsmith/errors is wrapped in `<UNTRUSTED_EVIDENCE_JSON>` tags, and `validate_ranking_output()` rejects unknown `change_id` values or prompt-injection phrases before falling back to deterministic rules.

---

## 3. Twelve Likely Judge Questions & Answers
1. **What if the LLM picks the wrong suspect or hallucinates an ID?**
   `validate_ranking_output()` rejects unknown IDs, triggers 1 repair retry, and falls back to deterministic `rank_with_rules()`. Even if a valid ID is wrong, the human reviews evidence + counter-evidence before typing `apply <plan_id> option <n>`, and `flagrewind undo` restores the prior state.
2. **Why not let the LLM call the Flagsmith Admin API directly?**
   Open-source Flagsmith org API keys are broad. Keeping the LLM read-only with zero tools prevents prompt injection from mutating or deleting production flags.
3. **What if someone changes the flag in the dashboard while the human is reading the brief?**
   `executor.py` re-reads the current `featurestate` right before `PATCH` and compares it to `option.precondition`. Any drift aborts immediately with exit code `5` and asks the operator to re-plan.
4. **How do you know FlagRewind actually saves time?**
   We compare time-to-mitigation (including typed human approval) across 10 seeded scenarios against a timed manual runbook baseline (B1) and a rules-only ablation (B2) in `drills/score.py`, transparently disclosing synthetic data and `n`.
5. **What happens if a webhook packet is dropped?**
   `AuditPoller` polls `/api/v1/audit/` every `15s` as a second source and records heartbeats. If a gap exceeds `max(60s, 4 * poll_s)`, `journal_complete` flips to `False` and FlagRewind asks instead of recommending an auto-revert.
6. **How does FlagRewind handle non-flag outages (like a database failure)?**
   In Scenario 7 (`FLAGREWIND_NON_FLAG_FAULT=1`), either zero candidates match or error messages/stack frames fail to correlate with writable Flagsmith changes above `0.35`, so FlagRewind outputs `verdict="not_flag_related"` and `action="none"`.
7. **What if a change happened only in staging?**
   `evidence.py` sets `features.env = 0.0` and `rules_score = 0.0` for non-writable environments (`staging`), and `executor.py` hard-blocks any write outside `writable: true` environments (`production`).
8. **How do you handle Feature Versioning v2?**
   Feature Versioning v2 is kept OFF on P0 demo environments because Flagsmith v2.287.0 removed experimental direct update endpoints when v2 versioning is enabled (`400 Bad Request`).
9. **Can FlagRewind work without paid LLM APIs?**
   Yes: `--no-llm` runs the full pipeline deterministically, and the OpenAI-compatible client supports both Google AI Studio's free tier and local Ollama models.
10. **How do you prevent approval fatigue / rubber-stamping?**
    The brief stays on one 80-col screen with counter-evidence and exact blast radius, allows one option per approval, and requires typing the plan-specific phrase `apply PLN-XXXX option N` (not just `y/n`).
11. **How is FlagRewind different from Flagsmith Feature Health or Sentry?**
    Sentry flags suspicious edits and Feature Health marks flags unhealthy when alerts already name the feature; FlagRewind closes the loop by correlating blind metrics/stack frames with Flagsmith history, generating the exact inverse, gating on human approval, applying, and verifying recovery.
12. **How can this connect to real production observability?**
    `symptoms/demo.py` exposes a single `SymptomSummary` contract; adding a Prometheus or Datadog adapter is isolated to `src/flagrewind/symptoms/` without touching evidence, ranking, planning, or execution.
