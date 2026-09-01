"""Strict structured contracts shared by agents and deterministic tools."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Identifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
CurrencyCode = Annotated[str, StringConstraints(to_upper=True, pattern=r"^[A-Z]{3}$")]
Money = Annotated[Decimal, Field(max_digits=20, decimal_places=2)]
Confidence = Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("1"), max_digits=5, decimal_places=4)]


class StrictSchema(BaseModel):
    """Base schema that rejects silent contract drift."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
    )


class BatchStatus(StrEnum):
    UPLOADED = "UPLOADED"
    VALIDATING = "VALIDATING"
    NORMALIZING = "NORMALIZING"
    RECONCILING = "RECONCILING"
    VERIFYING = "VERIFYING"
    FORECASTING = "FORECASTING"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    PROCESSING_FAILED = "PROCESSING_FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.VALIDATION_FAILED,
            self.PROCESSING_FAILED,
            self.CANCELLED,
        }


class RecordStatus(StrEnum):
    UNPROCESSED = "UNPROCESSED"
    CANDIDATES_FOUND = "CANDIDATES_FOUND"
    PROPOSED = "PROPOSED"
    AUTO_RECONCILED = "AUTO_RECONCILED"
    MANUALLY_RECONCILED = "MANUALLY_RECONCILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNRESOLVED = "UNRESOLVED"
    REJECTED = "REJECTED"


class FileKind(StrEnum):
    BANK_TRANSACTIONS = "bank_transactions"
    INVOICES = "invoices"
    LEDGER_ENTRIES = "ledger_entries"
    REMITTANCES = "remittances"


class ProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED = "REJECTED"
    COMMITTED = "COMMITTED"


class Decision(StrEnum):
    AUTO_RECONCILED = "AUTO_RECONCILED"
    MANUALLY_RECONCILED = "MANUALLY_RECONCILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNRESOLVED = "UNRESOLVED"
    REJECTED = "REJECTED"


class AgentName(StrEnum):
    CONTROLLER = "controller"
    RECONCILIATION = "reconciliation"
    VERIFICATION = "verification"
    FORECAST = "forecast"
    EVALUATION = "evaluation"


class EventStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    WARNING = "warning"
    FAILED = "failed"


