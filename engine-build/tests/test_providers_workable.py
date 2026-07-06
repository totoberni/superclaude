"""Workable provider (engine.providers.workable): the FOURTH reference
implementation of the `Provider` contract, W5.4 -- the HYBRID path (greenhouse-
class schema CAPTURE, lever-class native-DOM FILL with a wider Turnstile hand-off).

No patchright, no network. CAPTURE is grounded in the REAL public form schemas
fetched 2026-07-06 and committed at `tests/fixtures/providers/workable/`:
`form-57CFF1B2AF.json` (powerlines; standard-only, 3 required), `form-0F5F662A46
.json` (movement-labs; QA_ custom questions incl. a required boolean, 18
required), `form-FAF4116602.json` (io-global; CA_ account attributes, 7 required).
Each is parsed through the REAL offline `fieldmap.parse_workable`. FILL is driven
through a FAKE page/locator harness (mirrors the apply DOM: native text controls,
the hidden file input, the DOM-sweep selectors). The SSOT is a hand-built FAKE (no
owner PII). A real live-browser run against apply.workable.com is a SEPARATE later
step (it needs a network host outside the WSL allowlist; it runs on toto); this
suite proves the LOGIC offline, matching test_providers_greenhouse.py /
test_providers_lever.py.
"""

import json
from pathlib import Path

import pytest

# Import engine.fill at module load (before the autouse no_network fixture patches
# socket.socket): workable.py imports engine.fill lazily at call time, and a FIRST
# import under the socket patch would drag in ssl (class SSLSocket(socket)) and
# fail. Mirrors test_providers_lever.py / test_providers_greenhouse.py.
import engine.fill  # noqa: F401
import engine.fieldmap as fieldmap
from engine.fieldmap import FieldType, Section, capture_workable, parse_workable
from engine.fill import FieldValue, FillAssets, FillSafetyError, ResolvedValues
from engine.profile_map import profile_from_real_ssot
from engine.providers import base, protocol, registry, workable
from engine.ssot import SSOT

_FIXTURES = Path(__file__).parent / "fixtures" / "providers" / "workable"
_PINNED = "2026-07-03T00:00:00+00:00"

# The powerlines standard-only form's required labels: the DOM-sweep required set
# for a COMPLETE run (only firstname/lastname/email are required there).
_POWERLINES_REQUIRED_LABELS = ("First name", "Last name", "Email")


# -- fixture loaders -------------------------------------------------------


def _raw(filename: str):
    return json.loads((_FIXTURES / filename).read_text())


def _fieldmap(filename: str, shortcode: str):
    """A field map parsed through the REAL offline Workable parser."""
    return parse_workable(_raw(filename), "fauxco", shortcode,
                          now=lambda: _PINNED)


def _fake_ssot() -> SSOT:
    # FAKE, invented placeholder data only -- no owner PII, matching the existing
    # real_ssot fixture convention and test_providers_lever.py.
    return SSOT({
        "identity": {
            "name": "Test Candidate",
            "email": "test.candidate@example.invalid",
        },
        "canned_answers": {
            "privacy_consent_default": "yes",
        },
    })


def _assets(tmp_path, *, ats=True, atsi=True, photo=True) -> FillAssets:
    def make(name, present):
        p = tmp_path / name
        if present:
            p.write_bytes(b"stub")
        return p
    return FillAssets(cv_ats=make("cv-ats.pdf", ats),
                      cv_atsi=make("cv-atsi.pdf", atsi),
                      photo=make("Me.png", photo))


def _resolved_values(fieldmap, *, tmp_path=None, assets_kwargs=None):
    ssot = _fake_ssot()
    profile = profile_from_real_ssot(ssot)
    assets = (_assets(tmp_path, **(assets_kwargs or {}))
              if tmp_path is not None else None)
    return workable.resolve_values(fieldmap, ssot, profile, assets=assets)


# -- fake opener (real capture path, no network) ---------------------------


class _Resp:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    def read(self):
        return self._body


class _CaptureOpener:
    def __init__(self, raw):
        self._body = json.dumps(raw).encode("utf-8")
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _Resp(self._body)


# -- fake DOM harness (mirrors the apply form) -----------------------------


