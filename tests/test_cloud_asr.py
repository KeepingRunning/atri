import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import aiohttp

from atri_bot.cloud_asr import ASRConfig, CloudASR, _audio_stream, _multipart, _transcript
from atri_bot.tools import ToolError


KEY = "test-secret-never-log"
BASE = "https://configured-asr.example/api/v1"
HOST = "https://temporary.oss-cn-beijing.aliyuncs.com"
RESULT = "https://results.oss-cn-beijing.aliyuncs.com/result.json?signature=private"
POLICY = {"upload_host": HOST, "upload_dir": "dashscope-instant/account/date",
          "oss_access_key_id": "oss-id", "policy": "encoded-policy", "signature": "private-signature",
          "x_oss_object_acl": "private", "x_oss_forbid_overwrite": "true", "max_file_size_mb": 128}


@dataclass
class Response:
    data: object = field(default_factory=dict)
    status: int = 200
    delay: float = 0
    content_length: int | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    consumed: bytes | None = None
    stream: object = None
    closed: bool = False
    cancelled: bool = False

    @property
    def content(self):
        return self

    async def __aenter__(self):
        if self.stream is not None:
            self.consumed = b"".join([chunk async for chunk in self.stream])
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def iter_chunked(self, size):
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if isinstance(self.data, Exception):
                raise self.data
            data = self.data if isinstance(self.data, bytes) else json.dumps(self.data, ensure_ascii=False).encode()
            for offset in range(0, len(data), size):
                yield data[offset:offset + size]
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class Session:
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
            raise AssertionError("Unexpected extra request/retry")
        response = self.responses.pop(0)
        response.stream = kwargs.get("data")
        return response


def success():
    return [Response({"data": dict(POLICY)}), Response(b""),
            Response({"output": {"task_id": "task-123"}}),
            Response({"output": {"task_status": "SUCCEEDED", "results": [
                {"subtask_status": "SUCCEEDED", "transcription_url": RESULT}]}}),
            Response({"transcripts": [{"channel_id": 0, "text": "第一句。 第二句。",
                                       "sentences": [{"text": "第一句。", "begin_time": 0, "end_time": 900},
                                                     {"text": "第二句。", "begin_time": 1000, "end_time": 1900}]}],
                      "properties": {"original_duration_in_milliseconds": 2000}})]


class CloudConfigTests(unittest.TestCase):
    def test_bounded_new_settings(self):
        ASRConfig().validate()
        for field_name, bad_values in {
            "enabled": [1, None, "true"], "dns_over_https": [1, None, "false"],
            "max_audio_bytes": [0, -1, True, 1.5, 1073741825],
            "max_audio_seconds": [0, -1, True, float("nan"), 43201],
            "timeout": [1801, float("inf")], "download_timeout": [0, True, 301],
        }.items():
            for value in bad_values:
                with self.subTest(name=field_name, value=value), self.assertRaisesRegex(ValueError, field_name):
                    ASRConfig(**{field_name: value}).validate()

    def test_diagnostic_config_is_same_class(self):
        from atri_bot.asr_test import ASRConfig as DiagnosticConfig
        self.assertIs(DiagnosticConfig, ASRConfig)
        self.assertNotIn(KEY, repr(ASRConfig(api_key=KEY)))


class CloudASRTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "input.m4a"
        self.path.write_bytes(b"fake-bounded-audio\x00" * 10)
        self.audio = SimpleNamespace(path=self.path, bytes=self.path.stat().st_size, duration_seconds=2)
        logger = patch("atri_bot.cloud_asr.log")
        self.logger = logger.start()
        self.addCleanup(logger.stop)

    def config(self, **kwargs):
        return ASRConfig(api_key=KEY, base_url=BASE, **kwargs)

    async def run_fake(self, responses=None, *, config=None, check_active=None):
        session = Session(success() if responses is None else responses)
        with patch("atri_bot.cloud_asr.aiohttp.ClientSession", return_value=session), patch("atri_bot.cloud_asr.POLL_SECONDS", 0):
            result = await CloudASR(config or self.config()).transcribe(self.audio, check_active or Mock())
        self.assertTrue(session.closed)
        self.assertNotIn(KEY, repr(self.logger.mock_calls))
        self.assertNotIn("private-signature", repr(self.logger.mock_calls))
        self.assertNotIn(RESULT, repr(self.logger.mock_calls))
        return result, session

    async def assert_failure(self, responses, code, *, config=None):
        session = Session(responses)
        with patch("atri_bot.cloud_asr.aiohttp.ClientSession", return_value=session), self.assertRaises(ToolError) as caught:
            await CloudASR(config or self.config()).transcribe(self.audio, Mock())
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(KEY, str(caught.exception))
        self.assertTrue(session.closed)
        return session

    async def test_success_preserves_raw_text_timestamps_and_offsets(self):
        transcript, _ = await self.run_fake()
        self.assertEqual(transcript.text, "第一句。 第二句。")
        self.assertEqual(transcript.duration_ms, 2000)
        self.assertEqual(transcript.task_id, "task-123")
        self.assertEqual(transcript.segments[1], {"text": "第二句。", "begin_time": 1000, "end_time": 1900,
                                                 "char_start": 5, "char_end": 9})

    async def test_upload_has_exact_content_length_and_authorization_only_on_api(self):
        responses = success()
        _, session = await self.run_fake(responses)
        upload = responses[1].consumed
        self.assertIsNotNone(upload)
        self.assertIn(self.path.read_bytes(), upload)
        upload_headers = session.calls[1][2]["headers"]
        self.assertEqual(int(upload_headers["Content-Length"]), len(upload))
        self.assertNotIn("Transfer-Encoding", upload_headers)
        self.assertNotIn(KEY.encode(), upload)
        self.assertNotIn(str(self.path).encode(), upload)
        for _, url, kwargs in session.calls:
            self.assertFalse(kwargs["allow_redirects"])
            if url.startswith(BASE + "/"):
                self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + KEY)
            else:
                self.assertNotIn("Authorization", kwargs.get("headers", {}))
        submission = session.calls[2][2]
        self.assertEqual(submission["json"]["parameters"], {"channel_id": [0]})
        self.assertTrue(submission["json"]["input"]["file_urls"][0].startswith("oss://dashscope-instant/"))
        self.assertEqual(submission["headers"]["X-DashScope-OssResourceResolve"], "enable")

    async def test_stream_bounds_memory_for_large_local_audio(self):
        self.path.write_bytes(b"a" * (3 * 128 * 1024 + 17))
        size = self.path.stat().st_size
        _, _, prefix, trailer, headers = _multipart(POLICY, self.path, size)
        lengths = [len(part) async for part in _audio_stream(self.path, size, prefix, trailer, Mock())]
        self.assertEqual(sum(lengths), int(headers["Content-Length"]))
        self.assertLessEqual(max(lengths), 128 * 1024)

    async def test_stream_detects_changed_audio(self):
        self.path.write_bytes(b"changed")
        with self.assertRaises(ToolError):
            [part async for part in _audio_stream(self.path, self.audio.bytes, b"", b"", Mock())]

    async def test_polling_does_not_resubmit(self):
        responses = success()
        responses.insert(3, Response({"output": {"task_status": "PENDING"}}))
        responses.insert(4, Response({"output": {"task_status": "RUNNING"}}))
        _, session = await self.run_fake(responses)
        self.assertEqual(sum(url.endswith("/transcription") for _, url, _ in session.calls), 1)
        self.assertEqual(sum("/tasks/" in url for _, url, _ in session.calls), 3)

    async def test_http_failure_no_retry_or_provider_message_leak(self):
        session = await self.assert_failure([Response({"code": KEY, "message": KEY}, status=401)], "asr_http_error")
        self.assertEqual(len(session.calls), 1)

    async def test_upload_failure_does_not_submit(self):
        session = await self.assert_failure([success()[0], Response(KEY.encode(), status=403)], "asr_upload_failed")
        self.assertEqual(len(session.calls), 2)

    async def test_failed_tasks_do_not_download_result(self):
        for status in ("FAILED", "CANCELED", "UNKNOWN", "UNEXPECTED"):
            with self.subTest(status=status):
                responses = success()[:3] + [Response({"output": {"task_status": status, "message": KEY}})]
                session = await self.assert_failure(responses, "asr_invalid_response" if status == "UNEXPECTED" else "asr_task_failed")
                self.assertEqual(len(session.calls), 4)

    async def test_success_without_successful_subtask_fails(self):
        for results in ([], [{"subtask_status": "FAILED"}], [{"subtask_status": "SUCCEEDED"}] * 2):
            with self.subTest(results=results):
                await self.assert_failure(success()[:3] + [Response({"output": {"task_status": "SUCCEEDED", "results": results}})], "asr_task_failed")

    async def test_invalid_json_and_oversized_bodies_fail_boundedly(self):
        for body in (b"not JSON", b"[1, 2]", b'{"value":NaN}'):
            with self.subTest(body=body):
                await self.assert_failure([Response(body)], "asr_invalid_response")
        await self.assert_failure([Response({}, content_length=10_000_000)], "asr_response_too_large")
        await self.assert_failure([Response(b"a" * (256 * 1024 + 1))], "asr_response_too_large")

    async def test_result_download_has_separate_size_limit(self):
        responses = success()
        responses[-1] = Response({}, content_length=33 * 1024 * 1024)
        await self.assert_failure(responses, "asr_response_too_large")

    async def test_cloud_addresses_must_be_https_provider_hosts(self):
        urls = ["http://temporary.oss-cn-beijing.aliyuncs.com", "https://127.0.0.1/private",
                "https://aliyuncs.com.evil.example/a", "https://evil.example/a",
                "https://user:password@results.aliyuncs.com/a", "https://results.aliyuncs.com:8443/a",
                "https://results.aliyuncs.com/a#fragment"]
        for url in urls:
            for stage in ("upload", "result"):
                with self.subTest(url=url, stage=stage):
                    responses = success()
                    if stage == "upload":
                        responses[0].data["data"]["upload_host"] = url
                        responses = responses[:1]
                    else:
                        responses[3].data["output"]["results"][0]["transcription_url"] = url
                        responses = responses[:4]
                    await self.assert_failure(responses, "asr_invalid_response")

    async def test_redirects_are_errors_not_followed(self):
        await self.assert_failure([Response({}, status=302)], "asr_http_error")

    async def test_upload_policy_size_and_fields_are_validated(self):
        for changes, code in (({"max_file_size_mb": 0}, "asr_audio_too_large"),
                              ({"signature": "invalid\r\nfield"}, "asr_invalid_response"),
                              ({"oss_access_key_id": None}, "asr_invalid_response"),
                              ({"upload_dir": "unsafe\nvalue"}, "asr_invalid_response")):
            with self.subTest(changes=changes):
                policy = {**POLICY, **changes}
                await self.assert_failure([Response({"data": policy})], code)

    async def test_audio_limits_fail_before_network(self):
        for values, code in (({"max_audio_bytes": 1}, "asr_audio_too_large"),
                             ({"max_audio_seconds": 1}, "asr_audio_too_long")):
            with self.subTest(values=values), patch("atri_bot.cloud_asr.aiohttp.ClientSession") as factory:
                with self.assertRaises(ToolError) as caught:
                    await CloudASR(self.config(**values)).transcribe(self.audio, Mock())
                self.assertEqual(caught.exception.code, code)
                factory.assert_not_called()

    async def test_missing_key_fails_before_network(self):
        with patch("atri_bot.cloud_asr.aiohttp.ClientSession") as factory, self.assertRaises(ToolError) as caught:
            await CloudASR(ASRConfig()).transcribe(self.audio, Mock())
        self.assertEqual(caught.exception.code, "asr_not_configured")
        factory.assert_not_called()

    async def test_network_exception_hides_detail(self):
        await self.assert_failure([Response(aiohttp.ClientConnectionError(KEY))], "asr_network_error")

    async def test_one_deadline_covers_every_stage_and_no_resubmission(self):
        responses = success()
        for response in responses[:4]:
            response.delay = .01
        responses[-1].delay = 1
        session = await self.assert_failure(responses, "tool_timeout", config=self.config(timeout=.07))
        self.assertTrue(responses[-1].started.is_set())
        self.assertTrue(responses[-1].cancelled)
        self.assertTrue(responses[-1].closed)
        self.assertEqual(sum(url.endswith("/transcription") for _, url, _ in session.calls), 1)

    async def test_poll_sleep_uses_same_deadline(self):
        responses = success()[:3] + [Response({"output": {"task_status": "RUNNING"}})]
        with patch("atri_bot.cloud_asr.POLL_SECONDS", 10):
            await self.assert_failure(responses, "tool_timeout", config=self.config(timeout=.03))

    async def test_external_cancellation_propagates_and_closes_session(self):
        responses = success()
        responses[-1].delay = 60
        session = Session(responses)
        with patch("atri_bot.cloud_asr.aiohttp.ClientSession", return_value=session):
            task = asyncio.create_task(CloudASR(self.config()).transcribe(self.audio, Mock()))
            await asyncio.wait_for(responses[-1].started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(session.closed)
        self.assertTrue(responses[-1].cancelled)
        self.assertTrue(responses[-1].closed)

    async def test_check_active_stops_before_next_network_side_effect(self):
        class Blocked(RuntimeError):
            pass
        session = Session(success())
        def check():
            if session.calls:
                raise Blocked()
        with patch("atri_bot.cloud_asr.aiohttp.ClientSession", return_value=session), self.assertRaises(Blocked):
            await CloudASR(self.config()).transcribe(self.audio, check)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.closed)


class TranscriptTests(unittest.TestCase):
    def test_text_preserved_and_no_invented_offsets(self):
        data = {"transcripts": [{"text": "  原始文字。\n", "sentences": [
            {"text": "不同的字。", "begin_time": 0, "end_time": 100}]}]}
        result = _transcript(data, "task", 2)
        self.assertEqual(result.text, "  原始文字。\n")
        self.assertNotIn("char_start", result.segments[0])
        self.assertEqual(result.duration_ms, 2000)

    def test_empty_multichannel_or_invalid_sentence_is_failure(self):
        bad = [{}, {"transcripts": []}, {"transcripts": [{"text": " "}]},
               {"transcripts": [{"text": "one"}, {"text": "two"}]},
               {"transcripts": [{"channel_id": 1, "text": "one"}]},
               {"transcripts": [{"text": "one", "sentences": [{"text": "one", "begin_time": 5, "end_time": 2}]}]},
               {"transcripts": [{"text": "one", "sentences": [{"text": "one", "begin_time": True, "end_time": 2}]}]}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ToolError) as caught:
                _transcript(data, "task", 2)
            self.assertEqual(caught.exception.code, "asr_invalid_result")
