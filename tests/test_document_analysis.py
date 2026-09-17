"""Document analysis uses isolated local fakes; never contacts a model provider."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from atri_bot.document_analysis import (DocumentProcessor, _normalize_generated_references,
                                       estimate_input_tokens, validate_analysis)
from atri_bot.documents import DocumentConfig, DocumentStore, build_document
from atri_bot.model import ModelError, ModelRequestBlocked, guard_model_requests, reserve_model_slot
from atri_bot.tools import ToolError


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class FakeModel:
    def __init__(self):
        self.config = SimpleNamespace(model="local-neutral-model")
        self.calls = []
        self.handler = None

    async def complete(self, messages, **options):
        self.calls.append((deepcopy(messages), options))
        payload = json.loads(messages[-1]["content"])
        if self.handler:
            return await self.handler(payload, options)
        if options["purpose"] == "document_select":
            return encode({"chunk_ids": [row["id"] for row in payload["items"]
                                         if "结尾修正" in encode(row)][:3]})
        return encode(self.overview(payload))

    @staticmethod
    def overview(payload):
        if payload["input_kind"] == "chunks":
            rows = [{"title": row["heading"] or "正文段落", "summary": row["text"][-100:],
                     "chunk_ids": [row["id"]]} for row in payload["items"]]
        else:
            rows = [{"title": "合并段落", "summary": item["outline"][-1]["summary"],
                     "chunk_ids": item["covered_chunk_ids"]} for item in payload["items"]]
        ids = [id for row in rows for id in row["chunk_ids"]]
        return {"summary": "文档按段落说明观点及其适用条件。", "outline": rows,
                "key_points": [{"text": rows[-1]["summary"], "chunk_ids": rows[-1]["chunk_ids"]}],
                "covered_chunk_ids": ids, "complete": True}


class DocumentAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DocumentStore(Path(self.tmp.name))
        self.config = DocumentConfig(chunk_chars=500, overview_min_chars=100)
        self.model = FakeModel()
        self.processor = DocumentProcessor(self.config, self.model, self.store)
        self.active_checks = 0

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def active(self):
        self.active_checks += 1

    def document(self, text=None, **kwargs):
        return build_document(scope=("bot", "group"), url="https://example.invalid/article",
                              title="一个中立资料", source="asr", chunk_chars=self.config.chunk_chars,
                              text=text or "前文介绍一种方法，结论依赖于输入条件。\n" * 80 +
                              "结尾修正：只有输入满足限制时才成立，不应理解为总是成立。", **kwargs)

    async def test_full_overview_covers_tail_and_persists_without_persona(self):
        document = self.document()
        result = await self.processor.prepare(document, self.active)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNone(document.analysis)
        self.assertEqual(result.analysis["covered_chunk_ids"], [chunk.id for chunk in document.chunks])
        self.assertIn("结尾修正", result.analysis["key_points"][-1]["text"])
        stored = await self.store.get(document.scope, document.id)
        self.assertEqual(stored.analysis, result.analysis)
        messages, options = self.model.calls[0]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertIn("结尾修正", messages[-1]["content"])
        self.assertNotIn("ATRI", encode(messages))
        self.assertNotIn("snapshot", encode(messages))
        self.assertEqual(options["purpose"], "document_overview")
        self.assertTrue(options["json_mode"])
        self.assertGreaterEqual(self.active_checks, 5)

    async def test_short_or_disabled_processing_needs_no_model(self):
        document = self.document("只保留这句完整的原文。")
        self.assertIs(await self.processor.prepare(document, self.active), document)
        self.config.enabled = False
        long_document = self.document()
        self.assertIs(await self.processor.prepare(long_document, self.active), long_document)
        self.assertFalse(self.model.calls)

    async def test_disk_cache_reused_and_config_model_content_versions_invalidate(self):
        document = self.document()
        self.assertFalse(self.processor.has_valid_analysis(document))
        first = await self.processor.prepare(document, self.active)
        self.assertTrue(self.processor.has_valid_analysis(first))
        processor = DocumentProcessor(self.config, self.model, self.store)
        cached = await processor.prepare(document, self.active)
        self.assertEqual(cached.analysis, first.analysis)
        self.assertEqual(len(self.model.calls), 1)
        self.config.max_output_tokens += 1
        self.assertFalse(processor.has_valid_analysis(cached))
        changed_config = await processor.prepare(cached, self.active)
        self.assertEqual(len(self.model.calls), 2)
        self.assertNotEqual(first.analysis["version"], changed_config.analysis["version"])
        self.model.config.model = "other-model"
        await processor.prepare(changed_config, self.active)
        self.assertEqual(len(self.model.calls), 3)
        changed_source = self.document(document.text + "补充结论。")
        changed_source.analysis = changed_config.analysis
        await processor.prepare(changed_source, self.active)
        self.assertEqual(len(self.model.calls), 4)

    async def test_corrupt_cached_reference_is_not_reused(self):
        result = await self.processor.prepare(self.document(), self.active)
        result.analysis["outline"][-1]["chunk_ids"] = ["nonexistent"]
        self.assertFalse(self.processor.has_valid_analysis(result))
        await self.store.put(result)
        prepared = await self.processor.prepare(result, self.active)
        self.assertEqual(len(self.model.calls), 2)
        self.assertNotIn("nonexistent", encode(prepared.analysis))

    async def test_previous_prompt_version_is_reanalyzed_before_reuse(self):
        document = self.document()
        payload = {"input_kind": "chunks", "items": [self.processor._chunk_item(chunk)
                                                   for chunk in document.chunks]}
        document.analysis = self.model.overview(payload)
        with patch("atri_bot.document_analysis.PROMPT_VERSION", "document-overview-4"):
            document.analysis["version"] = self.processor.version(document)
        old_version = document.analysis["version"]
        await self.store.put(document)
        self.assertFalse(self.processor.has_valid_analysis(document))
        result = await self.processor.prepare(document, self.active)
        self.assertEqual(len(self.model.calls), 1)
        self.assertNotEqual(result.analysis["version"], old_version)
        self.assertTrue(self.processor.has_valid_analysis(result))

    async def test_multiple_batches_and_merge_cover_every_source_block(self):
        self.config.input_token_budget = 10000
        self.config.max_model_calls = 16
        document = self.document("".join(f"## 第{i}段\n" + "内容有其条件，不能推广到所有情况。" * 35 + "\n\n"
                                          for i in range(12)) + "结尾修正：最后再次限定适用范围。")
        result = await self.processor.prepare(document, self.active)
        kinds = [json.loads(messages[-1]["content"])["input_kind"] for messages, _ in self.model.calls]
        self.assertGreater(kinds.count("chunks"), 1)
        self.assertIn("overviews", kinds)
        self.assertEqual(set(result.analysis["covered_chunk_ids"]), {chunk.id for chunk in document.chunks})
        self.assertIn("结尾修正", encode(result.analysis))
        for messages, _ in self.model.calls:
            self.assertLessEqual(estimate_input_tokens(messages), self.config.input_token_budget)

    async def test_merge_may_omit_repeated_key_points_but_keeps_all_directory_references(self):
        self.config.input_token_budget = 10000
        self.config.max_model_calls = 16
        document = self.document("".join(f"## 第{i}段\n" + "观点只在指定条件下成立。" * 40 + "\n\n"
                                          for i in range(8)) + "结尾修正：最后一段保留例外。")
        async def concise_merge(payload, options):
            value = self.model.overview(payload)
            if payload["input_kind"] == "overviews":
                value["key_points"] = []
            return encode(value)
        self.model.handler = concise_merge
        result = await self.processor.prepare(document, self.active)
        kinds = [json.loads(messages[-1]["content"])["input_kind"] for messages, _ in self.model.calls]
        self.assertIn("overviews", kinds)
        self.assertEqual(result.analysis["key_points"], [])
        references = {id for row in result.analysis["outline"] for id in row["chunk_ids"]}
        self.assertEqual(references, {chunk.id for chunk in document.chunks})
        self.assertIn("结尾修正", encode(result.analysis["outline"]))

    async def test_generated_exact_single_reference_becomes_array_without_new_evidence(self):
        document = self.document()
        async def scalar_references(payload, options):
            value = self.model.overview(payload)
            for row in value["outline"] + value["key_points"]:
                row["chunk_ids"] = row["chunk_ids"][0]
            return encode(value)
        self.model.handler = scalar_references
        with self.assertLogs("atri.documents", level="DEBUG") as captured:
            result = await self.processor.prepare(document, self.active)
        self.assertEqual(result.analysis["covered_chunk_ids"], [chunk.id for chunk in document.chunks])
        for row in result.analysis["outline"] + result.analysis["key_points"]:
            self.assertIsInstance(row["chunk_ids"], list)
            self.assertEqual(len(row["chunk_ids"]), 1)
            self.assertIn(row["chunk_ids"][0], result.analysis["covered_chunk_ids"])
        normalized = [row for row in captured.output if "[单引用数组归一化]" in row]
        self.assertTrue(normalized)
        self.assertTrue(all("chunk_ids" in row and "c000" not in row for row in normalized))

    async def test_generated_unknown_joined_or_incomplete_scalar_references_still_fail(self):
        document = self.document()
        ids = [chunk.id for chunk in document.chunks]
        for invalid in ("unknown", ",".join(ids[:2]), ids[0] + "-" + ids[-1], " " + ids[0]):
            async def bad_reference(payload, options):
                value = self.model.overview(payload)
                value["key_points"][0]["chunk_ids"] = invalid
                return encode(value)
            self.model.handler = bad_reference
            with self.subTest(reference=invalid), self.assertRaises(ToolError):
                await self.processor.prepare(document, self.active)
        async def missing_coverage(payload, options):
            value = self.model.overview(payload)
            value["outline"] = value["outline"][:-1]
            for row in value["outline"]:
                row["chunk_ids"] = row["chunk_ids"][0]
            return encode(value)
        self.model.handler = missing_coverage
        with self.assertLogs("atri.documents", level="WARNING") as captured, self.assertRaises(ToolError):
            await self.processor.prepare(document, self.active)
        self.assertIn("missing_outline_coverage", "\n".join(captured.output))
        self.assertIsNone(await self.store.get(document.scope, document.id))

    def test_reference_normalization_does_not_mutate_cache_or_relax_coverage_array(self):
        document = self.document("有条件的原文。")
        value = {"summary": "原文概括",
                 "outline": [{"title": "观点", "summary": "具有条件", "chunk_ids": "c0001"}],
                 "key_points": [{"text": "条件不能省略", "chunk_ids": "c0001"}],
                 "covered_chunk_ids": ["c0001"], "complete": True}
        before = deepcopy(value)
        normalized = _normalize_generated_references(value, ["c0001"])
        self.assertEqual(value, before)
        validate_analysis(normalized, ["c0001"])
        document.analysis = {**value, "version": self.processor.version(document)}
        self.assertFalse(self.processor.has_valid_analysis(document))
        self.assertEqual(document.analysis["outline"][0]["chunk_ids"], "c0001")
        value["covered_chunk_ids"] = "c0001"
        with self.assertRaises(ToolError):
            validate_analysis(_normalize_generated_references(value, ["c0001"]), ["c0001"])

    async def test_invalid_json_missing_coverage_or_reference_fails_without_retry(self):
        document = self.document()

        async def attempt(change):
            self.model.calls.clear()
            async def handler(payload, options):
                value = self.model.overview(payload)
                changed = change(value)
                return changed if isinstance(changed, str) else encode(value)
            self.model.handler = handler
            with self.assertRaises(ToolError) as raised:
                await self.processor.prepare(document, self.active)
            self.assertEqual(raised.exception.code, "document_analysis_invalid")
            self.assertEqual(len(self.model.calls), 1)
            self.assertIsNone(await self.store.get(document.scope, document.id))

        cases = [lambda value: "This is not JSON.",
                 lambda value: value.update(covered_chunk_ids=value["covered_chunk_ids"][:-1]),
                 lambda value: value["outline"].pop(),
                 lambda value: value["key_points"][0].update(chunk_ids=["invented"]),
                 lambda value: value.update(complete=1),
                 lambda value: value.update(summary="过" * 2001),
                 lambda value: value["covered_chunk_ids"].append(value["covered_chunk_ids"][0]),
                 lambda value: encode(value)[:-1] + ',"complete":true}']
        for index, case in enumerate(cases):
            with self.subTest(case=index):
                await attempt(case)

    async def test_invalid_results_log_structure_without_source_or_untrusted_keys(self):
        document = self.document()
        private = "PRIVATE_TRANSCRIPT_OR_MODEL_VALUE"
        cases = [
            (lambda value: value.update(summary=private * 100), "string_too_long", "summary", "actual_chars"),
            (lambda value: value.update({private: private}), "object_fields", "$", "extra_field_count"),
            (lambda value: value["key_points"][0].update(chunk_ids=[private]),
             "unknown_references", "key_points[0].chunk_ids", "unknown_count"),
            (lambda value: value.update(covered_chunk_ids=value["covered_chunk_ids"][:-1]),
             "missing_coverage", "covered_chunk_ids", "missing_chunk_count"),
            (lambda value: private + "{broken}", "json_syntax", "$", "actual_chars"),
        ]
        for change, reason, field, count in cases:
            async def handler(payload, options):
                value = self.model.overview(payload)
                changed = change(value)
                return changed if isinstance(changed, str) else encode(value)
            self.model.handler = handler
            with self.subTest(reason=reason), self.assertLogs("atri.documents", level="WARNING") as captured:
                with self.assertRaises(ToolError) as raised:
                    await self.processor.prepare(document, self.active)
            output = "\n".join(captured.output)
            self.assertIn("原因=" + reason, output)
            self.assertIn("字段=" + field, output)
            self.assertIn(count, output)
            self.assertNotIn(private, output)
            self.assertNotIn(document.text[:50], output)
            self.assertNotIn(private, str(raised.exception))

    async def test_model_failure_and_timeout_do_not_retry_or_save_partial_analysis(self):
        document = self.document()
        await self.store.put(document)
        for code, expected in (("model_http_503", "document_analysis_failed"), ("model_timeout", "tool_timeout")):
            async def failed(payload, options):
                raise ModelError("provider private details", code)
            self.model.handler = failed
            self.model.calls.clear()
            with self.assertRaises(ToolError) as raised:
                await self.processor.prepare(document, self.active)
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(len(self.model.calls), 1)
            self.assertIsNone((await self.store.get(document.scope, document.id)).analysis)

    async def test_total_timeout_cancels_model_and_releases_lock(self):
        self.config.timeout = .02
        cancelled = asyncio.Event()
        async def slow(payload, options):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.model.handler = slow
        document = self.document()
        with self.assertRaises(ToolError) as raised:
            await self.processor.prepare(document, self.active)
        self.assertEqual(raised.exception.code, "tool_timeout")
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.processor._locks)
        self.assertEqual(len(self.model.calls), 1)
        self.assertIsNone(await self.store.get(document.scope, document.id))

    async def test_cancellation_propagates_without_background_work(self):
        entered = asyncio.Event()
        async def slow(payload, options):
            entered.set()
            await asyncio.Event().wait()
        self.model.handler = slow
        document = self.document()
        task = asyncio.create_task(self.processor.prepare(document, self.active))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.processor._locks)
        self.assertIsNone(await self.store.get(document.scope, document.id))

    async def test_guard_and_stale_snapshot_propagate_before_and_after_request(self):
        document = self.document()
        with guard_model_requests(lambda: False):
            with self.assertRaises(ModelRequestBlocked):
                await self.processor.prepare(document, self.active)
        self.assertFalse(self.model.calls)
        live = True
        def active():
            if not live:
                raise ModelRequestBlocked("snapshot expired")
        async def invalidate(payload, options):
            nonlocal live
            live = False
            return encode(self.model.overview(payload))
        self.model.handler = invalidate
        with self.assertRaises(ModelRequestBlocked):
            await self.processor.prepare(document, active)
        self.assertIsNone(await self.store.get(document.scope, document.id))

    async def test_model_slot_is_reused_and_released_after_request(self):
        semaphore = asyncio.Semaphore(1)
        async with reserve_model_slot(semaphore):
            await asyncio.wait_for(self.processor.prepare(self.document(), self.active), 1)
            await asyncio.wait_for(semaphore.acquire(), .1)
            semaphore.release()

    async def test_same_document_concurrent_calls_only_analyze_once(self):
        document = self.document()
        entered, released = asyncio.Event(), asyncio.Event()
        async def wait_then_answer(payload, options):
            entered.set()
            await released.wait()
            return encode(self.model.overview(payload))
        self.model.handler = wait_then_answer
        first = asyncio.create_task(self.processor.prepare(document, self.active))
        await entered.wait()
        second = asyncio.create_task(self.processor.prepare(document, self.active))
        await asyncio.sleep(0)
        released.set()
        a, b = await asyncio.gather(first, second)
        self.assertEqual(a.analysis, b.analysis)
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(self.processor._locks)

    async def test_scopes_are_independent(self):
        original = self.document()
        other = build_document(scope=("bot", "other-group"), url=original.url, title=original.title,
                               text=original.text, source=original.source, chunk_chars=self.config.chunk_chars)
        first, second = await asyncio.gather(self.processor.prepare(original, self.active),
                                            self.processor.prepare(other, self.active))
        self.assertEqual(len(self.model.calls), 2)
        self.assertNotEqual(first.analysis["version"], second.analysis["version"])

    async def test_select_reads_directory_and_returns_tail_evidence(self):
        result = await self.processor.prepare(self.document(), self.active)
        ids = await self.processor.select(result, "后文是否修正了前面的结论？", self.active)
        self.assertEqual(ids, [result.chunks[-1].id])
        messages, options = self.model.calls[-1]
        self.assertEqual(options["purpose"], "document_select")
        payload = json.loads(messages[-1]["content"])
        self.assertTrue(all("text" not in item for item in payload["items"]))
        self.assertTrue(all("outline" in item for item in payload["items"]))

    async def test_select_short_source_and_no_match(self):
        document = self.document("一句与问题无关的原文。")
        self.assertEqual(await self.processor.select(document, "原文有没有这个内容？", self.active), [])
        payload = json.loads(self.model.calls[-1][0][-1]["content"])
        self.assertEqual(payload["items"][0]["text"], document.text)

    async def test_select_disabled_makes_no_request(self):
        self.config.enabled = False
        with self.assertRaises(ToolError) as raised:
            await self.processor.select(self.document(), "相关段落？", self.active)
        self.assertEqual(raised.exception.code, "document_processing_disabled")
        self.assertFalse(self.model.calls)

    async def test_select_batches_all_source_then_reduces_candidates(self):
        self.config.input_token_budget = 10000
        self.config.max_model_calls = 32
        document = self.document("原文内容具有相关线索，也要读到结尾。" * 700)
        async def choose_latest(payload, options):
            return encode({"chunk_ids": [item["id"] for item in payload["items"]][-3:]})
        self.model.handler = choose_latest
        selected = await self.processor.select(document, "核对最后的论述", self.active)
        self.assertEqual(selected, [chunk.id for chunk in document.chunks][-3:])
        self.assertGreater(len(self.model.calls), 1)
        seen = {item["id"] for messages, _ in self.model.calls
                for item in json.loads(messages[-1]["content"])["items"]}
        self.assertEqual(seen, {chunk.id for chunk in document.chunks})
        self.assertTrue(all(estimate_input_tokens(messages) <= self.config.input_token_budget
                            for messages, _ in self.model.calls))

    async def test_select_no_match_scans_all_batches(self):
        self.config.input_token_budget = 10000
        self.config.max_model_calls = 32
        document = self.document("原文没有相关线索，内容需要读完。" * 700)
        async def no_match(payload, options):
            return encode({"chunk_ids": []})
        self.model.handler = no_match
        self.assertEqual(await self.processor.select(document, "一个无关问题", self.active), [])
        seen = [item["id"] for messages, _ in self.model.calls
                for item in json.loads(messages[-1]["content"])["items"]]
        self.assertEqual(seen, [chunk.id for chunk in document.chunks])

    async def test_selection_rejects_invalid_ids_duplicates_and_more_than_three(self):
        document = self.document()
        for selected in (["unknown"], [document.chunks[0].id] * 2,
                         [chunk.id for chunk in document.chunks[:4]]):
            async def answer(payload, options):
                return encode({"chunk_ids": selected})
            self.model.handler = answer
            with self.subTest(selected=selected), self.assertRaises(ToolError) as raised:
                await self.processor.select(document, "找证据", self.active)
            self.assertEqual(raised.exception.code, "document_analysis_invalid")

    async def test_budget_fails_before_submitting_oversized_input(self):
        document = self.document()
        self.config.input_token_budget = 30
        with self.assertRaises(ToolError) as raised:
            await self.processor.prepare(document, self.active)
        self.assertEqual(raised.exception.code, "document_input_budget")
        self.assertFalse(self.model.calls)

    async def test_call_budget_preflight_does_not_process_only_start(self):
        self.config.input_token_budget = 10000
        self.config.max_model_calls = 1
        document = self.document("长篇中文内容。" * 3000)
        with self.assertRaises(ToolError) as raised:
            await self.processor.prepare(document, self.active)
        self.assertEqual(raised.exception.code, "document_call_budget")
        self.assertFalse(self.model.calls)

    def test_estimate_counts_utf8_and_message_overhead(self):
        messages = [{"role": "user", "content": "汉字" * 100}]
        self.assertGreater(estimate_input_tokens(messages), 600)
        self.assertGreater(estimate_input_tokens(messages), len(encode(messages)))

    def test_directory_has_no_arbitrary_twelve_item_limit(self):
        ids = [f"c{i:04}" for i in range(30)]
        value = {"summary": "完整目录", "outline": [{"title": "段", "summary": "摘要", "chunk_ids": [id]}
                                                for id in ids],
                 "key_points": [], "covered_chunk_ids": ids, "complete": True}
        self.assertIs(validate_analysis(value, ids), value)

    def test_soft_length_targets_allow_complete_text_without_truncation(self):
        value = {"summary": "述" * 901,
                 "outline": [{"title": "有条件的观点", "summary": "条" * 205, "chunk_ids": ["c0001"]}],
                 "key_points": [{"text": "限" * 300, "chunk_ids": ["c0001"]}],
                 "covered_chunk_ids": ["c0001"], "complete": True}
        self.assertIs(validate_analysis(value, ["c0001"]), value)
        self.assertEqual(len(value["summary"]), 901)
        self.assertEqual(len(value["outline"][0]["summary"]), 205)
        self.assertEqual(len(value["key_points"][0]["text"]), 300)

    def test_hard_length_limits_still_reject_excess_without_repair(self):
        value = {"summary": "述" * 2000,
                 "outline": [{"title": "题" * 80, "summary": "条" * 500, "chunk_ids": ["c0001"]}],
                 "key_points": [{"text": "限" * 600, "chunk_ids": ["c0001"]}],
                 "covered_chunk_ids": ["c0001"], "complete": True}
        self.assertIs(validate_analysis(value, ["c0001"]), value)
        for location, field in (("root", "summary"), ("outline", "title"),
                                ("outline", "summary"), ("key_points", "text")):
            changed = deepcopy(value)
            target = changed if location == "root" else changed[location][0]
            target[field] += "多"
            before = deepcopy(changed)
            with self.subTest(location=location, field=field), self.assertRaises(ToolError) as raised:
                validate_analysis(changed, ["c0001"])
            self.assertEqual(raised.exception.code, "document_analysis_invalid")
            self.assertEqual(changed, before)


if __name__ == "__main__":
    unittest.main()
