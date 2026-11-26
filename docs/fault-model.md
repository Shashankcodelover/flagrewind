# FlagRewind Synthetic Fault Model ("Acme Checkout")

> **SYNTHETIC DATA NOTICE:** All endpoints, users, traffic distributions, and faults below belong exclusively to the synthetic `demo_service` benchmark world (`SYNTHETIC`). Fault rules are frozen prior to any benchmark run.

## 1. Demo Service Endpoints (`http://localhost:8001`)
- `GET /checkout?country=<CC>&plan=<PLAN>&user_id=<ID>` — Evaluates checkout flags via Flagsmith local evaluation (`environment_refresh_interval_seconds=5`).
- `GET /search?q=<QUERY>&country=<CC>&plan=<PLAN>&user_id=<ID>` — Evaluates search latency flags.
- `GET /profile?country=<CC>&plan=<PLAN>&user_id=<ID>` — Evaluates user profile flags.
- `GET /metrics` — Returns 10-second rolling request/error counts overall and broken down by `country`, `plan`, and `endpoint`.
- `GET /errors` — Returns up to the most recent `50` error samples (`{timestamp, type, message, endpoint, country, plan, frames}`).

## 2. Frozen Feature Flags, Remote Config & Fault Rules

| Flag / Switch Key | Type | Trigger Condition | Synthetic Symptom |
| --- | --- | --- | --- |
| `tax_engine_v2` | Boolean flag | Enabled AND `country in {"DE", "FR"}` (or `eu-customers` segment override) | `VATCalculationError` on `60%` of `/checkout` requests (deterministic hash of `request_seq` / `seed`) |
| `payment_timeout_ms` | Remote config (`int`) | Integer value `< 500` (default safe value: `3000`) | `TimeoutError` on `30%` of `/checkout` requests |
| `profile_v2` | Boolean flag | Enabled AND `plan == "free"` (or `free-plan` segment override) | `KeyError` on `100%` of `/profile` requests for affected users |
| `search_ranker_v2` | Boolean flag | Enabled | Adds `400 ms` latency on `/search`; `0%` errors (latency-only, no breach) |
| `payment_provider` | Multivariate | Variant `"broken_gateway"` selected (weight shifted in Scenario 8; safe baseline `"stripe_v1"` at 100% or low broken weight below threshold) | `PaymentGatewayError` on `80%` of `/checkout` requests routed to `"broken_gateway"` |
| `checkout_layout`, `maintenance_banner`, `export_csv`, `dark_mode` | Boolean decoys | Any state | `0` errors (safe decoys for noise/decoy drills) |
| `FLAGREWIND_NON_FLAG_FAULT` | Host env var (`"1"`) | Set to `"1"` (Scenario 7) | `DatabaseConnectionError` on `25%` of `/checkout` requests regardless of Flagsmith flags |

## 3. Code-to-Flag Mapping (`demo_service/faults.py`)
- `demo_service/faults.py` explicitly evaluates `tax_engine_v2`, `payment_timeout_ms`, `profile_v2`, `search_ranker_v2`, and `payment_provider` inside dedicated helper functions (`apply_tax_engine`, `check_payment_timeout`, `render_profile`, `run_search_ranker`, `charge_payment_provider`) so stack frames cleanly trace to the exact files indexed by `codeindex.py`.

## 4. Synthetic Traffic Mix (`demo_service/traffic.py`)
- Default rate: `20 req/s` (`seed` controllable for deterministic reproduction).
- Endpoints: `50% /checkout`, `30% /search`, `20% /profile`.
- Countries: `US` (`45%`), `GB` (`20%`), `DE` (`15%`), `FR` (`15%`), `IT` (`5%`, tests Scenario 10 segment widening).
- Plans: `free` (`50%`), `pro` (`35%`), `enterprise` (`15%`).
