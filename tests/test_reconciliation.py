import pytest

from payresolve.db import connection, resolve_reconciliation_review
from payresolve.llm import LLMResult
from payresolve.repository import (
    fetch_order_ids_for_transaction, fetch_pending_reconciliations,
    fetch_reconciliation_history, fetch_reconciliation_logs,
)
from payresolve.service import investigate, reconcile_order
from scripts.seed_db import seed_database, seed_reconciliation_data


@pytest.fixture
def reconciliation_db(tmp_path, monkeypatch):
    database = tmp_path / "reconciliation.sqlite"
    seed_database(database, transaction_count=500)
    from payresolve.agents import investigation, resolution

    monkeypatch.setattr(
        investigation, "generate",
        lambda prompt: LLMResult(text=None, used=False, error="test fallback"),
    )
    monkeypatch.setattr(
        resolution, "generate",
        lambda prompt: LLMResult(text=None, used=False, error="test fallback"),
    )
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
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 500
    assert fetch_order_ids_for_transaction(
        "SHOP-TXN-PAID-UNPAID", reconciliation_db
    ) == ["SHOP-ORD-PAID-UNPAID"]


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


def test_single_transaction_investigation_checks_all_linked_records(reconciliation_db):
    report = investigate(
        "SHOP-TXN-PAID-UNPAID",
        "Customer asks about the payment and order.",
        persist=True,
        db_path=str(reconciliation_db),
    )

    assert report.transaction.found
    assert len(report.reconciliations) == 1
    reconciliation = report.reconciliations[0]
    assert reconciliation.order_id == "SHOP-ORD-PAID-UNPAID"
    assert reconciliation.reconciliation.status == "review_required"
    assert any("ShopKart order status is created" in item
               for item in reconciliation.reconciliation.discrepancies)
    assert report.resolution.requires_human_review
    assert report.resolution.recommendation == "Review linked merchant payment records"
    assert "Reconciliation Agent" in [event.agent for event in report.events]
    assert "Supervisor Agent" in [event.agent for event in report.events]
    assert report.dispute_id is not None
    assert reconciliation.run_id is not None
    with connection(reconciliation_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM disputes").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM reconciliation_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM investigation_logs").fetchone()[0] == len(report.events)


def test_single_transaction_investigation_reports_all_clear_when_linked_records_match(reconciliation_db):
    report = investigate(
        "SHOP-TXN-MATCHED",
        "Please check this payment.",
        persist=False,
        db_path=str(reconciliation_db),
    )

    assert report.transaction.found
    assert len(report.reconciliations) == 1
    assert report.reconciliations[0].reconciliation.status == "matched"
    assert not report.resolution.requires_human_review


def test_transaction_without_linked_order_is_explicitly_unverified(reconciliation_db):
    with connection(reconciliation_db) as conn:
        conn.execute(
            "DELETE FROM gateway_settlement_lines WHERE gateway_payment_id IN "
            "(SELECT gateway_payment_id FROM gateway_payments WHERE transaction_id='DEMO-SUCCESS-001')"
        )
        conn.execute("DELETE FROM gateway_payments WHERE transaction_id='DEMO-SUCCESS-001'")
        conn.execute("DELETE FROM orders WHERE order_id='ORD-DEMO-SUCCESS-001'")

    report = investigate(
        "DEMO-SUCCESS-001",
        "Please check this payment.",
        persist=False,
        db_path=str(reconciliation_db),
    )

    assert report.transaction.found
    assert report.reconciliations == []
    assert report.resolution.requires_human_review
    assert "No ShopKart order is linked" in report.resolution.review_reason
