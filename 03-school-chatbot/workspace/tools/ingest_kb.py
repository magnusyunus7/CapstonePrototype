"""Ingest SIMCC knowledge-base documents (PDF) into Qdrant for RAG grounding.

Reads every PDF under workspace/knowledge-base/{moe-syllabus, school-papers,
simcc-rules}/, extracts text page-by-page with pdfplumber, chunks it with
source-file + page provenance preserved, embeds via Ollama, and upserts into
the matching Qdrant collection (rag_client.COLL_*).

Rerunnable / safe: --reset wipes a collection first. An unreachable Qdrant or
Ollama raises RagUnavailableError loudly — never silently stores nothing.

Usage (run from 03-school-chatbot/, host-side):
    python workspace/tools/ingest_kb.py --list                # dry-run inventory
    python workspace/tools/ingest_kb.py --target moe-syllabus  # just one folder
    python workspace/tools/ingest_kb.py --reset                # wipe + reindex all

Requires: pdfplumber  (pip install pdfplumber), httpx
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

# ingest_kb.py runs on the Windows HOST, while rag_client.py's defaults are
# compose-network URLs (http://qdrant:6333, http://host.docker.internal:11434)
# meant for simcc_pipe.py / apps/question-form/app.py inside Docker. Override
# down to real host-side resolvable URLs BEFORE rag_client is imported (it
# captures the env at import time). If running from inside a container, call
# ingest_kb.py with --help or set SIMCC_QDRANT_URL / SIMCC_EMBED_MODEL / etc.
os.environ.setdefault("SIMCC_QDRANT_URL", "http://localhost:7333")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

import rag_client
from rag_client import RagUnavailableError

ROOT = Path(__file__).resolve().parents[1]  # workspace/
KB = ROOT / "knowledge-base"

# folder name -> (collection, chunk kind)
TARGETS = {
    "moe-syllabus": (rag_client.COLL_SYLLABUS, "moe-syllabus"),
    "school-papers": (rag_client.COLL_SCHOOL_PAPERS, "school-papers"),
    "simcc-rules": (rag_client.COLL_SIMCC_RULES, "simcc-rules"),
}

CHUNK_SIZE = 900
CHUNK_OVERLAP = 150


# ============================================================================
# === SCHOOL-PAPERS PAIR PARSER (structure-aware chunking) ===================
# Driven by real structure dumps from the 7 papersets currently in
# workspace/knowledge-base/school-papers/ (biology-01..03, chemistry-01..04).
# Two answer-paper shapes exist in the wild and BOTH are supported:
#   * tabular (pdfplumber extract_tables) -- Temasek, TKGS, Dunman (PDF/PDF+ANS)
#   * free text with inline mark chords `[N]` -- Zhonghua, St Gabriel's (PDF)
# MCQ grids (Dunman 6092/01 and Temasek Paper 1) yield per-question letter
# answers. Anything that doesn't fit these shapes DEGRADES to the plain
# page-level chunker rather than fabricating question/answer prose.
# ============================================================================

_SCHOOL_RE = re.compile(r"\b([A-Z][A-Z'&\-\. ]{3,50}?(?:SCHOOL|SECONDARY SCHOOL))\b")
_SUBJECT_RE = re.compile(r"\b(BIOLOGY|CHEMISTRY|PHYSICS|SCIENCE)\b", re.IGNORECASE)
_PAPER_CODE_RE = re.compile(r"\b(\d{4}\s*/\s*\d{2})\b")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_LEVEL_RE = re.compile(r"\b(SECONDARY FOUR(?:\s+EXPRESS)?|4E|S[A-Z])\b", re.IGNORECASE)

# `1 (a)` / `1(a)` / `(a)` / `(i)` / `2 (a)(i)`: question-part separators.
_SUBPART_RE = re.compile(r"^\s*(\d{1,2})?\s*\(?([a-z])\)?(?:\s*\(([ivx]+)\))?\s*$")
# Any line that begins a fresh sub-question marker in dense text: '(a) ', '(i) ' etc.
_NEWPART_RE = re.compile(r"(?:^|(?<=[\s\.\?\!]|\]))\((\d{1,2})?\s*([a-z])\)(?:\s*\(([ivx]+)\))?\s+")
_MARK_TAIL_RE = re.compile(r"\[(\d+(?:\.\d+)?)\]\s*$|\[(\d+(?:\.\d+)?)\][^A-Za-z]*$")
_TOTAL_RE = re.compile(r"\[Total:?\s*(\d+)\]", re.IGNORECASE)


def extract_paper_meta(text: str) -> dict:
    """Pull school / year / subject / paper code from the exam cover header.
    Returns {} when nothing traces (never fabricates)."""
    out: dict = {}
    if not text:
        return out
    t = " ".join(text.split())
    m = _SCHOOL_RE.search(t)
    if m:
        school = m.group(1).strip().rstrip(",'").strip()
        out["school"] = school
    m = _SUBJECT_RE.search(t)
    if m:
        out["subject"] = m.group(1).upper()
    m = _PAPER_CODE_RE.search(t)
    if m:
        out["paper_code"] = re.sub(r"\s*/\s*", "/", m.group(1)).strip()
    m = _YEAR_RE.search(t)
    if m:
        try:
            out["year"] = int("".join(ch for ch in t[m.start() : m.end()] if ch.isdigit()))
        except ValueError:
            pass
    m = _LEVEL_RE.search(t)
    if m:
        dict_lvl = m.group(1)
        out["level"] = dict_lvl.title()
    return out


def parse_question_subparts(page_text: str) -> list[dict]:
    """Partitions one question-paper page into subparts like (a), (b)(i).

    Returns [{label, marks, text}] where `label` preserves the nested letter
    ('(a)' | '(b)' | '(ii)') and `marks` is the int from a trailing [N], else 0.
    Skips '[Total: N]' and other non-part footer lines."""
    if not page_text:
        return []
    lines = page_text.splitlines()
    out: list[dict] = []
    cur: dict | None = None
    for line in lines:
        if not line.strip():
            continue
        if _TOTAL_RE.search(line):
            if cur and cur["text"].strip():
                out.append(cur)
            cur = None
            continue
        m = _NEWPART_RE.search(line)
        if not m and not _SUBPART_RE.match(line.strip()):
            if cur is not None:
                cur["text"] = (cur["text"] + " " + line.strip()).strip()
                mm = _MARK_TAIL_RE.search(line)
                if mm:
                    num = mm.group(1) or mm.group(2)
                    if num:
                        try:
                            cur["raw_marks"] = float(num)
                            cur["marks"] = int(float(num))
                        except ValueError:
                            pass
            continue
        if m:
            label = f"({m.group(2)})"
            if m.group(3):
                label = f"({m.group(2)})({m.group(3)})"
        else:
            head = _SUBPART_RE.match(line.strip())
            inner = f"({head.group(3)})" if head.group(3) else ""
            label = f"({head.group(2)}){inner}"
        if label:
            if cur and cur["text"].strip():
                out.append(cur)
            cur = {"label": label, "marks": 0, "text": "", "raw_marks": 0}
        if cur is not None:
            # strip the label token(s) from the line itself to avoid double-text
            stripped = line
            for pat in (_NEWPART_RE, _SUBPART_RE):
                stripped = pat.sub("", stripped)
            cur["text"] = (cur["text"] + " " + stripped.strip()).strip()
            mm = _MARK_TAIL_RE.search(line)
            if mm:
                num = mm.group(1) or mm.group(2)
                if num:
                    try:
                        cur["raw_marks"] = float(num)
                        cur["marks"] = int(float(num))
                    except ValueError:
                        pass
    if cur and cur["text"].strip():
        out.append(cur)
    return out


_ANSWER_TABLE_HEADER_WORDS = {"q", "qn", "no", "ans", "answer", "mark", "marks", "remark", "remarks", "question"}

# A question-number-anchored key: one or two digits + optional parens/a-appendage,
# optionally followed by roman-numeral depth. Used to keep question-side and
# answer-side sub-question keys in one canonical shape ("1a" | "1(a)" -> "1a").
_QKEY_RE = re.compile(r"^\s*(\d{1,2})\s*\(?([a-z])\)?(?:\s*\(([ivx]+)\))?")


def canonical_qkey(qnum: str = "", qsub: str = "", label: str = "") -> str:
    """Normalises the 3 observed answer-scheme dialects into one key:
      '1a'  '1(a)'  '1 (a)'  '1a(i)'  -> '1a' / '1a1' (roman depth folded into '1'/'2'/etc).
    Bare subletter labels '(a)' with NO question number match against any
    number-prefixed key with the same letter (pairing is done at the leaf, not
    the root question number)."""
    raw = f"{qnum}{qsub}{label}"
    raw = raw.strip()
    if not raw:
        return ""
    # strip whatever parens / whitespace / punctuation variant
    raw = re.sub(r"[\s\(\)\[\]]+", "", raw)
    m = re.match(r"^(\d{1,2})?([a-z]{0,2})(\d{0,2})$", raw, re.IGNORECASE)
    if m:
        digits = m.group(1) or ""
        letters = (m.group(2) or "").lower()
        tail = m.group(3) or ""
        return f"{digits}{letters}{tail}"
    return raw.lower()


def _strip_key_noise(qkey: str) -> str:
    """Trailing junk like '7Photosynthesis;' or '4a2' (mark-chord suffix) should
    NOT survive into the pairing key; keep only the leading digits+letter shape."""
    m = _QKEY_RE.match(qkey.strip())
    if m:
        digits = m.group(1) or ""
        letters = m.group(2) or ""
        roman = m.group(3) or ""
        suffix = ""
        if roman:
            suffix = str(len(roman))  # (i) -> depth 1, (ii) -> 2
        return f"{digits}{letters}{suffix}"
    # bare letter (question-side labels like '(a)' -- keep the letter)
    m2 = re.match(r"^\s*([a-z])$", qkey.strip(), re.IGNORECASE)
    if m2:
        return m2.group(1).lower()
    return ""  # unrecognizable -> no pairing key


def parse_marking_scheme_table(rows: list[list[str | None]]) -> list[dict]:
    """Accepts pdfplumber extract_tables() output (potentially ragged, header
    included). Two real answer-scheme shapes are seen in the corpus:
      * Temasek/Dunman (3 cols): [Qn, Answer, Remarks] with pre-concatenated
        qnums ("1a", "bi", "bii"). qnum kept VERBATIM (never split).
      * TKGS ragged (5-6 cols): [_, Q, Sub, Ans, Mark, Remarks] with separate
        main-question and sub-part columns; blank cells carry the previous
        question's number forward (standard exam-table semantics).
    Returns [{qnum, qsub, marks, remarks}]. Score bands like "Max\\n3" are
    preserved VERBATIM (a human-readable band, not an int).
    """
    out: list[dict] = []
    prev_qnum = ""
    for row in rows:
        if not row:
            continue
        cells = [c if c is not None else "" for c in row]
        if _looks_like_answer_table_header(cells):
            continue
        if len(cells) >= 5:
            # TKGS-style ragged schema: separate main-qnum + subpart columns.
            qnum = str(cells[0]).strip() or prev_qnum
            qsub = str(cells[2]).strip()
            marks = str(cells[4]).strip()
            remarks = str(cells[5]).strip() if len(cells) > 5 else ""
        elif len(cells) == 3:
            # Temasek-style combined qnum in cells[0].
            qnum = str(cells[0]).strip()
            qsub = ""
            marks = ""
            remarks = str(cells[2]).strip()
        else:
            # Generic ragged fallback: pick qnum-like + first-parens-letter + numeric + longest free text.
            qnum = ""
            qsub = ""
            marks = ""
            remarks = ""
            for c in cells:
                s = str(c).strip()
                if not s:
                    continue
                if not qnum and re.fullmatch(r"[a-z]+\d*[a-z]*|\d{1,2}[a-z]*|\d{1,2}\([a-z]+\)", s):
                    qnum = s
                elif not qsub and re.fullmatch(r"\(?[a-z]\)?(?:\([ivx]+\))?", s) and not qnum:
                    qsub = s
                elif not marks and (re.fullmatch(r"[\d\n\.\s]+", s) or s.startswith("Max")):
                    marks = s
                elif not remarks and not re.fullmatch(r"[\d\n\.\s]+", s):
                    remarks = s
        if not qnum and not qsub:
            continue
        if qnum:
            prev_qnum = qnum
        out.append({"qnum": qnum, "qsub": qsub, "marks": marks, "remarks": remarks})
    return out


def _looks_like_answer_table_header(cells: list[str]) -> bool:
    head_tokens = {c.strip().lower() for c in cells if c}
    return bool(head_tokens & _ANSWER_TABLE_HEADER_WORDS)


def parse_mcq_grids(tables: list[list[list[str | None]]] | list[list[str | None]]) -> dict[str, str]:
    """Detects a Dunman-style MCQ letter grid: rows of qnums (digits) + rows of
    answer letters. Returns {"1": "A", ...}. Accepts EITHER a single table as a
    list of rows, OR a list of tables (pdfplumber extract_tables() shape)."""
    if tables and isinstance(tables[0], (list, tuple)) and tables[0] and isinstance(tables[0][0], (list, tuple)):
        pass  # already a list of tables
    elif tables and isinstance(tables[0], (list, tuple)):
        # single table as list of rows -> normalize to list-of-tables
        tables = [tables]  # type: ignore[assignment]
    answers: dict[str, str] = {}
    for tbl in tables:
        if not tbl:
            continue
        for row in tbl:
            cells = [c.strip() if c else "" for c in row]
            digits = [c for c in cells if re.fullmatch(r"\d{1,2}", c)]
            letters = [c.upper() for c in cells if re.fullmatch(r"[A-D]", c.strip().upper())]
            if digits and letters and len(digits) == len(letters):
                for d, L in zip(digits, letters):
                    answers[d] = L
            elif digits and not letters:
                # alternating layout: collect from next letter row below
                pass
    # alternating layout: pair row-by-row if needed
    if not answers:
        for tbl in tables:
            if len(tbl) < 2:
                continue
            for i in range(len(tbl) - 1):
                r1 = [c.strip() if c else "" for c in tbl[i]]
                r2 = [c.strip() if c else "" for c in tbl[i + 1]]
                d1 = [c for c in r1 if re.fullmatch(r"\d{1,2}", c)]
                l2 = [c.upper() for c in r2 if re.fullmatch(r"[A-D]", c.strip().upper())]
                if d1 and l2 and len(d1) == len(l2):
                    for d, L in zip(d1, l2):
                        answers.setdefault(d, L)
    return answers


def _answer_page_pairs(answer_pages: list[dict]) -> list[dict]:
    """Extract per-question answer records from an answer paper's pages.
    Handles both tabular (extract_tables) and free-text (inline marks) shapes."""
    records: list[dict] = []
    for page in answer_pages:
        tables = page.get("tables") or []
        if tables:
            rows = parse_marking_scheme_table(tables[0]) if len(tables[0]) > 0 else []
            for r in rows:
                if r.get("qnum") or r.get("qsub"):
                    records.append({"qnum": r.get("qnum", ""), "qsub": r.get("qsub", ""),
                                    "marks": r.get("marks", ""), "remarks": r.get("remarks", ""),
                                    "answer_text": r.get("answer_text", ""), "page": page.get("number")})
        else:
            text = page.get("text") or ""
            # free-text shape: split on qnum-start lines like "1 (a) Answer... [N]"
            for mm in re.finditer(r"(?m)^\s*(\d{1,2})?\s*\(?([a-z]|Qn)\)?\s+(.*?)(?=\n\s*(?:\d{1,2}\s*\(?[a-z]?\)?|\Z))", text, re.DOTALL):
                qnum = mm.group(1) or ""
                qsub = f"({mm.group(2)})" if mm.group(2) and mm.group(2) != "Qn" else ""
                body = mm.group(3).strip()
                marks = ""
                for ann in re.finditer(r"\[([\d\.]+)\]", body):
                    marks = ann.group(1)
                    break
                records.append({"qnum": qnum, "qsub": qsub, "marks": marks,
                                "remarks": "", "answer_text": body, "page": page.get("number")})
    return records


def _fallback_page_chunk(out: list[dict], pg: dict, source_name: str, meta: dict) -> None:
    """One chunk per page for papers that resist decomposition (MCQ-only,
    image-heavy). Preserves real `source`/school/year/paper_code provenance."""
    page_text = pg.get("text", "") or ""
    if not page_text.strip():
        return
    chunk_doc = {
        "path": source_name,
        "kind": "school-papers",
        "grade": [],
        "page_start": pg["number"],
        "page_end": pg["number"],
        **{k: meta.get(k, "") for k in ("school", "year", "subject", "paper_code")},
        "has_image": bool(pg.get("img_count", 0)) or bool(re.search(r"\bFig\.?\s*\d", page_text)),
    }
    out.extend(chunk_text(page_text, chunk_doc, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP))


def chunk_question_records(paper: dict, ngram_seed: str = "") -> list[dict]:
    """Structure-aware chunker for one school paperset folder.

    paper (dict):
      {"folder": str, "question_file": str, "answer_file": str,
       "question_pages": [{"number", "text", "img_count"}],
       "answer_pages":  [{"number", "text", "img_count", "tables"?}]}

    Returns 1-chunk-per-subquestion records (question stem + official answer +
    examiner remarks, each with real source file + page provenance).
    DEGRADES to the generic page-chunker when no subquestion structure is
    detectable (MCQ-only, image-only)."""
    q_pages = paper.get("question_pages") or []
    a_pages = paper.get("answer_pages") or []

    out: list[dict] = []

    # Metadata: use first few pages of the question paper (cover block).
    cover_text = "\n".join(p.get("text", "") for p in q_pages[:3])
    meta = extract_paper_meta(cover_text)

    # Answer records keyed by canonical form ("1a", "1b(i)") when available.
    answer_records = _answer_page_pairs(a_pages)
    ans_by_key: dict[str, dict] = {}
    ans_by_letter: dict[str, list[dict]] = {}
    for r in answer_records:
        key = canonical_qkey(qnum=r.get("qnum", ""), qsub=r.get("qsub", ""))
        key = _strip_key_noise(key)
        if key:
            ans_by_key[key] = r
            letter = re.sub(r"^\d+", "", key)
            if letter:
                ans_by_letter.setdefault(letter, []).append(r)

    graded_flag: dict[int, bool] = {}
    for p in q_pages:
        graded_flag[p["number"]] = bool(p.get("img_count", 0))

    used_answer_ids: set[int] = set()

    def _pick_answer(sp_label: str) -> dict:
        # 1) exact canonical key match (question-side already has a number prefix)
        key = canonical_qkey(label=sp_label)
        key = _strip_key_noise(key)
        if key and key in ans_by_key:
            r = ans_by_key[key]
            rid = id(r)
            if rid not in used_answer_ids:
                used_answer_ids.add(rid)
                return r
        # 2) letter-only fallback: question label '(a)' pairs with first unused
        #    answer whose letter suffix matches ('1a' | '2a(i)' | anything when
        #    the LABEL letter is the last structural component).
        letter = re.sub(r"^[^a-z]*", "", (sp_label or "").lower())
        letter = re.sub(r"[^a-z].*$", "", letter)
        if letter and letter in ans_by_letter:
            for r in ans_by_letter[letter]:
                if id(r) not in used_answer_ids:
                    used_answer_ids.add(id(r))
                    return r
        return {}

    for pg in q_pages:
        text = pg.get("text", "") or ""
        if not text.strip():
            continue
        subparts = parse_question_subparts(text)
        has_img = graded_flag.get(pg["number"], False) or bool(re.search(r"\bFig\.?\s*\d", text))
        if not subparts:
            # Degenerate page (MCQ-only, prose header, no (a)/(b) markers): keep
            # the real source as one page-level chunk rather than silently
            # dropping its content. Never fabricate a decomposition.
            out.append({
                "text": " ".join(text.split()),
                "label": "",
                "marks": 0,
                "raw_marks": 0,
                "answer_text": "",
                "remarks": "",
                "source": paper.get("question_file", "questionpaper.pdf"),
                "answer_source": paper.get("answer_file", "answerpaper.pdf"),
                "page_start": pg["number"],
                "page_end": pg["number"],
                "kind": "school-papers",
                "subject": meta.get("subject", ""),
                "school": meta.get("school", ""),
                "year": meta.get("year", 0),
                "paper_code": meta.get("paper_code", ""),
                "has_image": has_img,
                "grade": [],
            })
            continue
        for sp in subparts:
            a_rec = _pick_answer(sp["label"])
            out.append({
                "text": sp["text"],
                "label": sp["label"],
                "marks": sp.get("marks", 0),
                "raw_marks": sp.get("raw_marks", 0),
                "answer_text": a_rec.get("answer_text", ""),
                "remarks": a_rec.get("remarks", ""),
                "source": paper.get("question_file", "questionpaper.pdf"),
                "answer_source": paper.get("answer_file", "answerpaper.pdf"),
                "page_start": pg["number"],
                "page_end": pg["number"],
                "kind": "school-papers",
                "subject": meta.get("subject", ""),
                "school": meta.get("school", ""),
                "year": meta.get("year", 0),
                "paper_code": meta.get("paper_code", ""),
                "has_image": has_img,
                "grade": [],  # O-level prelims are S3/S4, currently out of catalogue
            })

    if not out:
        # Graceful degenerate: MCQ-only answer pages only, or text empty --
        # fall through to plain page-chunker on BOTH sides. Chunk provenance
        # must point at the file the text ACTUALLY came from (answer-page
        # chunks must not claim `question_file` as their source).
        for pg in q_pages:
            _fallback_page_chunk(out, pg, paper.get("question_file", "questionpaper.pdf"), meta)
        for pg in a_pages:
            _fallback_page_chunk(out, pg, paper.get("answer_file", "answerpaper.pdf"), meta)

    # Deterministic ordering: page number, then in-page label order.
    return sorted(out, key=lambda c: (c.get("page_start", 0), c.get("label", "")))


def _iter_pdf_pages(path: Path):
    """Yield (page_number, text) for every page of a PDF, or () if unreadable."""
    if pdfplumber is None:
        raise SystemExit("pdfplumber is required for ingest_kb.py (pip install pdfplumber)")
    try:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    yield i, text
    except Exception as e:  # corrupt file / password - skip with a warning, don't abort the run
        print(f"  !! skipping unreadable PDF {path.name}: {e}")


def chunk_text(text: str, doc: dict, *, start_id: int = 0, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Slice `text` into metadata-carrying chunks (deterministic ids, in order).

    doc: {'path': filename, 'kind': 'moe-syllabus'|'school-papers'|...,
          'grade': 'P5'|None, 'page_start': int|None, 'page_end': int|None}
    Every field provenance needs (source, page range, grade) is stamped onto
    EACH chunk, so `generation_record.sources` can cite what actually informed a
    question (knowledge-base/README.md provenance rule).
    """
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return []

    chunks: list[dict] = []
    start = 0
    idx = start_id
    while start < len(cleaned):
        end = min(start + size, len(cleaned))
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append({
                "id": idx,
                "text": piece,
                "source": doc.get("path", "unknown"),
                "kind": doc.get("kind", "unknown"),
                "grade": doc.get("grade"),
                "topic_code": doc.get("topic_code"),
                "page_start": doc.get("page_start"),
                "page_end": doc.get("page_end"),
            })
            idx += 1
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _grade_from_name(name: str) -> str | None:
    """DEPRECATED positional name kept for backwards compat; prefer grade_from_name()."""
    grades = grade_from_name(name)
    return grades[0] if grades else None


