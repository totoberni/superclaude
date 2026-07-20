"""Vendor-agnostic resolve engine: coverage classification + fill-value render.

The kernel's deterministic (no-LLM) decision core, moved verbatim from
`engine.fieldmap` (the generic `coverage`/`_classify_field` cluster) and
`engine.fill` (the `resolve_values` closure). Two responsibilities:

1. COVERAGE -- classify every required field as answerable / missing:<path-guess>
   / manual-only, by keyword-matching the SSOT buckets (`coverage`,
   `_classify_field`, `_answerable_path`, `_manual_only_reason`).
2. RESOLVE -- render each field to a concrete fill value by type (`resolve_values`
   and its render/select/boolean/upload helpers), and compute a fill report's
   completeness denominator (`_completeness`).

VENDOR-WIDGET INJECTION SEAM (W5.1 spec 3.4): the kernel carries NO vendor
portal-widget knowledge. Greenhouse's location-autocomplete, paste-in
resume/cover-letter textareas, and longitude/latitude telemetry are reconnected
through a duck-typed `vendor_resolver` (methods `location_path`, `key_text_path`,
`manual_reason`, `hidden_widget`). The default `_NOOP_RESOLVER` is a vendor with
no quirks; the Greenhouse behaviour lives in
`engine.providers.greenhouse.resolve.GREENHOUSE_WIDGET_RESOLVER`. It is injected
by the live callers: `engine.providers.greenhouse.fill` passes it into
`resolve_values` / `_completeness`, the pipeline (`engine.run`) builds it PER
vendor from the registry and passes it into `coverage`, and the test harness
passes an explicit `vendor_resolver`.

Layering: imports only stdlib + `engine.kernel.*`.
Nothing from `engine.fieldmap` / `engine.fill` / `engine.providers` / pipeline
ever enters here -- enforced by the kernel-layering invariant test with NO
allowlist entry for this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.kernel.contracts import (
    Field,
    FieldMap,
    FieldValue,
    FillAssets,
    ResolvedValues,
    Section,
)
from engine.kernel.fill_toolkit import _is_upload_field
from engine.kernel.ssot import MISSING, SSOT


# -- vendor-widget injection seam (spec 3.4) -----------------------------------

class _NoopVendorResolver:
    """Default vendor_resolver: a vendor with no portal-widget quirks."""
    def location_path(self, fld, ssot): return None
    def key_text_path(self, fld, ssot): return None
    def manual_reason(self, fld): return ""
    def hidden_widget(self, fld): return False


_NOOP_RESOLVER = _NoopVendorResolver()


# ============================================================================ #
# Generic coverage classification (moved from engine.fieldmap).
# ============================================================================ #

# The three classification verdicts a required field can receive.
ANSWERABLE = "answerable"
MISSING_STATUS = "missing"
MANUAL_ONLY = "manual-only"

# Sections that are always declinable and never block a fill/coverage run.
_DECLINE_SECTIONS = frozenset({
    Section.COMPLIANCE_EEOC, Section.DEMOGRAPHIC, Section.VOLUNTARY,
})

# Label keywords that mark a field as EEO/demographic no matter which section it
# arrived in (defence in depth on top of the source tag).
_DEMOGRAPHIC_KEYWORDS = (
    "gender", "race", "ethnic", "veteran", "disability", "disabilities",
    "sexual orientation", "hispanic", "latino", "self-identification",
    "self identification",
)

# A required "how much X experience do you have" question is answerable
# in principle: the SSOT's skills bucket can decide yes/no for any named
# technology, even if the honest answer is "no" (answerability is about
# whether the SSOT can decide, not about the polarity of the answer).
_SKILLS_EXPERIENCE_RE = re.compile(r"experience\s+(?:using|with|in)\b",
                                  re.IGNORECASE)

# First/last name label keywords, shared with `engine.fill` (imported there) so
# the full-name-split fallback at render time detects the same fields these
# matchers do.
_FIRST_NAME_KEYWORDS = ("first name", "given name", "forename")
_LAST_NAME_KEYWORDS = ("last name", "surname", "family name")

# Ordered label-keyword -> candidate SSOT dotted paths. First matcher whose any
# keyword is a substring of the (lowercased) label wins; within it the first
# candidate path that resolves in the SSOT makes the field answerable. The
# discrete first_name/last_name key leads each list so a form with BOTH a First
# Name and a Last Name field never has the full name typed into both; the
# full-name paths remain as a fallback (split at render time in engine.fill)
# for an SSOT that only carries a combined name. Order is load-bearing: the
# country-of-residence matcher MUST precede the generic current-location
# matcher (below) so a "country of residence" question resolves the discrete
# `identity.country` rather than the full postal address, which matches no
# country-name option.
_ANSWER_MATCHERS: list[tuple[tuple[str, ...], list[str]]] = [
    (_FIRST_NAME_KEYWORDS,
     ["identity.first_name", "identity.name", "identity.full_name"]),
    (_LAST_NAME_KEYWORDS,
     ["identity.last_name", "identity.name", "identity.full_name"]),
    (("full name", "legal name", "your name"),
     ["identity.name", "identity.full_name"]),
    (("email", "e-mail"), ["identity.email"]),
    (("phone", "mobile number", "telephone"),
     ["identity.phone", "canned_answers.phone"]),
    (("linkedin",), ["links.linkedin", "canned_answers.linkedin"]),
    (("github",), ["links.github"]),
    (("portfolio", "personal website", "personal site", "web site", "website"),
     ["links.site", "links.website", "links.portfolio"]),
    # KEYWORD DISCIPLINE (RS-h): every keyword must be DOMAIN-ANCHORED, never a
    # bare generic adjective/verb that also occurs in an unrelated question. A
    # bare token that matches a foreign-domain free-text field fills it from a
    # domain-mismatched scalar (a WRONG FILL, worse than a skip). The bare verb
    # "notice" is dropped for notice-DOMAIN phrases: "1 month" must not land in
    # a "please take notice ..." box.
    (("notice period", "period of notice", "notice required",
      "much notice", "weeks notice", "weeks of notice", "months notice",
      "months of notice", "give notice", "notice to give"),
     ["canned_answers.notice_period"]),
    (("employment agreement", "post-employment", "post employment",
      "non-compete", "noncompete", "restrictive covenant"),
     ["canned_answers.post_employment_restrictions"]),
    (("previously worked", "previously consulted", "worked at or consulted",
      "previously employed at", "previously interned"),
     ["canned_answers.previously_worked_at_company",
      "canned_answers.previously_applied_default"]),
    # RS-a: the region-keyed owner policy leads, so a sponsorship SELECT derives
    # its value from the POSTING country (via `_render_sponsorship_by_region`)
    # rather than inheriting a US-specific canned seed on a non-US posting. It is
    # PREFERRED over the work_authorization.<region>.sponsorship_required
    # structured fact (profile_map.py:70): the two are cross-checked and the
    # explicit policy wins on disagreement. The canned scalars stay the fallback
    # for an SSOT that carries no region policy.
    (("sponsorship", "sponsor", "visa"),
     ["policies.sponsorship_by_region",
      "canned_answers.sponsorship_answer_by_region",
      "canned_answers.visa_sponsorship_required",
      "canned_answers.us_visa_sponsorship_required"]),
    (("authorized to work", "authorised to work", "right to work",
      "eligible to work", "work authorization", "work authorisation",
      "legally authorized", "legally authorised", "work permit"),
     ["work_authorization", "canned_answers.work_authorization"]),
    (("relocat",),
     ["canned_answers.relocation", "canned_answers.willing_to_relocate"]),
    # The bare adjective "expected" is dropped (RS-h live defect: it matched a
    # degree question's "expected result" and filled the compensation scalar
    # "EUR 26,000 gross annual (RAL)" into a FREE-TEXT box). A compensation match
    # now requires a compensation NOUN (salary / compensation / remuneration) or
    # an explicit pay phrase, never a generic adjective. "What are your
    # compensation expectations for the role?" still hits on "compensation".
    (("salary", "compensation", "remuneration", "pay expectation",
      "expected pay"),
     ["preferences.comp_floor", "canned_answers.salary_expectation"]),
    (("country of residence", "current country", "country you reside",
      "country you are located"),
     ["identity.country"]),
    (("currently located in",
      "where are you currently located", "where are you located",
      "current location", "location"),
     ["identity.current_location", "identity.address", "identity.country"]),
    # A consent/privacy SELECT is ANSWERABLE (reaches `_render_select` -> the RS-g
    # `_consent_select_option` single-affirmative pick) whenever EITHER consent
    # source datum is seeded. `privacy_consent_default` is the live SSOT key
    # (`optional_consents` is the synthetic-fixture key and is ABSENT live), so
    # without it the live greenhouse `question_37455721` privacy select classified
    # MISSING, never reached RS-g, and fell to the overlay's "no option match".
    # Both are `_CONSENT_SOURCE_PATHS`. An AI-policy attestation that also matches
    # here ("I agree ... AI ...") is failed closed inside `_consent_select_option`.
    (("please confirm", "privacy policy", "consent to", "i agree"),
     ["canned_answers.optional_consents",
      "canned_answers.privacy_consent_default"]),
    (("accommodation", "accommodations", "accessible and inclusive",
      "reasonable adjustment", "accessibility need"),
     ["canned_answers.accommodations"]),
    (("name",), ["identity.name", "identity.full_name"]),
]


@dataclass
class FieldCoverage:
    key: str
    label: str
    status: str          # answerable | missing | manual-only
    path: str            # answerable: resolving path; missing: dotted-path guess
    reason: str = ""     # manual-only: why (file-upload | demographic/EEO)

    def classification(self) -> str:
        """The compact verdict string (`missing:` carries the guessed path)."""
        if self.status == MISSING_STATUS:
            return f"{MISSING_STATUS}:{self.path}"
        return self.status


@dataclass
class CoverageReport:
    vendor: str
    posting_id: str
    fields: list[FieldCoverage]

    @property
    def answerable(self) -> int:
        return sum(1 for f in self.fields if f.status == ANSWERABLE)

    @property
    def missing(self) -> int:
        return sum(1 for f in self.fields if f.status == MISSING_STATUS)

    @property
    def manual_only(self) -> int:
        return sum(1 for f in self.fields if f.status == MANUAL_ONLY)

    @property
    def required_total(self) -> int:
        return len(self.fields)

    def missing_paths(self) -> list[str]:
        """Dotted-path guesses for every unanswerable required field (feeds 7.6)."""
        return [f.path for f in self.fields if f.status == MISSING_STATUS]

    def summary_line(self) -> str:
        return (f"{self.answerable} answerable, {self.missing} missing, "
                f"{self.manual_only} manual-only of {self.required_total} required")


def coverage(fieldmap: FieldMap, ssot: SSOT, profile: dict,
             vendor_resolver=None) -> CoverageReport:
    """Classify every REQUIRED field of `fieldmap` against the SSOT + profile.

    Deterministic, no LLM. Order per field: manual-only (file upload or
    EEO/demographic, never auto-answered) wins first; then a keyword match
    against the SSOT buckets makes it answerable; otherwise it is missing and
    gets a dotted-path guess (canned_answers.<slug> for an unrecognised
    question) that a questionnaire item can later resolve.

    `vendor_resolver` (spec 3.4) reconnects any vendor portal-widget quirks; it
    is resolved to `_NOOP_RESOLVER` (no quirks) once and threaded down. The
    Greenhouse behaviour is injected by the caller (the pipeline builds
    `GREENHOUSE_WIDGET_RESOLVER` from the registry per vendor; the test harness
    passes it explicitly).
    """
    resolver = vendor_resolver if vendor_resolver is not None else _NOOP_RESOLVER
    profile = profile or {}
    results: list[FieldCoverage] = []
    for fld in fieldmap.required_fields():
        results.append(_classify_field(fld, ssot, profile, resolver))
    return CoverageReport(vendor=fieldmap.vendor,
                          posting_id=fieldmap.posting_id, fields=results)


def _classify_field(fld: Field, ssot: SSOT, profile: dict,
                    vendor_resolver=_NOOP_RESOLVER) -> FieldCoverage:
    reason = _manual_only_reason(fld, vendor_resolver)
    if reason:
        return FieldCoverage(fld.key, fld.label, MANUAL_ONLY, "", reason)
    path = _answerable_path(fld, ssot, profile, vendor_resolver)
    if path is not None:
        return FieldCoverage(fld.key, fld.label, ANSWERABLE, path)
    return FieldCoverage(fld.key, fld.label, MISSING_STATUS,
                         _missing_path_guess(fld.label))


def _manual_only_reason(fld: Field, vendor_resolver=_NOOP_RESOLVER) -> str:
    """"file-upload" ONLY for a genuine file control: a native file type, or a
    label carrying an explicit upload/attach verb (mirrors `engine.fill`'s
    `_is_upload_field`). A bare "resume"/"cv" label keyword is NOT enough --
    Greenhouse's paste-in `resume_text`/`cover_letter_text` textareas share
    their label with the sibling file-upload field ("Resume"/"Resume/CV"),
    so tagging on the label alone would wrongly classify a fillable free-text
    field as manual-only file-upload (never resolved, never fillable).

    A vendor portal-widget manual reason (e.g. Greenhouse's longitude/latitude
    "portal-widget") is asked LAST, via the injected `vendor_resolver`."""
    if "file" in fld.type.lower():
        return "file-upload"
    label = fld.label.lower()
    if any(word in label for word in ("upload", "attach")):
        return "file-upload"
    if fld.source in ("demographic", "eeo", "eeoc", "compliance"):
        return "demographic/EEO"
    if any(word in label for word in _DEMOGRAPHIC_KEYWORDS):
        return "demographic/EEO"
    reason = vendor_resolver.manual_reason(fld)
    if reason:
        return reason
    return ""


def _answerable_path(fld: Field, ssot: SSOT, profile: dict,
                     vendor_resolver=_NOOP_RESOLVER) -> str | None:
    location_path = vendor_resolver.location_path(fld, ssot)
    if location_path is not None:
        return location_path
    key_text_path = vendor_resolver.key_text_path(fld, ssot)
    if key_text_path is not None:
        return key_text_path
    # F.1 EXACT-SLUG PRECEDENCE (the G6 trap): a question whose EXACT canned slug
    # is seeded is answered from that owner datum BEFORE any keyword-table guess.
    # Exact data beats heuristic -- a keyword tuple ("sponsor", the in-person
    # phrasing) can otherwise fire on a label whose owner-seeded answer says the
    # opposite (live: greenhouse question_37393963 was keyword-derived "No" while
    # the exact slug the owner later seeded says "Yes"). The keyword tables below
    # stay the fallback for every UNseeded label; they still legitimately serve a
    # label whose exact slug is absent.
    exact_slug = _missing_path_guess(fld.label)
    if ssot.get(exact_slug) is not MISSING:
        return exact_slug
    low = fld.label.lower()
    if _SKILLS_EXPERIENCE_RE.search(low) and ssot.get("skills") is not MISSING:
        return "skills"
    for keywords, candidates in _ANSWER_MATCHERS:
        if not any(keyword in low for keyword in keywords):
            continue
        for path in candidates:
            if ssot.get(path) is not MISSING:
                return path
        if _profile_answers_work_auth(candidates, profile):
            return "profile.capabilities"
        return None
    return None


def _profile_answers_work_auth(candidates: list[str], profile: dict) -> bool:
    """Work-authorization questions may be answered from a profile capability
    (e.g. work_authorization_eu) even when the raw SSOT string is absent."""
    if "work_authorization" not in candidates:
        return False
    caps = profile.get("capabilities") or []
    return any(str(cap).startswith("work_authorization") for cap in caps)


def _missing_path_guess(label: str) -> str:
    """A free-form unrecognised question is answered from canned_answers (7.6)."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    slug = slug or "unlabelled"
    return f"canned_answers.{slug}"


