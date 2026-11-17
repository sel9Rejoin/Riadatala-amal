"""Provider-agnostic calendar interface. Nothing outside this package may depend on a specific provider."""
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class CalendarError(Exception):
    """Transient or permanent provider failure."""


class CalendarAuthError(CalendarError):
    """Credentials expired/revoked: the coach must re-link."""


@dataclass(frozen=True)
class BusyBlock:
    start: datetime  # tz-aware UTC
    end: datetime


class CalendarProvider(Protocol):
    def list_busy(self, coach_id: int, start: datetime, end: datetime) -> list[BusyBlock]: ...

    def create_event(self, coach_id: int, *, request_id: str, start: datetime, end: datetime,
                     title: str) -> str:
        """Idempotent on request_id: a retry must return the same event id, never a duplicate."""

    def delete_event(self, coach_id: int, event_id: str) -> None: ...
