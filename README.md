# Job Prep Copilot

**A RAG-powered research assistant for technical job interviews.** Point it at a company's
docs, API references and job postings, and ask questions like *"How does their API handle
auth?"*. It answers with Claude and cites the exact source chunks it used.

I'm building it for my own Forward Deployed Engineer / Solutions Engineer job search, and
to learn Retrieval-Augmented Generation properly: first **by hand**, then with **LangChain**,
then as a **full chat app**. Every step comes with a dated learning note in
[`docs/journey/`](docs/journey/).

> **Status:** Step 1 complete. Step 0 is a working RAG pipeline in ~140 lines of plain
> Python over Cisco Meraki's API docs. Step 1 adds LangChain loaders that build a
> company's knowledge base from saved markdown, PDFs and live web pages.

---

## Demo

```text
$ python rag_by_hand.py "How does the Meraki API authenticate requests, and how long do tokens last?"

Loaded 28 chunks from data/cisco

Retrieved chunks:
  [1] score=0.694  data/cisco/meraki_api_auth.md (chunk 4)
  [2] score=0.680  data/cisco/meraki_api_auth.md (chunk 0)
  [3] score=0.647  data/cisco/meraki_api_auth.md (chunk 7)
  [4] score=0.630  data/cisco/meraki_api_auth.md (chunk 9)

Answer:
The Meraki Dashboard API supports two forms of authentication [2]:
- App-scoped access: OAuth 2.0 grants, for third-party apps and org-wide automation [2]
- Admin-scoped access: API keys, for personal scripts and admin-specific tasks [2]

Requests use Bearer auth in the standard Authorization header [1] ...
OAuth tokens last 60 minutes and auto-refresh [2] ...

(answered by claude-opus-5-5)
```

It also refuses honestly. Asked *"How much does a Meraki MX firewall cost?"*, it replies
that **the sources don't include pricing** instead of making up a number.

---

## How it works

```text
 data/cisco/*.md
      │  1. load            read every document, remember its source path
      ▼
 documents
      │  2. chunk           800-char windows, 100-char overlap
      ▼
 28 chunks ──3. embed──▶ 28 vectors (384-dim, all-MiniLM-L6-v2, runs locally)
                                  │
 question ──3. embed──▶ query vector
                                  │  4. retrieve: cosine similarity → top 4
                                  ▼
                         top-k chunks, numbered [1]..[4]
                                  │  5. generate: Claude answers ONLY from these,
                                  ▼     citing [n]
                         answer with citations
```

| Stage | Function | Why it exists |
|---|---|---|
| Load | `load_documents` | Keeps the source path attached to every document so citations are possible |
| Chunk | `chunk_text`, `chunk_documents` | LLM context is finite and embeddings work best on focused passages |
| Embed | `embed` | Turns text into vectors where *similar meaning = nearby vectors* |
| Retrieve | `retrieve` | Normalized vectors make a dot product equal cosine similarity, so the top-k are the closest |
| Generate | `ask_claude` | Grounds the answer in retrieved text and forces citations |

---

## Quickstart

Requires **Python 3.12** and an [Anthropic API key](https://console.anthropic.com).

```bash
git clone https://github.com/ronish645/job-prep-copilot.git
cd job-prep-copilot

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then put your real key in .env

python rag_by_hand.py "How many API requests per second does Meraki allow?"
```

To use a different company, drop `.md` files into a folder under `data/` and point
`DATA_DIR` in `rag_by_hand.py` at it.

### Loading a company's sources (step 1)

Put everything for one company in `data/<company>/`:

| Source | How | Loader |
|---|---|---|
| Saved pages | `*.md` files | `TextLoader` |
| PDFs (e.g. a job posting) | `*.pdf` files | `PyPDFLoader`, one document per page |
| Live pages | one URL per line in `urls.txt` | `WebBaseLoader` |

```bash
python -m copilot.loaders cisco    # prints every loaded document and its metadata
pytest                             # 20 offline tests
```

Every document gets the same metadata (`source`, `source_type`, `company`, `title`), so
later steps can cite and filter the same way for every source type. Broken inputs (404s,
HTML saved as `.pdf`, corrupt PDFs, non-UTF-8 files) are skipped with a warning, so they
neither crash the run nor slip into the knowledge base.

---

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Build style | By hand first, LangChain second | Understand what the framework hides before using it |
| LLM | Claude (`anthropic` SDK) | Strong instruction-following for "answer only from sources + cite" |
| Embeddings | `sentence-transformers` / all-MiniLM-L6-v2 | Claude has no embeddings API. This model is free, local and fast. |
| Vector store | NumPy array (step 0), Chroma (step 3) | 28 vectors don't need a database yet |
| Secrets | `.env` + `python-dotenv` | Keeps keys out of code and git |

## What I learned so far

- **Chunk boundaries matter more than tuning knobs.** A table row ("Token lifetime:
  Permanent until revoked") was flattened by scraping and split by the chunker. Raising
  `TOP_K` from 4 to 8, or shrinking chunks from 800 to 300 characters, still didn't
  retrieve it. The fix is structure-aware splitting (step 2), not bigger numbers.
- **The prompt is a safety rail.** "Use ONLY the sources, say so if they don't contain
  the answer" made Claude admit missing information instead of guessing.
- **"Loaded" doesn't mean "loaded the right thing".** `WebBaseLoader` happily loaded a
  404 page as content until I set `raise_for_status=True`, and a blocked download saved
  an HTML error page as `.pdf`. Now both are caught and tested.
- **Similarity scores are a useful signal.** On-topic questions scored about 0.69, an
  off-topic pricing question about 0.44, so a relevance floor is a cheap future guard.

Full notes: [`docs/journey/`](docs/journey/)

---

## Roadmap

- [x] **0.** Raw RAG by hand: load → chunk → embed → retrieve → answer with sources
- [x] **1.** LangChain document loaders (`WebBaseLoader`, `PyPDFLoader`)
- [ ] **2.** Structure-aware text splitters
- [ ] **3.** Persistent vector store: Chroma, one collection per company
- [ ] **4.** Retriever + LCEL chain that returns sources (tests start here)
- [ ] **5.** Structured company briefing (Pydantic): product, customers, tech stack, demo idea
- [ ] **6.** Multi-turn chat with history
- [ ] **7.** Evals: 20 labeled questions, LangSmith
- [ ] **8.** FastAPI backend + chat UI + deployment
- [ ] **Later:** Mock Customer Simulator, role-play discovery calls graded by an LLM judge

## Tech stack

Python 3.12 · Anthropic Claude · sentence-transformers · NumPy · LangChain · pytest ·
Chroma (upcoming) · FastAPI (upcoming)

## Data note

`data/cisco/` contains excerpts of Cisco Meraki's public developer documentation
([developer.cisco.com/meraki](https://developer.cisco.com/meraki/)), used only as a
sample corpus for learning. All rights belong to Cisco.
