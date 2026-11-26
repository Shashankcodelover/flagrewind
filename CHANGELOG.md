# Changelog

All notable changes to FlagRewind will be documented in this file.

## [0.1.0] - 2026-11-26
### Added
- Strict Pydantic v2 configuration loader (`flagrewind.yaml`) and Typer CLI (`doctor`, `journal`, `watch`, `investigate`, `plan`, `apply`, `undo`, `report`).
- Pinned Flagsmith `v2.295.2` Docker Compose stack and idempotent bootstrap (`scripts/bootstrap.py`).
- Synthetic "Acme Checkout" FastAPI demo service (`demo_service/`) with frozen deterministic faults and traffic generator.
- HMAC-SHA256 Flagsmith webhook receiver, Admin audit-log poller, gap detector, AST code indexer, and evidence bundle builder.
- Deterministic rules ranker, validated OpenAI-compatible LLM ranker (Gemini free tier / local Ollama) with repair retry and labelled rules fallback, inverse planner, typed approval gate, allowlisted executor (drift exit `5`), recovery verifier, Rich 80-col/NO_COLOR reports, and redacted audit trail.
- P1 read-only Flagsmith MCP client (`mcp_reader.py`), `--notify-url` webhook support, 10-scenario synthetic drill suite (`drills/`), GitHub Actions CI, and documentation.
