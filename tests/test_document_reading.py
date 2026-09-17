"""Document reading integration through real tools, processor and persisted store."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from atri_bot.document_analysis import DocumentProcessor
from atri_bot.documents import DocumentConfig, DocumentStore, build_document
from atri_bot.link_tools import LinkConfig, LinkReader, register_links
from atri_bot.tools import ToolContext, ToolRegistry, ToolsConfig


ARTICLE = "https://mp.weixin.qq.com/s/document-integration"


class OverviewModel:
    def __init__(self):
        self.config = SimpleNamespace(model="integration-model")
        self.calls = []
        self.wait_forever = False
        self.started = asyncio.Event()
        self.cancelled = False
        self.no_match = False

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.started.set()
        if self.wait_forever:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        value = json.loads(messages[-1]["content"])
        if kwargs["purpose"] == "document_select":
            return json.dumps({"chunk_ids": [] if self.no_match else [value["items"][-1]["id"]]})
        ids = ([item["id"] for item in value["items"]] if value["input_kind"] == "chunks" else
               [chunk_id for item in value["items"] for chunk_id in item["covered_chunk_ids"]])
        # One directory item per chunk deliberately exercises bounded directory pages.
        return json.dumps({"summary": "中立全文概览，末尾对开头结论增加适用条件。",
                           "outline": [{"title": f"目录 {chunk_id}", "summary": f"{chunk_id} 的内容与条件。",
                                        "chunk_ids": [chunk_id]} for chunk_id in ids],
                           "key_points": [{"text": "末尾存在适用条件。", "chunk_ids": [ids[-1]]}],
                           "covered_chunk_ids": ids, "complete": True})


class DocumentReadingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "documents"
        self.config = DocumentConfig(chunk_chars=200, overview_min_chars=100, input_token_budget=48000)
        self.links = LinkConfig(enabled=True)
        self.tools = ToolsConfig(max_result_chars=1800)
        self.context = ToolContext("group-1", "user-1", "bot-1", "request-1", 1,
                                   None, lambda: None, lambda _: None)
        self.body = "# 标题\n" + "\n\n".join(
            f"第 {i} 部分。" + ("引文包含中文🙂、换行与转义符\"\\，条件需要联系上下文。" * 7)
            for i in range(1, 15)) + "\n\n末尾补充：先前结论仅限特定条件，不能推广。"
        self.mcp = SimpleNamespace(call=AsyncMock(return_value={
            "content": [{"type": "text", "text": self.body}]}))
        self.model = OverviewModel()
        self.restart()

    def restart(self):
        self.store = DocumentStore(self.directory)
        self.processor = DocumentProcessor(self.config, self.model, self.store)
        self.reader = LinkReader(self.links, self.mcp, self.tools.max_result_chars, processor=self.processor)
        self.registry = ToolRegistry()
        register_links(self.registry, self.reader)

    async def execute(self, name, arguments, *, context=None):
        return await self.registry.execute(name, json.dumps(arguments), context or self.context, self.tools)

    async def acquired(self, **arguments):
        result = await self.execute("read_link", {"url": ARTICLE, **arguments})
        self.assertTrue(result.ok, result.error)
        return result

    async def record(self, document_id):
        record = await self.store.get((self.context.self_id, self.context.group_id), document_id)
        self.assertIsNotNone(record)
        return record

    async def test_first_long_read_covers_full_source_without_claiming_raw_was_read(self):
        result = await self.acquired()
        self.assertEqual(result.data["view"], "overview")
        self.assertNotIn("text", result.data)
        self.assertNotIn("passages", result.data)
        self.assertTrue(result.meta["overview_complete"])
        self.assertEqual(result.meta["overview_scope"], "acquired_text")
        self.assertEqual(result.meta["raw_read_chunk_ids"], [])
        self.assertEqual(result.meta["raw_read_chars"], 0)
        self.assertEqual(result.meta["raw_total_chars"], len(self.body))
        self.assertFalse(result.meta["visuals_read"])
        record = await self.record(result.data["document_id"])
        self.assertEqual(record.text, self.body)
        self.assertEqual(set(record.analysis["covered_chunk_ids"]), {c.id for c in record.chunks})
        self.assertEqual(len(self.model.calls), 1)

    async def test_question_locates_last_chunk_and_returns_its_exact_source(self):
        result = await self.acquired(question="最后增加了什么条件？")
        record = await self.record(result.data["document_id"])
        self.assertEqual(result.data["view"], "passages")
        self.assertEqual([row["chunk_id"] for row in result.data["passages"]], [record.chunks[-1].id])
        self.assertEqual(result.data["passages"][0]["text"], record.chunks[-1].text)
        self.assertTrue(result.data["passages"][0]["complete"])
        self.assertIn("末尾补充", result.data["passages"][0]["text"])
        self.assertEqual(result.meta["raw_read_chunk_ids"], [record.chunks[-1].id])
        self.assertEqual([call[1]["purpose"] for call in self.model.calls],
                         ["document_overview", "document_select"])

    async def test_question_selects_tail_when_unanalyzed_short_text_exceeds_tool_budget(self):
        self.config.overview_min_chars = len(self.body) + 1
        self.restart()
        first = await self.acquired(question="末尾对适用条件做了什么补充？")
        record = await self.record(first.data["document_id"])
        self.assertIsNone(record.analysis)
        self.assertEqual(first.data["view"], "passages")
        self.assertEqual(first.data["passages"][0]["chunk_id"], record.chunks[-1].id)
        self.assertEqual(first.data["passages"][0]["text"], record.chunks[-1].text)
        self.assertIn("末尾补充", first.data["passages"][0]["text"])
        self.assertFalse(first.meta["overview_complete"])
        self.assertNotIn("overview", first.data)
        second = await self.execute("read_document", {"document_id": record.id,
                                                        "question": "请再定位文档末尾的条件。"})
        self.assertTrue(second.ok, second.error)
        self.assertEqual(second.data["view"], "passages")
        self.assertEqual(second.data["passages"][0]["text"], record.chunks[-1].text)
        self.assertEqual([call[1]["purpose"] for call in self.model.calls],
                         ["document_select", "document_select"])

    async def test_restart_recovers_original_and_overview_without_mcp_or_model_work(self):
        first = await self.acquired()
        record = await self.record(first.data["document_id"])
        self.restart()
        overview = await self.acquired()
        self.assertEqual(overview.data["document_id"], record.id)
        self.assertTrue(overview.meta["cached"])
        result = await self.execute("read_document", {"document_id": record.id,
                                                       "chunk_ids": [record.chunks[-1].id]})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data["passages"][0]["text"], record.chunks[-1].text)
        self.assertEqual(self.mcp.call.await_count, 1)
        self.assertEqual(len(self.model.calls), 1)
        for context in (replace(self.context, group_id="foreign-group"),
                        replace(self.context, self_id="foreign-bot")):
            other = await self.execute("read_document", {"document_id": record.id}, context=context)
            self.assertFalse(other.ok)
            self.assertEqual(other.error["code"], "document_not_found")

    async def test_changed_size_or_chunk_config_invalidates_ids_and_rebuilds_without_refetch(self):
        base_directory = self.directory
        for index, (limit, chunk_size) in enumerate(((1000, 200), (200000, 350), (1000, 350))):
            with self.subTest(max_document_chars=limit, chunk_chars=chunk_size):
                self.directory = base_directory / str(index)
                self.config.chunk_chars = 200
                self.links.max_document_chars = 200000
                self.restart()
                initial = await self.acquired()
                old_record = await self.record(initial.data["document_id"])
                requests_before = self.mcp.call.await_count
                self.links.max_document_chars = limit
                self.config.chunk_chars = chunk_size
                self.restart()
                obsolete = await self.execute("read_document", {"document_id": old_record.id,
                                                                  "chunk_ids": [old_record.chunks[0].id]})
                self.assertFalse(obsolete.ok)
                self.assertEqual(obsolete.error["code"], "document_config_changed")
                rebuilt = await self.acquired()
                new_record = await self.record(rebuilt.data["document_id"])
                self.assertNotEqual(new_record.id, old_record.id)
                self.assertEqual(new_record.text, self.body[:limit])
                self.assertEqual(new_record.chunk_chars, chunk_size)
                self.assertTrue(all(len(chunk.text) <= chunk_size for chunk in new_record.chunks))
                self.assertEqual("".join(chunk.text for chunk in new_record.chunks), new_record.text)
                self.assertEqual(rebuilt.meta["truncated"], len(self.body) > limit)
                self.assertEqual(rebuilt.meta["partial"], len(self.body) > limit)
                if len(self.body) > limit:
                    self.assertEqual(rebuilt.meta["warning"], "document_size_limit")
                self.assertTrue(rebuilt.meta["overview_complete"])
                self.assertEqual(self.mcp.call.await_count, requests_before)
                persisted_original = await self.record(old_record.id)
                self.assertEqual(persisted_original.text, self.body)

    async def test_old_prompt_summary_is_hidden_when_reading_persisted_source_chunks(self):
        first = await self.acquired()
        record = await self.record(first.data["document_id"])
        previous_summary = record.analysis["summary"]
        calls_before = len(self.model.calls)
        with patch("atri_bot.document_analysis.PROMPT_VERSION", "test-next-prompt-version"):
            self.restart()
            self.assertFalse(self.processor.has_valid_analysis(record))
            result = await self.execute("read_document", {"document_id": record.id,
                                                           "chunk_ids": [record.chunks[-1].id]})
            self.assertTrue(result.ok, result.error)
            self.assertFalse(result.meta["overview_complete"])
            self.assertNotIn("overview", result.data)
            self.assertNotIn(previous_summary, result.to_json())
            self.assertEqual(result.data["passages"][0]["text"], record.chunks[-1].text)
            raw = await self.execute("read_document", {"document_id": record.id})
            self.assertTrue(raw.ok, raw.error)
            self.assertFalse(raw.meta["overview_complete"])
            self.assertNotIn(previous_summary, raw.to_json())
        self.assertEqual(len(self.model.calls), calls_before)

    async def test_directory_pagination_has_boundaries_and_never_exposes_raw(self):
        self.reader.max_result_chars = self.tools.max_result_chars = 1200
        result = await self.acquired()
        record = await self.record(result.data["document_id"])
        expected = record.analysis["outline"]
        collected, previous_end, pages = [], 0, 0
        while True:
            pages += 1
            self.assertLess(pages, 100)
            self.assertEqual(result.data["view"], "overview")
            self.assertEqual(result.data["outline_range"]["start"], previous_end)
            previous_end = result.data["outline_range"]["end"]
            self.assertEqual(result.meta["raw_read_chars"], 0)
            self.assertLessEqual(len(result.to_json()), self.tools.max_result_chars)
            collected.extend(result.data["outline"])
            if not result.data["has_more"]:
                break
            cursor = result.data["next_cursor"]
            self.assertTrue(cursor.startswith("o."))
            result = await self.execute("read_document", {"document_id": record.id, "cursor": cursor})
            self.assertTrue(result.ok, result.error)
        self.assertGreater(pages, 1)
        self.assertEqual(collected, expected)
        self.assertEqual(previous_end, len(expected))
        self.assertIsNone(result.data["next_cursor"])
        self.assertEqual(len(self.model.calls), 1)

    async def test_directory_cursor_is_signed_bound_to_source_and_invalid_after_restart(self):
        self.reader.max_result_chars = self.tools.max_result_chars = 1200
        first = await self.acquired()
        first_id, cursor = first.data["document_id"], first.data["next_cursor"]
        second = await self.execute("read_link", {"url": ARTICLE + "2"})
        self.assertTrue(second.ok, second.error)
        forged = cursor.rsplit(".", 1)[0] + "." + "0" * 24
        for document_id, candidate in ((second.data["document_id"], cursor), (first_id, forged),
                                       (first_id, "o.0." + "0" * 24),
                                       (first_id, "o.ffffff." + "0" * 24)):
            result = await self.execute("read_document", {"document_id": document_id, "cursor": candidate})
            self.assertFalse(result.ok)
            self.assertEqual(result.error["code"], "invalid_document_cursor")
        self.restart()
        result = await self.execute("read_document", {"document_id": first_id, "cursor": cursor})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "invalid_document_cursor")

    async def test_explicit_raw_pagination_is_lossless_and_coverage_is_per_page(self):
        first = await self.acquired()
        record = await self.record(first.data["document_id"])
        arguments = {"document_id": record.id}
        pieces, previous_end = [], 0
        for _ in range(len(self.body)):
            result = await self.execute("read_document", arguments)
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.data["view"], "text")
            span = result.data["range"]
            self.assertEqual(span["start"], previous_end)
            previous_end = span["end"]
            self.assertEqual(result.meta["raw_read_chars"], len(result.data["text"]))
            self.assertEqual(result.meta["raw_read_chunk_ids"],
                             [c.id for c in record.chunks if span["start"] <= c.start and c.end <= span["end"]])
            self.assertLessEqual(len(result.to_json()), self.tools.max_result_chars)
            pieces.append(result.data["text"])
            if not result.data["has_more"]:
                break
            arguments = {"document_id": record.id, "cursor": result.data["next_cursor"]}
            self.assertFalse(arguments["cursor"].startswith("o."))
        self.assertEqual("".join(pieces), self.body)
        self.assertEqual(previous_end, len(self.body))
        self.assertEqual(len(self.model.calls), 1)

    async def test_small_budget_passage_is_honest_about_partial_text_and_source_timestamps(self):
        self.config.chunk_chars = 1800
        self.restart()
        text = "原文很长，需要按预算截取。" * 200
        record = build_document(scope=(self.context.self_id, self.context.group_id), url=ARTICLE,
                                title="ASR", text=text, source="local_asr", chunk_chars=1800,
                                segments=[{"text": text, "begin_time": 1200, "end_time": 60000}])
        await self.reader.present_document(self.context, record)
        result = await self.execute("read_document", {"document_id": record.id,
                                                       "chunk_ids": [record.chunks[0].id, record.chunks[1].id]})
        self.assertTrue(result.ok, result.error)
        self.assertLessEqual(len(result.to_json()), self.tools.max_result_chars)
        passage = result.data["passages"][0]
        self.assertFalse(passage["complete"])
        self.assertLess(passage["end"], passage["chunk_end"])
        self.assertEqual(passage["text"], text[passage["start"]:passage["end"]])
        self.assertEqual((passage["begin_ms"], passage["end_ms"]), (1200, 60000))
        self.assertEqual(passage["time_scope"], "source_chunk")
        self.assertEqual(result.meta["raw_read_chunk_ids"], [])
        self.assertEqual(result.meta["raw_read_chars"], len(passage["text"]))
        self.assertEqual(result.data["unread_chunk_ids"], [record.chunks[1].id])
        self.assertEqual(result.data["next_cursor_view"], "text")
        continued = await self.execute("read_document", {"document_id": record.id,
                                                          "cursor": result.data["next_cursor"]})
        self.assertTrue(continued.ok, continued.error)
        self.assertEqual(continued.data["range"]["start"], passage["end"])

    async def test_complete_overview_of_truncated_source_does_not_claim_complete_original(self):
        self.links.max_document_chars = 1000
        self.restart()
        result = await self.acquired()
        self.assertTrue(result.meta["partial"])
        self.assertTrue(result.meta["truncated"])
        self.assertEqual(result.meta["warning"], "document_size_limit")
        self.assertTrue(result.meta["overview_complete"])
        self.assertEqual(result.meta["overview_scope"], "acquired_text")
        self.assertEqual(result.meta["raw_total_chars"], 1000)
        record = await self.record(result.data["document_id"])
        self.assertEqual(record.text, self.body[:1000])

    async def test_no_selection_match_does_not_fabricate_raw_passages(self):
        self.model.no_match = True
        result = await self.acquired(question="文档完全没有提到的问题")
        self.assertEqual(result.data["view"], "passages")
        self.assertFalse(result.data["selection_matched"])
        self.assertEqual(result.data["passages"], [])
        self.assertEqual(result.meta["raw_read_chunk_ids"], [])
        self.assertEqual(result.meta["raw_read_chars"], 0)
        self.assertTrue(result.meta["overview_complete"])

    async def test_selector_combinations_rejected_before_any_additional_processing(self):
        result = await self.acquired()
        record = await self.record(result.data["document_id"])
        combinations = [
            {"question": "问题", "chunk_ids": [record.chunks[0].id]},
            {"question": "问题", "cursor": result.data["next_cursor"]},
            {"chunk_ids": [record.chunks[0].id], "cursor": result.data["next_cursor"]},
            {"chunk_ids": [record.chunks[0].id, record.chunks[0].id]},
            {"chunk_ids": ["c9999"]}, {"question": "   "},
        ]
        for selectors in combinations:
            with self.subTest(selectors=selectors):
                rejected = await self.execute("read_document", {"document_id": record.id, **selectors})
                self.assertFalse(rejected.ok)
                self.assertEqual(rejected.error["code"], "invalid_arguments")
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.mcp.call.await_count, 1)

    async def test_disabled_links_or_absent_processor_do_not_silently_allow_question_lookup(self):
        absent = LinkReader(self.links, self.mcp)
        registry = ToolRegistry()
        register_links(registry, absent)
        result = await registry.execute("read_link", json.dumps({"url": ARTICLE, "question": "问题"}),
                                        self.context, self.tools)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "document_processing_disabled")
        disabled = LinkReader(LinkConfig(enabled=False), self.mcp, processor=self.processor)
        registry = ToolRegistry()
        register_links(registry, disabled)
        self.assertEqual(registry.definitions(), [])
        result = await registry.execute("read_link", json.dumps({"url": ARTICLE}), self.context, self.tools)
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "unknown_tool")
        self.mcp.call.assert_not_awaited()
        self.assertEqual(self.model.calls, [])

    async def test_timeout_keeps_acquired_source_without_analysis_or_success_cache(self):
        self.config.timeout = .03
        self.model.wait_forever = True
        self.restart()
        result = await self.execute("read_link", {"url": ARTICLE})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "tool_timeout")
        self.assertFalse(result.meta["retryable"])
        self.assertIsNone(result.data)
        record = await self.store.find((self.context.self_id, self.context.group_id), ARTICLE)
        self.assertIsNotNone(record)
        self.assertEqual(record.text, self.body)
        self.assertIsNone(record.analysis)
        self.assertFalse(self.reader._documents)
        self.assertTrue(self.model.cancelled)
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.mcp.call.await_count, 1)

    async def test_cancellation_propagates_without_success_and_only_raw_is_persisted(self):
        self.model.wait_forever = True
        task = asyncio.create_task(self.execute("read_link", {"url": ARTICLE}))
        await asyncio.wait_for(self.model.started.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        record = await self.store.find((self.context.self_id, self.context.group_id), ARTICLE)
        self.assertIsNotNone(record)
        self.assertIsNone(record.analysis)
        self.assertEqual(record.text, self.body)
        self.assertFalse(self.reader._documents)
        self.assertTrue(self.model.cancelled)
        self.assertEqual(len(self.model.calls), 1)


if __name__ == "__main__":
    unittest.main()
