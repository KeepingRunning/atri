import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.model import ModelError
from atri_bot.onebot import Peer
from atri_bot.schedule import ScheduleConfig, ScheduleService, time_labels, validate_routine, window_start
from atri_bot.storage import read_jsonl
from atri_bot.types import Event, Receipt
from atri_bot.willingness import ReplyAssessment, ReplyConfig
from test_bot import ROOT, raw

ZONE = ZoneInfo('Asia/Shanghai')


def instant(text='2026-09-11T18:35:00'):
    value = datetime.fromisoformat(text)
    return value.replace(tzinfo=ZONE) if value.tzinfo is None else value


def routine(rid='sample', labels=None):
    return {'schema_version': 1, 'id': rid, 'title': rid + '-日常', 'summary': '阅读后做笔记。',
            'duration_minutes': 120, 'time_basis': 'relative_minutes', 'scene': '住处',
            'participants': ['亚托莉'], 'preconditions': ['手边有书和纸笔'],
            'suggested_time_of_day': labels or ['不限'], 'source': {'secret': '原文不入上下文'},
            'review': {'status': 'pending_user_review'},
            'macro': [{'start_minute': 0, 'end_minute': 60, 'activity': '阅读', 'intent': '读一会儿书'},
                      {'start_minute': 60, 'end_minute': 120, 'activity': '笔记', 'intent': '整理笔记'}],
            'micro': [{'start_minute': n * 10, 'end_minute': (n + 1) * 10,
                       'activity': '阅读' if n < 6 else '笔记',
                       'detail': f'{rid}-关注点{n:02d}', 'mood': '认真'} for n in range(12)]}


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / 'routines'
        self.library.mkdir()
        self.write(routine('a'))
        self.write(routine('b'))
        self.now = instant()
        self.choices = []
        self.service = self.new_service()

    def write(self, data, filename=None):
        (self.library / (filename or data['id'] + '.json')).write_text(json.dumps(data), encoding='utf-8')

    def choose(self, pool):
        self.choices.append([r['id'] for r in pool])
        return pool[0]

    def new_service(self):
        return ScheduleService(ScheduleConfig(routines_dir='routines'), self.root / 'data',
                               root=self.root, now=lambda: self.now, choose=self.choose)

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def test_approved_library_validates_and_covers_every_awake_window(self):
        library = [json.loads(p.read_text()) for p in (ROOT / 'resources/daily_routines').glob('*.json')]
        self.assertGreater(len(library), 0)
        for data in library:
            with self.subTest(routine=data['id']):
                validate_routine(data)
        for hour in range(8, 24, 2):
            labels = time_labels(instant().replace(hour=hour)) | {'不限'}
            self.assertTrue(any(labels.intersection(r['suggested_time_of_day']) for r in library))

    async def test_time_matching_for_all_windows_and_avoid_immediate_repeat(self):
        for p in self.library.glob('*.json'):
            p.unlink()
        for i, label in enumerate(('清晨', '上午', '中午', '下午', '傍晚', '夜晚')):
            self.write(routine(str(i), [label]))
        for hour, expected in ((8, '0'), (10, '1'), (12, '2'), (14, '3'), (16, '3'),
                               (18, '4'), (20, '5'), (22, '5')):
            self.now = instant().replace(hour=hour)
            self.assertEqual(self.service.current_plan()['routine_id'], expected)
        self.assertEqual(len(self.choices), 8)

    async def test_stable_window_no_pregeneration_and_current_micro_only(self):
        self.service.context()
        for stamp in ('18:39:59', '18:40:00', '19:50:00', '19:59:59'):
            self.now = instant('2026-09-11T' + stamp)
            text = self.service.context()
            index = (self.now.hour % 2 * 60 + self.now.minute) // 10
            for n in range(12):
                self.assertEqual(f'a-关注点{n:02d}' in text, n == index)
            self.assertNotIn('原文不入上下文', text)
            self.assertNotIn('pending_user_review', text)
            self.assertIn('阅读', text)
            self.assertIn('笔记', text)
        self.assertEqual(self.choices, [['a', 'b']])
        self.now = instant('2026-09-11T20:00:00')
        self.assertIn('b-关注点00', self.service.context())
        self.assertEqual(self.choices, [['a', 'b'], ['b']])

    async def test_restart_restores_selection_without_random_or_full_generated_cache(self):
        text = self.service.context()
        restored = self.new_service()
        self.assertEqual(restored.context(), text)
        self.assertEqual(len(self.choices), 1)
        cache = json.loads(self.service.path.read_text())
        self.assertEqual(cache['version'], 2)
        self.assertEqual(cache['routine_id'], 'a')
        self.assertNotIn('micro', cache)
        self.assertNotIn('plans', cache)
        self.assertFalse(self.service.path.with_suffix('.tmp').exists())

    async def test_legacy_and_corrupt_caches_are_replaced(self):
        self.service.path.parent.mkdir(parents=True)
        for content in ('{"version":1,"plans":{"old":"generated"}}', '{torn', '[]'):
            self.service.path.write_text(content)
            restored = self.new_service()
            self.assertEqual(restored.current_plan()['routine_id'], 'a')
            self.assertEqual(json.loads(restored.path.read_text())['version'], 2)

    async def test_deleted_or_changed_templates_are_reloaded_at_restart_or_next_window(self):
        self.service.current_plan()
        (self.library / 'a.json').unlink()
        self.assertEqual(self.new_service().current_plan()['routine_id'], 'b')
        self.now = instant('2026-09-11T20:00:00')
        self.assertEqual(self.service.current_plan()['routine_id'], 'b')
        self.write(routine('b', ['上午']))
        self.now = instant('2026-09-11T22:00:00')
        self.assertEqual(self.service.current_plan()['routine_id'], '__rest__')

    async def test_bad_files_duplicates_and_wrong_time_are_skipped(self):
        (self.library / 'broken.json').write_text('{')
        self.write(routine('a'), 'duplicate.json')
        bad = routine('bad')
        bad['micro'].pop()
        self.write(bad)
        bad = routine('unknown', ['午夜'])
        self.write(bad)
        self.write(routine('morning', ['上午']))
        self.assertEqual(self.service.current_plan()['routine_id'], 'b')
        self.assertEqual(self.choices, [['b']])

    async def test_sleep_boundaries_timezone_and_empty_library_fallback(self):
        for p in self.library.glob('*.json'):
            p.unlink()
        for stamp, asleep in (('2026-09-11T23:59:59', False), ('2026-09-12T00:00:00', True),
                              ('2026-09-12T07:59:59', True), ('2026-09-12T08:00:00', False)):
            self.now = instant(stamp).astimezone(timezone.utc)
            plan = self.service.current_plan()
            self.assertEqual(plan['sleeping'], asleep)
            self.assertEqual(plan['routine_id'], '__sleep__' if asleep else '__rest__')
            self.assertEqual(datetime.fromisoformat(plan['window_end']) - datetime.fromisoformat(plan['window_start']),
                             timedelta(hours=2))
        self.assertFalse(self.choices)
        self.assertEqual(window_start(instant('2026-09-12T00:01:00')), instant('2026-09-12T00:00:00'))

    async def test_sleep_does_not_read_library_and_disabled_day_context_is_empty(self):
        self.service.config.enabled = False
        self.assertEqual(self.service.context(), '')
        self.now = instant('2026-09-12T00:00:00')
        with patch.object(self.service, 'read_library', side_effect=AssertionError('must not read library')):
            self.assertTrue(self.service.blocks_reply())
            self.assertEqual(self.service.current_plan()['macro']['title'], '睡觉')

    async def test_disk_failure_keeps_in_memory_selection(self):
        with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
            self.assertEqual(self.service.current_plan()['routine_id'], 'a')
            self.assertIn('a-关注点03', self.service.context())
        self.assertEqual(len(self.choices), 1)

    async def test_sleep_switch_replaces_cached_sleep_with_night_routine_and_restores(self):
        self.now = instant('2026-09-12T00:35:00')
        self.write(routine('night', ['夜晚']))
        self.write(routine('morning', ['上午']))
        self.assertEqual(self.service.current_plan()['routine_id'], '__sleep__')
        self.service.config.sleep_enabled = False
        plan = self.service.current_plan()
        self.assertFalse(plan['sleeping'])
        self.assertIn('night', self.choices[-1])
        self.assertNotIn('morning', self.choices[-1])
        self.assertNotIn('不回复消息', self.service.context())
        self.assertFalse(self.service.blocks_reply(received_at=self.now - timedelta(days=1),
                                                    timestamp=self.now.timestamp()))
        restored = self.new_service()
        restored.config.sleep_enabled = False
        self.assertEqual(restored.current_plan()['routine_id'], plan['routine_id'])
        self.service.config.enabled = False
        self.assertEqual(self.service.context(), '')
        self.assertFalse(self.service.blocks_reply())
        self.service.config.sleep_enabled = True
        self.assertTrue(self.service.blocks_reply())
        self.assertEqual(self.service.current_plan()['routine_id'], '__sleep__')

    async def test_background_start_is_idempotent_and_clock_jump_is_seen_without_poll(self):
        self.service.start()
        task = self.service.task
        self.service.start()
        self.assertIs(task, self.service.task)
        self.now = instant('2026-09-11T20:00:00')
        self.assertIn('b-关注点00', self.service.context())
        await self.service.close()
        self.assertTrue(task.done())
        self.assertIsNone(self.service.task)

    async def test_invalid_routine_grid_is_rejected(self):
        for modify in (lambda r: r['micro'][0].update(start_minute=True),
                       lambda r: r['macro'][0].update(end_minute=65),
                       lambda r: r['micro'][2].update(activity='不存在'),
                       lambda r: r.update(id='__sleep__')):
            candidate = routine()
            modify(candidate)
            with self.assertRaises(ValueError):
                validate_routine(candidate)

    async def test_legacy_model_config_is_ignored_and_paths_are_validated(self):
        path = self.root / 'config.toml'
        path.write_text('[schedule]\nmodel="old-generator"\nroutines_dir="routines"\n')
        config = Config.load(path)
        self.assertTrue(config.schedule.enabled)
        self.assertEqual(config.schedule.routines_dir, 'routines')
        self.assertFalse(hasattr(config.schedule, 'model'))
        path.write_text('[schedule]\nsleep_enabled=false\n')
        self.assertFalse(Config.load(path).schedule.sleep_enabled)
        path.write_text('[schedule]\nsleep_enabled="false"\n')
        with self.assertRaisesRegex(ValueError, 'schedule.sleep_enabled'):
            Config.load(path)
        for config in (ScheduleConfig(enabled='true'), ScheduleConfig(sleep_enabled='false'), ScheduleConfig(timezone='UTC'),
                       ScheduleConfig(routines_dir='')):
            with self.assertRaises(ValueError):
                config.validate()


