"""Request-level concurrency through real Planner/reply tool loops and local HTTP."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.history_tools import object_schema
from atri_bot.model import ChatModel
from atri_bot.tools import ToolResult, ToolSpec
from atri_bot.types import Event, Receipt
from atri_bot.vision import ImageAccess, register_vision
from atri_bot.willingness import ReplyConfig
from test_bot import ROOT, daytime, raw
from test_planner import action
from test_tools import call


class ModelConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests, self.sent = [], []
        self.active = self.peak = 0
        self.clock = daytime()
        self.tool_entered, self.tool_release = asyncio.Event(), asyncio.Event()
        self.handler = self.respond

        async def provider(request):
            payload = await request.json()
            self.requests.append(payload)
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                message = await self.handler(payload)
                if isinstance(message, web.Response):
                    return message
                for index, tool_call in enumerate(message.get("tool_calls", [])):
                    tool_call["id"] = f"request_{len(self.requests)}_{index}"
                return web.json_response({"choices": [{"message": message, "finish_reason": "stop"}]})
            finally:
                self.active -= 1

        app = web.Application()
        app.router.add_post("/chat/completions", provider)
        self.provider = TestServer(app)
        await self.provider.start_server()
        self.http = ClientSession()
        self.config = Config(ROOT, Path(self.tmp.name), groups=frozenset({"1", "2"}), self_id="99",
            api_key="local-test", model="local-test", base_url=str(self.provider.make_url("/")),
            parallel=1, reply=ReplyConfig(mode="planner", cooldown_seconds=0))
        self.config.planner.debounce_seconds = .001
        self.config.planner.max_batch_seconds = .01
        self.bot = Bot(self.config, ChatModel(self.config, self.http), now=lambda: self.clock)
        for gid in self.config.groups:
            self.bot.group(gid).now = lambda: self.clock.timestamp()

        async def slow(context, args):
            self.tool_entered.set()
            await self.tool_release.wait()
            return ToolResult(True, data={"text": "慢工具得到的内容"})
        self.bot.tool_registry.register(ToolSpec("slow_read", "读取测试文档", object_schema({}), slow))

    async def asyncTearDown(self):
        self.tool_release.set()
        await self.bot.close(timeout=.05)
        await self.http.close()
        await self.provider.close()
        self.tmp.cleanup()

    @staticmethod
    def snapshot(payload):
        return json.loads(payload["messages"][1]["content"]).get("snapshot")

    @staticmethod
    def is_planning(payload):
        return any(tool["function"]["name"] == "observe" for tool in payload.get("tools", []))

    async def respond(self, payload):
        has_result = any(m["role"] == "tool" for m in payload["messages"])
        if self.is_planning(payload):
            snapshot = self.snapshot(payload)
            if "读取慢工具" in str(snapshot) and not has_result:
                return {"role": "assistant", "content": None,
                        "tool_calls": [call("slow_read", {}, "read")]}
            return action(ids=[row["message_id"] for row in snapshot["pending"]])
        if payload.get("tools") and "读取慢工具" in str(payload["messages"]) and not has_result:
            return {"role": "assistant", "content": None,
                    "tool_calls": [call("slow_read", {}, "read")]}
        return {"content": "本轮正文"}

    def submit(self, **args):
        async def send(gid, parts):
            self.sent.append((gid, parts))
            return Receipt("sent", str(100 + len(self.sent)))
        return self.bot.enqueue(Event.parse(raw(**args)), send)

    async def assert_slot_released(self):
        await asyncio.wait_for(self.bot.semaphore.acquire(), .2)
        self.bot.semaphore.release()

    async def slow_tool_allows_other_group(self):
        first = self.submit(text="读取慢工具")
        await asyncio.wait_for(self.tool_entered.wait(), 1)
        second = self.submit(gid=2, text="另一群的普通消息")
        self.assertEqual((await asyncio.wait_for(second, 1)).status, "sent")
        self.assertFalse(first.done())
        self.assertEqual([gid for gid, _ in self.sent], ["2"])
        self.tool_release.set()
        self.assertEqual((await asyncio.wait_for(first, 1)).status, "sent")
        self.assertEqual(self.peak, 1)
        await self.assert_slot_released()

    async def test_planner_tool_does_not_hold_only_model_slot(self):
        await self.slow_tool_allows_other_group()

    async def test_legacy_reply_tool_does_not_hold_only_model_slot(self):
        self.config.reply.mode = "at_only"
        await self.slow_tool_allows_other_group()

    async def test_cross_midnight_during_tool_stops_followup_and_send(self):
        first = self.submit(text="读取慢工具")
        await asyncio.wait_for(self.tool_entered.wait(), 1)
        self.clock = self.clock.replace(hour=0)
        self.tool_release.set()
        result = await asyncio.wait_for(first, 1)
        self.assertEqual(result.reason, "sleeping")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.sent)
        await self.assert_slot_released()

    async def test_cancel_while_reading_releases_batch_without_reply(self):
        first = self.submit(text="读取慢工具")
        await asyncio.wait_for(self.tool_entered.wait(), 1)
        await self.bot.close(timeout=.001)
        self.assertEqual((await first).reason, "shutdown")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.sent)
        self.assertFalse(self.bot.inflight)
        await self.assert_slot_released()

    async def test_waiting_first_slot_collects_new_messages_before_snapshot(self):
        await self.bot.semaphore.acquire()
        first = self.submit(text="前半句")
        await asyncio.sleep(.02)
        second = self.submit(mid=2, text="后半句")
        self.bot.semaphore.release()
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual([r.status for r in results], ["sent", "sent"])
        plans = [p for p in self.requests if self.is_planning(p)]
        self.assertEqual(len(plans), 1)
        self.assertEqual([m["message_id"] for m in self.snapshot(plans[0])["pending"]], ["1", "2"])

    async def test_related_message_while_replyer_waits_skips_outdated_request(self):
        planning, release_plan = asyncio.Event(), asyncio.Event()
        other_entered, release_other = asyncio.Event(), asyncio.Event()
        held_first = False

        async def handle(payload):
            nonlocal held_first
            snapshot = self.snapshot(payload)
            if self.is_planning(payload) and snapshot["group_id"] == "1" and not held_first:
                held_first = True
                planning.set()
                await release_plan.wait()
            elif snapshot["group_id"] == "2":
                other_entered.set()
                await release_other.wait()
            return await self.respond(payload)

        self.handler = handle
        first = self.submit(text="最初的问题")
        await asyncio.wait_for(planning.wait(), 1)
        other = self.submit(gid=2)
        await asyncio.sleep(.02)  # Other group has finished collection and queued for the slot.
        release_plan.set()
        await asyncio.wait_for(other_entered.wait(), 1)
        supplement = self.submit(mid=2, text="更正刚才的问题")
        release_other.set()
        results = await asyncio.wait_for(asyncio.gather(first, other, supplement), 1)
        self.assertTrue(all(r.status == "sent" for r in results))
        replies = [p for p in self.requests if not self.is_planning(p)
                   and self.snapshot(p)["group_id"] == "1"]
        self.assertEqual(len(replies), 1)
        self.assertEqual([r["message_id"] for r in self.snapshot(replies[0])["pending"]], ["1", "2"])
        self.assertEqual(self.peak, 1)

    async def test_image_tool_model_call_uses_slot_without_deadlocking(self):
        self.config.vision.enabled = True
        self.config.vision.model = "vision-test"
        register_vision(self.bot.tool_registry, self.config.vision)
        image_entered, image_release = asyncio.Event(), asyncio.Event()

        async def handle(payload):
            if payload["model"] == "vision-test":
                image_entered.set()
                await image_release.wait()
                return {"content": "图片是红色方块"}
            if self.is_planning(payload):
                snapshot = self.snapshot(payload)
                if snapshot["group_id"] == "1" and not any(m["role"] == "tool" for m in payload["messages"]):
                    return {"role": "assistant", "content": None,
                            "tool_calls": [call("inspect_image", {"image_id": "img_1_1"}, "image")]}
            return await self.respond(payload)

        self.handler = handle
        async def download(access, image_id):
            return b"synthetic-image", {"first_frame_only": False}

        event = replace(Event.parse(raw()), parts=({"type": "image", "data": {"url": "not-downloaded"}},))
        async def send(gid, parts):
            self.sent.append((gid, parts))
            return Receipt("sent", "image-reply")
        with patch.object(ImageAccess, "_download", download):
            first = self.bot.enqueue(event, send)
            await asyncio.wait_for(image_entered.wait(), 1)
            other = self.submit(gid=2)
            await asyncio.sleep(.02)
            self.assertEqual(len(self.requests), 2)  # Planner + vision; other group is still waiting.
            image_release.set()
            results = await asyncio.wait_for(asyncio.gather(first, other), 1)
        self.assertTrue(all(r.status == "sent" for r in results))
        self.assertEqual(self.peak, 1)
        await self.assert_slot_released()

    async def test_planner_retry_yields_slot_to_waiting_group(self):
        entered, release = asyncio.Event(), asyncio.Event()
        failed = False
        order = []

        async def handle(payload):
            nonlocal failed
            snapshot = self.snapshot(payload)
            order.append(snapshot["group_id"])
            if snapshot["group_id"] == "1" and not failed:
                failed = True
                entered.set()
                await release.wait()
                return web.json_response({"error": "temporary"}, status=503)
            return await self.respond(payload)

        self.handler = handle
        first = self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        other = self.submit(gid=2)
        await asyncio.sleep(.02)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, other), 1)
        self.assertTrue(all(r.status == "sent" for r in results))
        self.assertEqual(order[:3], ["1", "2", "1"])
        self.assertEqual(self.peak, 1)
        await self.assert_slot_released()
