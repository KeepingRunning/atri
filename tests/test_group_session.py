import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.model import ModelError
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyConfig
from tests.support.factories import ROOT, action, call, daytime, raw
from tests.support.models import PlanningModel


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = Config(ROOT, self.data_dir, groups=frozenset({"1", "2"}), self_id="99",
                             reply=ReplyConfig(mode="planner", cooldown_seconds=0))
        self.config.tools.enabled = False
        self.config.planner.debounce_seconds = .01
        self.config.planner.max_batch_seconds = .04
        self.config.planner.max_wait_seconds = .05
        self.clock = daytime()
        self.model = PlanningModel()
        self.bot = Bot(self.config, self.model, now=lambda: self.clock)
        self.addAsyncCleanup(self.bot.close, timeout=.1)
        for gid in self.config.groups:
            self.bot.group(gid).now = lambda: self.clock.timestamp()
        self.sent = []

    async def send(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt("sent", str(100 + len(self.sent)))

    def submit(self, **args):
        return self.bot.enqueue(Event.parse(raw(**args)), self.send)

    def rows(self, gid="1"):
        return [json.loads(line) for line in self.bot.group(gid).path.read_text().splitlines()]

    async def test_burst_is_one_snapshot_and_one_reply_with_immediate_persistence(self):
        a = self.submit(text="我想问", mention=False)
        b = self.submit(mid=2, text="你在干嘛", mention=False)
        self.assertEqual(sum(row["kind"] == "incoming" for row in self.rows()), 2)
        self.assertEqual([r.status for r in await asyncio.gather(a, b)], ["sent", "sent"])
        self.assertEqual(len(self.model.plans), 1)
        self.assertEqual(len(self.sent), 1)
        p = json.loads(self.model.plans[0][1]["content"])["snapshot"]
        r = json.loads(self.model.replies[0][1]["content"])["snapshot"]
        self.assertEqual(p, r)
        self.assertEqual([m["message_id"] for m in p["pending"]], ["1", "2"])
        self.assertEqual(p["history"], [])
        self.assertTrue(p["schedule"])
        self.assertEqual([row.get("role") for row in self.bot.group("1").history].count("assistant"), 1)

    async def test_observe_and_frequency_zero_do_not_generate(self):
        self.model.steps = [action("observe")]
        self.assertEqual((await self.submit(mention=False)).reason, "planner_observe")
        self.config.reply.frequency = 0
        self.assertEqual((await self.submit(mid=2, mention=False)).reason, "automatic_reply_disabled")
        self.assertEqual(len(self.model.plans), 1)
        self.assertFalse(self.model.replies)
        self.assertFalse(self.sent)

    async def test_wait_wakes_and_rebuilds_without_holding_global_slot(self):
        waiting = asyncio.Event()
        async def wait(messages):
            waiting.set()
            return action("wait")
        self.model.steps = [wait]
        a = self.submit(text="等等我还没说完")
        await asyncio.wait_for(waiting.wait(), 1)
        # Another group's planning/reply is not blocked by this group's explicit wait.
        other = self.submit(gid=2, mid=30)
        b = self.submit(mid=2, text="我的意思是这个")
        await asyncio.wait_for(asyncio.gather(a, b, other), 1)
        group_plans = [json.loads(m[1]["content"]) for m in self.model.plans
                       if json.loads(m[1]["content"])["snapshot"]["group_id"] == "1"]
        self.assertEqual(len(group_plans), 2)
        self.assertEqual(group_plans[-1]["remaining_waits"], 0)
        self.assertEqual(len(group_plans[-1]["snapshot"]["pending"]), 2)
        self.assertEqual(len([sent for sent in self.sent if sent[0] == "1"]), 1)

    async def test_wait_timeout_has_one_followup_then_observe(self):
        self.model.steps = [action("wait", seconds=.01), action("observe")]
        self.assertEqual((await self.submit()).reason, "planner_observe")
        self.assertEqual(len(self.model.plans), 2)
        self.assertFalse(self.sent)

    async def test_related_input_during_planning_invalidates_before_writing(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(messages):
            entered.set()
            await release.wait()
            return action()
        self.model.steps = [slow]
        a = self.submit(text="先说一半")
        await asyncio.wait_for(entered.wait(), 1)
        b = self.submit(mid=2, text="刚才说错了，是另一件事")
        release.set()
        await asyncio.wait_for(asyncio.gather(a, b), 1)
        self.assertEqual(len(self.model.plans), 2)
        self.assertEqual(len(self.model.replies), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(any(r.get("stage") == "stale" for r in self.rows()))
        self.assertNotIn("刚才说错了", str(self.model.plans[0]))

    async def test_related_input_during_reply_discards_old_draft(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(messages):
            self.model.replies.append(deepcopy(messages))
            if len(self.model.replies) == 1:
                entered.set()
                await release.wait()
                return "旧草稿不应发送"
            return "新回复"
        self.model.complete = complete
        a = self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        b = self.submit(mid=2, text="补充一下")
        release.set()
        await asyncio.gather(a, b)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("旧草稿", str(self.sent))
        self.assertNotIn("旧草稿", self.bot.group("1").path.read_text())

    async def test_unrelated_new_message_does_not_cancel_snapshot_or_leak_between_groups(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(messages):
            entered.set()
            await release.wait()
            return action()
        self.model.steps = [slow]
        a = self.submit(text="群一问题")
        await asyncio.wait_for(entered.wait(), 1)
        b = self.submit(mid=2, uid=3, text="另起话题", mention=False)
        other = self.submit(gid=2, text="群二内容")
        release.set()
        await asyncio.gather(a, b, other)
        self.assertEqual(len(self.sent), 3)
        for messages in self.model.plans:
            snapshot = json.loads(messages[1]["content"])["snapshot"]
            self.assertNotIn("群二内容" if snapshot["group_id"] == "1" else "群一问题", str(snapshot))
        self.assertNotIn("另起话题", str(self.model.replies[0]))

    async def test_replan_cap_releases_batch_without_sending_obsolete_text(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(messages):
            entered.set()
            await release.wait()
            return action()
        self.config.planner.max_replans = 0
        self.model.steps = [slow]
        a = self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        b = self.submit(mid=2)
        release.set()
        self.assertEqual((await a).reason, "superseded")
        self.assertEqual((await b).status, "sent")
        self.assertEqual(len(self.sent), 1)

    async def test_night_and_cross_midnight_never_generate_or_retry(self):
        self.clock = self.clock.replace(hour=1)
        self.assertEqual((await self.submit()).reason, "sleeping")
        self.assertFalse(self.model.plans)
        self.clock = self.clock.replace(hour=23)
        async def midnight(messages):
            self.clock = (self.clock + timedelta(days=1)).replace(hour=0)
            raise ModelError("timeout", "model_timeout")
        self.model.steps = [midnight]
        self.assertEqual((await self.submit(mid=2)).reason, "sleeping")
        self.assertEqual(len(self.model.plans), 1)
        self.assertFalse(self.model.replies)
        self.assertFalse(self.sent)

    async def test_midnight_during_reply_and_receipt_unknown_not_in_history(self):
        async def midnight(messages):
            self.clock = self.clock.replace(hour=0)
            return "不能发送"
        self.model.complete = midnight
        self.assertEqual((await self.submit()).reason, "sleeping")
        self.assertFalse(self.sent)
        self.clock = daytime()
        self.model = PlanningModel()
        self.bot.model = self.model
        async def unknown(gid, parts):
            return Receipt("unknown")
        self.send = unknown
        self.assertEqual((await self.submit(mid=2)).status, "unknown")
        self.assertFalse(any(r.get("role") == "assistant" for r in self.bot.group("1").history))

    async def test_close_finishes_every_future_in_collected_batch(self):
        entered = asyncio.Event()
        async def forever(messages):
            entered.set()
            await asyncio.Event().wait()
        self.model.steps = [forever]
        futures = [self.submit(mid=i) for i in range(1, 4)]
        await asyncio.wait_for(entered.wait(), 1)
        await self.bot.close(timeout=.01)
        self.assertTrue(all(f.done() for f in futures))
        self.assertFalse(self.bot.inflight)
        await asyncio.wait_for(self.bot.queues["1"].join(), .1)

    async def test_message_cap_prevents_unbounded_collection(self):
        self.config.planner.max_batch_messages = 2
        futures = [self.submit(mid=i) for i in range(1, 6)]
        await asyncio.wait_for(asyncio.gather(*futures), 1)
        sizes = [len(json.loads(m[1]["content"])["snapshot"]["pending"]) for m in self.model.plans]
        self.assertEqual(sizes, [2, 2, 1])
        # The capped earlier batches see queued supplements by the same speaker;
        # only the final batch is allowed to send, with the others retained in history.
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(futures[0].result().reason, "superseded")
        first = json.loads(self.model.plans[0][1]["content"])["snapshot"]
        self.assertFalse(first["history"])

    async def test_continuous_input_does_not_extend_collection_past_deadline(self):
        self.config.planner.debounce_seconds = .03
        self.config.planner.max_batch_seconds = .06
        entered = asyncio.Event()
        original = self.model.plan
        async def plan(messages, definitions):
            entered.set()
            return await original(messages, definitions)
        self.model.plan = plan
        futures = []
        async def produce():
            for i in range(1, 25):
                futures.append(self.submit(mid=i, uid=i + 10, text=f"持续输入-{i}", mention=False))
                await asyncio.sleep(.01)
        producer = asyncio.create_task(produce())
        try:
            await asyncio.wait_for(entered.wait(), .5)
            self.assertFalse(producer.done())
            await producer
            await asyncio.gather(*futures)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def test_sleep_messages_not_replayed_on_waking(self):
        self.clock = self.clock.replace(hour=7, minute=59)
        self.assertEqual((await self.submit(text="夜间待处理内容")).reason, "sleeping")
        self.clock = self.clock.replace(hour=8, minute=0)
        self.assertEqual((await self.submit(mid=2)).status, "sent")
        self.assertNotIn("夜间待处理内容", str(self.model.plans))
        self.assertIn("夜间待处理内容", self.bot.group("1").path.read_text())

    async def test_sleep_disabled_allows_planning_and_reply_at_night(self):
        self.clock = self.clock.replace(hour=1)
        self.config.schedule.sleep_enabled = False
        self.assertEqual((await self.submit()).status, "sent")
        self.assertEqual(len(self.model.plans), 1)
        self.assertEqual(len(self.model.replies), 1)
        self.assertEqual(len(self.sent), 1)
        snapshot = json.loads(self.model.plans[0][1]["content"])["snapshot"]
        self.assertNotIn('不回复消息', snapshot['schedule'])
        self.assertFalse(self.bot.schedule.current_plan()['sleeping'])

    async def test_batch_image_tool_can_read_first_message_and_feed_replyer(self):
        from atri_bot.vision import register_vision, ImageAccess
        self.config.tools.enabled = True
        self.config.vision.enabled = True
        self.config.vision.model = "vision-test"
        self.model.config = self.config
        register_vision(self.bot.tool_registry, self.config.vision)
        self.model.steps = [dict(role="assistant", content=None, tool_calls=[call("inspect_image", {"image_id": "img_1_1"})]),
                            action(ids=["2"], reference_facts=[{"source_id": "tool:call_1", "text": "红色方块"}])]
        purposes = []
        async def complete(messages, **kwargs):
            purposes.append(kwargs.get("purpose", "reply"))
            if kwargs.get("purpose") == "vision":
                return "图片里是一个红色方块。"
            self.assertIn("红色方块", str(messages))
            return "红色方块。"
        self.model.complete = complete
        async def download(access, image_id):
            self.assertIn(image_id, access.sources)
            return b"synthetic-image", {"first_frame_only": False}
        event = replace(Event.parse(raw()), parts=({"type": "image", "data": {"url": "never-downloaded"}},))
        with patch.object(ImageAccess, "_download", download):
            a = self.bot.enqueue(event, self.send)
            b = self.submit(mid=2, text="图里是什么？")
            await asyncio.gather(a, b)
        self.assertEqual(purposes, ["vision", "reply"])
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("图片里是一个红色方块", str(self.bot.group("1").history))

    async def test_cooldown_stale_messages_and_waiting_for_slot_obey_gates(self):
        await self.submit()
        self.config.reply.cooldown_seconds = 10
        self.assertEqual((await self.submit(mid=2, mention=False)).reason, "cooldown")
        event = replace(Event.parse(raw(mid=3)), timestamp=self.clock.timestamp() - 121)
        self.assertEqual((await self.bot.enqueue(event, self.send)).reason, "stale_message")
        self.assertEqual(len(self.model.plans), 1)
        self.bot.semaphore = asyncio.Semaphore(0)
        pending = self.submit(mid=4)
        await asyncio.sleep(.03)
        self.clock = self.clock.replace(hour=0)
        self.bot.semaphore.release()
        self.assertEqual((await pending).reason, "sleeping")
        self.assertEqual(len(self.model.plans), 1)
