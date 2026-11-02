import re
from dataclasses import dataclass

from telethon.tl import types

from .runtime import iso, utcnow


@dataclass(frozen=True)
class Chat:
    id: int
    title: str
    username: str | None = None
    channel: bool = True
    forum: bool = False


def topic_id(message, forum):
    if not forum:
        return None
    reply = getattr(message, "reply_to", None)
    if reply and getattr(reply, "forum_topic", False):
        return reply.reply_to_top_id or reply.reply_to_msg_id
    if isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
        return message.id
    return 1


def links(message):
    text = message.message or ""
    found = re.findall(r"(?:https?://|tg://|www\.|t\.me/)[^\s<>]+", text)
    found = [u.rstrip(".,;!?)\"'") for u in found]
    encoded = text.encode("utf-16-le")
    for entity in message.entities or []:
        if isinstance(entity, types.MessageEntityTextUrl):
            found.append(entity.url)
        elif isinstance(entity, types.MessageEntityUrl):
            found.append(
                encoded[entity.offset * 2 : (entity.offset + entity.length) * 2].decode("utf-16-le")
            )
    return list(dict.fromkeys(found))


def normalize(message, chat, kind, classifier):
    text = message.message or ""
    media = getattr(message, "media", None) is not None
    topic = topic_id(message, chat.forum)
    url, scope = None, "unavailable"
    if chat.channel:
        if chat.username:
            url, scope = f"https://t.me/{chat.username}/{message.id}", "public"
        else:
            # Marked channel ID = -(1_000_000_000_000 + bare channel ID).
            bare = -chat.id - 1_000_000_000_000
            if bare > 0:
                url, scope = f"https://t.me/c/{bare}/{message.id}", "members_only"
        if url and topic and topic != 1:
            url += f"?thread={topic}"
    verdict, reason = classifier.classify(text, media, kind)
    return {
        "record_key": f"{chat.id}:{message.id}",
        "chat_id": chat.id,
        "source_title": chat.title,
        "source_username": chat.username,
        "message_id": message.id,
        "topic_id": topic,
        "published_at": iso(message.date),
        "edited_at": iso(message.edit_date),
        "collected_at": iso(utcnow()),
        "text": text,
        "message_url": url,
        "link_access": scope,
        "links": links(message),
        "has_media": media,
        "filter_result": verdict,
        "filter_reason": reason,
    }
