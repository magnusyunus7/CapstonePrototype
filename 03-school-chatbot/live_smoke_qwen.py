r"""Live smoke: drive the SIMCC form /api/generate end-to-end through the new
provider (currently qwen3.6 via openai_compat), assert real questions come back
with real question_ids + draft invariants.

Read-only against the product; writes nothing.

Usage:
    python live_smoke_qwen.py
"""
import json
import sys
import urllib.request

BASE = "http://localhost:3000"

payload = {
    "grade": "P5",
    "topic_code": "ELEC",
    "skill": "Application",
    "difficulty": "Hard",
    "quantity": 2,
}
req = urllib.request.Request(
    f"{BASE}/api/generate",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


print(f"POST {BASE}/api/generate  {payload}")
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        status = r.status
        body = r.read().decode()
except Exception as e:
    print(f"  [FAIL] request did not complete: {e}")
    sys.exit(1)

check("HTTP 200 from /api/generate", status == 200, f"got {status}")
data = json.loads(body)
check("ok flag true", data.get("ok") is True, str(data.get("ok")))
qs = data.get("questions", [])
check("2 questions returned", len(qs) == 2, f"got {len(qs)}")
check("every question has a real question_id", all(q.get("question_id", "").startswith("Q-P5-ELEC-") for q in qs),
      str([q.get("question_id") for q in qs]))
check("every question review.status is draft (AI never approves)",
      all(q.get("review", {}).get("status") == "draft" for q in qs),
      str([q.get("review") for q in qs]))
check("every question has 4 A-D options",
      all(len(q.get("options") or []) == 4 for q in qs))
check("every question has an explanation", all(q.get("explanation") for q in qs))
check("every question carries originality block", all("originality" in q for q in qs))

print("\n--- sample question (first) ---")
if qs:
    q = qs[0]
    print(f"qid:  {q.get('question_id')}")
    print(f"stem: {q.get('stem', '')[:220]}")
    print(f"ans:  {q.get('answer_key')}")
    print(f"orig: {q.get('originality', {})}")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL LIVE SMOKE CHECKS PASSED")