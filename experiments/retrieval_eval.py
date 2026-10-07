"""Tiny retrieval eval: does the right chunk land in the top-k? (Retrieval only, no Claude calls.)

Each case is a question plus a phrase only the answering chunk(s) contain (use the answer
sentence, not a keyword like "429" that appears in many chunks). Metrics:
  hit@k  the phrase appears in at least one of the top-k chunks
  MRR    mean of 1/rank of the first matching chunk (1.0 = always ranked first)

DEV_CASES were used while designing the step-2 splitter. HELD_OUT_CASES were written after
the design was frozen. 16 questions is a smoke test for regressions, not proof of quality.

Usage: python -m experiments.retrieval_eval [fixed|structured]   (default: both)
"""

import sys

import numpy as np
from langchain_core.documents import Document
from sentence_transformers import SentenceTransformer

from copilot.loaders import load_company

EMBED_MODEL = "all-MiniLM-L6-v2"
COMPANY = "cisco"
TOP_K = 4
FIXED_CHUNK_SIZE = 800    # step 0's settings
FIXED_CHUNK_OVERLAP = 100

DEV_CASES = [
    ("How long do Meraki API keys last?", "Permanent until revoked"),
    ("How long does an OAuth access token last?", "60 minutes"),
    ("How many API keys can one identity have?", "up to two valid API keys"),
    ("How many API requests per second can I make per organization?", "10 requests per second per organization"),
    ("What status code means I hit the rate limit?", "the API returns a 429 status code"),
    ("Which query parameter sets the page size?", "The number of entries to be returned in the page"),
    ("What does a 404 error mean in the Meraki API?", "doesn't exist"),
    ("What is the base URL for Meraki API v1?", "every API request will begin with the following"),
    ("Which auth method should I use for a personal script?", "Personal scripts"),
    ("Where do API key permissions come from?", "Inherits from admin's role"),
    ("What is the rate limit per source IP address?", "100 requests per second"),
]

HELD_OUT_CASES = [
    ("How long should I wait after getting rate limited?", "how long to wait before sending the next request"),
    ("What's the API base URL for a dashboard hosted in China?", "api.meraki.cn"),
    ("How can I group many write calls into a single request?", "action batches"),
    ("Why would I get a 401 Missing API key error?", "Missing API key"),
    ("How do I fetch every page of results with the Python library?", "total_pages"),
]


def fixed_chunks(docs: list[Document]) -> list[str]:
    """Step 0's chunker: fixed character windows, blind to structure."""
    step = FIXED_CHUNK_SIZE - FIXED_CHUNK_OVERLAP
    return [
        doc.page_content[start : start + FIXED_CHUNK_SIZE]
        for doc in docs
        for start in range(0, max(len(doc.page_content) - FIXED_CHUNK_OVERLAP, 1), step)
    ]


def structured_chunks(docs: list[Document], embedder: SentenceTransformer) -> list[str]:
    from copilot.splitters import split_documents  # step 2; imported lazily so baseline runs without it

    return [chunk.page_content for chunk in split_documents(docs, embedder.tokenizer)]


def score_cases(cases: list[tuple[str, str]], chunks: list[str], vecs: np.ndarray,
                embedder: SentenceTransformer) -> tuple[int, float]:
    hits, reciprocal_ranks = 0, []
    for question, phrase in cases:
        scores = vecs @ embedder.encode([question], normalize_embeddings=True)[0]
        ranking = np.argsort(scores)[::-1]
        rank = next((r for r, i in enumerate(ranking, start=1) if phrase in chunks[i]), None)
        hit = rank is not None and rank <= TOP_K
        hits += hit
        reciprocal_ranks.append(1 / rank if rank else 0.0)
        matches = sum(phrase in chunk for chunk in chunks)  # >3 means the phrase is too loose
        print(f"  {'HIT ' if hit else 'MISS'} rank={str(rank):>4}  matches={matches}  {question}")
    return hits, sum(reciprocal_ranks) / len(reciprocal_ranks)


def evaluate(name: str, chunks: list[str], embedder: SentenceTransformer) -> None:
    vecs = embedder.encode(chunks, normalize_embeddings=True)
    print(f"\n=== {name}: {len(chunks)} chunks ===")
    for label, cases in [("dev", DEV_CASES), ("held-out", HELD_OUT_CASES)]:
        print(f" {label}:")
        hits, mrr = score_cases(cases, chunks, vecs, embedder)
        print(f"  {label} hit@{TOP_K}: {hits}/{len(cases)}   MRR: {mrr:.2f}")


def main() -> None:
    strategies = sys.argv[1:] or ["fixed", "structured"]
    docs = load_company(COMPANY)
    embedder = SentenceTransformer(EMBED_MODEL)
    for name in strategies:
        if name == "fixed":
            evaluate("fixed 800-char (step 0)", fixed_chunks(docs), embedder)
        elif name == "structured":
            evaluate("structured (step 2)", structured_chunks(docs, embedder), embedder)
        else:
            sys.exit(f"Unknown strategy {name!r}: use fixed or structured")


if __name__ == "__main__":
    main()
