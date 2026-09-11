from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


def unescape(text: str) -> str:
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def segments(message: Any) -> list[dict]:
    if isinstance(message, list):
        return [s for s in message if isinstance(s, dict) and isinstance(s.get("data"), dict)]
    if not isinstance(message, str):
        raise ValueError("Invalid OneBot message")
    result, pos = [], 0
    for match in re.finditer(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]", message):
        if match.start() > pos:
            result.append({"type": "text", "data": {"text": unescape(message[pos:match.start()])}})
        data = {}
        for pair in match[2].lstrip(",").split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                data[key] = unescape(value)
        result.append({"type": match[1], "data": data})
        pos = match.end()
    if pos < len(message):
        result.append({"type": "text", "data": {"text": unescape(message[pos:])}})
    return result


def display_text(parts, self_id):
    """Readable mentions for the model; identity checks still use structured segments."""
    return "".join(str(s["data"].get("text", "")) if s.get("type") == "text"
        else "@" + str(s["data"].get("qq", "")) if s.get("type") == "at" and str(s["data"].get("qq")) != self_id
        else "" if s.get("type") in ("at", "reply") else f"[{s.get('type', 'unknown')}]"
        for s in parts).strip()


def image_references(parts, message_id):
    """Expose stable message-local image IDs, never download URLs or filesystem paths."""
    if not re.fullmatch(r"-?[0-9]{1,24}", str(message_id)):
        return []
    images = [p for p in parts if isinstance(p, dict) and p.get("type") == "image"]
    return [{"image_id": f"img_{message_id}_{i}", "position": i} for i in range(1, len(images) + 1)]


@dataclass(frozen=True)
class Event:
    group_id: str
    user_id: str
    self_id: str
    message_id: str
    timestamp: float
    nickname: str
    text: str
    mentions: tuple[str, ...]
    reply_id: str | None
    parts: tuple[dict, ...]

    @classmethod
    def parse(cls, raw: dict) -> Event | None:
        if raw.get("post_type") != "message" or raw.get("message_type") != "group":
            return None
        parts = segments(raw.get("message", []))
        sender = raw.get("sender") or {}
        text = "".join(str(s["data"].get("text", "")) if s.get("type") == "text"
                       else f'[{s.get("type", "unknown")}]' if s.get("type") not in ("at", "reply") else ""
                       for s in parts).strip()
        return cls(str(int(raw["group_id"])), str(int(raw["user_id"])), str(int(raw["self_id"])),
            str(int(raw["message_id"])), float(raw.get("time", 0)),
            str(sender.get("card") or sender.get("nickname") or raw["user_id"])[:80], text,
            tuple(str(s["data"].get("qq")) for s in parts if s.get("type") == "at"),
            next((str(s["data"].get("id")) for s in parts if s.get("type") == "reply"), None), tuple(parts))

    @property
    def key(self):
        return f"{self.self_id}:{self.group_id}:{self.message_id}"


@dataclass(frozen=True)
class Receipt:
    status: str
    message_id: str | None = None
    reason: str = ""
