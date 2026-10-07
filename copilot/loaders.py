"""Step 1: load a company's sources into LangChain Documents.

Everything for one company lives in data/<company>/:
  *.md      -> TextLoader     (pages we saved by hand in step 0)
  *.pdf     -> PyPDFLoader    (one Document per page, e.g. a job posting saved as PDF)
  urls.txt  -> WebBaseLoader  (one URL per line, fetched live)

Each loader returns different metadata. We normalise it so every Document carries the
same keys (source, source_type, company, title), because later steps cite `source`.

Usage: python -m copilot.loaders cisco
"""

import os
import re
import sys
from collections import Counter
from pathlib import Path

# WebBaseLoader warns at import time if USER_AGENT is unset, so set it before importing.
USER_AGENT = "job-prep-copilot/0.1 (learning project)"
os.environ.setdefault("USER_AGENT", USER_AGENT)

import requests  # noqa: E402
from langchain_community.document_loaders import (  # noqa: E402
    PyPDFLoader,
    TextLoader,
    WebBaseLoader,
)
from langchain_core.documents import Document  # noqa: E402
from pypdf.errors import PyPdfError  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
URLS_FILE = "urls.txt"
COMPANY_NAME = re.compile(r"^[a-z0-9_-]+$")  # also blocks "../" path tricks
PDF_MAGIC = b"%PDF-"
PREVIEW_CHARS = 160
WEB_TIMEOUT_S = 15  # don't hang forever on a slow server


# ---------- Cleaning ----------
def clean_text(text: str) -> str:
    """Strip trailing spaces and collapse runs of blank lines left over from HTML layout."""
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def split_header(text: str) -> tuple[dict, str]:
    """Pull the 'Source:' / 'Title:' lines from the top of a step-0 .md file into metadata."""
    header = {}
    lines = text.splitlines()
    while lines and (match := re.match(r"^(Source|Title):\s*(.+)$", lines[0])):
        header[match.group(1).lower()] = match.group(2).strip()
        lines = lines[1:]
    return header, "\n".join(lines)


def make_doc(text: str, *, source: str, source_type: str, company: str, **extra) -> Document:
    """Build a Document with our standard metadata keys, plus any loader-specific extras."""
    metadata = {"source": source, "source_type": source_type, "company": company, **extra}
    return Document(page_content=clean_text(text), metadata=metadata)


# ---------- Loaders ----------
def load_markdown(folder: Path, company: str) -> list[Document]:
    docs = []
    for path in sorted(folder.glob("*.md")):
        local_path = str(path.relative_to(PROJECT_ROOT))
        try:
            raw = TextLoader(str(path), encoding="utf-8").load()[0]
        except RuntimeError as e:  # TextLoader wraps UnicodeDecodeError in RuntimeError
            print(f"  skip {local_path}: not valid UTF-8 ({e.__cause__})", file=sys.stderr)
            continue
        header, body = split_header(raw.page_content)
        docs.append(make_doc(
            body,
            source=header.get("source", local_path),  # cite the real URL when we know it
            source_type="markdown",
            company=company,
            title=header.get("title", path.stem),
            path=local_path,
        ))
    return docs


def is_real_pdf(path: Path) -> bool:
    """A blocked download often saves an HTML error page with a .pdf name. Check the bytes."""
    with path.open("rb") as f:
        return f.read(len(PDF_MAGIC)) == PDF_MAGIC


