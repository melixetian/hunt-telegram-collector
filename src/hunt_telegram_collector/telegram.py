import os
from getpass import getpass

from telethon import TelegramClient, functions, types, utils

from .config import ConfigError
from .model import Chat


class Telegram:
    def __init__(self, root):
        try:
            api_id = int(os.environ.get("TELEGRAM_API_ID", ""))
            api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
            if (
                api_id <= 0
                or len(api_hash) != 32
                or any(c not in "0123456789abcdefABCDEF" for c in api_hash)
            ):
                raise ValueError
        except ValueError as exc:
            raise ConfigError(
                "Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH локально в .env"
            ) from exc
        self.client = TelegramClient(
            str(root / "telegram"),
            api_id,
            api_hash,
            receive_updates=False,
            request_retries=0,
            connection_retries=2,
            flood_sleep_threshold=0,
            raise_last_call_error=True,
            timeout=15,
        )

    async def connect(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ConfigError("Сначала выполните hunt login в локальном терминале")
        if await self.client.is_bot():
            raise ConfigError("Нужна пользовательская сессия, не Bot API")

    async def login(self):
        await self.client.start(
            phone=lambda: getpass("Номер Telegram (скрытый ввод): "),
            code_callback=lambda: getpass("Код Telegram (скрытый ввод): "),
            password=lambda: getpass("Пароль 2FA (скрытый ввод): "),
        )
        if await self.client.is_bot():
            raise ConfigError("Нужна пользовательская сессия")

    async def close(self):
        await self.client.disconnect()

    async def resolve(self, peer):
        try:
            entity = await self.client.get_entity(peer)
        except ValueError:
            if not isinstance(peer, int):
                raise
            # Populate access hashes for available groups; no message-history requests.
            entity = None
            async for dialog in self.client.iter_dialogs(ignore_migrated=True):
                if dialog.id == peer:
                    entity = dialog.entity
                    break
            if entity is None:
                raise ValueError("Чат не найден среди доступных диалогов") from None
        if not isinstance(entity, (types.Channel, types.Chat)):
            raise ValueError("Допустимы только группы и каналы")
        return entity, Chat(
            utils.get_peer_id(entity),
            entity.title,
            getattr(entity, "username", None),
            isinstance(entity, types.Channel),
            bool(getattr(entity, "forum", False)),
        )

    async def page(self, entity, topic, before, limit):
        # General (1) has no GetReplies history; filter it from group history locally.
        return await self.client.get_messages(
            entity, limit=limit, offset_id=before, reply_to=topic if topic and topic != 1 else None
        )

    async def sources(self):
        async for dialog in self.client.iter_dialogs(ignore_migrated=True):
            if isinstance(dialog.entity, (types.Channel, types.Chat)):
                yield {
                    "id": dialog.id,
                    "title": dialog.name,
                    "username": getattr(dialog.entity, "username", None),
                    "forum": bool(getattr(dialog.entity, "forum", False)),
                }

    async def topics(self, entity):
        offset_date, offset_id, offset_topic = None, 0, 0
        seen = set()
        while True:
            result = await self.client(
                functions.messages.GetForumTopicsRequest(
                    peer=entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100,
                )
            )
            topics = [t for t in result.topics if isinstance(t, types.ForumTopic)]
            if not topics:
                return
            fresh = [t for t in topics if t.id not in seen]
            if not fresh:
                raise RuntimeError("Пагинация тем не продвигается")
            for topic in fresh:
                seen.add(topic.id)
                yield {"id": topic.id, "title": topic.title}
            if len(seen) >= result.count:
                return
            last = topics[-1]
            offset_topic, offset_id = last.id, last.top_message
            by_id = {m.id: m.date for m in result.messages}
            offset_date = last.date if result.order_by_create_date else by_id.get(last.top_message)
            if offset_date is None:
                raise RuntimeError("Нет даты для продолжения списка тем")
