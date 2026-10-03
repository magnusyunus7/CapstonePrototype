"""SQLite store for generated SIMCC questions (stdlib only, no new dependency).

Why this exists: before this module, /api/generate returned the drafts to the
browser and nothing was kept. Now every draft that the pipeline returns is also
written here, with the request that produced it, the originality result, and the
retrieval hits that grounded it.

What it deliberately does NOT do:
  * It never changes what the pipeline generates or returns.
  * It never approves anything. Every question is stored as `draft`, whatever
    the incoming dict says (the AI never approves its own work - deck slide 9).
    SIMCC reviewers set approved/rejected later; the review columns are there
    for them (reviewed_by / reviewed_at / review_changed), but nothing in this
    module writes them yet.

Tables
  generation_requests  one row per /api/generate call (the spec + model +
                       the real retrieval hits recorded by the server)
  questions            one row per generated question; queryable columns plus
                       `raw_json`, the full question dict exactly as returned
                       to the UI (so nothing the model or pipeline added is lost,
                       including fields the JSON schema does not list yet)

`grounding_sources_json` semantics: NULL = no retrieval happened or it was
unavailable; '[]' = retrieval ran and found nothing; otherwise a list of
{source, page_start, page_end, score}. These come from Qdrant hits, NOT from the
LLM - `generation_record.sources` inside a question is written by the model and
can be invented, so it is kept separately as `llm_sources_json`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
REVIEW_STATUSES = ("draft", "pending_review", "approved", "rejected", "revised")

# Same enum as workspace/schema/question.schema.json -> review.status
_REVIEW_CHECK = ", ".join(f"'{s}'" for s in REVIEW_STATUSES)

DDL = f"""
CREATE TABLE IF NOT EXISTS generation_requests (
    request_id              TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    grade                   TEXT NOT NULL,
    topic_code              TEXT NOT NULL,
    topic_name              TEXT,
    skill                   TEXT NOT NULL,
    difficulty              TEXT NOT NULL,
    format                  TEXT NOT NULL DEFAULT 'MCQ',
    quantity_requested      INTEGER NOT NULL,
    quantity_generated      INTEGER NOT NULL,
    model                   TEXT,
    grounding_sources_json  TEXT
);

