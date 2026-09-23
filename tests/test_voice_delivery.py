import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.storage import GroupLog, read_jsonl
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig
from tests.support.factories import ROOT, daytime, raw
from tests.support.models import RecordingModel
from tests.support.stickers import FakeStickerLibrary, FakeVoiceLibrary, VOICE


class VoiceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = daytime()
        self.config = Config(ROOT, self.directory, groups=frozenset({"1"}), self_id="99",
                             reply=ReplyConfig(mode="planner"))
        self.config.voices.enabled = self.config.stickers.enabled = True
        self.voice_library = FakeVoiceLibrary()
        with patch("atri_bot.bot.VoiceLibrary", return_value=self.voice_library), \
                patch("atri_bot.bot.StickerLibrary", return_value=FakeStickerLibrary()):
            self.bot = Bot(self.config, RecordingModel(), now=lambda: self.clock)
        self.addAsyncCleanup(self.bot.close, timeout=.1)
        self.group = self.bot.group("1")
        self.group.now = lambda: self.clock.timestamp()
        self.sent = []
        self.event = Event.parse(raw())

    async def send(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt("sent", f"out-{len(self.sent)}")

    async def parent(self):
        return await self.bot.deliver_reply(self.event, self.send, "这点事情难不倒我。")

    async def voice(self, **kwargs):
        return await self.bot.deliver_reply(self.event, self.send, "", voice_id="V0001",
            delivery_origin="voice_supplement", parent_message_id="out-1", **kwargs)

    async def test_record_is_independent_and_only_confirmed_content_enters_history(self):
        await self.parent()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(gid, parts):
            self.sent.append((gid, parts))
            entered.set()
            await finish.wait()
            return Receipt("sent", "out-2")
        self.send = delayed
        task = asyncio.create_task(self.voice())
        await asyncio.wait_for(entered.wait(), 1)
        try:
            self.assertEqual(len(self.group.history), 1)
            self.assertEqual(self.sent[-1], ("1", [VOICE]))
            self.assertEqual(self.group.supplement_state()["recent_supplement_count"], 0)
        finally:
            finish.set()
        self.assertEqual((await task).status, "sent")
        self.assertEqual(len(self.group.history), 2)
        self.assertIn("日语语音", self.group.history[-1]["text"])
        self.assertEqual(self.group.last_sent["message_id"], "out-1")
        self.assertEqual(self.group.supplement_state()["recent_turns"], 1)
        self.assertEqual(self.group.supplement_state()["recent_supplement_count"], 1)
        for secret in ("base64://", "usage", "avoid", "semantic_guess"):
            self.assertNotIn(secret, self.group.path.read_text())
        restored = GroupLog(self.directory, "1", now=self.group.now)
        self.assertEqual(restored.supplement_state(), self.group.supplement_state())

    async def test_text_and_multiple_media_rejected_and_parent_required(self):
        self.assertEqual((await self.voice()).reason, "unconfirmed_voice_parent")
        await self.parent()
        for values, expected in [({"voice_id": "V0001"}, "mixed_voice_reply"),
                                 ({"voice_id": "V0001", "sticker_id": "happy"}, "mixed_media_reply")]:
            receipt = await self.bot.deliver_reply(self.event, self.send, "额外文字", **values)
            self.assertEqual(receipt.reason, expected)
        self.assertEqual(len(self.sent), 1)

    async def test_unknown_voice_blocks_another_voice_or_sticker_for_the_same_parent(self):
        await self.parent()
        async def unconfirmed(gid, parts):
            self.sent.append((gid, parts))
            return Receipt("unknown", reason="delivery_unconfirmed")
        self.send = unconfirmed
        self.assertEqual((await self.voice()).status, "unknown")
        self.assertEqual((await self.voice()).reason, "supplement_already_attempted")
        sticker = await self.bot.deliver_reply(self.event, self.send, "", sticker_id="happy",
            delivery_origin="sticker_supplement", parent_message_id="out-1")
        self.assertEqual(sticker.reason, "supplement_already_attempted")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len(self.group.history), 1)
        self.assertEqual(self.group.supplement_state()["recent_supplement_count"], 0)

    async def test_prepare_failure_does_not_change_parent_and_is_not_retried(self):
        await self.parent()
        self.voice_library.prepare = lambda _: (_ for _ in ()).throw(ValueError("bad audio"))
        self.assertEqual((await self.voice()).reason, "voice_prepare_failed")
        self.assertEqual((await self.voice()).reason, "supplement_already_attempted")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.group.last_receipts[self.event.key]["status"], "sent")

    async def test_stale_or_sleeping_after_prepare_does_not_submit(self):
        await self.parent()
        original = self.voice_library.prepare
        current = True
        def invalidate(identity):
            nonlocal current
            current = False
            return original(identity)
        self.voice_library.prepare = invalidate
        self.assertEqual((await self.voice(is_current=lambda: current)).reason, "superseded")
        def sleep(identity):
            self.clock = self.clock.replace(hour=2)
            return original(identity)
        self.voice_library.prepare = sleep
        self.assertEqual((await self.voice()).reason, "sleeping")
        self.assertEqual(len(self.sent), 1)
        deliveries = [row for row in read_jsonl(self.group.path) if row["kind"] == "delivery"]
        self.assertEqual(len(deliveries), 2)  # Parent pending + sent; sleep has a separate audit.

    async def test_late_prepare_failure_cannot_overwrite_a_concurrent_success(self):
        await self.parent()
        original = self.voice_library.prepare
        entered, finish = threading.Event(), threading.Event()
        calls = 0
        def prepare(identity):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                if not finish.wait(2):
                    raise TimeoutError("test release missing")
                raise ValueError("late decode failure")
            return original(identity)
        self.voice_library.prepare = prepare
        first = asyncio.create_task(self.voice())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.assertEqual((await self.voice()).status, "sent")
        finally:
            finish.set()
        self.assertEqual((await first).reason, "supplement_already_attempted")
        self.assertEqual(self.group.last_receipts[f"{self.event.key}:voice:out-1"]["status"], "sent")
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
