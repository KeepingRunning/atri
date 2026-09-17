"""Bounded, allowlisted stdio MCP connections using the official SDK."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import Context, ContextVar
from dataclasses import dataclass, field
import logging
import math
import os
from pathlib import Path
import re
import time

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT

from .tools import ToolError

log = logging.getLogger("atri.mcp")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_QUEUE_SIZE = 16
_LIST_PAGES = 8
_private_io = ContextVar("atri_mcp_private_io", default=False)


def _is_timeout(error):
    if isinstance(error, BaseExceptionGroup):
        return any(_is_timeout(child) for child in error.exceptions)
    return isinstance(error, TimeoutError) or isinstance(error, MCPError) and error.code == REQUEST_TIMEOUT


def _timeout_error():
    return ToolError("mcp_timeout", "MCP 请求超时，本次读取失败。")


class _PrivateSDKLogs(logging.Filter):
    def filter(self, record):
        # SDK validation tracebacks can contain raw stdout, including credentials.
        # Only suppress records in our connection tasks; other SDK users are untouched.
        return not _private_io.get()


_sdk_log_filter = _PrivateSDKLogs()


@dataclass
class MCPServerConfig:
    command: str
    args: list[str] = field(default_factory=list, repr=False)
    enabled: bool = True
    env: dict[str, str] = field(default_factory=dict, repr=False)
    env_passthrough: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("mcp server enabled must be a boolean")
        if not isinstance(self.command, str) or not self.command.strip() or "\0" in self.command:
            raise ValueError("mcp server command must be a nonempty executable")
        if (not isinstance(self.args, list)
                or any(not isinstance(arg, str) or "\0" in arg for arg in self.args)):
            raise ValueError("mcp server args must be strings without NUL bytes")
        if (not isinstance(self.env, dict) or any(
                not isinstance(key, str) or not _ENV_NAME.fullmatch(key)
                or not isinstance(value, str) or "\0" in value
                for key, value in self.env.items())):
            raise ValueError("mcp server env must contain valid names and string values")
        for name, pattern in (("env_passthrough", _ENV_NAME), ("allowed_tools", _NAME)):
            values = getattr(self, name)
            if (not isinstance(values, list)
                    or any(not isinstance(value, str) or not pattern.fullmatch(value) for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"mcp server {name} must contain unique valid names")


@dataclass
class MCPConfig:
    enabled: bool = False
    startup_timeout: float = 20
    request_timeout: float = 30
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError("Invalid [mcp] configuration")
        values = dict(raw)
        servers = values.pop("servers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcp.servers must be a table")
        try:
            conf = cls(**values, servers={name: MCPServerConfig(**server) for name, server in servers.items()})
        except (TypeError, ValueError):
            raise ValueError("Invalid [mcp] configuration fields") from None
        conf.validate()
        return conf

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("mcp.enabled must be a boolean")
        for name in ("startup_timeout", "request_timeout"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 120:
                raise ValueError(f"mcp.{name} must be in (0, 120]")
        if not isinstance(self.servers, dict):
            raise ValueError("mcp.servers must be a table")
        for name, server in self.servers.items():
            if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(server, MCPServerConfig):
                raise ValueError("mcp.servers must contain named server configurations")
            server.validate()


@dataclass
class _Request:
    tool: str
    arguments: dict = field(repr=False)
    future: asyncio.Future = field(repr=False)


class _Connection:
    """One task owns every SDK context enter/exit, including cancellation cleanup."""

    def __init__(self, name, config, root, startup_timeout, request_timeout):
        self.name, self.config, self.root = name, config, root
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self.active = None
        self.scope = None
        self.timeout_handle = None
        self.closing = False
        # A shared connection must not inherit the first caller's group trace,
        # sleep guard, or reserved model slot.
        self.task = asyncio.create_task(self.run(), name=f"atri.mcp.{name}", context=Context())

    def cancel(self, request):
        if self.active is request and self.scope is not None:
            self.clear_timeout()
            self.scope.cancel()

    def finish(self, error=None, result=None):
        self.clear_timeout()
        if self.active is not None and not self.active.future.done():
            if error is not None:
                self.active.future.set_exception(error)
            else:
                self.active.future.set_result(result)
        self.active = None

    def clear_timeout(self):
        if self.timeout_handle is not None:
            self.timeout_handle.cancel()
            self.timeout_handle = None

    def arm_timeout(self, seconds):
        self.clear_timeout()
        request, scope = self.active, self.scope

        def expire():
            if self.active is request and self.scope is scope:
                # Settle the caller before entering the SDK's shielded shutdown.
                # The same worker still owns and exits every AnyIO context.
                self.finish(_timeout_error())
                scope.cancel()

        self.timeout_handle = asyncio.get_running_loop().call_later(seconds, expire)

    def fail_pending(self):
        self.finish(ToolError("mcp_closed", "MCP 服务已关闭。"))
        while not self.queue.empty():
            request = self.queue.get_nowait()
            if not request.future.done():
                request.future.set_exception(ToolError("mcp_closed", "MCP 服务已关闭。"))

    async def next_request(self):
        while True:
            request = await self.queue.get()
            if not request.future.done():
                self.active = request
                return

    async def run(self):
        token = _private_io.set(True)
        try:
            while not self.closing:
                await self.next_request()
                try:
                    # This scope remains outside the SDK contexts for their entire
                    # lifetime. Timers settle requests before cancelling this scope,
                    # so subprocess cleanup does not extend the caller's timeout.
                    with anyio.CancelScope() as scope:
                        self.scope = scope
                        self.arm_timeout(self.startup_timeout)
                        await self.connected(scope)
                    if self.active is not None:
                        code, message = (("mcp_closed", "MCP 服务已关闭。") if self.closing else
                                         ("mcp_timeout", "MCP 请求超时，本次读取失败。"))
                        self.finish(ToolError(code, message))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Never include command, args, environment, or remote exception text.
                    log.warning("[MCP连接失败] 服务=%s", self.name)
                    error = (_timeout_error() if _is_timeout(exc) else
                             ToolError("mcp_unavailable", "MCP 服务连接或协议失败，请稍后重试。"))
                    self.finish(error)
                finally:
                    self.clear_timeout()
                    self.scope = None
        finally:
            self.fail_pending()
            _private_io.reset(token)

    async def connected(self, scope):
        env = {key: os.environ[key] for key in self.config.env_passthrough if key in os.environ}
        env.update(self.config.env)
        params = StdioServerParameters(command=self.config.command, args=list(self.config.args),
                                       env=env, cwd=str(self.root))
        with open(os.devnull, "w") as stderr:
            transport = stdio_client(params, errlog=stderr)
            # Client.__aenter__ unwinds its transport before exposing SDK errors.
            # A short request timeout must not preempt our startup timer there;
            # actual tool calls still receive their own request_timeout below.
            sdk_timeout = max(self.startup_timeout, self.request_timeout)
            async with Client(transport, read_timeout_seconds=sdk_timeout, cache=None) as client:
                try:
                    if scope.cancel_called:
                        return
                    available, cursor = set(), None
                    for _ in range(_LIST_PAGES):
                        listing = await client.list_tools(cursor=cursor)
                        if scope.cancel_called:
                            return
                        available.update(tool.name for tool in listing.tools)
                        cursor = listing.next_cursor
                        if cursor is None or set(self.config.allowed_tools) <= available:
                            break
                    self.clear_timeout()
                    log.info("[MCP已连接] 服务=%s", self.name)
                    while not self.closing:
                        if scope.cancel_called or self.active.future.cancelled():
                            return
                        if self.active.tool not in available:
                            self.finish(ToolError("mcp_tool_unavailable", "MCP 服务未提供配置的工具。"))
                        else:
                            # The caller's overall tool deadline also includes
                            # queueing/startup and may cancel sooner.
                            self.arm_timeout(self.request_timeout)
                            result = await client.call_tool(self.active.tool, self.active.arguments,
                                                            read_timeout_seconds=self.request_timeout)
                            if scope.cancel_called:
                                return
                            self.finish(result=result.model_dump(by_alias=True, exclude_none=True))
                        await self.next_request()
                except Exception as exc:
                    if _is_timeout(exc):
                        # SDK timeouts can arrive before our timer. They follow
                        # the same failure contract, without waiting for teardown.
                        self.finish(_timeout_error())
                    raise

    async def close(self):
        self.closing = True
        self.clear_timeout()
        if self.scope is not None:
            # Cooperative cancellation allows the SDK's shielded process cleanup.
            self.scope.cancel()
        else:
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        # A task cancelled before its first step never enters run()'s finally.
        self.fail_pending()


class MCPManager:
    def __init__(self, config: MCPConfig, root: Path):
        config.validate()
        self.config, self.root = config, Path(root).resolve()
        self._connections = {}
        self._closed = False
        self._close_task = None
        # Imported SDK loggers may log validation exception bodies. A scoped
        # filter protects our worker tasks without changing global logging levels.
        for name, logger in list(logging.Logger.manager.loggerDict.items()):
            if isinstance(logger, logging.Logger) and (name.startswith("mcp.") or name == "client"):
                logger.addFilter(_sdk_log_filter)

    async def start(self):
        """Connections are lazy: an unavailable optional service cannot block startup."""
        if self._closed:
            raise ToolError("mcp_closed", "MCP 服务已关闭。")

    async def call(self, server_name, tool_name, arguments):
        if self._closed:
            raise ToolError("mcp_closed", "MCP 服务已关闭。")
        server = self.config.servers.get(server_name)
        if not self.config.enabled or server is None or not server.enabled:
            raise ToolError("mcp_disabled", "所需 MCP 服务未启用。")
        if tool_name not in server.allowed_tools:
            raise ToolError("mcp_tool_denied", "该 MCP 工具不在允许列表中。")
        if not isinstance(arguments, dict):
            raise ToolError("invalid_arguments", "MCP 工具参数必须是对象。")
        connection = self._connections.get(server_name)
        if connection is None or connection.task.done():
            connection = self._connections[server_name] = _Connection(
                server_name, server, self.root, self.config.startup_timeout, self.config.request_timeout)
        request = _Request(tool_name, deepcopy(arguments), asyncio.get_running_loop().create_future())
        try:
            connection.queue.put_nowait(request)
        except asyncio.QueueFull:
            raise ToolError("mcp_busy", "MCP 服务正在处理其他请求，请稍后重试。") from None
        try:
            started = time.perf_counter()
            log.debug("[MCP调用开始] 服务=%s 工具=%s", server_name, tool_name)
            result = await request.future
            log.info("[MCP调用结束] 服务=%s 工具=%s 耗时=%.1fms", server_name, tool_name,
                     (time.perf_counter() - started) * 1000)
            return result
        except ToolError as exc:
            log.warning("[MCP调用失败] 服务=%s 工具=%s 错误=%s 耗时=%.1fms", server_name, tool_name,
                        exc.code, (time.perf_counter() - started) * 1000)
            raise
        except asyncio.CancelledError:
            connection.cancel(request)
            raise

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        # Even if the Bot's shutdown waiter is cancelled, child cleanup completes.
        await asyncio.shield(self._close_task)

    async def _close(self):
        await asyncio.gather(*(connection.close() for connection in self._connections.values()))
        self._connections.clear()
