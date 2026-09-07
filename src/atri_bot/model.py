import asyncio
import json

import aiohttp

from .willingness import ReplyAssessment


class ModelError(RuntimeError):
    def __init__(self, message, code="model_error"):
        super().__init__(message)
        self.code = code


class ChatModel:
    """OpenAI-compatible chat/completions provider."""

    def __init__(self, config, session):
        self.config, self.session = config, session

    async def complete(self, messages, *, max_output_tokens=None, model=None):
        self.config.require_live()
        payload = {"model": model or self.config.model, "messages": messages,
                   self.config.output_limit_field: max_output_tokens or self.config.max_output_tokens}
        try:
            async with self.session.post(
                self.config.base_url.rstrip("/") + "/chat/completions",
                json=payload, headers={"Authorization": f"Bearer {self.config.api_key}"},
                allow_redirects=False, timeout=aiohttp.ClientTimeout(total=self.config.llm_timeout)
            ) as response:
                if response.status != 200:
                    raise ModelError(f"Model HTTP {response.status}", f"model_http_{response.status}")
                data = await response.json()
            message = data["choices"][0]["message"]
            content = message.get("content") or message.get("refusal")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("Model returned no text", "model_empty_response")
            return content.strip()
        except ModelError:
            raise
        except asyncio.TimeoutError:
            raise ModelError("Model request timed out", "model_timeout") from None
        except Exception as exc:
            raise ModelError(f"Model request failed: {type(exc).__name__}") from None

    async def assess_reply(self, messages):
        text = await self.complete(messages, max_output_tokens=256,
                                   model=self.config.reply.judgment_model)
        try:
            return ReplyAssessment.parse(json.loads(text))
        except (ValueError, TypeError):
            # 无效判断不能被误当作聊天内容发到群里，也不能悄悄当成同意回复。
            raise ModelError("Invalid reply assessment", "invalid_reply_assessment") from None
