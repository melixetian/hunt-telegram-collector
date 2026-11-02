import json

import pytest

from hunt_telegram_collector.cli import parser, run
from hunt_telegram_collector.config import ConfigError
from hunt_telegram_collector.storage import Store


def test_status_and_jsonl_without_any_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HUNT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.chdir(tmp_path)
    assert run(parser().parse_args(["status"])) == 0
    assert json.loads(capsys.readouterr().out)["messages"] == 0
    assert run(parser().parse_args(["export"])) == 0
    assert (tmp_path / "state/exports/messages.jsonl").read_text() == ""


def test_empty_allowlist_fails_before_telegram(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNT_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("sources: []\n")
    with pytest.raises(ConfigError, match="sources пуст"):
        run(parser().parse_args(["collect"]))


def test_interrupted_runs_reported_separately(store):
    first = store.start_run("collect")
    second = store.start_run("export")
    runs = store.status()["recent_runs"]
    assert runs[0]["id"] == second
    assert runs[1]["id"] == first
    assert runs[1]["status"] == "interrupted"


def test_checkpoints_survive_database_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    store.stream("42:all")
    store.begin_scan("42:all", {"before": 123})
    store.close()
    reopened = Store(path)
    try:
        assert json.loads(reopened.stream("42:all")["scan"])["before"] == 123
    finally:
        reopened.close()
