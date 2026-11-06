from typing import Literal

from pydantic import BaseModel, Field


class TransactionOutput(BaseModel):
    found: bool
    transaction: dict | None = None
    attempts: list[dict] = Field(default_factory=list)
    refunds: list[dict] = Field(default_factory=list)
    settlements: list[dict] = Field(default_factory=list)
    related_transactions: list[dict] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    data_conflicts: list[str] = Field(default_factory=list)
    error: str | None = None


class FraudOutput(BaseModel):
    risk_level: Literal["low", "medium", "high", "unknown"]
    risk_score: int = Field(ge=0, le=100)
    evidence: list[str] = Field(default_factory=list)
    classification_note: str


class InvestigationOutput(BaseModel):
    category: str
    summary: str
    confirmed_facts: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    customer_reported_conflicts: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class ResolutionOutput(BaseModel):
    recommendation: str
    next_steps: list[str] = Field(default_factory=list)
    customer_response: str
    requires_human_review: bool
    review_reason: str | None = None
    llm_used: bool = False
    llm_draft_accepted: bool = False


class AgentEvent(BaseModel):
    agent: str
    status: Literal["completed", "warning", "failed"]
    started_at: str
    completed_at: str
    summary: str
    tool_calls: list[dict] = Field(default_factory=list)


class DisputeInput(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=100)
    complaint: str = Field(min_length=5, max_length=5000)
    claimed_amount: float | None = Field(default=None, gt=0)


class InvestigationReport(BaseModel):
    dispute_id: int | None = None
    transaction_id: str
    transaction: TransactionOutput
    fraud: FraudOutput
    investigation: InvestigationOutput
    resolution: ResolutionOutput
    reconciliations: list["ReconciliationReport"] = Field(default_factory=list)
    events: list[AgentEvent] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReconciliationOutput(BaseModel):
    order_id: str
    order_found: bool
    order: dict | None = None
    gateway_payments: list[dict] = Field(default_factory=list)
    settlement_lines: list[dict] = Field(default_factory=list)
    settlement_batch_totals: list[dict] = Field(default_factory=list)
    bank_entries: list[dict] = Field(default_factory=list)
    status: Literal["matched", "review_required", "not_found"]
    discrepancies: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class ReconciliationResolution(BaseModel):
    recommendation: str
    next_steps: list[str] = Field(default_factory=list)
    support_summary: str
    requires_human_review: bool
    review_reason: str | None = None


class ReconciliationRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=100)


class ReconciliationReport(BaseModel):
    run_id: int | None = None
    order_id: str
    reconciliation: ReconciliationOutput
    resolution: ReconciliationResolution
    events: list[AgentEvent] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


InvestigationReport.model_rebuild()