# ============================================================================ #
# Deterministic value resolution (moved from engine.fill).
# ============================================================================ #

# Canned-answer paths (checked in order) that ratify consent: the first that
# resolves to a non-negative value gates every consent/confirmation checkbox to
# True. The real SSOT keys this as `privacy_consent_default`; the synthetic v1.4
# fixture keys it as `optional_consents`, so both are consulted.
_CONSENT_SOURCE_PATHS = (
    "canned_answers.privacy_consent_default",
    "canned_answers.optional_consents",
)

# Field types that render as an option choice rather than free text.
_SELECT_TYPES = frozenset({
    "multi_value_single_select", "multi_value_multi_select", "yes_no",
})

# -- checkbox intent classifiers (RS-g consent classes) ------------------------
# Each consent-class checkbox is dispositioned by an OWNER POLICY seeded under
# `policies.consent.<class>` in the SSOT, NOT by a hard-coded YES/NO. A label is
# first sorted into ONE class (`_classify_checkbox`), then that class's policy
# verdict decides the fill:
#   assertion          -> policies.consent.assertion    (truthful_only: tick ONLY
#                         if the SSOT proves the factual claim true; never a false
#                         check -- owner ruling 2026-07-19)
#   assessment         -> policies.consent.assessment   (consent -> tick)
#   talent_pool        -> policies.consent.talent_pool  (decline -> never tick;
#                         RETIRES the superseded talent-pool YES split)
#   marketing          -> policies.consent.marketing    (decline -> never tick)
#   application_privacy -> policies.consent.application_privacy (consent -> tick;
#                         the ONLY class auto-consented when application-necessary)
# Classification order below is assertion -> assessment -> talent_pool ->
# marketing -> application_privacy, so a factual/assessment/marketing ask that
# also says "I agree" is never mis-sorted into legal privacy consent.
# BACKWARD COMPAT: when `policies.consent` is ABSENT, only application_privacy
# auto-fills, and only when the legacy `_CONSENT_SOURCE_PATHS` ratify it;
# talent-pool/marketing/assessment/assertion never auto-fill (conservative).
_CONSENT_RE = re.compile(
    r"please confirm|privacy|consent|i agree|\bagree\b|\bterms\b|gdpr|"
    r"data processing|i acknowledge|i certify|i confirm", re.I)
# The future-contact / talent-pool opt-in. The `future\s+(?:\w+\s+){0,3}...`
# window catches the live lever wording "future job opportunities" (the
# "...consent to contact me about future job opportunities" box the first-pass
# census wrongly consented) as well as the bare "future opportunities"; a
# leading "consent" verb in that same box must NOT reroute it to legal privacy
# consent, which is exactly why talent_pool is classified BEFORE
# application_privacy.
_TALENT_POOL_RE = re.compile(
    r"talent (pool|community|network)|"
    r"future\s+(?:\w+\s+){0,3}(?:opportunit|role|position|job|vacan|opening)|"
    r"contact me about (?:future|other|new)|"
    r"keep .*on file|consider me for|stay in touch|keep me in mind|"
    r"other (roles|positions|opportunit)", re.I)
_MARKETING_RE = re.compile(
    r"marketing|newsletter|promotional|promotions|subscribe|mailing list|"
    r"updates and offers|product updates|latest news", re.I)
# An assessment/aptitude PARTICIPATION consent (W1: "20 minute aptitude
# assessment ... Are you happy to do this?").
_ASSESSMENT_RE = re.compile(r"\baptitude\b|\bassessment\b|\bskills? test\b", re.I)
# An affirmative acknowledgement option on a consent SELECT ("Acknowledge",
# "Confirm", "I agree") that no bare yes/no option match can reach (RS-g).
_AFFIRMATIVE_OPTION_RE = re.compile(
    r"acknowledg|\bconfirm\b|\bagree\b|\baccept\b|\byes\b|\bconsent\b", re.I)

# -- AI-policy attestation detector (SSOT, shared with bin/generate_answers.py) --
# An AI-policy question asks whether AI was used to WRITE THIS APPLICATION. It is
# the ONE thing the engine must never answer on the owner's behalf: its Yes/No
# polarity is not fixed across employers (Canonical's "I agree to use only my own
# words ... AI ... will disqualify" reads Yes = compliant, while "did you use
# AI?" reads Yes = used AI), so no seeded scalar can answer both honestly. The
# generator (`bin/generate_answers.py`) forbids it at write time; the fill-time
# consent paths below MUST fail closed on it too, because a consent label that
# also carries "I agree" would otherwise be auto-ticked once it becomes
# answerable. This predicate is the SINGLE SOURCE both layers consult (a mirrored
# copy in the generator would drift, exactly as `is_essay_question` delegates to
# `content.is_free_text`); `bin/generate_answers.py` re-imports it from here.
# The AI token is matched on WORD BOUNDARIES ("ai", not the "ai" inside "email").
_AI_TOKEN_RE = re.compile(
    r"\b(ai|a\.i\.|chatgpt|copilot|artificial intelligence|"
    r"large language models?|llms?|generative ai)\b")
_AI_POLICY_PHRASES = ("ai policy", "ai-use policy", "ai use policy",
                      "ai usage policy", "policy on ai", "policy on the use of ai",
                      "ai disclosure", "disclosure of ai")
_AI_APPLICATION_CONTEXT = (
    "this application", "this form", "this questionnaire",
    "this submission", "this response", "these responses",
    "your responses", "this answer", "these answers",
    "your answers", "this cover letter", "this essay",
    "write this", "writing this", "complete this",
    "completing this", "draft this", "drafting this",
    "prepare this", "preparing this", "answering these")


def is_ai_policy_question(label: str) -> bool:
    """True iff the label asks whether AI was used to WRITE THIS APPLICATION.

    Narrow ON PURPOSE (see `_AI_APPLICATION_CONTEXT`): a question about the owner's
    AI WORK ("your experience using large language models") is an essay, not a
    policy question, and it must reach the model rather than be answered with the
    authorship disclosure.
    """
    low = re.sub(r"\s+", " ", str(label or "").casefold())
    if any(phrase in low for phrase in _AI_POLICY_PHRASES):
        return True
    if not _AI_TOKEN_RE.search(low):
        return False
    return any(phrase in low for phrase in _AI_APPLICATION_CONTEXT)


# The skip reason a fill-time consent path records when it fails closed on an
# AI-policy attestation (never auto-answered; routed to human handoff / the
# content overlay's ToS-forbidden verdict instead).
_AI_ATTESTATION_SKIP_REASON = (
    "AI-policy attestation: never auto-answered (its Yes/No polarity varies per "
    "posting, so no seeded scalar answers it honestly; human handoff)")

