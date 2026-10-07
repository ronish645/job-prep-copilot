"""Save web pages as Markdown files in data/<company>/, so the corpus is frozen and versioned.

Live pages change. Evals (step 7) need a corpus that doesn't move under them, and git
shows exactly what changed when we re-snapshot. Files use the same 'Source:' / 'Title:'
header that load_markdown() reads, so a snapshot cites the original URL.

Usage: python -m copilot.snapshot cisco https://developer.cisco.com/meraki/api-v1/authorization/
"""

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from copilot.loaders import COMPANY_NAME, DATA_ROOT, PROJECT_ROOT, clean_text, fetch_page

# Meraki API keys are 40 hex characters. Docs contain example keys, and GitHub's secret
# scanning blocks pushes with key-shaped strings, so they never get written to disk.
API_KEY_LIKE = re.compile(r"\b[0-9a-f]{40}\b")
KEY_PLACEHOLDER = "<YOUR_MERAKI_API_KEY>"


def redact(text: str) -> str:
    return API_KEY_LIKE.sub(KEY_PLACEHOLDER, text)


def snapshot_filename(url: str) -> str:
    """https://developer.cisco.com/meraki/api-v1/rate-limit/ -> meraki_api_v1_rate_limit.md"""
    slug = re.sub(r"[^a-z0-9]+", "_", urlparse(url).path.lower()).strip("_")
    return f"{slug or 'index'}.md"


def snapshot(url: str, folder: Path) -> Path:
    title, markdown = fetch_page(url)
    path = folder / snapshot_filename(url)
    path.write_text(f"Source: {url}\nTitle: {title}\n\n{redact(clean_text(markdown))}\n", encoding="utf-8")
    return path


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("Usage: python -m copilot.snapshot <company> <url> [<url> ...]")
    company, urls = sys.argv[1].strip().lower(), sys.argv[2:]
    if not COMPANY_NAME.match(company):
        sys.exit(f"Company name must be lowercase letters, digits, - or _: {company!r}")
    folder = DATA_ROOT / company
    folder.mkdir(parents=True, exist_ok=True)
    for url in urls:
        if not url.startswith(("http://", "https://")):
            print(f"  skip {url!r}: not an http(s) URL", file=sys.stderr)
            continue
        try:
            path = snapshot(url, folder)
        except requests.RequestException as e:
            print(f"  skip {url}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        print(f"  saved {path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
