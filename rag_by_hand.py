"""Step 0: RAG by hand. Load -> chunk -> embed -> retrieve -> Claude answers with sources.

Usage: python rag_by_hand.py "How does the Meraki API authenticate requests?"
"""

import sys
from pathlib import Path

import anthropic
import numpy as np
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data" / "cisco"  # works from any working directory
CHUNK_SIZE = 800      # characters per chunk (~240 tokens max here; MiniLM truncates past 256)
CHUNK_OVERLAP = 100   # characters shared between neighbouring chunks
TOP_K = 4             # how many chunks to hand to Claude
EMBED_MODEL = "all-MiniLM-L6-v2"  # small, fast, local (384-dim vectors)
CLAUDE_MODEL = "claude-opus-5-5"
MAX_TOKENS = 2000

SYSTEM_PROMPT = (
    "You answer questions about a company using ONLY the numbered sources provided. "
    "Cite sources inline like [1] or [2][3]. If the sources don't contain the answer, "
    "say so plainly instead of guessing."
)


# ---------- 1. Load ----------
def load_documents(folder: Path) -> list[dict]:
    paths = sorted(folder.glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"No .md files found in {folder}")
    return [
        {"source": str(p.relative_to(PROJECT_ROOT)), "text": p.read_text(encoding="utf-8")}
        for p in paths
    ]


# ---------- 2. Chunk ----------
def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    step = size - overlap
    # Stop before a final chunk that would only repeat the previous chunk's overlap
    last_start = max(len(text) - overlap, 1)
    return [text[start : start + size] for start in range(0, last_start, step)]


def chunk_documents(docs: list[dict]) -> list[dict]:
    return [
        {"source": doc["source"], "chunk_id": i, "text": chunk}
        for doc in docs
        for i, chunk in enumerate(chunk_text(doc["text"], CHUNK_SIZE, CHUNK_OVERLAP))
        if chunk.strip()  # skip whitespace-only chunks: they embed to noise
    ]


# ---------- 3. Embed ----------
def embed(model: SentenceTransformer, texts: list[str]) -> np.ndarray:
    # normalize=True makes every vector length 1, so dot product == cosine similarity
    return model.encode(texts, normalize_embeddings=True)


# ---------- 4. Retrieve ----------
def retrieve(
    query_vec: np.ndarray, chunk_vecs: np.ndarray, chunks: list[dict], k: int
) -> list[dict]:
    scores = chunk_vecs @ query_vec            # one similarity score per chunk
    top = np.argsort(scores)[::-1][:k]         # indices of the k highest scores
    return [{**chunks[i], "score": float(scores[i])} for i in top]


# ---------- 5. Generate ----------
def build_prompt(question: str, hits: list[dict]) -> str:
    sources = "\n\n".join(
        f"[{n}] (from {hit['source']}, chunk {hit['chunk_id']})\n{hit['text']}" for n, hit in enumerate(hits, start=1)
    )
    return f"Sources:\n\n{sources}\n\nQuestion: {question}"


def ask_claude(client: anthropic.Anthropic, question: str, hits: list[dict]) -> str:
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if Claude declines, the API retries on a fallback model
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_prompt(question, hits)}],
    )
    if response.stop_reason == "refusal":
        return "Claude declined to answer this question."
    answer = "".join(block.text for block in response.content if block.type == "text")
    if response.stop_reason == "max_tokens":
        answer += "\n[answer truncated: hit MAX_TOKENS]"
    # With fallbacks on, a different model may have answered; show which one did
    return f"{answer}\n\n(answered by {response.model})"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('Usage: python rag_by_hand.py "your question"')
    question = sys.argv[1].strip()
    if not question:
        sys.exit("Question must not be empty")
    load_dotenv()

    try:
        chunks = chunk_documents(load_documents(DATA_DIR))
    except FileNotFoundError as e:
        sys.exit(str(e))
    if not chunks:
        sys.exit(f"No text to index in {DATA_DIR}")
    print(f"Loaded {len(chunks)} chunks from {DATA_DIR.relative_to(PROJECT_ROOT)}")

    # Step 0 re-embeds every chunk on every run. Fine for 28 chunks; step 3 stores them in Chroma.
    embedder = SentenceTransformer(EMBED_MODEL)
    chunk_vecs = embed(embedder, [c["text"] for c in chunks])
    query_vec = embed(embedder, [question])[0]
    hits = retrieve(query_vec, chunk_vecs, chunks, TOP_K)

    print("\nRetrieved chunks:")
    for n, hit in enumerate(hits, start=1):
        print(f"  [{n}] score={hit['score']:.3f}  {hit['source']} (chunk {hit['chunk_id']})")

    try:
        answer = ask_claude(anthropic.Anthropic(), question, hits)
    except anthropic.AuthenticationError:
        sys.exit("Invalid API key. Check ANTHROPIC_API_KEY in .env")
    except anthropic.APIStatusError as e:
        sys.exit(f"Claude API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error: couldn't reach the Claude API")
    except anthropic.AnthropicError as e:  # e.g. no credentials configured at all
        sys.exit(f"Claude client error: {e}")

    print(f"\nAnswer:\n{answer}")


if __name__ == "__main__":
    main()
