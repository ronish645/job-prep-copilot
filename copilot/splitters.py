"""Step 2: split Documents into chunks that follow the document's structure, sized in tokens.

Two passes per Document:
  1. MarkdownHeaderTextSplitter cuts at #, ## and ### headings, so a chunk never mixes two
     sections. The heading path is kept in metadata (h1, h2, h3).
  2. RecursiveCharacterTextSplitter cuts any section that is still too long, trying paragraph,
     then line, then word boundaries. Length is measured in the embedding model's tokens,
     because that is the limit that matters (all-MiniLM-L6-v2 silently ignores tokens past 256).

Two fixes found by measuring (see docs/journey/2026-10-07-step2-splitters.md):
  - Each chunk starts with its "breadcrumb" (page title > h1 > h2 > h3), so a chunk still says
    what it is about even when its own text doesn't.
  - Each Markdown table row becomes its own chunk, rewritten to repeat the column names:
    "Token lifetime — OAuth 2.0 Grants: 60 minutes (auto-refresh); API Keys: Permanent until
    revoked". One embedding for a whole table blurs six facts into one vector.

Usage: python -m copilot.splitters cisco
"""

import re
import sys

from langchain_core.documents import Document
from langchain_text_splitters import (
    ExperimentalMarkdownSyntaxTextSplitter,
    RecursiveCharacterTextSplitter,
)
from transformers import AutoTokenizer, PreTrainedTokenizerBase

from copilot.loaders import load_company

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_MAX_TOKENS = 254    # the model reads 256 tokens, 2 of which are its [CLS] and [SEP] markers
CHUNK_TOKENS = 200        # target size of a chunk's body, before the breadcrumb is added
CHUNK_OVERLAP_TOKENS = 30
MIN_BODY_TOKENS = 50      # never squeeze a body below this, even under a long breadcrumb
FENCE = "```"
FENCE_TOKENS = 10         # room for the ```lang ... ``` wrapped around each piece of code
HEADINGS = [("#", "h1"), ("##", "h2"), ("###", "h3")]  # deeper headings stay as plain text
HEADING_KEYS = [key for _, key in HEADINGS]
BREADCRUMB_SEP = " > "
NEWLINE = "\n"
TABLE_DIVIDER = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?$")  # e.g. ---|---|---


def load_tokenizer() -> PreTrainedTokenizerBase:
    return AutoTokenizer.from_pretrained(EMBED_MODEL)


def count_tokens(tokenizer: PreTrainedTokenizerBase, text: str) -> int:
    return len(tokenizer.tokenize(text))


def breadcrumb(doc: Document, section: Document) -> str:
    """'Page title > h1 > h2 > h3', using whichever headings this section sits under."""
    parts = [doc.metadata.get("title", "")]
    parts += [section.metadata[key] for key in HEADING_KEYS if key in section.metadata]
    return BREADCRUMB_SEP.join(part for part in parts if part)


def table_cells(line: str) -> list[str]:
    return [cell.strip(" *") for cell in line.strip().strip("|").split("|")]


def row_sentence(headers: list[str], cells: list[str]) -> str:
    """['Feature','API Keys'] + ['Token lifetime','Permanent'] -> 'Feature: Token lifetime — API Keys: Permanent'"""
    label = f"{headers[0]}: {cells[0]}" if headers[0] else cells[0]
    pairs = "; ".join(f"{h}: {v}" for h, v in zip(headers[1:], cells[1:]) if v)
    return f"{label} — {pairs}" if pairs else label


def split_tables(text: str) -> tuple[str, list[str]]:
    """Pull Markdown tables out of `text`. Returns (text without tables, one sentence per row)."""
    lines = text.splitlines()
    prose, rows, i, in_fence = [], [], 0, False
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith(FENCE):
            in_fence = not in_fence
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if in_fence or "|" not in line or not TABLE_DIVIDER.match(next_line):
            prose.append(lines[i])  # the original line: code keeps its indentation
            i += 1
            continue
        headers = table_cells(line)
        i += 2  # skip the header line and the ---|--- divider
        # A row must have the header's column count, so a stray "a | b" line isn't swallowed
        while i < len(lines) and len(table_cells(lines[i])) == len(headers) and "|" in lines[i]:
            rows.append(row_sentence(headers, table_cells(lines[i])))
            i += 1
    return "\n".join(prose).strip("\n"), rows


