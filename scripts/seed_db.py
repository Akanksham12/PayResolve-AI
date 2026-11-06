import argparse
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from payresolve.db import connection, initialize_database


def _insert_transaction(conn, tx_id, customer_id, amount, status, method, created_at,
                        attempt_statuses=None, failure_reason=None, settlement=True,
                        refund=None):
    conn.execute(
        """INSERT INTO transactions
        (transaction_id,customer_id,amount,currency,status,payment_method,created_at,failure_reason,merchant_reference)
        VALUES (?,?,?,'INR',?,?,?,?,?)""",
        (tx_id, customer_id, amount, status, method, created_at, failure_reason, f"MR-{tx_id}"),
    )
    for i, attempt_status in enumerate(attempt_statuses or [status], 1):
        conn.execute(
            """INSERT INTO payment_attempts
            (transaction_id,attempt_number,status,attempted_at,payment_method,amount)
            VALUES (?,?,?,?,?,?)""",
            (tx_id, i, attempt_status, created_at, method, amount),
        )
    if settlement:
        conn.execute(
            "INSERT INTO settlements VALUES (?,?,?,?,?,?)",
            (f"SET-{tx_id}", tx_id, amount, "settled", created_at, f"BATCH-{created_at[:10]}"),
        )
    if refund:
        refund_status, refund_amount = refund
        completed_at = created_at if refund_status == "completed" else None
        conn.execute(
            "INSERT INTO refunds VALUES (?,?,?,?,?,?,?)",
            (f"REF-{tx_id}", tx_id, refund_amount, refund_status, created_at, completed_at, f"RREF-{tx_id}"),
        )


def _insert_gateway_payment(conn, order_id, gateway_payment_id, transaction_id,
                            amount, currency, status, method, attempted_at,
                            provider_reference, failure_reason=None):
    conn.execute(
        """INSERT INTO gateway_payments
        (gateway_payment_id,order_id,transaction_id,gateway,provider_reference,
         amount,currency,status,payment_method,attempted_at,failure_reason)
        VALUES (?,?,?,'Razorpay (synthetic)',?,?,?,?,?,?,?)""",
        (
            gateway_payment_id, order_id, transaction_id, provider_reference,
            amount, currency, status, method, attempted_at, failure_reason,
        ),
    )


def _insert_settlement_line(conn, payment_id, batch_reference, amount, settled_at):
    fee = round(amount * 0.02, 2)
    conn.execute(
        """INSERT INTO gateway_settlement_lines
        (settlement_line_id,gateway_payment_id,batch_reference,gross_amount,fee_amount,
         net_amount,status,settled_at)
        VALUES (?,?,?,?,?,?,'settled',?)""",
        (f"GWS-{payment_id}", payment_id, batch_reference, amount, fee, round(amount - fee, 2), settled_at),
    )


def _ensure_reconciliation_demo_transactions(conn) -> list[tuple]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    created_at = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR IGNORE INTO customers (customer_id,name,email,created_at) VALUES (?,?,?,?)",
        ("CUST-DEMO-01", "Alex Sample", "cust-demo-01@example.test", now.isoformat()),
    )
    demos = [
        ("SHOP-ORD-PAID-UNPAID", "SHOP-TXN-PAID-UNPAID", 50000.00, "successful", True),
        ("SHOP-ORD-AMOUNT-MISMATCH", "SHOP-TXN-AMOUNT-MISMATCH", 49900.00, "successful", True),
        ("SHOP-ORD-PAID-GATEWAY-FAILED", "SHOP-TXN-PAID-GATEWAY-FAILED", 1200.00, "failed", False),
        ("SHOP-ORD-MISSING-SETTLEMENT", "SHOP-TXN-MISSING-SETTLEMENT", 799.00, "successful", False),
        ("SHOP-ORD-MISSING-BANK-CREDIT", "SHOP-TXN-MISSING-BANK-CREDIT", 899.00, "successful", True),
        ("SHOP-ORD-MATCHED", "SHOP-TXN-MATCHED", 2500.00, "successful", True),
    ]
    for _, transaction_id, amount, status, settlement in demos:
        exists = conn.execute(
            "SELECT 1 FROM transactions WHERE transaction_id=?", (transaction_id,)
        ).fetchone()
        if exists:
            continue
        _insert_transaction(
            conn,
            transaction_id,
            "CUST-DEMO-01",
            amount,
            status,
            "UPI",
            created_at,
            [status],
            "Issuer declined" if status == "failed" else None,
            settlement,
        )
    return [
        (order_id, transaction_id, amount, status, created_at)
        for order_id, transaction_id, amount, status, _ in demos
    ]


