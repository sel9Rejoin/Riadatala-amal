import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from riadatala.calendars.fake import FakeCalendar
from riadatala.db import connect
from riadatala.scheduling import (ConsentRequired, HoldExpired, NotFound, Scheduling, SlotUnavailable,
                                  local_str)

UTC = timezone.utc
WEEKDAYS = json.dumps({str(d): [["09:00", "17:00"]] for d in range(5)})
NOW = datetime(2026, 11, 17, 6, 0, tzinfo=UTC)          # Tuesday
WED = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
WED_END = datetime(2026, 11, 18, 23, 59, tzinfo=UTC)


def seed(conn, coach_tz="Asia/Riyadh", buffer_after=0):
    conn.execute("INSERT INTO coaches(id,name,tz) VALUES(1,'Coach A',?)", (coach_tz,))
    conn.execute("INSERT INTO coaches(id,name,tz) VALUES(2,'Coach B','Asia/Riyadh')")
    conn.execute("INSERT INTO specialties(id,key,name) VALUES(1,'life','Life Coaching')")
    conn.execute("INSERT INTO session_models(id,coach_id,specialty_id,name,duration_min,buffer_after_min) "
                 "VALUES(1,1,1,'Discovery',60,?)", (buffer_after,))
    conn.execute("INSERT INTO session_models(id,coach_id,specialty_id,name,duration_min) VALUES(2,2,1,'B model',60)")
    for cid in (1, 2):
        conn.execute("INSERT INTO availability_rules(coach_id,weekly_hours) VALUES(?,?)", (cid, WEEKDAYS))
    conn.execute("INSERT INTO calendar_accounts(coach_id,provider) VALUES(1,'fake')")
    for i, tz in ((1, "Asia/Riyadh"), (2, "America/New_York")):
        conn.execute("INSERT INTO clients(id,channel,channel_user_id,tz,consent) VALUES(?,?,?,?,1)",
                     (i, "telegram", f"u{i}", tz))
    conn.execute("INSERT INTO clients(id,channel,channel_user_id,tz,consent) VALUES(3,'telegram','u3','UTC',0)")


