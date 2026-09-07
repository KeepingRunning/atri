from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
import uuid


def read_jsonl(path: Path):
    """Recover only an incomplete final append; preserve its bytes for inspection."""
    if not path.exists():
        return
    with path.open("rb+") as f:
        while True:
            offset = f.tell()
            line = f.readline()
            if not line:
                return
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if f.read(1):
                    raise ValueError(f"Corrupt JSONL record in {path.name} at {offset}")
                backup = path.with_name(path.name + f".torn-{uuid.uuid4().hex}")
                backup.write_bytes(line)
                f.seek(offset)
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
                return
            if not line.endswith(b"\n"):
                f.seek(0, 2)
                f.write(b"\n")
                f.flush()
                os.fsync(f.fileno())
            yield row


@contextmanager
def single_instance(data: Path):
    """The JSONL store has exactly one process writer (POSIX and Windows)."""
    data.mkdir(parents=True, exist_ok=True)
    with (data / ".lock").open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Another ATRI process uses this data directory") from None
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another ATRI process uses this data directory") from None
        yield


class GroupLog:
    def __init__(self, root: Path, group_id: str, history_limit=51):
        self.directory = root / "groups" / str(int(group_id))
        self.path = self.directory / "messages.jsonl"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.seen: set[str] = set()
        self.history = deque(maxlen=history_limit)
        self.last_receipts = {}
        self.sent_message_ids = set()
        self.last_sent = None
        self.activity = deque()
        for row in read_jsonl(self.path):
            self._apply(row)
        # A crash after submission leaves an unknown result, never silently retried.
        for key, row in list(self.last_receipts.items()):
            if row["status"] == "pending":
                self.append({"kind": "delivery", "key": key, "status": "unknown", "reason": "process_restarted"})

    def _apply(self, row):
        now = row["time"]
        while self.activity and now - self.activity[0][0] > 300:
            self.activity.popleft()
        if row["kind"] == "incoming":
            self.seen.add(row["key"])
            self.history.append(row)
            self.activity.append((now, False))
        elif row["kind"] == "delivery":
            self.last_receipts[row["key"]] = row
            if row["status"] == "sent":
                self.last_sent = row
                if row.get("message_id") is not None:
                    self.sent_message_ids.add(str(row["message_id"]))
                self.activity.append((now, True))
                self.history.append({"role": "assistant", "text": row.get("text", ""), "time": now,
                                     "message_id": row.get("message_id")})

    def append(self, row):
        row = {"time": time.time(), **row}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._apply(row)