class _FakeTextLocator:
    """A plain text/email/paragraph/phone input: type_human -> press_sequentially,
    readback via input_value(). Raises on the forbidden fill()/click()/check()
    paths (the Turnstile human-cadence + no-auto-click invariants)."""

    def __init__(self):
        self.value = ""

    def press_sequentially(self, ch, delay=None):
        self.value += ch

    def input_value(self):
        return self.value

    def get_attribute(self, name):
        return None

    def fill(self, *args, **kwargs):
        raise AssertionError("Workable text fields must use type_human, never fill()")

    def check(self, *args, **kwargs):
        raise AssertionError("Workable must never programmatically .check() (Turnstile)")

    def click(self, *args, **kwargs):
        raise AssertionError("Workable must never programmatically .click() a field")

    def select_option(self, *args, **kwargs):
        raise AssertionError("a text field must not be select_option'd")


class _FakeBadTextLocator(_FakeTextLocator):
    """A text input whose value silently never takes (readback always empty):
    exercises the readback-gate rejecting a value the page dropped."""

    def input_value(self):
        return ""


class _FakeFileInput:
    def __init__(self, *, id=None, name=None, accept=None):
        self._attrs = {"id": id, "name": name, "accept": accept}
        self.set_input_files_calls = 0
        self.uploaded = None

    def get_attribute(self, name):
        return self._attrs.get(name)

    def set_input_files(self, files):
        self.set_input_files_calls += 1
        self.uploaded = files

    def input_value(self):
        return self.uploaded or ""


class _FakeSweepLocator:
    def __init__(self, *, attrs=None, visible=True, text=""):
        self._attrs = attrs or {}
        self._visible = visible
        self._text = text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text


class _FakeLocatorSet:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _PageBoundContext:
    """The fake page's owning BrowserContext. `install_never_send` targets the
    CONTEXT (so the guard covers every page/popup it opens); the registration is
    recorded here AND mirrored onto the page so the page-level assertions work."""

    def __init__(self, page):
        self._page = page
        self.routed = []

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))
        self._page.route(pattern, handler)


class _FakeWorkablePage:
    """One fake page driving the WHOLE fill() sequence for Workable: native TEXT
    controls only (auto-built from the field map's textbox-role fields), the hidden
    file input(s), and the sweep_required CSS selectors. A boolean/checkbox/
    dropdown/multiple/group control is deliberately NOT built: fill() must hand it
    off (never request a locator for it), so any such request KeyErrors -- proving
    the no-auto-click invariant."""

    def __init__(self, fieldmap, *,
                 url="https://apply.workable.com/fauxco/j/57CFF1B2AF/apply/",
                 sweep_required_labels=_POWERLINES_REQUIRED_LABELS,
                 file_inputs=None, bad_keys=()):
        self._url = url
        self.controls = {}
        for fld in fieldmap.fields:
            if fld.locator.role == "textbox":
                key = (fld.locator.role, fld.locator.name)
                self.controls[key] = (_FakeBadTextLocator()
                                      if fld.key in bad_keys
                                      else _FakeTextLocator())
        self.file_inputs = (list(file_inputs) if file_inputs is not None else
                            [_FakeFileInput(id="resume", name="resume",
                                           accept=".pdf,.doc,.docx"),
                             _FakeFileInput(id="avatar", name="avatar",
                                           accept="image/*")])
        self.routed = []
        self.context = _PageBoundContext(self)
        self.requested = []
        self.located_css = []
        self._sweep_required = [
            _FakeSweepLocator(attrs={"aria-label": label})
            for label in sweep_required_labels]

    @property
    def url(self):
        return self._url

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))

    def get_by_role(self, role, name=None):
        self.requested.append(("role", role, name))
        return self.controls[(role, name)]

    def get_by_label(self, label):
        self.requested.append(("label", label))
        return self.controls[(None, label)]

    def query_selector_all(self, selector):
        if "file" in selector:
            return list(self.file_inputs)
        return []

    def wait_for_timeout(self, ms):  # pragma: no cover - native path never waits
        pass

    def locator(self, css):
        self.located_css.append(css)
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(self._sweep_required)
        return _FakeLocatorSet([])


# =============================================================================
# capture / apply_url: thin delegation to the registry
# =============================================================================


def test_capture_delegates_to_registry_capture_fn(monkeypatch):
    # registry._capture_workable (== PROVIDERS["workable"].capture_fn) lazily
    # imports and calls fieldmap.capture_workable at CALL time; patching that
    # module attribute proves capture() rides the SAME registry wiring end to end.
    calls = []

    def fake_capture(slug, job_id, opener=None):
        calls.append((slug, job_id, opener))
        return "SENTINEL"

    monkeypatch.setattr(fieldmap, "capture_workable", fake_capture)
    result = workable.capture("powerlines", "57CFF1B2AF", opener="OPENER")
    assert result == "SENTINEL"
    assert calls == [("powerlines", "57CFF1B2AF", "OPENER")]
    assert registry.resolve("workable").capture_fn is registry._capture_workable


