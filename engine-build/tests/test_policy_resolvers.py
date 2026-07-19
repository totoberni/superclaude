"""SSOT policy resolvers (W5.1d; owner rulings 12, 13, 14).

Three policies no seeded string can express: the start date (computed at fire
time, in the order the CONTROL declares), the nearest of the owner's cities to
the POSTING, and a referral answer given only when the form REQUIRES one.

FAKE data only. The SSOT is built in memory (`make_ssot`), so no owner value is
ever copied into the suite; the two candidate cities come FROM the fake SSOT,
which is also what proves the resolver reads them rather than carrying them.

Every resolver is asserted on BOTH sides of its bookkeeping: a value that lands
in `fields` must also leave `skipped`, and a refusal must leave the field empty
AND be reported. The negatives are the point of the file: an underivable date
order, an unplaceable posting, an optional referral box, and the cross cases
where the CONTROL disagrees with its own LABEL.
"""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from engine import content
from engine.content import (
    CITY_CHOICE_NOT_DECISIVE,
    LOCATION_NOT_PLACEABLE,
    MECHANISM_NATIVE_DATE,
    MECHANISM_PICKER_ONLY,
    MECHANISM_PLAIN_TEXT,
    MECHANISM_TEXT_ENTRY,
    MECHANISM_UNPROBED,
    MISROUTE_WARNING,
    NO_DATE_FORMAT,
    NO_DISCOVERY_SOURCE,
    NO_OPTION_MATCH,
    NO_POSTING_LOCATION,
    PICKER_NOT_DRIVEN,
    POLICY_DATE_KEYWORDS,
    REFERRAL_NOT_VOLUNTEERED,
    REFERRAL_PERSON_PATH,
    REFERRAL_WANTS_A_PERSON,
    ContentSchemaError,
    DateControl,
    GeneratedAnswer,
    GeneratedAnswers,
    apply_content_overlay,
    date_format_from_placeholder,
    is_supported_date_format,
    load_generated_answers,
    nearest_city,
)
from engine.kernel.contracts import (
    Field,
    FieldMap,
    FieldValue,
    Locator,
    ResolvedValues,
)
from engine.kernel.resolve import resolve_values
from engine.kernel.ssot import SSOT

TOOL_PATH = Path(__file__).parent.parent / "bin" / "generate_answers.py"

# The FAKE notice string. Deliberately NOT the owner's own wording: the resolver
# must ROUTE to the SSOT, so a test that hardcodes the real string would pass just
# as well against a resolver that hardcoded it too.
FAKE_NOTICE = "None: free to start at once."


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("generate_answers_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_ssot(*, notice: str | None = FAKE_NOTICE,
              cities: list[str] | None = None,
              referral_person: str | None = None) -> SSOT:
    data: dict = {"canned_answers": {}, "preferences": {"location_policy": {}}}
    if notice is not None:
        data["canned_answers"]["notice_period"] = notice
    if cities is not None:
        data["preferences"]["location_policy"]["allowed_cities"] = list(cities)
    if referral_person is not None:
        data["canned_answers"]["referral_person_if_required"] = referral_person
    return SSOT(data)


def make_field(key: str, label: str, *, type_: str = "input_text",
               options: list[str] | None = None, required: bool = False,
               norm_type: str = "", max_length: int | None = None) -> Field:
    return Field(key=key, label=label, type=type_, required=required,
                 options=list(options or []), source="test",
                 locator=Locator(role="textbox", name=label),
                 norm_type=norm_type, max_length=max_length)


def make_map(*fields: Field) -> FieldMap:
    return FieldMap(vendor="workable", posting_id="ABC123",
                    captured_at="2026-07-14T00:00:00Z", fields=list(fields))


def make_generated(*, location: str = "", source: str = "",
                   controls: list[DateControl] | None = None) -> GeneratedAnswers:
    return GeneratedAnswers(vendor="workable", slug="acme", job_id="ABC123",
                            posting_lang="en", posting_location=location,
                            discovery_source=source,
                            date_controls=list(controls or []))


def overlay_one(fld: Field, ssot: SSOT, *, generated: GeneratedAnswers | None = None,
                reason: str = "missing:canned_answers.unknown"):
    """Run the overlay over a one-field map whose only field the kernel skipped."""
    resolved = ResolvedValues(fields=[], skipped=[(fld.key, reason)])
    report = apply_content_overlay(resolved, make_map(fld), ssot, generated=generated)
    return resolved, report


# -- R1: start date and notice (ruling 12) ------------------------------------

def test_start_date_fills_today_in_the_order_the_control_declares() -> None:
    """The LIVE workable shape: schema says `date`, the DOM box declares DD/MM/YYYY."""
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE",
                     required=True)
    generated = make_generated(controls=[
        DateControl(key="QA_1", mechanism=MECHANISM_TEXT_ENTRY,
                    date_format="%d/%m/%Y", evidence="placeholder=DD/MM/YYYY")])
    resolved, report = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {"QA_1": date.today().strftime("%d/%m/%Y")}
    assert report.applied == [("QA_1", "policy:start_date:%d/%m/%Y")]
    assert resolved.skipped == []


def test_start_date_order_follows_the_control_never_a_default() -> None:
    """The SAME field, a control declaring the US order, must answer in THAT order.

    The mutant this kills is the whole point of the wave: a hardcoded order types
    05/08 into a box that meant 08/05, six months wrong, with total confidence.
    """
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")
    generated = make_generated(controls=[
        DateControl(key="QA_1", mechanism=MECHANISM_TEXT_ENTRY,
                    date_format="%m/%d/%Y", evidence="placeholder=MM/DD/YYYY")])
    resolved, _ = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {"QA_1": date.today().strftime("%m/%d/%Y")}
    assert resolved.values != {"QA_1": date.today().strftime("%d/%m/%Y")} or (
        date.today().day == date.today().month)


def clock_at(day: date):
    """A `date` class whose `today()` is `day`. The clock is replaced AT ITS SOURCE,
    not by stubbing `_today` itself: stubbing the resolver's own accessor cannot
    tell a resolver that READS THE CLOCK from one that returns a SEEDED LITERAL,
    because the stub replaces both. A seeded literal is the single most dangerous
    thing this wave could ship (it is right on the day it is written and wrong on
    every day after), so the test that forbids it must be able to SEE it.
    """
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return day

    return FrozenDate


