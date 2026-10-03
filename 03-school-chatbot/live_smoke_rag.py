"""Live smoke: verify RAG retrieval from the real Qdrant `simcc-syllabus` collection
returns semantically relevant chunks with real source-file + page-number citations.

Not an offline unit test (these live in test_rag.py). This is the "is the KB
actually populated with real, traceable data?" drive-by check.

Usage:
    python live_smoke_rag.py
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("SIMCC_QDRANT_URL", "http://localhost:7333")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
sys.path.insert(0, str(Path(__file__).resolve().parent / "workspace" / "tools"))

import rag_client

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


print("== live Qdrant collections (host side) ==")
try:
    c = rag_client.QDRANT.list_collections()
    check("simcc-syllabus collection exists", "simcc-syllabus" in c, str(c))
except rag_client.RagUnavailableError as e:
    check("Qdrant reachable", False, str(e))
    raise SystemExit(1)

print("== live embed call (host side, real Ollama nomic-embed-text) ==")
try:
    vec = rag_client.EMBED("P5 electrical circuits learning objective")
    check("embedding returned 768-dim vector", len(vec) == 768, f"got {len(vec)} dims")
except rag_client.RagUnavailableError as e:
    check("Ollama embed reachable", False, str(e))
    raise SystemExit(1)

print("== semantic retrieval on P5 Electrical System ==")
flt = rag_client.GradeTopicFilter()
flt.add_grade("P5").add_topic_code("ELEC")
hits = rag_client.query("P5 electrical circuits and conductors", collection="simcc-syllabus", top_k=5, flt=flt)
check(">= 1 hit returned (grounding actually populated)", len(hits) >= 1, f"got {len(hits)}")
check("every hit carries a real source filename", all(h.get("source") for h in hits), str([h.get('source') for h in hits]))
check("every hit carries a real page number", all(h.get("page_start") for h in hits), str([h.get('page_start') for h in hits]))

print("\n--- retrieved chunks (top 3) ---")
for h in hits[:3]:
    print(f"  [score={h.get('score', 0):.3f}] {h.get('source')} p.{h.get('page_start')}-{h.get('page_end')}")
    print(f"      {h.get('text', '')[:120]}...")

print("\n== grounding block builder (real data) ==")
blk = rag_client.build_grounding_block("P5", "ELEC", "P5 electrical circuits conductors insulators", top_k_each=3)
check("retrieved chunk count > 0", len(blk["chunks"]) > 0, str(len(blk['chunks'])))
check("block() surfaces real source citations",
      any(c.get("source") for c in blk["sources"]),
      str(blk["sources"][:2]))

print("\n--- grounding block preview ---")
print(blk["block"][:600])

print("\n== school papers: semantic retrieval (style/difficulty exemplars) ==")
try:
    hits_papers = rag_client.query(
        "biology photosynthesis chloroplasts",
        collection=rag_client.COLL_SCHOOL_PAPERS,
        top_k=5,
    )
    check("at least 1 school-paper chunk returned", len(hits_papers) >= 1, f"got {len(hits_papers)}")
    check("every hit carries real source filename", all(h.get("source") for h in hits_papers),
          str([h.get('source') for h in hits_papers]))
    check("every hit carries paper metadata (school or year or subject)",
          all((h.get("school") or h.get("year") or h.get("subject")) for h in hits_papers),
          str([(h.get('school'), h.get('year'), h.get('subject')) for h in hits_papers[:3]]))
    print("\n--- school-paper hits (top 3) ---")
    for h in hits_papers[:3]:
        print(f"  [score={h.get('score', 0):.3f}] {h.get('source')} p.{h.get('page_start')} "
              f"school={h.get('school')!r} year={h.get('year')} subject={h.get('subject')!r}")
        print(f"      {h.get('text', '')[:200]}...")
except rag_client.RagUnavailableError as e:
    check("school-papers collection queryable", False, str(e))

print("\n== school paper originality check (verdict + groundings with real provenance) ==")
try:
    sim = rag_client.school_paper_similarity(
        "A pupil measures the rate of photosynthesis in pondweed and records the gas produced."
    )
    check("sim returns verdict string", sim["verdict"] in ("pass", "flagged"), str(sim["verdict"]))
    check("groundings carries at least one real source citation",
          any((g.get("source") or g.get("page_start") is not None) for g in sim.get("groundings", [])),
          str(sim.get("groundings", [])))
except rag_client.RagUnavailableError as e:
    check("sim reachable", False, str(e))

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL LIVE SMOKE CHECKS PASSED")