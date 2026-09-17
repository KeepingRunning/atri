import asyncio
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import anyio
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import REQUEST_TIMEOUT, CallToolResult

from atri_bot.mcp_client import MCPConfig, MCPManager, MCPServerConfig, _Connection, _Request
from atri_bot.logging_setup import current_log_context, log_context
from atri_bot.tools import ToolError


FAKE_SERVER = r'''
import asyncio
import json
import os
from pathlib import Path
import sys

def event(value):
    with open(os.environ["TEST_EVENTS"], "a") as f:
        f.write(json.dumps({"event": value, "pid": os.getpid()}) + "\n")

event("spawned")
mode = os.getenv("TEST_MODE", "normal")
if mode == "legacy_startup_delay":
    import time
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "server/discover":
            response["error"] = {"code": -32601, "message": "legacy server"}
        elif method == "initialize":
            event("initialize:start")
            time.sleep(float(os.getenv("TEST_HANDSHAKE_DELAY", ".15")))
            response["result"] = {"protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "legacy-test", "version": "1"}}
        elif method == "tools/list":
            response["result"] = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            event("echo:legacy")
            response["result"] = {"content": [], "structuredContent": {"text": "legacy", "pid": os.getpid()}}
        print(json.dumps(response), flush=True)
    sys.exit(0)
if mode == "startup_hang":
    import time
    time.sleep(60)
if mode == "startup_crash":
    print(os.getenv("TEST_SECRET", ""), file=sys.stderr, flush=True)
    os._exit(12)
if mode == "stubborn_shutdown":
    import atexit
    import signal
    import time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    atexit.register(time.sleep, 60)

from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent

server = MCPServer("local-test")

@server.tool()
async def echo(text: str = "hello") -> CallToolResult:
    event("echo:" + text)
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content={
        "text": text, "pid": os.getpid(), "cwd": os.getcwd(),
        "allowed": os.getenv("TEST_ALLOWED"), "denied_present": "TEST_DENIED" in os.environ,
        "path_present": bool(os.getenv("PATH"))})

@server.tool()
async def slow(seconds: float = 0.1) -> dict:
    event("slow:start")
    await asyncio.sleep(seconds)
    event("slow:end")
    return {"finished": True, "pid": os.getpid()}

@server.tool()
async def crash() -> dict:
    event("crash")
    os._exit(19)

@server.tool()
async def malformed() -> dict:
    print(os.getenv("TEST_SECRET", ""), file=sys.stderr, flush=True)
    print("not-json:" + os.getenv("TEST_SECRET", ""), flush=True)
    os._exit(21)

@server.tool()
async def tool_error() -> CallToolResult:
    return CallToolResult(is_error=True, content=[TextContent(type="text", text="unavailable")])

@server.tool()
async def manage_account() -> str:
    event("must-not-run")
    return "not allowed"

event("started")
server.run()
'''


