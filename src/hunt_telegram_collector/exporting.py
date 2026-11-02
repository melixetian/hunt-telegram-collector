import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from .config import ConfigError
from .storage import canonical

FIELDS = [
    "record_key",
    "chat_id",
    "source_title",
    "source_username",
    "message_id",
    "topic_id",
    "published_at",
    "edited_at",
    "collected_at",
    "text",
    "message_url",
    "link_access",
    "links",
    "has_media",
    "filter_result",
    "filter_reason",
    "probable_duplicate_of",
]


def export_jsonl(store, path):
    path = Path(path).expanduser().resolve()
    if path.suffix != ".jsonl":
        raise ConfigError("Для JSONL укажите файл с расширением .jsonl")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    records = list(store.records())
    fd, name = tempfile.mkstemp(prefix=".hunt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(canonical(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        store.confirm_export(f"jsonl:{path}", records)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return len(records)


class SheetsError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Google Sheets HTTP {status}")


def column(number):
    value = ""
    while number:
        number, rem = divmod(number - 1, 26)
        value = chr(65 + rem) + value
    return value


def sheet_row(record):
    def cell(value):
        if value is None:
            return ""
        if isinstance(value, (list, dict, bool)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    values = [cell(record.get(field)) for field in FIELDS]
    if any(len(v) > 50000 for v in values):
        raise ConfigError("Поле превышает лимит Sheets; полный текст доступен в JSONL")
    return values


class Sheets:
    def __init__(self, spreadsheet, worksheet, credentials_path):
        if not spreadsheet or not worksheet:
            raise ConfigError("Укажите spreadsheet_id и worksheet в config.yaml")
        if worksheet.strip().casefold() == "вакансии":
            raise ConfigError("Запись в лист «Вакансии» запрещена; выберите входящий лист")
        if not credentials_path:
            raise ConfigError("Укажите GOOGLE_APPLICATION_CREDENTIALS локально")
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise ConfigError(
                "Установите необязательные зависимости: pip install '.[sheets]'"
            ) from exc
        credentials = Credentials.from_service_account_file(
            str(Path(credentials_path).expanduser()),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.session = AuthorizedSession(credentials)
        self.base = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet, safe='')}"
        self.tab = "'" + worksheet.replace("'", "''") + "'"
        self.destination = f"sheets:{spreadsheet}:{worksheet}"

    def close(self):
        self.session.close()

    def request(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=45, **kwargs)
        if not response.ok:
            # Never print the response body: it may echo private message contents.
            raise SheetsError(response.status_code)
        return response.json()

    def read(self):
        # Read entire existing tab, including human-owned columns, to avoid reusing occupied rows.
        result = self.request(
            "GET",
            self.base + "/values/" + quote(self.tab, safe=""),
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
        )
        return result.get("values", [])

    def ensure_capacity(self, rows):
        result = self.request("GET", self.base, params={"fields": "sheets.properties"})
        title = self.tab[1:-1].replace("''", "'")
        properties = next(
            (s["properties"] for s in result["sheets"] if s["properties"]["title"] == title), None
        )
        if not properties:
            raise ConfigError("Создайте согласованный входящий лист до экспорта")
        grid = properties["gridProperties"]
        grow = {}
        if rows > grid["rowCount"]:
            grow["rowCount"] = rows + 100
        if len(FIELDS) > grid["columnCount"]:
            grow["columnCount"] = len(FIELDS)
        if grow:
            self.request(
                "POST",
                self.base + ":batchUpdate",
                json={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": properties["sheetId"],
                                    "gridProperties": grow,
                                },
                                "fields": ",".join("gridProperties." + k for k in grow),
                            }
                        }
                    ]
                },
            )

    def write(self, updates):
        data = [
            {"range": f"{self.tab}!A{index}:{column(len(FIELDS))}{index}", "values": [values]}
            for index, values in updates
        ]
        self.request(
            "POST",
            self.base + "/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": data},
        )


def sync_sheet(store, remote):
    rows = remote.read()
    updates = []
    if rows:
        if rows[0][: len(FIELDS)] != FIELDS:
            raise ConfigError("Заголовки входящего листа не совпадают со схемой; запись отменена")
    else:
        updates.append((1, FIELDS))
    indexed = {}
    for number, row in enumerate(rows[1:], 2):
        key = str(row[0]) if row else ""
        if key:
            if key in indexed:
                raise ConfigError("В Google Sheets уже есть повторяющиеся record_key")
            indexed[key] = (number, row)
    records = list(store.records())
    next_row = max(2, len(rows) + 1)
    for record in records:
        key, values = record["record_key"], sheet_row(record)
        if key in indexed:
            number, old = indexed[key]
            old = [str(v) for v in old[: len(FIELDS)]]
            old += [""] * (len(FIELDS) - len(old))
            if old != values:
                updates.append((number, values))
        else:
            updates.append((next_row, values))
            next_row += 1
    if updates:
        remote.ensure_capacity(max(index for index, _ in updates))
        for start in range(0, len(updates), 100):
            remote.write(updates[start : start + 100])
    # Also acknowledge rows already present after an uncertain previous write.
    store.confirm_export(remote.destination, records)
    return len(records)


def export_sheets(store, remote, retries=3, sleep=time.sleep):
    import requests

    for attempt in range(retries + 1):
        try:
            return sync_sheet(store, remote)
        except (requests.ConnectionError, requests.Timeout, SheetsError) as exc:
            if isinstance(exc, SheetsError) and exc.status not in (429, 500, 502, 503, 504):
                raise
            if attempt == retries:
                raise
            sleep(min(2**attempt, 30))
            # Re-read keys before replanning; never blindly retry an append.