# Grade phrases that describe the whole syllabus rather than one grade.
_DOC_TITLE_GRADE_PHRASES = {
    "primary science syllabus": ["P3", "P4", "P5", "P6"],
    "lower secondary science": ["S1", "S2"],
    "g2-3": ["S1", "S2"],
    "upper secondary science": ["S3", "S4"],
}

GRADE_RE = re.compile(r"\b([ps])([1-6])\b", re.IGNORECASE)


def grade_from_name(name: str) -> list[str]:
    """Extract EVERY grade a document covers, NOT just the first. A multi-grade
    syllabus PDF (e.g. 'Primary Science Syllabus 2023.pdf' covers P3-P6) has no
    per-file grade string - lists derived from the DOCUMENT TITLE avoid the trap
    where a per-file-scalar grade='None' file drops out of the grade filter for
    ALMOST EVERY query (hence the retrieval returned 0 for every query on the two
    whole-syllabus PDFs ingested on 2026-09-30). Returns [] if no grade can be
    traced - callers must NOT fabricate a grade from 'guess' for consolidated docs."""
    nl = name.lower()
    for phrase, grades in _DOC_TITLE_GRADE_PHRASES.items():
        if phrase in nl:
            return list(grades)
    found = sorted({(m.group(1) + m.group(2)).upper() for m in GRADE_RE.finditer(name)})
    return found


