"""Ashby's adoption of the shared kernel control-driving mechanism
(`engine.kernel.control_toolkit.drive_control`, W5.1c) for checkbox controls,
narrowing the pre-W5.1c blanket Turnstile hand-off
(`engine.providers.ashby.fill._HUMAN_HANDOFF_REASON`).

Exercises `fill._control_kind` (the routing decision) and `fill._fill_control`
(the drive seam `fill()`'s loop now calls for a control `_control_kind` claims)
directly, with a minimal fake page/locator harness -- no live browser, no
network, mirroring the convention of `tests/providers/greenhouse/
test_control_toolkit_adoption.py`.

WHICH KINDS ASHBY ACTUALLY USES: CHECKBOX (a single, unambiguous boolean
control, the graphql `Boolean` type) and, since the W5.1c radio-group adoption
completed on the TB5 live run, RADIO (a radio-rendered
`multi_value_single_select` GROUP, 33/34 live ValueSelect fields). Ashby's
`Field.locator` is one role+name pair per FIELD, so a GROUP is driven a different
way: `fill._locate_option` builds the per-option locator (lever's FX1 shape,
scoped to the field's `data-field-path` entry) and `_fill_control` ticks exactly
the requested options. DATE is not applicable because Ashby's `Date` graphql type
collapses into the generic canonical `input_text` type
(`capture._ASHBY_TYPE_MAP`) with no surviving signal to route it through
`ControlKind.DATE`.

P2-5, THE CHECKBOX-GROUP ADOPTION. A checkbox-rendered
`multi_value_multi_select` GROUP (a LIST value on one field-level locator) was
the scoped-out sibling of the radio wave; it now drives through THE SAME
`_locate_option` + `_fill_option_group` the radio group uses, with the role as
the only parameter that differs. The tests below pin the SHARING itself (one
locate, one drive, reached by both kinds), not just the new behaviour, because
this codebase's characteristic defect is a second parallel emission site that
gets fixed one instance at a time.

Three properties are specific to the multi-select and are pinned here:
PARTIAL IS A GAP (some-but-not-all confirmed is never a fill), IDEMPOTENCE (a
group already in the desired state is confirmed WITHOUT being touched, because a
click toggles), and the CONSENT/EEO EXCLUSION (`fill._is_excluded_group`: the
newly drivable population is exactly where the kernel's consent policy does not
reach, so those groups keep the human hand-off).
"""

import importlib
import re

from engine.kernel.contracts import FieldValue, FillSafetyError, Locator
from engine.kernel.control_toolkit import ControlKind

# `engine.providers.ashby.fill` (the SUBMODULE) is shadowed at package scope by
# the `fill` Provider-callable it re-exports (see `capture.py`'s package NAME
# NOTE, which applies identically to `fill`); reached via
# `importlib.import_module`, exactly as `tests/providers/greenhouse/
# test_control_toolkit_adoption.py` does for its own vendor.
ashby_fill = importlib.import_module("engine.providers.ashby.fill")


class _FakeCheckboxLocator:
    """A native `<input type="checkbox">`: `.check()`/`.uncheck()` flip
    `checked`; `.is_checked()` is the readback `control_toolkit._confirm` reads
    via the shared kernel `_readback`."""

    def __init__(self, checked: bool | None = None):
        self.checked = checked

    def check(self):
        self.checked = True

    def uncheck(self):
        self.checked = False

    def is_checked(self):
        return bool(self.checked)


class _NoUncheckLocator:
    """A control exposing `.check()` but no `.uncheck()` -- the fail-soft path
    `control_toolkit._drive_toggle` takes for an untick it cannot perform."""

    def __init__(self):
        self.checked = None

    def check(self):
        self.checked = True

    def is_checked(self):
        return bool(self.checked)


class _FakeRolePage:
    """`page.get_by_role(role, name=...)` returns whichever fake locator was
    registered for that (role, name) pair -- mirrors `base._locate`'s own
    `page.get_by_role(role, name=name)` call exactly."""

    def __init__(self, controls: dict):
        self._controls = controls

    def get_by_role(self, role, name=None):
        return self._controls[(role, name)]


