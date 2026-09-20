"""Track consecutive plain-text runs before asynchronous group processing."""
from dataclasses import dataclass
import math


@dataclass
class RepeatRun:
    text: str
    last_user: str
    triggered: bool = False
    claimed: bool = False


class Repetition:
    def __init__(self):
        self.current = {}
        self.pending = {}

    def reset(self, group_id):
        self.current.pop(group_id, None)

    def receive(self, event, *, now, max_age):
        if (not event.text or not event.parts
                or any(part.get("type") != "text" for part in event.parts)
                or (math.isfinite(event.timestamp) and event.timestamp > 0
                    and now - event.timestamp > max_age)):
            self.reset(event.group_id)
            return
        run = self.current.get(event.group_id)
        if run is None or run.text != event.text:
            run = RepeatRun(event.text, event.user_id)
            self.current[event.group_id] = run
        elif run.last_user != event.user_id:
            run.triggered = True
        run.last_user = event.user_id
        self.pending[event.key] = run

    def contains(self, event):
        run = self.pending.get(event.key)
        return run is not None and run.triggered

    def claim(self, event):
        run = self.pending[event.key]
        if run.claimed:
            return None
        # Claim before awaiting delivery. Failed/unknown sends are not retried.
        run.claimed = True
        return run.text

    def finish(self, event):
        self.pending.pop(event.key, None)
