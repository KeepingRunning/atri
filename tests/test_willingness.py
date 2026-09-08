import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from atri_bot.bot import Bot
from atri_bot.config import Config
from atri_bot.context import build_willingness_context
from atri_bot.model import ModelError
from atri_bot.storage import GroupLog, read_jsonl
from atri_bot.types import Event, Receipt
from atri_bot.willingness import GateDecision, ReplyAssessment, ReplyConfig, ReplyWillingness
from test_bot import ROOT, raw


class GateTests(unittest.TestCase):
    def setUp(self):
        self.config = ReplyConfig()
        self.state = ReplyWillingness(self.config)
        self.now = 1000
        self.group = SimpleNamespace(sent_message_ids={"501"}, last_sent=None, activity=[])

    def check(self, text="今天又下雨了", *, mention=False, target=None, quote=None, **kwargs):
        data = raw(text=text, mention=mention, **kwargs)
        if target:
            data['message'].insert(0, {'type': 'at', 'data': {'qq': target}})
        if quote:
            data['message'].insert(0, {'type': 'reply', 'data': {'id': quote}})
        return self.state.evaluate(Event.parse(data), self.group, self.now)

    def test_real_at_and_verified_quote_bypass_frequency_and_backoff(self):
        self.config.frequency = 0
        self.state.finish_check(False, self.now)
        self.group.activity = [(self.now, True)] * 10
        for kwargs in ({'mention': True}, {'quote': '501'}):
            self.assertTrue(self.check('请先别说话', **kwargs).consider)
        # 这里只进入判断，不把“必须回复”写死在分数里。
        self.assertFalse(self.check('亚托莉帮我看看').consider)
        self.assertFalse(self.check('[CQ:at,qq=99] 帮我看看').consider)
        self.assertFalse(self.check('[CQ:reply,id=501]').consider)

    def test_other_addressees_and_media_do_not_accumulate_pressure(self):
        for _ in range(30):
            self.assertFalse(self.check('这个问题怎么处理？', target='98').consider)
            self.assertFalse(self.check('大家帮我看看', target='all').consider)
            self.assertFalse(self.check('怎么弄？', quote='unknown').consider)
            self.assertFalse(self.check('哈哈哈！').consider)
        data = raw(mention=False)
        data['message'] = [{'type': 'image', 'data': {'file': 'ATRI帮我看看.png'}}]
        self.assertFalse(self.state.evaluate(Event.parse(data), self.group, self.now).consider)
        self.assertEqual(len(self.state.pending), 0)

    def test_names_are_soft_and_english_names_have_boundaries(self):
        result = self.check('亚托莉这游戏怎么样')
        self.assertTrue(result.consider)
        self.assertEqual(result.reason, 'name_mentioned')
        self.state.pending.clear()
        result = self.check('patriotic atrial matrix')
        self.assertFalse(result.consider)
        self.assertEqual(result.factors['relation'], 0)

    def test_continuation_is_scoped_to_actual_recipient_and_expires(self):
        self.group.last_sent = {'time': self.now - 1, 'reply_to_user_id': '2'}
        result = self.check('然后呢')
        self.assertTrue(result.consider)
        self.assertEqual(result.reason, 'continuation')
        # 简短确认或数字可能是在回答机器人刚问的问题，应交给语义层辨别。
        self.assertTrue(self.check('好').consider)
        self.assertTrue(self.check('42').consider)
        self.assertEqual(self.check('然后呢', uid=3).reason, 'cooldown')
        self.now += 100
        result = self.check('然后呢')
        self.assertEqual(result.factors['relation'], 0)

    def test_conversation_pressure_expires_and_low_frequency_is_quieter(self):
        results = [self.check() for _ in range(5)]
        self.assertFalse(results[0].consider)
        self.assertTrue(results[-1].consider)
        self.now += 100
        self.assertFalse(self.check().consider)
        quiet = ReplyWillingness(ReplyConfig(frequency=.1))
        active = ReplyWillingness(ReplyConfig(frequency=1))
        event = Event.parse(raw(text='有人知道这个怎么处理吗？', mention=False))
        self.assertTrue(active.evaluate(event, self.group, self.now).consider)
        self.assertFalse(quiet.evaluate(event, self.group, self.now).consider)

    def test_recent_presence_lowers_score_without_counting_expired_activity(self):
        event = Event.parse(raw(text='今天学习了一件新的事情', mention=False))
        baseline = ReplyWillingness(self.config).evaluate(event, self.group, self.now)
        self.group.activity = [(self.now - 1, True)] * 6 + [(self.now, False)] * 4
        busy = ReplyWillingness(self.config).evaluate(event, self.group, self.now)
        self.assertLess(busy.score, baseline.score)
        self.now += 301
        expired = ReplyWillingness(self.config).evaluate(event, self.group, self.now)
        self.assertEqual(expired.score, baseline.score)

    def test_wait_backoff_then_cooldown_and_direct_interrupt(self):
        self.state.begin_check(self.now)
        self.state.finish_check(False, self.now)
        self.assertEqual(self.check('谁能帮我看看怎么弄？').reason, 'waiting_backoff')
        self.assertTrue(self.check('看看', mention=True).consider)
        self.state.begin_check(self.now)
        self.state.finish_check(True, self.now)
        self.assertEqual(self.check('谁能帮我看看怎么弄？').reason, 'cooldown')
        self.now += 11
        self.assertTrue(self.check('谁能帮我看看怎么弄？').consider)

    def test_old_messages_do_not_wake_a_group(self):
        data = raw()
        data['time'] = self.now - 121
        result = self.state.evaluate(Event.parse(data), self.group, self.now)
        self.assertEqual(result.reason, 'stale_message')
        self.assertFalse(result.consider)