class _FakeOptionControl:
    """One native option control in a GROUP -- a radio or a checkbox, which are
    the same shape here because they are the same shape live: its OWN accessible
    name is the option label, and it shares the group's `data-field-path` entry.
    `.check()`/`.is_checked()` are the drive + readback the shared kernel
    mechanism uses.

    `.click()` RAISES. A checkbox group is the case where the difference bites: a
    click TOGGLES, so a driver that clicked would untick an already-correct box,
    and this fake makes that failure loud instead of silent."""

    def __init__(self, path, name, checked=None):
        self.path = path
        self.name = name
        self.checked = checked
        self.checks = 0
        self.unchecks = 0

    def check(self):
        self.checks += 1
        self.checked = True

    def uncheck(self):
        self.unchecks += 1
        self.checked = False

    def is_checked(self):
        return bool(self.checked)

    def click(self, *args, **kwargs):
        raise AssertionError(
            "an option control must be driven via .check(), never .click(): a "
            "click TOGGLES and would invert an already-correct checkbox")


class _FakeOptionMatch:
    """A Playwright-Locator-shaped match over option controls: `.count()`,
    `.and_()` (the FX1 intersection), and the drive/readback verbs delegated to
    the SINGLE matched control (a strict-mode violation otherwise)."""

    def __init__(self, nodes):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def and_(self, other):
        return _FakeOptionMatch(
            [n for n in self.nodes if any(n is o for o in other.nodes)])

    def _only(self):
        assert len(self.nodes) == 1, (
            f"strict mode: {len(self.nodes)} elements, not 1")
        return self.nodes[0]

    def check(self):
        self._only().check()

    def uncheck(self):
        self._only().uncheck()

    def is_checked(self):
        return self._only().is_checked()


class _FakeUnreadableOption(_FakeOptionControl):
    """An option whose `.check()` lands but whose readback never confirms it: the
    silent-no-op the readback gate exists to catch, and the ingredient of a
    PARTIAL multi-select."""

    def is_checked(self):
        return False


class _FakeOptionGroupPage:
    """A page modelling one or more option GROUPS of a given ROLE. Serves the two
    calls `fill._locate_option` makes: `get_by_role(<role>, name=<option>,
    exact=True)` (page-wide, by the option's OWN accessible name) and
    `locator('[data-field-path="<key>"] input')` (the group's entry scope), so
    `.and_()` narrows a page-wide option match to the one option inside a group.

    ONE fake for both roles, mirroring the ONE production mechanism: the role is
    a constructor argument, and `get_by_role` answers with the group's controls
    only when it is ASKED FOR THAT ROLE. A driver that asked with the wrong role
    (the type-derived guess this vendor shipped three phantoms of) therefore gets
    zero matches here and hands off, exactly as it would live."""

    def __init__(self, groups: dict, role: str = "radio"):
        self.role = role
        self.nodes = []
        self._by_path = {}
        self.roles_requested = []
        for path, options in groups.items():
            # An entry is either an option WORDING (a default, unticked control)
            # or an already-built control, so a test can seed a pre-ticked or
            # unreadable option without a second page class.
            opts = [_FakeOptionControl(path, o) if isinstance(o, str) else o
                    for o in options]
            self._by_path[path] = opts
            self.nodes.extend(opts)

    def get_by_role(self, role, name=None, exact=None):
        self.roles_requested.append(role)
        if role != self.role:
            return _FakeOptionMatch([])
        return _FakeOptionMatch([n for n in self.nodes if n.name == name])

    def locator(self, css):
        match = re.match(r'\[data-field-path="([^"]+)"\] input$', css)
        path = match.group(1) if match else None
        return _FakeOptionMatch(self._by_path.get(path, []))


def _boolean_field(key, label, *, value, locator_name=None):
    return FieldValue(
        key=key, label=label, type="boolean",
        locator=Locator(role="checkbox", name=locator_name or label),
        value=value)


