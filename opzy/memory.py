"""Small SQLite store: which emails were handled, pending approvals, and an activity log.

The LangGraph checkpointer keeps each run's state; this store keeps what the
business-level system needs to know across runs.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time


class Memory:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed (msg_id TEXT PRIMARY KEY, at REAL);
            CREATE TABLE IF NOT EXISTS approvals (
                code TEXT PRIMARY KEY, run_id TEXT, candidate TEXT, status TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS log (at REAL, run_id TEXT, event TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY, candidate_email TEXT, candidate_name TEXT, role TEXT,
                slots TEXT, hold_event_id TEXT, status TEXT, at REAL);
            """
        )

    # ---- processed emails
    def seen(self, msg_id: str) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM processed WHERE msg_id=?", (msg_id,)).fetchone() is not None

    def mark_seen(self, msg_id: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO processed VALUES (?,?)", (msg_id, time.time()))
            self.db.commit()

    def unmark_seen(self, msg_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM processed WHERE msg_id=?", (msg_id,))
            self.db.commit()

    # ---- approvals
    def new_approval(self, run_id: str, candidate: str) -> str:
        code = secrets.token_hex(2).upper()  # e.g. "A3F2"
        with self._lock:
            self.db.execute("INSERT INTO approvals VALUES (?,?,?,?,?)", (code, run_id, candidate, "pending", time.time()))
            self.db.commit()
        return code

    def find_pending(self, code: str | None):
        with self._lock:
            if code:
                row = self.db.execute(
                    "SELECT code, run_id, candidate FROM approvals WHERE code=? AND status='pending'", (code.upper(),)
                ).fetchone()
            else:  # no code given: the most recent pending one
                row = self.db.execute(
                    "SELECT code, run_id, candidate FROM approvals WHERE status='pending' ORDER BY at DESC LIMIT 1"
                ).fetchone()
        return row

    def pending_count(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]

    def close_approval(self, code: str, status: str) -> None:
        with self._lock:
            self.db.execute("UPDATE approvals SET status=? WHERE code=?", (status, code))
            self.db.commit()

    # ---- conversations Opzy started (so it can follow up on the candidate's answer)
    def save_thread(self, thread_id, candidate_email, candidate_name, role, slots, hold_event_id) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO threads VALUES (?,?,?,?,?,?,?,?)",
                            (thread_id, candidate_email, candidate_name, role, json.dumps(slots),
                             hold_event_id, "offered", time.time()))
            self.db.commit()

    def open_thread(self, thread_id: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT thread_id, candidate_email, candidate_name, role, slots, hold_event_id "
                                "FROM threads WHERE thread_id=? AND status='offered'", (thread_id,)).fetchone()
        if not r:
            return None
        return {"thread_id": r[0], "candidate_email": r[1], "candidate_name": r[2], "role": r[3],
                "slots": json.loads(r[4]), "hold_event_id": r[5]}

    def open_thread_ids(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT thread_id FROM threads WHERE status='offered'")]

    def set_thread_status(self, thread_id: str, status: str) -> None:
        with self._lock:
            self.db.execute("UPDATE threads SET status=? WHERE thread_id=?", (status, thread_id))
            self.db.commit()

    # ---- log
    def log(self, run_id: str, event: str, data: dict | None = None) -> None:
        with self._lock:
            self.db.execute("INSERT INTO log VALUES (?,?,?,?)", (time.time(), run_id, event, json.dumps(data or {})))
            self.db.commit()

    def events(self, run_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT event FROM log WHERE run_id=? ORDER BY at", (run_id,))]
