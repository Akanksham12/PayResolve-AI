import logging
from datetime import datetime, timezone
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from payresolve.agents.fraud_detection import run_fraud_agent
from payresolve.agents.investigation import run_investigation_agent
from payresolve.agents.resolution import run_resolution_agent
from payresolve.agents.supervisor import select_route
from payresolve.agents.transaction import run_transaction_agent
from payresolve.reconciliation_graph import reconciliation_graph
from payresolve.repository import fetch_order_ids_for_transaction
from payresolve.schemas import (
    AgentEvent, FraudOutput, InvestigationOutput, ReconciliationOutput,
    ReconciliationReport, ResolutionOutput, TransactionOutput,
)

logger = logging.getLogger(__name__)


class InvestigationState(TypedDict):
    transaction_id: str
    complaint: str
    claimed_amount: float | None
    db_path: str | None
    transaction: TransactionOutput | None
    reconciliations: list[ReconciliationReport]
    merchant_lookup_note: str | None
    fraud: FraudOutput | None
    investigation: InvestigationOutput | None
    resolution: ResolutionOutput | None
    events: list[dict]
    errors: list[str]
    route: Literal["complete", "human_review"]


def _time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_event(state: InvestigationState, agent: str, started: str, status: str,
                  summary: str, tool_calls: list[dict] | None = None) -> list[dict]:
    event = AgentEvent(
        agent=agent, status=status, started_at=started, completed_at=_time(),
        summary=summary, tool_calls=tool_calls or [],
    )
    return [*state["events"], event.model_dump()]


def transaction_node(state: InvestigationState) -> dict:
    started = _time()
    try:
        result = run_transaction_agent(
            state["transaction_id"], state["claimed_amount"], state.get("db_path")
        )
        status = "completed" if result.found else "warning"
        summary = f"Found transaction; {len(result.attempts)} attempts, {len(result.refunds)} refunds, and {len(result.settlements)} settlements." if result.found else result.error or "Transaction not found."
        calls = [{"tool": "sqlite.transaction_lookup", "input": {"transaction_id": state["transaction_id"]},
                  "result": "found" if result.found else "not_found"},
                 {"tool": "sqlite.related_records", "result": {
                     "attempts": len(result.attempts), "refunds": len(result.refunds),
                     "settlements": len(result.settlements), "nearby_transactions": len(result.related_transactions),
                 }}]
        errors = state["errors"]
        if result.error:
            errors = [*errors, result.error]
        if result.data_conflicts:
            status = "warning"
        return {
            "transaction": result, "errors": errors,
            "events": _append_event(state, "Transaction Agent", started, status, summary, calls),
        }
    except Exception as exc:
        logger.exception("Transaction agent failed")
        message = f"Transaction agent failed: {exc}"
        return {
            "transaction": TransactionOutput(found=False, error=message),
            "errors": [*state["errors"], message],
            "events": _append_event(state, "Transaction Agent", started, "failed", message),
        }


def merchant_reconciliation_node(state: InvestigationState) -> dict:
    started = _time()
    transaction = state["transaction"]
    db_path = state.get("db_path")
    if not transaction or not transaction.found:
        note = "Merchant records could not be checked because the transaction was not found."
        return {
            "reconciliations": [],
            "merchant_lookup_note": note,
            "events": _append_event(
                state, "Merchant Reconciliation Lookup", started, "warning", note,
                [{"tool": "sqlite.gateway_order_link_lookup", "result": "skipped_transaction_not_found"}],
            ),
        }
    try:
        order_ids = fetch_order_ids_for_transaction(state["transaction_id"], db_path)
        if not order_ids:
            note = (
                "No ShopKart order is linked to this transaction ID in the available ledger; "
                "merchant order reconciliation could not be verified."
            )
            return {
                "reconciliations": [],
                "merchant_lookup_note": note,
                "events": _append_event(
                    state, "Merchant Reconciliation Lookup", started, "warning", note,
                    [{"tool": "sqlite.gateway_order_link_lookup",
                      "input": {"transaction_id": state["transaction_id"]},
                      "result": "no_linked_order"}],
                ),
            }

        reports = []
        events = []
        errors = []
        for order_id in order_ids:
            result = reconciliation_graph.invoke({
                "order_id": order_id,
                "db_path": db_path,
                "reconciliation": None,
                "resolution": None,
                "events": [],
                "errors": [],
                "route": "complete",
            })
            report = ReconciliationReport(
                order_id=order_id,
                reconciliation=result["reconciliation"],
                resolution=result["resolution"],
                events=result["events"],
                errors=result["errors"],
            )
            reports.append(report)
            events.extend(result["events"])
            errors.extend(result["errors"])

        summary = (
            f"Reconciled {len(order_ids)} ShopKart order(s) linked to transaction {state['transaction_id']}."
        )
        return {
            "reconciliations": reports,
            "merchant_lookup_note": None,
            "errors": [*state["errors"], *errors],
            "events": _append_event(
                state, "Merchant Reconciliation Lookup", started,
                "warning" if errors else "completed", summary,
                [{"tool": "sqlite.gateway_order_link_lookup",
                  "input": {"transaction_id": state["transaction_id"]},
                  "result": {"linked_orders": order_ids}}],
            ) + events,
        }
    except Exception as exc:
        logger.exception("Merchant reconciliation lookup failed for %s", state["transaction_id"])
        message = f"Merchant reconciliation lookup failed: {exc}"
        return {
            "reconciliations": [],
            "merchant_lookup_note": message,
            "errors": [*state["errors"], message],
            "events": _append_event(
                state, "Merchant Reconciliation Lookup", started, "failed", message,
                [{"tool": "sqlite.gateway_order_link_lookup", "result": "failed"}],
            ),
        }


