import asyncio
from contextvars import Context
import io
import logging
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.logging_setup import LoggingConfig, ModuleFormatter, configure_logging, current_log_context, log_context, preview
from atri_bot.onebot import Peer
from atri_bot.types import Event, Receipt
from test_bot import ROOT, raw, daytime
from test_willingness import JudgingModel


class LogCapture:
    def __init__(self, **kwargs):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stream = io.StringIO()
        self.logger = logging.getLogger('atri')
        self.saved = (self.logger.handlers[:], self.logger.level, self.logger.propagate)
        self.saved_levels = {name: obj.level for name, obj in logging.Logger.manager.loggerDict.items()
                             if name.startswith('atri.') and isinstance(obj, logging.Logger)}
        # 不让被测初始化关闭测试外部的 handler。
        self.logger.handlers = []
        self.config = LoggingConfig(**kwargs)
        configure_logging(self.config, self.root, secrets=('secret-api-value', 'secret-onebot-value'), stream=self.stream)

    def close(self):
        for handler in self.logger.handlers[:]:
            handler.close()
        self.logger.handlers, level, self.logger.propagate = self.saved
        self.logger.setLevel(level)
        for name, obj in logging.Logger.manager.loggerDict.items():
            if name.startswith('atri.') and isinstance(obj, logging.Logger):
                obj.setLevel(self.saved_levels.get(name, logging.NOTSET))
        self.tmp.cleanup()

    def file_text(self):
        return (self.root / self.config.file).read_text(encoding='utf-8')


