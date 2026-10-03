r"""Swappable AI chat-client layer (provider-agnostic) for SIMCC LLM calls.

Canonical, testable implementation. `app.py` imports this one directly.
`simcc_pipe.py` (which is exec'd standalone in Open WebUI's Function sandbox and
CANNOT import repo code per the existing `_TOPIC_BY_GRADE` convention) carries
its own inline copy of the same request/response shape; `test_ai_client.py`
asserts the two stay deferred-shape-compatible.

Two supported providers:

  * `ollama`        - native Ollama `/api/chat` endpoint (local GGUF/qwen/llama).
  * `openai_compat` - any OpenAI-style `/v1/chat/completions` HTTP endpoint
                      (works against corporate proxies, LiteLLM, vLLM, OpenRouter,
                      , LocalAI, LM Studio, llama.cpp server, etc).

Swapping to a new model means changing `DEFAULT_AI_MODEL` (or whichever valve /
envvar currently drives the config) - never editing code. Fail-loud: any
transport error raises `AiUnavailableError`; the caller decides whether to
retry, apologise, or fall back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class AiUnavailableError(RuntimeError):
    """The chat-completions backend (Ollama, OpenAI-compatible proxy, etc.) is
    unreachable or returned an unusable response. Raised for HTTP transport
    failures; callers typically convert this into a polite user message or a
    retry, never into ungrounded generation."""


@dataclass
class ProviderConfig:
    """Describes one bot-authorised chat-completions endpoint."""

    provider: str = "ollama"    # "ollama" | "openai_compat"
    base_url: str = ""          # e.g. "http://host.docker.internal:11434" or ".../v1"
    api_key: str = ""           # bearer token; ignored for Ollama
    model_name: str = ""        # e.g. "llama3.1:8b", "qwen3.6"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "ProviderConfig":
        """Build from OS environment variables. Every AI_* name has a documented
        fallback chain so one-var overrides behave predictably:

          AI_PROVIDER   (default "ollama")         which transport to use
          AI_BASE_URL   (default OLLAMA_BASE_URL)   chat-completions base
          AI_API_KEY    (default "")                bearer token; ignored for ollama
          AI_MODEL      (default OLLAMA_MODEL)       model tag
        """
        provider = (env.get("AI_PROVIDER") or "ollama").strip().lower()
        base_url = env.get("AI_BASE_URL") or env.get("OLLAMA_BASE_URL") or ""
        model_name = env.get("AI_MODEL") or env.get("OLLAMA_MODEL") or ""
        return cls(
            provider=provider,
            base_url=base_url.rstrip("/"),
            api_key=env.get("AI_API_KEY", ""),
            model_name=model_name,
        )

    @property
    def is_openai_compat(self) -> bool:
        return self.provider in ("openai_compat", "openai", "litellm", "vllm")


# --------------------------------------------------------------------------
# Wire-shape builders (pure, testable - no I/O). The pipe's inline copy of
# these request-shape rules is asserted by test_ai_client.py to match.
# --------------------------------------------------------------------------
def build_ollama_chat_request(config: ProviderConfig, system: str, user: str,
                              *, temperature: float, json_mode: bool) -> tuple[str, dict, dict]:
    """Returns (url, headers, payload) for Ollama's /api/chat."""
    payload = {
        "model": config.model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": temperature, "top_p": 0.9},
    }
    if json_mode:
        payload["format"] = "json"
    return f"{config.base_url}/api/chat", {}, payload