class ExceptionReason(StrEnum):
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    MISSING_INVOICE = "MISSING_INVOICE"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    SUSPECTED_FEE = "SUSPECTED_FEE"
    SUSPECTED_WITHHOLDING = "SUSPECTED_WITHHOLDING"
    OVERPAYMENT = "OVERPAYMENT"
    UNRECONCILABLE = "UNRECONCILABLE"
    MISSING_REMITTANCE = "MISSING_REMITTANCE"
    INVALID_RECORD = "INVALID_RECORD"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ExceptionStatus(StrEnum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    RESOLVED = "RESOLVED"


class EvidenceItem(StrictSchema):
    evidence_id: Identifier
    evidence_type: Annotated[str, Field(min_length=2, max_length=64)]
    summary: Annotated[str, Field(min_length=3, max_length=500)]
    source_reference: Annotated[str, Field(min_length=1, max_length=256)]


class Allocation(StrictSchema):
    invoice_id: Identifier
    amount: Money = Field(gt=Decimal("0"))
    currency: CurrencyCode

    @field_validator("amount")
    @classmethod
    def normalize_amount(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class MatchProposal(StrictSchema):
    proposal_id: Identifier
    batch_id: Identifier
    transaction_id: Identifier
    allocations: list[Allocation] = Field(min_length=1, max_length=100)
    total_allocated: Money = Field(gt=Decimal("0"))
    transaction_amount: Money = Field(gt=Decimal("0"))
    currency: CurrencyCode
    permitted_deduction: Money = Field(default=Decimal("0.00"), ge=Decimal("0"))
    confidence: Confidence
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=50)
    risk_flags: list[Annotated[str, Field(min_length=2, max_length=100)]] = Field(default_factory=list, max_length=30)
    status: ProposalStatus = ProposalStatus.PROPOSED
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_financial_invariants(self) -> "MatchProposal":
        if any(allocation.currency != self.currency for allocation in self.allocations):
            raise ValueError("every allocation currency must equal the transaction currency")
        allocation_sum = sum((allocation.amount for allocation in self.allocations), Decimal("0.00"))
        if allocation_sum != self.total_allocated:
            raise ValueError("total_allocated must equal the deterministic allocation sum")
        if self.total_allocated - self.permitted_deduction != self.transaction_amount:
            raise ValueError("allocated total less permitted deduction must equal transaction amount")
        return self


class VerificationResult(StrictSchema):
    proposal_id: Identifier
    approved: bool
    policy_version: Annotated[str, Field(min_length=1, max_length=64)]
    confidence_threshold: Confidence
    checked_at: datetime
    reasons: list[Annotated[str, Field(min_length=2, max_length=300)]] = Field(default_factory=list, max_length=30)
    hard_risk_flags: list[Annotated[str, Field(min_length=2, max_length=100)]] = Field(default_factory=list, max_length=30)


class ReconciliationDecision(StrictSchema):
    decision_id: Identifier
    batch_id: Identifier
    transaction_id: Identifier
    decision: Decision
    confidence: Confidence
    decision_source: Literal["deterministic_policy", "human"]
    model_name: str | None = Field(default=None, max_length=100)
    policy_version: Annotated[str, Field(min_length=1, max_length=64)]
    proposal_id: Identifier | None = None
    committed_at: datetime | None = None
    idempotency_key: Annotated[str, Field(min_length=12, max_length=128)] | None = None


class AgentEvent(StrictSchema):
    sequence: int = Field(ge=1)
    batch_id: Identifier
    agent_name: AgentName
    event_type: Annotated[str, Field(min_length=2, max_length=80)]
    message: Annotated[str, Field(min_length=2, max_length=500)]
    input_reference: str | None = Field(default=None, max_length=256)
    tool_name: str | None = Field(default=None, max_length=100)
    tool_result_reference: str | None = Field(default=None, max_length=256)
    timestamp: datetime
    latency_ms: int = Field(default=0, ge=0, le=3_600_000)
    status: EventStatus


class BatchIdInput(StrictSchema):
    batch_id: Identifier


class InspectBatchInput(BatchIdInput):
    pass


class InspectBatchResult(StrictSchema):
    batch_id: Identifier
    total_records: int = Field(ge=0)
    counts_by_type: dict[str, int]
    uploaded_file_count: int = Field(ge=0)


class ValidateBatchInput(BatchIdInput):
    pass


class ValidationIssue(StrictSchema):
    code: Annotated[str, Field(min_length=2, max_length=80)]
    severity: Literal["warning", "error"]
    count: int = Field(ge=1)
    record_references: list[str] = Field(default_factory=list, max_length=100)


class ValidateBatchResult(StrictSchema):
    batch_id: Identifier
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class GetBatchSummaryInput(BatchIdInput):
    pass


class BatchSummaryResult(StrictSchema):
    batch_id: Identifier
    status: BatchStatus
    total_records: int = Field(ge=0)
    processed_records: int = Field(ge=0)
    automatic_matches: int = Field(ge=0)
    review_records: int = Field(ge=0)
    unresolved_records: int = Field(ge=0)


class GetUnprocessedRecordsInput(BatchIdInput):
    limit: int = Field(default=100, ge=1, le=500)


class ReconciliationRecord(StrictSchema):
    record_id: Identifier
    record_type: Literal["bank_transaction", "ledger_entry"]
    status: RecordStatus
    amount: Money
    currency: CurrencyCode
    counterparty: Annotated[str, Field(min_length=1, max_length=200)]
    reference: Annotated[str, Field(min_length=1, max_length=500)]
    effective_date: date


class GetUnprocessedRecordsResult(StrictSchema):
    batch_id: Identifier
    records: list[ReconciliationRecord]


class FinalizeBatchInput(BatchIdInput):
    pass


class FinalizeBatchResult(StrictSchema):
    batch_id: Identifier
    finalized: bool
    terminal_record_count: int = Field(ge=0)
    nonterminal_record_ids: list[Identifier] = Field(default_factory=list)


class NormalizeRecordInput(StrictSchema):
    record_id: Identifier


class NormalizeResult(StrictSchema):
    record_id: Identifier
    normalized_value: Annotated[str, Field(min_length=1, max_length=500)]
    transformations: list[str] = Field(default_factory=list)


class ResolveCustomerAliasInput(StrictSchema):
    name: Annotated[str, Field(min_length=1, max_length=200)]


class ResolveCustomerAliasResult(StrictSchema):
    input_name: str
    customer_id: Identifier | None = None
    canonical_name: str | None = None
    confidence: Confidence


class ValidateCurrencyAmountInput(StrictSchema):
    record_id: Identifier


class ValidateCurrencyAmountResult(StrictSchema):
    record_id: Identifier
    valid: bool
    amount: Money | None = None
    currency: CurrencyCode | None = None
    errors: list[str] = Field(default_factory=list)


class FindCandidateInvoicesInput(StrictSchema):
    transaction_id: Identifier
    limit: int = Field(default=20, ge=1, le=100)


class CandidateInvoice(StrictSchema):
    invoice_id: Identifier
    invoice_number: Annotated[str, Field(min_length=1, max_length=100)]
    remaining_balance: Money = Field(gt=Decimal("0"))
    currency: CurrencyCode
    reference_similarity: Confidence
    counterparty_similarity: Confidence
    hard_risk_flags: list[str] = Field(default_factory=list)


class FindCandidateInvoicesResult(StrictSchema):
    transaction_id: Identifier
    candidates: list[CandidateInvoice]


class FindCandidateLedgerEntriesInput(StrictSchema):
    transaction_id: Identifier
    limit: int = Field(default=20, ge=1, le=100)


class LedgerCandidate(StrictSchema):
    ledger_entry_id: Identifier
    amount: Money
    currency: CurrencyCode
    score: Confidence


class FindCandidateLedgerEntriesResult(StrictSchema):
    transaction_id: Identifier
    candidates: list[LedgerCandidate]


class ParseRemittanceTextInput(StrictSchema):
    remittance_id: Identifier


class ParseRemittanceTextResult(StrictSchema):
    remittance_id: Identifier
    counterparty: str | None = Field(default=None, max_length=200)
    invoice_references: list[str] = Field(default_factory=list, max_length=100)
    payment_type: Literal["single_invoice_payment", "combined_invoice_payment", "partial_payment", "unknown"]
    deduction_hint: Literal["bank_or_processing_charges", "withholding", "none", "unknown"]


class SolvePaymentAllocationInput(StrictSchema):
    transaction_id: Identifier
    candidate_invoice_ids: list[Identifier] = Field(min_length=1, max_length=100)


class SolvePaymentAllocationResult(StrictSchema):
    transaction_id: Identifier
    feasible: bool
    allocations: list[Allocation] = Field(default_factory=list)
    permitted_deduction: Money = Decimal("0.00")
    method: Literal["exact", "constraint_solver", "none"]
    alternatives: int = Field(default=0, ge=0)


class GetMatchEvidenceInput(StrictSchema):
    transaction_id: Identifier
    candidate_ids: list[Identifier] = Field(min_length=1, max_length=100)


class GetMatchEvidenceResult(StrictSchema):
    transaction_id: Identifier
    evidence: list[EvidenceItem]
    confidence: Confidence
    risk_flags: list[str] = Field(default_factory=list)


class ProposeMatchInput(StrictSchema):
    batch_id: Identifier
    transaction_id: Identifier
    transaction_amount: Money = Field(gt=Decimal("0"))
    currency: CurrencyCode
    allocations: list[Allocation] = Field(min_length=1, max_length=100)
    permitted_deduction: Money = Field(default=Decimal("0.00"), ge=Decimal("0"))
    confidence: Confidence
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=50)
    risk_flags: list[str] = Field(default_factory=list)