def test_start_date_is_computed_at_fire_time_not_seeded(monkeypatch) -> None:
    """Two different 'todays' must produce two different answers, through the REAL
    `_today`."""
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")
    generated = make_generated(controls=[
        DateControl(key="QA_1", mechanism=MECHANISM_TEXT_ENTRY,
                    date_format="%d/%m/%Y")])

    monkeypatch.setattr(content, "date", clock_at(date(2026, 5, 8)))
    first, _ = overlay_one(fld, make_ssot(), generated=generated)
    monkeypatch.setattr(content, "date", clock_at(date(2026, 8, 5)))
    second, _ = overlay_one(fld, make_ssot(), generated=generated)

    assert first.values == {"QA_1": "08/05/2026"}
    assert second.values == {"QA_1": "05/08/2026"}


def test_the_answer_reads_the_system_clock(monkeypatch) -> None:
    """`_today` reads the clock. Pinned INDEPENDENTLY OF WHAT DAY IT IS: a literal
    seeded with today's date passes every assertion made against `date.today()`,
    and starts lying tomorrow."""
    monkeypatch.setattr(content, "date", clock_at(date(1999, 12, 31)))
    assert content._today() == date(1999, 12, 31)


def test_native_date_input_fills_iso() -> None:
    """An `input[type=date]` carries an ISO value by the HTML standard, whatever it
    displays."""
    fld = make_field("start", "Start date", type_="date", norm_type="DATE")
    generated = make_generated(controls=[
        DateControl(key="start", mechanism=MECHANISM_NATIVE_DATE,
                    date_format="%Y-%m-%d", evidence="input[type=date]")])
    resolved, _ = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {"start": date.today().strftime("%Y-%m-%d")}


def test_undderivable_date_order_is_not_filled_and_is_reported() -> None:
    """FAIL CLOSED. A date control whose order nothing declared stays EMPTY."""
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE",
                     required=True)
    generated = make_generated(controls=[
        DateControl(key="QA_1", mechanism=MECHANISM_TEXT_ENTRY, date_format=None,
                    evidence="date control declaring no order")])
    resolved, report = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {}
    assert resolved.skipped == [("QA_1", NO_DATE_FORMAT)]
    assert report.unresolved == [("QA_1", NO_DATE_FORMAT)]
    assert report.applied == []


def test_unprobed_date_control_is_not_filled() -> None:
    """No probe at all (no generated document, or a control the page never showed)
    is not a licence to guess an order."""
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")
    resolved, report = overlay_one(fld, make_ssot(), generated=None)

    assert resolved.values == {}
    assert report.unresolved == [("QA_1", NO_DATE_FORMAT)]


def test_click_only_date_picker_is_not_filled_and_names_the_click_wave() -> None:
    """A readonly calendar widget is set by a CLICK, and the content channel never
    drives the page. Reported by name for W5.1c, never typed into blind."""
    fld = make_field("start", "Start date", type_="date", norm_type="DATE")
    generated = make_generated(controls=[
        DateControl(key="start", mechanism=MECHANISM_PICKER_ONLY, date_format=None,
                    evidence="readonly input (calendar widget)")])
    resolved, report = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {}
    assert report.unresolved == [("start", PICKER_NOT_DRIVEN)]
    assert "W5.1c" in PICKER_NOT_DRIVEN


def test_text_control_labelled_start_date_answers_with_zero_notice() -> None:
    """CROSS CASE (ruling 12): the label asks for a date, the CONTROL is a plain
    text box, so the answer is the owner's (zero) notice, read from the SSOT."""
    fld = make_field("start", "When can you start?", type_="input_text")
    generated = make_generated(controls=[
        DateControl(key="start", mechanism=MECHANISM_PLAIN_TEXT, date_format=None,
                    evidence="input[type=text] with no date affordance")])
    resolved, report = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {"start": FAKE_NOTICE}
    assert report.applied == [("start", "canned:canned_answers.notice_period")]


def test_date_control_labelled_notice_answers_with_a_date() -> None:
    """CROSS CASE, the other way: the label says "notice period", the CONTROL is a
    date box, so it gets a DATE.

    NOTE the field is presented here as SKIPPED. Live it is not: the frozen kernel
    matches "notice" on the LABEL (`kernel/resolve.py:123`) and fills the date box
    with prose before the overlay sees it. That defect is pinned by
    `test_kernel_misroutes_a_date_control_labelled_notice` and escalated; this test
    pins what the resolver does with the field once it is given it.
    """
    fld = make_field("notice", "Notice period", type_="date", norm_type="DATE")
    generated = make_generated(controls=[
        DateControl(key="notice", mechanism=MECHANISM_TEXT_ENTRY,
                    date_format="%d/%m/%Y", evidence="placeholder=DD/MM/YYYY")])
    resolved, _ = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {"notice": date.today().strftime("%d/%m/%Y")}
    assert FAKE_NOTICE not in resolved.values.values()


def test_notice_duration_lands_on_the_matching_option() -> None:
    """A notice SELECT takes the option its own list carries, not the SSOT prose."""
    fld = make_field("notice", "What is your notice period?", type_="yes_no",
                     options=["Immediately", "1 month", "3 months"])
    resolved, _ = overlay_one(fld, make_ssot(notice="None: available immediately"),
                              generated=make_generated())

    assert resolved.values == {"notice": "Immediately"}


def test_start_date_never_routes_to_the_superseded_ssot_datum() -> None:
    """Ruling 12 SUPERSEDES `canned_answers.earliest_start_date` for a start-date
    CONTROL. The owner's datum stays in the SSOT; it is simply not this route."""
    ssot = SSOT({"canned_answers": {"notice_period": FAKE_NOTICE,
                                    "earliest_start_date": "2026-08-20 preferred"}})
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")
    generated = make_generated(controls=[
        DateControl(key="QA_1", mechanism=MECHANISM_TEXT_ENTRY,
                    date_format="%d/%m/%Y")])
    resolved, _ = overlay_one(fld, ssot, generated=generated)

    assert resolved.values == {"QA_1": date.today().strftime("%d/%m/%Y")}
    assert "2026-08-20" not in str(resolved.values)


