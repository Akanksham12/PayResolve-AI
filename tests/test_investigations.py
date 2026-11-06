import pytest

from payresolve.graph import investigation_graph
from payresolve.llm import LLMResult
from payresolve.schemas import DisputeInput
from scripts.seed_db import seed_database


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    database = tmp_path / "test.sqlite"
    seed_database(database, transaction_count=500)
    from payresolve.agents import transaction, investigation, resolution

    original = transaction.fetch_transaction_bundle
    monkeypatch.setattr(
        transaction, "fetch_transaction_bundle",
        lambda tx_id, db_path=None: original(tx_id, database),
    )
    monkeypatch.setattr(investigation, "generate", lambda prompt: LLMResult(text=None, used=False, error="test fallback"))
    monkeypatch.setattr(resolution, "generate", lambda prompt: LLMResult(text=None, used=False, error="test fallback"))
    return database


def run_case(transaction_id, complaint, claimed_amount=None, db_path=None):
    request = DisputeInput(
        transaction_id=transaction_id, complaint=complaint, claimed_amount=claimed_amount
    )
    return investigation_graph.invoke({
        "transaction_id": request.transaction_id,
        "complaint": request.complaint,
        "claimed_amount": request.claimed_amount,
        "db_path": str(db_path) if db_path else None,
        "transaction": None, "fraud": None, "investigation": None, "resolution": None,
        "reconciliations": [], "merchant_lookup_note": None,
        "events": [], "errors": [], "route": "complete",
    })


def test_successful_payment(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "I want to know whether my payment succeeded.", db_path=seeded_db)
    assert result["transaction"].found
    assert result["transaction"].transaction["status"] == "successful"
    assert result["route"] == "complete"


def test_failed_payment(seeded_db):
    result = run_case("DEMO-FAILED-001", "My payment failed.", db_path=seeded_db)
    assert result["transaction"].transaction["status"] == "failed"
    assert result["resolution"].recommendation == "Verify any reported debit with the payment provider"


def test_customer_reports_debit_but_payment_failed(seeded_db):
    result = run_case("DEMO-DEBIT-FAILED-001", "My bank account was debited but payment failed.", db_path=seeded_db)
    assert result["transaction"].transaction["status"] == "failed"
    assert any("bank-side debit confirmation" in item for item in result["investigation"].customer_reported_conflicts)
    assert result["resolution"].requires_human_review
    assert result["resolution"].recommendation == "Review customer report against payment records"
    assert "do not verify activity in your bank account" in result["resolution"].customer_response


def test_customer_reports_pending_but_transaction_is_successful(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "This transaction is pending.", db_path=seeded_db)

    assert result["transaction"].transaction["status"] == "successful"
    assert any("reports the payment as pending" in item
               for item in result["investigation"].customer_reported_conflicts)
    assert result["resolution"].requires_human_review
    assert result["resolution"].recommendation == "Review customer report against payment records"
    assert result["route"] == "human_review"


def test_customer_reports_failed_but_transaction_is_successful(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "The payment failed in my app.", db_path=seeded_db)

    assert any("reports the payment as failed" in item
               for item in result["investigation"].customer_reported_conflicts)
    assert result["resolution"].requires_human_review


def test_customer_reports_success_but_transaction_is_failed(seeded_db):
    result = run_case("DEMO-FAILED-001", "The payment was successful but my order is missing.", db_path=seeded_db)

    assert any("reports the payment as successful" in item
               for item in result["investigation"].customer_reported_conflicts)
    assert result["resolution"].requires_human_review


def test_customer_says_payment_not_successful_but_record_is_successful(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "My payment was not successful.", db_path=seeded_db)

    assert any("reports the payment as failed" in item
               for item in result["investigation"].customer_reported_conflicts)
    assert result["resolution"].requires_human_review


def test_reported_unsettled_payment_without_settlement_requires_review(seeded_db):
    result = run_case("DEMO-NO-SETTLEMENT-001", "My payment settlement is pending.", db_path=seeded_db)

    assert any("no settlement record is available" in item
               for item in result["investigation"].customer_reported_conflicts)
    assert result["resolution"].requires_human_review


