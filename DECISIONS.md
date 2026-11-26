# FlagRewind — Architectural & Verification Decisions (`DECISIONS.md`)

All findings below were verified during M0 Discovery on `2026-11-26` (IST). Nothing is inferred without marking it.

## D01 — Environment & Toolchain Baseline (M0)
- **Verified:** `Windows_NT 10.0.26300` (16 CPUs, `16.23 GB` RAM with `8.79 GB` free / `45.9%` used, `282.82 GB` free on `D:`).
- **Verified:** `git 2.52.0.windows.1`, `Python 3.11.9`, `uv.exe` (`C:\Users\Shashank_J\.local\bin\uv.exe`), `Docker 29.4.3` + `Docker Compose v5.1.3`.
- **Verified:** Ports `8000`, `8001`, `8787` are all `FREE`.
- **Gap / Blocker:** Docker Desktop Linux engine pipe (`npipe:////./pipe/dockerDesktopLinuxEngine`) is not running yet; `ollama` is not installed; `GEMINI_API_KEY` and `LLM_API_KEY` are unset (`False`).

## D02 — Workspace & GitHub Account Isolation
- **Decision:** Initialize `flagrewind` repository inside `d:\users\Shashank J\Desktop\track 2\flagrewind\` (keeping `track 2/GEMINI.md` and `track 2/TASK_BRIEF.md` outside the public repo).
- **Decision:** Configure `git config --local user.name "Shashankcodelover"` and matching `--local` email inside `flagrewind`; never touch `--global` Git config (`shashankshashi831431@gmail.com` belongs to a separate account).
- **Source:** Workspace `GEMINI.md` & `git config --global` inspection (`2026-11-26`).

## D03 — Package & Repo Name Availability (`flagrewind`)
- **Verified:** Web search across PyPI (`pypi.org/project/flagrewind`) and GitHub (`Shashankcodelover/flagrewind`, `flagrewind` on GitHub) returned no exact package or repository collision.
- **Source:** `search_web` (`2026-11-26`).

## D04 — Hackathon Rules & Track 02 Requirements Re-Check
- **Verified:** Track 02 ("AI Agents and Feature Flags", `$1,000` cash) hacking window runs `Nov 26, 2026 08:00 IST` to `Nov 29, 2026 20:00 IST`; late submissions rejected; deliverables: public GitHub repo, comprehensive README (setup + how Flagsmith is used), short demo video; AI coding assistants allowed if participant understands and can explain code.
- **Source:** `wemakedevs.org/hackathons/flagsmith` and `/rules` via `search_web` (`2026-11-26`).

## D05 — Flagsmith Admin API, Webhooks & Audit Log Contracts
- **Verified:**
  - Feature state mutations on environments **without** Feature Versioning v2 use `GET / POST / PUT / PATCH /api/v1/environments/{environment_api_key}/featurestates/{feature_state_id}/` (body supports `enabled`, `feature_state_value`). Feature Versioning v2 disables legacy `/featurestates/` writes (`400 Bad Request`), so Feature Versioning v2 **must remain OFF** in P0 demo environments.
  - Segment overrides use `POST /api/v1/features/feature-segments/` and priority ordering `POST /api/v1/features/feature-segments/update-priorities/`.
  - Audit log REST endpoints: `/api/v1/audit/`, `/api/v1/audit/{id}/` (`change_details`), `/api/v1/organisations/{id}/audit/`.
  - Environment webhooks fire `event_type: "FLAG_UPDATED"` (and `FLAG_DELETED`) with `data.changed_by`, `data.previous_state` (`enabled`, `environment`, `feature`, `feature_state_value`), and `data.new_state`, signed via `X-Flagsmith-Signature` hex HMAC-SHA256 of raw request body.
- **Pending live verification (M1/M2):** Exact Swagger schema on local pinned container (`http://localhost:8000/api/v1/docs/`), exact captured webhook payload shape, and exact response schema of `/api/v1/audit/`.
- **Sources:** `docs.flagsmith.com`, `github.com/Flagsmith/flagsmith`, `apis.io` via `search_web` (`2026-11-26`).

## D06 — Flagsmith Docker Compose Port `8001` Collision Prevention
- **Finding:** Upstream `Flagsmith/flagsmith` `docker-compose.yml` binds `flagsmith-task-processor` to host port `8001:8000`, which collides with FlagRewind's `demo_service` on port `8001`.
- **Decision:** In `docker/docker-compose.yml` (crediting upstream BSD-3-Clause), keep `flagsmith` on `8000:8000` and either map `flagsmith-task-processor` to `8002:8000` or omit its external port mapping so port `8001` stays exclusively reserved for `demo_service`. Pin an exact `v2.x` Flagsmith tag (verify latest stable `v2.288.0+` / `v2.292.0` tag when pulling in M1).
- **Source:** Upstream `Flagsmith/flagsmith/docker-compose.yml` via `search_web` (`2026-11-26`).

## D07 — Flagsmith Python SDK & MCP Server Contracts
- **Verified (Python SDK `flagsmith==4.0.1` installed source):**
  - `Flagsmith(environment_key=..., api_url="http://localhost:8000/api/v1/", enable_local_evaluation=True, environment_refresh_interval_seconds=5)`
  - `get_identity_flags(identifier: str, traits: Mapping[str, None | int | float | bool | str | TraitConfig] = None) -> Flags`
  - `get_environment_flags() -> Flags` (`is_feature_enabled`, `get_feature_value`)
- **Verified (MCP Server P1):** `flagsmith/flagsmith-mcp` supports `FLAGSMITH_API_TOKEN` + `FLAGSMITH_API_URL`, exposing core read/list tools for features, environments, segments, segment overrides, and metrics.
- **Source:** `inspect.signature` on `.venv` `flagsmith==4.0.1` and `docs.flagsmith.com` (`2026-11-26`).

## D08 — Flagsmith Pinned Image & Server-Side SDK Key Endpoint
- **Verified:** Latest stable Flagsmith release tag on GitHub is `v2.295.2` (published `2026-11-23`). Pinned `docker.flagsmith.com/flagsmith/flagsmith:v2.295.2` in `docker/docker-compose.yml` with `EXPERIMENTATION_ENABLED="false"` and `flagsmith-task-processor` moved to host port `8002:8000` so `demo_service` retains port `8001`.
- **Verified:** Server-side SDK keys (`ser.*`) are provisioned via `POST /api/v1/environments/{environment_api_key}/api-keys/` and only returned once on creation.
- **Source:** `api.github.com/repos/Flagsmith/flagsmith/releases/latest`, `raw.githubusercontent.com/.../v2.295.2/docker-compose.yml`, and `docs.flagsmith.com` (`2026-11-26`).

## D09 — Verified Gemini OpenAI-Compatible Model ID
- **Verified:** `OpenAICompatibleLLMClient.smoke_test_schema()` passed against `https://generativelanguage.googleapis.com/v1beta/openai/` using `LLM_MODEL=gemini-flash-latest` (`schema=ok`). `gemini-2.5-flash` and `gemini-2.5-flash-lite` returned HTTP `404` on the OpenAI-compatible chat completions route for this key.
- **Source:** Live `smoke_test_schema()` check (`2026-11-26`).
