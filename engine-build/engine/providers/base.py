"""Shared browser primitives every ATS provider builds on (W5.1 spine).

This module is the single home for the cross-vendor fill mechanics that the
per-vendor providers (greenhouse/lever/ashby/workable, landing in W5.2) reuse:
the four live fill primitives from `engine.fill`, a STRUCTURAL never-send network
interceptor, human-cadence typing, a DOM-sweep completeness check, and the
react-select combobox driver that W4 deferred.

LAZY-IMPORT INVARIANT (load-bearing, mirrors registry.py): the daily poller must
never load the browser stack. `engine.run` imports `engine.providers` (the package
__init__, which pulls in `registry` only), NOT this module, and this module never
imports patchright or `engine.browse` at load time. Every browser reference here is
resolved through the page/locator/route objects the caller passes in; the only
cross-module import (`engine.fill`, itself patchright-free) happens at CALL time
inside the re-export wrappers.

FILL-PRIMITIVE ACCESS -- re-export via call-time wrappers, NOT a top-level
`from engine.fill import ...` and NOT a code move out of fill.py:
- No code is moved out of the live W4 `fill.py` (the running jobhunt fills through
  it); a move would be a needless risk. The primitives stay where `fill_form` uses
  them and are surfaced here by thin pass-through wrappers.
- The wrappers look the target up on the `engine.fill` module object at call time,
  so they honour the monkeypatch seam (a test patching `engine.fill._safe_click`
  is reflected here) exactly as `registry.py` looks up `browse.capture_ashby` at
  call time. A top-level `from engine.fill import _safe_click` would bind the
  reference at import and defeat that seam.
- Importing `engine.fill` lazily (inside the wrappers) also keeps this module's own
  import cheap and dodges any import-order fragility with the providers package.

The NEW primitives (`install_never_send`, `type_human`, `sweep_required` +
`completeness_mismatch`, `select_react_combobox`) are pure-Python here: they drive
whatever page/locator/route object is handed to them, so their branching logic is
unit-tested now with fakes and their live-DOM behaviour is fixture-validated in
W5.2.
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.parse

# -- re-exported fill primitives (call-time lookup preserves the patch seam) ----


def _fill():
    """The live `engine.fill` module, imported lazily so this module stays cheap
    to load and the reference is resolved fresh on every call (patch seam)."""
    from engine import fill
    return fill


def _safe_click(*args, **kwargs):
    """Re-export of `engine.fill._safe_click` (the sole sanctioned click gateway;
    refuses any submit-like accessible name)."""
    return _fill()._safe_click(*args, **kwargs)


def _safe_upload(*args, **kwargs):
    """Re-export of `engine.fill._safe_upload` (whitelisted-asset attach; never
    submits)."""
    return _fill()._safe_upload(*args, **kwargs)


def _readback(*args, **kwargs):
    """Re-export of `engine.fill._readback` (reads a control back to confirm a
    value actually landed)."""
    return _fill()._readback(*args, **kwargs)


def _locate(*args, **kwargs):
    """Re-export of `engine.fill._locate` (role/label locator resolution)."""
    return _fill()._locate(*args, **kwargs)


# -- STRUCTURAL never-send (HOLE-FIX a): abort submit POSTs at the network layer -
# The last-line network guarantee that the fill-only phase never applies. Registered
# on every launched context/page by default (browse.py); a submit POST is aborted
# before it leaves the browser regardless of any UI bug.
#
# The submit-endpoint URL set is derived from the registry apply hosts
# (greenhouse.io / lever.co / ashbyhq.com / workable.com) plus the known
# per-vendor application-submission paths (spec section 1 table).

# POST to any of these URLs is unconditionally a form submission -> abort.
_SUBMIT_URL_PATTERNS = (
    # Greenhouse: legacy embed application POST, board-API / job-board application
    # POST (.../jobs/{id}[/applications]), and any explicit /applications action.
    re.compile(
        r"greenhouse(?:-api)?\.io/(?:embed/job_app\b|"
        r".*?/jobs/\d+(?:/applications?)?/?(?:[?#]|$))", re.I),
    re.compile(r"greenhouse(?:-api)?\.io/.*?/applications?(?:[/?#]|$)", re.I),
    # Lever: the apply-form POST (jobs.lever.co/{slug}/{id}/apply) + api postings apply.
    re.compile(r"lever\.co/.*?/apply(?:[/?#]|$)", re.I),
    re.compile(r"lever\.co/(?:v\d+/)?postings/[^/]+/[^/]+/apply\b", re.I),
    # Ashby: the direct application-submit path (the non-graphql submit shape).
    re.compile(r"ashbyhq\.com/.*?application(?:Form)?[./]submit\b", re.I),
    # Workable: candidate-create (application submission) + apply POST.
    re.compile(r"workable\.com/.*?/candidates?(?:[/?#]|$)", re.I),
    re.compile(
        r"workable\.com/(?:spi/v\d+/)?.*?/(?:apply|application)(?:[/?#]|$)", re.I),
    # Workable defense-in-depth (W5.4, additive): the two workable patterns above
    # are keyed on the literal `workable.com` host, which is all discovery ever
    # emits (apply.workable.com). These two cover the paths that host match would
    # miss, both still POST-gated by `_is_submit_request`:
    #  (a) the apply POST on a CUSTOM-DOMAIN / redirect tenant (host-agnostic): the
    #      shortcode-keyed submit path is distinctive enough to abort on any host.
    re.compile(r"/api/v\d+/jobs/[A-Z0-9]{6,}/apply(?:[/?#]|$)", re.I),
    #  (b) the post-submit EEOC send (a second application-data POST after apply).
    re.compile(r"workable\.com/api/v\d+/eeoc/", re.I),
)

# Graphql endpoints carry BOTH read queries and submit mutations on ONE URL, so a
# POST here is aborted only when the request's operation actually resolves to a
# submit op, never by scanning the whole raw body for a submit-keyword substring:
# that over-matched a real Ashby READ operation (ApiOrganizationFromHostedJobsPageName)
# whose query TEXT happened to reference a submitApplicationForm-shaped field name
# (confirmed live on toto 2026-07-05; see _graphql_submit_match below). The decision
# is never-send biased throughout: an ALLOW requires a fully-parsed body where every
# operationName is innocent, no mutation+submit-keyword co-occurrence, and no
# submit-flagged `op=` URL param; any ambiguity blocks instead.
_SUBMIT_GRAPHQL_URL_PATTERNS = (
    re.compile(r"ashbyhq\.com/.*?non-user-graphql", re.I),
    re.compile(r"workable\.com/.*?graphql", re.I),
)
_SUBMIT_OPERATION_RE = re.compile(
    r"submitApplication|SubmitApplicationForm|applicationForm\.submit|"
    r"ApplicationSubmit|createCandidate|candidateCreate|submitForm", re.I)

# The GraphQL mutation keyword is lowercase syntax, not a type name (case-sensitive
# so a result type like `...MutationResult` never counts as one).
_GRAPHQL_MUTATION_RE = re.compile(r"\bmutation\b")


def _graphql_operation_names(post_data: str | None) -> tuple[list[str], bool]:
    """Parse a graphql body into (operation names, complete).

    `complete` is True only when every operation the body will execute resolved to
    a non-empty string `operationName`: a single dict root with one, or a batched
    list root where EVERY element is a dict with one. Anything the parse cannot
    fully vouch for (unparseable JSON, a non-dict/list root, an empty/None body, a
    dict root with no usable operationName, or any batched element missing one)
    yields complete=False, which keeps the whole-body fail-safe (rule 4 in
    `_graphql_submit_match`) live for it. Never-send bias: ambiguity here never
    resolves to "safe", only to "unresolved".
    """
    if not post_data:
        return [], False
    try:
        parsed = json.loads(post_data)
    except (ValueError, TypeError):
        return [], False
    if isinstance(parsed, dict):
        name = parsed.get("operationName")
        return ([name], True) if isinstance(name, str) and name else ([], False)
    if isinstance(parsed, list):
        names: list[str] = []
        complete = True
        for element in parsed:
            name = element.get("operationName") if isinstance(element, dict) else None
            if isinstance(name, str) and name:
                names.append(name)
            else:
                complete = False
        return names, complete
    return [], False


def _url_op_params(url: str) -> list[str]:
    """All values of every URL query-string key named `op` (case-insensitive).

    Used ONLY as a block-only signal (see `_graphql_submit_match`): the URL is
    client-controlled independently of the body it sends, so a submit-shaped
    `op=` value is evidence of intent to abort, but its absence is never evidence
    that the body itself is safe. The key is matched case-insensitively and
    EVERY value is collected (not just the first), so neither an `OP=` casing
    variant nor a repeated `op=` parameter can dodge the check.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    values: list[str] = []
    for key, vals in query.items():
        if key.lower() == "op":
            values.extend(vals)
    return values


