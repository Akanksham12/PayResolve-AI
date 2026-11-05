import logging

from payresolve.db import initialize_database, record_dispute, log_agent_events, connection
from payresolve.graph import investigation_graph
from payresolve.schemas import DisputeInput, InvestigationReport

logger = logging.getLogger(__name__)


def ensure_database() -> None:
    initialize_database()
    with connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if count == 0:
        from scripts.seed_db import seed_database

        seed_database()


def investigate(transaction_id: str, complaint: str, claimed_amount: float | None = None,
                persist: bool = True) -> InvestigationReport:
    request = DisputeInput(
        transaction_id=transaction_id.strip(),
        complaint=complaint.strip(),
        claimed_amount=claimed_amount,
    )
    state = investigation_graph.invoke({
        "transaction_id": request.transaction_id,
        "complaint": request.complaint,
        "claimed_amount": request.claimed_amount,
        "transaction": None, "fraud": None, "investigation": None, "resolution": None,
        "events": [], "errors": [], "route": "complete",
    })
    report = InvestigationReport(
        transaction_id=request.transaction_id,
        transaction=state["transaction"],
        fraud=state["fraud"],
        investigation=state["investigation"],
        resolution=state["resolution"],
        events=state["events"],
        errors=state["errors"],
    )
    if persist:
        dispute_id = record_dispute(
            report.model_dump(), request.complaint, request.claimed_amount,
        )
        try:
            log_agent_events(dispute_id, [event.model_dump() for event in report.events])
        except Exception:
            logger.exception("Unable to persist investigation execution logs for dispute %s", dispute_id)
            raise
        report.dispute_id = dispute_id
    return report
