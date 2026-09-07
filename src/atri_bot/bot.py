import asyncio
from dataclasses import asdict
import logging
import time

from .context import build_conversation, build_willingness_context
from .storage import GroupLog
from .types import Receipt
from .willingness import ReplyWillingness

log = logging.getLogger("atri")


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
        # Keep the WebSocket reader free to receive send acknowledgements.
        future = asyncio.get_running_loop().create_future()
        if (self.closed or event.group_id not in self.config.groups or
                event.self_id != self.config.self_id or event.user_id == event.self_id):
            future.set_result(Receipt("ignored"))
            return future
        if event.key in self.inflight or event.key in self.group(event.group_id).seen:
            future.set_result(Receipt("duplicate"))
            return future
        gid = event.group_id
        if gid not in self.queues:
            self.queues[gid] = asyncio.Queue(self.config.queue_size)
            self.tasks[gid] = asyncio.create_task(self.worker(gid), name=f"atri-group-{gid}")
        try:
            self.queues[gid].put_nowait((event, sender, future))
        except asyncio.QueueFull:
            future.set_result(Receipt("busy", reason="queue_full"))
            log.warning("queue full group=%s message=%s", gid, event.message_id)
        else:
            self.inflight.add(event.key)
        return future

    async def worker(self, gid):
        queue = self.queues[gid]
        while True:
            event, sender, future = await queue.get()
            try:
                receipt = await self.process(event, sender)
                if not future.done():
                    future.set_result(receipt)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_result(Receipt("unknown", reason="shutdown"))
                raise
            except Exception as exc:
                log.error("reply failed group=%s message=%s error=%s", gid, event.message_id,
                          getattr(exc, "code", type(exc).__name__))
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
            if event.self_id not in event.mentions:
                return Receipt("ignored")
        else:
            ignored = await self.check_willingness(event, group)
            if ignored is not None:
                return ignored
        conversation = build_conversation(self.personal_info, event, group.history,
                                          self.config.recent_messages)
        async with self.semaphore:
            reply = await self.model.complete(conversation)
        if not isinstance(reply, str) or not reply.strip():
            return Receipt("failed", reason="empty_reply")
        reply = reply.strip()
        parts = [{"type": "text", "data": {"text": reply}}]
        target = {"reply_to_user_id": event.user_id, "reply_to_message_id": event.message_id}
        group.append({"kind": "delivery", "key": event.key, "status": "pending", "text": reply, **target})
        receipt = Receipt("unknown", reason="delivery_unconfirmed")
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
        log.info("reply group=%s message=%s status=%s", event.group_id, event.message_id, receipt.status)
        return receipt

    async def check_willingness(self, event, group):
        state = self.willingness[event.group_id]
        gate = state.evaluate(event, group, time.time())
        group.append({"kind": "willingness", "key": event.key, "stage": "gate", **asdict(gate)})
        log.info("willingness group=%s message=%s score=%s/%s consider=%s reason=%s factors=%s",
                 event.group_id, event.message_id, gate.score, gate.threshold, gate.consider, gate.reason, gate.factors)
        if not gate.consider:
            return Receipt("ignored", reason=gate.reason)
        messages = build_willingness_context(self.personal_info, event, group.history, gate)
        state.begin_check(time.time())
        try:
            async with self.semaphore:
                assessment = await self.model.assess_reply(messages)
        except Exception as exc:
            state.finish_check(False, time.time())
            group.append({"kind": "willingness", "key": event.key, "stage": "judgment",
                          "status": "failed", "reason": getattr(exc, "code", type(exc).__name__)})
            raise
        should_reply = assessment.score >= self.config.reply.threshold
        state.finish_check(should_reply, time.time())
        group.append({"kind": "willingness", "key": event.key, "stage": "judgment",
                      "status": "reply" if should_reply else "wait",
                      "threshold": self.config.reply.threshold, **asdict(assessment)})
        log.info("willingness group=%s message=%s judgment=%s/%s reply=%s reason=%s",
                 event.group_id, event.message_id, assessment.score, self.config.reply.threshold,
                 should_reply, assessment.reason)
        return None if should_reply else Receipt("ignored", reason="model_wait")

    async def close(self, timeout=5):
        self.closed = True
        try:
            await asyncio.wait_for(asyncio.gather(*(q.join() for q in self.queues.values())), timeout)
        except asyncio.TimeoutError:
            pass
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for queue in self.queues.values():
            while not queue.empty():
                event, _, future = queue.get_nowait()
                self.inflight.discard(event.key)
                if not future.done():
                    future.set_result(Receipt("failed", reason="shutdown_before_processing"))
                queue.task_done()
