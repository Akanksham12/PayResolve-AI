import re
from difflib import SequenceMatcher

from payresolve.llm import generate
from payresolve.schemas import (
    FraudOutput, InvestigationOutput, ReconciliationReport, TransactionOutput,
)


def _mentions_duplicate_charge(complaint: str) -> bool:
    text = complaint.casefold()
    if re.search(
        r"\b(?:twice|double|duplicate|duplicated)\b"
        r"|\b(?:two|2)\s+times?\b"
        r"|\b(?:second|another)\s+(?:charge|payment|transaction)\b"
        r"|\b(?:charged|charge|billed|debited)\s+again\b",
        text,
    ):
        return True

    tokens = re.findall(r"[a-z0-9]+", text)
    return any(
        SequenceMatcher(None, token, indicator).ratio() >= 0.8
        for token in tokens
        for indicator in ("twice", "double")
    )


def categorize_complaint(complaint: str) -> str:
    text = complaint.casefold()
    if _mentions_duplicate_charge(complaint):
        return "duplicate_payment"
    if any(word in text for word in ("refund", "reversal", "reversed")):
        return "refund"
    if any(word in text for word in ("pending", "processing", "stuck")):
        return "pending_payment"
    if any(word in text for word in ("failed", "declined", "unsuccessful", "not successful", "did not go through")):
        return "failed_payment"
    if any(word in text for word in ("debited", "charged", "deducted", "money gone")):
        return "debit_or_charge"
    return "other"


def _reported_payment_status(complaint: str, category: str) -> str | None:
    text = complaint.casefold()
    if category in {"refund", "duplicate_payment"} or re.search(r"\bsettlement|payout\b", text):
        return None
    if category == "pending_payment":
        return "pending"
    if category == "failed_payment":
        return "failed"
    if re.search(r"\b(?:payment|transaction|charge)\b.{0,30}\b(?:successful|succeeded|completed|paid|went through|went thru)\b", text):
        return "successful"
    return None


def _reported_record_conflicts(
    complaint: str, category: str, transaction: TransactionOutput,
    reconciliations: list[ReconciliationReport],
) -> list[str]:
    if not transaction.found:
        return []

    text = complaint.casefold()
    tx = transaction.transaction
    conflicts = []
    reported_status = _reported_payment_status(complaint, category)
    if reported_status and reported_status != tx["status"]:
        conflicts.append(
            f"Customer reports the payment as {reported_status}, but transaction records show {tx['status']}."
        )

    reports_debit = bool(re.search(r"\b(?:debited|deducted|charged|taken from my account)\b", text))
    if reports_debit and tx["status"] in {"failed", "pending"}:
        conflicts.append(
            f"Customer reports money was debited, but the transaction is recorded as {tx['status']}; "
            "PayResolve has no bank-side debit confirmation."
        )

    if category == "refund":
        refund_statuses = {refund["status"] for refund in transaction.refunds}
        reports_not_received = bool(re.search(r"\b(?:not received|haven't received|have not received|missing)\b", text))
        reports_pending = bool(re.search(r"\b(?:pending|processing|not processed)\b", text))
        reports_completed = bool(re.search(r"\b(?:completed|processed|received|issued)\b", text))
        if reports_not_received and "completed" in refund_statuses:
            conflicts.append(
                "Customer reports not receiving the refund, while a refund record is marked completed; "
                "the customer's bank receipt is not verified."
            )
        elif reports_pending and "completed" in refund_statuses:
            conflicts.append("Customer reports the refund is pending, but the refund record is marked completed.")
        elif reports_completed and "completed" not in refund_statuses:
            conflicts.append(
                "Customer reports the refund as completed, but no completed refund record was found."
            )
        elif not transaction.refunds:
            conflicts.append("Customer asks about a refund, but no refund record was found for this transaction.")

    reports_unsettled = bool(
        re.search(
            r"\b(?:not settled|unsettled|settlement(?: is)? pending|payout(?: is)? pending)\b",
            text,
        )
    )
    if reports_unsettled and transaction.settlements and all(
        item["status"] == "settled" for item in transaction.settlements
    ):
        conflicts.append(
            "Customer reports settlement is still pending, but settlement records are marked settled; "
            "bank receipt should be verified separately."
        )
    elif reports_unsettled and not transaction.settlements:
        conflicts.append(
            "Customer reports settlement is pending and no settlement record is available to verify it."
        )

    order_status_claims = []
    if re.search(r"\b(?:order|my order)\b.{0,30}\b(?:failed|unsuccessful)\b", text):
        order_status_claims.append("failed")
    if re.search(r"\b(?:order|my order)\b.{0,30}\b(?:cancelled|canceled)\b", text):
        order_status_claims.append("cancelled")
    reports_order_not_paid = bool(re.search(r"\b(?:unpaid|not paid)\b", text))
    if reports_order_not_paid:
        order_status_claims.append("not_paid")
    if not reports_order_not_paid and re.search(
        r"\b(?:order|my order)\b.{0,30}\b(?:paid|successful|succeeded)\b", text
    ):
        order_status_claims.append("paid")
    for report in reconciliations:
        order = report.reconciliation.order
        if not order:
            continue
        actual_status = order["status"]
        for claimed_status in order_status_claims:
            conflicts_with_record = (
                claimed_status == "not_paid" and actual_status == "paid"
                or claimed_status in {"failed", "cancelled"} and actual_status != claimed_status
                or claimed_status == "paid" and actual_status != "paid"
            )
            if conflicts_with_record:
                conflicts.append(
                    f"Customer reports the ShopKart order as {claimed_status.replace('_', ' ')}, "
                    f"but the linked order record shows {actual_status}."
                )
                break
    return conflicts


