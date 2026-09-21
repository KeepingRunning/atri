import asyncio
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.history_tools import ChatArchive
from atri_bot.storage import GroupLog, read_jsonl
from atri_bot.stickers import StickerLibrary
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig
from tests.support.factories import ROOT, daytime, raw
from tests.support.models import RecordingModel


IMAGE = {"type": "image", "data": {"file": "base64://c3RpY2tlci1ieXRlcw=="}}
METADATA = {"id": "happy", "title": "开心", "description": "亚托莉笑着举起双手。",
            "visible_text": ["好耶！"], "usage": "不要把用途写入历史", "file": "/private/stickers/happy.png"}


class TestLibrary:
    def __init__(self):
        self.prepared = []

    def prepare(self, sticker_id):
        self.prepared.append((sticker_id, threading.get_ident()))
        if sticker_id != "happy":
            raise FileNotFoundError("/private/stickers/missing.png")
        return IMAGE, METADATA


class StickerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = daytime()
        self.config = Config(ROOT, self.data_dir, groups=frozenset({"1", "2"}), self_id="99",
                             reply=ReplyConfig(mode="planner"))
        self.config.stickers.enabled = True
        self.library = TestLibrary()
        with patch("atri_bot.bot.StickerLibrary", return_value=self.library):
            self.bot = Bot(self.config, RecordingModel(), now=lambda: self.clock)
        self.addAsyncCleanup(self.bot.close, timeout=.1)
        for gid in self.config.groups:
            self.bot.group(gid).now = lambda: self.clock.timestamp()
        self.sent = []

    async def send(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt("sent", str(100 + len(self.sent)))

    async def deliver(self, text="", *, mid=1, gid=1, sticker_id="happy", **kwargs):
        return await self.bot.deliver_reply(Event.parse(raw(mid=mid, gid=gid)), self.send, text,
                                           sticker_id=sticker_id, **kwargs)

    async def test_image_only_waits_for_receipt_and_records_visible_context(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def acknowledge_later(gid, parts):
            self.sent.append((gid, parts))
            entered.set()
            await finish.wait()
            return Receipt("sent", "101")
        self.send = acknowledge_later
        task = asyncio.create_task(self.deliver())
        await asyncio.wait_for(entered.wait(), 1)
        group = self.bot.group("1")
        try:
            self.assertFalse(group.history)
            self.assertEqual(group.sticker_state()["recent_turns"], 0)
            self.assertEqual(group.last_receipts["99:1:1"]["status"], "pending")
        finally:
            finish.set()
        self.assertEqual((await task).status, "sent")
        self.assertEqual(self.sent, [("1", [IMAGE])])
        self.assertNotEqual(self.library.prepared[0][1], threading.get_ident())
        self.assertEqual(group.last_sent["message_id"], "101")
        history = group.history[-1]
        self.assertEqual(history["role"], "assistant")
        self.assertIn("[表情 happy：亚托莉笑着举起双手。；图中文字：好耶！]", history["text"])
        state = group.sticker_state()
        self.assertEqual(state["recent_turns"], 1)
        self.assertEqual(state["recent_sticker_count"], 1)
        self.assertEqual(state["turns_since_last_sticker"], 0)
        self.assertEqual(state["last_sticker_id"], "happy")
        archive = ChatArchive(group.path, group_id="1", self_id="99", now=self.clock.timestamp(), exclude_key="")
        result = await archive.run("search", {"query": "好耶"})
        self.assertEqual(result.data["items"][0]["text"], history["text"])
        events = await archive.run("events", {"message_id": "101"})
        self.assertEqual(events.data["items"][0]["sticker"]["id"], "happy")
        for content in (group.path.read_text(), json.dumps(list(group.history)), result.to_json(), events.to_json()):
            for secret in ("base64://", "c3RpY2tlci1ieXRlcw==", "/private/stickers", "usage"):
                self.assertNotIn(secret, content)

    async def test_text_is_separate_and_mixed_payload_is_rejected(self):
        text = "好，知道了。OK, thanks."
        expected = {"type": "text", "data": {"text": "好，，，知道了)OK，，， thanks."}}
        self.assertEqual((await self.deliver(text)).reason, "mixed_sticker_reply")
        self.assertFalse(self.sent)
        receipt = await self.deliver(text, sticker_id=None)
        await self.deliver(parent_message_id=receipt.message_id, delivery_origin="sticker_supplement")
        self.assertEqual(self.sent, [("1", [expected]), ("1", [IMAGE])])
        self.assertEqual(self.bot.group("1").sticker_state()["recent_turns"], 1)

    async def test_real_library_image_reaches_sender_and_visible_text_reaches_history(self):
        directory = self.data_dir / "stickers"
        directory.mkdir()
        path = directory / "happy.png"
        Image.new("RGB", (8, 8), "red").save(path)
        raw_image = path.read_bytes()
        row = {**METADATA, "file": "happy.png", "sha256": hashlib.sha256(raw_image).hexdigest(),
               "emotions": ["高兴"], "intensity": 2, "usage": ["庆祝"], "avoid": [],
               "animation_summary": "", "needs_review": False}
        (directory / "catalog.json").write_text(json.dumps({"items": [row]}, ensure_ascii=False))
        self.config.stickers.catalog = "stickers/catalog.json"
        self.bot.stickers = StickerLibrary(self.data_dir, self.config.stickers)
        self.assertEqual((await self.deliver()).status, "sent")
        payload = self.sent[0][1][0]["data"]["file"]
        self.assertEqual(base64.b64decode(payload.removeprefix("base64://")), raw_image)
        history = self.bot.group("1").history[-1]
        self.assertEqual(history["sticker"]["visible_text"], ["好耶！"])
        self.assertIn("图中文字：好耶！", history["text"])
        self.assertNotIn("usage", history["sticker"])

    async def test_prepare_error_never_falls_back_to_text_or_logs_path(self):
        receipt = await self.deliver(sticker_id="missing")
        self.assertEqual(receipt, Receipt("failed", reason="sticker_prepare_failed"))
        self.assertFalse(self.sent)
        group = self.bot.group("1")
        self.assertFalse(group.history)
        self.assertEqual(group.sticker_state()["recent_turns"], 0)
        rows = list(read_jsonl(group.path))
        self.assertEqual([row["status"] for row in rows], ["failed"])
        self.assertNotIn("/private/stickers", group.path.read_text())

    async def test_failed_unknown_and_timeout_never_advance_history_or_frequency(self):
        for mid, status in enumerate(("failed", "unknown", "timeout"), 1):
            with self.subTest(status=status):
                async def failed_sender(gid, parts):
                    self.sent.append((gid, parts))
                    if status == "timeout":
                        await asyncio.Event().wait()
                    return Receipt(status, reason="simulated")
                self.send = failed_sender
                self.config.action_timeout = .01
                receipt = await self.deliver(mid=mid)
                self.assertEqual(receipt.status, "unknown" if status == "timeout" else status)
        group = self.bot.group("1")
        self.assertEqual(len(self.sent), 3)
        self.assertFalse(group.history)
        self.assertIsNone(group.last_sent)
        self.assertEqual(group.sticker_state()["recent_turns"], 0)

    async def test_sleep_or_snapshot_change_during_prepare_prevents_all_submission(self):
        original = self.library.prepare
        def enter_sleep(sticker_id):
            self.clock = self.clock.replace(hour=2)
            return original(sticker_id)
        self.library.prepare = enter_sleep
        self.assertEqual((await self.deliver()).reason, "sleeping")
        self.clock = self.clock.replace(hour=12)
        current = True
        def invalidate(sticker_id):
            nonlocal current
            current = False
            return original(sticker_id)
        self.library.prepare = invalidate
        self.assertEqual((await self.deliver(mid=2, is_current=lambda: current)).reason, "superseded")
        self.assertFalse(self.sent)
        self.assertFalse(self.bot.group("1").history)
        self.assertEqual(self.bot.group("1").sticker_state()["recent_turns"], 0)

    async def test_frequency_recovers_deduplicates_and_excludes_repetition_and_health(self):
        await self.deliver("普通回复", sticker_id=None)
        await self.deliver(mid=2)
        await self.deliver("下一轮", mid=3, sticker_id=None)
        await self.deliver("再一轮", mid=4, sticker_id=None)
        event = Event.parse(raw(mid=5))
        with patch.object(self.bot.repetition, "claim", return_value="自动复读"):
            await self.bot.repeat(event, self.send)
        await self.bot.health_command(Event.parse(raw(mid=6, text="/health")), self.send)
        await self.deliver("另一群", mid=1, gid=2, sticker_id=None)
        group = self.bot.group("1")
        successful = next(row for row in read_jsonl(group.path)
                          if row.get("status") == "sent" and row.get("sticker"))
        group.append({**successful, "key": "99:1:duplicate"})
        expected = {"turns_since_last_sticker": 2, "recent_turns": 4,
                    "recent_sticker_count": 1, "recent_sticker_ids": ["happy"],
                    "last_sticker_id": "happy", "target_turns_min": 3, "target_turns_max": 5}
        self.assertEqual(group.sticker_state(), expected)
        self.assertEqual(sum(row.get("role") == "assistant" for row in group.history), 5)
        restored = GroupLog(self.config.data, "1", now=lambda: self.clock.timestamp())
        self.assertEqual(restored.sticker_state(), expected)
        self.assertEqual(sum(row.get("role") == "assistant" for row in restored.history), 5)
        window = restored.sticker_state(window=2, target_min=2, target_max=4)
        self.assertEqual(window["recent_turns"], 2)
        self.assertEqual(window["recent_sticker_count"], 0)
        self.assertEqual(window["turns_since_last_sticker"], 2)
        self.assertEqual((window["target_turns_min"], window["target_turns_max"]), (2, 4))
        other = GroupLog(self.config.data, "2", now=lambda: self.clock.timestamp())
        self.assertEqual(other.sticker_state()["recent_turns"], 1)
        self.assertIsNone(other.sticker_state()["last_sticker_id"])

    async def test_old_text_deliveries_count_but_pending_recovery_does_not(self):
        group = self.bot.group("1")
        group.append({"kind": "delivery", "key": "99:1:old", "status": "sent", "text": "旧聊天",
                      "message_id": "old"})
        group.append({"kind": "delivery", "key": "99:1:pending", "status": "pending", "text": "未确认",
                      "sticker": METADATA})
        restored = GroupLog(self.config.data, "1", now=lambda: self.clock.timestamp())
        self.assertEqual(restored.sticker_state()["recent_turns"], 1)
        self.assertEqual(restored.sticker_state()["turns_since_last_sticker"], 1)
        self.assertIsNone(restored.sticker_state()["last_sticker_id"])

    async def test_recent_window_is_bounded_without_losing_total_since_last_sticker(self):
        group = self.bot.group("1")
        for number in range(125):
            group.append({"kind": "delivery", "key": f"99:1:{number}", "status": "sent",
                          "text": "普通聊天", "message_id": str(number)})
        self.assertEqual(len(group._chat_turns), 100)
        state = group.sticker_state(window=100)
        self.assertEqual(state["recent_turns"], 100)
        self.assertEqual(state["turns_since_last_sticker"], 125)
        for window in (0, 101, True):
            with self.assertRaises(ValueError):
                group.sticker_state(window=window)
