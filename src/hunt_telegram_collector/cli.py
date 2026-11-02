import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .collector import collect, retry
from .config import ConfigError, load_config, parse_peer
from .exporting import Sheets, export_jsonl, export_sheets
from .runtime import data_directory, exclusive_lock
from .storage import Store
from .telegram import Telegram


def parser():
    result = argparse.ArgumentParser(description="Read-only сборщик Telegram-вакансий")
    result.add_argument("--config", type=Path, default=Path("config.yaml"))
    result.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Интерактивный вход в локальном терминале")
    sources = commands.add_parser("sources", help="Доступные группы/каналы и их ID")
    sources.add_argument("--topics", metavar="PEER", help="Темы конкретного форума")
    commands.add_parser("collect", help="Новые сообщения и перепроверка недавних")
    export = commands.add_parser("export", help="Экспорт всех накопленных сообщений")
    export.add_argument("--format", choices=("jsonl", "sheets"), default="jsonl")
    export.add_argument("--output", type=Path)
    commands.add_parser("status", help="Состояние SQLite и последних запусков")
    return result


def local_times(value):
    if isinstance(value, dict):
        return {k: local_times(v) for k, v in value.items()}
    if isinstance(value, list):
        return [local_times(v) for v in value]
    if isinstance(value, str) and len(value) > 19 and value[4:5] == "-" and "T" in value:
        try:
            return datetime.fromisoformat(value).astimezone(ZoneInfo("Europe/Moscow")).isoformat()
        except ValueError:
            pass
    return value


async def telegram_command(args, root, store, config):
    gateway = Telegram(root)
    try:
        if args.command == "login":
            if not sys.stdin.isatty():
                raise ConfigError("Выполните hunt login самостоятельно в локальном терминале")
            await gateway.login()
            print("Пользовательская сессия сохранена локально.")
            return []
        await retry(gateway.connect, config.collection)
        if args.command == "collect":
            return await collect(gateway, store, config)
        if args.topics:
            entity, chat = await retry(
                lambda: gateway.resolve(parse_peer(args.topics)), config.collection
            )
            if not chat.forum:
                raise ConfigError("Источник не является форумом")
            async for topic in gateway.topics(entity):
                print(json.dumps(topic, ensure_ascii=False))
        else:
            async for source in gateway.sources():
                print(json.dumps(source, ensure_ascii=False))
        return []
    finally:
        await gateway.close()


def run(args):
    # CLI creates session/database/export files owner-only; no secret values in logs.
    os.umask(0o077)
    logging.getLogger("telethon").setLevel(logging.CRITICAL)
    load_dotenv(args.env_file, override=False)
    root = data_directory()
    with exclusive_lock(root):
        store = Store(root / "messages.sqlite3")
        run_id = None
        try:
            if args.command == "status":
                print(json.dumps(local_times(store.status()), ensure_ascii=False, indent=2))
                return 0
            from .config import Config

            needs_config = args.command == "collect" or (
                args.command == "export" and args.format == "sheets"
            )
            config = load_config(args.config) if needs_config or args.config.exists() else Config()
            if args.command == "collect" and not config.sources:
                raise ConfigError("Список sources пуст: выберите источники в config.yaml")
            run_id = store.start_run(args.command)
            if args.command in ("login", "sources", "collect"):
                outcomes = asyncio.run(telegram_command(args, root, store, config))
                failed = any("error" in result for result in outcomes)
                store.finish_run(run_id, "partial_failure" if failed else "ok", outcomes)
                return 1 if failed else 0
            if args.format == "jsonl":
                output = args.output or root / "exports/messages.jsonl"
                count = export_jsonl(store, output)
                destination = str(output.expanduser().resolve())
            else:
                if args.output:
                    raise ConfigError("--output применяется только к JSONL")
                remote = Sheets(
                    config.sheets.get("spreadsheet_id"),
                    config.sheets.get("worksheet"),
                    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
                )
                try:
                    count = export_sheets(store, remote, config.collection.retries)
                    destination = remote.destination
                finally:
                    remote.close()
            store.finish_run(run_id, "ok", {"records": count, "destination": destination})
            print(f"Экспортировано записей: {count}; {destination}")
            return 0
        except BaseException as exc:
            if run_id is not None:
                store.finish_run(
                    run_id,
                    "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed",
                    {"error": type(exc).__name__},
                )
            raise
        finally:
            store.close()


def main():
    args = parser().parse_args()
    try:
        code = run(args)
    except ConfigError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        print(
            "Прервано; сохранённые страницы будут использованы при повторном запуске.",
            file=sys.stderr,
        )
        code = 130
    except Exception as exc:
        print(
            f"Ошибка {type(exc).__name__}. Проверьте hunt status, доступ и конфигурацию. "
            "Детали API скрыты, чтобы не раскрыть секреты.",
            file=sys.stderr,
        )
        code = 1
    raise SystemExit(code)