def _all_ops_carry_inline_query(post_data: str | None) -> bool:
    """True iff every operation the body executes carries non-empty query text
    (no persisted/hash-only op). Empty/unparseable -> False (never-send bias).

    A persisted-query (APQ) op ships only a `sha256Hash`, not the `query` text
    itself; its innocence cannot be vouched for by a text search the way a
    fully-inlined operation's can, so rule 4 (see `_graphql_submit_match`)
    treats any body containing one as unresolved regardless of operationName.
    """
    if not post_data:
        return False
    try:
        parsed = json.loads(post_data)
    except (ValueError, TypeError):
        return False

    def _has_query(element) -> bool:
        return (isinstance(element, dict)
                and isinstance(element.get("query"), str)
                and bool(element.get("query")))

    if isinstance(parsed, dict):
        return _has_query(parsed)
    if isinstance(parsed, list):
        return bool(parsed) and all(_has_query(element) for element in parsed)
    return False


def _graphql_submit_match(url: str, post_data: str | None) -> bool:
    """True iff a graphql-endpoint POST's URL or body names a submit operation.

    Never-send bias: these are independent BLOCK conditions, never ALLOW
    conditions -- an allow falls out only when none of them fire. Checked:
      1. any parsed operationName (single or batched) matches the submit regex;
      2. any of the URL's `op` query-string values (key matched
         case-insensitively, every value checked) matches the submit regex --
         block-only signal, NEVER used as evidence to allow (a client-set URL
         param can disagree with what the body actually executes);
      3. the body contains a GraphQL `mutation` keyword AND the submit regex
         matches the body -- defense in depth for a submit mutation renamed to
         an innocent operationName;
      4. the submit regex matches the whole body AND EITHER the operationName(s)
         could not be fully resolved (unparseable body, missing name, ...) OR
         any executed operation is persisted/hash-only (no inline `query` text,
         i.e. an APQ) -- the whole-body fail-safe for anything the parse cannot
         fully vouch for. A persisted op's innocence cannot be confirmed by
         query text the way an inlined operation's can, so it is treated the
         same as an unresolved operationName. Accepted cost: a persisted READ
         carrying a stray submit token elsewhere in the body is false-blocked
         (the annoying side of the asymmetry, never-send bias throughout).
    """
    names, complete = _graphql_operation_names(post_data)
    if any(_SUBMIT_OPERATION_RE.search(name) for name in names):
        return True
    if any(_SUBMIT_OPERATION_RE.search(value) for value in _url_op_params(url)):
        return True
    body = post_data or ""
    if _GRAPHQL_MUTATION_RE.search(body) and _SUBMIT_OPERATION_RE.search(body):
        return True
    return bool(_SUBMIT_OPERATION_RE.search(body)) and (
        not complete or not _all_ops_carry_inline_query(post_data))


