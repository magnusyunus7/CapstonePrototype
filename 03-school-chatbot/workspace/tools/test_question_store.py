"""Offline tests for question_store.py (temp SQLite file; no Docker/Qdrant/LLM).

    python workspace/tools/test_question_store.py
"""
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import question_store as qs  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def spec(rid="req00001", qty=2, **over):
    s = {"request_id": rid, "grade": "P5", "topic": "ELEC", "topic_name": "Electrical System",
         "skill": "Application", "difficulty": "Hard", "format": "MCQ", "quantity": qty, "model": None}
    s.update(over)
    return s


def question(qid="Q-P5-ELEC-AAAAAA", **over):
    q = {
        "question_id": qid, "grade": "P5", "topic": "ELEC", "topic_name": "Electrical System",
        "skill": "Application", "difficulty": "Hard", "format": "MCQ",
        "stem": "A circuit has a 3 \u03a9 resistor and a 6 V cell. What happens when a second bulb is added in series?",
        "options": [{"label": "A", "text": "Brighter"}, {"label": "B", "text": "Dimmer"},
                    {"label": "C", "text": "Same"}, {"label": "D", "text": "Off"}],
        "answer_key": "B", "explanation": "More resistance, less current.",
        "curriculum_mapping": {"objective_text": "Series circuits", "objective_source": "Primary Science Syllabus 2023.pdf p.60"},
        "generation_record": {"sources": ["internal"], "informed_by": "pattern X", "generated_by": "form generator"},
        "review": {"status": "draft", "reviewed_by": None, "reviewed_at": None, "changed": None},
        "originality": {"similarity": 0.12, "verdict": "pass", "groundings": [{"source": "q.pdf", "page": 8}]},
    }
    q.update(over)
    return q


def fresh() -> qs.QuestionStore:
    return qs.QuestionStore(Path(tempfile.mkdtemp()) / "sub" / "q.db")  # also proves parent dir is created


def rows(store, sql, *a):
    c = sqlite3.connect(store.path)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


print("== init ==")
s = fresh()
names = {r["name"] for r in rows(s, "SELECT name FROM sqlite_master WHERE type='table'")}
check("both tables exist", {"generation_requests", "questions"} <= names, names)
s.init()
check("init is idempotent", True)
check("user_version set", rows(s, "PRAGMA user_version")[0][0] == qs.SCHEMA_VERSION)

print("\n== save + read back ==")
s = fresh()
q1, q2 = question("Q-P5-ELEC-AAAAAA"), question("Q-P5-ELEC-BBBBBB", difficulty="Hard", stem="Second stem")
src = [{"source": "Primary Science Syllabus 2023.pdf", "page_start": 60, "page_end": 61, "score": 0.81}]
res = s.save_generation(spec(), [q1, q2], grounding_sources=src, model="qwen3.6")
check("reports 2 saved", res["saved"] == 2 and res["question_ids"] == ["Q-P5-ELEC-AAAAAA", "Q-P5-ELEC-BBBBBB"], res)
got = s.get_question("Q-P5-ELEC-AAAAAA")
check("stem round-trips incl. unicode (\u03a9)", got["stem"] == q1["stem"] and "\u03a9" in got["stem"])
check("options round-trip", got["options"] == q1["options"])
check("originality block preserved (field the JSON schema doesn't list)", got["originality"]["verdict"] == "pass")
check("request_id + created_at attached", got["request_id"] == "req00001" and bool(got["created_at"]))
r = rows(s, "SELECT * FROM questions WHERE question_id='Q-P5-ELEC-AAAAAA'")[0]
check("queryable columns filled", (r["objective_source"], r["answer_key"], r["originality_verdict"], r["originality_similarity"])
      == ("Primary Science Syllabus 2023.pdf p.60", "B", "pass", 0.12))
