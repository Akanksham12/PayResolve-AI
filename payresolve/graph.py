import logging
from datetime import datetime, timezone
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from payresolve.agents.fraud_detection import run_fraud_agent
from payresolve.agents.investigation import run_investigation_agent
from payresolve.agents.resolution import run_resolution_agent
from payresolve.agents.supervisor import select_route
from payresolve.agents.transaction import run_transaction_agent
from payresolve.schemas import (
    AgentEvent, FraudOutput, InvestigationOutput, ResolutionOutput, TransactionOutput,
)

logger = logging.getLogger(__name__)


class InvestigationState(TypedDict):
    transaction_id: str
    complaint: str
    claimed_amount: float | None
    transaction: TransactionOutput | None
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
        result = run_transaction_agent(state["transaction_id"], state["claimed_amount"])
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
        if not result.llm_used:
            llm_status = "unavailable"
        elif result.llm_draft_accepted:
            llm_status = "draft accepted after safety checks"
        else:
            llm_status = "draft rejected by safety checks; verified-record template used"
        return {
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
    workflow.add_node("fraud_agent", fraud_node)
    workflow.add_node("investigation_agent", investigation_node)
    workflow.add_node("resolution_agent", resolution_node)
    workflow.add_node("supervisor_agent", supervisor_node)
    workflow.add_node("human_review", human_review_node)
    workflow.add_edge(START, "transaction_agent")
    workflow.add_edge("transaction_agent", "fraud_agent")
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
