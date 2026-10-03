# Knowledge Base (RAG Upload)

This folder is the staging area for SIMCC-authorised source material to be
embedded into Qdrant through Open WebUI's document RAG pipeline. The
Generator and Validator treat these three classes as the ONLY allowed
grounding. Anything outside this set must never inspire a question (see
`workspace/prompts/generator.md` non-negotiable #5).

## Subfolders and what goes in each

### `moe-syllabus/`
MOE Science syllabus documents, one file per grade (P3, P4, P5, P6, ...).
Content the AI needs: topic names, learning objectives, learning experiences,
and any stated cognitive-skill expectations per grade. PDF or Markdown.

Naming convention: `moe-science-syllabus-<grade>.<ext>` — e.g. `moe-science-syllabus-p5.pdf`.

### `simcc-rules/`
SIMCC's internal question-writing rules, difficulty expectations, style guide,
and reviewer feedback patterns. PDF, Markdown or plain text.

One rule per block where possible so retrieval chunks land cleanly.

Naming convention: `simcc-question-writing-rules.<ext>`, `simcc-difficulty-rubrics.<ext>`.

### `school-papers/`
Only school papers SIMCC is licensed to use. Keep one PDF per paper; name
them so the reviewer can trace every `generation_record.sources` entry back to
a specific paper and question (e.g. `2023-AIS-P5-SA2.pdf`).

**Do not copy or dump exact question text into prompts unless the AI is
already working from these exact documents** — the point is grounding, not
memorisation, per the project overview (source material is not a database to
copy existing questions from).

## Uploading to Qdrant via Open WebUI

1. Start the stack (see `../README.md`).
2. Open http://localhost:3000 -> Admin Panel -> Documents.
3. Upload each PDF/Markdown file here (or drag-drop into a Workspace Knowledge).
4. Tag files by class where the UI allows (e.g. `moe-syllabus`, `simcc-rules`,
   `school-papers`) so queries can filter scope.
5. The vector DB (`school-chatbot-qdrant`, exposed on host port 7333 for REST)
   stores the embeddings; no further manual step needed.

## Provenance rule

Every `generation_record.sources` entry in a generated question JSON should
list filenames or question IDs from these folders — that is the trace the
PPTX (slide 7) requires before human approval. If the model cites a source that
does not exist here, reject the question regardless of quality.