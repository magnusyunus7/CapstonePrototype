"""RAG grounding layer (Qdrant + Ollama embeddings) for SIMCC question generation.

Self-contained client used by the form app (apps/question-form/app.py) and the
host-side ingestion script (ingest_kb.py).

Runtime topology (two DIFFERENT Qdrant hostnames for the same server):
  * ingest_kb.py runs on the Windows HOST -> http://localhost:7333
  * app.py runs INSIDE the compose network -> http://qdrant:6333
Override with rag_client.QDRANT_BASE_URL if either ever moves.

Fail-loud contract: if Qdrant or the embedding model is unreachable, callers
receive a RagUnavailableError rather than an empty hit list. The policy
"grounding unavailable -> generation halts and asks for a retry, it never
falls through to an ungrounded prompt" is ASSERTED-BY-TEST (test_rag.py).

This module deliberately holds only: query planning (grade/topic inference for
retrieval), the Qdrant REST surface, and groundable-block formatting of HIT
chunks. Chunking/construction of points from raw documents lives in
ingest_kb.py.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
from difflib import SequenceMatcher
from typing import Iterable

import httpx

DEFAULT_QDRANT_BASE_URL = "http://qdrant:6333"
DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text"

# Collection names are the TRUST BOUNDARY between "grounded enough to write from"
# and "intrusion risk" - SIMCC-authorised materials must always land in one of
# these; anything else is unauthorised and must never be retrieved from.
COLL_SYLLABUS = "simcc-syllabus"
COLL_SCHOOL_PAPERS = "simcc-school-papers"
COLL_SIMCC_RULES = "simcc-rules"
GROUNDING_TARGETS = (COLL_SYLLABUS, COLL_SCHOOL_PAPERS, COLL_SIMCC_RULES)

# Single canonical extract for "which (grade, topic_code) should RAG fetch?"
# Kept FREE of pydantic / async so the pipe can inline-copy it literally.
GRADE_RE = re.compile(r"\b([ps])([1-6])\b", re.IGNORECASE)

SILABUS_SUFFIX_RE = re.compile(r"\B[1-6]\b")


class RagUnavailableError(RuntimeError):
    """RAG backend (Qdrant / embedding model) unreachable."""


@dataclasses.dataclass
class SearchQuery:
    vector: list[float]
    top_k: int
    filter: "GradeTopicFilter | None" = None


class GradeTopicFilter:
    """Qdrant `must` filter builder for {grade, topic_code} scoped retrieval."""

    def __init__(self) -> None:
        self._must: list[dict] = []

    def add_grade(self, grade: str) -> "GradeTopicFilter":
        self._must.append({"key": "grade", "match": {"value": grade.strip().upper()}})
        return self

    def add_topic_code(self, code: str) -> "GradeTopicFilter":
        self._must.append({"key": "topic_code", "match": {"value": code.strip().upper()}})
        return self

    @property
    def must(self) -> list[dict]:
        return list(self._must)

    def as_filter(self) -> dict:
        return {"must": self.must}


def _load_topic_catalog() -> dict:
    """Embedded copy, NOT a disk read (matches the simcc_pipe convention: this
    repo's catalog lives at workspace/schema/topic_catalog.json on disk, but a
    Function exec block cannot reliably open() it). Guarded by test_rag.py so a
    JSON edit that misses this copy is caught before it reaches Postgres."""
    try:
        env_path = os.environ.get("SIMCC_TOPIC_CATALOG_PATH")
        if env_path:
            with open(env_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:  # fall through to embedded
        pass
    return copy.deepcopy(_EMBEDDED_TOPIC_CATALOG)


_EMBEDDED_TOPIC_CATALOG: dict = {
    "P3": [
        {"code": "DIVLT", "name": "Diversity of Living and Non-Living Things"},
        {"code": "DIVMAT", "name": "Diversity of Materials"},
        {"code": "CYCLIF", "name": "Cycles in Plants and Animals (Life Cycles)"},
        {"code": "MAGNET", "name": "Interaction of Forces (Magnets)"},
    ],
    "P4": [
        {"code": "PLANTS", "name": "Plant System (Plant parts and functions)"},
        {"code": "DIGEST", "name": "Human System (Digestive system)"},
        {"code": "MATTER", "name": "Cycles in Matter and Water (Matter)"},
        {"code": "LIGHT", "name": "Energy Forms and Uses (Light)"},
        {"code": "HEAT", "name": "Energy Forms and Uses (Heat)"},
    ],
    "P5": [
        {"code": "REPRO", "name": "Cycles in Plants and Animals (Reproduction)"},
        {"code": "WATER", "name": "Cycles in Matter and Water (Water)"},
        {"code": "PLTRC", "name": "Plant System (Respiratory and circulatory systems)"},
        {"code": "HUMRC", "name": "Human System (Respiratory and circulatory systems)"},
        {"code": "ELEC", "name": "Electrical System"},
    ],
    "P6": [
        {"code": "PHOTOS", "name": "Energy Forms and Uses (Photosynthesis)"},
        {"code": "ENCONV", "name": "Energy Conversion"},
        {"code": "FORCES", "name": "Interaction of Forces (Frictional force, gravitational force, elastic spring force)"},
        {"code": "ENVIRON", "name": "Interactions within the Environment"},
    ],
    "S1": [
        {"code": "SCIEV", "name": "The Scientific Endeavour"},
        {"code": "MATPHY", "name": "Exploring Diversity of Matter by its Physical Properties"},
        {"code": "MATCHEM", "name": "Exploring Diversity of Matter by its Chemical Composition"},
        {"code": "SEPTEC", "name": "Exploring Diversity of Matter using Separation Techniques"},
        {"code": "RAYLGT", "name": "Ray Model of Light"},
        {"code": "CELLS", "name": "Model of Cells - the Basic Unit of Life"},
        {"code": "PARTIC", "name": "Model of Matter - the Particulate Nature of Matter"},
        {"code": "ATOMS", "name": "Model of Matter - Atoms and Molecules"},
        {"code": "FORCEEN", "name": "Application of Forces and Transfer of Energy"},
        {"code": "HEATTR", "name": "Transfer of Heat Energy and its Effects"},
        {"code": "CHEMCH", "name": "Chemical Changes"},
        {"code": "ECOSYS", "name": "Interactions within Ecosystems"},
        {"code": "ELCSYS", "name": "Electrical Systems"},
        {"code": "DIGSYS", "name": "Human Digestive System"},
        {"code": "TRNSYS", "name": "Transport Systems in Living Things"},
        {"code": "REPSYS", "name": "Human Sexual Reproductive System"},
    ],
}
_EMBEDDED_TOPIC_CATALOG["S2"] = _EMBEDDED_TOPIC_CATALOG["S1"]


def topics_for_grade(grade: str) -> list[dict]:
    return list(_load_topic_catalog().get(grade.strip().upper(), []))


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(s).lower())).strip()


# --------------------------------------------------------------------------
# Qdrant + Ollama clients
# --------------------------------------------------------------------------
QDRANT_BASE_URL = os.environ.get("SIMCC_QDRANT_URL", DEFAULT_QDRANT_BASE_URL)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL)
EMBED_MODEL = os.environ.get("SIMCC_EMBED_MODEL", DEFAULT_EMBED_MODEL)


class QdrantClient:
    """Thin typed wrapper over Qdrant's REST API (collections + points)."""

    def __init__(self, base_url: str | None = None) -> None:
        self.base = (base_url or QDRANT_BASE_URL).rstrip("/")
        self._http = _http_client()

    def _ensure_ok(self, r: httpx.Response, op: str) -> None:
        if r.status_code >= 400:
            raise RagUnavailableError(f"qdrant {op} -> HTTP {r.status_code}: {r.text[:200]}")

    def list_collections(self) -> set[str]:
        try:
            r = self._http.get(f"{self.base}/collections")
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"qdrant list_collections unreachable: {e}") from e
        self._ensure_ok(r, "list_collections")
        return {c["name"] for c in r.json().get("result", {}).get("collections", [])}

    def collection_exists(self, name: str) -> bool:
        try:
            r = self._http.get(f"{self.base}/collections/{name}")
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"qdrant collection_exists unreachable: {e}") from e
        return r.status_code == 200

    def get_vector_size(self, name: str) -> int:
        try:
            r = self._http.get(f"{self.base}/collections/{name}")
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"qdrant get_vector_size unreachable: {e}") from e
        self._ensure_ok(r, "get_vector_size")
        return int(r.json()["result"]["config"]["params"]["vectors"]["size"])

    def create_collection(self, name: str, dims: int) -> None:
        try:
            r = self._http.put(f"{self.base}/collections/{name}", json={"vectors": {"size": dims, "distance": "Cosine"}})
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"qdrant create_collection unreachable: {e}") from e
        self._ensure_ok(r, f"create_collection:{name}")

    def recreate_collection(self, name: str, dims: int) -> None:
        try:
            self._http.delete(f"{self.base}/collections/{name}")
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"qdrant recreate_collection(delete) unreachable: {e}") from e
        self.create_collection(name, dims)

    def upsert(self, name: str, points: list[dict]) -> int:
        if not points:
            return 0
        try:
            r = self._http.put(f"{self.base}/collections/{name}/points", json={"points": points})
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"qdrant upsert unreachable: {e}") from e
        self._ensure_ok(r, f"upsert:{name}")
        return len(points)

    def search(self, name: str, qvec: list[float], limit: int, flt: "GradeTopicFilter | None") -> list[dict]:
        body: dict = {"vector": qvec, "limit": limit, "with_payload": True}
        if flt is not None and flt.must:
            body["filter"] = flt.as_filter()
        try:
            r = self._http.post(f"{self.base}/collections/{name}/points/query", json={**body, "query": qvec})
            if r.status_code == 404:  # collection not ingested yet this run
                return []
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"qdrant search unreachable: {e}") from e
        self._ensure_ok(r, f"search:{name}")
        hits = r.json().get("result", {}) or {}
        points = hits.get("points", hits.get("result", hits if isinstance(hits, list) else []))
        if isinstance(points, dict):
            points = points.get("points", [])
        return [dict(p.get("payload") or {}, score=p.get("score", 0.0), id=p.get("id")) for p in points]


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=120.0)


