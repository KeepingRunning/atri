import asyncio
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
from test_bot import ROOT, raw


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = []
        self.response = {"choices": [{"message": {"content": "模型回复"}}]}
        self.status = 200
        async def provider(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer test-api-key")
            self.requests.append(await request.json())
            return web.json_response(self.response, status=self.status)
        app = web.Application()
        app.router.add_post("/v1/chat/completions", provider)
        self.provider = TestServer(app)
        await self.provider.start_server()
        self.session = aiohttp.ClientSession()
        self.config = Config(ROOT, Path(self.tmp.name), groups=frozenset({"1"}), self_id="99",
                             token="test-token", api_key="test-api-key", model="test-model",
                             base_url=str(self.provider.make_url('/v1')), action_timeout=.2)
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