def _is_submit_request(method: str, url: str, post_data: str | None) -> bool:
    """True iff this is a POST to a vendor application-submit endpoint.

    GETs (and every other verb) pass -- only a POST can submit. A POST to a plain
    submit URL matches outright; a POST to a shared graphql endpoint is decided by
    `_graphql_submit_match` (operation-name resolution, never a raw whole-body
    substring scan alone), so a form-schema read whose query TEXT merely mentions
    a submit-shaped field name is never aborted. Never-send bias throughout: any
    case the guard cannot fully resolve blocks rather than allows.
    """
    if (method or "").upper() != "POST":
        return False
    url = url or ""
    for pattern in _SUBMIT_URL_PATTERNS:
        if pattern.search(url):
            return True
    for pattern in _SUBMIT_GRAPHQL_URL_PATTERNS:
        if pattern.search(url) and _graphql_submit_match(url, post_data):
            return True
    return False


def _never_send_handler():
    """Build the `page.route` handler that aborts submit POSTs, passes the rest."""
    def _handler(route):
        request = getattr(route, "request", None)
        method = getattr(request, "method", "") or ""
        url = getattr(request, "url", "") or ""
        post_data = _request_post_data(request)
        if _is_submit_request(method, url, post_data):
            route.abort()
        else:
            route.continue_()
    return _handler


def _request_post_data(request) -> str | None:
    """Best-effort read of a request body; None when absent or unreadable."""
    if request is None:
        return None
    try:
        return request.post_data
    except Exception:
        return None