def renumber_ids(chunks: list[dict]) -> list[dict]:
    """Re-assign chunk ids 0..N-1 across ALL documents so no two chunks collide
    (Qdrant upserts 'same id = overwrite'. seen live: multi-PDF ingest silently
    lost chunks). Mutates and returns the same list for chaining."""
    for i, c in enumerate(chunks):
        c["id"] = i
    return chunks


def stamp_topic_codes(chunks: list[dict], allowed_grades: list[str]) -> list[dict]:
    """Assign `topic_code` (list-typed) to each chunk by matching chunk text against
    the MOE topic catalogue (code/name/subtopic), restricted to the document's
    `allowed_grades` so a P5-only topic code is never stamped into an S1 chunk.

    List-typed because one chunk's text can legitimately hint at more than one
    topic, and Qdrant's `Match` semantics already apply per-item-to-array
    (shredded-must semantics tested in test_rag.py), so this stays symmetric with
    how `grade` is stored. Mutates and returns the same list, for chaining."""
    allowed = {g.strip().upper() for g in allowed_grades}
    catalog = rag_client._load_topic_catalog()
    # Build candidates as (code, subtopic-or-name, single_grade_or_scope_markers).
    # Every entry is checked for two word-blob signals: `code` literal appearing
    # in chunk text (cheap, exact) OR the entry's `name`/`subtopic` prose matching
    # after normalization.
    candidates: list[tuple[str, str, set[str]]] = []  # (code, search_blob, grade_scope)
    for grade_key, topics in catalog.items():
        # grade_key is "P3"/"P4"/.../"S1"/"S2"; S2 shares S1 in catalog.
        if grade_key == "S2":
            grade_key_norm = "S1"  # don't re-add S1's topics under S2's key
        else:
            grade_key_norm = grade_key
        if grade_key not in allowed and grade_key_norm not in allowed:
            continue
        for t in topics:
            code = t.get("code", "").strip().upper()
            if not code:
                continue
            blob = rag_client._normalize(f"{code} {t.get('name','')} {t.get('subtopic','')}")
            candidates.append((code, blob, {grade_key}))

    for chunk in chunks:
        text_norm = rag_client._normalize(chunk.get("text", ""))
        matched: list[str] = []
        for code, blob, scope in candidates:
            if scope & allowed and (code.lower() in chunk.get("text", "").lower() or blob and blob in text_norm):
                if code not in matched:
                    matched.append(code)
        chunk["topic_code"] = matched
    return chunks