def load_pdfs(folder: Path, company: str) -> list[Document]:
    docs = []
    for path in sorted(folder.glob("*.pdf")):
        local_path = str(path.relative_to(PROJECT_ROOT))
        if not is_real_pdf(path):
            print(f"  skip {local_path}: not a real PDF (maybe an HTML error page)", file=sys.stderr)
            continue
        try:
            pages = PyPDFLoader(str(path)).load()  # one Document per page
        except PyPdfError as e:  # truncated, corrupt or password-protected
            print(f"  skip {local_path}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        for page in pages:
            if not page.page_content.strip():
                continue  # scanned/image-only pages have no text layer
            docs.append(make_doc(
                page.page_content,
                source=local_path,
                source_type="pdf",
                company=company,
                title=path.stem,
                page=page.metadata.get("page", 0) + 1,  # PyPDFLoader counts from 0
            ))
    return docs


def read_urls(urls_file: Path) -> list[str]:
    if not urls_file.exists():
        return []
    urls = []
    for line in urls_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("http://", "https://")):
            raise ValueError(f"{urls_file.name}: not an http(s) URL: {line!r}")
        urls.append(line)
    return urls


def load_web(urls: list[str], company: str) -> list[Document]:
    docs = []
    for url in urls:  # one at a time, so one dead link doesn't sink the whole batch
        try:
            # raise_for_status: without it, a 404/403 page is "loaded" as if it were content
            loader = WebBaseLoader(
                url,
                header_template={"User-Agent": USER_AGENT},
                raise_for_status=True,
                requests_kwargs={"timeout": WEB_TIMEOUT_S},
            )
            pages = loader.load()
        except requests.RequestException as e:  # HTTP errors, timeouts, DNS failures
            print(f"  skip {url}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if not pages:
            print(f"  skip {url}: loader returned nothing", file=sys.stderr)
            continue
        page = pages[0]
        docs.append(make_doc(
            page.page_content,
            source=url,
            source_type="web",
            company=company,
            title=page.metadata.get("title", url),
        ))
    return docs


# ---------- Orchestration ----------
def dedupe(docs: list[Document]) -> list[Document]:
    """Keep the first Document per (source, page), e.g. a URL also saved as a .md file."""
    seen, unique = set(), []
    for doc in docs:
        key = (doc.metadata["source"], doc.metadata.get("page"))
        if key in seen:
            print(f"  skip duplicate {doc.metadata['source']}", file=sys.stderr)
            continue
        seen.add(key)
        unique.append(doc)
    return unique


def load_company(company: str, data_root: Path = DATA_ROOT) -> list[Document]:
    if not COMPANY_NAME.match(company):
        raise ValueError(f"Company name must be lowercase letters, digits, - or _: {company!r}")
    folder = data_root / company
    if not folder.is_dir():
        raise FileNotFoundError(f"No folder for {company!r} at {folder}")

    docs = (
        load_markdown(folder, company)
        + load_pdfs(folder, company)
        + load_web(read_urls(folder / URLS_FILE), company)
    )
    # Drop empty docs *before* dedupe, so an empty copy can't hide a good duplicate
    docs = dedupe([doc for doc in docs if doc.page_content])
    if not docs:
        raise FileNotFoundError(f"No loadable sources (.md, .pdf, {URLS_FILE}) in {folder}")
    return docs


def print_summary(docs: list[Document]) -> None:
    counts = Counter(doc.metadata["source_type"] for doc in docs)
    total_chars = sum(len(doc.page_content) for doc in docs)
    print(f"\nLoaded {len(docs)} documents ({dict(counts)}), {total_chars:,} characters\n")
    for n, doc in enumerate(docs, start=1):
        meta = doc.metadata
        page = f" p.{meta['page']}" if "page" in meta else ""
        print(f"[{n}] {meta['source_type']:<8} {len(doc.page_content):>6,} chars  {meta['source']}{page}")
    first = docs[0]
    print(f"\nFirst document's metadata: {first.metadata}")
    print(f"First {PREVIEW_CHARS} chars: {first.page_content[:PREVIEW_CHARS]!r}")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m copilot.loaders <company>   e.g. cisco")
    try:
        docs = load_company(sys.argv[1].strip().lower())
    except (ValueError, FileNotFoundError) as e:
        sys.exit(str(e))
    print_summary(docs)


if __name__ == "__main__":
    main()