def test_settled_record_is_flagged_when_customer_reports_settlement_pending(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "My payment settlement is pending.", db_path=seeded_db)

    assert any("settlement records are marked settled" in item
               for item in result["investigation"].customer_reported_conflicts)
    assert result["resolution"].requires_human_review


def test_duplicate_transaction_pattern(seeded_db):
    result = run_case("DEMO-DUPLICATE-001", "I was charged twice for this payment.", db_path=seeded_db)
    assert result["transaction"].related_transactions
    assert result["fraud"].risk_level in {"medium", "high"}
    assert result["resolution"].recommendation == "Review possible duplicate payment"
    assert result["resolution"].requires_human_review


def test_unverified_duplicate_complaint_with_typo_requires_review(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "i was charged twicw", db_path=seeded_db)
    assert result["investigation"].category == "duplicate_payment"
    assert not result["transaction"].related_transactions
    assert result["fraud"].risk_level == "low"
    assert result["investigation"].confidence < 0.6
    assert any("second successful transaction was not found" in item for item in result["investigation"].hypotheses)
    assert result["resolution"].recommendation == "Verify the unconfirmed duplicate-charge report"
    assert result["resolution"].requires_human_review
    assert result["route"] == "human_review"
    assert "could not verify a second" in result["resolution"].customer_response
    assert "confirmed a duplicate charge" in result["resolution"].customer_response


def test_refund_pending(seeded_db):
    result = run_case("DEMO-REFUND-PENDING", "Where is my refund?", db_path=seeded_db)
    assert result["transaction"].refunds[0]["status"] == "pending"
    assert result["resolution"].recommendation == "Monitor the pending refund record"


def test_refund_completed(seeded_db):
    result = run_case("DEMO-REFUND-DONE", "I have not received my refund.", db_path=seeded_db)
    assert result["transaction"].refunds[0]["status"] == "completed"
    assert result["resolution"].recommendation == "Review customer report against payment records"
    assert result["resolution"].requires_human_review


def test_missing_transaction_id(seeded_db):
    result = run_case("DOES-NOT-EXIST", "Please investigate this payment.", db_path=seeded_db)
    assert not result["transaction"].found
    assert result["route"] == "human_review"
    assert result["resolution"].requires_human_review


def test_suspicious_repeated_attempts(seeded_db):
    result = run_case("DEMO-SUSPICIOUS-001", "Please check this transaction.", db_path=seeded_db)
    assert result["fraud"].risk_level == "high"
    assert result["fraud"].risk_score >= 65
    assert result["route"] == "human_review"
    assert "not a finding of fraud" in result["fraud"].classification_note


def test_missing_settlement_record(seeded_db):
    result = run_case("DEMO-NO-SETTLEMENT-001", "My payment has not settled.", db_path=seeded_db)
    assert not result["transaction"].settlements
    assert result["resolution"].recommendation == "Reconcile missing settlement record"
    assert any("unverified" in item for item in result["transaction"].evidence)


def test_contradictory_payment_information(seeded_db):
    result = run_case("DEMO-CONTRADICTORY-001", "The payment was successful but the app says failed.", db_path=seeded_db)
    assert result["transaction"].data_conflicts
    assert result["fraud"].risk_level == "medium"
    assert result["route"] == "human_review"


def test_claimed_amount_conflict_is_reviewed(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "I need help with this payment.", 9999.0, db_path=seeded_db)
    assert result["transaction"].data_conflicts
    assert result["resolution"].requires_human_review


def test_unsupported_llm_refund_claim_is_rejected(seeded_db, monkeypatch):
    from payresolve.agents import resolution

    monkeypatch.setattr(
        resolution, "generate",
        lambda prompt: LLMResult(text="Your refund has been initiated.", used=True),
    )
    result = run_case("DEMO-REFUND-PENDING", "Where is my refund?", db_path=seeded_db)
    assert result["resolution"].llm_used
    assert not result["resolution"].llm_draft_accepted
    assert "Your refund has been initiated." not in result["resolution"].customer_response