def gather_documents(target: str) -> list[dict]:
    """Inventory of all ingestible docs for one folder. No embedding, no Qdrant."""
    folder = KB / target
    if not folder.is_dir():
        return []
    kind = TARGETS[target][1]
    docs = []
    for path in sorted(folder.rglob("*.pdf")):
        rel = path.relative_to(folder).as_posix()
        docs.append({"path": rel, "kind": kind, "grade": grade_from_name(path.name)})
    return docs


def build_chunks_for_doc(path: Path, doc: dict) -> list[dict]:
    """Page-preserving chunking: one flat chunk list across the whole document,
    each chunk carrying the page range of the text it actually came from."""
    out: list[dict] = []
    running_id = 0
    for page_no, page_text in _iter_pdf_pages(path):
        page_doc = {**doc, "page_start": page_no, "page_end": page_no}
        page_chunks = chunk_text(page_text, page_doc, start_id=running_id)
        out.extend(page_chunks)
        running_id += len(page_chunks)
    return out


def _iter_pdf_pages_full(path: Path) -> list[dict]:
    """Like _iter_pdf_pages but returns per-page dicts with text, tables and
    image count (needed by chunk_question_records, which flags has_image)."""
    if pdfplumber is None:
        raise SystemExit("pdfplumber is required for ingest_kb.py (pip install pdfplumber)")
    pages: list[dict] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                tables = page.extract_tables() or []
                pages.append({
                    "number": i,
                    "text": text,
                    "tables": tables,
                    "img_count": len(page.images),
                })
    except Exception as e:
        print(f"  !! skipping unreadable PDF {path.name}: {e}")
    return pages