# RS-g consent-class -> the owner policy dotted path that dispositions it. The
# policy value is one of "consent" | "decline" | "truthful_only" | "opt_out".
_CONSENT_POLICY_PATHS = {
    "assertion": "policies.consent.assertion",
    "assessment": "policies.consent.assessment",
    "talent_pool": "policies.consent.talent_pool",
    "marketing": "policies.consent.marketing",
    "application_privacy": "policies.consent.application_privacy",
}

# -- yes/no select intent + region coverage (criterion: yes/no selects) --------
# Right-to-work / sponsorship selects are answered by deriving an affirmative or
# negative from the SSOT work-authorization facts, then picking the matching
# Yes/No option. A posting whose label targets a region the SSOT does not cover
# (e.g. the United States) is region-ambiguous and is left honestly unfilled.
_SPONSOR_INTENT_RE = re.compile(r"sponsor|\bvisa\b", re.I)
_WORK_AUTH_INTENT_RE = re.compile(
    r"authori[sz]ed to work|authori[sz]ation to work|right to work|"
    r"eligible to work|legally (authori[sz]ed|entitled|permitted|able)|"
    r"work permit|work authori[sz]ation|permitted to work|able to work in|"
    r"do you have the right to work", re.I)
# What a SPONSORSHIP-intent CHECKBOX asserts about the candidate: that
# sponsorship IS required (`_SPONSOR_NEEDED_ASSERT_RE`) or that it is NOT
# (`_SPONSOR_NOT_NEEDED_ASSERT_RE`). The two have OPPOSITE polarity and the
# negated form is tested FIRST, since "will not require sponsorship" also
# contains the affirmative verb. A label matching NEITHER states no polarity
# this code can read, so the checkbox parks rather than guessing -- ticking a
# sponsorship box from EU right-to-work facts would assert the OPPOSITE of the
# truth on a legally significant question.
#
# Both forms are ANCHORED to the sponsorship noun (`_SPONSOR_NEED_TAIL`): the
# requirement verb must actually govern sponsorship or a visa, so a negated
# clause about something ELSE in a multi-sentence label ("I do not need
# relocation assistance. Will you require visa sponsorship?") cannot flip the
# polarity of the sponsorship claim and produce a false tick. Without this
# anchor that exact label reads "do not need" as "sponsorship not required" and
# ticks a box asserting the OPPOSITE of the truth. The negator window is
# likewise bounded, sized to the live "will you now or in the future require
# sponsorship" shape. Pinned by
# `test_multi_sentence_negated_clause_does_not_flip_sponsorship_polarity`.
_SPONSOR_NEED_TAIL = (r"(?:requir\w*|need\w*|request\w*)\s+"
                      r"(?:\w+\s+){0,3}?(?:visa|sponsor\w*)\b")
_SPONSOR_NOT_NEEDED_ASSERT_RE = re.compile(
    r"\b(?:do not|don'?t|does not|doesn'?t|did not|will not|won'?t|"
    r"would not|wouldn'?t|never|no|not)\s+(?:\w+\s+){0,5}?" + _SPONSOR_NEED_TAIL
    + r"|\bwithout\s+(?:\w+\s+){0,2}?sponsorship\b"
    + r"|\bno\s+(?:visa\s+)?sponsorship\b", re.I)
_SPONSOR_NEEDED_ASSERT_RE = re.compile(r"\b" + _SPONSOR_NEED_TAIL, re.I)
# H.1 (owner ruling 2026-07-20): two MORE grammatical shapes, so the detector
# reads requirement polarity BIDIRECTIONALLY rather than only the verb-then-noun
# requirement form above. Each stays anchored to the sponsorship/visa noun, so a
# requirement verb elsewhere in a multi-sentence label cannot flip the polarity.
#
# Noun-then-verb requirement ("sponsorship is required", "a visa is needed"): the
# noun is the SUBJECT a copula plus a requirement verb governs. The noun-to-copula
# gap is bounded and negator-free (a period ends it, and a "not"/"no" token fails
# it), so a negated or cross-sentence requirement reads None, never a false
# REQUIRED. A "not"/"no" BETWEEN the copula and the verb ("is not needed") also
# fails to match, since the copula must be followed directly by the requirement
# verb.
_SPONSOR_NEED_HEAD_RE = re.compile(
    r"\b(?:visa|sponsor\w*)\s+"
    r"(?:(?!\bnot\b|\bno\b|\bnever\b|\bwithout\b)\w+\s+){0,3}?"
    r"(?:is|are|was|were|be|been|being|will\s+be|would\s+be)\s+"
    r"(?:requir\w*|need\w*)\b", re.I)
# Noun-then-verb NEGATED requirement ("No visa is required.", "No work visa is
# required for this role."): a leading negator (no|not|never|without) governs the
# SAME visa/sponsorship noun the copula plus requirement verb governs, so the
# label asserts sponsorship is NOT required. Tested FIRST, before the affirmative
# `_SPONSOR_NEED_HEAD_RE`, which would otherwise match the bare "visa is required"
# tail and read the leading negator as REQUIRED -- the exact false-legal fill this
# shape prevents. Anchored like the affirmative head: the negator-to-noun and
# noun-to-copula windows are bounded and cannot cross a period, so a negator about
# a DIFFERENT clause ("No relocation is required. Will visa sponsorship be
# required?") never reaches the sponsorship noun and the genuine requirement still
# reads REQUIRED.
_SPONSOR_NOT_NEEDED_HEAD_RE = re.compile(
    r"\b(?:no|not|never|without)\s+"
    r"(?:\w+\s+){0,3}?(?:visa|sponsor\w*)\s+"
    r"(?:(?!\bnot\b|\bno\b|\bnever\b|\bwithout\b)\w+\s+){0,3}?"
    r"(?:is|are|was|were|be|been|being|will\s+be|would\s+be)\s+"
    r"(?:requir\w*|need\w*)\b", re.I)
# Possession/negation NOT-required. Affirmative possession of a work visa / permit
# / authorization is a NOT-required claim (the candidate already holds the right).
# The verb-to-object gap admits only determiners/adjectives, so an in-progress
# verb phrase like "have applied for a visa" (an unproven claim) does NOT read as
# possession.
_SPONSOR_HAVE_RE = re.compile(
    r"\b(?:i\s+)?(?:already\s+|currently\s+)?(?:have|hold|possess|carry)\s+"
    r"(?:(?:a|an|my|the|valid|current|full|permanent|existing|eu)\s+){0,3}?"
    r"(?:work\s+visa|work\s+permit|residence\s+permit|"
    r"work\s+authori[sz]ation|visa|permit)\b", re.I)
# A NEGATED possession ("do not have a visa", "without a work permit") SUPPRESSES
# the possession read: the candidate LACKS the right, which does not establish
# that no sponsorship is needed, so it fails closed to None rather than inverting.
_SPONSOR_NEG_POSSESS_RE = re.compile(
    r"\b(?:do not|don'?t|does not|doesn'?t|did not|will not|won'?t|never|"
    r"no longer|not|without|lack\w*)\s+"
    r"(?:\w+\s+){0,3}?(?:have|hold|possess|carry|visa|work\s+permit|"
    r"residence\s+permit|permit)\b", re.I)
_COVERED_REGION_RE = re.compile(
    r"\beu\b|\be\.u\.\b|european union|\beurope\b|\beea\b|ital", re.I)
_UNCOVERED_REGION_RE = re.compile(
    r"united states|\bu\.?s\.?a?\.?\b|\bamerica|\bcanad|"
    r"united kingdom|\bu\.?k\.?\b|\bbritain\b|\bengland\b|\baustralia\b|"
    r"\bindia\b|\bsingapore\b|\buae\b|\bdubai\b", re.I)
_YESNO_NEG_RE = re.compile(r"^\s*(no\b|n\b|not\b|none\b|false\b|nope\b)", re.I)
_YESNO_POS_RE = re.compile(r"^\s*(yes\b|y\b|true\b|yep\b|yeah\b)", re.I)

# A candidate-photo control: label reads like a portrait ask (English + Italian)
# or the field is an image-accepting file input (criterion 3).
_PHOTO_LABEL_RE = re.compile(
    r"photo|picture|headshot|profile image|foto|immagine", re.I)

# A cover-letter file-upload control: key or label names "cover letter" (also
# matches the underscore/hyphen key form cover_letter, cover-letter-text). A
# match here resolves to the dedicated `FillAssets.cover_letter` document
# asset when one is present, and must NEVER fall through to the CV-selection
# branch -- see the live-run bug where a `cover_letter` file field silently
# received `cv-ats.pdf`. With no cover-letter asset it is honestly skipped
# (there is nothing to upload). Deliberately narrow (requires "cover"
# immediately followed by "letter") so it never misfires on `resume` or
# `avatar`/`photo` keys that merely share a stray token.
_COVER_LETTER_RE = re.compile(r"cover[\s_-]*letter", re.I)

# Ordered (pattern-tuple -> extra_documents key) matchers for an upload field
# that names an OPTIONAL/required transcript or certification slot. Matched
# case-insensitively against the field label/key, in the style of
# `_ANSWER_MATCHERS`: the first tuple with any substring hit wins. Order is
# load-bearing -- the IB-transcript patterns MUST precede the generic transcript
# patterns so a "secondary school transcript" resolves the more specific
# `transcript_ib` rather than the university transcript. REQUIRED and optional
# attachment fields are treated identically (owner rule 2026-07-10: attach
# whenever we can). A generic "other attachments"-style label with NO named
# pattern is DELIBERATELY out of scope: a later vendor loop decides that policy
# through its escalation channel; this matcher only fires on an explicitly named
# document, never on a bare "attachments" catch-all. The "certificate"/"diploma"
# substrings are DELIBERATELY broad (owner rule: attach whenever a slot exists),
# so they can also hit an unrelated certificate slot (a background-check or
# right-to-work certificate). That is an ACCEPTED false-positive surface: it is
# bounded by the dry-run (nothing is ever submitted) and by fail-soft (a
# mis-attach uploads a real document, never crashes); a later vendor loop may
# narrow it if a real posting warrants.
_EXTRA_DOCUMENT_MATCHERS: list[tuple[tuple[str, ...], str]] = [
    (("ib transcript", "secondary school transcript",
      "high school transcript", "diploma transcript"), "transcript_ib"),
    (("transcript", "academic record", "academic transcript",
      "university transcript"), "transcript_university"),
    (("certification", "certificate", "grade letter",
      "degree certificate", "diploma"), "lse_certification"),
]


