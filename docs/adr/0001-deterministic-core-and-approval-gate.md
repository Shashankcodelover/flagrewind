# ADR 0001: Deterministic Core with Isolated LLM Ranking and Code-Enforced Approval

- **Status:** Accepted (`2026-11-26`)
- **Context:** FlagRewind operates on Flagsmith production state during incidents, where Flagsmith organisation keys are broad and log/flag text can contain untrusted content.
- **Decision:** Keep all evidence collection, inverse operation planning, precondition verification, and Admin API execution strictly deterministic in Python (`evidence.py`, `planner.py`, `approval.py`, `executor.py`). Restrict the LLM (`ranking/llm.py`) exclusively to ranking deterministic candidate IDs and writing short summaries inside a Pydantic-validated JSON schema without API tools.
- **Alternatives Considered:** Giving an agent framework (e.g., LangGraph) direct Flagsmith API write tools — rejected because prompt injection or hallucinated parameters could mutate wrong flags or environments.
- **Consequences:** Non-interactive runs and rejected approvals are provably incapable of writing to Flagsmith (`tests/unit/test_safety_no_unapproved_writes.py`), and `--no-llm` works as a full-loop deterministic fallback.
