"""L1 pure-code, fail-closed anti-injection validator (spec 6b, section 4).

Design principle (spec section 4, non-negotiable): posting text NEVER determines
field VALUES. Identity values flow code-only from the SSOT; the posting shapes
PROSE only, and the prose is validated here. Detection is a BACKSTOP, not the
primary defense: this validator assumes a `generated_output` may already have
been tampered with and re-verifies every value against the SSOT ground truth.

The validator is FAIL-CLOSED: any violation, an unverifiable SSOT reference, or
an internal error all yield `ValidationResult.ok == False`. It never trusts a
value it cannot positively confirm.

Checks implemented (all pure code, no LLM):
- identity fields (email/phone/name/url/address) BYTE-EQUAL to the SSOT after
  canonicalization (lowercase email, E.164-ish phone, normalized URL);
- enum answers are members of the SSOT canned-answer set;
- free-text link scan: every candidate link HOST is allowlist-checked against the
  SSOT link hosts REGARDLESS of scheme (schemed `http(s)://`, protocol-relative
  `//host`, or scheme-less `host/path`), and every email/phone must be on the
  SSOT allowlist;
- cross-field exfil: SSOT sensitive values (a `sensitive` section) must not
  appear in a free-text field not authorized to carry them;
- invisible characters (zero-width, bidi, BOM, soft hyphen, Unicode tag chars,
  controls) rejected everywhere;
- homoglyphs: Cyrillic/Greek/Armenian/Cherokee/Coptic letters that impersonate
  Latin (NFKC canonicalizes, a confusable-script scan flags what survives);
- base64-like blobs in free text;
- markdown links whose href host is not allowlisted, or whose visible-text host
  differs from the href host;
- structural: field set is a subset of the schema, per-field length bounds, and
  no HTML/script markup in any value.

`field_schema` is a mapping `{field_key: spec}` where `spec` is a dict:
- identity:  `{"class": "identity", "identity_kind": "email"|"phone"|"name"|
             "url"|"address", "ssot": "<dotted path>"}`
- enum:      `{"class": "enum", "ssot_answers": "<dotted path>"}`
- free_text: `{"class": "free_text", "max_len": int (optional),
             "authorized_sensitive": ["<dotted path>", ...] (optional)}`

The free-text allowlist (authorized URLs/emails/phones) and the sensitive set are
derived from the SSOT by convention (see the module constants), so free-text
fields need no per-field allowlist wiring.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from engine.ssot import MISSING, SSOT

# --- Convention: which SSOT sections seed the free-text allowlist / sensitive set.
# The engine has one SSOT shape; these names are the contract. Contact values the
# generated prose is allowed to echo:
_ALLOWLIST_LINK_SECTION = "links"          # every leaf URL's host under links.* is allowed
_ALLOWLIST_EMAIL_PATH = "identity.email"
_ALLOWLIST_PHONE_PATH = "identity.phone"
# Values that must never leak into an unauthorized free-text field:
SENSITIVE_SECTION = "sensitive"

DEFAULT_MAX_LEN = 8000

# Scripts whose letters are classic Latin look-alikes. NFKC folds compatibility
# variants (fullwidth, ligatures); these blocks survive NFKC and are the residual
# confusable surface for our Latin-only (English/Italian) corpus. Cyrillic and
# Greek are the common two; Armenian, Cherokee and Coptic round out the other
# whole-script Latin look-alikes used in homoglyph attacks (each has letters that
# render identically to Latin a/o/A/B/... yet is a distinct script).
_CONFUSABLE_SCRIPTS = ("CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE", "COPTIC")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)[^)]*\)")
# A bare, scheme-less host: a chain of dot-separated labels ending in a letters-
# only TLD (`sub.example.com`). The letters-only TLD keeps numeric tails such as
# version strings (`3.12`, `v2.0`) from ever looking like a host.
_BARE_HOST = r"(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,}"
# The link cue that may follow a host: a path or a query string. Its PRESENCE is
# what promotes a bare host to a candidate outbound link (see _LINK_TOKEN_RE).
_LINK_CUE_PATH = r"[/?][^\s<>()\[\]\"'.,;:!?]*"

# A candidate outbound link, SCHEME-INDEPENDENT but CUE-GATED for bare hosts.
# Three clickable forms are matched:
#   1. a schemed or protocol-relative URL (`http(s)://host...` or `//host...`);
#   2. a `www.`-prefixed host (the `www.` is itself the link cue), path optional;
#   3. a bare host FOLLOWED BY a path/query cue (`host.tld/...` or `host.tld?...`).
# A bare `token.ext` with NO scheme, NO `www.` and NO path/query (e.g. `Node.js`,
# `asp.net`, `main.py`, `resume.pdf`) is NOT a link: the cover-letter body is
# rendered LaTeX-escaped as plain text (see engine/artifacts.py `_latex_body`;
# `\url{}` wraps only the trusted posting URL, never model output), so a bare host
# is inert, non-clickable text and carries no working-link risk. Markdown hrefs
# `[...](href)` are extracted separately (see _candidate_link_hosts) and stay
# scheme- AND cue-independent. The leading lookbehind stops a bare-host match from
# starting inside an email local/domain, a longer URL, or a mid-word token.
_LINK_TOKEN_RE = re.compile(
    (
        r"(?<![\w@/.\-])(?:"
        r"(?:https?:)?//[^\s<>()\[\]\"']+"
        r"|www\.{host}(?:{cue})?"
        r"|{host}{cue}"
        r")"
    ).format(host=_BARE_HOST, cue=_LINK_CUE_PATH),
    re.IGNORECASE,
)
_HOST_RE = re.compile(r"(?:https?://)?([a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9\-]+)+)", re.IGNORECASE)
_B64_RE = re.compile(r"[A-Za-z0-9+/]{20,}={0,2}")
# any `<tag`, closing tag, HTML entity-ish script, javascript: or on*= handler
_HTML_RE = re.compile(r"<\s*/?[a-zA-Z][a-zA-Z0-9]*|javascript:|data:text/html|on\w+\s*=", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    """A single fail-closed finding. `field` is None for structural findings."""
    code: str
    field: str | None
    detail: str


@dataclass
class ValidationResult:
    ok: bool
    violations: list[Violation] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok

    @property
    def codes(self) -> set[str]:
        return {v.code for v in self.violations}


# --------------------------------------------------------------------------- #
# Canonicalization (identity byte-equality after normalization).
# --------------------------------------------------------------------------- #

def _strip_invisible(s: str) -> str:
    """Remove invisible/format/control characters so canonical comparison is not
    defeated by hidden padding (their PRESENCE is flagged separately)."""
    return "".join(ch for ch in s if not _is_invisible(ch))


def _is_invisible(ch: str) -> bool:
    cat = unicodedata.category(ch)
    if cat == "Cf" or cat == "Co":            # format (zero-width, bidi, tags), private-use
        return True
    if cat == "Cc" and ch not in "\t\n\r":    # control, minus ordinary whitespace
        return True
    return False


def canon_email(s: str) -> str:
    return unicodedata.normalize("NFKC", _strip_invisible(str(s))).strip().casefold()


def canon_name(s: str) -> str:
    text = unicodedata.normalize("NFKC", _strip_invisible(str(s))).strip()
    return re.sub(r"\s+", " ", text).casefold()


canon_address = canon_name


def canon_phone(s: str) -> str:
    stripped = _strip_invisible(str(s)).strip()
    digits = re.sub(r"\D", "", stripped)
    return ("+" if stripped.startswith("+") else "") + digits


def canon_url(s: str) -> str:
    text = unicodedata.normalize("NFKC", _strip_invisible(str(s))).strip()
    raw = text if ("://" in text or text.startswith("//")) else "//" + text
    try:
        parts = urlsplit(raw)
    except ValueError:
        return text.casefold()
    host = _strip_www((parts.hostname or "").lower())
    path = parts.path.rstrip("/")
    return f"{host}{path}".casefold()


def _canon_enum(v) -> str:
    return unicodedata.normalize("NFKC", _strip_invisible(str(v))).strip().casefold()


_CANON = {
    "email": canon_email,
    "phone": canon_phone,
    "name": canon_name,
    "address": canon_address,
    "url": canon_url,
}


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


# --------------------------------------------------------------------------- #
# Universal per-field threat scans (invisible chars, homoglyphs, HTML).
# --------------------------------------------------------------------------- #

def _flag_invisible(key: str, text: str, out: list[Violation]) -> None:
    bad = [ch for ch in text if _is_invisible(ch)]
    if bad:
        points = ", ".join(f"U+{ord(ch):04X}" for ch in dict.fromkeys(bad))
        out.append(Violation("invisible_char", key,
                             f"invisible/hidden character(s) present: {points}"))


def _flag_homoglyph(key: str, text: str, out: list[Violation]) -> None:
    bad = _homoglyph_chars(text)
    if bad:
        points = ", ".join(f"U+{ord(ch):04X}" for ch in dict.fromkeys(bad))
        out.append(Violation("homoglyph", key,
                             f"confusable non-Latin letter(s) present: {points}"))


def _homoglyph_chars(s: str) -> list[str]:
    out: list[str] = []
    for ch in s:
        if ch.isalpha() and ord(ch) > 0x7F:
            try:
                name = unicodedata.name(ch)
            except ValueError:
                continue
            if name.split(" ", 1)[0] in _CONFUSABLE_SCRIPTS:
                out.append(ch)
    return out


def _flag_html(key: str, text: str, out: list[Violation]) -> None:
    if _HTML_RE.search(text):
        out.append(Violation("html_or_script", key, "HTML/script markup present"))


# --------------------------------------------------------------------------- #
# Free-text-only scans (allowlist, exfil, base64, markdown host mismatch).
# --------------------------------------------------------------------------- #

def _first_host_in(text: str) -> str:
    m = _HOST_RE.search(text)
    return _strip_www(m.group(1).lower()) if m else ""


def _host(url: str) -> str:
    u = url.strip()
    if "://" not in u and not u.startswith("//"):
        u = "//" + u
    try:
        netloc = urlsplit(u).hostname or ""
    except ValueError:
        return ""
    return _strip_www(netloc.lower())


def _markdown_host_mismatches(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for m in _MD_LINK_RE.finditer(text):
        visible, href = m.group(1), m.group(2)
        href_host = _host(href)
        if not href_host:
            continue
        visible_host = _first_host_in(visible)
        if visible_host and visible_host != href_host:
            out.append((visible, href))
    return out


def _candidate_link_hosts(text: str) -> list[tuple[str, str]]:
    """Every candidate outbound link in free text as `(raw_token, host)`, keyed by
    HOST and independent of scheme. Covers markdown link hrefs `[...](HREF)`, plus
    every schemed / protocol-relative / bare-host token in the prose. The host is
    resolved past any userinfo (`good@evil` -> `evil`), so a non-allowlisted host
    is caught whether the link carries `http(s)://`, `//`, or nothing at all.
    De-duplicated by host so a host appearing several times is reported once."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        host = _host(raw)
        if host and host not in seen:
            seen.add(host)
            out.append((raw, host))

    for m in _MD_LINK_RE.finditer(text):
        _add(m.group(2))
    for m in _LINK_TOKEN_RE.finditer(text):
        _add(m.group().rstrip(".,;:!?"))
    return out


