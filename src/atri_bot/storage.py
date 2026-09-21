from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import time
import uuid

log = logging.getLogger("atri.storage")


def history_timestamp(row):
    """Use message time for incoming messages and confirmed-send time for bot replies."""
    candidates = (row.get("time"),) if row.get("role") == "assistant" else (row.get("timestamp"), row.get("time"))
    for value in candidates:
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            return value
    return None


def sticker_metadata(value):
    """Only visible, bounded sticker metadata belongs in logs or model context."""
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        return None
    limits = {"id": 80, "title": 120, "description": 500, "visible_text": 300}
    metadata = {field: value[field][:limit] for field, limit in limits.items()
                if isinstance(value.get(field), str)}
    if isinstance(value.get("visible_text"), list):
        metadata["visible_text"] = [text[:80] for text in value["visible_text"][:5]
                                    if isinstance(text, str)]
    return metadata


def delivery_text(row):
    """Render sent images as image observations, never as words spoken by ATRI."""
    text = str(row.get("text", ""))
    sticker = sticker_metadata(row.get("sticker"))
    if sticker is None:
        return text
    description = sticker.get("description") or sticker.get("title") or "表情图片"
    if sticker.get("visible_text"):
        visible_text = sticker["visible_text"]
        if isinstance(visible_text, list):
            visible_text = " / ".join(visible_text)
        description += f"；图中文字：{visible_text}"
    marker = f"[表情 {sticker['id']}：{description}]"
    pieces = [marker, text] if row.get("sticker_position") == "before" else [text, marker]
    return "\n".join(piece for piece in pieces if piece)


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
    def __init__(self, root: Path, group_id: str, *, history_seconds=3600, now=None):
        self.directory = root / "groups" / str(int(group_id))
        self.path = self.directory / "messages.jsonl"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.seen: set[str] = set()
        self.history = deque()
        self.history_seconds = history_seconds
        self.now = now or time.time
        self.last_receipts = {}
        self.sent_message_ids = set()
        self._confirmed_deliveries = set()
        self._chat_turns = deque(maxlen=100)
        self._turns_since_last_sticker = 0
        self._last_sticker_id = None
        self.last_sent = None
        self.activity = deque()
        log.debug("[加载群记录] 路径=%s 历史时间窗口=%ds", self.path, history_seconds)
        for row in read_jsonl(self.path):
            self._apply(row)
        self.prune_history()
        # A crash after submission leaves an unknown result, never silently retried.
        for key, row in list(self.last_receipts.items()):
            if row["status"] == "pending":
                log.warning("[恢复发送状态] key=%s 原状态=pending，标记为unknown，避免重复发送", key)
                self.append({**row, "time": self.now(), "status": "unknown", "reason": "process_restarted"})
        log.debug("[加载完成] 历史=%d 已收消息=%d 已发送ID=%d 近期活动=%d",
                  len(self.history), len(self.seen), len(self.sent_message_ids), len(self.activity))

    def _apply(self, row):
        now = row["time"]
        while self.activity and now - self.activity[0][0] > 300:
            self.activity.popleft()
        if row["kind"] == "command":
            self.seen.add(row["key"])
        elif row["kind"] == "incoming":
            self.seen.add(row["key"])
            self._remember(row)
            self.activity.append((now, False))
        elif row["kind"] == "delivery":
            self.last_receipts[row["key"]] = row
            if row["status"] == "sent":
                identity = (("message", str(row["message_id"])) if row.get("message_id") is not None
                            else ("key", row["key"]))
                if identity in self._confirmed_deliveries:
                    return
                self._confirmed_deliveries.add(identity)
                supplement = row.get("delivery_origin") == "sticker_supplement"
                if not supplement:
                    self.last_sent = row
                if row.get("message_id") is not None:
                    self.sent_message_ids.add(str(row["message_id"]))
                if not supplement:
                    self.activity.append((now, True))
                sticker = sticker_metadata(row.get("sticker"))
                if row.get("delivery_origin", "chat") == "chat":
                    sticker_id = sticker["id"] if sticker else None
                    self._chat_turns.append({"id": row.get("turn_id", row["key"]),
                        "message_id": str(row.get("message_id")), "sticker_id": sticker_id})
                    if sticker_id is None:
                        self._turns_since_last_sticker += 1
                    else:
                        self._turns_since_last_sticker = 0
                        self._last_sticker_id = sticker_id
                elif supplement and sticker:
                    # Attach the confirmed image to the existing text turn, even
                    # if its network acknowledgement arrived after newer text.
                    for turn in self._chat_turns:
                        if (turn["id"] == row.get("turn_id")
                                and turn["message_id"] == str(row.get("parent_message_id"))
                                and turn["sticker_id"] is None):
                            turn["sticker_id"] = sticker["id"]
                            for distance, recent in enumerate(reversed(self._chat_turns)):
                                if recent["sticker_id"] is not None:
                                    self._turns_since_last_sticker = distance
                                    self._last_sticker_id = recent["sticker_id"]
                                    break
                            break
                self._remember({"role": "assistant", "text": delivery_text(row), "time": now,
                                "message_id": row.get("message_id"),
                                **({"sticker": sticker} if sticker else {})})

    def sticker_state(self, *, target_min=3, target_max=5, window=20):
        if type(window) is not int or not 1 <= window <= 100:
            raise ValueError("sticker history window must be in 1..100")
        recent = [turn["sticker_id"] for turn in list(self._chat_turns)[-window:]]
        recent_ids = [sticker_id for sticker_id in recent if sticker_id is not None]
        return {"turns_since_last_sticker": self._turns_since_last_sticker,
                "recent_turns": len(recent), "recent_sticker_count": len(recent_ids),
                "recent_sticker_ids": recent_ids, "last_sticker_id": self._last_sticker_id,
                "target_turns_min": target_min, "target_turns_max": target_max}

    def _remember(self, row):
        stamp = history_timestamp(row)
        if stamp is not None and stamp >= self.now() - self.history_seconds:
            self.history.append(row)

    def prune_history(self):
        cutoff = self.now() - self.history_seconds
        # Message timestamps may arrive out of order, so trimming only the head is insufficient.
        kept = [row for row in self.history if (stamp := history_timestamp(row)) is not None and stamp >= cutoff]
        self.history.clear()
        self.history.extend(kept)

    def append(self, row):
        row = {"time": self.now(), **row}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._apply(row)
        self.prune_history()
        log.debug("[记录已落盘] 类型=%s 阶段=%s 状态=%s key=%s 历史条数=%d",
                  row["kind"], row.get("stage", "-"), row.get("status", "-"), row.get("key", "-"), len(self.history))