check("llm sources kept separate from grounding", json.loads(r["llm_sources_json"]) == ["internal"])
rq = s.get_request("req00001")
check("request row: model + grounding hits + question ids",
      rq["model"] == "qwen3.6" and rq["grounding_sources"] == src and rq["question_ids"] == res["question_ids"]
      and rq["quantity_requested"] == 2 and rq["quantity_generated"] == 2, rq)
check("unknown request -> None", s.get_request("nope") is None and s.get_question("nope") is None)

print("\n== draft invariant (AI never approves) ==")
s = fresh()
bad = question("Q-P5-ELEC-CCCCCC", review={"status": "approved", "reviewed_by": "AI", "reviewed_at": "now", "changed": None})
s.save_generation(spec("req00002", 1), [bad])
check("incoming 'approved' stored as draft", s.get_question("Q-P5-ELEC-CCCCCC")["review"]["status"] == "draft")
check("DB column is draft", rows(s, "SELECT review_status FROM questions")[0][0] == "draft")
nullrev = question("Q-P5-ELEC-DDDDDD", review=None)
s.save_generation(spec("req00003", 1), [nullrev])
check("review=null from model handled -> draft", s.get_question("Q-P5-ELEC-DDDDDD")["review"]["status"] == "draft")
try:
    c = sqlite3.connect(s.path)
    c.execute("UPDATE questions SET review_status='maybe'")
    ok = False
except sqlite3.IntegrityError:
    ok = True
finally:
    c.close()
check("DB rejects a review_status outside the schema enum", ok)

print("\n== grounding NULL vs [] ==")
s = fresh()
s.save_generation(spec("req_none", 1), [question("Q-P5-ELEC-EEEEE1")], grounding_sources=None)
s.save_generation(spec("req_empty", 1), [question("Q-P5-ELEC-EEEEE2")], grounding_sources=[])
check("None (unavailable) stays NULL", s.get_request("req_none")["grounding_sources"] is None)
check("[] (nothing found) stays []", s.get_request("req_empty")["grounding_sources"] == [])

print("\n== tolerant of sloppy model output ==")
s = fresh()
sloppy = question("Q-P5-ELEC-FFFFFF", curriculum_mapping="not a dict", generation_record=None, originality=None, options=None)
res = s.save_generation(spec("req00004", 1), [sloppy])
check("non-dict mapping/record/originality + null options still stored", res["saved"] == 1)
check("missing originality -> NULL columns", rows(s, "SELECT originality_verdict FROM questions")[0][0] is None)
weird = question("Q-P5-ELEC-FFFFF2", originality={"similarity": "n/a", "verdict": "pass"})
s.save_generation(spec("req00005", 1), [weird])
check("non-numeric similarity -> NULL, not a crash",
      rows(s, "SELECT originality_similarity FROM questions WHERE question_id='Q-P5-ELEC-FFFFF2'")[0][0] is None)

print("\n== question_id collision never overwrites ==")
s = fresh()
orig = question("Q-P5-ELEC-ZZZZZZ", stem="ORIGINAL")
s.save_generation(spec("req00006", 1), [orig])
dup = question("Q-P5-ELEC-ZZZZZZ", stem="NEWER")
res = s.save_generation(spec("req00007", 1), [dup])
check("colliding id got a fresh id", res["question_ids"][0] != "Q-P5-ELEC-ZZZZZZ" and dup["question_id"] == res["question_ids"][0], res)
check("fresh id still matches the schema pattern",
      re.match(r"^Q-[A-Z0-9]{1,4}-[A-Z0-9]{1,6}-[A-Z0-9]{6}$", res["question_ids"][0]) is not None)
check("original row untouched", s.get_question("Q-P5-ELEC-ZZZZZZ")["stem"] == "ORIGINAL")
check("both rows exist", rows(s, "SELECT COUNT(*) FROM questions")[0][0] == 2)

print("\n== all-or-nothing ==")
s = fresh()
good, nostem = question("Q-P5-ELEC-GGGGG1"), question("Q-P5-ELEC-GGGGG2", stem="")
try:
    s.save_generation(spec("req00008", 2), [good, nostem])
    raised = False