def test_kernel_misroutes_a_date_control_labelled_notice(caplog) -> None:
    """The FROZEN kernel, run for real, types PROSE into a DATE box.

    Evidence for the escalation, not a fix: the kernel's matcher keys on the LABEL
    (`resolve.py:123`) and renders free text (`resolve.py:672-675`), so the field
    lands in `resolved.fields` and the additive-only overlay may not touch it. The
    overlay REPORTS it by name instead. Deleting this test hides a live defect.
    """
    ssot = make_ssot()
    fld = make_field("notice", "Notice period", type_="date", norm_type="DATE",
                     required=True)
    fieldmap = make_map(fld)

    resolved = resolve_values(fieldmap, ssot, {})
    assert resolved.values == {"notice": FAKE_NOTICE}, "kernel behaviour changed"

    report = apply_content_overlay(resolved, fieldmap, ssot,
                                   generated=make_generated(controls=[
                                       DateControl(key="notice",
                                                   mechanism=MECHANISM_TEXT_ENTRY,
                                                   date_format="%d/%m/%Y")]))

    assert report.misrouted == [
        ("notice", "kernel filled a date control with a non-date value")]
    assert resolved.values == {"notice": FAKE_NOTICE}, "overlay must not overwrite"


def test_a_real_date_the_kernel_filled_is_not_reported_as_misrouted() -> None:
    """The detector must not cry wolf: a date box carrying a DATE is fine."""
    fld = make_field("start", "Start date", type_="date", norm_type="DATE")
    resolved = ResolvedValues(
        fields=[FieldValue(key="start", label="Start date", type="date",
                           locator=fld.locator, value="01/02/2026")],
        skipped=[])
    report = apply_content_overlay(resolved, make_map(fld), make_ssot(),
                                   generated=make_generated(controls=[
                                       DateControl(key="start",
                                                   mechanism=MECHANISM_TEXT_ENTRY,
                                                   date_format="%d/%m/%Y")]))

    assert report.misrouted == []


# -- date format derivation ---------------------------------------------------

@pytest.mark.parametrize("placeholder,expected", [
    ("DD/MM/YYYY", "%d/%m/%Y"),
    ("MM/DD/YYYY", "%m/%d/%Y"),
    ("YYYY-MM-DD", "%Y-%m-%d"),
    ("dd.mm.yyyy", "%d.%m.%Y"),
    ("DD/MM/YY", "%d/%m/%y"),
])
def test_date_format_is_derived_from_the_placeholder(placeholder, expected) -> None:
    assert date_format_from_placeholder(placeholder) == expected


@pytest.mark.parametrize("placeholder", [
    "", "Date", "Select a date", "Month DD, YYYY", "MM/YYYY", "DD/MM", "gg/mm/aaaa",
])
def test_an_underivable_placeholder_yields_no_format(placeholder) -> None:
    """Not derivable is not a licence to fall back on a default."""
    assert date_format_from_placeholder(placeholder) is None


@pytest.mark.parametrize("fmt,ok", [
    ("%d/%m/%Y", True), ("%m/%d/%Y", True), ("%Y-%m-%d", True),
    ("%c", False), ("%x", False), ("%Y", False), ("%d/%m", False),
    ("%d/%d/%Y", False), ("rm -rf /", False),
])
def test_only_a_recognized_date_format_is_accepted(fmt, ok) -> None:
    assert is_supported_date_format(fmt) is ok


# -- R2: nearest city (ruling 13) ---------------------------------------------

def test_city_control_answers_with_the_nearest_owner_city_to_the_posting() -> None:
    """The LIVE seal target: movement-labs is in Atlanta, and Milan is 200 km
    nearer to Atlanta than Bologna is."""
    fld = make_field("QA_2", "Please specify your city", required=True,
                     max_length=127)
    generated = make_generated(location="Atlanta, Georgia, United States")
    resolved, report = overlay_one(fld, make_ssot(cities=["Milan", "Bologna"]),
                                   generated=generated)

    assert resolved.values == {"QA_2": "Milan"}
    assert report.applied == [
        ("QA_2", "policy:nearest_city:preferences.location_policy.allowed_cities")]
    assert resolved.skipped == []


def test_the_city_flips_with_the_posting() -> None:
    """The mutant this kills is a hardcoded city: the SAME control, a different
    posting, must answer differently."""
    fld = make_field("QA_2", "Please specify your city")
    ssot = make_ssot(cities=["Milan", "Bologna"])

    north, _ = overlay_one(fld, ssot, generated=make_generated(location="Turin, Italy"))
    south, _ = overlay_one(fld, ssot, generated=make_generated(location="Rome, Italy"))

    assert north.values == {"QA_2": "Milan"}
    assert south.values == {"QA_2": "Bologna"}


def test_the_candidate_cities_come_from_the_ssot_not_from_code() -> None:
    """No owner data is carried in this module: change the SSOT's cities and the
    answer changes with them."""
    fld = make_field("QA_2", "Please specify your city")
    resolved, _ = overlay_one(fld, make_ssot(cities=["Berlin", "Madrid"]),
                              generated=make_generated(location="Lisbon, Portugal"))

    assert resolved.values == {"QA_2": "Madrid"}


def test_absent_posting_location_is_not_guessed() -> None:
    fld = make_field("QA_2", "Please specify your city", required=True)
    resolved, report = overlay_one(fld, make_ssot(cities=["Milan", "Bologna"]),
                                   generated=make_generated(location=""))

    assert resolved.values == {}
    assert report.unresolved == [("QA_2", NO_POSTING_LOCATION)]


def test_unplaceable_posting_location_is_not_guessed() -> None:
    fld = make_field("QA_2", "Please specify your city", required=True)
    resolved, report = overlay_one(fld, make_ssot(cities=["Milan", "Bologna"]),
                                   generated=make_generated(location="Remote"))

    assert resolved.values == {}
    assert report.unresolved == [("QA_2", LOCATION_NOT_PLACEABLE)]


def test_a_location_too_coarse_to_decide_is_not_decided() -> None:
    """"Italy" is a real place and still cannot answer this: the nearer city
    genuinely depends on WHICH Italian city. A centroid would answer it anyway,
    which is a coin flip wearing a computation's clothes."""
    fld = make_field("QA_2", "Please specify your city", required=True)
    resolved, report = overlay_one(fld, make_ssot(cities=["Milan", "Bologna"]),
                                   generated=make_generated(location="Italy"))

    assert resolved.values == {}
    assert report.unresolved == [("QA_2", CITY_CHOICE_NOT_DECISIVE)]


def test_no_city_is_answered_without_a_generated_document() -> None:
    fld = make_field("QA_2", "Please specify your city")
    resolved, report = overlay_one(fld, make_ssot(cities=["Milan", "Bologna"]),
                                   generated=None)

    assert resolved.values == {}
    assert report.unresolved == [("QA_2", NO_POSTING_LOCATION)]