def _radio_group_field(key, label, *, value="Yes"):
    """A radio-rendered `multi_value_single_select` (33/34 live Ashby
    ValueSelect fields): ONE Field for the whole group, string value, never a
    bool."""
    return FieldValue(
        key=key, label=label, type="multi_value_single_select",
        locator=Locator(role="radio", name=label), value=value)


def _checkbox_group_field(key, label, *, value=("Documentaries",)):
    """A checkbox-rendered `multi_value_multi_select` (Ashby's native checkbox
    GROUP, per `capture._ASHBY_FALLBACK_ROLE`): ONE Field, list value."""
    return FieldValue(
        key=key, label=label, type="multi_value_multi_select",
        locator=Locator(role="checkbox", name=label), value=list(value))


# The REAL option wordings of the live five-checkbox MultiValueSelect
# (elevenlabs/1713bfd7, path 2726a0b3-..., captured 2026-07-13; the same control
# `tests/test_providers_ashby.py::_SIBLING_DOM` carries verbatim). A test that
# invented friendlier one-word options would not exercise the wordings the
# accessible-name match actually has to carry.
_LIVE_CONTENT_OPTIONS = [
    "Movies/TV shows/other dramatic content",
    "Creator content (e.g. YouTube videos)",
    "Ads/socials",
    "E-learning or informational content",
    "Documentaries",
]
_LIVE_CONTENT_LABEL = ("Which of the following types of content have you "
                       "produced dubs or audiovisual translations for in a "
                       "professional context?")


def test_drives_a_boolean_checkbox_true_and_confirms():
    """A required consent-style boolean now DRIVES (never hands off): the
    fake `.check()`/`is_checked()` round-trips True, so the outcome confirms."""
    fv = _boolean_field("certify", "I certify the above is true", value=True)
    checkbox = _FakeCheckboxLocator()
    page = _FakeRolePage({("checkbox", fv.label): checkbox})

    kind = ashby_fill._control_kind(fv)
    assert kind == ControlKind.CHECKBOX
    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)

    assert ok is True and reason == ""
    assert checkbox.checked is True


def test_drives_a_boolean_checkbox_false_and_confirms():
    """An unticked-intent boolean (e.g. an optional marketing consent left
    False) drives via `.uncheck()`, not just the True/tick path."""
    fv = _boolean_field("marketing_opt_in", "I want marketing emails",
                        value=False)
    checkbox = _FakeCheckboxLocator(checked=True)
    page = _FakeRolePage({("checkbox", fv.label): checkbox})

    kind = ashby_fill._control_kind(fv)
    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)

    assert ok is True and reason == ""
    assert checkbox.checked is False


def test_unconfirmed_drive_is_reported_never_silently_filled():
    """A control that cannot perform the requested transition (no `.uncheck()`
    here) is `driven=False` (`control_toolkit._skip`), surfaced as an
    unconfirmed, unfilled field with a reason -- never counted as filled."""
    fv = _boolean_field("newsletter", "Subscribe to the newsletter",
                        value=False)
    page = _FakeRolePage({("checkbox", fv.label): _NoUncheckLocator()})

    kind = ashby_fill._control_kind(fv)
    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)

    assert ok is False
    assert reason and "uncheck" in reason


def test_submit_denylist_name_still_raises_fillsafetyerror():
    """The submit-denylist guard (`control_toolkit._guard_name`) is not
    bypassed by routing the boolean checkbox through this new seam: a control
    named like a submit control still aborts the fill, never a silent skip."""
    fv = _boolean_field("apply", "Submit application", value=True)
    page = _FakeRolePage({("checkbox", fv.label): _FakeCheckboxLocator()})

    kind = ashby_fill._control_kind(fv)
    try:
        ashby_fill._fill_control(page, fv, kind)
        assert False, "expected FillSafetyError"
    except FillSafetyError:
        pass


