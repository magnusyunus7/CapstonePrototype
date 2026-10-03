"""SIMCC structured question request form - standalone FastAPI service.

Replaces the free-text chat textbox for question generation with 5 dropdowns
(Grade, Topic, Skill, Difficulty, Quantity). Question format is locked to MCQ
and is NOT shown in the UI. Topic options are filtered by selected grade from
workspace/schema/topic_catalog.json (extracted from real MOE syllabus PDFs).

Endpoints:
    GET  /                      HTML form (static/index.html)
    GET  /api/catalog           full dropdown model (grades->topics + enums)
    GET  /api/catalog/{grade}   topic options for one grade
    POST /api/submit            validate a structured request only (no LLM)
    POST /api/generate          validate + generate the question sheet
    GET  /healthz               health

Run (dev):
    uvicorn app:app --host 0.0.0.0 --port 5005
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]  # 03-school-chatbot/
STATIC = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# AI provider surface - one swappable layer for all 3 stages (generator,
# validator, orchestrator). Defaults mirror simcc_pipe's Valves so a model
# swap is a config/env change, never a code change.
#   AI_PROVIDER   "ollama" (local /api/chat) OR "openai_compat" (corporate
#                 proxy /v1/chat/completions, e.g. qwen3.6 via LiteLLM).
#   AI_BASE_URL   Chat-completions base URL (override). Falls back to
#                 OLLAMA_BASE_URL when unset.
#   AI_API_KEY_FILE  Path to a vault file holding the bearer token (dpaste -
#                  the source of truth is secrets/ai_api_key mounted to
#                  /run/secrets/simcc/ai_api_key). NEVER put a literal
#                  `sk-...`/key value in .env or this file - only the path.
#   AI_MODEL       chat model tag (overrides OLLAMA_MODEL).
# ---------------------------------------------------------------------------
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
AI_PROVIDER = (os.environ.get("AI_PROVIDER") or "ollama").strip().lower()
AI_BASE_URL = (os.environ.get("AI_BASE_URL") or OLLAMA_BASE_URL).rstrip("/")
AI_API_KEY_FILE = os.environ.get("AI_API_KEY_FILE", "")
AI_MODEL = os.environ.get("AI_MODEL") or OLLAMA_MODEL

TEMPERATURE_GEN = 0.7

# RAG grounding (Qdrant + Ollama embeddings) - mirrors simcc_pipe's valves.
ENABLE_RAG = True
RAG_TOP_K_EACH = 3

# Cache of local Ollama model tags (populated lazily by _is_ollama_model) so we
# can route a UI-selected model to Ollama vs the provider proxy by its source.
_OLLAMA_MODELS_CACHE: set | None = None

# rag_client lives in workspace/tools/, not in this app's own folder. Resolve
# it the same way _find_catalog does - works locally (03-school-chatbot layout)
# and inside the Docker container (single-folder /opt/simcc layout).
for _tools_dir in (
    ROOT / "workspace" / "tools",
    Path(__file__).resolve().parent / "workspace" / "tools",
    Path("/opt/simcc/workspace/tools"),
):
    if _tools_dir.is_dir():
        sys.path.insert(0, str(_tools_dir))
        break

try:
    import rag_client  # type: ignore
except ImportError:  # pragma: no cover - fail loud at request time, not import time
    rag_client = None

try:
    import ai_client  # type: ignore
except ImportError:  # pragma: no cover
    ai_client = None


def _grounding_query_text(spec: dict, extra: str = "") -> str:
    return f"{spec['grade']} {spec['topic_name']} ({spec['topic']}) {spec['theme']} {extra}".strip()


def _build_grounding_block_for_spec(spec: dict, extra: str = "") -> str:
    """Retrieve grounding chunks (just the formatted block string, easy to inject
    into any prompt) for the given structured spec."""
    if rag_client is None:
        return ""
    try:
        blk = rag_client.build_grounding_block(
            spec["grade"], spec["topic"], _grounding_query_text(spec, extra),
            top_k_each=RAG_TOP_K_EACH,
        )
        return blk.get("block", "")
    except rag_client.RagUnavailableError:
        return "<!-- grounding unavailable; treat as ungrounded and decline to fabricate citations -->"


async def _check_originality(q: dict) -> dict:
    """Live originality check for one generated question against the school-papers
    RAG collection. Returns a serialisable {similarity, verdict, groundings} dict."""
    if rag_client is None:
        return {"similarity": 0.0, "verdict": "unavailable", "groundings": [], "reason": "rag_client not importable"}
    stem_text = (q.get("stem") or "") + " " + " ".join(
        o.get("text", "") for o in (q.get("options") or []) if isinstance(o, dict)
    )
    try:
        result = rag_client.school_paper_similarity(stem_text)
    except rag_client.RagUnavailableError as e:
        return {"similarity": 0.0, "verdict": "unavailable", "groundings": [], "reason": str(e)}
    return {
        "similarity": result.get("aggregate_similarity", 0.0),
        "verdict": result.get("verdict", "pass"),
        "groundings": result.get("groundings", []),
    }

VALID_SKILLS = ["Knowledge", "Understanding", "Application", "Reasoning"]
VALID_DIFFICULTIES = ["Easy", "Medium", "Hard"]
QUANTITY_MIN, QUANTITY_MAX = 1, 20
GRADE_RE = re.compile(r"^(P[3-6]|S[12])$")
CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,6}$")

app = FastAPI(title="SIMCC Question Request Form", version="1.1")


# ---------------------------------------------------------------- catalogue --
def _find_catalog() -> Path:
    candidates = [
        ROOT / "workspace" / "schema" / "topic_catalog.json",
        Path(__file__).resolve().parent / "workspace" / "schema" / "topic_catalog.json",
        Path("/opt/simcc/workspace/schema/topic_catalog.json"),
    ]
    for c in candidates:
        if c.exists():
            return c
    raise RuntimeError(f"topic_catalog.json not found in any of: {[str(x) for x in candidates]}")


CATALOG_PATH = _find_catalog()


def _load_catalog() -> dict:
    with open(CATALOG_PATH, encoding="utf-8") as f:
        return json.load(f)


CATALOG = _load_catalog()


def topics_for_grade(grade: str) -> list[dict]:
    if grade in ("P3", "P4", "P5", "P6"):
        return CATALOG.get("grades", {}).get(grade, [])
    if grade in ("S1", "S2"):
        return CATALOG.get("grades_lower_secondary_shared", {}).get("topics", [])
    return []


VALID_GRADES = ["P3", "P4", "P5", "P6", "S1", "S2"]


# ------------------------------------------------------------------ models --
class GenerateRequest(BaseModel):
    grade: str
    topic_code: str
    skill: str
    difficulty: str
    quantity: int = Field(default=5)
    model: str | None = Field(default=None)  # optional model override (from /api/models)

    @field_validator("grade")
    @classmethod
    def _grade_ok(cls, v: str) -> str:
        v = v.strip().upper()
        if not GRADE_RE.match(v):
            raise ValueError(f"grade must be one of {VALID_GRADES}, got {v!r}")
        return v

    @field_validator("topic_code")
    @classmethod
    def _code_ok(cls, v: str) -> str:
        v = v.strip().upper()
        if not CODE_RE.match(v):
            raise ValueError(f"topic_code must match {CODE_RE.pattern}, got {v!r}")
        return v

    @field_validator("skill")
    @classmethod
    def _skill_ok(cls, v: str) -> str:
        if v not in VALID_SKILLS:
            raise ValueError(f"skill must be one of {VALID_SKILLS}, got {v!r}")
        return v

    @field_validator("difficulty")
    @classmethod
    def _diff_ok(cls, v: str) -> str:
        if v not in VALID_DIFFICULTIES:
            raise ValueError(f"difficulty must be one of {VALID_DIFFICULTIES}, got {v!r}")
        return v

    @field_validator("quantity")
    @classmethod
    def _qty_ok(cls, v: int) -> int:
        if not (QUANTITY_MIN <= v <= QUANTITY_MAX):
            raise ValueError(f"quantity must be {QUANTITY_MIN}..{QUANTITY_MAX}, got {v}")
        return v


# ------------------------------------------------------------- generation ----
GENERATOR_SYS = """You are the SIMCC Science Question Generator (Singapore MOE syllabus).
You receive ONE fully-specified request and must produce EXACTLY ONE original
MCQ question object. All fields are already chosen and must NOT be changed. Do
NOT ask for clarification. Do NOT output a list, preamble, or any prose. Output
ONLY one JSON object (no fences, no wrapping).

