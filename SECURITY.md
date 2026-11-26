# Security Policy

## Reporting a Vulnerability
Please report security issues privately via GitHub Security Advisories on `Shashankcodelover/flagrewind` or directly to the repository owner (`shashank.j8426@gmail.com`) rather than opening a public issue.

## Security Boundaries Enforced by FlagRewind
- **No Unapproved Writes:** `flagrewind.approval` requires interactive typed confirmation (`apply <plan_id> option <n>`); `flagrewind.executor` enforces writable environment allowlists, forbids `DELETE`, and aborts on state drift (`exit 5`).
- **Prompt-Injection Containment:** The LLM receives no tools, sees evidence wrapped in `<UNTRUSTED_EVIDENCE_JSON>` delimiters, and has every candidate ID, verdict, and string verified deterministically in `flagrewind.ranking.schema`.
- **Key Hygiene:** Organisation API keys on self-hosted Flagsmith are broad; use a dedicated demo organisation, separate agent vs. drill-runner keys, and keep secrets only in environment variables or gitignored `.env`.