def test_apply_url_delegates_to_registry_apply_url_fn():
    assert (workable.apply_url("foo", "123")
           == "https://apply.workable.com/foo/j/123/apply/")


def test_workable_module_satisfies_provider_protocol():
    # The load-bearing conformance check: the module-scope shape structurally
    # satisfies the SAME Provider Protocol greenhouse / lever do.
    assert isinstance(workable, protocol.Provider)
    assert workable.vendor == "workable"


# =============================================================================
# capture: the real form fixtures -> canonical FieldMap
# =============================================================================


@pytest.mark.parametrize("filename,shortcode,req_count,custom_id", [
    ("form-57CFF1B2AF.json", "57CFF1B2AF", 3, None),
    ("form-0F5F662A46.json", "0F5F662A46", 18, "QA_11919111"),
    ("form-FAF4116602.json", "FAF4116602", 7, "CA_6419"),
])
def test_capture_parses_real_fixture(filename, shortcode, req_count, custom_id):
    fm = _fieldmap(filename, shortcode)
    assert fm.vendor == "workable"
    assert fm.posting_id == shortcode
    assert len(fm.required_fields()) == req_count
    by_key = {f.key: f for f in fm.fields}
    if custom_id is None:
        # standard-only form: no QA_/CA_ custom-section field
        assert not any(f.section == Section.CUSTOM for f in fm.fields)
    else:
        assert custom_id in by_key
        assert by_key[custom_id].section == Section.CUSTOM


def test_capture_workable_rides_the_real_opener_get():
    # The end-to-end HTTP-only capture path (capture_workable -> parse_workable)
    # against a FAKE opener: one GET to the public form endpoint, no browser.
    opener = _CaptureOpener(_raw("form-0F5F662A46.json"))
    fm = capture_workable("movement-labs", "0F5F662A46", opener,
                          now=lambda: _PINNED)
    assert len(opener.requests) == 1
    assert opener.requests[0].full_url == \
        "https://apply.workable.com/api/v1/jobs/0F5F662A46/form"
    assert fm.vendor == "workable" and fm.posting_id == "0F5F662A46"
    assert len(fm.required_fields()) == 18


def test_parse_workable_dropdown_multiple_and_number_types_and_choice_labels():
    # SYNTHETIC payload: none of the three real fixtures samples a dropdown,
    # multiple-choice, or number field (see `_workable_choice_labels`'s own
    # docstring), so this hand-built sections payload pins the type->role
    # mapping and the choices[].body choice-label extraction that would
    # otherwise be defensive-only, untested code.
    raw = [{
        "name": "Custom questions",
        "fields": [
            {
                "id": "QA_1001", "required": True,
                "label": "Which office would you prefer?",
                "type": "dropdown",
                "choices": [{"id": 1, "body": "London"},
                           {"id": 2, "body": "Remote"}],
            },
            {
                "id": "QA_1002", "required": False,
                "label": "Which languages do you speak?",
                "type": "multiple",
                "choices": [{"id": 3, "body": "English"},
                           {"id": 4, "body": "Italian"}],
            },
            {
                "id": "QA_1003", "required": True,
                "label": "Years of professional experience",
                "type": "number",
            },
        ],
    }]

    fm = parse_workable(raw, "fauxco", "SYNTHETIC", now=lambda: _PINNED)
    by_key = {f.key: f for f in fm.fields}

    dropdown = by_key["QA_1001"]
    assert dropdown.locator.role == "combobox"
    assert dropdown.norm_type == FieldType.SINGLE_SELECT
    assert dropdown.options == ["London", "Remote"]

    multiple = by_key["QA_1002"]
    assert multiple.locator.role == "listbox"
    assert multiple.norm_type == FieldType.MULTI_SELECT
    assert multiple.options == ["English", "Italian"]

    number = by_key["QA_1003"]
    assert number.locator.role == "textbox"
    assert number.norm_type == FieldType.NUMBER
    assert number.options == []


# =============================================================================
# resolve_values: INHERITED hole-fix e structural CV/photo choice (FAKE ssot)
# =============================================================================


def test_resolve_values_inherits_cv_atsi_and_photo_when_avatar_present(tmp_path):
    # powerlines exposes an `avatar` (Photo) upload field -> the inherited
    # structural signal fires: resume becomes cv-atsi and the photo attaches.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    by_key = {fv.key: fv for fv in values.fields}
    assert by_key["resume"].asset == "cv-atsi"
    assert "photo field present" in by_key["resume"].upload_reason
    assert by_key["avatar"].asset == "photo"
    assert Path(by_key["avatar"].value).name == "Me.png"


