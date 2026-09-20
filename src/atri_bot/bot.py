import asyncio
from contextvars import Context
from dataclasses import asdict
import logging
import time

from .context import build_conversation, build_willingness_context
from .logging_setup import log_context, preview
from .model import (ModelError, ModelRequestBlocked, guard_model_requests, check_request_allowed,
                    reserve_model_slot)
from .history_tools import ChatArchive, history_registry, tool_instructions
from .tools import ToolContext, ToolSession
from .vision import ImageAccess, register_vision, VISION_INSTRUCTIONS
from .storage import GroupLog
from .types import Receipt
from .willingness import ReplyWillingness
from .schedule import ScheduleService
from .group_session import GroupSession
from .mcp_client import MCPManager
from .link_tools import LinkReader, register_links, LINK_INSTRUCTIONS
from .documents import DocumentStore
from .document_analysis import DocumentProcessor
from .cloud_asr import CloudASR
from .video_cache import VideoSourceCache
from .repetition import Repetition

log = logging.getLogger("atri.bot")
receive_log = logging.getLogger("atri.receive")
queue_log = logging.getLogger("atri.queue")
willingness_log = logging.getLogger("atri.willingness")
send_log = logging.getLogger("atri.send")
repeat_log = logging.getLogger("atri.repetition")
command_log = logging.getLogger("atri.command")


