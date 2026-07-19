"""Workable's control driving (W5.1c + W5.1c-R4): `engine.providers.workable.fill`
drives its DATE control through `engine.kernel.control_toolkit.drive_control`, and
its BOOLEAN (radio) control by CLICKING the yes/no option WRAPPER and confirming
via aria-checked (`_drive_boolean_radio`) -- never a `.check()` on the aria-hidden
inner input, the R3 QA_11599465/66 drive-timeout mode. Fakes only: no live
browser, no network.

Imported via the submodule path (`import ... as wf`), not `from ... import
fill`, because the package `__init__` rebinds the package-level `fill`
attribute to the FUNCTION (see workable/__init__.py's NAME NOTE); the
submodule import reaches sys.modules directly and keeps the private helpers
(`_drive_boolean_radio`, `_radiogroup_option`, `_date_control_spec`,
`_needs_human_handoff`) reachable.
"""

from __future__ import annotations

import importlib
from datetime import date

import pytest

from engine.kernel.contracts import (
    Field, FieldMap, FieldType, FieldValue, FillSafetyError, Locator,
    ResolvedValues, Section)
from engine.kernel.control_toolkit import ControlKind, drive_control

# The package `__init__` rebinds the package-level `fill` attribute to the
# FUNCTION (see workable/__init__.py's NAME NOTE), so `import ...fill as wf`
# would bind `wf` to that function, not the submodule (the "as" binding
# traverses the package attribute, it does not read sys.modules directly).
# `importlib.import_module` reaches the real submodule, exactly as the
# package docstring documents.
wf = importlib.import_module("engine.providers.workable.fill")


# -- fakes: the same tickable/typed surfaces the kernel contract names --------


def _has_text_match(pattern, text):
    """Model playwright's `filter(has_text=...)`: a regex is matched against the
    element's normalized-whitespace text (`_radiogroup_option` passes an anchored,
    case-insensitive regex); a plain string is a case-insensitive substring."""
    normalized = " ".join(str(text).split())
    if hasattr(pattern, "search"):
        return bool(pattern.search(normalized))
    return str(pattern).strip().lower() in normalized.lower()


class _FakeRadioWrapper:
    """One radio OPTION wrapper (`div[data-ui="option"][role="radio"]`): the node a
    human CLICKS. Its visible text is only the option word; clicking flips its
    aria-checked "true" and its group siblings "false" (single-select). `lands=False`
    models a click the page silently drops (aria-checked stays "false"). `.check()`
    is forbidden -- the driver CLICKS the wrapper, never `.check()`s the aria-hidden
    inner input (the R3 timeout mode)."""

    def __init__(self, group, text, *, lands=True, aria_checked="false"):
        self._group = group
        self.text = text
        self._lands = lands
        self.aria_checked = aria_checked
        self.clicks = 0

    def get_attribute(self, name):
        return {"aria-checked": self.aria_checked, "role": "radio",
                "data-ui": "option"}.get(name)

    def inner_text(self):
        return self.text

    def click(self):
        self.clicks += 1
        if not self._lands:
            return
        for wrapper in self._group.wrappers:
            wrapper.aria_checked = "false"
        self.aria_checked = "true"

    def check(self, *args, **kwargs):
        raise AssertionError(
            "the radio wrapper is CLICKED, never .check()'d (the R3 timeout mode)")


class _FakeOptionSet:
    """A live set of option wrappers, narrowable by `.filter(has_text=...)` and
    countable by `.count()` -- the Locator surface `_radiogroup_option` drives.
    `.filter` matches VISIBLE TEXT; `.get_attribute`/`.click` require the set to
    have narrowed to exactly one wrapper (playwright strict mode)."""

    def __init__(self, items):
        self._items = list(items)

    def filter(self, has_text=None):
        if has_text is None:
            return _FakeOptionSet(self._items)
        return _FakeOptionSet(
            [w for w in self._items if _has_text_match(has_text, w.inner_text())])

    def count(self):
        return len(self._items)

    def get_attribute(self, name):
        assert len(self._items) == 1, f"get_attribute on {len(self._items)} matches"
        return self._items[0].get_attribute(name)

    def click(self):
        assert len(self._items) == 1, f"click on {len(self._items)} matches"
        self._items[0].click()


class _FakeRadioGroup:
    """`fieldset[data-ui="<key>"][role="radiogroup"]`: a SCOPE holding its option
    wrappers. `.locator('div[data-ui="option"][role="radio"]')` returns them."""

    def __init__(self):
        self.wrappers = []

    def locator(self, css):
        assert css == wf._OPTION_WRAPPER_CSS, css
        return _FakeOptionSet(self.wrappers)


def _radio_group(*, yes_lands=True):
    """A yes/no radiogroup: each wrapper's VISIBLE TEXT is only the option word, so
    the visible-text filter resolves each to exactly one wrapper."""
    group = _FakeRadioGroup()
    group.wrappers = [_FakeRadioWrapper(group, "Yes", lands=yes_lands),
                      _FakeRadioWrapper(group, "No")]
    return group


