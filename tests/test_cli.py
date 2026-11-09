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


def test_limit_must_be_positive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["import", "--limit", "0"])
    assert "at least 1" in capsys.readouterr().err
