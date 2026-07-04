"""Shared browser primitives (engine.providers.base): the W5.1 spine.

No patchright, no network: every primitive is driven through fake route / locator /
page objects. The autouse no-network guard holds throughout. What is LOGIC-tested
here (branching, sequencing, safety): the never-send submit predicate + interceptor,
human-cadence typing, the completeness diff + normalization, and the react-select
driver's call sequence. What is DEFERRED to W5.2 fixture validation: the live-DOM
extraction inside sweep_required and the real-browser timing of type_human /
select_react_combobox (their logic paths are still exercised here with fakes).
"""

import subprocess
import sys
from pathlib import Path

import pytest

# Import engine.fill at module load (before the autouse no_network fixture patches
# socket.socket): the base re-export wrappers import it lazily at call time, and a
# FIRST import under the socket patch would drag in ssl (class SSLSocket(socket))
# and fail. Mirrors test_providers_registry's top-level `from engine import fill`.
import engine.fill  # noqa: F401
from engine.providers import base

_REPO_ROOT = Path(__file__).resolve().parents[1]


# -- fakes ---------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, method, url, post_data=None):
        self.method = method
        self.url = url
        self.post_data = post_data


class _FakeRoute:
    def __init__(self, request):
        self.request = request
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class _FakeContext:
    def __init__(self):
        self.routed = []

    def route(self, pattern, handler):
        self.routed.append((pattern, handler))


def _drive(handler, method, url, post_data=None):
    route = _FakeRoute(_FakeRequest(method, url, post_data))
    handler(route)
    return route


# -- never-send: submit predicate (per-vendor) ---------------------------------


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/acme/jobs/12345",
    "https://job-boards.greenhouse.io/acme/jobs/12345/applications",
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs/12345",
    "https://boards.greenhouse.io/embed/job_app?token=acme",
])
def test_submit_predicate_aborts_greenhouse_application_post(url):
    assert base._is_submit_request("POST", url, None) is True


@pytest.mark.parametrize("url", [
    "https://jobs.lever.co/globex/req-77/apply",
    "https://api.lever.co/v0/postings/globex/req-77/apply",
])
def test_submit_predicate_aborts_lever_apply_post(url):
    assert base._is_submit_request("POST", url, None) is True


def test_submit_predicate_aborts_ashby_direct_submit_path():
    assert base._is_submit_request(
        "POST", "https://jobs.ashbyhq.com/api/application/submit", None) is True


def test_submit_predicate_aborts_workable_candidate_create():
    url = "https://acme.workable.com/spi/v3/accounts/1/jobs/abc/candidates"
    assert base._is_submit_request("POST", url, None) is True


def test_submit_predicate_get_to_submit_endpoint_passes():
    # A GET (page load / schema read) to the very same URL is never a submission.
    url = "https://jobs.lever.co/globex/req-77/apply"
    assert base._is_submit_request("GET", url, None) is False


@pytest.mark.parametrize("url", [
    "https://api.segment.io/v1/track",
    "https://boards.greenhouse.io/embed/job_board/js?for=acme",
    "https://jobs.lever.co/globex/req-77/resumeUpload",
    "https://acme.workable.com/spi/v3/accounts/1/jobs/abc/questions",
])
def test_submit_predicate_nonsubmit_post_passes(url):
    assert base._is_submit_request("POST", url, None) is False


def test_submit_predicate_ashby_graphql_read_passes_submit_aborts():
    # The Ashby form-schema READ and the application SUBMIT share ONE graphql URL;
    # only the submit-operation body is a submission (the read must still flow, or
    # capture breaks).
    url = "https://jobs.ashbyhq.com/api/non-user-graphql"
    read_body = '{"operationName":"ApiJobPosting","variables":{}}'
    submit_body = '{"operationName":"SubmitApplicationForm","variables":{}}'
    assert base._is_submit_request("POST", url, read_body) is False
    assert base._is_submit_request("POST", url, submit_body) is True


def test_submit_predicate_workable_graphql_create_candidate_aborts():
    url = "https://www.workable.com/api/graphql"
    assert base._is_submit_request(
        "POST", url, '{"operationName":"createCandidate"}') is True
    assert base._is_submit_request(
        "POST", url, '{"operationName":"jobPosting"}') is False


# -- never-send: interceptor install + handler behaviour -----------------------


def test_install_never_send_registers_catchall_route():
    ctx = _FakeContext()
    handler = base.install_never_send(ctx)
    assert len(ctx.routed) == 1
    pattern, registered = ctx.routed[0]
    assert pattern == "**"
    assert registered is handler