def test_radio_group_drives_the_resolved_option_via_per_option_locate():
    """W5.1c radio-group adoption: a radio-rendered `multi_value_single_select`
    is now a `ControlKind.RADIO` target, DRIVEN, not handed off. `_control_kind`
    returns RADIO, and `_fill_control` locates the resolved option INSIDE the
    group's own entry (`_locate_option`, lever's FX1 shape) and ticks exactly it,
    single-select, readback-confirmed via the shared kernel mechanism."""
    fv = _radio_group_field("work_auth", "Are you authorized to work?",
                            value="Yes")
    page = _FakeOptionGroupPage({fv.key: ["Yes", "No"]}, role="radio")

    kind = ashby_fill._control_kind(fv)
    assert kind == ControlKind.RADIO

    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)
    assert ok is True and reason == ""
    by_name = {o.name: o for o in page._by_path[fv.key]}
    assert by_name["Yes"].is_checked() is True     # exactly the resolved option
    assert by_name["No"].is_checked() is False     # the other stays untouched


def test_radio_group_option_that_cannot_be_located_hands_off():
    """A radio group whose resolved option resolves to NO control inside its own
    entry is handed off loudly (`_GROUP_OPTION_HANDOFF_REASON`), never a guessed
    click: the answer is not among the group's rendered options, so no control is
    ticked and the field surfaces as an honest gap."""
    fv = _radio_group_field("work_auth", "Are you authorized to work?",
                            value="Maybe")   # not a rendered option
    page = _FakeOptionGroupPage({fv.key: ["Yes", "No"]}, role="radio")

    ok, actual, reason = ashby_fill._fill_control(
        page, fv, ashby_fill._control_kind(fv))
    assert ok is False
    assert reason == ashby_fill._GROUP_OPTION_HANDOFF_REASON
    assert all(o.is_checked() is False for o in page._by_path[fv.key])


def test_checkbox_group_drives_every_requested_option_and_only_those():
    """P2-5, THE ADOPTION. A checkbox-rendered `multi_value_multi_select` GROUP
    is now a `ControlKind.CHECKBOX` target DRIVEN per option, not handed off:
    each requested option is located INSIDE the group's own entry
    (`_locate_option`, the same call the radio group makes) and ticked through
    the shared kernel mechanism, readback-confirmed.

    THE MULTI-SELECT'S OWN RISK, pinned: the group ends in exactly the requested
    COMBINATION. Two of the five live options are requested; those two are
    ticked and the other three are left untouched, so the driver cannot quietly
    widen the answer the candidate gave.

    The RIGHT-HAND assertion is on `.checks`, not just on final state: it proves
    each box was set ONCE, so nothing was toggled twice into place."""
    requested = ["Documentaries", "Ads/socials"]
    fv = _checkbox_group_field("content_types", _LIVE_CONTENT_LABEL,
                               value=requested)
    page = _FakeOptionGroupPage({fv.key: _LIVE_CONTENT_OPTIONS},
                                role="checkbox")

    kind = ashby_fill._control_kind(fv)
    assert kind == ControlKind.CHECKBOX

    ok, actual, reason = ashby_fill._fill_control(page, fv, kind)
    assert ok is True and reason == ""
    assert actual == requested          # the report says what was confirmed

    by_name = {o.name: o for o in page._by_path[fv.key]}
    for option in _LIVE_CONTENT_OPTIONS:
        want = option in requested
        assert by_name[option].is_checked() is want, option
        assert by_name[option].checks == (1 if want else 0), option
        assert by_name[option].unchecks == 0, option


def test_checkbox_group_already_in_the_desired_state_is_never_toggled_off():
    """IDEMPOTENCE, the property a multi-select needs and a radio does not.

    A checkbox CLICK toggles, so a second pass over an already-correct group is
    exactly where a naive driver inverts the answer it just gave. Here both
    requested options arrive ALREADY ticked: the group must confirm as filled,
    stay ticked, and -- the strong assertion -- be driven ZERO times, because
    `_already_ticked` reads the desired state before deciding to act.

    Driving it twice in a row must also be stable, which is the property an
    engine that may re-open a form actually depends on."""
    pre_ticked = [_FakeOptionControl("content_types", name, checked=True)
                  for name in ("Documentaries", "Ads/socials")]
    others = [_FakeOptionControl("content_types", name)
              for name in ("Creator content (e.g. YouTube videos)",)]
    fv = _checkbox_group_field("content_types", _LIVE_CONTENT_LABEL,
                               value=["Documentaries", "Ads/socials"])
    page = _FakeOptionGroupPage({fv.key: pre_ticked + others}, role="checkbox")

    for _ in range(2):
        ok, actual, reason = ashby_fill._fill_control(
            page, fv, ashby_fill._control_kind(fv))
        assert ok is True and reason == ""
        assert actual == ["Documentaries", "Ads/socials"]

    for control in pre_ticked:
        assert control.is_checked() is True     # still ticked, never inverted
        assert control.checks == 0              # and NEVER touched at all
        assert control.unchecks == 0
    assert others[0].is_checked() is False      # an unrequested box stays off