def install_never_send(context_or_page):
    """Register a route interceptor that ABORTS any application-submit POST.

    Installs at CONTEXT scope whenever possible so the guard covers EVERY page
    the context opens -- including a popup / new tab a submit-form opens mid-fill,
    which a page-scoped route would NOT cover (that page escaped the interceptor).
    When handed a bare Page, the route is installed on `page.context`; when handed
    a BrowserContext (or any object with no reachable `.context`), it is installed
    on that object directly (safe fallback). GETs and non-submit POSTs pass
    through untouched; a POST whose method+URL (and, for graphql endpoints, body)
    matches a vendor submit endpoint is aborted before it leaves the browser.
    Returns the handler so a caller (or test) can drive it directly.
    """
    handler = _never_send_handler()
    _route_target(context_or_page).route("**", handler)
    return handler


def _route_target(context_or_page):
    """Prefer the browser CONTEXT (covers every page/popup it opens) over a bare
    page: return `context_or_page.context` when present, else the object itself.

    A Playwright Page exposes `.context` (its owning BrowserContext); a
    BrowserContext has no `.context`, so it is used directly -- the safe fallback
    that also keeps a caller passing a context (or a page-shaped fake with no
    `.context`) working unchanged.
    """
    context = getattr(context_or_page, "context", None)
    if context is not None:
        return context
    return context_or_page


# -- human-cadence typing (reCAPTCHA v3 score protection) ----------------------


def type_human(locator, text, *, min_delay: float = 60, max_delay: float = 180):
    """Type `text` into `locator` one real keystroke at a time, random per-char delay.

    Uses `press_sequentially` (genuine key events) with a fresh random inter-key
    delay in `[min_delay, max_delay]` ms per character. NEVER `locator.fill()` and
    NEVER any JS injection: reCAPTCHA v3 scores an instant value-set as bot-like,
    so the whole point is a human keystroke cadence. `locator` is injected by the
    caller (a real Playwright locator live; a fake in tests).

    A live Greenhouse run showed the FIRST character silently dropped
    (`first_name` "Federico" landed as "ederico" in the post-fill DOM): typing
    started before the control was focus-ready, so the leading keydown raced
    the control's own focus-in handling and never registered. `_settle_focus`
    clicks the control first (a real Playwright click already waits for
    actionability), so every text field -- not just first_name -- is focused
    and settled before any keystroke is sent."""
    if not text:
        return
    _settle_focus(locator)
    for char in str(text):
        delay = random.uniform(min_delay, max_delay)
        locator.press_sequentially(char, delay=delay)


def _settle_focus(locator) -> None:
    """Click `locator` to force focus to land and settle before typing.

    Never raises: a fake/partial locator with no `.click()` (or one whose
    click fails) still falls through to typing rather than crashing the
    fill -- this is a best-effort settle, not a hard precondition."""
    clicker = getattr(locator, "click", None)
    if not callable(clicker):
        return
    try:
        clicker()
    except Exception:
        pass


# -- DOM-sweep completeness (HOLE-FIX d) ---------------------------------------
# A form is COMPLETE only when the DOM's required-field set and the schema's
# required-field set agree. Lever carries no custom-question schema at all, so the
# DOM sweep is the sole completeness oracle there; for the schema vendors it is a
# cross-check that the schema did not miss a field the page actually requires.

_REQUIRED_CSS = "[required], [aria-required='true']"
_ASTERISK_CSS = "label, legend"


def _normalize_name(text) -> str:
    """Lowercase, strip `*` required-markers, and collapse whitespace to a stable
    accessible-name key so DOM and schema names compare apples-to-apples."""
    if not text:
        return ""
    cleaned = str(text).replace("*", " ")
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def completeness_mismatch(schema_required: set[str],
                          dom_required: set[str]) -> dict:
    """Two-directional diff of the required-field sets (normalized both sides).

    Returns {"dom_only": [...], "schema_only": [...]} sorted for determinism.
    `dom_only` = required on the page but absent from the schema (the schema
    missed a field); `schema_only` = required by the schema but not found on the
    page. Any non-empty side means the form is NOT_COMPLETE.
    """
    schema = {_normalize_name(name) for name in (schema_required or set())} - {""}
    dom = {_normalize_name(name) for name in (dom_required or set())} - {""}
    return {
        "dom_only": sorted(dom - schema),
        "schema_only": sorted(schema - dom),
    }


