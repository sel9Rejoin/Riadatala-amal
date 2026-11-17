"""Deterministic scheduling engine (no AI). Source of truth for bookings; the calendar is synced asynchronously.

Flow: available_slots -> hold (pending_confirmation, TTL) -> confirm (explicit client confirmation)
      -> process_sync (create calendar event, retries, failed_sync) ; cancel ; expire_holds.
"""
import json
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .calendars.base import CalendarAuthError, CalendarError, CalendarProvider

UTC = timezone.utc
ACTIVE = ("confirmed", "failed_sync")  # pending holds are active only while unexpired


class SchedulingError(Exception):
    pass


class SlotUnavailable(SchedulingError):
    pass


class HoldExpired(SchedulingError):
    pass


class ConsentRequired(SchedulingError):
    pass


class NotFound(SchedulingError):
    pass


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; times must be tz-aware")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def local_str(dt: datetime, tz: str) -> str:
    """Render an instant in a participant's timezone, naming the zone explicitly."""
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z (") + tz + ")"


Notifier = Callable[[int, str, int], None]  # (coach_id, kind, booking_id)


class Scheduling:
    def __init__(self, conn: sqlite3.Connection, calendar: CalendarProvider, *,
                 hold_ttl_min: int = 10, max_sync_attempts: int = 5, notifier: Notifier | None = None):
        self.db, self.cal = conn, calendar
        self.hold_ttl = timedelta(minutes=hold_ttl_min)
        self.max_attempts = max_sync_attempts
        self.notify = notifier or (lambda coach_id, kind, booking_id: None)

    # ---------- helpers ----------
    def _audit(self, coach_id, actor, action, detail=""):
        self.db.execute("INSERT INTO audit_log(ts_utc,coach_id,actor,action,detail) VALUES(?,?,?,?,?)",
                        (iso(datetime.now(UTC)), coach_id, actor, action, detail))

    def _model(self, coach_id: int, model_id: int):
        m = self.db.execute("SELECT * FROM session_models WHERE id=? AND coach_id=? AND active=1",
                            (model_id, coach_id)).fetchone()
        if not m:
            raise NotFound("session model not found for this coach")
        return m

    def _block(self, m, start: datetime):
        end = start + timedelta(minutes=m["duration_min"])
        return (start - timedelta(minutes=m["buffer_before_min"]),
                end + timedelta(minutes=m["buffer_after_min"]), end)

    def _db_conflict(self, coach_id: int, bs: datetime, be: datetime, now: datetime,
                     exclude_id: int | None = None) -> bool:
        row = self.db.execute(
            """SELECT 1 FROM bookings WHERE coach_id=? AND id IS NOT ?
               AND block_start_utc < ? AND block_end_utc > ?
               AND (status IN ('confirmed','failed_sync')
                    OR (status='pending_confirmation' AND hold_expires_utc > ?)) LIMIT 1""",
            (coach_id, exclude_id, iso(be), iso(bs), iso(now))).fetchone()
        return row is not None

    # ---------- availability ----------
    def available_slots(self, coach_id: int, model_id: int, range_start: datetime, range_end: datetime,
                        now: datetime) -> list[datetime]:
        """Free slot start instants (UTC). Fails closed: if the calendar can't be read, raises (no slots offered)."""
        coach = self.db.execute("SELECT * FROM coaches WHERE id=? AND status='active'", (coach_id,)).fetchone()
        if not coach:
            raise NotFound("coach not found")
        rules = self.db.execute("SELECT * FROM availability_rules WHERE coach_id=?", (coach_id,)).fetchone()
        if not rules:
            return []
        m = self._model(coach_id, model_id)
        lo = max(range_start, now + timedelta(minutes=rules["min_notice_min"]))
        hi = min(range_end, now + timedelta(days=rules["max_horizon_days"]))
        if lo > hi:
            return []
        tz = ZoneInfo(coach["tz"])
        weekly = json.loads(rules["weekly_hours"])
        step = timedelta(minutes=rules["slot_step_min"])
        dur = timedelta(minutes=m["duration_min"])

        busy = self.cal.list_busy(coach_id, lo - timedelta(days=1), hi + timedelta(days=1))
        slots: list[datetime] = []
        day = (lo.astimezone(tz) - timedelta(days=1)).date()
        last_day = (hi.astimezone(tz) + timedelta(days=1)).date()
        while day <= last_day:
            for w_start, w_end in weekly.get(str(day.weekday()), []):
                ws = datetime.combine(day, time.fromisoformat(w_start), tzinfo=tz).astimezone(UTC)
                we = datetime.combine(day, time.fromisoformat(w_end), tzinfo=tz).astimezone(UTC)
                s = ws
                while s + dur <= we:
                    if lo <= s <= hi:
                        bs, be, _ = self._block(m, s)
                        if not any(b.start < be and b.end > bs for b in busy) \
                                and not self._db_conflict(coach_id, bs, be, now):
                            slots.append(s)
                    s += step
            day += timedelta(days=1)
        return slots

    # ---------- booking lifecycle ----------
    def hold(self, coach_id: int, client_id: int, model_id: int, start: datetime, idempotency_key: str,
             now: datetime):
        existing = self.db.execute("SELECT * FROM bookings WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            if existing["coach_id"] != coach_id or existing["client_id"] != client_id:
                raise SchedulingError("idempotency key belongs to another booking")
            return existing
        client = self.db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            raise NotFound("client not found")
        if not client["consent"]:
            raise ConsentRequired("client has not consented to data processing")
        m = self._model(coach_id, model_id)
        start = start.astimezone(UTC)
        if start not in self.available_slots(coach_id, model_id, start, start, now):  # fresh free/busy read
            raise SlotUnavailable("slot is no longer available")
        bs, be, end = self._block(m, start)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute("SELECT * FROM bookings WHERE idempotency_key=?",
                                       (idempotency_key,)).fetchone()
            if existing:
                self.db.execute("COMMIT")
                return existing
            if self._db_conflict(coach_id, bs, be, now):  # authoritative check under the write lock
                raise SlotUnavailable("slot was just taken")
            cur = self.db.execute(
                """INSERT INTO bookings(coach_id,client_id,model_id,start_utc,end_utc,block_start_utc,block_end_utc,
                   status,hold_expires_utc,idempotency_key,created_utc)
                   VALUES(?,?,?,?,?,?,?,'pending_confirmation',?,?,?)""",
                (coach_id, client_id, model_id, iso(start), iso(end), iso(bs), iso(be),
                 iso(now + self.hold_ttl), idempotency_key, iso(now)))
            booking_id = cur.lastrowid
            self._audit(coach_id, f"client:{client_id}", "hold", str(booking_id))
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        return self._get(booking_id)

    def _get(self, booking_id: int):
        return self.db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()

    def confirm(self, booking_id: int, client_id: int, now: datetime):
        """Explicit client confirmation. Re-checks the calendar so a late manual event is caught."""
        b = self._get(booking_id)
        if not b or b["client_id"] != client_id:
            raise NotFound("booking not found")
        if b["status"] in ("confirmed", "failed_sync"):
            return b  # idempotent
        if b["status"] != "pending_confirmation":
            raise SchedulingError(f"cannot confirm a {b['status']} booking")
        if parse(b["hold_expires_utc"]) <= now:
            self.db.execute("UPDATE bookings SET status='expired' WHERE id=?", (booking_id,))
            raise HoldExpired("confirmation window elapsed")
        bs, be = parse(b["block_start_utc"]), parse(b["block_end_utc"])
        if any(x.start < be and x.end > bs for x in self.cal.list_busy(b["coach_id"], bs, be)):
            self.db.execute("UPDATE bookings SET status='expired' WHERE id=?", (booking_id,))
            self._audit(b["coach_id"], "system", "conflict_on_confirm", str(booking_id))
            raise SlotUnavailable("coach calendar changed; slot no longer free")
        self.db.execute(
            "UPDATE bookings SET status='confirmed', hold_expires_utc=NULL, next_sync_utc=? "
            "WHERE id=? AND status='pending_confirmation'", (iso(now), booking_id))
        self._audit(b["coach_id"], f"client:{client_id}", "confirm", str(booking_id))
        return self._get(booking_id)

    def cancel(self, booking_id: int, coach_id: int | None = None, client_id: int | None = None):
        """Cancel by the owning client or by the coach (pass exactly the caller's id)."""
        b = self._get(booking_id)
        if not b or (client_id is not None and b["client_id"] != client_id) \
                or (coach_id is not None and b["coach_id"] != coach_id) \
                or (client_id is None and coach_id is None):
            raise NotFound("booking not found")
        if b["status"] in ("cancelled", "expired", "completed"):
            return b
        self.db.execute("UPDATE bookings SET status='cancelled', hold_expires_utc=NULL WHERE id=?", (booking_id,))
        if b["external_event_id"]:
            try:
                self.cal.delete_event(b["coach_id"], b["external_event_id"])
            except CalendarError:
                self._audit(b["coach_id"], "system", "delete_event_failed", str(booking_id))
                self.notify(b["coach_id"], "delete_event_failed", booking_id)
        self._audit(b["coach_id"], "coach" if coach_id is not None else f"client:{client_id}", "cancel",
                    str(booking_id))
        return self._get(booking_id)

    def expire_holds(self, now: datetime) -> int:
        cur = self.db.execute(
            "UPDATE bookings SET status='expired' WHERE status='pending_confirmation' AND hold_expires_utc <= ?",
            (iso(now),))
        return cur.rowcount

    # ---------- calendar sync (local DB is the source of truth) ----------
    def process_sync(self, now: datetime) -> dict:
        """Push confirmed bookings to the calendar. Idempotent per booking; exponential backoff; failed_sync."""
        done = failed = retried = 0
        rows = self.db.execute(
            "SELECT * FROM bookings WHERE status='confirmed' AND external_event_id IS NULL "
            "AND next_sync_utc <= ? ORDER BY id", (iso(now),)).fetchall()
        for b in rows:
            m = self.db.execute("SELECT name FROM session_models WHERE id=?", (b["model_id"],)).fetchone()
            try:
                eid = self.cal.create_event(b["coach_id"], request_id=f"booking-{b['id']}",
                                            start=parse(b["start_utc"]), end=parse(b["end_utc"]),
                                            title=f"{m['name']} (#{b['id']})")  # no client PII in the title
                self.db.execute("UPDATE bookings SET external_event_id=?, next_sync_utc=NULL WHERE id=?",
                                (eid, b["id"]))
                done += 1
            except CalendarAuthError:
                self.db.execute("UPDATE calendar_accounts SET sync_state='auth_expired' WHERE coach_id=?",
                                (b["coach_id"],))
                self.db.execute("UPDATE bookings SET status='failed_sync' WHERE id=?", (b["id"],))
                self.notify(b["coach_id"], "calendar_auth_expired", b["id"])
                failed += 1
            except CalendarError:
                attempts = b["sync_attempts"] + 1
                if attempts >= self.max_attempts:
                    self.db.execute("UPDATE bookings SET status='failed_sync', sync_attempts=? WHERE id=?",
                                    (attempts, b["id"]))
                    self.notify(b["coach_id"], "sync_failed", b["id"])
                    failed += 1
                else:
                    delay = timedelta(minutes=2 ** attempts)
                    self.db.execute("UPDATE bookings SET sync_attempts=?, next_sync_utc=? WHERE id=?",
                                    (attempts, iso(now + delay), b["id"]))
                    retried += 1
        return {"synced": done, "retry_scheduled": retried, "failed": failed}

    def retry_failed_sync(self, booking_id: int, coach_id: int, now: datetime):
        """Coach-initiated retry (e.g. after re-linking the calendar)."""
        self.db.execute(
            "UPDATE bookings SET status='confirmed', sync_attempts=0, next_sync_utc=? "
            "WHERE id=? AND coach_id=? AND status='failed_sync' AND external_event_id IS NULL",
            (iso(now), booking_id, coach_id))
        self.db.execute("UPDATE calendar_accounts SET sync_state='ok' WHERE coach_id=?", (coach_id,))