CREATE TABLE IF NOT EXISTS questions (
    question_id             TEXT PRIMARY KEY,
    request_id              TEXT NOT NULL REFERENCES generation_requests(request_id),
    created_at              TEXT NOT NULL,
    grade                   TEXT NOT NULL,
    topic_code              TEXT NOT NULL,
    topic_name              TEXT,
    skill                   TEXT NOT NULL,
    difficulty              TEXT NOT NULL,
    format                  TEXT NOT NULL DEFAULT 'MCQ',
    stem                    TEXT NOT NULL,
    options_json            TEXT,
    answer_key              TEXT,
    explanation             TEXT,
    objective_text          TEXT,
    objective_source        TEXT,
    llm_sources_json        TEXT,
    informed_by             TEXT,
    generated_by            TEXT,
    originality_similarity  REAL,
    originality_verdict     TEXT,
    review_status           TEXT NOT NULL DEFAULT 'draft'
                            CHECK (review_status IN ({_REVIEW_CHECK})),
    reviewed_by             TEXT,
    reviewed_at             TEXT,
    review_changed          TEXT,
    raw_json                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_questions_request  ON questions(request_id);
CREATE INDEX IF NOT EXISTS idx_questions_filter   ON questions(grade, topic_code, difficulty);
CREATE INDEX IF NOT EXISTS idx_questions_review   ON questions(review_status);
CREATE INDEX IF NOT EXISTS idx_questions_created  ON questions(created_at);
"""

# Columns a caller may filter list_questions() on (whitelist -> safe to
# interpolate the column name; values are always bound parameters).
_FILTER_COLUMNS = ("grade", "topic_code", "skill", "difficulty", "review_status", "request_id")


class QuestionStoreError(RuntimeError):
    """Any storage failure (collision, bad record, I/O). Callers decide whether
    to surface it; api_generate reports it in the response and still returns the
    questions so a slow generation is never lost to a storage fault."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_question_id(grade: str, topic_code: str) -> str:
    """Q-<GRADE>-<TOPIC>-<6 uppercase hex>, same shape app.py generates."""
    return f"Q-{grade}-{topic_code}-{uuid.uuid4().hex[:6].upper()}"


def default_db_path() -> str:
    """QUESTION_DB_PATH if set; else /data/questions.db when /data exists (the
    Docker volume); else <03-school-chatbot>/data/questions.db for local runs."""
    env = (os.environ.get("QUESTION_DB_PATH") or "").strip()
    if env:
        return env
    if Path("/data").is_dir():
        return "/data/questions.db"
    return str(Path(__file__).resolve().parents[2] / "data" / "questions.db")


def _as_dict(v) -> dict:
    return v if isinstance(v, dict) else {}


def _json(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, default=str)


def _float_or_none(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


class QuestionStore:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = str(path) if path else default_db_path()
        self.init()

    # -------------------------------------------------------------- plumbing --
    def _connect(self) -> sqlite3.Connection:
        # One short-lived connection per operation: safe across FastAPI's
        # threads, and the file is never held open between requests.
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                conn.executescript(DDL)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
        except (OSError, sqlite3.Error) as e:
            raise QuestionStoreError(f"cannot open/initialise question DB at {self.path!r}: {e}") from e

    # ----------------------------------------------------------------- write --
    def save_generation(
        self,
        spec: dict,
        questions: list[dict],
        *,
        grounding_sources: list[dict] | None = None,
        model: str | None = None,
    ) -> dict:
        """Write one request row + all its questions in ONE transaction (all or
        nothing). Returns {"request_id", "saved", "question_ids"}.

        - review_status is forced to 'draft' (never trusts the incoming dict).
        - If a question_id already exists, a fresh id is assigned (and written
          back into the question dict, so the UI response matches the DB);
          nothing already stored is ever overwritten.
        - A duplicate request_id, or a question with no stem, raises
          QuestionStoreError and writes nothing.
        """
        now = _now()
        try:
            with closing(self._connect()) as conn:
                try:
                    conn.execute(
                        "INSERT INTO generation_requests (request_id, created_at, grade, topic_code,"
                        " topic_name, skill, difficulty, format, quantity_requested, quantity_generated,"
                        " model, grounding_sources_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            spec["request_id"], now, spec["grade"], spec["topic"], spec.get("topic_name"),
                            spec["skill"], spec["difficulty"], spec.get("format", "MCQ"),
                            int(spec["quantity"]), len(questions), model,
                            _json(grounding_sources),
                        ),
                    )
                    ids: list[str] = []
                    for q in questions:
                        ids.append(self._insert_question(conn, spec["request_id"], spec, q, now))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        except QuestionStoreError:
            raise
        except (sqlite3.Error, KeyError, TypeError, ValueError) as e:
            raise QuestionStoreError(f"could not store generation {spec.get('request_id')!r}: {e}") from e
        return {"request_id": spec["request_id"], "saved": len(ids), "question_ids": ids}

    def _insert_question(self, conn, request_id: str, spec: dict, q: dict, now: str) -> str:
        stem = q.get("stem")
        if not isinstance(stem, str) or not stem.strip():
            raise QuestionStoreError("question has no stem; refusing to store a partial generation")

        cm = _as_dict(q.get("curriculum_mapping"))
        gr = _as_dict(q.get("generation_record"))
        orig = _as_dict(q.get("originality"))

        qid = q.get("question_id") or new_question_id(spec["grade"], spec["topic"])
        for _attempt in range(6):
            q["question_id"] = qid
            if not isinstance(q.get("review"), dict):  # missing, null, or junk from the model
                q["review"] = {"status": "draft", "reviewed_by": None, "reviewed_at": None, "changed": None}
            q["review"]["status"] = "draft"  # AI never approves its own work
            try:
                conn.execute(
                    "INSERT INTO questions (question_id, request_id, created_at, grade, topic_code,"
                    " topic_name, skill, difficulty, format, stem, options_json, answer_key, explanation,"
                    " objective_text, objective_source, llm_sources_json, informed_by, generated_by,"
                    " originality_similarity, originality_verdict, review_status, raw_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        qid, request_id, now, spec["grade"], spec["topic"], spec.get("topic_name"),
                        spec["skill"], spec["difficulty"], spec.get("format", "MCQ"), stem,
                        _json(q.get("options")), q.get("answer_key"), q.get("explanation"),
                        cm.get("objective_text"), cm.get("objective_source"),
                        _json(gr.get("sources")), gr.get("informed_by"), gr.get("generated_by"),
                        _float_or_none(orig.get("similarity")), orig.get("verdict"),
                        "draft", _json(q),
                    ),
                )
                return qid
            except sqlite3.IntegrityError as e:
                if "question_id" not in str(e):  # not an id collision -> real problem
                    raise
                qid = new_question_id(spec["grade"], spec["topic"])
        raise QuestionStoreError("could not find a free question_id after 6 attempts")

    # ------------------------------------------------------------------ read --
    @staticmethod
    def _question_from_row(row: sqlite3.Row) -> dict:
        """The stored question exactly as returned to the UI, with `review`
        refreshed from the DB columns (they are the source of truth once a
        reviewer starts updating them) plus request_id / created_at."""
        q = json.loads(row["raw_json"])
        q["review"] = {
            "status": row["review_status"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "changed": row["review_changed"],
        }
        q["request_id"] = row["request_id"]
        q["created_at"] = row["created_at"]
        return q

    def get_question(self, question_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM questions WHERE question_id = ?", (question_id,)).fetchone()
        return self._question_from_row(row) if row else None

    def get_request(self, request_id: str) -> dict | None:
        with closing(self._connect()) as conn:
            r = conn.execute("SELECT * FROM generation_requests WHERE request_id = ?", (request_id,)).fetchone()
            if not r:
                return None
            ids = [x["question_id"] for x in conn.execute(
                "SELECT question_id FROM questions WHERE request_id = ? ORDER BY rowid", (request_id,))]
        out = dict(r)
        gs = out.pop("grounding_sources_json")
        out["grounding_sources"] = None if gs is None else json.loads(gs)
        out["question_ids"] = ids
        return out

    def list_questions(self, *, limit: int = 50, offset: int = 0, **filters) -> dict:
        """Newest first. Unknown filter names are rejected (never interpolated)."""
        bad = set(filters) - set(_FILTER_COLUMNS)
        if bad:
            raise QuestionStoreError(f"unsupported filter(s): {sorted(bad)}")
        clauses, params = [], []
        for col in _FILTER_COLUMNS:
            if filters.get(col) is not None:
                clauses.append(f"{col} = ?")
                params.append(filters[col])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        with closing(self._connect()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM questions{where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM questions{where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "items": [self._question_from_row(r) for r in rows]}