def test_resolve_values_inherits_cv_ats_when_no_photo_field(tmp_path):
    # Strip the avatar field -> no photo signal -> the inherited negative branch
    # picks the plain ATS CV for the resume upload.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    fieldmap.fields = [f for f in fieldmap.fields if f.key != "avatar"]
    values = _resolved_values(fieldmap, tmp_path=tmp_path)
    resume = {fv.key: fv for fv in values.fields}["resume"]
    assert resume.asset == "cv-ats"
    assert "no photo field" in resume.upload_reason


# =============================================================================
# fill(): the ordered Provider-contract sequence (native DOM path)
# =============================================================================


def test_fill_completes_when_all_safe_required_land():
    # The powerlines standard-only form's required set is firstname/lastname/email
    # (all text). With no assets the optional uploads skip; every required text
    # field lands and readback-confirms and the live sweep agrees -> COMPLETE.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap)

    report = workable.fill(page, fieldmap, values)

    assert report.vendor == "workable"
    # (1) never-send installed exactly once, at CONTEXT scope.
    assert len(page.context.routed) == 1 and page.context.routed[0][0] == "**"
    # (2)+(3) text via type_human, readback-confirmed.
    assert page.controls[("textbox", "First name")].input_value() == "Test Candidate"
    assert page.controls[("textbox", "Email")].input_value() == \
        "test.candidate@example.invalid"
    assert report.readback_mismatches == []
    # (4) live sweep agrees with the schema required set -> no gap.
    assert report.required_unfilled == []
    assert report.complete is True
    assert report.caption().endswith("COMPLETE")
    assert not report.caption().endswith("NOT COMPLETE")


def test_fill_required_boolean_qa_is_human_handoff_and_not_complete():
    # movement-labs QA_11919111 is a REQUIRED boolean rendered as a yes/no radio
    # fieldset. Even if a boolean value HAD been resolved (a consent-style SSOT
    # answer), fill() must HAND IT OFF: never programmatically click a radio (the
    # invisible Cloudflare Turnstile intercepts the click mid-form). So a required
    # boolean forces NOT_COMPLETE and the control is NEVER touched.
    fieldmap = _fieldmap("form-0F5F662A46.json", "0F5F662A46")
    qa = {f.key: f for f in fieldmap.fields}["QA_11919111"]
    assert qa.required and qa.type == "boolean" and qa.locator.role == "radio"
    # Feed the boolean as a resolved True to drive fill()'s hand-off branch
    # directly (the real label is not consent-shaped, so resolve_values would
    # resolve-skip it; this proves the fill()-level refusal to auto-click).
    values = ResolvedValues(fields=[FieldValue(
        key=qa.key, label=qa.label, type=qa.type, locator=qa.locator, value=True)])
    page = _FakeWorkablePage(fieldmap, sweep_required_labels=tuple(
        f.label for f in fieldmap.required_fields()))

    report = workable.fill(page, fieldmap, values)

    assert report.complete is False
    gap = {g["key"]: g for g in report.required_unfilled}
    assert qa.key in gap
    assert "Turnstile" in gap[qa.key]["reason"]
    # fill() never requested a locator for the radio (no auto-click path).
    assert not any(qa.key in str(req) or qa.label in str(req)
                   for req in page.requested)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_unrecognised_type_is_handed_off_not_typed():
    # A field type outside the known Workable vocabulary (a future SPA widget
    # this wave never sampled) must be HANDED OFF, never guessed as free text:
    # never-send bias means an unrecognised control is safer handed to a human
    # than blindly typed. `_workable_role_for_type` defaults an unrecognised
    # native type to "combobox" (a HAND-OFF role) precisely so this holds.
    raw = [{
        "name": "Custom questions",
        "fields": [{
            "id": "QA_9999", "required": True, "label": "Mystery widget",
            "type": "shiny_new_widget",
        }],
    }]
    fm = parse_workable(raw, "fauxco", "SYNTHETIC2", now=lambda: _PINNED)
    mystery = {f.key: f for f in fm.fields}["QA_9999"]
    assert mystery.required and mystery.locator.role == "combobox"

    # Feed a concrete guessed value directly (bypassing resolve_values' own
    # SSOT-match policy) to prove fill() itself refuses to auto-drive an
    # unrecognised-type control, mirroring the boolean hand-off test above.
    values = ResolvedValues(fields=[FieldValue(
        key=mystery.key, label=mystery.label, type=mystery.type,
        locator=mystery.locator, value="a sneaky guessed answer")])
    page = _FakeWorkablePage(fm, sweep_required_labels=(mystery.label,))

    report = workable.fill(page, fm, values)

    assert report.complete is False
    gap = {g["key"]: g for g in report.required_unfilled}
    assert mystery.key in gap
    assert "handed off" in gap[mystery.key]["reason"]
    # fill() never requested a locator for it -- no false readback-fill: the
    # guessed value was never typed anywhere, so there is nothing to read back.
    assert not any(mystery.key in str(req) or mystery.label in str(req)
                   for req in page.requested)
    assert report.readback_mismatches == []
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_readback_mismatch_on_required_field_forces_not_complete():
    # A required field whose value silently never takes (readback empty) must NOT
    # count as filled -> it surfaces as a genuine required gap + a readback mismatch.
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, bad_keys={"email"})

    report = workable.fill(page, fieldmap, values)

    assert report.complete is False
    assert any(g["key"] == "email" for g in report.required_unfilled)
    assert any(m["key"] == "email" for m in report.readback_mismatches)


