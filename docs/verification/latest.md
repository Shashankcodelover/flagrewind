# FlagRewind Local Verification Output

> **Verification Status:** local run; GitHub Actions blocked by an account billing hold.
> *(No CI badge is displayed here until GitHub Actions workflows run cleanly on GitHub.)*

- **Generated (UTC):** `2026-11-27T12:31:32Z`
- **Command:** `powershell -ExecutionPolicy Bypass -File scripts/verify.ps1`
- **Python:** `Python 3.11.9`
- **Overall Result:** **PASS** (`ruff` exit `0`, `pytest` exit `0`)

## 1. Ruff Lint (`ruff check .`)

- **Exit Code:** `0`

```text
All checks passed!
```

## 2. Pytest Suite (`pytest --tb=short`)

- **Exit Code:** `0`

```text
.................................                                        [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\fastapi\testclient.py:1
  D:\users\Shashank J\Desktop\track 2\flagrewind\.venv\Lib\site-packages\fastapi\testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
33 passed, 1 warning in 29.16s
```
