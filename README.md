# PayResolve AI

PayResolve AI is a local, synthetic-data **merchant payment investigation and reconciliation assistant**. It is designed for an e-commerce merchant's support and payment-operations staff (illustrated as ShopKart), not for customers to query all banks and merchants. It compares ShopKart orders with Razorpay-style gateway attempts, settlement report lines, and supplied bank-statement credits.

The application uses Streamlit, SQLite, deterministic Python reconciliation rules, Pydantic output schemas, and LangGraph workflows. Ollama with `qwen2.5:7b` is available for the existing complaint-summary and response-drafting workflow. Reconciliation facts and discrepancy decisions are calculated from the database, not inferred by an LLM.

This is a portfolio demonstration: **no live ShopKart, Razorpay, bank, or payment-provider systems are connected**. The Razorpay-style and bank-statement data is synthetic. The application never initiates or executes a payment, refund, transfer, settlement, or order correction. Recommendations are for a support executive; mismatches are routed to human review.

## Architecture

```text
Streamlit UI
  ├─ Customer Support -> existing transaction-dispute workflow
  ├─ Merchant Reconciliation -> ShopKart order ID
  │    └─ LangGraph StateGraph (typed shared state)
  │         1. Reconciliation Agent -> compare order, gateway, settlement, bank records
  │         2. Support Resolution Agent -> evidence-based internal next steps
  │         3. Reconciliation Supervisor -> route mismatches to human review
  └─ Agent Execution / Admin / Investigation History

Existing dispute workflow:
  Transaction Agent -> Fraud Detection -> Investigation -> Resolution -> Supervisor
```

SQLite stores customers, transactions, payment attempts, refunds, settlements, disputes, orders, gateway payments, settlement lines, bank-statement entries, execution logs, and human-review decisions. Synthetic data is repeatable with a fixed seed and includes both dispute examples and reconciliation mismatch examples.

## Requirements

- Python 3.10 or newer
- Ollama is optional for running the application; install it and pull the model to enable local LLM inference.
- No paid APIs, API keys, or payment credentials are needed.

## Setup (Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m scripts.seed_db
streamlit run app.py
```

Open the local URL printed by Streamlit. On systems where `py` is unavailable, create the virtual environment with `python -m venv .venv`.

### Optional local LLM setup

Install Ollama, then run:

```powershell
ollama pull qwen2.5:7b
ollama serve
```

Keep Ollama running, then launch Streamlit in another terminal. Defaults are `http://localhost:11434` and `qwen2.5:7b`. Copy `.env.example` to `.env` and set `OLLAMA_HOST`, `OLLAMA_MODEL`, or `PAYRESOLVE_DB_PATH` to customize them.

If Ollama is stopped or the model is missing, deterministic dispute-workflow fallbacks remain available, and the agent timeline records that inference was unavailable. The reconciliation workflow is deterministic and does not depend on Ollama.

## Synthetic database

The app initializes the SQLite schema and seeds it if no transactions exist. If an older transaction-only demo database is detected, it adds synthetic ShopKart orders and gateway/settlement/bank records without deleting the existing transaction data. To explicitly recreate the database with 750 transaction records and reconciliation demo cases:

```powershell
python -m scripts.seed_db
```

This reseeds the configured database. To choose another database:

```powershell
python -m scripts.seed_db --db data\demo.sqlite --count 750 --seed 2026
```

The transaction count must be between 500 and 1,000, including the existing 10 dispute demo transactions. Some transaction IDs:

| ID | Scenario |
|---|---|
| `DEMO-SUCCESS-001` | Successful payment |
| `DEMO-FAILED-001` | Failed payment |
| `DEMO-DEBIT-FAILED-001` | Failed payment; customer reports debit |
| `DEMO-DUPLICATE-001` | Same-amount nearby payment attempts |
| `DEMO-REFUND-PENDING` | Pending refund |
| `DEMO-REFUND-DONE` | Completed refund |
| `DEMO-SUSPICIOUS-001` | Repeated failed attempts |
| `DEMO-NO-SETTLEMENT-001` | Missing settlement record |
| `DEMO-CONTRADICTORY-001` | Failed transaction with successful attempt record |

`DEMO-DUPLICATE-002` is the related second transaction for the duplicate scenario. No transaction data is invented when a lookup misses.

### ShopKart order reconciliation

Open **Merchant Reconciliation**, enter an order ID, and choose **Reconcile order**. The workflow compares:

1. ShopKart order status and amount.
2. Synthetic Razorpay-style payment attempts, references, statuses, amounts, and currencies.
3. Gateway settlement gross, fee, net, and status lines.
4. ShopKart-provided synthetic bank-statement credits grouped by settlement batch.

Example order IDs:

| Order ID | Demonstrated case |
|---|---|
| `SHOP-ORD-PAID-UNPAID` | Gateway payment succeeded but the ShopKart order is still `created`. |
| `SHOP-ORD-AMOUNT-MISMATCH` | Gateway payment amount differs from the ShopKart order amount. |
| `SHOP-ORD-PAID-GATEWAY-FAILED` | ShopKart order is paid but the gateway attempt failed. |
| `SHOP-ORD-MISSING-SETTLEMENT` | Successful gateway payment has no settlement line. |
| `SHOP-ORD-MISSING-BANK-CREDIT` | Settled gateway batch has no matching bank-statement credit. |
| `SHOP-ORD-MATCHED` | Order, successful gateway payment, settlement, and bank credit agree. |

Every reconciliation run and its agent timeline is saved locally. Mismatches and missing order records enter the reconciliation human-review queue in the Admin Dashboard. Review decisions record who reviewed the case and their note; they do not modify an order or financial record.

The comparison only covers records loaded into this synthetic SQLite database. It cannot verify an actual Razorpay account or a real bank credit; a production integration would need authenticated imports/connectors and access controls.

Duplicate-charge complaints are kept separate from confirmed transaction facts. If the ledger search does not find another successful same-customer, same-amount transaction within 10 minutes, the case is marked uncertain and routed for human review; the response asks for another reference and does not claim that a duplicate was confirmed.

## Tests

```powershell
pytest -q
```

Tests use temporary SQLite databases and stub only the optional Ollama network call in the dispute workflow. They exercise both LangGraph workflows, deterministic reconciliation rules, settlement-batch aggregation, persistence, and human-review decisions.

## Safety and review

- Risk classifications describe patterns for review and are not accusations of fraud.
- Transaction calculations and risk rules are deterministic; source records and discrepancies are shown separately.
- Complaints that report debits against failed transactions are not treated as verified bank debits.
- Missing transaction records, conflicting data, low-confidence investigations, and high-risk signals require human review.
- Admin review decisions only update the local review record. They do not execute any financial action.
- The reconciliation demo uses seeded synthetic bank-statement records; CSV import and live provider connectors are not implemented.
- Never enter real customer information; the included dataset and project are intended for local portfolio demonstration.
