"""Media candidates and scripted sub-planner used by protocol and session tests."""
from copy import deepcopy

from atri_bot.stickers import StickerConfig
from atri_bot.voices import VoiceConfig, semantic_group
from tests.support.factories import call
from tests.support.models import PlanningModel


IMAGE = {"type": "image", "data": {"file": "base64://ZmFrZS1pbWFnZQ=="}}
CANDIDATE = {"id": "happy", "title": "开心", "description": "亚托莉笑着举起双手",
             "visible_text": ["好耶"], "emotions": ["高兴"], "usage": ["庆祝"],
             "avoid": ["悲伤时"], "animation_summary": "静态图", "intensity": 2}
VOICE = {"type": "record", "data": {"file": "base64://ZmFrZS1hdWRpbw=="}}
VOICE_CANDIDATE = {"id": "V0001", "title": "谢谢", "text_ja": "ありがとうございます。", "text_zh": "谢谢你。",
                   "duration_seconds": 1.8, "description": "简短地道谢；语气标签由文字推断，实际听感待试听。",
                   "emotions": ["感激"], "intensity": 1, "usage": ["回应帮助"], "avoid": ["表达不满时"],
                   "context_requirements": ["刚得到对方的帮助"], "semantic_group": semantic_group("ありがとうございます。")}


def supplement(sticker_id="happy", **extra):
    return media("none" if sticker_id is None else "sticker", sticker_id, **extra)


def media(kind="voice", asset_id="V0001", **extra):
    return {"role": "assistant", "content": None, "tool_calls": [call("supplement_media",
        {"kind": kind, "asset_id": asset_id, "reason": "补充本轮语气", **extra})]}


class FakeStickerLibrary:
    def __init__(self):
        self.config = StickerConfig(enabled=True)
        self.queries, self.prepared = [], []
        self.items = [deepcopy(CANDIDATE)]

    def search(self, query, limit=6, recent_ids=()):
        self.queries.append((query, list(recent_ids)))
        return deepcopy(self.items)

    def prepare(self, identity):
        self.prepared.append(identity)
        if identity != "happy":
            raise ValueError("unknown sticker")
        return deepcopy(IMAGE), deepcopy(CANDIDATE)


class FakeVoiceLibrary:
    def __init__(self):
        self.config = VoiceConfig(enabled=True)
        self.queries, self.prepared = [], []
        self.items = [deepcopy(VOICE_CANDIDATE)]

    def search(self, query, limit=6, *, recent_ids=(), recent_groups=()):
        self.queries.append((query, list(recent_ids), list(recent_groups)))
        return deepcopy(self.items)

    def prepare(self, identity):
        self.prepared.append(identity)
        if identity != "V0001":
            raise ValueError("unknown voice")
        return deepcopy(VOICE), deepcopy(VOICE_CANDIDATE)


class SupplementModel(PlanningModel):
    def __init__(self):
        super().__init__()
        self.supplement_plans, self.supplement_steps, self.main_definitions = [], [], []

    async def plan(self, messages, definitions, *, purpose="planner"):
        if purpose == "planner":
            self.main_definitions.append(deepcopy(definitions))
            return await super().plan(messages, definitions)
        if purpose != "supplement":
            raise AssertionError("optional expression requests must use purpose=supplement")
        self.supplement_plans.append((deepcopy(messages), deepcopy(definitions)))
        value = self.supplement_steps.pop(0) if self.supplement_steps else supplement()
        if callable(value):
            value = await value(messages)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)
