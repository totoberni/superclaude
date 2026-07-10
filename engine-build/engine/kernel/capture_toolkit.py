"""Generic, vendor-agnostic browser/HTML capture primitives (W5.1 kernel).

Moved verbatim from the former `engine.browse` shim (the Patchright lifecycle +
the minimal stdlib HTML tree) plus `UA` from `engine.fetch` (W5.1 stage 5): the
shared plumbing every vendor's browser-based field-map capture (ashby graphql
interception, lever DOM parse, and future vendor plugins) and every vendor's
HTTP capture builds on. Vendor-specific parsing (ashby/lever field mapping) now
lives in the per-vendor plugin packages (`engine.providers.ashby.capture`,
`engine.providers.lever.capture`), which import these primitives straight from
this kernel module; the `engine.browse` re-export shim was dissolved in Stage 4.

Import guard: this module imports cleanly
WITHOUT the browser driver installed (stdlib + kernel only). Patchright is
imported lazily inside `_require_patchright`, only when a capture is actually
invoked with the default (real) browser factory.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable

# The HTTP user-agent shared by every vendor's HTTP capture path (fetch.py's
# HttpFetcher default, and the greenhouse/workable HTTP capture in
# fieldmap.py); kept alongside the browser capture plumbing so every capture
# path (browser or HTTP) draws its identifying UA from ONE place.
UA = "abe-automations-jobhunt/0.1 (personal job-search; polite reader)"

# The browser driver is Patchright (an undetected Playwright fork: it patches
# navigator.webdriver + the Runtime.enable CDP leak at source, beating
# playwright-stealth on our non-Cloudflare ATSes). Pinned to the 1.61.x line;
# surfaced verbatim in the not-installed error so an operator copies the exact
# remediation.
PATCHRIGHT_PIN = "patchright==1.61.*"

_TIMEOUT_MS = 20_000


class CaptureShapeError(RuntimeError):
    """A live DOM/graphql shape did not match this fixture-derived parser.

    Raised (never swallowed into an empty FieldMap) with the exact selector or
    key that missed, so a shape drift surfaces loudly during the SSOT playtest
    loop instead of quietly reporting an empty form. The runner catches it and
    counts the item as a failed capture (fail-soft).
    """


# -- patchright lifecycle (lazy import; the only place the driver is touched) ---

def _require_patchright():
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "patchright is not installed, so the browser field-map capture path "
            f"is unavailable. Install the pinned build: pip install "
            f"{PATCHRIGHT_PIN} && patchright install chrome"
        ) from exc
    return sync_playwright


def _headless_default() -> bool:
    """Run headed under a real display (Xvfb sets DISPLAY), headless otherwise.

    Patchright drives real Chrome (`channel="chrome"`); headed under Xvfb is the
    least-detectable mode on toto, while offline/CI (no DISPLAY) falls back to
    headless so the capture still runs without a display server.
    """
    return not bool(os.environ.get("DISPLAY"))


@contextlib.contextmanager
def _default_browser_page(user_data_dir=None):
    """Yield a real-Chrome page with the never-send interceptor already armed.

    One context per capture, torn down unconditionally on exit. A throwaway
    (stateless) context is the default; passing `user_data_dir` opts into a
    persistent per-vendor profile (design default is throwaway for capture). The
    STRUCTURAL never-send interceptor is installed on the context by default
    (defence in depth for the fill-only phase): a submit POST is aborted at the
    network layer regardless of any UI path.
    """
    from engine.kernel.never_send import install_never_send  # lazy: keeps import light

    sync_playwright = _require_patchright()
    headless = _headless_default()
    with sync_playwright() as controller:
        if user_data_dir is not None:
            context = controller.chromium.launch_persistent_context(
                str(user_data_dir), channel="chrome", headless=headless)
            browser = None
        else:
            browser = controller.chromium.launch(
                channel="chrome", headless=headless)
            context = browser.new_context()  # anonymous: no storage_state, no cookies
        try:
            install_never_send(context)
            page = context.new_page()
            page.set_default_timeout(_TIMEOUT_MS)
            yield page
        finally:
            context.close()
            if browser is not None:
                browser.close()


# -- minimal HTML tree (stdlib html.parser; engine core stays dependency-free) -

# HTML elements with no end tag; the tree builder must not expect to pop them.
_VOID_TAGS = frozenset({
    "input", "br", "img", "hr", "meta", "link", "source", "col", "area",
    "base", "embed", "param", "track", "wbr",
})


class _Node:
    __slots__ = ("tag", "attrs", "children", "text_parts")

    def __init__(self, tag: str, attrs):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children: list[_Node] = []
        self.text_parts: list[str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", [])
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs)
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self._stack[-1].children.append(_Node(tag, attrs))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self._stack[-1].text_parts.append(data)


def _build_tree(html_source: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html_source or "")
    builder.close()
    return builder.root


def _find_all(node: _Node, predicate) -> list[_Node]:
    out: list[_Node] = []
    for child in node.children:
        if predicate(child):
            out.append(child)
        out.extend(_find_all(child, predicate))
    return out


def _first(node: _Node, predicate):
    for child in node.children:
        if predicate(child):
            return child
        found = _first(child, predicate)
        if found is not None:
            return found
    return None


def _has_class(node: _Node, cls: str) -> bool:
    return cls in (node.attrs.get("class") or "").split()


def _node_text(node: _Node, exclude_cls: str | None = None) -> str:
    if exclude_cls and _has_class(node, exclude_cls):
        return ""
    parts = list(node.text_parts)
    for child in node.children:
        parts.append(_node_text(child, exclude_cls))
    return " ".join(part.strip() for part in parts if part and part.strip())


# -- shared helpers ------------------------------------------------------------

def _response_url(response) -> str:
    return getattr(response, "url", "") or ""


def _dig(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _now(now: Callable[[], str] | None) -> str:
    return (now or _utc_now_iso)()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
