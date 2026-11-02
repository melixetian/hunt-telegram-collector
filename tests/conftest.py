from datetime import UTC, datetime, timedelta

import pytest
from telethon.tl import types

from hunt_telegram_collector.model import Chat
from hunt_telegram_collector.storage import Store

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
CHAT = Chat(-1000000000042, "Jobs", "jobs_test")


def message(number, text="Hiring Python backend", days=0, **kwargs):
    return types.Message(
        id=number,
        peer_id=types.PeerChannel(42),
        date=NOW - timedelta(days=days),
        message=text,
        **kwargs,
    )


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "test.sqlite3")
    yield value
    value.close()


class FakeTelegram:
    def __init__(self, messages, bad_peers=()):
        self.messages = messages
        self.bad_peers = bad_peers
        self.fail_before = None
        self.calls = []

    async def resolve(self, peer):
        if peer in self.bad_peers:
            raise ValueError("unavailable")
        return peer, CHAT

    async def page(self, entity, topic, before, limit):
        self.calls.append((entity, topic, before))
        if before == self.fail_before:
            raise ConnectionError("temporary disconnect")
        return sorted(
            [m for m in self.messages if not before or m.id < before],
            key=lambda m: m.id,
            reverse=True,
        )[:limit]
