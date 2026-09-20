import asyncio
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig
from test_bot import ROOT, daytime, raw
from test_planner import PlanningModel, action


class RepetitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(ROOT, Path(self.tmp.name), groups=frozenset({"1", "2"}), self_id="99",
                             reply=ReplyConfig(mode="planner", cooldown_seconds=0))
        self.config.tools.enabled = False
        self.config.planner.debounce_seconds = .01
        self.config.planner.max_batch_seconds = .03
        self.clock = daytime()
        self.model = PlanningModel()
        self.bot = Bot(self.config, self.model, now=lambda: self.clock)
        self.sent = []
        for gid in self.config.groups:
            self.bot.group(gid).now = lambda: self.clock.timestamp()

    async def asyncTearDown(self):
        await self.bot.close(timeout=.1)
        self.tmp.cleanup()

    async def send(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt("sent", str(100 + len(self.sent)))

    def submit(self, mid, uid, text="好耶", **kwargs):
        kwargs.setdefault("mention", False)
        return self.bot.enqueue(Event.parse(raw(mid=mid, uid=uid, text=text, **kwargs)), self.send)

    async def test_long_run_repeats_once_without_any_model_call(self):
        await asyncio.gather(*(self.submit(i, i + 1) for i in range(1, 5)))
        await self.submit(5, 9)
        self.assertEqual(self.sent, [("1", [{"type": "text", "data": {"text": "好耶"}}])])
        self.assertFalse(self.model.plans)
        self.assertFalse(self.model.replies)
        history = list(self.bot.group("1").history)
        self.assertEqual(sum(row.get("role") == "assistant" for row in history), 1)
        self.assertFalse(self.bot.repetition.pending)

    async def test_same_user_does_not_trigger_but_next_different_user_does(self):
        self.model.steps = [action("observe")]
        await asyncio.gather(self.submit(1, 2), self.submit(2, 2))
        self.assertFalse(self.sent)
        self.assertEqual(len(self.model.plans), 1)
        await self.submit(3, 3)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.model.plans), 1)

    async def test_repeated_text_is_formatted_only_at_delivery(self):
        text = "好，走吧。OK, go."
        await asyncio.gather(self.submit(1, 2, text=text), self.submit(2, 3, text=text))
        await self.submit(3, 4, text=text)
        self.assertEqual(self.sent, [("1", [{"type": "text", "data": {"text": "好，，，走吧)OK，，， go."}}])])
        self.assertEqual([row["text"] for row in self.bot.group("1").history
                          if row.get("role") != "assistant"], [text, text, text])
        self.assertFalse(self.model.plans)

    async def test_changed_text_resets_run_and_groups_are_independent(self):
        await asyncio.gather(self.submit(1, 2), self.submit(2, 3))
        self.model.steps = [action("observe")]
        await self.submit(3, 2, text="换个话题")
        await asyncio.gather(self.submit(4, 2), self.submit(5, 3))
        self.model.steps = [action("observe")]
        await self.submit(6, 2, gid=2)
        self.assertEqual(len(self.sent), 2)
        await self.submit(7, 3, gid=2)
        self.assertEqual([gid for gid, _ in self.sent], ["1", "1", "2"])

    async def test_duplicate_message_does_not_count_as_second_person(self):
        self.model.steps = [action("observe")]
        first = self.submit(1, 2)
        duplicate = self.submit(1, 3)
        self.assertEqual((await duplicate).status, "duplicate")
        await first
        self.assertFalse(self.sent)
        await self.submit(2, 3)
        self.assertEqual(len(self.sent), 1)

    async def test_images_and_mentions_are_not_plain_text_runs(self):
        for mid, uid in ((1, 2), (2, 3)):
            data = raw(mid=mid, uid=uid, mention=False)
            data["message"] = [{"type": "image", "data": {"file": f"image-{mid}"}}]
            self.model.steps = [action("observe")]
            await self.bot.enqueue(Event.parse(data), self.send)
        self.model.steps = [action("observe")]
        await asyncio.gather(self.submit(3, 2, mention=True), self.submit(4, 3, mention=True))
        self.assertFalse(self.sent)
        self.assertEqual(len(self.model.plans), 3)

    async def test_mixed_batch_preserves_normal_question(self):
        await asyncio.gather(self.submit(1, 2), self.submit(2, 3),
                             self.submit(3, 4, text="亚托莉你在干嘛", mention=True))
        self.assertEqual(len(self.sent), 2)
        pending = json.loads(self.model.plans[0][1]["content"])["snapshot"]["pending"]
        self.assertEqual([row["message_id"] for row in pending], ["3"])

    async def test_new_repeat_during_generation_discards_draft(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(messages, **kwargs):
            entered.set()
            await release.wait()
            return "这条旧草稿不该发送"
        self.model.complete = complete
        first = self.submit(1, 2)
        await asyncio.wait_for(entered.wait(), 1)
        second = self.submit(2, 3)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(self.sent, [("1", [{"type": "text", "data": {"text": "好耶"}}])])
        self.assertNotIn("旧草稿", self.bot.group("1").path.read_text())

    async def test_unknown_send_is_not_retried_or_added_to_history(self):
        async def uncertain(gid, parts):
            self.sent.append((gid, parts))
            return Receipt("unknown", reason="timeout")
        self.send = uncertain
        await asyncio.gather(self.submit(1, 2), self.submit(2, 3))
        await self.submit(3, 4)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(any(row.get("role") == "assistant" for row in self.bot.group("1").history))

    async def test_night_and_stale_messages_do_not_trigger_repeats(self):
        self.clock = self.clock.replace(hour=2)
        await asyncio.gather(self.submit(1, 2), self.submit(2, 3))
        self.clock = self.clock.replace(hour=12)
        for mid, uid in ((3, 2), (4, 3)):
            data = raw(mid=mid, uid=uid, mention=False)
            data["time"] = (self.clock - timedelta(hours=1)).timestamp()
            await self.bot.enqueue(Event.parse(data), self.send)
        self.assertFalse(self.sent)
        self.assertFalse(self.model.plans)

    async def test_legacy_mode_also_repeats_without_model(self):
        self.config.reply.mode = "at_only"
        await asyncio.gather(self.submit(1, 2), self.submit(2, 3), self.submit(3, 4))
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.model.replies)
        self.assertFalse(self.bot.repetition.pending)
