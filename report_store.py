"""Short-lived, session-scoped reports; no raw CSV files are saved."""
import json
from contextlib import contextmanager
from pathlib import Path
import secrets
import sqlite3
import time

REPORT_TTL = 3600
MAX_REPORTS = 100
SESSION_QUESTION_LIMIT = 10
GLOBAL_QUESTION_LIMIT = 100


class ReportStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS questions (owner TEXT NOT NULL, created REAL NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def prune(self, db, now):
        db.execute("DELETE FROM reports WHERE created <= ?", (now - REPORT_TTL,))
        db.execute("DELETE FROM questions WHERE created <= ?", (now - REPORT_TTL,))

    def save(self, owner, report):
        report_id = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.prune(db, time.time())
            db.execute("INSERT INTO reports VALUES (?, ?, ?, ?)",
                       (report_id, owner, json.dumps(report, allow_nan=False), time.time()))
            db.execute("DELETE FROM reports WHERE id IN (SELECT id FROM reports ORDER BY created DESC LIMIT -1 OFFSET ?)", (MAX_REPORTS,))
        return report_id

    def get(self, report_id, owner):
        with self.connect() as db:
            self.prune(db, time.time())
            row = db.execute("SELECT payload FROM reports WHERE id = ? AND owner = ?", (report_id, owner)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, report_id, owner):
        with self.connect() as db:
            db.execute("DELETE FROM reports WHERE id = ? AND owner = ?", (report_id, owner))

    def reserve_question(self, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.prune(db, time.time())
            total = db.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
            personal = db.execute("SELECT COUNT(*) FROM questions WHERE owner = ?", (owner,)).fetchone()[0]
            if total >= GLOBAL_QUESTION_LIMIT or personal >= SESSION_QUESTION_LIMIT:
                return False
            db.execute("INSERT INTO questions VALUES (?, ?)", (owner, time.time()))
        return True