def resolve_values(fieldmap: FieldMap, ssot: SSOT, profile: dict, *,
                   assets: FillAssets | None = None,
                   posting_lang: str = "en",
                   vendor_resolver=None) -> ResolvedValues:
    """Classify + render every field of `fieldmap` into concrete fill values.

    File-upload fields resolve to a whitelisted asset (owner override): a
    candidate-photo field gets the profile photo, a cover-letter file field
    (key/label matching "cover letter", e.g. `cover_letter`) gets the
    dedicated cover-letter document asset when one is present in `FillAssets`
    -- it must NEVER receive the CV instead -- and is otherwise honestly
    SKIPPED (no cover-letter document asset), and every OTHER file field gets
    a CV picked by the owner-ratified structural rule (cv-ats when the form has
    a photo field, so the photo attaches separately; cv-atsi when it has none,
    so the ATSI variant embeds the photo -- posting-language independent). With
    no `assets` (the pre-override default) file fields keep the old
    "file-upload" skip, so the existing contract holds.

    A checkbox (boolean) is resolved by its label intent (`_resolve_boolean`): a
    consent/confirmation box ticks True when the SSOT ratifies consent, a
    talent-pool box ticks True, a marketing box is left unticked. Every other
    field reuses `_classify_field` (the SSOT coverage classifier):
    manual-only (EEO-demographic / portal widget) and missing (unanswerable)
    fields are SKIPPED with their classifier reason. An answerable field is
    rendered by type: free text from the resolved SSOT string, and an option
    label for a select (an exact case-insensitive option match, else a yes/no
    normalization for right-to-work / sponsorship questions, else skipped).
    Deterministic, no LLM; never writes the SSOT.

    `vendor_resolver` (spec 3.4) reconnects any vendor portal-widget quirks
    through `_classify_field`; it defaults to `_NOOP_RESOLVER`. The Greenhouse
    behaviour is injected by `engine.providers.greenhouse.fill`, which passes its
    `GREENHOUSE_WIDGET_RESOLVER` into this call.
    """
    resolver = vendor_resolver if vendor_resolver is not None else _NOOP_RESOLVER
    profile = profile or {}
    # POSTING context (RS-b/RS-d): the resolution profile carries the posting's
    # location under "posting_location" (w5_accept seeds it from the generated
    # document, the same value the content overlay's nearest-city policy reads).
    # Absent -> "" -> the two posting-aware resolvers park honestly.
    posting_location = str(profile.get("posting_location") or "")
    assets = assets.verified() if assets is not None else None
    resolved = ResolvedValues()
    has_photo_field = _form_has_photo_field(fieldmap)
    for fld in fieldmap.fields:
        if _is_upload_field(fld):
            _resolve_upload(fld, resolved, assets, has_photo_field)
            continue
        if (fld.type or "").lower() == "boolean":
            _resolve_boolean(fld, resolved, ssot, profile, resolver)
            continue
        classified = _classify_field(fld, ssot, profile, resolver)
        if classified.status == MANUAL_ONLY:
            resolved.skipped.append((fld.key, classified.reason or MANUAL_ONLY))
            continue
        if classified.status == MISSING_STATUS:
            resolved.skipped.append((fld.key, classified.classification()))
            continue
        value, skip_reason = _render_value(fld, classified.path, ssot,
                                           posting_location)
        if skip_reason is not None:
            resolved.skipped.append((fld.key, skip_reason))
            continue
        resolved.fields.append(FieldValue(
            key=fld.key, label=fld.label, type=fld.type,
            locator=fld.locator, value=value))
    return resolved


def _is_photo_field(fld) -> bool:
    """A candidate-image field: label matches the portrait pattern (EN + IT)
    (criterion 3). Only consulted for fields that are already upload fields,
    so a stray text match cannot trigger an upload.

    `Field` (engine.kernel.contracts) carries no `accept` MIME attribute, so an
    accept-sniffing branch would be dead in production; the label regex is the
    sole detection signal."""
    return bool(_PHOTO_LABEL_RE.search(fld.label or ""))


def _form_has_photo_field(fieldmap: FieldMap) -> bool:
    return any(_is_upload_field(f) and _is_photo_field(f) for f in fieldmap.fields)


def _is_cover_letter_field(fld) -> bool:
    """A cover-letter file-upload field: `_COVER_LETTER_RE` matches the key or
    the label. Only consulted for fields already classified as upload fields,
    so a stray text match elsewhere can never trigger this."""
    return bool(_COVER_LETTER_RE.search(fld.key or "")
                or _COVER_LETTER_RE.search(fld.label or ""))


def _match_extra_document(fld) -> str | None:
    """The `extra_documents` key an upload field's label/key names, or None.

    Case-insensitive substring match over BOTH the label and the key (like
    `_is_cover_letter_field`); the first `_EXTRA_DOCUMENT_MATCHERS` tuple with
    any hit wins. Consulted only for fields already classified as uploads and
    only AFTER the photo/cover-letter branches, so it never steals a
    CV/photo/cover-letter field.

    Word separators (whitespace, underscore, hyphen) in the label/key are folded
    to a single space before matching, so the multi-word patterns (which embed
    literal spaces) also fire on the snake_case/hyphenated KEY form: an
    `ib_transcript` key with no label still hits the "ib transcript" pattern and
    resolves `transcript_ib`, preserving the IB-beats-generic ordering lock even
    when a field is exposed by key alone (the same separator tolerance the
    cover-letter matcher applies)."""
    haystack = re.sub(
        r"[\s_-]+", " ", f"{fld.label or ''} {fld.key or ''}".lower())
    for patterns, key in _EXTRA_DOCUMENT_MATCHERS:
        if any(pattern in haystack for pattern in patterns):
            return key
    return None


# The cover-letter file field is skipped with this reason ONLY when
# `FillAssets.cover_letter` is absent: the field is optional and there is
# nothing to upload, so it is honestly skipped rather than silently
# receiving the CV (the live-confirmed bug this guards against). When a real
# cover-letter document asset IS present, `_resolve_upload` uploads it
# instead -- see the cover-letter branch below.
_COVER_LETTER_SKIP_REASON = (
    "optional cover-letter upload; no cover-letter document asset (cover "
    "letter is drafted per-posting in the manual flow)")


def _resolve_upload(fld, resolved: ResolvedValues, assets: FillAssets | None,
                    has_photo_field: bool) -> None:
    if assets is None:
        # Pre-override contract: no assets -> file fields are skipped, not filled.
        resolved.skipped.append((fld.key, "file-upload"))
        return
    if _is_photo_field(fld):
        asset_name, path, reason = ("photo", assets.photo,
                                    "candidate photo/portrait field")
    elif _is_cover_letter_field(fld):
        if assets.cover_letter is None:
            # Never resolve a cover-letter file field to the CV asset: with
            # no cover-letter document asset there is nothing to upload, and
            # the field is optional, so it is honestly skipped.
            resolved.skipped.append((fld.key, _COVER_LETTER_SKIP_REASON))
            return
        asset_name, path, reason = ("cover-letter", assets.cover_letter,
                                    "cover-letter document asset")
    else:
        extra_key = _match_extra_document(fld)
        if extra_key is not None and extra_key in assets.extra_documents:
            asset_name, path, reason = (
                extra_key, assets.extra_documents[extra_key],
                f"matched extra-document slot ({extra_key}); owner rule attaches "
                "it whenever the slot exists")
        else:
            # No named-document match, or the matched key is absent from the
            # assets: the field keeps its EXISTING behavior (a CV upload, or an
            # asset-missing skip when the CV is absent). Fail-soft, never a crash.
            asset_name, path, reason = _select_cv(assets, has_photo_field)
    if path is None:
        resolved.skipped.append((fld.key, f"asset missing: {asset_name}"))
        return
    resolved.fields.append(FieldValue(
        key=fld.key, label=fld.label, type=fld.type, locator=fld.locator,
        value=path, asset=asset_name, upload_reason=reason))


def _select_cv(assets: FillAssets, has_photo_field: bool):
    """The owner-ratified structural CV rule (2026-07-07): purely form-driven,
    posting-language INDEPENDENT. A form that HAS a dedicated photo/portrait
    field carries the real photo on that field, so the plain ATS CV is uploaded
    (cv-ats); a form with NO photo field has nowhere to carry the portrait, so
    the embedded-photo ATSI CV variant is uploaded (cv-atsi)."""
    if not has_photo_field:
        return ("cv-atsi", assets.cv_atsi,
                "no photo field on the form; embedding the photo via the ATSI "
                "CV variant")
    return ("cv-ats", assets.cv_ats,
            "photo field present; plain ATS CV, photo attached to the photo "
            "field")


# -- checkbox (boolean) resolution ---------------------------------------------

# RS-g consent-checkbox skip reasons.
_NON_CONSENT_CHECKBOX_REASON = "non-consent checkbox not auto-checked in dry run"
_MARKETING_SKIP_REASON = "marketing/newsletter checkbox left unticked"
_CONSENT_UNRATIFIED_REASON = (
    "consent checkbox not auto-ticked: SSOT carries no ratified consent answer")
_ASSERTION_UNPROVEN_REASON = (
    "assertion checkbox left unchecked (owner ruling 2026-07-19): checking it "
    "would claim a fact the SSOT does not prove true; a truthful_only assertion "
    "is never falsely checked")


def _resolve_boolean(fld, resolved: ResolvedValues, ssot: SSOT,
                     profile: dict, vendor_resolver=_NOOP_RESOLVER) -> None:
    """Resolve a checkbox by its label intent (RS-g consent classes).

    An EEO/demographic or file boolean stays manual-only (never auto-answered).
    An in-office-commitment boolean is DERIVED from the W4-COMMUTE-GATE policy
    (RS-d). Every other checkbox is sorted into a consent CLASS
    (`_classify_checkbox`) and dispositioned by that class's owner policy under
    `policies.consent.<class>` (`_consent_disposition`): application-privacy /
    assessment consent ticks True, talent-pool / marketing decline, an assertion
    ticks ONLY when the SSOT proves its factual claim true (owner ruling
    2026-07-19: never a false check). Absent a seeded policy, only
    application-necessary privacy consent auto-fills (from the legacy
    `_CONSENT_SOURCE_PATHS`); the superseded talent-pool YES is retired. A
    checkbox matching no class is left for a human."""
    classified = _classify_field(fld, ssot, profile, vendor_resolver)
    if classified.status == MANUAL_ONLY:
        resolved.skipped.append((fld.key, classified.reason or MANUAL_ONLY))
        return
    # RS-d: an in-office-commitment boolean is DERIVED from the W4-COMMUTE-GATE
    # policy plus the posting location, never truthiness-coerced from the prose
    # attendance answer (that PROSE reaching a boolean control is the live
    # "boolean question resolved to a non-boolean value" defect). Resolving it to
    # a real bool here also keeps the additive content overlay from re-routing it
    # to the prose scalar (the overlay only touches still-skipped fields).
    if _is_in_office_commitment(fld.label):
        value, skip = _resolve_in_office_boolean(fld, ssot, profile)
        if skip is not None:
            resolved.skipped.append((fld.key, skip))
        else:
            resolved.fields.append(_bool_field(fld, value))
        return
    if is_ai_policy_question(fld.label):
        # An AI-policy attestation checkbox ("... I agree to use only my own words
        # ... AI ... will disqualify") classifies as application_privacy via
        # "agree", but its polarity is a stance the owner did not make: fail closed
        # here exactly as the SELECT consent path does, never a silent tick.
        resolved.skipped.append((fld.key, _AI_ATTESTATION_SKIP_REASON))
        return
    kind = _classify_checkbox(fld.label)
    if kind is None:
        resolved.skipped.append((fld.key, _NON_CONSENT_CHECKBOX_REASON))
        return
    action, reason = _consent_disposition(kind, ssot)
    if action == "tick":
        resolved.fields.append(_bool_field(fld, True))
        return
    if action == "truthful_only":
        if _assertion_proven_true(fld, ssot, profile) is True:
            resolved.fields.append(_bool_field(fld, True))
        else:
            resolved.skipped.append((fld.key, _ASSERTION_UNPROVEN_REASON))
        return
    resolved.skipped.append((fld.key, reason))


