"""Public Bilibili source retention, separate from disposable group observations.

An unexpired source is never evicted to admit another video.  The directory must
be dedicated to this cache; chat history and group document IDs do not belong in
it.  Locks cover acquisition and analysis at the caller, not background tasks.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .documents import Document, DocumentStore, build_document
from .tools import ToolError

log = logging.getLogger("atri.documents")
_NAMESPACE = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"doc_[0-9a-f]{32}\Z")


@dataclass
class _LockEntry:
    lock: threading.Lock
    users: int = 0


_FLIGHTS: dict[tuple[str, str], _LockEntry] = {}
_FLIGHTS_GUARD = threading.Lock()


def _key(self_id, url):
    if not isinstance(self_id, str) or not self_id or len(self_id) > 128:
        raise ToolError("invalid_video_cache_key", "视频缓存的机器人标识无效。")
    try:
        if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port is not None or parsed.netloc.lower() != parsed.hostname
                or parsed.hostname not in ("www.bilibili.com", "bilibili.com", "m.bilibili.com")):
            raise ValueError()
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", parsed.path)
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=40)
        pages = query.get("p", ["1"])
        if match is None or len(pages) != 1 or not re.fullmatch(r"[1-9][0-9]{0,3}", pages[0]):
            raise ValueError()
        canonical = "https://www.bilibili.com/video/" + match[1]
        if pages[0] != "1":
            canonical += "?p=" + pages[0]
    except (ValueError, TypeError):
        raise ToolError("invalid_video_cache_key", "视频缓存需要规范的 B站 BV 视频和分 P 编号。") from None
    digest = hashlib.sha256((self_id + "\0" + canonical).encode("utf-8")).hexdigest()
    return canonical, digest


class _RetentionStore(DocumentStore):
    """Reuse the document codec and atomic writes without its LRU eviction."""

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
                # This directory is reserved for public video sources.  Never
                # remove other document kinds even if it was misconfigured.
                if document is not None and document.scope[1] != "video-source":
                    continue
                if document is None or not 0 <= now - document.created_at < self.ttl_seconds:
                    if path.is_file() or path.is_symlink():
                        path.unlink(missing_ok=True)
                    continue
                records.append((document, path))
        records.sort(key=lambda item: (item[0].created_at, item[0].id), reverse=True)
        return records

    def _put(self, document):
        with self._lock:
            records = self._scan()
            existing = [(item, path) for item, path in records
                        if item.scope == document.scope and item.url == document.url]
            if existing:
                original = min((item for item, _ in existing), key=lambda item: item.created_at)
                # A different chunk setting can rebuild the same source, but
                # neither saving its overview nor re-reading may extend expiry.
                if any(getattr(original, key) != getattr(document, key) for key in
                       ("text", "title", "source", "sections", "partial", "truncated", "warning", "segments")):
                    raise ToolError("video_cache_conflict", "该视频已有未过期的来源记录，本次未覆盖原文。")
                document.created_at = original.created_at
            if not 0 <= time.time() - document.created_at < self.ttl_seconds:
                raise ToolError("video_cache_expired", "视频来源已过期，本次未延长旧资料的有效期。")
            replacing = {path for _, path in existing}
            remaining = [(item, path) for item, path in records if path not in replacing]
            if (len(remaining) >= self.max_documents
                    or sum(len(item.text) for item, _ in remaining) + len(document.text) > self.max_total_chars):
                raise ToolError("video_cache_full", "视频缓存容量已满；未删除24小时内的来源，请增加容量或等待过期。")
            super()._put(document)
            # New chunk versions replace only this video's old representation,
            # after the new record has been atomically persisted.
            for item, path in existing:
                if item.id != document.id:
                    path.unlink(missing_ok=True)

    def _find(self, scope, url):
        with self._lock:
            records = self._scan()
            found = next((item for item, _ in records if item.scope == scope and item.url == url), None)
            if found is None and (len(records) >= self.max_documents
                                  or sum(len(item.text) for item, _ in records) >= self.max_total_chars):
                # Refuse before another download/ASR when the cache is already
                # full.  Admission also checks exact size once text is acquired.
                raise ToolError("video_cache_full", "视频缓存容量已满；未删除24小时内的来源，请增加容量或等待过期。")
            return found

    def _check_capacity(self, max_chars):
        with self._lock:
            records = self._scan()
            if (len(records) >= self.max_documents
                    or sum(len(item.text) for item, _ in records) + max_chars > self.max_total_chars):
                raise ToolError("video_cache_full", "视频缓存没有足够容量保存新来源；未删除24小时内的资料，本次未开始读取。")


class VideoSourceCache:
    """24-hour bot-scoped source archive, shared safely between its groups."""

    def __init__(self, directory: Path, *, ttl_seconds=86400, max_documents=256,
                 max_total_chars=8_000_000):
        self.directory = Path(directory).resolve()
        self.ttl_seconds = ttl_seconds
        self.store = _RetentionStore(self.directory, ttl_seconds=ttl_seconds,
            max_documents_per_scope=max_documents, max_documents=max_documents,
            max_total_chars=max_total_chars)

    @asynccontextmanager
    async def lock(self, self_id, url):
        """Serialize a video across cache instances, with cancellable waiting.

        A threading lock also covers distinct asyncio loops in one process.
        Nonblocking polling avoids an abandoned executor acquisition on timeout.
        No model request slot should be held while waiting for this lock.
        """
        _, digest = _key(self_id, url)
        key = (str(self.directory), digest)
        with _FLIGHTS_GUARD:
            entry = _FLIGHTS.setdefault(key, _LockEntry(threading.Lock()))
            entry.users += 1
        acquired = False
        try:
            while not entry.lock.acquire(blocking=False):
                await asyncio.sleep(0.05)
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with _FLIGHTS_GUARD:
                entry.users -= 1
                if entry.users == 0:
                    _FLIGHTS.pop(key, None)

    async def get(self, self_id, url):
        canonical, _ = _key(self_id, url)
        value = await self.store.find((self_id, "video-source"), canonical)
        if value is not None:
            log.debug("[视频来源缓存命中] 来源=%s 字符数=%d 已有概览=%s", value.source,
                      len(value.text), value.analysis is not None)
        return value

    async def check_capacity(self, max_chars: int) -> None:
        """Conservative preflight before spending network/ASR work on a miss.

        This is not a reservation: concurrent different videos are rechecked at
        commit.  Call with the maximum text length that acquisition may retain.
        """
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError("Maximum video source size must be a positive integer")
        await self.store._run(self.store._check_capacity, max_chars)

    async def put(self, self_id, url, document):
        canonical, _ = _key(self_id, url)
        if not isinstance(document, Document) or document.scope[0] != self_id:
            raise ToolError("invalid_video_cache_scope", "视频来源与当前机器人的缓存作用域不一致。")
        document_url, _ = _key(self_id, document.url)
        if canonical != document_url:
            raise ToolError("invalid_video_cache_key", "视频来源与缓存的视频或分 P 编号不一致。")
        record = build_document(scope=(self_id, "video-source"), url=canonical,
            title=document.title, text=document.text, source=document.source, sections=document.sections,
            partial=document.partial, truncated=document.truncated, warning=document.warning,
            segments=document.segments, chunk_chars=document.chunk_chars, now=document.created_at)
        record.analysis = deepcopy(document.analysis)
        await self.store.put(record)
