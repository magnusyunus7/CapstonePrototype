"""Extract + verify the grade->topic catalogue for SIMCC Science question generation.

Ground truth used by --verify comes from two places that pdfplumber extracts
CLEANLY and reliably (unlike prose/folded-column text, which is torn apart by
layout extraction and is NOT trustworthy for exact name fidelity):

1. Primary P3-P6: per-topic SECTION HEADINGS come from each topic's own
   Learning Outcomes table (page.extract_tables()[0][0][0]), e.g.
   "Energy Forms and Uses (Light) (P4)" or "Interaction of Forces (Magnets) (P3)".
2. Lower Secondary S1-S2: the numbered Topic list under "2.1 Syllabus Framework"
   -> "Table 3" (already clean in extract_text).

Usage:
    python workspace/tools/build_topic_catalog.py          # == --verify
    python workspace/tools/build_topic_catalog.py --extract
    python workspace/tools/build_topic_catalog.py --verify

Exit 0 iff every catalog entry traces to a real syllabus heading AND every
real syllabus heading is represented in the catalog. If either direction is
violated the form dropdown would drift from the real MOE syllabus, so this
fails loudly instead of ignoring it.

Requires: pdfplumber  (pip install pdfplumber)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

ROOT = Path(__file__).resolve().parents[1]  # workspace/
KB = ROOT / "knowledge-base" / "moe-syllabus"
CATALOG_PATH = ROOT / "schema" / "topic_catalog.json"

PRIMARY_PDF = KB / "Primary Science Syllabus 2023.pdf"
LSS_PDF = KB / "G2-3 Lower Secondary Science Teaching and Learning Syllabus.pdf"

CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,6}$")
MIN_TOPIC_LEN = 5
MIN_PREFIX = 35  # chars of normalized name used to pair fuzzy matches (prefix/suffix)

PRIMARY_HEAD_RE = re.compile(
    r"^(?P<title>.*?)\s*\(\s*P(?P<grade>[3-6])(?:\s+(?P<stream>Standard|Foundation))?\s*\)\s*$"
)
LSS_NUM_RE = re.compile(r"(?m)^\s*(\d{1,2})\.\s+([A-Z][^\n]{5,150}?)\s*$")


def norm(s: str) -> str:
    """Aggressive normalization for name comparison: strip punctuation/paren
    titles, collapse whitespace, lowercase. "Non-Living" == "non-living" == "non living"."""
    s = s.replace("\u2022", " ").replace("\u00b7", " ").replace("\u2018", "'").replace("\u2019", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def norm_topic(entry: dict) -> str:
    return norm(f"{entry.get('name','')} {entry.get('subtopic','')}")


def topic_blob(title: str) -> str:
    """Strip the trailing '(P3/Standard/Foundation)' grade&stream marker, keep name+subtopic."""
    m = PRIMARY_HEAD_RE.match(title.strip())
    return norm(m.group("title")) if m else norm(title)


def discover_primary() -> dict[str, list[str]]:
    """Ground truth: scan each per-topic Learning Outcomes table header (page.extract_tables),
    dedupe, and bucket by grade. This is the clean, reliable title source."""
    if pdfplumber is None:
        raise SystemExit("pdfplumber is required")
    by_grade: dict[str, set[str]] = {}
    with pdfplumber.open(str(PRIMARY_PDF)) as pdf:
        for page in pdf.pages:
            for tbl in page.extract_tables() or []:
                if not tbl or not tbl[0]:
                    continue
                head = (tbl[0][0] or "").strip()
                if not head or len(head) < MIN_TOPIC_LEN:
                    continue
                m = PRIMARY_HEAD_RE.match(head)
                if not m:
                    continue
                title = m.group("title").strip().strip(",.:")
                grade = "P" + m.group("grade")
                by_grade.setdefault(grade, set()).add(title)
    return {g: sorted(v) for g, v in by_grade.items()}


def discover_lss() -> list[str]:
    if pdfplumber is None:
        raise SystemExit("pdfplumber is required")
    titles: set[str] = set()
    with pdfplumber.open(str(LSS_PDF)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            for m in LSS_NUM_RE.finditer(t):
                num = int(m.group(1))
                name = m.group(2).strip().strip(",.:")
                if 1 <= num <= 20 and len(name) >= MIN_TOPIC_LEN:
                    # filter obvious TOC/section-label true-section headers
                    if name.isupper() or name.lower().startswith(("table", "section")):
                        continue
                    titles.add(name)
    return sorted(titles)


def matches(a: str, b: str) -> bool:
    """Two normal-ish topic-name strings are 'the same topic' if one is a prefix
    of the other (allowing for one-side subtopic suffix) by >=MIN_PREFIX chars,
    or equal exactly after stripping parens."""
    a, b = a.strip(), b.strip()
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer.startswith(shorter[:MIN_PREFIX]) or shorter == longer[: len(shorter)] and longer.startswith(shorter) and len(shorter) >= MIN_TOPIC_LEN


def verify(catalog: dict) -> list[str]:
    errors: list[str] = []
    seen_codes: dict[str, str] = {}

    def check_code(code: str, where: str):
        if not CODE_RE.match(code):
            errors.append(f"{where}/{code!r}: code must match {CODE_RE.pattern}")
        if code in seen_codes:
            errors.append(f"duplicate code {code!r}: {seen_codes[code]} AND {where}")
        else:
            seen_codes[code] = where

    grades = catalog.get("grades", {})
    for grade, topics in grades.items():
        if not re.fullmatch(r"P[3-6]", grade):
            errors.append(f"grade key {grade!r}: only P3-P6 have MOE syllabus sources on disk")
        if not topics:
            errors.append(f"grade {grade} has an empty topic list")
        for t in topics:
            for f in ("code", "name", "theme"):
                if not t.get(f):
                    errors.append(f"{grade}/{t.get('code','?')}: missing '{f}'")
            if len(t.get("name", "")) < MIN_TOPIC_LEN:
                errors.append(f"{grade}/{t.get('code')}: name too short (likely a typo)")
            check_code(t.get("code", ""), f"{grade}/{t.get('name','?')}")

    shared = catalog.get("grades_lower_secondary_shared", {}).get("topics", [])
    if not shared:
        errors.append("grades_lower_secondary_shared is empty (S1/S2 need this for dropdowns)")
    for t in shared:
        for f in ("code", "name", "theme"):
            if not t.get(f):
                errors.append(f"S1S2/{t.get('code','?')}: missing '{f}'")
        check_code(t.get("code", ""), f"S1S2/{t.get('name','?')}")

    prim = discover_primary()
    lss = discover_lss()
    prim_blob_by_grade = {g: [topic_blob(h) for h in hs] for g, hs in prim.items()}
    lss_blob = [norm(h) for h in lss]

    # forward: catalog -> PDF (every catalog entry must map to a real heading)
    for grade, topics in grades.items():
        for t in topics:
            cat_norm = norm_topic(t)
            if not any(matches(cat_norm, h) for h in prim_blob_by_grade.get(grade, [])):
                errors.append(
                    f"{grade}/{t['code']}: {t['name']!r} has no matching per-topic Learning "
                    f"Outcomes heading in {PRIMARY_PDF.name}"
                )

    for t in shared:
        cat_norm = norm_topic(t)
        if not any(matches(cat_norm, h) for h in lss_blob):
            errors.append(f"S1S2/{t['code']}: {t['name']!r} has no matching Table 3 entry in {LSS_PDF.name}")

    # reverse: PDF -> catalog (no real syllabus topic may be silently missing)
    for grade, headings in prim_blob_by_grade.items():
        cat_norms = [norm_topic(t) for t in grades.get(grade, [])]
        for h in headings:
            if not any(matches(h, c) for c in cat_norms):
                errors.append(
                    f"{grade}: syllabus topic {h!r} MISSING from {CATALOG_PATH.name} "
                    f"(grade would silently omit a real dropdown option)"
                )
    for h in lss_blob:
        cat_norms = [norm_topic(t) for t in shared]
        if not any(matches(h, c) for c in cat_norms):
            errors.append(
                f"S1S2: syllabus topic {h!r} MISSING from {CATALOG_PATH.name} "
                f"(S1/S2 would silently omit a real dropdown option)"
            )

    return errors


def extract():
    print("\n--- Primary P3-P6 per-topic headings (ground truth) ---")
    for grade, headings in discover_primary().items():
        for h in headings:
            print(f"  [{grade}]  {h}")
    print("\n--- Lower Secondary S1-S2 Table 3 numbered topics (ground truth) ---")
    for h in discover_lss():
        print(f"  16x  {h}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--verify", action="store_true", help="(default) confirm catalog matches MOE syllabus PDFs")
    args = ap.parse_args()

    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)

    if args.extract:
        extract()
        return 0

    issues = verify(catalog)
    if issues:
        print(f"CATALOG VERIFY FAILED ({len(issues)} problem(s)) in {CATALOG_PATH.name}:")
        for e in issues:
            print("  -", e)
        return 1

    total = sum(len(v) for v in catalog.get("grades", {}).values()) + len(
        catalog.get("grades_lower_secondary_shared", {}).get("topics", [])
    )
    print(f"CATALOG VERIFY OK: {total} topics (P3-P6 + S1/S2) all traceable to real MOE syllabus sections.")
    return 0


if __name__ == "__main__":
    sys.exit(main())