def sweep_required(page) -> set[str]:
    """Enumerate the page's visible required-looking controls -> normalized names.

    Collects controls carrying `required` / `aria-required="true"` plus fields whose
    label/legend shows a visible asterisk, skipping aria-hidden / offscreen nodes,
    and returns their normalized accessible-name set. The live-DOM extraction is
    fixture-validated in W5.2; the normalization + diff logic (`_normalize_name`,
    `completeness_mismatch`) is unit-tested now.
    """
    names: set[str] = set()
    for locator in _visible_locators(page, _REQUIRED_CSS):
        name = _normalize_name(_accessible_name(locator))
        if name:
            names.add(name)
    for locator in _visible_locators(page, _ASTERISK_CSS):
        text = _locator_text(locator)
        if text and "*" in text:
            name = _normalize_name(text)
            if name:
                names.add(name)
    return names


def _visible_locators(page, css: str) -> list:
    """All locators matching `css` that are visible and not aria-hidden.

    Guarded end to end: a page/locator missing a probed method is treated as
    zero matches rather than raising, so a partial fake never crashes the sweep."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return []
    try:
        candidates = locator_fn(css).all()
    except Exception:
        return []
    visible: list = []
    for locator in candidates or []:
        if _is_visible(locator) and not _is_aria_hidden(locator):
            visible.append(locator)
    return visible


def _is_visible(locator) -> bool:
    checker = getattr(locator, "is_visible", None)
    if checker is None:
        return True
    try:
        return bool(checker())
    except Exception:
        return False


def _is_aria_hidden(locator) -> bool:
    try:
        return (locator.get_attribute("aria-hidden") or "").strip().lower() == "true"
    except Exception:
        return False


def _accessible_name(locator) -> str:
    """Best-effort accessible name: aria-label, then label text, then placeholder,
    then the control's own name attribute (live-DOM refinement is W5.2's job)."""
    for attr in ("aria-label", "placeholder", "name"):
        try:
            value = (locator.get_attribute(attr) or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return _locator_text(locator)


def _locator_text(locator) -> str:
    getter = getattr(locator, "inner_text", None) or getattr(locator, "text_content", None)
    if getter is None:
        return ""
    try:
        return (getter() or "").strip()
    except Exception:
        return ""


# -- react-select combobox driver (Greenhouse; W4-deferred) --------------------
# LIVE-DOM FIX #1 (2026-07-06, gitlab/8503792002 acceptance run): the driver used
# to click `#react-select-{field_id}-input` to open the widget -- that id DOES
# NOT EXIST on Greenhouse's react-select v5 markup, so every combobox timed out
# before a single option could be picked. The ids Greenhouse's live DOM DOES
# confirm are prefixed `react-select-{field_id}-` (e.g. `-placeholder`,
# `-live-region`); the control itself is `div.select__control` (a build-hashed
# class suffix, e.g. `remix-css-13cymwt-control`, so never matched by a full
# class-string equality), containing `div.select__value-container` >
# `div.select__placeholder` (the confirmed `-placeholder` id) and
# `div.select__input-container` > the control's own `<input>`. The driver below
# anchors on the CONFIRMED placeholder id (via Playwright's `:has()` CSS
# extension) to reach the control.
#
# LIVE-DOM FIX #2 (2026-07-06, same acceptance run, re-run after fix #1): even
# with the control correctly reached, clicking the rendered `div.select__option`
# left `.select__single-value` empty on every one of the 4 comboboxes (all read
# back "value did not take"). Clicking a react-select option div is unreliable
# under Playwright/Patchright: the click can land before the option's own click
# handler is wired, or race the menu's re-render after each filter keystroke.
# The robust, well-known react-select pattern is TYPE-TO-FILTER then ENTER --
# react-select keeps the first filtered row highlighted and commits it on
# `Enter`, exactly like a human using the widget with the keyboard alone (never
# `Escape` first, which would just close the menu with no selection). The
# driver now presses `Enter` on the control's own input instead of clicking any
# `div.select__option`, so the menu-lookup/option-click step is gone entirely.


def _combobox_control_selector(field_id: str) -> str:
    """CSS for the field's react-select control div, scoped by the per-field
    `-live-region` id, which PERSISTS across selection (unlike `-placeholder`,
    which unmounts the moment a value is picked, so a placeholder-anchored
    scope silently stops matching the control post-commit and the readback
    reads empty). The live-region node is a DIRECT child of the react-select
    container alongside `.select__control`, so anchoring on it reaches the
    control in BOTH the empty and the selected state. Live-DOM verified."""
    return (f'div:has(> [id="react-select-{field_id}-live-region"]) '
            f'div.select__control')


def select_react_combobox(page, field_id: str, option_text: str, *,
                          min_delay: float = 60, max_delay: float = 180,
                          poll_ms: tuple[int, ...] = (200, 500)) -> bool:
    """Drive one react-select combobox: open, filter, commit, and confirm.

    Sequence (fresh locators at every step; react-select recycles nodes):
      1. Click the field's control (scoped via `_combobox_control_selector`,
         anchored on the confirmed `-placeholder` id) to open the menu.
      2. `type_human` the option text into the control's own input to filter
         the menu (never fill()) -- also the long-country-list path.
      3. Press `Enter` on that same input: react-select commits the
         highlighted (first-filtered) option itself -- never a `div.select__
         option` click, which does not reliably commit (see LIVE-DOM FIX #2
         above).
      4. Poll `.select__single-value` (scoped to the field's control) at
         +200/+500 ms to confirm the value landed.
      5. Dismiss with Escape -- harmless (react-select already closed the menu
         on Enter-commit) but a safe no-op net for the case Enter had nothing
         to commit (e.g. no option matched the filter). NEVER blur (a blur can
         re-open / clear the widget).

    Returns True iff the readback confirms the selection landed.
    """
    control = _combobox_control(page, field_id)
    control.click()
    combo_input = _combobox_input(page, field_id)
    type_human(combo_input, option_text, min_delay=min_delay, max_delay=max_delay)

    # Commit via a FOCUS-FOLLOWING keyboard Enter, not the filter input's own
    # press: react-select re-renders (detaches) the filter input on each
    # keystroke, so `combo_input.press("Enter")` hangs on Playwright's
    # actionability wait for a now-stale node. The live input still holds
    # focus, so the page keyboard commits the highlighted first-filtered option
    # reliably. Live-DOM verified. Falls back to the locator's own press for
    # the offline fake harness (no `page.keyboard`).
    _keyboard_press(page, combo_input, "Enter")

    landed = _poll_single_value(page, field_id, option_text, poll_ms)
    # Dismiss the still-open menu (a no-op after an Enter-commit) without a blur.
    _keyboard_press(page, combo_input, "Escape")
    return landed


def _keyboard_press(page, locator, key: str) -> None:
    """Press `key` on the PAGE keyboard (focus-following, so it survives react-
    select re-rendering/detaching its filter input mid-interaction) when the
    page exposes one; otherwise fall back to the locator's own `press` (the
    offline fake-harness path, which has no `page.keyboard`)."""
    keyboard = getattr(page, "keyboard", None)
    presser = getattr(keyboard, "press", None) if keyboard is not None else None
    if callable(presser):
        presser(key)
        return
    locator_press = getattr(locator, "press", None)
    if callable(locator_press):
        locator_press(key)


def _combobox_control(page, field_id: str):
    """A FRESH locator for the field's react-select control div."""
    return page.locator(_combobox_control_selector(field_id))