def build_openai_compat_request(config: ProviderConfig, system: str, user: str,
                                *, temperature: float, json_mode: bool) -> tuple[str, dict, dict]:
    """Returns (url, headers, payload) for an OpenAI-compatible /v1/chat/completions."""
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    payload = {
        "model": config.model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    base = config.base_url.rstrip("/")
    if base.endswith("/v1"):
        url = f"{base}/chat/completions"
    else:
        url = f"{base}/v1/chat/completions"
    return url, headers, payload


def extract_response_text(provider: str, response_json: dict) -> str:
    """Normalises the two providers' response envelopes into raw assistant text.
    Ollama : {"message": {"content": "..." }}
    OpenAI : {"choices": [{"message": {"content": "..." }}]}
    """
    if provider == "ollama":
        msg = response_json.get("message") or {}
        return str(msg.get("content") or "")
    # OpenAI-compatible (and anything duck-shaped like it)
    choices = response_json.get("choices") or []
    if not choices:
        return ""
    msg = (choices[0] or {}).get("message") or {}
    return str(msg.get("content") or "")


# --------------------------------------------------------------------------
# Secret resolution: 3-tier precedence, mirroring Open WebUI's pattern of
# never hardcoding a provider token in a committed config file.
#
#   1. `AI_API_KEY_FILE` env var   -> read the value from a runtime-secret file
#      (e.g. `/run/secrets/ai_api_key` mounted from a deployment secret store).
#   2. Postgres `config` table     -> row `openai.api_keys` (same shape
#      Open WebUI uses for its own Settings -> Connections).
#   3. `AI_API_KEY` env var        -> plain env-var fallback for dev/lab use.
#
# Fail-loud: if none of the three yields a usable token, raise
# AiUnavailableError - never return None/empty and let downstream code
# silently send an unauthenticated request.
# --------------------------------------------------------------------------
_SECRET_ROW_KEY = "openai.api_keys"


def _resolve_secret_core(*, env: dict, read_file, lookup_pg, env_var_name: str) -> tuple[str, str]:
    """Pure precedence chain, fully injectable (read_file / lookup_pg are fakes
    in tests). Returns (value, source) where source ∈ {"FILE_PATH","POSTGRES","ENV_VAR"}.

    Raises AiUnavailableError if every tier is absent or empty.

    Kept separate from `resolve_secret` below so tests can run without
    touching the filesystem or Postgres."""
    # Tier 1: file-path indirection.
    path = (env.get(env_var_name + "_FILE") or env.get("AI_API_KEY_FILE") or "").strip()
    if path:
        try:
            raw = read_file(path)
            if raw:
                return raw, "FILE_PATH"
            # fall through silently on empty file content
        except (OSError, ValueError):
            pass  # fall through to next tier - file missing / unreadable is not fatal.

    # Tier 2: Postgres config-table lookup (Open WebUI style row).
    row_val = lookup_pg(_SECRET_ROW_KEY)
    if row_val:
        return row_val, "POSTGRES"

    # Tier 3: plain env var (legacy / dev fallback).
    env_val = (env.get(env_var_name) or "").strip()
    if env_val:
        return env_val, "ENV_VAR"

    raise AiUnavailableError(
        f"No API key found in any secret store (tried file-path indirection, "
        f"Postgres config row {_SECRET_ROW_KEY!r}, and env var {env_var_name!r})."
        f" Set one of {env_var_name}_FILE / {env_var_name}, or run secrets_io.py set."
    )


def resolve_secret(*, env: dict | None = None, env_var_name: str = "AI_API_KEY") -> tuple[str, str]:
    """Real-I/O wrapper over `_resolve_secret_core`. File-reader and Postgres
    lookup are wired to the real filesystem / psql. For embedded use inside
    Open WebUI's Function sandbox, call `simcc_pipe.py`'s inline copy instead
    (kept shape-compatible by test_ai_client.py)."""
    import os
    import subprocess

    def _real_read_file(path: str) -> str:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()

    def _real_lookup_pg(key: str) -> str | None:
        """Read ONE row from school-chatbot Postgres `config` table via docker
        exec + psql -qAt (byte-safe, no shell interpolation - same plumbing as
        deploy_pipe_to_db.py). Returns None if table/row absent so the caller
        falls through gracefully."""
        try:
            r = subprocess.run(
                ["docker", "exec", "-i", "school-chatbot-db",
                 "psql", "-U", "school-chatbot", "-d", "school-chatbot", "-qAt", "-c",
                 f"SELECT value FROM config WHERE key='{key}';"],
                capture_output=True,
                timeout=15,
            )
            if r.returncode != 0:
                return None
            out = r.stdout.decode("utf-8", "replace").strip()
            return out if out else None
        except Exception:
            return None

    if env is None:
        env = os.environ  # type: ignore[assignment]
    return _resolve_secret_core(
        env=dict(env),
        read_file=_real_read_file,
        lookup_pg=_real_lookup_pg,
        env_var_name=env_var_name,
    )


# --------------------------------------------------------------------------
# Chat client - actual HTTP I/O.
# --------------------------------------------------------------------------
class ChatClient:
    """One configured chat-completions transport. .chat() is the single call
    surface used by every stage (generator, validator, orchestrator)."""

    def __init__(self, config: ProviderConfig, *, timeout: float | None = None) -> None:
        self.config = config
        self.timeout = timeout

    async def chat(self, system: str, user: str, *, temperature: float = 0.2,
                   json_mode: bool = True) -> str:
        if self.config.is_openai_compat:
            url, headers, payload = build_openai_compat_request(
                self.config, system, user, temperature=temperature, json_mode=json_mode
            )
        else:
            url, headers, payload = build_ollama_chat_request(
                self.config, system, user, temperature=temperature, json_mode=json_mode
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                r = await client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                raise AiUnavailableError(
                    f"AI transport failure calling {url} (model={self.config.model_name!r}): {e}"
                ) from e

        if r.status_code >= 400:
            raise AiUnavailableError(
                f"AI transport HTTP {r.status_code} from {url} "
                f"(model={self.config.model_name!r}): {r.text[:300]}"
            )

        try:
            body = r.json()
        except ValueError as e:
            raise AiUnavailableError(f"AI returned unparseable JSON envelope from {url}: {e}") from e

        text = extract_response_text(
            "ollama" if not self.config.is_openai_compat else "openai_compat", body
        )
        if not text:
            raise AiUnavailableError(
                f"AI returned an empty assistant message from {url} (model={self.config.model_name!r})"
            )
        return text