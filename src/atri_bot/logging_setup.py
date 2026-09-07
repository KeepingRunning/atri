"""模块配色、消息追踪上下文及无色轮转日志。"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import zlib


LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
COLORS = {"core": 117, "onebot": 45, "receive": 51, "queue": 75, "bot": 111,
          "willingness": 213, "context": 141, "model": 221, "send": 82, "storage": 109}
PLUGIN_COLORS = (208, 39, 177, 149, 203, 87, 219, 179, 69, 155, 209, 183)
TRACE = ContextVar("atri_log_trace", default={})
_module_overrides = set()


@dataclass
class LoggingConfig:
    level: str = "DEBUG"
    color: str = "auto"
    file: str = "data/logs/atri.log"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 3
    preview_chars: int = 500
    modules: dict[str, str] = field(default_factory=dict)

    def validate(self):
        if not isinstance(self.level, str) or self.level not in LEVELS:
            raise ValueError("logging.level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        if self.color not in ("auto", "always", "never"):
            raise ValueError("logging.color must be auto, always or never")
        if not isinstance(self.file, str):
            raise ValueError("logging.file must be a path string, or empty to disable file logging")
        for name, minimum in (("max_bytes", 1), ("backup_count", 1), ("preview_chars", 0)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"logging.{name} must be an integer >= {minimum}")
        if not isinstance(self.modules, dict):
            raise ValueError("logging.modules must be a table")
        for name, level in self.modules.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*", name)
                    or not isinstance(level, str) or level not in LEVELS):
                raise ValueError("Invalid logging.modules logger name or level")


@contextmanager
def log_context(**fields):
    """ContextVar 让不同群的并发任务保留各自的群号、消息号与用户号。"""
    token = TRACE.set({**TRACE.get(), **fields})
    try:
        yield
    finally:
        TRACE.reset(token)


def current_log_context():
    return dict(TRACE.get())


def preview(value, limit=500):
    """日志中的正文用单行 JSON 字符串展示，避免换行和控制符伪造日志行。"""
    text = str(value)
    if limit == 0:
        return f"[正文隐藏，{len(text)}字符]"
    suffix = f"…[截断，共{len(text)}字符]" if len(text) > limit else ""
    return json.dumps(text[:limit] + suffix, ensure_ascii=False)


class TraceFilter(logging.Filter):
    def filter(self, record):
        record.atri_trace = current_log_context()
        return True


class ModuleFormatter(logging.Formatter):
    def __init__(self, color=False, secrets=()):
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")
        self.color = color
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def clean(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
            # preview() 会将换行等转义，敏感配置的转义形式也应遮蔽。
            escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
            text = text.replace(escaped, "[REDACTED]")
        return re.sub(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]",
                      lambda m: f"\\u{ord(m[0]):04x}", text)

    @staticmethod
    def component(record):
        return record.name.removeprefix("atri.") if record.name != "atri" else "core"

    def format(self, record):
        component = self.clean(self.component(record))
        timestamp = f"{self.formatTime(record, self.datefmt)}.{int(record.msecs):03d}"
        trace = getattr(record, "atri_trace", {})
        location = " ".join(f"{short}={self.clean(str(trace[key]))}" for key, short in
                            (("group_id", "g"), ("message_id", "m"), ("user_id", "u"))
                            if trace.get(key) is not None)
        message = self.clean(record.getMessage())
        if record.exc_info:
            # 每条异常栈另起行并缩进；只信任格式化器产生的换行。
            message += "\n" + "\n".join("    " + self.clean(line)
                                           for line in self.formatException(record.exc_info).splitlines())
        if record.stack_info:
            message += "\n" + "\n".join("    " + self.clean(line) for line in record.stack_info.splitlines())
        if not self.color:
            return f"{timestamp} | {record.levelname:<8} | {component} | {location or '-'} | {message}"
        module_color = COLORS.get(component)
        if module_color is None:
            module_color = PLUGIN_COLORS[zlib.crc32(component.encode('utf-8')) % len(PLUGIN_COLORS)]
        level_color = 196 if record.levelno >= logging.ERROR else 214 if record.levelno >= logging.WARNING else 120 if record.levelno >= logging.INFO else 245
        body_color = level_color if record.levelno >= logging.WARNING else module_color
        return (f"\033[38;5;245m{timestamp}\033[0m | "
                f"\033[38;5;{level_color}m{record.levelname:<8}\033[0m | "
                f"\033[1;38;5;{module_color}m{component}\033[0m | "
                f"\033[38;5;245m{location or '-'}\033[0m | "
                f"\033[38;5;{body_color}m{message}\033[0m")


def configure_logging(config, root: Path, *, secrets=(), stream=None):
    config.validate()
    stream = sys.stderr if stream is None else stream
    use_color = config.color == "always" or (config.color == "auto" and
                "NO_COLOR" not in os.environ and hasattr(stream, "isatty") and stream.isatty())
    console = logging.StreamHandler(stream)
    console.setFormatter(ModuleFormatter(use_color, secrets))
    handlers = [console]
    if config.file:
        path = (root / config.file).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=config.max_bytes,
                                          backupCount=config.backup_count, encoding="utf-8")
        file_handler.setFormatter(ModuleFormatter(False, secrets))
        handlers.append(file_handler)
    logger = logging.getLogger("atri")
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    for handler in handlers:
        handler.addFilter(TraceFilter())
        logger.addHandler(handler)
    logger.setLevel(config.level)
    logger.propagate = False
    for name in _module_overrides:
        logging.getLogger("atri." + name).setLevel(logging.NOTSET)
    _module_overrides.clear()
    for name, level in config.modules.items():
        logging.getLogger("atri." + name).setLevel(level)
        _module_overrides.add(name)
    return logger
