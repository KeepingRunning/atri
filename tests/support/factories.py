"""Deterministic time, OneBot events and native model action builders."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]


def daytime():
    return datetime(2026, 9, 11, 12, 35, tzinfo=ZoneInfo("Asia/Shanghai"))


def raw(mid=1, gid=1, text="你好", uid=2, self_id=99, mention=True):
    parts = [{"type": "text", "data": {"text": text}}]
    if mention:
        parts.insert(0, {"type": "at", "data": {"qq": str(self_id)}})
    return {"post_type": "message", "message_type": "group", "self_id": self_id,
            "group_id": gid, "user_id": uid, "message_id": mid, "message": parts,
            "sender": {"nickname": "测试群友"}}


def call(name, args, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {"name": name,
            "arguments": json.dumps(args, ensure_ascii=False)}}


def action(name="reply", ids=("1",), **extra):
    args = {"understanding": {"topic": "自由话题", "interaction": "群友正在交流", "interest": "可以有具体回应"},
            "reason": "根据这批交流决定"}
    if name == "reply":
        args.update(target_message_ids=list(ids), purpose="回应本批消息", reference_facts=[],
                    interpretation="", style_hint="随交流自然表达")
    if name == "wait":
        args["seconds"] = .05
    args.update(extra)
    return {"role": "assistant", "content": None, "tool_calls": [call(name, args)]}