class ProposeMatchResult(StrictSchema):
    proposal: MatchProposal


class VerifyMatchInput(StrictSchema):
    proposal_id: Identifier


class VerifyMatchResult(StrictSchema):
    proposal: MatchProposal
    verification: VerificationResult


class CommitMatchInput(StrictSchema):
    proposal_id: Identifier
    idempotency_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=12, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


class CommitMatchResult(StrictSchema):
    decision: ReconciliationDecision
    idempotent_replay: bool = False


class CreateExceptionInput(StrictSchema):
    batch_id: Identifier
    record_id: Identifier
    reason_code: ExceptionReason
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=50)
    next_action: Annotated[str, Field(min_length=3, max_length=500)]


class ExceptionRecord(StrictSchema):
    exception_id: Identifier
    batch_id: Identifier
    record_id: Identifier
    proposal_id: Identifier | None = None
    reason_code: ExceptionReason
    evidence: list[EvidenceItem]
    next_action: str
    status: ExceptionStatus
    created_at: datetime
    resolved_at: datetime | None = None
    resolution: str | None = None
    amount: Money | None = None
    currency: CurrencyCode | None = None
    counterparty: str | None = Field(default=None, max_length=200)
    reference: str | None = Field(default=None, max_length=500)
    candidate_invoices: list[CandidateInvoice] = Field(default_factory=list, max_length=100)


