"""Video acquisition, cloud fallback, retention and tools share one real pipeline."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from atri_bot.cloud_asr import Transcript
from atri_bot.document_analysis import DocumentProcessor
from atri_bot.documents import DocumentConfig, DocumentStore, build_document
from atri_bot.link_tools import LinkConfig, LinkReader, register_links
from atri_bot.tools import ToolContext, ToolError, ToolRegistry, ToolsConfig
from atri_bot.video_cache import VideoSourceCache


URL = "https://www.bilibili.com/video/BV1dVRdBpEze"
TEXT = "\n\n".join(f"第{i}节：" + "保留原始转写的说明和适用条件。" * 10 for i in range(1, 6))


class VideoMCP:
    def __init__(self):
        self.calls = []
        self.subtitle_error = "SUBTITLE_UNAVAILABLE"
        self.metadata_error = None

    async def call(self, server, name, arguments):
        self.calls.append((server, name, arguments))
        error = self.metadata_error if name == "get_video_metadata" else self.subtitle_error
        if error:
            return {"isError": True, "structuredContent": {"error": "failed", "code": error}}
        if name == "get_video_metadata":
            return {"structuredContent": {"bvid": "BV1dVRdBpEze", "title": "公开讲解视频",
                                           "description": "元信息简介", "author": "讲解者"}}
        return {"structuredContent": {"bvid": "BV1dVRdBpEze", "page": arguments["page"],
                                       "data_source": "subtitle", "transcript": TEXT}}


class NeutralModel:
    def __init__(self):
        self.config = SimpleNamespace(model="test-neutral-model")
        self.calls = []
        self.fail = False

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.fail:
            return "not valid JSON"
        value = json.loads(messages[-1]["content"])
        if kwargs["purpose"] == "document_select":
            return json.dumps({"chunk_ids": [value["items"][-1]["id"]]})
        ids = ([item["id"] for item in value["items"]] if value["input_kind"] == "chunks" else
               [id for item in value["items"] for id in item["covered_chunk_ids"]])
        return json.dumps({"summary": "视频说明了方法及其适用条件。",
                           "outline": [{"title": "方法和条件", "summary": "全文依次解释方法及条件。",
                                        "chunk_ids": ids}],
                           "key_points": [{"text": "结论有适用条件。", "chunk_ids": [ids[-1]]}],
                           "covered_chunk_ids": ids, "complete": True})


class FakeCloudASR:
    def __init__(self):
        self.config = SimpleNamespace(timeout=1.0)
        self.calls = []
        self.error = None
        self.block = False
        self.started = asyncio.Event()
        self.cancelled = False

    async def transcribe(self, audio, check_active):
        check_active()
        self.calls.append(audio)
        self.started.set()
        if not audio.path.is_file():
            raise AssertionError("temporary audio must exist during transcription")
        if self.error is not None:
            raise self.error
        if self.block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return Transcript(TEXT, ({"text": TEXT, "begin_time": 100, "end_time": 120000},),
                          120000, "isolated-fake-task")


class VideoASRIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.links = LinkConfig(enabled=True, cache_ttl_seconds=86400)
        self.documents = DocumentConfig(chunk_chars=200, overview_min_chars=100)
        self.tools = ToolsConfig(max_result_chars=8000)
        self.context = ToolContext("group-a", "user-a", "bot-a", "request-a", 1,
                                   None, lambda: None, lambda _: None)
        self.mcp, self.model, self.asr = VideoMCP(), NeutralModel(), FakeCloudASR()
        self.audio_calls, self.audio_paths, self.audio_cleaned = [], [], []
        self.audio_error = None
        self.audio_patch = patch("atri_bot.link_tools.acquire_audio", self.acquire_audio)
        self.audio_patch.start()
        self.addCleanup(self.audio_patch.stop)
        self.restart()

    @asynccontextmanager
    async def acquire_audio(self, bvid, page, config, check_active):
        check_active()
        self.audio_calls.append((bvid, page))
        if self.audio_error:
            raise self.audio_error
        path = self.directory / f"temporary-{len(self.audio_calls)}.aac"
        path.write_bytes(b"fake public audio")
        self.audio_paths.append(path)
        try:
            yield SimpleNamespace(path=path, duration_seconds=120, bytes=17)
        finally:
            path.unlink(missing_ok=True)
            self.audio_cleaned.append(path)

    def restart(self, *, processing=True, asr_enabled=True, max_documents=256):
        self.store = DocumentStore(self.directory / "group-documents", ttl_seconds=86400)
        self.processor = DocumentProcessor(self.documents, self.model, self.store) if processing else None
        self.video_cache = VideoSourceCache(self.directory / "video-sources", max_documents=max_documents)
        self.reader = LinkReader(self.links, self.mcp, self.tools.max_result_chars,
                                processor=self.processor, asr=self.asr if asr_enabled else None,
                                video_cache=self.video_cache)
        self.registry = ToolRegistry()
        register_links(self.registry, self.reader)

    async def execute(self, name="read_link", arguments=None, *, context=None):
        return await self.registry.execute(name, json.dumps(arguments or {"url": URL}),
                                           context or self.context, self.tools)

    def assert_pipeline_counts(self, *, metadata=1, subtitle=1, audio=1, asr=1, overview=1):
        self.assertEqual(sum(name == "get_video_metadata" for _, name, _ in self.mcp.calls), metadata)
        self.assertEqual(sum(name == "get_video_transcript" for _, name, _ in self.mcp.calls), subtitle)
        self.assertEqual(len(self.audio_calls), audio)
        self.assertEqual(len(self.asr.calls), asr)
        self.assertEqual(sum(options["purpose"] == "document_overview" for _, options in self.model.calls), overview)

    async def test_subtitle_success_never_downloads_audio_or_calls_cloud(self):
        self.mcp.subtitle_error = None
        result = await self.execute()
        self.assertTrue(result.ok, result.error)
        self.assert_pipeline_counts(audio=0, asr=0)
        source = await self.video_cache.get("bot-a", URL)
        self.assertIn(TEXT, source.text)
        self.assertEqual(source.sections[-1][0], "subtitle")
        self.assertFalse(result.meta["visuals_read"])

    async def test_subtitle_unavailable_or_cookie_expired_use_public_audio_once(self):
        for index, error in enumerate(("SUBTITLE_UNAVAILABLE", "COOKIE_EXPIRED")):
            with self.subTest(error=error):
                self.mcp.subtitle_error = error
                context = replace(self.context, self_id=f"bot-{index}")
                result = await self.execute(context=context)
                self.assertTrue(result.ok, result.error)
                self.assertEqual(result.meta["warning"], "cloud_asr_after_" + error.lower())
                self.assertFalse(result.meta["partial"])
                self.assertFalse(result.meta["visuals_read"])
                source = await self.video_cache.get(context.self_id, URL)
                self.assertEqual(source.text, TEXT)
                self.assertEqual(source.chunks[0].begin_ms, 100)
                self.assertEqual(source.sections[0][0], "cloud_asr")
                self.assertFalse(self.audio_paths[-1].exists())
        self.assert_pipeline_counts(metadata=2, subtitle=2, audio=2, asr=2, overview=2)
        for _, name, arguments in self.mcp.calls:
            if name == "get_video_transcript":
                self.assertFalse(arguments["fallback_to_asr"])
                self.assertFalse(arguments["force_asr"])

    async def test_access_network_timeout_and_rate_errors_never_start_cloud_fallback(self):
        for error, expected in (("ACCESS_DENIED", "access_denied"), ("PAID_VIDEO", "paid_video"),
                                ("NETWORK_TIMEOUT", "tool_timeout"), ("NETWORK_ERROR", "network_error"),
                                ("API_RATE_LIMITED", "api_rate_limited")):
            with self.subTest(error=error):
                self.mcp.subtitle_error = error
                result = await self.execute()
                self.assertFalse(result.ok)
                self.assertEqual(result.error["code"], expected)
                self.assertIsNone(await self.video_cache.get("bot-a", URL))
        self.assertEqual(self.audio_calls, [])
        self.assertEqual(self.asr.calls, [])
        self.assertEqual(self.model.calls, [])

    async def test_metadata_failure_does_not_attempt_subtitles_or_audio(self):
        self.mcp.metadata_error = "NETWORK_TIMEOUT"
        result = await self.execute()
        self.assertEqual(result.error["code"], "tool_timeout")
        self.assert_pipeline_counts(subtitle=0, audio=0, asr=0, overview=0)
        self.assertIsNone(await self.video_cache.get("bot-a", URL))

    async def test_public_audio_failure_does_not_call_asr_or_save_metadata_as_success(self):
        self.audio_error = ToolError("audio_access_denied", "public playback is unavailable")
        result = await self.execute()
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "audio_access_denied")
        self.assert_pipeline_counts(asr=0, overview=0)
        self.assertIsNone(await self.video_cache.get("bot-a", URL))
        self.assertEqual(list((self.directory / "group-documents").glob("*/*.json")), [])

    async def test_cloud_timeout_returns_failure_and_cleans_audio_without_success_cache(self):
        self.asr.block = True
        self.asr.config.timeout = 0.02
        self.restart()
        result = await self.execute()
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "tool_timeout")
        self.assertTrue(self.asr.cancelled)
        self.assertEqual(self.audio_cleaned, self.audio_paths)
        self.assertTrue(all(not path.exists() for path in self.audio_paths))
        self.assert_pipeline_counts(overview=0)
        self.assertIsNone(await self.video_cache.get("bot-a", URL))

    async def test_cloud_error_and_explicit_cancellation_clean_audio_and_do_not_cache(self):
        self.asr.error = ToolError("asr_http_error", "cloud rejected request")
        failed = await self.execute()
        self.assertEqual(failed.error["code"], "asr_http_error")
        self.asr.error, self.asr.block = None, True
        self.asr.started.clear()
        task = asyncio.create_task(self.execute())
        await self.asr.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.asr.cancelled)
        self.assertEqual(self.audio_cleaned, self.audio_paths)
        self.assertTrue(all(not path.exists() for path in self.audio_paths))
        self.assertIsNone(await self.video_cache.get("bot-a", URL))
        self.assertEqual(self.model.calls, [])

    async def test_overview_failure_keeps_successful_raw_for_retry_without_new_asr(self):
        self.model.fail = True
        failed = await self.execute()
        self.assertEqual(failed.error["code"], "document_analysis_invalid")
        saved = await self.video_cache.get("bot-a", URL)
        self.assertEqual(saved.text, TEXT)
        self.assertIsNone(saved.analysis)
        self.assertEqual(self.audio_cleaned, self.audio_paths)
        self.model.fail = False
        self.restart()
        retry = await self.execute()
        self.assertTrue(retry.ok, retry.error)
        self.assertTrue(retry.meta["cached"])
        self.assert_pipeline_counts(overview=2)

    async def test_variants_concurrent_groups_and_restart_reuse_one_source_and_overview(self):
        group_b = replace(self.context, group_id="group-b")
        first, second = await asyncio.gather(
            self.execute(arguments={"url": URL + "/?p=1&share_source=qq"}),
            self.execute(arguments={"url": "https://m.bilibili.com/video/BV1dVRdBpEze#share"}, context=group_b))
        self.assertTrue(first.ok, first.error)
        self.assertTrue(second.ok, second.error)
        first_id, second_id = first.data["document_id"], second.data["document_id"]
        self.assertNotEqual(first_id, second_id)
        self.assert_pipeline_counts()
        self.restart()
        reused = await self.execute()
        self.assertTrue(reused.ok, reused.error)
        self.assertTrue(reused.meta["cached"])
        self.assertEqual(reused.data["document_id"], first_id)
        self.assert_pipeline_counts()
        foreign = await self.execute("read_document", {"document_id": first_id}, context=group_b)
        self.assertEqual(foreign.error["code"], "document_not_found")
        own = await self.execute("read_document", {"document_id": second_id, "chunk_ids": ["c0001"]}, context=group_b)
        self.assertTrue(own.ok, own.error)
        self.assertEqual(own.data["passages"][0]["begin_ms"], 100)

    async def test_different_parts_and_bots_acquire_separately(self):
        for arguments, context in (({"url": URL}, self.context), ({"url": URL + "?p=2"}, self.context),
                                   ({"url": URL}, replace(self.context, self_id="bot-b"))):
            result = await self.execute(arguments=arguments, context=context)
            self.assertTrue(result.ok, result.error)
        self.assert_pipeline_counts(metadata=3, subtitle=3, audio=3, asr=3, overview=3)
        self.assertEqual(self.audio_calls, [("BV1dVRdBpEze", 1), ("BV1dVRdBpEze", 2), ("BV1dVRdBpEze", 1)])

    async def test_updated_source_overview_replaces_old_group_overview_without_second_model_call(self):
        with patch("atri_bot.video_cache.time.time", return_value=100000):
            first = await self.execute()
            self.assertTrue(first.ok, first.error)
            document_id = first.data["document_id"]
        with patch("atri_bot.video_cache.time.time", return_value=100100), \
                patch("atri_bot.document_analysis.PROMPT_VERSION", "new-overview-prompt-version"):
            self.restart()
            refreshed = await self.execute()
            self.assertTrue(refreshed.ok, refreshed.error)
            self.assertEqual(refreshed.data["document_id"], document_id)
            self.assert_pipeline_counts(overview=2)
            source = await self.video_cache.get("bot-a", URL)
            group = await self.store.get(("bot-a", "group-a"), document_id)
            self.assertTrue(self.processor.has_valid_analysis(source))
            self.assertTrue(self.processor.has_valid_analysis(group))
            self.assertEqual(source.created_at, 100000)
            self.assertEqual(group.created_at, 100000)
            self.assertNotEqual(source.analysis["version"], group.analysis["version"])
            again = await self.execute()
            self.assertTrue(again.ok, again.error)
            self.assert_pipeline_counts(overview=2)

    async def test_exact_24_hour_boundary_refetches_without_read_extending_ttl(self):
        with patch("atri_bot.video_cache.time.time", return_value=100000):
            first = await self.execute()
            self.assertTrue(first.ok, first.error)
        with patch("atri_bot.video_cache.time.time", return_value=186399):
            self.restart()
            cached = await self.execute()
            self.assertTrue(cached.ok, cached.error)
            self.assertTrue(cached.meta["cached"])
            self.assert_pipeline_counts()
        with patch("atri_bot.video_cache.time.time", return_value=186400):
            refreshed = await self.execute()
            self.assertTrue(refreshed.ok, refreshed.error)
            self.assertFalse(refreshed.meta["cached"])
            self.assert_pipeline_counts(metadata=2, subtitle=2, audio=2, asr=2, overview=2)

    async def test_processing_disabled_returns_raw_and_read_document_without_llm(self):
        self.restart(processing=False)
        result = await self.execute()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["text"], TEXT)
        reread = await self.execute("read_document", {"document_id": result.data["document_id"]})
        self.assertTrue(reread.ok, reread.error)
        self.assertEqual(reread.data["text"], TEXT)
        self.restart(processing=False)
        cached = await self.execute()
        self.assertTrue(cached.ok, cached.error)
        self.assertTrue(cached.meta["cached"])
        self.assertEqual(cached.data["text"], TEXT)
        self.assert_pipeline_counts(overview=0)

    async def test_full_source_cache_fails_before_metadata_audio_or_paid_cloud(self):
        self.restart(max_documents=1)
        other = URL.replace("BpEze", "BpEzf")
        await self.video_cache.put("bot-a", other, build_document(scope=("bot-a", "video-source"),
            url=other, title="保留的旧视频", text="保留一天的正文", source="bilibili"))
        failed = await self.execute()
        self.assertEqual(failed.error["code"], "video_cache_full")
        self.assert_pipeline_counts(metadata=0, subtitle=0, audio=0, asr=0, overview=0)
        self.assertIsNotNone(await self.video_cache.get("bot-a", other))

    async def test_insufficient_character_capacity_fails_preflight_before_any_network(self):
        # An empty cache can still lack room for the configured acquisition cap.
        self.reader.video_cache = VideoSourceCache(self.directory / "small-cache", max_total_chars=1000)
        failed = await self.execute()
        self.assertEqual(failed.error["code"], "video_cache_full")
        self.assert_pipeline_counts(metadata=0, subtitle=0, audio=0, asr=0, overview=0)

    async def test_capped_transcript_marks_source_partial_without_inventing_timestamps(self):
        self.links.max_document_chars = 1000
        # Return one source segment that extends beyond the retained text cap.
        body = TEXT * 3

        async def long_transcript(audio, check_active):
            self.asr.calls.append(audio)
            return Transcript(body, ({"text": body, "begin_time": 100, "end_time": 120000},),
                              120000, "long-fake-task")

        self.asr.transcribe = long_transcript
        result = await self.execute()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.meta["partial"])
        self.assertTrue(result.meta["truncated"])
        self.assertTrue(result.meta["overview_complete"])
        self.assertEqual(result.meta["overview_scope"], "acquired_text")
        self.assertEqual(result.meta["warning"], "document_size_limit")
        source = await self.video_cache.get("bot-a", URL)
        self.assertEqual(source.text, body[:1000])
        self.assertEqual(source.segments, ())
        self.assertTrue(all(chunk.begin_ms is None and chunk.end_ms is None for chunk in source.chunks))
        self.assert_pipeline_counts()


if __name__ == "__main__":
    unittest.main()
