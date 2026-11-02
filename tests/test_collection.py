import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import CHAT, NOW, FakeTelegram, message
from telethon import errors

from hunt_telegram_collector.collector import collect, retry
from hunt_telegram_collector.config import Collection, Config, Source
from hunt_telegram_collector.filtering import Filter
from hunt_telegram_collector.model import normalize

SETTINGS = Collection(page_size=2, page_pause=0, retries=0)
CONFIG = Config(sources=(Source("@jobs_test"),), collection=SETTINGS)


def run(gateway, store, config=CONFIG, now=NOW):
    return asyncio.run(collect(gateway, store, config, report=lambda _: None, now=now))


def test_pagination_repeat_and_edits(store):
    gateway = FakeTelegram([message(i) for i in range(1, 8)] + [message(0, days=10)])
    assert run(gateway, store)[0]["observed"] == 7
    first = list(store.records())
    run(gateway, store)
    assert list(store.records()) == first
    assert store.stream(f"{CHAT.id}:all")["cursor"] == 7
    gateway.messages[2].message = "Hiring senior Python, updated compensation"
    gateway.messages[2].edit_date = NOW + timedelta(hours=1)
    run(gateway, store)
    records = list(store.records())
    assert len(records) == 7
    assert records[2]["text"].endswith("updated compensation")
    assert records[2]["edited_at"] == gateway.messages[2].edit_date.isoformat()


def test_resume_initial_scan_after_failure_keeps_original_cutoff(store):
    gateway = FakeTelegram([message(i, days=6) for i in range(1, 7)])
    gateway.fail_before = 5
    assert run(gateway, store)[0]["error"] == "ConnectionError"
    state = store.stream(f"{CHAT.id}:all")
    assert state["cursor"] == 0
    assert json.loads(state["scan"])["before"] == 5
    assert len(list(store.records())) == 2
    gateway.fail_before = None
    gateway.messages.append(message(7))
    # Ten days later the unfinished scan must retain its initial seven-day boundary.
    run(gateway, store, now=NOW + timedelta(days=10))
    assert len(list(store.records())) == 6
    assert store.stream(f"{CHAT.id}:all")["cursor"] == 6
    run(gateway, store, now=NOW + timedelta(days=10))
    assert len(list(store.records())) == 7


def test_incremental_gap_longer_than_refresh_window(store):
    gateway = FakeTelegram([message(1, days=1)])
    run(gateway, store)
    gateway.messages += [message(i) for i in range(2, 9)]
    run(gateway, store, now=NOW + timedelta(days=20))
    assert len(list(store.records())) == 8
    assert store.stream(f"{CHAT.id}:all")["cursor"] == 8


def test_failure_of_one_source_does_not_block_next(store):
    config = replace(CONFIG, sources=(Source("@broken"), Source("@jobs_test")))
    outcomes = run(FakeTelegram([message(1)], bad_peers={"@broken"}), store, config)
    assert "error" in outcomes[0]
    assert outcomes[1]["observed"] == 1
    assert len(list(store.records())) == 1


def test_crash_inside_page_rolls_back_messages_and_checkpoint(store):
    key = f"{CHAT.id}:all"
    store.stream(key)
    scan = {"high": 2, "before": 1}
    store.db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON messages
                     WHEN NEW.message_id=2 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    records = [normalize(message(i), CHAT, "jobs", Filter()) for i in (1, 2)]
    with pytest.raises(sqlite3.IntegrityError):
        store.save_page(key, records, scan)
    assert list(store.records()) == []
    assert store.stream(key)["scan"] is None
    assert store.stream(key)["cursor"] == 0


def test_empty_and_old_history_complete_without_collecting_old(store):
    run(FakeTelegram([message(2, days=8)]), store)
    assert list(store.records()) == []
    assert store.stream(f"{CHAT.id}:all")["cursor"] == 2
    run(FakeTelegram([]), store)
    assert store.stream(f"{CHAT.id}:all")["scan"] is None


def test_stuck_pagination_is_detected(store):
    class Stuck(FakeTelegram):
        async def page(self, *args):
            return [message(2), message(1)]

    result = run(Stuck([]), store)
    assert result[0]["error"] == "RuntimeError"
    assert store.stream(f"{CHAT.id}:all")["cursor"] == 0


def test_flood_wait_and_network_retry_are_bounded():
    sleeps, calls = [], []

    async def sleep(seconds):
        sleeps.append(seconds)

    async def operation():
        calls.append(1)
        if len(calls) == 1:
            raise errors.FloodWaitError(request=None, capture=3)
        if len(calls) == 2:
            raise ConnectionError()
        return 42

    settings = replace(SETTINGS, retries=2)
    assert asyncio.run(retry(operation, settings, sleep)) == 42
    assert sleeps == [3, 2]

    async def long_wait():
        raise errors.FloodWaitError(request=None, capture=120)

    with pytest.raises(errors.FloodWaitError):
        asyncio.run(retry(long_wait, settings, sleep))
    assert sleeps == [3, 2]


def test_persistent_network_error_exhausts_retries():
    calls = []

    async def operation():
        calls.append(1)
        raise ConnectionError()

    async def sleep(_):
        pass

    with pytest.raises(ConnectionError):
        asyncio.run(retry(operation, replace(SETTINGS, retries=2), sleep))
    assert len(calls) == 3
