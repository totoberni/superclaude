import pytest

from engine.config import ConfigError, load_config


def test_jobhunt_locked_params(jobhunt_config):
    assert jobhunt_config.name == "jobhunt"
    assert jobhunt_config.topic == "abe-jobsearch"
    assert jobhunt_config.id_prefix == "j-"
    assert jobhunt_config.threshold == 70
    assert jobhunt_config.buffer_size == 50
    assert jobhunt_config.automatable_vendors == ("ashby", "greenhouse", "lever")


def test_phd_locked_params(phd_config):
    assert phd_config.topic == "abe-phd"
    assert phd_config.id_prefix == "phd-"
    assert phd_config.threshold == 70
    assert phd_config.buffer_size == 15
    assert phd_config.terminal_state == "pending_review"


def test_papers_locked_params(papers_config):
    assert papers_config.topic == "abe-papers"
    assert papers_config.id_prefix == "p-"
    assert papers_config.threshold == 50
    assert papers_config.buffer_size == 20


def test_axis_weights_sum_to_one(jobhunt_config, phd_config, papers_config):
    for cfg in (jobhunt_config, phd_config, papers_config):
        assert abs(sum(cfg.axes.values()) - 1.0) < 1e-6


def test_channel_classification(jobhunt_config):
    assert jobhunt_config.channel_for("greenhouse") == "automatable"
    assert jobhunt_config.channel_for("workday") == "manual"


def test_bad_weight_sum_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "name: x\ntopic: t\nid_prefix: x-\nthreshold: 70\nbuffer_size: 10\n"
        "terminal_state: pending_review\nssot: job.yaml\n"
        "scoring:\n  axes:\n    role_fit: 0.5\n    skills_overlap: 0.2\n"
    )
    with pytest.raises(ConfigError):
        load_config(bad)


def test_missing_key_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\ntopic: t\n")
    with pytest.raises(ConfigError):
        load_config(bad)
