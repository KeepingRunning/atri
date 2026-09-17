"""Lossless source chunks and bounded, persistent, bot/group-scoped documents."""
from __future__ import annotations

import asyncio
from bisect import bisect_right
from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .tools import ToolError

log = logging.getLogger("atri.documents")
_SCHEMA_VERSION = 1
_MAX_TEXT_CHARS = 2_000_000
_MAX_RECORD_BYTES = 32_000_000
_ID = re.compile(r"doc_[0-9a-f]{32}\Z")
_NAMESPACE = re.compile(r"[0-9a-f]{64}\Z")
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass
class DocumentConfig:
    enabled: bool = True
    chunk_chars: int = 1200
    overview_min_chars: int = 4000
    input_token_budget: int = 48000
    max_output_tokens: int = 4096
    timeout: float = 60
    model: str = ""
    max_model_calls: int = 8

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("documents.enabled must be a boolean")
        for name, low, high in (("chunk_chars", 200, 8000),
                                ("overview_min_chars", 0, _MAX_TEXT_CHARS),
                                ("input_token_budget", 4000, 512000),
                                ("max_output_tokens", 256, 16000),
                                ("max_model_calls", 1, 32)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"documents.{name} must be an integer in {low}..{high}")
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 120):
            raise ValueError("documents.timeout must be in (0, 120]")
        if not isinstance(self.model, str) or len(self.model) > 256 or any(ord(c) < 32 for c in self.model):
            raise ValueError("documents.model must be a model name of at most 256 characters")


@dataclass(frozen=True)
class Chunk:
    id: str
    start: int
    end: int
    text: str
    heading: str = ""
    begin_ms: int | None = None
    end_ms: int | None = None
    hard_split: bool = False


@dataclass
class Document:
    id: str
    scope: tuple[str, str]
    url: str
    title: str
    text: str
    source: str
    sections: tuple[tuple[str, int, int], ...]
    partial: bool
    truncated: bool
    warning: str | None
    created_at: float
    digest: str
    chunks: tuple[Chunk, ...]
    segments: tuple[dict, ...] = ()
    analysis: dict | None = None
    chunk_chars: int = 1200


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _scope(value):
    if (not isinstance(value, (tuple, list)) or len(value) != 2
            or any(not isinstance(part, str) or not part or len(part) > 128 for part in value)):
        raise ValueError("Invalid document scope")
    return tuple(value)


def _namespace(scope):
    return hashlib.sha256(_json(_scope(scope)).encode("utf-8")).hexdigest()


def _structure(text):
    """Find Markdown headings and code spans without changing any source bytes."""
    headings, code = [], []
    offset, fence_start, fence_token = 0, None, ""
    lines = text.splitlines(keepends=True)
    previous = None
    for line in lines:
        fence = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence_start is not None:
            if (fence and fence.group(1)[0] == fence_token[0]
                    and len(fence.group(1)) >= len(fence_token)
                    and not line[fence.end():].strip()):
                code.append((fence_start, offset + len(line)))
                fence_start = None
        elif fence:
            fence_start, fence_token = offset, fence.group(1)
        else:
            heading = re.match(r" {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$", line)
            if heading:
                headings.append((offset, heading.group(1).strip()[:500]))
            elif previous is not None and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*(?:\r?\n)?", line):
                previous_offset, previous_line = previous
                if previous_line.strip() and not previous_line.lstrip().startswith(("#", "-", "*", ">")):
                    headings.append((previous_offset, previous_line.strip()[:500]))
        previous = (offset, line) if fence_start is None else None
        offset += len(line)
    if fence_start is not None:
        code.append((fence_start, len(text)))
    return headings, code


def _timestamp(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= 365 * 24 * 60 * 60 * 1000):
        return None
    return int(value)


