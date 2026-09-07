from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid

from aiohttp import web, WSMsgType

from .types import Event, Receipt

log = logging.getLogger("atri")


class Peer:
    def __init__(self, ws, timeout):
        self.ws, self.timeout = ws, timeout
        self.pending = {}

    async def send(self, gid, parts):
        if self.ws.closed:
            return Receipt("failed", reason="disconnected_before_submission")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send_json({"action": "send_group_msg", "params": {"group_id": int(gid), "message": parts}, "echo": echo})
            return await asyncio.wait_for(future, self.timeout)
        except (asyncio.TimeoutError, ConnectionError, RuntimeError):
            return Receipt("unknown", reason="delivery_unconfirmed")
        finally:
            self.pending.pop(echo, None)

    def acknowledge(self, data):
        future = self.pending.get(str(data.get("echo")))
        if future is None or future.done():
            return
        message_id = data["data"].get("message_id") if isinstance(data.get("data"), dict) else None
        if data.get("status") == "ok" and data.get("retcode") == 0 and message_id is not None:
            result = Receipt("sent", str(message_id))
        elif data.get("status") == "failed":
            result = Receipt("failed", reason="onebot_rejected")
        else:
            result = Receipt("unknown", reason="onebot_unconfirmed")
        future.set_result(result)

    def disconnect(self):
        for future in self.pending.values():
            if not future.done():
                future.set_result(Receipt("unknown", reason="disconnected_after_submission"))


def create_app(config, bot):
    active = None
    guard = asyncio.Lock()

    async def websocket(request):
        nonlocal active
        authorization = request.headers.get("Authorization", "")
        if not config.token or not hmac.compare_digest(authorization.encode(), f"Bearer {config.token}".encode()):
            raise web.HTTPUnauthorized()
        if request.headers.get("X-Self-ID") != config.self_id:
            raise web.HTTPForbidden(text="Unexpected bot account")
        if request.headers.get("X-Client-Role", "Universal").lower() != "universal":
            raise web.HTTPBadRequest(text="Use a Universal reverse WebSocket")
        async with guard:
            if active is not None:
                raise web.HTTPConflict(text="Bot already connected")
            ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024*1024)
            await ws.prepare(request)
            peer = Peer(ws, config.action_timeout)
            active = peer
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                    if not isinstance(data, dict):
                        raise ValueError("Expected object")
                    if "echo" in data:
                        peer.acknowledge(data)
                        continue
                    event = Event.parse(data)
                    if event:
                        bot.enqueue(event, peer.send)
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    log.warning("invalid OneBot event dropped")
        finally:
            peer.disconnect()
            if active is peer:
                active = None
        return ws

    async def health(request):
        return web.json_response({"status": "ok", "connected": active is not None})

    async def shutdown(app):
        if active is not None:
            active.disconnect()
            await active.ws.close(code=1001, message=b"Server shutdown")
        await bot.close()

    app = web.Application()
    app.router.add_get(config.ws_path, websocket)
    app.router.add_get("/healthz", health)
    app.on_shutdown.append(shutdown)
    return app