class CreateExceptionResult(StrictSchema):
    exception: ExceptionRecord


class ListRelatedExceptionsInput(StrictSchema):
    record_id: Identifier


class ListRelatedExceptionsResult(StrictSchema):
    exceptions: list[ExceptionRecord]


class RequestHumanReviewInput(StrictSchema):
    exception_id: Identifier


class ResolveExceptionInput(StrictSchema):
    exception_id: Identifier
    resolution: Annotated[str, Field(min_length=3, max_length=1000)]


class ExceptionMutationResult(StrictSchema):
    exception: ExceptionRecord


class EditMatchInput(StrictSchema):
    proposal_id: Identifier
    expected_revision: int = Field(ge=1)
    allocations: list[Allocation] = Field(min_length=1, max_length=100)
    permitted_deduction: Money = Field(default=Decimal("0.00"), ge=Decimal("0"))
    edit_reason: Annotated[str, Field(min_length=3, max_length=500)]


class EditMatchResult(StrictSchema):
    proposal: MatchProposal
    verification: VerificationResult


class ApproveMatchInput(StrictSchema):
    proposal_id: Identifier
    expected_revision: int = Field(ge=1)
    idempotency_key: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=12,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ]
    approval_note: Annotated[str, Field(min_length=3, max_length=500)]


class RejectMatchInput(StrictSchema):
    proposal_id: Identifier
    expected_revision: int = Field(ge=1)
    rejection_reason: Annotated[str, Field(min_length=3, max_length=500)]


class HumanReviewResult(StrictSchema):
    proposal: MatchProposal
    decision: ReconciliationDecision | None = None
    exception: ExceptionRecord | None = None
    idempotent_replay: bool = False


class CalculateVerifiedCashInput(StrictSchema):
    batch_id: Identifier
    as_of_date: date


class CalculateVerifiedCashResult(StrictSchema):
    batch_id: Identifier
    as_of_date: date
    amount: Money
    currency: CurrencyCode
    source_transaction_count: int = Field(ge=0)
    excluded_unverified_count: int = Field(ge=0)


class DateRange(StrictSchema):
    start: date
    end: date

    @model_validator(mode="after")
    def chronological(self) -> "DateRange":
        if self.end < self.start:
            raise ValueError("date range end must be on or after start")
        return self


class ExpectedReceivablesInput(StrictSchema):
    batch_id: Identifier
    date_range: DateRange


class CommittedPayablesInput(ExpectedReceivablesInput):
    pass


class CashFlowItem(StrictSchema):
    cash_flow_id: Identifier
    effective_date: date
    amount: Money
    currency: CurrencyCode
    probability: Confidence = Decimal("1.0000")


class CashFlowListResult(StrictSchema):
    items: list[CashFlowItem]