class _FakeDateBox:
    """The plain typed date textbox (no picker)."""

    def __init__(self, *, value=""):
        self.value = value
        self.clicks = 0

    def click(self):
        self.clicks += 1

    def press_sequentially(self, char, delay=None):
        self.value += char

    def input_value(self):
        return self.value


class _FakePage:
    def __init__(self, *, fieldsets=None, roles=None):
        # `fieldsets` is keyed by the radiogroup css and holds `_FakeRadioGroup`s.
        self._fieldsets = fieldsets or {}
        self._roles = roles or {}

    def locator(self, selector):
        # A css this test never wired KeyErrors -- deliberate for the radiogroup
        # scope; the OVERVIEW/cookie recovery css is tolerated by _visible_locators
        # (it swallows the KeyError as zero matches), so no recovery fires here.
        return self._fieldsets[selector]

    def get_by_role(self, role, name=None):
        return self._roles[(role, name)]


_RADIOGROUP_CSS = 'fieldset[data-ui="QA_1"][role="radiogroup"]'


def _boolean_fv(key="QA_1", label="Are you legally authorized to work?",
               value=True):
    return FieldValue(key=key, label=label, type="boolean",
                      locator=Locator(role="radio", name=label), value=value)


def _date_fv(key="QA_2", label="When can you start?", value="01/09/2026"):
    return FieldValue(key=key, label=label, type="date",
                      locator=Locator(role="textbox", name=label), value=value)


# -- _drive_boolean_radio: click the option wrapper, confirm via aria-checked ---


def test_boolean_true_clicks_the_yes_wrapper():
    group = _radio_group()
    page = _FakePage(fieldsets={_RADIOGROUP_CSS: group})
    outcome = wf._drive_boolean_radio(page, _boolean_fv(value=True))
    yes, no = group.wrappers
    assert (outcome.key, outcome.kind) == ("QA_1", ControlKind.RADIO)
    assert (outcome.driven, outcome.confirmed) == (True, True)
    assert (yes.clicks, no.clicks) == (1, 0)          # the Yes wrapper was CLICKED
    assert (yes.aria_checked, no.aria_checked) == ("true", "false")


def test_boolean_false_clicks_the_no_wrapper():
    # Selecting "No" IS selecting a different option (not clearing "Yes"): the
    # intent picks WHICH wrapper is clicked, and that wrapper reads back checked.
    group = _radio_group()
    page = _FakePage(fieldsets={_RADIOGROUP_CSS: group})
    outcome = wf._drive_boolean_radio(page, _boolean_fv(value=False))
    yes, no = group.wrappers
    assert (outcome.driven, outcome.confirmed) == (True, True)
    assert (yes.clicks, no.clicks) == (0, 1)          # the No wrapper was CLICKED
    assert (yes.aria_checked, no.aria_checked) == ("false", "true")


def test_boolean_locates_the_option_by_visible_text_via_radiogroup_option():
    # The locator helper resolves each option to EXACTLY ONE wrapper, scoped to the
    # radiogroup and filtered by the wrapper's own visible text.
    group = _radio_group()
    page = _FakePage(fieldsets={_RADIOGROUP_CSS: group})
    assert wf._option_count(wf._radiogroup_option(page, "QA_1", "Yes")) == 1
    assert wf._option_count(wf._radiogroup_option(page, "QA_1", "No")) == 1


def test_boolean_swallowed_click_is_not_confirmed():
    """The failure this mechanism exists to catch: the wrapper is clicked but the
    page silently drops it, so aria-checked never reads back "true"."""
    group = _radio_group(yes_lands=False)
    page = _FakePage(fieldsets={_RADIOGROUP_CSS: group})
    outcome = wf._drive_boolean_radio(page, _boolean_fv(value=True))
    yes = group.wrappers[0]
    assert yes.clicks == 1 and yes.aria_checked == "false"   # clicked, did not stick
    assert (outcome.driven, outcome.confirmed) == (True, False)
    assert outcome.reason == wf._RADIO_READBACK_FAIL_REASON


def test_boolean_submit_like_label_is_refused_before_any_click():
    """The submit denylist guard fires through `_safe_click` exactly as it does for
    every other click (never bypassed for workable's own boolean drive)."""
    group = _radio_group()
    page = _FakePage(fieldsets={_RADIOGROUP_CSS: group})
    with pytest.raises(FillSafetyError, match="submit denylist"):
        wf._drive_boolean_radio(page, _boolean_fv(label="Continue", value=True))
    assert all(w.clicks == 0 for w in group.wrappers)   # refused BEFORE any click


# -- _date_control_spec: the typed-textbox path, never a picker ---------------


def test_date_spec_has_no_day_cell_and_passes_the_value_through_unchanged():
    box = _FakeDateBox()
    label = "When can you start?"
    page = _FakePage(roles={("textbox", label): box})
    spec = wf._date_control_spec(page, _date_fv(label=label, value="01/09/2026"))
    assert (spec.kind, spec.value, spec.day_cell) == (
        ControlKind.DATE, "01/09/2026", None)
    assert spec.locator is box


