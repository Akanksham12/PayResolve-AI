import logging

from payresolve.repository import fetch_reconciliation_bundle
from payresolve.schemas import ReconciliationOutput

logger = logging.getLogger(__name__)


def _money_differs(left: float, right: float) -> bool:
    return abs(round(float(left) - float(right), 2)) > 0.01


def run_reconciliation_agent(order_id: str, db_path=None) -> ReconciliationOutput:
    bundle = fetch_reconciliation_bundle(order_id, db_path)
    order = bundle["order"]
    if order is None:
        return ReconciliationOutput(
            order_id=order_id.strip(),
            order_found=False,
            status="not_found",
            discrepancies=[f"No ShopKart order record was found for order ID {order_id.strip()}."],
            evidence=["Order lookup returned no matching record."],
        )

    payments = bundle["gateway_payments"]
    settlements = bundle["settlement_lines"]
    bank_entries = bundle["bank_entries"]
    discrepancies = []
    evidence = [
        f"ShopKart order {order['order_id']} is recorded as {order['status']} for "
        f"{order['currency']} {order['amount']:.2f}.",
        f"Found {len(payments)} synthetic gateway payment attempt(s).",
    ]
    successful = [payment for payment in payments if payment["status"] == "successful"]
    if not payments:
        discrepancies.append("No gateway payment attempt is recorded for this order.")
    if order["status"] == "paid" and not successful:
        discrepancies.append("ShopKart marks the order paid, but no successful gateway payment is recorded.")
    if successful and order["status"] != "paid":
        discrepancies.append(
            f"The gateway records {len(successful)} successful payment(s), but ShopKart order status is "
            f"{order['status']} rather than paid."
        )
    if len(successful) > 1:
        discrepancies.append(
            f"The gateway records {len(successful)} successful payments against one order; confirm whether "
            "each payment was intended."
        )

    for payment in successful:
        evidence.append(
            f"Gateway reference {payment['provider_reference']} is successful for "
            f"{payment['currency']} {payment['amount']:.2f}."
        )
        if payment["currency"] != order["currency"] or _money_differs(payment["amount"], order["amount"]):
            discrepancies.append(
                f"Successful gateway payment {payment['provider_reference']} amount "
                f"{payment['currency']} {payment['amount']:.2f} does not match the ShopKart order amount "
                f"{order['currency']} {order['amount']:.2f}."
            )

    settlements_by_payment: dict[str, list[dict]] = {}
    for line in settlements:
        settlements_by_payment.setdefault(line["gateway_payment_id"], []).append(line)
    order_batch_refs = set()
    for payment in successful:
        lines = settlements_by_payment.get(payment["gateway_payment_id"], [])
        if not lines:
            discrepancies.append(
                f"No gateway settlement line is recorded for successful payment {payment['provider_reference']}."
            )
            continue
        for line in lines:
            evidence.append(
                f"Settlement batch {line['batch_reference']} records gross "
                f"{line['gross_amount']:.2f}, fee {line['fee_amount']:.2f}, net {line['net_amount']:.2f} "
                f"({line['status']})."
            )
            if line["status"] != "settled":
                discrepancies.append(
                    f"Gateway settlement line {line['settlement_line_id']} is not finalized."
                )
                continue
            expected_net = round(float(line["gross_amount"]) - float(line["fee_amount"]), 2)
            if _money_differs(line["net_amount"], expected_net):
                discrepancies.append(
                    f"Settlement line {line['settlement_line_id']} net amount does not equal gross less fees."
                )
            if payment["currency"] != order["currency"] or _money_differs(
                line["gross_amount"], payment["amount"]
            ):
                discrepancies.append(
                    f"Settlement line {line['settlement_line_id']} gross amount does not match its gateway payment."
                )
            order_batch_refs.add(line["batch_reference"])

    entries_by_batch: dict[str, list[dict]] = {}
    for entry in bank_entries:
        entries_by_batch.setdefault(entry["batch_reference"], []).append(entry)
    batch_totals = {
        item["batch_reference"]: item
        for item in bundle.get("settlement_batch_totals", [])
        if item["batch_reference"] in order_batch_refs
    }
    for batch_reference, batch in batch_totals.items():
        expected_amount = round(float(batch["net_amount"]), 2)
        batch_currency = batch["currency"]
        entries = entries_by_batch.get(batch_reference, [])
        if not entries:
            discrepancies.append(
                f"No ShopKart bank-statement credit was found for settled batch {batch_reference} "
                f"(expected {batch_currency} {expected_amount:.2f})."
            )
            continue
        actual_amount = round(sum(float(entry["amount"]) for entry in entries), 2)
        if any(entry["currency"] != batch_currency for entry in entries):
            discrepancies.append(
                f"Bank-statement currency does not match settlement batch {batch_reference}."
            )
        if _money_differs(actual_amount, expected_amount):
            discrepancies.append(
                f"Bank-statement credits for batch {batch_reference} total {actual_amount:.2f}, "
                f"but gateway settlement net totals {expected_amount:.2f}."
            )
        else:
            evidence.append(
                f"Bank-statement credits match settlement batch {batch_reference} net total "
                f"{batch_currency} {expected_amount:.2f}."
            )

    for payment in payments:
        if payment["status"] == "pending" and order["status"] in {"paid", "failed", "cancelled"}:
            discrepancies.append(
                f"Order is {order['status']} while gateway payment {payment['provider_reference']} remains pending."
            )
    for item in discrepancies:
        logger.info("Reconciliation discrepancy for order %s: %s", order_id, item)
    return ReconciliationOutput(
        order_id=order["order_id"],
        order_found=True,
        order=order,
        gateway_payments=payments,
        settlement_lines=settlements,
        bank_entries=bank_entries,
        status="review_required" if discrepancies else "matched",
        discrepancies=discrepancies,
        evidence=evidence,
    )