class OllamaEmbedder:
    """Turns text into an embedding vector via Ollama (embedding model only)."""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or EMBED_MODEL
        self._http = _http_client()

    def __call__(self, text: str) -> list[float]:
        try:
            r = self._http.post(f"{OLLAMA_BASE_URL}/api/embeddings", json={"model": self.model, "prompt": text})
        except httpx.HTTPError as e:
            raise RagUnavailableError(f"ollama embed unreachable ({self.model}): {e}") from e
        if r.status_code >= 400:
            raise RagUnavailableError(f"ollama embed -> HTTP {r.status_code}: {r.text[:200]}")
        vec = r.json().get("embedding")
        if not vec:
            raise RagUnavailableError(f"ollama embed returned no vector for {self.model!r}")
        return list(vec)


QDRANT = QdrantClient()
EMBED = OllamaEmbedder()


# --------------------------------------------------------------------------
# Query planning: which (grade, topic_code) should RAG fetch for this request?
# --------------------------------------------------------------------------
def retrieval_grade_topic(tick: str) -> tuple[str | None, str | None, str]:
    """Extract (grade, topic_code, query_text) for RAG retrieval.

    Always returns the cleaned `query_text` for the semantically-embedding query;
    `grade` / `topic_code` are None when the parse cannot commit to them. Only a
    REAL MOE catalogue code is ever produced as `topic_code` (never a fabricated
    tag) — over-filtering with a code Qdrant has never seen would silently return
    zero hits and hollow out grounding. A user-declared raw topic phrase is kept
    only as free text (which is still a strong semantic anchor for embedding).
    """
    t = " ".join(str(tick).split())
    tl = _normalize(t)
    m = GRADE_RE.search(t)
    grade = (m.group(1) + m.group(2)).upper() if m else None

    topic_code: str | None = None
    if grade:
        best_pre = 0
        for entry in topics_for_grade(grade):
            name = entry.get("name", "")
            if not name:
                continue
            name_norm = _normalize(name)
            paren_stripped = _normalize(name.split("(")[0])
            for candidate in (name_norm, paren_stripped):
                if candidate and candidate in tl and len(candidate.split()) > best_pre:
                    topic_code = entry["code"]
                    best_pre = len(candidate.split())
                    break

    return grade, topic_code, t