class Base(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        seed(self.db)
        self.cal = FakeCalendar()
        self.events = []
        self.s = Scheduling(self.db, self.cal, notifier=lambda c, k, b: self.events.append((c, k, b)))

    def slots(self, a=WED, b=WED_END):
        return self.s.available_slots(1, 1, a, b, NOW)

    def book(self, start, client=1, key=None):
        h = self.s.hold(1, client, 1, start, key or f"k-{start.isoformat()}-{client}", NOW)
        return self.s.confirm(h["id"], client, NOW)


class AvailabilityTests(Base):
    def test_respects_coach_hours_in_coach_timezone(self):
        s = self.slots()
        self.assertEqual(s[0], datetime(2026, 11, 18, 6, 0, tzinfo=UTC))    # 09:00 Riyadh
        self.assertEqual(s[-1], datetime(2026, 11, 18, 13, 0, tzinfo=UTC))  # 16:00 Riyadh (ends 17:00)

    def test_calendar_busy_hides_slots(self):
        self.cal.add_busy(1, datetime(2026, 11, 18, 7, 0, tzinfo=UTC), datetime(2026, 11, 18, 8, 0, tzinfo=UTC))
        s = self.slots()
        self.assertNotIn(datetime(2026, 11, 18, 7, 0, tzinfo=UTC), s)
        self.assertNotIn(datetime(2026, 11, 18, 6, 30, tzinfo=UTC), s)  # overlaps the busy block
        self.assertIn(datetime(2026, 11, 18, 8, 0, tzinfo=UTC), s)

    def test_min_notice_and_horizon(self):
        self.assertEqual(self.s.available_slots(1, 1, NOW, NOW + timedelta(hours=1), NOW), [])
        self.assertEqual(self.s.available_slots(1, 1, NOW + timedelta(days=60), NOW + timedelta(days=61), NOW), [])

    def test_buffer_after_blocks_adjacent_slot(self):
        db = connect(); seed(db, buffer_after=15); cal = FakeCalendar(); s = Scheduling(db, cal)
        h = s.hold(1, 1, 1, datetime(2026, 11, 18, 6, 0, tzinfo=UTC), "k", NOW)
        s.confirm(h["id"], 1, NOW)
        free = s.available_slots(1, 1, WED, WED_END, NOW)
        self.assertNotIn(datetime(2026, 11, 18, 7, 0, tzinfo=UTC), free)   # inside 15-min buffer
        self.assertIn(datetime(2026, 11, 18, 7, 30, tzinfo=UTC), free)

    def test_calendar_outage_fails_closed(self):
        self.cal.fail_next = 1
        with self.assertRaises(Exception):
            self.slots()

    def test_dst_shift_for_coach_timezone(self):
        db = connect(); seed(db, coach_tz="Europe/London"); s = Scheduling(db, FakeCalendar())
        now = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)
        before = s.available_slots(1, 1, datetime(2026, 10, 23, 0, 0, tzinfo=UTC),
                                   datetime(2026, 10, 23, 23, 0, tzinfo=UTC), now)   # BST (UTC+1)
        after = s.available_slots(1, 1, datetime(2026, 10, 27, 0, 0, tzinfo=UTC),
                                  datetime(2026, 10, 27, 23, 0, tzinfo=UTC), now)    # GMT (UTC+0)
        self.assertEqual(before[0].hour, 8)
        self.assertEqual(after[0].hour, 9)

    def test_client_sees_own_timezone(self):
        t = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        self.assertIn("09:00 +03", local_str(t, "Asia/Riyadh"))
        self.assertIn("01:00 EST", local_str(t, "America/New_York"))


