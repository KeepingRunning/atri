"""In-memory models with recorded requests and scriptable planner outcomes."""
from copy import deepcopy
import json

from atri_bot.willingness import ReplyAssessment
from tests.support.factories import action


class RecordingModel:
    def __init__(self):
        self.prompts = []

    async def complete(self, messages, *, tool_session=None):
        self.prompts.append(messages)
        return "收到啦 [CQ:at,qq=all]"


class JudgingModel:
    def __init__(self):
        self.assessment = ReplyAssessment(85, '在向我提问')
        self.judgments, self.replies = [], []

    async def assess_reply(self, messages):
        self.judgments.append(messages)
        return self.assessment

    async def complete(self, messages, *, tool_session=None):
        self.replies.append(messages)
        return '这是实际回复'


class PlanningModel:
    def __init__(self):
        self.plans, self.replies = [], []
        self.steps = []

    async def plan(self, messages, definitions):
        self.plans.append(deepcopy(messages))
        ids = [row["message_id"] for row in json.loads(messages[1]["content"])["snapshot"]["pending"]]
        value = self.steps.pop(0) if self.steps else action(ids=ids)
        if callable(value):
            value = await value(messages)
        if isinstance(value, Exception):
            raise value
        value = deepcopy(value)
        for c in value.get("tool_calls", []):
            c["id"] = "call_" + str(len(self.plans))
        return value

    async def complete(self, messages, **kwargs):
        self.replies.append(deepcopy(messages))
        return "这次回应。"
