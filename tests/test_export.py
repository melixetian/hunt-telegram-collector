import json
from copy import deepcopy

import pytest
import requests
from conftest import CHAT, message

from hunt_telegram_collector.config import ConfigError
from hunt_telegram_collector.exporting import FIELDS, Sheets, export_jsonl, export_sheets
from hunt_telegram_collector.filtering import Filter
from hunt_telegram_collector.model import normalize


def seed(store, text="Hiring Python"):
    store.stream("test")
    store.save_page(
        "test", [normalize(message(1, text), CHAT, "jobs", Filter())], {"high": 1}, complete=True
    )


class FakeSheet:
    destination = "sheets:test:Incoming"

    def __init__(self):
        self.rows = []
        self.writes = 0
        self.lose_ack = False

    def read(self):
        return deepcopy(self.rows)

    def ensure_capacity(self, rows):
        pass

    def write(self, updates):
        self.writes += 1
        for number, values in updates:
            while len(self.rows) < number:
                self.rows.append([])
            self.rows[number - 1][: len(FIELDS)] = deepcopy(values)
        if self.lose_ack:
            self.lose_ack = False
            raise requests.Timeout("write happened, response lost")


def test_jsonl_repeat_and_edit_no_duplicates(store, tmp_path):
    seed(store)
    path = tmp_path / "out.jsonl"
    assert export_jsonl(store, path) == 1
    original = path.read_bytes()
    export_jsonl(store, path)
    assert path.read_bytes() == original
    seed(store, "Changed Python job")
    export_jsonl(store, path)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["text"] == "Changed Python job"
    assert store.status()["exports"][0]["records"] == 1


def test_jsonl_failure_before_replace_preserves_previous_file(store, tmp_path, monkeypatch):
    seed(store)
    path = tmp_path / "out.jsonl"
    export_jsonl(store, path)
    original = path.read_bytes()
    seed(store, "Changed")

    def fail(*_):
        raise OSError("disk failure")

    monkeypatch.setattr("hunt_telegram_collector.exporting.os.replace", fail)
    with pytest.raises(OSError):
        export_jsonl(store, path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".hunt-*"))


def test_sheets_repeat_updates_preserve_chatgpt_fields(store):
    seed(store)
    remote = FakeSheet()
    export_sheets(store, remote)
    remote.rows[0] += ["processing_status", "notes"]
    remote.rows[1] += ["reviewed", "keep me"]
    export_sheets(store, remote)
    assert len(remote.rows) == 2
    assert remote.writes == 1
    seed(store, '=HYPERLINK("https://example.org")')
    export_sheets(store, remote)
    assert remote.rows[1][FIELDS.index("text")] == '=HYPERLINK("https://example.org")'
    assert remote.rows[1][len(FIELDS) :] == ["reviewed", "keep me"]
    assert len(remote.rows) == 2


def test_sheets_response_lost_after_write_reconciles_without_duplicate(store):
    seed(store)
    remote = FakeSheet()
    remote.lose_ack = True
    export_sheets(store, remote, sleep=lambda _: None)
    assert len(remote.rows) == 2
    assert remote.writes == 1
    assert store.status()["exports"][0]["records"] == 1


def test_local_ack_failure_after_successful_remote_write(store, monkeypatch):
    seed(store)
    remote = FakeSheet()
    confirm = store.confirm_export

    def crash(*_):
        raise RuntimeError("process died before acknowledgement")

    monkeypatch.setattr(store, "confirm_export", crash)
    with pytest.raises(RuntimeError):
        export_sheets(store, remote)
    assert store.status()["exports"] == []
    monkeypatch.setattr(store, "confirm_export", confirm)
    export_sheets(store, remote)
    assert len(remote.rows) == 2
    assert remote.writes == 1


def test_foreign_header_and_duplicate_keys_refused(store):
    seed(store)
    remote = FakeSheet()
    remote.rows = [["Вакансии", "Статус"]]
    with pytest.raises(ConfigError):
        export_sheets(store, remote)
    assert remote.writes == 0
    remote.rows = [FIELDS, ["duplicate"], ["duplicate"]]
    with pytest.raises(ConfigError):
        export_sheets(store, remote)
    assert remote.writes == 0


def test_rows_with_human_only_data_are_not_reused(store):
    seed(store)
    remote = FakeSheet()
    remote.rows = [FIELDS, [""] * len(FIELDS) + ["human note"]]
    export_sheets(store, remote)
    assert remote.rows[1][-1] == "human note"
    assert remote.rows[2][0] == f"{CHAT.id}:1"


def test_main_sheet_is_refused_before_credentials_are_loaded():
    with pytest.raises(ConfigError, match="запрещена"):
        Sheets("test", " Вакансии ", "never-read.json")


def test_sheets_adapter_uses_raw_only_owned_columns():
    remote = object.__new__(Sheets)
    remote.base, remote.tab = "https://example.invalid", "'Incoming'"
    calls = []
    remote.request = lambda *a, **k: calls.append((a, k))
    remote.write([(2, ["=not_a_formula"] * len(FIELDS))])
    body = calls[0][1]["json"]
    assert body["valueInputOption"] == "RAW"
    assert body["data"][0]["range"] == "'Incoming'!A2:Q2"
