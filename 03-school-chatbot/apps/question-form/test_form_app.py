"""Offline smoke tests for the SIMCC question-form app (no Docker needed).

Covers the exact bugs we just fixed at the product level:
  * P1 is not selectable as a grade at all (P1 isn't in the available set)
  * selecting a grade only exposes topics genuinely in that grade's MOE syllabus
  * the client can't sneak `kinematics` past the dropdown validator directly
  * quantity bounds + MCQ lock (format is derived, not accepted from the client)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Never touch a real question DB from tests.
os.environ["QUESTION_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "form_test.db")

from fastapi.testclient import TestClient
import app as app_module

app = app_module.app

client = TestClient(app)
failures = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


print("== /api/catalog ==")
r = client.get("/api/catalog")
check("200", r.status_code == 200, str(r.status_code))
cat = r.json()
check("no P1 grade exposed", "P1" not in cat["grades"], cat["grades"])
check("no P2 grade exposed", "P2" not in cat["grades"], cat["grades"])
check("no S3/S4 grade exposed", "S3" not in cat["grades"] and "S4" not in cat["grades"], cat["grades"])
check("P3 present", "P3" in cat["grades"], cat["grades"])
check("S1 present", "S1" in cat["grades"], cat["grades"])
check("qty 1-20", cat["quantity"] == {"min": 1, "max": 20, "default": 5}, cat["quantity"])
check("format locked MCQ", cat["format"] == ["MCQ"], cat["format"])

p3_codes = {t["code"] for t in cat["topics"]["P3"]}
p5_codes = {t["code"] for t in cat["topics"]["P5"]}
check("P3 has topics", len(p3_codes) > 0, p3_codes)
check("P3 does NOT have ELEC (kubernetes-level filter)", "ELEC" not in p3_codes, p3_codes)
check("P3 does NOT have FORCES (forces is P6-only)", "FORCES" not in p3_codes, p3_codes)
check("P5 DOES have ELEC (your original ask works at real grade)", "ELEC" in p5_codes, p5_codes)

print("\n== /api/catalog/{grade} specificity ==")
check("P3 dropdown differs from P6 dropdown",
      {t["code"] for t in client.get("/api/catalog/P3").json()["topics"]}
      != {t["code"] for t in client.get("/api/catalog/P6").json()["topics"]})
check("nonexistent P2 returns 404 (invalid grade), not 500",
      client.get("/api/catalog/P2").status_code == 404)
check("P4 dropdown excludes P5-only topics (WATER)",
      "WATER" not in {t["code"] for t in client.get("/api/catalog/P4").json()["topics"]})

print("\n== POST /api/submit: valid ==")
r = client.post("/api/submit", json={
    "grade": "P5", "topic_code": "ELEC", "skill": "Application",
    "difficulty": "Hard", "quantity": 5,
})
check("200", r.status_code == 200, r.text)
check("spec format is MCQ (locked server-side)", r.json()["spec"]["format"] == "MCQ", r.text)
check("spec quantity echoes", r.json()["spec"]["quantity"] == 5)

print("\n== POST /api/submit: server-side rejection (dropdown bypass attempt) ==")
r = client.post("/api/submit", json={
    "grade": "P1", "topic_code": "KINEMATICS", "skill": "Application",
    "difficulty": "Hard", "quantity": 5,
})
check("P1+kinematics rejected", r.status_code in (400, 422), r.text)

r = client.post("/api/submit", json={
    "grade": "P5", "topic_code": "KINEMATICS", "skill": "Application",
    "difficulty": "Hard", "quantity": 5,
})
check("kinematics code rejected even at a real grade", r.status_code in (400, 422), r.text)

r = client.post("/api/submit", json={
    "grade": "P3", "topic_code": "ELEC", "skill": "Application",
    "difficulty": "Hard", "quantity": 5,
})
check("P3+ELEC rejected (ELEC is P5-only, not P3)", r.status_code in (400, 422), r.text)

r = client.post("/api/submit", json={
    "grade": "P5", "topic_code": "ELEC", "skill": "Guesswork",
    "difficulty": "Hard", "quantity": 5,
})
check("bad skill rejected", r.status_code in (400, 422), r.text)

r = client.post("/api/submit", json={
    "grade": "P5", "topic_code": "ELEC", "skill": "Application",
    "difficulty": "Hard", "quantity": 21,
})
check("quantity 21 rejected", r.status_code in (400, 422), r.text)

print("\n== POST /api/generate: shared validation still rejects BEFORE hitting LLM ==")
r = client.post("/api/generate", json={
    "grade": "P1", "topic_code": "KIN", "skill": "Application",
    "difficulty": "Hard", "quantity": 5,
})
check("P1+KIN rejected server-side (no LLM roundtrip)", r.status_code in (400, 422), r.text[:200])

r = client.post("/api/generate", json={
    "grade": "P5", "topic_code": "KIN", "skill": "Application",
    "difficulty": "Hard", "quantity": 5,
})
check("P5+KIN rejected (kinematics is NOT a MOE code)", r.status_code in (400, 422), r.text[:200])

print("\n== POST /api/generate: drafts are stored (LLM + Qdrant stubbed out) ==")


def _fake_q(n, status="draft"):
    return {
        "question_id": "model-invented-id", "grade": "P5", "topic": "ELEC", "topic_name": "Electrical System",
        "skill": "Application", "difficulty": "Hard", "format": "MCQ",
        "stem": f"Stub question {n}: what happens to the bulb brightness?",
        "options": [{"label": l, "text": f"opt {l}"} for l in "ABCD"], "answer_key": "B",
        "explanation": "because", "curriculum_mapping": {"objective_text": "series circuits", "objective_source": "syllabus p.60"},
        "generation_record": {"sources": ["internal"], "informed_by": "x", "generated_by": "stub"},
        "review": {"status": status, "reviewed_by": None, "reviewed_at": None, "changed": None},
    }


_GROUNDING = [{"source": "Primary Science Syllabus 2023.pdf", "page_start": 60, "page_end": 61, "score": 0.8}]


async def _fake_generate(spec):
    return [_fake_q(1), _fake_q(2, status="approved")], _GROUNDING  # 2nd claims 'approved'


async def _fake_originality(q):
    return {"similarity": 0.1, "verdict": "pass", "groundings": []}


_real_generate, _real_orig = app_module._generate_n_questions, app_module._check_originality
app_module._generate_n_questions, app_module._check_originality = _fake_generate, _fake_originality
payload = {"grade": "P5", "topic_code": "ELEC", "skill": "Application", "difficulty": "Hard", "quantity": 2}

r = client.post("/api/generate", json=payload)
body = r.json()
check("generate 200", r.status_code == 200, r.text[:200])
check("storage reported: 2 saved, no error", body.get("storage") == {"saved": 2, "error": None}, body.get("storage"))
check("response still has both questions (UI contract unchanged)", len(body["questions"]) == 2 and body["spec"]["status"] == "generated")
check("a question claiming 'approved' comes back draft", all(q["review"]["status"] == "draft" for q in body["questions"]))
rid = body["spec"]["request_id"]
qids = [q["question_id"] for q in body["questions"]]
check("server ids replaced the model's invented id", all(i.startswith("Q-P5-ELEC-") for i in qids) and len(set(qids)) == 2, qids)

r = client.get("/api/questions")
check("GET /api/questions lists them", r.status_code == 200 and r.json()["total"] == 2, r.text[:200])
check("stored ids == ids the UI was given", sorted(i["question_id"] for i in r.json()["items"]) == sorted(qids))
check("filter grade=P3 -> none", client.get("/api/questions?grade=P3").json()["total"] == 0)
check("filter grade=p5 (case-insensitive) -> 2", client.get("/api/questions?grade=p5").json()["total"] == 2)
check("filter review_status=approved -> none", client.get("/api/questions?review_status=approved").json()["total"] == 0)

r = client.get(f"/api/questions/{qids[0]}")
check("GET one question", r.status_code == 200 and r.json()["stem"].startswith("Stub question 1"), r.text[:200])
check("unknown question -> 404", client.get("/api/questions/Q-P5-ELEC-NOPE00").status_code == 404)

r = client.get(f"/api/requests/{rid}")
rj = r.json()
check("GET request: real retrieval hits recorded", r.status_code == 200 and rj["grounding_sources"] == _GROUNDING, r.text[:200])
check("request lists its questions + model + quantity", rj["question_ids"] == qids and rj["quantity_requested"] == 2 and bool(rj["model"]))
check("unknown request -> 404", client.get("/api/requests/nope").status_code == 404)

client.post("/api/generate", json=payload)
check("a second generation accumulates (4 total)", client.get("/api/questions").json()["total"] == 4)

print("\n== storage fault must not lose a finished generation ==")
_real_get_store = app_module._get_store


def _boom():
    raise RuntimeError("disk full")


app_module._get_store = _boom
r = client.post("/api/generate", json=payload)
body = r.json()
check("still 200 with both questions", r.status_code == 200 and len(body["questions"]) == 2, r.text[:200])
check("failure is reported, not silent", body["storage"]["saved"] == 0 and "disk full" in (body["storage"]["error"] or ""), body["storage"])
check("read endpoints say 503 when the store is down", client.get("/api/questions").status_code == 503)
app_module._get_store = _real_get_store
check("nothing from the failed run was stored", client.get("/api/questions").json()["total"] == 4)
app_module._generate_n_questions, app_module._check_originality = _real_generate, _real_orig

print("\n== real _generate_n_questions: Qdrant hits flow through as provenance (only LLM + Qdrant stubbed) ==")
import asyncio

_calls = []


async def _stub_llm(system, user, *, temperature=0.7, model_override=None):
    _calls.append(user)
    return _fake_q(len(_calls))


_real_llm = app_module._call_llm_json
app_module._call_llm_json = _stub_llm
_spec = {"request_id": "rq000001", "grade": "P5", "topic": "ELEC", "topic_name": "Electrical System",
         "theme": "Energy", "skill": "Application", "difficulty": "Hard", "format": "MCQ", "quantity": 2, "model": None}
_hits = [{"source": "Primary Science Syllabus 2023.pdf", "page_start": 60, "page_end": 61, "score": 0.8}]


class _FakeRag:
    class RagUnavailableError(RuntimeError):
        pass

    mode = "ok"

    @classmethod
    def build_grounding_block(cls, grade, topic, query, top_k_each=3):
        if cls.mode == "down":
            raise cls.RagUnavailableError("qdrant down")
        if cls.mode == "empty":
            return {"block": "", "sources": [], "chunks": []}
        return {"block": "[S1 Primary Science Syllabus 2023.pdf (p. 60)] ...", "sources": _hits, "chunks": []}


_real_rag = app_module.rag_client
app_module.rag_client = _FakeRag

qs_, gs_ = asyncio.run(app_module._generate_n_questions(_spec))
check("returns (questions, sources) tuple", len(qs_) == 2 and gs_ == _hits, gs_)
check("grounding block still injected into every prompt (behaviour unchanged)", all("[S1 Primary Science" in u for u in _calls) and len(_calls) == 2)
_FakeRag.mode = "empty"
check("retrieval ran, found nothing -> [] (not None)", asyncio.run(app_module._generate_n_questions(_spec))[1] == [])
_FakeRag.mode = "down"
qs_, gs_ = asyncio.run(app_module._generate_n_questions(_spec))
check("Qdrant down -> sources None, generation still proceeds", gs_ is None and len(qs_) == 2)
check("...and the unavailable sentinel still reaches the prompt", "grounding unavailable" in _calls[-1])

app_module.rag_client, app_module._call_llm_json = _real_rag, _real_llm

print("\n== GET / ==")
r = client.get("/")
check("index.html served", r.status_code == 200 and "SIMCC Question Generator" in r.text, r.text[:80])

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL FORM-APP TESTS PASSED")