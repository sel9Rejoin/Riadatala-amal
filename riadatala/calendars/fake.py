"""In-memory provider for deterministic tests and local development."""
from datetime import datetime
from itertools import count

from .base import BusyBlock, CalendarAuthError, CalendarError


class FakeCalendar:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.manual_busy: dict[int, list[BusyBlock]] = {}
        self._by_request: dict[str, str] = {}
        self._ids = count(1)
        self.fail_next = 0          # upcoming calls that raise CalendarError
        self.auth_expired = False

    def _maybe_fail(self):
        if self.auth_expired:
            raise CalendarAuthError("token expired")
        if self.fail_next > 0:
            self.fail_next -= 1
            raise CalendarError("temporary outage")

    def add_busy(self, coach_id: int, start: datetime, end: datetime):
        self.manual_busy.setdefault(coach_id, []).append(BusyBlock(start, end))

    def list_busy(self, coach_id, start, end):
        self._maybe_fail()
        blocks = list(self.manual_busy.get(coach_id, []))
        blocks += [BusyBlock(e["start"], e["end"]) for e in self.events.values() if e["coach_id"] == coach_id]
        return [b for b in blocks if b.start < end and b.end > start]

    def create_event(self, coach_id, *, request_id, start, end, title):
        self._maybe_fail()
        if request_id in self._by_request:
            return self._by_request[request_id]
        eid = f"evt{next(self._ids)}"
        self.events[eid] = dict(coach_id=coach_id, start=start, end=end, title=title)
        self._by_request[request_id] = eid
        return eid

    def delete_event(self, coach_id, event_id):
        self._maybe_fail()
        self.events.pop(event_id, None)