def _combobox_input(page, field_id: str):
    """A FRESH locator for the control's own text input (there is exactly one
    per control; react-select recycles the node, so this is re-resolved on
    every call rather than cached)."""
    return _combobox_control(page, field_id).locator("input")


def _poll_single_value(page, field_id: str, option_text: str,
                       poll_ms: tuple[int, ...]) -> bool:
    """Poll the rendered `.select__single-value` at the given cumulative offsets.

    Returns True as soon as the shown value contains the chosen option text. Waits
    are the cumulative deltas so `(200, 500)` reads at +200 ms then +500 ms."""
    want = _normalize_name(option_text)
    if not want:
        return False
    elapsed = 0
    for mark in poll_ms:
        _wait_timeout(page, mark - elapsed)
        elapsed = mark
        shown = _normalize_name(_single_value_text(page, field_id))
        if shown and want in shown:
            return True
    return False


def _single_value_text(page, field_id: str) -> str:
    """The field's currently-shown `.select__single-value` text, scoped to
    its own control. A control that no longer matches the `-placeholder`-
    anchored scope (e.g. the placeholder unmounts once a value is selected --
    UNVERIFIED live, flagged for the owner's live iteration) degrades to a
    fast empty read via `.count()` rather than hanging on Playwright's
    default actionability wait for a selector that will never resolve."""
    locator_fn = getattr(page, "locator", None)
    if locator_fn is None:
        return ""
    try:
        single_value = _combobox_control(page, field_id).locator(
            ".select__single-value")
        counter = getattr(single_value, "count", None)
        if callable(counter) and counter() == 0:
            return ""
        return _locator_text(single_value)
    except Exception:
        return ""


def _wait_timeout(page, ms: int) -> None:
    if ms <= 0:
        return
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        waiter(ms)
    else:
        time.sleep(ms / 1000.0)