def test_the_owner_city_needs_seeded_coordinates() -> None:
    """A candidate city this module cannot place is a GAP, never a fallback onto
    the other one."""
    city, reason = nearest_city("Rome, Italy", ["Atlantis"])
    assert city is None
    assert "no coordinates" in reason


def test_a_work_arrangement_word_is_not_a_place() -> None:
    assert nearest_city("Remote", ["Milan", "Bologna"])[0] is None
    assert nearest_city("Milan, Italy (Remote)", ["Milan", "Bologna"])[0] == "Milan"


# -- R3: conditional referral (ruling 14) -------------------------------------

def test_required_referral_answers_with_the_class_of_source() -> None:
    """The source class is stated to the ONE control that asks for it: a REQUIRED
    control that OFFERS the class as an option.

    The option list IS the evidence that the box wants a source class rather than a
    person, and it is the only such evidence a fieldmap carries. This test formerly
    ran on an option-less box and asserted "Vendor" landed in it; that box was the
    live QA_12042640, whose question is "who were you referred by?" -- a PERSON --
    and the assertion pinned a lie sent to an employer. It is now pinned as a gap by
    `test_a_required_referral_box_demanding_a_person_is_a_gap_not_a_source_token`.
    """
    fld = make_field("QA_3", "How were you referred to this role?", required=True,
                     type_="yes_no",
                     options=["Vendor", "LinkedIn", "Other", "Employee referral"])
    resolved, report = overlay_one(fld, make_ssot(),
                                   generated=make_generated(source="workable"))

    assert resolved.values == {"QA_3": "Vendor"}
    assert report.applied == [("QA_3", "policy:referral:workable")]


def test_an_optional_referral_box_is_left_alone() -> None:
    """Owner ruling 14, verbatim: "else: do nothing". The box is left EMPTY.

    Left alone means OWNED-and-suppressed, never DISOWNED. A disowned field is not
    an untouched one: it falls straight through to the canned/generated ladder
    behind the policy, which is how an optional referral box came to be filled at
    all (see `test_an_optional_referral_essay_box_is_never_volunteered_into`).
    Owning it and returning no value is what stops the ladder; the reason is
    recorded so the suppression is visible rather than silent.
    """
    fld = make_field("QA_3", "Who referred you?", required=False)
    resolved, report = overlay_one(fld, make_ssot(),
                                   generated=make_generated(source="workable"))

    assert resolved.values == {}
    assert report.applied == []
    assert resolved.skipped == [("QA_3", REFERRAL_NOT_VOLUNTEERED)]
    assert report.unresolved == [("QA_3", REFERRAL_NOT_VOLUNTEERED)]


def test_referral_never_states_a_name() -> None:
    """A person's name can never come out of this resolver, and neither can a
    source-class token typed into a question that asked for a person.

    Both shapes of the same rule: an option-bearing control gets the CLASS (never a
    name, whatever the source string carries), and an option-less one that demands a
    person gets NOTHING.
    """
    select = make_field("QA_3", "Who referred you?", required=True, type_="yes_no",
                        options=["Vendor", "LinkedIn", "Other"])
    resolved, _ = overlay_one(select, make_ssot(),
                              generated=make_generated(source="a friend of mine"))
    assert resolved.values == {"QA_3": "Other"}
    assert set(resolved.values.values()) <= {"Vendor", "LinkedIn", "Other"}

    box = make_field("QA_4", "Who referred you?", required=True, max_length=127)
    resolved, report = overlay_one(box, make_ssot(),
                                   generated=make_generated(source="a friend of mine"))
    assert resolved.values == {}
    assert report.unresolved == [("QA_4", REFERRAL_WANTS_A_PERSON)]


def test_referral_maps_a_linkedin_source() -> None:
    fld = make_field("QA_3", "Were you referred by anyone?", required=True,
                     type_="yes_no", options=["Vendor", "LinkedIn", "Other"])
    resolved, _ = overlay_one(fld, make_ssot(),
                              generated=make_generated(source="LinkedIn"))

    assert resolved.values == {"QA_3": "LinkedIn"}


def test_an_unknown_discovery_source_is_not_guessed() -> None:
    fld = make_field("QA_3", "Who referred you?", required=True, type_="yes_no",
                     options=["Vendor", "LinkedIn", "Other"])
    resolved, report = overlay_one(fld, make_ssot(), generated=make_generated())

    assert resolved.values == {}
    assert report.unresolved == [("QA_3", NO_DISCOVERY_SOURCE)]


def test_required_referral_select_lands_on_an_option() -> None:
    fld = make_field("QA_3", "Referral source", required=True, type_="yes_no",
                     options=["Vendor", "LinkedIn", "Other"])
    resolved, _ = overlay_one(fld, make_ssot(),
                              generated=make_generated(source="lever"))

    assert resolved.values == {"QA_3": "Vendor"}


def test_a_required_referral_box_demanding_a_person_is_a_gap_not_a_source_token() -> None:
    """BLOCKING-2, the live field, pinned: QA_12042640 on movement-labs 0F5F662A46 --
    "If you were referred to apply to this role, who were you referred by?",
    required, `options=[]`, `max_length=127`. It asks for a PERSON.

    The whole pipeline typed `Vendor` into it: a source-class token answering a
    who-question, asserting to a real employer a referral that never happened. The
    control -- not the label -- says what the box wants, and a control with NO
    OPTIONS wants prose about a person. So it stays EMPTY and is reported by name.
    A gap is a question the owner answers; a wrong value is a lie already sent.
    """
    fld = make_field("QA_12042640",
                     "If you were referred to apply to this role, who were you "
                     "referred by?", required=True, max_length=127)
    resolved, report = overlay_one(fld, make_ssot(),
                                   generated=make_generated(source="workable"))

    assert resolved.values == {}
    assert "Vendor" not in str(resolved.values)
    assert resolved.skipped == [("QA_12042640", REFERRAL_WANTS_A_PERSON)]
    assert report.unresolved == [("QA_12042640", REFERRAL_WANTS_A_PERSON)]
    assert report.applied == []


def test_a_seeded_referral_person_fills_the_same_box_ruling_17a() -> None:
    """Ruling 17a: the SAME control as the gap test above, but the owner has
    seeded `REFERRAL_PERSON_PATH`. The seeded literal is consumed verbatim, never
    composed, and the field is no longer a gap."""
    fld = make_field("QA_12042640",
                     "If you were referred to apply to this role, who were you "
                     "referred by?", required=True, max_length=127)
    ssot = make_ssot(referral_person="Jordan Smith, a former colleague")
    resolved, report = overlay_one(fld, ssot,
                                   generated=make_generated(source="workable"))

    assert resolved.values == {"QA_12042640": "Jordan Smith, a former colleague"}
    assert report.applied == [("QA_12042640", f"canned:{REFERRAL_PERSON_PATH}")]
    assert resolved.skipped == []


