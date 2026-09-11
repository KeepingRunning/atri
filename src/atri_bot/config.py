from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from .willingness import ReplyConfig
from .logging_setup import LoggingConfig
from .schedule import ScheduleConfig
from .tools import ToolsConfig
from .vision import VisionConfig


@dataclass
class Config:
    root: Path
    data: Path
    groups: frozenset[str] = frozenset()
    self_id: str = ""
    queue_size: int = 32
    parallel: int = 4
    host: str = "127.0.0.1"
    port: int = 8080
    ws_path: str = "/onebot/v11/ws"
    action_timeout: float = 15
    token: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    model: str = ""
    llm_timeout: float = 60
    max_output_tokens: int = 512
    output_limit_field: str = "max_tokens"
    thinking: str = ""
    history_seconds: int = 3600
    personal_info: str = "personal_info.txt"
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)

    @classmethod
    def load(cls, path: Path) -> Config:
        path = path.resolve()
        with path.open("rb") as f:
            raw = tomllib.load(f)
        b, w, l, c = (raw.get(k, {}) for k in ("bot", "onebot", "llm", "context"))
        conf = cls(
            root=path.parent, data=(path.parent / b.get("data_dir", "data")).resolve(),
            groups=frozenset(str(int(g)) for g in b.get("allowed_groups", [])),
            self_id=str(b.get("self_id", "")),
            queue_size=b.get("queue_size", 32), parallel=b.get("max_parallel_generations", 4),
            host=w.get("host", "127.0.0.1"), port=w.get("port", 8080),
            ws_path=w.get("path", "/onebot/v11/ws"), action_timeout=w.get("action_timeout", 15),
            token=w.get("token", ""), api_key=l.get("api_key", ""),
            base_url=l.get("base_url", ""), model=l.get("model", ""),
            llm_timeout=l.get("timeout", 60), max_output_tokens=l.get("max_output_tokens", 512),
            output_limit_field=l.get("output_limit_field", "max_tokens"),
            thinking=l.get("thinking", ""),
            history_seconds=c.get("history_seconds", 3600),
            personal_info=b.get("personal_info", "personal_info.txt"))
        try:
            conf.reply = ReplyConfig(**raw.get("reply", {}))
        except TypeError:
            raise ValueError("Invalid [reply] configuration fields") from None
        conf.reply.validate()
        try:
            conf.logging = LoggingConfig(**raw.get("logging", {}))
        except TypeError:
            raise ValueError("Invalid [logging] configuration fields") from None
        conf.logging.validate()
        try:
            schedule = dict(raw.get("schedule", {}))
            # The retired generator's model option has no effect on local selection.
            schedule.pop("model", None)
            conf.schedule = ScheduleConfig(**schedule)
        except TypeError:
            raise ValueError("Invalid [schedule] configuration fields") from None
        conf.schedule.validate()
        try:
            conf.tools = ToolsConfig(**raw.get("tools", {}))
        except TypeError:
            raise ValueError("Invalid [tools] configuration fields") from None
        conf.tools.validate()
        try:
            conf.vision = VisionConfig(**raw.get("vision", {}))
        except TypeError:
            raise ValueError("Invalid [vision] configuration fields") from None
        conf.vision.validate()
        if conf.vision.enabled and not conf.tools.enabled:
            raise ValueError("vision.enabled requires tools.enabled=true")
        if type(conf.history_seconds) is not int or conf.history_seconds <= 0:
            raise ValueError("context.history_seconds must be a positive integer")
        for name in ("queue_size", "parallel", "action_timeout", "llm_timeout", "max_output_tokens"):
            if getattr(conf, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if conf.output_limit_field not in ("max_tokens", "max_completion_tokens"):
            raise ValueError("Unsupported llm.output_limit_field")
        if conf.thinking not in ("", "enabled", "disabled"):
            raise ValueError("llm.thinking must be empty, enabled or disabled")
        if conf.self_id:
            conf.self_id = str(int(conf.self_id))
        return conf

    def read_personal_info(self):
        text = (self.root / self.personal_info).read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("Personal info must not be empty")
        return text

    def require_live(self):
        if not self.api_key or not self.base_url or not self.model:
            raise ValueError("Set llm.base_url, llm.model and llm.api_key in config.toml")
        url = urlsplit(self.base_url)
        if (url.scheme not in ("https", "http") or not url.hostname or url.username or
                url.password or url.query or url.fragment):
            raise ValueError("Invalid API base URL")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Remote model endpoints must use HTTPS")

    def require_serve(self):
        self.require_live()
        self.read_personal_info()
        if not self.groups or not self.self_id or not self.token:
            raise ValueError("Set bot.allowed_groups, bot.self_id and onebot.token in config.toml")
