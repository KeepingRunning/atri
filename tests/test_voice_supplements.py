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
from atri_bot.context import build_snapshot
from atri_bot.storage import GroupLog, read_jsonl
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig
from tests.support.factories import ROOT, action, daytime, raw
from tests.support.stickers import (VOICE, VOICE_CANDIDATE, FakeStickerLibrary, FakeVoiceLibrary,
                                    SupplementModel, media, supplement)


class VoiceSupplementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = daytime()
        self.config = Config(ROOT, self.data, groups=frozenset({"1", "2"}), self_id="99",
                             reply=ReplyConfig(mode="planner", cooldown_seconds=0))
        self.config.stickers.enabled = self.config.voices.enabled = True
        self.config.planner.debounce_seconds = .001
        self.config.planner.max_batch_seconds = .002
        self.stickers, self.voices, self.model = FakeStickerLibrary(), FakeVoiceLibrary(), SupplementModel()
        self.stickers.config, self.voices.config = self.config.stickers, self.config.voices
        with patch("atri_bot.bot.StickerLibrary", return_value=self.stickers), \
                patch("atri_bot.bot.VoiceLibrary", return_value=self.voices):
            self.bot = Bot(self.config, self.model, now=lambda: self.clock)
        self.addAsyncCleanup(self.bot.close, timeout=.1)
        for gid in self.config.groups:
            self.bot.group(gid).now = lambda: self.clock.timestamp()
        self.model.supplement_steps = [media()]
        self.sent = []

    async def sender(self, gid, parts):
        self.sent.append((gid, deepcopy(parts)))
        return Receipt("sent", f"out-{len(self.sent)}")

    def submit(self, **kwargs):
        return self.bot.enqueue(Event.parse(raw(**kwargs)), self.sender)

    async def drain(self):
        await asyncio.wait_for(asyncio.gather(*list(self.bot.supplements.tasks), return_exceptions=True), 2)

    def rows(self):
        return list(read_jsonl(self.bot.group("1").path))

    async def test_one_optional_voice_has_its_own_receipt_and_shared_turn(self):
        self.assertEqual(await self.submit(), Receipt("sent", "out-1"))
        await self.drain()
        self.assertEqual([parts[0]["type"] for _, parts in self.sent], ["text", "record"])
        self.assertEqual(self.sent[-1][1], [VOICE])
        self.assertEqual(len(self.model.supplement_plans), 1)
        self.assertEqual(self.stickers.prepared, [])
        self.assertEqual(self.voices.prepared, ["V0001"])
        group = self.bot.group("1")
        self.assertEqual(group.last_receipts["99:1:1"]["message_id"], "out-1")
        self.assertEqual(group.last_receipts["99:1:1:voice:out-1"]["message_id"], "out-2")
        self.assertEqual(group.last_sent["message_id"], "out-1")
        state = group.supplement_state()
        self.assertEqual((state["recent_turns"], state["recent_supplement_count"], state["recent_voice_count"]), (1, 1, 1))
        self.assertEqual(state["recent_sticker_count"], 0)
        self.assertEqual(state["turns_since_last_supplement"], 0)
        self.assertEqual(state["turns_since_last_voice"], 0)
        self.assertEqual(state["recent_voice_groups"], [VOICE_CANDIDATE["semantic_group"]])
        history = [row for row in group.history if row.get("role") == "assistant"]
        self.assertEqual([row["message_id"] for row in history], ["out-1", "out-2"])
        self.assertIn(VOICE_CANDIDATE["text_ja"], history[-1]["text"])
        self.assertIn(VOICE_CANDIDATE["text_zh"], history[-1]["text"])
        self.assertNotIn(VOICE_CANDIDATE["description"], history[-1]["text"])
        self.assertNotIn("base64://", json.dumps(self.rows()))
        decisions = [row for row in self.rows() if row.get("kind") == "supplement_plan" and row.get("stage") == "decision"]
        self.assertEqual((decisions[0]["media_kind"], decisions[0]["asset_id"]), ("voice", "V0001"))
        restored = GroupLog(self.data, "1", now=lambda: self.clock.timestamp())
        self.assertEqual(restored.supplement_state(), state)
        self.assertEqual(restored.last_sent["message_id"], "out-1")

    async def test_voice_only_configuration_still_starts_optional_planning(self):
        self.config.stickers.enabled = False
        self.bot.stickers = None
        await self.submit()
        await self.drain()
        self.assertEqual([parts[0]["type"] for _, parts in self.sent], ["text", "record"])
        candidates = json.loads(self.model.supplement_plans[0][0][1]["content"])["candidates"]
        self.assertEqual(candidates["stickers"], [])
        self.assertEqual(candidates["voices"], [VOICE_CANDIDATE])

    async def test_unknown_voice_cannot_be_retried_or_replaced_by_an_image(self):
        original = self.sender
        async def sender(gid, parts):
            if parts[0]["type"] == "record":
                self.sent.append((gid, deepcopy(parts)))
                return Receipt("unknown", reason="no_ack")
            return await original(gid, parts)
        self.sender = sender
        receipt = await self.submit()
        await self.drain()
        group = self.bot.group("1")
        self.assertEqual(group.last_receipts["99:1:1:voice:out-1"]["status"], "unknown")
        self.assertEqual(group.supplement_state()["recent_voice_count"], 0)
        self.assertEqual(group.supplement_state()["turns_since_last_supplement"], 1)
        self.assertEqual(len([r for r in group.history if r.get("role") == "assistant"]), 1)
        event = Event.parse(raw())
        snapshot = build_snapshot([event], [], now=self.clock.timestamp())
        self.bot.supplements.start(event, self.sender, receipt, snapshot, {"target_message_ids": ["1"]},
                                   received_at=self.clock, revision=self.bot.supplements.revision("1"))
        await self.drain()
        self.assertEqual(len(self.model.supplement_plans), 1)
        refused = await self.bot.deliver_reply(event, self.sender, "", sticker_id="happy",
                                               delivery_origin="sticker_supplement", parent_message_id="out-1")
        self.assertEqual(refused.reason, "supplement_already_attempted")
        self.assertEqual(len(self.sent), 2)

    async def test_unknown_image_also_blocks_a_second_voice_for_the_parent(self):
        self.model.supplement_steps = [supplement()]
        original = self.sender
        async def sender(gid, parts):
            if parts[0]["type"] == "image":
                self.sent.append((gid, deepcopy(parts)))
                return Receipt("unknown", reason="no_ack")
            return await original(gid, parts)
        self.sender = sender
        await self.submit()
        await self.drain()
        refused = await self.bot.deliver_reply(Event.parse(raw()), self.sender, "", voice_id="V0001",
                                               delivery_origin="voice_supplement", parent_message_id="out-1")
        self.assertEqual(refused.reason, "supplement_already_attempted")
        self.assertFalse(self.voices.prepared)
        self.assertEqual(len(self.sent), 2)

    async def test_voice_preparation_failure_never_falls_back_to_image_or_repeats_text(self):
        self.voices.prepare = lambda _: (_ for _ in ()).throw(ValueError("bad recording"))
        await self.submit()
        await self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.stickers.prepared)
        self.assertEqual(self.bot.group("1").last_receipts["99:1:1"]["status"], "sent")
        self.assertTrue(any(row.get("reason") == "voice_prepare_failed" for row in self.rows()))
        self.assertEqual(self.bot.group("1").supplement_state()["recent_voice_count"], 0)

    async def test_new_message_cancels_voice_planning(self):
        entered = asyncio.Event()
        async def slow(_):
            entered.set()
            await asyncio.Event().wait()
        self.model.supplement_steps = [slow]
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        old = next(iter(self.bot.supplements.tasks))
        self.model.steps = [action("observe")]
        await self.submit(mid=2, uid=3)
        await self.drain()
        self.assertTrue(old.cancelled())
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.voices.prepared)

    async def test_voice_preparation_timeout_does_not_send_from_finished_thread(self):
        self.config.supplements.max_age_seconds = .03
        entered, release = threading.Event(), threading.Event()
        original = self.voices.prepare
        def slow(identity):
            entered.set()
            release.wait(2)
            return original(identity)
        self.voices.prepare = slow
        await self.submit()
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await self.drain()
            self.assertEqual(len(self.sent), 1)
            self.assertTrue(any(row.get("reason") == "expired" for row in self.rows()))
        finally:
            release.set()

    async def test_new_message_during_voice_preparation_discards_stale_recording(self):
        entered, release = threading.Event(), threading.Event()
        original = self.voices.prepare
        def slow(identity):
            entered.set()
            release.wait(2)
            return original(identity)
        self.voices.prepare = slow
        await self.submit()
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            self.model.steps = [action("observe")]
            await self.submit(mid=2, uid=3)
            await self.drain()
            self.assertEqual(len(self.sent), 1)
            self.assertTrue(any(row.get("reason") == "new_message" for row in self.rows()))
        finally:
            release.set()

    async def test_submitted_voice_awaits_ack_and_belongs_to_its_original_turn(self):
        self.config.supplements.max_age_seconds = .03
        entered, ack = asyncio.Event(), asyncio.Event()
        original = self.sender
        async def sender(gid, parts):
            if parts[0]["type"] == "record":
                self.sent.append((gid, deepcopy(parts)))
                entered.set()
                await ack.wait()
                return Receipt("sent", "voice-old")
            return await original(gid, parts)
        self.sender = sender
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        self.clock += timedelta(seconds=2)
        second = await self.submit(mid=2)
        await asyncio.sleep(.06)
        self.assertTrue(self.bot.supplements.tasks)
        self.assertEqual(len(self.model.supplement_plans), 1)
        ack.set()
        await self.drain()
        group = self.bot.group("1")
        self.assertEqual(group.last_sent["message_id"], second.message_id)
        self.assertEqual(group.supplement_state()["recent_turns"], 2)
        self.assertEqual(group.supplement_state()["recent_voice_count"], 1)
        self.assertEqual(group.supplement_state()["turns_since_last_voice"], 1)
        self.assertEqual(group.supplement_state()["turns_since_last_supplement"], 1)
        restored = GroupLog(self.data, "1", now=lambda: self.clock.timestamp())
        self.assertEqual(restored.supplement_state(), group.supplement_state())

    async def test_other_group_does_not_share_voice_cadence_or_cancel_job(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def slow(_):
            entered.set()
            await finish.wait()
            return media()
        self.model.supplement_steps = [slow, supplement(None)]
        await self.submit(gid=1)
        await asyncio.wait_for(entered.wait(), 1)
        await self.submit(gid=2, mid=2)
        finish.set()
        await self.drain()
        self.assertEqual(self.bot.group("1").supplement_state()["recent_voice_count"], 1)
        self.assertEqual(self.bot.group("2").supplement_state()["recent_voice_count"], 0)
        self.assertEqual(self.sent[-1], ("1", [VOICE]))

    async def test_voice_without_confirmed_parent_and_combined_payloads_are_rejected(self):
        event = Event.parse(raw())
        result = await self.bot.deliver_reply(event, self.sender, "", voice_id="V0001",
                                              delivery_origin="voice_supplement", parent_message_id="missing")
        self.assertEqual(result.reason, "unconfirmed_voice_parent")
        mixed = await self.bot.deliver_reply(event, self.sender, "文字", voice_id="V0001")
        self.assertEqual(mixed.reason, "mixed_voice_reply")
        both = await self.bot.deliver_reply(event, self.sender, "", voice_id="V0001", sticker_id="happy")
        self.assertEqual(both.reason, "mixed_media_reply")
        self.assertFalse(self.sent)

    async def test_sleep_after_main_text_prevents_voice_submission(self):
        async def sleep(_):
            self.clock = self.clock.replace(hour=2)
            return media()
        self.model.supplement_steps = [sleep]
        await self.submit()
        await self.drain()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.voices.prepared)
        self.assertTrue(any(row.get("reason") == "sleeping" for row in self.rows()))