class Bot:
    def __init__(self, config, model, *, now=None):
        self.config, self.model = config, model
        self.personal_info = config.read_personal_info()
        config.reply.validate()
        config.planner.validate()
        config.tools.validate()
        config.vision.validate()
        config.mcp.validate()
        config.links.validate()
        config.documents.validate()
        if config.vision.enabled and not config.tools.enabled:
            raise ValueError("vision.enabled requires tools.enabled=true")
        self.tool_registry = history_registry()
        if config.vision.enabled:
            register_vision(self.tool_registry, config.vision)
        self.mcp = MCPManager(config.mcp, config.root)
        self.link_reader = None
        if config.links.enabled:
            if not config.tools.enabled or not config.mcp.enabled:
                raise ValueError("links.enabled requires tools.enabled=true and mcp.enabled=true")
            processor = None
            if config.documents.enabled:
                store = DocumentStore(config.data / "documents",
                    ttl_seconds=config.links.cache_ttl_seconds,
                    max_documents_per_scope=config.links.max_documents_per_group)
                processor = DocumentProcessor(config.documents, self.model, store)
            self.link_reader = LinkReader(config.links, self.mcp,
                max_result_chars=config.tools.max_result_chars, processor=processor,
                asr=CloudASR(config.asr) if config.asr.enabled else None,
                video_cache=VideoSourceCache(config.data / "video-sources",
                                            ttl_seconds=config.links.cache_ttl_seconds))
            register_links(self.tool_registry, self.link_reader)
        self.semaphore = asyncio.Semaphore(config.parallel)
        self.groups, self.queues, self.tasks = {}, {}, {}
        self.willingness = {}
        self.waiting = {}
        self.inflight = set()
        self.repetition = Repetition()
        self.command_tasks = set()
        self.started_at = time.monotonic()
        self.closed = False
        self.schedule = ScheduleService(config.schedule, config.data, root=config.root, now=now)

    async def start(self):
        if not self.closed:
            await self.mcp.start()
        if self.schedule is not None and not self.closed:
            self.schedule.start()

    def group(self, gid):
        if gid not in self.groups:
            self.groups[gid] = GroupLog(self.config.data, gid, history_seconds=self.config.history_seconds)
            self.willingness[gid] = ReplyWillingness(self.config.reply)
        return self.groups[gid]

    def enqueue(self, event, sender, *, command_sender=None):
        with log_context(group_id=event.group_id, message_id=event.message_id, user_id=event.user_id):
            return self._enqueue(event, sender, command_sender=command_sender)

    def _enqueue(self, event, sender, *, command_sender=None):
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
        if event.text == "/health" and event.parts and all(p.get("type") == "text" for p in event.parts):
            self.repetition.reset(event.group_id)
            self.group(event.group_id).append({"kind": "command", "key": event.key,
                "command": "health", "message_id": event.message_id, "user_id": event.user_id})
            task = asyncio.create_task(self.health_command(event, command_sender or sender),
                                       name=f"atri-health-{event.group_id}-{event.message_id}")
            self.command_tasks.add(task)
            task.add_done_callback(self.command_tasks.discard)
            return task
        received_at = self.schedule.local_now()
        if self.schedule.blocks_reply(received_at=received_at, timestamp=event.timestamp):
            self.repetition.reset(event.group_id)
            self.record_incoming(event, self.group(event.group_id))
            future.set_result(self.ignore_sleep(event, "接收"))
            return future
        gid = event.group_id
        if self.config.reply.mode == "planner":
            self.record_incoming(event, self.group(gid))
        if gid not in self.queues:
            self.queues[gid] = asyncio.Queue(self.config.queue_size)
            self.waiting[gid] = {}
            # 工作任务不继承第一条消息的追踪上下文，每次出队时单独绑定。
            self.tasks[gid] = asyncio.create_task(self.worker(gid), name=f"atri-group-{gid}", context=Context())
            queue_log.debug("[新建队列] 容量=%d", self.config.queue_size)
        try:
            self.queues[gid].put_nowait((event, sender, future, time.perf_counter(), received_at))
        except asyncio.QueueFull:
            self.repetition.reset(gid)
            future.set_result(Receipt("busy", reason="queue_full"))
            queue_log.warning("[队列已满] 当前=%d 容量=%d，本条未入队", self.queues[gid].qsize(), self.config.queue_size)
        else:
            self.repetition.receive(event, now=self.group(gid).now(),
                                    max_age=self.config.reply.max_message_age_seconds)
            self.inflight.add(event.key)
            if self.config.reply.mode == "planner":
                self.waiting[gid][event.key] = event
            queue_log.debug("[入队] 等待消息=%d 处理中总数=%d", self.queues[gid].qsize(), len(self.inflight))
        return future

    async def worker(self, gid):
        if self.config.reply.mode == "planner":
            return await GroupSession(self, gid).run()
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
                    self.repetition.finish(event)
                    queue.task_done()

    def record_incoming(self, event, group):
        group.append({"kind": "incoming", "key": event.key, "message_id": event.message_id,
                      "user_id": event.user_id, "nickname": event.nickname, "text": event.text,
                      "parts": list(event.parts), "reply_id": event.reply_id, "timestamp": event.timestamp})

    def health_status(self):
        failed_workers = sum(task.done() for task in self.tasks.values())
        return {"status": "stopping" if self.closed else "degraded" if failed_workers else "ok",
                "uptime_seconds": max(0, int(time.monotonic() - self.started_at)),
                "queued_messages": sum(queue.qsize() for queue in self.queues.values()),
                "failed_workers": failed_workers}

    async def health_command(self, event, sender):
        status = self.health_status()
        queue = self.queues.get(event.group_id)
        text = (f"ATRI 服务：{'正常' if status['status'] == 'ok' else '异常'}\n"
                f"运行时间：{status['uptime_seconds']} 秒\n"
                f"当前群排队：{queue.qsize() if queue else 0} 条\n"
                f"异常群任务：{status['failed_workers']}\n"
                "模型 API / MCP：未主动探测")
        command_log.info("[健康检查] 状态=%s 运行秒数=%d 异常任务=%d", status["status"],
                         status["uptime_seconds"], status["failed_workers"])
        try:
            return await self.deliver_reply(event, sender, text, record_chat=False, respect_sleep=False)
        except asyncio.CancelledError:
            return Receipt("unknown", reason="shutdown")
        except Exception as exc:
            command_log.exception("[健康检查失败] 类型=%s", type(exc).__name__)
            return Receipt("failed", reason=type(exc).__name__)

    def ignore_sleep(self, event, stage):
        logging.getLogger("atri.schedule").info("[睡眠拦截] 阶段=%s 时段=00:00–08:00，不回复或补发", stage)
        self.group(event.group_id).append({"kind": "schedule", "key": event.key,
                                          "stage": stage, "status": "ignored", "reason": "sleeping"})
        return Receipt("ignored", reason="sleeping")

    def sleep_guard(self, event, received_at, stage):
        if self.schedule.blocks_reply(received_at=received_at, timestamp=event.timestamp):
            return self.ignore_sleep(event, stage)
        return None

    async def repeat(self, event, sender, *, received_at=None):
        if ignored := self.sleep_guard(event, received_at, "复读前"):
            return ignored
        text = self.repetition.claim(event)
        if text is None:
            repeat_log.debug("[复读跳过] 同一轮已经跟读，不再调用模型")
            return Receipt("ignored", reason="repeat_already_handled")
        repeat_log.info("[自动复读] 两名不同群友连续发送相同正文，字符=%d", len(text))
        return await self.deliver_reply(event, sender, text, received_at=received_at)

    async def process(self, event, sender, *, received_at=None):
        received_at = received_at or self.schedule.local_now()
        group = self.group(event.group_id)
        self.record_incoming(event, group)
        if ignored := self.sleep_guard(event, received_at, "出队"):
            return ignored
        if self.repetition.contains(event):
            return await self.repeat(event, sender, received_at=received_at)
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
        async with reserve_model_slot(self.semaphore):
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
                                                  (VISION_INSTRUCTIONS if self.config.vision.enabled else "") +
                                                  (LINK_INSTRUCTIONS if self.config.links.enabled else "")) if tool_session else "",
                                              vision_enabled=self.config.vision.enabled,
                                              links_enabled=self.config.links.enabled)
            try:
                with guard_model_requests(lambda: not self.schedule.blocks_reply(
                        received_at=received_at, timestamp=event.timestamp)):
                    if tool_session is None:
                        reply = await self.model.complete(conversation)
                    else:
                        reply = await self.model.complete(conversation, tool_session=tool_session)
            except ModelRequestBlocked:
                return self.ignore_sleep(event, "回复HTTP前")
        if self.repetition.contains(event):
            return await self.repeat(event, sender, received_at=received_at)
        return await self.deliver_reply(event, sender, reply, received_at=received_at)

    async def deliver_reply(self, event, sender, reply, *, received_at=None, is_current=None,
                            record_chat=True, respect_sleep=True):
        """Shared delivery; operational commands use separate, non-chat audit records."""
        group = self.group(event.group_id)
        if respect_sleep and (ignored := self.sleep_guard(event, received_at, "生成后")):
            return ignored
        if not isinstance(reply, str) or not reply.strip():
            log.warning("[生成回复] 模型没有返回有效正文，结束处理")
            return Receipt("failed", reason="empty_reply")
        reply = reply.strip()
        log.info("[回复已生成] 字符=%d 正文=%s", len(reply), preview(reply, self.config.logging.preview_chars))
        parts = [{"type": "text", "data": {"text": reply}}]
        target = {"reply_to_user_id": event.user_id, "reply_to_message_id": event.message_id}
        kind = "delivery" if record_chat else "command_delivery"
        group.append({"kind": kind, "key": event.key, "status": "pending", "text": reply, **target})
        receipt = Receipt("unknown", reason="delivery_unconfirmed")
        delivery_started = time.perf_counter()
        send_log.info("[开始发送] 等待确认，超时=%.1fs", self.config.action_timeout)
        try:
            async def deliver():
                if respect_sleep and (ignored := self.sleep_guard(event, received_at, "发送前")):
                    return ignored
                if is_current is not None and not is_current():
                    return Receipt("ignored", reason="superseded")
                return await sender(event.group_id, parts)
            receipt = await asyncio.wait_for(deliver(), self.config.action_timeout)
            if not isinstance(receipt, Receipt) or (receipt.status not in ("sent", "failed", "unknown")
                    and not (receipt.status == "ignored" and receipt.reason in ("sleeping", "superseded"))):
                receipt = Receipt("unknown", reason="invalid_receipt")
        except asyncio.CancelledError:
            receipt = Receipt("unknown", reason="shutdown_after_submission")
            raise
        except Exception as exc:
            receipt = Receipt("unknown", reason=type(exc).__name__)
        finally:
            group.append({"kind": kind, "key": event.key, "text": reply, **target, **asdict(receipt)})
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
            async with reserve_model_slot(self.semaphore):
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
        closing_tasks = [*self.tasks.values(), *self.command_tasks]
        for task in closing_tasks:
            task.cancel()
        await asyncio.gather(*closing_tasks, return_exceptions=True)
        for queue in self.queues.values():
            while not queue.empty():
                event, _, future, _, _ = queue.get_nowait()
                self.inflight.discard(event.key)
                self.repetition.finish(event)
                self.waiting.get(event.group_id, {}).pop(event.key, None)
                if not future.done():
                    future.set_result(Receipt("failed", reason="shutdown_before_processing"))
                queue.task_done()
        await self.mcp.close()