def test_an_unseeded_referral_person_stays_the_gap_ruling_17a() -> None:
    """The other direction, pinned alongside the seeded case: with no seed at
    `REFERRAL_PERSON_PATH`, the gap behaviour from
    `test_a_required_referral_box_demanding_a_person_is_a_gap_not_a_source_token`
    is unchanged. Nothing is fabricated in its absence."""
    fld = make_field("QA_12042640",
                     "If you were referred to apply to this role, who were you "
                     "referred by?", required=True, max_length=127)
    resolved, report = overlay_one(fld, make_ssot(),
                                   generated=make_generated(source="workable"))

    assert resolved.values == {}
    assert resolved.skipped == [("QA_12042640", REFERRAL_WANTS_A_PERSON)]
    assert report.unresolved == [("QA_12042640", REFERRAL_WANTS_A_PERSON)]
    assert report.applied == []


def test_an_optional_referral_essay_box_is_never_volunteered_into() -> None:
    """BLOCKING-1 pinned: an OPTIONAL, essay-shaped referral box for which the
    generator wrote an answer.

    On branch code the resolver DISOWNED the field, it fell through to the generated
    ladder, and an LLM-authored "A member of your engineering team referred me."
    was typed into a box the form never required -- a relationship that DOES NOT
    EXIST, volunteered. The box must come out EMPTY, whatever any downstream route
    holds for it.
    """
    fld = make_field("ref", "If you were referred, who referred you and how do you "
                            "know them?", type_="textarea", norm_type="LONGTEXT",
                     required=False, max_length=800)
    generated = GeneratedAnswers(
        vendor="workable", slug="acme", job_id="ABC123", posting_lang="en",
        discovery_source="workable",
        answers=[GeneratedAnswer(
            key="ref", label=None,
            value="A member of your engineering team referred me.")])

    resolved, report = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {}
    assert "referred me" not in str(resolved.values)
    assert report.applied == []
    assert resolved.skipped == [("ref", REFERRAL_NOT_VOLUNTEERED)]
    assert report.unresolved == [("ref", REFERRAL_NOT_VOLUNTEERED)]


def test_a_referral_question_never_reaches_a_model(tool) -> None:
    """The other half of BLOCKING-1: the fabrication is never WRITTEN either.

    A referral is a FACT about the owner, not a thing to compose, so the offline
    generator does not send a referral question to a model at all. The essay box
    beside it still goes (the exclusion is the referral family, not the shape).
    """
    referral = make_field("ref", "If you were referred, who referred you?",
                          type_="textarea", norm_type="LONGTEXT", max_length=800)
    essay = make_field("why", "Why do you want to work here?", type_="textarea",
                       norm_type="LONGTEXT", max_length=800)

    questions = tool.questions_from_fieldmap(make_map(referral, essay))

    assert [q["key"] for q in questions] == ["why"]


# -- schema (the posting context rides on the generated document) -------------

def test_the_generated_document_carries_the_posting_context(tmp_path) -> None:
    path = tmp_path / "answers.yaml"
    path.write_text(
        "schema_version: '1'\nvendor: workable\nslug: acme\njob_id: ABC123\n"
        "posting_lang: en\nposting_location: 'Atlanta, Georgia, United States'\n"
        "discovery_source: workable\n"
        "date_controls:\n"
        "  - key: QA_1\n    mechanism: text_entry\n    date_format: '%d/%m/%Y'\n"
        "    evidence: placeholder=DD/MM/YYYY\n"
        "answers: []\n")
    loaded = load_generated_answers(path)

    assert loaded.posting_location == "Atlanta, Georgia, United States"
    assert loaded.discovery_source == "workable"
    assert loaded.date_controls == [
        DateControl(key="QA_1", mechanism="text_entry", date_format="%d/%m/%Y",
                    evidence="placeholder=DD/MM/YYYY")]


def test_a_document_from_before_this_wave_still_loads(tmp_path) -> None:
    """Backwards compatibility is not a nicety here: the generated files already on
    disk carry no posting context, and they must keep loading (and simply resolve
    no policy answer) rather than crashing a live run."""
    path = tmp_path / "answers.yaml"
    path.write_text("schema_version: '1'\nvendor: workable\nslug: acme\n"
                    "job_id: ABC123\nposting_lang: en\nanswers: []\n")
    loaded = load_generated_answers(path)

    assert loaded.posting_location == ""
    assert loaded.discovery_source == ""
    assert loaded.date_controls == []


def test_an_unsupported_date_format_is_refused_at_load(tmp_path) -> None:
    """The format decides WHICH DAY is typed into a real application. A format
    nothing validated never reaches the page."""
    path = tmp_path / "answers.yaml"
    path.write_text("schema_version: '1'\nvendor: workable\nslug: acme\n"
                    "job_id: ABC123\nposting_lang: en\nanswers: []\n"
                    "date_controls:\n  - key: QA_1\n    mechanism: text_entry\n"
                    "    date_format: '%c'\n")
    with pytest.raises(ContentSchemaError, match="unsupported date_format"):
        load_generated_answers(path)


# -- the generator's probe (the format is READ, never assumed) ----------------

class FakeElement:
    def __init__(self, attrs: dict) -> None:
        self._attrs = attrs

    def get_attribute(self, name: str):
        return self._attrs.get(name)


class FakePage:
    """A page that CAN miss: an element it was not given is simply not there, which
    is what makes the not-found path reachable (a fake that cannot fail proves
    nothing)."""

    def __init__(self, elements: dict[str, dict]) -> None:
        self._elements = elements

    def query_selector(self, selector: str):
        attrs = self._elements.get(selector)
        return FakeElement(attrs) if attrs is not None else None


def fake_factory(page: FakePage):
    @contextmanager
    def factory(apply_url: str):
        yield page

    return factory


def test_probe_reads_the_live_workable_date_box(tool) -> None:
    """The exact shape observed in the live DOM (movement-labs 0F5F662A46,
    /tmp/w5-dom-workable.html): a react-datepicker TEXT input declaring DD/MM/YYYY."""
    page = FakePage({'input[name="QA_1"]': {
        "type": "text", "placeholder": "DD/MM/YYYY", "aria-readonly": "false",
        "inputmode": "tel"}})
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")

    controls = tool.probe_date_controls("https://example.invalid/apply", [fld],
                                        page_factory=fake_factory(page))

    assert controls == [{"key": "QA_1", "mechanism": MECHANISM_TEXT_ENTRY,
                         "date_format": "%d/%m/%Y",
                         "evidence": "placeholder=DD/MM/YYYY"}]


