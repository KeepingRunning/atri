"""Reviewed local voice clips, bounded search, and OneBot record preparation."""
from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import unicodedata

from .stickers import _terms
from .tools import ToolError

log = logging.getLogger("atri.voices")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_PUBLIC = ("id", "title", "text_ja", "text_zh", "duration_seconds", "description",
           "emotions", "intensity", "usage", "avoid", "semantic_group")
_FIELDS = {"text_zh": 5, "title": 4, "description": 1, "emotions": 4,
           "usage": 2, "context_requirements": 1}


def _relative(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(char) < 32 for char in value) or "\\" in value or ":" in value):
        raise ValueError("invalid local voice path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("voice paths must be relative and stay inside the library")
    return path


@dataclass
class VoiceConfig:
    enabled: bool = False
    catalog: str = "data/voice_review/catalog.json"
    selection: str = "data/voice_review/selection.json"
    search_limit: int = 6
    target_turns_min: int = 6
    target_turns_max: int = 10
    preferred_max_seconds: float = 5
    max_audio_bytes: int = 8 * 1024 * 1024

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("voices.enabled must be a boolean")
        for name in ("catalog", "selection"):
            try:
                _relative(getattr(self, name))
            except ValueError:
                raise ValueError(f"voices.{name} must be a relative local path") from None
        for name, low, high in (("search_limit", 1, 20), ("target_turns_min", 1, 100),
                                ("target_turns_max", 1, 100),
                                ("max_audio_bytes", 1024, 32 * 1024 * 1024)):
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ValueError(f"voices.{name} must be an integer in {low}..{high}")
        if self.target_turns_max < self.target_turns_min:
            raise ValueError("voices.target_turns_max must be >= target_turns_min")
        if (type(self.preferred_max_seconds) not in (int, float)
                or not math.isfinite(self.preferred_max_seconds)
                or not 0 < self.preferred_max_seconds <= 60):
            raise ValueError("voices.preferred_max_seconds must be in (0, 60]")


def semantic_group(text):
    """Recordings of the same original line share a stable repetition key."""
    normalized = "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                         if not char.isspace() and not unicodedata.category(char).startswith("P"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _inside(directory, relative):
    path = (directory / _relative(relative)).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("voice path escapes library")
    return path


def _read_json(path, max_bytes):
    with path.open("rb") as file:
        raw = file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("voice metadata too large")

    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate voice metadata key")
            obj[key] = value
        return obj

    return json.loads(raw, object_pairs_hook=unique)


def _validate_row(row, directory):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"]):
        raise ValueError("invalid voice identifier")
    for key, max_chars in (("text_ja", 1500), ("text_zh", 1500), ("title", 100), ("description", 1000)):
        value = row.get(key)
        if (not isinstance(value, str) or not value.strip() or len(value) > max_chars
                or any(ord(char) < 32 and char not in "\n\t\r" for char in value)):
            raise ValueError("invalid voice text")
    for key in ("emotions", "usage", "avoid", "context_requirements"):
        values = row.get(key, []) if key == "context_requirements" else row.get(key)
        if (not isinstance(values, list) or len(values) > 20
                or any(not isinstance(value, str) or not value.strip() or len(value) > 1000
                       or any(ord(char) < 32 for char in value) for value in values)):
            raise ValueError("invalid voice keywords")
    if type(row.get("intensity")) is not int or not 1 <= row["intensity"] <= 3:
        raise ValueError("invalid voice intensity")
    seconds = row.get("duration_seconds")
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise ValueError("invalid voice duration")
    if row.get("annotation_status") not in ("pending", "complete", "failed") or type(row.get("needs_review")) is not bool:
        raise ValueError("invalid voice review state")
    if not isinstance(row.get("sha256"), str) or not _SHA.fullmatch(row["sha256"]):
        raise ValueError("invalid voice hash")
    path = _inside(directory, row.get("file"))
    if path.suffix.lower() != ".mp3":
        raise ValueError("voice file must be MP3")


def _mp3_format(raw):
    """Check ID3 bounds and two contiguous MPEG Layer III frames, without decoding."""
    offset = 0
    if raw.startswith(b"ID3"):
        if len(raw) < 10 or raw[3] not in (2, 3, 4) or any(byte & 0x80 for byte in raw[6:10]):
            return False
        offset = 10 + sum(byte << (7 * (3 - index)) for index, byte in enumerate(raw[6:10]))
        if raw[3] == 4 and raw[5] & 0x10:
            offset += 10
    for _ in range(2):
        if offset + 4 > len(raw):
            return False
        header = int.from_bytes(raw[offset:offset + 4], "big")
        version, layer = (header >> 19) & 3, (header >> 17) & 3
        bitrate_index, rate_index = (header >> 12) & 15, (header >> 10) & 3
        if (header >> 21 != 0x7ff or version == 1 or layer != 1
                or bitrate_index in (0, 15) or rate_index == 3):
            return False
        rates = (44100, 48000, 32000)
        bitrates = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
                    if version == 3 else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))
        rate = rates[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
        frame_length = (144 if version == 3 else 72) * bitrates[bitrate_index] * 1000 // rate + ((header >> 9) & 1)
        offset += frame_length
        if offset > len(raw):
            return False
    return True


class VoiceLibrary:
    """Keep all assets on disk; expose only selected, annotated, usable lines."""

    def __init__(self, root: Path, config: VoiceConfig):
        config.validate()
        self.config = config
        self.root = Path(root).resolve()
        self.catalog = _inside(self.root, config.catalog)
        self.selection = _inside(self.root, config.selection)
        self._items, self._index, self._df = {}, {}, Counter()
        self.stats = {key: 0 for key in ("total", "retained", "available", "pending", "needs_review", "punctuation", "unselected")}
        if not config.enabled:
            return
        try:
            rows = _read_json(self.catalog, 16 * 1024 * 1024)["items"]
            selected = _read_json(self.selection, 1024 * 1024)
            if not isinstance(rows, list) or len(rows) > 10000 or not isinstance(selected, dict):
                raise ValueError("invalid voice catalog")
            ids_by_state = {}
            for key in ("keep_ids", "delete_ids", "unreviewed_ids"):
                values = selected.get(key, []) if key != "keep_ids" else selected[key]
                if (not isinstance(values, list) or len(values) > 10000
                        or any(not isinstance(identity, str) or not _ID.fullmatch(identity) for identity in values)
                        or len(values) != len(set(values))):
                    raise ValueError("invalid voice selection")
                ids_by_state[key] = set(values)
            keep = ids_by_state["keep_ids"]
            if any(ids_by_state[left] & ids_by_state[right]
                   for left, right in (("keep_ids", "delete_ids"), ("keep_ids", "unreviewed_ids"), ("delete_ids", "unreviewed_ids"))):
                raise ValueError("conflicting voice selection")
            seen = set()
            for row in rows:
                _validate_row(row, self.catalog.parent)
                identity = row["id"]
                if identity in seen:
                    raise ValueError("duplicate voice identifier")
                seen.add(identity)
                if identity not in keep:
                    self.stats["unselected"] += 1
                    continue
                self.stats["retained"] += 1
                if not any(char.isalnum() for char in row["text_ja"]):
                    self.stats["punctuation"] += 1
                    continue
                if row["annotation_status"] != "complete":
                    self.stats["pending"] += 1
                    continue
                if row["needs_review"]:
                    self.stats["needs_review"] += 1
                    continue
                item = deepcopy(row)
                item["semantic_group"] = semantic_group(row["text_ja"])
                self._items[identity] = item
                weights = Counter()
                for key, weight in _FIELDS.items():
                    value = item.get(key, [])
                    value = value if isinstance(value, str) else " ".join(value)
                    for term in _terms(value, unigrams=True):
                        weights[term] += weight
                self._index[identity] = weights
                self._df.update(weights.keys())
            if set.union(*ids_by_state.values()) - seen:
                raise ValueError("selection contains unknown voice identifiers")
            self.stats.update(total=len(rows), available=len(self))
        except (OSError, ValueError, KeyError, TypeError, RecursionError, OverflowError):
            raise ValueError("Cannot load a valid voice catalog and selection") from None
        log.info("[语音库加载] 保留=%d 可用=%d 待标注=%d 待复核=%d 纯标点=%d", self.stats["retained"],
                 len(self), self.stats["pending"], self.stats["needs_review"], self.stats["punctuation"])

    def __len__(self):
        return len(self._items)

    @property
    def exclusion_counts(self):
        return {key: self.stats[key] for key in ("pending", "needs_review", "punctuation", "unselected")}

    def get(self, voice_id):
        if not isinstance(voice_id, str) or voice_id not in self._items:
            raise ToolError("voice_not_found", "语音编号不可用，或素材尚未完成用途标注与审核。")
        row = self._items[voice_id]
        metadata = {key: row[key] for key in _PUBLIC}
        if "context_requirements" in row:
            metadata["context_requirements"] = row["context_requirements"]
        return deepcopy(metadata)

    def search(self, query, limit=6, *, recent_ids=(), recent_groups=()):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ToolError("invalid_voice_query", "请提供不超过 500 字的台词含义、情绪或语境。")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ToolError("invalid_voice_limit", "语音检索条数应为 1 到 20 的整数。")
        terms = _terms(query)
        recent = set(recent_ids) if isinstance(recent_ids, (tuple, list, set, frozenset)) else set()
        groups = set(recent_groups) if isinstance(recent_groups, (tuple, list, set, frozenset)) else set()
        # A recent ID also identifies its line when another recording exists.
        groups.update(self._items[identity]["semantic_group"] for identity in recent if identity in self._items)
        scored = []
        for identity, weights in self._index.items():
            score = sum(weights[term] * (1 + math.log((len(self) + 1) / (self._df[term] + 1)))
                        for term in terms if term in weights)
            if score <= 0:
                continue
            row = self._items[identity]
            short = min(1, self.config.preferred_max_seconds / row["duration_seconds"])
            recent_factor = 0.3 if identity in recent or row["semantic_group"] in groups else 1
            scored.append((score * (0.6 + 0.4 * short) * recent_factor, identity))
        scored.sort(key=lambda entry: (-entry[0], self._items[entry[1]]["duration_seconds"], entry[1]))
        result, seen = [], set()
        for _, identity in scored:
            group = self._items[identity]["semantic_group"]
            if group in seen:
                continue
            seen.add(group)
            result.append(self.get(identity))
            if len(result) == limit:
                break
        return result

    def prepare(self, voice_id):
        metadata = self.get(voice_id)
        row = self._items[voice_id]
        try:
            path = _inside(self.catalog.parent, row["file"])
            if not path.is_file():
                raise OSError("not a regular audio file")
            with path.open("rb") as file:
                raw = file.read(self.config.max_audio_bytes + 1)
        except ValueError:
            raise ToolError("voice_invalid_path", "语音文件位置不符合素材库约束。") from None
        except OSError:
            raise ToolError("voice_file_unavailable", "语音文件不可读取。") from None
        if len(raw) > self.config.max_audio_bytes:
            raise ToolError("voice_too_large", "语音文件超过发送大小上限。")
        if hashlib.sha256(raw).hexdigest() != row["sha256"]:
            raise ToolError("voice_changed", "语音文件与已审核记录不一致，请重新检查素材。")
        if not _mp3_format(raw):
            raise ToolError("voice_invalid_audio", "语音文件头或 MP3 音频帧无效。")
        log.info("[语音发送准备] 编号=%s 时长=%.2fs 字节=%d", voice_id, row["duration_seconds"], len(raw))
        return {"type": "record", "data": {"file": "base64://" + base64.b64encode(raw).decode("ascii")}}, {
            **metadata, "format": "MP3", "sha256": row["sha256"], "bytes": len(raw)}
