"""Bounded public Bilibili AAC acquisition; no credentials or local ASR runtime.

Playback fields follow XZXZZX-Ai/bilibili-mcp's playback/video-api adapters.
Optional DoH follows https://developers.google.com/speed/public-dns/docs/doh/json.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import ipaddress
import json
import logging
import math
from pathlib import Path
import re
import socket
import tempfile
import time
from urllib.parse import urlencode, urljoin, urlsplit

import aiohttp

from .cloud_asr import ASRConfig
from .tools import ToolError

log = logging.getLogger("atri.asr")
API_HOST = "api.bilibili.com"
MEDIA_SUFFIXES = ("bilivideo.com", "bilivideo.cn")
DOH_HOST = "dns.google"
DOH_URL = "https://dns.google/resolve"
JSON_BYTES = 1_000_000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ATRI/1.0)",
    "Referer": "https://www.bilibili.com/",
    "Accept-Encoding": "identity",
}


@dataclass(frozen=True)
class AudioFile:
    path: Path
    duration_seconds: float
    bytes: int


def _safe_host(host):
    return host == API_HOST or any(host == suffix or host.endswith("." + suffix)
                                  for suffix in MEDIA_SUFFIXES)


def _validate_url(value, *, media=False):
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 8192
                or re.search(r"[\s\\\x00-\x1f\x7f]", value)):
            raise ValueError()
        parsed = urlsplit(value)
        host = parsed.hostname
        valid_host = (host != API_HOST and _safe_host(host or "")) if media else host == API_HOST
        if (parsed.scheme != "https" or not valid_host or parsed.username is not None
                or parsed.password is not None or parsed.port is not None
                or parsed.fragment or parsed.netloc.lower() != host):
            raise ValueError()
    except ValueError:
        raise ToolError("unsafe_audio_url", "音轨地址或跳转目标不受支持，已停止读取。") from None
    return value


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if address.version == 6:
        # Exclude transition/tunnel forms that can encode non-public destinations.
        return (address in ipaddress.ip_network("2000::/3") and not address.ipv4_mapped
                and address.sixtofour is None and address.teredo is None)
    return True


async def _json_body(response, *, limit=JSON_BYTES):
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > limit:
            raise ToolError("audio_response_too_large", "音轨接口返回内容超过读取上限。")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ToolError("invalid_audio_result", "音轨接口返回内容无效。") from None


class _BootstrapResolver(aiohttp.abc.AbstractResolver):
    """Only the fixed Google DoH service, retaining TLS hostname verification."""
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        if host != DOH_HOST:
            raise ToolError("unsafe_audio_url", "拒绝非预期的 DNS 服务地址。")
        return [{"hostname": host, "host": "8.8.8.8", "port": port,
                 "family": socket.AF_INET, "proto": 0, "flags": socket.AI_NUMERICHOST}]

    async def close(self):
        pass


class PublicAudioResolver(aiohttp.abc.AbstractResolver):
    """Validate and pin each connection to public answers, including redirects."""
    def __init__(self, *, dns_over_https=False):
        self.dns_over_https = dns_over_https
        self._system = None if dns_over_https else aiohttp.resolver.DefaultResolver()
        self._doh = None

    def _allows_host(self, host):
        return _safe_host(host)

    async def _doh_records(self, host, port, family):
        if self._doh is None:
            self._doh = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=_BootstrapResolver()),
                timeout=aiohttp.ClientTimeout(total=10),
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                auto_decompress=False, headers={"Accept-Encoding": "identity"},
            )
        # IPv4 suffices for public Bilibili CDN access; no automatic resolver fallback.
        async with self._doh.get(DOH_URL, params={"name": host, "type": "A",
                                 "edns_client_subnet": "0.0.0.0/0"},
                                 allow_redirects=False) as response:
            if response.status != 200:
                raise ToolError("audio_dns_error", "公共 DNS 查询失败。")
            data = await _json_body(response, limit=65536)
        if not isinstance(data, dict) or data.get("Status") != 0 or data.get("TC"):
            raise ToolError("audio_dns_error", "公共 DNS 未返回完整有效解析。")
        answers = data.get("Answer")
        if not isinstance(answers, list) or len(answers) > 256:
            raise ToolError("audio_dns_error", "公共 DNS 未返回有效解析。")
        return [{"hostname": host, "host": answer.get("data"), "port": port,
                 "family": socket.AF_INET, "proto": 0, "flags": socket.AI_NUMERICHOST}
                for answer in answers if isinstance(answer, dict) and answer.get("type") == 1]

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        if not self._allows_host(host):
            raise ToolError("unsafe_audio_url", "拒绝连接非 B站音轨服务。")
        records = (await self._doh_records(host, port, family) if self.dns_over_https
                   else await self._system.resolve(host, port, family))
        if not records:
            raise ToolError("audio_dns_error", "音轨域名解析失败。")
        if any(not _public_ip(record.get("host", "")) for record in records):
            raise ToolError("unsafe_audio_address", "音轨域名解析到了非公网地址，已停止读取。")
        return records

    async def close(self):
        if self._system is not None:
            await self._system.close()
        if self._doh is not None:
            await self._doh.close()


async def _api(session, path, params, check_active):
    check_active()
    url = _validate_url("https://" + API_HOST + path + "?" + urlencode(params))
    async with session.get(url, allow_redirects=False) as response:
        log.debug("[音轨接口] 阶段=%s 状态码=%d", path.rsplit("/", 1)[-1], response.status)
        if response.status in (401, 403, 412):
            raise ToolError("audio_access_denied", "B站拒绝公开音轨访问，不能继续转写。")
        if response.status != 200:
            raise ToolError("audio_network_error", "B站音轨接口请求失败。")
        data = await _json_body(response)
    check_active()
    if not isinstance(data, dict) or type(data.get("code")) is not int:
        raise ToolError("invalid_audio_result", "B站音轨接口返回无效状态。")
    if data["code"] != 0:
        raise ToolError("audio_access_denied", "B站未提供可公开读取的音轨，不能继续转写。")
    if not isinstance(data.get("data"), dict):
        raise ToolError("invalid_audio_result", "B站音轨接口缺少有效数据。")
    return data["data"]


def _duration(value, maximum):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ToolError("invalid_audio_result", "视频缺少可信的音频时长。")
    if value > maximum:
        raise ToolError("audio_limit_exceeded", "视频分 P 时长超过音频转写上限。")
    return float(value)


async def get_audio_source(session, bvid, page, config, check_active):
    """Resolve a public part and select one lowest-bandwidth MP4/AAC source."""
    info = await _api(session, "/x/web-interface/view", {"bvid": bvid}, check_active)
    if info.get("bvid") != bvid:
        raise ToolError("invalid_audio_result", "B站返回的视频编号与请求不一致。")
    rights = info.get("rights", {})
    if not isinstance(rights, dict) or any(rights.get(key) for key in ("pay", "ugc_pay")):
        raise ToolError("audio_access_denied", "此视频需要付费或受限访问，不能读取公开音轨。")
    pages = info.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 1000:
        raise ToolError("invalid_audio_result", "视频缺少有效的分 P 信息。")
    selected = [row for row in pages if isinstance(row, dict) and type(row.get("page")) is int
                and row["page"] == page]
    if len(selected) != 1:
        raise ToolError("invalid_video_part", "视频中不存在唯一对应的分 P。")
    selected = selected[0]
    cid = selected.get("cid")
    if type(cid) is not int or not 0 < cid <= 2**53 - 1:
        raise ToolError("invalid_audio_result", "视频分 P 缺少有效 CID。")
    expected_duration = _duration(selected.get("duration"), config.max_audio_seconds)
    playback = await _api(session, "/x/player/playurl",
                          {"bvid": bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1},
                          check_active)
    if any(playback.get(key) for key in ("need_vip", "need_login", "is_preview", "clip_info_list")):
        raise ToolError("audio_access_denied", "此视频仅提供受限或试听音轨，不能按全文转写。")
    milliseconds = playback.get("timelength")
    duration = _duration(milliseconds, config.max_audio_seconds * 1000) / 1000
    if abs(duration - expected_duration) > 5:
        raise ToolError("incomplete_audio", "公开音轨时长与视频分 P 不符，不能按全文转写。")
    dash = playback.get("dash")
    representations = dash.get("audio") if isinstance(dash, dict) else None
    if not isinstance(representations, list) or len(representations) > 256:
        raise ToolError("invalid_audio_result", "视频未返回有效的 DASH 音轨列表。")
    candidates = []
    for item in representations:
        if not isinstance(item, dict):
            continue
        bandwidth = item.get("bandwidth")
        mime = item.get("mimeType", item.get("mime_type"))
        codec = item.get("codecs")
        if (type(bandwidth) in (int, float) and math.isfinite(bandwidth) and bandwidth > 0
                and mime in ("audio/mp4", "audio/m4a")
                and isinstance(codec, str) and codec.lower().startswith("mp4a")):
            candidates.append(item)
    if not candidates:
        raise ToolError("audio_unavailable", "视频没有可读取的 AAC/MP4 音轨。")
    chosen = min(candidates, key=lambda item: item["bandwidth"])
    backups = chosen.get("backupUrl", chosen.get("backup_url", []))
    if not isinstance(backups, list):
        backups = []
    # Upstream often prefers a P2P CDN on port 8082. Select one allowed standard
    # HTTPS URL before the first download, without retrying failed connections.
    for location in [chosen.get("baseUrl", chosen.get("base_url")), *backups[:8]]:
        try:
            return _validate_url(location, media=True), duration
        except ToolError:
            continue
    raise ToolError("audio_unavailable", "视频未提供符合连接限制的 AAC/MP4 音轨地址。")


async def _download(session, url, path, config, check_active):
    for redirects in range(4):
        check_active()
        _validate_url(url, media=True)
        async with session.get(url, allow_redirects=False) as response:
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if redirects == 3 or not location:
                    raise ToolError("audio_redirect_failed", "音轨跳转次数过多或目标无效。")
                url = _validate_url(urljoin(url, location), media=True)
                continue
            if response.status in (401, 403, 412):
                raise ToolError("audio_access_denied", "B站拒绝公开音轨下载。")
            if response.status != 200:
                raise ToolError("audio_network_error", "音轨下载失败，未取得完整音频。")
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in ("audio/mp4", "audio/m4a", "audio/x-m4a", "video/mp4", "application/octet-stream"):
                raise ToolError("invalid_audio_result", "下载内容不是可用的 MP4 音轨。")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ToolError("invalid_audio_result", "音轨返回了不受支持的传输编码。")
            raw_length = response.headers.get("Content-Length")
            expected = None
            if raw_length is not None:
                if not re.fullmatch(r"[0-9]{1,16}", raw_length):
                    raise ToolError("invalid_audio_result", "音轨长度无效。")
                expected = int(raw_length)
                if expected > config.max_audio_bytes:
                    raise ToolError("audio_limit_exceeded", "音轨文件超过大小上限。")
            received = 0
            prefix = bytearray()
            # Small bounded writes preserve cancellation cleanup without background write races.
            with path.open("xb") as output:
                path.chmod(0o600)
                async for chunk in response.content.iter_chunked(65536):
                    check_active()
                    received += len(chunk)
                    if received > config.max_audio_bytes:
                        raise ToolError("audio_limit_exceeded", "音轨文件超过大小上限。")
                    if len(prefix) < 12:
                        prefix.extend(chunk[:12 - len(prefix)])
                    output.write(chunk)
            if received == 0 or expected is not None and expected != received:
                raise ToolError("incomplete_audio", "音轨为空或下载不完整。")
            if len(prefix) < 12 or prefix[4:8] != b"ftyp":
                raise ToolError("invalid_audio_result", "下载内容缺少有效的 MP4 文件头。")
            check_active()
            return received
    raise ToolError("audio_redirect_failed", "音轨跳转失败。")


@asynccontextmanager
async def acquire_audio(bvid: str, page: int, config: ASRConfig, check_active):
    """Yield a temporary audio file, deleting it after success, failure or cancellation.