def test_probe_reads_the_control_not_the_label(tool) -> None:
    """A box the SCHEMA calls a date, rendering in the US order, is answered in the
    US order. The label says nothing about it either way."""
    page = FakePage({'input[name="QA_1"]': {"type": "text",
                                            "placeholder": "MM/DD/YYYY"}})
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")

    controls = tool.probe_date_controls("https://example.invalid/apply", [fld],
                                        page_factory=fake_factory(page))

    assert controls[0]["date_format"] == "%m/%d/%Y"


def test_probe_marks_a_readonly_calendar_as_click_only(tool) -> None:
    page = FakePage({'input[name="start"]': {"type": "text", "readonly": "",
                                             "aria-readonly": "true"}})
    fld = make_field("start", "Start date", type_="date", norm_type="DATE")

    controls = tool.probe_date_controls("https://example.invalid/apply", [fld],
                                        page_factory=fake_factory(page))

    assert controls[0]["mechanism"] == MECHANISM_PICKER_ONLY
    assert controls[0]["date_format"] is None


def test_probe_fails_closed_on_a_date_box_declaring_no_order(tool) -> None:
    """A date control with nothing to derive an order from stays a date control with
    NO format. It must NOT decay into a prose box: "available immediately" typed
    into a date field is as wrong as the wrong day."""
    page = FakePage({'input[name="QA_1"]': {"type": "text"}})
    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")

    controls = tool.probe_date_controls("https://example.invalid/apply", [fld],
                                        page_factory=fake_factory(page))

    assert controls[0]["mechanism"] == MECHANISM_TEXT_ENTRY
    assert controls[0]["date_format"] is None


def test_probe_marks_a_plain_text_control_as_prose(tool) -> None:
    """A control the schema does NOT call a date, with no date affordance in the
    DOM, wants the duration answer."""
    page = FakePage({'input[name="start"]': {"type": "text"}})
    fld = make_field("start", "When can you start?", type_="input_text")

    controls = tool.probe_date_controls("https://example.invalid/apply", [fld],
                                        page_factory=fake_factory(page))

    assert controls[0]["mechanism"] == MECHANISM_PLAIN_TEXT


def test_probe_records_a_control_the_page_never_showed(tool) -> None:
    controls = tool.probe_date_controls("https://example.invalid/apply",
                                        [make_field("QA_1", "When can you start?",
                                                    type_="date", norm_type="DATE")],
                                        page_factory=fake_factory(FakePage({})))

    assert controls[0]["mechanism"] == MECHANISM_UNPROBED


def test_a_browser_that_will_not_start_is_a_gap_not_a_silence(tool) -> None:
    """A failed probe must not return an empty list: that reads as "no date
    controls" and the gap disappears."""
    @contextmanager
    def broken(apply_url: str):
        raise RuntimeError("no display")
        yield  # pragma: no cover

    fld = make_field("QA_1", "When can you start?", type_="date", norm_type="DATE")
    controls = tool.probe_date_controls("https://example.invalid/apply", [fld],
                                        page_factory=broken)

    assert controls[0]["mechanism"] == MECHANISM_UNPROBED
    assert "probe failed" in controls[0]["evidence"]


def test_date_control_candidates_cover_both_the_schema_and_the_label(tool) -> None:
    """Probe what the schema calls a date AND what the label asks, because the two
    disagree vendor by vendor."""
    by_schema = make_field("a", "Anything at all", type_="date", norm_type="DATE")
    by_label = make_field("b", "What is your notice period?", type_="input_text")
    neither = make_field("c", "Why do you want this job?", type_="textarea")

    keys = [f.key for f in tool.date_control_candidates(
        make_map(by_schema, by_label, neither))]

    assert keys == ["a", "b"]


def test_generate_carries_the_posting_context_through(tool, tmp_path) -> None:
    """The generator ANSWERS questions; it does not re-derive facts. The context the
    probe read must arrive intact in the document the engine loads."""
    doc = tool.generate_answers(
        {"vendor": "workable", "slug": "acme", "job_id": "ABC123",
         "posting_lang": "en", "posting_location": "Atlanta, Georgia, United States",
         "discovery_source": "workable",
         "date_controls": [{"key": "QA_1", "mechanism": "text_entry",
                            "date_format": "%d/%m/%Y", "evidence": "placeholder"}],
         "questions": []},
        SSOT({}), company="Acme", runner=lambda prompt, model: "unused")

    assert doc["posting_location"] == "Atlanta, Georgia, United States"
    assert doc["discovery_source"] == "workable"
    assert doc["date_controls"][0]["date_format"] == "%d/%m/%Y"

    path = tmp_path / "answers.yaml"
    tool.write_yaml(doc, path)
    assert load_generated_answers(path).date_controls[0].date_format == "%d/%m/%Y"


def test_posting_location_comes_from_the_vendors_own_adapter(tool) -> None:
    """Read through the vendor's discover adapter, on the REAL workable widget
    shape: no second location parser lives in this tool to drift from it.

    Also pins the CAPTURE USER-AGENT. Without it the board answers 403 (observed
    live on apply.workable.com), and a 403 reads as "no location", which is a city
    gap manufactured by a missing header rather than by the posting."""
    import io
    import json as jsonlib

    payload = {"jobs": [{"shortcode": "ABC123", "title": "Engineer",
                         "locations": [{"city": "Atlanta", "region": "Georgia",
                                        "country": "United States"}]}]}
    seen: dict = {}

    class FakeOpener:
        def open(self, request, timeout=30):
            seen["url"] = request.full_url
            seen["ua"] = request.get_header("User-agent")
            return io.BytesIO(jsonlib.dumps(payload).encode())

    location = tool.posting_location("workable", "acme", "ABC123", FakeOpener())

    assert location == "Atlanta, Georgia, United States"
    assert "acme" in seen["url"]
    assert "jobhunt" in seen["ua"], "the board fetch must identify itself"


def test_an_unreachable_board_yields_no_location(tool) -> None:
    class BrokenOpener:
        def open(self, url, timeout=30):
            raise OSError("network unreachable")

    assert tool.posting_location("workable", "acme", "ABC123", BrokenOpener()) == ""