def fraud_node(state: InvestigationState) -> dict:
    started = _time()
    try:
        transaction = state["transaction"] or TransactionOutput(found=False, error="Transaction agent returned no result.")
        result = run_fraud_agent(transaction)
        return {
            "fraud": result,
            "events": _append_event(
                state, "Fraud Detection Agent", started, "completed",
                f"Risk assessment: {result.risk_level} ({result.risk_score}/100); {len(result.evidence)} evidence item(s).",
                [{"tool": "rules.repeated_attempts", "result": "evaluated"},
                 {"tool": "rules.duplicate_pattern", "result": "evaluated"},
                 {"tool": "rules.record_consistency", "result": "evaluated"}],
            ),
        }
    except Exception as exc:
        logger.exception("Fraud detection agent failed")
        message = f"Fraud detection agent failed: {exc}"
        return {
            "fraud": FraudOutput(risk_level="unknown", risk_score=0, classification_note=message),
            "errors": [*state["errors"], message],
            "events": _append_event(state, "Fraud Detection Agent", started, "failed", message),
        }


def investigation_node(state: InvestigationState) -> dict:
    started = _time()
    try:
        transaction = state["transaction"] or TransactionOutput(found=False)
        fraud = state["fraud"] or FraudOutput(
            risk_level="unknown", risk_score=0, classification_note="Risk assessment unavailable."
        )
        result, llm_used = run_investigation_agent(state["complaint"], transaction, fraud)
        return {
            "investigation": result,
            "events": _append_event(
                state, "Investigation Agent", started, "completed",
                f"Classified as {result.category}; confidence {result.confidence:.0%}. "
                f"{'Ollama inference used.' if llm_used else 'Rule-based fallback used; Ollama unavailable.'}",
                [{"tool": "ollama.chat", "result": "completed" if llm_used else "unavailable"},
                 {"tool": "evidence.reconcile", "result": f"{len(result.confirmed_facts)} facts; {len(result.hypotheses)} hypotheses"}],
            ),
        }
    except Exception as exc:
        logger.exception("Investigation agent failed")
        message = f"Investigation agent failed: {exc}"
        return {
            "investigation": InvestigationOutput(
                category="other", summary="Investigation could not be completed.",
                hypotheses=[message], confidence=0.0,
            ),
            "errors": [*state["errors"], message],
            "events": _append_event(state, "Investigation Agent", started, "failed", message),
        }


