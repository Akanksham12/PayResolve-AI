import logging
from datetime import datetime, timezone
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from payresolve.agents.reconciliation import run_reconciliation_agent
from payresolve.schemas import (
    AgentEvent, ReconciliationOutput, ReconciliationResolution,
)

logger = logging.getLogger(__name__)


class ReconciliationState(TypedDict):
    order_id: str
    db_path: str | None
    reconciliation: ReconciliationOutput | None
    resolution: ReconciliationResolution | None
    events: list[dict]
    errors: list[str]
    route: Literal["complete", "human_review"]


def _time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _add_event(state: ReconciliationState, agent: str, started: str, status: str,
               summary: str, tool_calls: list[dict]) -> list[dict]:
    event = AgentEvent(
        agent=agent, status=status, started_at=started, completed_at=_time(),
        summary=summary, tool_calls=tool_calls,
    )
    return [*state["events"], event.model_dump()]


def reconciliation_node(state: ReconciliationState) -> dict:
    started = _time()
    try:
        result = run_reconciliation_agent(state["order_id"], state["db_path"])
        status = "completed" if result.status == "matched" else "warning"
        summary = (
            "Order, gateway, settlement and bank records match."
            if result.status == "matched" else
            f"Order reconciliation found {len(result.discrepancies)} discrepancy/discrepancies."
        )
        return {
            "reconciliation": result,
            "events": _add_event(
                state, "Reconciliation Agent", started, status, summary,
                [
                    {"tool": "sqlite.order_lookup", "input": {"order_id": state["order_id"]},
                     "result": "found" if result.order_found else "not_found"},
                    {"tool": "sqlite.gateway_payment_lookup", "result": f"{len(result.gateway_payments)} attempt(s)"},
                    {"tool": "sqlite.settlement_lookup", "result": f"{len(result.settlement_lines)} line(s)"},
                    {"tool": "sqlite.bank_statement_lookup", "result": f"{len(result.bank_entries)} credit(s)"},
                ],
            ),
        }
    except Exception as exc:
        logger.exception("Reconciliation agent failed for order %s", state["order_id"])
        message = f"Reconciliation agent failed: {exc}"
        result = ReconciliationOutput(
            order_id=state["order_id"], order_found=False, status="not_found",
            discrepancies=[message], evidence=[],
        )
        return {
            "reconciliation": result,
            "errors": [*state["errors"], message],
            "events": _add_event(
                state, "Reconciliation Agent", started, "failed", message,
                [{"tool": "sqlite.order_lookup", "result": "failed"}],
            ),
        }


def resolution_node(state: ReconciliationState) -> dict:
    started = _time()
    result = state["reconciliation"] or ReconciliationOutput(
        order_id=state["order_id"], order_found=False, status="not_found",
        discrepancies=["Reconciliation result is unavailable."],
    )
    if not result.order_found:
        recommendation = "Request a valid ShopKart order reference"
        next_steps = ["Confirm the order ID with the customer and rerun reconciliation."]
        review_reason = "Order record could not be verified."
    elif not result.discrepancies:
        recommendation = "No reconciliation action required"
        next_steps = ["Share the matched order, gateway, settlement, and bank-credit records with support."]
        review_reason = None
    else:
        recommendation = "Review payment reconciliation discrepancies"
        next_steps = [
            "Compare the order status and amount with the listed gateway payment references.",
            "Verify any missing or differing settlement and bank-statement records with ShopKart operations.",
            "Correct ShopKart records only after an authorized support executive confirms the source evidence.",
        ]
        review_reason = f"{len(result.discrepancies)} order/payment/settlement discrepancy/discrepancies require review."
    requires_review = not result.order_found or bool(result.discrepancies) or bool(state["errors"])
    output = ReconciliationResolution(
        recommendation=recommendation,
        next_steps=next_steps,
        support_summary=(
            "No discrepancies were found in the available synthetic records."
            if result.order_found and not result.discrepancies else
            "The available records show a possible mismatch. Verify the listed source records before changing an order or responding to the customer."
        ),
        requires_human_review=requires_review,
        review_reason=review_reason if requires_review else None,
    )
    return {
        "resolution": output,
        "events": _add_event(
            state, "Support Resolution Agent", started, "warning" if requires_review else "completed",
            f"{output.recommendation}. {'Human review required.' if requires_review else 'No action needed.'}",
            [{"tool": "reconciliation.recommendation_rules", "result": output.recommendation}],
        ),
    }


def supervisor_node(state: ReconciliationState) -> dict:
    started = _time()
    resolution = state["resolution"]
    route = (
        "human_review"
        if state["errors"] or resolution is None or resolution.requires_human_review
        else "complete"
    )
    summary = (
        "Reconciliation is consistent and ready to share with support."
        if route == "complete" else "Case routed to the reconciliation human-review queue."
    )
    return {
        "route": route,
        "events": _add_event(
            state, "Reconciliation Supervisor", started,
            "warning" if route == "human_review" else "completed", summary,
            [{"tool": "workflow.review_router", "result": route},
             {"tool": "workflow.failure_check", "result": f"{len(state['errors'])} error(s)"}],
        ),
    }


def human_review_node(state: ReconciliationState) -> dict:
    started = _time()
    reason = (
        state["resolution"].review_reason
        if state["resolution"] and state["resolution"].review_reason
        else "Supervisor routed the reconciliation for review."
    )
    return {
        "events": _add_event(
            state, "Reconciliation Human Review Queue", started, "warning",
            reason, [{"tool": "review_queue.enqueue", "result": "pending"}],
        ),
    }


def build_reconciliation_graph():
    workflow = StateGraph(ReconciliationState)
    workflow.add_node("reconciliation_agent", reconciliation_node)
    workflow.add_node("resolution_agent", resolution_node)
    workflow.add_node("supervisor_agent", supervisor_node)
    workflow.add_node("human_review", human_review_node)
    workflow.add_edge(START, "reconciliation_agent")
    workflow.add_edge("reconciliation_agent", "resolution_agent")
    workflow.add_edge("resolution_agent", "supervisor_agent")
    workflow.add_conditional_edges(
        "supervisor_agent",
        lambda state: state["route"],
        {"complete": END, "human_review": "human_review"},
    )
    workflow.add_edge("human_review", END)
    return workflow.compile()


reconciliation_graph = build_reconciliation_graph()
