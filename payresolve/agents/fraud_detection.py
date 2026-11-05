from payresolve.schemas import FraudOutput, TransactionOutput


def run_fraud_agent(transaction: TransactionOutput) -> FraudOutput:
    if not transaction.found:
        return FraudOutput(
            risk_level="unknown", risk_score=0, evidence=[],
            classification_note="Risk could not be assessed because no transaction record was found.",
        )

    score = 0
    evidence = []
    failed_attempts = sum(1 for attempt in transaction.attempts if attempt["status"] == "failed")
    if failed_attempts >= 5:
        score += 65
        evidence.append(f"{failed_attempts} failed attempts are recorded for this transaction.")
    elif failed_attempts >= 3:
        score += 35
        evidence.append(f"{failed_attempts} failed attempts are recorded for this transaction.")
    elif failed_attempts >= 2:
        score += 20
        evidence.append(f"{failed_attempts} failed attempts are recorded for this transaction.")

    successful_related = [row for row in transaction.related_transactions if row["status"] == "successful"]
    if successful_related:
        score += min(45, 30 + 10 * (len(successful_related) - 1))
        evidence.append(
            f"{len(successful_related)} other successful transaction(s) with the same amount were recorded "
            "for this customer within 10 minutes."
        )

    if transaction.data_conflicts:
        score += 30
        evidence.extend(f"Record consistency concern: {item}" for item in transaction.data_conflicts)
    score = min(score, 100)
    level = "high" if score >= 65 else "medium" if score >= 30 else "low"
    note = (
        "Risk flags are indicators for review, not a finding of fraud or wrongdoing."
        if evidence else "No configured suspicious pattern was detected; this is not a guarantee of safety."
    )
    return FraudOutput(risk_level=level, risk_score=score, evidence=evidence, classification_note=note)
