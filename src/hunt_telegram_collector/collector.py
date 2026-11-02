import asyncio
import json
from datetime import datetime, timedelta

from telethon import errors

from .filtering import Filter
from .model import normalize, topic_id
from .runtime import iso, utcnow


async def retry(operation, settings, sleep=asyncio.sleep):
    for attempt in range(settings.retries + 1):
        try:
            return await asyncio.wait_for(operation(), timeout=settings.request_timeout)
        except errors.FloodWaitError as exc:
            if attempt == settings.retries or exc.seconds > settings.max_flood_wait:
                raise
            await sleep(exc.seconds)
        except (OSError, TimeoutError, errors.ServerError, errors.RpcCallFailError):
            if attempt == settings.retries:
                raise
            await sleep(min(2**attempt, 30))


async def collect_stream(gateway, store, source, entity, chat, topic, settings, classifier, now):
    key = f"{chat.id}:{topic or 'all'}"
    state = store.stream(key)
    if state["scan"]:
        scan = json.loads(state["scan"])
    else:
        days = settings.refresh_days if state["initialized"] else settings.initial_days
        scan = {
            "before": 0,
            "high": state["cursor"],
            "cursor": state["cursor"],
            "initial": not state["initialized"],
            "cutoff": iso(now - timedelta(days=days)),
        }
        store.begin_scan(key, scan)
    cutoff = datetime.fromisoformat(scan["cutoff"])
    saved = 0
    while True:
        page = await retry(
            lambda: gateway.page(entity, topic, scan["before"], settings.page_size), settings
        )
        if not page:
            store.save_page(key, [], scan, complete=True)
            return saved
        ids = [message.id for message in page]
        if ids != sorted(set(ids), reverse=True) or (scan["before"] and max(ids) >= scan["before"]):
            raise RuntimeError("Telegram вернул страницу без продвижения; курсор сохранён")
        scan["high"] = max(scan["high"], max(ids))
        records, complete = [], False
        for message in page:
            if message.date is None:
                raise RuntimeError("Сообщение без даты; обход остановлен без продвижения")
            old = message.date < cutoff
            if old and (scan["initial"] or message.id <= scan["cursor"]):
                complete = True
                break
            if topic is not None and topic_id(message, chat.forum) != topic:
                continue
            records.append(normalize(message, chat, source.kind, classifier))
        scan["before"] = min(ids)
        store.save_page(key, records, scan, complete)
        saved += len(records)
        if complete:
            return saved
        await asyncio.sleep(settings.page_pause)


async def collect(gateway, store, config, report=print, now=None):
    now = now or utcnow()
    classifier = Filter(config.filters)
    outcomes = []
    for source in config.sources:
        try:
            entity, chat = await retry(
                lambda peer=source.peer: gateway.resolve(peer), config.collection
            )
            if source.topics and not chat.forum:
                raise ValueError("Темы заданы для источника, который не является форумом")
        except Exception as exc:
            # Exception text can contain API data; only expose its type.
            outcomes.append({"source": str(source.peer), "error": type(exc).__name__})
            report(f"{source.peer}: ошибка {type(exc).__name__}")
            continue
        for topic in source.topics or (None,):
            label = f"{source.peer} / {topic or 'all'}"
            try:
                count = await collect_stream(
                    gateway, store, source, entity, chat, topic, config.collection, classifier, now
                )
                outcomes.append({"source": label, "observed": count})
                report(f"{label}: обработано {count}")
            except Exception as exc:
                outcomes.append({"source": label, "error": type(exc).__name__})
                report(f"{label}: ошибка {type(exc).__name__}; прогресс сохранён")
    return outcomes
