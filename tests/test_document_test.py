import asyncio
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from atri_bot.config import Config
from atri_bot.document_test import DIAGNOSTIC_SCOPE, load_document_input, render_preview, run_document_test
from atri_bot.documents import DocumentConfig
from atri_bot.model import ModelError
from atri_bot.tools import ToolResult


class DocumentInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def load(self, name, content, **kwargs):
        path = self.root / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return load_document_input(path, max_chars=kwargs.pop("max_chars", 20000), chunk_chars=200, **kwargs)

    def test_plain_markdown_is_lossless_and_never_carries_absolute_path(self):
        text = "# 小标题\r\n\r\n  正文保留缩进和换行。\n"
        document = self.load("source.md", text)
        self.assertEqual(document.text, text)
        self.assertEqual("".join(chunk.text for chunk in document.chunks), text)
        self.assertEqual(document.scope, DIAGNOSTIC_SCOPE)
        self.assertEqual(document.url, "local:document-test")
        self.assertEqual(document.title, "source")
        self.assertEqual(document.source, "local_document")
        self.assertNotIn(str(self.root), repr(document))

    def test_asr_json_imports_text_millisecond_timestamps_and_ignores_word_metadata(self):
        value = {"bvid": "BV1dVRdBpEze", "task_id": "not-model-input", "text": "前半段。后半段。",
                 "segments": [{"text": "前半段。", "begin_time": 1200, "end_time": 5000,
                               "words": [{"text": "前", "confidence": .99}]},
                              {"text": "后半段。", "begin_time": 6000, "end_time": 9500}]}
        document = self.load("transcript.json", json.dumps(value),
                             url="https://www.bilibili.com/video/BV1dVRdBpEze/?share=1")
        self.assertEqual(document.text, value["text"])
        self.assertEqual(document.source, "local_asr")
        self.assertEqual(document.title, "BV1dVRdBpEze 语音转写")
        self.assertEqual(document.url, "https://www.bilibili.com/video/BV1dVRdBpEze")
        self.assertEqual(document.sections, (("asr_transcript", 0, len(value["text"])),))
        self.assertEqual((document.chunks[0].begin_ms, document.chunks[0].end_ms), (1200, 9500))
        self.assertNotIn("words", document.segments[0])
        self.assertNotIn("task_id", repr(document))

    def test_empty_invalid_encoding_and_malformed_json_are_rejected(self):
        cases = [("empty.txt", " \n "), ("encoding.txt", b"\xff\xfe"),
                 ("broken.json", "{not-json"), ("list.json", "[]"),
                 ("missing.json", '{"segments": []}'), ("nan.json", '{"text":"abc", "x": NaN}'),
                 ("binary.txt", "valid\x00invalid"), ("other.html", "<p>other</p>")]
        for name, content in cases:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.load(name, content)

    def test_source_size_limits_reject_instead_of_truncate(self):
        with self.assertRaisesRegex(ValueError, "max_document_chars"):
            self.load("large.txt", "文" * 401, max_chars=400)
        with patch("atri_bot.document_test.MAX_INPUT_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "读取上限"):
                self.load("bytes.txt", "文" * 4)

    def test_missing_file_and_directory_do_not_leak_local_path(self):
        directory = self.root / "directory.txt"
        directory.mkdir()
        for path in (self.root / "missing.txt", directory):
            with self.subTest(path=path), self.assertRaises(ValueError) as error:
                load_document_input(path, max_chars=20000, chunk_chars=200)
            self.assertNotIn(str(self.root), str(error.exception))

    def test_invalid_asr_times_and_unaligned_segments_are_rejected(self):
        invalid = [
            {"text": "正文", "begin_time": 3, "end_time": 1},
            {"text": "正文", "begin_time": True, "end_time": 1},
            {"text": "正文", "begin_time": 0},
            {"text": "不在原文中", "begin_time": 0, "end_time": 2},
            {"text": 1, "begin_time": 0, "end_time": 2},
        ]
        for segment in invalid:
            with self.subTest(segment=segment), self.assertRaises(ValueError):
                self.load("bad.json", json.dumps({"text": "正文", "segments": [segment]}))

    def test_preview_keeps_analysis_distinct_from_raw_evidence(self):
        document = self.load("source.txt", "原文内容。")
        analysis = {"summary": "中立概览", "outline": [{"title": "题目", "summary": "目录内容",
                    "chunk_ids": [document.chunks[0].id]}], "key_points": [],
                    "covered_chunk_ids": [document.chunks[0].id], "complete": True}
        document = replace(document, analysis=analysis)
        result = ToolResult(True, data={"passages": [], "has_more": True},
                            meta={"overview_complete": True, "raw_read_chunk_ids": []})
        preview = render_preview(document, result)
        self.assertIn("中立概览", preview)
        self.assertIn("完整覆盖所获原文：是", preview)
        self.assertIn("本轮未展开原文块", preview)
        self.assertNotIn("原文内容", preview)
        self.assertIn("没有读取原图或视频画面", preview)


class FakeDocumentModel:
    def __init__(self, config, *, error=None, delay=0):
        self.config, self.error, self.delay = config, error, delay
        self.calls = []
        self.cancelled = False

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        value = json.loads(messages[-1]["content"])
        if kwargs["purpose"] == "document_select":
            return json.dumps({"chunk_ids": [value["items"][-1]["id"]]})
        ids = ([item["id"] for item in value["items"]] if value["input_kind"] == "chunks" else
               [chunk_id for item in value["items"] for chunk_id in item["covered_chunk_ids"]])
        return json.dumps({"summary": "中立的全文概览，保留前后条件。",
                           "outline": [{"title": "全文目录", "summary": "开头说明，末尾补充条件。",
                                        "chunk_ids": ids}],
                           "key_points": [{"text": "结论有适用条件。", "chunk_ids": [ids[-1]]}],
                           "covered_chunk_ids": ids, "complete": True})


class DocumentDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "production-data"
        self.config = Config(root=self.root, data=self.data, model="unit-model",
                             api_key="unit-model-secret", base_url="http://127.0.0.1:1/v1")
        self.config.documents = DocumentConfig(overview_min_chars=0, chunk_chars=200)
        self.path = self.root / "supplied.txt"
        self.original = "第一部分解释概念。" * 20 + "\n\n" + "末尾补充适用条件。" * 20
        self.path.write_text(self.original, encoding="utf-8")

    async def run_fake(self, *, model=None, question=None, path=None):
        model = model or FakeDocumentModel(self.config)
        output = io.StringIO()
        with patch("atri_bot.document_test.ChatModel", return_value=model), \
                patch.object(Config, "read_personal_info", side_effect=AssertionError("Do not read persona")):
            passed = await run_document_test(self.config, path or self.path, question=question, stream=output)
        self.assertNotIn(self.config.api_key, output.getvalue())
        self.assertNotIn(str(self.root), json.dumps(model.calls, ensure_ascii=False))
        return passed, output.getvalue(), model

    def report(self):
        paths = list((self.data / "diagnostics" / "document-reading").glob("doc_*/result.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))

    async def test_local_real_pipeline_overview_and_selected_evidence_are_isolated(self):
        self.data.mkdir()
        history = self.data / "group-history.jsonl"
        history.write_text("existing production history", encoding="utf-8")
        passed, output, model = await self.run_fake(question="末尾有哪些适用条件？")
        self.assertTrue(passed, output)
        report_path, report = self.report()
        self.assertEqual(report["source_url"], "local:document-test")
        self.assertTrue(report["analysis"]["complete"])
        self.assertEqual(len(report["analysis"]["covered_chunk_ids"]), report["chunk_count"])
        self.assertEqual([call[1]["purpose"] for call in model.calls], ["document_overview", "document_select"])
        preview = report_path.with_name("preview.md").read_text(encoding="utf-8")
        self.assertIn("末尾补充适用条件", preview)
        self.assertIn("中立的全文概览", preview)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.original)
        self.assertEqual(history.read_text(encoding="utf-8"), "existing production history")
        self.assertEqual({path.name for path in self.data.iterdir()}, {"diagnostics", history.name})

    async def test_repeated_document_reuses_saved_overview_without_model_call(self):
        first, output, model = await self.run_fake()
        self.assertTrue(first, output)
        second, output, model = await self.run_fake(model=model)
        self.assertTrue(second, output)
        self.assertEqual(len(model.calls), 1)

    async def test_model_failure_does_not_leak_raw_exception_or_retry(self):
        model = FakeDocumentModel(self.config, error=ModelError(self.config.api_key + " hidden exception"))
        passed, output, model = await self.run_fake(model=model)
        self.assertFalse(passed)
        self.assertNotIn("hidden exception", output)
        self.assertEqual(len(model.calls), 1)
        _, report = self.report()
        self.assertFalse(report["tool_result"]["ok"])
        self.assertFalse(report["tool_result"]["meta"]["retryable"])
        self.assertIsNone(report["analysis"])

    async def test_total_timeout_cancels_inflight_model_and_does_not_retry(self):
        self.config.documents.timeout = .03
        model = FakeDocumentModel(self.config, delay=30)
        passed, output, model = await self.run_fake(model=model)
        self.assertFalse(passed)
        self.assertTrue(model.cancelled)
        self.assertEqual(len(model.calls), 1)
        _, report = self.report()
        self.assertEqual(report["tool_result"]["error"]["code"], "tool_timeout")

    async def test_invalid_input_fails_before_model_or_diagnostic_writes(self):
        self.path.write_text("", encoding="utf-8")
        passed, output, model = await self.run_fake()
        self.assertFalse(passed)
        self.assertFalse(model.calls)
        self.assertFalse(self.data.exists())


class DocumentDiagnosticCLITests(unittest.TestCase):
    def test_cli_requires_document_and_restricts_document_arguments(self):
        from atri_bot.cli import main

        for args in (["test-document"], ["check", "--document", "source.txt"],
                     ["test-links", "--url", "https://example.com", "--question", "问题"]):
            with self.subTest(args=args), patch("sys.stderr", new_callable=io.StringIO), \
                    self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)

    def test_cli_routes_document_without_qq_and_disables_production_file_logs(self):
        from atri_bot.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text('[llm]\napi_key="unit-secret"\nmodel="unit-model"\n'
                            'base_url="http://127.0.0.1:1/v1"\n', encoding="utf-8")
            document = root / "source.txt"
            with patch("atri_bot.cli.run_document_test", new_callable=AsyncMock, return_value=True) as run, \
                    patch("atri_bot.cli.configure_logging") as configure, \
                    patch("atri_bot.cli.serve", new_callable=AsyncMock) as serve:
                self.assertIsNone(main(["--config", str(path), "test-document", "--document", str(document),
                                        "--url", "https://www.bilibili.com/video/BV1dVRdBpEze/",
                                        "--question", "最后的条件是什么？"]))
            run.assert_awaited_once()
            self.assertEqual(run.await_args.args[1], document)
            self.assertEqual(run.await_args.kwargs["question"], "最后的条件是什么？")
            self.assertEqual(configure.call_args.args[0].file, "")
            self.assertIn("unit-secret", configure.call_args.kwargs["secrets"])
            serve.assert_not_awaited()
            self.assertEqual(list(root.iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
