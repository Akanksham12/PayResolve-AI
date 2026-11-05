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


def run_case(transaction_id, complaint, claimed_amount=None):
    request = DisputeInput(
        transaction_id=transaction_id, complaint=complaint, claimed_amount=claimed_amount
    )
    return investigation_graph.invoke({
        "transaction_id": request.transaction_id,
        "complaint": request.complaint,
        "claimed_amount": request.claimed_amount,
        "transaction": None, "fraud": None, "investigation": None, "resolution": None,
        "events": [], "errors": [], "route": "complete",
    })


def test_successful_payment(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "I want to know whether my payment succeeded.")
    assert result["transaction"].found
    assert result["transaction"].transaction["status"] == "successful"
    assert result["route"] == "complete"


def test_failed_payment(seeded_db):
    result = run_case("DEMO-FAILED-001", "My payment failed.")
    assert result["transaction"].transaction["status"] == "failed"
    assert result["resolution"].recommendation == "Verify any reported debit with the payment provider"


def test_customer_reports_debit_but_payment_failed(seeded_db):
    result = run_case("DEMO-DEBIT-FAILED-001", "My bank account was debited but payment failed.")
    assert result["transaction"].transaction["status"] == "failed"
    assert any("temporary bank authorization" in item for item in result["investigation"].hypotheses)
    assert result["resolution"].requires_human_review is False


def test_duplicate_transaction_pattern(seeded_db):
    result = run_case("DEMO-DUPLICATE-001", "I was charged twice for this payment.")
    assert result["transaction"].related_transactions
    assert result["fraud"].risk_level in {"medium", "high"}
    assert result["resolution"].recommendation == "Review possible duplicate payment"


def test_refund_pending(seeded_db):
    result = run_case("DEMO-REFUND-PENDING", "Where is my refund?")
    assert result["transaction"].refunds[0]["status"] == "pending"
    assert result["resolution"].recommendation == "Monitor the pending refund record"


def test_refund_completed(seeded_db):
    result = run_case("DEMO-REFUND-DONE", "I have not received my refund.")
    assert result["transaction"].refunds[0]["status"] == "completed"
    assert result["resolution"].recommendation == "Share the verified refund reference"


def test_missing_transaction_id(seeded_db):
    result = run_case("DOES-NOT-EXIST", "Please investigate this payment.")
    assert not result["transaction"].found
    assert result["route"] == "human_review"
    assert result["resolution"].requires_human_review


def test_suspicious_repeated_attempts(seeded_db):
    result = run_case("DEMO-SUSPICIOUS-001", "Please check this transaction.")
    assert result["fraud"].risk_level == "high"
    assert result["fraud"].risk_score >= 65
    assert result["route"] == "human_review"
    assert "not a finding of fraud" in result["fraud"].classification_note


def test_missing_settlement_record(seeded_db):
    result = run_case("DEMO-NO-SETTLEMENT-001", "My payment has not settled.")
    assert not result["transaction"].settlements
    assert result["resolution"].recommendation == "Reconcile missing settlement record"
    assert any("unverified" in item for item in result["transaction"].evidence)


def test_contradictory_payment_information(seeded_db):
    result = run_case("DEMO-CONTRADICTORY-001", "The payment was successful but the app says failed.")
    assert result["transaction"].data_conflicts
    assert result["fraud"].risk_level == "medium"
    assert result["route"] == "human_review"


def test_claimed_amount_conflict_is_reviewed(seeded_db):
    result = run_case("DEMO-SUCCESS-001", "I need help with this payment.", 9999.0)
    assert result["transaction"].data_conflicts
    assert result["resolution"].requires_human_review


def test_unsupported_llm_refund_claim_is_rejected(seeded_db, monkeypatch):
    from payresolve.agents import resolution

    monkeypatch.setattr(
        resolution, "generate",
        lambda prompt: LLMResult(text="Your refund has been initiated.", used=True),
    )
    result = run_case("DEMO-REFUND-PENDING", "Where is my refund?")
    assert result["resolution"].llm_used
    assert not result["resolution"].llm_draft_accepted
    assert "Your refund has been initiated." not in result["resolution"].customer_response