def _time_ranges(text, segments):
    ranges, cursor = [], 0
    compact, positions = None, None
    for segment in segments:
        content = segment.get("text")
        begin = _timestamp(segment.get("begin_time", segment.get("begin_ms")))
        end = _timestamp(segment.get("end_time", segment.get("end_ms")))
        if not isinstance(content, str) or not content.strip():
            continue
        start = text.find(content, cursor)
        finish = start + len(content)
        if start == -1:
            # A renderer may add line breaks/spaces between ASR words. Only ignore
            # whitespace for alignment; never rewrite words or invent timestamps.
            if compact is None:
                positions = [i for i, char in enumerate(text) if not char.isspace()]
                compact = "".join(text[i] for i in positions)
            needle = "".join(content.split())
            index = compact.find(needle, bisect_right(positions, cursor - 1))
            if index == -1:
                continue
            start, finish = positions[index], positions[index + len(needle) - 1] + 1
        cursor = finish
        if begin is not None and end is not None and end >= begin:
            ranges.append((start, finish, begin, end))
    clock = r"(\d{1,3}):([0-5]\d):([0-5]\d)(?:[.,](\d{1,3}))?"
    pattern = re.compile(r"(?m)^[ \t]*\[" + clock + r"\s*-->\s*" + clock + r"\][^\n]*(?:\n|$)")
    for match in pattern.finditer(text):
        def milliseconds(values):
            hours, minutes, seconds, fraction = values
            return ((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + int((fraction or "0").ljust(3, "0"))
        begin, end = milliseconds(match.groups()[:4]), milliseconds(match.groups()[4:])
        if begin <= end:
            ranges.append((match.start(), match.end(), begin, end))
    return ranges


def _chunks(text, size, segments):
    headings, code = _structure(text)
    code_starts = [span[0] for span in code]

    def in_code(offset):
        index = bisect_right(code_starts, offset) - 1
        return index >= 0 and code[index][0] < offset < code[index][1]

    boundaries = []
    for pattern in (r"\n[ \t]*\n+", r"[。！？!?][”’\"'）)\]]*[ \t]*(?:\n)?|\.(?=\s|$)\s*",
                    r"\n", r"[ \t]+"):
        boundaries.append([m.end() for m in re.finditer(pattern, text) if not in_code(m.end())])
    boundaries[0] = sorted(set(boundaries[0]) | {b for _, b in code})
    ranges = _time_ranges(text, segments)
    regions = sorted({0, len(text), *(start for start, _ in headings)})
    heading_positions = [start for start, _ in headings]
    chunks = []
    for region_start, region_end in zip(regions, regions[1:]):
        start, previous_hard = region_start, False
        while start < region_end:
            limit = min(start + size, region_end)
            end, hard = limit, False
            if limit < region_end:
                floor = start + max(1, size // 2)
                candidates = []
                for category in boundaries:
                    index = bisect_right(category, limit) - 1
                    candidates.append(category[index] if index >= 0 and category[index] >= floor else None)
                end = next((candidate for candidate in candidates if candidate is not None), None)
                if end is None:
                    # Keep a fitting fenced block intact even when it starts near
                    # the beginning of the current chunk.
                    end = next((a for a, b in code if start < a <= limit < b and b - a <= size), None)
                if end is None:
                    end, hard = limit, True
            index = bisect_right(heading_positions, start) - 1
            heading = headings[index][1] if index >= 0 else ""
            times = [r for r in ranges if r[0] < end and r[1] > start]
            chunks.append(Chunk(f"c{len(chunks) + 1:04d}", start, end, text[start:end], heading,
                                min((r[2] for r in times), default=None),
                                max((r[3] for r in times), default=None), hard or previous_hard))
            previous_hard, start = hard, end
    return tuple(chunks)


def _version(document):
    return {"schema": _SCHEMA_VERSION, "scope": document.scope, "url": document.url,
            "title": document.title, "text": document.text, "source": document.source,
            "sections": document.sections, "partial": document.partial,
            "truncated": document.truncated, "warning": document.warning,
            "segments": document.segments, "chunk_chars": document.chunk_chars,
            "chunks": [asdict(chunk) for chunk in document.chunks]}


def build_document(*, scope, url, title, text, source, sections=(), partial=False,
                   truncated=False, warning=None, segments=None, chunk_chars=1200, now=None):
    """Build a stable content version. Offsets are Python Unicode character offsets."""
    scope = _scope(scope)
    for value, limit in ((url, 8192), (title, 4096), (source, 128), (text, _MAX_TEXT_CHARS)):
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("Invalid document content")
    if not text or not source or type(partial) is not bool or type(truncated) is not bool:
        raise ValueError("Invalid document content")
    if warning is not None and (not isinstance(warning, str) or len(warning) > 4096):
        raise ValueError("Invalid document warning")
    if type(chunk_chars) is not int or not 200 <= chunk_chars <= 8000:
        raise ValueError("Invalid document chunk size")
    sections = tuple(tuple(section) for section in sections)
    if len(sections) > 10000:
        raise ValueError("Invalid document sections")
    for section in sections:
        if (len(section) != 3 or not isinstance(section[0], str) or not 1 <= len(section[0]) <= 128
                or any(type(offset) is not int for offset in section[1:])
                or not 0 <= section[1] < section[2] <= len(text)):
            raise ValueError("Invalid document section offsets")
    if segments is None:
        segments = ()
    if (not isinstance(segments, (tuple, list)) or len(segments) > 100000
            or any(not isinstance(segment, dict) for segment in segments)):
        raise ValueError("Invalid document segments")
    segments = tuple(deepcopy(segments))
    if len(_json(segments).encode("utf-8")) > _MAX_RECORD_BYTES // 2:
        raise ValueError("Document segments too large")
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError("Invalid document time")
    document = Document("", scope, url, title, text, source, sections, partial, truncated, warning,
                        float(now), "", _chunks(text, chunk_chars, segments), segments,
                        chunk_chars=chunk_chars)
    document.digest = hashlib.sha256(_json(_version(document)).encode("utf-8")).hexdigest()
    document.id = "doc_" + document.digest[:32]
    return document


class DocumentStore:
    """Single JSON per version; no separate index that can become stale on restart."""

    def __init__(self, directory: Path, *, ttl_seconds=86400, max_documents_per_scope=32,
                 max_documents=256, max_total_chars=8000000):
        if (type(ttl_seconds) not in (int, float) or not math.isfinite(ttl_seconds)
                or not 0 < ttl_seconds <= 365 * 86400):
            raise ValueError("Invalid document store TTL")
        for value in (max_documents_per_scope, max_documents, max_total_chars):
            if type(value) is not int or value <= 0:
                raise ValueError("Document store capacities must be positive integers")
        self.directory = Path(directory).resolve()
        self.ttl_seconds = ttl_seconds
        self.max_documents_per_scope = max_documents_per_scope
        self.max_documents = max_documents
        self.max_total_chars = max_total_chars
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.directory), threading.RLock())

    async def _run(self, method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError):
            log.warning("[文档存储失败] 操作=%s", method.__name__.lstrip("_"))
            raise ToolError("document_storage_failed", "文档存储失败，本次未能保存或读取资料。") from None

    async def put(self, document):
        await self._run(self._put, document)

    async def get(self, scope, id):
        if not isinstance(id, str) or not _ID.fullmatch(id):
            return None
        return await self._run(self._get, scope, id)

    async def find(self, scope, url):
        return await self._run(self._find, scope, url)

    @staticmethod
    def _encode(document):
        record = {"schema": _SCHEMA_VERSION, "document": asdict(document)}
        record["checksum"] = hashlib.sha256(_json(record).encode("utf-8")).hexdigest()
        data = _json(record).encode("utf-8")
        if len(data) > _MAX_RECORD_BYTES:
            raise ValueError("Document record too large")
        return data

    @staticmethod
    def _decode(data, namespace, document_id):
        def reject_constant(value):
            raise ValueError("Non-finite JSON value")

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Duplicate JSON key")
                value[key] = item
            return value

        record = json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_object)
        if not isinstance(record, dict) or set(record) != {"schema", "document", "checksum"}:
            raise ValueError("Invalid document record")
        checksum = record.pop("checksum")
        if (type(record["schema"]) is not int or record["schema"] != _SCHEMA_VERSION
                or not isinstance(checksum, str)
                or hashlib.sha256(_json(record).encode("utf-8")).hexdigest() != checksum):
            raise ValueError("Invalid document checksum")
        value = record["document"]
        if not isinstance(value, dict) or set(value) != set(Document.__dataclass_fields__):
            raise ValueError("Invalid document fields")
        if value["analysis"] is not None and not isinstance(value["analysis"], dict):
            raise ValueError("Invalid document analysis")
        document = build_document(scope=value["scope"], url=value["url"], title=value["title"],
            text=value["text"], source=value["source"], sections=value["sections"],
            partial=value["partial"], truncated=value["truncated"], warning=value["warning"],
            segments=value["segments"], chunk_chars=value["chunk_chars"], now=value["created_at"])
        if (document.id != document_id or document.id != value["id"]
                or document.digest != value["digest"] or _namespace(document.scope) != namespace
                or _json([asdict(chunk) for chunk in document.chunks]) != _json(value["chunks"])):
            raise ValueError("Document content does not match its version")
        document.analysis = value["analysis"]
        return document

    def _read(self, path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_RECORD_BYTES:
            return None
        try:
            with path.open("rb") as source:
                data = source.read(_MAX_RECORD_BYTES + 1)
            if len(data) > _MAX_RECORD_BYTES:
                return None
            return self._decode(data, path.parent.name, path.stem)
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
            log.warning("[文档记录忽略] 原因=invalid_record")
            return None

    def _scan(self):
        records = []
        if not self.directory.exists():
            return records
        now = time.time()
        for namespace in self.directory.iterdir():
            if namespace.is_symlink() or not _NAMESPACE.fullmatch(namespace.name) or not namespace.is_dir():
                continue
            for path in namespace.iterdir():
                if path.suffix != ".json" or not _ID.fullmatch(path.stem):
                    continue
                document = self._read(path)
                if (document is None or not 0 <= now - document.created_at < self.ttl_seconds
                        or len(document.text) > self.max_total_chars):
                    # Remove only the directory entry itself; never follow symlinks.
                    if path.is_file() or path.is_symlink():
                        path.unlink(missing_ok=True)
                    continue
                records.append((document, path))
        # Newest source versions win; access does not prolong expiry.
        records.sort(key=lambda item: (item[0].created_at, item[0].id), reverse=True)
        kept, scopes, total_chars = [], {}, 0
        for document, path in records:
            count = scopes.get(document.scope, 0)
            if (len(kept) >= self.max_documents or count >= self.max_documents_per_scope
                    or total_chars + len(document.text) > self.max_total_chars):
                path.unlink(missing_ok=True)
                continue
            kept.append((document, path))
            scopes[document.scope] = count + 1
            total_chars += len(document.text)
        return kept

    def _put(self, document):
        with self._lock:
            if not isinstance(document, Document) or len(document.text) > self.max_total_chars:
                raise ValueError("Invalid document")
            if not _ID.fullmatch(document.id):
                raise ValueError("Invalid document ID")
            namespace = _namespace(document.scope)
            encoded = self._encode(document)
            # Mutability supports adding analysis, but source edits must create a
            # fresh version through build_document rather than corrupting an ID.
            self._decode(encoded, namespace, document.id)
            folder = self.directory / namespace
            self.directory.mkdir(parents=True, exist_ok=True)
            if folder.is_symlink():
                raise ValueError("Invalid document namespace")
            folder.mkdir(exist_ok=True)
            target = folder / (document.id + ".json")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="wb", prefix=".document-", suffix=".tmp",
                                                 dir=folder, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(encoded)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._scan()
            log.debug("[文档已保存] 块数=%d 字符数=%d 已有概览=%s",
                      len(document.chunks), len(document.text), document.analysis is not None)

    def _get(self, scope, id):
        scope = _scope(scope)
        with self._lock:
            return next((document for document, _ in self._scan()
                         if document.id == id and document.scope == scope), None)

    def _find(self, scope, url):
        scope = _scope(scope)
        with self._lock:
            return next((document for document, _ in self._scan()
                         if document.url == url and document.scope == scope), None)