class ScenarioParameters(StrictSchema):
    scenario_name: Annotated[str, Field(min_length=1, max_length=100)] = "base"
    action_type: Literal[
        "base", "customer_payment_delay", "payable_delay", "one_time_outflow"
    ] = "base"
    customer_name: str | None = Field(default=None, max_length=200)
    payable_name: str | None = Field(default=None, max_length=200)
    delay_days: int = Field(default=0, ge=0, le=90)
    one_time_outflow: Money = Field(default=Decimal("0.00"), ge=Decimal("0"))
    currency: CurrencyCode = "USD"


class RunCashForecastInput(StrictSchema):
    batch_id: Identifier
    horizon_days: int = Field(default=30, ge=1, le=365)
    scenario: ScenarioParameters = Field(default_factory=ScenarioParameters)


class ForecastPosition(StrictSchema):
    date: date
    confirmed: Money
    expected: Money
    risk_adjusted: Money
    p10: Money | None = None
    p50: Money | None = None
    p90: Money | None = None


class RunCashForecastResult(StrictSchema):
    forecast_id: Identifier
    batch_id: Identifier
    currency: CurrencyCode
    as_of_date: date
    horizon_days: int
    scenario: ScenarioParameters
    positions: list[ForecastPosition] = Field(min_length=1)
    minimum_expected_cash: Money
    minimum_expected_cash_date: date
    shortfall_date: date | None = None


class RunMonteCarloForecastInput(RunCashForecastInput):
    simulations: int = Field(default=500, ge=100, le=10_000)
    random_seed: int = Field(default=20260901, ge=0, le=2_147_483_647)


class SimulateCashActionInput(StrictSchema):
    batch_id: Identifier
    action: ScenarioParameters


class ExplainForecastMovementInput(StrictSchema):
    forecast_id: Identifier
    date: date


class ExplainForecastMovementResult(StrictSchema):
    forecast_id: Identifier
    date: date
    drivers: list[Annotated[str, Field(min_length=2, max_length=300)]]
    evidence_references: list[str]


class CalculateMatchMetricsInput(BatchIdInput):
    pass


class MatchMetricsResult(StrictSchema):
    batch_id: Identifier
    precision: Confidence
    recall: Confidence
    automation_coverage: Confidence
    value_weighted_coverage: Confidence
    false_approval_rate: Confidence
    exception_recall: Confidence
    value_reconciled: Money
    unresolved_value: Money
    currency: CurrencyCode


class CalculateForecastMetricsInput(StrictSchema):
    forecast_id: Identifier


class ForecastMetricsResult(StrictSchema):
    forecast_id: Identifier
    mae: Money
    currency: CurrencyCode
    evaluated_days: int = Field(ge=0)


class GenerateAuditReportInput(BatchIdInput):
    pass


class AuditEntry(StrictSchema):
    sequence: int = Field(ge=1)
    action: str
    actor: str
    reference: str
    timestamp: datetime


class AuditReportResult(StrictSchema):
    report_id: Identifier
    batch_id: Identifier
    policy_version: str
    generated_at: datetime
    entries: list[AuditEntry]


class CompareWithGroundTruthInput(BatchIdInput):
    evaluator_token: Annotated[str, Field(min_length=16, max_length=256)]


class CompareWithGroundTruthResult(StrictSchema):
    evaluation_id: Identifier
    batch_id: Identifier
    metrics: MatchMetricsResult
    ground_truth_visible_to_agent: Literal[False] = False


class ToolError(StrictSchema):
    code: Annotated[str, Field(min_length=2, max_length=80)]
    message: Annotated[str, Field(min_length=2, max_length=500)]
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class PlannedToolCall(StrictSchema):
    call_id: Identifier
    tool_name: Annotated[str, Field(min_length=2, max_length=100)]
    arguments: dict[str, Any]


class ModelPlan(StrictSchema):
    response_id: Identifier
    calls: list[PlannedToolCall] = Field(default_factory=list, max_length=20)
    final_message: str | None = Field(default=None, max_length=2000)


class ControllerRunResult(StrictSchema):
    batch_id: Identifier
    status: BatchStatus
    tool_calls: int = Field(ge=0)
    automatic_matches: int = Field(ge=0)
    exceptions_created: int = Field(ge=0)
    forecast_id: Identifier | None = None
    report_id: Identifier | None = None