class JudgingModel:
    def __init__(self):
        self.assessment = ReplyAssessment(85, '在向我提问')
        self.judgments, self.replies = [], []

    async def assess_reply(self, messages):
        self.judgments.append(messages)
        return self.assessment

    async def complete(self, messages):
        self.replies.append(messages)
        return '这是实际回复'


class WillingnessBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(ROOT, Path(self.tmp.name), groups=frozenset({'1', '2'}), self_id='99')
        self.model = JudgingModel()
        self.bot = Bot(self.config, self.model)
        self.sent = []

    async def asyncTearDown(self):
        await self.bot.close(timeout=.1)
        self.tmp.cleanup()

    async def send(self, gid, parts):
        self.sent.append((gid, parts))
        return Receipt('sent', str(500 + len(self.sent)))

    async def submit(self, **kwargs):
        return await self.bot.enqueue(Event.parse(raw(**kwargs)), self.send)

    async def test_ordinary_question_can_reply_and_decision_is_not_sent(self):
        receipt = await self.submit(text='谁能帮我看看这个报错怎么解决？', mention=False)
        self.assertEqual(receipt.status, 'sent')
        self.assertEqual(len(self.model.judgments), 1)
        self.assertEqual(len(self.model.replies), 1)
        self.assertEqual(self.sent[0][1][0]['data']['text'], '这是实际回复')
        rows = list(read_jsonl(self.bot.group('1').path))
        decisions = [r for r in rows if r['kind'] == 'willingness']
        self.assertEqual([r['stage'] for r in decisions], ['gate', 'judgment'])
        self.assertEqual(decisions[-1]['status'], 'reply')
        self.assertNotIn('在向我提问', str(self.model.replies))

    async def test_at_can_be_declined_and_noise_never_calls_model(self):
        self.model.assessment = ReplyAssessment(5, '对方要求安静')
        self.assertEqual((await self.submit(text='先别回复了')).reason, 'model_wait')
        for mid in range(2, 20):
            self.assertEqual((await self.submit(mid=mid, text='哈哈哈', mention=False)).reason, 'reaction_or_media')
        self.assertEqual(len(self.model.judgments), 1)
        self.assertFalse(self.sent)
        self.assertFalse(self.model.replies)

    async def test_judgment_failure_falls_back_without_poisoning_history(self):
        async def fail(messages):
            raise ModelError('bad result', 'invalid_reply_assessment', attempts=3)
        self.model.assess_reply = fail
        result = await self.submit()
        self.assertEqual(result.status, 'sent')
        self.assertEqual(len(self.sent), 1)
        group = self.bot.group('1')
        self.assertEqual([r['text'] for r in group.history if r.get('role') == 'assistant'], ['这是实际回复'])
        rows = list(read_jsonl(group.path))
        self.assertTrue(any(r.get('stage') == 'judgment' and r['status'] == 'failed' for r in rows))
        fallback = next(r for r in rows if r.get('stage') == 'fallback')
        self.assertEqual((fallback['source'], fallback['status'], fallback['attempts']), ('rule', 'reply', 3))
        self.assertEqual(self.bot.willingness['1'].wait_count, 0)

    async def test_programming_errors_do_not_trigger_rule_fallback(self):
        async def fail(messages):
            raise RuntimeError('unexpected bug')
        self.model.assess_reply = fail
        result = await self.submit()
        self.assertEqual(result.status, 'failed')
        self.assertFalse(self.sent)
        self.assertEqual(self.bot.willingness['1'].wait_count, 0)

    async def test_cancellation_does_not_trigger_rule_fallback(self):
        async def cancel(messages):
            raise asyncio.CancelledError
        self.model.assess_reply = cancel
        result = await self.submit()
        self.assertEqual(result.reason, 'shutdown')
        self.assertFalse(self.sent)
        self.assertEqual(self.bot.willingness['1'].wait_count, 0)

    async def test_quote_identity_survives_restart_and_is_group_scoped(self):
        await self.submit()
        await self.bot.close()
        self.config.reply.frequency = 0
        self.bot = Bot(self.config, self.model)
        for gid, expected in ((2, 'ignored'), (1, 'sent')):
            data = raw(mid=2, gid=gid, mention=False, text='还有一个问题')
            data['message'].insert(0, {'type': 'reply', 'data': {'id': '501'}})
            result = await self.bot.enqueue(Event.parse(data), self.send)
            self.assertEqual(result.status, expected)
        self.assertEqual(self.bot.group('1').last_sent['reply_to_user_id'], '2')

    async def test_failed_delivery_does_not_create_continuation_or_quote_identity(self):
        async def fail(gid, parts):
            return Receipt('unknown', '777')
        await self.bot.enqueue(Event.parse(raw()), fail)
        group = self.bot.group('1')
        self.assertIsNone(group.last_sent)
        self.assertFalse(group.sent_message_ids)
        self.assertFalse(any(own for _, own in group.activity))

    async def test_duplicate_is_not_reassessed_and_groups_have_separate_backoff(self):
        self.model.assessment = ReplyAssessment(10, '无需参与')
        await self.submit()
        self.assertEqual((await self.submit()).status, 'duplicate')
        self.assertEqual((await self.submit(mid=2, text='谁能帮忙？', mention=False)).reason, 'waiting_backoff')
        self.model.assessment = ReplyAssessment(85, '可以帮忙')
        self.assertEqual((await self.submit(gid=2, text='谁能帮忙？', mention=False)).status, 'sent')

    async def test_recent_presence_is_not_truncated_to_context_length(self):
        group = self.bot.group('1')
        for mid in range(100):
            group.append({'kind': 'incoming', 'key': str(mid), 'text': '正常聊天'})
        self.assertEqual(len(group.history), 51)
        self.assertEqual(len(group.activity), 100)
        restored = GroupLog(self.config.data, '1')
        self.assertEqual(len(restored.activity), 100)

    async def test_judging_respects_global_model_concurrency(self):
        self.bot.semaphore = asyncio.Semaphore(1)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def assess(messages):
            calls.append(messages)
            entered.set()
            await release.wait()
            return ReplyAssessment(0, '等待')
        self.model.assess_reply = assess
        first = self.bot.enqueue(Event.parse(raw()), self.send)
        other = self.bot.enqueue(Event.parse(raw(gid=2)), self.send)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(len(calls), 1)
        release.set()
        await asyncio.gather(first, other)
        self.assertEqual(len(calls), 2)