def test_date_is_typed_and_confirmed_via_the_real_kernel():
    box = _FakeDateBox()
    label = "When can you start?"
    page = _FakePage(roles={("textbox", label): box})
    outcome = drive_control(wf._date_control_spec(page, _date_fv(label=label)))
    assert box.value == "01/09/2026"
    assert (outcome.driven, outcome.confirmed) == (True, True)


def test_date_never_formats_a_non_str_value():
    """The kernel's own guard: this module passes fv.value through UNCHANGED,
    so a date object reaching here (an upstream bug) is caught, not silently
    coerced by this adopter."""
    box = _FakeDateBox()
    label = "When can you start?"
    page = _FakePage(roles={("textbox", label): box})
    spec = wf._date_control_spec(page, _date_fv(label=label, value=date(2026, 9, 1)))
    with pytest.raises(FillSafetyError, match="never formats a date"):
        drive_control(spec)
    assert box.value == ""


# -- _needs_human_handoff: the narrowed hazard set ----------------------------


def _fv_with_role(role, key="k"):
    return FieldValue(key=key, label="L", type="dropdown",
                      locator=Locator(role=role, name="L"), value="x")


def test_radio_no_longer_needs_human_handoff():
    """Booleans (role 'radio') are driven now, not handed off -- the W5.1c
    behavioural change this adoption makes."""
    assert wf._needs_human_handoff(_fv_with_role("radio")) is False


@pytest.mark.parametrize("role", ["combobox", "listbox"])
def test_unsampled_custom_widgets_still_hand_off(role):
    assert wf._needs_human_handoff(_fv_with_role(role)) is True


def test_group_subfield_still_hands_off():
    fv = FieldValue(key="education.school", label="School", type="text",
                    locator=Locator(role="textbox", name="School"), value="x")
    assert wf._needs_human_handoff(fv) is True


# -- fill(): the loop dispatches boolean/date through drive_control -----------


def _field(key, label, native_type, role, *, required=True):
    return Field(key=key, label=label, type=native_type, required=required,
                options=[], source="workable_form",
                locator=Locator(role=role, name=label), step_index=0,
                conditional_on=None, decline_allowed=False, max_length=None,
                accept_types=None,
                norm_type={"boolean": FieldType.BOOLEAN, "date": FieldType.DATE,
                          "dropdown": FieldType.SINGLE_SELECT}[native_type],
                section=Section.CUSTOM)


def test_fill_drives_boolean_and_date_and_still_hands_off_dropdown(monkeypatch):
    monkeypatch.setattr(wf.base, "install_never_send", lambda page: None)
    monkeypatch.setattr(wf, "_workable_dom_required", lambda page: set())

    fieldmap = FieldMap(vendor="workable", posting_id="0F5F662A46",
                        captured_at="2026-07-16T00:00:00Z", fields=[
                            _field("QA_1", "Are you legally authorized to work?",
                                  "boolean", "radio"),
                            _field("QA_2", "When can you start?", "date", "textbox"),
                            _field("QA_3", "Pick one", "dropdown", "combobox",
                                  required=False),
                        ])
    values = ResolvedValues(fields=[
        _boolean_fv(value=True),
        _date_fv(),
        FieldValue(key="QA_3", label="Pick one", type="dropdown",
                  locator=Locator(role="combobox", name="Pick one"), value="a"),
    ])

    group = _radio_group()
    date_box = _FakeDateBox()
    page = _FakePage(
        fieldsets={_RADIOGROUP_CSS: group},
        roles={("textbox", "When can you start?"): date_box},
    )
    page.url = "https://apply.workable.com/acme/j/0F5F662A46/apply/"

    report = wf.fill(page, fieldmap, values)

    yes, no = group.wrappers
    assert (yes.clicks, no.clicks) == (1, 0)          # boolean: Yes wrapper CLICKED
    assert yes.aria_checked == "true"                 # ... and confirmed via aria-checked
    assert date_box.value == "01/09/2026"
    assert report.filled == 2
    assert ("QA_3", wf._HUMAN_HANDOFF_REASON) in report.skipped


def test_fill_never_swallows_a_date_format_safety_error(monkeypatch):
    monkeypatch.setattr(wf.base, "install_never_send", lambda page: None)
    monkeypatch.setattr(wf, "_workable_dom_required", lambda page: set())

    fieldmap = FieldMap(vendor="workable", posting_id="0F5F662A46",
                        captured_at="2026-07-16T00:00:00Z",
                        fields=[_field("QA_2", "When can you start?", "date", "textbox")])
    values = ResolvedValues(fields=[_date_fv(value=date(2026, 9, 1))])

    date_box = _FakeDateBox()
    page = _FakePage(roles={("textbox", "When can you start?"): date_box})
    page.url = "https://apply.workable.com/acme/j/0F5F662A46/apply/"

    with pytest.raises(FillSafetyError, match="never formats a date"):
        wf.fill(page, fieldmap, values)
