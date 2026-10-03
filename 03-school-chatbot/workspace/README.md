# SIMCC School Chatbot — Workspace

Supporting code and data for the SIMCC AI Science Question Generation System
(Project 3 in `demo/03-school-chatbot`). Implements the workflow from the
project overview deck: generate original Singapore MOE Science questions,
ground them on authorised source material, check originality, and hand drafts
to a human approval gate. Nothing here approves its own output.

## What the product actually is

The live product is a standalone **FastAPI form app** (`apps/question-form`),
served on **http://localhost:3000**. It is the sole user interface. There is no
Open WebUI and no chat pipe anymore — those were removed when the stack was
consolidated. See `../docker-compose.yml`: the only services are `qdrant` and
`question-form`.

Flow: pick Grade / Topic / Skill / Difficulty / Quantity (Question type is
locked to MCQ) → the app validates the request against the MOE catalogue →
retrieves grounding from Qdrant → calls the configured AI model → returns draft
MCQs with answers, explanations, syllabus mapping, and an originality check.
Every question comes back `review.status = "draft"` — a human is the approval
gate (deck slide 9).

## Layout

```
workspace/
├── schema/
│   ├── question.schema.json       Canonical question record shape
│   └── topic_catalog.json         ★ Grade→topic catalogue (drives dropdowns +
│                                     server-side validation); traced from the
│                                     real MOE syllabus PDFs
├── prompts/                       Human-readable prompt docs (reference only;
│   ├── generator.md                 the live prompts live in app.py)
│   ├── validator.md
│   └── orchestrator.md
├── tools/                         ★ Load-bearing Python used by the form app
│   ├── rag_client.py              Qdrant + Ollama-embed client; retrieval,
│   │                                grounding block, originality check
│   ├── ai_client.py               Provider-agnostic chat-completions client
│   ├── ingest_kb.py               PDF → chunk → embed → upsert into Qdrant
│   ├── build_topic_catalog.py     Verifies topic_catalog.json against the PDFs
│   ├── test_rag.py                Offline tests (mocked infra)
│   ├── test_ai_client.py          Offline tests
│   └── test_paper_parse.py        Offline tests for the paper parser
└── knowledge-base/                RAG source material (staging for ingestion)
    ├── moe-syllabus/              MOE syllabus PDFs
    ├── simcc-rules/               SIMCC writing rules (currently empty)
    └── school-papers/             Licensed past papers
```

## How grounding (RAG) is loaded

Dropping PDFs into `knowledge-base/` does **not** make them searchable on its
own. They must be ingested into Qdrant first:

```powershell
# run from 03-school-chatbot/ , with Ollama + nomic-embed-text available
python workspace/tools/ingest_kb.py --list
python workspace/tools/ingest_kb.py --target moe-syllabus --reset
python workspace/tools/ingest_kb.py --target school-papers
```

The form app then queries the resulting Qdrant collections (`simcc-syllabus`,
`simcc-school-papers`) at generation time. Embedding model must match at index
and query time (`nomic-embed-text`).

## Verifying the topic catalogue against the PDFs

`topic_catalog.json` is the source of truth for the dropdowns and for the
server-side (grade, topic) validation. To confirm it still matches the MOE
PDFs after any edit:

```powershell
python workspace/tools/build_topic_catalog.py --verify
```

## Offline tests

```powershell
python -m pip install pydantic httpx pdfplumber
python workspace/tools/test_rag.py
python workspace/tools/test_ai_client.py
python workspace/tools/test_paper_parse.py
python apps/question-form/test_form_app.py
```

## Human review workflow (enforced)

1. A request specifies grade / topic / skill / difficulty / quantity (MCQ locked).
2. The app validates it, grounds it, generates, and runs an originality check.
3. Every question is emitted as `draft`; the AI never sets `approved`.
4. A human academic reviewer approves before anything enters a question bank.

Per deck slide 9, the AI cannot approve its own work, decide final difficulty,
or override a reviewer.

## Known gaps

- No question-bank storage yet — approved questions aren't persisted anywhere.
- `simcc-rules/` is empty; adding SIMCC writing rules/rubrics would improve
  question craft.
- Only grades with a syllabus PDF on disk (P3–P6, S1–S2) are supported.
