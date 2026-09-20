import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler

from atri_bot.cli import serve
from atri_bot.config import Config
from atri_bot.storage import single_instance


ROOT = Path(__file__).resolve().parents[1]


def write_config(root, port=8080):
    (root / "personal_info.txt").write_text("仅用于本地停机测试。", encoding="utf-8")
    path = root / "config.toml"
    path.write_text(f'''[bot]
self_id = "99"
allowed_groups = [1]
data_dir = "data"
[onebot]
host = "127.0.0.1"
port = {port}
token = "test-token"
[llm]
api_key = "test-api-key"
base_url = "http://127.0.0.1:1/v1"
model = "test-model"
[schedule]
enabled = false
sleep_enabled = false
[logging]
level = "INFO"
color = "never"
file = ""
''', encoding="utf-8")
    return path


class ServeCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_startup_failure_closes_resources_and_restores_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.load(write_config(Path(directory)))
            loop = asyncio.get_running_loop()
            previous_handler = signal.getsignal(signal.SIGTERM)
            bot = MagicMock(close=AsyncMock())
            runner = MagicMock(setup=AsyncMock(side_effect=RuntimeError("startup failed")),
                               cleanup=AsyncMock())
            with (patch("atri_bot.cli.Bot", return_value=bot),
                  patch("atri_bot.cli.web.AppRunner", return_value=runner),
                  patch.object(loop, "add_signal_handler") as add_signal,
                  patch.object(loop, "remove_signal_handler") as remove_signal,
                  patch("atri_bot.cli.signal.signal") as restore_signal):
                with self.assertRaisesRegex(RuntimeError, "startup failed"):
                    await serve(config)
            runner.cleanup.assert_awaited_once()
            bot.close.assert_awaited_once()
            self.assertEqual(add_signal.call_args.args[0], signal.SIGTERM)
            remove_signal.assert_called_once_with(signal.SIGTERM)
            restore_signal.assert_called_once_with(signal.SIGTERM, previous_handler)
            with single_instance(config.data):
                pass

    async def test_unsupported_signal_handler_and_cleanup_failure_still_close_bot(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.load(write_config(Path(directory)))
            loop = asyncio.get_running_loop()
            bot = MagicMock(close=AsyncMock())
            runner = MagicMock(setup=AsyncMock(),
                               cleanup=AsyncMock(side_effect=RuntimeError("cleanup failed")))
            site = MagicMock(start=AsyncMock(side_effect=OSError("bind failed")))
            with (patch("atri_bot.cli.Bot", return_value=bot),
                  patch("atri_bot.cli.web.AppRunner", return_value=runner),
                  patch("atri_bot.cli.web.TCPSite", return_value=site),
                  patch.object(loop, "add_signal_handler", side_effect=NotImplementedError),
                  patch.object(loop, "remove_signal_handler") as remove_signal):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    await serve(config)
            runner.cleanup.assert_awaited_once()
            bot.close.assert_awaited_once()
            remove_signal.assert_not_called()
            with single_instance(config.data):
                pass


@unittest.skipIf(os.name == "nt", "POSIX process signals are required")
class ShutdownProcessTests(unittest.TestCase):
    def assert_graceful_exit(self, stop_signal):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            config = write_config(root, port)
            log_path = root / "server.log"
            with log_path.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "atri_bot.cli", "--config", str(config), "serve"],
                    cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
                try:
                    local_http = build_opener(ProxyHandler({}))
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            with local_http.open(f"http://127.0.0.1:{port}/healthz", timeout=.2) as response:
                                status = json.load(response)
                            break
                        except (URLError, TimeoutError):
                            time.sleep(.05)
                    else:
                        self.fail("Test server did not become ready: " + log_path.read_text(encoding="utf-8"))
                    self.assertEqual(status["status"], "ok")
                    self.assertFalse(status["connected"])
                    with self.assertRaisesRegex(RuntimeError, "Another ATRI process"):
                        with single_instance(root / "data"):
                            pass
                    process.send_signal(stop_signal)
                    self.assertEqual(process.wait(timeout=10), 0,
                                     log_path.read_text(encoding="utf-8"))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("正在关闭连接并等待消息队列结束", log)
            self.assertIn("服务已退出", log)
            self.assertNotIn("Traceback", log)
            self.assertNotIn("Unclosed", log)
            self.assertNotIn("[请求开始]", log)
            with single_instance(root / "data"):
                pass
            with socket.socket() as probe:
                probe.settimeout(.2)
                self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def test_sigterm_closes_listener_and_releases_data_lock(self):
        self.assert_graceful_exit(signal.SIGTERM)

    def test_sigint_preserves_ctrl_c_cleanup(self):
        self.assert_graceful_exit(signal.SIGINT)
