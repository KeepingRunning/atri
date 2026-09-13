"""按上海时间从本地日常库选取两小时安排；默认00:00–08:00睡眠，可配置关闭。"""
from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .logging_setup import preview

log = logging.getLogger('atri.schedule')
WINDOW = timedelta(hours=2)
TIME_LABELS = frozenset({'清晨', '上午', '中午', '下午', '傍晚', '夜晚', '不限'})
CHAT_BACKGROUND = '''【虚构日常背景】
下面是亚托莉在虚构世界里的当前安排，可用于自然回答“在干嘛”和延续话题。
原作伙伴只是背景人物，不能把群友当作夏生、主人或恋人，也不能据此编造与群友共有的经历。
只有当前十分钟格描述此刻的活动；大日程中的未来安排还没发生，过去安排也不证明已经完成。
不需要每次汇报日程，优先回应当前聊天；不要把原文来源、配置或审核记录说给群友听。'''


@dataclass
class ScheduleConfig:
    enabled: bool = True
    timezone: str = 'Asia/Shanghai'
    routines_dir: str = 'resources/daily_routines'
    sleep_enabled: bool = True

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError('schedule.enabled must be a boolean')
        if type(self.sleep_enabled) is not bool:
            raise ValueError('schedule.sleep_enabled must be a boolean')
        if not isinstance(self.routines_dir, str) or not self.routines_dir.strip():
            raise ValueError('schedule.routines_dir must be a nonempty path')
        if self.timezone != 'Asia/Shanghai':
            raise ValueError('schedule.timezone currently supports Asia/Shanghai only')
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError('Schedule timezone data is unavailable') from None