def test_checkbox_group_partially_confirmed_is_a_gap_never_a_fill():
    """PARTIAL CONFIRMATION IS A GAP -- the convention that predates this
    adoption and must survive it.

    One requested option's tick lands but never reads back (the silent-no-op the
    readback gate exists for). The group is therefore reported UNFILLED with the
    unconfirmed option NAMED, even though the other option really did commit: a
    multi-select answered in part is a DIFFERENT answer, and reporting it filled
    would be the silent wrong answer this engine forbids. `actual` still carries
    the option that DID confirm, so the report says what the page holds."""
    good = _FakeOptionControl("content_types", "Documentaries")
    silent = _FakeUnreadableOption("content_types", "Ads/socials")
    fv = _checkbox_group_field("content_types", _LIVE_CONTENT_LABEL,
                               value=["Documentaries", "Ads/socials"])
    page = _FakeOptionGroupPage({fv.key: [good, silent]}, role="checkbox")

    ok, actual, reason = ashby_fill._fill_control(
        page, fv, ashby_fill._control_kind(fv))

    assert ok is False                       # NOT a fill, despite one success
    assert actual == ["Documentaries"]       # honest about what did land
    assert "Ads/socials" in reason           # and about what did not
    assert reason == ashby_fill._partial_group_reason(["Ads/socials"])
    assert good.is_checked() is True         # the one that worked is untouched


def test_checkbox_group_with_an_unlocatable_option_drives_nothing_at_all():
    """ATOMICITY, and why the locate runs BEFORE any drive.

    One requested option is not among the group's rendered controls (an SSOT
    answer the page does not offer). The WHOLE group is handed off and NOT ONE
    box is ticked -- including the option that WAS locatable. A driver that
    resolved lazily inside its drive loop would have ticked that one and then
    abandoned the group, leaving the page holding a COMBINATION the candidate
    never gave, which is precisely the multi-select hazard.

    Never a near-match either: "Documentaries" is rendered, "Documentary" is not,
    and the unrendered wording is refused rather than snapped to its neighbour."""
    fv = _checkbox_group_field("content_types", _LIVE_CONTENT_LABEL,
                               value=["Documentaries", "Documentary"])
    page = _FakeOptionGroupPage({fv.key: _LIVE_CONTENT_OPTIONS},
                                role="checkbox")

    ok, actual, reason = ashby_fill._fill_control(
        page, fv, ashby_fill._control_kind(fv))

    assert ok is False
    assert reason == ashby_fill._GROUP_OPTION_HANDOFF_REASON
    assert actual == []
    controls = page._by_path[fv.key]
    assert all(o.is_checked() is False for o in controls)
    assert all(o.checks == 0 for o in controls)     # the page was never touched


def test_checkbox_group_with_no_requested_option_is_never_a_fill():
    """An EMPTY request has nothing to confirm, so it is a gap, not a vacuous
    success. `resolve._render_select` already skips a multi-select no option
    matched; this is the defence-in-depth guard at the driver boundary, and it
    is the degenerate end of the partial-is-a-gap convention."""
    fv = _checkbox_group_field("content_types", _LIVE_CONTENT_LABEL, value=[])
    page = _FakeOptionGroupPage({fv.key: _LIVE_CONTENT_OPTIONS},
                                role="checkbox")

    ok, actual, reason = ashby_fill._fill_control(
        page, fv, ashby_fill._control_kind(fv))

    assert ok is False
    assert reason == ashby_fill._EMPTY_GROUP_REASON
    assert all(o.checks == 0 for o in page._by_path[fv.key])


