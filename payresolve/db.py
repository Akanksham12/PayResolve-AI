import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from payresolve.config import DB_PATH

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    amount REAL NOT NULL CHECK(amount > 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('successful','failed','pending')),
    payment_method TEXT NOT NULL,
    created_at TEXT NOT NULL,
    failure_reason TEXT,
    merchant_reference TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('successful','failed','pending')),
    attempted_at TEXT NOT NULL,
    payment_method TEXT NOT NULL,
    amount REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    amount REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    reference TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
    amount REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','settled')),
    settled_at TEXT,
    batch_reference TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes (
    dispute_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL,
    complaint TEXT NOT NULL,
    claimed_amount REAL,
    category TEXT,
    outcome TEXT,
    risk_level TEXT,
    confidence REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id INTEGER NOT NULL REFERENCES disputes(dispute_id),
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    tool_calls_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS human_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispute_id INTEGER NOT NULL UNIQUE REFERENCES disputes(dispute_id),
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
    reason TEXT NOT NULL,
    reviewer TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_customer ON transactions(customer_id);
CREATE INDEX IF NOT EXISTS idx_attempts_transaction ON payment_attempts(transaction_id);
CREATE INDEX IF NOT EXISTS idx_refunds_transaction ON refunds(transaction_id);
CREATE INDEX IF NOT EXISTS idx_settlements_transaction ON settlements(transaction_id);
CREATE INDEX IF NOT EXISTS idx_disputes_created ON disputes(created_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database(db_path: str | Path | None = None) -> None:
    with connection(db_path) as conn:
        conn.executescript(SCHEMA)


def log_agent_events(dispute_id: int, events: list[dict], db_path: str | Path | None = None) -> None:
    with connection(db_path) as conn:
        conn.executemany(
            """INSERT INTO investigation_logs
            (dispute_id,agent,status,started_at,completed_at,summary,tool_calls_json)
            VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    dispute_id, event["agent"], event["status"], event["started_at"],
                    event["completed_at"], event["summary"],
                    json.dumps(event.get("tool_calls", [])),
                )
                for event in events
            ],
        )


def record_dispute(report: dict, complaint: str, claimed_amount: float | None,
                   db_path: str | Path | None = None) -> int:
    now = utc_now()
    with connection(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO disputes
            (transaction_id,complaint,claimed_amount,category,outcome,risk_level,confidence,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                report["transaction_id"], complaint, claimed_amount,
                report["investigation"]["category"], report["resolution"]["recommendation"],
                report["fraud"]["risk_level"], report["investigation"]["confidence"], now,
            ),
        )
        dispute_id = int(cursor.lastrowid)
        if report["resolution"]["requires_human_review"]:
            conn.execute(
                """INSERT INTO human_reviews
                (dispute_id,status,reason,created_at,updated_at) VALUES (?,?,?,?,?)""",
                (dispute_id, "pending", report["resolution"]["review_reason"] or "Review required", now, now),
            )
        return dispute_id


def resolve_human_review(dispute_id: int, decision: str, reviewer: str, note: str,
                         db_path: str | Path | None = None) -> None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Decision must be approved or rejected")
    with connection(db_path) as conn:
        cursor = conn.execute(
            """UPDATE human_reviews
            SET status=?,reviewer=?,decision_note=?,updated_at=?
            WHERE dispute_id=? AND status='pending'""",
            (decision, reviewer, note, utc_now(), dispute_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("No pending human review exists for this dispute")