def _pair_paper_folder(folder: Path) -> list[dict]:
    """Detects (questionpaper.pdf, answerpaper.pdf...) pairs in one paperset
    subfolder. Handles the three shapes documented in the real corpus:
      * questionpaper.pdf + answerpaper.pdf            (most folders)
      * questionandanswer.pdf                          (combined; chemistry-01/03)
      * questionpaper1.pdf (Paper 1 MCQ) + questionpaper.pdf (Paper 2)
        + answerpaper.pdf (combined marking scheme)  (chemistry-04)
    Returns a list of paper dicts for chunk_question_records."""
    files = sorted(p.name for p in folder.iterdir() if p.suffix == ".pdf")
    q_file = next((f for f in files if f.lower().startswith("questionpaper") and "answer" not in f.lower()), None)
    a_file = next((f for f in files if f.lower().startswith("answerpaper")), None)
    qa_file = next((f for f in files if f.lower().startswith("questionandanswer")), None)

    papers: list[dict] = []
    if q_file and a_file:
        papers.append({"folder": folder.name, "question_file": q_file, "answer_file": a_file,
                       "question_file2": None, "answer_file2": None})
    elif qa_file:
        papers.append({"folder": folder.name, "question_file": qa_file, "answer_file": qa_file,
                       "question_file2": None, "answer_file2": None})

    # Extra unpaired question-paper variants (e.g. `questionpaper1.pdf` = MCQ)
    already = {p.get("question_file2") or p["question_file"] for p in papers}
    for f in files:
        if f in already or f.lower().startswith("answerpaper"):
            continue
        if f != q_file and f.lower().startswith("questionpaper"):
            papers.append({"folder": folder.name, "question_file": f, "answer_file": a_file or qa_file or f,
                           "question_file2": None, "answer_file2": None})
    return papers


