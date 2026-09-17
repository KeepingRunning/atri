import asyncio
from dataclasses import dataclass, field
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from atri_bot.asr_test import ASRConfig, SAMPLE_AUDIO_URL, run_asr_test
from atri_bot.config import Config


FAKE_KEY = "fake-asr-key-for-unit-tests"
BASE_URL = "https://asr.example/api/v1"
RESULT_URL = "https://results.example/transcript.json?signature=fake-signature"


@dataclass
class FakeResponse:
    data: object
    status: int = 200
    delay: float = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    downloaded: bool = False
    cancelled: bool = False
    closed: bool = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def json(self, **_):
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(self.data, Exception):
                raise self.data
            self.downloaded = True
            return self.data
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected additional request (no network fallback)")
        return self.responses.pop(0)


def submitted(**kwargs):
    return FakeResponse({"output": {"task_id": "task-123", "task_status": "PENDING"}}, **kwargs)


def completed(**kwargs):
    return FakeResponse({"output": {"task_status": "SUCCEEDED", "results": [{
        "subtask_status": "SUCCEEDED", "transcription_url": RESULT_URL,
    }]}}, **kwargs)


class ASRConfigTests(unittest.TestCase):
    def test_defaults_and_repr_do_not_expose_key(self):
        ASRConfig().validate()
        config = ASRConfig(api_key=FAKE_KEY)
        config.validate()
        self.assertEqual(config.model, "fun-asr")
        self.assertNotIn(FAKE_KEY, repr(config))
        self.assertNotIn("api_key", repr(config))

    def test_timeout_must_be_positive_finite_number(self):
        for timeout in (True, False, "60", None, 0, -1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "asr.timeout"):
                ASRConfig(timeout=timeout).validate()
        for timeout in (.01, 60, 120):
            with self.subTest(valid_timeout=timeout):
                ASRConfig(timeout=timeout).validate()

    def test_url_and_key_validation(self):
        for base_url in (
            "https://asr.example/compatible-mode/v1", "http://asr.example/api/v1",
            "https://user:password@asr.example/api/v1", "https://asr.example/api/v1?key=hidden",
            "https://asr.example:bad/api/v1", "https://asr.example/api/v1#fragment",
        ):
            with self.subTest(base_url=base_url), self.assertRaisesRegex(ValueError, "asr.base_url"):
                ASRConfig(base_url=base_url).validate()
        for key in (None, 123, "has whitespace", "has\nnewline"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "asr.api_key"):
                ASRConfig(api_key=key).validate()
        ASRConfig(base_url="http://127.0.0.1:12345/api/v1").validate()

    def test_config_load_reads_asr_and_rejects_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            text = ('[asr]\nbase_url="' + BASE_URL + '"\nmodel="fun-asr"\n'
                    'api_key="' + FAKE_KEY + '"\ntimeout=12.5\n')
            path.write_text(text, encoding="utf-8")
            config = Config.load(path)
            self.assertEqual(config.asr.base_url, BASE_URL)
            self.assertEqual(config.asr.model, "fun-asr")
            self.assertEqual(config.asr.api_key, FAKE_KEY)
            self.assertEqual(config.asr.timeout, 12.5)
            self.assertNotIn(FAKE_KEY, repr(config))
            path.write_text(text + 'typo="hidden"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"Invalid \[asr\] configuration fields"):
                Config.load(path)

    def test_cli_with_only_asr_config_needs_no_llm_or_qq_and_disables_file_logging(self):
        from atri_bot.cli import main

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.toml"
            path.write_text('[asr]\napi_key="' + FAKE_KEY + '"\n', encoding="utf-8")
            with (
                patch("atri_bot.cli.run_asr_test", new_callable=AsyncMock, return_value=True) as run,
                patch("atri_bot.cli.configure_logging") as configure_logging,
                patch.object(Config, "require_live", side_effect=AssertionError("Unexpected LLM requirement")),
                patch.object(Config, "require_serve", side_effect=AssertionError("Unexpected QQ requirement")),
                patch("atri_bot.cli.serve", new_callable=AsyncMock) as serve,
            ):
                self.assertIsNone(main(["--config", str(path), "test-asr"]))
            run.assert_awaited_once()
            serve.assert_not_awaited()
            config = run.await_args.args[0]
            self.assertEqual(config.asr.api_key, FAKE_KEY)
            self.assertEqual((config.api_key, config.model, config.token, config.self_id), ("", "", "", ""))
            self.assertFalse(config.groups)
            configure_logging.assert_called_once()
            self.assertEqual(configure_logging.call_args.args[0].file, "")
            self.assertIn(FAKE_KEY, configure_logging.call_args.kwargs["secrets"])
            self.assertEqual(list(root.iterdir()), [path])


class ASRTestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These tests neither read real credentials nor configure production logs.
        logger_patch = patch("atri_bot.asr_test.log")
        self.logger = logger_patch.start()
        self.addCleanup(logger_patch.stop)

    def config(self, **kwargs):
        return SimpleNamespace(asr=ASRConfig(base_url=BASE_URL, api_key=FAKE_KEY, **kwargs))

    async def run_fake(self, responses, *, config=None):
        session = FakeSession(responses)
        output = io.StringIO()
        with patch("atri_bot.asr_test.aiohttp.ClientSession", return_value=session) as factory:
            result = await run_asr_test(config or self.config(), stream=output)
        factory.assert_called_once()
        self.assertTrue(session.closed)
        self.assertNotIn(FAKE_KEY, output.getvalue())
        self.assertNotIn(FAKE_KEY, repr(self.logger.mock_calls))
        return result, output.getvalue(), session

    async def test_success_downloads_text_and_keeps_authorization_on_api_only(self):
        transcript = FakeResponse({"transcripts": [{"text": "  第一段测试文字  "}, {"text": "第二段"}]})
        pending = FakeResponse({"output": {"task_status": "RUNNING"}})
        with patch("atri_bot.asr_test.POLL_SECONDS", 0):
            result, output, session = await self.run_fake([submitted(), pending, completed(), transcript])
        self.assertTrue(result)
        self.assertTrue(transcript.downloaded)
        self.assertTrue(transcript.closed)
        self.assertIn("[通过]", output)
        self.assertIn("第一段测试文字", output)
        self.assertIn("第二段", output)
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET", "GET", "GET"])
        method, url, options = session.calls[0]
        self.assertEqual(url, BASE_URL + "/services/audio/asr/transcription")
        self.assertEqual(options["json"], {"model": "fun-asr", "input": {"file_urls": [SAMPLE_AUDIO_URL]}, "parameters": {}})
        self.assertEqual(options["headers"]["X-DashScope-Async"], "enable")
        for _, api_url, options in session.calls[:-1]:
            self.assertTrue(api_url.startswith(BASE_URL + "/"))
            self.assertEqual(options["headers"]["Authorization"], "Bearer " + FAKE_KEY)
            self.assertFalse(options["allow_redirects"])
        self.assertEqual(session.calls[1][1], BASE_URL + "/tasks/task-123")
        self.assertEqual(session.calls[-1][1], RESULT_URL)
        self.assertNotIn("Authorization", session.calls[-1][2].get("headers", {}))
        self.assertFalse(session.calls[-1][2]["allow_redirects"])

    async def test_http_401_fails_without_retry_or_remote_message_leak(self):
        failure = FakeResponse({"code": "InvalidApiKey", "message": FAKE_KEY}, status=401)
        result, output, session = await self.run_fake([failure])
        self.assertFalse(result)
        self.assertIn("HTTP 401", output)
        self.assertIn("InvalidApiKey", output)
        self.assertEqual(len(session.calls), 1)

    async def test_failed_cancelled_and_unknown_tasks_stop_polling(self):
        for status in ("FAILED", "CANCELED", "UNKNOWN", "UNEXPECTED"):
            with self.subTest(status=status):
                failure = FakeResponse({"output": {"task_status": status, "code": "TaskFailure", "message": FAKE_KEY}})
                result, output, session = await self.run_fake([submitted(), failure])
                self.assertFalse(result)
                self.assertIn("[失败]", output)
                self.assertEqual(len(session.calls), 2)

    async def test_successful_task_with_failed_subtask_does_not_download(self):
        failure = FakeResponse({"output": {"task_status": "SUCCEEDED", "results": [{
            "subtask_status": "FAILED", "code": "FILE_DOWNLOAD_FAILED", "message": FAKE_KEY,
            "transcription_url": RESULT_URL,
        }]}})
        result, output, session = await self.run_fake([submitted(), failure])
        self.assertFalse(result)
        self.assertIn("FILE_DOWNLOAD_FAILED", output)
        self.assertEqual(len(session.calls), 2)

    async def test_empty_or_missing_transcript_text_is_failure(self):
        for data in ({}, {"transcripts": []}, {"transcripts": [{"text": " \n\t"}, {"text": None}, {}]}):
            with self.subTest(data=data):
                result, output, session = await self.run_fake([submitted(), completed(), FakeResponse(data)])
                self.assertFalse(result)
                self.assertIn("[失败]", output)
                self.assertNotIn("[通过]", output)
                self.assertEqual(len(session.calls), 3)

    async def test_download_failure_cannot_report_submission_as_success(self):
        failure = FakeResponse({"code": "AccessDenied", "message": FAKE_KEY}, status=403)
        result, output, session = await self.run_fake([submitted(), completed(), failure])
        self.assertFalse(result)
        self.assertIn("HTTP 403", output)
        self.assertNotIn("[通过]", output)
        self.assertEqual(len(session.calls), 3)

    async def test_network_failure_does_not_retry_or_print_exception_message(self):
        result, output, session = await self.run_fake([FakeResponse(aiohttp.ClientConnectionError(FAKE_KEY))])
        self.assertFalse(result)
        self.assertIn("ClientConnectionError", output)
        self.assertEqual(len(session.calls), 1)

    async def test_deadline_includes_poll_sleep(self):
        pending = FakeResponse({"output": {"task_status": "PENDING"}})
        with patch("atri_bot.asr_test.POLL_SECONDS", 60):
            result, output, session = await self.run_fake([submitted(), pending], config=self.config(timeout=.02))
        self.assertFalse(result)
        self.assertIn("总超时", output)
        self.assertIn("不重试", output)
        self.assertEqual(len(session.calls), 2)

    async def test_one_deadline_covers_submission_polling_and_transcript_download(self):
        # Each operation is below the deadline, but the whole workflow exceeds it.
        # The final download would finish if each stage reset the overall timeout.
        transcript = FakeResponse({"transcripts": [{"text": "too late"}]}, delay=.4)
        pending = FakeResponse({"output": {"task_status": "RUNNING"}}, delay=.02)
        with patch("atri_bot.asr_test.POLL_SECONDS", .08):
            result, output, session = await self.run_fake(
                [submitted(delay=.08), pending, completed(delay=.02), transcript],
                config=self.config(timeout=.5),
            )
        self.assertFalse(result)
        self.assertIn("总超时", output)
        self.assertTrue(transcript.started.is_set())
        self.assertTrue(transcript.cancelled)
        self.assertTrue(transcript.closed)
        self.assertFalse(transcript.downloaded)
        self.assertEqual([c[0] for c in session.calls].count("POST"), 1)
        self.assertEqual(len(session.calls), 4)

    async def test_caller_cancellation_propagates_and_closes_session(self):
        for stage in ("poll", "download"):
            with self.subTest(stage=stage):
                blocked = FakeResponse({}, delay=60)
                responses = [submitted(), blocked] if stage == "poll" else [submitted(), completed(), blocked]
                session = FakeSession(responses)
                output = io.StringIO()
                with patch("atri_bot.asr_test.aiohttp.ClientSession", return_value=session):
                    task = asyncio.create_task(run_asr_test(self.config(), stream=output))
                    try:
                        await asyncio.wait_for(blocked.started.wait(), 1)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(blocked.cancelled)
                self.assertTrue(blocked.closed)
                self.assertTrue(session.closed)
                self.assertNotIn("[通过]", output.getvalue())
                self.assertNotIn("[失败]", output.getvalue())
                self.assertEqual([c[0] for c in session.calls].count("POST"), 1)

    async def test_missing_key_fails_before_constructing_session(self):
        config = SimpleNamespace(asr=ASRConfig())
        with patch("atri_bot.asr_test.aiohttp.ClientSession") as factory:
            with self.assertRaisesRegex(ValueError, "api_key"):
                await run_asr_test(config, stream=io.StringIO())
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
