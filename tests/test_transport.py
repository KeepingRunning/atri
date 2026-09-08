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
from atri_bot.types import Event, Receipt
from atri_bot.storage import read_jsonl
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
        self.config.thinking = 'disabled'
        self.responses = [
            {'choices': [{'message': {'content': json.dumps({'score': 90, 'reason': '可以回答'})}}]},
            {'choices': [{'message': {'content': '接话结果'}}]},
        ]
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw(text='谁能帮我看看这个报错怎么处理？', mention=False))
        action = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertEqual(action['params']['message'][0]['data']['text'], '接话结果')
        self.assertEqual([r['model'] for r in self.requests], ['fast-judge', 'test-model'])
        self.assertEqual([r['thinking'] for r in self.requests], [{'type': 'disabled'}] * 2)
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
            invalid = {'choices': [{'message': {'content': content}}]}
            waiting = {'choices': [{'message': {'content': '{"score":10,"reason":"保持安静"}'}}]}
            self.responses = [invalid, invalid, waiting] if mid < 4 else [waiting]
            result = await self.bot.enqueue(Event.parse(raw(mid=mid)), forbidden_sender)
            self.assertEqual(result.reason, 'model_wait')
        self.assertEqual(len(self.requests), 10)
        self.assertFalse(any(r.get('role') == 'assistant' for r in self.bot.group('1').history))

    async def test_second_attempt_recovers_and_keeps_original_prompt_unchanged(self):
        self.responses = [
            {'choices': [{'message': {'content': '应该回复'}}]},
            {'choices': [{'message': {'content': '{"score":80,"reason":"可以回应"}'}}]},
        ]
        messages = [{'role': 'system', 'content': '只判断接话意愿。'}, {'role': 'user', 'content': '你好'}]
        assessment = await self.bot.model.assess_reply(messages)
        self.assertEqual(assessment.score, 80)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(messages[0]['content'], '只判断接话意愿。')
        self.assertIn('重新判断', self.requests[1]['messages'][0]['content'])
        self.assertNotIn('应该回复', str(self.requests[1]['messages']))
        self.assertTrue(all('response_format' not in r for r in self.requests))

    async def test_exhausted_judgment_uses_rule_score_for_reply_and_wait(self):
        self.config.reply.mode = 'willingness'
        self.config.groups = frozenset({'1', '2'})
        failed = {'choices': [{'message': {'content': '应当回应，但没有输出 JSON'}}]}
        reply = {'choices': [{'message': {'content': '这是重新生成的聊天回复'}}]}
        self.responses = [failed, failed, failed, reply, failed, failed, failed]
        sent = []
        async def sender(gid, parts):
            sent.append(parts[0]['data']['text'])
            return Receipt('sent', '801')
        high = await self.bot.enqueue(Event.parse(raw(text='亚托莉在干嘛', mention=False)), sender)
        low = await self.bot.enqueue(Event.parse(raw(gid=2, text='有人在吗？', mention=False)), sender)
        self.assertEqual(high.status, 'sent')
        self.assertEqual(low.reason, 'rule_fallback_wait')
        self.assertEqual(sent, ['这是重新生成的聊天回复'])
        self.assertEqual(len(self.requests), 7)
        self.assertEqual(self.bot.willingness['1'].wait_count, 0)
        self.assertEqual(self.bot.willingness['2'].wait_count, 1)
        for gid, score, status in [('1', 70, 'reply'), ('2', 45, 'wait')]:
            rows = list(read_jsonl(self.bot.group(gid).path))
            fallback = next(r for r in rows if r.get('stage') == 'fallback')
            self.assertEqual((fallback['score'], fallback['threshold'], fallback['status'], fallback['attempts']),
                             (score, 60, status, 3))
        # 降级回复是独立生成；失败的评分文本不会混入聊天上下文。
        self.assertNotIn('应当回应，但没有输出 JSON', str(self.requests[3]['messages']))

    async def test_http_failure_is_retried_exactly_twice(self):
        self.status = 503
        with self.assertRaises(ModelError) as raised:
            await self.bot.model.assess_reply([{'role': 'user', 'content': '你好'}])
        self.assertEqual(raised.exception.code, 'model_http_503')
        self.assertEqual(raised.exception.attempts, 3)
        self.assertEqual(len(self.requests), 3)
