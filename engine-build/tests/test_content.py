"""Content-channel tests: canned routing, option matching, essay delivery.

FAKE SSOT only (tests/fixtures/content/ssot-fake.yaml): no owner data ever enters
the test suite. The overlay is asserted on both sides of its bookkeeping, because
a value that leaks into `fields` without leaving `skipped` would inflate the
completeness numerator.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from engine.content import (
    NO_OPTION_MATCH,
    OVER_MAX_LENGTH,
    ContentSchemaError,
    GeneratedAnswer,
    GeneratedAnswers,
    TosForbidden,
    apply_content_overlay,
    load_generated_answers,
)
from engine.kernel.contracts import Field, FieldMap, FieldValue, Locator, ResolvedValues
from engine.kernel.ssot import SSOT

FIXTURES = Path(__file__).parent / "fixtures" / "content"


@pytest.fixture
def ssot() -> SSOT:
    return SSOT.load(FIXTURES / "ssot-fake.yaml")


def make_field(key: str, label: str, *, type_: str = "input_text",
               options: list[str] | None = None, required: bool = False,
               section: str = "STANDARD", decline_allowed: bool = False,
               norm_type: str = "", max_length: int | None = None) -> Field:
    return Field(key=key, label=label, type=type_, required=required,
                 options=list(options or []), source="test",
                 locator=Locator(role="textbox", name=label),
                 section=section, decline_allowed=decline_allowed,
                 norm_type=norm_type, max_length=max_length)


def make_map(*fields: Field) -> FieldMap:
    return FieldMap(vendor="greenhouse", posting_id="12345",
                    captured_at="2026-07-13T00:00:00Z", fields=list(fields))


def skipped_of(fld: Field, reason: str = "missing:canned_answers.unknown"):
    return ResolvedValues(fields=[], skipped=[(fld.key, reason)])


def overlay_one(fld: Field, ssot: SSOT, **kwargs):
    """Run the overlay over a one-field map whose only field is skipped."""
    resolved = skipped_of(fld)
    report = apply_content_overlay(resolved, make_map(fld), ssot, **kwargs)
    return resolved, report


def test_overlay_routes_how_did_you_hear(ssot: SSOT) -> None:
    fld = make_field("q_hear", "How did you hear about this role?")
    resolved, report = overlay_one(fld, ssot)
    assert resolved.values == {"q_hear": "LinkedIn"}
    assert report.applied == [
        ("q_hear", "canned:canned_answers.how_did_you_hear_default")]
    assert resolved.skipped == []


def test_overlay_routes_in_office_percentage(ssot: SSOT) -> None:
    fld = make_field("q_office",
                     "How much of your time are you willing to spend in office?")
    resolved, report = overlay_one(fld, ssot)
    assert resolved.values["q_office"] == "Yes, five days a week in the office"
    assert report.applied == [
        ("q_office", "canned:canned_answers.in_office_attendance")]


def test_overlay_routes_interviewed_before(ssot: SSOT) -> None:
    fld = make_field("q_before", "Have you previously applied or interviewed here?",
                     type_="yes_no", options=["Yes", "No"])
    resolved, report = overlay_one(fld, ssot)
    assert resolved.values == {"q_before": "No"}
    assert report.applied == [
        ("q_before", "canned:canned_answers.previously_applied_default")]


def test_overlay_option_match_exact_case_insensitive(ssot: SSOT) -> None:
    """The canned "No" lands on a lower-cased option label, unchanged in case."""
    fld = make_field("q_before", "Have you interviewed with us before?",
                     type_="yes_no", options=["yes", "no"])
    resolved, _ = overlay_one(fld, ssot)
    assert resolved.values == {"q_before": "no"}


def test_overlay_option_match_leading_yesno_verbose_options(ssot: SSOT) -> None:
    """A bare "Yes" carries onto a verbose option through its leading yes/no."""
    fld = make_field("q_office", "Are you able to work in the office?",
                     type_="yes_no",
                     options=["Yes, I can work in the office",
                              "No, I cannot work in the office"])
    resolved, _ = overlay_one(fld, ssot)
    assert resolved.values == {"q_office": "Yes, I can work in the office"}


def test_overlay_option_match_token_subset(ssot: SSOT) -> None:
    """"LinkedIn" is a token subset of exactly one option, so it maps there."""
    fld = make_field("q_hear", "How did you hear about us?",
                     type_="multi_value_single_select",
                     options=["LinkedIn job board", "Company website", "Referral"])
    resolved, _ = overlay_one(fld, ssot)
    assert resolved.values == {"q_hear": "LinkedIn job board"}


def test_overlay_option_match_ambiguous_stays_skipped(ssot: SSOT) -> None:
    """Two options share the leading "yes": ambiguity is never guessed away."""
    fld = make_field("q_reloc", "Are you willing to relocate?", type_="yes_no",
                     options=["Yes, immediately", "Yes, within six months", "No"])
    resolved, report = overlay_one(fld, ssot)
    assert resolved.fields == []
    assert resolved.skipped == [("q_reloc", NO_OPTION_MATCH)]
    assert report.applied == []
    assert report.unresolved == [("q_reloc", NO_OPTION_MATCH)]


def test_overlay_relocation_verbose_dropdown_end_to_end(ssot: SSOT) -> None:
    """The verbose relocation dropdown resolves, and falls back to the plain
    willing_to_relocate answer when the dropdown answer was never seeded."""
    fld = make_field("q_reloc", "Are you willing to relocate for this role?",
                     type_="yes_no",
                     options=["Yes, I am willing to relocate",
                              "No, I am not willing to relocate"])
    resolved, report = overlay_one(fld, ssot)
    assert resolved.values == {"q_reloc": "Yes, I am willing to relocate"}
    assert report.applied == [
        ("q_reloc", "canned:canned_answers.relocation_dropdown")]

    fallback_ssot = SSOT({"canned_answers": {"willing_to_relocate": "Yes"}})
    resolved, report = overlay_one(fld, fallback_ssot)
    assert resolved.values == {"q_reloc": "Yes, I am willing to relocate"}
    assert report.applied == [
        ("q_reloc", "canned:canned_answers.willing_to_relocate")]


def test_overlay_canned_never_hijacks_a_long_text_essay(ssot: SSOT) -> None:
    """The canned table matches loose label substrings ("relocat", "how did you
    hear"), and the SAME questions are also asked as essays. A one-word canned
    scalar must never land in an essay box: it would answer an 800-character
    question with "Yes", DISCARD the generated essay written for that very field,
    and still count the field complete -- inflating the completeness numerator
    with a non-answer. The control decides (`is_long_text`), never the label.

    Both long-text shapes are covered: the `textarea` control, and a field the
    vendor normalized to LONGTEXT."""
    essay = make_field("q_reloc_essay",
                       "Are you willing to relocate, and what would help you "
                       "settle in?",
                       type_="textarea", norm_type="LONGTEXT", required=True,
                       max_length=800)
    generated = GeneratedAnswers(
        vendor="greenhouse", slug="acme", job_id="12345", posting_lang="en",
        answers=[GeneratedAnswer(
            key="q_reloc_essay", label=None,
            value="I am willing to relocate; my two years of backend work sit "
                  "in Python, and a settled start would help most.")])

    resolved, report = overlay_one(essay, ssot, generated=generated)
    assert resolved.values["q_reloc_essay"].startswith("I am willing to relocate")
    assert report.applied == [("q_reloc_essay", "generated:key")]
    assert resolved.skipped == []

    # Canned "Yes" would have hit the same label. With no generation, the essay
    # stays HONESTLY skipped rather than being filled with a word.
    bare, bare_report = overlay_one(essay, ssot, generated=None)
    assert bare.fields == []
    assert bare.skipped == [("q_reloc_essay", "missing:canned_answers.unknown")]
    assert bare_report.applied == []

    hear = make_field("q_hear_essay",
                      "Tell us how did you hear about us and why this role "
                      "interests you",
                      type_="input_text", norm_type="LONGTEXT", max_length=1000)
    resolved, report = overlay_one(hear, ssot)
    assert resolved.fields == []          # never "LinkedIn" in a 1000-char box
    assert report.applied == []
    assert report.unresolved == [("q_hear_essay", "missing:canned_answers.unknown")]


def test_overlay_canned_beats_generated_on_a_short_field(ssot: SSOT) -> None:
    """Precedence for ONE field that has both: on a SHORT (option-bearing) field
    the deterministic SSOT route wins over model prose. The canned ladder is not
    removed by the long-text guard, only bounded by it.

    Bounded by its KEYWORDS too: the previously-applied row does not answer "which
    team have you applied to?", a short-text question whose answer is a team name
    and which the canned "No" would have filled outright.

    And a value that survives the option match is still not filled when the form
    would truncate it: a value over the field's own `max_length` leaves the field
    skipped (`OVER_MAX_LENGTH`), never counted complete with half a sentence in
    it. The canned routes are the reason this guard lives HERE: the offline
    generator refuses its own over-cap answers, but it never sees an SSOT scalar.
    """
    fld = make_field("q_hear", "How did you hear about this role?",
                     type_="multi_value_single_select",
                     options=["LinkedIn job board", "Company website"])
    generated = GeneratedAnswers(
        vendor="greenhouse", slug="acme", job_id="12345", posting_lang="en",
        answers=[GeneratedAnswer(key="q_hear", label=None,
                                 value="Company website")])
    resolved, report = overlay_one(fld, ssot, generated=generated)
    assert resolved.values == {"q_hear": "LinkedIn job board"}
    assert report.applied == [
        ("q_hear", "canned:canned_answers.how_did_you_hear_default")]

    team = make_field("q_team", "Which team have you applied to?")
    resolved, report = overlay_one(team, ssot)
    assert resolved.fields == []            # never the previously-applied "No"
    assert report.applied == []
    assert report.unresolved == [("q_team", "missing:canned_answers.unknown")]

    capped = make_field("q_office", "Will you attend in the office?",
                        max_length=10)     # the canned answer runs to 35 characters
    resolved, report = overlay_one(capped, ssot)
    assert resolved.fields == []
    assert resolved.skipped == [("q_office", OVER_MAX_LENGTH)]
    assert report.unresolved == [("q_office", OVER_MAX_LENGTH)]
    assert report.applied == []


def test_overlay_failed_canned_route_does_not_block_the_generated_answer(
        ssot: SSOT) -> None:
    """A canned route that HITS the label but does not FIT the field is not the
    field's last word: the generated answer written for that very field is tried
    behind it.

    The canned table matches loose label SUBSTRINGS, so its one-word scalar reaches
    fields whose options it matches none of ("Yes" against "Hybrid"/"Fully remote")
    and fields whose cap it overruns. Stopping the ladder at that first hit lost
    coverage to a route that was never usable: a field with a generated answer that
    WOULD have fitted stayed skipped. Preference is untouched -- canned still wins
    wherever it fits (test_overlay_canned_beats_generated_on_a_short_field) -- and
    so is the fill: every candidate is still option-matched and length-checked, and
    nothing is guessed.
    """
    # The canned relocation route hits this label, and its "Yes" fits neither option.
    fld = make_field("q_reloc", "Are you willing to relocate?",
                     type_="multi_value_single_select",
                     options=["Hybrid", "Fully remote"])
    generated = GeneratedAnswers(
        vendor="greenhouse", slug="acme", job_id="12345", posting_lang="en",
        answers=[GeneratedAnswer(key="q_reloc", label=None, value="Fully remote")])
    resolved, report = overlay_one(fld, ssot, generated=generated)
    assert resolved.values == {"q_reloc": "Fully remote"}
    assert report.applied == [("q_reloc", "generated:key")]
    assert resolved.skipped == []
    assert report.unresolved == []

    # Same for a canned value the field's OWN cap would truncate: the shorter
    # generated answer behind it still lands.
    capped = make_field("q_office", "Will you attend in the office?", max_length=10)
    short = GeneratedAnswers(
        vendor="greenhouse", slug="acme", job_id="12345", posting_lang="en",
        answers=[GeneratedAnswer(key="q_office", label=None, value="Yes")])
    resolved, report = overlay_one(capped, ssot, generated=short)
    assert resolved.values == {"q_office": "Yes"}
    assert report.applied == [("q_office", "generated:key")]

    # With NOTHING behind it, the failed canned route still leaves the field
    # honestly skipped, with the specific reason a human can act on.
    resolved, report = overlay_one(fld, ssot)
    assert resolved.fields == []
    assert resolved.skipped == [("q_reloc", NO_OPTION_MATCH)]
    assert report.unresolved == [("q_reloc", NO_OPTION_MATCH)]


def test_overlay_essay_from_generated_by_key(ssot: SSOT) -> None:
    """The generation joins on the field key. It is also language-bound: an essay
    written for another posting language is never applied (an English essay must
    not land in an Italian posting), and the guard is the language mismatch, not a
    broken join, since the SAME generation applies once the languages agree."""
    fld = make_field("question_1", "Why do you want to work here?",
                     type_="textarea", required=True)
    generated = load_generated_answers(FIXTURES / "generated-sample.yaml")
    assert generated.posting_lang == "en"

    other, other_report = overlay_one(fld, ssot, generated=generated,
                                      posting_lang="it")
    assert other.fields == []
    assert other.skipped == [("question_1", "missing:canned_answers.unknown")]
    assert other_report.applied == []
    assert other_report.unresolved == [
        ("question_1", "missing:canned_answers.unknown")]

    resolved, report = overlay_one(fld, ssot, generated=generated)
    assert resolved.values["question_1"].startswith("Your work on distributed")
    assert report.applied == [("question_1", "generated:key")]
    assert resolved.skipped == []


def test_overlay_essay_from_generated_by_label(ssot: SSOT) -> None:
    """The vendor re-keyed the field between capture and fill: the normalized
    label is the join, and the required-marker asterisk does not defeat it."""
    fld = make_field("re_keyed_9", "What interests you about this role? *",
                     type_="textarea", required=True)
    generated = load_generated_answers(FIXTURES / "generated-sample.yaml")
    resolved, report = overlay_one(fld, ssot, generated=generated)
    assert resolved.values["re_keyed_9"].startswith("The role centres on backend")
    assert report.applied == [("re_keyed_9", "generated:label")]


def test_overlay_essay_missing_generation_pending(ssot: SSOT) -> None:
    """No generation for this posting: the essay stays honestly unfilled."""
    fld = make_field("question_1", "Why do you want to work here?",
                     type_="textarea", required=True)
    resolved, report = overlay_one(fld, ssot, generated=None)
    assert resolved.fields == []
    assert resolved.skipped == [("question_1", "missing:canned_answers.unknown")]
    assert report.unresolved == [("question_1", "missing:canned_answers.unknown")]
    assert report.tos_forbidden == []


def test_overlay_tos_forbidden_justified_skip(ssot: SSOT) -> None:
    """A ToS-forbidden question is never filled, and never hidden either. Neither
    is a field policy never auto-answers (COMPLIANCE_EEOC / DEMOGRAPHIC /
    VOLUNTARY, or `decline_allowed`): a stale generation carrying an answer for
    one is ignored, and the field keeps its ORIGINAL skip reason (its own
    justified-skip route through the gate), so it can never land in `fields` and
    inflate the completeness numerator. That guard sits at step 0, ahead of the
    canned ladder, so a canned SSOT route that DOES hit the label of a
    policy-declined field still fills nothing."""
    fld = make_field("q_ai", "AI Policy for Application", type_="textarea",
                     required=True)
    generated = load_generated_answers(FIXTURES / "generated-sample.yaml")
    resolved, report = overlay_one(fld, ssot, generated=generated)
    assert resolved.fields == []
    assert resolved.skipped == [("q_ai", "missing:canned_answers.unknown")]
    assert report.tos_forbidden == ["q_ai"]
    assert report.applied == []

    stale = GeneratedAnswers(
        vendor="greenhouse", slug="acme", job_id="12345", posting_lang="en",
        answers=[GeneratedAnswer(key="q_gender", label="Gender", value="Male")])
    for section, decline_allowed in [("COMPLIANCE_EEOC", False),
                                     ("DEMOGRAPHIC", False),
                                     ("VOLUNTARY", False),
                                     ("STANDARD", True)]:
        declined = make_field("q_gender", "Gender",
                              type_="multi_value_single_select",
                              options=["Male", "Female",
                                       "Decline to self identify"],
                              section=section, decline_allowed=decline_allowed)
        resolved, report = overlay_one(declined, ssot, generated=stale)
        assert resolved.fields == [], section
        assert resolved.skipped == [
            ("q_gender", "missing:canned_answers.unknown")], section
        assert report.applied == [], section
        assert report.unresolved == [], section
        assert report.tos_forbidden == [], section

    reloc = make_field("q_reloc", "Are you willing to relocate?", type_="yes_no",
                       options=["Yes, I am willing to relocate",
                                "No, I am not willing to relocate"],
                       section="VOLUNTARY")
    resolved, report = overlay_one(reloc, ssot)
    assert resolved.fields == []
    assert resolved.skipped == [("q_reloc", "missing:canned_answers.unknown")]
    assert report.applied == []


def test_overlay_never_overwrites_filled_fields(ssot: SSOT) -> None:
    """A field the kernel already resolved is untouchable, even when a canned
    route would have hit its label."""
    fld = make_field("q_hear", "How did you hear about this role?")
    kernel_value = FieldValue(key="q_hear", label=fld.label, type=fld.type,
                              locator=fld.locator, value="A friend told me")
    resolved = ResolvedValues(fields=[kernel_value], skipped=[])
    report = apply_content_overlay(resolved, make_map(fld), ssot)
    assert resolved.fields == [kernel_value]
    assert resolved.values == {"q_hear": "A friend told me"}
    assert report.applied == []


def test_overlay_fields_skipped_bookkeeping_consistent(ssot: SSOT) -> None:
    """Applied fields leave `skipped` exactly once; unresolved and ToS-forbidden
    fields stay there exactly once. No duplicate, no leak."""
    applied = make_field("q_hear", "How did you hear about this role?")
    unresolved = make_field("q_other", "Describe a project you are proud of",
                            type_="textarea")
    forbidden = make_field("q_ai", "AI Policy for Application", type_="textarea")
    upload = make_field("q_cv", "Upload your CV", type_="input_file")
    fieldmap = make_map(applied, unresolved, forbidden, upload)
    resolved = ResolvedValues(fields=[], skipped=[
        (applied.key, "missing:canned_answers.unknown"),
        (unresolved.key, "missing:canned_answers.unknown"),
        (forbidden.key, "missing:canned_answers.unknown"),
        (upload.key, "asset missing: cv-ats"),
    ])
    generated = GeneratedAnswers(
        vendor="greenhouse", slug="acme", job_id="12345", posting_lang="en",
        answers=[],
        tos_forbidden=[TosForbidden(label="AI Policy for Application",
                                    reason="employer requires human authorship")])

    report = apply_content_overlay(resolved, fieldmap, ssot, generated=generated)

    filled_keys = [fv.key for fv in resolved.fields]
    skipped_keys = [key for key, _ in resolved.skipped]
    assert filled_keys == ["q_hear"]
    assert skipped_keys == ["q_other", "q_ai", "q_cv"]
    assert not set(filled_keys) & set(skipped_keys)
    assert len(filled_keys) + len(skipped_keys) == 4
    assert [key for key, _ in report.applied] == ["q_hear"]
    assert report.tos_forbidden == ["q_ai"]
    assert [key for key, _ in report.unresolved] == ["q_other"]


MALFORMED_ANSWER_DOCS = [
    ("top-level-not-a-mapping", "not a mapping"),
    ("unknown-schema-version",
     "schema_version: '9'\nvendor: greenhouse\nslug: acme\n"
     "job_id: '1'\nanswers: []"),
    ("missing-vendor",
     "schema_version: '1'\nslug: acme\njob_id: '1'\nanswers: []"),
    ("answer-without-key-or-label",
     "schema_version: '1'\nvendor: greenhouse\nslug: acme\n"
     "job_id: '1'\nanswers:\n  - value: neither key nor label"),
    ("answer-with-empty-value",
     "schema_version: '1'\nvendor: greenhouse\nslug: acme\n"
     "job_id: '1'\nanswers:\n  - key: q1\n    value: '   '"),
    ("tos-forbidden-without-label",
     "schema_version: '1'\nvendor: greenhouse\nslug: acme\n"
     "job_id: '1'\nanswers: []\ntos_forbidden:\n  - reason: no label"),
]


def test_generated_answers_loader_rejects_malformed(tmp_path) -> None:
    """Every malformed shape raises, loudly. A corrupt answers file must stop the
    loop, never degrade into "no answers" (which would read as a justified skip
    and quietly lower completeness). The case name travels into the failure, so a
    regression names the shape it broke on. The well-formed sample still loads at
    the end: the loader is strict, not broken."""
    for case, document in MALFORMED_ANSWER_DOCS:
        path = tmp_path / "generated.yaml"
        path.write_text(document)
        try:
            load_generated_answers(path)
        except ContentSchemaError:
            continue
        raise AssertionError(f"malformed document accepted: {case}")

    good = load_generated_answers(FIXTURES / "generated-sample.yaml")
    assert good.vendor == "greenhouse"
    assert [a.key for a in good.answers] == ["question_1", None]
    assert good.tos_forbidden[0].reason == "employer requires human-authored response"


BANNED_IMPORTS = ("engine.providers", "engine.fetch", "engine.run", "subprocess",
                  "socket", "requests", "urllib", "http.client", "playwright")


def import_names(tree: ast.AST, *, deferred: bool) -> list[str]:
    """The modules an AST imports. `deferred=False` reports only the IMPORT-TIME
    imports (module level, class bodies included), skipping function bodies, whose
    imports Python does not execute until the function is called."""
    names: list[str] = []
    stack = list(ast.iter_child_nodes(tree))
    while stack:
        node = stack.pop()
        if not deferred and isinstance(node, (ast.FunctionDef,
                                              ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names += [base] + [f"{base}.{alias.name}" for alias in node.names]
        else:
            stack += list(ast.iter_child_nodes(node))
    return names


def assert_no_banned_import(module: str, names: list[str]) -> None:
    for name in names:
        assert not any(name == bad or name.startswith(bad + ".")
                       for bad in BANNED_IMPORTS), \
            f"{module} imports {name}: content channel must stay pure"


def test_content_module_import_purity() -> None:
    """`engine.content` reaches no vendor plugin, no subprocess, and no network,
    transitively: the engine's content channel stays deterministic and offline.
    Asserted on the import graph (AST), so it cannot be defeated by another test
    having already imported a provider into `sys.modules`.

    The walk covers the graph Python ACTUALLY EXECUTES on `import engine.content`,
    which is why it is seeded with every ANCESTOR PACKAGE too: `engine/__init__.py`
    runs first and eagerly imports config, ssot, store, match, queue_sm,
    questionnaire and notify, none of which the old key-only seed ever parsed. A
    network transport growing inside `engine.notify` would have slipped straight
    past this guard.

    Two rules, deliberately different:

    * the TRANSITIVE walk follows IMPORT-TIME imports only. A function-body import
      is not executed by `import engine.content` (that is exactly what keeps
      `engine/__init__`'s PEP 562 lazy `engine.run` re-export, and `engine.notify`'s
      in-function `urllib.request`, out of the executed graph);
    * `engine.content` ITSELF is held to the stricter rule: EVERY import in it,
      deferred ones included, must be clean, so no in-function escape hatch can
      hide a subprocess or a socket inside the content channel.
    """
    import engine.content

    engine_root = Path(engine.content.__file__).resolve().parent
    assert_no_banned_import("engine.content", import_names(
        ast.parse(Path(engine.content.__file__).read_text()), deferred=True))

    seen: set[str] = set()
    queue: list[str] = []

    def enqueue(module: str) -> None:
        # ...and every ancestor package: importing `engine.kernel.ssot` executes
        # `engine/__init__.py` and `engine/kernel/__init__.py` on the way.
        parts = module.split(".")
        queue.extend(".".join(parts[:i]) for i in range(1, len(parts) + 1))

    enqueue("engine.content")
    while queue:
        module = queue.pop()
        if module in seen or module.split(".")[0] != "engine":
            continue
        seen.add(module)
        base = engine_root.joinpath(*module.split(".")[1:])
        sources = [base / "__init__.py"] + ([base.with_suffix(".py")]
                                            if module != "engine" else [])
        for pyfile in (path for path in sources if path.exists()):
            names = import_names(
                ast.parse(pyfile.read_text(), filename=str(pyfile)),
                deferred=False)
            assert_no_banned_import(module, names)
            for name in names:
                if name.startswith("engine."):
                    enqueue(name)

    # The walk really traversed the EXECUTED graph: the package __init__ and its
    # eager siblings, not just the kernel modules content.py names itself.
    assert {"engine", "engine.notify", "engine.store",
            "engine.kernel.ssot"} <= seen

    # And GeneratedAnswer stays a plain data carrier the vendor loops can build.
    assert GeneratedAnswer(key="k", label=None, value="v").value == "v"
