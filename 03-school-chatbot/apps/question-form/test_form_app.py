"""Offline smoke tests for the SIMCC question-form app (no Docker needed).

Covers the exact bugs we just fixed at the product level:
  * P1 is not selectable as a grade at all (P1 isn't in the available set)
  * selecting a grade only exposes topics genuinely in that grade's MOE syllabus
  * the client can't sneak `kinematics` past the dropdown validator directly
  * quantity bounds + MCQ lock (format is derived, not accepted from the client)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
from app import app

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

print("\n== GET / ==")
r = client.get("/")
check("index.html served", r.status_code == 200 and "SIMCC Question Generator" in r.text, r.text[:80])

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL FORM-APP TESTS PASSED")