def test_never_send_handler_aborts_submit_continues_get():
    ctx = _FakeContext()
    handler = base.install_never_send(ctx)

    submit = _drive(handler, "POST", "https://jobs.lever.co/g/r/apply")
    assert submit.aborted is True
    assert submit.continued is False

    passed = _drive(handler, "GET", "https://jobs.lever.co/g/r/apply")
    assert passed.aborted is False
    assert passed.continued is True

    nonsubmit = _drive(handler, "POST", "https://api.segment.io/v1/track")
    assert nonsubmit.aborted is False
    assert nonsubmit.continued is True


def test_never_send_handler_survives_request_without_post_data():
    # A request object whose post_data access raises must not crash the handler.
    class _Raises:
        method = "POST"
        url = "https://api.segment.io/v1/track"

        @property
        def post_data(self):
            raise RuntimeError("no body")

    ctx = _FakeContext()
    handler = base.install_never_send(ctx)
    route = _FakeRoute(_Raises())
    handler(route)
    assert route.continued is True and route.aborted is False


# -- type_human ----------------------------------------------------------------


class _FakeTypeLocator:
    def __init__(self):
        self.keys = []

    def press_sequentially(self, text, delay=None):
        self.keys.append((text, delay))

    def fill(self, *args, **kwargs):
        raise AssertionError("type_human must never call fill()")

    def type(self, *args, **kwargs):
        raise AssertionError("type_human must never call type()")


def test_type_human_presses_each_char_with_delay_in_range():
    loc = _FakeTypeLocator()
    base.type_human(loc, "Ab9", min_delay=60, max_delay=180)

    assert [k[0] for k in loc.keys] == ["A", "b", "9"]
    for _char, delay in loc.keys:
        assert 60 <= delay <= 180


def test_type_human_empty_text_is_a_noop():
    loc = _FakeTypeLocator()
    base.type_human(loc, "")
    assert loc.keys == []


# -- completeness diff + normalization -----------------------------------------


def test_normalize_name_strips_asterisk_collapses_ws_lowercases():
    assert base._normalize_name("  First   Name * ") == "first name"
    assert base._normalize_name("EMAIL*") == "email"
    assert base._normalize_name(None) == ""


def test_completeness_mismatch_reports_both_directions():
    schema = {"First Name", "Email", "Phone"}
    dom = {"first name", "email", "Cover Letter *"}
    diff = base.completeness_mismatch(schema, dom)
    assert diff["dom_only"] == ["cover letter"]
    assert diff["schema_only"] == ["phone"]


def test_completeness_mismatch_agreement_is_empty_both_sides():
    schema = {"Name", "Email"}
    dom = {"name  ", "EMAIL"}
    diff = base.completeness_mismatch(schema, dom)
    assert diff == {"dom_only": [], "schema_only": []}


# -- sweep_required (light live-DOM logic; full fixture validation in W5.2) -----


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


class _FakeSweepPage:
    def __init__(self, *, required, labels):
        self._required = required
        self._labels = labels

    def locator(self, css):
        if css == base._REQUIRED_CSS:
            return _FakeLocatorSet(self._required)
        if css == base._ASTERISK_CSS:
            return _FakeLocatorSet(self._labels)
        return _FakeLocatorSet([])


def test_sweep_required_collects_visible_skips_hidden_and_offscreen():
    required = [
        _FakeSweepLocator(attrs={"aria-label": "First name"}),
        _FakeSweepLocator(attrs={"aria-label": "Off screen"}, visible=False),
        _FakeSweepLocator(attrs={"aria-label": "Hidden",
                                 "aria-hidden": "true"}),
    ]
    labels = [
        _FakeSweepLocator(text="Email *"),
        _FakeSweepLocator(text="Optional note"),  # no asterisk -> skipped
    ]
    page = _FakeSweepPage(required=required, labels=labels)
    assert base.sweep_required(page) == {"first name", "email"}


# -- select_react_combobox -----------------------------------------------------


class _FakeComboInput:
    def __init__(self):
        self.clicked = 0
        self.keys = []
        self.pressed = []

    def click(self):
        self.clicked += 1

    def press_sequentially(self, text, delay=None):
        self.keys.append(text)

    def press(self, key):
        self.pressed.append(key)

    def fill(self, *args, **kwargs):
        raise AssertionError("react-select driver must never call fill()")

    def blur(self, *args, **kwargs):
        raise AssertionError("react-select driver must never blur (Escape only)")


