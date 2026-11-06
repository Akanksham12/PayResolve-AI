import logging

from payresolve.db import (
    connection, initialize_database, record_dispute, log_agent_events,
    record_reconciliation_run,
)
from payresolve.graph import investigation_graph
from payresolve.reconciliation_graph import reconciliation_graph
from payresolve.schemas import (
    DisputeInput, InvestigationReport, ReconciliationReport,
    ReconciliationRequest,
)

logger = logging.getLogger(__name__)


def ensure_database() -> None:
    initialize_database()
    with connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    if count == 0:
        from scripts.seed_db import seed_database

        seed_database()
    else:
        from scripts.seed_db import seed_reconciliation_data

        seed_reconciliation_data()


def investigate(transaction_id: str, complaint: str, claimed_amount: float | None = None,
                persist: bool = True, db_path: str | None = None) -> InvestigationReport:
    request = DisputeInput(
        transaction_id=transaction_id.strip(),
        complaint=complaint.strip(),
        claimed_amount=claimed_amount,
    )
    state = investigation_graph.invoke({
        "transaction_id": request.transaction_id,
        "complaint": request.complaint,
        "claimed_amount": request.claimed_amount,
        "db_path": str(db_path) if db_path is not None else None,
        "transaction": None, "reconciliations": [], "merchant_lookup_note": None,
        "fraud": None, "investigation": None, "resolution": None,
        "events": [], "errors": [], "route": "complete",
    })
    reconciliation_reports = []
    if persist:
        for result in state.get("reconciliations", []):
            run_id = record_reconciliation_run(
                result.model_dump(),
                [event.model_dump() for event in result.events],
                db_path,
            )
            reconciliation_reports.append(result.model_copy(update={"run_id": run_id}))
    else:
        reconciliation_reports = state.get("reconciliations", [])
    report = InvestigationReport(
        transaction_id=request.transaction_id,
        transaction=state["transaction"],
        fraud=state["fraud"],
        investigation=state["investigation"],
        resolution=state["resolution"],
        reconciliations=reconciliation_reports,
        events=state["events"],
        errors=state["errors"],
    )
    if persist:
        dispute_id = record_dispute(
            report.model_dump(), request.complaint, request.claimed_amount, db_path,
        )
        try:
            log_agent_events(dispute_id, [event.model_dump() for event in report.events], db_path)
        except Exception:
            logger.exception("Unable to persist investigation execution logs for dispute %s", dispute_id)
            raise
        report.dispute_id = dispute_id
    return report


def reconcile_order(order_id: str, persist: bool = True,
                    db_path: str | None = None) -> ReconciliationReport:
    request = ReconciliationRequest(order_id=order_id.strip())
    state = reconciliation_graph.invoke({
        "order_id": request.order_id,
        "db_path": str(db_path) if db_path is not None else None,
        "reconciliation": None,
        "resolution": None,
        "events": [],
        "errors": [],
        "route": "complete",
    })
    report = ReconciliationReport(
        order_id=request.order_id,
        reconciliation=state["reconciliation"],
        resolution=state["resolution"],
        events=state["events"],
        errors=state["errors"],
    )
    if persist:
        report.run_id = record_reconciliation_run(
            report.model_dump(),
            [event.model_dump() for event in report.events],
            db_path,
        )
    return report
