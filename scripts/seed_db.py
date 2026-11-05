import argparse
import random
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


def seed_database(db_path: str | Path | None = None, transaction_count: int = 750, seed: int = 2026) -> int:
    if not 500 <= transaction_count <= 1000:
        raise ValueError("transaction_count must be between 500 and 1000")
    random_count = transaction_count - 10
    initialize_database(db_path)
    rng = random.Random(seed)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    statuses = ["successful", "successful", "successful", "failed", "pending"]
    methods = ["UPI", "card", "netbanking", "wallet"]
    with connection(db_path) as conn:
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
    return transaction_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Recreate PayResolve synthetic SQLite data.")
    parser.add_argument("--db", type=Path, default=None, help="Optional SQLite database path.")
    parser.add_argument("--count", type=int, default=750, help="Total synthetic transactions, including demo cases (500-1000).")
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args()
    total = seed_database(args.db, args.count, args.seed)
    print(f"Seeded {total} transactions (including 10 demo cases) into {args.db or 'configured database'}.")


if __name__ == "__main__":
    main()
