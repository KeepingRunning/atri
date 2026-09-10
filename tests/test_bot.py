import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import tempfile
import unittest

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.model import ModelError
from atri_bot.storage import GroupLog, read_jsonl
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig

ROOT = Path(__file__).resolve().parents[1]


def daytime():
    return datetime(2026, 9, 11, 12, 35, tzinfo=ZoneInfo("Asia/Shanghai"))


def raw(mid=1, gid=1, text="你好", uid=2, self_id=99, mention=True):
    parts = [{"type": "text", "data": {"text": text}}]
    if mention:
        parts.insert(0, {"type": "at", "data": {"qq": str(self_id)}})
    return {"post_type": "message", "message_type": "group", "self_id": self_id,
            "group_id": gid, "user_id": uid, "message_id": mid, "message": parts,
            "sender": {"nickname": "测试群友"}}


class RecordingModel:
    def __init__(self):
        self.prompts = []

    async def complete(self, messages):
        self.prompts.append(messages)
        return "收到啦 [CQ:at,qq=all]"


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(ROOT, Path(self.tmp.name), groups=frozenset({"1", "2"}), self_id="99",
                             reply=ReplyConfig(mode="at_only"))
        self.model = RecordingModel()
        self.bot = Bot(self.config, self.model, now=daytime)
        self.sent = []

    async def asyncTearDown(self):
        await self.bot.close(timeout=.1)
        self.tmp.cleanup()

    async def send(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt("sent", str(100 + len(self.sent)))

    async def submit(self, **kwargs):
        return await self.bot.enqueue(Event.parse(raw(**kwargs)), self.send)

    async def test_only_structured_at_triggers_but_all_chat_is_recorded(self):
        for mid, text in enumerate(("ATRI 你好？", "亚托莉", "@99", "[CQ:reply,id=101]", "闲聊"), 1):
            self.assertEqual((await self.submit(mid=mid, text=text, mention=False)).status, "ignored")
        for mid, target in ((6, "all"), (7, "98")):
            data = raw(mid=mid)
            data["message"][0]["data"]["qq"] = target
            self.assertEqual((await self.bot.enqueue(Event.parse(data), self.send)).status, "ignored")
        self.assertFalse(self.model.prompts)
        self.assertEqual((await self.submit(mid=8, text="唯一当前消息")).status, "sent")
        messages = self.model.prompts[-1]
        self.assertIn(self.config.read_personal_info(), messages[0]["content"])
        self.assertEqual(len(messages), 9)
        text = json.dumps(messages, ensure_ascii=False)
        self.assertEqual(text.count("唯一当前消息"), 1)
        self.assertIn("闲聊", text)
        self.assertEqual(self.sent[0][1], [{"type": "text", "data": {"text": "收到啦 [CQ:at,qq=all]"}}])
        self.assertEqual(self.bot.group("1").history[-1]["role"], "assistant")

    async def test_cq_string_at_still_works(self):
        data = raw()
        data["message"] = "[CQ:at,qq=99]你好"
        self.assertEqual((await self.bot.enqueue(Event.parse(data), self.send)).status, "sent")

    async def test_latest_50_history_and_group_isolation(self):
        await self.submit(gid=2, text="群二秘密", mention=False)
        for mid in range(1, 61):
            await self.submit(mid=mid, text=f"历史-{mid}", mention=False)
        await self.submit(mid=61, text="当前")
        messages = self.model.prompts[-1]
        self.assertEqual(len(messages), 52)
        self.assertEqual([json.loads(row["content"])["text"] for row in messages[1:-1]],
                         [f"历史-{mid}" for mid in range(11, 61)])
        self.assertNotIn("群二秘密", str(messages))

    async def test_denied_self_and_wrong_account_are_not_logged(self):
        for args in ({"gid": 3}, {"uid": 99}, {"self_id": 88}):
            self.assertEqual((await self.submit(**args)).status, "ignored")
        self.assertFalse(self.bot.groups)

    async def test_duplicate_and_restart_restore_history(self):
        event = Event.parse(raw(text="已发过"))
        first = self.bot.enqueue(event, self.send)
        self.assertEqual((await self.bot.enqueue(event, self.send)).status, "duplicate")
        await first
        await self.bot.close()
        self.bot = Bot(self.config, self.model, now=daytime)
        self.assertEqual((await self.bot.enqueue(event, self.send)).status, "duplicate")
        await self.submit(mid=2)
        self.assertTrue(any(row["role"] == "assistant" for row in self.model.prompts[-1]))
        self.assertEqual(len(self.sent), 2)

    async def test_failed_unknown_and_model_failure_do_not_enter_history(self):
        for mid, status in enumerate(("failed", "unknown"), 1):
            async def sender(gid, parts):
                return Receipt(status)
            result = await self.bot.enqueue(Event.parse(raw(mid=mid)), sender)
            self.assertEqual(result.status, status)
        self.assertFalse(any(row.get("role") == "assistant" for row in self.bot.group("1").history))
        async def fail(messages):
            raise ModelError("Failure", "model_timeout")
        self.model.complete = fail
        self.assertEqual((await self.submit(mid=3)).reason, "model_timeout")
        self.assertFalse(self.sent)

    async def test_send_timeout_and_pending_restart_are_unknown(self):
        self.config.action_timeout = .01
        async def sender(gid, parts):
            await asyncio.Event().wait()
        result = await self.bot.enqueue(Event.parse(raw()), sender)
        self.assertEqual(result.status, "unknown")
        group = self.bot.group("1")
        group.append({"kind": "delivery", "key": "crashed", "status": "pending", "text": "未确认"})
        recovered = GroupLog(self.config.data, "1")
        self.assertEqual(recovered.last_receipts["crashed"]["status"], "unknown")
        self.assertFalse(any(row.get("role") == "assistant" for row in recovered.history))

    async def test_serial_per_group_and_bounded_parallel_across_groups(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(messages):
            self.model.prompts.append(messages)
            if len(self.model.prompts) == 2:
                entered.set()
            await release.wait()
            return "回复"
        self.model.complete = complete
        first = self.bot.enqueue(Event.parse(raw(text="第一条")), self.send)
        second = self.bot.enqueue(Event.parse(raw(mid=2, text="未来消息")), self.send)
        other = self.bot.enqueue(Event.parse(raw(gid=2)), self.send)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(len(self.model.prompts), 2)
        self.assertNotIn("未来消息", str(self.model.prompts[0]))
        release.set()
        await asyncio.gather(first, second, other)
        self.assertTrue(any(row["role"] == "assistant" for row in self.model.prompts[-1]))


class StorageTests(unittest.TestCase):
    def test_torn_tail_recovered_without_losing_complete_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "messages.jsonl"
            path.write_bytes(b'{"kind":"ok"}\n{"torn":')
            self.assertEqual(list(read_jsonl(path)), [{"kind": "ok"}])
            self.assertEqual(len(list(Path(directory).glob('*.torn-*'))), 1)
