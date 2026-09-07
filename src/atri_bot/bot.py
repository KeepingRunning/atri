import asyncio
from contextvars import Context
from dataclasses import asdict
import logging
import time

from .context import build_conversation, build_willingness_context
from .logging_setup import log_context, preview
from .storage import GroupLog
from .types import Receipt
from .willingness import ReplyWillingness

log = logging.getLogger("atri.bot")
receive_log = logging.getLogger("atri.receive")
queue_log = logging.getLogger("atri.queue")
willingness_log = logging.getLogger("atri.willingness")
send_log = logging.getLogger("atri.send")


class Bot:
    def __init__(self, config, model):
        self.config, self.model = config, model
        self.personal_info = config.read_personal_info()
        config.reply.validate()
        self.semaphore = asyncio.Semaphore(config.parallel)
        self.groups, self.queues, self.tasks = {}, {}, {}
        self.willingness = {}
        self.inflight = set()
        self.closed = False

    def group(self, gid):
        if gid not in self.groups:
            self.groups[gid] = GroupLog(self.config.data, gid, self.config.recent_messages + 1)
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
        gid = event.group_id
        if gid not in self.queues:
            self.queues[gid] = asyncio.Queue(self.config.queue_size)
            # 工作任务不继承第一条消息的追踪上下文，每次出队时单独绑定。
            self.tasks[gid] = asyncio.create_task(self.worker(gid), name=f"atri-group-{gid}", context=Context())
            queue_log.debug("[新建队列] 容量=%d", self.config.queue_size)
        try:
            self.queues[gid].put_nowait((event, sender, future, time.perf_counter()))
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
            event, sender, future, enqueued_at = await queue.get()
            with log_context(group_id=gid, message_id=event.message_id, user_id=event.user_id):
                started = time.perf_counter()
                queue_log.debug("[出队] 排队耗时=%.1fms 剩余=%d", (started - enqueued_at) * 1000, queue.qsize())
                try:
                    receipt = await self.process(event, sender)
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

    async def process(self, event, sender):
        group = self.group(event.group_id)
        group.append({"kind": "incoming", "key": event.key, "message_id": event.message_id,
                      "user_id": event.user_id, "nickname": event.nickname, "text": event.text,
                      "parts": list(event.parts), "reply_id": event.reply_id, "timestamp": event.timestamp})
        if self.config.reply.mode == "at_only":
            log.debug("[回复模式] at_only 真实at=%s", event.self_id in event.mentions)
            if event.self_id not in event.mentions:
                log.info("[无需回复] 仅 @ 模式，本条没有真正 @ 机器人")
                return Receipt("ignored")
        else:
            log.debug("[回复模式] willingness，开始计算接话意愿")
            ignored = await self.check_willingness(event, group)
            if ignored is not None:
                return ignored
        conversation = build_conversation(self.personal_info, event, group.history,
                                          self.config.recent_messages)
        slot_started = time.perf_counter()
        log.debug("[生成回复] 等待模型并发槽位")
        async with self.semaphore:
            log.debug("[生成回复] 已取得槽位，等待=%.1fms", (time.perf_counter() - slot_started) * 1000)
            reply = await self.model.complete(conversation)
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
            receipt = await asyncio.wait_for(sender(event.group_id, parts), self.config.action_timeout)
            if not isinstance(receipt, Receipt) or receipt.status not in ("sent", "failed", "unknown"):
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

    async def check_willingness(self, event, group):
        state = self.willingness[event.group_id]
        gate = state.evaluate(event, group, time.time())
        group.append({"kind": "willingness", "key": event.key, "stage": "gate", **asdict(gate)})
        if not gate.consider:
            return Receipt("ignored", reason=gate.reason)
        messages = build_willingness_context(self.personal_info, event, group.history, gate)
        state.begin_check(time.time())
        started = time.perf_counter()
        willingness_log.info("[模型判断开始] 判断阈值=%d 上下文条数=%d", self.config.reply.threshold, len(messages))
        try:
            async with self.semaphore:
                willingness_log.debug("[模型判断] 已取得并发槽位，等待=%.1fms", (time.perf_counter() - started) * 1000)
                assessment = await self.model.assess_reply(messages)
        except Exception as exc:
            state.finish_check(False, time.time())
            group.append({"kind": "willingness", "key": event.key, "stage": "judgment",
                          "status": "failed", "reason": getattr(exc, "code", type(exc).__name__)})
            willingness_log.error("[模型判断失败] 错误=%s 耗时=%.1fms",
                                   getattr(exc, "code", type(exc).__name__), (time.perf_counter() - started) * 1000)
            raise
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
        queue_log.info("[关闭队列] 群数=%d 处理中=%d 等待超时=%.1fs", len(self.queues), len(self.inflight), timeout)
        self.closed = True
        try:
            await asyncio.wait_for(asyncio.gather(*(q.join() for q in self.queues.values())), timeout)
        except asyncio.TimeoutError:
            queue_log.warning("[关闭队列] 等待处理完成超时，将取消剩余任务")
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for queue in self.queues.values():
            while not queue.empty():
                event, _, future, _ = queue.get_nowait()
                self.inflight.discard(event.key)
                if not future.done():
                    future.set_result(Receipt("failed", reason="shutdown_before_processing"))
                queue.task_done()
