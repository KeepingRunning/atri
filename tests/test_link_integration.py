import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.context import build_snapshot, build_conversation
from atri_bot.types import Event, Receipt, link_references
from tests.support.factories import ROOT, action, call, daytime, raw
from tests.support.models import PlanningModel


class SharedLinkTests(unittest.TestCase):
    def test_text_after_prompt_clipping_and_json_share_card(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD?p=2"
        event = Event.parse(raw(text="长消息" * 1500 + " " + url))
        snapshot = build_snapshot([event], [], now=daytime().timestamp(), links_enabled=True)
        row = snapshot.data["pending"][0]
        self.assertTrue(row["text_truncated"])
        self.assertNotIn(url, row["text"])
        self.assertEqual(row["links"], [{"url": url}])
        parts = ({"type": "json", "data": {"data": json.dumps({"meta": {"detail_1": {
            "qqdocurl": "https://b23.tv/aBc123", "preview": "https://image.example/cover.jpg"}}})}},)
        event = replace(event, parts=parts, text="[json]")
        snapshot = build_snapshot([event], [], now=daytime().timestamp(), links_enabled=True)
        self.assertEqual(snapshot.data["pending"][0]["links"], [{"url": "https://b23.tv/aBc123"}])
        legacy = build_conversation("人设", event, [], links_enabled=True)
        self.assertIn("https://b23.tv/aBc123", legacy[-1]["content"])
        self.assertNotIn("image.example", snapshot.encoded)

    def test_xml_cards_escaped_urls_and_untrusted_hosts(self):
        parts = [{"type": "xml", "data": {"data": '<item url="https://mp.weixin.qq.com/s?__biz=a&amp;mid=1&amp;idx=1&amp;sn=b"/>'}},
                 {"type": "image", "data": {"url": "https://b23.tv/not-a-shared-link"}}]
        self.assertEqual(link_references(parts)["links"], [{"url": "https://mp.weixin.qq.com/s?__biz=a&mid=1&idx=1&sn=b"}])
        text = "https://mp.weixin.qq.com.evil.test/s/a https://user:secret@mp.weixin.qq.com/s/a"
        self.assertEqual(link_references([], text), {})
        self.assertEqual(link_references([{"type": "json", "data": {"data": "invalid json"}}]), {})

    def test_links_cannot_exceed_snapshot_budget_and_disabled_feature_has_no_card_urls(self):
        text = " ".join("https://mp.weixin.qq.com/s/" + c * 1900 for c in "abc")
        event = Event.parse(raw(text=text))
        snapshot = build_snapshot([event], [], now=daytime().timestamp(), links_enabled=True, max_chars=2000)
        self.assertLessEqual(len(snapshot.encoded), 2000)
        self.assertTrue(snapshot.data["pending"][0]["links_truncated"])
        snapshot = build_snapshot([event], [], now=daytime().timestamp())
        self.assertNotIn("links", snapshot.data["pending"][0])


class LinkConfigurationTests(unittest.TestCase):
    def test_template_can_enable_links_without_model_or_qq_credentials(self):
        text = (ROOT / "config.toml.template").read_text()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text.replace("enabled = false", "enabled = true")
                            .replace("[asr]\nenabled = true", "[asr]\nenabled = false"))
            config = Config.load(path)
            self.assertTrue(config.links.enabled)
            self.assertTrue(config.mcp.enabled)
            self.assertEqual(config.mcp.servers["website2markdown"].allowed_tools, ["convert_url"])
            self.assertFalse(config.api_key)

    def test_invalid_or_incomplete_link_configuration_fails_early(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for text in ("[links]\nenabled=true", "[links]\nenabled=true\n[mcp]\nenabled=true",
                         "[links]\ntimeout=nan", "[links]\ncache_ttl_seconds=true",
                         "[links]\ntypo=1", "[mcp]\nstartup_timeout=0"):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(ValueError):
                    Config.load(path)


class LinkFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_reads_link_and_replyer_receives_evidence_without_archiving_document(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.load(ROOT / "config.toml.template")
            config.data = Path(directory)
            config.groups, config.self_id = frozenset({"1"}), "99"
            config.links.enabled = config.mcp.enabled = True
            config.planner.debounce_seconds = .01
            config.planner.max_batch_seconds = .04
            config.schedule.sleep_enabled = False
            url = "https://mp.weixin.qq.com/s/example"
            model = PlanningModel()
            model.steps = [{"role": "assistant", "content": None,
                            "tool_calls": [call("read_link", {"url": url})]},
                           action(reference_facts=[{"source_id": "tool:call_1", "text": "证据中的事实"}])]
            bot = Bot(config, model, now=daytime)
            document = "# 示例文章\n正文仅留在工具观察里。"
            bot.mcp.call = AsyncMock(return_value={"content": [{"type": "text", "text": document}], "isError": False})
            sent = []
            async def send(gid, parts):
                sent.append(parts)
                return Receipt("sent", "100")
            try:
                receipt = await asyncio.wait_for(bot.enqueue(Event.parse(raw(text="看看 " + url)), send), 2)
                self.assertEqual(receipt.status, "sent")
                self.assertEqual(len(sent), 1)
                bot.mcp.call.assert_awaited_once_with("website2markdown", "convert_url", {"url": url, "format": "markdown"})
                reply = json.loads(model.replies[0][1]["content"])
                self.assertEqual(reply["observations"][0]["result"]["data"]["text"], document)
                self.assertTrue(reply["observations"][0]["result"]["meta"]["untrusted"])
                self.assertIn("read_link", model.plans[0][0]["content"])
                self.assertNotIn("正文仅留在工具观察里", bot.group("1").path.read_text())
            finally:
                await bot.close(timeout=.1)
