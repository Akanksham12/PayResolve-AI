import pytest

from payresolve.db import connection, resolve_reconciliation_review
from payresolve.repository import (
    fetch_pending_reconciliations, fetch_reconciliation_history, fetch_reconciliation_logs,
)
from payresolve.service import reconcile_order
from scripts.seed_db import seed_database, seed_reconciliation_data


@pytest.fixture
def reconciliation_db(tmp_path):
    database = tmp_path / "reconciliation.sqlite"
    seed_database(database, transaction_count=500)
    return database


def test_matched_order_has_no_discrepancies_and_no_review(reconciliation_db):
    report = reconcile_order("SHOP-ORD-MATCHED", db_path=reconciliation_db)

    assert report.reconciliation.status == "matched"
    assert report.reconciliation.discrepancies == []
    assert report.resolution.recommendation == "No reconciliation action required"
    assert not report.resolution.requires_human_review
    assert report.run_id is not None
    assert len(report.events) == 3


def test_random_settlement_batch_is_compared_using_all_batch_lines(reconciliation_db):
    with connection(reconciliation_db) as conn:
        order_id = conn.execute(
            """SELECT o.order_id FROM orders o
            JOIN gateway_payments g USING(order_id)
            JOIN gateway_settlement_lines s USING(gateway_payment_id)
            WHERE o.order_id LIKE 'ORD-TXN-%' AND o.status='paid'
            LIMIT 1"""
        ).fetchone()["order_id"]

    report = reconcile_order(order_id, db_path=reconciliation_db)

    assert report.reconciliation.status == "matched"
    assert report.reconciliation.discrepancies == []


def test_bank_credit_amount_mismatch_is_flagged(reconciliation_db):
    with connection(reconciliation_db) as conn:
        conn.execute(
            """UPDATE bank_statement_entries SET amount=amount+100
            WHERE batch_reference='RZP-BATCH-SHOP-ORD-MATCHED'"""
        )

    report = reconcile_order("SHOP-ORD-MATCHED", db_path=reconciliation_db, persist=False)

    assert any("but gateway settlement net totals" in item for item in report.reconciliation.discrepancies)
    assert report.resolution.requires_human_review


def test_transaction_only_database_can_be_backfilled_without_reseeding(reconciliation_db):
    with connection(reconciliation_db) as conn:
        conn.execute("DELETE FROM bank_statement_entries")
        conn.execute("DELETE FROM gateway_settlement_lines")
        conn.execute("DELETE FROM gateway_payments")
        conn.execute("DELETE FROM orders")

    assert seed_reconciliation_data(reconciliation_db) == 500
    assert seed_reconciliation_data(reconciliation_db) == 0
    with connection(reconciliation_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 500
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 506


def test_successful_gateway_payment_with_unpaid_order_is_flagged(reconciliation_db):
    report = reconcile_order("SHOP-ORD-PAID-UNPAID", db_path=reconciliation_db)

    assert any("ShopKart order status is created" in item for item in report.reconciliation.discrepancies)
    assert report.resolution.requires_human_review
    assert report.events[-1].agent == "Reconciliation Human Review Queue"


def test_gateway_order_amount_mismatch_is_flagged(reconciliation_db):
    report = reconcile_order("SHOP-ORD-AMOUNT-MISMATCH", db_path=reconciliation_db)

    assert any("does not match the ShopKart order amount" in item for item in report.reconciliation.discrepancies)
    assert report.reconciliation.gateway_payments[0]["amount"] == 49900.0
    assert report.reconciliation.order["amount"] == 50000.0


def test_paid_order_without_successful_gateway_payment_is_flagged(reconciliation_db):
    report = reconcile_order("SHOP-ORD-PAID-GATEWAY-FAILED", db_path=reconciliation_db)

    assert any("no successful gateway payment" in item for item in report.reconciliation.discrepancies)
    assert report.resolution.requires_human_review


def test_successful_payment_without_settlement_line_is_flagged(reconciliation_db):
    report = reconcile_order("SHOP-ORD-MISSING-SETTLEMENT", db_path=reconciliation_db)

    assert any("No gateway settlement line" in item for item in report.reconciliation.discrepancies)


def test_settlement_without_bank_credit_is_flagged(reconciliation_db):
    report = reconcile_order("SHOP-ORD-MISSING-BANK-CREDIT", db_path=reconciliation_db)

    assert any("No ShopKart bank-statement credit" in item for item in report.reconciliation.discrepancies)


def test_unknown_order_is_not_found_and_routed_for_review(reconciliation_db):
    report = reconcile_order("UNKNOWN-ORDER", db_path=reconciliation_db)

    assert report.reconciliation.status == "not_found"
    assert not report.reconciliation.order_found
    assert report.resolution.requires_human_review
    assert report.resolution.recommendation == "Request a valid ShopKart order reference"


def test_reconciliation_history_logs_and_human_decision_are_persisted(reconciliation_db):
    report = reconcile_order("SHOP-ORD-PAID-UNPAID", db_path=reconciliation_db)

    assert report.run_id is not None
    assert len(fetch_reconciliation_logs(report.run_id, reconciliation_db)) == 4
    assert len(fetch_pending_reconciliations(reconciliation_db)) == 1
    assert len(fetch_reconciliation_history(reconciliation_db)) == 1

    resolve_reconciliation_review(
        report.run_id, "approved", "Support Executive", "Order corrected after source-system verification.",
        reconciliation_db,
    )
    with connection(reconciliation_db) as conn:
        row = conn.execute(
            "SELECT human_review_status,reviewer,decision_note FROM reconciliation_runs WHERE run_id=?",
            (report.run_id,),
        ).fetchone()
    assert tuple(row) == (
        "approved", "Support Executive", "Order corrected after source-system verification."
    )
    assert fetch_pending_reconciliations(reconciliation_db) == []
