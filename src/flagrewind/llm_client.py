"""OpenAI-compatible LLM client supporting Gemini free tier and local Ollama."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI, OpenAIError

from flagrewind.audit import json_sha256
from flagrewind.config import LLMConfig, resolve_env


class LLMClientError(RuntimeError):
    """Raised when the LLM endpoint is unreachable, unconfigured, or errors."""


@dataclass(frozen=True, slots=True)
class LLMCallResult:
    """Raw completion result and metadata for audit logging."""

    raw_text: str
    parsed_json: dict[str, Any]
    model: str
    prompt_sha256: str
    response_sha256: str
    total_tokens: int | None = None


class OpenAICompatibleLLMClient:
    """Thin wrapper over the official openai SDK using env-configured endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        timeout_s: float = 30.0,
        openai_client: OpenAI | None = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.timeout_s = timeout_s
        self._client = openai_client or OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_s,
            max_retries=0,
        )

    @classmethod
    def from_config(cls, cfg: LLMConfig) -> OpenAICompatibleLLMClient | None:
        """Build client from environment variables referenced by LLMConfig, or None if unset."""
        base_url = resolve_env(cfg.base_url_env)
        model = resolve_env(cfg.model_env)
        api_key = resolve_env(cfg.api_key_env)
        if not base_url or not model:
            return None
        # Local Ollama needs no real key, but openai SDK requires a non-empty placeholder.
        is_local = "localhost" in base_url or "127.0.0.1" in base_url
        effective_key = api_key or ("ollama" if is_local else "")
        if not effective_key:
            return None
        return cls(
            base_url=base_url,
            api_key=effective_key,
            model=model,
            temperature=cfg.temperature,
            timeout_s=float(cfg.timeout_s),
        )

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMCallResult:
        """Request a structured JSON completion from the configured model."""
        prompt_hash = json_sha256({"system": system_prompt, "user": user_prompt})
        response_format: dict[str, Any]
        if json_schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "flagrewind_verdict",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        else:
            response_format = {"type": "json_object"}

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Rate-limiting / pacing and exponential backoff for free-tier APIs (e.g. Gemini 15 RPM).
        # Retry up to 3 times on 429/ResourceExhausted with jittered backoff.
        max_attempts = 4
        backoff_delay = 5.0
        resp = None

        for attempt in range(max_attempts):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=self.temperature,
                    response_format=response_format,  # type: ignore[arg-type]
                )
                break
            except OpenAIError as exc:
                err_msg = str(exc).lower()
                is_rate_limit = (
                    "429" in err_msg or "resource_exhausted" in err_msg or "quota" in err_msg
                )
                if is_rate_limit and attempt < max_attempts - 1:
                    time.sleep(backoff_delay)
                    backoff_delay *= 2.0
                    continue

                # Fallback once from json_schema to json_object if endpoint rejects strict schema.
                if json_schema is not None and not is_rate_limit:
                    try:
                        resp = self._client.chat.completions.create(
                            model=self.model,
                            messages=messages,  # type: ignore[arg-type]
                            temperature=self.temperature,
                            response_format={"type": "json_object"},  # type: ignore[arg-type]
                        )
                        break
                    except OpenAIError as retry_exc:
                        raise LLMClientError(f"LLM API failed: {retry_exc}") from retry_exc
                else:
                    raise LLMClientError(f"LLM API failed: {exc}") from exc

        raw_text = ""
        if resp.choices and resp.choices[0].message.content:
            raw_text = resp.choices[0].message.content.strip()

        usage = getattr(resp, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage is not None else None
        resp_hash = json_sha256(raw_text)

        try:
            parsed = json.loads(_strip_code_fence(raw_text))
        except json.JSONDecodeError as exc:
            raise LLMClientError(f"LLM returned invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise LLMClientError("LLM JSON root must be an object.")

        return LLMCallResult(
            raw_text=raw_text,
            parsed_json=parsed,
            model=self.model,
            prompt_sha256=prompt_hash,
            response_sha256=resp_hash,
            total_tokens=int(total_tokens) if total_tokens is not None else None,
        )

    def smoke_test_schema(self) -> tuple[bool, str]:
        """Run a minimal structured-output test for `flagrewind doctor`."""
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        try:
            res = self.complete_json(
                system_prompt="Return JSON matching the schema.",
                user_prompt='Return {"ok": true}.',
                json_schema=schema,
            )
        except LLMClientError as exc:
            return False, str(exc)[:80]
        return bool(res.parsed_json.get("ok") is True), f"model={self.model} schema=ok"


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return stripped