def _bool_field(fld, value: bool) -> FieldValue:
    return FieldValue(key=fld.key, label=fld.label, type=fld.type,
                      locator=fld.locator, value=value)


def _classify_checkbox(label: str) -> str | None:
    """The RS-g consent class of a checkbox label, or None.

    One of "assertion" | "assessment" | "talent_pool" | "marketing" |
    "application_privacy" | None. Order is load-bearing: a factual work-auth
    ASSERTION and an ASSESSMENT-participation ask are sorted before the generic
    consent/marketing patterns, and marketing before application_privacy, so a
    box that also says "I agree" is never mis-sorted into legal privacy consent.
    A SPONSORSHIP assertion ("will you require sponsorship") is the same class of
    factual claim about the candidate, so it takes that same leading slot; it is
    recognised through `_select_intent`, the SAME classifier the yes/no select
    path uses (sponsorship tested before right-to-work), so the checkbox and
    select paths can never drift into two disagreeing notions of intent. Which
    TRUTH the assertion is checked against, and with which polarity, is
    `_assertion_proven_true`'s job -- the two intents are NOT interchangeable:
    EU rights answer Yes to authorization and No to sponsorship-required.

    H.1 (owner ruling 2026-07-20): the sponsorship-intent gate is RELAXED. A
    work-auth assertion always takes the assertion slot, and a SPONSORSHIP-intent
    label takes it only when `_sponsorship_assertion_polarity` reads a requirement
    polarity. A sponsorship-intent label with NO readable polarity is not a legal
    claim, so a genuine consent box that merely MENTIONS "visa" falls through to
    the consent branches and auto-ticks again. The fall-through is SCOPED: a bare
    sponsorship mention matching NO consent class stays a (parked) assertion, so a
    naked sponsorship mention is still never auto-ticked."""
    low = (label or "").lower()
    intent = _select_intent(low)
    if intent == "work_auth":
        return "assertion"
    if intent == "sponsorship":
        if _sponsorship_assertion_polarity(low) is not None:
            return "assertion"
        consent = _consent_class_of(low)
        return consent if consent is not None else "assertion"
    return _consent_class_of(low)


def _consent_class_of(low: str) -> str | None:
    """The non-assertion consent class of an already-lowercased label, or None.

    Order is load-bearing: an ASSESSMENT-participation ask, then a talent-pool
    opt-in, then marketing, then application_privacy, so a box that also says "I
    agree" is never mis-sorted into legal privacy consent."""
    if _ASSESSMENT_RE.search(low):
        return "assessment"
    if _TALENT_POOL_RE.search(low):
        return "talent_pool"
    if _MARKETING_RE.search(low):
        return "marketing"
    if _CONSENT_RE.search(low):
        return "application_privacy"
    return None


def _consent_disposition(kind: str, ssot: SSOT):
    """RS-g: the fill disposition for a consent-class checkbox, driven by the
    owner policy seeded under `policies.consent.<class>`.

    Returns ("tick" | "truthful_only" | "skip", skip_reason_or_None). A seeded
    verdict of "consent" ticks, "truthful_only" defers to the SSOT truth check
    (assertions), and anything else ("decline" / "opt_out" / unknown) is an
    honest non-fill. When NO policy is seeded, backward compat holds via
    `_consent_disposition_legacy`."""
    raw = ssot.get(_CONSENT_POLICY_PATHS[kind])
    if raw is MISSING:
        return _consent_disposition_legacy(kind, ssot)
    verdict = str(raw).strip().lower()
    if verdict == "consent":
        return "tick", None
    if verdict == "truthful_only":
        return "truthful_only", None
    return "skip", _policy_decline_reason(kind, verdict)


def _consent_disposition_legacy(kind: str, ssot: SSOT):
    """Backward compat when `policies.consent` is absent (owner ruling
    2026-07-19): only application-necessary privacy consent auto-fills, and only
    when `_CONSENT_SOURCE_PATHS` ratify it; marketing / talent-pool / assessment /
    assertion never auto-fill (the superseded talent-pool YES is retired)."""
    if kind == "application_privacy":
        if _consent_ratified(ssot):
            return "tick", None
        return "skip", _CONSENT_UNRATIFIED_REASON
    if kind == "marketing":
        return "skip", _MARKETING_SKIP_REASON
    return "skip", _consent_unseeded_reason(kind)


def _consent_unseeded_reason(kind: str) -> str:
    return (f"{kind} checkbox left unticked: no consent policy seeded "
            "(owner ruling 2026-07-19; only application-necessary privacy "
            "consent auto-fills without a seeded policy)")


def _policy_decline_reason(kind: str, verdict: str) -> str:
    return f"{kind} checkbox not ticked: consent policy verdict {verdict!r}"


def _assertion_proven_true(fld, ssot: SSOT, profile: dict):
    """RS-g/W2: is the factual claim an assertion checkbox makes PROVABLY true?
    True / False / None.

    A SPONSORSHIP-intent label is checked against the sponsorship truth with its
    OWN (opposite) polarity and never against right-to-work facts; every other
    assertion is a right-to-work claim. The split is `_select_intent`'s, the same
    one the yes/no select path applies.

    Right-to-work routes through the RS-b work-authorization machinery (never a
    duplicate): the region is read from the assertion LABEL (which names the
    country, e.g. "... authorized to work in the United States"), falling back to
    the posting location; the region-keyed `work_authorization` mapping then
    yields the authorized verdict (`_authorized_verdict`). None when the SSOT
    carries no region-keyed mapping, the region cannot be placed, or the entry
    states no usable answer -- the checkbox then stays unchecked (never a false
    check)."""
    if _select_intent(fld.label) == "sponsorship":
        return _sponsorship_assertion_proven_true(fld, ssot)
    raw = ssot.get("work_authorization")
    if raw is MISSING:
        raw = ssot.get("canned_answers.work_authorization")
    if raw is MISSING or not isinstance(raw, dict):
        return None
    region = _posting_region_key(fld.label)
    if region is None:
        region = _posting_region_key(
            str((profile or {}).get("posting_location") or ""))
    if region is None:
        return None
    entry = raw.get(region, MISSING)
    if entry is MISSING:
        return None
    return _authorized_verdict(entry)


def _sponsorship_assertion_proven_true(fld, ssot: SSOT):
    """Is the SPONSORSHIP claim a checkbox makes PROVABLY true? True/False/None.

    The polarity is the INVERSE of right-to-work's and is never derived from it:
    the owner's EU rights mean Yes to "authorized to work" but No to "require
    sponsorship", so a box asserting that sponsorship IS required is true only
    when the SSOT says sponsorship is needed. The truth comes from
    `_sponsorship_needed`, the same source the sponsorship SELECT answers from.

    Parks (None) on three ambiguities, each of which would otherwise be a guess
    on a legally significant question: a region the SSOT does not cover (the same
    `_region_ambiguous` gate the select path applies, so the checkbox path is
    never the more permissive of the two), a label whose requirement polarity
    this code cannot read, and an SSOT that does not establish the requirement."""
    if _region_ambiguous(fld.label):
        return None
    asserted = _sponsorship_assertion_polarity(fld.label)
    if asserted is None:
        return None
    needed = _sponsorship_needed(ssot)
    if needed is None:
        return None
    return asserted is needed


def _sponsorship_assertion_polarity(label: str):
    """Does the label assert that sponsorship IS required (True), that it is NOT
    (False), or state no readable polarity (None -> the caller parks)?

    H.1 (owner ruling 2026-07-20): reads polarity from THREE grammatical shapes,
    each anchored to the sponsorship/visa noun so a requirement verb elsewhere in
    a multi-sentence label cannot flip it:
      - verb-then-noun requirement ("require visa sponsorship") -> REQUIRED
      - noun-then-verb requirement ("sponsorship is required")  -> REQUIRED
      - possession / negated requirement, verb-then-noun OR noun-then-verb
        ("I have a work visa", "do not require sponsorship",
        "No visa is required") -> NOT-REQUIRED
    NOT-required forms are tested FIRST: a negated requirement also contains the
    affirmative verb (so order defuses it), and a possession claim is
    unambiguously not-required. A "/" is normalised to a space so a slash-joined
    verb ("require/ask for visa sponsorship") still reads as a requirement. An
    in-progress or negated possession ("have applied for a visa", "do not have a
    visa") reads None rather than a false NOT-required."""
    low = re.sub(r"/", " ", (label or "").lower())
    if _SPONSOR_NOT_NEEDED_ASSERT_RE.search(low):
        return False
    if _SPONSOR_HAVE_RE.search(low) and not _SPONSOR_NEG_POSSESS_RE.search(low):
        return False
    if _SPONSOR_NOT_NEEDED_HEAD_RE.search(low):
        return False
    if _SPONSOR_NEEDED_ASSERT_RE.search(low):
        return True
    if _SPONSOR_NEED_HEAD_RE.search(low):
        return True
    return None


def _consent_ratified(ssot: SSOT) -> bool:
    """True iff the SSOT carries a non-negative consent answer (never fabricated:
    an explicit "no" or an absent answer leaves the box unticked)."""
    for path in _CONSENT_SOURCE_PATHS:
        value = ssot.get(path)
        if value is MISSING:
            continue
        if _yesno(value) is not False:   # True or non-yes/no prose -> ratified
            return True
    return False


# Full-name SSOT paths: when the fieldmap matcher falls back to one of these
# (no discrete identity.first_name/identity.last_name in the SSOT), a first- or
# last-name field must split the combined value rather than type it whole into
# both fields.
_FULL_NAME_PATHS = frozenset({"identity.name", "identity.full_name"})


def _render_value(fld, path: str, ssot: SSOT, posting_location: str = ""):
    """Render one ANSWERABLE field to (value, None) or (None, skip_reason).

    File and boolean fields are handled by their own branches of `resolve_values`
    and never reach here; the file guard below is defence in depth so a file
    field can never be rendered as free text even if the dispatch changes.

    `posting_location` reaches only the work-authorization mapping resolver (RS-b),
    which selects the region scalar by the posting's country."""
    if fld.type == "input_file":
        return None, "file-upload"
    raw = ssot.get(path)
    if raw is MISSING:
        return None, f"answerable via {path} but no literal SSOT value"
    if isinstance(raw, dict):
        return _render_dict_value(fld, path, raw, posting_location)
    if path in _FULL_NAME_PATHS:
        kind = _name_part_kind(fld.label)
        if kind is not None:
            return _split_full_name(kind, path, raw)
    if fld.type in _SELECT_TYPES:
        return _render_select(fld, raw, ssot)
    return _render_text(raw, path)


