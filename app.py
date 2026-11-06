import json

import pandas as pd
import streamlit as st

from payresolve.db import resolve_human_review, resolve_reconciliation_review
from payresolve.logging_config import configure_logging
from payresolve.repository import (
    fetch_admin_metrics, fetch_dispute_logs, fetch_history, fetch_pending_reviews,
    fetch_reconciliation_history, fetch_reconciliation_logs,
    fetch_pending_reconciliations,
)
from payresolve.service import ensure_database, investigate

configure_logging()
st.set_page_config(page_title="PayResolve AI", page_icon="◈", layout="wide")

st.markdown(
    """
    <style>
    .stApp { background: #f5f7fb; }
    .block-container { padding-top: 1.6rem; }
    [data-testid="stMetric"] { background: white; border: 1px solid #e3e9f2;
        padding: 16px 18px; border-radius: 12px; }
    .brand { color: #172554; font-weight: 800; letter-spacing: -.04em; font-size: 1.75rem; }
    .muted { color: #64748b; }
    </style>
    """,
    unsafe_allow_html=True,
)

try:
    ensure_database()
except Exception as exc:
    st.error(f"Could not initialize the local database: {exc}")
    st.stop()

with st.sidebar:
    st.markdown('<div class="brand">◈ PayResolve AI</div>', unsafe_allow_html=True)
    st.caption("Local payment dispute investigation")
    page = st.radio(
        "Workspace",
        [
            "Customer Support", "Agent Execution", "Investigation Report",
            "Admin Dashboard", "Investigation History",
        ],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("Synthetic portfolio data · No live payments")
    st.caption("LLM: Ollama / qwen2.5:7b")


def display_report(report: dict) -> None:
    transaction = report["transaction"]
    fraud = report["fraud"]
    investigation = report["investigation"]
    resolution = report["resolution"]
    if report.get("dispute_id"):
        st.caption(f"Investigation #{report['dispute_id']} · Transaction {report['transaction_id']}")
    if report.get("errors"):
        for error in report["errors"]:
            st.warning(error)
    cols = st.columns(4)
    cols[0].metric("Transaction", "Found" if transaction["found"] else "Not found")
    cols[1].metric("Payment status", transaction["transaction"]["status"].title() if transaction["found"] else "Unknown")
    cols[2].metric("Risk level", fraud["risk_level"].title(), f"Score {fraud['risk_score']}/100")
    cols[3].metric("Confidence", f"{investigation['confidence']:.0%}")
    if resolution["requires_human_review"]:
        st.warning(f"Human review required: {resolution['review_reason'] or 'Supervisor escalation.'}")
    else:
        st.success("No mandatory human-review flag. Support should still verify any customer-reported bank debit.")

    left, right = st.columns([1.25, 1])
    with left:
        st.subheader("Investigation findings")
        st.write(investigation["summary"])
        st.markdown("**Confirmed database facts**")
        if investigation["confirmed_facts"]:
            for fact in investigation["confirmed_facts"]:
                st.markdown(f"- {fact}")
        else:
            st.caption("No confirmed facts were recorded.")
        if investigation["hypotheses"]:
            st.markdown("**Possible explanations (unconfirmed)**")
            for hypothesis in investigation["hypotheses"]:
                st.markdown(f"- {hypothesis}")
    with right:
        st.subheader("Resolution")
        st.markdown(f"**Recommendation:** {resolution['recommendation']}")
        for step in resolution["next_steps"]:
            st.markdown(f"- {step}")
        st.markdown("**Customer response draft**")
        st.info(resolution["customer_response"])
        st.caption(
            "Draft only: no payment, refund, transfer, or other financial action is executed by this app."
        )

    with st.expander("Transaction records and evidence"):
        if transaction["found"]:
            st.dataframe(pd.DataFrame([transaction["transaction"]]), width="stretch", hide_index=True)
        else:
            st.info(transaction["error"] or "No transaction data.")
        tabs = st.tabs(["Payment attempts", "Refunds", "Settlements", "Related payments", "Risk evidence"])
        datasets = [
            transaction["attempts"], transaction["refunds"], transaction["settlements"],
            transaction["related_transactions"], fraud["evidence"],
        ]
        for tab, data in zip(tabs[:4], datasets[:4]):
            with tab:
                st.dataframe(pd.DataFrame(data), width="stretch", hide_index=True) if data else st.caption("No records.")
        with tabs[4]:
            for item in fraud["evidence"]:
                st.markdown(f"- {item}")
            st.caption(fraud["classification_note"])
        if transaction["data_conflicts"]:
            st.error("Conflicting records: " + " ".join(transaction["data_conflicts"]))
    st.divider()
    st.subheader("Linked ShopKart / gateway reconciliation")
    reconciliations = report.get("reconciliations", [])
    if reconciliations:
        for reconciliation in reconciliations:
            display_reconciliation_report(reconciliation)
    elif report["transaction"]["found"]:
        st.info(
            "No linked ShopKart order was found for this transaction in the demo ledger. "
            "Merchant order status could not be checked."
        )
    else:
        st.caption("Merchant records could not be checked because the transaction ID was not found.")


def display_reconciliation_report(report: dict) -> None:
    reconciliation = report["reconciliation"]
    resolution = report["resolution"]
    st.caption(f"Reconciliation run #{report['run_id']} · ShopKart order {report['order_id']}")
    if reconciliation["status"] == "matched":
        st.success("Order, gateway, settlement, and available bank-statement records match.")
    elif reconciliation["status"] == "not_found":
        st.error(f"No ShopKart order was found for {report['order_id']}.")
    else:
        st.warning(
            f"{len(reconciliation['discrepancies'])} discrepancy/discrepancies found. "
            "A support executive must verify source records before changing an order."
        )
    if report["errors"]:
        for error in report["errors"]:
            st.error(error)

    order = reconciliation["order"]
    if order:
        columns = st.columns(4)
        columns[0].metric("ShopKart order", order["status"].title())
        columns[1].metric("Order amount", f"{order['currency']} {order['amount']:.2f}")
        columns[2].metric("Gateway attempts", len(reconciliation["gateway_payments"]))
        columns[3].metric("Result", reconciliation["status"].replace("_", " ").title())

    st.subheader("Support recommendation")
    st.markdown(f"**{resolution['recommendation']}**")
    st.write(resolution["support_summary"])
    for step in resolution["next_steps"]:
        st.markdown(f"- {step}")
    if resolution["requires_human_review"]:
        st.warning(f"Human review required: {resolution['review_reason']}")

    if reconciliation["discrepancies"]:
        st.subheader("Discrepancies to verify")
        for discrepancy in reconciliation["discrepancies"]:
            st.markdown(f"- {discrepancy}")
    if reconciliation["evidence"]:
        with st.expander("Reconciliation evidence", expanded=True):
            for item in reconciliation["evidence"]:
                st.markdown(f"- {item}")
            tabs = st.tabs(["ShopKart order", "Gateway attempts", "Settlement lines", "Bank statement"])
            datasets = [
                [order] if order else [],
                reconciliation["gateway_payments"],
                reconciliation["settlement_lines"],
                reconciliation["bank_entries"],
            ]
            for tab, data in zip(tabs, datasets):
                with tab:
                    st.dataframe(pd.DataFrame(data), width="stretch", hide_index=True) if data else st.caption("No records.")


if page == "Customer Support":
    st.title("Unified Payment Investigation")
    st.write(
        "Enter the payment transaction ID and complaint. PayResolve checks the linked transaction, "
        "ShopKart order, gateway attempts, settlement lines, and bank-statement credits in one investigation."
    )
    st.caption(
        "All records are synthetic local demo data. This app does not connect to a real merchant, "
        "Razorpay, or bank."
    )
    with st.form("investigation_form"):
        transaction_id = st.text_input("Transaction ID", placeholder="For example: DEMO-SUCCESS-001")
        claimed_amount = st.number_input("Amount reported by customer (optional)", min_value=0.0, value=0.0, step=50.0)
        complaint = st.text_area(
            "Complaint description", height=150,
            placeholder="Describe the reported issue, including any debit, refund, or order-status concern.",
        )
        submit = st.form_submit_button("Investigate payment", type="primary", width="stretch")
    st.caption(
        "Demo records: DEMO-SUCCESS-001, DEMO-FAILED-001, DEMO-DEBIT-FAILED-001, "
        "DEMO-DUPLICATE-001, DEMO-REFUND-PENDING, DEMO-REFUND-DONE, "
        "DEMO-SUSPICIOUS-001, DEMO-NO-SETTLEMENT-001, DEMO-CONTRADICTORY-001."
    )
    st.caption(
        "Reconciliation transactions: SHOP-TXN-PAID-UNPAID, SHOP-TXN-AMOUNT-MISMATCH, "
        "SHOP-TXN-PAID-GATEWAY-FAILED, SHOP-TXN-MISSING-SETTLEMENT, "
        "SHOP-TXN-MISSING-BANK-CREDIT, SHOP-TXN-MATCHED."
    )
    if submit:
        if not transaction_id.strip() or len(complaint.strip()) < 5:
            st.error("Enter a transaction ID and a complaint of at least five characters.")
        else:
            with st.spinner("Agents are checking the transaction and linked merchant records..."):
                try:
                    report = investigate(transaction_id, complaint, claimed_amount or None)
                    st.session_state["current_report"] = report.model_dump()
                    st.success("Investigation completed and saved to history.")
                except Exception as exc:
                    st.exception(exc)
    report = st.session_state.get("current_report")
    if report:
        st.divider()
        display_report(report)

elif page == "Agent Execution":
    st.title("Agent Execution")
    report = st.session_state.get("current_report")
    if not report:
        st.info("Run an investigation from Customer Support to see the live agent timeline.")
    if report:
        st.caption(f"Transaction {report['transaction_id']} · {len(report['events'])} workflow steps")
        for event in report["events"]:
            icon = "✅" if event["status"] == "completed" else "⚠️" if event["status"] == "warning" else "❌"
            with st.expander(f"{icon} {event['agent']} · {event['status'].upper()}", expanded=True):
                st.write(event["summary"])
                st.caption(f"{event['started_at']} → {event['completed_at']}")
                if event["tool_calls"]:
                    st.markdown("**Tool calls and results**")
                    st.json(event["tool_calls"])
        st.markdown("**Investigation progress**")
        st.progress(1.0, text="Workflow complete")
        if report["resolution"]["requires_human_review"]:
            st.warning("The supervisor routed this case to the human review queue.")

elif page == "Investigation Report":
    st.title("Investigation Report")
    report = st.session_state.get("current_report")
    if report:
        display_report(report)
    else:
        st.info("No current investigation report. Start from Customer Support.")

elif page == "Admin Dashboard":
    st.title("Admin Dashboard")
    try:
        metrics = fetch_admin_metrics()
        totals = metrics["totals"]
        a, b, c = st.columns(3)
        a.metric("Total disputes", int(totals["disputes"]))
        b.metric("Failed transactions", int(totals["failed_transactions"]))
        c.metric("High-risk cases", int(totals["high_risk"]))
        left, right = st.columns(2)
        with left:
            st.subheader("Dispute categories")
            if not metrics["categories"].empty:
                st.bar_chart(metrics["categories"].set_index("category"))
            else:
                st.caption("No disputes submitted yet.")
        with right:
            st.subheader("Agent execution statistics")
            if not metrics["agents"].empty:
                st.dataframe(metrics["agents"].round(3), width="stretch", hide_index=True)
            else:
                st.caption("Agent statistics will appear after investigations.")
        st.divider()
        st.subheader("Merchant payment reconciliation")
        reconciliation_totals = metrics["reconciliation_totals"]
        rc1, rc2, rc3, rc4 = st.columns(4)
        rc1.metric("ShopKart orders", int(reconciliation_totals["orders"]))
        rc2.metric("Gateway payment attempts", int(reconciliation_totals["gateway_payments"]))
        rc3.metric("Reconciliation flags", int(reconciliation_totals["flagged_runs"]))
        rc4.metric("Reviews pending", int(reconciliation_totals["pending_reviews"]))
        if not metrics["reconciliation_agents"].empty:
            st.markdown("**Reconciliation agent execution statistics**")
            st.dataframe(metrics["reconciliation_agents"].round(3), width="stretch", hide_index=True)
        st.divider()
        st.subheader("Human review queue")
        reviews = fetch_pending_reviews()
        if not reviews:
            st.caption("No cases currently await human review.")
        for review in reviews:
            with st.expander(
                f"Dispute #{review['dispute_id']} · {review['transaction_id']} · {review['risk_level']} risk"
            ):
                st.write(review["reason"])
                st.write(review["outcome"])
                with st.form(f"review_{review['dispute_id']}"):
                    reviewer = st.text_input("Reviewer", key=f"reviewer_{review['dispute_id']}")
                    note = st.text_area("Decision note", key=f"note_{review['dispute_id']}")
                    decision = st.selectbox("Decision", ["approved", "rejected"], key=f"decision_{review['dispute_id']}")
                    if st.form_submit_button("Record review decision"):
                        if not reviewer.strip():
                            st.error("Reviewer name is required.")
                        else:
                            try:
                                resolve_human_review(review["dispute_id"], decision, reviewer.strip(), note.strip())
                                st.success("Human-review decision saved.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Could not save review decision: {exc}")
        st.divider()
        st.subheader("Reconciliation review queue")
        reconciliation_reviews = fetch_pending_reconciliations()
        if not reconciliation_reviews:
            st.caption("No order reconciliations currently await human review.")
        for review in reconciliation_reviews:
            with st.expander(f"Reconciliation #{review['run_id']} · {review['order_id']}"):
                st.write(review["recommendation"])
                for discrepancy in json.loads(review["discrepancies_json"]):
                    st.markdown(f"- {discrepancy}")
                with st.form(f"reconciliation_review_{review['run_id']}"):
                    reviewer = st.text_input("Reviewer", key=f"recon_reviewer_{review['run_id']}")
                    note = st.text_area("Decision note", key=f"recon_note_{review['run_id']}")
                    decision = st.selectbox(
                        "Decision", ["approved", "rejected"], key=f"recon_decision_{review['run_id']}"
                    )
                    if st.form_submit_button("Record reconciliation review"):
                        if not reviewer.strip():
                            st.error("Reviewer name is required.")
                        else:
                            try:
                                resolve_reconciliation_review(
                                    review["run_id"], decision, reviewer.strip(), note.strip()
                                )
                                st.success("Reconciliation review decision saved.")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Could not save review decision: {exc}")
    except Exception as exc:
        st.error(f"Could not load dashboard data: {exc}")

else:
    st.title("Investigation History")
    try:
        dispute_tab, reconciliation_tab = st.tabs(["Payment disputes", "Order reconciliations"])
        with dispute_tab:
            history = fetch_history()
            if history.empty:
                st.info("No saved investigations yet.")
            else:
                st.dataframe(history, width="stretch", hide_index=True)
                selected = st.selectbox("View execution details for dispute", history["dispute_id"].tolist())
                st.markdown("**Agent timeline**")
                for event in fetch_dispute_logs(int(selected)):
                    with st.expander(f"{event['agent']} · {event['status']}"):
                        st.write(event["summary"])
                        st.json(event["tool_calls"])
        with reconciliation_tab:
            history = fetch_reconciliation_history()
            if history.empty:
                st.info("No order reconciliations yet.")
            else:
                st.dataframe(history, width="stretch", hide_index=True)
                selected = st.selectbox("View reconciliation execution", history["run_id"].tolist())
                st.markdown("**Agent timeline**")
                for event in fetch_reconciliation_logs(int(selected)):
                    with st.expander(f"{event['agent']} · {event['status']}"):
                        st.write(event["summary"])
                        st.json(event["tool_calls"])
    except Exception as exc:
        st.error(f"Could not load investigation history: {exc}")