def run_investigation_agent(complaint: str, transaction: TransactionOutput,
                            fraud: FraudOutput,
                            reconciliations: list[ReconciliationReport] | None = None
                            ) -> tuple[InvestigationOutput, bool]:
    category = categorize_complaint(complaint)
    facts = list(transaction.evidence)
    hypotheses = []
    customer_reported_conflicts = _reported_record_conflicts(
        complaint, category, transaction, reconciliations or []
    )
    if not transaction.found:
        facts.append("No matching transaction record was found; transaction status cannot be verified.")
        hypotheses.append("The supplied ID may contain a typo or belong to a different payment record.")
    else:
        for conflict in transaction.data_conflicts:
            facts.append(f"Conflicting source records: {conflict}")
        if transaction.transaction["status"] == "failed" and any(
            token in complaint.casefold() for token in ("debit", "debited", "charged")
        ):
            hypotheses.append(
                "The customer may see a temporary bank authorization or delayed reversal; no bank-side debit confirmation is available here."
            )
        if transaction.transaction["status"] == "successful" and not transaction.settlements:
            hypotheses.append("Settlement may still be pending or its record may be missing; neither is confirmed.")
        if any(refund["status"] == "pending" for refund in transaction.refunds):
            hypotheses.append("The refund record is pending; a completion time is not present in the supplied records.")
        if category == "duplicate_payment":
            successful_related = [
                item for item in transaction.related_transactions
                if item["status"] == "successful"
            ]
            if successful_related:
                hypotheses.append(
                    f"The ledger contains {len(successful_related)} other successful same-customer, "
                    "same-amount transaction(s) within 10 minutes; whether either charge was unintended is unconfirmed."
                )
            else:
                facts.append(
                    "No additional successful same-customer, same-amount transaction was found in the ledger search."
                )
                hypotheses.append(
                    "The customer reports a possible duplicate charge, but a second successful transaction "
                    "was not found in the searched ledger records. A bank-side debit cannot be verified here."
                )
        elif transaction.related_transactions:
            hypotheses.append("The nearby same-amount records may indicate a duplicate payment; customer intent is unknown.")
    if fraud.evidence:
        facts.extend(f"Risk signal (not a fraud finding): {item}" for item in fraud.evidence)

    prompt = (
        "Summarize this customer payment complaint in one short sentence and suggest a likely issue category. "
        "Do not add facts not listed. Treat all possible causes as hypotheses.\n"
        f"Complaint: {complaint}\nVerified transaction facts: {facts}\n"
        f"Recorded evidence and concerns: {transaction.data_conflicts}"
    )
    llm = generate(prompt)
    summary = (
        f"The complaint was categorized as {category.replace('_', ' ')}. "
        + ("The available records require follow-up." if hypotheses else "No cause was confirmed from the available records.")
    )
    confidence = 0.3 if not transaction.found else 0.75
    if transaction.data_conflicts:
        confidence = min(confidence, 0.45)
    elif customer_reported_conflicts:
        confidence = min(confidence, 0.45)
    elif category == "duplicate_payment" and not any(
        item["status"] == "successful" for item in transaction.related_transactions
    ):
        confidence = min(confidence, 0.45)
    elif hypotheses:
        confidence = 0.6
    return InvestigationOutput(
        category=category,
        summary=summary,
        confirmed_facts=facts,
        hypotheses=hypotheses,
        customer_reported_conflicts=customer_reported_conflicts,
        evidence=transaction.evidence + fraud.evidence,
        confidence=confidence,
    ), llm.used
