# 2026-10-01: Setup + Step 0, RAG by hand

**Goal of the day:** go from an empty folder to a working Retrieval-Augmented Generation
(RAG) system, built with no framework, so I understand every moving part before
LangChain hides them.

**Result:** `rag_by_hand.py` (~140 lines) answers questions about Cisco Meraki's API
docs with Claude, citing the chunks it used. Commit `feat: raw RAG pipeline by hand
over Cisco Meraki docs`.

---

## 1. What we did, in order

| # | Step | Why |
|---|---|---|
| 1 | Created `.gitignore` (`.env`, `.venv/`, `chroma_db/`, private Claude config) | Keep secrets, big folders and personal files out of git *before* the first commit |
| 2 | Made a virtual env with Python **3.12** (`python3.12 -m venv .venv`) | Project-only packages. 3.12 rather than 3.14 because chromadb/torch wheels lag new Python releases. |
| 3 | Wrote `requirements.txt`, grouped by the step that needs each package | One command rebuilds the environment on any machine |
| 4 | `pip install -r requirements.txt`, then verified with `pip check` and test imports | "Install finished" doesn't prove the packages work. Importing them does. |
| 5 | Got an Anthropic API key, stored it in `.env` | Secrets live in the environment, never in code |
| 6 | Picked a target company (**Cisco**) and narrowed to **Meraki Dashboard API** | One focused product gives cleaner retrieval than all of Cisco |
| 7 | Scraped 3 docs pages into `data/cisco/*.md` (auth, intro, rate limits) | The knowledge base. cisco.com blocked automated requests (403) and the jobs site needs JavaScript, so we used developer.cisco.com. |
| 8 | Wrote `rag_by_hand.py`: load → chunk → embed → retrieve → generate | The core of this project |
| 9 | Ran experiments (TOP_K, chunk size, out-of-scope questions) | To see *why* RAG fails, not just that it works |
| 10 | Two reviewer sub-agents (RAG pipeline + Python) reviewed the code, and we fixed their findings | Same as a real code review before merging |
| 11 | Redacted a fake example API key from the scraped docs, then pushed to GitHub | GitHub secret scanning can block pushes that contain key-shaped strings |

---

## 2. The concepts (the part to revise)

### What problem does RAG solve?
An LLM only knows its training data. It doesn't know a company's latest API docs, and if
you ask anyway it may **hallucinate** a confident wrong answer. RAG works in two moves:
1. **Retrieve** the few most relevant passages from *your* documents.
2. **Generate** an answer with the LLM, telling it to use *only* those passages.

You can't just paste every document into the prompt. That stops scaling quickly: it costs
more per call, gets slower, and models get worse at finding one fact in a huge context.

### The 5 stages

**1. Load.** Read files and keep `{"source": path, "text": ...}`. The **source travels
with the text the whole way**, or citations become impossible.

**2. Chunk.** Cut text into ~800-character windows with 100 characters of **overlap**.
- *Why chunk?* Embeddings represent a focused passage better than a whole document, and
  you only want to send Claude the relevant parts.
- *Why overlap?* So a fact sitting on a boundary appears whole in at least one chunk.
- *The weakness:* fixed-size character cuts ignore sentences, tables and headings
  (see the experiment below).

**3. Embed.** An embedding model turns text into a vector (here 384 numbers) where
**similar meaning → nearby vectors**. We used `all-MiniLM-L6-v2` from
sentence-transformers. It's local and free, and needed because Claude has no embeddings
API. Gotcha: this model only reads **256 tokens** and silently ignores the rest. Our
longest chunk was 237 tokens, so we were safe, but only just.

**4. Retrieve.** Embed the question the same way, score it against every chunk, keep the
top-k.
- `normalize_embeddings=True` makes every vector length 1. Then **dot product = cosine
  similarity**, so retrieval is one line: `scores = chunk_vecs @ query_vec`.
- `np.argsort(scores)[::-1][:k]` gives the indices of the k highest scores.

**5. Generate.** Build a prompt with numbered sources `[1] (from file, chunk N) ...`
plus the question. A system prompt says: *use ONLY these sources, cite [n], say so
if the answer isn't there.* That instruction is what stops hallucination.

---

## 3. Experiments and what they taught me

