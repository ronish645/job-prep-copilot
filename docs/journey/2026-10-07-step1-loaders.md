# 2026-10-07: Step 1, LangChain document loaders

**Goal of the day:** replace step 0's 8-line `load_documents()` with LangChain's
loaders, so one company's knowledge base can mix **saved markdown, PDFs and live web
pages**, and every piece keeps the same, citation-ready metadata.

**Result:** `copilot/loaders.py` (`python -m copilot.loaders cisco`) loads 6 documents
for Cisco: 3 saved `.md` pages and 3 live pages from `data/cisco/urls.txt`. There are 20
offline tests in `tests/test_loaders.py`. Commit `feat: LangChain loaders for markdown, PDF and web sources`.

```text
$ python -m copilot.loaders cisco

Loaded 6 documents ({'markdown': 3, 'web': 3}), 36,208 characters

[1] markdown  6,514 chars  https://developer.cisco.com/meraki/api-v1/authorization/
[2] markdown  3,123 chars  https://developer.cisco.com/meraki/api-v1/introduction/
[3] markdown  8,554 chars  https://developer.cisco.com/meraki/api-v1/rate-limit/
[4] web       8,940 chars  https://developer.cisco.com/meraki/api-v1/getting-started/
[5] web       7,032 chars  https://developer.cisco.com/meraki/api-v1/pagination/
[6] web       2,045 chars  https://developer.cisco.com/meraki/api-v1/errors/
```

Note that step 1 only **loads**. Chunking, embedding and answering still live in
`rag_by_hand.py`. Steps 2 to 4 will rebuild those stages with LangChain on top of this module.

---

## 1. What we did, in order

