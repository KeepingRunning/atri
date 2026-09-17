import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from atri_bot.bilibili_audio import (
    PublicAudioResolver, _BootstrapResolver, _download, _public_ip,
    _validate_url, acquire_audio, get_audio_source,
)
from atri_bot.tools import ToolError


BVID = "BV1dVRdBpEze"
CDN = "https://example.bilivideo.com/audio.m4s?sign=private-signature"
MP4 = b"\x00\x00\x00\x18ftypM4A " + b"audio-body" * 10


def configuration(**kwargs):
    values = dict(max_audio_seconds=7200, max_audio_bytes=134217728,
                  download_timeout=60, dns_over_https=False)
    return SimpleNamespace(**(values | kwargs))


class Content:
    def __init__(self, body, *, block=None, chunks=None):
        self.body = body
        self.block = block
        self.chunks = chunks
        self.started = asyncio.Event()
        self.cancelled = False

    async def iter_chunked(self, size):
        self.started.set()
        if self.block:
            try:
                await self.block.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        chunks = self.chunks if self.chunks is not None else [self.body[i:i + size] for i in range(0, len(self.body), size)]
        for chunk in chunks:
            yield chunk


class Response:
    def __init__(self, body=b"", *, status=200, headers=None, block=None, chunks=None):
        self.status = status
        self.headers = {} if headers is None else headers
        self.content = Content(body, block=block, chunks=chunks)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


def json_response(data, **kwargs):
    return Response(json.dumps(data).encode(), **kwargs)


def view_response(*, page=1, duration=120, **kwargs):
    return json_response({"code": 0, "data": {"bvid": BVID, "pages": [
        {"page": page, "cid": 123, "duration": duration},
    ], **kwargs}})


def audio_representation(*, bandwidth=32000, url=CDN, codec="mp4a.40.2", mime="audio/mp4"):
    return {"id": 30216, "bandwidth": bandwidth, "baseUrl": url,
            "mimeType": mime, "codecs": codec}


def play_response(*, audio=None, **kwargs):
    return json_response({"code": 0, "data": {"timelength": 120000,
        "dash": {"audio": [audio_representation()] if audio is None else audio}, **kwargs}})


