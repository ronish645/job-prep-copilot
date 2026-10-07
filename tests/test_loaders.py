"""Offline tests for copilot/loaders.py. No network: WebBaseLoader is replaced with a fake."""

from pathlib import Path

import pytest
import requests
from langchain_core.documents import Document

import copilot.loaders as loaders


@pytest.fixture
def company_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway data/acme/ folder, with PROJECT_ROOT pointed at tmp_path."""
    monkeypatch.setattr(loaders, "PROJECT_ROOT", tmp_path)
    folder = tmp_path / "data" / "acme"
    folder.mkdir(parents=True)
    return folder


class FakeWebLoader:
    """Stands in for WebBaseLoader: returns a page, or raises for URLs containing 'dead'."""

    def __init__(self, url: str, **kwargs) -> None:
        self.url = url

    def load(self) -> list[Document]:
        if "dead" in self.url:
            raise requests.HTTPError(f"404 Client Error for url: {self.url}")
        return [Document(page_content="\n\n\n\nHello   \n\n\n\nWorld", metadata={"title": "Hi"})]


def test_clean_text_collapses_blank_lines_and_trailing_spaces():
    assert loaders.clean_text("\n\n\na  \n\n\n\n\nb\n") == "a\n\nb"


def test_split_header_moves_source_and_title_into_metadata():
    header, body = loaders.split_header("Source: https://x.dev/a\nTitle: A page\n\nBody text")
    assert header == {"source": "https://x.dev/a", "title": "A page"}
    assert body.strip() == "Body text"


def test_split_header_leaves_files_without_header_untouched():
    header, body = loaders.split_header("Just text\nSource: not at the top")
    assert header == {}
    assert body == "Just text\nSource: not at the top"


def test_markdown_cites_original_url_and_keeps_local_path(company_dir: Path):
    (company_dir / "auth.md").write_text("Source: https://x.dev/auth\nTitle: Auth\n\nUse OAuth.")
    [doc] = loaders.load_markdown(company_dir, "acme")
    assert doc.page_content == "Use OAuth."
    assert doc.metadata == {
        "source": "https://x.dev/auth",
        "source_type": "markdown",
        "company": "acme",
        "title": "Auth",
        "path": "data/acme/auth.md",
    }


def test_markdown_without_header_falls_back_to_local_path(company_dir: Path):
    (company_dir / "notes.md").write_text("Plain notes")
    [doc] = loaders.load_markdown(company_dir, "acme")
    assert doc.metadata["source"] == "data/acme/notes.md"
    assert doc.metadata["title"] == "notes"


def test_html_saved_as_pdf_is_skipped(company_dir: Path):
    (company_dir / "blocked.pdf").write_text("<HTML><body>403 Forbidden</body></HTML>")
    assert loaders.load_pdfs(company_dir, "acme") == []


def test_read_urls_ignores_comments_and_rejects_non_http(tmp_path: Path):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("# comment\n\nhttps://x.dev/a\n")
    assert loaders.read_urls(urls_file) == ["https://x.dev/a"]

    urls_file.write_text("file:///etc/passwd\n")
    with pytest.raises(ValueError, match="not an http"):
        loaders.read_urls(urls_file)


def test_read_urls_returns_empty_when_file_missing(tmp_path: Path):
    assert loaders.read_urls(tmp_path / "urls.txt") == []


def test_load_web_cleans_pages_and_skips_failed_urls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loaders, "WebBaseLoader", FakeWebLoader)
    docs = loaders.load_web(["https://x.dev/ok", "https://x.dev/dead"], "acme")
    assert len(docs) == 1
    assert docs[0].page_content == "Hello\n\nWorld"
    assert docs[0].metadata["source"] == "https://x.dev/ok"
    assert docs[0].metadata["source_type"] == "web"


def test_dedupe_keeps_first_document_per_source():
    first = Document(page_content="saved copy", metadata={"source": "https://x.dev/a"})
    second = Document(page_content="live copy", metadata={"source": "https://x.dev/a"})
    assert loaders.dedupe([first, second]) == [first]


def test_dedupe_keeps_different_pages_of_same_pdf():
    pages = [Document(page_content=str(n), metadata={"source": "a.pdf", "page": n}) for n in (1, 2)]
    assert loaders.dedupe(pages) == pages


def test_load_company_combines_all_source_types(company_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loaders, "WebBaseLoader", FakeWebLoader)
    (company_dir / "intro.md").write_text("Intro")
    (company_dir / "urls.txt").write_text("https://x.dev/blog\n")
    docs = loaders.load_company("acme", data_root=company_dir.parent)
    assert [d.metadata["source_type"] for d in docs] == ["markdown", "web"]


@pytest.mark.parametrize("bad_name", ["../etc", "Acme Corp", "", "a/b"])
def test_load_company_rejects_unsafe_names(bad_name: str):
    with pytest.raises(ValueError):
        loaders.load_company(bad_name)


def test_load_company_errors_on_missing_or_empty_folder(company_dir: Path):
    with pytest.raises(FileNotFoundError, match="No folder"):
        loaders.load_company("nobody", data_root=company_dir.parent)
    with pytest.raises(FileNotFoundError, match="No loadable sources"):
        loaders.load_company("acme", data_root=company_dir.parent)


def test_truncated_pdf_is_skipped_not_crashing(company_dir: Path):
    (company_dir / "broken.pdf").write_bytes(b"%PDF-1.7\n1 0 obj\n<<")  # header, then nothing
    assert loaders.load_pdfs(company_dir, "acme") == []


def test_non_utf8_markdown_is_skipped_not_crashing(company_dir: Path):
    (company_dir / "latin1.md").write_bytes("caf\xe9".encode("latin-1"))
    (company_dir / "ok.md").write_text("fine")
    docs = loaders.load_markdown(company_dir, "acme")
    assert [d.page_content for d in docs] == ["fine"]


def test_empty_copy_does_not_hide_non_empty_duplicate(company_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(loaders, "WebBaseLoader", FakeWebLoader)
    (company_dir / "empty.md").write_text("Source: https://x.dev/page\n\n   \n")
    (company_dir / "urls.txt").write_text("https://x.dev/page\n")
    [doc] = loaders.load_company("acme", data_root=company_dir.parent)
    assert doc.metadata["source_type"] == "web"
