import asyncio
import json
import logging
import time

import aiohttp

from .willingness import ReplyAssessment
from .logging_setup import preview

log = logging.getLogger("atri.model")


class ModelError(RuntimeError):
    def __init__(self, message, code="model_error"):
        super().__init__(message)
        self.code = code


class ChatModel:
    """OpenAI-compatible chat/completions provider."""

    def __init__(self, config, session):
        self.config, self.session = config, session

    async def complete(self, messages, *, max_output_tokens=None, model=None, purpose="reply"):
        self.config.require_live()
        payload = {"model": model or self.config.model, "messages": messages,
                   self.config.output_limit_field: max_output_tokens or self.config.max_output_tokens}
        if self.config.thinking:
            payload["thinking"] = {"type": self.config.thinking}
        started = time.perf_counter()
        log.info("[请求开始] 用途=%s 模型=%s 消息条数=%d 输出上限=%d 超时=%.1fs",
                 purpose, payload["model"], len(messages), payload[self.config.output_limit_field], self.config.llm_timeout)
        log.debug("[请求构成] 用途=%s 各角色=%s 上下文字符=%d 限额字段=%s",
                  purpose, [m.get("role", "unknown") for m in messages],
                  sum(len(str(m.get("content", ""))) for m in messages), self.config.output_limit_field)
        if self.config.thinking:
            log.debug("[思考模式] 用途=%s thinking=%s", purpose, self.config.thinking)
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
            message = data["choices"][0]["message"]
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
