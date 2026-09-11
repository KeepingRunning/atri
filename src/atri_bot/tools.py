"""Reusable tool contracts and execution; independent of the model provider."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import math
import re
import time
from typing import Awaitable, Callable, Protocol

from jsonschema import Draft202012Validator

log = logging.getLogger("atri.tools")


@dataclass
class ToolsConfig:
    enabled: bool = True
    max_rounds: int = 2
    max_calls: int = 4
    timeout: float = 5
    max_result_chars: int = 8000

    def validate(self):
        if type(self.enabled) is not bool:
            raise ValueError("tools.enabled must be a boolean")
        for name, low, high in (("max_rounds", 1, 5), ("max_calls", 1, 16), ("max_result_chars", 1000, 32000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"tools.{name} must be an integer in {low}..{high}")
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 30:
            raise ValueError("tools.timeout must be in (0, 30]")


class ToolError(Exception):
    """An expected failure, safe to show to the model. Never include secrets or paths."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ArchiveReader(Protocol):
    async def run(self, method: str, arguments: dict) -> ToolResult: ...


@dataclass(frozen=True)
class ToolContext:
    group_id: str
    user_id: str
    self_id: str
    request_key: str
    now: float
    # Capabilities are supplied by the application, never by model arguments.
    archive: ArchiveReader = field(repr=False)
    check_active: Callable[[], None] = field(repr=False)
    audit: Callable[[dict], None] = field(repr=False)


@dataclass
class ToolResult:
    ok: bool
    data: dict | None = None
    error: dict | None = None
    meta: dict = field(default_factory=dict)

    @classmethod
    def failure(cls, code, message):
        return cls(False, error={"code": code, "message": message})

    def as_dict(self):
        return {"version": 1, "ok": self.ok, "data": self.data, "error": self.error, "meta": self.meta}

    def to_json(self):
        return json.dumps(self.as_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def bounded(self, limit):
        result = deepcopy(self)
        # Only remove whole items: the envelope always stays valid JSON.
        while len(result.to_json()) > limit:
            if result.data and isinstance(result.data.get("items"), list) and result.data["items"]:
                items = result.data["items"]
                # Context queries retain the anchor while shedding surrounding messages.
                index = (0 if len(items) > 1 and result.data.get("anchor") and isinstance(items[-1], dict)
                         and items[-1].get("record_id") == result.data["anchor"] else -1)
                items.pop(index)
                result.meta["truncated"] = True
                if "offset" in result.data:
                    next_offset = result.data["offset"] + len(result.data["items"])
                    limited = next_offset > result.data.get("max_offset", math.inf)
                    result.data["next_offset"] = None if limited else next_offset
                    result.meta["pagination_limited"] = limited
                    result.data["has_more"] = True
            else:
                return self.failure("result_too_large", "工具结果过长，请缩小范围或降低 limit。")
        if result.meta.get("truncated") and not result.data.get("items"):
            return self.failure("result_too_large", "单条结果过长，请缩小检索范围。")
        return result


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], Awaitable[ToolResult]]


class ToolRegistry:
    def __init__(self):
        self._tools = {}

    def register(self, spec: ToolSpec):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", spec.name) or spec.name in self._tools:
            raise ValueError("Invalid or duplicate tool name")
        schema = deepcopy(spec.parameters)
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("Tool parameters must be an object with additionalProperties=false")
        Draft202012Validator.check_schema(schema)
        self._tools[spec.name] = (spec, Draft202012Validator(schema))

    def definitions(self):
        return [{"type": "function", "function": {"name": name, "description": spec.description,
                 "parameters": deepcopy(validator.schema)}}
                for name, (spec, validator) in self._tools.items()]

    async def execute(self, name, arguments, context: ToolContext, config: ToolsConfig):
        context.check_active()
        try:
            if name not in self._tools:
                raise ToolError("unknown_tool", "该工具未注册。")
            if not isinstance(arguments, str) or len(arguments) > 8000:
                raise ToolError("invalid_arguments", "参数必须是长度不超过 8000 的 JSON 对象字符串。")
            def reject_constant(value):
                raise ValueError("Non-finite JSON number")
            def finite_float(value):
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("Non-finite JSON number")
                return number
            def unique_object(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError("Duplicate JSON key")
                    obj[key] = value
                return obj
            try:
                args = json.loads(arguments, parse_constant=reject_constant, parse_float=finite_float,
                                  object_pairs_hook=unique_object)
            except (ValueError, RecursionError):
                raise ToolError("invalid_arguments", "参数不是有效的 JSON 对象。") from None
            spec, validator = self._tools[name]
            error = next(validator.iter_errors(args), None)
            if error:
                # jsonschema's full message may contain arbitrary input or private data.
                raise ToolError("invalid_arguments", f"参数不符合工具 schema（{error.validator}）。")
            log.debug("[工具参数校验通过] 工具=%s 字段=%s", name, sorted(args))
            async with asyncio.timeout(config.timeout):
                result = await spec.handler(context, args)
            if not isinstance(result, ToolResult):
                raise TypeError("Handler must return ToolResult")
            result = result.bounded(config.max_result_chars)
        except ToolError as exc:
            result = ToolResult.failure(exc.code, str(exc)).bounded(config.max_result_chars)
        except TimeoutError:
            result = ToolResult.failure("tool_timeout", "检索超时，未获得完整结果，请缩小查询范围。")
        except Exception as exc:
            log.error("[工具异常] 工具=%s 异常类型=%s", name, type(exc).__name__)
            result = ToolResult.failure("tool_failed", "工具执行失败，不能据此断言没有相关记录。")
        context.check_active()
        return result


class ToolSession:
    """One reply's execution budget, audit trail and duplicate-call cache."""

    def __init__(self, registry, context, config):
        config.validate()
        self.registry, self.context, self.config = registry, context, config
        self.calls = 0
        self._cache = {}

    async def execute(self, call):
        self.context.check_active()
        name, arguments = call["function"]["name"], call["function"]["arguments"]
        started = time.perf_counter()
        log.info("[工具调用开始] 工具=%s 调用号=%s 已用次数=%d/%d", name, call["id"], self.calls, self.config.max_calls)
        # Arguments are deliberately absent from persistent audit and logs.
        cache_key = (name, arguments)
        cached = False
        if self.calls >= self.config.max_calls:
            result = ToolResult.failure("call_limit", "本轮工具调用次数已用完，请根据已有结果回答。")
        else:
            self.calls += 1
            if cache_key in self._cache:
                result, cached = self._cache[cache_key], True
            else:
                result = await self.registry.execute(name, arguments, self.context, self.config)
                self._cache[cache_key] = result
        self.context.check_active()
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        code = (result.error or {}).get("code", "")
        items = len((result.data or {}).get("items", []))
        self.context.audit({"kind": "tool", "key": self.context.request_key, "tool": name,
                            "call_id": call["id"], "status": "ok" if result.ok else "failed",
                            "error_code": code, "elapsed_ms": elapsed, "items": items,
                            "truncated": bool(result.meta.get("truncated")), "cached": cached})
        log.log(logging.INFO if result.ok else logging.WARNING,
                "[工具调用结束] 工具=%s 调用号=%s 状态=%s 错误=%s 命中条数=%d 截断=%s 缓存=%s 耗时=%.1fms",
                name, call["id"], "ok" if result.ok else "failed", code or "无", items,
                bool(result.meta.get("truncated")), cached, elapsed)
        return result.to_json()
