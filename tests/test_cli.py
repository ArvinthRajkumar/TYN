"""CLI wiring, exit codes and safe error output."""

import json
from pathlib import Path

import httpx
import pytest

from tests.conftest import FAKE_EMAIL, FAKE_TOKEN, sample_payload
from tests.fakes import FakeJira
from tyn import cli


def test_missing_config_exits_2_and_names_variable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "load_jira_settings", lambda: cli_load_without_env_file())
    code = cli.main(["import", "--dry-run"])
    err = capsys.readouterr().err
    assert code == cli.EXIT_ERROR
    assert "JIRA_BASE_URL is not set" in err


def cli_load_without_env_file() -> object:
    from tyn.config import load_jira_settings

    return load_jira_settings(env_file=None)


@pytest.mark.usefixtures("jira_env")
def test_dry_run_end_to_end_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_path = tmp_path / "inputs" / "dataset.json"
    dataset_path.parent.mkdir()
    dataset_path.write_text(json.dumps(sample_payload()), encoding="utf-8")
    fake = FakeJira()
    monkeypatch.setattr(cli, "load_jira_settings", cli_load_without_env_file)
    monkeypatch.setattr(
        cli,
        "build_http_client",
        lambda settings: httpx.Client(base_url=settings.jira_base_url, transport=fake.transport()),
    )
    code = cli.main(["import", "--dry-run", "--dataset", str(dataset_path)])
    out = capsys.readouterr().out
    assert code == cli.EXIT_OK
    assert "dry run, nothing changed" in out
    assert "issues    would create 5" in out
    assert fake.write_calls() == []
    assert FAKE_TOKEN not in out
    assert FAKE_EMAIL not in out


@pytest.mark.usefixtures("jira_env")
def test_invalid_project_argument_is_rejected(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "load_jira_settings", cli_load_without_env_file)
    code = cli.main(["import", "--project", "bad key"])
    assert code == cli.EXIT_ERROR
    assert "project key" in capsys.readouterr().err


def test_log_level_is_accepted_after_the_command() -> None:
    args = cli.build_parser().parse_args(["import", "--log-level", "INFO", "--limit", "5"])
    assert args.log_level == "INFO"
    assert args.limit == 5


def test_limit_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["import", "--limit", "0"])
    assert "at least 1" in capsys.readouterr().err


@pytest.mark.usefixtures("jira_env")
def test_report_command_renders_saved_checkpoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_report import _checkpoint
    from tyn.agent.checkpoint import CheckpointStore

    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key-0123456789")
    monkeypatch.setattr(cli, "load_jira_settings", cli_load_without_env_file)
    monkeypatch.setattr(cli, "load_groq_settings", lambda: _groq_without_env_file())
    checkpoint = _checkpoint("Fails on the target build.").model_copy(
        update={"label": "scope", "release": "R5.0", "project": "TYN"}
    )
    CheckpointStore("TYN", "scope", "R5.0").save(checkpoint)
    code = cli.main(["report", "--label", "scope", "--release", "R5.0"])
    out = capsys.readouterr().out
    assert code == cli.EXIT_FAILURES
    assert "Overall decision: BLOCKED" in out
    assert "reports/release_report.md" in out
    assert (tmp_path / "reports" / "metrics.json").exists()


def test_report_without_checkpoint_is_an_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, jira_env: None
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key-0123456789")
    monkeypatch.setattr(cli, "load_jira_settings", cli_load_without_env_file)
    monkeypatch.setattr(cli, "load_groq_settings", lambda: _groq_without_env_file())
    assert cli.main(["report", "--label", "scope", "--release", "R5.0"]) == cli.EXIT_ERROR
    assert "run assess first" in capsys.readouterr().err


def _groq_without_env_file() -> object:
    from tyn.config import load_groq_settings

    return load_groq_settings(env_file=None)
