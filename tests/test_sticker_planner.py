import json
import unittest

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from atri_bot.config import Config
from atri_bot.context import build_snapshot
from atri_bot.model import ChatModel, ModelError
from atri_bot.supplements import SupplementConfig, SupplementPlanner
from atri_bot.types import Event
from tests.support.factories import ROOT, action, daytime, raw
from tests.support.stickers import (CANDIDATE, VOICE_CANDIDATE, FakeStickerLibrary, FakeVoiceLibrary,
                                    SupplementModel, media, supplement)


class StickerProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.model, self.library = SupplementModel(), FakeStickerLibrary()
        self.planner = SupplementPlanner(self.model, self.library, None, SupplementConfig())
        self.snapshot = build_snapshot([Event.parse(raw())], [], now=daytime().timestamp())
        self.sent = {"message_id": "text-1", "text": "好耶，，，太开心了)", "time": self.snapshot.now}
        self.decision = {"target_message_ids": ["1"], "understanding": {"interest": "分享开心"}}
        self.state = {"turns_since_last_supplement": 4, "recent_sticker_ids": ["old"],
                      "recent_turns": 4, "recent_sticker_count": 0, "turns_since_last_voice": 8,
                      "recent_voice_ids": ["old-voice"], "recent_voice_groups": ["old-group"]}

    async def decide(self):
        return await self.planner.decide("人设", self.snapshot, self.sent, self.decision, self.state)

    async def test_select_or_skip_uses_sent_text_snapshot_and_real_candidates(self):
        for identity in ("happy", None):
            self.model.supplement_steps = [supplement(identity)]
            result = await self.decide()
            self.assertEqual(result["asset_id"], identity)
            self.assertEqual(result["kind"], "none" if identity is None else "sticker")
            messages, definitions = self.model.supplement_plans[-1]
            data = json.loads(messages[1]["content"])
            self.assertEqual(data["snapshot"], self.snapshot.data)
            self.assertEqual(data["sent_reply"], self.sent)
            self.assertEqual(data["candidates"], {"stickers": [CANDIDATE], "voices": []})
            self.assertEqual(definitions[0]["function"]["parameters"]["properties"]["asset_id"]["enum"], [None, "happy"])
        self.assertEqual(self.library.queries[-1][1], ["old"])
        self.assertIn(self.sent["text"], self.library.queries[-1][0])

    async def test_no_candidates_never_calls_model(self):
        self.library.items = []
        self.assertIsNone((await self.decide())["asset_id"])
        self.assertFalse(self.model.supplement_plans)

    async def test_invalid_ids_extra_fields_wrong_tool_and_duplicate_keys_are_rejected(self):
        duplicate = supplement()
        duplicate["tool_calls"][0]["function"]["arguments"] = '{"kind":"sticker","asset_id":null,"asset_id":"happy","reason":"重复键"}'
        for message in (supplement("foreign"), supplement("/tmp/image.png"), supplement(text="加一句"), action(), duplicate):
            with self.subTest(message=message):
                self.model.supplement_steps = [message] * 3
                with self.assertRaises(ModelError) as error:
                    await self.decide()
                self.assertEqual((error.exception.code, error.exception.attempts), ("invalid_supplement_decision", 3))
        self.assertFalse(self.library.prepared)

    async def test_retry_keeps_invalid_output_out_of_context(self):
        self.model.supplement_steps = [supplement("foreign"), supplement(None)]
        self.assertIsNone((await self.decide())["asset_id"])
        second = self.model.supplement_plans[1][0]
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
            self.assertEqual((await self.decide())["asset_id"], "happy")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["model"], "planning-model")
        self.assertEqual(payloads[0]["tool_choice"], "required")
        self.assertEqual([t["function"]["name"] for t in payloads[0]["tools"]], ["supplement_media"])
        self.assertEqual(json.loads(payloads[0]["messages"][1]["content"])["sent_reply"], self.sent)

    async def test_one_decision_has_both_media_and_voice_literal_context(self):
        self.planner.voices = voices = FakeVoiceLibrary()
        self.model.supplement_steps = [media()]
        result = await self.decide()
        self.assertEqual((result["kind"], result["asset_id"]), ("voice", "V0001"))
        self.assertEqual(len(self.model.supplement_plans), 1)
        messages, definitions = self.model.supplement_plans[0]
        data = json.loads(messages[1]["content"])
        self.assertEqual(data["candidates"], {"stickers": [CANDIDATE], "voices": [VOICE_CANDIDATE]})
        self.assertEqual(data["supplement_state"], self.state)
        self.assertEqual(voices.queries[0][1:], (["old-voice"], ["old-group"]))
        self.assertIn("3～5", messages[0]["content"])
        self.assertIn("6～10", messages[0]["content"])
        self.assertIn("日文原句", messages[0]["content"])
        self.assertIn("剧情事实", messages[0]["content"])
        self.assertEqual(len(definitions), 1)

    async def test_kind_must_match_candidate_and_only_one_media_choice_is_allowed(self):
        self.planner.voices = FakeVoiceLibrary()
        multiple = media()
        multiple["tool_calls"].extend(supplement()["tool_calls"])
        for message in (media("voice", "happy"), media("sticker", "V0001"), media("none", "happy"),
                        media("voice", None), media("other", "V0001"), media("voice", "foreign"), multiple):
            with self.subTest(message=message):
                self.model.supplement_steps = [message] * 3
                with self.assertRaisesRegex(ModelError, "Invalid media"):
                    await self.decide()
        self.assertFalse(self.library.prepared)
        self.assertFalse(self.planner.voices.prepared)

    async def test_voice_can_be_used_when_stickers_are_unavailable(self):
        self.planner.stickers = None
        self.planner.voices = FakeVoiceLibrary()
        self.model.supplement_steps = [media()]
        self.assertEqual((await self.decide())["kind"], "voice")
        self.assertEqual(json.loads(self.model.supplement_plans[0][0][1]["content"])["candidates"]["stickers"], [])