| # | Step | Why |
|---|---|---|
| 1 | Probed `WebBaseLoader` on two Meraki pages before writing code | Check it works on *this* site first. It did, but the text was padded with dozens of blank lines. |
| 2 | Tried to find a public Meraki PDF to test `PyPDFLoader` | cisco.com returned **403** to scripts (same as step 0). curl then saved the 403 HTML page as `webinar.pdf`, and pypdf crashed on it with "Stream has ended unexpectedly". |
| 3 | Created a `copilot/` package with `loaders.py` | Step 0 was one script. From now on, code that later steps reuse goes in a package. |
| 4 | Added `data/cisco/urls.txt` (3 pages we hadn't saved yet) | Sources become a config file, not code. Adding a company = adding a folder. |
| 5 | Ran it on Cisco, then on a throwaway "demo" folder with a real PDF, a fake PDF, a duplicate URL and a dead URL | Test the unhappy paths on purpose |
| 6 | **Found a bug:** the dead URL loaded as a "document" made of Cisco's 404 page | Fixed with `raise_for_status=True` (section 3) |
| 7 | A Python-reviewer sub-agent found 5 more real issues; we fixed them all | Corrupt PDF / non-UTF-8 file crashing the run, no timeout, unguarded `[0]`, filter order |
| 8 | Wrote 20 offline tests (fake web loader, no network) | Tests become required at step 4. These were cheap and they pin the bugs we found. |

---

## 2. The concepts (the part to revise)

### The `Document`: LangChain's unit of data
Every LangChain loader, splitter, vector store and retriever passes around the same object:

```python
Document(page_content="the text...", metadata={"source": "...", ...})
```

It's exactly the dict we invented in step 0, `{"source": path, "text": ...}`, under
standard names. That common shape is the whole point of a framework: any loader can
feed any splitter, which can feed any vector store.

| Step 0 (by hand) | Step 1 (LangChain) |
|---|---|
| `{"source": ..., "text": ...}` | `Document(page_content=..., metadata={...})` |
| `p.read_text()` over `*.md` | `TextLoader(path).load()` |
| couldn't read PDFs | `PyPDFLoader(path).load()` gives one Document **per page** |
| scraped pages manually, saved as `.md` | `WebBaseLoader(url).load()` fetches + strips HTML live |

### The loader interface
All loaders share one contract: `loader.load() -> list[Document]` (and
`lazy_load()`, a generator that yields Documents one at a time, useful for huge
sources so you don't hold everything in memory). Learn the contract once and every
one of the ~100+ community loaders (Notion, GitHub, S3, Confluence...) works the same way.

### What each loader we used actually does
- **`TextLoader`**: reads a file. Metadata: just `{"source": path}`.
- **`PyPDFLoader`**: wraps the `pypdf` library and returns **one Document per page**,
  with `page` (counted from **0**) in metadata. Per-page Documents mean a citation can
  say "job_posting.pdf p.2". It only reads the PDF's *text layer*: a scanned PDF (an
  image of text) comes back empty and would need OCR.
- **`WebBaseLoader`**: `requests.get(url)` → BeautifulSoup → `soup.get_text()`.
  Metadata: `source`, `title`, `description`, `language` from the HTML `<head>`.
  It does **not** run JavaScript, so pages built in the browser (React apps, many job
  boards) come back nearly empty.

### Why we normalise metadata
Each loader returns *different* metadata keys. If later steps had to know which loader
made each Document, every stage would need `if source_type == ...` branches. So
`make_doc()` gives every Document the same core keys:

```python
{"source": ..., "source_type": "markdown" | "pdf" | "web", "company": ..., "title": ..., **extras}
```

- `source` is **what we cite**. For the step-0 `.md` files we lift the original URL out of
  their `Source:` header line, so citations point to the real page, not a local file.
- `company` will become the Chroma collection / filter in step 3.
- `source_type` lets us debug ("are the bad answers all coming from web pages?").

**Rule of thumb:** metadata is *about* the text (for filtering, citing, debugging);
`page_content` is the text that gets embedded. That's why we **moved** the
`Source:`/`Title:` header out of the content: a URL in the content just adds noise to the embedding.

### Cleaning is part of loading
Web pages came back with dozens of `\n` from HTML layout. `clean_text()` strips trailing
spaces and collapses 3+ newlines into one blank line. **Garbage in = garbage chunks =
garbage retrieval**, and the loader is the cheapest place to fix it.

---

## 3. Code walkthrough (`copilot/loaders.py`)

```text
load_company("cisco")
 ├─ validate name (regex: lowercase, digits, - _)  → blocks "../" path tricks
 ├─ load_markdown(folder)   *.md   → TextLoader  → split_header → make_doc
 ├─ load_pdfs(folder)       *.pdf  → is_real_pdf? → PyPDFLoader → make_doc per page
 ├─ load_web(read_urls())   urls.txt → WebBaseLoader (1 URL at a time) → make_doc
 ├─ drop empty docs
 └─ dedupe by (source, page)
```

Key decisions, and the bug behind each one:

| Code | What would go wrong without it |
|---|---|
| `raise_for_status=True` on `WebBaseLoader` | **Real bug we hit.** By default WebBaseLoader doesn't check the HTTP status, so a 404 page is "successfully loaded" and Cisco's error page text goes into the knowledge base. Claude would then cite it. |
| `is_real_pdf()` checks the first 5 bytes are `%PDF-` | **Real failure we hit.** A blocked download saves an HTML error page with a `.pdf` name. Checking "magic bytes" beats trusting the file extension. |
| `try/except PyPdfError` around `PyPDFLoader` | A truncated or password-protected PDF *has* the right header but still crashes pypdf, and without the `except` it takes the whole run down with it |
| `try/except RuntimeError` around `TextLoader` | A non-UTF-8 file. TextLoader wraps `UnicodeDecodeError` in a `RuntimeError` (the original is on `e.__cause__`) |
| One `WebBaseLoader` per URL, `except requests.RequestException` | One dead link skips one page instead of failing the batch |
| `requests_kwargs={"timeout": 15}` | Without a timeout, a server that never answers hangs the CLI forever |
| `read_urls()` rejects non-`http(s)` lines | Input from a file is still untrusted input |
| Filter empty docs **before** `dedupe` | Otherwise an empty first copy would "win" and hide a good duplicate |
| `os.environ.setdefault("USER_AGENT", ...)` before the import | WebBaseLoader warns at import time without it. Identifying your scraper is also basic web etiquette. |

**Pattern to notice:** every external input (file, PDF, URL) gets **skip + explain on
stderr** rather than **crash** or **silently swallow**. A partial knowledge base with a
clear warning beats both no knowledge base and a quietly polluted one.

### The tests (`tests/test_loaders.py`)
- `FakeWebLoader` replaces `WebBaseLoader` with `monkeypatch`, so tests never touch the
  network. They're fast and deterministic, and still check our cleaning, metadata and error handling.
- `company_dir` fixture: a temp `data/acme/` folder, with `PROJECT_ROOT` pointed at it.
- Each bug from section 1 has a test, e.g. `test_html_saved_as_pdf_is_skipped`,
  `test_truncated_pdf_is_skipped_not_crashing`, `test_empty_copy_does_not_hide_non_empty_duplicate`.
- Run: `pytest` (config in `pyproject.toml`).

---

## 4. Things that surprised us

- **`langchain-community` prints a deprecation warning on import.** It's being sunset in
  favour of standalone packages (`langchain-anthropic`, `langchain-chroma`, ...). The
  loaders still work. Lesson: in AI tooling, packages move fast, so pin versions
  (`requirements.txt`) and read warnings instead of ignoring them.
- **"It loaded" doesn't mean "it loaded the right thing".** Both of our real bugs (the 404 page
  and the HTML-as-PDF file) *looked* like successes at first. Always print a summary and
  eyeball a sample of `page_content`.
