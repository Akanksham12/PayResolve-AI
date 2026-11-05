import logging

from payresolve.repository import fetch_transaction_bundle
from payresolve.schemas import TransactionOutput

logger = logging.getLogger(__name__)


def run_transaction_agent(transaction_id: str, claimed_amount: float | None = None,
                          db_path=None) -> TransactionOutput:
    bundle = fetch_transaction_bundle(transaction_id, db_path)
    tx = bundle["transaction"]
    if tx is None:
        return TransactionOutput(
            found=False,
            error=f"No transaction record exists for ID {transaction_id}.",
            evidence=["Transaction ID lookup returned no matching record."],
        )

    evidence = [
        f"Database status: {tx['status']}.",
        f"Recorded amount: {tx['currency']} {tx['amount']:.2f}.",
        f"Payment method: {tx['payment_method']}.",
        f"Recorded timestamp: {tx['created_at']}.",
    ]
    conflicts = []
    if claimed_amount is not None and abs(float(tx["amount"]) - claimed_amount) > 0.01:
        conflicts.append(
            f"Customer-entered amount {claimed_amount:.2f} differs from database amount {tx['amount']:.2f} {tx['currency']}."
        )
    if tx["status"] == "failed" and any(item["status"] == "successful" for item in bundle["attempts"]):
        conflicts.append("A successful payment attempt is recorded for a transaction whose current status is failed.")
    if tx["status"] == "successful" and bundle["attempts"] and not any(
        item["status"] == "successful" for item in bundle["attempts"]
    ):
        conflicts.append("Transaction status is successful, but no successful payment attempt is recorded.")
    if tx["status"] == "successful" and not bundle["settlements"]:
        evidence.append("No settlement record is present in the database; settlement completion is unverified.")
    if bundle["refunds"]:
        for refund in bundle["refunds"]:
            evidence.append(f"Refund record {refund['refund_id']} is {refund['status']} for {refund['amount']:.2f}.")
    if bundle["related"]:
        evidence.append(
            f"Found {len(bundle['related'])} same-customer, same-amount transaction(s) within 10 minutes."
        )
    for conflict in conflicts:
        logger.warning("Transaction data conflict for %s: %s", transaction_id, conflict)
    return TransactionOutput(
        found=True,
        transaction=tx,
        attempts=bundle["attempts"],
        refunds=bundle["refunds"],
        settlements=bundle["settlements"],
        related_transactions=bundle["related"],
        evidence=evidence,
        data_conflicts=conflicts,
    )
