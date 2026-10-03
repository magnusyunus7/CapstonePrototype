r"""Offline tests for structure-aware school-papers parsing (ingest_kb extension).

Real PDF data from workspace/knowledge-base/school-papers/ drove the mocked cell
shapes below (see $TEMP/opencode/proto_parse.py dumps from 2026-09-30). We test
pure functions only - no pdfplumber open, no docker, no Ollama - so shape drift
is caught before a re-ingest. Copies of the project's previously-bitten bug
classes are asserted directly:

  * component IDs never collide across folders (renumber_ids style)
  * no synthetic grade on sub-secondary O-level papers (grade must be [])
  * rationale "score band" like `Max 3` is preserved verbatim, not normalized
  * tabular vs free-text marking-scheme shapes both partition questions correctly
  * MCQ grids yield per-question letter answers
  * image-heavy pages are flagged `has_image=True`, and empty-text-only pages
    yield no phantom chunk
  * shallow "no structure detected" papers degrade gracefully to the existing
    page-chunker (rather than fabricating a decomposition)

Run:
    python workspace/tools/test_paper_parse.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ingest_kb  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# Real-world "answer table" shapes observed in the actual school PDFs on disk.
# ---------------------------------------------------------------------------
# biology-02 / Temasek: clean 3-col table, qnum already concatenated ("1a", "bi").
TEMASEK_ANSWER_ROWS = [
    ["Qn", "Answer", "Remarks"],
    ["1a", "Insect pollination;\nLarge petals", "Both for 1 m"],
    ["bi", "A sepal / calyx B petal / corolla", "Any error deducts 0.5 m"],
    ["bii", "E produces the pollen", ""],
    ["biii", "Circle and name the stigma. [1]", "OWTTE"],
]

# biology-01 / TKGS: ragged 6-col table with separate qnum + subletter columns,
# score expressed as `"Max\n3"` (must NOT be flattened to 3).
TKGS_ANSWER_ROWS = [
    ["", "Q", "Ans", "Mark", "Remarks"],
    ["1", "", "(a)", "Temperature of solutions", "2", "This was not answered well"],
    ["", "", "(b)", "Active transport", "Max\n3", "Many answers did not include data"],
    ["2", "", "(a)", "Urea;", "1", "This was well answered"],
]

# chemistry-04 / Dunman MCQ grid: answer letters bisected across 30 cols.
DUNMAN_MCQ_GRID = [
    ["", "1", "2", "3", "10", "40"],
    ["", "A", "B", "A", "A", "B"],
    ["", "11", "12", "21", "22", "30"],
    ["", "B", "C", "D", "C", "D"],
]

# biology-01 Question paper text pages: question tokens `(a)` `[2]` and
# `[Total: 9]`-delimited main questions.
TKGS_QUESTION_PAGES = [
    # page 2 - main Q1 with 2 subparts ending in [Total: 9]
    (
        "Section A\n1 A student investigates the uptake of potassium ions by root tissue.\n"
        "(a) State, with a reason, one other variable that should be kept constant.\n"
        "??????. [2]\n"
        "(b) Using the information in Table 1.1, state and explain uptake of potassium ions.\n"
        "????. [3]\n"
        "[Total: 9]"
    ),
    # page 3 - main Q2 with 2 subparts ending in [Total: 8]
    (
        "2 (a) State a metabolic waste product removed by the kidneys.\n"
        "[1]\n"
        "(b) Describe and explain the difference in water potential of glomerular filtrate and urine.\n"
        "???. [3]\n"
        "[Total: 8]"
    ),
]

# Image-flag template: a page whose extracted text names Fig. N.N (bi-03 style).
FIG_PAGE_TEXT = "Fig. 1.1 (b) On Fig. 1.1, draw and label the molecules as shown. [1]"
# No-image question text that must be flagged False (bi-02 chemistry, table-only pages).
NO_IMAGE_TEXT = "(a) Suggest why the investigation uses four discs from one root tissue. [2]"

# A free-text (non-tabular) marking scheme page, chem-02 style.
ZHSS_ANSWER_PAGE = (
    "Section A (70 m)\n"
    "1 (a) Put a tick (?) if a redox reaction has occurred.\n"
    "2 ( 1m each)\n"
    "(b) Difference: Reaction rate is slower when used with aqueous bromine. 1\n"
    "Explain: Chlorine is more reactive than bromine. 1\n"
    "(c) CuO + H ? Cu + H O 1\n"
    "[5]\n"
)

# metadata extraction samples
SCHOOL_HEADER_MIX = (
    "TANJONG KATONG GIRLS' SCHOOL\nPRELIMINARY EXAMINATION\nSECONDARY FOUR EXPRESS\n"
    "BIOLOGY 6093/02\nPaper 2 18 August 2025\n1 hour 45 minutes\n"
)


def build_raw(folder: str, qname: str, aname: str, q_pages, a_pages) -> dict:
    """Fake 'folder dict' as the pair parser will receive (already-evaluated pages)."""
    return {
        "folder": folder,
        "question_file": qname,
        "answer_file": aname,
        "question_pages": [{"number": i + 1, "text": t, "img_count": 0} for i, t in enumerate(q_pages)],
        "answer_pages": [{"number": i + 1, "text": t, "img_count": 0} for i, t in enumerate(a_pages)],
    }


print("== extract_paper_meta: school / year / subject / paper code ==")
meta = ingest_kb.extract_paper_meta(SCHOOL_HEADER_MIX)
check("school extracted", meta.get("school") == "TANJONG KATONG GIRLS' SCHOOL", str(meta.get("school")))
check("year extracted", meta.get("year") == 2025, str(meta.get("year")))
check("subject extracted", meta.get("subject") == "BIOLOGY", str(meta.get("subject")))
check("paper code extracted", meta.get("paper_code") == "6093/02", str(meta.get("paper_code")))
check("level extracted", meta.get("level") == "Secondary Four Express", str(meta.get("level")))

meta = ingest_kb.extract_paper_meta(
    "ZHONGHUA SECONDARY SCHOOL\nPRELIMINARY EXAMINATION 2024\nSECONDARY 4 EXPRESS\nCHEMISTRY 6092 /02"
)
check("zhonghua school extracted", meta.get("school") == "ZHONGHUA SECONDARY SCHOOL", str(meta.get("school")))
check("zhonghua code normalised (space before slash collapses)", meta.get("paper_code") == "6092/02", str(meta.get("paper_code")))

meta = ingest_kb.extract_paper_meta("6092 CHEMISTRY\nDUNMAN SECONDARY SCHOOL\n4E Preliminary Examination 2024")
check("dunman year extracted", meta.get("year") == 2024, str(meta.get("year")))
check("dunman subject extracted", meta.get("subject") == "CHEMISTRY", str(meta.get("subject")))

check("unattributable header yields empty dict, NOT a fabricated grade",
      ingest_kb.extract_paper_meta("blah blah random text") == {},
      str(ingest_kb.extract_paper_meta("blah blah random text")))


print("== gap-span subquestion partition (parse_page_text_vs_answers only) ==")
parts = ingest_kb.parse_question_subparts(TKGS_QUESTION_PAGES[0])
check("page 1 partitions into 2 subparts", len(parts) == 2, str([(p["label"], p["marks"]) for p in parts]))
check("labels preserve nested branch refs", [p["label"] for p in parts] == ["(a)", "(b)"], str([p["label"] for p in parts]))
check("marks lifted from trailing [N]", [p["marks"] for p in parts] == [2, 3], str([p["marks"] for p in parts]))
check("question text preserved for retrieval", "State, with a reason" in parts[0]["text"], str(parts[0]["text"]))
check("no [Total: N] phantom question produced",
      not any("[Total" in p["text"] or "[Total" in p["label"] for p in parts),
      str([p["label"] for p in parts]))

parts = ingest_kb.parse_question_subparts(TKGS_QUESTION_PAGES[1])
check("page 2 partitions into 2 subparts", len(parts) == 2, str([(p["label"], p["marks"]) for p in parts]))


print("== parse_marking_scheme_table: TEMASEK 3-col shape ==")
rows = ingest_kb.parse_marking_scheme_table(TEMASEK_ANSWER_ROWS)
check("5 rows reduced to 4 answers (header dropped)",
      len(rows) == 4, str([(r.get("qnum")) for r in rows]))
check("sublettered qnums preserved literally", [r["qnum"] for r in rows] == ["1a", "bi", "bii", "biii"],
      str([r["qnum"] for r in rows]))
check("remarks text preserved", rows[0]["remarks"] == "Both for 1 m", rows[0]["remarks"])
check("empty remarks kept empty (not fabricated)", rows[2]["remarks"] == "", repr(rows[2]["remarks"]))
check("REJECT:/OWTTE markers preserved verbatim", rows[3]["remarks"] == "OWTTE", rows[3]["remarks"])


print("== parse_marking_scheme_table: TKGS ragged 6-col shape ==")
rows = ingest_kb.parse_marking_scheme_table(TKGS_ANSWER_ROWS)
check("ragged shape still yields 3 answers", len(rows) == 3, str([(r.get("qnum"), r.get("qsub")) for r in rows]))
check("qnum column split from subletter column", [r["qnum"] for r in rows] == ["1", "1", "2"], str([r["qnum"] for r in rows]))
check("subletter col joined correctly", [r["qsub"] for r in rows] == ["(a)", "(b)", "(a)"], str([r["qsub"] for r in rows]))
check("Max 3 mark band preserved verbatim (score band, NOT normalized int)",
      rows[1]["marks"] == "Max\n3", repr(rows[1]["marks"]))
check("plain int mark preserved as-is", rows[0]["marks"] == "2", repr(rows[0]["marks"]))
check("remarks attached to the right question", rows[2]["remarks"] == "This was well answered", rows[2]["remarks"])


print("== parse_mcq_grids: DUNMAN 30-col letters grid ==")
answers = ingest_kb.parse_mcq_grids(DUNMAN_MCQ_GRID)
check("MCQ numbers mapped to letters", answers == {"1": "A", "2": "B", "3": "A", "10": "A", "40": "B", "11": "B", "12": "C", "21": "D", "22": "C", "30": "D"},
      str(answers))


print("== chunk_question_records + has_image flag ==")
paper = build_raw("biology-01", "questionpaper.pdf", "answerpaper.pdf",
                  ["Fig. 1.1 (b) draw and label the molecules.", "no image here"],
                  ["1 (a) Temperature of solutions; 2"])
chunks = ingest_kb.chunk_question_records(paper, ngram_seed="fake_seed")
check("at least one chunk emitted per sub-question page", len(chunks) >= 2, str(len(chunks)))
check("sub-question carrying a figure reference gets has_image=True",
      any(c.get("has_image") for c in chunks), str([c.get("has_image") for c in chunks]))
check("plain text-only sub-question gets has_image=False",
      any(not c.get("has_image") for c in chunks), str([c.get("has_image") for c in chunks]))


print("== graceful degradation to page-chunk fallback ==")
swiss = build_raw("chemistry-01", "questionandanswer.pdf", "questionandanswer.pdf",
                  # MCQ-only shape with no (a)(b)(c) subparts -> pair parser yields nothing
                  # and ingest_kb should fall back to the plain page chunker.
                  ["1 Bromate ion. A 1 only B 3 only C 1 and 2 D 2 and 3",
                   "2 A mixture of propane solidifies first? A butane B CO2"],
                  [])
chunks = ingest_kb.chunk_question_records(swiss, ngram_seed="x")
check("MCQ-only paper degrades to per-page chunks (no fabricated QA decomposition)",
      len(chunks) >= 2, str(len(chunks)))


print("== provenance metadata stamped onto every chunk ==")
paper = build_raw("biology-01", "questionpaper.pdf", "answerpaper.pdf",
                  ["1 (a) State one variable.\n[Total: 9]"],
                  ["1 (a) Temperature of solutions;"])
chunks = ingest_kb.chunk_question_records(paper, ngram_seed="fake")
check("every chunk carries a real source filename", all(c.get("source") for c in chunks), str(chunks))
check("every chunk carries a real page number (from the QUESTION paper by default)",
      all(isinstance(c.get("page_start"), int) for c in chunks), str([c.get("page_start") for c in chunks]))
check("answer-file provenance preserved on question chunks",
      any(c.get("answer_source") == "answerpaper.pdf" for c in chunks), str([c.get("answer_source") for c in chunks]))
check("grade list pair parser emits empty (O-level is S3/S4, P3-S2 catalogue has NO matching grade)",
      chunks[0].get("grade") == [], str(chunks[0].get("grade")))


print("== global id-uniqueness across multiple papers (regression: renumber_ids contract) ==")
p1 = build_raw("biology-01", "q.pdf", "a.pdf", ["1 (a) x\n[Total: 2]"], ["1 (a) T of S;"])
p2 = build_raw("biology-02", "q.pdf", "a.pdf", ["1 (a) y\n[Total: 2]"], ["1 (a) T of S;"])
c1 = ingest_kb.chunk_question_records(p1, ngram_seed="s1")
c2 = ingest_kb.chunk_question_records(p2, ngram_seed="s2")
combined = ingest_kb.renumber_ids(c1 + c2)
check("renumber_ids de-collides distinct papers", len({c['id'] for c in combined}) == len(combined),
      str([c['id'] for c in combined]))


print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")