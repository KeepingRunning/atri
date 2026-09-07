import asyncio

import aiohttp


class ModelError(RuntimeError):
    def __init__(self, message, code="model_error"):
        super().__init__(message)
        self.code = code


class ChatModel:
    """OpenAI-compatible chat/completions provider."""

    def __init__(self, config, session):
        self.config, self.session = config, session

    async def complete(self, messages):
        self.config.require_live()
        payload = {"model": self.config.model, "messages": messages,
                   self.config.output_limit_field: self.config.max_output_tokens}
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
