import fcntl
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def utcnow():
    return datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat() if value else None


def data_directory():
    configured = os.environ.get("HUNT_DATA_DIR", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local/share/hunt-telegram-collector"
    )
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


@contextmanager
def exclusive_lock(root):
    # Never unlink a flock file: another process could then lock a different inode.
    with (root / "collector.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Другой процесс hunt уже использует этот каталог данных") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
