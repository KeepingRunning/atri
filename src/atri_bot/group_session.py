"""One serial group worker: collect, snapshot, plan, write and deliver."""
import asyncio
from dataclasses import asdict
import logging
import math
import time

from .context import build_snapshot, build_planned_reply
from .history_tools import ChatArchive
from .logging_setup import log_context, preview
from .model import ModelError, ModelRequestBlocked, check_request_allowed, guard_model_requests
from .planner import Planner
from .storage import history_timestamp
from .tools import ToolContext, ToolSession
from .types import Receipt
from .vision import ImageAccess
from .willingness import contains_name

log = logging.getLogger("atri.queue")
reply_log = logging.getLogger("atri.replyer")


class GroupSession:
    def __init__(self, bot, gid):
        self.bot, self.gid = bot, gid
        self.config = bot.config
        self.queue = bot.queues[gid]
        self.waiting = bot.waiting[gid]
        self.group = bot.group(gid)

    async def take(self):
        item = await self.queue.get()
        self.waiting.pop(item[0].key, None)
        return item

    def drain(self, batch):
        while not self.queue.empty() and len(batch) < self.config.planner.max_batch_messages:
            item = self.queue.get_nowait()
            self.waiting.pop(item[0].key, None)
            batch.append(item)

    async def collect(self, batch, *, wait_seconds=None):
        """Normal collection resets a quiet interval; explicit wait wakes on first input."""
        settings = self.config.planner
        if wait_seconds is not None:
            if len(batch) >= settings.max_batch_messages:
                return
            try:
                batch.append(await asyncio.wait_for(self.take(), wait_seconds))
            except TimeoutError:
                return
        started = time.perf_counter()
        deadline = started + settings.max_batch_seconds
        self.drain(batch)
        while len(batch) < settings.max_batch_messages:
            timeout = min(settings.debounce_seconds, deadline - time.perf_counter())
            if timeout <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self.take(), timeout))
                self.drain(batch)
            except TimeoutError:
                break
        log.info("[收集消息批次] 消息=%d 剩余队列=%d 收集耗时=%.1fms",
                 len(batch), self.queue.qsize(), (time.perf_counter() - started) * 1000)

    def direct(self, event):
        return event.self_id in event.mentions or event.reply_id in self.group.sent_message_ids

    def eligible(self, batch):
        now = self.group.now()
        # No topic/keyword scoring here. Ordinary messages also reach Planner.
        return [item for item in batch if not (
            self.bot.schedule.blocks_reply(received_at=item[4], timestamp=item[0].timestamp)
            or (math.isfinite(item[0].timestamp) and item[0].timestamp > 0
                and now - item[0].timestamp > self.config.reply.max_message_age_seconds))]

    def related_pending(self, events, decision):
        ids = set(decision["target_message_ids"])
        users = {e.user_id for e in events if e.message_id in ids}
        # Conservative interruption rule, not topic classification. Unrelated new turns
        # stay queued; a supplement by the target speaker or a direct call invalidates the draft.
        return any(e.user_id in users or e.reply_id in ids or self.direct(e)
                   or contains_name(e.text, self.config.reply.names) for e in self.waiting.values())

    def tool_context(self, event, snapshot):
        return ToolContext(self.gid, event.user_id, event.self_id, event.key, snapshot.now,
            ChatArchive(self.group.path, group_id=self.gid, self_id=event.self_id,
                        now=snapshot.now, exclude_key=event.key),
            lambda: check_request_allowed("tool"), self.group.append,
            ImageAccess(self.config.vision, self.bot.model, event, self.visible_history(),
                        now=snapshot.now, history_seconds=self.config.history_seconds)
            if self.config.vision.enabled else None)

    def visible_history(self):
        # Night messages are archived but are not replayed into morning snapshots.
        # Messages queued beyond this batch are not relabeled as its prior history.
        return [row for row in self.group.history if row.get("key") not in self.waiting and
                (row.get("role") == "assistant"
                 or not self.bot.schedule.blocks_reply(timestamp=history_timestamp(row) or 0))]

    async def process(self, batch):
        planner = Planner(self.bot.model, self.config)
        waits = replans = 0
        tools = None
        await self.collect(batch)
        while True:
            active = self.eligible(batch)
            if not active:
                sleeping = any(self.bot.schedule.blocks_reply(received_at=i[4], timestamp=i[0].timestamp) for i in batch)
                return Receipt("ignored", reason="sleeping" if sleeping else "stale_message")
            events = [item[0] for item in active]
            if self.config.reply.frequency == 0 and not any(self.direct(e) for e in events):
                return Receipt("ignored", reason="automatic_reply_disabled")
            # Ordinary messages during cooldown remain in history for the next batch.
            # Direct calls bypass this technical gate.
            last = self.group.last_sent
            if last and not any(self.direct(e) for e in events):
                remaining = self.config.reply.cooldown_seconds - (self.group.now() - last["time"])
                if remaining > 0:
                    return Receipt("ignored", reason="cooldown")
            event = events[-1]
            allowed = lambda: not any(self.bot.schedule.blocks_reply(received_at=i[4], timestamp=i[0].timestamp)
                                      for i in active)
            with guard_model_requests(allowed):
                async with self.bot.semaphore:
                    check_request_allowed("planner")
                    # Include input received while awaiting a global model slot.
                    if self.waiting and len(batch) < self.config.planner.max_batch_messages:
                        self.drain(batch)
                        continue
                    snapshot = build_snapshot(events, self.visible_history(), now=self.group.now(),
                        history_seconds=self.config.history_seconds, schedule_context=self.bot.schedule.context(),
                        vision_enabled=self.config.vision.enabled, max_chars=self.config.planner.max_snapshot_chars)
                    self.group.append({"kind": "snapshot", "key": event.key, "snapshot_id": snapshot.id,
                        "message_ids": [e.message_id for e in events],
                        "history_count": len(snapshot.data["history"]), "chars": len(snapshot.encoded),
                        "omitted_history": snapshot.data["omitted_history"]})
                    if self.config.tools.enabled:
                        context = self.tool_context(event, snapshot)
                        if tools is None:
                            tools = ToolSession(self.bot.tool_registry, context, self.config.tools)
                        else:
                            tools.context = context  # Budget/cache survive a refreshed snapshot.
                    remaining_waits = self.config.planner.max_waits - waits
                    if len(batch) >= self.config.planner.max_batch_messages:
                        remaining_waits = 0
                    decision = await planner.decide(self.bot.personal_info, snapshot, tools,
                                                    remaining_waits=remaining_waits)
                    check_request_allowed("planner_result")
                    self.group.append({"kind": "planner", "key": event.key, "snapshot_id": snapshot.id,
                                       "message_ids": [e.message_id for e in events], "stage": "decision", **decision})
                if decision["action"] == "observe":
                    return Receipt("ignored", reason="planner_observe")
                if decision["action"] == "wait":
                    waits += 1
                    log.info("[等待补充] 快照=%s 最多=%.1fs 已等待=%d/%d", snapshot.id,
                             decision["seconds"], waits, self.config.planner.max_waits)
                    await self.collect(batch, wait_seconds=decision["seconds"])
                    continue
                if not self.related_pending(events, decision):
                    async with self.bot.semaphore:
                        check_request_allowed("reply")
                        # Recheck after waiting for the second model slot.
                        if not self.related_pending(events, decision):
                            reply_log.info("[生成正文] 快照=%s 目标=%s 目的=%s 风格=%s",
                                snapshot.id, decision["target_message_ids"],
                                preview(decision["purpose"], self.config.logging.preview_chars),
                                preview(decision["style_hint"], self.config.logging.preview_chars))
                            reply = await self.bot.model.complete(build_planned_reply(
                                self.bot.personal_info, snapshot, decision, planner.observations))
                            check_request_allowed("reply_result")
                if self.related_pending(events, decision):
                    replans += 1
                    can_replan = (replans <= self.config.planner.max_replans
                                  and len(batch) < self.config.planner.max_batch_messages)
                    self.group.append({"kind": "planner", "key": event.key, "snapshot_id": snapshot.id,
                        "stage": "stale", "reason": "new_related_messages", "replans": replans,
                        "status": "replan" if can_replan else "discard"})
                    log.info("[快照已过时] 快照=%s 重规划=%d/%d 处理=%s", snapshot.id, replans,
                             self.config.planner.max_replans, "重建" if can_replan else "丢弃本批草稿")
                    if not can_replan:
                        return Receipt("ignored", reason="superseded")
                    await self.collect(batch)
                    continue
                target_id = decision["target_message_ids"][-1]
                target = next(item for item in active if item[0].message_id == target_id)
                # No await between the staleness check and entering the shared delivery code.
                return await self.bot.deliver_reply(target[0], target[1], reply, received_at=target[4],
                    is_current=lambda: not self.related_pending(events, decision))

    async def run(self):
        while True:
            batch = [await self.take()]
            event = batch[0][0]
            started = time.perf_counter()
            receipt = Receipt("unknown", reason="shutdown")
            with log_context(group_id=self.gid, message_id=event.message_id, user_id=event.user_id):
                try:
                    receipt = await self.process(batch)
                except asyncio.CancelledError:
                    raise
                except ModelRequestBlocked:
                    receipt = self.bot.ignore_sleep(event, "规划或回复")
                except ModelError as exc:
                    receipt = Receipt("failed", reason=exc.code)
                    logging.getLogger("atri.planner").error("[本批失败] 错误=%s 尝试=%d", exc.code, exc.attempts)
                except Exception as exc:
                    receipt = Receipt("failed", reason=type(exc).__name__)
                    log.exception("[本批异常] 类型=%s", type(exc).__name__)
                finally:
                    try:
                        self.group.append({"kind": "batch", "key": event.key,
                            "message_ids": [item[0].message_id for item in batch], **asdict(receipt)})
                        log.info("[本批结束] 消息=%d 状态=%s 原因=%s 耗时=%.1fms", len(batch), receipt.status,
                                 receipt.reason or "无", (time.perf_counter() - started) * 1000)
                    finally:
                        for member, _, future, _, _ in batch:
                            if not future.done():
                                future.set_result(receipt)
                            self.bot.inflight.discard(member.key)
                            self.queue.task_done()
