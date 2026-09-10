import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import time

import aiohttp

from .willingness import ReplyAssessment
from .context import WILLINGNESS_OUTPUT_RULES
from .logging_setup import preview

log = logging.getLogger("atri.model")
_request_allowed = ContextVar("atri_model_request_allowed", default=None)


class ModelRequestBlocked(RuntimeError):
    """The message is no longer eligible for a model request; do not retry or fall back."""


@contextmanager
def guard_model_requests(allowed):
    # Task-local: concurrent groups and standalone API tests do not share this guard.
    token = _request_allowed.set(allowed)
    try:
        yield
    finally:
        _request_allowed.reset(token)


def check_request_allowed(purpose):
    allowed = _request_allowed.get()
    if allowed is not None and not allowed():
        log.info("[模型请求拦截] 用途=%s 消息已因睡眠失效，不请求或重试", purpose)
        raise ModelRequestBlocked("Message blocked by sleep policy")


class ModelError(RuntimeError):
    def __init__(self, message, code="model_error", *, attempts=1):
        super().__init__(message)
        self.code = code
        self.attempts = attempts


class ChatModel:
    """OpenAI-compatible chat/completions provider."""

    def __init__(self, config, session):
        self.config, self.session = config, session

    async def complete(self, messages, *, max_output_tokens=None, model=None, purpose="reply", json_mode=False):
        check_request_allowed(purpose)
        self.config.require_live()
        payload = {"model": model or self.config.model, "messages": messages,
                   self.config.output_limit_field: max_output_tokens or self.config.max_output_tokens}
        if self.config.thinking:
            payload["thinking"] = {"type": self.config.thinking}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        log.info("[请求开始] 用途=%s 模型=%s 消息条数=%d 输出上限=%d 超时=%.1fs",
                 purpose, payload["model"], len(messages), payload[self.config.output_limit_field], self.config.llm_timeout)
        log.debug("[请求构成] 用途=%s 各角色=%s 上下文字符=%d 限额字段=%s",
                  purpose, [m.get("role", "unknown") for m in messages],
                  sum(len(str(m.get("content", ""))) for m in messages), self.config.output_limit_field)
        if self.config.thinking:
            log.debug("[思考模式] 用途=%s thinking=%s", purpose, self.config.thinking)
        # Check immediately before HTTP submission, including every judgment retry.
        # Keep this outside error conversion: blocking is not a provider failure.
        check_request_allowed(purpose)
        try:
            async with self.session.post(
                self.config.base_url.rstrip("/") + "/chat/completions",
                json=payload, headers={"Authorization": f"Bearer {self.config.api_key}"},
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=self.config.llm_timeout)
            ) as response:
                log.debug("[HTTP响应] 用途=%s 状态码=%d 首次响应耗时=%.1fms", purpose, response.status, (time.perf_counter() - started) * 1000)
                if response.status != 200:
                    raise ModelError(f"Model HTTP {response.status}", f"model_http_{response.status}")
                data = await response.json()
            choice = data["choices"][0]
            log.debug("[生成结束原因] 用途=%s finish_reason=%s", purpose, choice.get("finish_reason", "未提供"))
            message = choice["message"]
            content = message.get("content") or message.get("refusal")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("Model returned no text", "model_empty_response")
            usage = data.get("usage") or {}
            counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                      if isinstance(usage, dict) and type(usage.get(key)) is int}
            log.info("[请求完成] 用途=%s 耗时=%.1fms 输出字符=%d token用量=%s",
                     purpose, (time.perf_counter() - started) * 1000, len(content.strip()), counts or "接口未提供")
            return content.strip()
        except ModelError as exc:
            log.error("[请求失败] 用途=%s 错误=%s 耗时=%.1fms", purpose, exc.code, (time.perf_counter() - started) * 1000)
            raise
        except asyncio.TimeoutError:
            log.error("[请求超时] 用途=%s 耗时=%.1fms", purpose, (time.perf_counter() - started) * 1000)
            raise ModelError("Model request timed out", "model_timeout") from None
        except Exception as exc:
            log.error("[请求异常] 用途=%s 类型=%s 耗时=%.1fms", purpose, type(exc).__name__, (time.perf_counter() - started) * 1000)
            raise ModelError(f"Model request failed: {type(exc).__name__}") from None

    async def assess_reply(self, messages):
        for attempt in range(1, 4):
            log.info("[判断尝试] 第%d/3次，用途=willingness", attempt)
            attempt_messages = messages
            if attempt > 1:
                # 重申协议，不把不合法输出加为 assistant 示例，也不修改调用方上下文。
                attempt_messages = [{**m} for m in messages]
                correction = "\n\n本次是失败后的重新判断，请重新分析原始数据并完整输出 JSON。\n" + WILLINGNESS_OUTPUT_RULES
                if attempt_messages and attempt_messages[0].get("role") == "system":
                    attempt_messages[0]["content"] += correction
                else:
                    attempt_messages.insert(0, {"role": "system", "content": correction.strip()})
            try:
                assessment = await self._assess_reply_once(attempt_messages)
                log.info("[判断尝试成功] 第%d/3次 score=%d", attempt, assessment.score)
                return assessment
            except ModelError as exc:
                exc.attempts = attempt
                check_request_allowed("willingness")
                if attempt == 3:
                    log.error("[判断重试耗尽] 已尝试3次（首次+2次重试），错误=%s", exc.code)
                    raise
                log.warning("[判断重试] 第%d/3次失败 错误=%s，将进行第%d/3次", attempt, exc.code, attempt + 1)

    async def _assess_reply_once(self, messages):
        text = await self.complete(messages, max_output_tokens=256,
                                   model=self.config.reply.judgment_model, purpose="willingness")
        try:
            assessment = ReplyAssessment.parse(json.loads(text))
            log.debug("[判断解析成功] score=%d reason=%s", assessment.score, preview(assessment.reason, 160))
            return assessment
        except (ValueError, TypeError):
            # 无效判断不能被误当作聊天内容发到群里，也不能悄悄当成同意回复。
            log.warning("[判断解析失败] 返回值不符合意愿JSON协议，内容=%s", preview(text, self.config.logging.preview_chars))
            raise ModelError("Invalid reply assessment", "invalid_reply_assessment") from None
