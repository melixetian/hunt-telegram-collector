import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import CHAT, NOW, FakeTelegram, message
from telethon.tl import types

from hunt_telegram_collector.collector import collect
from hunt_telegram_collector.config import Collection, Config, ConfigError, Source, load_config
from hunt_telegram_collector.filtering import Filter
from hunt_telegram_collector.model import Chat, links, normalize, topic_id
from hunt_telegram_collector.runtime import exclusive_lock
from hunt_telegram_collector.telegram import Telegram


@pytest.mark.parametrize(
    "text,media,kind,verdict,reason",
    [
        ("Python engineer", False, "jobs", "candidate", "jobs_source_broad_coverage"),
        ("Ищем Python инженера", False, "general", "candidate", "vacancy_and_professional_signal"),
        (
            "Hiring Golang engineer",
            False,
            "general",
            "candidate",
            "vacancy_and_professional_signal",
        ),
        ("Ищу работу Python", False, "jobs", "review", "likely_resume"),
        ("Ищу работу. Вакансия Python", False, "jobs", "review", "ambiguous_resume_and_vacancy"),
        ("Open to work, backend engineer", False, "general", "review", "likely_resume"),
        ("Backend для роботов", False, "general", "review", "partial_signal"),
        ("Hiring manager", False, "general", "review", "partial_signal"),
        ("Всем доброе утро", False, "general", "noise", "no_configured_signals"),
        ("", True, "jobs", "review", "media_without_text"),
        ("", False, "jobs", "noise", "empty_or_service"),
    ],
)
def test_filter(text, media, kind, verdict, reason):
    assert Filter().classify(text, media, kind) == (verdict, reason)


def test_custom_filter_or_matching():
    classifier = Filter({"relevance_patterns": ["elixir", "rust"]})
    assert classifier.classify("Hiring Rust engineer", False, "general")[0] == "candidate"


def test_utf16_entities_hidden_and_plain_urls():
    m = message(
        1,
        "🤖 https://example.org more",
        entities=[
            types.MessageEntityUrl(offset=3, length=19),
            types.MessageEntityTextUrl(offset=23, length=4, url="https://hidden.example/jobs"),
        ],
    )
    assert links(m) == ["https://example.org", "https://hidden.example/jobs"]


def test_links_private_basic_groups_and_media():
    m = message(1, "", media=types.MessageMediaPhoto())
    record = normalize(m, replace(CHAT, username=None), "jobs", Filter())
    assert record["message_url"] == "https://t.me/c/42/1"
    assert record["link_access"] == "members_only"
    assert record["filter_reason"] == "media_without_text"
    record = normalize(m, Chat(-42, "Basic", channel=False), "jobs", Filter())
    assert record["message_url"] is None
    assert record["link_access"] == "unavailable"


def test_topic_roots_replies_and_general():
    root_reply = message(3, reply_to=types.MessageReplyHeader(reply_to_msg_id=2, forum_topic=True))
    nested = message(
        4, reply_to=types.MessageReplyHeader(reply_to_msg_id=3, reply_to_top_id=2, forum_topic=True)
    )
    general = message(5, reply_to=types.MessageReplyHeader(reply_to_msg_id=4))
    assert topic_id(root_reply, True) == topic_id(nested, True) == 2
    assert topic_id(general, True) == 1
    assert topic_id(nested, False) is None
    record = normalize(nested, replace(CHAT, forum=True), "general", Filter())
    assert record["message_url"].endswith("/4?thread=2")


def test_only_selected_topics_are_saved_and_have_independent_state(store):
    class Forum(FakeTelegram):
        async def resolve(self, peer):
            return peer, replace(CHAT, forum=True)

    gateway = Forum(
        [
            message(4, reply_to=types.MessageReplyHeader(reply_to_msg_id=2, forum_topic=True)),
            message(5, reply_to=types.MessageReplyHeader(reply_to_msg_id=3, forum_topic=True)),
            message(6),
        ]
    )
    config = Config((Source("@forum", topics=(2, 1)),), Collection(page_pause=0))
    asyncio.run(collect(gateway, store, config, report=lambda _: None, now=NOW))
    assert {r["message_id"] for r in store.records()} == {4, 6}
    assert len(store.status()["streams"]) == 2


def test_duplicate_crossposts_preserve_originals(store):
    store.stream("test")
    records = [
        normalize(message(1), chat, "jobs", Filter())
        for chat in (CHAT, replace(CHAT, id=CHAT.id - 1))
    ]
    store.save_page("test", records, {"high": 1}, True)
    exported = list(store.records())
    assert len(exported) == 2
    assert all(r["probable_duplicate_of"] for r in exported)
    assert exported[0]["probable_duplicate_of"] == exported[1]["record_key"]


def test_example_config_is_valid():
    config = load_config(Path(__file__).parents[1] / "config.example.yaml")
    assert config.sources == ()
    assert config.collection.initial_days == 7


@pytest.mark.parametrize(
    "content",
    [
        "sources: [{peer: 123}]",
        "sources: [{peer: '@channel', kind: typo}]",
        "sources: [{peer: '@channel', topics: [0]}]",
        "collection: {page_size: 0}",
        "collection: {retries: -1}",
        "collection: {initial_days: 'seven'}",
        "filters: {relevance_patterns: ['[']}",
        "typo: true",
        "sources: [{peer: '@channel'}, {peer: '@CHANNEL'}]",
    ],
)
def test_invalid_config_refused(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ConfigError):
        load_config(path)


def test_process_lock_and_release(tmp_path):
    with exclusive_lock(tmp_path), pytest.raises(RuntimeError):
        with exclusive_lock(tmp_path):
            pass
    with exclusive_lock(tmp_path):
        pass


def test_telegram_page_parameters_and_general_topic():
    calls = []

    class Client:
        async def get_messages(self, entity, **kwargs):
            calls.append((entity, kwargs))
            return []

    gateway = object.__new__(Telegram)
    gateway.client = Client()
    asyncio.run(gateway.page("forum", 55, 100, 20))
    asyncio.run(gateway.page("forum", 1, 80, 20))
    assert calls[0] == ("forum", {"limit": 20, "offset_id": 100, "reply_to": 55})
    assert calls[1][1]["reply_to"] is None