def split_section(text: str, tokenizer: PreTrainedTokenizerBase, budget: int) -> list[str]:
    """Cut one section into pieces of at most `budget` tokens, at the cleanest boundary."""
    splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
        tokenizer,
        chunk_size=budget,
        chunk_overlap=min(CHUNK_OVERLAP_TOKENS, budget // 4),
        strip_whitespace=False,  # stripping would eat the indentation of a piece's first line
    )
    pieces = [piece.strip("\n") for piece in splitter.split_text(text)]
    return [piece for piece in pieces if piece.strip()]


def is_code_block(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith(FENCE) and stripped.endswith(FENCE) and "\n" in stripped


def split_code(block: str, tokenizer: PreTrainedTokenizerBase, budget: int) -> list[str]:
    """Split the code inside a ``` block, then re-fence each piece so every chunk is valid Markdown."""
    lines = block.strip().splitlines()
    opening, code = lines[0], "\n".join(lines[1:-1])  # opening keeps a language tag like ```python
    pieces = split_section(code, tokenizer, budget - FENCE_TOKENS) if code.strip() else []
    return [f"{opening}\n{piece}\n{FENCE}" for piece in pieces]


def split_document(doc: Document, tokenizer: PreTrainedTokenizerBase) -> list[Document]:
    # The "experimental" splitter keeps indentation; MarkdownHeaderTextSplitter strips it,
    # which breaks Python code samples. It also puts each code block in its own section.
    header_splitter = ExperimentalMarkdownSyntaxTextSplitter(HEADINGS, strip_headers=True)
    chunks = []
    for section in header_splitter.split_text(doc.page_content):
        headings = {k: v for k, v in section.metadata.items() if k in HEADING_KEYS}
        crumb = breadcrumb(doc, section)
        # Leave room for the breadcrumb, so breadcrumb + body still fits the embedding model
        room = EMBED_MAX_TOKENS - count_tokens(tokenizer, crumb)
        budget = max(min(CHUNK_TOKENS, room), MIN_BODY_TOKENS)
        # The experimental splitter gives each ``` block its own section, so code is handled whole
        if is_code_block(section.page_content):
            bodies = split_code(section.page_content, tokenizer, budget)
        else:
            prose, rows = split_tables(section.page_content)
            bodies = (split_section(prose, tokenizer, budget) if prose.strip() else []) + rows
        for body in bodies:
            chunks.append(Document(
                page_content=f"{crumb}\n\n{body.strip(NEWLINE)}",
                metadata={**doc.metadata, **headings, "section": crumb},
            ))
    return [
        Document(page_content=c.page_content, metadata={**c.metadata, "chunk_index": i})
        for i, c in enumerate(chunks)
    ]


def split_documents(docs: list[Document], tokenizer: PreTrainedTokenizerBase) -> list[Document]:
    return [chunk for doc in docs for chunk in split_document(doc, tokenizer)]


def print_summary(chunks: list[Document], tokenizer: PreTrainedTokenizerBase) -> None:
    sizes = [count_tokens(tokenizer, c.page_content) for c in chunks]
    print(f"\n{len(chunks)} chunks, tokens per chunk: min {min(sizes)}, "
          f"avg {sum(sizes) // len(sizes)}, max {max(sizes)} (model limit {EMBED_MAX_TOKENS})\n")
    for chunk, size in zip(chunks, sizes):
        meta = chunk.metadata
        print(f"  {size:>4} tok  #{meta['chunk_index']:<3} {meta['section']}")
    print(f"\nExample chunk:\n{'-' * 60}\n{chunks[0].page_content}\n{'-' * 60}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m copilot.splitters <company>   e.g. cisco")
    try:
        docs = load_company(sys.argv[1].strip().lower())
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    tokenizer = load_tokenizer()
    print_summary(split_documents(docs, tokenizer), tokenizer)


if __name__ == "__main__":
    main()
