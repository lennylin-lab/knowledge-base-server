"""Seed a running knowledge base through its HTTP API.

Reads the markdown documents in `tests/seeds/` (front matter included) and
POSTs each to `POST /api/v1/documents`, so the server-side front-matter
parsing, chunking, and indexing pipeline run exactly as in production.

Idempotent by title: documents whose (front-matter derived) title already
exists are skipped; `--force` disables the check and always creates. The
created documents index asynchronously — see README ("Re-indexing after a
search-index change") for the sweep when a leg falls behind.

Usage:
    uv run python tests/seed_data.py [--base-url http://localhost:8000] [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import frontmatter
import httpx

SEEDS_DIR = Path(__file__).resolve().parent / "seeds"
DEFAULT_BASE_URL = "http://localhost:8000"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 30.0


def derived_title(content: str) -> str:
    """Mirror the service's title resolution for a title-less create request.

    Front-matter title (non-empty str) wins, otherwise "Untitled" — the same
    fallback `DocumentService._parse_front_matter` applies, so the dedupe
    check matches what the API will store.
    """
    metadata, _ = frontmatter.parse(content)
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return "Untitled"


def existing_titles(client: httpx.Client) -> set[str]:
    """Collect the titles of every live document, walking keyset pagination."""
    titles: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": PAGE_SIZE}
        if cursor is not None:
            params["cursor"] = cursor
        response = client.get("/api/v1/documents", params=params)
        response.raise_for_status()
        page = response.json()
        titles.update(item["title"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return titles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help=f"API base URL (default {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Create even when a document with the same title already exists",
    )
    args = parser.parse_args(argv)

    seed_files = sorted(SEEDS_DIR.glob("*.md"))
    if not seed_files:
        print(f"no seed documents found in {SEEDS_DIR}", file=sys.stderr)
        return 1

    failures = 0
    with httpx.Client(base_url=args.base_url, timeout=REQUEST_TIMEOUT) as client:
        titles = set() if args.force else existing_titles(client)
        for path in seed_files:
            content = path.read_text(encoding="utf-8")
            title = derived_title(content)
            if title in titles:
                print(f"SKIP {path.name}: '{title}' already exists")
                continue
            response = client.post("/api/v1/documents", json={"content": content})
            if response.status_code == 201:
                print(f"OK   {path.name}: created '{title}' ({response.json()['id']})")
            else:
                failures += 1
                detail = response.text[:200]
                print(
                    f"FAIL {path.name}: HTTP {response.status_code} for '{title}' — {detail}",
                    file=sys.stderr,
                )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
