import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from atri_bot.link_tools import (LinkConfig, LinkReader, _PublicResolver, register_links,
                                resolve_short_link, validate_link)
from atri_bot.tools import ToolContext, ToolError, ToolRegistry, ToolsConfig


ARTICLE = "https://mp.weixin.qq.com/s/example_article"
VIDEO = "https://www.bilibili.com/video/BV1xx411c7mD"


def mcp_text(value, **extra):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], **extra}


def metadata(**extra):
    return {"bvid": "BV1xx411c7mD", "title": "视频标题", "author": "作者", "description": "简介内容",
            "pages": [{"page": 1, "cid": 10, "title": "第一集", "duration": 20}], **extra}


def transcript(**extra):
    return {"bvid": "BV1xx411c7mD", "title": "视频标题", "transcript": "[00:00:01 --> 00:00:03] 字幕内容",
            "data_source": "subtitle", "source_url": VIDEO, "page": 1, **extra}


class LinkToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mcp = SimpleNamespace(call=AsyncMock(return_value=mcp_text("# 标题\n正文")))
        self.reader = LinkReader(LinkConfig(enabled=True), self.mcp)
        self.registry = ToolRegistry()
        register_links(self.registry, self.reader)
        self.tools = ToolsConfig()
        self.context = ToolContext("100", "2", "99", "99:100:8", 123, None, lambda: None, lambda row: None)

    async def execute(self, name, arguments, context=None):
        return await self.registry.execute(name, json.dumps(arguments), context or self.context, self.tools)

    async def test_article_native_mcp_text_and_cache(self):
        first = await self.execute("read_link", {"url": ARTICLE})
        self.assertTrue(first.ok)
        self.assertEqual(first.data["title"], "标题")
        self.assertEqual(first.data["text"], "# 标题\n正文")
        self.assertEqual(first.data["read_sections"], ["article_text"])
        self.assertFalse(first.data["has_more"])
        self.assertFalse(first.meta["visuals_read"])
        self.assertTrue(first.meta["untrusted"])
        self.mcp.call.assert_awaited_once_with("website2markdown", "convert_url", {"url": ARTICLE, "format": "markdown"})
        second = await self.execute("read_link", {"url": ARTICLE})
        self.assertEqual(second.data["document_id"], first.data["document_id"])
        self.assertTrue(second.meta["cached"])
        self.assertEqual(self.mcp.call.await_count, 1)

    async def test_article_structured_content(self):
        self.mcp.call.return_value = {"structuredContent": {"markdown": "# 结构化文章\n正文"}, "content": []}
        result = await self.execute("read_link", {"url": ARTICLE})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["text"], "# 结构化文章\n正文")

    async def test_pagination_covers_escaped_unicode_without_loss(self):
        body = '# 标题\n' + ('中文🙂\n"\\\t\x01' * 1200)
        self.mcp.call.return_value = mcp_text(body)
        self.reader.max_result_chars = self.tools.max_result_chars = 1000
        result = await self.execute("read_link", {"url": ARTICLE})
        pieces, previous_end, document_id = [], 0, result.data["document_id"]
        for _ in range(len(body)):
            self.assertTrue(result.ok)
            self.assertLessEqual(len(result.to_json()), 1000)
            self.assertEqual(result.data["range"]["start"], previous_end)
            previous_end = result.data["range"]["end"]
            pieces.append(result.data["text"])
            self.assertEqual(result.to_json(), result.bounded(1000).to_json())
            if not result.data["has_more"]:
                break
            result = await self.execute("read_document", {"document_id": document_id,
                                                          "cursor": result.data["next_cursor"]})
        self.assertEqual(''.join(pieces), body)
        self.assertEqual(previous_end, len(body))
        self.assertIsNone(result.data["next_cursor"])
        self.assertEqual(self.mcp.call.await_count, 1)

    async def test_cursor_binds_document_and_cannot_be_forged(self):
        self.mcp.call.return_value = mcp_text("x" * 9000)
        first = await self.execute("read_link", {"url": ARTICLE})
        second = await self.execute("read_link", {"url": ARTICLE + "2"})
        for cursor in (first.data["next_cursor"], "1." + "a" * 24, "-1", "0." + "a" * 24):
            result = await self.execute("read_document", {"document_id": second.data["document_id"], "cursor": cursor})
            self.assertFalse(result.ok)
            self.assertEqual(result.error["code"], "invalid_document_cursor")

    async def test_group_and_bot_scopes_and_expiration(self):
        with patch("atri_bot.link_tools.time.monotonic", return_value=100):
            first = await self.execute("read_link", {"url": ARTICLE})
        for foreign in (replace(self.context, group_id="200"), replace(self.context, self_id="22")):
            with patch("atri_bot.link_tools.time.monotonic", return_value=101):
                result = await self.execute("read_document", {"document_id": first.data["document_id"]}, foreign)
            self.assertEqual(result.error["code"], "document_not_found")
        with patch("atri_bot.link_tools.time.monotonic", return_value=100 + self.reader.config.cache_ttl_seconds):
            result = await self.execute("read_document", {"document_id": first.data["document_id"]})
        self.assertEqual(result.error["code"], "document_not_found")

    async def test_scopes_do_not_share_cached_document_ids(self):
        first = await self.execute("read_link", {"url": ARTICLE})
        other = await self.execute("read_link", {"url": ARTICLE}, replace(self.context, group_id="200"))
        self.assertNotEqual(first.data["document_id"], other.data["document_id"])
        self.assertEqual(self.mcp.call.await_count, 2)

    async def test_cache_has_group_and_global_bounds(self):
        self.reader.config.max_documents_per_group = 1
        first = await self.execute("read_link", {"url": ARTICLE})
        second = await self.execute("read_link", {"url": ARTICLE + "2"})
        result = await self.execute("read_document", {"document_id": first.data["document_id"]})
        self.assertEqual(result.error["code"], "document_not_found")
        with patch("atri_bot.link_tools.MAX_TOTAL_DOCUMENTS", 1):
            await self.execute("read_link", {"url": ARTICLE}, replace(self.context, group_id="200"))
        result = await self.execute("read_document", {"document_id": second.data["document_id"]})
        self.assertEqual(result.error["code"], "document_not_found")

    async def test_document_truncation_disclosed_separately_from_pagination(self):
        self.reader.config.max_document_chars = 1000
        self.mcp.call.return_value = mcp_text("正文" * 800)
        result = await self.execute("read_link", {"url": ARTICLE})
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["text"]), 1000)
        self.assertFalse(result.data["has_more"])
        self.assertTrue(result.meta["partial"])
        self.assertTrue(result.meta["truncated"])
        self.assertEqual(result.meta["warning"], "document_size_limit")

    async def test_empty_error_and_oversized_results_never_cached(self):
        for raw in (mcp_text(""), mcp_text("Error 500: private credentials", isError=True),
                    {"content": [{"type": "image", "data": "secret"}]},
                    mcp_text("x" * 1_000_001)):
            self.mcp.call.return_value = raw
            result = await self.execute("read_link", {"url": ARTICLE})
            self.assertFalse(result.ok)
            self.assertNotIn("private credentials", result.to_json())
            self.assertEqual(len(self.reader._documents), 0)
        self.assertEqual(self.mcp.call.await_count, 4)

    async def test_video_metadata_json_transcript_structured_and_part_preserved(self):
        self.mcp.call.side_effect = [mcp_text(metadata()),
            {"structuredContent": transcript(page=3, data_source="ai_subtitle"), "content": []}]
        result = await self.execute("read_link", {"url": VIDEO + "?p=3&share_source=qq"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["source_url"], VIDEO + "?p=3")
        self.assertEqual(result.data["read_sections"], ["metadata_description", "ai_subtitle"])
        self.assertIn("[00:00:01 --> 00:00:03]", result.data["text"])
        self.assertIn("当前分集：P3", result.data["text"])
        args = self.mcp.call.await_args_list[1].args[2]
        self.assertEqual(args["page"], 3)
        self.assertFalse(args["fallback_to_asr"])
        self.assertFalse(args["force_asr"])
        self.assertFalse(args["fallback_to_description"])
        self.assertTrue(args["include_timestamps"])

    async def test_video_text_json_transcript_supported(self):
        self.mcp.call.side_effect = [mcp_text(metadata()), mcp_text(transcript())]
        result = await self.execute("read_link", {"url": VIDEO})
        self.assertTrue(result.ok)
        self.assertFalse(result.meta["partial"])

    async def test_only_unavailable_subtitle_can_fall_back_to_metadata(self):
        for code in ("SUBTITLE_UNAVAILABLE", "COOKIE_EXPIRED", "NETWORK_ERROR", "NETWORK_TIMEOUT", "ACCESS_DENIED"):
            self.mcp.call.side_effect = [mcp_text(metadata()),
                mcp_text({"error": True, "code": code, "message": "secret upstream message"}, isError=True)]
            result = await self.execute("read_link", {"url": VIDEO})
            if code == "SUBTITLE_UNAVAILABLE":
                self.assertTrue(result.ok)
                self.assertTrue(result.meta["partial"])
                self.assertEqual(result.meta["warning"], "subtitle_unavailable")
                self.assertEqual(result.data["read_sections"], ["metadata_description"])
                self.reader._documents.clear()
            else:
                self.assertFalse(result.ok)
                self.assertEqual(result.error["code"], "tool_timeout" if code == "NETWORK_TIMEOUT" else code.lower())
                self.assertEqual(len(self.reader._documents), 0)
            self.assertNotIn("secret upstream message", result.to_json())

    async def test_video_mismatched_or_empty_subtitles_are_not_metadata_success(self):
        for value in (transcript(page=2), transcript(page=True), transcript(transcript=""), transcript(data_source="description"),
                      transcript(bvid="BV1xx411c7mE"), {"wrong": True}):
            self.mcp.call.side_effect = [mcp_text(metadata()), mcp_text(value)]
            result = await self.execute("read_link", {"url": VIDEO})
            self.assertEqual(result.error["code"], "invalid_link_result")
            self.assertFalse(self.reader._documents)

    async def test_page_source_reports_only_text_actually_delivered(self):
        self.mcp.call.side_effect = [mcp_text(metadata(description="很长的简介" * 5000)), mcp_text(transcript())]
        result = await self.execute("read_link", {"url": VIDEO})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["read_sections"], ["metadata_description"])
        self.assertEqual(result.meta["data_source"], "metadata_description")
        self.assertTrue(result.data["has_more"])
        body = result.data["text"]
        while result.data["has_more"]:
            result = await self.execute("read_document", {"document_id": result.data["document_id"],
                                                           "cursor": result.data["next_cursor"]})
            body += result.data["text"]
        self.assertIn("很长的简介" * 5000, body)
        self.assertEqual(result.meta["data_source"], "subtitle")

    async def test_unsupported_urls_never_reach_backend(self):
        for url in ("http://mp.weixin.qq.com/s/abc", "https://mp.weixin.qq.com.evil.test/s/abc",
                    "https://mp.weixin.qq.com@127.0.0.1/s/abc", "https://user@mp.weixin.qq.com/s/abc",
                    "https://mp.weixin.qq.com:443/s/abc", "https://127.0.0.1/s/abc",
                    "https://www.zhihu.com/question/123", "https://www.bilibili.com/account",
                    VIDEO + "?p=0", VIDEO + "?p=1&p=2", "https://b23.tv.evil.test/abc",
                    "https://mp.weixin.qq.com\\@evil.test/s/abc", "https://mp.weixin.qq.com./s/abc"):
            result = await self.execute("read_link", {"url": url})
            self.assertFalse(result.ok, url)
        self.mcp.call.assert_not_awaited()

    async def test_short_link_resolves_then_only_canonical_video_goes_to_mcp(self):
        for enabled in (False, True):
            self.reader.config.dns_over_https = enabled
            self.reader._documents.clear()
            self.mcp.call.reset_mock()
            self.mcp.call.side_effect = [mcp_text(metadata()), mcp_text(transcript(page=2))]
            with patch("atri_bot.link_tools.resolve_short_link", AsyncMock(return_value=VIDEO + "?p=2")) as resolver:
                result = await self.execute("read_link", {"url": "https://b23.tv/abc"})
            self.assertTrue(result.ok)
            resolver.assert_awaited_once_with("https://b23.tv/abc", dns_over_https=enabled)
            self.assertEqual(self.mcp.call.await_args_list[0].args[2]["bvid_or_url"], VIDEO + "?p=2")

    async def test_cancelled_request_does_not_populate_cache(self):
        active = True
        def check():
            if not active:
                raise asyncio.CancelledError()
        async def invalidate(*args):
            nonlocal active
            active = False
            return mcp_text("正文")
        self.mcp.call.side_effect = invalidate
        context = replace(self.context, check_active=check)
        with self.assertRaises(asyncio.CancelledError):
            await self.reader.read_link(context, {"url": ARTICLE})
        self.assertFalse(self.reader._documents)

    async def test_timeout_does_not_populate_cache(self):
        self.reader.config.timeout = .01
        async def delayed(*args):
            await asyncio.sleep(1)
        self.mcp.call.side_effect = delayed
        result = await self.execute("read_link", {"url": ARTICLE})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "tool_timeout")
        self.assertIsNone(result.data)
        self.assertFalse(result.meta["retryable"])
        self.assertNotIn("稍后", result.error["message"])
        self.assertEqual(self.mcp.call.await_count, 1)
        self.assertFalse(self.reader._documents)

    async def test_mcp_timeouts_fail_without_returning_metadata_or_retrying(self):
        for during_metadata in (True, False):
            self.mcp.call.reset_mock()
            timeout = ToolError("mcp_timeout", "upstream timeout detail")
            self.mcp.call.side_effect = [timeout] if during_metadata else [mcp_text(metadata()), timeout]
            result = await self.execute("read_link", {"url": VIDEO})
            self.assertFalse(result.ok)
            self.assertIsNone(result.data)
            self.assertEqual(result.error["code"], "tool_timeout")
            self.assertFalse(result.meta["retryable"])
            self.assertEqual(self.mcp.call.await_count, 1 if during_metadata else 2)
            self.assertFalse(self.reader._documents)

    async def test_short_link_timeout_never_starts_platform_reader(self):
        with patch("atri_bot.link_tools.resolve_short_link", AsyncMock(side_effect=TimeoutError())):
            result = await self.execute("read_link", {"url": "https://b23.tv/abc"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error["code"], "tool_timeout")
        self.assertFalse(result.meta["retryable"])
        self.mcp.call.assert_not_awaited()
        self.assertFalse(self.reader._documents)

    async def test_short_link_dns_failure_never_starts_platform_reader(self):
        self.reader.config.dns_over_https = True
        for code in ("unsafe_redirect", "link_network_error"):
            with self.subTest(code=code), patch("atri_bot.link_tools.resolve_short_link",
                    AsyncMock(side_effect=ToolError(code, "短链接域名解析失败。"))) as resolver:
                result = await self.execute("read_link", {"url": "https://b23.tv/abc"})
            self.assertFalse(result.ok)
            self.assertEqual(result.error["code"], code)
            resolver.assert_awaited_once_with("https://b23.tv/abc", dns_over_https=True)
            self.mcp.call.assert_not_awaited()
            self.assertFalse(self.reader._documents)

    async def test_disabled_tools_are_not_registered(self):
        registry = ToolRegistry()
        register_links(registry, LinkReader(LinkConfig(), self.mcp))
        self.assertEqual(registry.definitions(), [])

    def test_config_and_supported_article_routes(self):
        for config in (LinkConfig(timeout=float("nan")), LinkConfig(enabled=1),
                       LinkConfig(dns_over_https=1), LinkConfig(dns_over_https="true"),
                       LinkConfig(max_document_chars=True), LinkConfig(cache_ttl_seconds=0)):
            with self.assertRaises(ValueError):
                config.validate()
        for url in ("https://zhuanlan.zhihu.com/p/123?utm=abc",
                    "https://www.zhihu.com/question/123/answer/456", "https://www.zhihu.com/answer/456",
                    "https://mp.weixin.qq.com/s?__biz=MzA%3D&mid=123&idx=1&sn=abc"):
            self.assertEqual(validate_link(url)[0], "article")


class ShortLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_resolver_blocks_private_answers(self):
        resolver = _PublicResolver()
        try:
            for address in ("0.0.0.0", "127.0.0.1", "198.18.0.141", "::ffff:127.0.0.1",
                            "::ffff:8.8.8.8", "invalid"):
                with self.subTest(address=address), patch.object(resolver._system, "resolve",
                        AsyncMock(return_value=[{"host": address}])):
                    with self.assertRaises(ToolError) as caught:
                        await resolver.resolve("b23.tv", 443)
                    self.assertEqual(caught.exception.code, "unsafe_redirect")
            with patch.object(resolver._system, "resolve", AsyncMock(return_value=[{"host": "93.184.216.34"}])):
                self.assertEqual((await resolver.resolve("b23.tv", 443))[0]["host"], "93.184.216.34")
        finally:
            await resolver.close()

    async def test_doh_pins_public_answers_without_system_dns(self):
        with patch("atri_bot.link_tools.aiohttp.resolver.DefaultResolver") as system_factory:
            resolver = _PublicResolver(dns_over_https=True)
            try:
                records = [{"hostname": "b23.tv", "host": "93.184.216.34", "port": 443}]
                with patch.object(resolver, "_doh_records", AsyncMock(return_value=records)) as doh:
                    self.assertEqual(await resolver.resolve("b23.tv", 443), records)
                doh.assert_awaited_once_with("b23.tv", 443, 0)
                system_factory.assert_not_called()
            finally:
                await resolver.close()

    async def test_doh_still_blocks_nonpublic_and_mixed_answers(self):
        resolver = _PublicResolver(dns_over_https=True)
        try:
            for addresses in (("0.0.0.0",), ("198.18.0.141",), ("127.0.0.1",),
                              ("93.184.216.34", "10.0.0.1"), ("2002:0808:0808::1",),
                              ("::ffff:8.8.8.8",)):
                with self.subTest(addresses=addresses), patch.object(resolver, "_doh_records",
                        AsyncMock(return_value=[{"host": item} for item in addresses])):
                    with self.assertRaises(ToolError) as caught:
                        await resolver.resolve("b23.tv", 443)
                    self.assertEqual(caught.exception.code, "unsafe_redirect")
        finally:
            await resolver.close()

    async def test_doh_failure_does_not_fallback_or_expose_error_details(self):
        with patch("atri_bot.link_tools.aiohttp.resolver.DefaultResolver") as system_factory:
            resolver = _PublicResolver(dns_over_https=True)
            try:
                for failure in (ToolError("audio_dns_error", "secret details"),
                                aiohttp.ClientConnectionError("secret details"),
                                TimeoutError("secret details")):
                    with patch.object(resolver, "_doh_records", AsyncMock(side_effect=failure)) as doh, \
                            self.assertLogs("atri.links", level="WARNING") as logs:
                        with self.assertRaises(type(failure)) as caught:
                            await resolver.resolve("b23.tv", 443)
                    if isinstance(failure, ToolError):
                        self.assertEqual(caught.exception.code, "link_network_error")
                        self.assertNotIn("secret details", str(caught.exception))
                    self.assertNotIn("secret details", "\n".join(logs.output))
                    self.assertIn("DNS=DoH", "\n".join(logs.output))
                    doh.assert_awaited_once()
                    system_factory.assert_not_called()
            finally:
                await resolver.close()

    async def test_short_link_resolver_refuses_other_audio_and_private_hosts(self):
        resolver = _PublicResolver(dns_over_https=True)
        try:
            with patch.object(resolver, "_doh_records", AsyncMock()) as doh:
                for host in ("api.bilibili.com", "cdn.bilivideo.com", "127.0.0.1", "evil.test"):
                    with self.subTest(host=host), self.assertRaises(ToolError) as caught:
                        await resolver.resolve(host, 443)
                    self.assertEqual(caught.exception.code, "unsafe_redirect")
                doh.assert_not_awaited()
        finally:
            await resolver.close()

    async def test_direct_short_link_entry_rejects_other_urls_without_request(self):
        with patch("atri_bot.link_tools.aiohttp.ClientSession") as session:
            for url in (VIDEO, ARTICLE, "https://127.0.0.1/private"):
                with self.subTest(url=url), self.assertRaises(ToolError):
                    await resolve_short_link(url, dns_over_https=True)
            session.assert_not_called()

    async def test_redirect_validation_cookies_and_part(self):
        class Response:
            status = 302
            def __init__(self, target):
                self.headers = {"Location": target}
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        class Session:
            def __init__(self, target, connector):
                self.target, self.connector, self.requests = target, connector, []
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                await self.connector.close()
            def get(self, url, **kwargs):
                self.requests.append((url, kwargs))
                return Response(self.target)
        for target, succeeds, doh_enabled in ((VIDEO, True, False), (VIDEO, True, True),
                (VIDEO + "?p=2", True, True), ("https://127.0.0.1/private", False, True),
                ("https://b23.tv@evil.test/abc", False, True), (ARTICLE, False, True)):
            sessions = []
            def make_session(**kwargs):
                self.assertFalse(kwargs["trust_env"])
                self.assertEqual(type(kwargs["cookie_jar"]).__name__, "DummyCookieJar")
                self.assertEqual(kwargs["connector"]._resolver.dns_over_https, doh_enabled)
                session = Session(target, kwargs["connector"])
                sessions.append(session)
                return session
            with patch("atri_bot.link_tools.aiohttp.ClientSession", side_effect=make_session), \
                    self.assertLogs("atri.links", level="DEBUG") as logs:
                if succeeds:
                    result = await resolve_short_link("https://b23.tv/abc?p=3&token=private-query",
                                                      dns_over_https=doh_enabled)
                    self.assertEqual(result, target if "?p=" in target else VIDEO + "?p=3")
                else:
                    with self.assertRaises(ToolError):
                        await resolve_short_link("https://b23.tv/abc?token=private-query",
                                                 dns_over_https=doh_enabled)
            self.assertNotIn("private-query", "\n".join(logs.output))
            self.assertIn("DNS=DoH" if doh_enabled else "DNS=system", "\n".join(logs.output))
            self.assertEqual(len(sessions[0].requests), 1)
            self.assertEqual(sessions[0].requests[0][1], {"allow_redirects": False})