def _render_dict_value(fld, path: str, raw: dict, posting_location: str = ""):
    """A dotted path that resolved to an SSOT sub-tree (dict) rather than a
    scalar. A select field may still be answerable from one of the dict's
    scalar values matching an option (exact match first, then the leading-
    Yes/No-token fallback, `_extract_yesno_option` -- e.g. a region-keyed
    `sponsorship_answer_by_region` dict whose EU sub-value is a full sentence
    "No, I have the right to work..." maps onto a bare "No" option); a text
    field (or a select with no matching scalar) is honestly skipped rather
    than typing/matching the mapping itself.

    The work_authorization MAPPING (RS-b) is region-keyed and CANNOT be answered
    by blindly scanning its values (that is exactly the live "no usable scalar"
    failure): it is routed to the per-country resolver, which selects the region
    entry by the posting's country and parks honestly without one."""
    if path in _WORK_AUTH_DICT_PATHS:
        return _render_work_auth_by_country(fld, raw, posting_location)
    if path == _SPONSORSHIP_BY_REGION_PATH:
        return _render_sponsorship_by_region(fld, raw, posting_location)
    if fld.type in _SELECT_TYPES:
        for value in raw.values():
            match = _match_option(fld.options, value)
            if match is not None:
                return match, None
        for value in raw.values():
            extracted = _extract_yesno_option(fld.options, value)
            if extracted is not None:
                return extracted, None
    return None, f"{path} resolved to a mapping with no usable scalar"


def _name_part_kind(label: str) -> str | None:
    """"first" / "last" / None, using the SAME label keywords the fieldmap
    matchers use to identify a first- or last-name question."""
    low = (label or "").lower()
    if any(keyword in low for keyword in _FIRST_NAME_KEYWORDS):
        return "first"
    if any(keyword in low for keyword in _LAST_NAME_KEYWORDS):
        return "last"
    return None


def _split_full_name(kind: str, path: str, raw):
    """Split a combined-name SSOT value for a discrete first/last name field.

    A single-token name gives the first-name field the whole token; the
    last-name field has nothing left to split out, so it is honestly skipped
    rather than typed as an empty string."""
    tokens = str(raw).split()
    if not tokens:
        return None, _empty_value_skip(path)
    if kind == "first":
        return tokens[0], None
    if len(tokens) == 1:
        return None, f"{path} is a single-token name; no last name to split out"
    return tokens[-1], None


def _render_select(fld, raw, ssot: SSOT):
    if fld.type == "multi_value_multi_select":
        candidates = raw if isinstance(raw, list) else [raw]
        matched = [m for m in (_match_option(fld.options, c) for c in candidates)
                   if m is not None]
        if not matched:
            return None, f"no option matches SSOT value {_short(raw)!r}"
        return matched, None
    intent = _select_intent(fld.label)
    if intent is not None:
        return _resolve_yes_no_select(fld, ssot, intent, raw)
    match = _match_option(fld.options, raw)
    if match is not None:
        return match, None
    extracted = _extract_yesno_option(fld.options, raw)
    if extracted is not None:
        return extracted, None
    consent_option = _consent_select_option(fld, raw, ssot)
    if consent_option is not None:
        return consent_option, None
    return None, f"no option matches SSOT value {_short(raw)!r}"


def _consent_select_option(fld, raw, ssot: SSOT):
    """RS-g: a consent-class SELECT (an application-necessary privacy-notice
    acknowledgement) whose sole affirmative option ("Acknowledge"/"Confirm"/"I
    agree") no bare yes/no match can reach. Under an application_privacy consent
    policy that ticks, pick that single affirmative option; return None (leaving
    the caller's honest "no option matches" skip) otherwise.

    Fixes the live greenhouse `question_37455721` ("Please confirm that you have
    read and agree to ... Privacy Notice and Privacy Policy", options
    ["Acknowledge/Confirm"]) failing "no option match". Never overrides an
    explicit SSOT "no", and never invents a choice among SEVERAL affirmatives.

    FAILS CLOSED on an AI-policy attestation (`is_ai_policy_question`): a label
    such as "During this application process I agree to use only my own words ...
    AI ... will disqualify" also matches `_classify_checkbox` as
    application_privacy via "agree", but its affirmative is a stance the owner did
    not make (FX3, the generator forbids it) and must never be auto-picked."""
    if is_ai_policy_question(fld.label):
        return None
    if _classify_checkbox(fld.label) != "application_privacy":
        return None
    action, _ = _consent_disposition("application_privacy", ssot)
    if action != "tick":
        return None
    if _yesno(raw) is False:                # an explicit SSOT "no" is respected
        return None
    affirmatives = [o for o in (fld.options or []) if _is_affirmative_option(o)]
    if len(affirmatives) == 1:
        return affirmatives[0]
    return None


def _is_affirmative_option(option) -> bool:
    """An option label that reads as an affirmative acknowledgement (RS-g)."""
    return bool(_AFFIRMATIVE_OPTION_RE.search(str(option)))


def _extract_yesno_option(options, raw):
    """Fallback for a Yes/No select whose SSOT value is a full sentence
    carrying a leading Yes/No token ("No. I have no non-compete.", "Yes, I
    would relocate."): map it onto the option that reads EXACTLY "Yes" or
    "No" (case-insensitively strip/first-word), when one exists. Applied only
    AFTER an exact option match has already failed (`_match_option`), never
    as a replacement for it.

    Never guesses a specific "Yes, <detail>" variant from a bare Yes token:
    an option set carrying only "Yes, X" phrasing (no BARE "Yes" option, e.g.
    a sponsorship select enumerating regions) has no single right answer to
    pick, so this returns None and the caller's existing "no option matches"
    skip stays honest rather than fabricating a choice among several
    plausible variants. A leading "No" mapping onto a bare "No" option always
    wins, since a bare negative reads the same regardless of enumeration."""
    verdict = _yesno(raw)
    if verdict is None:
        return None
    target = "yes" if verdict else "no"
    for option in options or []:
        if str(option).strip().lower() == target:
            return option
    return None


# -- yes/no select normalization (criterion: right-to-work / sponsorship) ------

def _select_intent(label: str) -> str | None:
    low = (label or "").lower()
    if _SPONSOR_INTENT_RE.search(low):
        return "sponsorship"
    if _WORK_AUTH_INTENT_RE.search(low):
        return "work_auth"
    return None


def _resolve_yes_no_select(fld, ssot: SSOT, intent: str, raw):
    """Answer a right-to-work / sponsorship select conservatively.

    The region gate takes precedence over a naive exact option match: a posting
    whose label targets a region the SSOT does not cover (e.g. the US) is left
    honestly unfilled with a questionnaire pointer rather than answered from
    EU-context facts. Otherwise an exact option match wins, then a yes/no derived
    from the SSOT work-authorization facts (EU/Italy rights -> Yes to
    authorization / No to sponsorship-required). Never fabricates a Yes for a
    right the SSOT does not state."""
    if _region_ambiguous(fld.label):
        detail = ("region-ambiguous work authorization" if intent == "work_auth"
                  else "region-ambiguous visa sponsorship")
        return None, _questionnaire_skip(
            fld, f"{detail} (posting region outside the SSOT's EU/Italy work "
            "rights)")
    match = _match_option(fld.options, raw)
    if match is not None:
        return match, None
    if intent == "work_auth":
        if not _has_eu_work_rights(_work_auth_text(ssot)):
            return None, _questionnaire_skip(
                fld, "work authorization not established in the SSOT")
        want_yes = True
    else:
        needed = _sponsorship_needed(ssot)
        if needed is None:
            return None, _questionnaire_skip(
                fld, "visa sponsorship requirement not established in the SSOT")
        want_yes = needed                        # sponsorship needed -> Yes
    option = _pick_option(fld.options, want_yes)
    if option is None:
        return None, f"no yes/no option to answer {_short(fld.label)!r}"
    return option, None


def _region_ambiguous(label: str) -> bool:
    """True when the label names a region the SSOT does not cover and does NOT
    also name a covered (EU/Italy) region."""
    return bool(_UNCOVERED_REGION_RE.search(label or "")
                and not _COVERED_REGION_RE.search(label or ""))


def _work_auth_text(ssot: SSOT) -> str:
    raw = ssot.get("work_authorization")
    if raw is MISSING:
        return ""
    if isinstance(raw, dict):
        return " ".join(str(v) for v in raw.values()).lower()
    if isinstance(raw, (list, tuple)):
        return " ".join(str(v) for v in raw).lower()
    return str(raw).lower()


def _has_eu_work_rights(text: str) -> bool:
    if not text:
        return False
    region = re.search(r"\beu\b|european|\beea\b|ital|europe", text)
    rights = re.search(
        r"work right|authori|citizen|permit|entitled|no visa|no sponsor|"
        r"freedom of movement", text)
    return bool(region and rights)


def _sponsorship_needed(ssot: SSOT):
    """True/False/None: does the candidate require visa sponsorship? Prefers the
    dedicated canned answer, then the work-authorization prose."""
    raw = ssot.get("canned_answers.visa_sponsorship_required")
    if raw is not MISSING:
        verdict = _yesno(raw)
        if verdict is not None:
            return verdict
    text = _work_auth_text(ssot)
    if re.search(r"no (visa )?sponsor|sponsorship not (needed|required)|"
                 r"without sponsor|no need for sponsor", text):
        return False
    return None


def _pick_option(options, want_yes: bool):
    """The option whose label reads affirmative (want_yes) or negative. A yes_no
    field with no enumerated options falls back to the literal "Yes"/"No"."""
    for option in options or []:
        if _yesno(option) is want_yes:
            return option
    if not options:
        return "Yes" if want_yes else "No"
    return None


def _yesno(value):
    """True/False/None for a scalar: yes/no leading token, else undetermined."""
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return None
    if _YESNO_NEG_RE.match(text):
        return False
    if _YESNO_POS_RE.match(text):
        return True
    return None


def _questionnaire_skip(fld, detail: str) -> str:
    """A skip reason that both explains the ambiguity and carries a
    questionnaire dotted-path pointer (same shape as fieldmap's missing guess),
    so the required field stays honestly unfilled and feeds a questionnaire."""
    return f"needs questionnaire ({detail}): {_missing_path_guess(fld.label)}"


def _match_option(options, raw):
    """The option label equal (case-insensitively) to a scalar SSOT value."""
    if isinstance(raw, (list, dict)):
        return None
    target = str(raw).strip().lower()
    if not target:
        return None
    for option in options:
        if str(option).strip().lower() == target:
            return option
    return None


def _render_text(raw, path: str):
    if isinstance(raw, bool):
        return ("Yes" if raw else "No"), None
    if isinstance(raw, str):
        if not raw.strip():
            return None, _empty_value_skip(path)
        return raw, None
    if isinstance(raw, (int, float)):
        return str(raw), None
    if isinstance(raw, list) and all(
            isinstance(item, (str, int, float)) for item in raw):
        rendered = ", ".join(str(item) for item in raw)
        if not rendered.strip():
            return None, _empty_value_skip(path)
        return rendered, None
    return None, f"value for {path} is not renderable as text"


def _empty_value_skip(path: str) -> str:
    """The skip reason for a required/answerable field whose SSOT path resolves
    to an empty/whitespace value: there is nothing to fill, so it is SKIPPED
    (never a confirmed fill). A required field with this reason lands in
    `required_unfilled` -> NOT COMPLETE, never a silent false-COMPLETE."""
    return f"empty SSOT value at {path} (nothing to fill)"


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ============================================================================ #
# Posting-location resolution (RS-b work auth by country, RS-d commute boolean).
# ============================================================================ #
#
# Two resolver classes genuinely need the POSTING's country, which the kernel
# historically did not carry. It now rides on the resolution profile
# ("posting_location", seeded by w5_accept from the generated document -- the
# same value the content overlay's nearest-city policy reads). Both resolvers
# park honestly without it: a wrong answer is a lie sent under the owner's name.
#
# Two DISTINCT notions of region ride on the location, kept deliberately apart:
#   * work ELIGIBILITY (RS-b): the SSOT work_authorization region KEY
#     (eu/ch/uk/us/ca) a posting country belongs to. The UK is its OWN key
#     (post-Brexit it is not EU for work rights), mirroring profile_map's split.
#   * GEOGRAPHIC commute band (RS-d): how far the owner would travel, where the
#     UK IS Europe. match.py draws the same _EU_* vs _EUROPE_* distinction; the
#     kernel firewall (stdlib + engine.kernel.* only) forbids importing it, so
#     the focused tables below are a deliberate, documented duplication.

