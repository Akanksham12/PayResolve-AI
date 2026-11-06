import re

from payresolve.llm import generate
from payresolve.schemas import FraudOutput, InvestigationOutput, ResolutionOutput, TransactionOutput


def _draft_is_consistent(text: str, transaction: TransactionOutput) -> bool:
    if not transaction.found:
        return False
    if re.search(
        r"\b(?:successful|success|failed|declined|pending|completed|processing|processed|initiated|issued|sent|credited|"
        r"transferred|reversed|refunded|settled|refund|payment|transaction|settlement|transfer|debit|credit|"
        r"money|bank|account|promise|guarantee|today|tomorrow|soon)\b",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    return not re.search(r"(?:INR|Rs\.?|₹|\$)\s*\d|\b\d+(?:\.\d{1,2})?\s*(?:INR|Rs\.?|₹|\$)", text, re.IGNORECASE)


def run_resolution_agent(complaint: str, transaction: TransactionOutput, fraud: FraudOutput,
                         investigation: InvestigationOutput) -> ResolutionOutput:
    must_review = (
        not transaction.found or fraud.risk_level == "high"
        or investigation.confidence < 0.6 or bool(transaction.data_conflicts)
        or bool(investigation.customer_reported_conflicts)
        or investigation.category == "duplicate_payment"
    )
    review_reasons = []
    if not transaction.found:
        review_reasons.append("transaction record could not be verified")
    if fraud.risk_level == "high":
        review_reasons.append("high risk indicators require human review")
    if investigation.confidence < 0.6:
        review_reasons.append("investigation confidence is low")
    if transaction.data_conflicts:
        review_reasons.append("transaction records contain contradictions")
    if investigation.customer_reported_conflicts:
        review_reasons.append("customer report does not match or cannot be verified against available records")
    if investigation.category == "duplicate_payment":
        review_reasons.append("customer reports a duplicate charge that needs transaction-reference verification")

    if not transaction.found:
        recommendation = "Request transaction verification"
        next_steps = ["Ask the customer to confirm the transaction ID and share a redacted payment reference."]
    elif investigation.customer_reported_conflicts and investigation.category != "duplicate_payment":
        if any("no settlement record is available" in item
               for item in investigation.customer_reported_conflicts):
            recommendation = "Reconcile missing settlement record"
            next_steps = ["Ask the settlement operations team to verify the settlement record and any reported bank credit."]
        else:
            recommendation = "Review customer report against payment records"
            next_steps = [
                "Explain what the available transaction records show without treating them as proof of the customer's bank activity.",
                "Ask for a redacted screenshot or reference for the reported status, debit, refund, or settlement.",
                "Have support verify the relevant gateway, order, refund, settlement, or bank records before advising on an action.",
            ]
    elif investigation.category == "duplicate_payment":
        successful_related = [
            item for item in transaction.related_transactions
            if item["status"] == "successful"
        ]
        if successful_related:
            recommendation = "Review possible duplicate payment"
            next_steps = [
                "Compare the recorded transaction references and confirm which charge the customer disputes."
            ]
        else:
            recommendation = "Verify the unconfirmed duplicate-charge report"
            next_steps = [
                "Explain that this ledger search did not find a second matching successful payment.",
                "Ask the customer for the other transaction reference or a redacted bank record.",
                "Have a support specialist verify the reported second charge; do not state that a duplicate was confirmed.",
            ]
    else:
        tx = transaction.transaction
        refunds = transaction.refunds
        if any(refund["status"] == "pending" for refund in refunds):
            recommendation = "Monitor the pending refund record"
            next_steps = ["Verify the refund status with the authorized payments support team."]
        elif any(refund["status"] == "completed" for refund in refunds):
            recommendation = "Share the verified refund reference"
            next_steps = ["Provide the recorded refund reference and completion timestamp to the customer."]
        elif tx["status"] == "failed":
            recommendation = "Verify any reported debit with the payment provider"
            next_steps = [
                "Explain that this system records the payment as failed.",
                "Ask the customer to check with their bank for any debit or pending authorization.",
                "Escalate for reconciliation if the debit remains after the bank's stated reversal period.",
            ]
        elif tx["status"] == "pending":
            recommendation = "Wait for final payment status and recheck"
            next_steps = ["Recheck the payment status; do not retry until its final status is known."]
        elif not transaction.settlements:
            recommendation = "Reconcile missing settlement record"
            next_steps = ["Ask the settlement operations team to verify the settlement record."]
        elif transaction.related_transactions:
            recommendation = "Review possible duplicate payment"
            next_steps = ["Compare both recorded transaction references and confirm which charge the customer disputes."]
        else:
            recommendation = "Share transaction status and request any remaining evidence"
            next_steps = ["Explain the verified transaction status and ask for a redacted bank reference if the issue persists."]

    review_reason = "; ".join(review_reasons) if review_reasons else None
    llm = generate(
        "Draft a brief, empathetic customer-support response using only these verified facts and recommended steps. "
        "Do not claim any refund, settlement, transfer, reversal, or other action has been initiated or completed "
        "unless explicitly present as a completed record. Do not promise timelines.\n"
        f"Complaint: {complaint}\n"
        f"Verified transaction: {transaction.transaction if transaction.found else 'No record found'}\n"
        f"Refund records: {transaction.refunds}\n"
        f"Investigation findings: {investigation.confirmed_facts}\n"
        f"Recommended steps: {next_steps}"
    )
    draft_accepted = bool(llm.text and _draft_is_consistent(llm.text, transaction))
    if draft_accepted:
        customer_response = llm.text
    elif not transaction.found:
        customer_response = (
            "Thanks for contacting us. We could not locate a payment with the transaction ID provided, "
            "so we cannot verify its status yet. Please confirm the ID and share a redacted payment reference."
        )
    elif investigation.category == "duplicate_payment" and not any(
        item["status"] == "successful" for item in transaction.related_transactions
    ):
        tx = transaction.transaction
        customer_response = (
            f"Thanks for letting us know. Our records show transaction {tx['transaction_id']} as "
            f"{tx['status']} for {tx['currency']} {tx['amount']:.2f}, but we could not verify a second "
            "matching successful payment in the ledger. Please share the other transaction reference or "
            "a redacted bank record so a support specialist can review it. We have not confirmed a duplicate charge."
        )
    elif investigation.customer_reported_conflicts:
        tx = transaction.transaction
        customer_response = (
            f"Thanks for contacting us. You reported a concern that does not fully match what our available "
            f"records show: transaction {tx['transaction_id']} is recorded as {tx['status']}. "
            "These records do not verify activity in your bank account. Please share a redacted screenshot "
            "or reference so a support specialist can compare the relevant records. No payment or refund "
            "action has been initiated."
        )
    else:
        tx = transaction.transaction
        customer_response = (
            f"Thanks for contacting us. Our records show transaction {tx['transaction_id']} as "
            f"{tx['status']} for {tx['currency']} {tx['amount']:.2f}. {next_steps[0]} "
            "We have not initiated or confirmed any new payment or refund action."
        )
    return ResolutionOutput(
        recommendation=recommendation,
        next_steps=next_steps,
        customer_response=customer_response,
        requires_human_review=must_review,
        review_reason=review_reason,
        llm_used=llm.used,
        llm_draft_accepted=draft_accepted,
    )
