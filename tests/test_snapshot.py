"""Tests for copilot/snapshot.py. No network: fetch_page is replaced with a fake."""

from pathlib import Path

import pytest

import copilot.snapshot as snapshot
from copilot.loaders import split_header

FAKE_KEY = "0123456789abcdef" * 2 + "01234567"  # 40 hex chars, the shape of a Meraki key


def test_redact_replaces_40_hex_api_keys():
    assert snapshot.redact(f"API_KEY = '{FAKE_KEY}'") == "API_KEY = '<YOUR_MERAKI_API_KEY>'"


def test_redact_leaves_shorter_hex_alone():
    assert snapshot.redact("color #ff00aa, id 1234abcd") == "color #ff00aa, id 1234abcd"


@pytest.mark.parametrize("url, expected", [
    ("https://developer.cisco.com/meraki/api-v1/rate-limit/", "meraki_api_v1_rate_limit.md"),
    ("https://example.com/", "index.md"),
])
def test_snapshot_filename(url: str, expected: str):
    assert snapshot.snapshot_filename(url) == expected


def test_snapshot_writes_header_and_redacted_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(snapshot, "fetch_page", lambda url: ("Auth", f"# Auth\n\n\n\nkey {FAKE_KEY}"))
    path = snapshot.snapshot("https://x.dev/auth", tmp_path)

    header, body = split_header(path.read_text())
    assert header == {"source": "https://x.dev/auth", "title": "Auth"}  # load_markdown can read it
    assert FAKE_KEY not in body
    assert body.strip() == "# Auth\n\nkey <YOUR_MERAKI_API_KEY>"