def test_both_group_kinds_go_through_the_one_shared_locate_and_drive(monkeypatch):
    """THE SHARING ITSELF, pinned as behaviour rather than as a claim in a
    docstring.

    This vendor's characteristic defect is a SECOND emission site for a
    derivation that already had one, fixed an instance at a time while a third
    site was forgotten. So the checkbox adoption must not add a parallel locate:
    a spy on `_locate_option` must see BOTH kinds pass through it, each asking
    for its OWN role and its own entry scope, and `_fill_option_group` must be
    the one driver both reach.

    If a future change gives checkboxes their own private locate, this test goes
    RED even while the behavioural tests above stay green."""
    calls = []
    original = ashby_fill._locate_option

    def spy(page, role, key, option):
        calls.append((role, key, option))
        return original(page, role, key, option)

    monkeypatch.setattr(ashby_fill, "_locate_option", spy)

    radio_fv = _radio_group_field("work_auth", "Are you authorized to work?",
                                  value="Yes")
    radio_page = _FakeOptionGroupPage({radio_fv.key: ["Yes", "No"]},
                                      role="radio")
    checkbox_fv = _checkbox_group_field(
        "content_types", _LIVE_CONTENT_LABEL,
        value=["Documentaries", "Ads/socials"])
    checkbox_page = _FakeOptionGroupPage({checkbox_fv.key: _LIVE_CONTENT_OPTIONS},
                                         role="checkbox")

    for page, fv in ((radio_page, radio_fv), (checkbox_page, checkbox_fv)):
        ok, _, reason = ashby_fill._fill_control(
            page, fv, ashby_fill._control_kind(fv))
        assert ok is True and reason == "", fv.key

    assert calls == [
        ("radio", "work_auth", "Yes"),                 # single-select: ONE call
        ("checkbox", "content_types", "Documentaries"),
        ("checkbox", "content_types", "Ads/socials"),  # multi-select: one each
    ]
    # and each asked the page for its OWN role, never a role guessed from a type.
    assert set(radio_page.roles_requested) == {"radio"}
    assert set(checkbox_page.roles_requested) == {"checkbox"}


def test_radio_single_select_semantics_survive_the_shared_driver():
    """The shared driver must not have made a radio group multi-selectable. A
    radio's request is built by `_group_options` as the ONE resolved option, so
    exactly one control in the group is ever ticked no matter how the driver
    below it is written -- the single-select half of the FX1 adoption, re-pinned
    now that a multi-select shares its code path."""
    fv = _radio_group_field("work_auth", "Are you authorized to work?",
                            value="Yes")

    assert ashby_fill._group_options(fv, ControlKind.RADIO) == ["Yes"]

    page = _FakeOptionGroupPage({fv.key: ["Yes", "No", "Prefer not to say"]},
                                role="radio")
    ok, actual, reason = ashby_fill._fill_control(
        page, fv, ashby_fill._control_kind(fv))

    assert ok is True and reason == "" and actual == ["Yes"]
    ticked = [o.name for o in page._by_path[fv.key] if o.is_checked()]
    assert ticked == ["Yes"]