_WEEKS_PER_MONTH = 4.33  # calendar constant, mirrors match._WEEKS_PER_MONTH

# posting country -> SSOT work_authorization region key. Order is load-bearing:
# the sponsorship-required regions (us/uk/ca/ch) are tested BEFORE eu so a
# "London, United Kingdom" posting keys uk, never eu.
_REGION_KEY_PATTERNS = [
    ("us", re.compile(r"united states|\bu\.?s\.?a?\.?\b|\bamerica\b", re.I)),
    ("uk", re.compile(r"united kingdom|\bu\.?k\.?\b|great britain|\bbritain\b|"
                      r"\bengland\b|\bscotland\b|\bwales\b|northern ireland",
                      re.I)),
    ("ca", re.compile(r"\bcanada\b|\bcanadian\b|\btoronto\b|\bvancouver\b|"
                      r"\bmontreal\b|\bottawa\b", re.I)),
    ("ch", re.compile(r"switzerland|\bswiss\b|\bz(?:u|ü)rich\b|\bgeneva\b|"
                      r"\bbasel\b|\blausanne\b|\bbern\b", re.I)),
    ("eu", re.compile(
        r"european union|\beu\b|\be\.u\.\b|\beea\b|\beurope\b|"
        r"ireland|\bdublin\b|germany|\bberlin\b|\bmunich\b|france|\bparis\b|"
        r"spain|\bmadrid\b|\bbarcelona\b|ital|\brome\b|\bmilan\b|\bbologna\b|"
        r"netherlands|\bamsterdam\b|belgium|\bbrussels\b|portugal|\blisbon\b|"
        r"austria|\bvienna\b|poland|\bwarsaw\b|sweden|\bstockholm\b|denmark|"
        r"\bcopenhagen\b|finland|\bhelsinki\b|luxembourg|greece|\bathens\b|"
        r"czech|\bprague\b|hungary|romania|bulgaria|croatia|"
        r"slovak|sloven|estonia|latvia|lithuania|\bmalta\b|cyprus", re.I)),
]

# GEOGRAPHIC commute bands (RS-d): the UK IS Europe here (a London commute is a
# European commute). A location classifiable as NEITHER Europe nor a known
# outside country is left UNPLACEABLE (None) so the boolean parks, never guesses.
_EUROPE_GEO_RE = re.compile(
    r"european union|\beu\b|\beea\b|\beurope\b|united kingdom|\bu\.?k\.?\b|"
    r"great britain|\bbritain\b|\bengland\b|\bscotland\b|\bwales\b|\blondon\b|"
    r"ireland|\bdublin\b|germany|\bberlin\b|\bmunich\b|france|\bparis\b|"
    r"spain|\bmadrid\b|\bbarcelona\b|ital|\brome\b|\bmilan\b|\bbologna\b|"
    r"switzerland|\bswiss\b|\bz(?:u|ü)rich\b|\bgeneva\b|"
    r"netherlands|\bamsterdam\b|belgium|\bbrussels\b|portugal|\blisbon\b|"
    r"austria|\bvienna\b|poland|\bwarsaw\b|sweden|\bstockholm\b|denmark|"
    r"\bcopenhagen\b|finland|\bhelsinki\b|luxembourg|greece|\bathens\b|"
    r"czech|\bprague\b|hungary|romania|bulgaria|croatia|norway|\boslo\b|"
    r"iceland|slovak|sloven|estonia|latvia|lithuania|\bmalta\b|cyprus", re.I)
_OUTSIDE_EUROPE_RE = re.compile(
    r"united states|\bu\.?s\.?a?\.?\b|\bamerica\b|\bcanada\b|\btoronto\b|"
    r"\bvancouver\b|\bmontreal\b|australia|\bsydney\b|\bmelbourne\b|"
    r"\bindia\b|\bbangalore\b|\bbengaluru\b|singapore|\buae\b|\bdubai\b|"
    r"\bisrael\b|tel aviv|\bjapan\b|\btokyo\b|\bchina\b|\bbrazil\b|"
    r"new zealand|south africa|mexico", re.I)

# W4-COMMUTE-GATE cadence shapes (SSOT preferences.location_policy), mirroring
# match._ONSITE_AMOUNT_PATTERNS (the kernel firewall forbids importing it).
_ONSITE_CADENCE_PATTERNS = (
    (re.compile(r"(\d+)\s*days?\s*(?:per|a|/)\s*month", re.I), "month"),
    (re.compile(r"(\d+)\s*days?\s*(?:per|a|/)\s*week", re.I), "week"),
    (re.compile(r"(\d+)\s*times?\s*(?:per|a|/)\s*month", re.I), "month"),
    (re.compile(r"(\d+)\s*times?\s*(?:per|a|/)\s*week", re.I), "week"),
    (re.compile(r"(\d+)\s*days?\s*in\s*(?:the\s*)?office", re.I), "week"),
    # The MULTIPLIER form ("4x/week", "4x per month"), live on workable's
    # CA_9781. The per|a|/ separator is REQUIRED, matching the shapes above, so a
    # bare "2x" or a "2x your week" phrase is not read as a cadence.
    (re.compile(r"(\d+)\s*x\s*(?:per|a|/)\s*month", re.I), "month"),
    (re.compile(r"(\d+)\s*x\s*(?:per|a|/)\s*week", re.I), "week"),
)

# The gerund "coming into the office" is the live CA_9781 wording; the bare
# "come into the office" alternative this pattern shipped with did not reach it.
_IN_OFFICE_RE = re.compile(
    r"in[\s-]*office|in the office|on[\s-]*site|in[\s-]*person|"
    r"com(?:e|ing)\s+in(?:to)?\s+the\s+office|days?\s+in\s+(?:the\s+)?office",
    re.I)

# The work_authorization candidate paths (mirrors the work-auth `_ANSWER_MATCHERS`
# row): a dict resolved via one of these is region-keyed and routed to the
# per-country resolver, never scanned blindly for a matching option value.
_WORK_AUTH_DICT_PATHS = frozenset({
    "work_authorization", "canned_answers.work_authorization",
})

# RS-a: the region-keyed owner sponsorship policy. A dict resolved via this path
# is the visa-sponsorship ANSWER per region (eu/eea -> "No", any other region ->
# the "default"), selected by the POSTING country and NEVER by a US-specific seed.
_SPONSORSHIP_BY_REGION_PATH = "policies.sponsorship_by_region"


def _posting_region_key(posting_location: str):
    """The SSOT work_authorization region key a posting country belongs to, or
    None when the location is empty or maps to no known region (park, never
    guess)."""
    text = (posting_location or "").strip().lower()
    if not text:
        return None
    for key, pattern in _REGION_KEY_PATTERNS:
        if pattern.search(text):
            return key
    return None


def _render_work_auth_by_country(fld, raw: dict, posting_location: str):
    """RS-b: a work-authorization question that resolved to the region-keyed
    work_authorization MAPPING. Select the region entry by the POSTING country
    and render its authorized-to-work verdict. Parks honestly when the posting
    country is unavailable, unmapped, or the region entry states no usable
    answer -- never guesses a region, never types the mapping."""
    region = _posting_region_key(posting_location)
    if region is None:
        return None, _questionnaire_skip(
            fld, "work authorization: posting country unavailable or unmapped")
    entry = raw.get(region, MISSING)
    if entry is MISSING:
        return None, _questionnaire_skip(
            fld, f"work authorization: no SSOT entry for posting region {region!r}")
    verdict = _authorized_verdict(entry)
    if verdict is None:
        return None, _questionnaire_skip(
            fld, f"work authorization for region {region!r} states no usable answer")
    if fld.type in _SELECT_TYPES:
        option = _pick_option(fld.options, verdict)
        if option is None:
            return None, f"no yes/no option to answer {_short(fld.label)!r}"
        return option, None
    return ("Yes" if verdict else "No"), None


def _render_sponsorship_by_region(fld, raw: dict, posting_location: str):
    """RS-a: a visa-sponsorship question that resolved to the region-keyed
    `policies.sponsorship_by_region` policy. Select the region entry by the
    POSTING country (eu/eea -> "No", any other region -> the "default"), so a UK
    or US posting no longer inherits a US-specific canned "No"/"Yes". The looked-
    up value ("No"/"Yes") IS the answer; a SELECT gets the matching option, a
    text field the literal.

    Parks honestly when the posting country is unavailable, unmapped, or the
    policy states no usable answer for it -- a wrong sponsorship answer is a lie
    sent under the owner's name. Preferred over the
    work_authorization.<region>.sponsorship_required structured fact
    (profile_map.py:70): when both exist and disagree, this explicit owner policy
    wins (its path leads the sponsorship matcher, so it is the one that resolves)."""
    region = _posting_region_key(posting_location)
    if region is None:
        return None, _questionnaire_skip(
            fld, "visa sponsorship: posting country unavailable or unmapped")
    answer = raw.get(region, raw.get("default", MISSING))
    if answer is MISSING:
        return None, _questionnaire_skip(
            fld, f"visa sponsorship: no policy entry for region {region!r} "
            "and no default")
    verdict = _yesno(answer)
    if verdict is None:
        return None, _questionnaire_skip(
            fld, f"visa sponsorship for region {region!r} states no usable answer")
    if fld.type in _SELECT_TYPES:
        option = _pick_option(fld.options, verdict)
        if option is None:
            return None, f"no yes/no option to answer {_short(fld.label)!r}"
        return option, None
    return ("Yes" if verdict else "No"), None


def _authorized_verdict(entry):
    """Is the owner authorized to work in this region? True/False/None.

    A scalar entry is the owner's own answer (a yes/no leading token, or a bool).
    A structured entry carries `sponsorship_required`: authorized-to-work is the
    NEGATION of needing sponsorship (no sponsorship needed -> already authorized).
    None when the entry states neither (park)."""
    if isinstance(entry, bool):
        return entry
    if isinstance(entry, dict):
        flag = _sponsorship_required_flag(entry.get("sponsorship_required"))
        return (not flag) if flag is not None else None
    return _yesno(entry)


