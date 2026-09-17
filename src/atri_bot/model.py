import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager, contextmanager
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
_request_semaphore = ContextVar("atri_model_request_semaphore", default=None)
_reserved_slot = ContextVar("atri_model_reserved_slot", default=None)
_active_slot = ContextVar("atri_model_active_slot", default=None)


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


class _ModelSlot:
    def __init__(self, semaphore):
        self.semaphore = semaphore
        self.owner = asyncio.current_task()
        self.held = True

    def release(self):
        if self.held:
            self.held = False
            self.semaphore.release()


@asynccontextmanager
async def reserve_model_slot(semaphore):
    """Reserve the first request before building a fresh snapshot/conversation.

    The first model_request_slot consumes this reservation and releases it as
    soon as that request ends. Keeping this scope open while running tools does
    not retain the slot; subsequent requests acquire their own slots. A model
    implementation without request-level instrumentation releases it on exit.
    """
    await semaphore.acquire()
    slot = _ModelSlot(semaphore)
    semaphore_token = _request_semaphore.set(semaphore)
    reservation_token = _reserved_slot.set(slot)
    try:
        yield
    finally:
        slot.release()
        _reserved_slot.reset(reservation_token)
        _request_semaphore.reset(semaphore_token)


@asynccontextmanager
async def model_request_slot(purpose):
    """Limit actual requests, including image requests made inside tool tasks."""
    check_request_allowed(purpose)
    semaphore = _request_semaphore.get()
    active = _active_slot.get()
    # Planner wraps its model interface, while ChatModel wraps HTTP. Only calls
    # in this same task may reuse a slot; inherited child-task context is not a
    # permit to bypass the concurrency limit.
    if semaphore is None or (active is not None and active.held
                             and active.owner is asyncio.current_task()
                             and active.semaphore is semaphore):
        yield
        return
    slot = _reserved_slot.get()
    if slot is None or not slot.held or slot.owner is not asyncio.current_task():
        started = time.perf_counter()
        await semaphore.acquire()
        slot = _ModelSlot(semaphore)
        log.debug("[模型并发槽位] 用途=%s 等待=%.1fms", purpose, (time.perf_counter() - started) * 1000)
    token = _active_slot.set(slot)
    try:
        check_request_allowed(purpose)
        yield
    finally:
        _active_slot.reset(token)
        slot.release()


class ModelError(RuntimeError):
    def __init__(self, message, code="model_error", *, attempts=1):
        super().__init__(message)
        self.code = code
        self.attempts = attempts