def resolution_node(state: InvestigationState) -> dict:
    started = _time()
    try:
        transaction = state["transaction"] or TransactionOutput(found=False)
        fraud = state["fraud"] or FraudOutput(
            risk_level="unknown", risk_score=0, classification_note="Risk assessment unavailable."
        )
        investigation = state["investigation"] or InvestigationOutput(
            category="other", summary="Investigation unavailable.", confidence=0.0
        )
        result = run_resolution_agent(state["complaint"], transaction, fraud, investigation)
        reconciliation_reports = state.get("reconciliations", [])
        reconciliation_issues = [
            discrepancy
            for report in reconciliation_reports
            for discrepancy in report.reconciliation.discrepancies
        ]
        if len(reconciliation_reports) > 1:
            reconciliation_issues.append(
                f"Transaction is linked to {len(reconciliation_reports)} ShopKart orders; support must verify the intended order."
            )
        if state.get("merchant_lookup_note") and transaction.found:
            reconciliation_issues.append(state["merchant_lookup_note"])
        if reconciliation_issues:
            reason_parts = [result.review_reason] if result.review_reason else []
            reason_parts.extend(reconciliation_issues)
            result = result.model_copy(update={
                "recommendation": (
                    result.recommendation
                    if result.recommendation == "Reconcile missing settlement record"
                    else "Review linked merchant payment records"
                ),
                "next_steps": [
                    *result.next_steps,
                    "Verify the ShopKart order, gateway, settlement, and bank-statement evidence in the reconciliation report.",
                ],
                "customer_response": (
                    "Thanks for contacting us. We found a discrepancy between the linked merchant and payment "
                    "records. A support specialist must verify the source records before advising on the order "
                    "or payment. No order or financial record has been changed."
                ),
                "requires_human_review": True,
                "review_reason": "; ".join(reason_parts),
            })
            investigation = investigation.model_copy(update={
                "summary": investigation.summary + " Linked merchant records also require reconciliation review.",
                "confirmed_facts": [
                    *investigation.confirmed_facts,
                    *[
                        evidence
                        for report in reconciliation_reports
                        for evidence in report.reconciliation.evidence
                    ],
                    *reconciliation_issues,
                ],
                "evidence": [
                    *investigation.evidence,
                    *[
                        evidence
                        for report in reconciliation_reports
                        for evidence in report.reconciliation.evidence
                    ],
                ],
            })
        if not result.llm_used:
            llm_status = "unavailable"
        elif result.llm_draft_accepted:
            llm_status = "draft accepted after safety checks"
        else:
            llm_status = "draft rejected by safety checks; verified-record template used"
        return {
            "investigation": investigation,
            "resolution": result,
            "events": _append_event(
                state, "Resolution Agent", started, "completed",
                f"Recommendation prepared: {result.recommendation}. "
                f"{'Human review required.' if result.requires_human_review else 'No mandatory review flag.'} "
                f"Ollama: {llm_status}.",
                [{"tool": "resolution.policy_rules", "result": "evaluated"},
                 {"tool": "ollama.chat", "result": llm_status}],
            ),
        }
    except Exception as exc:
        logger.exception("Resolution agent failed")
        message = f"Resolution agent failed: {exc}"
        return {
            "resolution": ResolutionOutput(
                recommendation="Manual review required",
                next_steps=["Route this case to support for manual investigation."],
                customer_response="We are unable to complete the investigation automatically. A support specialist must review this case.",
                requires_human_review=True, review_reason=message,
            ),
            "errors": [*state["errors"], message],
            "events": _append_event(state, "Resolution Agent", started, "failed", message),
        }


def supervisor_node(state: InvestigationState) -> dict:
    started = _time()
    transaction = state["transaction"] or TransactionOutput(found=False)
    investigation = state["investigation"] or InvestigationOutput(
        category="other", summary="Investigation unavailable.", confidence=0.0
    )
    resolution = state["resolution"] or ResolutionOutput(
        recommendation="Manual review required", customer_response="A support specialist must review this case.",
        requires_human_review=True, review_reason="Agent results are incomplete.",
    )
    route = select_route(transaction, investigation, resolution, state["errors"])
    status = "warning" if route == "human_review" else "completed"
    summary = (
        "Required agent results are present; case routed for human review."
        if route == "human_review" else
        "Agent results are consistent; case is ready for support response."
    )
    return {
        "route": route,
        "events": _append_event(
            state, "Supervisor Agent", started, status, summary,
            [{"tool": "workflow.review_router", "result": route},
             {"tool": "workflow.failure_check", "result": f"{len(state['errors'])} error(s)"}],
        ),
    }


def human_review_node(state: InvestigationState) -> dict:
    started = _time()
    reason = (
        (state["resolution"].review_reason if state["resolution"] else None)
        or "Supervisor routed the case for human review."
    )
    return {
        "events": _append_event(
            state, "Human Review Queue", started, "warning",
            f"Case flagged for a support specialist: {reason}",
            [{"tool": "review_queue.enqueue", "result": "pending"}],
        ),
    }


def build_graph():
    workflow = StateGraph(InvestigationState)
    workflow.add_node("transaction_agent", transaction_node)
    workflow.add_node("merchant_reconciliation", merchant_reconciliation_node)
    workflow.add_node("fraud_agent", fraud_node)
    workflow.add_node("investigation_agent", investigation_node)
    workflow.add_node("resolution_agent", resolution_node)
    workflow.add_node("supervisor_agent", supervisor_node)
    workflow.add_node("human_review", human_review_node)
    workflow.add_edge(START, "transaction_agent")
    workflow.add_edge("transaction_agent", "merchant_reconciliation")
    workflow.add_edge("merchant_reconciliation", "fraud_agent")
    workflow.add_edge("fraud_agent", "investigation_agent")
    workflow.add_edge("investigation_agent", "resolution_agent")
    workflow.add_edge("resolution_agent", "supervisor_agent")
    workflow.add_conditional_edges(
        "supervisor_agent",
        lambda state: state["route"],
        {"complete": END, "human_review": "human_review"},
    )
    workflow.add_edge("human_review", END)
    return workflow.compile()


investigation_graph = build_graph()
