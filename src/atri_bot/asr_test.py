"""百炼录音文件转写连通性测试；不连接 QQ，不下载本地模型。"""
import asyncio
import logging
import re
import sys
import time
from urllib.parse import urlsplit

import aiohttp

from .cloud_asr import ASRConfig
from .logging_setup import preview


log = logging.getLogger("atri.asr")
SAMPLE_AUDIO_URL = "https://dashscope.oss-cn-beijing.aliyuncs.com/samples/audio/paraformer/hello_world_female2.wav"
POLL_SECONDS = 1


class ASRTestError(Exception):
    pass


def _error_code(data):
    code = data.get("code", "") if isinstance(data, dict) else ""
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) else "unknown"


async def _request_json(session, method, url, *, stage, **kwargs):
    # Never forward authorization across redirects or to the transcript download.
    async with session.request(method, url, allow_redirects=False, **kwargs) as response:
        log.debug("[HTTP响应] 阶段=%s 状态码=%d", stage, response.status)
        try:
            data = await response.json(content_type=None)
        except (ValueError, UnicodeError):
            raise ASRTestError(f"{stage}失败：HTTP {response.status}，响应不是 JSON") from None
        if response.status < 200 or response.status >= 300:
            hint = {401: "请检查百炼 Key 和地域", 403: "请检查模型权限、地域和账户状态",
                    429: "请求限流或额度受限"}.get(response.status, "请检查服务状态和配置")
            raise ASRTestError(f"{stage}失败：HTTP {response.status}，code={_error_code(data)}；{hint}")
        if not isinstance(data, dict):
            raise ASRTestError(f"{stage}失败：响应应为 JSON 对象")
        return data


async def _transcribe_sample(config, session):
    base = config.base_url.rstrip("/")
    auth = {"Authorization": f"Bearer {config.api_key}"}
    log.info("[提交任务] 模型=%s 音频=官方短音频", config.model)
    data = await _request_json(
        session, "POST", base + "/services/audio/asr/transcription", stage="提交任务",
        headers={**auth, "X-DashScope-Async": "enable"},
        json={"model": config.model, "input": {"file_urls": [SAMPLE_AUDIO_URL]}, "parameters": {}},
    )
    output = data.get("output")
    task_id = output.get("task_id") if isinstance(output, dict) else None
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        raise ASRTestError(f"提交任务失败：未返回有效 task_id，code={_error_code(data)}")
    log.info("[任务已提交] task_id=%s", task_id)
    previous_status = None
    while True:
        data = await _request_json(session, "GET", base + "/tasks/" + task_id,
                                   stage="查询任务", headers=auth)
        output = data.get("output")
        if not isinstance(output, dict):
            raise ASRTestError(f"查询任务失败：缺少 output，code={_error_code(data)}")
        status = output.get("task_status")
        if status not in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"):
            raise ASRTestError("查询任务失败：返回了未知任务状态")
        if status != previous_status:
            log.info("[任务状态] status=%s", status)
            previous_status = status
        if status == "SUCCEEDED":
            break
        if status not in ("PENDING", "RUNNING"):
            raise ASRTestError(f"云端任务失败：status={status}，code={_error_code(output)}")
        await asyncio.sleep(POLL_SECONDS)

    results = output.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ASRTestError("云端任务完成，但缺少单条音频的识别结果")
    result = results[0]
    if result.get("subtask_status") != "SUCCEEDED":
        raise ASRTestError(f"音频识别失败：code={_error_code(result)}")
    transcript_url = result.get("transcription_url")
    try:
        url = urlsplit(transcript_url) if isinstance(transcript_url, str) else None
        valid = url and url.scheme == "https" and url.hostname and not url.username and not url.password
    except ValueError:
        valid = False
    if not valid:
        raise ASRTestError("识别结果缺少有效的 HTTPS transcription_url")
    log.info("[读取转写] 正在下载云端转写 JSON")
    transcript = await _request_json(session, "GET", transcript_url, stage="读取转写")
    items = transcript.get("transcripts")
    if not isinstance(items, list):
        raise ASRTestError("转写结果缺少 transcripts")
    texts = [item["text"].strip() for item in items
             if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip()]
    if not texts:
        raise ASRTestError("转写结果为空，未识别出测试音频中的文字")
    return "\n".join(texts)


async def run_asr_test(config, *, stream=None):
    stream = sys.stdout if stream is None else stream
    asr = config.asr
    asr.validate()
    if not asr.api_key:
        raise ValueError("请在 config.toml 的 [asr] 下填写 api_key，再运行 test-asr")
    print("百炼语音测试：调用真实 API 转写官方短音频，按供应商规则计费。", file=stream, flush=True)
    started = time.monotonic()
    try:
        # One deadline covers submission, queueing, polling and result download.
        async with asyncio.timeout(asr.timeout):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=asr.timeout)) as session:
                text = await _transcribe_sample(asr, session)
    except TimeoutError:
        message = "总超时，本次测试失败且不重试；已提交的云端任务可能仍在处理"
    except ASRTestError as exc:
        message = str(exc)
    except aiohttp.ClientError as exc:
        message = f"网络连接失败（{type(exc).__name__}），请检查网络与地域地址"
    else:
        elapsed = time.monotonic() - started
        log.info("[测试通过] 耗时=%.2fs 输出字符=%d", elapsed, len(text))
        print(f"[通过] 百炼语音转写 | {elapsed:.2f} 秒 | {preview(text.replace(asr.api_key, '[REDACTED]'), 1000)}",
              file=stream, flush=True)
        return True
    message = message.replace(asr.api_key, "[REDACTED]")
    log.error("[测试失败] 耗时=%.2fs 原因=%s", time.monotonic() - started, message)
    print(f"[失败] {message}", file=stream, flush=True)
    return False
