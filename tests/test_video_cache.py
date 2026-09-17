import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atri_bot.documents import build_document
from atri_bot.tools import ToolError
from atri_bot.video_cache import VideoSourceCache


URL = "https://www.bilibili.com/video/BV1dVRdBpEze"
OTHER = "https://www.bilibili.com/video/BV1dVRdBpEzf"


def source(text="已取得的转写正文。", *, bot="99", group="100", url=URL, now=None, **changes):
    values = dict(scope=(bot, group), url=url, title="公开的视频", text=text,
                  source="bailian_asr", now=now, sections=(("asr_transcript", 0, len(text)),))
    values.update(changes)
    return build_document(**values)


class VideoCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "video-sources"
        self.cache = VideoSourceCache(self.directory)

    async def test_raw_and_analysis_survive_restart_without_extending_retention(self):
        with patch("atri_bot.video_cache.time.time", return_value=100000):
            original = source(segments=[{"text": "已取得的转写正文。", "begin_time": 12, "end_time": 2500}])
            await self.cache.put("99", URL, original)
        with patch("atri_bot.video_cache.time.time", return_value=100100):
            restored = await VideoSourceCache(self.directory).get("99", URL)
            self.assertEqual(restored.scope, ("99", "video-source"))
            self.assertNotEqual(restored.id, original.id)
            self.assertEqual(restored.text, original.text)
            self.assertEqual(restored.segments, original.segments)
            self.assertEqual(restored.chunks[0].begin_ms, 12)
            restored.analysis = {"summary": "中立概览", "covered_chunk_ids": ["c0001"]}
            restored.created_at = 100100
            await self.cache.put("99", URL, restored)
            self.assertEqual((await self.cache.get("99", URL)).created_at, 100000)
        with patch("atri_bot.video_cache.time.time", return_value=186399.99):
            cached = await VideoSourceCache(self.directory).get("99", URL)
            self.assertEqual(cached.analysis, restored.analysis)
        with patch("atri_bot.video_cache.time.time", return_value=186400):
            self.assertIsNone(await self.cache.get("99", URL))
            self.assertEqual(list(self.directory.glob("*/*.json")), [])

    async def test_shares_same_public_video_between_groups_but_not_bots_or_parts(self):
        await self.cache.put("99", URL + "/?p=1&share_source=qq", source())
        self.assertEqual((await self.cache.get("99", "https://m.bilibili.com/video/BV1dVRdBpEze/")).text,
                         source().text)
        self.assertIsNone(await self.cache.get("98", URL))
        self.assertIsNone(await self.cache.get("99", URL + "?p=2"))
        second = source("第二 P 的独立文本", group="101", url=URL + "?p=2")
        await self.cache.put("99", second.url, second)
        self.assertEqual((await self.cache.get("99", second.url)).text, second.text)
        await self.cache.put("98", URL, source("另一个机器人的来源", bot="98"))
        self.assertNotEqual((await self.cache.get("99", URL)).text, (await self.cache.get("98", URL)).text)

    async def test_same_video_concurrent_instances_only_acquire_once(self):
        other_instance = VideoSourceCache(self.directory)
        acquisitions = 0

        async def read(cache, url):
            nonlocal acquisitions
            async with cache.lock("99", url):
                document = await cache.get("99", url)
                if document is None:
                    acquisitions += 1
                    await asyncio.sleep(0.02)
                    await cache.put("99", url, source())
                    document = await cache.get("99", url)
                return document.text

        results = await asyncio.gather(read(self.cache, URL), read(other_instance, URL + "/?p=1"),
                                       read(self.cache, URL))
        self.assertEqual(acquisitions, 1)
        self.assertEqual(results, [source().text] * 3)

    async def test_cancelled_waiter_does_not_leave_lock_or_work_behind(self):
        attempted = asyncio.Event()
        entered = False

        async def waiter():
            nonlocal entered
            attempted.set()
            async with VideoSourceCache(self.directory).lock("99", URL):
                entered = True

        async with self.cache.lock("99", URL):
            waiting = asyncio.create_task(waiter())
            await attempted.wait()
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
        async with asyncio.timeout(0.2):
            async with self.cache.lock("99", URL):
                self.assertFalse(entered)

    async def test_distinct_parts_and_bots_do_not_block_each_other(self):
        async with self.cache.lock("99", URL):
            async with asyncio.timeout(0.2):
                async with self.cache.lock("99", URL + "?p=2"):
                    async with self.cache.lock("98", URL):
                        pass

    async def test_capacity_rejects_new_record_without_evicting_unexpired(self):
        cache = VideoSourceCache(self.directory, max_documents=1)
        await cache.put("99", URL, source())
        for operation in (cache.check_capacity(1), cache.get("99", OTHER),
                          cache.put("99", OTHER, source(url=OTHER))):
            with self.assertRaises(ToolError) as raised:
                await operation
            self.assertEqual(raised.exception.code, "video_cache_full")
        self.assertIsNotNone(await cache.get("99", URL))
        # A lower configured capacity on restart still cannot evict old records.
        reconfigured = VideoSourceCache(self.directory, max_total_chars=1)
        self.assertIsNotNone(await reconfigured.get("99", URL))
        self.assertEqual(len(list(self.directory.glob("*/*.json"))), 1)

    async def test_size_preflight_and_commit_check_and_expired_capacity_reuse(self):
        cache = VideoSourceCache(self.directory, max_total_chars=10, max_documents=1, ttl_seconds=10)
        with patch("atri_bot.video_cache.time.time", return_value=1000):
            await cache.check_capacity(10)
            await cache.put("99", URL, source("12345"))
            with self.assertRaises(ToolError) as raised:
                await cache.check_capacity(6)
            self.assertEqual(raised.exception.code, "video_cache_full")
        with patch("atri_bot.video_cache.time.time", return_value=1010):
            await cache.check_capacity(10)
            await cache.put("99", OTHER, source("1234567890", url=OTHER))
            self.assertEqual((await cache.get("99", OTHER)).text, "1234567890")
        room = VideoSourceCache(Path(self.temporary.name) / "small", max_total_chars=4)
        with self.assertRaises(ToolError) as raised:
            await room.put("99", URL, source("12345"))
        self.assertEqual(raised.exception.code, "video_cache_full")
        self.assertFalse(list(room.directory.glob("*/*.json")))

    async def test_overview_and_chunk_update_preserve_first_acquisition(self):
        with patch("atri_bot.video_cache.time.time", return_value=1000):
            await self.cache.put("99", URL, source("原文。" * 150))
        with patch("atri_bot.video_cache.time.time", return_value=2000):
            updated = source("原文。" * 150, chunk_chars=200)
            updated.analysis = {"summary": "按新分块整理"}
            await self.cache.put("99", URL, updated)
            cached = await self.cache.get("99", URL)
            self.assertEqual(cached.created_at, 1000)
            self.assertEqual(cached.chunk_chars, 200)
            self.assertEqual(cached.analysis, updated.analysis)
            self.assertEqual(len(list(self.directory.glob("*/*.json"))), 1)
            with self.assertRaises(ToolError) as raised:
                await self.cache.put("99", URL, source("不能无意覆盖已取得的正文"))
            self.assertEqual(raised.exception.code, "video_cache_conflict")
            self.assertEqual((await self.cache.get("99", URL)).text, updated.text)

    async def test_refuses_expired_source_without_resurrecting_it(self):
        with patch("atri_bot.video_cache.time.time", return_value=100000):
            with self.assertRaises(ToolError) as raised:
                await self.cache.put("99", URL, source(now=0))
            self.assertEqual(raised.exception.code, "video_cache_expired")

    async def test_corrupt_record_is_a_miss_and_unrelated_files_are_preserved(self):
        await self.cache.put("99", URL, source())
        path = next(self.directory.glob("*/*.json"))
        path.write_text('{"untrusted": "invalid document"}', encoding="utf-8")
        archive = self.directory / "transcript.txt"
        archive.write_text("keep user archives", encoding="utf-8")
        self.assertIsNone(await self.cache.get("99", URL))
        self.assertEqual(archive.read_text(encoding="utf-8"), "keep user archives")

    async def test_invalid_key_scope_and_symlink_cannot_escape_cache(self):
        for url in ("../../secret", URL + "?p=0", URL + "?p=1&p=2", "https://b23.tv/xyz",
                    "https://www.bilibili.com@localhost/video/BV1dVRdBpEze"):
            with self.subTest(url=url), self.assertRaises(ToolError):
                await self.cache.get("99", url)
        for item in (source(bot="98"), source(url=URL + "?p=2")):
            with self.assertRaises(ToolError):
                await self.cache.put("99", URL, item)
        await self.cache.put("99", URL, source())
        record = next(self.directory.glob("*/*.json"))
        outside = Path(self.temporary.name) / "private.txt"
        outside.write_text("private", encoding="utf-8")
        record.unlink()
        record.symlink_to(outside)
        self.assertIsNone(await self.cache.get("99", URL))
        self.assertEqual(outside.read_text(encoding="utf-8"), "private")

    async def test_invalid_limits_rejected(self):
        for limits in ({"ttl_seconds": 0}, {"max_documents": 0}, {"max_total_chars": -1}):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                VideoSourceCache(self.directory, **limits)
        for value in (False, 0, -1, "10"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await self.cache.check_capacity(value)


if __name__ == "__main__":
    unittest.main()
