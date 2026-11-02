import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    peer: str | int
    kind: str = "jobs"
    topics: tuple[int, ...] = ()


@dataclass(frozen=True)
class Collection:
    initial_days: float = 7
    refresh_days: float = 2
    page_size: int = 100
    retries: int = 3
    max_flood_wait: int = 60
    request_timeout: float = 45
    page_pause: float = 1


@dataclass(frozen=True)
class Config:
    sources: tuple[Source, ...] = ()
    collection: Collection = field(default_factory=Collection)
    filters: dict = field(default_factory=dict)
    sheets: dict = field(default_factory=dict)


def parse_peer(value):
    if isinstance(value, str) and re.fullmatch(r"-\d+", value):
        value = int(value)
    if type(value) is int and value < 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return "@" + value.lstrip("@").lower()
    raise ConfigError("peer: укажите @username или отрицательный ID из hunt sources")


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("Не удалось прочитать YAML конфигурации") from exc
    try:
        if not isinstance(raw, dict) or set(raw) - {"sources", "collection", "filters", "sheets"}:
            raise ValueError
        collection = Collection(**raw.get("collection", {}))
        for name in ("initial_days", "refresh_days", "request_timeout"):
            value = getattr(collection, name)
            if type(value) not in (int, float) or not 0 < value < 100000:
                raise ValueError
        for name, low, high in (
            ("page_size", 1, 100),
            ("retries", 0, 10),
            ("max_flood_wait", 0, 3600),
        ):
            value = getattr(collection, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError
        if type(collection.page_pause) not in (int, float) or not 0 <= collection.page_pause <= 60:
            raise ValueError
        sources = []
        seen = set()
        for item in raw.get("sources", []):
            if set(item) - {"peer", "kind", "topics"}:
                raise ValueError
            peer = parse_peer(item["peer"])
            kind = item.get("kind", "jobs")
            topics = item.get("topics", [])
            if kind not in ("jobs", "general") or not isinstance(topics, list):
                raise ValueError
            if any(type(t) is not int or t <= 0 for t in topics):
                raise ValueError
            if peer in seen or len(set(topics)) != len(topics):
                raise ValueError
            seen.add(peer)
            sources.append(Source(peer, kind, tuple(topics)))
        filters = raw.get("filters", {})
        if not isinstance(filters, dict) or set(filters) - {
            "vacancy_patterns",
            "relevance_patterns",
            "resume_patterns",
        }:
            raise ValueError
        for patterns in filters.values():
            if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
                raise ValueError
            for pattern in patterns:
                re.compile(pattern)
        sheets = raw.get("sheets", {})
        if not isinstance(sheets, dict) or set(sheets) - {"spreadsheet_id", "worksheet"}:
            raise ValueError
        if any(not isinstance(v, str) for v in sheets.values()):
            raise ValueError
        return Config(tuple(sources), collection, filters, sheets)
    except (TypeError, ValueError, KeyError, re.error) as exc:
        raise ConfigError("Некорректная конфигурация; сравните с config.example.yaml") from exc
