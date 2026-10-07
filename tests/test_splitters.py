"""Tests for copilot/splitters.py. Uses the real MiniLM tokenizer (small, cached after first run)."""

import pytest
from langchain_core.documents import Document

from copilot import splitters

TABLE_MD = """## Access methods

Two ways to authenticate.

Feature | OAuth 2.0 Grants | API Keys
---|---|---
**Best for** | Third-party apps | Personal scripts
**Token lifetime** | 60 minutes | Permanent until revoked

More prose after the table."""


@pytest.fixture(scope="module")
def tokenizer():
    return splitters.load_tokenizer()


def make_doc(text: str, title: str = "Auth - Meraki") -> Document:
    return Document(page_content=text, metadata={"source": "https://x.dev/auth", "title": title})


def test_split_tables_turns_each_row_into_a_self_contained_sentence():
    prose, rows = splitters.split_tables(TABLE_MD)
    assert rows == [
        "Feature: Best for — OAuth 2.0 Grants: Third-party apps; API Keys: Personal scripts",
        "Feature: Token lifetime — OAuth 2.0 Grants: 60 minutes; API Keys: Permanent until revoked",
    ]
    assert "|" not in prose
    assert "Two ways to authenticate." in prose and "More prose after the table." in prose


def test_split_tables_leaves_text_without_tables_alone():
    text = "Use a | pipe in prose.\nNo divider line follows."
    assert splitters.split_tables(text) == (text, [])


def test_two_column_table_rows():
    _, rows = splitters.split_tables("Code | Meaning\n---|---\n404 | Not found")
    assert rows == ["Code: 404 — Meaning: Not found"]


def test_line_with_pipe_after_table_is_not_swallowed_as_a_row():
    prose, rows = splitters.split_tables("A | B\n---|---\nx | y\nRun `ls | wc -l | sort` to count.")
    assert rows == ["A: x — B: y"]
    assert prose == "Run `ls | wc -l | sort` to count."


def test_table_syntax_inside_code_fence_is_left_alone():
    text = "```\nA | B\n---|---\nx | y\n```"
    assert splitters.split_tables(text) == (text, [])


def test_code_keeps_its_indentation(tokenizer):
    doc = make_doc("## Retry\n\n```\ndef retry():\n    time.sleep(1)\n```")
    [chunk] = splitters.split_document(doc, tokenizer)
    assert "\n    time.sleep(1)" in chunk.page_content


def test_long_code_block_pieces_are_each_valid_fenced_markdown(tokenizer):
    code = "\n".join(f"    call_api(page={n})  # fetch page {n}" for n in range(150))
    doc = make_doc(f"## Paging\n\n```\ndef fetch_all():\n{code}\n```")
    chunks = splitters.split_document(doc, tokenizer)
    assert len(chunks) > 1
    for chunk in chunks:
        body = chunk.page_content.split("\n\n", 1)[1]
        assert body.startswith("```") and body.endswith("```")
        assert splitters.count_tokens(tokenizer, chunk.page_content) <= splitters.EMBED_MAX_TOKENS


def test_split_code_keeps_language_tag_and_skips_empty_blocks(tokenizer):
    assert splitters.split_code("```python\nprint(1)\n```", tokenizer, 200) == ["```python\nprint(1)\n```"]
    assert splitters.split_code("```\n```", tokenizer, 200) == []


def test_repeated_code_lines_still_give_balanced_fences(tokenizer):
    # Overlap + repeated lines (like JSON samples) broke an earlier index-based fence repair
    json_lines = "\n".join('  {"address": "208.67.222.222"},' for _ in range(120))
    doc = make_doc(f"## Response\n\n```\n[\n{json_lines}\n]\n```\n\n```\n```")
    chunks = splitters.split_document(doc, tokenizer)
    assert len(chunks) > 1
    assert all(c.page_content.count("```") == 2 for c in chunks)


def test_only_heading_keys_reach_chunk_metadata(tokenizer):
    doc = make_doc("## Setup\n\n```python\nprint(1)\n```")
    [chunk] = splitters.split_document(doc, tokenizer)
    assert "Code" not in chunk.metadata  # the experimental splitter adds this; we drop it


def test_chunks_carry_breadcrumb_and_heading_metadata(tokenizer):
    chunks = splitters.split_document(make_doc("# Authorization\n" + TABLE_MD), tokenizer)
    crumb = "Auth - Meraki > Authorization > Access methods"
    assert all(c.page_content.startswith(crumb + "\n\n") for c in chunks)
    assert all(c.metadata["section"] == crumb for c in chunks)
    assert chunks[0].metadata["h1"] == "Authorization"
    assert chunks[0].metadata["h2"] == "Access methods"
    assert chunks[0].metadata["source"] == "https://x.dev/auth"  # original metadata kept


def test_each_table_row_is_its_own_chunk(tokenizer):
    chunks = splitters.split_document(make_doc(TABLE_MD), tokenizer)
    lifetime = [c for c in chunks if "Permanent until revoked" in c.page_content]
    assert len(lifetime) == 1
    assert "Personal scripts" not in lifetime[0].page_content  # not mixed with other rows


def test_sections_never_mix(tokenizer):
    doc = make_doc("## Rate limits\n\n10 requests per second.\n\n## Pagination\n\nUse perPage.")
    chunks = splitters.split_document(doc, tokenizer)
    assert len(chunks) == 2
    assert "perPage" not in chunks[0].page_content
    assert "10 requests" not in chunks[1].page_content


def test_hash_comments_inside_code_fences_are_not_headings(tokenizer):
    doc = make_doc("## Setup\n\n```\n# read the key\nexport KEY=1\n```")
    chunks = splitters.split_document(doc, tokenizer)
    assert len(chunks) == 1
    assert "h1" not in chunks[0].metadata


def test_long_section_is_split_and_every_chunk_fits_the_model(tokenizer):
    long_text = "## Big\n\n" + "\n\n".join(f"Paragraph {n} about Meraki API rate limits." for n in range(200))
    chunks = splitters.split_document(make_doc(long_text), tokenizer)
    assert len(chunks) > 1
    sizes = [splitters.count_tokens(tokenizer, c.page_content) for c in chunks]
    assert max(sizes) <= splitters.EMBED_MAX_TOKENS


def test_document_without_headings_uses_title_as_breadcrumb(tokenizer):
    doc = make_doc("Plain PDF page text.", title="job_posting")
    [chunk] = splitters.split_document(doc, tokenizer)
    assert chunk.page_content == "job_posting\n\nPlain PDF page text."


def test_chunk_index_counts_up_per_document(tokenizer):
    chunks = splitters.split_document(make_doc(TABLE_MD), tokenizer)
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))
