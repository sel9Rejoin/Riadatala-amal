"""SQLite schema. Every tenant-owned table carries coach_id; all times are stored in UTC (ISO-8601)."""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS coaches(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, tz TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS specialties(
  id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, name TEXT NOT NULL, bot_profile_ref TEXT);
CREATE TABLE IF NOT EXISTS coach_specialties(
  coach_id INTEGER NOT NULL REFERENCES coaches(id), specialty_id INTEGER NOT NULL REFERENCES specialties(id),
  PRIMARY KEY(coach_id, specialty_id));
CREATE TABLE IF NOT EXISTS session_models(
  id INTEGER PRIMARY KEY, coach_id INTEGER NOT NULL REFERENCES coaches(id),
  specialty_id INTEGER REFERENCES specialties(id), name TEXT NOT NULL,
  duration_min INTEGER NOT NULL, buffer_before_min INTEGER NOT NULL DEFAULT 0,
  buffer_after_min INTEGER NOT NULL DEFAULT 0, price REAL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS calendar_accounts(
  id INTEGER PRIMARY KEY, coach_id INTEGER NOT NULL REFERENCES coaches(id),
  provider TEXT NOT NULL, encrypted_tokens BLOB, sync_state TEXT NOT NULL DEFAULT 'ok');
CREATE TABLE IF NOT EXISTS availability_rules(
  coach_id INTEGER PRIMARY KEY REFERENCES coaches(id),
  weekly_hours TEXT NOT NULL,  -- JSON {"0":[["09:00","17:00"]],...} Monday=0, coach-local time
  min_notice_min INTEGER NOT NULL DEFAULT 120, max_horizon_days INTEGER NOT NULL DEFAULT 30,
  slot_step_min INTEGER NOT NULL DEFAULT 30);
CREATE TABLE IF NOT EXISTS clients(
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, channel_user_id TEXT NOT NULL,
  tz TEXT NOT NULL, consent INTEGER NOT NULL DEFAULT 0, UNIQUE(channel, channel_user_id));
CREATE TABLE IF NOT EXISTS bookings(
  id INTEGER PRIMARY KEY, coach_id INTEGER NOT NULL REFERENCES coaches(id),
  client_id INTEGER NOT NULL REFERENCES clients(id), model_id INTEGER NOT NULL REFERENCES session_models(id),
  start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
  block_start_utc TEXT NOT NULL, block_end_utc TEXT NOT NULL,  -- session + buffers, used for overlap checks
  status TEXT NOT NULL CHECK(status IN ('pending_confirmation','confirmed','completed','cancelled','expired','failed_sync')),
  hold_expires_utc TEXT, external_event_id TEXT, idempotency_key TEXT UNIQUE,
  sync_attempts INTEGER NOT NULL DEFAULT 0, next_sync_utc TEXT, created_utc TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_bookings_coach_time ON bookings(coach_id, block_start_utc, block_end_utc);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, ts_utc TEXT NOT NULL, coach_id INTEGER, actor TEXT, action TEXT NOT NULL, detail TEXT);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # isolation_level=None: transactions are managed explicitly (BEGIN IMMEDIATE) for double-booking safety.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
