"""FROZEN never-send guard (W5.1 stage 0): moved verbatim from engine.providers.base, byte-identical to tag never-send-sealed-v1."""

from __future__ import annotations

import json
import re
import urllib.parse


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