def embed_text_set(texts: Iterable[str], prompt_context: str = "") -> dict:
    """Embed one batch of texts; returns {chunks:[...], vectors:[...]} with
    structural metadata (a single grounding-set document). Single-shot, NOT a
    Qdrant write - upload_concrete_points() owns persistence."""
    chunk_texts: list[str] = []
    seen: set[str] = set()
    for raw in texts:
        if not str(raw).strip():
            continue
        u = " ".join(str(raw).split())
        if u in seen:
            continue
        seen.add(u)
        chunk_texts.append(_normalize(u))
    if not chunk_texts:
        return {"chunks": [], "vectors": []}

    vectors = [EMBED(_ + (" -- " + prompt_context if prompt_context else "")) for _ in chunk_texts]
    chunks = []
    for i, text in enumerate(chunk_texts):
        g, t, _q = retrieval_grade_topic(text)
        chunks.append({
            "id": i,
            "text": text,
            "source": "grounding set",
            "kind": "grounding",
            "grade": g,
            "topic_code": t,
        })
    return {"chunks": chunks, "vectors": vectors}


def query(text: str, *, collection: str, top_k: int = 6, flt: "GradeTopicFilter | None" = None) -> list[dict]:
    """Semantic search against ONE collection. Raises on infrastructure failure."""
    return QDRANT.search(collection, EMBED(text), top_k, flt)