- **Big company sites block scripts.** cisco.com returns 403. For PDFs you can't download
  automatically, the practical move is to save them by hand (browser → *Save as PDF*) into
  `data/<company>/`.

---

## 5. Interview questions this step prepares you for

1. **"Walk me through how you ingest documents for RAG."** Loaders turn each source into
   `Document(page_content, metadata)`, I normalise metadata so citations and filters work
   the same for every source type, clean the text, dedupe, then hand it to a splitter.
2. **"Why keep metadata separate from content?"** Metadata is for filtering (`company ==
   cisco`), citing (`source`, `page`) and debugging. Content is what gets embedded. Mixing
   URLs into content adds embedding noise, and you can't filter on it.
3. **"A customer says the bot cites a 404 page. How do you debug it?"** Look at
   ingestion first: was the HTTP status checked? In our case `WebBaseLoader` doesn't check it by
   default, and the fix was `raise_for_status=True` plus a test.
4. **"How would you handle PDFs?"** PyPDFLoader gives one Document per page, so citations can say
   "p.2". Check magic bytes, catch corrupt/encrypted files, and know that scanned PDFs need OCR.
5. **"How do you ingest a JavaScript-heavy site?"** `WebBaseLoader` doesn't run JS. You'd use
   a headless browser loader (Playwright), the site's API or sitemap, or a saved export.
6. **"Why test with a fake loader instead of hitting the real site?"** Tests should be
   fast, deterministic and offline. The site could change or block you, and that shouldn't turn CI red.
7. **"load() vs lazy_load()?"** `load()` returns a full list. `lazy_load()` is a generator
   that yields one Document at a time, for sources too big to hold in memory.

## 6. Key terms

- **Document**: LangChain's `page_content` + `metadata` object, used by every component
- **Document loader**: anything with `load() -> list[Document]`
- **Metadata normalisation**: forcing every source into the same metadata keys
- **Magic bytes**: the first bytes of a file that identify its real type (`%PDF-`)
- **Text layer**: the extractable text inside a PDF. Scanned PDFs don't have one.
- **`raise_for_status`**: turn HTTP 4xx/5xx responses into exceptions instead of content
- **monkeypatch / fake**: swap a real dependency for a stand-in during tests

## 7. Self-quiz (answers are above)

1. What two fields does every `Document` have?
2. Why does `load_markdown` move the `Source:` line into metadata instead of leaving it in the text?
3. PyPDFLoader's `page` metadata starts at what number? What do we store instead?
4. What did WebBaseLoader do with a 404 URL before our fix?
5. Why load URLs one at a time instead of passing the whole list to one loader?
6. Why do we drop empty documents *before* deduping?

## 8. Optional hands-on

- Save a real FDE / Solutions Engineer job posting as a PDF (browser → Print → *Save as
  PDF*) into `data/cisco/`, then rerun `python -m copilot.loaders cisco`. You should see
  `pdf` entries with page numbers.
- Add a second company: `mkdir data/<name>`, put 3 to 5 doc URLs in `urls.txt`, and run the loader.

## Next: step 2, text splitters
In step 0, a fixed 800-character cut split a table row in half and broke retrieval.
Step 2 swaps `chunk_text()` for LangChain's structure-aware splitters
(`RecursiveCharacterTextSplitter`, and header-aware markdown splitting), and we'll re-run
that failed experiment to see if it's fixed.
