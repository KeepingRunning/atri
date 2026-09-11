import json
from pathlib import Path
import tempfile
import unittest

from atri_bot.context import build_conversation, build_willingness_context
from atri_bot.storage import GroupLog
from atri_bot.types import Event
from atri_bot.willingness import GateDecision
from test_bot import raw


class HistoryWindowTests(unittest.TestCase):
    def setUp(self):
        self.now = 10000
        self.event = Event.parse(raw(text='当前问题'))
        self.gate = GateDecision(True, 70, 42, 'name_mentioned')

    def selected(self, history, *, now=None, history_seconds=3600):
        now = self.now if now is None else now
        reply = build_conversation('人设', self.event, history, now=now, history_seconds=history_seconds)
        judgment = build_willingness_context('人设', self.event, history, self.gate,
                                             now=now, history_seconds=history_seconds)
        reply_texts = [r['content'] if r['role'] == 'assistant' else json.loads(r['content'])['text']
                       for r in reply[1:-1]]
        data = json.loads(judgment[1]['content'])
        self.assertEqual(reply_texts, [r['text'] for r in data['history']])
        self.assertEqual(data['evaluated_at'], now)
        self.assertEqual(json.loads(reply[-1]['content'])['text'], '当前问题')
        return reply_texts

    def test_exact_cutoff_original_message_time_and_confirmed_reply_time(self):
        rows = [
            {'key': 'old', 'timestamp': 6399.99, 'time': 10000, 'text': '延迟送达的旧消息'},
            {'key': 'boundary', 'timestamp': 6400, 'time': 10000, 'text': '恰好一小时前'},
            {'key': 'recent', 'timestamp': 6400.01, 'time': 10000, 'text': '刚好在窗口内'},
            {'role': 'assistant', 'timestamp': 10000, 'time': 6399.99, 'text': '过期回复'},
            {'role': 'assistant', 'time': 6400, 'text': '窗口边界的回复'},
            {'role': 'assistant', 'time': 9999, 'text': '新的回复'},
            {'key': self.event.key, 'timestamp': 10000, 'text': '当前消息不得重复'},
        ]
        self.assertEqual(self.selected(rows), ['恰好一小时前', '刚好在窗口内', '窗口边界的回复', '新的回复'])
        self.assertEqual(self.selected(rows, now=13600), [])

    def test_missing_invalid_timestamp_falls_back_to_record_time(self):
        rows = [{'key': str(i), 'timestamp': value, 'time': 9999, 'text': str(i)}
                for i, value in enumerate((None, 0, -1, True, '9999', float('nan'), float('inf')))]
        rows += [{'key': 'missing', 'text': '无可信时间'},
                 {'key': 'future', 'timestamp': 10001, 'time': 9999, 'text': '未来时间'}]
        self.assertEqual(self.selected(rows), [str(i) for i in range(7)])
        self.assertEqual(self.selected(rows, now=10001)[-1], '未来时间')

    def test_window_setting_is_respected_without_filling_from_older_records(self):
        rows = [{'key': 'old', 'timestamp': 9500, 'text': '超出自定义窗口'},
                {'key': 'new', 'timestamp': 9750, 'text': '窗口内'}]
        self.assertEqual(self.selected(rows, history_seconds=300), ['窗口内'])
        self.assertEqual(self.selected(rows, now=11000, history_seconds=300), [])

    def test_sparse_old_history_is_not_used_even_when_fewer_than_50(self):
        rows = [{'key': 'old', 'timestamp': 1, 'text': '很久以前在群里待机'},
                {'role': 'assistant', 'time': 2, 'text': '以前的测试回复'}]
        self.assertEqual(self.selected(rows), [])

    def test_storage_prunes_out_of_order_timestamps_and_restores_only_time_window(self):
        with tempfile.TemporaryDirectory() as directory:
            now = 10000
            store = GroupLog(Path(directory), '1', now=lambda: now)
            for key, stamp in (('new', 9900), ('edge', 6400), ('late', 3000)):
                store.append({'kind': 'incoming', 'key': key, 'timestamp': stamp, 'text': key})
            self.assertEqual([r['key'] for r in store.history], ['new', 'edge'])
            now = 10001
            store.append({'kind': 'incoming', 'key': 'current', 'text': '无原始时间用落盘时间'})
            self.assertEqual([r['key'] for r in store.history], ['new', 'current'])
            restored = GroupLog(Path(directory), '1', now=lambda: now)
            self.assertEqual([r['key'] for r in restored.history], ['new', 'current'])
            self.assertEqual(restored.seen, {'new', 'edge', 'late', 'current'})
            self.assertEqual(len(store.path.read_text().splitlines()), 4)
            now += 3601
            restored.prune_history()
            self.assertFalse(restored.history)
