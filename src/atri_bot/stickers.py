"""Local, reviewed sticker search and bounded OneBot image preparation."""
from __future__ import annotations

import base64
from collections import Counter, OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import logging
import math
from pathlib import Path
import re
import threading
import unicodedata
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .tools import ToolError, ToolResult, ToolSpec

log = logging.getLogger("atri.stickers")
_PUBLIC = ("id", "title", "description", "visible_text", "emotions", "intensity",
           "usage", "avoid", "animation_summary")
_FIELDS = {"title": 4, "description": 1, "visible_text": 4, "emotions": 4, "usage": 2}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_WORDS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+")
_STOP = frozenset(("一个", "一些", "这张", "那个", "图片", "表情", "表情包", "群聊",
                   "角色", "亚托莉", "表达", "表示", "适合", "使用", "用于", "进行",
                   "可以", "自己", "对方", "场景", "时候", "一种"))
_CACHE_BYTES = 32 * 1024 * 1024
_CACHE_ITEMS = 16
_DECODE_PIXELS = 32_000_000


@dataclass
class StickerConfig:
    enabled: bool = False
    catalog: str = "data/sticker_review/catalog.json"
    search_limit: int = 6
    target_turns_min: int = 3
    target_turns_max: int = 5
    recent_window: int = 20
    max_image_bytes: int = 8 * 1024 * 1024
    max_age_seconds: float = 20

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("stickers.enabled must be a boolean")
        if not isinstance(self.catalog, str) or not self.catalog.strip() or "\0" in self.catalog:
            raise ValueError("stickers.catalog must be a nonempty path")
        for name, low, high in (("search_limit", 1, 20), ("target_turns_min", 1, 100),
                                ("target_turns_max", 1, 100), ("recent_window", 1, 100),
                                ("max_image_bytes", 1024, 32 * 1024 * 1024)):
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ValueError(f"stickers.{name} must be an integer in {low}..{high}")
        if self.target_turns_max < self.target_turns_min:
            raise ValueError("stickers.target_turns_max must be >= target_turns_min")
        if (type(self.max_age_seconds) not in (int, float) or not math.isfinite(self.max_age_seconds)
                or not 0 < self.max_age_seconds <= 60):
            raise ValueError("stickers.max_age_seconds must be in (0, 60]")


def _terms(text, *, unigrams=False):
    terms = set()
    for word in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if word.isascii() or len(word) <= 3:
            terms.add(word)
        if not word.isascii():
            for size in (2, 3):
                terms.update(word[i:i + size] for i in range(len(word) - size + 1))
            if unigrams:
                terms.update(word)
    return terms - _STOP


def _public(row):
    return deepcopy({key: row[key] for key in _PUBLIC})


