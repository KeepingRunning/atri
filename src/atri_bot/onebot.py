from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid

from aiohttp import web, WSMsgType

from .types import Event, Receipt
from .logging_setup import current_log_context, log_context, preview

log = logging.getLogger("atri.onebot")
send_log = logging.getLogger("atri.send")


class Peer:
    def __init__(self, ws, timeout, *, can_send=None):
        self.ws, self.timeout = ws, timeout
        self.pending = {}
        self.traces = {}
        self.can_send = can_send or (lambda: True)

    async def send(self, gid, parts):
        if not self.can_send():
            send_log.info("[睡眠拦截] OneBot 提交前已进入睡眠时段")
            return Receipt("ignored", reason="sleeping")
        if self.ws.closed:
            send_log.warning("[提交失败] WebSocket 已断开")
            return Receipt("failed", reason="disconnected_before_submission")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        self.traces[echo] = {**current_log_context(), "group_id": gid}
        try:
            send_log.debug("[OneBot提交] action=send_group_msg echo=%s 消息段=%d 待确认=%d", echo, len(parts), len(self.pending))
            await self.ws.send_json({"action": "send_group_msg", "params": {"group_id": int(gid), "message": parts}, "echo": echo})
            send_log.debug("[等待回执] echo=%s 超时=%.1fs", echo, self.timeout)
            return await asyncio.wait_for(future, self.timeout)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError) as exc:
            send_log.warning("[回执未确认] echo=%s 原因=%s", echo, type(exc).__name__)
            return Receipt("unknown", reason="delivery_unconfirmed")
        finally:
            self.pending.pop(echo, None)
            self.traces.pop(echo, None)

    def acknowledge(self, data):
        echo = str(data.get("echo"))
        with log_context(**self.traces.get(echo, {})):
            future = self.pending.get(echo)
            if future is None or future.done():
                send_log.debug("[忽略回执] 未知或重复 echo=%s", preview(echo, 80))
                return
            message_id = data["data"].get("message_id") if isinstance(data.get("data"), dict) else None
            if data.get("status") == "ok" and data.get("retcode") == 0 and message_id is not None:
                result = Receipt("sent", str(message_id))
            elif data.get("status") == "failed":
                result = Receipt("failed", reason="onebot_rejected")
            else:
                result = Receipt("unknown", reason="onebot_unconfirmed")
            send_log.log(logging.INFO if result.status == "sent" else logging.WARNING,
                         "[收到回执] echo=%s status=%s retcode=%s 回复消息号=%s 认定=%s",
                         echo, data.get("status"), data.get("retcode"), message_id, result.status)
            future.set_result(result)

    def disconnect(self):
        for echo, future in self.pending.items():
            if not future.done():
                with log_context(**self.traces.get(echo, {})):
                    send_log.warning("[连接断开] echo=%s 提交后尚未收到回执，结果标记unknown", echo)
                future.set_result(Receipt("unknown", reason="disconnected_after_submission"))


def create_app(config, bot):
    active = None
    guard = asyncio.Lock()

    async def websocket(request):
        nonlocal active
        authorization = request.headers.get("Authorization", "")
        if not config.token or not hmac.compare_digest(authorization.encode(), f"Bearer {config.token}".encode()):
            log.warning("[拒绝连接] 身份验证失败")
            raise web.HTTPUnauthorized()
        if request.headers.get("X-Self-ID") != config.self_id:
            log.warning("[拒绝连接] 机器人账号不匹配")
            raise web.HTTPForbidden(text="Unexpected bot account")
        if request.headers.get("X-Client-Role", "Universal").lower() != "universal":
            log.warning("[拒绝连接] 客户端角色必须为 Universal")
            raise web.HTTPBadRequest(text="Use a Universal reverse WebSocket")
        async with guard:
            if active is not None:
                log.warning("[拒绝连接] 已有活动连接")
                raise web.HTTPConflict(text="Bot already connected")
            ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024*1024)
            await ws.prepare(request)
            peer = Peer(ws, config.action_timeout, can_send=lambda: not bot.schedule.is_sleeping())
            active = peer
            log.info("[连接建立] OneBot 已连接，机器人账号=%s", config.self_id)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    log.debug("[非文本帧] 类型=%s", msg.type.name)
                    continue
                log.debug("[收到帧] 字节数=%d", len(msg.data.encode("utf-8")))
                try:
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        raise ValueError("Expected object")
                    if "echo" in data:
                        peer.acknowledge(data)
                        continue
                    event = Event.parse(data)
                    if event:
                        with log_context(group_id=event.group_id, message_id=event.message_id, user_id=event.user_id):
                            log.debug("[解析完成] 有效群消息，交给消息队列")
                        bot.enqueue(event, peer.send)
                    else:
                        log.debug("[忽略事件] 非群消息 post_type=%s message_type=%s",
                                  preview(data.get("post_type"), 40), preview(data.get("message_type"), 40))
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
                    log.warning("[无效事件] 丢弃无法解析的 OneBot 消息，类型=%s", type(exc).__name__)
        finally:
            peer.disconnect()
            if active is peer:
                active = None
            log.info("[连接关闭] OneBot 已断开 code=%s", ws.close_code)
        return ws

    async def health(request):
        return web.json_response({"status": "ok", "connected": active is not None})

    async def shutdown(app):
        if active is not None:
            active.disconnect()
            await active.ws.close(code=1001, message=b"Server shutdown")
        await bot.close()

    async def background(app):
        await bot.start()
        try:
            yield
        finally:
            await bot.close()

    app = web.Application()
    app.cleanup_ctx.append(background)
    app.router.add_get(config.ws_path, websocket)
    app.router.add_get("/healthz", health)
    app.on_shutdown.append(shutdown)
    return app