except qs.QuestionStoreError:
    raised = True
check("question with no stem -> QuestionStoreError", raised)
check("nothing written (request row + good question rolled back)",
      rows(s, "SELECT COUNT(*) FROM questions")[0][0] == 0 and rows(s, "SELECT COUNT(*) FROM generation_requests")[0][0] == 0)
s.save_generation(spec("req00009", 1), [question("Q-P5-ELEC-HHHHH1")])
try:
    s.save_generation(spec("req00009", 1), [question("Q-P5-ELEC-HHHHH2")])
    raised = False
except qs.QuestionStoreError:
    raised = True
check("duplicate request_id -> QuestionStoreError", raised)
check("...and the second attempt's question was not stored", s.get_question("Q-P5-ELEC-HHHHH2") is None)

print("\n== list / filter / paginate ==")
s = fresh()
s.save_generation(spec("reqL1", 2), [question("Q-P5-ELEC-LLLLL1"), question("Q-P5-ELEC-LLLLL2")])
s.save_generation(spec("reqL2", 1, grade="P3", topic="LIFE", difficulty="Easy", skill="Knowledge"),
                  [question("Q-P3-LIFE-LLLLL3", grade="P3", topic="LIFE", difficulty="Easy")])
allq = s.list_questions()
check("total counts all", allq["total"] == 3 and len(allq["items"]) == 3)
check("newest first", allq["items"][0]["question_id"] == "Q-P3-LIFE-LLLLL3")
check("filter by grade", s.list_questions(grade="P5")["total"] == 2)
check("filter by grade+difficulty", s.list_questions(grade="P3", difficulty="Easy")["total"] == 1)
check("filter by request_id", [i["question_id"] for i in s.list_questions(request_id="reqL1")["items"]] == ["Q-P5-ELEC-LLLLL2", "Q-P5-ELEC-LLLLL1"])
check("filter on review_status", s.list_questions(review_status="draft")["total"] == 3 and s.list_questions(review_status="approved")["total"] == 0)
pg = s.list_questions(limit=2, offset=2)
check("pagination: total stays 3, page has 1", pg["total"] == 3 and len(pg["items"]) == 1)
check("limit clamped to 1..200", s.list_questions(limit=0)["limit"] == 1 and s.list_questions(limit=10**6)["limit"] == 200)
try:
    s.list_questions(**{"grade; DROP TABLE questions": "x"})
    raised = False
except qs.QuestionStoreError:
    raised = True
check("unknown/injection-style filter name rejected", raised and rows(s, "SELECT COUNT(*) FROM questions")[0][0] == 3)
check("SQL-looking filter VALUE is just a value", s.list_questions(grade="P5' OR '1'='1")["total"] == 0)

print("\n== persistence ==")
s = fresh()
s.save_generation(spec("reqP1", 1), [question("Q-P5-ELEC-PPPPP1")])
s2 = qs.QuestionStore(s.path)
check("a new store object on the same file sees the data", s2.get_question("Q-P5-ELEC-PPPPP1") is not None)

print("\n== config ==")
import os  # noqa: E402
os.environ["QUESTION_DB_PATH"] = "/tmp/custom_q.db"
check("QUESTION_DB_PATH wins", qs.default_db_path() == "/tmp/custom_q.db")
del os.environ["QUESTION_DB_PATH"]
# Portable on Windows/Linux/macOS: a path whose "parent folder" is really a file
# can never be created, whatever the OS or user rights.
_blocker = Path(tempfile.mkdtemp()) / "a_file_not_a_folder"
_blocker.write_text("x")
try:
    qs.QuestionStore(_blocker / "sub" / "q.db")
    raised = False
except qs.QuestionStoreError:
    raised = True
check("unwritable path -> QuestionStoreError (not a bare OSError)", raised)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL QUESTION-STORE TESTS PASSED")