def test_fill_dom_sweep_extra_required_field_forces_not_complete():
    # GREENHOUSE semantics (Workable has an independent schema): the live sweep
    # shows a required control the schema did not carry. That dom_only mismatch
    # forces NOT_COMPLETE even though every schema-required field landed, and the
    # reason reads with the SCHEMA-oracle wording (not Lever's "authoritative").
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)
    page = _FakeWorkablePage(fieldmap, sweep_required_labels=(
        _POWERLINES_REQUIRED_LABELS + ("Cover letter",)))

    report = workable.fill(page, fieldmap, values)

    assert report.complete is False
    reasons = [g["reason"] for g in report.required_unfilled]
    assert any("absent from the schema" in r for r in reasons)
    assert report.caption().endswith("NOT COMPLETE")


def test_fill_never_send_interceptor_registered_before_any_field_access():
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)

    order = []

    class _OrderTrackingPage(_FakeWorkablePage):
        def route(self, pattern, handler):
            order.append("route")
            super().route(pattern, handler)

        def get_by_role(self, role, name=None):
            order.append("get_by_role")
            return super().get_by_role(role, name=name)

    page = _OrderTrackingPage(fieldmap)
    workable.fill(page, fieldmap, values)

    assert order[0] == "route"


def test_fill_raises_on_navigation_during_fill():
    fieldmap = _fieldmap("form-57CFF1B2AF.json", "57CFF1B2AF")
    values = _resolved_values(fieldmap)

    class _NavigatingPage(_FakeWorkablePage):
        def get_by_role(self, role, name=None):
            self._url = "https://apply.workable.com/fauxco/thanks"
            return super().get_by_role(role, name=name)

    page = _NavigatingPage(fieldmap)
    with pytest.raises(FillSafetyError, match="navigated during fill"):
        workable.fill(page, fieldmap, values)


# =============================================================================
# never-send guard: the workable submit endpoint set (no live submit)
# =============================================================================


def test_never_send_aborts_the_real_apply_post():
    assert base._is_submit_request(
        "POST", "https://apply.workable.com/api/v1/jobs/57CFF1B2AF/apply",
        None) is True


def test_never_send_hardening_covers_custom_domain_and_eeoc():
    # (a) custom-domain / redirect tenant apply POST (host-agnostic shortcode path).
    assert base._is_submit_request(
        "POST", "https://careers.example.com/api/v2/jobs/ABCDEF/apply",
        None) is True
    # (b) the post-submit eeoc send (a second application-data POST after apply).
    assert base._is_submit_request(
        "POST", "https://apply.workable.com/api/v1/eeoc/send", None) is True


@pytest.mark.parametrize("method,url", [
    # the public schema read (a GET, never a submit)
    ("GET", "https://apply.workable.com/api/v1/jobs/57CFF1B2AF/form"),
    # the resume upload POST (an asset attach, not the application submit)
    ("POST", "https://apply.workable.com/api/v1/jobs/57CFF1B2AF/form/upload/resume"),
    # the S3 asset PUT (not even a POST)
    ("PUT", "https://workablehr.s3.amazonaws.com/uploads/abc123"),
    # the account job listing POST (discovery/read, not a submit)
    ("POST", "https://apply.workable.com/api/v3/accounts/powerlines/jobs"),
])
def test_never_send_allows_non_submit_workable_traffic(method, url):
    assert base._is_submit_request(method, url, None) is False
