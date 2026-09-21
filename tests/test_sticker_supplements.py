import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.model import ModelError
from atri_bot.storage import GroupLog, read_jsonl
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig
from tests.support.factories import ROOT, action, daytime, raw
from tests.support.stickers import CANDIDATE, IMAGE, FakeStickerLibrary, StickerModel, supplement


class SupplementSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = daytime()
        self.config = Config(ROOT, self.data_dir, groups=frozenset({"1", "2"}), self_id="99",
                             reply=ReplyConfig(mode="planner", cooldown_seconds=0))
        self.config.stickers.enabled = True
        self.config.planner.debounce_seconds = .001
        self.config.planner.max_batch_seconds = .002
        self.library, self.model = FakeStickerLibrary(), StickerModel()
        with patch("atri_bot.bot.StickerLibrary", return_value=self.library):
            self.bot = Bot(self.config, self.model, now=lambda: self.clock)
        self.addAsyncCleanup(self.bot.close, timeout=.1)
        for gid in self.config.groups:
            self.bot.group(gid).now = lambda: self.clock.timestamp()
        self.sent = []

    async def sender(self, gid, parts):
        self.sent.append((gid, deepcopy(parts)))
        return Receipt("sent", f"out-{len(self.sent)}")

    def submit(self, **kwargs):
        return self.bot.enqueue(Event.parse(raw(**kwargs)), self.sender)

    async def drain(self):
        await asyncio.wait_for(asyncio.gather(*list(self.bot.sticker_supplements.tasks), return_exceptions=True), 2)

    def rows(self, gid="1"):
        return list(read_jsonl(self.bot.group(gid).path))

    async def test_separate_receipts_history_and_one_logical_turn(self):
        self.assertEqual(await self.submit(), Receipt("sent", "out-1"))
        await self.drain()
        self.assertEqual([[p["type"] for p in parts] for _, parts in self.sent], [["text"], ["image"]])
        group = self.bot.group("1")
        self.assertEqual(group.last_receipts["99:1:1"]["message_id"], "out-1")
        self.assertEqual(group.last_receipts["99:1:1:sticker:out-1"]["message_id"], "out-2")
        self.assertEqual(group.last_sent["message_id"], "out-1")
        self.assertEqual([r["message_id"] for r in group.history if r.get("role") == "assistant"], ["out-1", "out-2"])
        self.assertEqual(group.sticker_state()["recent_turns"], 1)
        self.assertEqual(group.sticker_state()["recent_sticker_count"], 1)
        self.assertEqual(sum(own for _, own in group.activity), 1)
        data = json.loads(self.model.sticker_plans[0][0][1]["content"])
        self.assertEqual(data["sent_reply"]["text"], self.sent[0][1][0]["data"]["text"])
        main_snapshot = json.loads(self.model.plans[0][1]["content"])["snapshot"]
        self.assertEqual(data["snapshot"], main_snapshot)
        self.assertNotIn("stickers", main_snapshot)
        for definitions in self.model.main_definitions:
            names = {d["function"]["name"] for d in definitions}
            self.assertIn("reply", names)
            self.assertFalse(names & {"prepare_reply", "send_sticker", "search_stickers", "supplement_sticker"})
        self.assertNotIn("selected_sticker", json.dumps(self.model.replies))
        restored = GroupLog(self.config.data, "1", now=lambda: self.clock.timestamp())
        self.assertEqual(restored.sticker_state(), group.sticker_state())
        self.assertEqual(restored.last_sent["message_id"], "out-1")

    async def test_waits_for_text_confirmation_but_does_not_block_main_result(self):
        sent, ack, planning, finish = (asyncio.Event() for _ in range(4))
        original = self.sender
        async def sender(gid, parts):
            if parts[0]["type"] == "text":
                sent.set()
                await ack.wait()
            return await original(gid, parts)
        self.sender = sender
        async def slow(_):
            planning.set()
            await finish.wait()
            return supplement()
        self.model.sticker_steps = [slow]
        future = self.submit()
        await asyncio.wait_for(sent.wait(), 1)
        self.assertFalse(self.model.sticker_plans)
        ack.set()
        self.assertEqual((await asyncio.wait_for(future, 1)).status, "sent")
        await asyncio.wait_for(planning.wait(), 1)
        self.assertEqual(len(self.sent), 1)
        finish.set()
        await self.drain()
        self.assertEqual(len(self.sent), 2)

    async def test_unconfirmed_text_never_triggers_subplanner(self):
        for i, status in enumerate(("failed", "unknown"), 1):
            async def sender(gid, parts):
                return Receipt(status, reason="test")
            self.sender = sender
            self.assertEqual((await self.submit(mid=i)).status, status)
            await self.drain()
        self.assertFalse(self.model.sticker_plans)

    async def test_observe_health_repetition_and_disabled_do_not_schedule_supplements(self):
        self.model.steps = [action("observe")]
        self.assertEqual((await self.submit()).reason, "planner_observe")
        await self.submit(mid=2, text="/health", mention=False)
        with patch.object(self.bot.repetition, "claim", return_value="复读"):
            await self.bot.repeat(Event.parse(raw(mid=3)), self.sender)
        self.config.stickers.enabled = False
        await self.submit(mid=4)
        await self.drain()
        self.assertFalse(self.model.sticker_plans)

    async def test_skip_and_planning_failure_leave_text_untouched(self):
        for i, steps in enumerate(([supplement(None)], [ModelError("failure")] * 3), 1):
            self.model.sticker_steps = steps
            self.assertEqual((await self.submit(mid=i)).status, "sent")
            await self.drain()
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(parts[0]["type"] == "text" for _, parts in self.sent))
        self.assertEqual(self.bot.group("1").sticker_state()["recent_turns"], 2)

    async def test_prepare_failure_does_not_undo_or_repeat_text(self):
        self.library.prepare = lambda _: (_ for _ in ()).throw(ValueError("bad image"))
        self.assertEqual((await self.submit()).status, "sent")
        await self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.bot.group("1").last_receipts["99:1:1"]["status"], "sent")
        self.assertTrue(any(r.get("reason") == "sticker_prepare_failed" for r in self.rows()))

    async def test_unknown_image_does_not_change_cadence_or_text_receipt(self):
        original = self.sender
        async def sender(gid, parts):
            if parts[0]["type"] == "image":
                self.sent.append((gid, parts))
                return Receipt("unknown", reason="unconfirmed")
            return await original(gid, parts)
        self.sender = sender
        await self.submit()
        await self.drain()
        group = self.bot.group("1")
        self.assertEqual(group.last_receipts["99:1:1"]["status"], "sent")
        self.assertEqual(group.last_receipts["99:1:1:sticker:out-1"]["status"], "unknown")
        self.assertEqual(group.sticker_state()["turns_since_last_sticker"], 1)
        self.assertEqual(sum(r.get("role") == "assistant" for r in group.history), 1)
        self.assertEqual(len(self.sent), 2)

    async def test_new_message_cancels_planning_without_blocking_next_reply(self):
        entered = asyncio.Event()
        async def slow(_):
            entered.set()
            await asyncio.Event().wait()
        self.model.sticker_steps = [slow, supplement(None)]
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        old = next(iter(self.bot.sticker_supplements.tasks))
        self.assertEqual((await asyncio.wait_for(self.submit(mid=2, uid=3), 1)).status, "sent")
        await self.drain()
        self.assertTrue(old.cancelled())
        self.assertEqual([parts[0]["type"] for _, parts in self.sent], ["text", "text"])
        self.assertTrue(any(r.get("kind") == "sticker_plan" and r.get("reason") == "new_message" for r in self.rows()))

    async def test_input_during_text_ack_prevents_stale_supplement(self):
        entered, ack = asyncio.Event(), asyncio.Event()
        original = self.sender
        async def sender(gid, parts):
            if not self.sent:
                entered.set()
                await ack.wait()
            return await original(gid, parts)
        self.sender = sender
        first = self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        self.model.steps = [action("observe")]
        second = self.submit(mid=2, uid=3)
        ack.set()
        await asyncio.gather(first, second)
        await self.drain()
        self.assertFalse(self.model.sticker_plans)

    async def test_new_message_during_preparation_prevents_late_image(self):
        entered, release = threading.Event(), threading.Event()
        original = self.library.prepare
        def slow(identity):
            entered.set()
            release.wait(2)
            return original(identity)
        self.library.prepare = slow
        self.model.sticker_steps = [supplement(), supplement(None)]
        await self.submit()
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await self.submit(mid=2)
            await self.drain()
            self.assertEqual(len(self.sent), 2)
            self.assertTrue(all(parts[0]["type"] == "text" for _, parts in self.sent))
        finally:
            release.set()

    async def test_timeout_releases_global_slot(self):
        self.config.stickers.max_age_seconds = .03
        async def slow(_):
            await asyncio.Event().wait()
        self.model.sticker_steps = [slow]
        await self.submit()
        await self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.bot.semaphore._value, self.config.parallel)
        self.assertTrue(any(r.get("reason") == "expired" for r in self.rows()))

    async def test_waiting_for_model_slot_is_included_in_deadline(self):
        self.config.stickers.max_age_seconds = .03
        # Main text is allowed through; the optional branch then has no model slot.
        original = self.sender
        async def sender(gid, parts):
            receipt = await original(gid, parts)
            for _ in range(self.config.parallel):
                await self.bot.semaphore.acquire()
            return receipt
        self.sender = sender
        try:
            await self.submit()
            await self.drain()
            self.assertFalse(self.model.sticker_plans)
            self.assertEqual(len(self.sent), 1)
            self.assertTrue(any(r.get("reason") == "expired" for r in self.rows()))
        finally:
            for _ in range(self.config.parallel):
                self.bot.semaphore.release()

    async def test_preparation_timeout_cannot_send_later_from_worker_thread(self):
        self.config.stickers.max_age_seconds = .03
        entered, release = threading.Event(), threading.Event()
        original = self.library.prepare
        def slow(identity):
            entered.set()
            release.wait(2)
            return original(identity)
        self.library.prepare = slow
        await self.submit()
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await self.drain()
            self.assertEqual(len(self.sent), 1)
            self.assertTrue(any(r.get("reason") == "expired" for r in self.rows()))
        finally:
            release.set()

    async def test_submission_ends_freshness_deadline_but_keeps_receipt_wait(self):
        self.config.stickers.max_age_seconds = .03
        entered, ack = asyncio.Event(), asyncio.Event()
        original = self.sender
        async def sender(gid, parts):
            if parts[0]["type"] == "image":
                entered.set()
                await ack.wait()
            return await original(gid, parts)
        self.sender = sender
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.06)
        self.assertTrue(self.bot.sticker_supplements.tasks)
        ack.set()
        await self.drain()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.bot.group("1").sticker_state()["recent_sticker_count"], 1)

    async def test_pending_recovery_preserves_parent_and_never_replaces_text_receipt(self):
        self.config.stickers.enabled = False
        await self.submit()
        group = self.bot.group("1")
        key = "99:1:1:sticker:out-1"
        group.append({"kind": "delivery", "key": key, "turn_id": "99:1:1", "text": "",
                      "delivery_origin": "sticker_supplement", "parent_message_id": "out-1",
                      "status": "pending", "sticker": CANDIDATE})
        restored = GroupLog(self.config.data, "1", now=lambda: self.clock.timestamp())
        self.assertEqual(restored.last_receipts["99:1:1"]["status"], "sent")
        self.assertEqual(restored.last_receipts[key]["status"], "unknown")
        self.assertEqual(restored.last_receipts[key]["parent_message_id"], "out-1")
        self.assertEqual(restored.sticker_state()["recent_turns"], 1)
        self.assertEqual(restored.sticker_state()["recent_sticker_count"], 0)

    async def test_sleep_after_text_blocks_picture(self):
        async def enter_sleep(_):
            self.clock = self.clock.replace(hour=2)
            return supplement()
        self.model.sticker_steps = [enter_sleep]
        await self.submit()
        await self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(any(r.get("reason") == "sleeping" for r in self.rows()))

    async def test_other_group_and_health_do_not_cancel_supplement(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def slow(_):
            entered.set()
            await finish.wait()
            return supplement()
        self.model.sticker_steps = [slow, supplement(None)]
        await self.submit(gid=1)
        await asyncio.wait_for(entered.wait(), 1)
        await self.submit(mid=2, gid=2)
        await self.submit(mid=3, text="/health", mention=False)
        finish.set()
        await self.drain()
        self.assertEqual(self.bot.group("1").sticker_state()["recent_sticker_count"], 1)
        self.assertEqual(self.bot.group("2").sticker_state()["recent_sticker_count"], 0)
        self.assertEqual(self.sent[-1], ("1", [IMAGE]))

    async def test_submitted_image_receipt_attaches_to_parent_despite_newer_reply(self):
        entered, ack = asyncio.Event(), asyncio.Event()
        original = self.sender
        async def sender(gid, parts):
            if parts[0]["type"] == "image":
                self.sent.append((gid, deepcopy(parts)))
                entered.set()
                await ack.wait()
                return Receipt("sent", "image-old")
            return await original(gid, parts)
        self.sender = sender
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        self.clock += timedelta(seconds=2)
        second = await self.submit(mid=2)
        self.assertEqual(len(self.model.sticker_plans), 1)
        ack.set()
        await self.drain()
        group = self.bot.group("1")
        self.assertEqual(group.last_sent["message_id"], second.message_id)
        self.assertEqual(group.sticker_state()["recent_turns"], 2)
        self.assertEqual(group.sticker_state()["recent_sticker_count"], 1)
        self.assertEqual(group.sticker_state()["turns_since_last_sticker"], 1)
        self.assertIn("image-old", group.sent_message_ids)
        restored = GroupLog(self.config.data, "1", now=lambda: self.clock.timestamp())
        self.assertEqual(restored.sticker_state(), group.sticker_state())

    async def test_shutdown_cancels_planning(self):
        entered = asyncio.Event()
        async def slow(_):
            entered.set()
            await asyncio.Event().wait()
        self.model.sticker_steps = [slow]
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        await self.bot.close(timeout=.1)
        self.assertFalse(self.bot.sticker_supplements.tasks)
        self.assertEqual(len(self.sent), 1)