def _seed_reconciliation_data(conn) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payout_totals = defaultdict(float)
    payout_dates = {}
    transactions = conn.execute("SELECT * FROM transactions ORDER BY transaction_id").fetchall()
    existing_settlements = {
        row["transaction_id"]: row
        for row in conn.execute("SELECT * FROM settlements").fetchall()
    }

    for row in transactions:
        tx = dict(row)
        if tx["transaction_id"].startswith("SHOP-TXN-"):
            continue
        order_id = f"ORD-{tx['transaction_id']}"
        order_status = {
            "successful": "paid",
            "failed": "failed",
            "pending": "pending",
        }[tx["status"]]
        conn.execute(
            """INSERT INTO orders
            (order_id,customer_id,amount,currency,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)""",
            (
                order_id, tx["customer_id"], tx["amount"], tx["currency"],
                order_status, tx["created_at"], tx["created_at"],
            ),
        )
        attempts = conn.execute(
            "SELECT * FROM payment_attempts WHERE transaction_id=? ORDER BY attempt_number",
            (tx["transaction_id"],),
        ).fetchall()
        for attempt in attempts:
            payment_id = f"GWP-{tx['transaction_id']}-{attempt['attempt_number']}"
            provider_reference = f"RZPREF-{tx['transaction_id']}-{attempt['attempt_number']}"
            _insert_gateway_payment(
                conn, order_id, payment_id, tx["transaction_id"], attempt["amount"],
                tx["currency"], attempt["status"], attempt["payment_method"],
                attempt["attempted_at"], provider_reference,
                tx["failure_reason"] if attempt["status"] == "failed" else None,
            )
            if attempt["status"] == "successful" and tx["transaction_id"] in existing_settlements:
                date_key = attempt["attempted_at"][:10]
                batch = f"RZP-BATCH-{date_key}"
                _insert_settlement_line(
                    conn, payment_id, batch, attempt["amount"],
                    existing_settlements[tx["transaction_id"]]["settled_at"],
                )
                payout_totals[batch] += round(attempt["amount"] - round(attempt["amount"] * 0.02, 2), 2)
                payout_dates[batch] = existing_settlements[tx["transaction_id"]]["settled_at"] or attempt["attempted_at"]

    _ensure_reconciliation_demo_transactions(conn)
    customer_id = "CUST-DEMO-01"
    demo_time = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    demo_orders = [
        ("SHOP-ORD-PAID-UNPAID", "SHOP-TXN-PAID-UNPAID", 50000.00, "created", 50000.00, "successful", True, True),
        ("SHOP-ORD-AMOUNT-MISMATCH", "SHOP-TXN-AMOUNT-MISMATCH", 50000.00, "paid", 49900.00, "successful", True, True),
        ("SHOP-ORD-PAID-GATEWAY-FAILED", "SHOP-TXN-PAID-GATEWAY-FAILED", 1200.00, "paid", 1200.00, "failed", False, False),
        ("SHOP-ORD-MISSING-SETTLEMENT", "SHOP-TXN-MISSING-SETTLEMENT", 799.00, "paid", 799.00, "successful", False, False),
        ("SHOP-ORD-MISSING-BANK-CREDIT", "SHOP-TXN-MISSING-BANK-CREDIT", 899.00, "paid", 899.00, "successful", True, False),
        ("SHOP-ORD-MATCHED", "SHOP-TXN-MATCHED", 2500.00, "paid", 2500.00, "successful", True, True),
    ]
    for order_id, transaction_id, order_amount, order_status, payment_amount, payment_status, settled, bank_credited in demo_orders:
        conn.execute(
            """INSERT INTO orders
            (order_id,customer_id,amount,currency,status,created_at,updated_at)
            VALUES (?,?,?,'INR',?,?,?)""",
            (order_id, customer_id, order_amount, order_status, demo_time, demo_time),
        )
        payment_id = f"GWP-{order_id}"
        batch = f"RZP-BATCH-{order_id}"
        _insert_gateway_payment(
            conn, order_id, payment_id, transaction_id, payment_amount, "INR",
            payment_status, "UPI", demo_time, f"RZPREF-{order_id}",
            "Issuer declined" if payment_status == "failed" else None,
        )
        if settled:
            _insert_settlement_line(conn, payment_id, batch, payment_amount, demo_time)
            net = round(payment_amount - round(payment_amount * 0.02, 2), 2)
            payout_totals[batch] = net
            payout_dates[batch] = demo_time
        if bank_credited:
            amount = payout_totals.get(batch)
            if amount is not None:
                conn.execute(
                    """INSERT INTO bank_statement_entries
                    (bank_entry_id,batch_reference,amount,currency,credited_at,bank_reference)
                    VALUES (?,?,?,?,?,?)""",
                    (f"BANK-{order_id}", batch, amount, "INR", demo_time, f"BNKREF-{order_id}"),
                )

    for batch, amount in payout_totals.items():
        if batch.startswith("RZP-BATCH-SHOP-ORD-"):
            continue
        conn.execute(
            """INSERT INTO bank_statement_entries
            (bank_entry_id,batch_reference,amount,currency,credited_at,bank_reference)
            VALUES (?,?,?,?,?,?)""",
            (f"BANK-{batch}", batch, round(amount, 2), "INR", payout_dates[batch], f"BNKREF-{batch}"),
        )