def audio_response(body=MP4, **kwargs):
    kwargs.setdefault("headers", {"Content-Type": "video/mp4", "Content-Length": str(len(body))})
    return Response(body, **kwargs)


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected retry or additional request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class URLTests(unittest.TestCase):
    def test_rejects_non_bilibili_and_unsafe_redirect_urls(self):
        for url in (
            "http://example.bilivideo.com/audio", "https://evil.test/audio",
            "https://bilivideo.com.evil.test/audio", "https://bilivideo.com@evil.test/audio",
            "https://example.bilivideo.com:443/audio", "https://127.0.0.1/audio",
            "https://example.bilivideo.com\\@evil.test/audio",
            "https://example.bilivideo.com/audio\n", "https://api.bilibili.com/audio",
            "https://user:password@example.bilivideo.com/audio", None, "",
        ):
            with self.subTest(url=url), self.assertRaises(ToolError):
                _validate_url(url, media=True)
        self.assertEqual(_validate_url(CDN, media=True), CDN)
        self.assertEqual(_validate_url("https://a.bilivideo.cn/audio", media=True), "https://a.bilivideo.cn/audio")

    def test_public_ip_excludes_fake_ip_mapped_addresses_and_tunnels(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "198.18.0.1", "100.64.0.1",
                        "192.0.2.1", "224.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:8.8.8.8",
                        "2002:0808:0808::1", "2001:db8::1", "fc00::1", "ff00::1", "invalid", None):
            with self.subTest(address=address):
                self.assertFalse(_public_ip(address))
        self.assertTrue(_public_ip("8.8.8.8"))
        self.assertTrue(_public_ip("2606:4700:4700::1111"))


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_doh_requests_uncompressed_json_when_auto_decompression_is_disabled(self):
        resolver = PublicAudioResolver(dns_over_https=True)
        session = Session(json_response({"Status": 0, "Answer": [{"type": 1, "data": "8.8.8.8"}]}))
        with patch("atri_bot.bilibili_audio.aiohttp.ClientSession", return_value=session) as factory, \
                patch("atri_bot.bilibili_audio.aiohttp.TCPConnector"):
            result = await resolver.resolve("api.bilibili.com", 443)
        self.assertEqual(result[0]["host"], "8.8.8.8")
        self.assertEqual(factory.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertFalse(factory.call_args.kwargs["auto_decompress"])

    async def test_system_answers_checked_and_passed_directly_to_connector(self):
        resolver = PublicAudioResolver()
        resolver._system = SimpleNamespace(resolve=AsyncMock(return_value=[
            {"host": "8.8.8.8", "hostname": "a.bilivideo.com", "port": 443}]), close=AsyncMock())
        result = await resolver.resolve("a.bilivideo.com", 443)
        self.assertEqual(result[0]["host"], "8.8.8.8")
        resolver._system.resolve.return_value.append({"host": "127.0.0.1"})
        with self.assertRaises(ToolError) as caught:
            await resolver.resolve("a.bilivideo.com", 443)
        self.assertEqual(caught.exception.code, "unsafe_audio_address")
        with self.assertRaises(ToolError):
            await resolver.resolve("evil.test", 443)
        self.assertEqual(resolver._system.resolve.await_count, 2)
        await resolver.close()
        resolver._system.close.assert_awaited_once()

    async def test_explicit_doh_does_not_fallback_on_network_error(self):
        resolver = PublicAudioResolver(dns_over_https=True)
        resolver._doh = Session(aiohttp.ClientConnectionError("sensitive-network-details"))
        with self.assertRaises(aiohttp.ClientConnectionError):
            await resolver.resolve("api.bilibili.com", 443)
        self.assertEqual(len(resolver._doh.calls), 1)
        self.assertIsNone(resolver._system)

    async def test_doh_parses_public_answers_and_rejects_fake_ip(self):
        resolver = PublicAudioResolver(dns_over_https=True)
        session = Session(json_response({"Status": 0, "Answer": [
            {"type": 5, "data": "cdn.bilivideo.com."}, {"type": 1, "data": "8.8.8.8"}]}),
            json_response({"Status": 0, "Answer": [{"type": 1, "data": "198.18.0.1"}]}))
        resolver._doh = session
        result = await resolver.resolve("a.bilivideo.com", 443)
        self.assertEqual(result[0]["host"], "8.8.8.8")
        url, args = session.calls[0]
        self.assertEqual(url, "https://dns.google/resolve")
        self.assertEqual(args["params"]["edns_client_subnet"], "0.0.0.0/0")
        self.assertFalse(args["allow_redirects"])
        with self.assertRaises(ToolError):
            await resolver.resolve("a.bilivideo.com", 443)

    async def test_bootstrap_only_connects_to_fixed_google_public_ip(self):
        resolver = _BootstrapResolver()
        result = await resolver.resolve("dns.google", 443)
        self.assertEqual(result[0]["host"], "8.8.8.8")
        self.assertEqual(result[0]["family"], socket.AF_INET)
        with self.assertRaises(ToolError):
            await resolver.resolve("evil.test", 443)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_allowed_backup_before_download_when_primary_uses_nonstandard_port(self):
        primary = audio_representation(url="https://p2p.bilivideo.cn:8082/audio.m4s")
        primary["backupUrl"] = ["https://evil.test/audio", CDN]
        session = Session(view_response(), play_response(audio=[primary]))
        url, seconds = await get_audio_source(session, BVID, 1, configuration(), lambda: None)
        self.assertEqual(url, CDN)
        self.assertEqual(seconds, 120)
        self.assertEqual(len(session.calls), 2)

    async def test_matches_part_cid_selects_smallest_supported_aac(self):
        session = Session(view_response(page=2), play_response(audio=[
            audio_representation(bandwidth=128000, url="https://big.bilivideo.com/audio"),
            audio_representation(bandwidth=1000, codec="flac"), audio_representation(),
        ]))
        url, seconds = await get_audio_source(session, BVID, 2, configuration(), lambda: None)
        self.assertEqual(url, CDN)
        self.assertEqual(seconds, 120)
        self.assertIn("cid=123", session.calls[1][0])
        self.assertIn("bvid=" + BVID, session.calls[1][0])
        self.assertTrue(all(args["allow_redirects"] is False for _, args in session.calls))

    async def test_wrong_bvid_missing_part_and_paid_video_stop_before_playurl(self):
        for response in (view_response(bvid="BV0000000000"), view_response(page=2),
                         view_response(rights={"pay": 1}), view_response(rights={"ugc_pay": 1}),
                         view_response(pages=[{"page": 1, "cid": True, "duration": 120}])):
            session = Session(response)
            with self.assertRaises(ToolError):
                await get_audio_source(session, BVID, 1, configuration(), lambda: None)
            self.assertEqual(len(session.calls), 1)

    async def test_duration_limit_applies_before_audio_request(self):
        session = Session(view_response(duration=7201))
        with self.assertRaises(ToolError) as caught:
            await get_audio_source(session, BVID, 1, configuration(), lambda: None)
        self.assertEqual(caught.exception.code, "audio_limit_exceeded")
        self.assertEqual(len(session.calls), 1)

    async def test_preview_or_shortened_audio_is_not_full_transcript(self):
        for values in ({"timelength": 30000}, {"timelength": 7201000}, {"is_preview": 1},
                       {"need_login": True}, {"need_vip": True}, {"clip_info_list": [{}]}):
            with self.subTest(values=values), self.assertRaises(ToolError):
                await get_audio_source(Session(view_response(), play_response(**values)),
                                       BVID, 1, configuration(), lambda: None)

    async def test_failures_never_become_empty_or_missing_audio(self):
        for response in (Response(status=403), Response(status=412), Response(status=500),
                         json_response({"code": -101}), json_response({"code": 0}),
                         Response(b"<html>bad gateway</html>"), Response(b"x" * 1000001)):
            with self.subTest(status=response.status), self.assertRaises(ToolError):
                await get_audio_source(Session(response), BVID, 1, configuration(), lambda: None)

    async def test_no_supported_audio_and_unsafe_media_url_rejected(self):
        for audio in ([], [audio_representation(codec="flac")],
                      [audio_representation(url="https://evil.test/audio")]):
            with self.subTest(audio=audio), self.assertRaises(ToolError):
                await get_audio_source(Session(view_response(), play_response(audio=audio)),
                                       BVID, 1, configuration(), lambda: None)


class AcquisitionTests(unittest.IsolatedAsyncioTestCase):
    def mocked(self, session):
        stack = ExitStack()
        resolver = SimpleNamespace(close=AsyncMock())
        stack.enter_context(patch("atri_bot.bilibili_audio.PublicAudioResolver", return_value=resolver))
        stack.enter_context(patch("atri_bot.bilibili_audio.aiohttp.TCPConnector"))
        factory = stack.enter_context(patch("atri_bot.bilibili_audio.aiohttp.ClientSession", return_value=session))
        return stack, resolver, factory

    async def test_success_file_is_private_and_deleted_after_caller_returns(self):
        session = Session(view_response(), play_response(), audio_response())
        stack, resolver, factory = self.mocked(session)
        with stack:
            async with acquire_audio(BVID, 1, configuration(), lambda: None) as audio:
                path = audio.path
                self.assertEqual(path.read_bytes(), MP4)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(audio.bytes, len(MP4))
                self.assertEqual(audio.duration_seconds, 120)
                self.assertTrue(session.closed)
                resolver.close.assert_awaited_once()
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
        kwargs = factory.call_args.kwargs
        self.assertFalse(kwargs["trust_env"])
        self.assertIsInstance(kwargs["cookie_jar"], aiohttp.DummyCookieJar)
        self.assertNotIn("Cookie", kwargs["headers"])
        self.assertNotIn("Authorization", kwargs["headers"])

    async def test_caller_exception_is_unchanged_and_file_removed(self):
        session = Session(view_response(), play_response(), audio_response())
        stack, _, _ = self.mocked(session)
        with stack, self.assertRaisesRegex(RuntimeError, "caller error"):
            async with acquire_audio(BVID, 1, configuration(), lambda: None) as audio:
                path = audio.path
                raise RuntimeError("caller error")
        self.assertFalse(path.exists())

    async def test_invalid_identifiers_never_start_network(self):
        for bvid, page in (("../BV1dVRdBpEze", 1), (BVID, 0), (BVID, True), (BVID, "1")):
            with self.subTest(bvid=bvid, page=page), self.assertRaises(ToolError):
                async with acquire_audio(bvid, page, configuration(), lambda: None):
                    self.fail("Must not yield")

    async def test_download_timeout_covers_metadata_and_is_not_retried(self):
        response = json_response({"code": 0}, block=asyncio.Event())
        session = Session(response)
        stack, resolver, _ = self.mocked(session)
        with stack, self.assertRaises(ToolError) as caught:
            async with acquire_audio(BVID, 1, configuration(download_timeout=.01), lambda: None):
                self.fail("Must not yield")
        self.assertEqual(caught.exception.code, "tool_timeout")
        self.assertTrue(response.content.cancelled)
        self.assertEqual(len(session.calls), 1)
        resolver.close.assert_awaited_once()

    async def test_network_failure_is_safe_and_not_retried(self):
        session = Session(view_response(), aiohttp.ClientConnectionError(CDN))
        stack, _, _ = self.mocked(session)
        with stack, self.assertRaises(ToolError) as caught:
            async with acquire_audio(BVID, 1, configuration(), lambda: None):
                self.fail("Must not yield")
        self.assertEqual(caught.exception.code, "audio_network_error")
        self.assertNotIn("private-signature", str(caught.exception))
        self.assertEqual(len(session.calls), 2)

    async def test_cancellation_removes_partially_written_file(self):
        started = asyncio.Event()
        release = asyncio.Event()
        paths = []

        async def slow_download(session, url, path, config, check):
            paths.append(path)
            path.write_bytes(MP4)
            started.set()
            await release.wait()

        session = Session(view_response(), play_response())
        stack, resolver, _ = self.mocked(session)

        async def run():
            async with acquire_audio(BVID, 1, configuration(), lambda: None):
                self.fail("Must not yield")

        with stack, patch("atri_bot.bilibili_audio._download", side_effect=slow_download):
            task = asyncio.create_task(run())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[0].parent.exists())
        resolver.close.assert_awaited_once()

    async def test_activity_guard_stops_acquisition_without_swallowing(self):
        with self.assertRaisesRegex(RuntimeError, "snapshot expired"):
            async with acquire_audio(BVID, 1, configuration(),
                                     lambda: (_ for _ in ()).throw(RuntimeError("snapshot expired"))):
                self.fail("Must not yield")


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_is_revalidated_and_only_public_media_hosts_allowed(self):
        for target in ("https://evil.test/audio", "http://a.bilivideo.com/audio", "https://127.0.0.1/audio"):
            session = Session(Response(status=302, headers={"Location": target}))
            with tempfile.TemporaryDirectory() as directory, self.subTest(target=target), self.assertRaises(ToolError):
                await _download(session, CDN, Path(directory) / "audio.m4a", configuration(), lambda: None)
            self.assertEqual(len(session.calls), 1)
        session = Session(Response(status=302, headers={"Location": "https://b.bilivideo.cn/audio"}), audio_response())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio.m4a"
            self.assertEqual(await _download(session, CDN, path, configuration(), lambda: None), len(MP4))
            self.assertEqual(path.read_bytes(), MP4)

    async def test_byte_limit_checks_declared_and_actual_stream_size(self):
        for response in (audio_response(), audio_response(headers={"Content-Type": "audio/mp4"})):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ToolError) as caught:
                await _download(Session(response), CDN, Path(directory) / "audio.m4a",
                                configuration(max_audio_bytes=16), lambda: None)
            self.assertEqual(caught.exception.code, "audio_limit_exceeded")

    async def test_empty_partial_wrong_content_type_and_html_disguised_as_audio_fail(self):
        responses = (
            audio_response(b""), audio_response(status=206),
            audio_response(headers={"Content-Type": "text/html"}),
            audio_response(b"<html>not audio</html>"),
            audio_response(headers={"Content-Type": "audio/mp4", "Content-Length": "999"}),
            audio_response(headers={"Content-Type": "audio/mp4", "Content-Encoding": "gzip"}),
        )
        for response in responses:
            with tempfile.TemporaryDirectory() as directory, self.subTest(headers=response.headers), self.assertRaises(ToolError):
                await _download(Session(response), CDN, Path(directory) / "audio.m4a", configuration(), lambda: None)

    async def test_header_can_arrive_in_multiple_chunks(self):
        response = audio_response(chunks=[MP4[:3], MP4[3:8], MP4[8:]])
        with tempfile.TemporaryDirectory() as directory:
            result = await _download(Session(response), CDN, Path(directory) / "audio.m4a", configuration(), lambda: None)
        self.assertEqual(result, len(MP4))


if __name__ == "__main__":
    unittest.main()