def _ingest_school_papers(folder: Path, collection: str, *, reset: bool = False, dims: int | None = None) -> int:
    """Iterate paperset subfolders -> question/answer pairs -> structured chunks."""
    all_chunks: list[dict] = []
    subfolders = sorted([d for d in folder.iterdir() if d.is_dir()])
    if not subfolders:
        print(f"[skip] school-papers: no paperset subfolders under {folder}")
        return 0

    for sub in subfolders:
        for paper in _pair_paper_folder(sub):
            q_path = sub / paper["question_file"]
            a_path = sub / paper["answer_file"]
            paper["question_pages"] = _iter_pdf_pages_full(q_path) if q_path.exists() else []
            paper["answer_pages"] = _iter_pdf_pages_full(a_path) if a_path.exists() else []
            if paper["answer_file"] == paper["question_file"]:
                paper["answer_pages"] = []  # combined file: don't double-count
            chunks = chunk_question_records(paper)
            print(f"  {sub.name}/{paper['question_file']}: {len(chunks)} chunk(s)")
            all_chunks.extend(chunks)

    all_chunks = renumber_ids(all_chunks)

    if not all_chunks:
        print(f"[skip] school-papers: nothing to ingest")
        return 0

    if reset and rag_client.QDRANT.collection_exists(collection):
        rag_client.QDRANT.recreate_collection(collection, dims or 768)
        print(f"  [reset] wiped target {collection}")

    vectors = [rag_client.EMBED(c["text"]) for c in all_chunks]
    if dims is None and vectors:
        dims = len(vectors[0])

    uploaded = rag_client.upload_concrete_points(collection, all_chunks, vectors, dims=dims)
    n_with_remarks = sum(1 for c in all_chunks if c.get("remarks"))
    n_with_image = sum(1 for c in all_chunks if c.get("has_image"))
    n_with_school = sum(1 for c in all_chunks if c.get("school"))
    print(f"[done] school-papers -> {collection}: {uploaded} point(s)  "
          f"examiner-remarks={n_with_remarks}  has-image={n_with_image}  school-tagged={n_with_school}")
    return uploaded