The download deadline covers metadata, playback resolution and streaming only.
The caller owns the independent cloud upload/transcription deadline after yield.
"""
    if not isinstance(bvid, str) or not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
        raise ToolError("invalid_video_id", "B站视频编号无效。")
    if type(page) is not int or not 1 <= page <= 9999:
        raise ToolError("invalid_video_part", "B站分 P 编号无效。")
    check_active()
    started = time.monotonic()
    log.info("[音轨获取] 视频=%s 分P=%d 公共DNS=%s", bvid, page, config.dns_over_https)
    with tempfile.TemporaryDirectory(prefix="atri-bilibili-audio-") as directory:
        path = Path(directory) / "audio.m4a"
        resolver = PublicAudioResolver(dns_over_https=config.dns_over_https)
        try:
            try:
                async with asyncio.timeout(config.download_timeout):
                    async with aiohttp.ClientSession(
                        connector=aiohttp.TCPConnector(resolver=resolver),
                        timeout=aiohttp.ClientTimeout(total=config.download_timeout),
                        headers=HEADERS, cookie_jar=aiohttp.DummyCookieJar(),
                        trust_env=False, auto_decompress=False,
                    ) as session:
                        url, duration = await get_audio_source(session, bvid, page, config, check_active)
                        size = await _download(session, url, path, config, check_active)
            except TimeoutError:
                raise ToolError("tool_timeout", "B站音轨获取超时，本次失败且不重试。") from None
            except aiohttp.ClientError:
                raise ToolError("audio_network_error", "B站音轨网络请求失败，本次不重试。") from None
            except OSError:
                raise ToolError("audio_io_error", "无法安全保存临时音轨。") from None
        finally:
            await resolver.close()
        check_active()
        log.info("[音轨就绪] 视频=%s 分P=%d 时长=%.1fs 字节数=%d 耗时=%.2fs",
                 bvid, page, duration, size, time.monotonic() - started)
        yield AudioFile(path=path, duration_seconds=duration, bytes=size)