Contract (keys must match exactly):

{
  "question_id": "Q-<GRADE>-<TOPICCODE>-<6 uppercase alnum>",
  "grade": "<grade>", "topic": "<topic code>", "topic_name": "<topic name>",
  "skill": "<skill>", "difficulty": "<difficulty>", "format": "MCQ",
  "stem": "<question text, plain string>",
  "options": [{"label":"A","text":"..."},{"label":"B","text":"..."},{"label":"C","text":"..."},{"label":"D","text":"..."}],
  "answer_key": "<A|B|C|D>",
  "explanation": "<why the correct answer is correct AND why each other option is wrong>",
  "curriculum_mapping": {
    "objective_text": "<which MOE learning objective this assesses>",
    "objective_source": "<citation of the MOE syllabus topic for this grade>"
  },
  "generation_record": {
    "sources": ["<grounded source ids if known, else 'internal'>"],
    "informed_by": "<one line: what pattern shaped this question>",
    "generated_by": "simcc question-form generator"
  },
  "review": {"status": "draft", "reviewed_by": null, "reviewed_at": null, "changed": null}
}

Rules:
1. Exactly 4 options labelled A, B, C, D. Exactly one correct per `answer_key`.
   No "all of the above". Every incorrect option must be plausible but clearly
   wrong once the explanation is read.