class BookingTests(Base):
    def test_full_flow_creates_exactly_one_event_at_right_time(self):
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        b = self.book(start)
        self.assertEqual(b["status"], "confirmed")
        self.assertEqual(self.s.process_sync(NOW)["synced"], 1)
        self.assertEqual(self.s.process_sync(NOW + timedelta(minutes=5))["synced"], 0)  # no duplicate
        self.assertEqual(len(self.cal.events), 1)
        ev = next(iter(self.cal.events.values()))
        self.assertEqual(ev["start"], start)
        self.assertNotIn("Client", ev["title"])  # no client PII in calendar title

    def test_booked_slot_disappears_and_cannot_be_held_again(self):
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        self.book(start)
        self.assertNotIn(start, self.slots())
        with self.assertRaises(SlotUnavailable):
            self.s.hold(1, 2, 1, start, "other", NOW)

    def test_pending_hold_blocks_then_expires(self):
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        h = self.s.hold(1, 1, 1, start, "k1", NOW)
        self.assertNotIn(start, self.slots())
        later = NOW + timedelta(minutes=11)
        self.assertIn(start, self.s.available_slots(1, 1, WED, WED_END, later))  # hold lapsed
        with self.assertRaises(HoldExpired):
            self.s.confirm(h["id"], 1, later)

    def test_hold_is_idempotent(self):
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        a = self.s.hold(1, 1, 1, start, "same", NOW)
        b = self.s.hold(1, 1, 1, start, "same", NOW)
        self.assertEqual(a["id"], b["id"])

    def test_requires_consent_and_tenant_isolation(self):
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        with self.assertRaises(ConsentRequired):
            self.s.hold(1, 3, 1, start, "k", NOW)
        with self.assertRaises(NotFound):
            self.s.hold(1, 1, 2, start, "k2", NOW)  # model 2 belongs to coach 2
        h = self.s.hold(1, 1, 1, start, "k3", NOW)
        with self.assertRaises(NotFound):
            self.s.confirm(h["id"], 2, NOW)          # another client cannot confirm it
        with self.assertRaises(NotFound):
            self.s.cancel(h["id"], coach_id=2)       # another coach cannot cancel it

    def test_late_manual_calendar_event_caught_on_confirm(self):
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        h = self.s.hold(1, 1, 1, start, "k", NOW)
        self.cal.add_busy(1, start, start + timedelta(hours=1))   # coach adds a meeting meanwhile
        with self.assertRaises(SlotUnavailable):
            self.s.confirm(h["id"], 1, NOW)

    def test_cancel_removes_calendar_event_and_frees_slot(self):
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        b = self.book(start)
        self.s.process_sync(NOW)
        self.s.cancel(b["id"], client_id=1)
        self.assertEqual(len(self.cal.events), 0)
        self.assertIn(start, self.slots())

    def test_concurrent_holds_only_one_wins(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        seed(connect(path)); cal = FakeCalendar()
        start = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
        results, barrier = [], threading.Barrier(2)

        def attempt(client):
            s = Scheduling(connect_existing(path), cal)
            barrier.wait()
            try:
                s.hold(1, client, 1, start, f"k{client}", NOW); results.append("ok")
            except SlotUnavailable:
                results.append("taken")

        ts = [threading.Thread(target=attempt, args=(c,)) for c in (1, 2)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(results), ["ok", "taken"])


def connect_existing(path):
    import sqlite3
    c = sqlite3.connect(path, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


class SyncFailureTests(Base):
    def test_transient_outage_retries_then_recovers_without_duplicates(self):
        b = self.book(datetime(2026, 11, 18, 6, 0, tzinfo=UTC))
        self.cal.fail_next = 2
        self.assertEqual(self.s.process_sync(NOW)["retry_scheduled"], 1)
        self.assertEqual(self.s.process_sync(NOW + timedelta(seconds=30))["synced"], 0)  # backoff not elapsed
        self.assertEqual(self.s.process_sync(NOW + timedelta(minutes=3))["retry_scheduled"], 1)
        self.assertEqual(self.s.process_sync(NOW + timedelta(minutes=30))["synced"], 1)
        self.assertEqual(len(self.cal.events), 1)
        self.assertEqual(self.db.execute("SELECT status FROM bookings WHERE id=?", (b["id"],)).fetchone()[0],
                         "confirmed")

    def test_permanent_failure_becomes_failed_sync_and_notifies(self):
        s = Scheduling(self.db, self.cal, max_sync_attempts=2,
                       notifier=lambda c, k, b: self.events.append((c, k, b)))
        b = self.book(datetime(2026, 11, 18, 6, 0, tzinfo=UTC))
        self.cal.fail_next = 99
        s.process_sync(NOW); s.process_sync(NOW + timedelta(hours=1))
        row = self.db.execute("SELECT status FROM bookings WHERE id=?", (b["id"],)).fetchone()
        self.assertEqual(row[0], "failed_sync")
        self.assertIn((1, "sync_failed", b["id"]), self.events)
        # booking still holds the slot locally (source of truth) until the coach resolves it
        self.cal.fail_next = 0
        self.assertNotIn(datetime(2026, 11, 18, 6, 0, tzinfo=UTC), self.s.available_slots(1, 1, WED, WED_END, NOW))

    def test_expired_auth_flags_account_and_notifies(self):
        b = self.book(datetime(2026, 11, 18, 6, 0, tzinfo=UTC))
        self.cal.auth_expired = True
        self.s.process_sync(NOW)
        self.assertEqual(self.db.execute("SELECT sync_state FROM calendar_accounts WHERE coach_id=1").fetchone()[0],
                         "auth_expired")
        self.assertIn((1, "calendar_auth_expired", b["id"]), self.events)
        self.cal.auth_expired = False
        self.s.retry_failed_sync(b["id"], 1, NOW)
        self.assertEqual(self.s.process_sync(NOW)["synced"], 1)


if __name__ == "__main__":
    unittest.main()