def _sponsorship_required_flag(value):
    """A boolean-ish `sponsorship_required` value -> True/False, else None."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return _yesno(value)
    return None


def _is_in_office_commitment(label: str) -> bool:
    """An in-office ATTENDANCE-cadence boolean: it names in-office presence AND
    carries a parseable days-per-week|month cadence. The cadence requirement
    keeps a plain consent box ("consent to in-office monitoring?") out of the
    derivation path."""
    low = (label or "").lower()
    if not _IN_OFFICE_RE.search(low):
        return False
    amount, _ = _detect_onsite_cadence(low)
    return amount is not None


def _resolve_in_office_boolean(fld, ssot: SSOT, profile: dict):
    """RS-d: derive an in-office-commitment boolean from the W4-COMMUTE-GATE
    owner policy (SSOT preferences.location_policy) plus the POSTING location, never by truthiness-
    coercing the prose attendance answer. Milan/Bologna -> any cadence viable;
    elsewhere in Europe -> viable at or under the weekly cap; outside Europe ->
    viable at or under the monthly cap. Parks honestly (no location, no policy,
    unparseable cadence, or an unplaceable location).

    Returns (bool_value, None) when derived, or (None, skip_reason) to park."""
    posting_location = str((profile or {}).get("posting_location") or "")
    if not posting_location:
        return None, _questionnaire_skip(
            fld, "in-office commitment: posting location unavailable")
    policy = _commute_policy(ssot)
    if policy is None:
        return None, _questionnaire_skip(
            fld, "in-office commitment: no location policy in the SSOT")
    amount, unit = _detect_onsite_cadence(fld.label)
    if amount is None:
        return None, _questionnaire_skip(
            fld, "in-office commitment: on-site cadence not parseable")
    band = _commute_band(posting_location, policy["allowed_cities"])
    if band is None:
        return None, _questionnaire_skip(
            fld, "in-office commitment: posting location not placeable")
    if band == "allowed_city":
        return True, None
    if band == "europe":
        detected = amount if unit == "week" else amount / _WEEKS_PER_MONTH
        threshold = policy["max_onsite_days_per_week_europe"]
    else:
        detected = amount * _WEEKS_PER_MONTH if unit == "week" else amount
        threshold = policy["max_onsite_days_per_month_rest"]
    return detected <= threshold, None


def _commute_policy(ssot: SSOT):
    """The W4-COMMUTE-GATE policy from SSOT preferences.location_policy, or None
    when absent/partial (fail-closed: no policy -> the boolean parks). Mirrors
    match._location_policy_from_ssot (kernel firewall forbids importing it)."""
    raw = ssot.get("preferences.location_policy")
    if raw is MISSING or not isinstance(raw, dict):
        return None
    week_cap = raw.get("max_onsite_days_per_week_europe")
    month_cap = raw.get("max_onsite_days_per_month_rest")
    if (isinstance(week_cap, bool) or isinstance(month_cap, bool)
            or not isinstance(week_cap, (int, float))
            or not isinstance(month_cap, (int, float))):
        return None
    cities = raw.get("allowed_cities")
    allowed = ([str(c) for c in cities]
               if isinstance(cities, (list, tuple)) else [])
    return {"allowed_cities": allowed,
            "max_onsite_days_per_week_europe": float(week_cap),
            "max_onsite_days_per_month_rest": float(month_cap)}


def _detect_onsite_cadence(text: str):
    """(day-count, "week"|"month") parsed from an in-office question label, or
    (None, None)."""
    for pattern, unit in _ONSITE_CADENCE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return float(match.group(1)), unit
    return None, None


def _commute_band(posting_location: str, allowed_cities):
    """"allowed_city" | "europe" | "outside" | None for a posting location.

    An owner allowed city (Milan/Bologna) beats the coarse Europe/outside bands.
    None when the location is empty or classifiable as neither Europe nor a known
    outside country (park, never guess a band)."""
    text = (posting_location or "").strip().lower()
    if not text:
        return None
    for city in allowed_cities or []:
        name = str(city).strip().lower()
        if name and re.search(rf"\b{re.escape(name)}\b", text):
            return "allowed_city"
    if _EUROPE_GEO_RE.search(text):
        return "europe"
    if _OUTSIDE_EUROPE_RE.search(text):
        return "outside"
    return None


# -- fill completeness accounting ----------------------------------------------

def _completeness(fieldmap: FieldMap | None, filled_keys: set[str],
                  all_skips: list[tuple[str, str]], filled: int,
                  vendor_resolver=None):
    """Compute (fillable_total, required_unfilled, justified_skips) (criterion 1).

    A required field left unfilled for an UNjustified reason enters
    `required_unfilled` (Z); a non-hidden field left unfilled is counted in
    `justified_skips` only for a justified reason -- a GENUINE demographic-
    section skip (`_is_justified_eeo_skip`: section in COMPLIANCE_EEOC /
    DEMOGRAPHIC / VOLUNTARY, or decline_allowed=True -- regardless of
    requiredness) or an OPTIONAL file-upload/asset-missing skip. A REQUIRED
    field is never justified on EEO grounds merely because its label/reason
    contains an EEO keyword when it is NOT a genuine demographic-section
    field. Hidden portal-telemetry fields are excluded entirely. Without a
    field map the report degrades to the fields fill_form saw and cannot
    assert requiredness, so `required_unfilled` is empty.

    Hidden portal-telemetry detection is delegated to the injected
    `vendor_resolver` (spec 3.4); it defaults to `_NOOP_RESOLVER` (no hidden
    widgets). The Greenhouse behaviour is injected by
    `engine.providers.greenhouse.fill`.
    """
    resolver = vendor_resolver if vendor_resolver is not None else _NOOP_RESOLVER
    skip_reason = dict(all_skips)
    if fieldmap is None:
        # No field map means no requiredness to assert (required_unfilled stays
        # empty either way), so an upload skip is counted justified here same
        # as before the fix -- there is no `f.required` to gate it on.
        fillable_total = filled + len(skip_reason)
        justified = sum(1 for reason in skip_reason.values()
                        if _is_eeo_reason(reason) or _is_upload_skip(reason)
                        or _is_satisfied_by_sibling_upload(reason)
                        or _is_tos_forbidden_skip(reason))
        return fillable_total, [], justified

    non_hidden = [f for f in fieldmap.fields if not _is_hidden_field(f, resolver)]
    required_unfilled: list[dict] = []
    justified = 0
    for f in non_hidden:
        if f.key in filled_keys:
            continue
        reason = skip_reason.get(f.key, "not filled")
        if _is_justified_eeo_skip(f, reason):
            justified += 1
        elif _is_satisfied_by_sibling_upload(reason):
            justified += 1
        elif _is_tos_forbidden_skip(reason):
            # Verdict class ii: the employer's ToS forbids the ENGINE from
            # answering this field (documented handoff to a human), so it is a
            # justified skip subtracted from the gate, never a data gap -- even
            # when required. The never-send guard means the human completes it.
            justified += 1
        elif _is_upload_skip(reason) and not f.required:
            justified += 1
        elif f.required:
            required_unfilled.append(
                {"key": f.key, "label": f.label, "reason": reason})
    return len(non_hidden), required_unfilled, justified


def _is_hidden_field(fld, vendor_resolver=_NOOP_RESOLVER) -> bool:
    """Pure portal telemetry (longitude/latitude) is mechanically populated and
    never seen by the applicant, so it is not a fillable denominator field. The
    vendor-specific membership test is delegated to the injected
    `vendor_resolver.hidden_widget` (spec 3.4)."""
    return vendor_resolver.hidden_widget(fld)


def _is_eeo_reason(reason: str) -> bool:
    """True iff the skip reason names an EEO/demographic classification.

    A reason-STRING check ONLY: on its own it does NOT justify a skip. A real
    required question can carry this reason via a mere label-keyword match (the
    `_manual_only_reason` keyword list flags e.g. "disability" on a
    STANDARD-section field), so justification additionally requires the field to
    be a genuine voluntary demographic field -- see `_is_justified_eeo_skip`.
    Used directly only in the no-field-map branch of `_completeness`, where
    requiredness cannot be asserted anyway."""
    low = (reason or "").lower()
    return "demographic" in low or "eeo" in low


def _is_justified_eeo_skip(f, reason: str) -> bool:
    """An EEO/demographic skip is justified for a GENUINE demographic field:
    a COMPLIANCE_EEOC / DEMOGRAPHIC / VOLUNTARY section (`_DECLINE_SECTIONS`),
    or `decline_allowed=True`. This holds REGARDLESS of requiredness: policy
    never auto-answers a real demographic question and decline is always
    allowed there, so even a genuinely demographic field that (unusually)
    carries `required=True` stays justified, never a false gap (Greenhouse's
    own capture already forces `required=False` on these -- `_fields_from_
    question`/`_fields_from_demographic` -- but the gate itself must not
    depend on that normalization holding for every vendor/path).

    A REQUIRED field is NEVER justified on EEO grounds merely because its
    reason string (or its LABEL) happens to contain an EEO keyword: a
    genuinely non-demographic question (STANDARD/CUSTOM/LOCATION section,
    e.g. "disability accommodations needed for the interview?") stays a
    required gap even when `_manual_only_reason`'s keyword-based safety net
    (never auto-fill a suspected-EEO field) fires on its label -- that keyword
    match only prevents auto-fill; it never by itself proves the field is a
    real demographic question. The SECTION (a structural signal set from the
    vendor schema's own section/source tag, never from a label keyword) is the
    gate, not the reason string and not requiredness."""
    return (_is_eeo_reason(reason)
            and (f.decline_allowed
                 or getattr(f, "section", "") in _DECLINE_SECTIONS))


def _is_satisfied_by_sibling_upload(reason: str) -> bool:
    """True iff the skip reason names a satisfied-by-sibling-file-upload
    justification (Greenhouse's `resume_text`/`cover_letter_text` paste
    textarea: the schema exposes it even when the LIVE form is configured
    for file-upload instead of paste-text, so the textarea is simply ABSENT
    from the DOM and never attempted -- the sibling `resume`/`cover_letter`
    file field already carries the same document). Unlike `_is_upload_skip`,
    this is justified REGARDLESS of requiredness: the requirement genuinely
    IS satisfied by the equivalent uploaded artifact, not merely excused
    because the field happens to be optional."""
    return (reason or "").lower().startswith("satisfied by sibling file upload")


# The skip-reason prefix a ToS-forbidden field carries (verdict class ii). The
# content overlay writes it (`content.apply_content_overlay`, when a field label
# matches a `generated.tos_forbidden` entry) and `_is_tos_forbidden_skip` /
# `_completeness` read it. A single shared constant is the SSOT across the two
# modules, the same string-keying pattern `_is_satisfied_by_sibling_upload` uses.
TOS_FORBIDDEN_SKIP_PREFIX = "tos_forbidden (documented ToS, class ii)"


def _is_tos_forbidden_skip(reason: str) -> bool:
    """True iff the skip reason names a documented-ToS handoff (verdict class ii):
    the employer's Terms forbid the ENGINE from answering the field, so it is left
    for a human and counted a justified skip, never a data gap. The content
    overlay stamps this prefix when it routes a field to `tos_forbidden`."""
    return (reason or "").startswith(TOS_FORBIDDEN_SKIP_PREFIX)


def _is_upload_skip(reason: str) -> bool:
    """A file-upload skip: either the legacy no-assets skip ("file-upload") or
    a resolved-but-missing asset ("asset missing: <name>"). Unlike an
    EEO/demographic skip, this is justified ONLY when the field itself is
    OPTIONAL (see the `and not f.required` guard at the call site). A REQUIRED
    upload field left unfilled (no CV/photo attached) is a genuine gap, never a
    free pass -- it must land in `required_unfilled` so `complete` cannot read
    True while a mandatory document was never attached."""
    low = (reason or "").lower()
    return "file-upload" in low or "asset missing" in low