def seed_reconciliation_data(db_path: str | Path | None = None) -> int:
    """Backfill synthetic ShopKart/gateway records for an existing transaction-only database."""
    with connection(db_path) as conn:
        if conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0:
            return 0
        demos = _ensure_reconciliation_demo_transactions(conn)
        if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
            conn.executemany(
                "UPDATE gateway_payments SET transaction_id=? WHERE order_id=? AND transaction_id IS NULL",
                [(transaction_id, order_id) for order_id, transaction_id, *_ in demos],
            )
            return 0
        _seed_reconciliation_data(conn)
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def seed_database(db_path: str | Path | None = None, transaction_count: int = 750, seed: int = 2026) -> int:
    if not 500 <= transaction_count <= 1000:
        raise ValueError("transaction_count must be between 500 and 1000")
    random_count = transaction_count - 16
    initialize_database(db_path)
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    statuses = ["successful", "successful", "successful", "failed", "pending"]
    methods = ["UPI", "card", "netbanking", "wallet"]
    with connection(db_path) as conn:
        conn.execute("DELETE FROM reconciliation_logs")
        conn.execute("DELETE FROM reconciliation_runs")
        conn.execute("DELETE FROM bank_statement_entries")
        conn.execute("DELETE FROM gateway_settlement_lines")
        conn.execute("DELETE FROM gateway_payments")
        conn.execute("DELETE FROM orders")
        conn.execute("DELETE FROM human_reviews")
        conn.execute("DELETE FROM investigation_logs")
        conn.execute("DELETE FROM disputes")
        conn.execute("DELETE FROM settlements")
        conn.execute("DELETE FROM refunds")
        conn.execute("DELETE FROM payment_attempts")
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM customers")

        for i in range(1, random_count + 1):
            customer_id = f"CUST-{i:04d}"
            conn.execute(
                "INSERT INTO customers VALUES (?,?,?,?)",
                (customer_id, f"Customer {i:04d}", f"customer{i:04d}@example.test", now.isoformat()),
            )
            status = rng.choice(statuses)
            created = (now - timedelta(days=rng.randrange(180), minutes=rng.randrange(1440))).isoformat(timespec="seconds")
            attempt_count = rng.choices([1, 2, 3, 5], weights=[70, 20, 8, 2])[0]
            attempts = ["failed"] * max(0, attempt_count - 1) + [status]
            has_settlement = status == "successful" and rng.random() > 0.08
            refund = None
            if status == "successful" and rng.random() < 0.08:
                refund = (rng.choice(["pending", "completed"]), round(rng.uniform(10, 5000), 2))
            _insert_transaction(
                conn, f"TXN-{i:06d}", customer_id, round(rng.uniform(10, 50000), 2),
                status, rng.choice(methods), created, attempts,
                "Issuer declined" if status == "failed" else None, has_settlement, refund,
            )

        special_customers = [
            ("CUST-DEMO-01", "Alex Sample"),
            ("CUST-DEMO-02", "Jordan Sample"),
            ("CUST-DEMO-03", "Casey Sample"),
            ("CUST-DEMO-04", "Taylor Sample"),
            ("CUST-DEMO-05", "Morgan Sample"),
            ("CUST-DEMO-06", "Riley Sample"),
            ("CUST-DEMO-07", "Sam Sample"),
            ("CUST-DEMO-08", "Jamie Sample"),
            ("CUST-DEMO-09", "Avery Sample"),
        ]
        for customer_id, name in special_customers:
            conn.execute(
                "INSERT INTO customers VALUES (?,?,?,?)",
                (customer_id, name, f"{customer_id.lower()}@example.test", now.isoformat()),
            )
        base = now - timedelta(hours=2)
        stamp = lambda mins=0: (base + timedelta(minutes=mins)).isoformat(timespec="seconds")
        demo = [
            ("DEMO-SUCCESS-001", "CUST-DEMO-01", 1250.00, "successful", "UPI", stamp(), ["successful"], None, True, None),
            ("DEMO-FAILED-001", "CUST-DEMO-02", 799.00, "failed", "card", stamp(1), ["failed"], "Issuer declined", False, None),
            ("DEMO-DEBIT-FAILED-001", "CUST-DEMO-03", 1499.00, "failed", "UPI", stamp(2), ["failed"], "Issuer timeout", False, None),
            ("DEMO-DUPLICATE-001", "CUST-DEMO-04", 499.00, "successful", "UPI", stamp(3), ["failed", "successful"], None, True, None),
            ("DEMO-DUPLICATE-002", "CUST-DEMO-04", 499.00, "successful", "UPI", stamp(5), ["successful"], None, True, None),
            ("DEMO-REFUND-PENDING", "CUST-DEMO-05", 2300.00, "successful", "card", stamp(6), ["successful"], None, True, ("pending", 2300.00)),
            ("DEMO-REFUND-DONE", "CUST-DEMO-06", 825.00, "successful", "netbanking", stamp(7), ["successful"], None, True, ("completed", 825.00)),
            ("DEMO-SUSPICIOUS-001", "CUST-DEMO-07", 5600.00, "failed", "card", stamp(8), ["failed"] * 6, "Repeated issuer declines", False, None),
            ("DEMO-NO-SETTLEMENT-001", "CUST-DEMO-08", 3400.00, "successful", "wallet", stamp(9), ["successful"], None, False, None),
            ("DEMO-CONTRADICTORY-001", "CUST-DEMO-09", 915.00, "failed", "UPI", stamp(10), ["successful"], "Issuer declined", True, None),
        ]
        for row in demo:
            _insert_transaction(conn, *row)
        _seed_reconciliation_data(conn)
    return transaction_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Recreate PayResolve synthetic SQLite data.")
    parser.add_argument("--db", type=Path, default=None, help="Optional SQLite database path.")
    parser.add_argument(
        "--count", type=int, default=750,
        help="Total synthetic transactions, including 10 dispute and 6 reconciliation cases (500-1000).",
    )
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    total = seed_database(args.db, args.count, args.seed)
    print(
        f"Seeded {total} transactions (including 10 dispute and 6 ShopKart reconciliation cases) "
        f"into {args.db or 'configured database'}."
    )


if __name__ == "__main__":
    main()