def test_consent_and_eeo_checkbox_groups_are_excluded_from_the_new_driver():
    """THE EXCLUSION, and the highest-risk part of this adoption.

    Making checkbox groups drivable makes CONSENT-class and EEO/DEMOGRAPHIC
    checkbox groups drivable too, and the kernel's consent machinery does not
    reach them: `resolve._resolve_boolean` dispositions a consent checkbox
    through the seeded `policies.consent.<class>` policy, but only for the
    `Boolean` type, so a consent-class MULTI-SELECT would arrive at the driver
    with no policy verdict behind it at all. `_is_excluded_group` keeps exactly
    those on the human hand-off.

    Both classifiers are the KERNEL's own, so this test also pins that the gate
    agrees with `resolve._classify_checkbox` / `resolve._DEMOGRAPHIC_KEYWORDS`
    rather than restating them."""
    excluded = [
        ("privacy", "I agree to the processing of my personal data"),
        ("talent", "Keep me in mind for future opportunities"),
        ("marketing", "Sign me up to the newsletter"),
        ("race", "Race/Ethnicity (select all that apply)"),
        ("veteran", "Veteran status (select all that apply)"),
        ("disability", "Disability status (select all that apply)"),
    ]
    for key, label in excluded:
        fv = _checkbox_group_field(key, label, value=["Yes"])
        assert ashby_fill._is_excluded_group(fv) is True, label

    # the live content multi-select is NOT sensitive and stays drivable: the gate
    # must be a gate, not a blanket that quietly re-hands-off the whole adoption.
    drivable = _checkbox_group_field("content_types", _LIVE_CONTENT_LABEL,
                                     value=["Documentaries"])
    assert ashby_fill._is_excluded_group(drivable) is False

    # and the exclusion is scoped to the NEWLY drivable population: a lone
    # boolean consent checkbox keeps `_resolve_boolean`'s ratified policy
    # handling, and a radio group keeps its live-verified adoption. Neither is
    # re-routed by this gate.
    boolean_consent = _boolean_field("consent", "I agree to the privacy policy",
                                     value=True)
    radio_consent = _radio_group_field("consent_radio",
                                       "I agree to the privacy policy",
                                       value="Yes")
    assert ashby_fill._is_excluded_group(boolean_consent) is False
    assert ashby_fill._is_excluded_group(radio_consent) is False


def test_control_kind_covers_both_groups_and_a_lone_boolean_but_never_date():
    """WHICH KINDS ASHBY USES: CHECKBOX (a lone boolean AND a multi-select
    GROUP, told apart by the VALUE's shape, which is the schema's own
    distinction) and RADIO (a single-select group). `_control_kind` has NO branch
    that can return `ControlKind.DATE` -- Ashby's `Date` type collapses into
    `input_text` (`capture._ASHBY_TYPE_MAP`) with no surviving signal to route it
    through DATE.

    The RESIDUAL hand-off is pinned too: a click-hazard control whose value
    matches none of those shapes is still None, so an unexpected value shape is
    handed to a human rather than driven on a guess."""
    boolean_fv = _boolean_field("certify", "I certify", value=True)
    radio_fv = _radio_group_field("work_auth", "Are you authorized to work?")
    checkbox_group_fv = _checkbox_group_field("content_types", "Content types")

    assert ashby_fill._control_kind(boolean_fv) == ControlKind.CHECKBOX
    assert ashby_fill._control_kind(radio_fv) == ControlKind.RADIO
    assert ashby_fill._control_kind(checkbox_group_fv) == ControlKind.CHECKBOX
    # the VALUE shape, not the role, is what separates the two checkbox routes.
    assert ashby_fill._group_options(boolean_fv, ControlKind.CHECKBOX) is None
    assert ashby_fill._group_options(
        checkbox_group_fv, ControlKind.CHECKBOX) == ["Documentaries"]

    # The kernel STILL classifies a checkbox group as a click hazard. The adoption
    # works by PRE-EMPTING the hand-off gate (`kind` is no longer None, so `fill()`
    # never consults the predicate) rather than by WEAKENING the predicate itself.
    # Pinning it keeps that distinction honest: if a later edit ever makes this
    # False, the safety classifier has been relaxed, which is a different and much
    # larger change than the one this wave made.
    assert ashby_fill._needs_human_handoff(checkbox_group_fv) is True

    # the residual: a wrong-shaped value on a click-hazard role still hands off.
    wrong_shape = _checkbox_group_field("content_types", "Content types")
    wrong_shape.value = "Documentaries"       # a bare string, not a list
    assert ashby_fill._control_kind(wrong_shape) is None
    assert ashby_fill._needs_human_handoff(wrong_shape) is True

    for fv in (boolean_fv, radio_fv, checkbox_group_fv):
        assert ashby_fill._control_kind(fv) != ControlKind.DATE