# -- the CONTROL outranks the SCHEMA (the wave's core mechanism) ---------------

def test_the_control_outranks_the_schema_when_they_disagree() -> None:
    """The tiebreak this entire wave exists for, pinned in BOTH directions.

    Every other date test sets the schema and the DOM to AGREE, so the tiebreak in
    `_expects_a_date` never fires and the branch survived deletion: with it gone the
    code classifies from the vendor SCHEMA alone, and the schema is the witness this
    wave exists to distrust.

    Direction 1 is the live workable shape one step further on: the vendor declares
    `text` and the DOM renders a DD/MM/YYYY box. The box gets a DATE. Under a
    schema-only classifier it gets PROSE typed into a date field.
    Direction 2 is the mirror: the vendor declares `date` and the DOM renders a plain
    box with no date affordance at all. It gets the owner's duration answer.
    """
    box_says_date = make_field("notice", "Notice period", type_="input_text",
                               norm_type="TEXT")
    resolved, report = overlay_one(
        box_says_date, make_ssot(), generated=make_generated(controls=[
            DateControl(key="notice", mechanism=MECHANISM_TEXT_ENTRY,
                        date_format="%d/%m/%Y",
                        evidence="placeholder=DD/MM/YYYY")]))
    assert resolved.values == {"notice": date.today().strftime("%d/%m/%Y")}
    assert report.applied == [("notice", "policy:start_date:%d/%m/%Y")]
    assert FAKE_NOTICE not in str(resolved.values), "prose typed into a date box"

    schema_says_date = make_field("start", "When can you start?", type_="date",
                                  norm_type="DATE")
    resolved, report = overlay_one(
        schema_says_date, make_ssot(), generated=make_generated(controls=[
            DateControl(key="start", mechanism=MECHANISM_PLAIN_TEXT,
                        date_format=None,
                        evidence="input[type=text] with no date affordance")]))
    assert resolved.values == {"start": FAKE_NOTICE}
    assert report.applied == [("start", "canned:canned_answers.notice_period")]


def test_the_date_probe_list_is_a_superset_of_the_policy_list(tool) -> None:
    """One keyword list, not two. The probe must LOOK AT every control the policy
    CLAIMS, or the policy classifies from the vendor schema it exists to distrust.

    The two lists were duplicated across a module boundary and had drifted: the
    policy claimed a bare "notice" the probe never probed, so a "Notice" box
    rendering DD/MM/YYYY but declared `text` was never looked at and had prose typed
    into it. This asserts the SUPERSET relation the DRY fix restores, so a future
    local list in the tool cannot silently drift again.
    """
    probed = set(tool.POLICY_DATE_KEYWORDS)
    claimed = set(POLICY_DATE_KEYWORDS)

    assert claimed - probed == set(), "claimed by the policy, never probed"
    assert "notice" in claimed and "notice" in probed


def test_a_notice_box_the_vendor_declares_text_is_still_probed(tool) -> None:
    """The drift, end to end: the control the policy claims must reach the probe.

    A control labelled "Notice", declared `text` by its vendor, rendering a
    DD/MM/YYYY box. It is a probe CANDIDATE (it was not, before), the probe reads the
    order off the DOM, and the resolver types a DATE into it.
    """
    fld = make_field("n", "Notice", type_="input_text", norm_type="TEXT")

    candidates = tool.date_control_candidates(make_map(fld))
    assert [f.key for f in candidates] == ["n"]

    page = FakePage({'input[name="n"]': {"type": "text", "placeholder": "DD/MM/YYYY"}})
    controls = tool.probe_date_controls("https://example.invalid/apply", candidates,
                                        page_factory=fake_factory(page))
    assert controls[0]["date_format"] == "%d/%m/%Y"

    resolved, _ = overlay_one(fld, make_ssot(), generated=make_generated(controls=[
        DateControl(**controls[0])]))
    assert resolved.values == {"n": date.today().strftime("%d/%m/%Y")}


def test_the_probe_keeps_a_found_controls_format_when_another_is_absent(tool) -> None:
    """The not-found branch, on the only shape that can prove it: TWO candidates,
    one on the page and one not.

    With a single candidate the branch is near-equivalent (the failure falls into the
    outer handler and everything comes back UNPROBED anyway). With two, deleting it
    costs the PRESENT control its derived order -- and a control with no order is
    never filled, so the loss is silent.
    """
    page = FakePage({'input[name="here"]': {"type": "text",
                                            "placeholder": "DD/MM/YYYY"}})
    here = make_field("here", "When can you start?", type_="date", norm_type="DATE")
    gone = make_field("gone", "Notice period", type_="date", norm_type="DATE")

    controls = tool.probe_date_controls("https://example.invalid/apply", [here, gone],
                                        page_factory=fake_factory(page))

    assert controls == [
        {"key": "here", "mechanism": MECHANISM_TEXT_ENTRY, "date_format": "%d/%m/%Y",
         "evidence": "placeholder=DD/MM/YYYY"},
        {"key": "gone", "mechanism": MECHANISM_UNPROBED, "date_format": None,
         "evidence": "control not found on the apply page"}]


def test_a_kernel_misroute_is_announced_on_stderr(capsys) -> None:
    """DETECT AND *REPORT*. The overlay's only caller (`w5_accept.py`, frozen) builds
    its result document from applied/tos_forbidden/unresolved and DROPS `misrouted`,
    so a record that lives only in the report object reaches no human, no artefact
    and no gate. The overlay says it out loud instead -- on stderr, so it cannot
    corrupt a caller writing JSON to stdout.
    """
    ssot = make_ssot()
    fld = make_field("notice", "Notice period", type_="date", norm_type="DATE",
                     required=True)
    fieldmap = make_map(fld)
    resolved = resolve_values(fieldmap, ssot, {})
    assert resolved.values == {"notice": FAKE_NOTICE}, "kernel behaviour changed"

    report = apply_content_overlay(resolved, fieldmap, ssot,
                                   generated=make_generated(controls=[
                                       DateControl(key="notice",
                                                   mechanism=MECHANISM_TEXT_ENTRY,
                                                   date_format="%d/%m/%Y")]))
    captured = capsys.readouterr()

    assert report.misrouted, "nothing detected: the test's premise is gone"
    assert MISROUTE_WARNING in captured.err
    assert "key=notice" in captured.err
    assert captured.out == "", "a warning must never reach a caller's stdout"
    assert resolved.values == {"notice": FAKE_NOTICE}, "reported, never corrected"


