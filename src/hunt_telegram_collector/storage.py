import hashlib
import json
import sqlite3

from .runtime import iso, utcnow


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                payload TEXT NOT NULL, revision TEXT NOT NULL, text_hash TEXT,
                PRIMARY KEY(chat_id, message_id));
            CREATE INDEX IF NOT EXISTS messages_text_hash ON messages(text_hash);
            CREATE TABLE IF NOT EXISTS streams (
                key TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
                initialized INTEGER NOT NULL DEFAULT 0, scan TEXT, last_success TEXT);
            CREATE TABLE IF NOT EXISTS exports (
                destination TEXT NOT NULL, record_key TEXT NOT NULL,
                revision TEXT NOT NULL, exported_at TEXT NOT NULL,
                PRIMARY KEY(destination, record_key));
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, command TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL, detail TEXT);
        """)

    def close(self):
        self.db.close()

    def start_run(self, command):
        with self.db:
            self.db.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
            return self.db.execute(
                "INSERT INTO runs(command, started_at, status) VALUES (?, ?, 'running')",
                (command, iso(utcnow())),
            ).lastrowid

    def finish_run(self, run, status, detail):
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
                (iso(utcnow()), status, canonical(detail), run),
            )

    def stream(self, key):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO streams(key) VALUES (?)", (key,))
        return dict(self.db.execute("SELECT * FROM streams WHERE key=?", (key,)).fetchone())

    def begin_scan(self, key, scan):
        with self.db:
            self.db.execute("UPDATE streams SET scan=? WHERE key=?", (canonical(scan), key))

    def _save(self, record):
        revision = digest({k: v for k, v in record.items() if k != "collected_at"})
        text_hash = hashlib.sha256(record["text"].encode()).hexdigest() if record["text"] else None
        self.db.execute(
            """
            INSERT INTO messages VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, message_id) DO UPDATE SET
                payload=excluded.payload, revision=excluded.revision, text_hash=excluded.text_hash
            WHERE messages.revision != excluded.revision
        """,
            (record["chat_id"], record["message_id"], canonical(record), revision, text_hash),
        )

    def save_page(self, key, records, scan, complete=False):
        # Message writes and traversal checkpoint are one atomic SQLite transaction.
        with self.db:
            for record in records:
                self._save(record)
            if complete:
                self.db.execute(
                    """UPDATE streams SET cursor=MAX(cursor, ?), initialized=1,
                                scan=NULL, last_success=? WHERE key=?""",
                    (scan["high"], iso(utcnow()), key),
                )
            else:
                self.db.execute("UPDATE streams SET scan=? WHERE key=?", (canonical(scan), key))

    def records(self):
        rows = self.db.execute("""
            SELECT m.*, (SELECT d.chat_id || ':' || d.message_id FROM messages d
                WHERE d.text_hash=m.text_hash AND d.chat_id != m.chat_id
                ORDER BY d.chat_id, d.message_id LIMIT 1) AS duplicate_of
            FROM messages m ORDER BY m.chat_id, m.message_id
        """)
        for row in rows:
            record = json.loads(row["payload"])
            record["probable_duplicate_of"] = row["duplicate_of"]
            yield record

    def confirm_export(self, destination, records):
        with self.db:
            self.db.executemany(
                """INSERT INTO exports VALUES (?, ?, ?, ?)
                ON CONFLICT(destination, record_key) DO UPDATE SET
                revision=excluded.revision, exported_at=excluded.exported_at""",
                [(destination, r["record_key"], digest(r), iso(utcnow())) for r in records],
            )

    def status(self):
        return {
            "messages": self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "filters": dict(
                self.db.execute("""SELECT json_extract(payload, '$.filter_result'),
                                COUNT(*) FROM messages GROUP BY 1""")
            ),
            "streams": [
                dict(r)
                for r in self.db.execute(
                    "SELECT key, cursor, initialized, scan IS NOT NULL AS incomplete, "
                    "last_success FROM streams"
                )
            ],
            "recent_runs": [
                dict(r) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10")
            ],
            "exports": [
                dict(r)
                for r in self.db.execute("""SELECT destination, COUNT(*) AS records,
                          MAX(exported_at) AS last_export FROM exports GROUP BY destination""")
            ],
        }