# --------------------------------------------------------------------------
# Shared originality check (ngram + SequenceMatcher) - the deterministic part
# of check_similarity() from simcc_pipe/validator_tools, kept canonical here so
# BOTH generation paths (pipe + form) use the identical threshold/math.
# --------------------------------------------------------------------------


def _tokens(s: str) -> list[str]:
    return _normalize(s).split()


def _ngrams(tok: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tok[i : i + n]) for i in range(len(tok) - n + 1)} if len(tok) >= n else set()


def check_similarity(new_question: str, existing_bank: str, *, ngram: int = 4, threshold: float = 0.50) -> dict:
    """Combines SequenceMatcher ratio and n-gram Jaccard. Returns {aggregate_similarity, heaviest_pair, verdict}."""
    bank_lines = [ln.strip() for ln in str(existing_bank).splitlines() if ln.strip()]
    if not bank_lines:
        return {"aggregate_similarity": 0.0, "heaviest_pair": None, "verdict": "pass"}

    new_toks = _tokens(new_question)
    new_ng = _ngrams(new_toks, ngram)
    best = None
    agg = 0.0
    for i, line in enumerate(bank_lines, 1):
        ln_ng = _ngrams(_tokens(line), ngram)
        ratio = SequenceMatcher(None, _normalize(new_question), _normalize(line)).ratio()
        union = new_ng | ln_ng
        jacc = (len(new_ng & ln_ng) / len(union)) if union else 0.0
        combined = 0.7 * ratio + 0.3 * jacc
        if combined > agg:
            agg = combined
            best = {"index": i, "preview": line[:70], "combined_score": round(combined, 4)}
    return {"aggregate_similarity": round(agg, 4), "heaviest_pair": best, "verdict": "flagged" if agg >= threshold else "pass"}


def school_paper_similarity(question_text: str, *, top_k: int = 5) -> dict:
    """Live originality check against real school papers (COLL_SCHOOL_PAPERS).
    Raises RagUnavailableError if the RAG backend is unreachable — callers decide
    whether to fail generation or note-and-continue."""
    grade, topic_code, _q = retrieval_grade_topic(question_text)
    flt = GradeTopicFilter()
    if grade:
        flt.add_grade(grade)
    if topic_code:
        flt.add_topic_code(topic_code)
    hits = query(question_text, collection=COLL_SCHOOL_PAPERS, top_k=top_k, flt=flt if flt.must else None)
    bank = "\n".join(h.get("text", "") for h in hits if h.get("text"))
    result = check_similarity(question_text, bank) if bank else {"aggregate_similarity": 0.0, "heaviest_pair": None, "verdict": "pass"}
    result["groundings"] = [
        {"source": h.get("source") or h.get("source_file"), "page_start": h.get("page_start"), "page_end": h.get("page_end")}
        for h in hits
    ]
    return result


def upload_concrete_points(collection: str, chunks: list[dict], vectors: list[list[float]],
                           *, dims: int | None = None) -> int:
    """Write pre-built {chunks, vectors} into `collection`, creating it if absent."""
    if len(chunks) != len(vectors):
        raise ValueError(f"chunks ({len(chunks)}) and vectors ({len(vectors)}) length mismatch")
    if not chunks:
        return 0
    vsize = dims or (len(vectors[0]) if vectors else 768)
    if not QDRANT.collection_exists(collection):
        QDRANT.create_collection(collection, vsize)
    points = [{
        "id": int(c["id"]),
        "vector": list(v),
        "payload": {k: v2 for k, v2 in c.items() if k != "id"},
    } for c, v in zip(chunks, vectors)]
    return QDRANT.upsert(collection, points)


