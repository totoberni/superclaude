#!/usr/bin/env python3
"""
Crawl4AI native Python client — stdlib only (urllib.request + json).

Endpoint contract (container at 127.0.0.1:11235, no-auth, localhost-only):
  GET  /health          → HTTP 200 when live
  POST /md              body {"url":"<url>"}        → {"markdown":"…","success":true}
  POST /crawl           body {"urls":["…",…]}       → batch result dict

All public functions are FAIL-SAFE: they never raise; return None/False/{} on any
error including connection-refused when the container is down.

BASE_URL is read from env CRAWL4AI_URL, default http://127.0.0.1:11235.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Optional

BASE_URL: str = os.environ.get("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")


def health(timeout: float = 2.0) -> bool:
    """Return True if the Crawl4AI container is reachable, False otherwise.

    Sends GET /health and treats any HTTP 200 as live; any error → False.
    """
    try:
        with urllib.request.urlopen(f"{BASE_URL}/health", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def to_markdown(url: str, timeout: float = 30.0) -> Optional[str]:
    """Fetch a URL and return its markdown representation.

    POST /md with body {"url": url}.
    Returns the markdown string, or None if the request fails or success is falsey.
    """
    payload = json.dumps({"url": url}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/md",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("success"):
                return None
            return body.get("markdown") or None
    except Exception:
        return None


def _extract_markdown(value) -> Optional[str]:
    """Normalise a result's `markdown` field to a plain string.

    The /md endpoint flattens markdown to a string, but /crawl returns the rich
    MarkdownGenerationResult as a dict (raw_markdown, fit_markdown,
    markdown_with_citations, …). Prefer raw_markdown (the full conversion); fall
    back to fit_markdown then markdown_with_citations.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        return (
            value.get("raw_markdown")
            or value.get("fit_markdown")
            or value.get("markdown_with_citations")
            or None
        )
    return None


def crawl_batch(urls: list, timeout: float = 60.0) -> dict:
    """Crawl multiple URLs in one request and return a url→markdown|None mapping.

    POST /crawl with body {"urls": [...]}.
    Returns dict keyed by URL; value is markdown string or None on per-URL failure.
    On total request failure, returns {} (empty dict, never raises).
    """
    if not urls:
        return {}

    payload = json.dumps({"urls": urls}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/crawl",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except Exception:
        return {}

    # Normalise: the /crawl response shape may vary; handle both list and dict forms.
    result: dict = {}

    if isinstance(body, list):
        # List of per-URL result objects
        for item in body:
            u = item.get("url")
            if u is None:
                continue
            md = _extract_markdown(item.get("markdown")) if item.get("success") else None
            result[u] = md or None
    elif isinstance(body, dict):
        # Possibly a dict keyed by URL, or a dict with a "results" key
        if "results" in body and isinstance(body["results"], list):
            for item in body["results"]:
                u = item.get("url")
                if u is None:
                    continue
                md = _extract_markdown(item.get("markdown")) if item.get("success") else None
                result[u] = md or None
        else:
            for u in urls:
                item = body.get(u, {})
                if isinstance(item, dict):
                    md = _extract_markdown(item.get("markdown")) if item.get("success") else None
                    result[u] = md or None
                else:
                    result[u] = None

    # Ensure every requested URL has an entry
    for u in urls:
        result.setdefault(u, None)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_health() -> int:
    ok = health()
    print("up" if ok else "down")
    return 0 if ok else 1


def _cmd_md(url: str) -> int:
    md = to_markdown(url)
    if md is None:
        print(f"error: failed to fetch markdown for {url}", file=sys.stderr)
        return 1
    print(md)
    return 0


def _cmd_batch(urls: list) -> int:
    results = crawl_batch(urls)
    if not results:
        print("error: batch crawl returned no results", file=sys.stderr)
        return 1
    exit_code = 0
    for u, md in results.items():
        if md is None:
            print(f"# {u}\nerror: no markdown returned\n", file=sys.stderr)
            exit_code = 1
        else:
            print(f"# {u}\n{md}\n")
    return exit_code


def _usage() -> None:
    print(
        "usage: better_super_crawl.py --health\n"
        "       better_super_crawl.py --md <url>\n"
        "       better_super_crawl.py --batch <url> [<url> …]",
        file=sys.stderr,
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _usage()
        sys.exit(2)

    flag = args[0]
    if flag == "--health":
        sys.exit(_cmd_health())
    elif flag == "--md":
        if len(args) < 2:
            print("error: --md requires a URL argument", file=sys.stderr)
            sys.exit(2)
        sys.exit(_cmd_md(args[1]))
    elif flag == "--batch":
        if len(args) < 2:
            print("error: --batch requires at least one URL argument", file=sys.stderr)
            sys.exit(2)
        sys.exit(_cmd_batch(args[1:]))
    else:
        print(f"error: unknown flag {flag!r}", file=sys.stderr)
        _usage()
        sys.exit(2)
