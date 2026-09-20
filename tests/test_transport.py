import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

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
from atri_bot.tools import ToolSpec, ToolResult
from atri_bot.history_tools import object_schema
from test_bot import ROOT, raw, daytime
from test_tools import call


def tool_response(*calls, content=None, reasoning=None):
    message = {'role': 'assistant', 'content': content, 'tool_calls': list(calls)}
    if reasoning is not None:
        message['reasoning_content'] = reasoning
    return {'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.requests = []
        self.responses = []
        self.response = {"choices": [{"message": {"content": "模型回复"}}]}
        self.status = 200
        self.on_request = None
        async def provider(request):
            self.assertEqual(request.headers.get("Authorization"), "Bearer test-api-key")
            self.requests.append(await request.json())
            if self.on_request is not None:
                self.on_request()
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
        self.bot = Bot(self.config, ChatModel(self.config, self.session), now=daytime)
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
        self.assertEqual(set(request), {'model', 'messages', 'max_tokens', 'tools', 'tool_choice'})
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

    async def test_health_command_uses_ack_channel_at_night_without_model_or_chat(self):
        self.bot.schedule.now = lambda: daytime().replace(hour=2)
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw(text="/health", mention=False))
        action = await asyncio.wait_for(ws.receive_json(), 1)
        self.assertIn("ATRI 服务：正常", str(action["params"]["message"]))
        await ws.send_json({"echo": action["echo"], "status": "ok", "retcode": 0,
                            "data": {"message_id": 701}})
        async with asyncio.timeout(1):
            while self.bot.command_tasks:
                await asyncio.sleep(.001)
        self.assertFalse(self.requests)
        self.assertFalse(self.bot.group("1").history)
        self.assertEqual(list(read_jsonl(self.bot.group("1").path))[-1]["status"], "sent")
        await ws.send_json(raw(mid=2, text="睡眠期间普通消息", mention=True))
        async with asyncio.timeout(1):
            while "99:1:2" not in self.bot.group("1").seen:
                await asyncio.sleep(.001)
        self.assertFalse(self.requests)
        self.assertIsNone(self.bot.group("1").last_sent)
        await ws.close()

    async def test_http_health_exposes_local_status_and_worker_failure(self):
        response = await self.client.get("/healthz")
        status = await response.json()
        self.assertEqual(response.status, 200)
        self.assertFalse(status["connected"])
        self.assertEqual(status["status"], "ok")
        self.assertGreaterEqual(status["uptime_seconds"], 0)
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        self.bot.tasks["1"] = task
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["failed_workers"], 1)

    async def test_local_schedule_is_attached_to_real_reply_flow_without_schedule_http(self):
        self.assertEqual(self.requests, [])
        plan = self.bot.schedule.current_plan()
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw(text='在干嘛？'))
        action = await asyncio.wait_for(ws.receive_json(), 1)
        self.assertEqual([r['model'] for r in self.requests], ['test-model'])
        self.assertNotIn('response_format', self.requests[0])
        prompt = self.requests[0]['messages'][0]['content']
        self.assertIn(plan['macro']['title'], prompt)
        self.assertEqual(json.loads(prompt.split('【当前十分钟小日程】\n')[1]), plan['micro'][3])
        self.assertIn(self.config.read_personal_info(), prompt)
        await ws.send_json({'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 900}})
        await asyncio.wait_for(self.bot.queues['1'].join(), 1)
        self.assertEqual(self.bot.group('1').last_receipts['99:1:1']['status'], 'sent')
        task = self.bot.schedule.task
        await self.client.close()
        self.assertTrue(task.done())

    async def test_real_websocket_sleep_event_is_recorded_without_model_then_wakes(self):
        now = daytime().replace(hour=7, minute=59)
        self.bot.schedule.now = lambda: now
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw(text='睡觉时的 @'))
        async with asyncio.timeout(1):
            while '1' not in self.bot.groups or '99:1:1' not in self.bot.group('1').seen:
                await asyncio.sleep(.001)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.bot.queues)
        now = now.replace(hour=8, minute=0)
        await ws.send_json(raw(mid=2, text='起床后的新消息'))
        action = await asyncio.wait_for(ws.receive_json(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn('起床后的新消息', self.requests[0]['messages'][-1]['content'])
        await ws.send_json({'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 901}})
        await asyncio.wait_for(self.bot.queues['1'].join(), 1)
        self.assertEqual(self.bot.group('1').last_receipts['99:1:2']['status'], 'sent')
        await ws.close()

    async def test_sleep_disabled_allows_http_and_onebot_send_across_midnight(self):
        self.config.schedule.sleep_enabled = False
        now = daytime().replace(hour=23, minute=59, second=59)
        self.bot.schedule.now = lambda: now
        def cross_midnight():
            nonlocal now
            now += timedelta(seconds=1)
        self.on_request = cross_midnight
        ws = await self.client.ws_connect(self.config.ws_path, headers=self.headers)
        await ws.send_json(raw(text='临时夜间测试'))
        action = await asyncio.wait_for(ws.receive_json(), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(now.hour, 0)
        self.assertEqual(action['action'], 'send_group_msg')
        await ws.send_json({'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 902}})
        await asyncio.wait_for(self.bot.queues['1'].join(), 1)
        self.assertEqual(self.bot.group('1').last_receipts['99:1:1']['status'], 'sent')
        await ws.close()

    async def test_judgment_failure_after_midnight_stops_http_retries_and_rule_fallback(self):
        self.config.reply.mode = 'willingness'
        now = daytime().replace(hour=23, minute=59, second=59)
        midnight = (now + timedelta(seconds=1)).replace(microsecond=0)
        self.bot.schedule.now = lambda: now
        def cross_midnight():
            nonlocal now
            now = midnight
        self.on_request = cross_midnight
        async def forbidden_sender(gid, parts):
            self.fail('No midnight reply may be sent')
        for mid, status, content in ((1, 200, '不合法JSON'), (2, 503, '服务不可用')):
            now = midnight - timedelta(seconds=1)
            self.status = status
            self.response = {'choices': [{'message': {'content': content}}]}
            result = await self.bot.enqueue(Event.parse(raw(mid=mid)), forbidden_sender)
            self.assertEqual((result.status, result.reason), ('ignored', 'sleeping'))
            self.assertEqual(len(self.requests), mid)  # First attempt only; no retry HTTP.
        rows = list(read_jsonl(self.bot.group('1').path))
        self.assertFalse(any(r.get('stage') == 'fallback' for r in rows))

    async def test_context_build_crossing_midnight_never_submits_reply_http(self):
        now = daytime().replace(hour=23, minute=59, second=59)
        self.bot.schedule.now = lambda: now
        original = self.bot.schedule.context
        def cross_midnight():
            nonlocal now
            result = original()
            now += timedelta(seconds=1)
            return result
        async def forbidden_sender(gid, parts):
            self.fail('No midnight reply may be sent')
        with patch.object(self.bot.schedule, 'context', side_effect=cross_midnight):
            result = await self.bot.enqueue(Event.parse(raw()), forbidden_sender)
        self.assertEqual(result.reason, 'sleeping')
        self.assertEqual(self.requests, [])
        # The request guard is scoped to bot processing, not manual API diagnostics.
        self.assertEqual(await self.bot.model.complete([{'role': 'user', 'content': 'test'}]), '模型回复')
        self.assertEqual(len(self.requests), 1)

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
        self.assertNotIn('tools', self.requests[0])
        self.assertIn('tools', self.requests[1])
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

    async def test_tool_search_then_context_then_reply_only_final_text_is_sent_and_remembered(self):
        group = self.bot.group('1')
        group.append({'kind': 'incoming', 'key': '99:1:10', 'message_id': '10', 'user_id': '2',
                      'nickname': '测试群友', 'text': '我喜欢香草冰淇淋', 'timestamp': group.now() - 7200})
        self.config.groups = frozenset({'1', '2'})
        self.bot.group('2').append({'kind': 'incoming', 'key': '99:2:10', 'message_id': '10',
                                   'user_id': '2', 'text': '其他群的秘密冰淇淋', 'timestamp': group.now() - 7200})
        self.responses = [
            tool_response(call('search_chat_history', {'query': '冰淇淋'}), content='检索中的中间文本', reasoning='内部推理'),
            tool_response(call('get_chat_context', {'record_id': 'L1', 'before': 0, 'after': 1}, 'call_2')),
            {'choices': [{'message': {'content': '你说过喜欢香草冰淇淋。'}}]},
        ]
        sent = []
        async def sender(gid, parts):
            sent.append(parts[0]['data']['text'])
            return Receipt('sent', '700')
        result = await self.bot.enqueue(Event.parse(raw(text='我以前说喜欢吃什么？')), sender)
        self.assertEqual(result.status, 'sent')
        self.assertEqual(sent, ['你说过喜欢香草冰淇淋)'])
        self.assertEqual(len(self.requests), 3)
        self.assertNotIn('我喜欢香草冰淇淋', str(self.requests[0]['messages']))
        second = self.requests[1]['messages']
        self.assertEqual(second[-2]['reasoning_content'], '内部推理')
        self.assertEqual(second[-1]['tool_call_id'], 'call_1')
        self.assertEqual(json.loads(second[-1]['content'])['data']['items'][0]['record_id'], 'L1')
        self.assertNotIn('其他群的秘密', str(self.requests))
        self.assertEqual(self.requests[-1]['tool_choice'], 'none')
        rows = list(read_jsonl(group.path))
        audits = [r for r in rows if r['kind'] == 'tool']
        self.assertEqual([r['tool'] for r in audits], ['search_chat_history', 'get_chat_context'])
        self.assertTrue(all(r['status'] == 'ok' for r in audits))
        self.assertNotIn('内部推理', str(rows))
        self.assertNotIn('检索中的中间文本', str(group.history))
        self.assertEqual(len([r for r in group.history if r.get('role') == 'assistant']), 1)
        # A new user turn contains only normal history, not the previous tool protocol.
        await self.bot.enqueue(Event.parse(raw(mid=2, text='谢谢')), sender)
        self.assertFalse(any(m['role'] == 'tool' for m in self.requests[-1]['messages']))
        self.assertNotIn('内部推理', str(self.requests[-1]['messages']))

    async def test_invalid_tool_arguments_and_unknown_tool_are_returned_as_errors(self):
        self.responses = [tool_response(call('unknown', {}, 'one'),
                                        call('search_chat_history', {'query': 'x', 'group_id': '2'}, 'two')),
                          {'choices': [{'message': {'content': '暂时没能查到。'}}]}]
        result = await self.bot.enqueue(Event.parse(raw()), lambda gid, parts: asyncio.sleep(0, Receipt('sent', '701')))
        self.assertEqual(result.status, 'sent')
        errors = [json.loads(m['content'])['error']['code'] for m in self.requests[1]['messages'] if m['role'] == 'tool']
        self.assertEqual(errors, ['unknown_tool', 'invalid_arguments'])
        self.assertNotIn('2', self.bot.groups)

    async def test_batch_call_budget_and_forced_final_response(self):
        self.config.tools.max_calls = 1
        self.responses = [tool_response(call('search_chat_history', {'query': 'x'}, 'one'),
                                        call('search_chat_history', {'query': 'y'}, 'two')),
                          {'choices': [{'message': {'content': '没有检索到相关消息。'}}]}]
        result = await self.bot.enqueue(Event.parse(raw()), lambda gid, parts: asyncio.sleep(0, Receipt('sent', '702')))
        self.assertEqual(result.status, 'sent')
        self.assertEqual(self.requests[1]['tool_choice'], 'none')
        messages = [json.loads(m['content']) for m in self.requests[1]['messages'] if m['role'] == 'tool']
        self.assertTrue(messages[0]['ok'])
        self.assertEqual(messages[1]['error']['code'], 'call_limit')

    async def test_provider_ignoring_final_limit_fails_without_sending_tool_text(self):
        self.config.tools.max_rounds = 1
        self.responses = [tool_response(call('search_chat_history', {'query': 'x'}, 'one')),
                          tool_response(call('search_chat_history', {'query': 'y'}, 'two'), content='不得发送')]
        async def forbidden(gid, parts):
            self.fail('Tool preamble must not be sent')
        result = await self.bot.enqueue(Event.parse(raw()), forbidden)
        self.assertEqual(result.reason, 'tool_round_limit')
        rows = list(read_jsonl(self.bot.group('1').path))
        self.assertEqual(len([r for r in rows if r['kind'] == 'tool']), 1)
        self.assertFalse(any(r['kind'] == 'delivery' for r in rows))

    async def test_malformed_and_duplicate_call_envelopes_never_execute(self):
        original = call('search_chat_history', {'query': 'x'})
        for mid, response in enumerate((tool_response(original, original),
                tool_response({'id': 'one', 'type': 'shell', 'function': {'name': 'x', 'arguments': '{}'}})), 1):
            self.responses = [response]
            result = await self.bot.enqueue(Event.parse(raw(mid=mid)), lambda gid, parts: asyncio.sleep(0))
            self.assertEqual(result.reason, 'invalid_tool_calls')
        self.assertFalse(any(r['kind'] == 'tool' for r in read_jsonl(self.bot.group('1').path)))

    async def test_midnight_after_call_request_or_during_tool_prevents_followup_http(self):
        now = daytime().replace(hour=23, minute=59, second=59)
        self.bot.schedule.now = lambda: now
        def midnight():
            nonlocal now
            now += timedelta(seconds=1)
        self.on_request = midnight
        self.responses = [tool_response(call('search_chat_history', {'query': 'x'}))]
        async def forbidden(gid, parts):
            self.fail('Must not send at midnight')
        result = await self.bot.enqueue(Event.parse(raw()), forbidden)
        self.assertEqual(result.reason, 'sleeping')
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(any(r['kind'] == 'tool' for r in read_jsonl(self.bot.group('1').path)))
        # Midnight occurs inside an async tool handler instead of the HTTP response.
        now -= timedelta(seconds=1)
        self.on_request = None
        async def late(ctx, args):
            midnight()
            return ToolResult(True, {'items': []})
        self.bot.tool_registry.register(ToolSpec('late', '测试', object_schema({}), late))
        self.responses = [tool_response(call('late', {}))]
        result = await self.bot.enqueue(Event.parse(raw(mid=2)), forbidden)
        self.assertEqual(result.reason, 'sleeping')
        self.assertEqual(len(self.requests), 2)

    async def test_disabled_tools_use_plain_reply_request(self):
        self.config.tools.enabled = False
        result = await self.bot.enqueue(Event.parse(raw()), lambda gid, parts: asyncio.sleep(0, Receipt('sent', '703')))
        self.assertEqual(result.status, 'sent')
        self.assertNotIn('tools', self.requests[0])
        self.assertNotIn('【历史检索工具】', self.requests[0]['messages'][0]['content'])
