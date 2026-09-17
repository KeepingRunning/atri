"""Bounded cloud transcription using Bailian's asynchronous file API.

Audio is temporary local data; only transcripts are handed to document storage.
The API key is sent only to the configured API, never to OSS or result URLs.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import math
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

import aiohttp

from .tools import ToolError

log = logging.getLogger("atri.asr")
POLL_SECONDS = 2
_API_BYTES = 256 * 1024
_RESULT_BYTES = 32 * 1024 * 1024
_MAX_TEXT_CHARS = 2_000_000
_BLOCK_BYTES = 128 * 1024


@dataclass
class ASRConfig:
    enabled: bool = False
    base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    model: str = "fun-asr"
    api_key: str = field(default="", repr=False)
    timeout: float = 300
    max_audio_bytes: int = 134217728
    max_audio_seconds: float = 7200
    download_timeout: float = 60
    dns_over_https: bool = False

    def validate(self):
        for name in ("enabled", "dns_over_https"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"asr.{name} must be a boolean")
        if not isinstance(self.api_key, str) or any(c.isspace() for c in self.api_key):
            raise ValueError("asr.api_key must be a string without whitespace")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("asr.model must be a nonempty string")
        if not isinstance(self.base_url, str):
            raise ValueError("asr.base_url must be a URL string")
        try:
            url = urlsplit(self.base_url)
            valid = (url.scheme in ("https", "http") and url.hostname and not url.username
                     and not url.password and not url.query and not url.fragment
                     and url.path.rstrip("/") == "/api/v1"
                     and not any(c.isspace() for c in self.base_url))
            if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                valid = False
            url.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("asr.base_url must use HTTPS and end with /api/v1 (not /compatible-mode/v1)")
        for name, high in (("timeout", 1800), ("max_audio_seconds", 43200), ("download_timeout", 300)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= high:
                raise ValueError(f"asr.{name} must be in (0, {high}]")
        if type(self.max_audio_bytes) is not int or not 1 <= self.max_audio_bytes <= 1073741824:
            raise ValueError("asr.max_audio_bytes must be an integer in 1..1073741824")


@dataclass(frozen=True)
class Transcript:
    text: str
    segments: tuple[dict, ...]
    duration_ms: int | None
    task_id: str


def _cloud_url(value):
    """Only provider-hosted HTTPS URLs, including signed result URLs."""
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        valid = (parsed and parsed.scheme == "https" and parsed.hostname
                 and parsed.hostname.endswith(".aliyuncs.com")
                 and not parsed.username and not parsed.password and not parsed.fragment
                 and parsed.port in (None, 443) and len(value) <= 16384
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except ValueError:
        valid = False
    if not valid:
        raise ToolError("asr_invalid_response", "百炼返回的云端文件地址无效。")
    return value


async def _body(response, limit, check_active):
    if response.content_length is not None and response.content_length > limit:
        raise ToolError("asr_response_too_large", "百炼响应超过大小限制。")
    data = bytearray()
    async for block in response.content.iter_chunked(_BLOCK_BYTES):
        check_active()
        if len(data) + len(block) > limit:
            raise ToolError("asr_response_too_large", "百炼响应超过大小限制。")
        data.extend(block)
    check_active()
    return data


async def _request_json(session, method, url, *, stage, check_active, limit=_API_BYTES, **kwargs):
    check_active()
    async with session.request(method, url, allow_redirects=False, **kwargs) as response:
        check_active()
        log.debug("[HTTP响应] 阶段=%s 状态码=%d", stage, response.status)
        if not 200 <= response.status < 300:
            # Provider messages/URLs may contain secrets; never relay them.
            raise ToolError("asr_http_error", f"百炼{stage}失败（HTTP {response.status}），本次不重试。")
        body = await _body(response, limit, check_active)
        try:
            data = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise ToolError("asr_invalid_response", f"百炼{stage}返回了无效 JSON。") from None
        if not isinstance(data, dict):
            raise ToolError("asr_invalid_response", f"百炼{stage}未返回 JSON 对象。")
        return data


def _multipart(policy, path, size):
    if not isinstance(policy, dict):
        raise ToolError("asr_invalid_response", "百炼未返回上传凭证。")
    host = _cloud_url(policy.get("upload_host"))
    limit = policy.get("max_file_size_mb")
    if type(limit) in (int, float) and (not math.isfinite(limit) or size > limit * 1024 * 1024):
        raise ToolError("asr_audio_too_large", "音频超过百炼上传大小限制。")
    directory = policy.get("upload_dir")
    if not isinstance(directory, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,1024}", directory):
        raise ToolError("asr_invalid_response", "百炼上传目录无效。")
    suffix = path.suffix.lower()
    if suffix not in (".m4a", ".aac", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".mp4"):
        raise ToolError("asr_invalid_audio", "不支持该音频文件格式。")
    filename = secrets.token_hex(16) + suffix
    key = directory.rstrip("/") + "/" + filename
    fields = {"OSSAccessKeyId": policy.get("oss_access_key_id"), "policy": policy.get("policy"),
              "Signature": policy.get("signature"), "key": key,
              "x-oss-object-acl": policy.get("x_oss_object_acl"),
              "x-oss-forbid-overwrite": policy.get("x_oss_forbid_overwrite"),
              "success_action_status": "200"}
    if any(not isinstance(value, str) or not value or len(value) > 65536
           or "\r" in value or "\n" in value for value in fields.values()):
        raise ToolError("asr_invalid_response", "百炼上传凭证格式无效。")
    boundary = secrets.token_hex(24)
    prefix = b"".join((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                       f'{value}\r\n').encode("utf-8") for name, value in fields.items())
    prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"'
               '\r\nContent-Type: application/octet-stream\r\n\r\n').encode("ascii")
    trailer = f"\r\n--{boundary}--\r\n".encode("ascii")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
               "Content-Length": str(len(prefix) + size + len(trailer))}
    return host, key, prefix, trailer, headers


async def _audio_stream(path, size, prefix, trailer, check_active):
    """A deterministic multipart stream with an exact length and bounded memory."""
    check_active()
    yield prefix
    # Reading 128 KiB local blocks keeps memory bounded and avoids a second full
    # temporary audio copy. The downloader exclusively owns this temporary file.
    with path.open("rb") as source:
        remaining = size
        while remaining:
            check_active()
            block = source.read(min(_BLOCK_BYTES, remaining))
            if not block:
                raise ToolError("asr_invalid_audio", "临时音频文件大小发生变化。")
            remaining -= len(block)
            yield block
        if source.read(1):
            raise ToolError("asr_invalid_audio", "临时音频文件大小发生变化。")
    check_active()
    yield trailer


def _milliseconds(value):
    return type(value) is int and 0 <= value <= 43200 * 1000


def _transcript(data, task_id, audio_duration):
    channels = data.get("transcripts")
    if (not isinstance(channels, list) or len(channels) != 1 or not isinstance(channels[0], dict)
            or channels[0].get("channel_id", 0) != 0):
        raise ToolError("asr_invalid_result", "百炼未返回单声道转写结果。")
    channel = channels[0]
    text = channel.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS:
        raise ToolError("asr_invalid_result", "百炼转写正文为空或超过大小限制。")
    sentences = channel.get("sentences", [])
    if not isinstance(sentences, list) or len(sentences) > 100000:
        raise ToolError("asr_invalid_result", "百炼转写句段格式无效。")
    segments, cursor = [], 0
    for sentence in sentences:
        if not isinstance(sentence, dict):
            raise ToolError("asr_invalid_result", "百炼转写句段格式无效。")
        content, begin, end = sentence.get("text"), sentence.get("begin_time"), sentence.get("end_time")
        if (not isinstance(content, str) or not content.strip() or not _milliseconds(begin)
                or not _milliseconds(end) or end < begin):
            raise ToolError("asr_invalid_result", "百炼转写句段缺少有效文字或时间戳。")
        segment = {"text": content, "begin_time": begin, "end_time": end}
        start = text.find(content, cursor)
        if start >= 0:
            cursor = start + len(content)
            segment.update(char_start=start, char_end=cursor)
        # Unmatched sentences retain provider text/timestamps, without invented
        # offsets. Document chunking separately verifies alignment before use.
        segments.append(segment)
    properties = data.get("properties", {})
    duration = properties.get("original_duration_in_milliseconds") if isinstance(properties, dict) else None
    if not _milliseconds(duration):
        duration = round(audio_duration * 1000)
    return Transcript(text, tuple(segments), duration, task_id)


class CloudASR:
    def __init__(self, config: ASRConfig):
        config.validate()
        self.config = config

    async def transcribe(self, audio, check_active) -> Transcript:
        config = self.config
        if not config.api_key:
            raise ToolError("asr_not_configured", "百炼 API Key 尚未配置。")
        check_active()
        path = Path(audio.path)
        try:
            size = path.stat().st_size
        except OSError:
            raise ToolError("asr_invalid_audio", "临时音频文件不可用。") from None
        if not 0 < size <= config.max_audio_bytes or size != audio.bytes:
            raise ToolError("asr_audio_too_large", "临时音频文件为空、大小不符或超过限制。")
        duration = audio.duration_seconds
        if (type(duration) not in (int, float) or not math.isfinite(duration)
                or not 0 < duration <= config.max_audio_seconds):
            raise ToolError("asr_audio_too_long", "音频时长无效或超过限制。")
        started = time.monotonic()
        log.info("[转写开始] 模型=%s 音频字节=%d 时长=%.1fs 总超时=%.1fs",
                 config.model, size, duration, config.timeout)
        try:
            async with asyncio.timeout(config.timeout):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config.timeout)) as session:
                    result = await self._transcribe(session, path, size, duration, check_active)
        except TimeoutError:
            log.warning("[转写失败] 原因=tool_timeout 耗时=%.2fs", time.monotonic() - started)
            raise ToolError("tool_timeout", "百炼转写总超时，本次失败且不重试；已提交的云端任务可能仍在处理。") from None
        except (aiohttp.ClientError, OSError):
            # aiohttp may wrap an upload-generator guard exception in a network
            # exception; restore the active-request guard before returning failure.
            check_active()
            log.warning("[转写失败] 原因=asr_network_error 耗时=%.2fs", time.monotonic() - started)
            raise ToolError("asr_network_error", "百炼转写网络或临时文件读取失败，本次不重试。") from None
        except ToolError as error:
            log.warning("[转写失败] 原因=%s 耗时=%.2fs", error.code, time.monotonic() - started)
            raise
        check_active()
        log.info("[转写完成] 字符=%d 句段=%d 耗时=%.2fs", len(result.text), len(result.segments),
                 time.monotonic() - started)
        return result

    async def _transcribe(self, session, path, size, duration, check_active):
        config = self.config
        base = config.base_url.rstrip("/")
        auth = {"Authorization": f"Bearer {config.api_key}"}
        data = await _request_json(session, "GET", base + "/uploads", stage="获取上传凭证",
                                   check_active=check_active, headers=auth,
                                   params={"action": "getPolicy", "model": config.model})
        host, key, prefix, trailer, headers = _multipart(data.get("data"), path, size)
        check_active()
        log.debug("[上传音频] 字节=%d", size)
        stream = _audio_stream(path, size, prefix, trailer, check_active)
        try:
            async with session.request("POST", host, data=stream, headers=headers, allow_redirects=False) as response:
                check_active()
                if response.status != 200:
                    raise ToolError("asr_upload_failed", f"百炼音频上传失败（HTTP {response.status}），本次不重试。")
                await _body(response, _API_BYTES, check_active)
        finally:
            await stream.aclose()
        log.debug("[音频上传完成]")
        data = await _request_json(
            session, "POST", base + "/services/audio/asr/transcription", stage="提交转写",
            check_active=check_active,
            headers={**auth, "X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"},
            json={"model": config.model, "input": {"file_urls": ["oss://" + key]},
                  "parameters": {"channel_id": [0]}},
        )
        output = data.get("output")
        task_id = output.get("task_id") if isinstance(output, dict) else None
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ToolError("asr_invalid_response", "百炼未返回有效的转写任务编号。")
        previous_status = None
        while True:
            data = await _request_json(session, "GET", base + "/tasks/" + task_id, stage="查询转写",
                                       check_active=check_active, headers=auth)
            output = data.get("output")
            status = output.get("task_status") if isinstance(output, dict) else None
            if status not in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"):
                raise ToolError("asr_invalid_response", "百炼返回了无效任务状态。")
            if status != previous_status:
                log.debug("[转写任务状态] 状态=%s", status)
                previous_status = status
            if status == "SUCCEEDED":
                break
            if status not in ("PENDING", "RUNNING"):
                raise ToolError("asr_task_failed", "百炼转写任务失败，本次不重试。")
            check_active()
            await asyncio.sleep(POLL_SECONDS)
            check_active()
        results = output.get("results")
        if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                or results[0].get("subtask_status") != "SUCCEEDED"):
            raise ToolError("asr_task_failed", "百炼音频子任务失败或未返回结果。")
        url = _cloud_url(results[0].get("transcription_url"))
        data = await _request_json(session, "GET", url, stage="读取转写", check_active=check_active,
                                   limit=_RESULT_BYTES)
        check_active()
        return _transcript(data, task_id, duration)