class MCPConfigTests(unittest.TestCase):
    def test_disabled_defaults_and_strict_configuration(self):
        self.assertFalse(MCPConfig.from_dict({}).enabled)
        self.assertEqual(MCPConfig.from_dict({}).request_timeout, 30)
        conf = MCPConfig.from_dict({"enabled": True, "startup_timeout": 5, "request_timeout": 120, "servers": {
            "reader": {"command": "python", "args": ["fake.py"], "env": {"SECRET": "hidden"},
                       "env_passthrough": ["TOKEN"], "allowed_tools": ["read"]}}})
        self.assertEqual(conf.servers["reader"].allowed_tools, ["read"])
        self.assertEqual(conf.request_timeout, 120)
        self.assertNotIn("hidden", repr(conf))
        for raw in (None, [], {"unknown": 1}, {"enabled": 1}, {"startup_timeout": True},
                    {"startup_timeout": 0}, {"startup_timeout": float("nan")}, {"servers": []},
                    {"request_timeout": True}, {"request_timeout": "30"}, {"request_timeout": 0},
                    {"request_timeout": -1}, {"request_timeout": 121}, {"request_timeout": float("nan")},
                    {"request_timeout": float("inf")},
                    {"servers": {"reader": {"command": ""}}},
                    {"servers": {"reader": {"command": "python", "args": "bad"}}},
                    {"servers": {"reader": {"command": "python", "enabled": 1}}},
                    {"servers": {"reader": {"command": "python", "env": {"BAD=KEY": "hidden"}}}},
                    {"servers": {"reader": {"command": "python", "env_passthrough": ["X", "X"]}}},
                    {"servers": {"reader": {"command": "python", "allowed_tools": ["*"]}}},
                    {"servers": {"bad name": {"command": "python"}}}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                MCPConfig.from_dict(raw)


class MCPClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "fake_mcp.py"
        self.script.write_text(textwrap.dedent(FAKE_SERVER))
        self.events = self.root / "events.jsonl"
        self.managers = []

    async def asyncTearDown(self):
        await asyncio.gather(*(manager.close() for manager in self.managers))
        self.tmp.cleanup()

    def manager(self, *, mode="normal", timeout=5, request_timeout=30, extra_env=None, args=None):
        server = MCPServerConfig(command=sys.executable,
            args=[str(self.script)] if args is None else args,
            env={"TEST_EVENTS": str(self.events), "TEST_MODE": mode, **(extra_env or {})},
            env_passthrough=["TEST_ALLOWED"],
            allowed_tools=["echo", "slow", "crash", "malformed", "tool_error", "missing"])
        manager = MCPManager(MCPConfig(enabled=True, startup_timeout=timeout, request_timeout=request_timeout,
                                       servers={"reader": server}), self.root)
        self.managers.append(manager)
        return manager

    def records(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    async def wait_event(self, name, count=1):
        async with asyncio.timeout(5):
            while len([r for r in self.records() if r["event"] == name]) < count:
                await asyncio.sleep(.01)

    def data(self, result):
        return result["structuredContent"]

    async def test_handshake_reuse_environment_cwd_and_sdk_result(self):
        manager = self.manager()
        with patch.dict(os.environ, {"TEST_ALLOWED": "selected", "TEST_DENIED": "do-not-inherit"}):
            await manager.start()
            self.assertFalse(self.events.exists())  # Lazy, so unavailable services do not block startup.
            one = self.data(await manager.call("reader", "echo", {"text": "one"}))
            two = self.data(await manager.call("reader", "echo", {"text": "two"}))
        self.assertEqual(one["pid"], two["pid"])
        self.assertEqual(one["cwd"], str(self.root.resolve()))
        self.assertEqual(one["allowed"], "selected")
        self.assertFalse(one["denied_present"])
        self.assertTrue(one["path_present"])
        self.assertEqual(two["text"], "two")
        result = await manager.call("reader", "tool_error", {})
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "unavailable")

    async def test_allowlist_and_unavailable_tools(self):
        manager = self.manager()
        for server, tool, code in (("missing", "echo", "mcp_disabled"),
                                   ("reader", "manage_account", "mcp_tool_denied")):
            with self.assertRaises(ToolError) as cm:
                await manager.call(server, tool, {})
            self.assertEqual(cm.exception.code, code)
        self.assertFalse(self.events.exists())
        with self.assertRaises(ToolError) as cm:
            await manager.call("reader", "missing", {})
        self.assertEqual(cm.exception.code, "mcp_tool_unavailable")
        self.assertNotIn("must-not-run", [r["event"] for r in self.records()])
        self.assertEqual(self.data(await manager.call("reader", "echo", {}))["text"], "hello")

    async def test_concurrent_calls_are_serial_and_bounded(self):
        with patch("atri_bot.mcp_client._QUEUE_SIZE", 1):
            manager = self.manager()
            await manager.call("reader", "echo", {})
            first = asyncio.create_task(manager.call("reader", "slow", {"seconds": .15}))
            await self.wait_event("slow:start")
            second = asyncio.create_task(manager.call("reader", "echo", {"text": "queued"}))
            await asyncio.sleep(0)
            with self.assertRaises(ToolError) as cm:
                await manager.call("reader", "echo", {"text": "overflow"})
            self.assertEqual(cm.exception.code, "mcp_busy")
            await asyncio.gather(first, second)
        events = [r["event"] for r in self.records()]
        self.assertLess(events.index("slow:end"), events.index("echo:queued"))
        self.assertNotIn("echo:overflow", events)

    async def test_process_exit_is_safe_and_next_call_reconnects(self):
        manager = self.manager()
        first = self.data(await manager.call("reader", "echo", {}))
        with self.assertRaises(ToolError) as cm:
            await manager.call("reader", "crash", {})
        self.assertEqual(cm.exception.code, "mcp_unavailable")
        second = self.data(await manager.call("reader", "echo", {}))
        self.assertNotEqual(first["pid"], second["pid"])

    async def test_caller_cancellation_reconnects_without_cancelling_queued_call(self):
        manager = self.manager()
        first = asyncio.create_task(manager.call("reader", "slow", {"seconds": 60}))
        await self.wait_event("slow:start")
        queued = asyncio.create_task(manager.call("reader", "echo", {"text": "after"}))
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        result = self.data(await asyncio.wait_for(queued, 10))
        self.assertEqual(result["text"], "after")
        self.assertEqual(len([r for r in self.records() if r["event"] == "started"]), 2)

    async def test_cancelled_queued_call_never_reaches_server(self):
        manager = self.manager()
        first = asyncio.create_task(manager.call("reader", "slow", {"seconds": .15}))
        await self.wait_event("slow:start")
        queued = asyncio.create_task(manager.call("reader", "echo", {"text": "cancelled"}))
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        await first
        await manager.call("reader", "echo", {"text": "after"})
        self.assertNotIn("echo:cancelled", [r["event"] for r in self.records()])
        self.assertEqual(len([r for r in self.records() if r["event"] == "started"]), 1)

    async def test_request_timeout_and_startup_timeout_are_recoverable(self):
        manager = self.manager(request_timeout=2)
        await manager.call("reader", "echo", {})
        async with asyncio.timeout(5):
            with self.assertRaises(ToolError) as cm:
                await manager.call("reader", "slow", {"seconds": 60})
        self.assertEqual(cm.exception.code, "mcp_timeout")
        self.assertEqual(self.data(await manager.call("reader", "echo", {}))["text"], "hello")
        hanging = self.manager(mode="startup_hang", timeout=.1)
        with self.assertRaises(ToolError) as cm:
            await hanging.call("reader", "echo", {})
        self.assertEqual(cm.exception.code, "mcp_timeout")
        hanging.config.servers["reader"].env["TEST_MODE"] = "normal"
        hanging.config.startup_timeout = 5
        hanging._connections["reader"].startup_timeout = 5
        self.assertEqual(self.data(await hanging.call("reader", "echo", {}))["text"], "hello")

    @unittest.skipIf(sys.platform == "win32", "POSIX process liveness check")
    async def test_request_timeout_returns_before_shutdown_and_queued_call_reconnects(self):
        manager = self.manager(mode="stubborn_shutdown", request_timeout=2)
        pid = self.data(await manager.call("reader", "echo", {}))["pid"]
        connection = manager._connections["reader"]
        connection.request_timeout = .01
        started = asyncio.get_running_loop().time()
        timed_out = asyncio.create_task(manager.call("reader", "slow", {"seconds": 60}))
        queued = asyncio.create_task(manager.call("reader", "echo", {"text": "after-timeout"}))
        with self.assertRaises(ToolError) as cm:
            await timed_out
        self.assertLess(asyncio.get_running_loop().time() - started, .5)
        self.assertEqual(cm.exception.code, "mcp_timeout")
        self.assertEqual(str(cm.exception), "MCP 请求超时，本次读取失败。")
        os.kill(pid, 0)  # The failed call returns while the SDK still reaps this process.
        self.assertFalse(queued.done())
        connection.request_timeout = 2
        manager.config.servers["reader"].env["TEST_MODE"] = "normal"
        result = self.data(await asyncio.wait_for(queued, 10))
        self.assertEqual(result["text"], "after-timeout")
        self.assertNotEqual(result["pid"], pid)
        events = [record["event"] for record in self.records()]
        self.assertEqual(events.count("slow:start"), 1)
        self.assertNotIn("slow:end", events)
        self.assertEqual(events.count("echo:after-timeout"), 1)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        await manager.close()
        with self.assertRaises(ProcessLookupError):
            os.kill(result["pid"], 0)

    @unittest.skipIf(sys.platform == "win32", "POSIX process liveness check")
    async def test_startup_timeout_returns_before_shutdown_and_close_reaps_process(self):
        manager = self.manager(mode="startup_hang", timeout=.2)
        started = asyncio.get_running_loop().time()
        with self.assertRaises(ToolError) as cm:
            await manager.call("reader", "echo", {})
        self.assertLess(asyncio.get_running_loop().time() - started, .7)
        self.assertEqual(cm.exception.code, "mcp_timeout")
        self.assertEqual(str(cm.exception), "MCP 请求超时，本次读取失败。")
        records = self.records()
        self.assertEqual([record["event"] for record in records], ["spawned"])
        pid = records[0]["pid"]
        os.kill(pid, 0)
        await asyncio.wait_for(manager.close(), 10)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(len(self.records()), 1)  # No automatic startup retry.

    async def test_legacy_handshake_uses_startup_budget_when_request_timeout_is_shorter(self):
        manager = self.manager(mode="legacy_startup_delay", timeout=1, request_timeout=.05)
        result = self.data(await manager.call("reader", "echo", {}))
        self.assertEqual(result["text"], "legacy")
        self.assertEqual([record["event"] for record in self.records()],
                         ["spawned", "initialize:start", "echo:legacy"])

    @unittest.skipIf(sys.platform == "win32", "POSIX process liveness check")
    async def test_legacy_handshake_timeout_returns_before_cleanup_with_short_request_budget(self):
        manager = self.manager(mode="legacy_startup_delay", timeout=.3, request_timeout=.05,
                               extra_env={"TEST_HANDSHAKE_DELAY": "60"})
        started = asyncio.get_running_loop().time()
        with self.assertRaises(ToolError) as cm:
            await manager.call("reader", "echo", {})
        self.assertLess(asyncio.get_running_loop().time() - started, .8)
        self.assertEqual(cm.exception.code, "mcp_timeout")
        records = self.records()
        self.assertEqual([record["event"] for record in records], ["spawned", "initialize:start"])
        pid = records[0]["pid"]
        os.kill(pid, 0)
        await asyncio.wait_for(manager.close(), 10)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(len(self.records()), 2)

    async def test_sdk_timeout_has_safe_timeout_code_before_teardown(self):
        manager = self.manager(mode="stubborn_shutdown")
        await manager.call("reader", "echo", {})

        async def sdk_timeout(*args, **kwargs):
            raise ExceptionGroup("remote details", [MCPError(REQUEST_TIMEOUT, "private timeout details")])

        with patch.object(Client, "call_tool", sdk_timeout):
            started = asyncio.get_running_loop().time()
            with self.assertRaises(ToolError) as cm:
                await manager.call("reader", "slow", {})
        self.assertLess(asyncio.get_running_loop().time() - started, .5)
        self.assertEqual(cm.exception.code, "mcp_timeout")
        self.assertEqual(str(cm.exception), "MCP 请求超时，本次读取失败。")
        self.assertNotIn("slow:start", [record["event"] for record in self.records()])

    async def test_late_result_cannot_replace_timeout_or_consume_queued_call(self):
        manager = self.manager()
        await manager.call("reader", "echo", {})
        connection = manager._connections["reader"]
        connection.request_timeout = .01
        original_call = Client.call_tool
        late_result = asyncio.Event()
        calls = []

        async def delayed_result(client, name, arguments, **kwargs):
            if name != "slow":
                return await original_call(client, name, arguments, **kwargs)
            calls.append(name)
            with anyio.CancelScope(shield=True):
                await asyncio.sleep(.1)
            late_result.set()
            return CallToolResult(content=[], structured_content={"text": "too late"})

        with patch.object(Client, "call_tool", delayed_result):
            failed = asyncio.create_task(manager.call("reader", "slow", {}))
            queued = asyncio.create_task(manager.call("reader", "echo", {"text": "fresh"}))
            with self.assertRaises(ToolError) as cm:
                await failed
            self.assertEqual(cm.exception.code, "mcp_timeout")
            connection.request_timeout = 30
            result = self.data(await asyncio.wait_for(queued, 5))
        self.assertTrue(late_result.is_set())
        self.assertEqual(calls, ["slow"])
        self.assertEqual(result["text"], "fresh")
        self.assertIs(failed.exception(), cm.exception)

    async def test_close_finishes_pending_calls_and_reaps_subprocess(self):
        manager = self.manager()
        pid = self.data(await manager.call("reader", "echo", {}))["pid"]
        running = asyncio.create_task(manager.call("reader", "slow", {"seconds": 60}))
        await self.wait_event("slow:start")
        queued = asyncio.create_task(manager.call("reader", "echo", {}))
        await asyncio.sleep(0)
        await asyncio.wait_for(manager.close(), 10)
        results = await asyncio.gather(running, queued, return_exceptions=True)
        self.assertTrue(all(isinstance(result, ToolError) for result in results))
        self.assertEqual([result.code for result in results], ["mcp_closed", "mcp_closed"])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        await manager.close()
        with self.assertRaises(ToolError) as cm:
            await manager.call("reader", "echo", {})
        self.assertEqual(cm.exception.code, "mcp_closed")

    async def test_close_before_worker_starts_finishes_queued_future(self):
        manager = self.manager()
        worker = _Connection("reader", manager.config.servers["reader"], self.root, 5, 30)
        future = asyncio.get_running_loop().create_future()
        worker.queue.put_nowait(_Request("echo", {}, future))
        await worker.close()
        with self.assertRaises(ToolError) as cm:
            await future
        self.assertEqual(cm.exception.code, "mcp_closed")
        self.assertFalse(self.events.exists())

    async def test_secret_stderr_and_protocol_errors_do_not_enter_errors_or_logs(self):
        secret = "do-not-log-test-env-secret"
        manager = self.manager(extra_env={"TEST_SECRET": secret})
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        root_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        errors = []
        try:
            await manager.call("reader", "echo", {})
            with self.assertRaises(ToolError) as cm:
                await manager.call("reader", "malformed", {})
            errors.append(str(cm.exception))
            broken = self.manager(mode="startup_crash", extra_env={"TEST_SECRET": secret}, args=[str(self.script), secret])
            await broken.start()
            with self.assertRaises(ToolError) as cm:
                await broken.call("reader", "echo", {})
            errors.append(str(cm.exception))
        finally:
            await manager.close()
            root_logger.removeHandler(handler)
            root_logger.setLevel(previous_level)
        self.assertNotIn(secret, captured.getvalue())
        self.assertNotIn(secret, repr(errors))
        self.assertIn("MCP", captured.getvalue())

    async def test_worker_logs_have_no_first_group_context(self):
        records = []
        class Capture(logging.Handler):
            def emit(self, record):
                records.append((record.getMessage(), current_log_context()))
        handler = Capture()
        logger = logging.getLogger("atri.mcp")
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            manager = self.manager()
            with log_context(group_id="first"):
                await manager.call("reader", "echo", {})
            with log_context(group_id="second"):
                await manager.call("reader", "echo", {})
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        connected = [context for text, context in records if "MCP已连接" in text]
        completed = [context["group_id"] for text, context in records if "MCP调用结束" in text]
        self.assertEqual(connected, [{}])
        self.assertEqual(completed, ["first", "second"])

    @unittest.skipIf(sys.platform == "win32", "POSIX termination escalation")
    async def test_close_kills_stubborn_process_even_if_shutdown_waiter_cancelled(self):
        manager = self.manager(mode="stubborn_shutdown")
        pid = self.data(await manager.call("reader", "echo", {}))["pid"]
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(.05)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        await asyncio.wait_for(manager.close(), 10)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