class _FakeOption:
    def __init__(self):
        self.waited = None
        self.clicked = 0

    def filter(self, **kwargs):
        return self

    @property
    def first(self):
        return self

    def wait_for(self, **kwargs):
        self.waited = kwargs

    def click(self):
        self.clicked += 1


class _FakeSingleValue:
    """Reads the rendered value; a multi-element `reads` list pops one per poll so
    a value that appears only on the +500 ms read is expressible."""

    def __init__(self, reads):
        self._reads = list(reads)

    def inner_text(self):
        if len(self._reads) > 1:
            return self._reads.pop(0)
        return self._reads[0] if self._reads else ""


class _FakeComboPage:
    def __init__(self, *, field_id, single_value_reads):
        self._field_id = field_id
        self.combo = _FakeComboInput()
        self.option = _FakeOption()
        self._single_value = _FakeSingleValue(single_value_reads)
        self.timeouts = []

    def locator(self, css):
        if css == f"#react-select-{self._field_id}-input":
            return self.combo
        if css.startswith(f"#react-select-{self._field_id}-listbox"):
            return self.option
        if css.endswith(".select__single-value"):
            return self._single_value
        raise AssertionError(f"unexpected selector {css!r}")

    def wait_for_timeout(self, ms):
        self.timeouts.append(ms)


def test_select_react_combobox_happy_path_confirms_and_escapes():
    # Value rendered by the +200 ms read -> lands on the first poll mark.
    page = _FakeComboPage(field_id="country", single_value_reads=["Italy"])
    landed = base.select_react_combobox(page, "country", "Italy")

    assert landed is True
    assert page.combo.clicked == 1                 # opened once
    assert page.combo.keys == ["I", "t", "a", "l", "y"]  # human-typed filter
    assert page.option.waited == {"state": "visible", "timeout": 5000}
    assert page.option.clicked == 1                # option chosen
    assert page.combo.pressed == ["Escape"]        # dismissed via Escape, no blur
    assert page.timeouts == [200]                  # confirmed at +200 ms


def test_select_react_combobox_polls_second_mark_when_value_lags():
    # Empty at +200 ms, rendered by +500 ms -> both cumulative marks are waited.
    page = _FakeComboPage(field_id="country", single_value_reads=["", "Italy"])
    landed = base.select_react_combobox(page, "country", "Italy")

    assert landed is True
    assert page.timeouts == [200, 300]             # +200 then +300 (cumulative +500)


def test_select_react_combobox_reports_not_landed_on_readback_miss():
    page = _FakeComboPage(field_id="country", single_value_reads=[""])
    landed = base.select_react_combobox(page, "country", "Italy")

    assert landed is False
    assert page.timeouts == [200, 300]             # both marks exhausted
    assert page.combo.pressed == ["Escape"]        # still dismissed cleanly


# -- fill-primitive re-export: call-time lookup preserves the monkeypatch seam --


def test_reexported_safe_click_honours_patch_seam(monkeypatch):
    import engine.fill as fill
    calls = []
    monkeypatch.setattr(fill, "_safe_click",
                        lambda target, name: calls.append((target, name)))
    base._safe_click("TARGET", "Next")
    assert calls == [("TARGET", "Next")]


def test_reexported_readback_honours_patch_seam(monkeypatch):
    import engine.fill as fill
    monkeypatch.setattr(fill, "_readback",
                        lambda locator, value: ("sentinel", True))
    assert base._readback(object(), "x") == ("sentinel", True)


def test_reexported_locate_and_upload_delegate_to_fill(monkeypatch):
    import engine.fill as fill
    monkeypatch.setattr(fill, "_locate", lambda page, fv: "LOC")
    monkeypatch.setattr(fill, "_safe_upload",
                        lambda *a, **k: "UP")
    assert base._locate(object(), object()) == "LOC"
    assert base._safe_upload(object(), object(), object()) == "UP"


# -- lazy-import invariant: base must not load the browser stack ---------------


def test_importing_base_does_not_load_browser_stack():
    # engine.providers.base is the fill-primitive re-export home; importing it must
    # NOT pull in browse, patchright, or even engine.fill (the wrappers import fill
    # lazily at call time). Checked in a fresh interpreter.
    script = (
        "import sys, engine.providers.base; "
        "print('browse' if 'engine.browse' in sys.modules else 'no-browse'); "
        "print('patch' if 'patchright' in sys.modules else 'no-patch'); "
        "print('fill' if 'engine.fill' in sys.modules else 'no-fill')"
    )
    out = subprocess.run([sys.executable, "-c", script], cwd=_REPO_ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["no-browse", "no-patch", "no-fill"], out.stdout
