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
        reconciliation_totals = dict(conn.execute(
            """SELECT COUNT(*) AS orders,
            SUM(CASE WHEN o.status='paid' THEN 1 ELSE 0 END) AS paid_orders,
            (SELECT COUNT(*) FROM gateway_payments) AS gateway_payments,
            (SELECT COUNT(*) FROM reconciliation_runs WHERE status='review_required') AS flagged_runs,
            (SELECT COUNT(*) FROM reconciliation_runs WHERE human_review_status='pending') AS pending_reviews
            FROM orders o"""
        ).fetchone())
        reconciliation_totals = {
            key: value or 0 for key, value in reconciliation_totals.items()
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
        reconciliation_agents = pd.read_sql_query(
            """SELECT agent, COUNT(*) AS executions,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            AVG((julianday(completed_at)-julianday(started_at))*86400.0) AS avg_seconds
            FROM reconciliation_logs GROUP BY agent ORDER BY agent""",
            conn,
        )
        return {
            "totals": totals,
            "categories": categories,
            "agents": agents,
            "reconciliation_totals": reconciliation_totals,
            "reconciliation_agents": reconciliation_agents,
        }


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


def fetch_reconciliation_bundle(order_id: str, db_path: str | Path | None = None) -> dict:
    with connection(db_path) as conn:
        order = conn.execute(
            """SELECT o.*,c.name AS customer_name
            FROM orders o JOIN customers c USING(customer_id) WHERE order_id=?""",
            (order_id.strip(),),
        ).fetchone()
        if not order:
            return {"order": None, "gateway_payments": [], "settlement_lines": [], "bank_entries": []}
        payments = conn.execute(
            """SELECT gateway_payment_id,transaction_id,gateway,provider_reference,amount,currency,
            status,payment_method,attempted_at,failure_reason
            FROM gateway_payments WHERE order_id=? ORDER BY attempted_at,gateway_payment_id""",
            (order_id.strip(),),
        ).fetchall()
        payment_ids = [row["gateway_payment_id"] for row in payments]
        if not payment_ids:
            return {"order": dict(order), "gateway_payments": [], "settlement_lines": [], "bank_entries": []}
        placeholders = ",".join("?" for _ in payment_ids)
        settlements = conn.execute(
            f"""SELECT settlement_line_id,gateway_payment_id,batch_reference,gross_amount,fee_amount,
            net_amount,status,settled_at FROM gateway_settlement_lines
            WHERE gateway_payment_id IN ({placeholders}) ORDER BY batch_reference""",
            payment_ids,
        ).fetchall()
        batches = sorted({row["batch_reference"] for row in settlements})
        batch_totals = []
        bank_entries = []
        if batches:
            batch_placeholders = ",".join("?" for _ in batches)
            batch_totals = conn.execute(
                f"""SELECT s.batch_reference,g.currency,SUM(s.net_amount) AS net_amount,
                COUNT(*) AS payment_count
                FROM gateway_settlement_lines s
                JOIN gateway_payments g USING(gateway_payment_id)
                WHERE s.batch_reference IN ({batch_placeholders}) AND s.status='settled'
                GROUP BY s.batch_reference,g.currency""",
                batches,
            ).fetchall()
            bank_entries = conn.execute(
                f"""SELECT bank_entry_id,batch_reference,amount,currency,credited_at,bank_reference
                FROM bank_statement_entries WHERE batch_reference IN ({batch_placeholders})
                ORDER BY credited_at""",
                batches,
            ).fetchall()
        return {
            "order": dict(order),
            "gateway_payments": [dict(row) for row in payments],
            "settlement_lines": [dict(row) for row in settlements],
            "settlement_batch_totals": [dict(row) for row in batch_totals],
            "bank_entries": [dict(row) for row in bank_entries],
        }


def fetch_order_ids_for_transaction(transaction_id: str, db_path: str | Path | None = None) -> list[str]:
    with connection(db_path) as conn:
        rows = conn.execute(
            """SELECT DISTINCT order_id FROM gateway_payments
            WHERE transaction_id=? ORDER BY order_id""",
            (transaction_id.strip(),),
        ).fetchall()
        return [row["order_id"] for row in rows]


def fetch_order_samples(db_path: str | Path | None = None, limit: int = 20) -> pd.DataFrame:
    with connection(db_path) as conn:
        return pd.read_sql_query(
            """SELECT o.order_id,o.status,o.amount,o.currency,o.created_at,c.name AS customer
            FROM orders o JOIN customers c USING(customer_id)
            ORDER BY CASE WHEN o.order_id LIKE 'SHOP-ORD-%' THEN 0 ELSE 1 END,o.created_at DESC
            LIMIT ?""",
            conn,
            params=(limit,),
        )


def fetch_reconciliation_history(db_path: str | Path | None = None) -> pd.DataFrame:
    with connection(db_path) as conn:
        return pd.read_sql_query(
            """SELECT run_id,created_at,order_id,status,recommendation,human_review_status,
            reviewer,decision_note FROM reconciliation_runs ORDER BY created_at DESC""",
            conn,
        )


def fetch_pending_reconciliations(db_path: str | Path | None = None) -> list[dict]:
    with connection(db_path) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT run_id,order_id,status,discrepancies_json,recommendation,created_at
            FROM reconciliation_runs WHERE human_review_status='pending' ORDER BY created_at"""
        ).fetchall()]


def fetch_reconciliation_logs(run_id: int, db_path: str | Path | None = None) -> list[dict]:
    import json

    with connection(db_path) as conn:
        rows = conn.execute(
            """SELECT agent,status,started_at,completed_at,summary,tool_calls_json
            FROM reconciliation_logs WHERE run_id=? ORDER BY log_id""",
            (run_id,),
        ).fetchall()
        return [
            {**dict(row), "tool_calls": json.loads(row["tool_calls_json"])}
            for row in rows
        ]
