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
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    amount REAL NOT NULL CHECK(amount > 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('created','pending','paid','failed','cancelled','refunded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_payments (
    gateway_payment_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    transaction_id TEXT REFERENCES transactions(transaction_id),
    gateway TEXT NOT NULL,
    provider_reference TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('successful','failed','pending')),
    payment_method TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS gateway_settlement_lines (
    settlement_line_id TEXT PRIMARY KEY,
    gateway_payment_id TEXT NOT NULL REFERENCES gateway_payments(gateway_payment_id),
    batch_reference TEXT NOT NULL,
    gross_amount REAL NOT NULL,
    fee_amount REAL NOT NULL,
    net_amount REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','settled')),
    settled_at TEXT
);
CREATE TABLE IF NOT EXISTS bank_statement_entries (
    bank_entry_id TEXT PRIMARY KEY,
    batch_reference TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    credited_at TEXT NOT NULL,
    bank_reference TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('matched','review_required','not_found')),
    discrepancies_json TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    requires_human_review INTEGER NOT NULL CHECK(requires_human_review IN (0,1)),
    human_review_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK(human_review_status IN ('not_required','pending','approved','rejected')),
    reviewer TEXT,
    decision_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES reconciliation_runs(run_id),
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    tool_calls_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_customer ON transactions(customer_id);
CREATE INDEX IF NOT EXISTS idx_attempts_transaction ON payment_attempts(transaction_id);
CREATE INDEX IF NOT EXISTS idx_refunds_transaction ON refunds(transaction_id);
CREATE INDEX IF NOT EXISTS idx_settlements_transaction ON settlements(transaction_id);
CREATE INDEX IF NOT EXISTS idx_disputes_created ON disputes(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_gateway_payments_order ON gateway_payments(order_id);
CREATE INDEX IF NOT EXISTS idx_gateway_payments_transaction ON gateway_payments(transaction_id);
CREATE INDEX IF NOT EXISTS idx_gateway_settlements_batch ON gateway_settlement_lines(batch_reference);
CREATE INDEX IF NOT EXISTS idx_bank_statement_batch ON bank_statement_entries(batch_reference);
CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_created ON reconciliation_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_reconciliation_logs_run ON reconciliation_logs(run_id);
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


def record_reconciliation_run(report: dict, events: list[dict] | None = None,
                              db_path: str | Path | None = None) -> int:
    now = utc_now()
    reconciliation = report["reconciliation"]
    resolution = report["resolution"]
    requires_review = resolution["requires_human_review"]
    with connection(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO reconciliation_runs
            (order_id,status,discrepancies_json,recommendation,requires_human_review,
             human_review_status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                report["order_id"], reconciliation["status"], json.dumps(reconciliation["discrepancies"]),
                resolution["recommendation"], int(requires_review),
                "pending" if requires_review else "not_required", now, now,
            ),
        )
        run_id = int(cursor.lastrowid)
        if events:
            conn.executemany(
                """INSERT INTO reconciliation_logs
                (run_id,agent,status,started_at,completed_at,summary,tool_calls_json)
                VALUES (?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, event["agent"], event["status"], event["started_at"],
                        event["completed_at"], event["summary"],
                        json.dumps(event.get("tool_calls", [])),
                    )
                    for event in events
                ],
            )
        return run_id


def resolve_reconciliation_review(run_id: int, decision: str, reviewer: str, note: str,
                                  db_path: str | Path | None = None) -> None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("Decision must be approved or rejected")
    with connection(db_path) as conn:
        cursor = conn.execute(
            """UPDATE reconciliation_runs
            SET human_review_status=?,reviewer=?,decision_note=?,updated_at=?
            WHERE run_id=? AND human_review_status='pending'""",
            (decision, reviewer, note, utc_now(), run_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("No pending reconciliation review exists for this run")
