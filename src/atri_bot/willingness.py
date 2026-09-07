"""按群维护接话时机；分数用于筛选，不代表回复概率。

参考 MaiBot 的规则筛选、发言占比和空闲退避思路，针对 ATRI 独立实现。
规则只决定是否值得调用判断模型，模型仍可决定保持安静。
"""
from collections import deque
from dataclasses import dataclass, field
import math
import re


@dataclass
class ReplyConfig:
    mode: str = "willingness"
    frequency: float = 0.7
    names: tuple[str, ...] = ("亚托莉", "ATRI", "アトリ")
    threshold: int = 60
    cooldown_seconds: float = 10
    continuation_seconds: float = 90
    max_message_age_seconds: float = 120
    judgment_model: str = ""

    def validate(self):
        if self.mode not in ("willingness", "at_only"):
            raise ValueError("reply.mode must be willingness or at_only")
        for name, low, high in (("frequency", 0, 1), ("cooldown_seconds", 0, 3600),
                                ("continuation_seconds", 0, 3600), ("max_message_age_seconds", 1, 3600)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"reply.{name} must be a finite number in [{low}, {high}]")
        if type(self.threshold) is not int or not 1 <= self.threshold <= 100:
            raise ValueError("reply.threshold must be an integer in [1, 100]")
        if not isinstance(self.names, (list, tuple)) or not all(isinstance(n, str) and n.strip() for n in self.names):
            raise ValueError("reply.names must be a list of nonempty strings")
        if not isinstance(self.judgment_model, str):
            raise ValueError("reply.judgment_model must be a string")


@dataclass(frozen=True)
class GateDecision:
    consider: bool
    score: int
    threshold: int
    reason: str
    factors: dict[str, int] = field(default_factory=dict)
    pending_count: int = 0


@dataclass(frozen=True)
class ReplyAssessment:
    score: int
    reason: str

    @classmethod
    def parse(cls, data):
        if (not isinstance(data, dict) or set(data) != {"score", "reason"}
                or type(data["score"]) is not int or not 0 <= data["score"] <= 100
                or not isinstance(data["reason"], str) or not 1 <= len(data["reason"].strip()) <= 160):
            raise ValueError("Invalid reply assessment")
        return cls(data["score"], data["reason"].strip())


def contains_name(text, names):
    for name in names:
        # 英文名不能命中 atrial、patriotic 等单词；中文名允许嵌在句中。
        pattern = re.escape(name.strip())
        if name.isascii():
            pattern = r"(?<![a-zA-Z0-9_])" + pattern + r"(?![a-zA-Z0-9_])"
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def is_reaction(text):
    compact = re.sub(r"[\s，。！？!?~～…,.]", "", text).casefold()
    return (not compact or compact in {"好", "好的", "嗯", "嗯嗯", "哦", "噢", "收到", "谢谢", "谢了", "笑死", "草", "ok"}
            or bool(re.fullmatch(r"[哈呵嘿]+|6+|[0-9]+", compact)))


class ReplyWillingness:
    """单个群的短期调度状态。只在新消息到达时评估，不因沉默自发发言。"""

    def __init__(self, config):
        self.config = config
        self.pending = deque(maxlen=64)
        self.last_check = None
        self.wait_count = 0
        self.backoff_until = 0.0

    def evaluate(self, event, group, now):
        cfg = self.config
        threshold = round(35 + 25 * (1 - cfg.frequency))
        while self.pending and now - self.pending[0] > 90:
            self.pending.popleft()
        if event.timestamp > 0 and now - event.timestamp > cfg.max_message_age_seconds:
            return GateDecision(False, 0, threshold, "stale_message")

        at_self = event.self_id in event.mentions
        quotes_self = event.reply_id is not None and event.reply_id in group.sent_message_ids
        # 只读取真正的文本段，文件名、媒体占位符和 CQ 文本不会伪造 @/引用。
        text = "".join(str(p["data"].get("text", "")) for p in event.parts if p.get("type") == "text").strip()
        named = contains_name(text, cfg.names)
        direct = at_self or quotes_self
        last_reply = group.last_sent
        continuation = bool(last_reply and last_reply.get("reply_to_user_id") == event.user_id
                            and 0 <= now - last_reply["time"] <= cfg.continuation_seconds)
        if not direct and event.mentions:
            return GateDecision(False, 0, threshold, "addressed_elsewhere")
        if event.reply_id and not quotes_self and not at_self and not named:
            return GateDecision(False, 0, threshold, "reply_to_other_message")
        if not direct and (not text or (is_reaction(text) and not continuation)):
            return GateDecision(False, 0, threshold, "reaction_or_media")
        if not direct and cfg.frequency == 0:
            return GateDecision(False, 0, threshold, "automatic_reply_disabled")

        self.pending.append(now)
        relation = 100 if at_self else 90 if quotes_self else 50 if named else 30 if continuation else 0
        question = bool(re.search(r"怎么|为何|为什么|如何|什么|有没有|[吗呢？?]\s*$", text))
        request = bool(re.search(r"帮我|帮忙|求助|请教|求推荐|谁能|能不能|你觉得|有什么建议", text))
        content = (20 if text else 0) + (25 if question else 0) + (25 if request else 0)
        backlog = min(30, 8 * (len(self.pending) - 1))
        recent = [(stamp, own) for stamp, own in group.activity if 0 <= now - stamp <= 300]
        own_ratio = sum(own for _, own in recent) / len(recent) if recent else 0.0
        presence = round(min(30, max(0, own_ratio - 0.25) * 60))
        factors = dict(relation=relation, content=content, backlog=backlog, presence_penalty=-presence)
        score = max(0, min(100, sum(factors.values())))
        reason = "at_self" if at_self else "quote_self" if quotes_self else "name_mentioned" if named else "continuation" if continuation else "score"
        if direct:
            return GateDecision(True, score, threshold, reason, factors, len(self.pending))
        # 名字及续聊可提前交给模型辨别语义；普通话题受冷却与连续等待退避限制。
        if not named and not continuation:
            if now < self.backoff_until:
                return GateDecision(False, score, threshold, "waiting_backoff", factors, len(self.pending))
            last_activity = max(self.last_check or 0, last_reply["time"] if last_reply else 0)
            if last_activity and now - last_activity < cfg.cooldown_seconds:
                return GateDecision(False, score, threshold, "cooldown", factors, len(self.pending))
        return GateDecision(score >= threshold, score, threshold, reason if score >= threshold else "below_threshold", factors, len(self.pending))

    def begin_check(self, now):
        self.pending.clear()
        self.last_check = now

    def finish_check(self, should_reply, now):
        self.last_check = now
        self.wait_count = 0 if should_reply else min(8, self.wait_count + 1)
        self.backoff_until = 0 if should_reply else now + min(120, 15 * 2 ** (self.wait_count - 1))
