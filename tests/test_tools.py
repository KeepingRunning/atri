import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest

from atri_bot.config import Config
from atri_bot.history_tools import ChatArchive, history_registry, local_time, object_schema
from atri_bot.model import ModelRequestBlocked
from atri_bot.tools import ToolContext, ToolRegistry, ToolResult, ToolSession, ToolSpec, ToolsConfig
from tests.support.factories import call


class HistoryToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'messages.jsonl'
        self.now = 1789092000
        def incoming(mid, text, ago, **extra):
            return dict(kind='incoming', key=f'99:1:{mid}', time=self.now - 1,
                        timestamp=self.now - ago, message_id=str(mid), user_id='2', nickname='测试', text=text, **extra)
        self.rows = [
            incoming(1, '我喜欢香草冰淇淋', 7200),
            {'kind': 'willingness', 'key': '99:1:1', 'time': self.now - 7100, 'stage': 'judgment',
             'score': 80, 'reason': '可以接话', 'api_key': 'private-config-must-not-leak'},
            {'kind': 'delivery', 'key': '99:1:1', 'time': self.now - 7090, 'status': 'pending', 'text': '待发的秘密'},
            {'kind': 'delivery', 'key': '99:1:1', 'time': self.now - 7080, 'status': 'sent',
             'text': '我也想吃香草冰淇淋', 'message_id': '501', 'reply_to_message_id': '1'},
            {'kind': 'delivery', 'key': '99:1:3', 'time': self.now - 7000, 'status': 'failed', 'text': '失败的秘密'},
            incoming(4, '买了巧克力冰淇淋', 8000),  # Received out of order.
            incoming(5, '未来冰淇淋', -100),
            {**incoming(6, '其他群的冰淇淋', 5000), 'key': '99:2:6'},
            incoming(900, '当前触发问题冰淇淋', 0),
            {**incoming(7, 'ATRI 旧记录', 0), 'timestamp': 0},
        ]
        self.write_rows()
        self.audits = []
        self.archive = ChatArchive(self.path, group_id='1', self_id='99', now=self.now, exclude_key='99:1:900')
        self.context = ToolContext('1', '2', '99', '99:1:900', self.now, self.archive, lambda: None, self.audits.append)
        self.registry = history_registry()
        self.config = ToolsConfig()

    def write_rows(self):
        self.path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.rows))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def execute(self, name, args):
        return await self.registry.execute(name, json.dumps(args, ensure_ascii=False), self.context, self.config)

    async def test_old_messages_are_searchable_but_unsent_future_current_and_foreign_are_not(self):
        result = await self.execute('search_chat_history', {'query': '冰淇淋'})
        self.assertTrue(result.ok)
        self.assertEqual([r['record_id'] for r in result.data['items']], ['L4', 'L1', 'L6'])
        self.assertEqual(result.data['matched_total'], 3)
        self.assertEqual([r['user_id'] for r in result.data['items']], ['99', '2', '2'])
        self.assertTrue(all(r['timestamp'] < self.now - 3600 for r in result.data['items']))
        self.assertNotIn('秘密', result.to_json())

    async def test_literal_matching_filters_and_pagination(self):
        result = await self.execute('search_chat_history', {'query': '香草 冰淇淋', 'user_id': '2',
            'since': local_time(self.now - 7200), 'until': local_time(self.now - 7200)})
        self.assertEqual([r['record_id'] for r in result.data['items']], ['L1'])
        result = await self.execute('search_chat_history', {'query': '冰淇淋', 'limit': 1})
        self.assertTrue(result.data['has_more'])
        self.assertEqual(result.data['next_offset'], 1)
        result = await self.execute('search_chat_history', {'query': '冰淇淋', 'limit': 1, 'offset': 1})
        self.assertEqual(result.data['items'][0]['record_id'], 'L1')
        result = await self.execute('search_chat_history', {'query': '.*'})
        self.assertEqual(result.data['items'], [])
        result = await self.execute('search_chat_history', {'query': 'atri'})
        self.assertEqual(result.data['items'][0]['record_id'], 'L10')

    async def test_context_excludes_processing_rows_and_failed_deliveries(self):
        result = await self.execute('get_chat_context', {'record_id': 'L4', 'before': 1, 'after': 1})
        self.assertEqual([r['record_id'] for r in result.data['items']], ['L1', 'L4', 'L6'])
        for record_id in ('L2', 'L3', 'L5', 'L7', 'L8', 'L9', 'L999'):
            with self.subTest(record_id=record_id):
                result = await self.execute('get_chat_context', {'record_id': record_id})
                self.assertEqual(result.error['code'], 'record_not_found')

    async def test_event_log_projection_and_sent_message_id_lookup(self):
        result = await self.execute('search_event_logs', {'message_id': '1'})
        self.assertEqual([r['record_id'] for r in result.data['items']], ['L2', 'L3', 'L4'])
        self.assertNotIn('秘密', result.to_json())
        self.assertNotIn('private-config', result.to_json())
        result = await self.execute('search_event_logs', {'message_id': '501'})
        self.assertEqual(result.data['items'][0]['status'], 'sent')

    async def test_read_only_corrupt_and_incomplete_files_report_partial(self):
        with self.path.open('ab') as f:
            f.write(b'not-json\n{"kind": "unfinished"')
        original = self.path.read_bytes()
        result = await self.execute('search_chat_history', {'query': '冰淇淋'})
        self.assertTrue(result.ok)
        self.assertTrue(result.meta['partial'])
        self.assertEqual(result.meta['skipped_lines'], 2)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    async def test_tool_arguments_cannot_change_scope_or_open_files(self):
        for args in ({'query': '冰淇淋', 'group_id': '2'}, {'query': '冰淇淋', 'path': '/etc/passwd'},
                     {'query': '冰淇淋', 'limit': True}, {'query': '冰淇淋', 'limit': 21},
                     {'query': '冰淇淋', 'offset': -1}, {'query': 'x' * 201}, []):
            with self.subTest(args=args):
                result = await self.execute('search_chat_history', args)
                self.assertEqual(result.error['code'], 'invalid_arguments')
        result = await self.execute('search_chat_history', {})
        self.assertEqual(result.error['code'], 'empty_query')
        result = await self.execute('search_chat_history', {'since': '2026-09-11T09:00:00'})
        self.assertEqual(result.error['code'], 'invalid_time')
        result = await self.execute('search_chat_history', {'since': local_time(self.now + 1)})
        self.assertEqual(result.error['code'], 'invalid_time_range')
        result = await self.execute('get_chat_context', {'record_id': '../../other'})
        self.assertEqual(result.error['code'], 'invalid_arguments')

    async def test_long_match_excerpt_and_result_limit_keep_valid_json_and_anchor(self):
        self.rows[0]['text'] = '开头' * 2000 + '目标关键词' + '末尾' * 2000
        self.write_rows()
        result = await self.execute('search_chat_history', {'query': '目标关键词'})
        item = result.data['items'][0]
        self.assertIn('目标关键词', item['text'])
        self.assertTrue(item['text_truncated'])
        self.assertGreater(item['text_offset'], 0)
        long = ToolResult(True, {'items': [{'record_id': f'L{i}', 'text': 'x' * 700} for i in range(20)],
                                 'anchor': 'L19'})
        bounded = long.bounded(1800)
        self.assertTrue(bounded.ok)
        self.assertTrue(bounded.meta['truncated'])
        self.assertIn('L19', [i['record_id'] for i in bounded.data['items']])
        self.assertLessEqual(len(bounded.to_json()), 1800)
        self.assertTrue(json.loads(bounded.to_json())['ok'])
        self.assertEqual(len(long.data['items']), 20)

    async def test_pagination_limit_never_returns_an_invalid_next_offset(self):
        result = ChatArchive._result({'items': [{'text': 'x' * 800}] * 3, 'offset': 1000,
            'has_more': True, 'next_offset': 1003}, {'scanned_lines': 2000, 'skipped_lines': 0})
        self.assertTrue(result.data['has_more'])
        self.assertIsNone(result.data['next_offset'])
        self.assertTrue(result.meta['pagination_limited'])
        bounded = result.bounded(1500)
        self.assertIsNone(bounded.data['next_offset'])
        self.assertTrue(bounded.meta['pagination_limited'])

    async def test_reusable_schema_execution_timeout_cache_and_audit(self):
        runs = []
        async def extension(ctx, args):
            runs.append(ctx.group_id)
            return ToolResult(True, {'items': [{'text': args['payload']['text']}]})
        spec = ToolSpec('example_extension', '扩展示例', object_schema({'payload': object_schema({
            'text': {'type': 'string', 'maxLength': 10}}, ('text',))}, ('payload',)), extension)
        self.registry.register(spec)
        with self.assertRaises(ValueError):
            self.registry.register(spec)
        session = ToolSession(self.registry, self.context, self.config)
        args = {'payload': {'text': '测试'}}
        one = json.loads(await session.execute(call(spec.name, args)))
        two = json.loads(await session.execute(call(spec.name, args, 'call_2')))
        self.assertEqual(one, two)
        self.assertEqual(runs, ['1'])
        self.assertTrue(self.audits[-1]['cached'])
        self.assertNotIn('测试', str(self.audits))
        bad = await self.execute(spec.name, {'payload': {'text': '测试', 'unknown': 1}})
        self.assertEqual(bad.error['code'], 'invalid_arguments')
        async def slow(ctx, args):
            await asyncio.sleep(10)
        self.registry.register(ToolSpec('slow', '超时', object_schema({}), slow))
        self.config.timeout = .01
        result = await self.execute('slow', {})
        self.assertEqual(result.error['code'], 'tool_timeout')

    async def test_call_limit_unknown_malformed_arguments_and_cancellation(self):
        self.config.max_calls = 1
        session = ToolSession(self.registry, self.context, self.config)
        result = json.loads(await session.execute(call('unknown', {})))
        self.assertEqual(result['error']['code'], 'unknown_tool')
        result = json.loads(await session.execute(call('search_chat_history', {'query': '冰淇淋'}, 'next')))
        self.assertEqual(result['error']['code'], 'call_limit')
        for args in ('not json', '{"query":"x","query":"y"}', '{"limit":NaN}', '{"limit":1e999}', '{"query":'):
            result = await self.registry.execute('search_chat_history', args, self.context, self.config)
            self.assertEqual(result.error['code'], 'invalid_arguments')
        async def cancel(ctx, args):
            raise asyncio.CancelledError()
        self.registry.register(ToolSpec('cancel', '取消', object_schema({}), cancel))
        with self.assertRaises(asyncio.CancelledError):
            await self.execute('cancel', {})

    async def test_sleep_guard_does_not_become_tool_failure(self):
        blocked = False
        def guard():
            if blocked:
                raise ModelRequestBlocked('sleep')
        async def late(ctx, args):
            nonlocal blocked
            blocked = True
            return ToolResult(True, {'items': []})
        self.registry.register(ToolSpec('late', '跨午夜', object_schema({}), late))
        context = ToolContext('1', '2', '99', '99:1:900', self.now, self.archive, guard, self.audits.append)
        with self.assertRaises(ModelRequestBlocked):
            await ToolSession(self.registry, context, self.config).execute(call('late', {}))
        self.assertEqual(self.audits, [])

    async def test_timeout_signals_background_scanner_to_stop(self):
        ended = threading.Event()
        def scan(args, stop):
            stop.wait(1)
            if stop.is_set():
                ended.set()
            return ToolResult(True, {'items': []})
        self.archive.search = scan
        self.config.timeout = .02
        result = await self.execute('search_chat_history', {'query': 'x'})
        self.assertEqual(result.error['code'], 'tool_timeout')
        self.assertTrue(await asyncio.to_thread(ended.wait, 1))


class ToolsConfigTests(unittest.TestCase):
    def test_defaults_custom_values_and_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('')
            self.assertTrue(Config.load(path).tools.enabled)
            path.write_text('[tools]\nenabled=false\nmax_rounds=3\nmax_calls=6\ntimeout=2.5\nmax_result_chars=4000\n')
            config = Config.load(path).tools
            self.assertEqual((config.enabled, config.max_rounds, config.max_calls, config.timeout, config.max_result_chars),
                             (False, 3, 6, 2.5, 4000))
            for setting in ('enabled="yes"', 'max_rounds=0', 'max_calls=99', 'max_calls=true',
                            'max_result_chars=1', 'timeout=nan', 'timeout=true', 'timeout=0', 'typo=1'):
                path.write_text('[tools]\n' + setting)
                with self.subTest(setting=setting), self.assertRaises(ValueError):
                    Config.load(path)
