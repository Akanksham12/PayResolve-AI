from pathlib import Path

import pandas as pd

from payresolve.db import connection


def fetch_transaction(transaction_id: str, db_path: str | Path | None = None) -> dict | None:
    with connection(db_path) as conn:
        row = conn.execute(
            """SELECT t.*, c.name AS customer_name, c.email AS customer_email
            FROM transactions t JOIN customers c USING(customer_id)
            WHERE transaction_id=?""",
            (transaction_id.strip(),),
        ).fetchone()
        return dict(row) if row else None


def fetch_transaction_bundle(transaction_id: str, db_path: str | Path | None = None) -> dict:
    with connection(db_path) as conn:
        transaction = conn.execute(
            """SELECT t.*, c.name AS customer_name
            FROM transactions t JOIN customers c USING(customer_id)
            WHERE transaction_id=?""",
            (transaction_id.strip(),),
        ).fetchone()
        if not transaction:
            return {"transaction": None, "attempts": [], "refunds": [], "settlements": [], "related": []}
        tx = dict(transaction)
        attempts = conn.execute(
            "SELECT attempt_id,attempt_number,status,attempted_at,payment_method,amount FROM payment_attempts WHERE transaction_id=? ORDER BY attempt_number",
            (transaction_id.strip(),),
        ).fetchall()
        refunds = conn.execute(
            "SELECT refund_id,amount,status,requested_at,completed_at,reference FROM refunds WHERE transaction_id=?",
            (transaction_id.strip(),),
        ).fetchall()
        settlements = conn.execute(
            "SELECT settlement_id,amount,status,settled_at,batch_reference FROM settlements WHERE transaction_id=?",
            (transaction_id.strip(),),
        ).fetchall()
        related = conn.execute(
            """SELECT transaction_id,amount,status,created_at,payment_method
            FROM transactions
            WHERE customer_id=? AND transaction_id<>? AND ABS(amount-?) < 0.01
            AND datetime(created_at) BETWEEN datetime(?, '-10 minutes') AND datetime(?, '+10 minutes')
            ORDER BY created_at""",
            (tx["customer_id"], transaction_id.strip(), tx["amount"], tx["created_at"], tx["created_at"]),
        ).fetchall()
        return {
            "transaction": tx,
            "attempts": [dict(row) for row in attempts],
            "refunds": [dict(row) for row in refunds],
            "settlements": [dict(row) for row in settlements],
            "related": [dict(row) for row in related],
        }


def fetch_admin_metrics(db_path: str | Path | None = None) -> dict:
    with connection(db_path) as conn:
        dispute_count = conn.execute("SELECT COUNT(*) FROM disputes").fetchone()[0]
        failed_count = conn.execute("SELECT COUNT(*) FROM transactions WHERE status='failed'").fetchone()[0]
        high_risk_count = conn.execute(
            "SELECT COUNT(*) FROM disputes WHERE risk_level='high'"
        ).fetchone()[0]
        totals = {
            "disputes": dispute_count,
            "failed_transactions": failed_count,
            "high_risk": high_risk_count,
        }
        categories = pd.read_sql_query(
            "SELECT COALESCE(category,'unclassified') AS category, COUNT(*) AS count FROM disputes GROUP BY category ORDER BY count DESC",
            conn,
        )
        agents = pd.read_sql_query(
            """SELECT agent, COUNT(*) AS executions,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            AVG((julianday(completed_at)-julianday(started_at))*86400.0) AS avg_seconds
            FROM investigation_logs GROUP BY agent ORDER BY agent""",
            conn,
        )
        return {"totals": totals,
                "categories": categories, "agents": agents}


def fetch_history(db_path: str | Path | None = None) -> pd.DataFrame:
    with connection(db_path) as conn:
        return pd.read_sql_query(
            """SELECT d.dispute_id,d.created_at,d.transaction_id,d.category,d.outcome,
            d.risk_level,d.confidence,COALESCE(h.status,'not_required') AS human_review_status,
            h.reason AS human_review_reason
            FROM disputes d LEFT JOIN human_reviews h USING(dispute_id)
            ORDER BY d.created_at DESC""",
            conn,
        )


def fetch_pending_reviews(db_path: str | Path | None = None) -> list[dict]:
    with connection(db_path) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT h.dispute_id,h.reason,d.transaction_id,d.category,d.risk_level,d.outcome
            FROM human_reviews h JOIN disputes d USING(dispute_id)
            WHERE h.status='pending' ORDER BY h.created_at"""
        ).fetchall()]


def fetch_dispute_logs(dispute_id: int, db_path: str | Path | None = None) -> list[dict]:
    import json

    with connection(db_path) as conn:
        rows = conn.execute(
            "SELECT agent,status,started_at,completed_at,summary,tool_calls_json FROM investigation_logs WHERE dispute_id=? ORDER BY log_id",
            (dispute_id,),
        ).fetchall()
        return [
            {**dict(row), "tool_calls": json.loads(row["tool_calls_json"])}
            for row in rows
        ]
