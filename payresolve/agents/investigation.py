import re
from difflib import SequenceMatcher

from payresolve.llm import generate
from payresolve.schemas import FraudOutput, InvestigationOutput, TransactionOutput


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
    if any(word in text for word in ("failed", "declined", "unsuccessful")):
        return "failed_payment"
    if any(word in text for word in ("debited", "charged", "deducted", "money gone")):
        return "debit_or_charge"
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
    if llm.text:
        hypotheses.append(f"Unverified model interpretation: {llm.text}")
    summary = (
        f"The complaint was categorized as {category.replace('_', ' ')}. "
        + ("The available records require follow-up." if hypotheses else "No cause was confirmed from the available records.")
    )
    confidence = 0.3 if not transaction.found else 0.75
    if transaction.data_conflicts:
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
        evidence=transaction.evidence + fraud.evidence,
        confidence=confidence,
    ), llm.used
