import pandas as pd
import streamlit as st

from payresolve.db import resolve_human_review
from payresolve.logging_config import configure_logging
from payresolve.repository import (
    fetch_admin_metrics, fetch_dispute_logs, fetch_history, fetch_pending_reviews,
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
        ["Customer Support", "Agent Execution", "Investigation Report", "Admin Dashboard", "Investigation History"],
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
            st.dataframe(pd.DataFrame([transaction["transaction"]]), use_container_width=True, hide_index=True)
        else:
            st.info(transaction["error"] or "No transaction data.")
        tabs = st.tabs(["Payment attempts", "Refunds", "Settlements", "Related payments", "Risk evidence"])
        datasets = [
            transaction["attempts"], transaction["refunds"], transaction["settlements"],
            transaction["related_transactions"], fraud["evidence"],
        ]
        for tab, data in zip(tabs[:4], datasets[:4]):
            with tab:
                st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True) if data else st.caption("No records.")
        with tabs[4]:
            for item in fraud["evidence"]:
                st.markdown(f"- {item}")
            st.caption(fraud["classification_note"])
        if transaction["data_conflicts"]:
            st.error("Conflicting records: " + " ".join(transaction["data_conflicts"]))


if page == "Customer Support":
    st.title("Customer Support")
    st.write("Submit a complaint to inspect the synthetic payment ledger and prepare a support resolution.")
    with st.form("investigation_form"):
        transaction_id = st.text_input("Transaction ID", placeholder="For example: DEMO-SUCCESS-001")
        claimed_amount = st.number_input("Amount reported by customer (optional)", min_value=0.0, value=0.0, step=50.0)
        complaint = st.text_area(
            "Complaint description", height=150,
            placeholder="Describe what the customer reported, including any debit or refund concerns.",
        )
        submit = st.form_submit_button("Investigate dispute", type="primary", use_container_width=True)
    st.caption(
        "Demo records: DEMO-SUCCESS-001, DEMO-FAILED-001, DEMO-DEBIT-FAILED-001, "
        "DEMO-DUPLICATE-001, DEMO-REFUND-PENDING, DEMO-REFUND-DONE, "
        "DEMO-SUSPICIOUS-001, DEMO-NO-SETTLEMENT-001, DEMO-CONTRADICTORY-001."
    )
    if submit:
        if not transaction_id.strip() or len(complaint.strip()) < 5:
            st.error("Enter a transaction ID and a complaint of at least five characters.")
        else:
            with st.spinner("Agents are investigating the complaint..."):
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
    else:
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
                st.dataframe(metrics["agents"].round(3), use_container_width=True, hide_index=True)
            else:
                st.caption("Agent statistics will appear after investigations.")
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
    except Exception as exc:
        st.error(f"Could not load dashboard data: {exc}")

else:
    st.title("Investigation History")
    try:
        history = fetch_history()
        if history.empty:
            st.info("No saved investigations yet.")
        else:
            st.dataframe(history, use_container_width=True, hide_index=True)
            selected = st.selectbox("View execution details for dispute", history["dispute_id"].tolist())
            st.markdown("**Agent timeline**")
            for event in fetch_dispute_logs(int(selected)):
                with st.expander(f"{event['agent']} · {event['status']}"):
                    st.write(event["summary"])
                    st.json(event["tool_calls"])
    except Exception as exc:
        st.error(f"Could not load investigation history: {exc}")
