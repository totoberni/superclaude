from engine.ssot import MISSING, SSOT


def _ssot():
    return SSOT({
        "identity": {"full_name": "Abe"},
        "preferences": {
            "roles": ["backend engineer"],
            "comp_floor": 60000,
            "excludes": [],
            "empty_str": "",
        },
    })


def test_present_field_returns_value():
    assert _ssot().get("identity.full_name") == "Abe"
    assert _ssot().get("preferences.comp_floor") == 60000


def test_absent_field_is_missing():
    s = _ssot()
    assert s.get("preferences.notice_period") is MISSING
    assert s.is_missing("preferences.notice_period")


def test_empty_values_are_missing():
    s = _ssot()
    assert s.is_missing("preferences.excludes")
    assert s.is_missing("preferences.empty_str")


def test_missing_required_reports_only_missing():
    s = _ssot()
    required = ["identity.full_name", "preferences.notice_period",
               "preferences.comp_floor"]
    assert s.missing_required(required) == ["preferences.notice_period"]


def test_load_from_toy_fixture(job_ssot_path):
    s = SSOT.load(job_ssot_path)
    assert s.get("identity.email") == "abe@example.com"
    assert s.is_missing("preferences.notice_period")
