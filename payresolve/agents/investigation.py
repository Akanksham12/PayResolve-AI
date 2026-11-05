from payresolve.llm import generate
from payresolve.schemas import FraudOutput, InvestigationOutput, TransactionOutput


def categorize_complaint(complaint: str) -> str:
    text = complaint.casefold()
    if any(word in text for word in ("refund", "reversal", "reversed")):
        return "refund"
    if any(word in text for word in ("debited", "charged", "deducted", "money gone")):
        return "debit_or_charge"
    if any(word in text for word in ("duplicate", "twice", "double")):
        return "duplicate_payment"
    if any(word in text for word in ("pending", "processing", "stuck")):
        return "pending_payment"
    if any(word in text for word in ("failed", "declined", "unsuccessful")):
        return "failed_payment"
    return "other"


def run_investigation_agent(complaint: str, transaction: TransactionOutput,
                            fraud: FraudOutput) -> tuple[InvestigationOutput, bool]:
    category = categorize_complaint(complaint)
    facts = list(transaction.evidence)
    hypotheses = []
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
        if transaction.related_transactions:
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
    if llm.text:
        hypotheses.append(f"Unverified model interpretation: {llm.text}")
    summary = (
        f"The complaint was categorized as {category.replace('_', ' ')}. "
        + ("The available records require follow-up." if hypotheses else "No cause was confirmed from the available records.")
    )
    confidence = 0.3 if not transaction.found else 0.75
    if transaction.data_conflicts:
        confidence = min(confidence, 0.45)
    elif hypotheses:
        confidence = 0.6
    return InvestigationOutput(
        category=category,
        summary=summary,
        confirmed_facts=facts,
        hypotheses=hypotheses,
        evidence=transaction.evidence + fraud.evidence,
        confidence=confidence,
    ), llm.used