class FormatterTests(unittest.TestCase):
    def test_every_traceback_and_stack_line_has_the_full_log_prefix(self):
        capture = LogCapture(color='always')
        self.addCleanup(capture.close)
        with log_context(group_id='12', message_id='34', user_id='56'):
            try:
                raise ValueError('secret-api-value')
            except ValueError:
                logging.getLogger('atri.bot').exception('处理失败')
            logging.getLogger('atri.bot').error('调用位置', stack_info=True)
        plain = capture.file_text()
        self.assertIn('Traceback (most recent call last)', plain)
        self.assertNotIn('secret-api-value', plain)
        self.assertGreater(len(plain.splitlines()), 3)
        for line in plain.splitlines():
            self.assertRegex(line, r'^\d{4}-\d\d-\d\d .* \|ERROR\| bot \| g=12 m=34 u=56 \| ')
        self.assertEqual(re.sub(r'\x1b\[[0-9;]*m', '', capture.stream.getvalue()), plain)

    def test_module_colors_and_plain_file_keep_same_trace(self):
        capture = LogCapture(color='always')
        self.addCleanup(capture.close)
        with log_context(group_id='12', message_id='34', user_id='56'):
            logging.getLogger('atri.willingness').debug('意愿计算')
            logging.getLogger('atri.model').info('模型请求')
            logging.getLogger('atri.plugins.weather').info('天气插件')
        output = capture.stream.getvalue()
        self.assertIn('\033[1;38;5;213mwillingness', output)
        self.assertIn('\033[1;38;5;221mmodel', output)
        self.assertRegex(output, r'\x1b\[1;38;5;\d+mplugins\.weather')
        plain = capture.file_text()
        self.assertNotIn('\033', plain)
        self.assertEqual(plain.count('g=12 m=34 u=56'), 3)
        self.assertEqual(re.sub(r'\x1b\[[0-9;]*m', '', output), plain)

    def test_credentials_and_control_sequences_are_not_emitted(self):
        capture = LogCapture(color='never')
        self.addCleanup(capture.close)
        logger = logging.getLogger('atri.receive')
        logger.info('正文=%s', preview('secret-api-value\n\033[31m伪造颜色\u202e'))
        try:
            raise RuntimeError('secret-onebot-value')
        except RuntimeError:
            logger.exception('发送失败')
        for output in (capture.stream.getvalue(), capture.file_text()):
            self.assertNotIn('secret-api-value', output)
            self.assertNotIn('secret-onebot-value', output)
            self.assertNotIn('\033', output)
            self.assertNotIn('\u202e', output)
            self.assertIn('[REDACTED]', output)
            self.assertIn('RuntimeError', output)
            self.assertIn('\\n', output)

    def test_auto_color_honors_terminal_and_no_color(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True
        capture = LogCapture(file='')
        self.addCleanup(capture.close)
        for env, stream, expected in (({}, Terminal(), True), ({'NO_COLOR': ''}, Terminal(), False), ({}, io.StringIO(), False)):
            with patch.dict('os.environ', env, clear=True):
                configure_logging(capture.config, capture.root, stream=stream)
                logging.getLogger('atri.core').info('测试')
            self.assertEqual('\033' in stream.getvalue(), expected)

    def test_module_levels_can_make_willingness_more_verbose(self):
        capture = LogCapture(level='INFO', modules={'willingness': 'DEBUG'})
        self.addCleanup(capture.close)
        logging.getLogger('atri.willingness').debug('评分细节')
        logging.getLogger('atri.model').debug('模型细节')
        self.assertIn('评分细节', capture.stream.getvalue())
        self.assertNotIn('模型细节', capture.stream.getvalue())

    def test_rotating_file_is_bounded_and_latest_record_survives(self):
        capture = LogCapture(max_bytes=400, backup_count=2)
        self.addCleanup(capture.close)
        for i in range(30):
            logging.getLogger('atri.storage').info('轮转记录 %d %s', i, 'x' * 80)
        path = capture.root / capture.config.file
        self.assertEqual(len(list(path.parent.glob('atri.log*'))), 3)
        self.assertIn('轮转记录 29', capture.file_text())

    def test_preview_can_hide_or_limit_message_text(self):
        self.assertNotIn('私密内容', preview('私密内容', 0))
        self.assertIn('共6字符', preview('abcdef', 3))
        self.assertNotIn('def', preview('abcdef', 3))


class TraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.capture = LogCapture(color='never')

    async def asyncTearDown(self):
        self.capture.close()

    async def test_parallel_group_traces_and_each_queued_message_stay_separate(self):
        config = Config(ROOT, self.capture.root / 'chat', groups=frozenset({'1', '2'}), self_id='99')
        model = JudgingModel()
        entered, release = asyncio.Event(), asyncio.Event()
        active = 0
        async def complete(messages):
            nonlocal active
            active += 1
            if active == 2:
                entered.set()
            await release.wait()
            trace = current_log_context()
            logging.getLogger('atri.plugins.weather').info('插件处理 %s/%s', trace['group_id'], trace['message_id'])
            return '完成'
        model.complete = complete
        bot = Bot(config, model, now=daytime)
        async def send(gid, parts):
            return Receipt('sent', '500')
        try:
            one = bot.enqueue(Event.parse(raw(gid=1, mid=11)), send)
            queued = bot.enqueue(Event.parse(raw(gid=1, mid=12)), send)
            two = bot.enqueue(Event.parse(raw(gid=2, mid=21)), send)
            await asyncio.wait_for(entered.wait(), 1)
            release.set()
            await asyncio.gather(one, queued, two)
            self.assertEqual(current_log_context(), {})
        finally:
            release.set()
            await bot.close()
        output = self.capture.stream.getvalue()
        for gid, mid in ((1, 11), (1, 12), (2, 21)):
            matching = [line for line in output.splitlines() if f'插件处理 {gid}/{mid}' in line]
            self.assertEqual(len(matching), 1)
            self.assertIn(f'g={gid} m={mid} u=2', matching[0])
        for stage in ('收到消息', '入队', '出队', '01 参数', '04 内容计分', '05 未处理消息',
                      '06 发言占比', '07 汇总', '规则结果', '意愿上下文就绪', '模型判断结果',
                      '回复已生成', '发送结束', '处理结束'):
            self.assertIn(stage, output)

    async def test_acknowledgement_from_separate_reader_retains_original_message(self):
        class Socket:
            closed = False
            async def send_json(self, action):
                self.action = action
                asyncio.get_running_loop().call_soon(
                    peer.acknowledge,
                    {'echo': action['echo'], 'status': 'ok', 'retcode': 0, 'data': {'message_id': 800}},
                    context=Context())
        peer = Peer(Socket(), timeout=1)
        with log_context(group_id='1', message_id='123', user_id='456'):
            result = await peer.send('1', [{'type': 'text', 'data': {'text': '你好'}}])
        self.assertEqual(result.status, 'sent')
        lines = [line for line in self.capture.stream.getvalue().splitlines() if '[收到回执]' in line]
        self.assertEqual(len(lines), 1)
        self.assertIn('g=1 m=123 u=456', lines[0])
        self.assertFalse(peer.traces)
        self.assertFalse(peer.pending)

    async def test_filter_and_dedup_reasons_are_visible(self):
        config = Config(ROOT, self.capture.root / 'chat', groups=frozenset({'1'}), self_id='99')
        bot = Bot(config, JudgingModel(), now=daytime)
        async def forbidden_send(gid, parts):
            self.fail('Filtered messages must not send')
        try:
            await bot.enqueue(Event.parse(raw(gid=3)), forbidden_send)
            await bot.enqueue(Event.parse(raw(uid=99)), forbidden_send)
            await bot.enqueue(Event.parse(raw(text='哈哈哈', mention=False)), forbidden_send)
            await bot.enqueue(Event.parse(raw(text='哈哈哈', mention=False)), forbidden_send)
        finally:
            await bot.close()
        output = self.capture.stream.getvalue()
        for reason in ('群不在允许列表', '机器人自己的消息', '仅短反应或媒体', '已经记录'):
            self.assertIn(reason, output)