def _base64_blobs(text: str) -> list[str]:
    return [m.group() for m in _B64_RE.finditer(text) if _looks_base64(m.group())]


def _looks_base64(token: str) -> bool:
    core = token.rstrip("=")
    if len(core) < 20:
        return False
    has_digit = any(c.isdigit() for c in core)
    has_upper = any(c.isupper() for c in core)
    has_lower = any(c.islower() for c in core)
    special = "+" in core or "/" in core or token.endswith("=")
    if not (special or (has_digit and has_upper and has_lower)):
        return False
    try:
        raw = base64.b64decode(core + "=" * (-len(core) % 4), validate=True)
    except (ValueError, Exception):  # noqa: BLE001 - any decode error = not a clean blob
        return False
    return len(raw) >= 12


def _extract(pattern: re.Pattern, text: str) -> list[str]:
    return [m.group().strip().rstrip(".,;:!?") for m in pattern.finditer(text)]


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", _strip_invisible(str(s))).casefold()


def _contains_value(text: str, sensitive_value: str) -> bool:
    sval = str(sensitive_value).strip()
    if len(sval) < 3:
        return False
    if _norm(sval) in _norm(text):
        return True
    digits = re.sub(r"\D", "", sval)
    return len(digits) >= 4 and digits in re.sub(r"\D", "", text)