def test_a_bare_yes_no_canned_scalar_never_lands_in_an_option_less_box() -> None:
    """MAJOR-5: the `("relocat",)` canned row also hits the relocation-ADDRESS box.

    That box is FREE TEXT ("which address would you relocate from?"), it carries no
    options, and a text box takes any string verbatim -- so the row's routes, both of
    which render as the single word "Yes" (`willing_to_relocate` is a boolean in the
    SSOT), filled an address box with "Yes" and counted it complete.

    "Yes" is an answer to a BOOLEAN CONTROL, and only a control that OFFERS options
    is one. The control decides, never the label: the option-bearing dropdown still
    gets its scalar, and the option-less box takes prose or nothing.
    """
    ssot = SSOT({"canned_answers": {"relocation_dropdown": "Yes",
                                    "willing_to_relocate": True}})

    address = make_field("reloc_addr", "Relocation address", max_length=127)
    resolved, report = overlay_one(address, ssot)
    assert resolved.values == {}
    assert "Yes" not in str(resolved.values)
    assert report.applied == []

    dropdown = make_field("reloc", "Are you willing to relocate?", type_="yes_no",
                          options=["Yes", "No"])
    resolved, report = overlay_one(dropdown, ssot)
    assert resolved.values == {"reloc": "Yes"}
    assert report.applied == [("reloc", "canned:canned_answers.relocation_dropdown")]


# -- owning a field is TERMINAL (MAJOR-1) -------------------------------------

def test_a_generated_name_can_never_reach_a_policy_owned_referral_control() -> None:
    """MAJOR-1, the deepest cut of this wave's defect class: a REQUIRED referral
    control whose OPTIONS ARE PEOPLE.

    The resolver owns the field and answers it with the source CLASS (`Vendor`),
    which matches none of those options -- correctly, because the box is not asking
    for a class. On branch code the failed fit did not END the field: `_first_fitting`
    walked on to the GENERATED answer behind it and typed "Jane Doe" into "Who
    referred you?", asserting to a real employer a referral by a named person that
    never happened. The engine was stopped from inventing a referrer AT the resolver;
    this is the generic ladder inventing one anyway, one level deeper.

    OWNING IS TERMINAL: the field is offered the policy value ALONE. It cannot take
    it, so it stays EMPTY and is reported.
    """
    fld = make_field("r", "Who referred you?", required=True, type_="yes_no",
                     options=["Jane Doe", "John Smith"])
    generated = GeneratedAnswers(
        vendor="workable", slug="acme", job_id="ABC123", posting_lang="en",
        discovery_source="workable",
        answers=[GeneratedAnswer(key="r", label=None, value="Jane Doe")])

    resolved, report = overlay_one(fld, make_ssot(), generated=generated)

    assert resolved.values == {}
    assert "Jane Doe" not in str(resolved.values)
    assert report.applied == []
    assert resolved.skipped == [("r", NO_OPTION_MATCH)]
    assert report.unresolved == [("r", NO_OPTION_MATCH)]


def test_a_generated_answer_never_reaches_a_field_its_policy_declined() -> None:
    """The invariant behind MAJOR-1, across ALL THREE policy families: wherever a
    policy OWNS a field and declines to answer it, the field is a GAP, and NOTHING
    downstream may fill it -- least of all a model-written sentence.

    Each case carries a generated answer keyed to the very field the policy refused:
    a PERSON for the free-text referral box, a city for a posting too unplaceable to
    choose one, a date for a picker the content channel cannot drive. Every one must
    come out EMPTY, with the POLICY's own reason on it (not the ladder's), which is
    what a human closes the gap from.
    """
    referral = make_field("ref", "Who were you referred by?", required=True,
                          max_length=127)
    city = make_field("city", "Which city do you live in?", max_length=64)
    picker = make_field("start", "Start date", type_="date", norm_type="DATE")

    cases = [
        (referral, "Jane Doe", REFERRAL_WANTS_A_PERSON, {}),
        (city, "Springfield", LOCATION_NOT_PLACEABLE, {"location": "Atlantis"}),
        (picker, "2026-08-01", PICKER_NOT_DRIVEN,
         {"controls": [DateControl(key="start", mechanism=MECHANISM_PICKER_ONLY,
                                   date_format=None, evidence="readonly input")]}),
    ]

    for fld, invented, expected_reason, extra in cases:
        base = make_generated(source="workable", **extra)
        generated = GeneratedAnswers(
            vendor=base.vendor, slug=base.slug, job_id=base.job_id,
            posting_lang=base.posting_lang, posting_location=base.posting_location,
            discovery_source=base.discovery_source,
            date_controls=list(base.date_controls),
            answers=[GeneratedAnswer(key=fld.key, label=None, value=invented)])

        # Cities seeded, so the city case refuses for the reason under test (the
        # posting is unplaceable) and not for want of anything to choose from.
        resolved, report = overlay_one(fld, make_ssot(cities=["Milan", "Bologna"]),
                                       generated=generated)

        assert resolved.values == {}, f"{fld.key}: a declined field was filled"
        assert invented not in str(resolved.values)
        assert report.applied == []
        assert resolved.skipped == [(fld.key, expected_reason)]
        assert report.unresolved == [(fld.key, expected_reason)]


def test_a_canned_route_can_never_reach_a_policy_owned_field(monkeypatch) -> None:
    """The OTHER rung, pinned STRUCTURALLY rather than on today's table.

    No `_CONTENT_MATCHERS` keyword currently overlaps the referral / city / date
    families, so the canned rung cannot reach an owned field today -- by luck of the
    keyword lists, not by construction. A single new canned row ("referred", say)
    would be enough to hand a policy-owned box a canned value the policy had just
    refused. Here that row EXISTS, and the field must still come out empty: the
    guarantee is the terminal rule, not the table.
    """
    monkeypatch.setattr(content, "_CONTENT_MATCHERS",
                        [(("referred by",), ("canned_answers.referrer",))])
    ssot = SSOT({"canned_answers": {"referrer": "Jane Doe"}})

    fld = make_field("ref", "Who were you referred by?", required=True,
                     max_length=127)
    resolved, report = overlay_one(fld, ssot,
                                   generated=make_generated(source="workable"))

    assert resolved.values == {}
    assert "Jane Doe" not in str(resolved.values)
    assert report.applied == []
    assert resolved.skipped == [("ref", REFERRAL_WANTS_A_PERSON)]
    assert report.unresolved == [("ref", REFERRAL_WANTS_A_PERSON)]
