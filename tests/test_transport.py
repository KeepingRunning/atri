import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.model import ChatModel, ModelError
from atri_bot.onebot import create_app
from atri_bot.types import Event
from atri_bot.willingness import ReplyConfig
from test_bot import ROOT, raw


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = []
        self.responses = []
        self.response = {"choices": [{"message": {"content": "模型回复"}}]}
        self.status = 200
        async def provider(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer test-api-key")
            self.requests.append(await request.json())
            response = self.responses.pop(0) if self.responses else self.response
            return web.json_response(response, status=self.status)
        app = web.Application()
        app.router.add_post("/v1/chat/completions", provider)
        self.provider = TestServer(app)
        await self.provider.start_server()
        self.session = aiohttp.ClientSession()
        self.config = Config(ROOT, Path(self.tmp.name), groups=frozenset({"1"}), self_id="99",
                             token="test-token", api_key="test-api-key", model="test-model",
                             base_url=str(self.provider.make_url('/v1')), action_timeout=.2,
                             reply=ReplyConfig(mode="at_only"))
        self.bot = Bot(self.config, ChatModel(self.config, self.session))
        self.client = TestClient(TestServer(create_app(self.config, self.bot)))
        await self.client.start_server()
        self.headers = {"Authorization": "Bearer test-token", "X-Self-ID": "99"}

    async def asyncTearDown(self):
        await self.client.close()
        await self.session.close()
        await self.provider.close()
        self.tmp.cleanup()

    async def test_full_websocket_http_provider_and_qq_ack_flow(self):
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw(text="大家刚才在聊天", mention=False))
        await ws.send_json(raw(mid=2, text="请回复"))
        action = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertEqual(action['action'], 'send_group_msg')
        self.assertEqual(action['params'], {'group_id': 1, 'message': [
            {'type': 'text', 'data': {'text': '模型回复'}}]})
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(set(request), {'model', 'messages', 'max_tokens'})
        self.assertEqual(request['model'], 'test-model')
        self.assertIn(self.config.read_personal_info(), request['messages'][0]['content'])
        self.assertIn('大家刚才在聊天', str(request))
        ack = {'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 500}}
        await ws.send_json(ack)
        await ws.send_json(ack)
        await asyncio.wait_for(self.bot.queues['1'].join(), 1)
        self.assertEqual(self.bot.group('1').last_receipts['99:1:2']['status'], 'sent')
        self.assertEqual(len([r for r in self.bot.group('1').history if r.get('role') == 'assistant']), 1)
        await ws.close()

    async def test_auth_identity_and_duplicate_connection(self):
        for headers, status in (({}, 401), ({**self.headers, 'X-Self-ID': '98'}, 403),
                                ({**self.headers, 'X-Client-Role': 'Event'}, 400)):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as error:
                await self.client.ws_connect(self.config.ws_path, headers=headers)
            self.assertEqual(error.exception.status, status)
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as error:
            await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        self.assertEqual(error.exception.status, 409)
        await ws.close()

    async def test_disconnect_does_not_record_unsent_reply(self):
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw())
        await asyncio.wait_for(ws.receive_json(), 2)
        await ws.close()
        await asyncio.wait_for(self.bot.queues['1'].join(), 1)
        self.assertEqual(self.bot.group('1').last_receipts['99:1:1']['status'], 'unknown')
        self.assertFalse(any(r.get('role') == 'assistant' for r in self.bot.group('1').history))

    async def test_invalid_events_and_failed_ack(self):
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_str('not json')
        await ws.send_json(['not an event'])
        await ws.send_json(raw(gid=5))
        await ws.send_json(raw())
        action = await asyncio.wait_for(ws.receive_json(), 2)
        await ws.send_json({'echo': action['echo'], 'status': 'failed', 'retcode': 100})
        await asyncio.wait_for(self.bot.queues['1'].join(), 1)
        self.assertEqual(self.bot.group('1').last_receipts['99:1:1']['status'], 'failed')
        self.assertNotIn('5', self.bot.groups)
        await ws.close()

    async def test_provider_errors_empty_reply_and_output_limit(self):
        self.config.output_limit_field = 'max_completion_tokens'
        self.assertEqual(await self.bot.model.complete([{'role': 'user', 'content': 'hi'}]), '模型回复')
        self.assertIn('max_completion_tokens', self.requests[-1])
        self.response = {'choices': [{'message': {'content': None}}]}
        with self.assertRaises(ModelError):
            await self.bot.model.complete([])
        self.status = 401
        self.response = {'secret': 'test-api-key'}
        with self.assertRaises(ModelError) as error:
            await self.bot.model.complete([])
        self.assertNotIn('test-api-key', str(error.exception))
        self.assertEqual(error.exception.code, 'model_http_401')

    async def test_willingness_http_judgment_then_reply_and_ack(self):
        self.config.reply.mode = 'willingness'
        self.config.reply.judgment_model = 'fast-judge'
        self.config.output_limit_field = 'max_completion_tokens'
        self.responses = [
            {'choices': [{'message': {'content': json.dumps({'score': 90, 'reason': '可以回答'})}}]},
            {'choices': [{'message': {'content': '接话结果'}}]},
        ]
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw(text='谁能帮我看看这个报错怎么处理？', mention=False))
        action = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertEqual(action['params']['message'][0]['data']['text'], '接话结果')
        self.assertEqual([r['model'] for r in self.requests], ['fast-judge', 'test-model'])
        self.assertEqual(self.requests[0]['max_completion_tokens'], 256)
        self.assertEqual(self.requests[1]['max_completion_tokens'], 512)
        self.assertIn('群聊参与判断器', self.requests[0]['messages'][0]['content'])
        await ws.send_json({'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 800}})
        await asyncio.wait_for(self.bot.queues['1'].join(), 1)
        self.assertIn('800', self.bot.group('1').sent_message_ids)
        self.assertEqual(self.bot.group('1').last_sent['reply_to_user_id'], '2')
        await ws.close()

    async def test_willingness_invalid_or_waiting_http_response_never_sends(self):
        self.config.reply.mode = 'willingness'
        async def forbidden_sender(gid, parts):
            self.fail('Judgment must never be sent as a chat reply')
        for mid, content in enumerate(('不是JSON', '{"score":true,"reason":"x"}',
                                       '```json\n{"score":90,"reason":"x"}\n```',
                                       '{"score":10,"reason":"保持安静"}'), 1):
            self.response = {'choices': [{'message': {'content': content}}]}
            result = await self.bot.enqueue(Event.parse(raw(mid=mid)), forbidden_sender)
            self.assertEqual(result.reason, 'model_wait' if mid == 4 else 'invalid_reply_assessment')
        self.assertEqual(len(self.requests), 4)
        self.assertFalse(any(r.get('role') == 'assistant' for r in self.bot.group('1').history))