# --------------------------------------------------------------------------- #
# SSOT-derived policy (allowlist + sensitive set).
# --------------------------------------------------------------------------- #

def _build_allowlist(ssot: SSOT) -> dict[str, set[str]]:
    # HOST allowlist (not full-URL): a candidate link is judged by its host so the
    # gate is scheme-independent and path-independent. The subdomain trick stays
    # blocked because membership is EXACT host equality, never a suffix match.
    url_hosts: set[str] = set()
    links = ssot.get(_ALLOWLIST_LINK_SECTION)
    if isinstance(links, dict):
        values = links.values()
    elif isinstance(links, (list, tuple)):
        values = links
    else:
        values = []
    for v in values:
        if isinstance(v, str) and v.strip():
            host = _host(v)
            if host:
                url_hosts.add(host)

    emails: set[str] = set()
    email = ssot.get(_ALLOWLIST_EMAIL_PATH)
    if isinstance(email, str) and email.strip():
        emails.add(canon_email(email))

    phones: set[str] = set()
    phone = ssot.get(_ALLOWLIST_PHONE_PATH)
    if isinstance(phone, str) and phone.strip():
        phones.add(canon_phone(phone))

    return {"url_hosts": url_hosts, "emails": emails, "phones": phones}


def _build_sensitive(ssot: SSOT) -> dict[str, str]:
    section = ssot.get(SENSITIVE_SECTION)
    if not isinstance(section, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in section.items():
        if v is not None and str(v).strip():
            out[f"{SENSITIVE_SECTION}.{k}"] = str(v)
    return out


# --------------------------------------------------------------------------- #
# Per-class field checks.
# --------------------------------------------------------------------------- #

def _check_identity(key: str, value, spec: dict, ssot: SSOT, out: list[Violation]) -> None:
    text = str(value)
    _flag_invisible(key, text, out)
    _flag_homoglyph(key, text, out)
    _flag_html(key, text, out)

    kind = spec.get("identity_kind")
    canon = _CANON.get(kind)
    if canon is None:
        out.append(Violation("schema_error", key, f"unknown identity_kind {kind!r}"))
        return
    dotted = spec.get("ssot")
    ssot_value = ssot.get(dotted) if dotted else MISSING
    if ssot_value is MISSING:
        out.append(Violation("identity_missing_ssot", key,
                             f"SSOT has no {dotted!r} to verify against (fail-closed)"))
        return
    if canon(text) != canon(str(ssot_value)):
        out.append(Violation("identity_mismatch", key,
                             f"{kind} value does not byte-equal SSOT {dotted!r} after canonicalization"))


def _check_enum(key: str, value, spec: dict, ssot: SSOT, out: list[Violation]) -> None:
    text = str(value)
    _flag_invisible(key, text, out)
    _flag_homoglyph(key, text, out)
    _flag_html(key, text, out)

    dotted = spec.get("ssot_answers")
    allowed = ssot.get(dotted) if dotted else MISSING
    if allowed is MISSING:
        out.append(Violation("enum_missing_ssot", key,
                             f"SSOT has no canned-answer set {dotted!r} (fail-closed)"))
        return
    members = allowed if isinstance(allowed, (list, tuple, set)) else [allowed]
    allowed_set = {_canon_enum(a) for a in members}
    if _canon_enum(value) not in allowed_set:
        out.append(Violation("enum_not_member", key,
                             f"answer {value!r} is not in the SSOT canned-answer set for {dotted!r}"))


def _check_free_text(key: str, value, spec: dict, ssot: SSOT,
                     allowlist: dict[str, set[str]], sensitive: dict[str, str],
                     out: list[Violation]) -> None:
    text = str(value)

    max_len = spec.get("max_len", DEFAULT_MAX_LEN)
    if len(text) > max_len:
        out.append(Violation("length_exceeded", key,
                             f"length {len(text)} exceeds bound {max_len}"))

    _flag_invisible(key, text, out)
    _flag_homoglyph(key, text, out)
    _flag_html(key, text, out)

    for blob in _base64_blobs(text):
        out.append(Violation("base64_blob", key, f"base64-like blob present: {blob[:32]}..."))

    for visible, href in _markdown_host_mismatches(text):
        out.append(Violation("markdown_link_host_mismatch", key,
                             f"visible-text host differs from href host: {visible!r} -> {href!r}"))

    for raw, host in _candidate_link_hosts(text):
        if host not in allowlist["url_hosts"]:
            out.append(Violation("disallowed_url", key,
                                 f"link host not on SSOT allowlist: {host} (in {raw!r})"))
    for email in _extract(_EMAIL_RE, text):
        if canon_email(email) not in allowlist["emails"]:
            out.append(Violation("disallowed_email", key, f"email not on SSOT allowlist: {email}"))
    for phone in _extract(_PHONE_RE, text):
        if canon_phone(phone) not in allowlist["phones"]:
            out.append(Violation("disallowed_phone", key, f"phone not on SSOT allowlist: {phone}"))

    authorized = set(spec.get("authorized_sensitive", []))
    for path, sval in sensitive.items():
        if path in authorized:
            continue
        if _contains_value(text, sval):
            out.append(Violation("cross_field_exfil", key,
                                 f"SSOT sensitive value {path!r} present in a field not authorized to carry it"))


# --------------------------------------------------------------------------- #
# L1 entry point.
# --------------------------------------------------------------------------- #

def run_l1(generated_output: dict, ssot: SSOT, field_schema: dict) -> ValidationResult:
    """Run every L1 check. FAIL-CLOSED: any violation, unverifiable SSOT
    reference, or internal error -> ok=False."""
    violations: list[Violation] = []
    try:
        allowlist = _build_allowlist(ssot)
        sensitive = _build_sensitive(ssot)
        for key, value in generated_output.items():
            spec = field_schema.get(key)
            if spec is None:
                violations.append(Violation("unknown_field", key,
                                            "field is not a subset of the schema"))
                continue
            cls = spec.get("class")
            if cls == "identity":
                _check_identity(key, value, spec, ssot, violations)
            elif cls == "enum":
                _check_enum(key, value, spec, ssot, violations)
            elif cls == "free_text":
                _check_free_text(key, value, spec, ssot, allowlist, sensitive, violations)
            else:
                violations.append(Violation("unknown_class", key,
                                            f"unknown field class {cls!r} (fail-closed)"))
    except Exception as exc:  # noqa: BLE001 - fail-closed on ANY internal error
        violations.append(Violation("validator_error", None,
                                    f"{type(exc).__name__}: {exc}"))
    return ValidationResult(ok=not violations, violations=violations)