def window_start(now):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Schedule time must include timezone')
    return now.replace(hour=now.hour // 2 * 2, minute=0, second=0, microsecond=0)


def time_labels(start):
    if start.hour < 8:
        return set()
    if start.hour == 8:
        return {'清晨', '上午'}
    if start.hour < 12:
        return {'上午'}
    if start.hour < 14:
        return {'中午'}
    if start.hour < 18:
        return {'下午'}
    if start.hour < 20:
        return {'傍晚'}
    return {'夜晚'}


def validate_routine(data):
    """只验证运行时所需的时间和文字协议，不让一个坏文件拖垮整个库。"""
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('expected routine schema_version=1')
    if data.get('duration_minutes') != 120 or data.get('time_basis') != 'relative_minutes':
        raise ValueError('routine must cover 120 relative minutes')
    for key in ('id', 'title', 'summary', 'scene'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f'invalid routine {key}')
    if data['id'].startswith('__'):
        raise ValueError('reserved routine ID')
    for key in ('suggested_time_of_day', 'participants', 'preconditions'):
        value = data.get(key)
        if not isinstance(value, list) or not value or any(not isinstance(s, str) or not s.strip() for s in value):
            raise ValueError(f'invalid routine {key}')
    if not set(data['suggested_time_of_day']) <= TIME_LABELS:
        raise ValueError('unknown suggested_time_of_day')
    macro, micro = data.get('macro'), data.get('micro')
    if not isinstance(macro, list) or not 2 <= len(macro) <= 5 or not isinstance(micro, list) or len(micro) != 12:
        raise ValueError('expected 2–5 macro activities and 12 micro slots')
    end = 0
    for block in macro:
        if (not isinstance(block, dict) or type(block.get('start_minute')) is not int
                or type(block.get('end_minute')) is not int or block['start_minute'] != end
                or not end < block['end_minute'] <= 120 or block['end_minute'] % 10):
            raise ValueError('macro activities must continuously cover the ten-minute grid')
        for key in ('activity', 'intent'):
            if not isinstance(block.get(key), str) or not block[key].strip():
                raise ValueError(f'invalid macro {key}')
        end = block['end_minute']
    if end != 120:
        raise ValueError('macro must end at minute 120')
    for i, slot in enumerate(micro):
        if (not isinstance(slot, dict) or type(slot.get('start_minute')) is not int
                or type(slot.get('end_minute')) is not int
                or slot['start_minute'] != i * 10 or slot['end_minute'] != (i + 1) * 10):
            raise ValueError('micro slots must be twelve consecutive ten-minute intervals')
        parent = next(b for b in macro if b['start_minute'] <= i * 10 < b['end_minute'])
        if slot.get('activity') != parent['activity']:
            raise ValueError('micro activity does not match its macro parent')
        for key in ('detail', 'mood'):
            if not isinstance(slot.get(key), str) or not slot[key].strip():
                raise ValueError(f'invalid micro {key}')


def make_plan(start, routine=None, *, sleeping=None):
    sleeping = start.hour < 8 if sleeping is None else sleeping
    if routine is None:
        title = '睡觉' if sleeping else '自由休息'
        summary = '00:00–08:00 睡觉，不回复消息。' if sleeping else '暂时没有合适的日常，随意休息。'
        macro = [{'start_minute': 0, 'end_minute': 120, 'activity': title, 'intent': summary}]
        micro = [{'start_minute': n, 'end_minute': n + 10, 'activity': title,
                  'detail': '正在安静睡觉。' if sleeping else '安静歇一会儿，没有急着要做的事情。',
                  'mood': '平静'} for n in range(0, 120, 10)]
        routine = {'id': '__sleep__' if sleeping else '__rest__', 'title': title, 'summary': summary,
                   'scene': '住处', 'participants': ['亚托莉'], 'preconditions': [], 'macro': macro, 'micro': micro}
    def absolute(block):
        return {'start': (start + timedelta(minutes=block['start_minute'])).isoformat(),
                'end': (start + timedelta(minutes=block['end_minute'])).isoformat(),
                **{k: v for k, v in block.items() if k not in ('start_minute', 'end_minute')}}
    return {'schema_version': 2, 'window_start': start.isoformat(), 'window_end': (start + WINDOW).isoformat(),
            'routine_id': routine['id'], 'sleeping': sleeping,
            'macro': {**{k: routine[k] for k in ('title', 'summary', 'scene', 'participants', 'preconditions')},
                      'activities': [absolute(b) for b in routine['macro']]},
            'micro': [absolute(s) for s in routine['micro']]}


class ScheduleService:
    def __init__(self, config, data_dir, *, root, now=None, choose=None, sleep=asyncio.sleep):
        config.validate()
        self.config = config
        self.zone = ZoneInfo(config.timezone)
        self.now = now or (lambda: datetime.now(self.zone))
        self.choose, self.sleep = choose or random.choice, sleep
        self.directory = (Path(root) / config.routines_dir).resolve()
        self.path = Path(data_dir) / 'schedule/state.json'
        self.plan = None
        self.selection = None
        self.loaded = False
        self.task = None

    def local_now(self):
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Schedule clock must include timezone')
        return value.astimezone(self.zone)

    def is_sleeping(self, now=None):
        now = self.local_now() if now is None else now.astimezone(self.zone)
        return self.config.sleep_enabled and now.hour < 8

    def blocks_reply(self, *, received_at=None, timestamp=0):
        if not self.config.sleep_enabled:
            return False
        now = self.local_now()
        if self.is_sleeping(now):
            return True
        if received_at is not None:
            received_at = received_at.astimezone(self.zone)
            # Once a queued message crosses midnight, never replay it on waking.
            if self.is_sleeping(received_at) or received_at.date() != now.date():
                return True
        if timestamp > 0:
            try:
                return self.is_sleeping(datetime.fromtimestamp(timestamp, self.zone))
            except (ValueError, OverflowError, OSError):
                pass
        return False

    def read_library(self):
        routines, duplicates = {}, set()
        for path in sorted(self.directory.glob('*.json')):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                validate_routine(data)
                if data['id'] in routines or data['id'] in duplicates:
                    routines.pop(data['id'], None)
                    duplicates.add(data['id'])
                    raise ValueError('duplicate routine ID')
                routines[data['id']] = data
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning('[日常文件跳过] 文件=%s 原因=%s', path.name, preview(str(exc), 180))
        log.debug('[日常库加载] 目录=%s 有效=%d', self.directory, len(routines))
        return routines

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('version') != 2:
                log.info('[旧日程缓存停用] 将改用本地日常随机选择')
                return
            if data['timezone'] != self.config.timezone or data['routines_dir'] != str(self.directory):
                return
            start = datetime.fromisoformat(data['window_start'])
            if window_start(start) != start or start.astimezone(self.zone).isoformat() != data['window_start']:
                raise ValueError('invalid cached window')
            if not isinstance(data['routine_id'], str):
                raise ValueError('invalid cached routine ID')
            self.selection = data
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning('[日程恢复失败] 类型=%s，将重新选取当前日常', type(exc).__name__)

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.selection, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temporary.replace(self.path)
        except OSError as exc:
            log.error('[日程落盘失败] 类型=%s，本进程继续复用当前选择', type(exc).__name__)

    def current_plan(self, now=None):
        now = self.local_now() if now is None else now.astimezone(self.zone)
        start = window_start(now)
        if (self.plan is not None and self.plan['window_start'] == start.isoformat()
                and self.plan['sleeping'] == self.is_sleeping(now)):
            return self.plan
        self.load()
        routine = None
        if not self.is_sleeping(now) and self.config.enabled:
            labels = (time_labels(start) or {'夜晚'}) | {'不限'}
            pool = [r for r in self.read_library().values() if labels.intersection(r['suggested_time_of_day'])]
            old = self.selection or {}
            if old.get('window_start') == start.isoformat():
                routine = next((r for r in pool if r['id'] == old['routine_id']), None)
                if routine:
                    log.info('[日程恢复] 窗口=%s 日常=%s', start.isoformat(), routine['title'])
            if routine is None and pool:
                # If alternatives exist, avoid repeating the immediately preceding selection.
                alternatives = [r for r in pool if r['id'] != old.get('routine_id')]
                routine = self.choose(alternatives or pool)
                log.info('[随机日程选定] 窗口=%s 时段=%s 候选=%d ID=%s 标题=%s',
                         start.isoformat(), '、'.join(sorted(labels)), len(pool), routine['id'], routine['title'])
            if not pool:
                log.warning('[日程候选为空] 窗口=%s 使用自由休息，不调用模型', start.isoformat())
        self.plan = make_plan(start, routine, sleeping=self.is_sleeping(now))
        self.selection = {'version': 2, 'timezone': self.config.timezone, 'routines_dir': str(self.directory),
                          'window_start': start.isoformat(), 'routine_id': self.plan['routine_id']}
        self.save()
        log.info('[日程窗口生效] 窗口=%s 标题=%s 睡眠=%s', start.isoformat(),
                 self.plan['macro']['title'], self.plan['sleeping'])
        return self.plan

    def context(self):
        now = self.local_now()
        if not self.config.enabled and not self.is_sleeping(now):
            return ''
        plan = self.current_plan(now)
        slot = plan['micro'][(now.hour % 2 * 60 + now.minute) // 10]
        log.debug('[采用日程背景] ID=%s 小格=%s 活动=%s 关注=%s', plan['routine_id'], slot['start'],
                  slot['activity'], preview(slot['detail'], 100))
        return (CHAT_BACKGROUND + '\n当前时间：' + now.isoformat() + '\n【当前两小时大日程】\n'
                + json.dumps(plan['macro'], ensure_ascii=False) + '\n【当前十分钟小日程】\n'
                + json.dumps(slot, ensure_ascii=False))

    def start(self):
        if self.task is not None:
            return
        self.current_plan()
        self.task = asyncio.create_task(self.run(), name='atri-schedule', context=Context())
        log.info('[日程后台启动] 时区=%s 偶数整点本地随机选择 睡眠拦截=%s', self.config.timezone,
                 '00:00–08:00' if self.config.sleep_enabled else '已关闭')

    async def run(self):
        while True:
            # Polling also recovers promptly after a wall-clock adjustment or machine sleep.
            await self.sleep(30)
            self.current_plan()

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
            log.info('[日程后台停止]')