| Experiment | Result | Lesson |
|---|---|---|
| Auth question | Top 4 chunks all from `meraki_api_auth.md`, scores 0.63–0.69, correct cited answer | Retrieval works when the question and the text use similar words |
| "MX firewall pricing?" (not in docs) | Claude said the sources don't cover pricing. Top score only **0.44**. | The grounding prompt works. Low scores signal "nothing relevant", which could become a **relevance floor** later. |
| "How long do API keys last?" | Answer: "Permanent until…, text is cut off" | **Chunk boundary bug.** "Permanent until revoked" was split across two chunks. |
| TOP_K 4 → 8 | Still didn't retrieve the "revoked" chunk | More chunks ≠ the right chunk |
| CHUNK_SIZE 800 → 300 | Still didn't | The problem is **structure**: scraping flattened a table, so "revoked" lost its "Token lifetime / API keys" context. Fix = structure-aware splitting (step 2). |

**Big takeaway:** when RAG gives a bad answer, check **retrieval** first (which chunks,
what scores), not the LLM. Most RAG bugs are data and chunking bugs.

---

## 4. Code review fixes (and why each matters)

| Problem | Fix | Lesson |
|---|---|---|
| `overlap >= size` → `range()` crash or silent empty list | Raise a clear `ValueError` | Validate parameters at the boundary and fail loudly |
| Last chunk could just repeat the overlap | Stop at `len(text) - overlap` | Duplicate chunks waste embeddings and crowd out results |
| Whitespace-only chunks | Skip with `if chunk.strip()` | Junk chunks embed to noise that can still rank high |
| Script only worked from the repo root | `Path(__file__).parent / "data" / "cisco"` | Never depend on the current working directory |
| Citations named only the file | Added `chunk_id` | A citation you can't verify isn't worth much |
| Truncated answers looked complete | Check `stop_reason == "max_tokens"` | Always check *why* the model stopped |
| Fallback model could answer silently | Print `(answered by <model>)` | Know which model produced each output |

---

## 5. Engineering habits from today
- `.gitignore` **before** the first commit; secrets only in `.env`; ship a `.env.example`.
- Check secrets without printing them (`print(bool(key))`, never `print(key)`).
- Respect sites that block scraping (cisco.com returned 403, so we used the developer docs).
- Named constants (`CHUNK_SIZE`, `TOP_K`) make experiments a one-line change.
- Get a review before committing, then verify each claim yourself. (One reviewer
  suspected token truncation. We measured it and it wasn't happening.)

---

## 6. Self-quiz

<details><summary>1. Why can't we use Claude to make the embeddings?</summary>
Claude has no embeddings endpoint. It generates text. Embeddings need a separate model
(local sentence-transformers here, or a hosted one like Voyage AI).
</details>

<details><summary>2. Why does normalizing embeddings let us use a dot product?</summary>
Cosine similarity = dot(a, b) / (|a|·|b|). If every vector has length 1, the denominator
is 1, so the dot product *is* the cosine similarity.
</details>

<details><summary>3. What does chunk overlap protect against, and what doesn't it fix?</summary>
It protects facts that straddle a chunk boundary (each appears whole in at least one
chunk). It doesn't fix chunks that lose their *context*, such as a table cell separated
from its header.
</details>

<details><summary>4. The pricing question scored 0.44 and still sent 4 chunks to Claude. Why did the answer stay honest?</summary>
The system prompt told Claude to answer only from the sources and to say when they don't
contain the answer. A relevance floor (skip Claude if the top score is under ~0.5) would
add a second guard.
</details>

<details><summary>5. A RAG answer is wrong. What do you check first?</summary>
The retrieved chunks and their scores. If the right text wasn't retrieved, no prompt or
model can fix it.
</details>

<details><summary>6. Why did we re-embed every chunk on every run, and when does that stop being OK?</summary>
28 chunks embed in under a second, so a database was overkill. With hundreds of documents
it gets slow and wasteful, so step 3 stores vectors once in Chroma and only embeds the
question per query.
</details>

<details><summary>7. How would you explain RAG to a non-technical customer in two sentences?</summary>
"Before the AI answers, we look up the most relevant pages from *your* documents and hand
them to it, like an open-book exam. It answers only from those pages and shows you which
ones it used, so you can check it."
</details>

---

## 7. Next
**Step 1: LangChain loaders.** Replace the hand-written curl + BeautifulSoup scraping with
`WebBaseLoader` / `PyPDFLoader`, and compare what LangChain's `Document` object carries
(content + metadata) with our hand-made dicts. Later goal: grow this into a full chat app.