class ChatModel:
    """OpenAI-compatible chat/completions provider."""

    def __init__(self, config, session):
        self.config, self.session = config, session

    async def plan(self, messages, definitions):
        """One native action/tool call; execution belongs to Planner and Bot."""
        # DeepSeek rejects required tool_choice in thinking mode. Explicitly
        # disabled thinking can enforce a call instead of relying only on prose.
        choice = "required" if self.config.thinking == "disabled" else "auto"
        return await self._request(messages, purpose="planner", tools=definitions, tool_choice=choice,
                                   model=self.config.reply.judgment_model or None,
                                   max_output_tokens=self.config.planner.max_output_tokens,
                                   temperature=self.config.planner.temperature)

    async def complete(self, messages, *, max_output_tokens=None, model=None, purpose="reply", json_mode=False,
                       tool_session=None):
        options = dict(max_output_tokens=max_output_tokens, model=model, purpose=purpose, json_mode=json_mode)
        if tool_session is None:
            message = await self._request(messages, **options)
            if message.get("tool_calls"):
                raise ModelError("Unrequested tool call", "unexpected_tool_call")
            return message["content"].strip()
        if purpose != "reply" or json_mode:
            raise ValueError("Tools are only available for conversational replies")
        # Tool messages and provider reasoning stay in this one turn, never in group history.
        conversation = deepcopy(messages)
        definitions = tool_session.registry.definitions()
        used_ids = set()
        for round_number in range(tool_session.config.max_rounds + 1):
            tool_session.context.check_active()
            final = (round_number == tool_session.config.max_rounds or
                     tool_session.calls >= tool_session.config.max_calls)
            log.debug("[工具生成轮次] 轮次=%d/%d 工具选择=%s", round_number + 1,
                      tool_session.config.max_rounds + 1, "none" if final else "auto")
            message = await self._request(conversation, tools=definitions,
                                          tool_choice="none" if final else "auto", **options)
            tool_session.context.check_active()
            calls = message.get("tool_calls") or []
            if not calls:
                return message["content"].strip()
            if final:
                raise ModelError("Provider ignored tool_choice=none", "tool_round_limit")
            if any(call["id"] in used_ids for call in calls):
                raise ModelError("Provider repeated tool call ID", "invalid_tool_calls")
            used_ids.update(call["id"] for call in calls)
            conversation.append(message)
            for call in calls:
                result = await tool_session.execute(call)
                conversation.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        raise ModelError("Tool round limit reached", "tool_round_limit")

    async def _request(self, messages, *, max_output_tokens=None, model=None, purpose="reply", json_mode=False,
                       tools=None, tool_choice=None, temperature=None):
        async with model_request_slot(purpose):
            return await self._request_once(messages, max_output_tokens=max_output_tokens, model=model,
                                           purpose=purpose, json_mode=json_mode, tools=tools,
                                           tool_choice=tool_choice, temperature=temperature)

    async def _request_once(self, messages, *, max_output_tokens=None, model=None, purpose="reply", json_mode=False,
                            tools=None, tool_choice=None, temperature=None):
        check_request_allowed(purpose)
        self.config.require_live()
        payload = {"model": model or self.config.model, "messages": messages,
                   self.config.output_limit_field: max_output_tokens or self.config.max_output_tokens}
        if self.config.thinking:
            payload["thinking"] = {"type": self.config.thinking}
        if temperature is not None:
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        started = time.perf_counter()
        log.info("[请求开始] 用途=%s 模型=%s 消息条数=%d 输出上限=%d 超时=%.1fs",
                 purpose, payload["model"], len(messages), payload[self.config.output_limit_field], self.config.llm_timeout)
        text_chars, image_count = 0, 0
        for message in messages:
            content = message.get("content")
            if isinstance(content, str):
                text_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_chars += len(block.get("text", ""))
                    elif isinstance(block, dict) and block.get("type") == "image_url":
                        image_count += 1
        log.debug("[请求构成] 用途=%s 各角色=%s 文本字符=%d 图片=%d 限额字段=%s",
                  purpose, [m.get("role", "unknown") for m in messages],
                  text_chars, image_count, self.config.output_limit_field)
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
            calls = message.get("tool_calls") or []
            if calls:
                if choice.get("finish_reason") == "length" or not isinstance(calls, list) or len(calls) > 16:
                    raise ModelError("Incomplete or excessive tool calls", "invalid_tool_calls")
                ids = set()
                for call in calls:
                    if (not isinstance(call, dict) or call.get("type") != "function" or
                            not isinstance(call.get("id"), str) or not 0 < len(call["id"]) <= 200 or
                            call["id"] in ids or not isinstance(call.get("function"), dict)):
                        raise ModelError("Invalid tool call envelope", "invalid_tool_calls")
                    function = call["function"]
                    if (not isinstance(function.get("name"), str) or not 0 < len(function["name"]) <= 64 or
                            not isinstance(function.get("arguments"), str)):
                        raise ModelError("Invalid tool function", "invalid_tool_calls")
                    ids.add(call["id"])
            elif not isinstance(content, str) or not content.strip():
                raise ModelError("Model returned no text", "model_empty_response")
            result = {"role": "assistant", "content": content if isinstance(content, str) else None}
            if calls:
                result["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["function"]["name"], "arguments": c["function"]["arguments"]}} for c in calls]
            # Required by DeepSeek when tools and thinking are enabled; never log this field.
            if isinstance(message.get("reasoning_content"), str):
                result["reasoning_content"] = message["reasoning_content"]
            usage = data.get("usage") or {}
            counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                      if isinstance(usage, dict) and type(usage.get(key)) is int}
            log.info("[请求完成] 用途=%s 耗时=%.1fms 输出字符=%d 工具调用=%d token用量=%s",
                     purpose, (time.perf_counter() - started) * 1000, len(content.strip()) if isinstance(content, str) else 0,
                     len(calls), counts or "接口未提供")
            return result
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