class FakeModel:
    def __init__(self):
        self.calls, self.judgments = [], []

    async def complete(self, messages, *, tool_session=None):
        self.calls.append(deepcopy(messages))
        return '我在呢。'

    async def assess_reply(self, messages):
        self.judgments.append(deepcopy(messages))
        return ReplyAssessment(90, '可以接话')


class ScheduleBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        directory = Path(self.tmp.name) / 'routines'
        directory.mkdir()
        (directory / 'fixture.json').write_text(json.dumps(routine()))
        self.now = instant()
        self.config = Config(ROOT, Path(self.tmp.name) / 'data', groups=frozenset({'1', '2'}), self_id='99',
                             reply=ReplyConfig(mode='at_only'),
                             schedule=ScheduleConfig(routines_dir=str(directory)))
        self.model = FakeModel()
        self.bot = Bot(self.config, self.model, now=lambda: self.now)
        self.sent = []

    async def asyncTearDown(self):
        await self.bot.close(timeout=.1)
        self.tmp.cleanup()

    async def sender(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt('sent', str(100 + len(self.sent)))

    async def submit(self, **kwargs):
        return await self.bot.enqueue(Event.parse(raw(**kwargs)), self.sender)

    async def test_reply_has_persona_macro_current_micro_but_history_and_judge_do_not(self):
        self.config.reply.mode = 'willingness'
        self.assertEqual((await self.submit()).status, 'sent')
        prompt = self.model.calls[0][0]['content']
        self.assertIn(self.config.read_personal_info(), prompt)
        self.assertIn('sample-关注点03', prompt)
        self.assertNotIn('sample-关注点04', prompt)
        self.assertNotIn('sample-关注点', str(self.model.judgments))
        self.assertNotIn('sample-关注点', self.bot.group('1').path.read_text())
        self.assertEqual((await self.submit(gid=2)).status, 'sent')
        self.assertIn('sample-关注点03', self.model.calls[1][0]['content'])

    async def test_sleep_ignores_at_quote_name_ordinary_and_records_without_model_or_queue(self):
        self.now = instant('2026-09-12T00:00:00')
        self.config.reply.mode = 'willingness'
        for mid, text, mention in ((1, '你好', True), (2, '亚托莉救命', False), (3, '闲聊', False)):
            self.assertEqual((await self.submit(mid=mid, text=text, mention=mention)).reason, 'sleeping')
        data = raw(mid=4, mention=False)
        data['message'].insert(0, {'type': 'reply', 'data': {'id': '101'}})
        self.assertEqual((await self.bot.enqueue(Event.parse(data), self.sender)).reason, 'sleeping')
        self.assertEqual(len(self.bot.group('1').history), 4)
        self.assertEqual(len([r for r in read_jsonl(self.bot.group('1').path) if r['kind'] == 'schedule']), 4)
        self.assertFalse(self.bot.queues)
        self.assertFalse(self.model.calls or self.model.judgments or self.sent)
        self.now = instant('2026-09-12T08:00:00')
        self.assertEqual((await self.submit()).status, 'duplicate')
        self.assertEqual((await self.submit(mid=5)).status, 'sent')
        self.assertEqual(len(self.sent), 1)

    async def test_sleep_boundaries_still_apply_with_day_schedule_disabled(self):
        self.config.schedule.enabled = False
        for mid, stamp, expected in ((1, '2026-09-11T23:59:59', 'sent'),
                                     (2, '2026-09-12T00:00:00', 'ignored'),
                                     (3, '2026-09-12T07:59:59', 'ignored'),
                                     (4, '2026-09-12T08:00:00', 'sent')):
            self.now = instant(stamp)
            self.assertEqual((await self.submit(mid=mid)).status, expected)
        self.assertEqual(len(self.model.calls), 2)
        self.assertNotIn('【虚构日常背景】', str(self.model.calls))

    async def test_onebot_replayed_sleep_messages_are_not_answered_after_waking(self):
        self.now = instant('2026-09-12T08:00:00')
        data = raw()
        data['time'] = instant('2026-09-12T07:59:59').timestamp()
        self.assertEqual((await self.bot.enqueue(Event.parse(data), self.sender)).reason, 'sleeping')
        self.assertFalse(self.model.calls)

    async def test_waiting_for_reply_semaphore_reads_new_micro_at_generation(self):
        self.bot.semaphore = asyncio.Semaphore(0)
        self.now = instant('2026-09-11T18:39:59')
        future = self.bot.enqueue(Event.parse(raw()), self.sender)
        await asyncio.sleep(0)
        self.now = instant('2026-09-11T18:40:00')
        self.bot.semaphore.release()
        self.assertEqual((await future).status, 'sent')
        self.assertIn('sample-关注点04', self.model.calls[0][0]['content'])
        self.assertNotIn('sample-关注点03', self.model.calls[0][0]['content'])

    async def test_semaphore_wait_crossing_midnight_never_calls_either_model(self):
        for mid, mode in enumerate(('at_only', 'willingness'), 1):
            self.config.reply.mode = mode
            self.bot.semaphore = asyncio.Semaphore(0)
            self.now = instant('2026-09-11T23:59:59')
            future = self.bot.enqueue(Event.parse(raw(mid=mid)), self.sender)
            await asyncio.sleep(0)
            self.now = instant('2026-09-12T00:00:00')
            self.bot.semaphore.release()
            self.assertEqual((await future).reason, 'sleeping')
        self.assertFalse(self.model.calls or self.model.judgments or self.sent)

    async def test_generation_and_queued_messages_crossing_midnight_never_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.model.complete
        async def slow(messages, *, tool_session=None):
            entered.set()
            await release.wait()
            return await original(messages, tool_session=tool_session)
        self.model.complete = slow
        self.now = instant('2026-09-11T23:59:59')
        first = self.bot.enqueue(Event.parse(raw()), self.sender)
        await asyncio.wait_for(entered.wait(), 1)
        second = self.bot.enqueue(Event.parse(raw(mid=2)), self.sender)
        self.now = instant('2026-09-12T00:00:00')
        release.set()
        self.assertEqual([r.reason for r in await asyncio.gather(first, second)], ['sleeping', 'sleeping'])
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(self.sent)
        self.assertFalse(any(r.get('role') == 'assistant' for r in self.bot.group('1').history))

    async def test_queued_previous_day_message_is_discarded_even_if_resumed_after_eight(self):
        self.bot.semaphore = asyncio.Semaphore(0)
        self.now = instant('2026-09-11T23:59:59')
        first = self.bot.enqueue(Event.parse(raw()), self.sender)
        second = self.bot.enqueue(Event.parse(raw(mid=2)), self.sender)
        await asyncio.sleep(0)
        self.now = instant('2026-09-12T08:00:00')
        self.bot.semaphore.release()
        self.assertEqual([r.reason for r in await asyncio.gather(first, second)], ['sleeping', 'sleeping'])
        self.assertFalse(self.model.calls or self.sent)

    async def test_willingness_result_or_failure_crossing_midnight_cannot_reply_or_fallback(self):
        self.config.reply.mode = 'willingness'
        for mid, fail in enumerate((False, True), 1):
            async def assess(messages):
                self.now = instant('2026-09-12T00:00:00')
                if fail:
                    raise ModelError('parse failure', 'invalid_reply_assessment')
                return ReplyAssessment(100, 'direct mention')
            self.model.assess_reply = assess
            self.now = instant('2026-09-11T23:59:59')
            self.assertEqual((await self.submit(mid=mid)).reason, 'sleeping')
        self.assertFalse(self.model.calls or self.sent)
        self.assertFalse(any(r.get('stage') == 'fallback' for r in read_jsonl(self.bot.group('1').path)))

    async def test_send_checks_clock_again_when_pending_delivery_write_crosses_midnight(self):
        self.now = instant('2026-09-11T23:59:59')
        group = self.bot.group('1')
        original = group.append
        def slow_write(row):
            original(row)
            if row.get('kind') == 'delivery' and row.get('status') == 'pending':
                self.now = instant('2026-09-12T00:00:00')
        with patch.object(group, 'append', side_effect=slow_write):
            self.assertEqual((await self.submit()).reason, 'sleeping')
        self.assertFalse(self.sent)
        rows = list(read_jsonl(self.bot.group('1').path))
        self.assertTrue(any(r.get('stage') == '发送前' for r in rows))
        self.assertEqual(self.bot.group('1').last_receipts['99:1:1']['status'], 'ignored')
        self.assertFalse(any(r.get('role') == 'assistant' for r in self.bot.group('1').history))

    async def test_onebot_transport_has_final_guard_before_submission(self):
        ws = AsyncMock()
        ws.closed = False
        peer = Peer(ws, .1, can_send=lambda: False)
        receipt = await peer.send('1', [{'type': 'text', 'data': {'text': '禁止发出'}}])
        self.assertEqual((receipt.status, receipt.reason), ('ignored', 'sleeping'))
        ws.send_json.assert_not_awaited()
        self.assertFalse(peer.pending)
