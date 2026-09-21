"""Sticker candidates and scripted sub-planner used by protocol and session tests."""
from copy import deepcopy

from tests.support.factories import call
from tests.support.models import PlanningModel


IMAGE = {"type": "image", "data": {"file": "base64://ZmFrZS1pbWFnZQ=="}}
CANDIDATE = {"id": "happy", "title": "开心", "description": "亚托莉笑着举起双手",
             "visible_text": ["好耶"], "emotions": ["高兴"], "usage": ["庆祝"],
             "avoid": ["悲伤时"], "animation_summary": "静态图", "intensity": 2}


def supplement(sticker_id="happy", **extra):
    return {"role": "assistant", "content": None, "tool_calls": [call("supplement_sticker",
        {"sticker_id": sticker_id, "reason": "补充开心的语气", **extra})]}


class FakeStickerLibrary:
    def __init__(self):
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


class StickerModel(PlanningModel):
    def __init__(self):
        super().__init__()
        self.sticker_plans, self.sticker_steps, self.main_definitions = [], [], []

    async def plan(self, messages, definitions, *, purpose="planner"):
        if purpose == "planner":
            self.main_definitions.append(deepcopy(definitions))
            return await super().plan(messages, definitions)
        self.sticker_plans.append((deepcopy(messages), deepcopy(definitions)))
        value = self.sticker_steps.pop(0) if self.sticker_steps else supplement()
        if callable(value):
            value = await value(messages)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)
