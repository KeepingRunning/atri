import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atri_bot.documents import DocumentConfig, DocumentStore, build_document
from atri_bot.tools import ToolError


SCOPE = ("99", "100")
URL = "https://www.bilibili.com/video/BV1dVRdBpEze"


def document(text="原始正文。", **changes):
    arguments = {"scope": SCOPE, "url": URL, "title": "来源标题", "text": text, "source": "asr"}
    arguments.update(changes)
    return build_document(**arguments)


class DocumentTests(unittest.TestCase):
    def test_config_rejects_non_finite_bool_and_out_of_range(self):
        DocumentConfig().validate()
        for field, value in (("enabled", 1), ("chunk_chars", True), ("chunk_chars", 199),
                             ("overview_min_chars", -1), ("input_token_budget", 3000),
                             ("max_output_tokens", 16001), ("max_model_calls", 0),
                             ("timeout", float("nan")), ("timeout", float("inf")),
                             ("timeout", True), ("model", "model\nsecret")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                replace(DocumentConfig(), **{field: value}).validate()

    def test_lossless_unicode_headings_paragraphs_and_offsets(self):
        text = "# 首先\n\n" + "他并没有说线程无用。🙂\n\n" * 31 + "## 条件与反例\n\n" + "不满足条件时结论不成立。\n" * 42
        value = document(text, chunk_chars=200)
        self.assertEqual("".join(chunk.text for chunk in value.chunks), text)
        self.assertGreater(len(value.chunks), 3)
        offset = 0
        for index, chunk in enumerate(value.chunks):
            self.assertEqual(chunk.id, f"c{index + 1:04d}")
            self.assertEqual(chunk.start, offset)
            self.assertEqual(chunk.text, text[chunk.start:chunk.end])
            self.assertLessEqual(len(chunk.text), 200)
            self.assertFalse(chunk.hard_split)
            self.assertTrue(chunk.heading in ("首先", "条件与反例"))
            offset = chunk.end
        heading = next(chunk for chunk in value.chunks if chunk.text.startswith("## 条件与反例"))
        self.assertEqual(heading.heading, "条件与反例")
        self.assertEqual(offset, len(text))

    def test_setext_heading_and_fenced_code_preserved(self):
        code = "```python\n# This is code, not a heading\nif not ready:\n    return False\n```\n"
        text = "Overview\n========\n\n" + "说明。" * 47 + "\n\n" + code + "\n下一段。" * 40
        value = document(text, chunk_chars=200)
        self.assertEqual("".join(chunk.text for chunk in value.chunks), text)
        self.assertTrue(all(chunk.heading == "Overview" for chunk in value.chunks))
        self.assertTrue(any(code in chunk.text for chunk in value.chunks))

    def test_oversized_sentence_or_code_is_lossless_and_marked(self):
        for text in ("长" * 521, "```\n" + "🌸" * 480 + "\n```\n"):
            with self.subTest(text=text[:4]):
                value = document(text, chunk_chars=200)
                self.assertEqual("".join(chunk.text for chunk in value.chunks), text)
                self.assertTrue(all(len(chunk.text) <= 200 for chunk in value.chunks))
                self.assertTrue(all(chunk.hard_split for chunk in value.chunks))

    def test_content_version_scope_metadata_segments_and_config(self):
        value = document(now=100)
        self.assertEqual(value.id, document(now=200).id)
        self.assertEqual(value.digest, document(now=200).digest)
        for changes in ({"scope": ("99", "101")}, {"scope": ("98", "100")},
                        {"source": "subtitle"}, {"chunk_chars": 1400}, {"partial": True},
                        {"sections": (("asr", 0, 5),)},
                        {"segments": [{"text": "原始正文。", "begin_time": 0, "end_time": 1000}]}):
            with self.subTest(changes=changes):
                self.assertNotEqual(value.id, document(**changes).id)

    def test_asr_segments_match_with_source_times_and_retained_metadata(self):
        first, second = "第一段内容。" * 30, "第二段内容。" * 25
        segments = [{"text": first, "begin_time": 1000, "end_time": 4000, "speaker_id": 1},
                    {"text": second, "begin_time": 5000, "end_time": 8500, "speaker_id": 2},
                    {"text": "不存在的段落", "begin_time": 9000, "end_time": 12000}]
        value = document("# 第一段\n" + first + "\n\n# 第二段\n" + second, segments=segments, chunk_chars=200)
        self.assertEqual(value.segments, tuple(segments))
        segments[0]["speaker_id"] = 5
        self.assertEqual(value.segments[0]["speaker_id"], 1)
        self.assertEqual((value.chunks[0].begin_ms, value.chunks[0].end_ms), (1000, 4000))
        self.assertEqual((value.chunks[-1].begin_ms, value.chunks[-1].end_ms), (5000, 8500))
        self.assertTrue(all(chunk.end_ms != 12000 for chunk in value.chunks))

    def test_asr_whitespace_alignment_no_fabricated_unmatched_time(self):
        value = document("hello\nworld\n不同内容", segments=[
            {"text": "hello world", "begin_time": 100, "end_time": 200},
            {"text": "缺失内容", "begin_time": 300, "end_time": 400}])
        self.assertEqual((value.chunks[0].begin_ms, value.chunks[0].end_ms), (100, 200))
        value = document("无法匹配", segments=[{"text": "其他内容", "begin_time": 100, "end_time": 200}])
        self.assertIsNone(value.chunks[0].begin_ms)
        self.assertIsNone(value.chunks[0].end_ms)

    def test_embedded_subtitle_timestamps(self):
        text = "# 字幕\n[00:00:01.250 --> 00:00:03.5] 第一段字幕。\n[01:20:31 --> 01:20:35] 第二段字幕。"
        value = document(text, source="subtitle")
        self.assertEqual((value.chunks[0].begin_ms, value.chunks[0].end_ms), (1250, 4835000))
        self.assertEqual(value.text, text)

    def test_untimed_repeated_sentence_does_not_take_later_sentence_timestamp(self):
        text = "# 第一段\n重复的一句。\n# 第二段\n重复的一句。"
        value = document(text, segments=[{"text": "重复的一句。"},
            {"text": "重复的一句。", "begin_time": 100, "end_time": 200}])
        self.assertIsNone(value.chunks[0].begin_ms)
        self.assertEqual(value.chunks[1].begin_ms, 100)

    def test_invalid_offsets_timestamps_and_empty_documents(self):
        for changes in ({"sections": (("asr", 0, 999),)}, {"sections": (("asr", True, 2),)},
                        {"text": ""}, {"now": float("nan")}, {"scope": ("", "100")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                document(**changes)
        value = document(segments=[{"text": "原始正文。", "begin_time": -1, "end_time": 5}])
        self.assertIsNone(value.chunks[0].begin_ms)


class DocumentStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "documents"
        self.store = DocumentStore(self.directory)

    async def test_restart_reuses_raw_chunks_analysis_and_scope(self):
        value = document("原文与术语不能被概览覆盖。" * 40)
        value.analysis = {"overview": "这是概览", "chunks": [chunk.id for chunk in value.chunks]}
        await self.store.put(value)
        restarted = DocumentStore(self.directory)
        loaded = await restarted.get(SCOPE, value.id)
        self.assertEqual(loaded, value)
        self.assertEqual(await restarted.find(SCOPE, URL), value)
        self.assertIsNone(await restarted.get(("98", "100"), value.id))
        self.assertIsNone(await restarted.get(("99", "101"), value.id))
        self.assertIsNone(await restarted.find(("98", "100"), URL))
        loaded.analysis["overview"] = "调用方修改"
        self.assertEqual((await restarted.get(SCOPE, value.id)).analysis, value.analysis)

    async def test_ttl_is_persistent_and_reads_do_not_refresh(self):
        with patch("atri_bot.documents.time.time", return_value=100):
            value = document()
            await self.store.put(value)
        restarted = DocumentStore(self.directory, ttl_seconds=100)
        with patch("atri_bot.documents.time.time", return_value=199):
            self.assertEqual(await restarted.get(SCOPE, value.id), value)
        with patch("atri_bot.documents.time.time", return_value=200):
            self.assertIsNone(await restarted.get(SCOPE, value.id))
        self.assertEqual(list(self.directory.glob("*/*.json")), [])

    async def test_per_scope_and_global_capacity_enforced_after_restart(self):
        with patch("atri_bot.documents.time.time", return_value=100):
            a = document("第一份", now=90)
            b = document("第二份", now=91)
            c = document("另一群", scope=("99", "101"), now=92)
            for value in (a, b, c):
                await self.store.put(value)
            restarted = DocumentStore(self.directory, max_documents_per_scope=1)
            self.assertIsNone(await restarted.get(SCOPE, a.id))
            self.assertEqual(await restarted.get(SCOPE, b.id), b)
            self.assertEqual(await restarted.get(c.scope, c.id), c)
            restarted = DocumentStore(self.directory, max_documents=1)
            self.assertIsNone(await restarted.get(SCOPE, b.id))
            self.assertEqual(await restarted.get(c.scope, c.id), c)

    async def test_total_character_capacity_across_scopes(self):
        with patch("atri_bot.documents.time.time", return_value=100):
            older = document("旧" * 300, now=90)
            newer = document("新" * 300, now=91, scope=("99", "101"))
            await self.store.put(older)
            await self.store.put(newer)
            limited = DocumentStore(self.directory, max_total_chars=500)
            self.assertIsNone(await limited.get(SCOPE, older.id))
            self.assertEqual(await limited.get(newer.scope, newer.id), newer)
            with self.assertRaises(ToolError) as raised:
                await limited.put(document("大" * 501))
            self.assertEqual(raised.exception.code, "document_storage_failed")
            self.assertNotIn(str(self.directory), str(raised.exception))

    async def test_find_returns_latest_version(self):
        with patch("atri_bot.documents.time.time", return_value=100):
            older = document("旧内容", now=90)
            newer = document("修订后的内容", now=91)
            await self.store.put(older)
            await self.store.put(newer)
            self.assertEqual(await self.store.find(SCOPE, URL), newer)

    async def test_corruption_and_out_of_bounds_are_never_returned(self):
        for kind in ("body", "checksum", "offset", "bool_offset", "future", "analysis", "oversized"):
            with self.subTest(kind=kind):
                value = document()
                await self.store.put(value)
                path = next(self.directory.glob("*/*.json"))
                raw = json.loads(path.read_text())
                if kind == "body":
                    raw["document"]["text"] = "被篡改的来源"
                elif kind == "checksum":
                    raw["checksum"] = "bad"
                elif kind == "offset":
                    raw["document"]["chunks"][0]["end"] = 999999
                elif kind == "bool_offset":
                    raw["document"]["chunks"][0]["start"] = False
                elif kind == "future":
                    raw["document"]["created_at"] = 999999999999
                elif kind == "analysis":
                    raw["document"]["analysis"] = ["not a mapping"]
                elif kind == "oversized":
                    path.write_bytes(b"x" * 1000)
                    with patch("atri_bot.documents._MAX_RECORD_BYTES", 500):
                        self.assertIsNone(await self.store.get(SCOPE, value.id))
                    continue
                if kind in ("offset", "bool_offset", "future", "analysis"):
                    raw.pop("checksum")
                    raw["checksum"] = hashlib.sha256(json.dumps(raw, ensure_ascii=False, allow_nan=False,
                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                path.write_text(json.dumps(raw))
                self.assertIsNone(await self.store.get(SCOPE, value.id))

    async def test_path_traversal_and_namespace_symlinks(self):
        value = document(scope=("../../private", "../../group"))
        await self.store.put(value)
        self.assertEqual(await self.store.get(value.scope, value.id), value)
        self.assertIsNone(await self.store.get(value.scope, "../../secret"))
        namespace = next(self.directory.iterdir())
        outside = Path(self.temp.name) / "outside"
        namespace.rename(outside)
        namespace.symlink_to(outside, target_is_directory=True)
        self.assertIsNone(await self.store.get(value.scope, value.id))
        with self.assertRaises(ToolError):
            await self.store.put(value)
        self.assertTrue(next(outside.iterdir()).is_file())

    async def test_record_symlink_is_not_read_or_written_through(self):
        value = document()
        await self.store.put(value)
        path = next(self.directory.glob("*/*.json"))
        outside = Path(self.temp.name) / "outside.json"
        path.rename(outside)
        path.symlink_to(outside)
        self.assertIsNone(await self.store.get(SCOPE, value.id))
        self.assertTrue(outside.exists())
        await self.store.put(value)
        self.assertFalse(path.is_symlink())
        self.assertEqual(await self.store.get(SCOPE, value.id), value)

    async def test_concurrent_writers_and_restart_leave_only_complete_records(self):
        values = [document((str(i) + "内容。") * 100) for i in range(20)]
        stores = [self.store, DocumentStore(self.directory)]
        await asyncio.gather(*(stores[i % 2].put(value) for i, value in enumerate(values)))
        restarted = DocumentStore(self.directory)
        loaded = await asyncio.gather(*(restarted.get(SCOPE, value.id) for value in values))
        self.assertEqual(loaded, values)
        self.assertEqual(len(list(self.directory.glob("*/*.json"))), len(values))
        self.assertEqual(list(self.directory.glob("*/*.tmp")), [])
        for path in self.directory.glob("*/*.json"):
            self.assertEqual(json.loads(path.read_text())["schema"], 1)

    async def test_analysis_replacement_keeps_document_version(self):
        value = document()
        await self.store.put(value)
        updated = replace(value, analysis={"overview": "已覆盖全文"})
        await self.store.put(updated)
        self.assertEqual(await self.store.get(SCOPE, value.id), updated)
        self.assertEqual(len(list(self.directory.glob("*/*.json"))), 1)

    async def test_storage_failure_hides_paths_and_rejects_mutated_source(self):
        value = document()
        value.text = "修改了原文却没有更新版本"
        with self.assertRaises(ToolError) as raised:
            await self.store.put(value)
        self.assertEqual(raised.exception.code, "document_storage_failed")
        self.assertNotIn(str(self.directory), str(raised.exception))
        with patch("atri_bot.documents.os.replace", side_effect=PermissionError("secret filesystem path")):
            with self.assertRaises(ToolError) as raised:
                await self.store.put(document())
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(list(self.directory.glob("*/*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