class StickerLibrary:
    """Shared immutable catalog; recent-use preferences belong to each caller's group."""

    def __init__(self, root: Path, config: StickerConfig):
        config.validate()
        self.config = config
        self.root = Path(root).resolve()
        self.catalog = (self.root / config.catalog).resolve()
        self._items = {}
        self._index = {}
        self._df = Counter()
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._cache_lock = threading.Lock()
        if not config.enabled:
            return
        try:
            with self.catalog.open("rb") as file:
                raw = file.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("catalog too large")
            data = json.loads(raw)
            rows = data["items"]
            if not isinstance(rows, list) or len(rows) > 5000:
                raise ValueError("invalid catalog items")
            seen = set()
            reviewed = 0
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"]):
                    raise ValueError("invalid sticker identifier")
                if row["id"] in seen:
                    raise ValueError("duplicate sticker identifier")
                seen.add(row["id"])
                if row.get("needs_review", False) is not False:
                    reviewed += 1
                    continue
                for key in ("title", "description", "animation_summary"):
                    if not isinstance(row.get(key), str) or len(row[key]) > 1000:
                        raise ValueError("invalid sticker description")
                if not row["title"].strip() or not row["description"].strip():
                    raise ValueError("empty sticker description")
                for key in ("visible_text", "emotions", "usage", "avoid"):
                    values = row.get(key)
                    if (not isinstance(values, list) or len(values) > 20
                            or any(not isinstance(v, str) or len(v) > 1000 for v in values)):
                        raise ValueError("invalid sticker keywords")
                if type(row.get("intensity")) is not int or not 1 <= row["intensity"] <= 3:
                    raise ValueError("invalid sticker intensity")
                if (not isinstance(row.get("file"), str) or not row["file"] or "\0" in row["file"]
                        or not isinstance(row.get("sha256"), str) or not _SHA.fullmatch(row["sha256"])):
                    raise ValueError("invalid sticker file record")
                self._items[row["id"]] = deepcopy(row)
                weights = Counter()
                for key, weight in _FIELDS.items():
                    value = row[key] if isinstance(row[key], str) else " ".join(row[key])
                    for term in _terms(value, unigrams=True):
                        weights[term] += weight
                self._index[row["id"]] = weights
                self._df.update(weights.keys())
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise ValueError("Cannot load a valid sticker catalog") from None
        log.info("[表情库加载] 可用=%d 待复核排除=%d", len(self), reviewed)

    def __len__(self):
        return len(self._items)

    def get(self, sticker_id):
        if not isinstance(sticker_id, str) or sticker_id not in self._items:
            raise ToolError("sticker_not_found", "表情编号不可用，或该素材尚待人工复核。")
        return _public(self._items[sticker_id])

    def search(self, query, limit=6, recent_ids=()):
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ToolError("invalid_sticker_query", "请提供不超过 500 字的表情含义、情绪或配字。")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ToolError("invalid_sticker_limit", "表情检索条数应为 1 到 20 的整数。")
        terms = _terms(query)
        if not terms:
            return []
        recent = set(recent_ids) if isinstance(recent_ids, (tuple, list, set, frozenset)) else set()
        scored = []
        for identity, weights in self._index.items():
            score = sum(weights[term] * (1 + math.log((len(self) + 1) / (self._df[term] + 1)))
                        for term in terms if term in weights)
            if score > 0:
                scored.append((score * (0.3 if identity in recent else 1), identity))
        scored.sort(key=lambda value: (-value[0], value[1]))
        return [self.get(identity) for _, identity in scored[:limit]]

    def _read_original(self, row):
        try:
            relative = Path(row["file"])
            path = (self.catalog.parent / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(self.catalog.parent) or not path.is_file():
                raise ToolError("sticker_invalid_path", "表情文件位置不符合素材库约束。")
            with path.open("rb") as file:
                raw = file.read(self.config.max_image_bytes + 1)
            if len(raw) > self.config.max_image_bytes:
                raise ToolError("sticker_too_large", "表情文件超过发送大小上限。")
            if hashlib.sha256(raw).hexdigest() != row["sha256"]:
                raise ToolError("sticker_changed", "表情文件与已审核记录不一致，请重新检查素材。")
            return raw
        except (OSError, ValueError):
            raise ToolError("sticker_file_unavailable", "表情文件不可读取。") from None

    @staticmethod
    def _encode(raw):
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "GIF", "WEBP"}:
                    raise ToolError("sticker_invalid_image", "表情文件格式不支持。")
                count = getattr(source, "n_frames", 1)
                if count > 1000 or source.width * source.height * count > _DECODE_PIXELS:
                    raise ToolError("sticker_too_large", "表情像素或动画总帧数超过处理上限。")
                original_format = source.format
                loop = source.info.get("loop", 0)
                frames, durations = [], []
                elapsed, encoded_elapsed = 0, 0
                for index in range(count):
                    source.seek(index)
                    source.load()
                    if original_format == "WEBP" and count > 1:
                        frame = source.convert("RGBA")
                        palette = frame.convert("RGB").quantize(colors=255)
                        # Reserve palette slot 255 for transparent pixels. GIF has
                        # binary alpha and 10 ms timing resolution, unlike WebP.
                        colors = palette.getpalette()
                        colors += [0] * (768 - len(colors))
                        palette.putpalette(colors)
                        palette.paste(255, mask=frame.getchannel("A").point(lambda alpha: 255 if alpha < 128 else 0))
                        palette.info["transparency"] = 255
                        frames.append(palette)
                        elapsed += max(1, int(source.info.get("duration", 100)))
                        duration = max(10, round(elapsed / 10) * 10 - encoded_elapsed)
                        durations.append(duration)
                        encoded_elapsed += duration
                if original_format == "WEBP":
                    output = BytesIO()
                    if count > 1:
                        # GIF frame cropping compares RGB colors. Use one
                        # transparent color absent from every frame, so opaque
                        # pixels cannot look identical to the cleared background.
                        used = set()
                        for frame in frames:
                            colors = frame.getpalette()
                            used.update(tuple(colors[i:i + 3]) for i in range(0, 765, 3))
                        transparent = next((value >> 16, (value >> 8) & 255, value & 255)
                                           for value in range(len(used) + 1)
                                           if (value >> 16, (value >> 8) & 255, value & 255) not in used)
                        for frame in frames:
                            colors = frame.getpalette()
                            colors[765:768] = transparent
                            frame.putpalette(colors)
                        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                                       duration=durations, loop=loop, disposal=2, transparency=255, optimize=False)
                        output_format = "GIF"
                    else:
                        ImageOps.exif_transpose(source).save(output, format="PNG")
                        output_format = "PNG"
                    encoded = output.getvalue()
                else:
                    encoded, output_format = raw, original_format
                return encoded, {"format": output_format, "animated": count > 1, "frames": count}

    def prepare(self, sticker_id):
        metadata = self.get(sticker_id)
        row = self._items[sticker_id]
        raw = self._read_original(row)  # Recheck integrity even for a cached conversion.
        digest = row["sha256"]
        with self._cache_lock:
            cached = self._cache.get(digest)
            if cached:
                self._cache.move_to_end(digest)
        if cached is None:
            try:
                cached = self._encode(raw)
            except ToolError:
                raise
            except (UnidentifiedImageError, OSError, ValueError, EOFError,
                    Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise ToolError("sticker_invalid_image", "表情文件无法完整解码。") from None
            if len(cached[0]) > self.config.max_image_bytes:
                raise ToolError("sticker_too_large", "表情转换后超过发送大小上限。")
            if len(cached[0]) <= _CACHE_BYTES:
                with self._cache_lock:
                    if digest not in self._cache:
                        self._cache[digest] = cached
                        self._cache_bytes += len(cached[0])
                    while len(self._cache) > _CACHE_ITEMS or self._cache_bytes > _CACHE_BYTES:
                        _, previous = self._cache.popitem(last=False)
                        self._cache_bytes -= len(previous[0])
        body, media = cached
        log.info("[表情发送准备] 编号=%s 格式=%s 动图=%s 字节=%d",
                 sticker_id, media["format"], media["animated"], len(body))
        return {"type": "image", "data": {"file": "base64://" + base64.b64encode(body).decode("ascii")}}, {
            **metadata, **media, "sha256": digest}


def register_stickers(registry, library, config):
    async def search(context, args):
        state = getattr(context, "sticker_state", None) or {}
        recent = state.get("recent_sticker_ids", [])
        items = library.search(args["query"], args.get("limit", config.search_limit), recent_ids=recent)
        return ToolResult(True, data={"items": items}, meta={"read_only": True})

    registry.register(ToolSpec("search_stickers",
        "为本轮纯表情或图文回应挑选候选，不必等群友要求发图。用简短的含义、情绪、动作或配字检索本地已审核表情库，"
        "返回可选的表情 ID、画面描述、用途和误用提醒；近期用过的图片会降权。"
        "只检索，不发送。无合适结果就使用文字，不编造 ID。",
        {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
         "required": ["query"], "additionalProperties": False}, search))