class AssessmentTests(unittest.TestCase):
    def test_persona_and_chat_history_are_data_instead_of_judgment_examples(self):
        persona = '使用自然中文，一至三句，不要输出 JSON。'
        event = Event.parse(raw(text='亚托莉在干嘛'))
        history = [{'key': str(i), 'role': 'assistant' if i % 2 else 'user',
                    'text': f'旧聊天-{i}', 'time': i + 1} for i in range(15)]
        history.append({'key': event.key, 'text': event.text})
        gate = GateDecision(True, 100, 42, 'at_self')
        messages = build_willingness_context(persona, event, history, gate)
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        self.assertNotIn(persona, messages[0]['content'])
        self.assertIn('{"score":90,"reason":', messages[0]['content'])
        data = json.loads(messages[1]['content'])
        self.assertEqual(data['persona_reference'], persona)
        self.assertEqual(len(data['history']), 12)
        self.assertEqual(data['history'][0]['text'], '旧聊天-3')
        self.assertEqual(data['history'][0]['timestamp'], 4)
        self.assertEqual(data['current_message']['text'], event.text)
        self.assertTrue(data['current_message']['mentions_self'])
        self.assertEqual(data['rule_observation']['score'], 100)

    def test_only_bounded_integer_score_and_short_reason_are_accepted(self):
        self.assertEqual(ReplyAssessment.parse({'score': 80, 'reason': '需要帮忙'}).score, 80)
        for value in ({'score': True, 'reason': 'x'}, {'score': 80.5, 'reason': 'x'},
                      {'score': 101, 'reason': 'x'}, {'score': -1, 'reason': 'x'},
                      {'score': 80, 'reason': ''}, {'score': 80, 'reason': 'x' * 161},
                      {'score': 80, 'reason': 'x', 'reply': '不得发送'}, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReplyAssessment.parse(value)
