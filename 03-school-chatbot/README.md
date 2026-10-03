# SIMCC AI Science Question Generator

A prototype that generates original Singapore MOE Science questions (MCQ),
grounded on authorised source material, with an originality check and a
mandatory human-approval gate. See `../01 PROJECT OVERVIEW/` for the project
brief.

The product is a standalone **FastAPI form app** backed by **Qdrant** (vector
DB for RAG). It calls a configurable AI model for generation. There is no Open
WebUI and no chat UI — the form is the whole interface.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) + Docker Compose v2 (Docker Desktop on Windows)
- [Ollama](https://ollama.com/) running on the host with the `nomic-embed-text`
  model pulled (used for RAG embeddings, even though generation uses the
  configured chat model):
  ```powershell
  ollama pull nomic-embed-text
  ```
- Network access to the configured AI model endpoint (see `.env`)

## Setup

1. Create `.env` and the API-key secret (both are gitignored, so they don't
   exist on a fresh clone):

   ```powershell
   Copy-Item .env.template .env
   # then edit .env, and create the key file:
   #   secrets/ai_api_key   (contains only the bearer token, nothing else)
   ```

   In `.env`, the model connection is these lines (config-only, no UI):
   - `AI_PROVIDER`   — `openai_compat` or `ollama`
   - `AI_BASE_URL`   — chat-completions base URL
   - `AI_API_KEY_FILE` — path to the mounted secret (`/run/secrets/simcc/ai_api_key`)
   - `AI_MODEL`      — model tag

2. Start it:

   ```powershell
   docker compose up -d
   ```

3. Ingest the knowledge base into Qdrant (first run, or after `down -v`, or
   after adding new PDFs):

   ```powershell
   python workspace/tools/ingest_kb.py --target moe-syllabus --reset
   python workspace/tools/ingest_kb.py --target school-papers
   ```

4. Open http://localhost:3000 and generate questions.

## Services

| Service         | Image / build            | Port | Purpose                          |
|-----------------|--------------------------|------|----------------------------------|
| `question-form` | `./apps/question-form`   | 3000 | The form UI + generation backend |
| `qdrant`        | `qdrant/qdrant:latest`   | 7333 | Vector DB for RAG                |

Ports 3000/7333 are deliberately not the common 8080/6333 defaults, so this
stack coexists with any other local Open WebUI / Qdrant install.

## Changing the AI model (config-only)

No UI or code change. Edit the four `AI_*` lines in `.env` (and the key file if
the provider changes), then recreate the container:

```powershell
docker compose up -d --force-recreate --no-deps question-form
```

## Stop

```powershell
docker compose down          # stop, keep the Qdrant data volume
docker compose down -v       # stop AND wipe the vector DB (needs re-ingest after)
```

## How it works (short version)

Request (Grade/Topic/Skill/Difficulty/Quantity, MCQ locked) → validate against
the MOE topic catalogue → retrieve grounding from Qdrant (syllabus + school
papers) → generate with the configured model → originality check → return draft
MCQs with answers, explanations, and syllabus citations. Every question is
`draft`; a human approves before use (deck slide 9).

More detail: `workspace/README.md`. Full project history: `context.md`.
