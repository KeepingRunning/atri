"""显式运行的真实模型冒烟测试，使用模拟消息和现有上下文构造逻辑。"""
from dataclasses import dataclass
import sys
import time

import aiohttp

from .context import build_conversation, build_willingness_context
from .logging_setup import ModuleFormatter, preview
from .model import ChatModel, ModelError
from .types import Event
from .willingness import GateDecision


@dataclass(frozen=True)
class ApiTestResult:
    name: str
    passed: bool
    elapsed_seconds: float
    detail: str
    error: str = ""


def _event(text, message_id):
    return Event.parse({
        "post_type": "message", "message_type": "group",
        "group_id": 1, "user_id": 2, "self_id": 99,
        "message_id": message_id, "time": time.time(),
        "sender": {"nickname": "API 连接测试"},
        "message": [{"type": "at", "data": {"qq": "99"}},
                    {"type": "text", "data": {"text": text}}],
    })


async def run_api_tests(config, *, stream=None):
    """依次测试四个真实请求；不创建 Bot，不访问群聊存储或 OneBot。"""
    config.require_live()
    persona = config.read_personal_info()
    stream = sys.stdout if stream is None else stream
    formatter = ModuleFormatter(secrets=(config.api_key, config.token))

    def emit(message):
        print(formatter.clean(message), file=stream, flush=True)

    def sample(text):
        return preview(formatter.clean(text), config.logging.preview_chars)

    greeting = _event("亚托莉，你好，在吗？", 1)
    quiet = _event("亚托莉，这条消息不用回复，请保持安静。", 2)
    gate = GateDecision(True, 100, 42, "at_self", {"relation": 100, "content": 20})
    cases = [
        ("最短回复", "minimal", [{"role": "user", "content": "这是接口连接测试，请只回复 OK。"}], None),
        ("接话意愿：打招呼", "willingness", build_willingness_context(persona, greeting, [], gate), True),
        ("接话意愿：要求安静", "willingness", build_willingness_context(persona, quiet, [], gate), False),
        ("人设回复", "reply", build_conversation(persona, greeting, []), None),
    ]
    emit(f"API 地址：{config.base_url.rstrip('/')}/chat/completions")
    emit(f"回复模型：{config.model}；判断模型：{config.reply.judgment_model or config.model}")
    emit(f"单次超时：{config.llm_timeout:g} 秒；思考模式：{config.thinking or '接口默认'}")
    emit("将调用真实模型 4 次，使用人设和模拟消息，不连接 QQ。")
    results = []
    started = time.perf_counter()
    async with aiohttp.ClientSession() as session:
        model = ChatModel(config, session)
        for index, (name, kind, messages, expected) in enumerate(cases, 1):
            emit(f"[{index}/{len(cases)}] 开始：{name}")
            step_started = time.perf_counter()
            error = ""
            try:
                if kind == "willingness":
                    assessment = await model.assess_reply(messages)
                    should_reply = assessment.score >= config.reply.threshold
                    passed = should_reply == expected
                    detail = (f"score={assessment.score}，阈值={config.reply.threshold}，"
                              f"结果={'接话' if should_reply else '等待'}，"
                              f"预期={'接话' if expected else '等待'}，理由={sample(assessment.reason)}")
                else:
                    text = await model.complete(messages, purpose="connection_test" if kind == "minimal" else "reply")
                    passed = kind != "minimal" or text == "OK"
                    detail = f"回复={sample(text)}"
                if not passed:
                    error = "unexpected_response"
                    detail = "接口已返回结果，但未符合测试预期；" + detail
            except ModelError as exc:
                passed, error = False, exc.code
                detail = {
                    "model_timeout": "模型请求超时",
                    "model_empty_response": "接口未返回可用正文",
                    "invalid_reply_assessment": "意愿判断不符合 JSON 输出协议",
                }.get(error, str(exc))
            elapsed = time.perf_counter() - step_started
            result = ApiTestResult(name, passed, elapsed, detail, error)
            results.append(result)
            emit(f"[{'通过' if passed else '失败'}] {name} | {elapsed:.2f} 秒 | "
                 f"{error + '：' if error else ''}{detail}")
    emit(f"测试完成：{sum(r.passed for r in results)}/{len(results)} 通过，"
         f"总耗时 {time.perf_counter() - started:.2f} 秒。")
    return results