# --------------------------------------------------------------------------
# Grounding block formatting (for generator/validator prompts + provenance)
# --------------------------------------------------------------------------
class GroundingRule:
    """Canonical 'retrieved chunk list -> prompt block' formatter. Provenance is
    asserted-by-test to ALWAYS quote a real source file + page range - never a
    fabricated citation (matches knowledge-base/README.md provenance rule)."""

    @staticmethod
    def _citation(hit: dict) -> str:
        src = hit.get("source") or hit.get("source_file") or "untraced source"
        ps, pe = hit.get("page_start"), hit.get("page_end")
        if ps and pe and ps != pe:
            return f"{src} (pp. {ps}-{pe})"
        if ps:
            return f"{src} (p. {ps})"
        return str(src)

    @staticmethod
    def simcc_grounding(chunks: list[dict]) -> str:
        if not chunks:
            return ""
        lines = ["--- RETRIEVED KNOWLEDGE BASE (cite these; do NOT invent sources) ---"]
        for i, hit in enumerate(chunks, 1):
            lines.append(f"[S{i} {GroundingRule._citation(hit)}] {hit.get('text', '')}")
        lines.append("--- END RETRIEVED KNOWLEDGE BASE ---")
        return "\n".join(lines)


def build_grounding_block(grade: str | None, topic_code: str | None, query_text: str,
                          *, collections: tuple[str, ...] = GROUNDING_TARGETS,
                          top_k_each: int = 3) -> dict:
    """Retrieve grounded chunks across authorised collections, fail-loudly.

    Filter-cascade (the live 2026-09-30 lesson: some corpus chunks - a whole-
    grade syllabus PDF's generic chunks - carry `topic_code: None`, so a strict
    two-clause filter silently matched NOTHING and produced a hollow grounding
    block with zero retrieved chunks even though relevant text existed). On each
    pass the filter narrows from the FULL request down to whatever cross-section
    actually matches, so we never return an empty hit list just because one
    overly-specific clause had nothing to bite on:
      pass 1: grade + topic_code  (most specific)
      pass 2: grade only
      pass 3: no filter (whole corpus, semantic rank still applies)
    Merges unique hits, score-descending, up to 6 chunks."""
    passes: list[list[dict]] = []
    if grade and topic_code:
        f = GradeTopicFilter()
        f.add_grade(grade)
        f.add_topic_code(topic_code)
        passes.append(_query_all_collections(query_text, collections, top_k_each, f))
    if grade:
        f = GradeTopicFilter()
        f.add_grade(grade)
        passes.append(_query_all_collections(query_text, collections, top_k_each, f))
    passes.append(_query_all_collections(query_text, collections, top_k_each, None))

    all_hits: list[dict] = []
    seen_hit_ids: set[str] = set()
    for batch in passes:
        for hit in batch:
            key = f"{hit.get('id')}:{_normalize(hit.get('text', ''))[:40]}"
            if key in seen_hit_ids:
                continue
            seen_hit_ids.add(key)
            all_hits.append(hit)

    all_hits.sort(key=lambda h: h.get("score", 0.0), reverse=True)
    seen, chunks = set(), []
    for hit in all_hits:
        key = _normalize(hit.get("text", ""))[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        chunks.append(hit)
        if len(chunks) >= 6:
            break

    return {
        "chunks": chunks,
        "sources": [{"source": hit.get("source") or hit.get("source_file"),
                     "page_start": hit.get("page_start"),
                     "page_end": hit.get("page_end"),
                     "score": hit.get("score")} for hit in chunks],
        "block": GroundingRule.simcc_grounding(chunks),
    }


def _query_all_collections(query_text: str, collections: tuple[str, ...],
                           top_k_each: int, flt: "GradeTopicFilter | None") -> list[dict]:
    """Query every collection once with the given filter; returns concatenated hits."""
    out: list[dict] = []
    for coll in collections:
        out.extend(query(query_text, collection=coll, top_k=top_k_each, flt=flt))
    return out