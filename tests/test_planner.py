import json
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from atri_bot.config import Config
from atri_bot.context import build_snapshot
from atri_bot.model import ChatModel, ModelError
from atri_bot.planner import Planner
from atri_bot.tools import ToolContext, ToolRegistry, ToolSession, ToolSpec, ToolResult
from atri_bot.types import Event
from tests.support.factories import ROOT, action, call, daytime, raw
from tests.support.models import PlanningModel


class PlannerProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config(ROOT, ROOT / "unused-test-data")
        self.model = PlanningModel()
        self.snapshot = build_snapshot([Event.parse(raw())], [], now=daytime().timestamp())

    async def test_rejects_text_multiple_actions_bad_targets_and_invented_sources(self):
        multiple = action()
        multiple["tool_calls"].append(call("observe", {}, "second"))
        invalid = [{"content": "应该回复"}, multiple, action(ids=["foreign"]),
                   action(reference_facts=[{"source_id": "msg:999", "text": "编造内容"}]),
                   action("wait", seconds=True), action("wait", seconds=30), action("wait", seconds=.01)]
        for value in invalid:
            with self.subTest(value=value):
                self.model.steps = [value] * 3
                before = len(self.model.plans)
                with self.assertRaises(ModelError) as ctx:
                    await Planner(self.model, self.config).decide("人设", self.snapshot)
                self.assertEqual(ctx.exception.code, "invalid_planner_action")
                self.assertEqual(ctx.exception.attempts, 3)
                self.assertEqual(len(self.model.plans) - before, 3)

    async def test_retry_preserves_input_and_no_invalid_prose_reaches_reply(self):
        self.model.steps = [{"content": "失败草稿"}, action("observe")]
        result = await Planner(self.model, self.config).decide("人设", self.snapshot)
        self.assertEqual(result["action"], "observe")
        self.assertNotIn("失败草稿", str(self.model.plans[-1]))

    async def test_tools_result_and_budget_persist_across_wait_refresh(self):
        registry = ToolRegistry()
        executions = []
        async def lookup(ctx, args):
            executions.append(ctx.group_id)
            return ToolResult(True, data={"text": "有来源的内容"})
        registry.register(ToolSpec("lookup", "测试查询", {"type": "object", "properties": {},
                                                        "additionalProperties": False}, lookup))
        ctx = ToolContext("1", "2", "99", "99:1:1", self.snapshot.now, None, lambda: None, lambda _: None)
        session = ToolSession(registry, ctx, self.config.tools)
        self.config.tools.max_rounds = 1
        self.model.steps = [dict(role="assistant", content=None, tool_calls=[call("lookup", {})]),
                            action("wait", seconds=1),
                            action(reference_facts=[{"source_id": "tool:call_1", "text": "有来源的内容"}])]
        planner = Planner(self.model, self.config)
        self.assertEqual((await planner.decide("人设", self.snapshot, session, remaining_waits=1))["action"], "wait")
        self.assertIn("有来源的内容", str(self.model.plans[1]))
        self.assertEqual((await planner.decide("人设", self.snapshot, session))["action"], "reply")
        self.assertEqual(executions, ["1"])
        self.assertEqual(session.calls, 1)
        self.assertEqual(planner.read_rounds, 1)
        self.assertIn("tool:call_1", str(self.model.plans[-1]))

    async def test_actual_http_native_tools_and_reply_plain_text(self):
        payloads = []
        async def provider(request):
            payload = await request.json()
            payloads.append(payload)
            return web.json_response({"choices": [{"message": action() if "tools" in payload
                                                   else {"content": "角色正文"}, "finish_reason": "stop"}]})
        app = web.Application()
        app.router.add_post("/chat/completions", provider)
        server = TestServer(app)
        await server.start_server()
        self.config.base_url, self.config.api_key, self.config.model = str(server.make_url("/")), "test", "reply"
        self.config.reply.judgment_model = "plan"
        self.config.thinking = "disabled"
        try:
            async with ClientSession() as session:
                model = ChatModel(self.config, session)
                decision = await Planner(model, self.config).decide("人设", self.snapshot)
                from atri_bot.context import build_planned_reply
                result = await model.complete(build_planned_reply("人设", self.snapshot, decision, []))
            self.assertEqual(result, "角色正文")
            self.assertEqual(payloads[0]["model"], "plan")
            self.assertEqual(payloads[1]["model"], "reply")
            self.assertEqual(payloads[0]["max_tokens"], 1024)
            self.assertNotIn("tools", payloads[1])
            self.assertEqual(payloads[0]["tool_choice"], "required")
            self.assertEqual(payloads[0]["temperature"], .2)
            self.assertNotIn("temperature", payloads[1])
            targets = payloads[0]["tools"][0]["function"]["parameters"]["properties"]["target_message_ids"]
            self.assertEqual(targets["items"]["enum"], ["1"])
            self.assertTrue(all(d["function"]["description"] for d in payloads[0]["tools"]))
        finally:
            await server.close()

    async def test_main_understanding_stays_strict_and_retry_explains_structure(self):
        for misplaced in ("target_message_ids", "additionalProperties"):
            message = action()
            function = message["tool_calls"][0]["function"]
            args = json.loads(function["arguments"])
            args["understanding"][misplaced] = args.pop(misplaced) if misplaced == "target_message_ids" else False
            function["arguments"] = json.dumps(args)
            self.model.steps = [message, action()]
            self.assertEqual((await Planner(self.model, self.config).decide("人设", self.snapshot))["action"], "reply")
            correction = self.model.plans[-1][0]["content"].removeprefix(self.model.plans[-2][0]["content"])
            self.assertIn("最外层", correction)
            self.assertIn("仅含 topic、interaction、interest", correction)
