r"""Offline tests for the swappable AI chat-client layer (ai_client.py).

No Ollama / proxy needed - mocked transport. These pin the exact provider
routing and envelope shapes so a model swap (orca -> qwen -> whatever's next)
is a one-line config change that CAN'T silently regress the request shape.

Test coverage mirrors this repo's previously-bitten bug classes:
  * envelope extraction: Ollama {"message":{"content"}} vs OpenAI {"choices":[{"message":{"content"}}]}
  * provider routing: openai_compat -> /v1/chat/completions + Bearer; ollama -> /api/chat
  * fail-loud: transport HTTP error / empty text / empty envelope all raise
  * provider config: fallback chain AI_PROVIDER->"ollama", AI_MODEL->OLLAMA_MODEL, etc

Run:
    python workspace/tools/test_ai_client.py
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ai_client  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def raises(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return False
    except ai_client.AiUnavailableError:
        return True


# ---------------------------------------------------------------------------
# ProviderConfig.from_env: fallback chain
# ---------------------------------------------------------------------------
print("== ProviderConfig.from_env (env-var fallback chain) ==")
c = ai_client.ProviderConfig.from_env({})
check("default provider is ollama", c.provider == "ollama", c.provider)
check("default model_name empty", c.model_name == "", repr(c.model_name))

c = ai_client.ProviderConfig.from_env({"AI_PROVIDER": "OPENAI_COMPAT", "AI_BASE_URL": "http://x/v1", "AI_MODEL": "qwen3.6"})
check("env AI_PROVIDER ACCEPTS OpenAI_COMPAT case-insensitively", c.provider == "openai_compat", c.provider)
check("env AI_MODEL overrides", c.model_name == "qwen3.6", c.model_name)
check("is_openai_compat true", c.is_openai_compat)

c = ai_client.ProviderConfig.from_env({"OLLAMA_BASE_URL": "http://h:11434", "OLLAMA_MODEL": "llama3.1:8b"})
check("ollama fallback respected when AI_* not set",
      (c.provider, c.base_url, c.model_name) == ("ollama", "http://h:11434", "llama3.1:8b"),
      c.provider + c.base_url + c.model_name)

c = ai_client.ProviderConfig.from_env({"AI_MODEL": "qwen3.6", "OLLAMA_BASE_URL": "http://h:11434"})
check("AI_BASE_URL falls back to OLLAMA_BASE_URL when unset",
      c.base_url == "http://h:11434", c.base_url)
check("trailing slash stripped from base_url",
      ai_client.ProviderConfig.from_env({"AI_BASE_URL": "http://x/v1/"}).base_url == "http://x/v1")


# ---------------------------------------------------------------------------
# Wire-shape builders (pure, no I/O)
# ---------------------------------------------------------------------------
print("== build_ollama_chat_request ==")
cfg = ai_client.ProviderConfig(provider="ollama", base_url="http://h:11434", model_name="llama3.1:8b")
url, hdrs, payload = ai_client.build_ollama_chat_request(cfg, "SYS", "USER", temperature=0.5, json_mode=True)
check("ollama URL is /api/chat", url == "http://h:11434/api/chat", url)
check("ollama payload uses model field", payload["model"] == "llama3.1:8b", payload["model"])
check("ollama json_mode sets format=json", payload.get("format") == "json", str(payload.get("format")))
check("ollama options temperature", payload["options"]["temperature"] == 0.5, str(payload["options"]))

url, hdrs, payload = ai_client.build_ollama_chat_request(cfg, "SYS", "USER", temperature=0.5, json_mode=False)
check("ollama json_mode=False omits format key", "format" not in payload)

# ---------------------------------------------------------------------------
print("== build_openai_compat_request ==")
cfg = ai_client.ProviderConfig(provider="openai_compat", base_url="http://agent/v1", api_key="sk-abc", model_name="qwen3.6")
url, hdrs, payload = ai_client.build_openai_compat_request(cfg, "SYS", "USER", temperature=0.7, json_mode=True)
check("openai_compat URL is /chat/completions (NOT double /v1 when base already ends with /v1)",
      url == "http://agent/v1/chat/completions", url)
check("Authorization Bearer header set", hdrs.get("Authorization") == "Bearer sk-abc", str(hdrs))
check("payload has temperature (not options.temperature)", payload["temperature"] == 0.7 and "options" not in payload)
check("json_mode sets response_format json_object", payload.get("response_format") == {"type": "json_object"}, str(payload.get("response_format")))
check("openai_compat payload model field", payload["model"] == "qwen3.6", payload["model"])

cfg_stripped = ai_client.ProviderConfig(provider="openai_compat", base_url="http://agent", model_name="qwen3.6")
url, hdrs, payload = ai_client.build_openai_compat_request(cfg_stripped, "SYS", "USER", temperature=0.7, json_mode=True)
check("openai_compat auto-inserts /v1 when base does not end with it",
      url == "http://agent/v1/chat/completions", url)

url, hdrs, payload = ai_client.build_openai_compat_request(cfg, "SYS", "USER", temperature=0.7, json_mode=False)
check("openai_compat json_mode=False omits response_format", "response_format" not in payload)

cfg_no_key = ai_client.ProviderConfig(provider="openai_compat", base_url="http://agent/v1", model_name="qwen3.6")
url, hdrs, payload = ai_client.build_openai_compat_request(cfg_no_key, "SYS", "USER", temperature=0.1, json_mode=True)
check("no API key -> no Authorization header", "Authorization" not in hdrs, str(hdrs))


# ---------------------------------------------------------------------------
# extract_response_text (envelope normalisation)
# ---------------------------------------------------------------------------
print("== extract_response_text (Ollama vs OpenAI envelopes) ==")
ollama_env = {"message": {"content": "hello world"}}
check("ollama envelope extracted", ai_client.extract_response_text("ollama", ollama_env) == "hello world")
check("ollama empty message content -> empty string", ai_client.extract_response_text("ollama", {"message": {}}) == "")

openai_env = {"choices": [{"message": {"content": "hello from qwen"}}]}
check("openai envelope extracted", ai_client.extract_response_text("openai_compat", openai_env) == "hello from qwen")
check("openai empty choices -> empty string", ai_client.extract_response_text("openai_compat", {"choices": []}) == "")
check("openai missing message key -> empty string", ai_client.extract_response_text("openai_compat", {"choices": [{"index": 0}]}) == "")


# ---------------------------------------------------------------------------
# ChatClient.chat with mocked httpx (same wire-format verification as live)
# ---------------------------------------------------------------------------
print("== ChatClient.chat transport (mocked httpx) ==")


class FakeResp:
    def __init__(self, status: int = 200, text: str = "", json_data: dict | None = None):
        self.status_code = status
        self.text = text
        self._json = json_data
    def json(self):
        if self._json is None:
            raise ValueError("no body")
        return self._json


class FakeClient:
    last_url: str = ""
    last_headers: dict = {}
    last_json: dict = {}
    canned: FakeResp | None = None

    async def __aenter__(self):
        return self
    async def __aexit__(self, *exc):
        return None
    async def post(self, url, json=None, headers=None):
        cls = type(self)
        cls.last_url = url
        cls.last_headers = headers or {}
        cls.last_json = json or {}
        return cls.canned


def run(coro):
    return asyncio.run(coro)


# monkeypatch httpx.AsyncClient used inside ai_client
_orig_async_client = ai_client.httpx.AsyncClient

ai_client.httpx.AsyncClient = lambda timeout=None: FakeClient()

# Ollama end-to-end
FakeClient.canned = FakeResp(json_data={"message": {"content": "R"}})
cfg_ollama = ai_client.ProviderConfig(provider="ollama", base_url="http://h:11434", model_name="llama3.1:8b")
cl = ai_client.ChatClient(cfg_ollama)
txt = run(cl.chat("S", "U", temperature=0.3, json_mode=False))
check("ollama ChatClient returns content text", txt == "R", txt)
check("ollama uses /api/chat URL", FakeClient.last_url.endswith("/api/chat"), FakeClient.last_url)

# OpenAI-compat end-to-end
FakeClient.canned = FakeResp(json_data={"choices": [{"message": {"content": "T"}}]})
cfg_oai = ai_client.ProviderConfig(provider="openai_compat", base_url="http://agent/v1", api_key="sk-a", model_name="qwen3.6")
cl = ai_client.ChatClient(cfg_oai)
txt = run(cl.chat("S", "U", temperature=0.3, json_mode=True))
check("openai_compat ChatClient returns content text", txt == "T", txt)
check("openai_compat URL hits /v1/chat/completions", FakeClient.last_url.endswith("/v1/chat/completions"), FakeClient.last_url)
check("openai_compat sent Authorization header", FakeClient.last_headers.get("Authorization") == "Bearer sk-a", str(FakeClient.last_headers))
check("openai_compat sends response_format on json_mode", FakeClient.last_json.get("response_format") == {"type": "json_object"})

# Fail-loud surfaces
FakeClient.canned = FakeResp(status=500, text="boom")
cl = ai_client.ChatClient(cfg_oai)
check("HTTP >=400 raises AiUnavailableError", raises(run, cl.chat("S", "U")))

FakeClient.canned = FakeResp(json_data={"message": {"content": ""}})
cl = ai_client.ChatClient(cfg_ollama)
check("empty content raises AiUnavailableError", raises(run, cl.chat("S", "U")))

FakeClient.canned = FakeResp(json_data={"weird": "envelope"})
cl = ai_client.ChatClient(cfg_oai)
check("missing choices in openai envelope raises AiUnavailableError", raises(run, cl.chat("S", "U")))

ai_client.httpx.AsyncClient = _orig_async_client  # restore


# ---------------------------------------------------------------------------
# resolve_secret: precedence chain (FILE_PATH > POSTGRES > ENV_VAR, none -> exception)
# Pure + testable without real I/O: `_resolve_secret_core` takes fake callables.
# ---------------------------------------------------------------------------
print("== resolve_secret: FILE_PATH > POSTGRES > ENV_VAR precedence chain ==")


def fake_file_reader(known: dict):
    def _read(path: str) -> str:
        if path in known:
            return known[path]
        raise FileNotFoundError(path)
    return _read


def fake_pg_lookup(known: dict):
    def _lookup(key: str) -> str | None:
        return known.get(key)
    return _lookup


# FILE_PATH wins when set + file exists
val, source = ai_client._resolve_secret_core(
    env={"AI_API_KEY_FILE": "/run/secrets/ai_api_key"},
    # the FILE_PATH lookup: found
    read_file=fake_file_reader({"/run/secrets/ai_api_key": "sk-from-file"}),
    lookup_pg=fake_pg_lookup({}),  # nothing there - must not matter
    env_var_name="AI_API_KEY",
)
check("FILE_PATH set + file readable -> file value wins", val == "sk-from-file", repr(val))
check("FILE_PATH set + file readable -> source reported as FILE_PATH", source == "FILE_PATH", source)

# FILE_PATH set but NOT readable -> fall through to Postgres
val, source = ai_client._resolve_secret_core(
    env={"AI_API_KEY_FILE": "/nonexistent/path", "AI_API_KEY": "sk-from-env"},
    read_file=fake_file_reader({}),  # path not found for any path
    lookup_pg=fake_pg_lookup({"openai.api_keys": "sk-from-pg"}),
    env_var_name="AI_API_KEY",
)
check("FILE_PATH set but unreadable -> falls through to Postgres", val == "sk-from-pg", repr(val))
check("FILE_PATH-but-dead -> source reported as POSTGRES", source == "POSTGRES", source)

# NO FILE_PATH, Postgres has it -> Postgres wins over env var
val, source = ai_client._resolve_secret_core(
    env={"AI_API_KEY": "sk-from-env"},
    read_file=fake_file_reader({}),
    lookup_pg=fake_pg_lookup({"openai.api_keys": "sk-from-pg"}),
    env_var_name="AI_API_KEY",
)
check("NO file_path, Postgres row exists -> Postgres wins over env var", val == "sk-from-pg", repr(val))
check("NO file_path, Postgres row -> source reported as POSTGRES", source == "POSTGRES", source)

# NO FILE_PATH, NO Postgres, env var set -> env fallback works (legacy)
val, source = ai_client._resolve_secret_core(
    env={"AI_API_KEY": "sk-from-env"},
    read_file=fake_file_reader({}),
    lookup_pg=fake_pg_lookup({}),  # nothing in Postgres row store
    env_var_name="AI_API_KEY",
)
check("NO file_path, NO Postgres, env var set -> env fallback", val == "sk-from-env", repr(val))
check("env fallback -> source reported as ENV_VAR", source == "ENV_VAR", source)

# NOTHING available -> raise AiUnavailableError (fail-loud, never return None silently)
try:
    ai_client._resolve_secret_core(
        env={},  # no AI_API_KEY, no AI_API_KEY_FILE
        read_file=fake_file_reader({}),
        lookup_pg=fake_pg_lookup({}),
        env_var_name="AI_API_KEY",
    )
    check("NO file, NO Postgres, NO env -> AiUnavailableError raised", False, "returned without raising")
except ai_client.AiUnavailableError:
    check("NO file, NO Postgres, NO env -> AiUnavailableError raised", True)

# Postgres overrides env even when postgrestest yields empty string (empty = don't-fallthrough)
val, source = ai_client._resolve_secret_core(
    env={"AI_API_KEY": "sk-from-env"},
    read_file=fake_file_reader({}),
    lookup_pg=fake_pg_lookup({"openai.api_keys": ""}),  # empty is looked up but treats as fallback
    env_var_name="AI_API_KEY",
)
check("Postgres empty string treated as absent -> env fallback", val == "sk-from-env", repr(val))
check("Postgres-empty-column -> source ENV_VAR", source == "ENV_VAR", source)


print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")