2. Grade-appropriate Singapore academic English, familiar local/household
   contexts where natural, metric units where natural.
3. Skill=Knowledge->recall; Understanding->explain in own words;
   Application->apply concept to a new concrete scenario; Reasoning->multi-step
   cause-effect / predict / justify.
4. Difficulty=Hard means genuinely challenging for that grade ceiling (not just
   longer wording) - Application/Reasoning-level thinking at the grade's peak.
5. `stem` is a plain string (no markdown, no backticks). Return ONLY the JSON
   object. No prose, no fences, no extra keys.
6. Never approve your own output: `review.status` must stay "draft"."""


def _extract_json_obj_or_arr(raw: str):
    """Pull a top-level JSON object OR array out of raw LLM output. May be
    fenced in ``` markers, may start with prose, and may contain nested
    brackets inside strings (e.g. option text containing '['). Scans for '{'
    then '[' using json.JSONDecoder().raw_decode until a full self-describing
    value parses cleanly. Returns the parsed value (dict or list)."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text).strip()
    decoder = json.JSONDecoder()
    for opener in ("{", "["):
        idx = 0
        while True:
            start = text.find(opener, idx)
            if start == -1:
                break
            try:
                value, end_idx = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                idx = start + 1
                continue
            rest = text[end_idx:].strip()
            if rest and not rest.startswith((",", "}", "]")):
                idx = start + 1
                continue
            return value
    raise ValueError(f"no JSON object/array found in model output (starts: {raw[:60]!r})")


async def _is_ollama_model(model_name: str) -> bool:
    """Does this model tag live on the LOCAL Ollama server (vs the configured
    provider proxy)? The /api/models dropdown lists both sources, but a model
    name alone doesn't say where to route it. We resolve that here by checking
    Ollama's own tag list. Cached per-process to avoid a round-trip per call."""
    if not model_name:
        return False
    global _OLLAMA_MODELS_CACHE
    if _OLLAMA_MODELS_CACHE is None:
        names: set[str] = set()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
                if r.status_code == 200:
                    names = {m.get("name") for m in (r.json().get("models") or []) if m.get("name")}
        except (httpx.HTTPError, ValueError):
            names = set()
        _OLLAMA_MODELS_CACHE = names
    return model_name in _OLLAMA_MODELS_CACHE


async def _call_llm_json(system: str, user: str, *, temperature: float = TEMPERATURE_GEN, model_override: str | None = None):
    """Provider-aware chat-completions call via the shared `ai_client` layer
    (workspace/tools/ai_client.py). Works against BOTH Ollama's /api/chat and
    OpenAI-compatible /v1/chat/completions endpoints. JSON-only: we gold-string
    parse whatever comes back via the defensive `_extract_json_obj_or_arr` so
    the model need not honour `format=json`/`response_format` strictly.

    ROUTING: the /api/models dropdown lists BOTH the corp-proxy models and the
    local Ollama models. A model name alone doesn't say where it lives, so we
    route by SOURCE: if the chosen model is a local Ollama tag, we build an
    Ollama config (provider=ollama, base_url=OLLAMA_BASE_URL); otherwise we use
    the configured provider (AI_PROVIDER/AI_BASE_URL, e.g. the corp qwen proxy).
    Without this, picking a llama model would (wrongly) POST to the qwen proxy
    and fail. With no override, we fall back to the configured default model."""
    chosen_model = model_override or AI_MODEL
    if model_override and await _is_ollama_model(model_override):
        # Route to local Ollama, not the configured provider proxy.
        cfg = ai_client.ProviderConfig(
            provider="ollama",
            base_url=OLLAMA_BASE_URL,
            api_key="",  # Ollama ignores auth
            model_name=chosen_model,
        )
    else:
        resolved_key, _key_source = ai_client.resolve_secret(env_var_name="AI_API_KEY")
        cfg = ai_client.ProviderConfig(
            provider=AI_PROVIDER,
            base_url=AI_BASE_URL,
            api_key=resolved_key,
            model_name=chosen_model,
        )
    client = ai_client.ChatClient(cfg, timeout=None)
    raw = await client.chat(system, user, temperature=temperature, json_mode=True)
    return _extract_json_obj_or_arr(raw)


async def _generate_n_questions(spec: dict) -> list[dict]:
    """Call LLM once per question (large JSON-reliable models return ONE JSON
    object per call when asked to write one question, not a list). Returns up to
    `quantity` question dicts, in request order."""
    grounding_block = ""
    if ENABLE_RAG and rag_client is not None:
        grounding_block = _build_grounding_block_for_spec(spec)
    common_prompt = (
        f"EXACT REQUEST SPEC (do not alter any field):\n"
        f"grade={spec['grade']}, topic code={spec['topic']}, "
        f"topic name={spec['topic_name']}, theme={spec['theme']},\n"
        f"skill={spec['skill']}, difficulty={spec['difficulty']},\n"
        f"format=MCQ (locked, do not change).\n\n"
        f"{grounding_block}\n\n"
        f"Generate ONE original MCQ question for this exact spec. "
        f"This is question number {{N}} of {{TOTAL}} in a set - make its angle, "
        f"context, and tested concept DIFFERENT from the others in the set "
        f"(spread across distinct sub-concepts/contexts of this topic). "
        f"Return ONLY the JSON object per the contract."
    )

    out: list[dict] = []
    total = spec["quantity"]
    for n in range(1, total + 1):
        user_msg = common_prompt.replace("{N}", str(n)).replace("{TOTAL}", str(total))
        obj = await _call_llm_json(GENERATOR_SYS, user_msg, model_override=spec.get("model"))
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            obj = obj[0]
        if isinstance(obj, dict) and obj.get("stem"):
            out.append(obj)
    return out


def _build_spec(req: GenerateRequest) -> tuple[dict, dict]:
    valid_codes = {t["code"] for t in topics_for_grade(req.grade)}
    if req.topic_code not in valid_codes:
        readable = sorted(t["name"] for t in topics_for_grade(req.grade))
        raise HTTPException(
            status_code=422,
            detail=(
                f"topic_code {req.topic_code!r} is not valid for grade {req.grade}. "
                f"Valid topics for {req.grade}: {readable}."
            ),
        )
    chosen = next(t for t in topics_for_grade(req.grade) if t["code"] == req.topic_code)
    spec = {
        "request_id": str(uuid.uuid4())[:8],
        "grade": req.grade,
        "topic": req.topic_code,
        "topic_name": chosen["name"],
        "theme": chosen.get("theme", ""),
        "skill": req.skill,
        "difficulty": req.difficulty,
        "format": "MCQ",
        "quantity": req.quantity,
        "model": req.model,
    }
    return spec, chosen


# ------------------------------------------------------------------- routes --
@app.get("/api/catalog")
def api_catalog() -> JSONResponse:
    return JSONResponse({
        "grades": VALID_GRADES,
        "skill": VALID_SKILLS,
        "difficulty": VALID_DIFFICULTIES,
        "quantity": {"min": QUANTITY_MIN, "max": QUANTITY_MAX, "default": 5},
        "format": ["MCQ"],
        "topics": {g: topics_for_grade(g) for g in VALID_GRADES},
    })


@app.get("/api/catalog/{grade}")
def api_catalog_grade(grade: str) -> JSONResponse:
    grade = grade.strip().upper()
    topics = topics_for_grade(grade)
    if not topics and grade not in ("S1", "S2"):
        raise HTTPException(status_code=404, detail=f"no topics for grade {grade!r}")
    return JSONResponse({"grade": grade, "topics": topics})


@app.get("/api/models")
async def api_models() -> JSONResponse:
    """Enumerate every chat-completion model available on the configured AI
    provider (OpenAI-compatible /v1/models) plus local Ollama (llama3.1 etc.),
    so the UI dropdown can list them all without hardcoding. Returns
    {"models": [{"id","label","source"}...]} - never leaks API keys."""
    resolved_key, _key_source = ai_client.resolve_secret(env_var_name="AI_API_KEY")
    out: list[dict] = []

    # 1) OpenAI-compatible provider (corp proxy, LiteLLM, OpenRouter, etc.)
    try:
        headers = {"Authorization": f"Bearer {resolved_key}"} if resolved_key else {}
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{AI_BASE_URL}/models", headers=headers)
            if r.status_code == 200:
                for m in (r.json().get("data") or []):
                    mid = m.get("id")
                    if mid:
                        out.append({"id": mid, "label": mid, "source": "provider"})
    except (httpx.HTTPError, ValueError):
        pass  # provider unreachable or unparseable - skip; ollama fallback below may still work.

    # 2) Local Ollama (read-only /api/tags)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            if r.status_code == 200:
                for m in (r.json().get("models") or []):
                    name = m.get("name")
                    if name:
                        out.append({"id": name, "label": name, "source": "ollama"})
    except (httpx.HTTPError, ValueError):
        pass

    if not out:
        # Always surface at least the configured default so the dropdown is never empty.
        out.append({"id": AI_MODEL, "label": AI_MODEL, "source": "default"})
    return JSONResponse({"models": out})


@app.post("/api/submit")
async def api_submit(req: GenerateRequest) -> JSONResponse:
    """Validate only (no LLM call). Same guard as /api/generate."""
    spec, _ = _build_spec(req)
    return JSONResponse({"ok": True, "spec": {**spec, "status": "validated_spec_ready_for_generator"}})


@app.post("/api/generate")
async def api_generate(req: GenerateRequest) -> JSONResponse:
    """Validate the structured request AND generate the MCQ sheet (this is the
    endpoint the form calls to actually produce the questions). The model
    yields exactly ONE question object per call - so we loop `quantity` times
    and collect them."""
    spec, _ = _build_spec(req)

    try:
        questions = await _generate_n_questions(spec)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ollama request failed: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"model returned unparseable output: {e}") from e

    cleaned = []
    for q in questions[: spec["quantity"]]:
        if not isinstance(q, dict):
            continue
        if not q.get("stem") or not isinstance(q.get("options"), list):
            continue
        q["question_id"] = (
            f"Q-{spec['grade']}-{spec['topic']}-"
            f"{uuid.uuid4().hex[:6].upper()}"
        )
        q.setdefault("review", {"status": "draft", "reviewed_by": None, "reviewed_at": None, "changed": None})
        q["review"]["status"] = "draft"  # AI never approves its own work
        if ENABLE_RAG and rag_client is not None:
            q["originality"] = await _check_originality(q)
        cleaned.append(q)

    spec_out = {**spec, "status": "generated"}
    return JSONResponse({"ok": True, "spec": spec_out, "questions": cleaned})


@app.get("/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "catalog_loaded": bool(CATALOG)})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")