import asyncio
from contextvars import Context
from dataclasses import asdict
import logging
import time

from .context import build_conversation, build_willingness_context
from .logging_setup import log_context, preview
from .model import ModelError, ModelRequestBlocked, guard_model_requests, check_request_allowed
from .history_tools import ChatArchive, history_registry, tool_instructions
from .tools import ToolContext, ToolSession
from .vision import ImageAccess, register_vision, VISION_INSTRUCTIONS
from .storage import GroupLog
from .types import Receipt
from .willingness import ReplyWillingness
from .schedule import ScheduleService

log = logging.getLogger("atri.bot")
receive_log = logging.getLogger("atri.receive")
queue_log = logging.getLogger("atri.queue")
willingness_log = logging.getLogger("atri.willingness")
send_log = logging.getLogger("atri.send")


class Bot:
    def __init__(self, config, model, *, now=None):
        self.config, self.model = config, model
        self.personal_info = config.read_personal_info()
        config.reply.validate()
        config.tools.validate()
        config.vision.validate()
        if config.vision.enabled and not config.tools.enabled:
            raise ValueError("vision.enabled requires tools.enabled=true")
        self.tool_registry = history_registry()
        if config.vision.enabled:
            register_vision(self.tool_registry, config.vision)
        self.semaphore = asyncio.Semaphore(config.parallel)
        self.groups, self.queues, self.tasks = {}, {}, {}
        self.willingness = {}
        self.inflight = set()
        self.closed = False
        self.schedule = ScheduleService(config.schedule, config.data, root=config.root, now=now)

    async def start(self):
        if self.schedule is not None and not self.closed:
            self.schedule.start()

    def group(self, gid):
        if gid not in self.groups:
            self.groups[gid] = GroupLog(self.config.data, gid, history_seconds=self.config.history_seconds)
            self.willingness[gid] = ReplyWillingness(self.config.reply)
        return self.groups[gid]

    def enqueue(self, event, sender):
        with log_context(group_id=event.group_id, message_id=event.message_id, user_id=event.user_id):
            return self._enqueue(event, sender)

    def _enqueue(self, event, sender):
        # Keep the WebSocket reader free to receive send acknowledgements.
        future = asyncio.get_running_loop().create_future()
        receive_log.info("[收到消息] 昵称=%s at=%s 引用=%s 段类型=%s 正文=%s",
                         preview(event.nickname, 80), event.mentions, event.reply_id or "无",
                         [p.get("type") for p in event.parts], preview(event.text, self.config.logging.preview_chars))
        if (self.closed or event.group_id not in self.config.groups or
                event.self_id != self.config.self_id or event.user_id == event.self_id):
            reason = ("服务正在关闭" if self.closed else "群不在允许列表" if event.group_id not in self.config.groups
                      else "机器人账号不匹配" if event.self_id != self.config.self_id else "机器人自己的消息")
            receive_log.info("[过滤] 忽略消息，原因=%s", reason)
            future.set_result(Receipt("ignored"))
            return future
        if event.key in self.inflight or event.key in self.group(event.group_id).seen:
            receive_log.info("[去重] 忽略消息，状态=%s", "正在处理" if event.key in self.inflight else "已经记录")
            future.set_result(Receipt("duplicate"))
            return future
        received_at = self.schedule.local_now()
        if self.schedule.blocks_reply(received_at=received_at, timestamp=event.timestamp):
            self.record_incoming(event, self.group(event.group_id))
            future.set_result(self.ignore_sleep(event, "接收"))
            return future
        gid = event.group_id
        if gid not in self.queues:
            self.queues[gid] = asyncio.Queue(self.config.queue_size)
            # 工作任务不继承第一条消息的追踪上下文，每次出队时单独绑定。
            self.tasks[gid] = asyncio.create_task(self.worker(gid), name=f"atri-group-{gid}", context=Context())
            queue_log.debug("[新建队列] 容量=%d", self.config.queue_size)
        try:
            self.queues[gid].put_nowait((event, sender, future, time.perf_counter(), received_at))
        except asyncio.QueueFull:
            future.set_result(Receipt("busy", reason="queue_full"))
            queue_log.warning("[队列已满] 当前=%d 容量=%d，本条未入队", self.queues[gid].qsize(), self.config.queue_size)
        else:
            self.inflight.add(event.key)
            queue_log.debug("[入队] 等待消息=%d 处理中总数=%d", self.queues[gid].qsize(), len(self.inflight))
        return future

    async def worker(self, gid):
        queue = self.queues[gid]
        while True:
            event, sender, future, enqueued_at, received_at = await queue.get()
            with log_context(group_id=gid, message_id=event.message_id, user_id=event.user_id):
                started = time.perf_counter()
                queue_log.debug("[出队] 排队耗时=%.1fms 剩余=%d", (started - enqueued_at) * 1000, queue.qsize())
                try:
                    receipt = await self.process(event, sender, received_at=received_at)
                    log.info("[处理结束] 状态=%s 原因=%s 回复消息号=%s 总耗时=%.1fms",
                             receipt.status, receipt.reason or "无", receipt.message_id or "无",
                             (time.perf_counter() - started) * 1000)
                    if not future.done():
                        future.set_result(receipt)
                except asyncio.CancelledError:
                    log.warning("[处理取消] 服务关闭或任务被取消，耗时=%.1fms", (time.perf_counter() - started) * 1000)
                    if not future.done():
                        future.set_result(Receipt("unknown", reason="shutdown"))
                    raise
                except Exception as exc:
                    log.exception("[处理失败] 错误=%s 耗时=%.1fms", getattr(exc, "code", type(exc).__name__),
                                  (time.perf_counter() - started) * 1000)
                    if not future.done():
                        future.set_result(Receipt("failed", reason=getattr(exc, "code", type(exc).__name__)))
                finally:
                    self.inflight.discard(event.key)
                    queue.task_done()

    def record_incoming(self, event, group):
        group.append({"kind": "incoming", "key": event.key, "message_id": event.message_id,
                      "user_id": event.user_id, "nickname": event.nickname, "text": event.text,
                      "parts": list(event.parts), "reply_id": event.reply_id, "timestamp": event.timestamp})

    def ignore_sleep(self, event, stage):
        logging.getLogger("atri.schedule").info("[睡眠拦截] 阶段=%s 时段=00:00–08:00，不回复或补发", stage)
        self.group(event.group_id).append({"kind": "schedule", "key": event.key,
                                          "stage": stage, "status": "ignored", "reason": "sleeping"})
        return Receipt("ignored", reason="sleeping")

    def sleep_guard(self, event, received_at, stage):
        if self.schedule.blocks_reply(received_at=received_at, timestamp=event.timestamp):
            return self.ignore_sleep(event, stage)
        return None

    async def process(self, event, sender, *, received_at=None):
        received_at = received_at or self.schedule.local_now()
        group = self.group(event.group_id)
        self.record_incoming(event, group)
        if ignored := self.sleep_guard(event, received_at, "出队"):
            return ignored
        if self.config.reply.mode == "at_only":
            log.debug("[回复模式] at_only 真实at=%s", event.self_id in event.mentions)
            if event.self_id not in event.mentions:
                log.info("[无需回复] 仅 @ 模式，本条没有真正 @ 机器人")
                return Receipt("ignored")
        else:
            log.debug("[回复模式] willingness，开始计算接话意愿")
            ignored = await self.check_willingness(event, group, received_at=received_at)
            if ignored is not None:
                return ignored
        slot_started = time.perf_counter()
        log.debug("[生成回复] 等待模型并发槽位")
        async with self.semaphore:
            if ignored := self.sleep_guard(event, received_at, "生成前"):
                return ignored
            log.debug("[生成回复] 已取得槽位，等待=%.1fms", (time.perf_counter() - slot_started) * 1000)
            # 在实际生成前读钟，避免等待槽位时跨过十分钟边界而使用旧背景。
            background = self.schedule.context()
            now = group.now()
            tool_session = None
            if self.config.tools.enabled:
                archive = ChatArchive(group.path, group_id=event.group_id, self_id=event.self_id,
                                      now=now, exclude_key=event.key)
                context = ToolContext(event.group_id, event.user_id, event.self_id, event.key, now,
                                      archive, lambda: check_request_allowed("tool"), group.append,
                                      ImageAccess(self.config.vision, self.model, event, group.history, now=now,
                                                  history_seconds=self.config.history_seconds)
                                      if self.config.vision.enabled else None)
                tool_session = ToolSession(self.tool_registry, context, self.config.tools)
            conversation = build_conversation(self.personal_info, event, group.history,
                                              history_seconds=self.config.history_seconds, now=now,
                                              schedule_context=background,
                                              tool_context=(tool_instructions(now) +
                                                  (VISION_INSTRUCTIONS if self.config.vision.enabled else "")) if tool_session else "",
                                              vision_enabled=self.config.vision.enabled)
            try:
                with guard_model_requests(lambda: not self.schedule.blocks_reply(
                        received_at=received_at, timestamp=event.timestamp)):
                    if tool_session is None:
                        reply = await self.model.complete(conversation)
                    else:
                        reply = await self.model.complete(conversation, tool_session=tool_session)
            except ModelRequestBlocked:
                return self.ignore_sleep(event, "回复HTTP前")
        if ignored := self.sleep_guard(event, received_at, "生成后"):
            return ignored
        if not isinstance(reply, str) or not reply.strip():
            log.warning("[生成回复] 模型没有返回有效正文，结束处理")
            return Receipt("failed", reason="empty_reply")
        reply = reply.strip()
        log.info("[回复已生成] 字符=%d 正文=%s", len(reply), preview(reply, self.config.logging.preview_chars))
        parts = [{"type": "text", "data": {"text": reply}}]
        target = {"reply_to_user_id": event.user_id, "reply_to_message_id": event.message_id}
        group.append({"kind": "delivery", "key": event.key, "status": "pending", "text": reply, **target})
        receipt = Receipt("unknown", reason="delivery_unconfirmed")
        delivery_started = time.perf_counter()
        send_log.info("[开始发送] 等待确认，超时=%.1fs", self.config.action_timeout)
        try:
            async def deliver():
                if ignored := self.sleep_guard(event, received_at, "发送前"):
                    return ignored
                return await sender(event.group_id, parts)
            receipt = await asyncio.wait_for(deliver(), self.config.action_timeout)
            if not isinstance(receipt, Receipt) or (receipt.status not in ("sent", "failed", "unknown")
                    and not (receipt.status == "ignored" and receipt.reason == "sleeping")):
                receipt = Receipt("unknown", reason="invalid_receipt")
        except asyncio.CancelledError:
            receipt = Receipt("unknown", reason="shutdown_after_submission")
            raise
        except Exception as exc:
            receipt = Receipt("unknown", reason=type(exc).__name__)
        finally:
            group.append({"kind": "delivery", "key": event.key, "text": reply, **target, **asdict(receipt)})
        send_log.log(logging.INFO if receipt.status == "sent" else logging.WARNING,
                     "[发送结束] 状态=%s 消息号=%s 原因=%s 耗时=%.1fms",
                     receipt.status, receipt.message_id or "无", receipt.reason or "无",
                     (time.perf_counter() - delivery_started) * 1000)
        return receipt

    async def check_willingness(self, event, group, *, received_at=None):
        state = self.willingness[event.group_id]
        gate = state.evaluate(event, group, time.time())
        group.append({"kind": "willingness", "key": event.key, "stage": "gate", **asdict(gate)})
        if not gate.consider:
            return Receipt("ignored", reason=gate.reason)
        state.begin_check(time.time())
        started = time.perf_counter()
        willingness_log.info("[模型判断开始] 判断阈值=%d", self.config.reply.threshold)
        try:
            async with self.semaphore:
                if ignored := self.sleep_guard(event, received_at, "意愿模型前"):
                    return ignored
                willingness_log.debug("[模型判断] 已取得并发槽位，等待=%.1fms", (time.perf_counter() - started) * 1000)
                messages = build_willingness_context(self.personal_info, event, group.history, gate,
                                                    history_seconds=self.config.history_seconds, now=group.now(),
                                                    vision_enabled=self.config.vision.enabled)
                with guard_model_requests(lambda: not self.schedule.blocks_reply(
                        received_at=received_at, timestamp=event.timestamp)):
                    assessment = await self.model.assess_reply(messages)
        except ModelRequestBlocked:
            return self.ignore_sleep(event, "意愿HTTP或重试前")
        except ModelError as exc:
            if ignored := self.sleep_guard(event, received_at, "意愿失败后"):
                return ignored
            group.append({"kind": "willingness", "key": event.key, "stage": "judgment",
                          "status": "failed", "reason": exc.code, "attempts": exc.attempts})
            should_reply = gate.score >= self.config.reply.threshold
            # 只按最终决策更新一次状态，单次技术失败不累加“连续等待”。
            state.finish_check(should_reply, time.time())
            group.append({"kind": "willingness", "key": event.key, "stage": "fallback",
                          "status": "reply" if should_reply else "wait", "source": "rule",
                          "score": gate.score, "threshold": self.config.reply.threshold,
                          "reason": exc.code, "attempts": exc.attempts})
            willingness_log.warning("[规则降级] 模型判断失败 尝试=%d 错误=%s 规则分=%d 回复阈值=%d 决策=%s 总耗时=%.1fms",
                                    exc.attempts, exc.code, gate.score, self.config.reply.threshold,
                                    "回复" if should_reply else "等待", (time.perf_counter() - started) * 1000)
            return None if should_reply else Receipt("ignored", reason="rule_fallback_wait")
        if ignored := self.sleep_guard(event, received_at, "意愿模型后"):
            return ignored
        should_reply = assessment.score >= self.config.reply.threshold
        state.finish_check(should_reply, time.time())
        group.append({"kind": "willingness", "key": event.key, "stage": "judgment",
                      "status": "reply" if should_reply else "wait",
                      "threshold": self.config.reply.threshold, **asdict(assessment)})
        willingness_log.info("[模型判断结果] 分数=%d 阈值=%d 决策=%s 理由=%s 耗时=%.1fms",
                             assessment.score, self.config.reply.threshold, "回复" if should_reply else "等待",
                             preview(assessment.reason, 160), (time.perf_counter() - started) * 1000)
        return None if should_reply else Receipt("ignored", reason="model_wait")

    async def close(self, timeout=5):
        if self.closed:
            return
        queue_log.info("[关闭队列] 群数=%d 处理中=%d 等待超时=%.1fs", len(self.queues), len(self.inflight), timeout)
        self.closed = True
        if self.schedule is not None:
            await self.schedule.close()
        try:
            await asyncio.wait_for(asyncio.gather(*(q.join() for q in self.queues.values())), timeout)
        except asyncio.TimeoutError:
            queue_log.warning("[关闭队列] 等待处理完成超时，将取消剩余任务")
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for queue in self.queues.values():
            while not queue.empty():
                event, _, future, _, _ = queue.get_nowait()
                self.inflight.discard(event.key)
                if not future.done():
                    future.set_result(Receipt("failed", reason="shutdown_before_processing"))
                queue.task_done()
