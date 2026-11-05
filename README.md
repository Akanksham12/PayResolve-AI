# PayResolve AI

PayResolve AI is a local, synthetic-data payment dispute investigation application. It uses Streamlit for the support/admin UI, SQLite as the sole source of payment facts, deterministic Python rules for transaction and risk checks, Pydantic for validated agent results, and LangGraph to coordinate five distinct agents. Ollama with `qwen2.5:7b` is used for complaint summaries and response drafts when it is running; if unavailable, the interface and execution timeline explicitly mark the rule-based fallback.

This project does **not** connect to Razorpay, banks, payment gateways, or live payment systems. It never initiates refunds, payments, settlements, or transfers. Recommendations and customer messages are drafts; uncertain, missing, contradictory, or high-risk cases are routed to human review.

## Architecture

```text
Streamlit UI
  ├─ Customer Support / Execution / Report / Admin / History
  └─ service.investigate()
       └─ LangGraph StateGraph (shared typed state)
            1. Transaction Agent  -> SQLite records, attempts, refunds, settlements
            2. Fraud Detection    -> deterministic attempt/duplicate/consistency rules
            3. Investigation      -> verified facts vs. hypotheses + Ollama complaint summary
            4. Resolution         -> deterministic next steps + Ollama response draft
            5. Supervisor         -> failure checks and human-review routing
                 └─ Human Review Queue
```

SQLite stores customers, transactions, payment attempts, refunds, settlements, disputes, agent execution logs, and human-review decisions. Synthetic record generation is repeatable with a fixed seed and includes identifiable demo cases.

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

If Ollama is stopped or the model is missing, deterministic investigation and response fallbacks remain available, and the agent timeline records that inference was unavailable.

## Synthetic database

The app initializes the SQLite schema and automatically seeds it if there are no transactions. To explicitly recreate the database with 750 generated records and 10 demo cases:

```powershell
python -m scripts.seed_db
```

This reseeds the configured database. To choose another database:

```powershell
python -m scripts.seed_db --db data\demo.sqlite --count 750 --seed 2026
```

The total count must be between 500 and 1,000, including the 10 named demo transactions. Some demo transaction IDs:

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

## Tests

```powershell
pytest -q
```

Tests use a temporary SQLite database and stub only the optional Ollama network call; the actual graph, database lookups, deterministic agents, and review routing are exercised.

## Safety and review

- Risk classifications describe patterns for review and are not accusations of fraud.
- Transaction calculations and risk rules are deterministic; source records and discrepancies are shown separately.
- Complaints that report debits against failed transactions are not treated as verified bank debits.
- Missing transaction records, conflicting data, low-confidence investigations, and high-risk signals require human review.
- Admin review decisions only update the local review record. They do not execute any financial action.
- Never enter real customer information; the included dataset and project are intended for local portfolio demonstration.
