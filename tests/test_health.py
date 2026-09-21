import asyncio
from pathlib import Path
import tempfile
import unittest

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.history_tools import ChatArchive
from atri_bot.storage import read_jsonl
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig
from tests.support.factories import ROOT, daytime, raw
from tests.support.models import RecordingModel


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = Config(ROOT, self.data_dir, groups=frozenset({"1"}), self_id="99",
                             reply=ReplyConfig(mode="at_only"))
        self.model = RecordingModel()
        self.clock = daytime()
        self.bot = Bot(self.config, self.model, now=lambda: self.clock)
        self.addAsyncCleanup(self.bot.close, timeout=.1)
        self.sent = []

    async def send(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt("sent", str(100 + len(self.sent)))

    def submit(self, **kwargs):
        kwargs.setdefault("mention", False)
        kwargs.setdefault("text", "/health")
        return self.bot.enqueue(Event.parse(raw(**kwargs)), self.send)

    async def test_command_and_response_never_enter_chat_or_search_even_after_restart(self):
        self.assertEqual((await self.submit()).status, "sent")
        group = self.bot.group("1")
        self.assertFalse(group.history)
        self.assertFalse(group.activity)
        self.assertIsNone(group.last_sent)
        self.assertFalse(group.sent_message_ids)
        self.assertFalse(self.model.prompts)
        self.assertFalse(self.bot.queues)
        self.assertIn("未主动探测", str(self.sent))
        self.assertEqual([row["kind"] for row in read_jsonl(group.path)],
                         ["command", "command_delivery", "command_delivery"])
        archive = ChatArchive(group.path, group_id="1", self_id="99", now=group.now(), exclude_key="")
        for query in ("/health", "ATRI", "未主动探测"):
            result = await archive.run("search", {"query": query})
            self.assertEqual(result.data["items"], [])
        await self.bot.close()
        self.bot = Bot(self.config, self.model, now=daytime)
        self.addAsyncCleanup(self.bot.close, timeout=.1)
        self.assertEqual((await self.submit()).status, "duplicate")
        self.assertFalse(self.bot.group("1").history)
        await self.submit(mid=2, text="现在聊点别的", mention=True)
        self.assertNotIn("/health", str(self.model.prompts))
        self.assertNotIn("未主动探测", str(self.model.prompts))

    async def test_duplicate_commands_send_only_once_and_do_not_trigger_repetition(self):
        first = self.submit()
        duplicate = self.submit()
        self.assertEqual((await duplicate).status, "duplicate")
        await first
        await self.submit(mid=2, uid=3)
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(self.bot.repetition.current)
        self.assertFalse(self.model.prompts)

    async def test_command_works_at_night_and_respects_allowlist(self):
        self.clock = self.clock.replace(hour=2)
        self.assertEqual((await self.submit()).status, "sent")
        for kwargs in ({"gid": 2}, {"self_id": 88}, {"uid": 99}):
            self.assertEqual((await self.submit(mid=2, **kwargs)).status, "ignored")
        self.assertEqual(len(self.sent), 1)

    async def test_exact_plain_text_command_only(self):
        await self.submit(text="/health extra")
        await self.submit(mid=2, text="看看 /health")
        data = raw(mid=3, text="/health", mention=False)
        data["message"].append({"type": "image", "data": {"file": "picture"}})
        await self.bot.enqueue(Event.parse(data), self.send)
        self.assertFalse(self.sent)
        self.assertEqual(sum(row["kind"] == "incoming" for row in read_jsonl(self.bot.group("1").path)), 3)

    async def test_health_does_not_wait_for_busy_model(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(messages, **kwargs):
            entered.set()
            await release.wait()
            return "正常聊天回复"
        self.model.complete = complete
        normal = self.submit(text="你好", mention=True)
        await asyncio.wait_for(entered.wait(), 1)
        try:
            self.assertEqual((await asyncio.wait_for(self.submit(mid=2), .2)).status, "sent")
            self.assertFalse(normal.done())
            self.assertIsNone(self.bot.group("1").last_sent)
        finally:
            release.set()
            await normal
        self.assertEqual(len(self.sent), 2)

    async def test_dead_worker_is_reported_as_degraded(self):
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        self.bot.tasks["1"] = task
        await self.submit()
        self.assertIn("服务：异常", str(self.sent))
        self.assertIn("异常群任务：1", str(self.sent))
        self.assertEqual(self.bot.health_status()["status"], "degraded")

    async def test_failed_send_and_shutdown_do_not_leak_chat_or_tasks(self):
        self.config.action_timeout = .01
        async def blocked(gid, parts):
            await asyncio.Event().wait()
        self.send = blocked
        self.assertEqual((await self.submit()).status, "unknown")
        self.assertFalse(self.bot.group("1").history)
        command = self.submit(mid=2)
        await asyncio.sleep(0)
        await self.bot.close(timeout=.01)
        self.assertTrue(command.done())
        self.assertFalse(self.bot.command_tasks)
        self.assertFalse(self.bot.group("1").history)
