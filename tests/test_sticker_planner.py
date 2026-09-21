import json
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from atri_bot.config import Config
from atri_bot.context import build_snapshot
from atri_bot.model import ChatModel, ModelError
from atri_bot.sticker_planner import StickerPlanner
from atri_bot.stickers import StickerConfig
from atri_bot.types import Event
from tests.support.factories import ROOT, action, daytime, raw
from tests.support.stickers import CANDIDATE, FakeStickerLibrary, StickerModel, supplement


class StickerProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model, self.library = StickerModel(), FakeStickerLibrary()
        self.planner = StickerPlanner(self.model, self.library, StickerConfig(enabled=True))
        self.snapshot = build_snapshot([Event.parse(raw())], [], now=daytime().timestamp())
        self.sent = {"message_id": "text-1", "text": "好耶，，，太开心了)", "time": self.snapshot.now}
        self.decision = {"target_message_ids": ["1"], "understanding": {"interest": "分享开心"}}
        self.state = {"turns_since_last_sticker": 4, "recent_sticker_ids": ["old"],
                      "recent_turns": 4, "recent_sticker_count": 0}

    async def decide(self):
        return await self.planner.decide("人设", self.snapshot, self.sent, self.decision, self.state)

    async def test_select_or_skip_uses_sent_text_snapshot_and_real_candidates(self):
        for identity in ("happy", None):
            self.model.sticker_steps = [supplement(identity)]
            self.assertEqual((await self.decide())["sticker_id"], identity)
            messages, definitions = self.model.sticker_plans[-1]
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["snapshot"], self.snapshot.data)
            self.assertEqual(data["sent_reply"], self.sent)
            self.assertEqual(data["candidates"], [CANDIDATE])
            self.assertEqual(definitions[0]["function"]["parameters"]["properties"]["sticker_id"]["enum"], [None, "happy"])
        self.assertEqual(self.library.queries[-1][1], ["old"])
        self.assertIn(self.sent["text"], self.library.queries[-1][0])

    async def test_no_candidates_never_calls_model(self):
        self.library.items = []
        self.assertIsNone((await self.decide())["sticker_id"])
        self.assertFalse(self.model.sticker_plans)

    async def test_invalid_ids_extra_fields_wrong_tool_and_duplicate_keys_are_rejected(self):
        duplicate = supplement()
        duplicate["tool_calls"][0]["function"]["arguments"] = '{"sticker_id":null,"sticker_id":"happy","reason":"重复键"}'
        for message in (supplement("foreign"), supplement("/tmp/image.png"), supplement(text="加一句"), action(), duplicate):
            with self.subTest(message=message):
                self.model.sticker_steps = [message] * 3
                with self.assertRaises(ModelError) as error:
                    await self.decide()
                self.assertEqual((error.exception.code, error.exception.attempts), ("invalid_sticker_decision", 3))
        self.assertFalse(self.library.prepared)

    async def test_retry_keeps_invalid_output_out_of_context(self):
        self.model.sticker_steps = [supplement("foreign"), supplement(None)]
        self.assertIsNone((await self.decide())["sticker_id"])
        second = self.model.sticker_plans[1][0]
        self.assertEqual(len(second), 2)
        self.assertNotIn("foreign", json.dumps(second))

    async def test_real_http_contract_uses_only_supplement_tool_and_planning_model(self):
        payloads = []
        async def provider(request):
            payloads.append(await request.json())
            return web.json_response({"choices": [{"message": supplement(), "finish_reason": "tool_calls"}]})
        app = web.Application()
        app.router.add_post("/chat/completions", provider)
        async with TestServer(app) as server, ClientSession() as session:
            config = Config(ROOT, ROOT / "unused-test-sticker-data")
            config.base_url = str(server.make_url("/"))
            config.api_key, config.model = "test-only", "text-model"
            config.reply.judgment_model, config.thinking = "planning-model", "disabled"
            self.planner.model = ChatModel(config, session)
            self.assertEqual((await self.decide())["sticker_id"], "happy")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["model"], "planning-model")
        self.assertEqual(payloads[0]["tool_choice"], "required")
        self.assertEqual([t["function"]["name"] for t in payloads[0]["tools"]], ["supplement_sticker"])
        self.assertEqual(json.loads(payloads[0]["messages"][1]["content"])["sent_reply"], self.sent)
