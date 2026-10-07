# 2026-10-07: Step 2, structure-aware text splitters

**Goal of the day:** fix the bug step 0 couldn't. *"How long do Meraki API keys last?"*
failed because the answer ("Permanent until revoked") sat in a table that our fixed
800-character chunker cut up and stripped of meaning. Bigger `TOP_K` and smaller chunks
didn't help. Step 2 replaces character windows with chunks that follow the document's
**structure**: headings, tables, code blocks.

**Result:** the right chunk for the API-key question went from **rank 41 → rank 1**, and
Claude now answers it correctly with a citation. New files: `copilot/splitters.py`,
`copilot/snapshot.py`, `experiments/retrieval_eval.py`, plus a changed web loader. 44 tests.

```text
$ python -m experiments.retrieval_eval     hit@4 = right chunk in top 4, MRR = avg of 1/rank

                         dev hit@4  dev MRR   held-out hit@4  held-out MRR   chunks
fixed 800-char (step 0)     9/11     0.66          5/5            0.77          56
structured (step 2)        11/11     0.93          5/5            0.80         144
```

**The most important lesson of the day was *not* the splitter.** It was: **measure
before and after every change, and expect some changes to make things worse.** Two
of our four attempts did (section 3).

---

## 1. What we did, in order

| # | Step | Why |
|---|---|---|
| 1 | Re-read step 0's failure, then looked at the **live HTML** of the auth page | The `.md` file had lost the table, but the HTML still had a real `<table>` with an "API Keys" column. **A splitter can't recover structure the loader threw away.** |
| 2 | Tested `html2text` (HTML → Markdown) | Headings become `#`, tables become `a \| b \| c`. Structure survives as plain text. |
| 3 | Chose `WebBaseLoader.scrape()` + our own html2text config over LangChain's `AsyncHtmlLoader` + `Html2TextTransformer` | The ready-made pair has `raise_for_status` off by default (our step-1 bug again) and can't turn off 78-char line wrapping |
| 4 | Caught **mojibake** (`â ï¸`) in a quick raw `requests` test | `scrape()` already fixes encoding with `apparent_encoding`. One more reason to keep it. |
| 5 | **Wrote the eval first** (`experiments/retrieval_eval.py`) and recorded the baseline | Without a number from *before*, "it's better now" is just a feeling |
| 6 | Added 3 more questions *before* building anything | So I couldn't pick questions that flatter the new code |
| 7 | Found that `# comments` in code samples became fake headings | Fixed by turning html2text's `[code]` blocks into ```` ``` ```` fences, which the splitter skips |
| 8 | Built `copilot/splitters.py`: headings → token-sized pieces → breadcrumb | The core of this step |
| 9 | Ran the eval: splitter on old flat files = **no better**. Splitter + re-saved structured files = **worse** (9/11) | Section 3: the most useful part of the day |
| 10 | Diagnosed why (one vector per table), tried 3 fixes in a scratch script, kept the one that measured best: **one chunk per table row** | 11/11 |
| 11 | Wrote 5 **held-out** questions after the design was frozen | Check that I didn't just overfit to my own 11 questions |
| 12 | Built `copilot/snapshot.py` and re-saved the 3 step-0 pages as structured Markdown, with the example API key redacted | Fixes the source data, and gives a frozen corpus for step-7 evals |
| 13 | Asked Claude end to end. It now answers "permanent until revoked" [1] | The real goal: a correct, cited answer |
| 14 | A RAG-pipeline reviewer sub-agent found 2 splitter bugs and 3 eval flaws | Section 3b. All fixed except "add more questions" (that's step 7). |
| 15 | Found LangChain's `MarkdownHeaderTextSplitter` **strips indentation from every line**, breaking Python samples. Switched to `ExperimentalMarkdownSyntaxTextSplitter`. | Read the library's behavior, don't assume it |
| 16 | A first fence-repair attempt failed on repeated JSON lines. Replaced it with "split code inside its fences, then re-fence each piece". | The simpler design had no edge cases to get wrong |
| 17 | Tightened eval phrases and added MRR. The fixed baseline dropped from 10/11 to 9/11. | Loose phrases had been handing out free hits |
| 18 | 44 tests, all passing | One test per bug we hit |

---

## 2. The concepts (the part to revise)

### Why fixed-size chunks fail
A fixed window (800 chars, step 0) cuts wherever the counter lands: mid-sentence,
mid-table, between a heading and its text. A chunk then contains half of one idea and
half of another, and its embedding represents neither well.

### Structure-aware splitting: two passes
```text
Markdown document
  │ 1. MarkdownHeaderTextSplitter   cut at #, ##, ### → one piece per section,
  │                                 heading path saved in metadata {h1, h2, h3}
  ▼
sections
  │ 2. RecursiveCharacterTextSplitter   only if a section is too long: try to cut at
  │                                     "\n\n" (paragraph), then "\n" (line), then " " (word)
  ▼
chunks (each ≤ the embedding model's limit, measured in tokens)
```
- **"Recursive"** means: try the gentlest separator first, and only fall back to harsher
  cuts for pieces that are still too big. A paragraph break is better than a line
  break, which is better than cutting mid-word.
- Sections **never mix**: text under `## Rate limits` can't end up in a chunk with `## Pagination`.

### Measure size in *tokens*, not characters
Embedding models have a hard input limit in **tokens**. all-MiniLM-L6-v2 reads 256, and two of
those are its special markers `[CLS]`/`[SEP]`, which leaves **254**. Anything past that is
**silently ignored**: no error, it just never makes it into the vector. Characters
are only a rough proxy (code and URLs use more tokens per character). So we use
`RecursiveCharacterTextSplitter.from_huggingface_tokenizer(tokenizer, ...)`, which counts
with the **same tokenizer the model uses**. Our largest chunk is 230 tokens, so it fits.

### The breadcrumb (also called a "contextual chunk header")
Each chunk starts with where it came from:
```text
Authentication - Meraki Dashboard API v1 - Cisco Meraki Developer Hub > Authorization >
Access methods > Choosing the right authentication method

Feature: Token lifetime — OAuth 2.0 Grants: 60 minutes (auto-refresh); API Keys: Permanent until revoked
```
The chunk's own text may never say "Meraki" or "authentication", but the breadcrumb does,
so the embedding knows what the chunk is about. We subtract the breadcrumb's tokens from
the body's budget, so breadcrumb + body still fits in 254 tokens.

### Tables: one row = one chunk
A whole table in one chunk becomes **one vector averaging many facts** (here 6 rows × 2
columns). It matches no single question strongly. So each row becomes its own chunk,
**restating the column names**, because a row means nothing without its headers:
`| Token lifetime | 60 minutes | Permanent until revoked |` becomes
`Feature: Token lifetime — OAuth 2.0 Grants: 60 minutes; API Keys: Permanent until revoked`.

### Code blocks: split inside the fence, re-fence each piece
A long code sample gets cut into pieces like any long text. But a piece that opens a
```` ``` ```` fence and never closes it is broken Markdown, and Claude may misread
everything after it. So we take the code *out* of its fence, split it by lines, and wrap
each piece in its own ```` ```lang … ``` ````. Indentation is kept, because Python means something different without it.

### Garbage in → garbage chunks
The biggest fix happened in the **loader**, not the splitter. We switched from
`get_text()` (flat text) to HTML → Markdown, so headings and tables survive. Rule:
**keep structure as long as possible in the pipeline.** You can always flatten it later,
but you can't un-flatten.

---

## 3. The experiments (the most important section)

The right chunk's rank for *"How long do Meraki API keys last?"*, plus the overall score:

| # | Loader output | Splitter | API-key rank | dev hit@4 | What it taught me |
|---|---|---|---|---|---|
| A | flat text (step 0/1) | fixed 800 chars | **41** | 10/11 | The baseline. The bug is real and measurable. |
| B | flat text | structured (headings + breadcrumb) | 4 | 10/11 | Helped one question, hurt another ("permissions" 4 → 5). **Net zero.** |
| C | Markdown (tables kept) | structured | 8 | **9/11** | **Worse.** Keeping the table made one big multi-fact chunk. |
| D | Markdown | C + *short* breadcrumb | 48 | 9/11 | Worse. The page title carries "Meraki API v1", and the questions use those words. |
| E | Markdown | C + table rows as sentences (one chunk) | 15 | 10/11 | Better, but still one vector for six facts |
| F | Markdown | **C + one chunk per table row** | **1** | **11/11** | One fact per vector. Kept this. |
| | | *held-out (5 new questions), A vs F* | | 5/5 vs 5/5 | No regression, but F ranks 3 of the 5 answers 2nd instead of 1st |

What to take away:
1. **"Obviously better" ideas can make things worse.** Keeping tables (C) was the right
   idea and still dropped the score, until we fixed *how* tables become vectors.
2. **Ranks are fragile with a small embedding model.** The same fact ranked 1, 8, 15,
   41 or 48 across small variations. Fixing chunking helps a lot, but a stronger embedding
   model, hybrid keyword search, or a re-ranker are the next levers (steps 3, 4 and 7).
3. **Held-out questions are how you catch overfitting.** I designed F while looking at the dev
   questions, so a perfect dev score proves little. The held-out set says the change didn't
   break other things. It *doesn't* prove the change is better in general: 5 questions,
   written by me, on the same corpus.
4. **16 questions is a smoke test, not a benchmark.** It's enough to catch a regression and
   compare ideas. Step 7 makes it bigger, adds questions with *no* answer in the corpus, and
   grades answers, not just retrieval.

*(Table A–F was measured during development, with the first, looser answer phrases. The
final numbers at the top of this note use the tightened phrases from 3b.)*

## 3b. What the code review caught

A reviewer sub-agent (RAG-pipeline specialist) read the code, **ran it**, and diffed the
words in the source against the words in the chunks. Its findings, and what we did about each:

| Finding | Severity | What we did |
|---|---|---|
| **Eval phrases too loose.** "429" appeared in 6 chunks, "perPage" in 12, the base URL in **17**. Those hits were nearly free. | High | Each phrase is now the sentence that *answers* the question. The script prints `matches=` per case (more than 3 means the phrase is too loose). The fixed baseline fell from 10/11 to **9/11**, so its old score was inflated. |
| **Code lost its indentation.** | Medium | The cause was in LangChain: `MarkdownHeaderTextSplitter` calls `line.strip()` on every line. We switched to `ExperimentalMarkdownSyntaxTextSplitter`, which keeps whitespace and gives each code block its own section. Downside: it's labelled "experimental", so its behavior could change. Our tests would catch that. |
| **17 chunks had unclosed code fences.** | Medium | First fix: count fences up to each piece's `start_index`. **It failed** on JSON samples: overlap plus repeated lines made the index point at the wrong copy of the text. Second fix: split the code *inside* the fence and re-fence every piece, with no index math. Now 0 of 144 chunks are unbalanced. |
| Table rows dropped the first column's header ("Feature", "Metric", "Country") | Low | Rows now start with `Feature: Token lifetime — …` |
| A `a \| b` prose line right after a table was swallowed as a row, and table syntax inside code would be parsed | Low | A row must have the header's column count, and table detection skips code fences |
| Only hit@4 was reported, so a rank 1 → 2 slip was invisible | Medium | Added **MRR** |

**Lesson:** the review found more real problems than my own testing did, because it
*measured* things I'd only eyeballed (fence parity across all chunks, word-level diffs).
For data pipelines, write checks that run over **every** chunk, not just the ones you looked at.

### End to end
Same question, same Claude prompt (`ask_claude` from step 0), only the chunks differ:
- **Step-0 chunks:** *"The sources don't say how long Meraki API keys last."* Honest, but no help.
- **Step-2 chunks:** *"Meraki API keys are permanent until revoked [1]. … OAuth 2.0 grants
  have a 60-minute token lifetime with auto-refresh [1]."*

One thing to watch: Claude also wrote *"Because keys don't expire, Meraki advises against
hardcoding them [2]"*. The source says both facts but not the "because". Checking that
**each citation actually supports its sentence** is an eval for step 7.

---

## 4. Code walkthrough

### `copilot/loaders.py` (changed)
| Code | Why |
|---|---|
| `fetch_page(url)` uses `WebBaseLoader(...).scrape()` | Gets the parsed HTML (with the encoding fixed) and keeps step 1's `raise_for_status`, timeout and User-Agent |
| `html_to_markdown()`: `body_width = 0` | html2text wraps lines at 78 chars by default, which would split table rows across lines |
| `ignore_links = True` | Keeps the link *text*, drops the URL. URLs are token-hungry noise for embeddings. |
| `mark_code = True` + `fence_code()` | Code becomes a ```` ``` ```` fence. Unfenced, `# Example 1` in a Python sample was being treated as an `h1` heading. |

### `copilot/splitters.py` (new)
```text
split_document(doc)
  for each section from ExperimentalMarkdownSyntaxTextSplitter(#, ##, ###):
      crumb  = breadcrumb(doc, section)          title > h1 > h2 > h3
      budget = min(200, 254 - tokens(crumb))     body must fit next to the crumb
      if the section is one ``` block:
          bodies = split_code(section, budget)   split inside the fence, re-fence each piece
      else:
          prose, rows = split_tables(section)    tables → one sentence per row
          bodies = split_section(prose, budget) + rows
      chunk  = crumb + "\n\n" + body,  metadata = doc's + {h1,h2,h3, section}
  then number the chunks: chunk_index 0..n
```
- **`ExperimentalMarkdownSyntaxTextSplitter`**, not the classic `MarkdownHeaderTextSplitter`: the
  classic one strips indentation from every line. We keep only the `h1/h2/h3` metadata keys,
  because the experimental one also adds a `Code` key we don't need.
- `split_tables()` spots a table by a line with `|` followed by a divider line like
  `---|---|---` (`TABLE_DIVIDER` regex), skipping anything inside code fences. A row only
  counts if it has the header's column count. Each row becomes
  `FirstHeader: label — Header2: value; Header3: value` (`row_sentence()`).
- `is_code_block()` / `split_code()`: a section that is one ```` ``` ```` block is split *inside*
  the fence and each piece re-fenced. `FENCE_TOKENS = 10` is reserved for the fence lines.
- `split_section(..., strip_whitespace=False)`: the default strip would eat the first line's indentation.
- `MIN_BODY_TOKENS = 50` keeps the body from shrinking to nothing under a very long breadcrumb.
- `strip_headers=True` removes the `## Heading` line from the body, since the breadcrumb already says it.
- Only `#`–`###` start new sections. Deeper headings (`####`) stay in the text, which keeps
  sections a useful size instead of 2-line fragments.

### `copilot/snapshot.py` (new)
`python -m copilot.snapshot cisco <url> ...` saves a page as `data/cisco/<slug>.md` with
the `Source:`/`Title:` header that `load_markdown()` already reads, so a snapshot still
cites the live URL.
- **Why snapshot?** Live pages change. An eval needs a corpus that holds still, and git
  then shows exactly what changed when we re-snapshot.
- **`redact()`** replaces any 40-hex-character string (the shape of a Meraki API key) with
  `<YOUR_MERAKI_API_KEY>`. The docs contain an example key, and in step 0 GitHub's secret
  scanning flagged it. The trade-off: this regex would also mask a 40-hex git commit SHA
  in the docs. That's acceptable here, but a good point to raise in an interview.

### `experiments/retrieval_eval.py` (new)
For each `(question, phrase)` it embeds all chunks, ranks them by cosine similarity, and finds
the rank of the first chunk containing `phrase`. **hit@4** = that rank is ≤ 4. **MRR** = the average of
1/rank (1.0 = always first, 0.5 = typically second). `matches=` counts how many chunks contain
the phrase, as a check on the eval itself. It's retrieval only, so no Claude calls and it costs nothing to rerun.

---

## 5. Interview questions this step prepares you for

1. **"How do you choose a chunking strategy?"** Follow the document's structure
   (headings, paragraphs, tables, code), cap chunk size by the embedding model's token
   limit, and **measure** retrieval before and after. Don't pick a size because a tutorial used it.
2. **"Why measure chunk size in tokens?"** The model's limit is in tokens and text past it is
   silently dropped. Characters per token vary a lot (code and URLs vs prose).
3. **"How do you handle tables in RAG?"** Keep the structure when loading (HTML → Markdown),
   then index rows as self-contained sentences that repeat the column headers. The whole
   table can still be given to the LLM later (a "small-to-big" / parent-document retriever).
4. **"Your RAG gives a wrong answer. Where do you look first?"** Retrieval: which chunks came
   back and at what rank. In our case the answer was in the corpus all along, at rank 41.
5. **"How do you know a change improved things?"** A labeled eval set with a baseline,
   one change at a time, and a **held-out** set to catch overfitting.
6. **"What's a contextual chunk header?"** A breadcrumb (title > section > subsection) added
   to every chunk, so chunks that don't name their topic still embed and retrieve correctly.
7. **"Why freeze/snapshot web sources?"** Reproducible evals, a diff of what changed, offline
   runs, and somewhere to redact secrets before committing.
8. **"How do you know your eval itself is right?"** Check that the labels are specific (each answer
   phrase should match one or two chunks, not 17), that each one matches at least one chunk (we had one matching 0), and
   report a rank-sensitive metric like MRR next to hit@k. Our baseline score fell once the eval was fixed.
9. **"Tell me about a time a library didn't do what you expected."** `MarkdownHeaderTextSplitter`
   strips indentation from every line, including code. Found by a reviewer's word-level diff,
   confirmed with a 3-line repro, and fixed by switching splitters, with a test that locks it in.

## 6. Key terms

- **Structure-aware splitting**: cutting at headings/paragraphs instead of every N characters
- **RecursiveCharacterTextSplitter**: tries separators gentlest-first ("\n\n" → "\n" → " " → "")
- **MarkdownHeaderTextSplitter**: one chunk per Markdown section, headings kept in metadata
- **Token / tokenizer**: the units a model reads, and the tool that cuts text into them
- **Context window (embedding model)**: max tokens embedded (256 for MiniLM); the rest is silently cut
- **Breadcrumb / contextual chunk header**: the heading path prepended to each chunk
- **hit@k**: fraction of questions whose correct chunk is in the top k
- **MRR (mean reciprocal rank)**: average of 1/rank of the first correct chunk; rewards ranking it first
- **Dev set vs held-out set**: questions you tune on vs questions you only test on
- **Overfitting (to an eval)**: improving the score on questions you've seen, not in general
- **Mojibake**: garbled text from decoding bytes with the wrong encoding
- **Snapshot**: a saved, versioned copy of a live source

## 7. Self-quiz (answers are above)

1. Why couldn't any splitter fix step 0's table bug on its own?
2. What does "recursive" mean in RecursiveCharacterTextSplitter?
3. MiniLM reads 256 tokens. Why is our limit 254, and what happens to token #300?
4. Why did keeping the table *lower* the score at first, and what fixed it?
5. Why did shortening the breadcrumb hurt?
6. What is the held-out set for, and what *can't* our 5/5 held-out result prove?
7. Why do code comments need fences before header splitting?
8. Why did the fixed baseline's score *drop* after the review, when we hadn't touched its code?
9. Why did the first fence-repair approach fail on JSON samples?

## 8. Optional hands-on

- Change `CHUNK_TOKENS` to 100 and to 254, rerun `python -m experiments.retrieval_eval`,
  and write down what happens to the chunk count and the ranks.
- Write 3 new questions of your own (with the answer phrase) and add them to
  `HELD_OUT_CASES`. Do they pass?

## Next: step 3, embeddings + Chroma
Right now every run re-embeds all 144 chunks from scratch. Step 3 stores the vectors in
**Chroma** (one collection per company, persisted to disk) and decides the open question
from CLAUDE.md: keep local MiniLM, or switch to a stronger embedding model, which this
step's fragile ranks suggest could help. We'll answer that with the eval from this step.