def ingest(target: str, *, reset: bool = False, dims: int | None = None) -> int:
    """Full pipeline for one folder: extract -> chunk -> embed -> upsert. Returns #points."""
    collection, _ = TARGETS[target]
    folder = KB / target
    if not folder.is_dir():
        print(f"[skip] {target}: no folder at {folder}")
        return 0

    if target == "school-papers":
        return _ingest_school_papers(folder, collection, reset=reset, dims=dims)

    all_chunks: list[dict] = []
    for path in sorted(folder.rglob("*.pdf")):
        doc_grades = grade_from_name(path.name)
        doc = {"path": path.name, "kind": TARGETS[target][1], "grade": doc_grades}
        doc_chunks = build_chunks_for_doc(path, doc)
        doc_chunks = stamp_topic_codes(doc_chunks, doc_grades or [])
        n_tagged = sum(1 for c in doc_chunks if c.get("topic_code"))
        print(f"  {path.name}: {len(doc_chunks)} chunk(s)  grades={doc_grades}  topic-tagged={n_tagged}")
        all_chunks.extend(doc_chunks)

    # Critical: renumber across ALL docs so no two points collide on id
    # (Qdrant upserts 'same id = overwrite'; without this the second PDF in a
    # folder silently clobbers the first PDF's identically-numbered chunks).
    all_chunks = renumber_ids(all_chunks)

    if not all_chunks:
        print(f"[skip] {target}: no text extracted (empty folder or unreadable PDFs)")
        return 0

    if reset and rag_client.QDRANT.collection_exists(collection):
        rag_client.QDRANT.recreate_collection(collection, dims or 768)
        print(f"  [reset] wiped target {collection}")

    vectors = [rag_client.EMBED(c["text"]) for c in all_chunks]
    if dims is None and vectors:
        dims = len(vectors[0])

    uploaded = rag_client.upload_concrete_points(collection, all_chunks, vectors, dims=dims)
    print(f"[done] {target} -> {collection}: {uploaded} point(s)")
    return uploaded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=sorted(TARGETS.keys()), help="ingest only this folder (default: all)")
    ap.add_argument("--reset", action="store_true", help="drop and recreate the target collection(s) before ingest")
    ap.add_argument("--list", action="store_true", help="inventory only, no embedding/upsert")
    args = ap.parse_args()

    if args.list:
        for t in sorted(TARGETS.keys()):
            docs = gather_documents(t)
            print(f"{t} ({TARGETS[t][0]}): {len(docs)} pdf(s)")
            for d in docs:
                print(f"    {d['path']}  grade={d['grade']}  kind={d['kind']}")
        return 0

    total = 0
    for t in (sorted(TARGETS.keys()) if not args.target else [args.target]):
        print(f"==> ingesting {t}")
        total += ingest(t, reset=args.reset)
    print(f"TOTAL uploaded: {total} point(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())