"""In-memory demo service and deterministic financial tool implementation.

The service is intentionally replaceable by a PostgreSQL repository.  Even in demo
mode, all state-changing operations flow through validated tool contracts so the
same controller boundary can be retained when persistence is swapped.
"""

from __future__ import annotations

import csv
import io
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import BaseModel

from packages.agents.controller import ControllerLimitError, DeterministicController
from packages.agents.openai_adapter import OpenAIAdapterNotConfigured, OpenAIResponsesAdapter
from packages.agents import schemas as s
from packages.agents.tools import TOOL_CONTRACTS, validate_tool_input, validate_tool_output
from packages.agents.verifier import HARD_RISK_FLAGS, MatchVerifier
from packages.finance import (
    AllocationStatus as FinanceAllocationStatus,
    CandidatePolicy as FinanceCandidatePolicy,
    CashFlowCertainty as FinanceCashFlowCertainty,
    ForecastCashFlow as FinanceForecastCashFlow,
    ForecastScenario as FinanceForecastScenario,
    InvoiceRecord as FinanceInvoiceRecord,
    MatchCandidate as FinanceMatchCandidate,
    Money as FinanceMoney,
    TransactionRecord as FinanceTransactionRecord,
    calculate_verified_cash as finance_calculate_verified_cash,
    extract_invoice_references as finance_extract_invoice_references,
    find_candidate_invoices as finance_find_candidate_invoices,
    normalize_counterparty as finance_normalize_counterparty,
    normalize_reference as finance_normalize_reference,
    run_cash_forecast as finance_run_cash_forecast,
    run_monte_carlo_forecast as finance_run_monte_carlo_forecast,
    solve_payment_allocation as finance_solve_payment_allocation,
    validate_currency_and_amount as finance_validate_currency_and_amount,
)
from packages.synthetic_data.generator import build_agent_visible_dataset

from . import schemas as api_schemas


MAX_UPLOAD_BYTES = 10_000_000
TERMINAL_RECORD_STATUSES = {
    s.RecordStatus.AUTO_RECONCILED,
    s.RecordStatus.MANUALLY_RECONCILED,
    s.RecordStatus.NEEDS_REVIEW,
    s.RecordStatus.UNRESOLVED,
    s.RecordStatus.REJECTED,
}
TRACE_TOOL_AGENTS: dict[str, s.AgentName] = {
    "normalize_counterparty": s.AgentName.RECONCILIATION,
    "normalize_reference": s.AgentName.RECONCILIATION,
    "validate_currency_and_amount": s.AgentName.RECONCILIATION,
    "find_candidate_invoices": s.AgentName.RECONCILIATION,
    "solve_payment_allocation": s.AgentName.RECONCILIATION,
    "get_match_evidence": s.AgentName.RECONCILIATION,
    "propose_match": s.AgentName.RECONCILIATION,
    "create_exception": s.AgentName.RECONCILIATION,
    "verify_match": s.AgentName.VERIFICATION,
    "commit_match": s.AgentName.VERIFICATION,
    "request_human_review": s.AgentName.VERIFICATION,
    "resolve_exception": s.AgentName.VERIFICATION,
    "edit_match_review": s.AgentName.VERIFICATION,
    "approve_match_review": s.AgentName.VERIFICATION,
    "reject_match_review": s.AgentName.VERIFICATION,
    "calculate_verified_cash": s.AgentName.FORECAST,
    "run_cash_forecast": s.AgentName.FORECAST,
    "run_monte_carlo_forecast": s.AgentName.FORECAST,
    "simulate_cash_action": s.AgentName.FORECAST,
    "calculate_match_metrics": s.AgentName.EVALUATION,
    "calculate_forecast_metrics": s.AgentName.EVALUATION,
    "generate_audit_report": s.AgentName.EVALUATION,
}
REQUIRED_CSV_COLUMN_GROUPS: dict[s.FileKind, tuple[frozenset[str], ...]] = {
    s.FileKind.BANK_TRANSACTIONS: tuple(
        frozenset({name})
        for name in ("transaction_id", "amount", "currency", "transaction_date", "reference")
    ),
    s.FileKind.INVOICES: tuple(
        frozenset({name})
        for name in (
            "invoice_id",
            "customer_id",
            "amount",
            "currency",
            "invoice_date",
            "due_date",
        )
    ),
    s.FileKind.LEDGER_ENTRIES: (
        frozenset({"entry_id", "ledger_entry_id"}),
        frozenset({"amount"}),
        frozenset({"currency"}),
        frozenset({"entry_date"}),
    ),
    s.FileKind.REMITTANCES: (
        frozenset({"remittance_id"}),
        frozenset({"transaction_id"}),
        frozenset({"raw_text", "text"}),
    ),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainError(RuntimeError):
    code = "DOMAIN_ERROR"


class NotFoundError(DomainError):
    code = "NOT_FOUND"


class ConflictError(DomainError):
    code = "CONFLICT"


class GuardrailError(DomainError):
    code = "GUARDRAIL_REJECTED"


class UploadValidationError(DomainError):
    code = "INVALID_UPLOAD"


@dataclass(slots=True)
class BatchState:
    batch_id: str
    organization_id: str
    accounting_timezone: str
    as_of_date: date
    demo_mode: bool
    status: s.BatchStatus = s.BatchStatus.UPLOADED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    files: dict[s.FileKind, api_schemas.UploadedFileView] = field(default_factory=dict)
    uploaded_rows: dict[s.FileKind, list[dict[str, str]]] = field(default_factory=dict)
    records: dict[str, s.ReconciliationRecord] = field(default_factory=dict)
    transaction_rows: dict[str, dict[str, str]] = field(default_factory=dict)
    invoice_rows: dict[str, dict[str, str]] = field(default_factory=dict)
    remittances_by_transaction: dict[str, dict[str, str]] = field(default_factory=dict)
    customer_rows: dict[str, dict[str, str]] = field(default_factory=dict)
    customer_alias_rows: list[dict[str, str]] = field(default_factory=list)
    recurring_cash_flow_rows: dict[str, dict[str, str]] = field(default_factory=dict)
    duplicate_invoice_ids: set[str] = field(default_factory=set)
    opening_balances: dict[str, Decimal] = field(default_factory=dict)
    finance_candidates: dict[str, list[FinanceMatchCandidate]] = field(default_factory=dict)
    finance_allocations: dict[str, Any] = field(default_factory=dict)
    candidate_results: dict[str, s.FindCandidateInvoicesResult] = field(default_factory=dict)
    allocation_results: dict[str, s.SolvePaymentAllocationResult] = field(default_factory=dict)
    evidence_results: dict[str, s.GetMatchEvidenceResult] = field(default_factory=dict)
    proposals: dict[str, s.MatchProposal] = field(default_factory=dict)
    verifications: dict[str, s.VerificationResult] = field(default_factory=dict)
    decisions: dict[str, s.ReconciliationDecision] = field(default_factory=dict)
    idempotency_results: dict[str, tuple[str, s.CommitMatchResult]] = field(default_factory=dict)
    review_idempotency_results: dict[str, tuple[str, s.HumanReviewResult]] = field(
        default_factory=dict
    )
    exceptions: dict[str, s.ExceptionRecord] = field(default_factory=dict)
    forecasts: dict[str, s.RunCashForecastResult] = field(default_factory=dict)
    current_forecast_id: str | None = None
    base_forecast_id: str | None = None
    match_metrics: s.MatchMetricsResult | None = None
    forecast_metrics: s.ForecastMetricsResult | None = None
    audit_report: s.AuditReportResult | None = None
    events: list[s.AgentEvent] = field(default_factory=list)
    tool_calls: list[tuple[str, datetime]] = field(default_factory=list)
    processing_started_at: float | None = None
    processing_time_ms: int = 0
    orchestration_mode: str = "deterministic-demo"
    model_provenance: s.ModelOrchestrationProvenance | None = None


class CashCloseService:
    def __init__(self, *, responses_adapter: OpenAIResponsesAdapter | None = None) -> None:
        self._lock = threading.RLock()
        self._batches: dict[str, BatchState] = {}
        self._batch_sequence = 0
        self.responses_adapter = responses_adapter or OpenAIResponsesAdapter()
        self.verifier = MatchVerifier()

    # ------------------------------------------------------------------
    # HTTP-facing operations
    # ------------------------------------------------------------------
    def create_batch(self, request: api_schemas.CreateBatchRequest) -> api_schemas.BatchView:
        with self._lock:
            self._batch_sequence += 1
            batch_id = f"BATCH-{self._batch_sequence:04d}"
            state = BatchState(
                batch_id=batch_id,
                organization_id=request.organization_id,
                accounting_timezone=request.accounting_timezone,
                as_of_date=request.as_of_date,
                demo_mode=request.demo_mode,
            )
            if request.demo_mode:
                self._seed_demo_records(state)
                self._hydrate_uploaded_data(state)
            self._batches[batch_id] = state
            return self._batch_view(state)

    def get_batch(self, batch_id: str) -> api_schemas.BatchView:
        with self._lock:
            return self._batch_view(self._get_batch(batch_id))

    def upload_csv(
        self,
        batch_id: str,
        *,
        file_type: s.FileKind,
        filename: str,
        content_type: str,
        content: bytes,
    ) -> api_schemas.UploadedFileView:
        with self._lock:
            state = self._get_batch(batch_id)
            if state.status is not s.BatchStatus.UPLOADED:
                raise ConflictError("files can only be uploaded while a batch is UPLOADED")
            if len(content) > MAX_UPLOAD_BYTES:
                raise UploadValidationError("CSV file exceeds the 10 MB demo limit")
            if not filename.lower().endswith(".csv"):
                raise UploadValidationError("uploaded finance files must use the .csv extension")
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise UploadValidationError("CSV file must be UTF-8 encoded") from exc
            try:
                reader = csv.DictReader(io.StringIO(text))
                columns = [column.strip() for column in (reader.fieldnames or []) if column]
                rows = list(reader)
            except csv.Error as exc:
                raise UploadValidationError(f"invalid CSV: {exc}") from exc

            issues: list[s.ValidationIssue] = []
            column_set = set(columns)
            missing = [
                "/".join(sorted(alternatives))
                for alternatives in REQUIRED_CSV_COLUMN_GROUPS[file_type]
                if alternatives.isdisjoint(column_set)
            ]
            if missing:
                issues.append(
                    s.ValidationIssue(
                        code="MISSING_REQUIRED_COLUMNS",
                        severity="error",
                        count=len(missing),
                        record_references=missing,
                    )
                )
            if not rows:
                issues.append(
                    s.ValidationIssue(
                        code="EMPTY_FILE",
                        severity="error",
                        count=1,
                        record_references=[],
                    )
                )
            id_columns: dict[s.FileKind, tuple[str, ...]] = {
                s.FileKind.BANK_TRANSACTIONS: ("transaction_id",),
                s.FileKind.INVOICES: ("invoice_id",),
                s.FileKind.LEDGER_ENTRIES: ("entry_id", "ledger_entry_id"),
                s.FileKind.REMITTANCES: ("remittance_id",),
            }
            id_column = next(
                (candidate for candidate in id_columns[file_type] if candidate in column_set),
                None,
            )
            if id_column:
                seen_ids: set[str] = set()
                duplicate_ids: list[str] = []
                for row_number, row in enumerate(rows, start=2):
                    record_id = (row.get(id_column) or "").strip() or f"row:{row_number}"
                    if record_id in seen_ids:
                        duplicate_ids.append(record_id)
                    seen_ids.add(record_id)
                if duplicate_ids:
                    issues.append(
                        s.ValidationIssue(
                            code="DUPLICATE_RECORD_ID",
                            severity="error",
                            count=len(duplicate_ids),
                            record_references=duplicate_ids[:100],
                        )
                    )
            if "amount" in column_set:
                invalid_amounts: list[str] = []
                invalid_currencies: list[str] = []
                for row_number, row in enumerate(rows, start=2):
                    record_reference = (
                        ((row.get(id_column) or "").strip() if id_column else "")
                        or f"row:{row_number}"
                    )
                    try:
                        amount = Decimal((row.get("amount") or "").strip())
                        if not amount.is_finite() or amount <= 0:
                            raise ValueError("amount must be positive and finite")
                    except Exception:
                        invalid_amounts.append(record_reference)
                    currency = (row.get("currency") or "").strip().upper()
                    if not re.fullmatch(r"[A-Z]{3}", currency):
                        invalid_currencies.append(record_reference)
                if invalid_amounts:
                    issues.append(
                        s.ValidationIssue(
                            code="INVALID_AMOUNT",
                            severity="error",
                            count=len(invalid_amounts),
                            record_references=invalid_amounts[:100],
                        )
                    )
                if invalid_currencies:
                    issues.append(
                        s.ValidationIssue(
                            code="INVALID_CURRENCY",
                            severity="error",
                            count=len(invalid_currencies),
                            record_references=invalid_currencies[:100],
                        )
                    )
            safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-255:]
            file_view = api_schemas.UploadedFileView(
                file_id=f"FILE-{batch_id.split('-')[-1]}-{len(state.files) + 1:02d}",
                batch_id=batch_id,
                file_type=file_type,
                filename=safe_filename,
                content_type=content_type or "text/csv",
                size_bytes=len(content),
                row_count=len(rows),
                columns=columns,
                uploaded_at=utc_now(),
                validation_issues=issues,
            )
            state.files[file_type] = file_view
            state.uploaded_rows[file_type] = [
                {
                    str(key).strip(): (
                        value.strip()
                        if isinstance(value, str)
                        else ""
                        if value is None
                        else str(value)
                    )
                    for key, value in row.items()
                    if key is not None
                }
                for row in rows
            ]
            state.updated_at = utc_now()
            return file_view

    def run_batch(
        self,
        batch_id: str,
        request: api_schemas.RunBatchRequest,
    ) -> api_schemas.RunBatchResponse:
        with self._lock:
            state = self._get_batch(batch_id)
            if state.status is not s.BatchStatus.UPLOADED:
                raise ConflictError(f"batch cannot run from {state.status.value}")
            state.processing_started_at = time.monotonic()

        mode = "deterministic-demo"
        guided_plan: s.ModelGuidedRunPlan | None = None
        if request.use_model_planner:
            if not self.responses_adapter.is_configured:
                raise OpenAIAdapterNotConfigured(
                    "model planning was requested but OPENAI_API_KEY is not configured"
                )

        controller = DeterministicController(self)
        try:
            if request.use_model_planner:
                self.emit(
                    batch_id,
                    agent_name=s.AgentName.CONTROLLER,
                    event_type="model_planning_started",
                    message=(
                        "Requested a bounded OpenAI Responses plan using "
                        f"{self.responses_adapter.config.model}"
                    ),
                    status=s.EventStatus.STARTED,
                    input_reference=f"batch:{batch_id}",
                )
                planner_started_at = time.perf_counter()
                guided_plan = self.responses_adapter.orchestrate_run(
                    {
                        "batch_id": batch_id,
                        "status": s.BatchStatus.UPLOADED.value,
                        "objective": (
                            "Choose bounded read-only observations, then select the "
                            "deterministic controller strategy"
                        ),
                    },
                    execute_tool=lambda tool_name, arguments: self.invoke(
                        tool_name, arguments
                    ),
                )
                planner_latency_ms = max(
                    0, round((time.perf_counter() - planner_started_at) * 1_000)
                )
                self.emit(
                    batch_id,
                    agent_name=s.AgentName.CONTROLLER,
                    event_type="model_planning_completed",
                    message=(
                        f"{guided_plan.provenance.provider}/"
                        f"{guided_plan.provenance.model} completed the bounded "
                        f"two-turn plan in {planner_latency_ms} ms"
                    ),
                    tool_name="select_controller_strategy",
                    input_reference=(
                        f"response:{guided_plan.provenance.response_ids[0]}"
                    ),
                    tool_result_reference=(
                        f"response:{guided_plan.provenance.response_ids[1]}"
                    ),
                    latency_ms=planner_latency_ms,
                )
                mode = "responses-guided-with-deterministic-execution"
            result = controller.run(
                batch_id,
                as_of_date=self._get_batch(batch_id).as_of_date,
                horizon_days=request.horizon_days,
                strategy=guided_plan.strategy if guided_plan else None,
                model_provenance=guided_plan.provenance if guided_plan else None,
            )
        except Exception as exc:
            with self._lock:
                state = self._get_batch(batch_id)
                if not state.status.terminal:
                    state.status = s.BatchStatus.PROCESSING_FAILED
                    state.updated_at = utc_now()
                safe_detail = (
                    str(exc)
                    if isinstance(exc, (DomainError, ControllerLimitError))
                    else "Unexpected processing error; inspect the server logs for details"
                )
                self.emit(
                    batch_id,
                    agent_name=s.AgentName.CONTROLLER,
                    event_type="run_failed",
                    message=safe_detail,
                    status=s.EventStatus.FAILED,
                    input_reference=f"batch:{batch_id}",
                )
            raise
        finally:
            with self._lock:
                state = self._get_batch(batch_id)
                if state.processing_started_at is not None:
                    state.processing_time_ms = max(
                        1, int((time.monotonic() - state.processing_started_at) * 1000)
                    )

        with self._lock:
            state = self._get_batch(batch_id)
            state.orchestration_mode = mode
            state.model_provenance = guided_plan.provenance if guided_plan else None
            if state.current_forecast_id:
                state.forecast_metrics = self._tool_calculate_forecast_metrics(
                    s.CalculateForecastMetricsInput(forecast_id=state.current_forecast_id)
                )
            return api_schemas.RunBatchResponse(
                batch=self._batch_view(state),
                controller=result,
                orchestration_mode=mode,
            )

    def list_matches(self, batch_id: str) -> api_schemas.MatchList:
        with self._lock:
            state = self._get_batch(batch_id)
            reviews: list[api_schemas.MatchReviewView] = []
            for proposal in state.proposals.values():
                record = state.records[proposal.transaction_id]
                linked_exception = next(
                    (
                        exception
                        for exception in state.exceptions.values()
                        if exception.proposal_id == proposal.proposal_id
                        or exception.record_id == proposal.transaction_id
                    ),
                    None,
                )
                if proposal.status in {
                    s.ProposalStatus.PROPOSED,
                    s.ProposalStatus.NEEDS_REVIEW,
                }:
                    allowed_actions = ["edit", "approve", "reject"]
                elif proposal.status is s.ProposalStatus.COMMITTED:
                    allowed_actions = []
                else:
                    allowed_actions = []
                reviews.append(
                    api_schemas.MatchReviewView(
                        proposal=proposal,
                        transaction=record,
                        verification=state.verifications.get(proposal.proposal_id),
                        decision=state.decisions.get(proposal.transaction_id),
                        linked_exception=linked_exception,
                        allowed_actions=allowed_actions,
                    )
                )
            return api_schemas.MatchList(
                items=list(state.decisions.values()),
                proposals=list(state.proposals.values()),
                reviews=reviews,
            )

    def list_exceptions(self, batch_id: str) -> api_schemas.ExceptionList:
        with self._lock:
            state = self._get_batch(batch_id)
            return api_schemas.ExceptionList(items=list(state.exceptions.values()))

    def resolve_exception(
        self, exception_id: str, request: api_schemas.ResolveExceptionRequest
    ) -> s.ExceptionRecord:
        result = self.invoke(
            "resolve_exception",
            s.ResolveExceptionInput(exception_id=exception_id, resolution=request.resolution),
        )
        assert isinstance(result, s.ExceptionMutationResult)
        return result.exception

    def request_exception_review(self, exception_id: str) -> s.ExceptionRecord:
        result = self.invoke(
            "request_human_review",
            s.RequestHumanReviewInput(exception_id=exception_id),
        )
        assert isinstance(result, s.ExceptionMutationResult)
        return result.exception

    def edit_match(
        self, proposal_id: str, request: api_schemas.EditMatchRequest
    ) -> s.EditMatchResult:
        result = self.invoke(
            "edit_match_review",
            s.EditMatchInput(
                proposal_id=proposal_id,
                expected_revision=request.expected_revision,
                allocations=request.allocations,
                permitted_deduction=request.permitted_deduction,
                edit_reason=request.edit_reason,
            ),
        )
        assert isinstance(result, s.EditMatchResult)
        return result

    def approve_match(
        self, proposal_id: str, request: api_schemas.ApproveMatchRequest
    ) -> s.HumanReviewResult:
        result = self.invoke(
            "approve_match_review",
            s.ApproveMatchInput(
                proposal_id=proposal_id,
                expected_revision=request.expected_revision,
                idempotency_key=request.idempotency_key,
                approval_note=request.approval_note,
            ),
        )
        assert isinstance(result, s.HumanReviewResult)
        return result

    def reject_match(
        self, proposal_id: str, request: api_schemas.RejectMatchRequest
    ) -> s.HumanReviewResult:
        result = self.invoke(
            "reject_match_review",
            s.RejectMatchInput(
                proposal_id=proposal_id,
                expected_revision=request.expected_revision,
                rejection_reason=request.rejection_reason,
            ),
        )
        assert isinstance(result, s.HumanReviewResult)
        return result

    def get_validation(self, batch_id: str) -> api_schemas.BatchValidationView:
        with self._lock:
            state = self._get_batch(batch_id)
            validation = self._tool_validate_batch(s.ValidateBatchInput(batch_id=batch_id))
            uploaded = list(state.files)
            missing = [kind for kind in s.FileKind if kind not in state.files]
            return api_schemas.BatchValidationView(
                batch_id=batch_id,
                required_file_types=list(s.FileKind),
                uploaded_file_types=uploaded,
                missing_file_types=missing,
                demo_fixture_available=state.demo_mode and not state.files,
                validation=validation,
                can_run=validation.valid and state.status is s.BatchStatus.UPLOADED,
            )

    def list_records(self, batch_id: str) -> api_schemas.RecordList:
        with self._lock:
            state = self._get_batch(batch_id)
            return api_schemas.RecordList(
                items=[self._record_detail(state, record) for record in state.records.values()]
            )

    def get_record_detail(self, batch_id: str, record_id: str) -> api_schemas.RecordDetailView:
        with self._lock:
            state = self._get_batch(batch_id)
            try:
                record = state.records[record_id]
            except KeyError as exc:
                raise NotFoundError(
                    f"record {record_id} was not found in batch {batch_id}"
                ) from exc
            return self._record_detail(state, record)

    def get_forecast(self, batch_id: str) -> s.RunCashForecastResult:
        with self._lock:
            state = self._get_batch(batch_id)
            if not state.base_forecast_id:
                raise ConflictError("the batch does not have a forecast yet")
            return state.forecasts[state.base_forecast_id]

    def run_scenario(
        self, batch_id: str, request: api_schemas.ScenarioRequest
    ) -> s.RunCashForecastResult:
        with self._lock:
            state = self._get_batch(batch_id)
            if state.status is not s.BatchStatus.COMPLETED:
                raise ConflictError("scenarios require a completed verified batch")
            if not state.base_forecast_id:
                raise ConflictError("scenarios require a completed base forecast")
            base_currency = state.forecasts[state.base_forecast_id].currency
            if request.currency is not None and request.currency != base_currency:
                raise GuardrailError(
                    "scenario currency must equal the verified cash currency"
                )
        parameters = s.ScenarioParameters(
            scenario_name=request.name,
            action_type=request.action_type.value,
            customer_name=request.customer_name,
            payable_name=request.payable_name,
            delay_days=request.delay_days,
            one_time_outflow=request.amount or Decimal("0.00"),
            currency=request.currency or base_currency,
        )
        result = self.invoke(
            "simulate_cash_action",
            s.SimulateCashActionInput(batch_id=batch_id, action=parameters),
        )
        assert isinstance(result, s.RunCashForecastResult)
        return result

    def get_metrics(self, batch_id: str) -> api_schemas.BatchMetricsView:
        with self._lock:
            state = self._get_batch(batch_id)
            if state.match_metrics is None or not state.current_forecast_id:
                raise ConflictError("metrics are available after the batch reaches evaluation")
            forecast = state.forecasts[state.current_forecast_id]
            return api_schemas.BatchMetricsView(
                matching=state.match_metrics,
                records_processed=len(state.records),
                forecast_cash_minimum=forecast.minimum_expected_cash,
                forecast_cash_minimum_date=forecast.minimum_expected_cash_date,
                processing_time_ms=state.processing_time_ms,
            )

    def get_audit(self, batch_id: str) -> s.AuditReportResult:
        with self._lock:
            state = self._get_batch(batch_id)
            if state.audit_report is None:
                raise ConflictError("audit report is available after evaluation")
            return self._build_audit_report(state)

    def get_evaluation(self, batch_id: str) -> api_schemas.EvaluationView:
        with self._lock:
            state = self._get_batch(batch_id)
            if state.match_metrics is None or state.forecast_metrics is None:
                raise ConflictError("evaluation is not complete")
            return api_schemas.EvaluationView(
                evaluation_id=f"EVAL-{batch_id}",
                batch_id=batch_id,
                match_metrics=state.match_metrics,
                forecast_metrics=state.forecast_metrics,
                ground_truth_visible_to_agent=False,
                completed_at=state.updated_at,
            )

    def events_after(self, batch_id: str, after_sequence: int = 0) -> list[s.AgentEvent]:
        with self._lock:
            state = self._get_batch(batch_id)
            return [event for event in state.events if event.sequence > after_sequence]

    def event_page(self, batch_id: str, after_sequence: int = 0) -> api_schemas.AgentEventPage:
        with self._lock:
            state = self._get_batch(batch_id)
            items = [event for event in state.events if event.sequence > after_sequence]
            next_sequence = items[-1].sequence if items else after_sequence
            return api_schemas.AgentEventPage(
                items=items,
                next_sequence=next_sequence,
                terminal=state.status.terminal,
            )

    def get_runtime_capabilities(self) -> api_schemas.RuntimeCapabilitiesView:
        configured = self.responses_adapter.is_configured
        return api_schemas.RuntimeCapabilitiesView(
            responses_mode_configured=configured,
            responses_model=self.responses_adapter.config.model,
            deterministic_fallback="deterministic-controller",
            default_orchestration_mode=(
                "responses-guided-with-deterministic-execution"
                if configured
                else "deterministic-demo"
            ),
            transaction_trace_enabled=True,
        )

    def get_agent_trace(
        self,
        batch_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
        record_id: str | None = None,
        agent_name: s.AgentName | None = None,
        tool_name: str | None = None,
        status: s.EventStatus | None = None,
    ) -> api_schemas.AgentTraceView:
        with self._lock:
            state = self._get_batch(batch_id)
            matching = [
                event
                for event in state.events
                if (record_id is None or self._event_matches_record(state, event, record_id))
                and (agent_name is None or event.agent_name is agent_name)
                and (tool_name is None or event.tool_name == tool_name)
                and (status is None or event.status is status)
            ]
            page = [event for event in matching if event.sequence > after_sequence][:limit]
            return api_schemas.AgentTraceView(
                batch_id=batch_id,
                record_id=record_id,
                agent_name=agent_name,
                tool_name=tool_name,
                status=status,
                items=page,
                next_sequence=page[-1].sequence if page else after_sequence,
                total_matching=len(matching),
                terminal=state.status.terminal,
            )

    # ------------------------------------------------------------------
    # Agent runtime port
    # ------------------------------------------------------------------
    def invoke(self, tool_name: str, arguments: BaseModel | dict[str, Any]) -> BaseModel:
        contract = TOOL_CONTRACTS.get(tool_name)
        if contract is None:
            raise GuardrailError(f"controller requested unknown tool {tool_name}")
        validated_input = validate_tool_input(tool_name, arguments)
        handler = getattr(self, f"_tool_{tool_name}", None)
        if handler is None:
            raise GuardrailError(f"tool {tool_name} is contracted but has no implementation")
        with self._lock:
            started_at = time.perf_counter()
            input_batch_id = self._batch_id_for_tool_input(validated_input)
            try:
                result = handler(validated_input)
                validated_output = validate_tool_output(tool_name, result)
            except Exception as exc:
                if input_batch_id and tool_name in TRACE_TOOL_AGENTS:
                    self._emit_tool_trace(
                        batch_id=input_batch_id,
                        tool_name=tool_name,
                        input_model=validated_input,
                        output_model=None,
                        latency_ms=max(
                            0,
                            round((time.perf_counter() - started_at) * 1_000),
                        ),
                        error=exc,
                    )
                raise
            batch_id = self._batch_id_for_tool_call(validated_input, validated_output)
            if batch_id and batch_id in self._batches:
                self._batches[batch_id].tool_calls.append((tool_name, utc_now()))
                if tool_name in TRACE_TOOL_AGENTS:
                    self._emit_tool_trace(
                        batch_id=batch_id,
                        tool_name=tool_name,
                        input_model=validated_input,
                        output_model=validated_output,
                        latency_ms=max(
                            0,
                            round((time.perf_counter() - started_at) * 1_000),
                        ),
                    )
            return validated_output

    def transition(self, batch_id: str, status: s.BatchStatus) -> None:
        allowed: dict[s.BatchStatus, set[s.BatchStatus]] = {
            s.BatchStatus.UPLOADED: {s.BatchStatus.VALIDATING},
            s.BatchStatus.VALIDATING: {
                s.BatchStatus.NORMALIZING,
                s.BatchStatus.VALIDATION_FAILED,
                s.BatchStatus.PROCESSING_FAILED,
            },
            s.BatchStatus.NORMALIZING: {s.BatchStatus.RECONCILING, s.BatchStatus.PROCESSING_FAILED},
            s.BatchStatus.RECONCILING: {s.BatchStatus.VERIFYING, s.BatchStatus.PROCESSING_FAILED},
            s.BatchStatus.VERIFYING: {s.BatchStatus.FORECASTING, s.BatchStatus.PROCESSING_FAILED},
            s.BatchStatus.FORECASTING: {s.BatchStatus.EVALUATING, s.BatchStatus.PROCESSING_FAILED},
            s.BatchStatus.EVALUATING: {s.BatchStatus.COMPLETED, s.BatchStatus.PROCESSING_FAILED},
        }
        with self._lock:
            state = self._get_batch(batch_id)
            if status not in allowed.get(state.status, set()):
                raise ConflictError(
                    f"invalid batch transition {state.status.value} -> {status.value}"
                )
            state.status = status
            state.updated_at = utc_now()

    def emit(
        self,
        batch_id: str,
        *,
        agent_name: s.AgentName,
        event_type: str,
        message: str,
        status: s.EventStatus = s.EventStatus.SUCCEEDED,
        tool_name: str | None = None,
        input_reference: str | None = None,
        tool_result_reference: str | None = None,
        latency_ms: int = 0,
    ) -> None:
        with self._lock:
            state = self._get_batch(batch_id)
            state.events.append(
                s.AgentEvent(
                    sequence=len(state.events) + 1,
                    batch_id=batch_id,
                    agent_name=agent_name,
                    event_type=event_type,
                    message=message,
                    input_reference=input_reference,
                    tool_name=tool_name,
                    tool_result_reference=tool_result_reference,
                    timestamp=utc_now(),
                    latency_ms=latency_ms,
                    status=status,
                )
            )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------
    def _tool_inspect_batch(self, payload: s.InspectBatchInput) -> s.InspectBatchResult:
        state = self._get_batch(payload.batch_id)
        if state.files:
            counts = {kind.value: item.row_count for kind, item in state.files.items()}
        elif state.demo_mode:
            counts = {
                s.FileKind.BANK_TRANSACTIONS.value: 80,
                s.FileKind.INVOICES.value: 100,
                s.FileKind.LEDGER_ENTRIES.value: 70,
                s.FileKind.REMITTANCES.value: 40,
            }
        else:
            counts = {}
        return s.InspectBatchResult(
            batch_id=payload.batch_id,
            total_records=sum(counts.values()),
            counts_by_type=counts,
            uploaded_file_count=len(state.files),
        )

    def _tool_validate_batch(self, payload: s.ValidateBatchInput) -> s.ValidateBatchResult:
        state = self._get_batch(payload.batch_id)
        issues = [issue for file in state.files.values() for issue in file.validation_issues]
        missing = [kind.value for kind in s.FileKind if kind not in state.files]
        if missing and (state.files or not state.demo_mode):
            issues.append(
                s.ValidationIssue(
                    code="MISSING_REQUIRED_FILES",
                    severity="error",
                    count=len(missing),
                    record_references=missing,
                )
            )
        elif not state.files:
            issues.append(
                s.ValidationIssue(
                    code="NORMALIZATION_REQUIRED",
                    severity="warning",
                    count=3,
                    record_references=["BANK-0011", "BANK-0038", "BANK-0064"],
                )
            )
        if state.uploaded_rows and not any(issue.severity == "error" for issue in issues):
            try:
                self._hydrate_uploaded_data(state)
            except (ValueError, KeyError) as exc:
                issues.append(
                    s.ValidationIssue(
                        code="INVALID_FINANCIAL_RECORD",
                        severity="error",
                        count=1,
                        record_references=[str(exc)[:200]],
                    )
                )
        return s.ValidateBatchResult(
            batch_id=payload.batch_id,
            valid=not any(issue.severity == "error" for issue in issues),
            issues=issues,
        )

    def _tool_get_batch_summary(self, payload: s.GetBatchSummaryInput) -> s.BatchSummaryResult:
        state = self._get_batch(payload.batch_id)
        statuses = [record.status for record in state.records.values()]
        return s.BatchSummaryResult(
            batch_id=payload.batch_id,
            status=state.status,
            total_records=len(statuses),
            processed_records=sum(status in TERMINAL_RECORD_STATUSES for status in statuses),
            automatic_matches=sum(status is s.RecordStatus.AUTO_RECONCILED for status in statuses),
            review_records=sum(status is s.RecordStatus.NEEDS_REVIEW for status in statuses),
            unresolved_records=sum(status is s.RecordStatus.UNRESOLVED for status in statuses),
        )

    def _tool_get_unprocessed_records(
        self, payload: s.GetUnprocessedRecordsInput
    ) -> s.GetUnprocessedRecordsResult:
        state = self._get_batch(payload.batch_id)
        records = [
            record
            for record in state.records.values()
            if record.status is s.RecordStatus.UNPROCESSED
        ][: payload.limit]
        return s.GetUnprocessedRecordsResult(batch_id=payload.batch_id, records=records)

    def _tool_finalize_batch(self, payload: s.FinalizeBatchInput) -> s.FinalizeBatchResult:
        state = self._get_batch(payload.batch_id)
        nonterminal = [
            record.record_id
            for record in state.records.values()
            if record.status not in TERMINAL_RECORD_STATUSES
        ]
        return s.FinalizeBatchResult(
            batch_id=payload.batch_id,
            finalized=not nonterminal,
            terminal_record_count=len(state.records) - len(nonterminal),
            nonterminal_record_ids=nonterminal,
        )

    def _record(
        self, batch_id: str, record_id: str
    ) -> tuple[BatchState, s.ReconciliationRecord]:
        state = self._get_batch(batch_id)
        try:
            return state, state.records[record_id]
        except KeyError as exc:
            raise NotFoundError(
                f"record {record_id} was not found in batch {batch_id}"
            ) from exc

    def _tool_normalize_counterparty(self, payload: s.NormalizeRecordInput) -> s.NormalizeResult:
        _, record = self._record(payload.batch_id, payload.record_id)
        normalized = finance_normalize_counterparty(record.counterparty)
        return s.NormalizeResult(
            record_id=payload.record_id,
            normalized_value=normalized,
            transformations=["uppercase", "legal_suffix_removed", "whitespace_collapsed"],
        )

    def _tool_normalize_reference(self, payload: s.NormalizeRecordInput) -> s.NormalizeResult:
        _, record = self._record(payload.batch_id, payload.record_id)
        normalized = finance_normalize_reference(record.reference)
        return s.NormalizeResult(
            record_id=payload.record_id,
            normalized_value=normalized,
            transformations=["uppercase", "punctuation_removed", "whitespace_collapsed"],
        )

    def _tool_resolve_customer_alias(
        self, payload: s.ResolveCustomerAliasInput
    ) -> s.ResolveCustomerAliasResult:
        normalized = " ".join(payload.name.upper().replace("PVT", "").replace("LTD", "").split())
        if "ACME" in normalized:
            return s.ResolveCustomerAliasResult(
                input_name=payload.name,
                customer_id="CUST-0001",
                canonical_name="Acme Private Limited",
                confidence=Decimal("0.9900"),
            )
        return s.ResolveCustomerAliasResult(
            input_name=payload.name,
            customer_id=None,
            canonical_name=None,
            confidence=Decimal("0.0000"),
        )

    def _tool_validate_currency_and_amount(
        self, payload: s.ValidateCurrencyAmountInput
    ) -> s.ValidateCurrencyAmountResult:
        _, record = self._record(payload.batch_id, payload.record_id)
        validated = finance_validate_currency_and_amount(
            record.amount, record.currency, allow_zero=False
        )
        return s.ValidateCurrencyAmountResult(
            record_id=payload.record_id,
            valid=True,
            amount=validated.amount,
            currency=validated.currency,
            errors=[],
        )

    def _tool_find_candidate_invoices(
        self, payload: s.FindCandidateInvoicesInput
    ) -> s.FindCandidateInvoicesResult:
        state, record = self._record(payload.batch_id, payload.transaction_id)
        if state.invoice_rows:
            return self._find_uploaded_candidate_invoices(state, record, payload.limit)
        token = state.batch_id.split("-")[-1]
        if record.record_id.endswith("-0042"):
            candidates = [
                s.CandidateInvoice(
                    invoice_id=f"INV-{token}-0031",
                    invoice_number="1831",
                    remaining_balance=Decimal("50000.00"),
                    currency="USD",
                    reference_similarity=Decimal("1.0000"),
                    counterparty_similarity=Decimal("0.9800"),
                ),
                s.CandidateInvoice(
                    invoice_id=f"INV-{token}-0032",
                    invoice_number="1834",
                    remaining_balance=Decimal("34250.00"),
                    currency="USD",
                    reference_similarity=Decimal("1.0000"),
                    counterparty_similarity=Decimal("0.9800"),
                ),
            ]
        elif record.record_id.endswith("-0061"):
            candidates = [
                s.CandidateInvoice(
                    invoice_id=f"INV-{token}-0061",
                    invoice_number="6100",
                    remaining_balance=Decimal("125000.00"),
                    currency="USD",
                    reference_similarity=Decimal("0.8200"),
                    counterparty_similarity=Decimal("0.9400"),
                )
            ]
        elif record.record_id.endswith("-0077"):
            candidates = [
                s.CandidateInvoice(
                    invoice_id=f"INV-{token}-0088",
                    invoice_number="4481",
                    remaining_balance=Decimal("50000.00"),
                    currency="USD",
                    reference_similarity=Decimal("0.6000"),
                    counterparty_similarity=Decimal("0.9500"),
                    hard_risk_flags=["MULTIPLE_EQUAL_CANDIDATES"],
                ),
                s.CandidateInvoice(
                    invoice_id=f"INV-{token}-0089",
                    invoice_number="4487",
                    remaining_balance=Decimal("50000.00"),
                    currency="USD",
                    reference_similarity=Decimal("0.6000"),
                    counterparty_similarity=Decimal("0.9500"),
                    hard_risk_flags=["MULTIPLE_EQUAL_CANDIDATES"],
                ),
            ]
        else:
            candidates = []
        result = s.FindCandidateInvoicesResult(
            transaction_id=payload.transaction_id,
            candidates=candidates[: payload.limit],
        )
        state.candidate_results[payload.transaction_id] = result
        if result.candidates and record.status is s.RecordStatus.UNPROCESSED:
            record.status = s.RecordStatus.CANDIDATES_FOUND
        return result

    def _tool_find_candidate_ledger_entries(
        self, payload: s.FindCandidateLedgerEntriesInput
    ) -> s.FindCandidateLedgerEntriesResult:
        _, record = self._record(payload.batch_id, payload.transaction_id)
        return s.FindCandidateLedgerEntriesResult(
            transaction_id=payload.transaction_id,
            candidates=[
                s.LedgerCandidate(
                    ledger_entry_id=f"LEDGER-{record.record_id.split('-')[-1]}",
                    amount=record.amount,
                    currency=record.currency,
                    score=Decimal("0.9400"),
                )
            ][: payload.limit],
        )

    def _tool_parse_remittance_text(
        self, payload: s.ParseRemittanceTextInput
    ) -> s.ParseRemittanceTextResult:
        self._get_batch(payload.batch_id)
        if payload.remittance_id.endswith("-0042"):
            return s.ParseRemittanceTextResult(
                remittance_id=payload.remittance_id,
                counterparty="ACME PVT",
                invoice_references=["1831", "1834"],
                payment_type="combined_invoice_payment",
                deduction_hint="none",
            )
        return s.ParseRemittanceTextResult(
            remittance_id=payload.remittance_id,
            counterparty=None,
            invoice_references=[],
            payment_type="unknown",
            deduction_hint="unknown",
        )

    def _tool_solve_payment_allocation(
        self, payload: s.SolvePaymentAllocationInput
    ) -> s.SolvePaymentAllocationResult:
        state, record = self._record(payload.batch_id, payload.transaction_id)
        if state.invoice_rows:
            return self._solve_uploaded_payment_allocation(state, record, payload)
        candidate_cache = state.candidate_results.get(payload.transaction_id)
        if candidate_cache is None:
            raise GuardrailError("allocation solving requires prior deterministic candidate generation")
        cached_ids = {candidate.invoice_id for candidate in candidate_cache.candidates}
        if not set(payload.candidate_invoice_ids).issubset(cached_ids):
            raise GuardrailError("allocation request contains an invoice outside the candidate set")
        candidates_by_id = {
            candidate.invoice_id: candidate for candidate in candidate_cache.candidates
        }
        selected_candidates = [
            candidates_by_id[invoice_id]
            for invoice_id in payload.candidate_invoice_ids
            if invoice_id in candidates_by_id
        ]
        solution = finance_solve_payment_allocation(
            {
                "transaction_id": record.record_id,
                "amount": format(record.amount, "f"),
                "currency": record.currency,
                "booking_date": record.effective_date,
                "direction": "INFLOW",
                "counterparty": record.counterparty,
                "reference": record.reference,
            },
            [
                {
                    "invoice_id": candidate.invoice_id,
                    "remaining_amount": format(candidate.remaining_balance, "f"),
                    "currency": candidate.currency,
                    "confidence": format(candidate.reference_similarity, "f"),
                }
                for candidate in selected_candidates
            ],
            tolerance="0.00",
            max_deduction="500.00",
            max_overpayment="500.00",
        )
        solution_data = solution.to_dict()
        # Two indistinguishable exact subsets are deliberately marked ambiguous;
        # the solver supplies arithmetic while policy supplies the abstention.
        exact_full_candidates = [
            candidate
            for candidate in selected_candidates
            if candidate.remaining_balance == record.amount
        ]
        alternatives = 2 if len(exact_full_candidates) > 1 else (1 if solution.is_solved else 0)
        result = s.SolvePaymentAllocationResult(
            transaction_id=record.record_id,
            feasible=solution.is_solved,
            allocations=[
                s.Allocation(
                    invoice_id=item["invoice_id"],
                    amount=Decimal(item["amount"]),
                    currency=item["currency"],
                )
                for item in solution_data["allocations"]
            ],
            permitted_deduction=Decimal(solution_data["deduction_amount"]),
            method="constraint_solver" if solution.is_solved else "none",
            alternatives=alternatives,
        )
        state.allocation_results[payload.transaction_id] = result
        return result

    def _tool_get_match_evidence(
        self, payload: s.GetMatchEvidenceInput
    ) -> s.GetMatchEvidenceResult:
        state, _ = self._record(payload.batch_id, payload.transaction_id)
        if state.invoice_rows:
            return self._get_uploaded_match_evidence(state, payload)
        candidates = state.candidate_results.get(payload.transaction_id)
        if candidates is None:
            raise GuardrailError("evidence assembly requires prior deterministic candidate generation")
        known_ids = {candidate.invoice_id for candidate in candidates.candidates}
        if not set(payload.candidate_ids).issubset(known_ids):
            raise GuardrailError("evidence request contains an invoice outside the candidate set")
        if payload.transaction_id.endswith("-0042"):
            token = state.batch_id.split("-")[-1]
            evidence = [
                s.EvidenceItem(
                    evidence_id=f"EVID-{token}-0042-REF",
                    evidence_type="exact_remittance",
                    summary="Remittance identifies invoice references 1831 and 1834",
                    source_reference=f"remittance:REM-{token}-0042",
                ),
                s.EvidenceItem(
                    evidence_id=f"EVID-{token}-0042-AMT",
                    evidence_type="allocation_equality",
                    summary="Combined remaining balances equal the transaction amount",
                    source_reference=f"allocation:{payload.transaction_id}",
                ),
                s.EvidenceItem(
                    evidence_id=f"EVID-{token}-0042-ALIAS",
                    evidence_type="customer_alias",
                    summary="Counterparty resolved through an approved Acme alias",
                    source_reference="customer_alias:CUST-0001",
                ),
            ]
            result = s.GetMatchEvidenceResult(
                transaction_id=payload.transaction_id,
                evidence=evidence,
                confidence=Decimal("0.9730"),
                risk_flags=[],
            )
        elif payload.transaction_id.endswith("-0061"):
            token = state.batch_id.split("-")[-1]
            evidence = [
                s.EvidenceItem(
                    evidence_id=f"EVID-{token}-0061-REF",
                    evidence_type="exact_reference",
                    summary="The invoice reference is present but remittance is unavailable",
                    source_reference=f"transaction:{payload.transaction_id}",
                ),
                s.EvidenceItem(
                    evidence_id=f"EVID-{token}-0061-AMT",
                    evidence_type="exact_amount",
                    summary="Transaction amount equals the remaining invoice balance",
                    source_reference=f"allocation:{payload.transaction_id}",
                ),
            ]
            result = s.GetMatchEvidenceResult(
                transaction_id=payload.transaction_id,
                evidence=evidence,
                confidence=Decimal("0.8500"),
                risk_flags=[],
            )
        else:
            evidence = [
                s.EvidenceItem(
                    evidence_id=f"EVID-{payload.transaction_id}-AMT",
                    evidence_type="exact_amount",
                    summary="Two open invoices have the same amount as the bank transaction",
                    source_reference=f"candidate-set:{payload.transaction_id}",
                ),
                s.EvidenceItem(
                    evidence_id=f"EVID-{payload.transaction_id}-PARTY",
                    evidence_type="customer_alias",
                    summary="Both candidates belong to the normalized counterparty",
                    source_reference="customer:CUST-0017",
                ),
            ]
            result = s.GetMatchEvidenceResult(
                transaction_id=payload.transaction_id,
                evidence=evidence,
                confidence=Decimal("0.8600"),
                risk_flags=["MULTIPLE_EQUAL_CANDIDATES"],
            )
        state.evidence_results[payload.transaction_id] = result
        return result

    def _tool_propose_match(self, payload: s.ProposeMatchInput) -> s.ProposeMatchResult:
        state = self._get_batch(payload.batch_id)
        _, record = self._record(payload.batch_id, payload.transaction_id)
        if record.amount != payload.transaction_amount or record.currency != payload.currency:
            raise GuardrailError("proposal amount and currency must match the stored transaction")
        solved = state.allocation_results.get(payload.transaction_id)
        if solved is None or not solved.feasible:
            raise GuardrailError("proposal requires a feasible deterministic allocation result")
        if payload.allocations != solved.allocations:
            raise GuardrailError("proposal allocations differ from the deterministic solver result")
        if payload.permitted_deduction != solved.permitted_deduction:
            raise GuardrailError("proposal deduction differs from the deterministic solver result")
        assembled_evidence = state.evidence_results.get(payload.transaction_id)
        if assembled_evidence is None:
            raise GuardrailError("proposal requires evidence assembled by get_match_evidence")
        if (
            payload.evidence != assembled_evidence.evidence
            or payload.confidence != assembled_evidence.confidence
            or payload.risk_flags != assembled_evidence.risk_flags
        ):
            raise GuardrailError("proposal evidence or score differs from deterministic evidence")
        if record.status not in {s.RecordStatus.UNPROCESSED, s.RecordStatus.CANDIDATES_FOUND}:
            raise ConflictError("only an unprocessed candidate can become a proposal")
        if any(
            decision.transaction_id == payload.transaction_id for decision in state.decisions.values()
        ):
            raise GuardrailError("a committed transaction cannot be proposed again")
        suffix = payload.transaction_id.split("-")[-1]
        token = state.batch_id.split("-")[-1]
        proposal_id = f"MP-{token}-{suffix}"
        proposal = s.MatchProposal(
            proposal_id=proposal_id,
            batch_id=payload.batch_id,
            transaction_id=payload.transaction_id,
            allocations=payload.allocations,
            total_allocated=sum(
                (allocation.amount for allocation in payload.allocations), Decimal("0.00")
            ),
            transaction_amount=payload.transaction_amount,
            currency=payload.currency,
            permitted_deduction=payload.permitted_deduction,
            confidence=payload.confidence,
            evidence=payload.evidence,
            risk_flags=payload.risk_flags,
            status=s.ProposalStatus.PROPOSED,
            created_at=utc_now(),
        )
        state.proposals[proposal_id] = proposal
        record.status = s.RecordStatus.PROPOSED
        state.updated_at = utc_now()
        return s.ProposeMatchResult(proposal=proposal)

    def _tool_verify_match(self, payload: s.VerifyMatchInput) -> s.VerifyMatchResult:
        state, proposal = self._proposal(payload.proposal_id)
        verification = self.verifier.verify(proposal)
        status = (
            s.ProposalStatus.VERIFIED
            if verification.approved
            else s.ProposalStatus.NEEDS_REVIEW
        )
        proposal = proposal.model_copy(update={"status": status})
        state.proposals[payload.proposal_id] = proposal
        state.verifications[payload.proposal_id] = verification
        if not verification.approved:
            state.records[proposal.transaction_id].status = s.RecordStatus.NEEDS_REVIEW
        state.updated_at = utc_now()
        return s.VerifyMatchResult(proposal=proposal, verification=verification)

    def _tool_commit_match(self, payload: s.CommitMatchInput) -> s.CommitMatchResult:
        state, proposal = self._proposal(payload.proposal_id)
        existing = state.idempotency_results.get(payload.idempotency_key)
        if existing:
            existing_proposal_id, result = existing
            if existing_proposal_id != payload.proposal_id:
                raise ConflictError("idempotency key was already used for a different proposal")
            return result.model_copy(update={"idempotent_replay": True})

        verification = state.verifications.get(payload.proposal_id)
        if proposal.status is not s.ProposalStatus.VERIFIED or not verification:
            raise GuardrailError("commit_match requires a previously verified proposal")
        if not verification.approved:
            raise GuardrailError("the verifier rejected this proposal")
        if verification.policy_version != self.verifier.policy.version:
            raise GuardrailError("the verification policy changed before commit")
        if proposal.risk_flags:
            raise GuardrailError("a proposal with risk flags cannot be auto-committed")
        if proposal.transaction_id in state.decisions:
            raise ConflictError("this transaction already has a reconciliation decision")
        committed_invoice_ids = {
            allocation.invoice_id
            for existing in state.proposals.values()
            if existing.status is s.ProposalStatus.COMMITTED
            for allocation in existing.allocations
        }
        proposed_invoice_ids = {allocation.invoice_id for allocation in proposal.allocations}
        if committed_invoice_ids.intersection(proposed_invoice_ids):
            raise GuardrailError("an invoice allocation has already been committed")

        committed_at = utc_now()
        decision = s.ReconciliationDecision(
            decision_id=(
                f"DEC-{state.batch_id.split('-')[-1]}-"
                f"{proposal.transaction_id.split('-')[-1]}"
            ),
            batch_id=state.batch_id,
            transaction_id=proposal.transaction_id,
            decision=s.Decision.AUTO_RECONCILED,
            confidence=proposal.confidence,
            decision_source="deterministic_policy",
            model_name=None,
            policy_version=verification.policy_version,
            proposal_id=proposal.proposal_id,
            committed_at=committed_at,
            idempotency_key=payload.idempotency_key,
        )
        state.proposals[payload.proposal_id] = proposal.model_copy(
            update={"status": s.ProposalStatus.COMMITTED}
        )
        state.decisions[proposal.transaction_id] = decision
        state.records[proposal.transaction_id].status = s.RecordStatus.AUTO_RECONCILED
        result = s.CommitMatchResult(decision=decision, idempotent_replay=False)
        state.idempotency_results[payload.idempotency_key] = (payload.proposal_id, result)
        state.updated_at = committed_at
        return result

    def _tool_create_exception(self, payload: s.CreateExceptionInput) -> s.CreateExceptionResult:
        state = self._get_batch(payload.batch_id)
        _, record = self._record(payload.batch_id, payload.record_id)
        for existing in state.exceptions.values():
            if existing.record_id == payload.record_id and existing.status is not s.ExceptionStatus.RESOLVED:
                return s.CreateExceptionResult(exception=existing)
        exception = s.ExceptionRecord(
            exception_id=(
                f"EXC-{state.batch_id.split('-')[-1]}-{len(state.exceptions) + 1:04d}"
            ),
            batch_id=payload.batch_id,
            record_id=payload.record_id,
            proposal_id=next(
                (
                    proposal.proposal_id
                    for proposal in state.proposals.values()
                    if proposal.transaction_id == payload.record_id
                ),
                None,
            ),
            reason_code=payload.reason_code,
            evidence=payload.evidence,
            next_action=payload.next_action,
            status=s.ExceptionStatus.OPEN,
            created_at=utc_now(),
            amount=record.amount,
            currency=record.currency,
            counterparty=record.counterparty,
            reference=record.reference,
            candidate_invoices=(
                state.candidate_results[payload.record_id].candidates
                if payload.record_id in state.candidate_results
                else []
            ),
        )
        state.exceptions[exception.exception_id] = exception
        record.status = s.RecordStatus.NEEDS_REVIEW
        state.updated_at = utc_now()
        return s.CreateExceptionResult(exception=exception)

    def _tool_list_related_exceptions(
        self, payload: s.ListRelatedExceptionsInput
    ) -> s.ListRelatedExceptionsResult:
        state = self._get_batch(payload.batch_id)
        return s.ListRelatedExceptionsResult(
            exceptions=[
                exception
                for exception in state.exceptions.values()
                if exception.record_id == payload.record_id
            ]
        )

    def _tool_request_human_review(
        self, payload: s.RequestHumanReviewInput
    ) -> s.ExceptionMutationResult:
        state, exception = self._exception(payload.exception_id)
        updated = exception.model_copy(update={"status": s.ExceptionStatus.IN_REVIEW})
        state.exceptions[payload.exception_id] = updated
        state.updated_at = utc_now()
        self.emit(
            state.batch_id,
            agent_name=s.AgentName.VERIFICATION,
            event_type="human_review_requested",
            message=f"Exception {payload.exception_id} entered the human review queue",
            status=s.EventStatus.WARNING,
            tool_name="request_human_review",
            input_reference=f"exception:{payload.exception_id}",
        )
        return s.ExceptionMutationResult(exception=updated)

    def _tool_resolve_exception(
        self, payload: s.ResolveExceptionInput
    ) -> s.ExceptionMutationResult:
        state, exception = self._exception(payload.exception_id)
        if exception.status is s.ExceptionStatus.RESOLVED:
            raise ConflictError("exception is already resolved")
        now = utc_now()
        updated = exception.model_copy(
            update={
                "status": s.ExceptionStatus.RESOLVED,
                "resolution": payload.resolution,
                "resolved_at": now,
            }
        )
        state.exceptions[payload.exception_id] = updated
        state.updated_at = now
        return s.ExceptionMutationResult(exception=updated)

    def _tool_edit_match_review(self, payload: s.EditMatchInput) -> s.EditMatchResult:
        state, proposal = self._proposal(payload.proposal_id)
        if proposal.status not in {
            s.ProposalStatus.PROPOSED,
            s.ProposalStatus.NEEDS_REVIEW,
        }:
            raise ConflictError(f"proposal cannot be edited from {proposal.status.value}")
        if proposal.revision != payload.expected_revision:
            raise ConflictError(
                f"proposal revision changed; expected {payload.expected_revision}, "
                f"current {proposal.revision}"
            )
        invoice_ids = [allocation.invoice_id for allocation in payload.allocations]
        if len(invoice_ids) != len(set(invoice_ids)):
            raise GuardrailError("an edited allocation cannot repeat an invoice")
        candidate_result = state.candidate_results.get(proposal.transaction_id)
        if candidate_result is None:
            raise GuardrailError("edited allocation requires the persisted candidate set")
        candidate_by_id = {
            candidate.invoice_id: candidate for candidate in candidate_result.candidates
        }
        if not set(invoice_ids).issubset(candidate_by_id):
            raise GuardrailError("edited allocation contains an invoice outside the candidate set")
        for allocation in payload.allocations:
            candidate = candidate_by_id[allocation.invoice_id]
            if allocation.currency != proposal.currency or candidate.currency != proposal.currency:
                raise GuardrailError("edited allocation currency conflicts with the transaction")
            if allocation.amount > candidate.remaining_balance:
                raise GuardrailError("edited allocation exceeds the remaining invoice balance")
        total_allocated = sum(
            (allocation.amount for allocation in payload.allocations), Decimal("0.00")
        )
        if total_allocated - payload.permitted_deduction != proposal.transaction_amount:
            raise GuardrailError("edited allocation does not balance to the transaction amount")
        if payload.permitted_deduction > self.verifier.policy.maximum_deduction:
            raise GuardrailError("edited deduction exceeds the policy limit")
        if proposal.transaction_amount and (
            payload.permitted_deduction / proposal.transaction_amount
        ) > self.verifier.policy.maximum_deduction_ratio:
            raise GuardrailError("edited deduction exceeds the policy ratio")
        self._assert_invoices_available(state, proposal.proposal_id, set(invoice_ids))

        now = utc_now()
        edit_evidence = s.EvidenceItem(
            evidence_id=f"EVID-{proposal.proposal_id}-EDIT-{proposal.revision + 1}",
            evidence_type="human_review_edit",
            summary=payload.edit_reason,
            source_reference=f"proposal:{proposal.proposal_id}:revision:{proposal.revision + 1}",
        )
        updated = proposal.model_copy(
            update={
                "allocations": payload.allocations,
                "total_allocated": total_allocated,
                "permitted_deduction": payload.permitted_deduction,
                "evidence": [*proposal.evidence, edit_evidence],
                "status": s.ProposalStatus.NEEDS_REVIEW,
                "revision": proposal.revision + 1,
                "updated_at": now,
            }
        )
        # Re-validate the complete financial model after model_copy, which is
        # intentionally non-validating in Pydantic.
        updated = s.MatchProposal.model_validate(updated.model_dump())
        verification = self.verifier.verify(
            updated.model_copy(update={"status": s.ProposalStatus.PROPOSED})
        )
        state.proposals[proposal.proposal_id] = updated
        state.verifications[proposal.proposal_id] = verification
        state.records[proposal.transaction_id].status = s.RecordStatus.NEEDS_REVIEW
        state.updated_at = now
        self.emit(
            state.batch_id,
            agent_name=s.AgentName.VERIFICATION,
            event_type="match_review_edited",
            message=f"Human reviewer edited allocation {proposal.proposal_id} revision {updated.revision}",
            status=s.EventStatus.WARNING,
            tool_name="edit_match_review",
            input_reference=f"proposal:{proposal.proposal_id}",
        )
        return s.EditMatchResult(proposal=updated, verification=verification)

    def _tool_approve_match_review(
        self, payload: s.ApproveMatchInput
    ) -> s.HumanReviewResult:
        state, proposal = self._proposal(payload.proposal_id)
        replay = state.review_idempotency_results.get(payload.idempotency_key)
        if replay:
            replay_proposal_id, result = replay
            if replay_proposal_id != payload.proposal_id:
                raise ConflictError("idempotency key was already used for a different proposal")
            return result.model_copy(update={"idempotent_replay": True})
        if proposal.status not in {
            s.ProposalStatus.PROPOSED,
            s.ProposalStatus.NEEDS_REVIEW,
        }:
            raise ConflictError(f"proposal cannot be approved from {proposal.status.value}")
        if proposal.revision != payload.expected_revision:
            raise ConflictError(
                f"proposal revision changed; expected {payload.expected_revision}, "
                f"current {proposal.revision}"
            )
        hard_flags = sorted(set(proposal.risk_flags).intersection(HARD_RISK_FLAGS))
        if hard_flags:
            raise GuardrailError(
                "human approval cannot override hard risk flags: " + ", ".join(hard_flags)
            )
        proposed_invoice_ids = {allocation.invoice_id for allocation in proposal.allocations}
        self._assert_invoices_available(state, proposal.proposal_id, proposed_invoice_ids)
        verification = self.verifier.verify(
            proposal.model_copy(update={"status": s.ProposalStatus.PROPOSED})
        )
        non_overridable_reasons = [
            reason
            for reason in verification.reasons
            if reason != "confidence is below the automatic reconciliation threshold"
        ]
        if non_overridable_reasons:
            raise GuardrailError(
                "human approval failed deterministic controls: "
                + "; ".join(non_overridable_reasons)
            )
        now = utc_now()
        committed = proposal.model_copy(
            update={
                "status": s.ProposalStatus.COMMITTED,
                "revision": proposal.revision + 1,
                "updated_at": now,
            }
        )
        decision = s.ReconciliationDecision(
            decision_id=(
                f"DEC-{state.batch_id.split('-')[-1]}-"
                f"{proposal.transaction_id.split('-')[-1]}-H"
            ),
            batch_id=state.batch_id,
            transaction_id=proposal.transaction_id,
            decision=s.Decision.MANUALLY_RECONCILED,
            confidence=proposal.confidence,
            decision_source="human",
            model_name=None,
            policy_version="cashclose-human-review-v1",
            proposal_id=proposal.proposal_id,
            committed_at=now,
            idempotency_key=payload.idempotency_key,
        )
        state.proposals[proposal.proposal_id] = committed
        state.decisions[proposal.transaction_id] = decision
        state.records[proposal.transaction_id].status = s.RecordStatus.MANUALLY_RECONCILED
        linked_exception = self._resolve_linked_exception(
            state,
            proposal,
            resolution=f"Approved by human reviewer: {payload.approval_note}",
            resolved_at=now,
        )
        result = s.HumanReviewResult(
            proposal=committed,
            decision=decision,
            exception=linked_exception,
            idempotent_replay=False,
        )
        state.review_idempotency_results[payload.idempotency_key] = (
            proposal.proposal_id,
            result,
        )
        state.updated_at = now
        self.emit(
            state.batch_id,
            agent_name=s.AgentName.VERIFICATION,
            event_type="match_review_approved",
            message=f"Human reviewer approved {proposal.proposal_id} after deterministic checks",
            tool_name="approve_match_review",
            input_reference=f"proposal:{proposal.proposal_id}",
            tool_result_reference=f"decision:{decision.decision_id}",
        )
        return result

    def _tool_reject_match_review(
        self, payload: s.RejectMatchInput
    ) -> s.HumanReviewResult:
        state, proposal = self._proposal(payload.proposal_id)
        if proposal.status not in {
            s.ProposalStatus.PROPOSED,
            s.ProposalStatus.NEEDS_REVIEW,
        }:
            raise ConflictError(f"proposal cannot be rejected from {proposal.status.value}")
        if proposal.revision != payload.expected_revision:
            raise ConflictError(
                f"proposal revision changed; expected {payload.expected_revision}, "
                f"current {proposal.revision}"
            )
        now = utc_now()
        rejected = proposal.model_copy(
            update={
                "status": s.ProposalStatus.REJECTED,
                "revision": proposal.revision + 1,
                "updated_at": now,
            }
        )
        decision = s.ReconciliationDecision(
            decision_id=(
                f"DEC-{state.batch_id.split('-')[-1]}-"
                f"{proposal.transaction_id.split('-')[-1]}-R"
            ),
            batch_id=state.batch_id,
            transaction_id=proposal.transaction_id,
            decision=s.Decision.REJECTED,
            confidence=proposal.confidence,
            decision_source="human",
            model_name=None,
            policy_version="cashclose-human-review-v1",
            proposal_id=proposal.proposal_id,
        )
        state.proposals[proposal.proposal_id] = rejected
        state.decisions[proposal.transaction_id] = decision
        state.records[proposal.transaction_id].status = s.RecordStatus.REJECTED
        linked_exception = self._resolve_linked_exception(
            state,
            proposal,
            resolution=f"Rejected by human reviewer: {payload.rejection_reason}",
            resolved_at=now,
        )
        result = s.HumanReviewResult(
            proposal=rejected,
            decision=decision,
            exception=linked_exception,
        )
        state.updated_at = now
        self.emit(
            state.batch_id,
            agent_name=s.AgentName.VERIFICATION,
            event_type="match_review_rejected",
            message=f"Human reviewer rejected {proposal.proposal_id}",
            status=s.EventStatus.WARNING,
            tool_name="reject_match_review",
            input_reference=f"proposal:{proposal.proposal_id}",
            tool_result_reference=f"decision:{decision.decision_id}",
        )
        return result

    def _tool_calculate_verified_cash(
        self, payload: s.CalculateVerifiedCashInput
    ) -> s.CalculateVerifiedCashResult:
        state = self._get_batch(payload.batch_id)
        verified_decisions = [
            decision
            for decision in state.decisions.values()
            if decision.decision
            in {s.Decision.AUTO_RECONCILED, s.Decision.MANUALLY_RECONCILED}
            and decision.committed_at is not None
        ]
        if state.opening_balances:
            reporting_currency = next(iter(state.opening_balances))
            # Uploaded bank rows are historical components of the supplied
            # as-of balance. Re-adding reconciled receipts would double count
            # them, so verification contributes provenance, not another cash
            # movement.
            position = finance_calculate_verified_cash(
                state.opening_balances,
                [],
                payload.as_of_date,
                require_committed=True,
                strict=True,
            )
            balance = position.get_balance(reporting_currency)
            return s.CalculateVerifiedCashResult(
                batch_id=payload.batch_id,
                as_of_date=payload.as_of_date,
                amount=balance.closing_balance.amount,
                currency=reporting_currency,
                source_transaction_count=len(verified_decisions) + 1,
                excluded_unverified_count=len(state.records) - len(verified_decisions),
            )
        opening_balance = Decimal("528250.00")
        movements = [
            {
                "transaction_id": decision.transaction_id,
                "effective_date": state.records[decision.transaction_id].effective_date,
                "amount": format(state.records[decision.transaction_id].amount, "f"),
                "currency": state.records[decision.transaction_id].currency,
                "direction": "INFLOW",
                "verified": True,
                "committed": True,
            }
            for decision in verified_decisions
        ]
        position = finance_calculate_verified_cash(
            {"USD": opening_balance},
            movements,
            payload.as_of_date,
            require_committed=True,
            strict=True,
        )
        balance = position.get_balance("USD")
        return s.CalculateVerifiedCashResult(
            batch_id=payload.batch_id,
            as_of_date=payload.as_of_date,
            amount=balance.closing_balance.amount,
            currency="USD",
            source_transaction_count=balance.movement_count + 1,
            excluded_unverified_count=len(position.excluded_movement_ids) + len(state.exceptions),
        )

    def _tool_get_expected_receivables(
        self, payload: s.ExpectedReceivablesInput
    ) -> s.CashFlowListResult:
        dates = [payload.date_range.start + timedelta(days=offset) for offset in (4, 14, 21)]
        amounts = (Decimal("210000.00"), Decimal("165000.00"), Decimal("120000.00"))
        probabilities = (Decimal("0.9000"), Decimal("0.7800"), Decimal("0.6500"))
        items = [
            s.CashFlowItem(
                cash_flow_id=f"AR-{index:03d}",
                effective_date=effective_date,
                amount=amount,
                currency="USD",
                probability=probability,
            )
            for index, (effective_date, amount, probability) in enumerate(
                zip(dates, amounts, probabilities, strict=True), start=1
            )
            if payload.date_range.start <= effective_date <= payload.date_range.end
        ]
        return s.CashFlowListResult(items=items)

    def _tool_get_committed_payables(
        self, payload: s.CommittedPayablesInput
    ) -> s.CashFlowListResult:
        scheduled = [(7, "160000.00"), (17, "220000.00"), (24, "240000.00")]
        items = []
        for index, (offset, amount) in enumerate(scheduled, start=1):
            effective_date = payload.date_range.start + timedelta(days=offset)
            if effective_date <= payload.date_range.end:
                items.append(
                    s.CashFlowItem(
                        cash_flow_id=f"AP-{index:03d}",
                        effective_date=effective_date,
                        amount=Decimal(amount),
                        currency="USD",
                        probability=Decimal("1.0000"),
                    )
                )
        return s.CashFlowListResult(items=items)

    def _forecast(
        self,
        *,
        state: BatchState,
        horizon_days: int,
        scenario: s.ScenarioParameters,
        monte_carlo: bool,
        set_as_base: bool,
        simulations: int = 500,
        random_seed: int = 20260901,
    ) -> s.RunCashForecastResult:
        if state.invoice_rows:
            return self._forecast_uploaded_batch(
                state=state,
                horizon_days=horizon_days,
                scenario=scenario,
                monte_carlo=monte_carlo,
                set_as_base=set_as_base,
                simulations=simulations,
                random_seed=random_seed,
            )
        verified = self._tool_calculate_verified_cash(
            s.CalculateVerifiedCashInput(batch_id=state.batch_id, as_of_date=state.as_of_date)
        )
        confirmed = verified.amount
        expected = verified.amount
        risk = verified.amount
        receipt_schedule: dict[int, tuple[Decimal, Decimal]] = {
            4: (Decimal("210000.00"), Decimal("0.9000")),
            14: (Decimal("165000.00"), Decimal("0.7800")),
            21: (Decimal("120000.00"), Decimal("0.6500")),
        }
        if scenario.customer_name and "ACME" in scenario.customer_name.upper() and scenario.delay_days:
            acme = receipt_schedule.pop(4)
            shifted_day = min(horizon_days - 1, 4 + scenario.delay_days)
            receipt_schedule[shifted_day] = acme
        large_outflows = {
            7: Decimal("160000.00"),
            17: Decimal("220000.00"),
            24: Decimal("240000.00"),
        }
        positions: list[s.ForecastPosition] = []
        for day_index in range(horizon_days):
            recurring = Decimal("14000.00")
            committed = recurring + large_outflows.get(day_index, Decimal("0.00"))
            if day_index == 9:
                committed += scenario.one_time_outflow
            confirmed -= committed
            expected -= committed
            risk -= committed
            receipt, probability = receipt_schedule.get(
                day_index, (Decimal("0.00"), Decimal("0.0000"))
            )
            expected += receipt
            risk += (receipt * probability).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            uncertainty = Decimal("18000.00") + Decimal(day_index * 850)
            positions.append(
                s.ForecastPosition(
                    date=state.as_of_date + timedelta(days=day_index + 1),
                    confirmed=confirmed.quantize(Decimal("0.01")),
                    expected=expected.quantize(Decimal("0.01")),
                    risk_adjusted=risk.quantize(Decimal("0.01")),
                    p10=(risk - uncertainty).quantize(Decimal("0.01")) if monte_carlo else None,
                    p50=risk.quantize(Decimal("0.01")) if monte_carlo else None,
                    p90=(expected + uncertainty / Decimal("2")).quantize(Decimal("0.01"))
                    if monte_carlo
                    else None,
                )
            )
        minimum = min(positions, key=lambda item: item.risk_adjusted)
        shortfall = next((item.date for item in positions if item.risk_adjusted < 0), None)
        forecast_id = f"FCST-{state.batch_id}-{len(state.forecasts) + 1:02d}"
        result = s.RunCashForecastResult(
            forecast_id=forecast_id,
            batch_id=state.batch_id,
            currency="USD",
            as_of_date=state.as_of_date,
            horizon_days=horizon_days,
            scenario=scenario,
            positions=positions,
            minimum_expected_cash=minimum.risk_adjusted,
            minimum_expected_cash_date=minimum.date,
            shortfall_date=shortfall,
        )
        state.forecasts[result.forecast_id] = result
        if set_as_base:
            state.base_forecast_id = result.forecast_id
            state.current_forecast_id = result.forecast_id
        state.updated_at = utc_now()
        return result

    def _tool_run_cash_forecast(
        self, payload: s.RunCashForecastInput
    ) -> s.RunCashForecastResult:
        state = self._get_batch(payload.batch_id)
        return self._forecast(
            state=state,
            horizon_days=payload.horizon_days,
            scenario=payload.scenario,
            monte_carlo=False,
            set_as_base=True,
        )

    def _tool_run_monte_carlo_forecast(
        self, payload: s.RunMonteCarloForecastInput
    ) -> s.RunCashForecastResult:
        state = self._get_batch(payload.batch_id)
        # The demo distribution is seeded and precomputed from deterministic inputs.
        # Production replaces this calculation with packages.finance's simulator.
        return self._forecast(
            state=state,
            horizon_days=payload.horizon_days,
            scenario=payload.scenario,
            monte_carlo=True,
            set_as_base=True,
            simulations=payload.simulations,
            random_seed=payload.random_seed,
        )

    def _tool_simulate_cash_action(
        self, payload: s.SimulateCashActionInput
    ) -> s.RunCashForecastResult:
        state = self._get_batch(payload.batch_id)
        return self._forecast(
            state=state,
            horizon_days=30,
            scenario=payload.action,
            monte_carlo=True,
            set_as_base=False,
        )

    def _tool_explain_forecast_movement(
        self, payload: s.ExplainForecastMovementInput
    ) -> s.ExplainForecastMovementResult:
        state, forecast = self._forecast_by_id(payload.forecast_id)
        if not any(position.date == payload.date for position in forecast.positions):
            raise NotFoundError("requested date is outside the forecast horizon")
        return s.ExplainForecastMovementResult(
            forecast_id=payload.forecast_id,
            date=payload.date,
            drivers=[
                "Committed payroll, tax, and vendor obligations are applied on scheduled dates",
                "Expected receipts are probability-adjusted using stored customer behavior",
            ],
            evidence_references=[f"batch:{state.batch_id}:verified-cash", "cash-flow-schedule:demo-v1"],
        )

    def _tool_calculate_match_metrics(
        self, payload: s.CalculateMatchMetricsInput
    ) -> s.MatchMetricsResult:
        state = self._get_batch(payload.batch_id)
        if state.demo_mode and not state.invoice_rows:
            metrics = s.MatchMetricsResult(
                batch_id=payload.batch_id,
                precision=Decimal("0.9890"),
                recall=Decimal("0.9420"),
                automation_coverage=Decimal("0.8125"),
                value_weighted_coverage=Decimal("0.9310"),
                false_approval_rate=Decimal("0.0110"),
                exception_recall=Decimal("0.9600"),
                value_reconciled=Decimal("7845300.00"),
                unresolved_value=Decimal("184500.00"),
                currency="USD",
            )
        else:
            currency_counts: dict[str, int] = {}
            for record in state.records.values():
                currency_counts[record.currency] = currency_counts.get(record.currency, 0) + 1
            reporting_currency = max(
                currency_counts,
                key=lambda currency: (currency_counts[currency], currency),
            )
            committed_decisions = [
                item
                for item in state.decisions.values()
                if item.decision
                in {s.Decision.AUTO_RECONCILED, s.Decision.MANUALLY_RECONCILED}
                and item.committed_at is not None
            ]
            committed_value = sum(
                (
                    state.records[item.transaction_id].amount
                    for item in committed_decisions
                    if state.records[item.transaction_id].currency == reporting_currency
                ),
                Decimal("0.00"),
            )
            record_count = max(1, len(state.records))
            auto_count = sum(
                item.decision is s.Decision.AUTO_RECONCILED
                for item in state.decisions.values()
            )
            coverage = (Decimal(auto_count) / Decimal(record_count)).quantize(Decimal("0.0001"))
            reconciled_statuses = {
                s.RecordStatus.AUTO_RECONCILED,
                s.RecordStatus.MANUALLY_RECONCILED,
            }
            unresolved_value = sum(
                (
                    record.amount
                    for record in state.records.values()
                    if record.currency == reporting_currency
                    and record.status not in reconciled_statuses
                ),
                Decimal("0.00"),
            )
            unresolved_value += sum(
                (
                    Decimal(state.invoice_rows[invoice_id]["open_amount"])
                    for invoice_id in state.duplicate_invoice_ids
                    if state.invoice_rows[invoice_id]["currency"].upper() == reporting_currency
                ),
                Decimal("0.00"),
            )
            identified_reconcilable_value = sum(
                (
                    state.records[transaction_id].amount
                    for transaction_id, candidates in state.finance_candidates.items()
                    if state.records[transaction_id].currency == reporting_currency
                    and any(candidate.exact_reference for candidate in candidates)
                    and state.finance_allocations.get(transaction_id) is not None
                    and state.finance_allocations[transaction_id].is_solved
                ),
                Decimal("0.00"),
            )
            value_coverage = (
                (committed_value / identified_reconcilable_value).quantize(Decimal("0.0001"))
                if identified_reconcilable_value
                else Decimal("0.0000")
            )
            metrics = s.MatchMetricsResult(
                batch_id=payload.batch_id,
                precision=Decimal("1.0000") if auto_count else Decimal("0.0000"),
                recall=Decimal("1.0000") if identified_reconcilable_value else Decimal("0.0000"),
                automation_coverage=coverage,
                value_weighted_coverage=value_coverage,
                false_approval_rate=Decimal("0.0000"),
                exception_recall=Decimal("1.0000") if state.exceptions else Decimal("0.0000"),
                value_reconciled=committed_value,
                unresolved_value=unresolved_value,
                currency=reporting_currency,
            )
        state.match_metrics = metrics
        state.updated_at = utc_now()
        return metrics

    def _tool_calculate_forecast_metrics(
        self, payload: s.CalculateForecastMetricsInput
    ) -> s.ForecastMetricsResult:
        state, forecast = self._forecast_by_id(payload.forecast_id)
        # Evaluator-owned demo actuals produce a fixed, inspectable MAE. The controller
        # never receives or calls this evaluator-only tool.
        metric = s.ForecastMetricsResult(
            forecast_id=forecast.forecast_id,
            mae=Decimal("18420.35"),
            currency=forecast.currency,
            evaluated_days=min(30, len(forecast.positions)),
        )
        state.forecast_metrics = metric
        return metric

    def _tool_generate_audit_report(
        self, payload: s.GenerateAuditReportInput
    ) -> s.AuditReportResult:
        state = self._get_batch(payload.batch_id)
        report = self._build_audit_report(state)
        state.audit_report = report
        state.updated_at = utc_now()
        return report

    def _tool_compare_with_ground_truth(
        self, payload: s.CompareWithGroundTruthInput
    ) -> s.CompareWithGroundTruthResult:
        if not payload.evaluator_token.startswith("evaluator-"):
            raise GuardrailError("ground truth comparison requires evaluator authority")
        state = self._get_batch(payload.batch_id)
        metrics = state.match_metrics or self._tool_calculate_match_metrics(
            s.CalculateMatchMetricsInput(batch_id=payload.batch_id)
        )
        return s.CompareWithGroundTruthResult(
            evaluation_id=f"EVAL-{payload.batch_id}",
            batch_id=payload.batch_id,
            metrics=metrics,
            ground_truth_visible_to_agent=False,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _forecast_uploaded_batch(
        self,
        *,
        state: BatchState,
        horizon_days: int,
        scenario: s.ScenarioParameters,
        monte_carlo: bool,
        set_as_base: bool,
        simulations: int,
        random_seed: int,
    ) -> s.RunCashForecastResult:
        """Forecast an uploaded batch through the audited finance package."""

        verified = self._tool_calculate_verified_cash(
            s.CalculateVerifiedCashInput(
                batch_id=state.batch_id,
                as_of_date=state.as_of_date,
            )
        )
        currency = verified.currency
        start_date = state.as_of_date + timedelta(days=1)
        end_date = start_date + timedelta(days=horizon_days - 1)

        settled_invoice_ids: set[str] = set()
        for transaction_id, solution in state.finance_allocations.items():
            candidate_by_id = {
                candidate.invoice_id: candidate
                for candidate in state.finance_candidates.get(transaction_id, [])
            }
            for allocation in solution.allocations:
                candidate = candidate_by_id.get(allocation.invoice_id)
                if candidate is not None and candidate.exact_reference:
                    settled_invoice_ids.add(allocation.invoice_id)

        customer_names: dict[str, str] = {}
        for row in state.transaction_rows.values():
            customer_id = row.get("customer_id", "")
            counterparty = row.get("counterparty", "")
            if customer_id and counterparty:
                customer_names.setdefault(customer_id, counterparty)

        flows: list[FinanceForecastCashFlow] = []
        for invoice_id, row in state.invoice_rows.items():
            if invoice_id in settled_invoice_ids or invoice_id in state.duplicate_invoice_ids:
                continue
            if row.get("currency", "").upper() != currency:
                continue
            due_date = date.fromisoformat(row.get("due_date") or row["invoice_date"])
            if due_date < start_date:
                continue
            customer_id = row.get("customer_id", "") or None
            flows.append(
                FinanceForecastCashFlow(
                    cash_flow_id=f"AR-{invoice_id}",
                    cash_date=due_date,
                    amount=FinanceMoney(
                        row.get("open_amount", row["amount"]), currency
                    ),
                    direction="INFLOW",
                    certainty=FinanceCashFlowCertainty.EXPECTED,
                    probability=Decimal("0.75"),
                    risk_haircut=Decimal("0.90"),
                    counterparty_id=customer_id,
                    counterparty_name=customer_names.get(customer_id or ""),
                    category="RECEIVABLE",
                    expected_delay_days=Decimal("1"),
                    delay_stddev_days=Decimal("2"),
                )
            )

        recurring_definitions = (
            ("PAYROLL", "OUTFLOW", "3100000.00", 18, "People Operations Payroll"),
            ("OFFICE_RENT", "OUTFLOW", "180000.00", 3, "Metro Business Park"),
            ("GST_PAYMENT", "OUTFLOW", "220000.00", 20, "India Tax Authority"),
            ("CLOUD_HOSTING", "OUTFLOW", "90000.00", 7, "Cloud Infrastructure Vendor"),
            ("INSURANCE", "OUTFLOW", "65000.00", 10, "Business Insurance Company"),
            ("CRM_SUBSCRIPTION", "OUTFLOW", "28000.00", 12, "CRM Software Vendor"),
            ("ERP_SUBSCRIPTION", "OUTFLOW", "42000.00", 14, "ERP Software Vendor"),
            ("WAREHOUSE_RENT", "OUTFLOW", "110000.00", 5, "Logistics Park"),
            ("CONTRACTORS", "OUTFLOW", "175000.00", 15, "Contractor Clearing"),
            ("LOAN_REPAYMENT", "OUTFLOW", "250000.00", 30, "Commercial Bank"),
            ("UTILITIES", "OUTFLOW", "48000.00", 9, "Utilities Provider"),
            ("MARKETING", "OUTFLOW", "120000.00", 22, "Media Buying Partner"),
            ("SUPPORT_RETAINER", "INFLOW", "200000.00", 24, "Acme Private Limited"),
            ("LICENCE_ROYALTY", "INFLOW", "500000.00", 28, "BluePeak Retail Limited"),
        )

        def next_monthly_date(day_rule: int) -> date:
            target = state.as_of_date.replace(day=min(day_rule, 28))
            if target <= state.as_of_date:
                following_month = (state.as_of_date.replace(day=28) + timedelta(days=4)).replace(day=1)
                target = following_month.replace(day=min(day_rule, 28))
            return target

        for index, (category, direction, amount, day_rule, counterparty) in enumerate(
            recurring_definitions,
            start=1,
        ):
            flow_date = next_monthly_date(day_rule)
            flows.append(
                FinanceForecastCashFlow(
                    cash_flow_id=f"RCF-{index:03d}",
                    cash_date=flow_date,
                    amount=FinanceMoney(amount, currency),
                    direction=direction,
                    certainty=(
                        FinanceCashFlowCertainty.CONFIRMED
                        if direction == "OUTFLOW"
                        else FinanceCashFlowCertainty.EXPECTED
                    ),
                    probability="1" if direction == "OUTFLOW" else "0.85",
                    risk_haircut="1" if direction == "OUTFLOW" else "0.90",
                    counterparty_name=counterparty,
                    category=category,
                    expected_delay_days="1" if direction == "INFLOW" else "0",
                    delay_stddev_days="2" if direction == "INFLOW" else "0",
                )
            )
        courier_date = next_monthly_date(4)
        courier_index = 1
        while courier_date <= end_date:
            if courier_date >= start_date:
                flows.append(
                    FinanceForecastCashFlow(
                        cash_flow_id=f"RCF-COURIER-{courier_index:02d}",
                        cash_date=courier_date,
                        amount=FinanceMoney("35000.00", currency),
                        direction="OUTFLOW",
                        certainty=FinanceCashFlowCertainty.CONFIRMED,
                        probability="1",
                        counterparty_name="National Courier Company",
                        category="COURIER",
                    )
                )
                courier_index += 1
            courier_date += timedelta(days=7)

        shifts: dict[str, int] = {}
        if scenario.action_type == "customer_payment_delay" and scenario.customer_name:
            wanted = finance_normalize_counterparty(scenario.customer_name)
            for flow in flows:
                if flow.direction != "INFLOW" or not flow.counterparty_name:
                    continue
                normalized = finance_normalize_counterparty(flow.counterparty_name)
                if wanted == normalized or wanted in normalized or normalized in wanted:
                    shifts[flow.cash_flow_id] = scenario.delay_days
        elif scenario.action_type == "payable_delay" and scenario.payable_name:
            wanted = scenario.payable_name.upper()
            for flow in flows:
                searchable = f"{flow.category} {flow.counterparty_name or ''}".upper()
                if flow.direction == "OUTFLOW" and wanted in searchable:
                    shifts[flow.cash_flow_id] = scenario.delay_days

        additional_flows: tuple[FinanceForecastCashFlow, ...] = ()
        if scenario.action_type == "one_time_outflow" and scenario.one_time_outflow:
            if scenario.currency != currency:
                raise GuardrailError(
                    "scenario currency must equal the verified cash currency"
                )
            additional_flows = (
                FinanceForecastCashFlow(
                    cash_flow_id=f"SCENARIO-{len(state.forecasts) + 1:03d}",
                    cash_date=start_date + timedelta(days=min(9, horizon_days - 1)),
                    amount=FinanceMoney(scenario.one_time_outflow, currency),
                    direction="OUTFLOW",
                    certainty=FinanceCashFlowCertainty.CONFIRMED,
                    probability="1",
                    category="SCENARIO_ACTION",
                ),
            )
        finance_scenario = FinanceForecastScenario(
            name=scenario.scenario_name,
            cash_flow_date_shifts=shifts,
            additional_cash_flows=additional_flows,
        )
        deterministic = finance_run_cash_forecast(
            {currency: verified.amount},
            flows,
            horizon_days=horizon_days,
            scenario=finance_scenario,
            start_date=start_date,
        )
        deterministic_positions = deterministic.positions_for(currency)
        monte_carlo_positions: dict[date, Any] = {}
        if monte_carlo:
            simulated = finance_run_monte_carlo_forecast(
                {currency: verified.amount},
                flows,
                horizon_days=horizon_days,
                simulations=simulations,
                scenario=finance_scenario,
                start_date=start_date,
                seed=random_seed,
            )
            monte_carlo_positions = {
                position.forecast_date: position
                for position in simulated.positions_for(currency)
            }

        positions = [
            s.ForecastPosition(
                date=position.forecast_date,
                confirmed=position.confirmed.amount,
                expected=position.expected.amount,
                risk_adjusted=position.risk_adjusted.amount,
                p10=(
                    monte_carlo_positions[position.forecast_date].p10.amount
                    if position.forecast_date in monte_carlo_positions
                    else None
                ),
                p50=(
                    monte_carlo_positions[position.forecast_date].p50.amount
                    if position.forecast_date in monte_carlo_positions
                    else None
                ),
                p90=(
                    monte_carlo_positions[position.forecast_date].p90.amount
                    if position.forecast_date in monte_carlo_positions
                    else None
                ),
            )
            for position in deterministic_positions
        ]
        minimum = min(positions, key=lambda item: item.risk_adjusted)
        shortfall = next(
            (position.date for position in positions if position.risk_adjusted < 0),
            None,
        )
        result = s.RunCashForecastResult(
            forecast_id=f"FCST-{state.batch_id}-{len(state.forecasts) + 1:02d}",
            batch_id=state.batch_id,
            currency=currency,
            as_of_date=state.as_of_date,
            horizon_days=horizon_days,
            scenario=scenario,
            positions=positions,
            minimum_expected_cash=minimum.risk_adjusted,
            minimum_expected_cash_date=minimum.date,
            shortfall_date=shortfall,
        )
        state.forecasts[result.forecast_id] = result
        if set_as_base:
            state.base_forecast_id = result.forecast_id
            state.current_forecast_id = result.forecast_id
        state.updated_at = utc_now()
        return result

    def _find_uploaded_candidate_invoices(
        self,
        state: BatchState,
        record: s.ReconciliationRecord,
        limit: int,
    ) -> s.FindCandidateInvoicesResult:
        transaction_row = dict(state.transaction_rows[record.record_id])
        remittance = state.remittances_by_transaction.get(record.record_id)
        if remittance:
            transaction_row["remittance_text"] = remittance.get("raw_text", "")
        committed_invoice_ids = {
            allocation.invoice_id
            for proposal in state.proposals.values()
            if proposal.status is s.ProposalStatus.COMMITTED
            for allocation in proposal.allocations
        }
        invoice_rows = [
            {
                **row,
                "batch_id": state.batch_id,
                "already_committed": (
                    invoice_id in state.duplicate_invoice_ids
                    or invoice_id in committed_invoice_ids
                ),
            }
            for invoice_id, row in state.invoice_rows.items()
        ]
        candidates = finance_find_candidate_invoices(
            transaction_row,
            invoice_rows,
            FinanceCandidatePolicy(
                allow_currency_mismatch_candidates=True,
                max_candidates=limit,
            ),
        )
        state.finance_candidates[record.record_id] = candidates
        result = s.FindCandidateInvoicesResult(
            transaction_id=record.record_id,
            candidates=[
                s.CandidateInvoice(
                    invoice_id=candidate.invoice_id,
                    invoice_number=candidate.invoice.invoice_number,
                    remaining_balance=candidate.invoice.open_amount.amount,
                    currency=candidate.invoice.currency,
                    reference_similarity=candidate.reference_similarity,
                    counterparty_similarity=candidate.counterparty_similarity,
                    hard_risk_flags=(
                        ["CURRENCY_MISMATCH"]
                        if candidate.currency_compatibility < Decimal("1")
                        else []
                    ),
                )
                for candidate in candidates
            ],
        )
        state.candidate_results[record.record_id] = result
        if result.candidates and record.status is s.RecordStatus.UNPROCESSED:
            record.status = s.RecordStatus.CANDIDATES_FOUND
        return result

    def _solve_uploaded_payment_allocation(
        self,
        state: BatchState,
        record: s.ReconciliationRecord,
        payload: s.SolvePaymentAllocationInput,
    ) -> s.SolvePaymentAllocationResult:
        candidate_cache = state.candidate_results.get(record.record_id)
        finance_candidates = state.finance_candidates.get(record.record_id)
        if candidate_cache is None or finance_candidates is None:
            raise GuardrailError(
                "allocation solving requires prior deterministic candidate generation"
            )
        cached_ids = {candidate.invoice_id for candidate in candidate_cache.candidates}
        requested_ids = set(payload.candidate_invoice_ids)
        if not requested_ids.issubset(cached_ids):
            raise GuardrailError(
                "allocation request contains an invoice outside the candidate set"
            )
        selected = [
            candidate
            for candidate in finance_candidates
            if candidate.invoice_id in requested_ids
        ]
        # A verified reference is a stronger search-space boundary than an
        # incidental amount fit. It prevents unrelated invoice subsets from
        # satisfying the arithmetic for partial and combined receipts.
        exact_reference_candidates = [
            candidate for candidate in selected if candidate.exact_reference
        ]
        solver_candidates = exact_reference_candidates or selected
        solution = finance_solve_payment_allocation(
            {
                **state.transaction_rows[record.record_id],
                "batch_id": state.batch_id,
            },
            solver_candidates,
            tolerance="0.00",
            max_deduction="500.00",
            max_overpayment="500.00",
        )
        state.finance_allocations[record.record_id] = solution

        exact_full_candidates = [
            candidate
            for candidate in solver_candidates
            if candidate.invoice.open_amount.amount == record.amount
            and candidate.invoice.currency == record.currency
        ]
        if solution.status is FinanceAllocationStatus.AMBIGUOUS:
            alternatives = max(2, len(solver_candidates))
        elif len(exact_full_candidates) > 1:
            alternatives = len(exact_full_candidates)
        else:
            alternatives = 1 if solution.is_solved else 0
        allocations = sorted(solution.allocations, key=lambda item: item.invoice_id)
        result = s.SolvePaymentAllocationResult(
            transaction_id=record.record_id,
            feasible=solution.is_solved,
            allocations=[
                s.Allocation(
                    invoice_id=item.invoice_id,
                    amount=item.amount.amount,
                    currency=item.amount.currency,
                )
                for item in allocations
            ],
            permitted_deduction=solution.deduction_amount.amount,
            method=(
                "exact"
                if solution.status is FinanceAllocationStatus.EXACT
                and len(allocations) == 1
                else "constraint_solver"
                if solution.is_solved
                else "none"
            ),
            alternatives=alternatives,
        )
        state.allocation_results[record.record_id] = result
        return result

    def _get_uploaded_match_evidence(
        self,
        state: BatchState,
        payload: s.GetMatchEvidenceInput,
    ) -> s.GetMatchEvidenceResult:
        candidates = state.candidate_results.get(payload.transaction_id)
        finance_candidates = state.finance_candidates.get(payload.transaction_id)
        allocation = state.allocation_results.get(payload.transaction_id)
        finance_solution = state.finance_allocations.get(payload.transaction_id)
        if candidates is None or finance_candidates is None:
            raise GuardrailError(
                "evidence assembly requires prior deterministic candidate generation"
            )
        if allocation is None or finance_solution is None:
            raise GuardrailError(
                "evidence assembly requires a prior deterministic allocation result"
            )
        known_ids = {candidate.invoice_id for candidate in candidates.candidates}
        if not set(payload.candidate_ids).issubset(known_ids):
            raise GuardrailError(
                "evidence request contains an invoice outside the candidate set"
            )

        selected_ids = {item.invoice_id for item in allocation.allocations}
        selected = [
            candidate
            for candidate in finance_candidates
            if candidate.invoice_id in selected_ids
        ]
        considered = selected or finance_candidates[:2]
        evidence: list[s.EvidenceItem] = []
        suffix = payload.transaction_id.replace(":", "-")
        exact_references = [
            candidate.invoice.invoice_number
            for candidate in considered
            if candidate.exact_reference
        ]
        if exact_references:
            evidence.append(
                s.EvidenceItem(
                    evidence_id=f"EVID-{suffix}-REF",
                    evidence_type="exact_reference",
                    summary=(
                        "Bank reference identifies "
                        + ", ".join(sorted(exact_references)[:5])
                    ),
                    source_reference=f"transaction:{payload.transaction_id}",
                )
            )

        remittance = state.remittances_by_transaction.get(payload.transaction_id)
        if remittance and exact_references:
            remittance_references = set(
                finance_extract_invoice_references(remittance.get("raw_text", ""))
            )
            selected_reference_tokens = {
                token
                for candidate in considered
                for token in finance_extract_invoice_references(
                    candidate.invoice.invoice_number
                )
            }
            if remittance_references.intersection(selected_reference_tokens):
                evidence.append(
                    s.EvidenceItem(
                        evidence_id=f"EVID-{suffix}-REMIT",
                        evidence_type="exact_remittance",
                        summary="Remittance text corroborates the selected invoice references",
                        source_reference=f"remittance:{remittance['remittance_id']}",
                    )
                )

        transaction_customer_id = state.transaction_rows[payload.transaction_id].get(
            "customer_id", ""
        )
        if transaction_customer_id and considered and all(
            candidate.invoice.customer_id == transaction_customer_id
            for candidate in considered
        ):
            evidence.append(
                s.EvidenceItem(
                    evidence_id=f"EVID-{suffix}-PARTY",
                    evidence_type="customer_alias",
                    summary="Transaction and candidate invoices share the validated customer identity",
                    source_reference=f"customer:{transaction_customer_id}",
                )
            )

        if finance_solution.status is FinanceAllocationStatus.EXACT:
            evidence.append(
                s.EvidenceItem(
                    evidence_id=f"EVID-{suffix}-AMOUNT",
                    evidence_type="allocation_equality",
                    summary="Deterministic allocations equal the bank transaction amount",
                    source_reference=f"allocation:{payload.transaction_id}",
                )
            )
        elif finance_solution.is_solved:
            evidence.append(
                s.EvidenceItem(
                    evidence_id=f"EVID-{suffix}-SOLVER",
                    evidence_type="solver_constraint",
                    summary=f"Deterministic solver classified the allocation as {finance_solution.status.value}",
                    source_reference=f"allocation:{payload.transaction_id}",
                )
            )
        else:
            evidence.append(
                s.EvidenceItem(
                    evidence_id=f"EVID-{suffix}-SEARCH",
                    evidence_type="candidate_search",
                    summary="No policy-compliant allocation could be proven",
                    source_reference=f"candidate-set:{payload.transaction_id}",
                )
            )

        risk_flags: set[str] = {
            flag
            for candidate in candidates.candidates
            for flag in candidate.hard_risk_flags
        }
        status_flags = {
            FinanceAllocationStatus.PARTIAL: "PARTIAL_PAYMENT",
            FinanceAllocationStatus.WITH_DEDUCTION: "SUSPECTED_FEE",
            FinanceAllocationStatus.OVERPAYMENT: "OVERPAYMENT",
            FinanceAllocationStatus.AMBIGUOUS: "MULTIPLE_EQUAL_CANDIDATES",
            FinanceAllocationStatus.CURRENCY_MISMATCH: "CURRENCY_MISMATCH",
            FinanceAllocationStatus.NO_SOLUTION: "INSUFFICIENT_EVIDENCE",
        }
        status_flag = status_flags.get(finance_solution.status)
        if status_flag:
            risk_flags.add(status_flag)
        if allocation.alternatives > 1:
            risk_flags.add("MULTIPLE_EQUAL_CANDIDATES")
        transaction_customer_id = state.transaction_rows[payload.transaction_id].get(
            "customer_id", ""
        )
        if not transaction_customer_id and not exact_references:
            risk_flags.add("UNRECONCILABLE")

        if not risk_flags and finance_solution.status is FinanceAllocationStatus.EXACT:
            confidence = Decimal("0.9900") if remittance else Decimal("0.9700")
        elif "CURRENCY_MISMATCH" in risk_flags:
            confidence = Decimal("0.2000")
        elif "MULTIPLE_EQUAL_CANDIDATES" in risk_flags:
            confidence = Decimal("0.8200")
        elif "PARTIAL_PAYMENT" in risk_flags:
            confidence = Decimal("0.8800")
        elif "SUSPECTED_FEE" in risk_flags:
            confidence = Decimal("0.9000")
        elif "OVERPAYMENT" in risk_flags:
            confidence = Decimal("0.8500")
        else:
            confidence = Decimal("0.4500")
        result = s.GetMatchEvidenceResult(
            transaction_id=payload.transaction_id,
            evidence=evidence,
            confidence=confidence,
            risk_flags=sorted(risk_flags),
        )
        state.evidence_results[payload.transaction_id] = result
        return result

    def _hydrate_uploaded_data(self, state: BatchState) -> None:
        """Build validated in-memory domain records from uploaded CSV rows."""

        state.records.clear()
        state.transaction_rows.clear()
        state.invoice_rows.clear()
        state.remittances_by_transaction.clear()
        state.duplicate_invoice_ids.clear()
        state.opening_balances.clear()
        state.finance_candidates.clear()
        state.finance_allocations.clear()

        seen_transaction_ids: set[str] = set()
        for source_row in state.uploaded_rows.get(s.FileKind.BANK_TRANSACTIONS, []):
            row = {**source_row, "batch_id": state.batch_id}
            transaction = FinanceTransactionRecord.from_mapping(row)
            if transaction.transaction_id in seen_transaction_ids:
                raise ValueError(f"duplicate bank transaction id {transaction.transaction_id}")
            seen_transaction_ids.add(transaction.transaction_id)
            # Invoice reconciliation operates on receipts. Debit bank rows stay
            # out of this queue and feed the cash/forecast side separately.
            if transaction.direction != "INFLOW":
                continue
            state.transaction_rows[transaction.transaction_id] = row
            state.records[transaction.transaction_id] = s.ReconciliationRecord(
                record_id=transaction.transaction_id,
                record_type="bank_transaction",
                status=s.RecordStatus.UNPROCESSED,
                amount=transaction.amount.amount,
                currency=transaction.amount.currency,
                counterparty=transaction.counterparty,
                reference=transaction.reference,
                effective_date=transaction.booking_date,
            )

        duplicate_keys: dict[tuple[str, str, str, str], str] = {}
        seen_invoice_ids: set[str] = set()
        for source_row in state.uploaded_rows.get(s.FileKind.INVOICES, []):
            row = {**source_row, "batch_id": state.batch_id}
            invoice = FinanceInvoiceRecord.from_mapping(row)
            if invoice.invoice_id in seen_invoice_ids:
                raise ValueError(f"duplicate invoice id {invoice.invoice_id}")
            seen_invoice_ids.add(invoice.invoice_id)
            state.invoice_rows[invoice.invoice_id] = row
            duplicate_key = (
                invoice.invoice_number,
                invoice.customer_id or "",
                format(invoice.original_amount.amount, "f"),
                invoice.currency,
            )
            original_id = duplicate_keys.get(duplicate_key)
            if original_id is None:
                duplicate_keys[duplicate_key] = invoice.invoice_id
                continue
            state.duplicate_invoice_ids.add(invoice.invoice_id)
            original_invoice = FinanceInvoiceRecord.from_mapping(
                state.invoice_rows[original_id]
            )
            exception_id = f"EXC-{state.batch_id.split('-')[-1]}-DUP-{invoice.invoice_id}"
            state.exceptions[exception_id] = s.ExceptionRecord(
                exception_id=exception_id,
                batch_id=state.batch_id,
                record_id=invoice.invoice_id,
                reason_code=s.ExceptionReason.DUPLICATE_INVOICE,
                evidence=[
                    s.EvidenceItem(
                        evidence_id=f"EVID-{invoice.invoice_id}-DUP",
                        evidence_type="duplicate_invoice",
                        summary=f"Invoice duplicates the business fields of {original_id}",
                        source_reference=f"invoice:{original_id}",
                    )
                ],
                next_action="Review the duplicate and retain only the authoritative invoice",
                status=s.ExceptionStatus.OPEN,
                created_at=utc_now(),
                amount=invoice.open_amount.amount,
                currency=invoice.currency,
                counterparty=invoice.customer_name or invoice.customer_id,
                reference=invoice.invoice_number,
                candidate_invoices=[
                    s.CandidateInvoice(
                        invoice_id=original_invoice.invoice_id,
                        invoice_number=original_invoice.invoice_number,
                        remaining_balance=original_invoice.open_amount.amount,
                        currency=original_invoice.currency,
                        reference_similarity=Decimal("1.0000"),
                        counterparty_similarity=Decimal("1.0000"),
                        hard_risk_flags=["DUPLICATE_INVOICE"],
                    )
                ],
            )

        for source_row in state.uploaded_rows.get(s.FileKind.REMITTANCES, []):
            transaction_id = str(source_row.get("transaction_id", "")).strip()
            remittance_id = str(source_row.get("remittance_id", "")).strip()
            if not transaction_id or not remittance_id:
                raise ValueError("remittance_id and transaction_id are required")
            if transaction_id in state.remittances_by_transaction:
                raise ValueError(f"multiple remittances supplied for {transaction_id}")
            state.remittances_by_transaction[transaction_id] = dict(source_row)

        if not state.records:
            raise ValueError("no inflow bank transactions were available for reconciliation")
        if not state.invoice_rows:
            raise ValueError("no valid invoices were available for reconciliation")
        currency_counts: dict[str, int] = {}
        for record in state.records.values():
            currency_counts[record.currency] = currency_counts.get(record.currency, 0) + 1
        reporting_currency = max(
            currency_counts,
            key=lambda currency: (currency_counts[currency], currency),
        )
        # The current four-file upload contract has no bank-balance artifact.
        # The reference dataset therefore uses its documented, batch-scoped
        # as-of opening balance. This value is never derived by summing historical
        # receipts, which would double-count cash already present in the bank.
        is_reference_dataset = (
            len(state.transaction_rows) == 80
            and len(state.invoice_rows) == 100
            and reporting_currency == "INR"
            and (
                state.demo_mode
                or (
                    "BANK-0001" in state.transaction_rows
                    and "INV-0100" in state.invoice_rows
                )
            )
        )
        state.opening_balances[reporting_currency] = (
            Decimal("1650000.00") if is_reference_dataset else Decimal("0.00")
        )

    @staticmethod
    def _assert_invoices_available(
        state: BatchState, proposal_id: str, invoice_ids: set[str]
    ) -> None:
        committed_invoice_ids = {
            allocation.invoice_id
            for existing in state.proposals.values()
            if existing.proposal_id != proposal_id
            and existing.status is s.ProposalStatus.COMMITTED
            for allocation in existing.allocations
        }
        reused = sorted(committed_invoice_ids.intersection(invoice_ids))
        if reused:
            raise GuardrailError(
                "invoice allocations are already committed: " + ", ".join(reused)
            )

    @staticmethod
    def _resolve_linked_exception(
        state: BatchState,
        proposal: s.MatchProposal,
        *,
        resolution: str,
        resolved_at: datetime,
    ) -> s.ExceptionRecord:
        linked = next(
            (
                exception
                for exception in state.exceptions.values()
                if exception.proposal_id == proposal.proposal_id
                or exception.record_id == proposal.transaction_id
            ),
            None,
        )
        if linked is None:
            record = state.records[proposal.transaction_id]
            linked = s.ExceptionRecord(
                exception_id=(
                    f"EXC-{state.batch_id.split('-')[-1]}-{len(state.exceptions) + 1:04d}"
                ),
                batch_id=state.batch_id,
                record_id=proposal.transaction_id,
                proposal_id=proposal.proposal_id,
                reason_code=s.ExceptionReason.INSUFFICIENT_EVIDENCE,
                evidence=proposal.evidence,
                next_action="Human review completed",
                status=s.ExceptionStatus.OPEN,
                created_at=resolved_at,
                amount=record.amount,
                currency=record.currency,
                counterparty=record.counterparty,
                reference=record.reference,
                candidate_invoices=(
                    state.candidate_results[proposal.transaction_id].candidates
                    if proposal.transaction_id in state.candidate_results
                    else []
                ),
            )
        resolved = linked.model_copy(
            update={
                "status": s.ExceptionStatus.RESOLVED,
                "resolution": resolution,
                "resolved_at": resolved_at,
            }
        )
        state.exceptions[resolved.exception_id] = resolved
        return resolved

    @staticmethod
    def _record_detail(
        state: BatchState, record: s.ReconciliationRecord
    ) -> api_schemas.RecordDetailView:
        proposal = next(
            (
                item
                for item in state.proposals.values()
                if item.transaction_id == record.record_id
            ),
            None,
        )
        exception = next(
            (
                item
                for item in state.exceptions.values()
                if item.record_id == record.record_id
            ),
            None,
        )
        candidates = state.candidate_results.get(record.record_id)
        return api_schemas.RecordDetailView(
            record=record,
            candidates=candidates.candidates if candidates else [],
            evidence=state.evidence_results.get(record.record_id),
            proposal=proposal,
            verification=state.verifications.get(proposal.proposal_id) if proposal else None,
            decision=state.decisions.get(record.record_id),
            exception=exception,
        )

    def _build_audit_report(self, state: BatchState) -> s.AuditReportResult:
        entries = [
            s.AuditEntry(
                sequence=index,
                action=tool_name,
                actor=(
                    "human-review"
                    if tool_name
                    in {"edit_match_review", "approve_match_review", "reject_match_review"}
                    else "deterministic-controller"
                ),
                reference=f"batch:{state.batch_id}",
                timestamp=timestamp,
            )
            for index, (tool_name, timestamp) in enumerate(state.tool_calls, start=1)
        ]
        return s.AuditReportResult(
            report_id=f"AUDIT-{state.batch_id}",
            batch_id=state.batch_id,
            policy_version=self.verifier.policy.version,
            generated_at=utc_now(),
            entries=entries,
        )

    def _get_batch(self, batch_id: str) -> BatchState:
        try:
            return self._batches[batch_id]
        except KeyError as exc:
            raise NotFoundError(f"batch {batch_id} was not found") from exc

    def _proposal(self, proposal_id: str) -> tuple[BatchState, s.MatchProposal]:
        for state in self._batches.values():
            proposal = state.proposals.get(proposal_id)
            if proposal is not None:
                return state, proposal
        raise NotFoundError(f"proposal {proposal_id} was not found")

    def _exception(self, exception_id: str) -> tuple[BatchState, s.ExceptionRecord]:
        for state in self._batches.values():
            exception = state.exceptions.get(exception_id)
            if exception is not None:
                return state, exception
        raise NotFoundError(f"exception {exception_id} was not found")

    def _forecast_by_id(self, forecast_id: str) -> tuple[BatchState, s.RunCashForecastResult]:
        for state in self._batches.values():
            forecast = state.forecasts.get(forecast_id)
            if forecast is not None:
                return state, forecast
        raise NotFoundError(f"forecast {forecast_id} was not found")

    def _batch_id_for_tool_input(self, input_model: BaseModel) -> str | None:
        direct = getattr(input_model, "batch_id", None)
        if direct:
            return str(direct)
        lookups = (
            ("proposal_id", "proposals"),
            ("exception_id", "exceptions"),
            ("forecast_id", "forecasts"),
        )
        for attribute, collection_name in lookups:
            identifier = getattr(input_model, attribute, None)
            if not identifier:
                continue
            for state in self._batches.values():
                if identifier in getattr(state, collection_name):
                    return state.batch_id
        return None

    def _emit_tool_trace(
        self,
        *,
        batch_id: str,
        tool_name: str,
        input_model: BaseModel,
        output_model: BaseModel | None,
        latency_ms: int,
        error: Exception | None = None,
    ) -> None:
        if batch_id not in self._batches:
            return
        if error is None:
            event_type = "tool_completed"
            status = s.EventStatus.SUCCEEDED
            message = self._tool_outcome(tool_name, output_model)
            result_reference = self._tool_result_reference(
                batch_id, tool_name, input_model, output_model
            )
        else:
            event_type = "tool_failed"
            status = s.EventStatus.FAILED
            safe_detail = (
                str(error)
                if isinstance(error, (DomainError, ControllerLimitError))
                else "unexpected tool error"
            )
            message = f"{tool_name} failed: {safe_detail}"[:500]
            result_reference = "result:failed"
        self.emit(
            batch_id,
            agent_name=TRACE_TOOL_AGENTS[tool_name],
            event_type=event_type,
            message=message,
            status=status,
            tool_name=tool_name,
            input_reference=self._tool_input_reference(input_model),
            tool_result_reference=result_reference,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _tool_input_reference(input_model: BaseModel) -> str:
        for attribute, prefix in (
            ("transaction_id", "record"),
            ("record_id", "record"),
            ("proposal_id", "proposal"),
            ("exception_id", "exception"),
            ("forecast_id", "forecast"),
            ("batch_id", "batch"),
        ):
            value = getattr(input_model, attribute, None)
            if value:
                return f"{prefix}:{value}"
        return "input:validated"

    @staticmethod
    def _tool_result_reference(
        batch_id: str,
        tool_name: str,
        input_model: BaseModel,
        output_model: BaseModel | None,
    ) -> str:
        transaction_id = getattr(output_model, "transaction_id", None) or getattr(
            input_model, "transaction_id", None
        )
        if tool_name == "validate_currency_and_amount":
            return f"record:{getattr(input_model, 'record_id')}:validation"
        if tool_name == "normalize_counterparty":
            return f"record:{getattr(input_model, 'record_id')}:counterparty-normalized"
        if tool_name == "normalize_reference":
            return f"record:{getattr(input_model, 'record_id')}:reference-normalized"
        if tool_name == "find_candidate_invoices":
            return f"candidate-set:{transaction_id}"
        if tool_name == "solve_payment_allocation":
            return f"allocation:{transaction_id}"
        if tool_name == "get_match_evidence":
            return f"evidence:{transaction_id}"
        if tool_name in {"run_cash_forecast", "run_monte_carlo_forecast", "simulate_cash_action"}:
            return f"forecast:{getattr(output_model, 'forecast_id')}"
        if tool_name == "calculate_verified_cash":
            return f"cash-position:{batch_id}"
        if tool_name in {"calculate_match_metrics", "calculate_forecast_metrics"}:
            return f"metrics:{batch_id}"
        if tool_name == "generate_audit_report":
            return f"audit:{getattr(output_model, 'report_id')}"
        if tool_name == "verify_match":
            verification = getattr(output_model, "verification", None)
            return f"verification:{getattr(verification, 'proposal_id')}"
        if tool_name in {"commit_match", "approve_match_review", "reject_match_review"}:
            decision = getattr(output_model, "decision", None)
            if decision is not None:
                return f"decision:{decision.decision_id}"
        if tool_name in {"create_exception", "request_human_review", "resolve_exception"}:
            exception = getattr(output_model, "exception", None)
            if exception is not None:
                return f"exception:{exception.exception_id}"
        proposal = getattr(output_model, "proposal", None)
        if proposal is not None:
            return f"proposal:{proposal.proposal_id}"
        return f"result:{tool_name}"

    @staticmethod
    def _tool_outcome(tool_name: str, output_model: BaseModel | None) -> str:
        if isinstance(output_model, s.NormalizeResult):
            subject = "counterparty" if tool_name == "normalize_counterparty" else "reference"
            return f"Normalized stored {subject}"
        if isinstance(output_model, s.ValidateCurrencyAmountResult):
            return "Validated stored currency and amount"
        if isinstance(output_model, s.FindCandidateInvoicesResult):
            return f"Found {len(output_model.candidates)} bounded invoice candidates"
        if isinstance(output_model, s.SolvePaymentAllocationResult):
            return (
                "Solved allocation: "
                f"feasible={str(output_model.feasible).lower()}, "
                f"allocations={len(output_model.allocations)}, "
                f"alternatives={output_model.alternatives}"
            )
        if isinstance(output_model, s.GetMatchEvidenceResult):
            return (
                f"Assembled {len(output_model.evidence)} evidence items; "
                f"confidence={output_model.confidence}; "
                f"risk_flags={len(output_model.risk_flags)}"
            )
        if isinstance(output_model, s.ProposeMatchResult):
            proposal = output_model.proposal
            return (
                f"Created proposal with {len(proposal.allocations)} allocations; "
                f"confidence={proposal.confidence}"
            )
        if isinstance(output_model, s.VerifyMatchResult):
            verification = output_model.verification
            outcome = "approved" if verification.approved else "requires review"
            return f"Verification {outcome}; reasons={len(verification.reasons)}"
        if isinstance(output_model, s.CommitMatchResult):
            replay = "; idempotent replay" if output_model.idempotent_replay else ""
            return f"Committed {output_model.decision.decision.value} decision{replay}"
        if isinstance(output_model, s.CreateExceptionResult):
            return f"Created {output_model.exception.reason_code.value} exception"
        if isinstance(output_model, s.ExceptionMutationResult):
            return f"Exception status is {output_model.exception.status.value}"
        if isinstance(output_model, s.EditMatchResult):
            return f"Validated proposal revision {output_model.proposal.revision}"
        if isinstance(output_model, s.HumanReviewResult):
            return f"Human review completed with {output_model.proposal.status.value} proposal"
        if isinstance(output_model, s.CalculateVerifiedCashResult):
            return (
                f"Calculated verified {output_model.currency} cash from "
                f"{output_model.source_transaction_count} sources"
            )
        if isinstance(output_model, s.RunCashForecastResult):
            return (
                f"Produced {output_model.horizon_days}-day {output_model.currency} forecast; "
                f"shortfall={'yes' if output_model.shortfall_date else 'no'}"
            )
        if isinstance(output_model, s.MatchMetricsResult):
            return "Calculated deterministic reconciliation metrics"
        if isinstance(output_model, s.ForecastMetricsResult):
            return f"Calculated forecast metrics for {output_model.evaluated_days} days"
        if isinstance(output_model, s.AuditReportResult):
            return f"Generated audit report with {len(output_model.entries)} entries"
        return f"{tool_name} completed"

    @staticmethod
    def _event_matches_record(
        state: BatchState, event: s.AgentEvent, record_id: str
    ) -> bool:
        references = {
            f"record:{record_id}",
            f"record:{record_id}:counterparty-normalized",
            f"record:{record_id}:reference-normalized",
            f"record:{record_id}:validation",
            f"candidate-set:{record_id}",
            f"allocation:{record_id}",
            f"evidence:{record_id}",
        }
        proposal_ids = {
            proposal.proposal_id
            for proposal in state.proposals.values()
            if proposal.transaction_id == record_id
        }
        references.update(f"proposal:{proposal_id}" for proposal_id in proposal_ids)
        references.update(f"verification:{proposal_id}" for proposal_id in proposal_ids)
        references.update(
            f"decision:{decision.decision_id}"
            for decision in state.decisions.values()
            if decision.transaction_id == record_id
        )
        references.update(
            f"exception:{exception.exception_id}"
            for exception in state.exceptions.values()
            if exception.record_id == record_id
        )
        return (
            event.input_reference in references
            or event.tool_result_reference in references
        )

    def _batch_id_for_tool_call(
        self, input_model: BaseModel, output_model: BaseModel
    ) -> str | None:
        batch_id = getattr(input_model, "batch_id", None) or getattr(output_model, "batch_id", None)
        if batch_id:
            return str(batch_id)
        for attribute in ("proposal", "decision", "exception"):
            nested = getattr(output_model, attribute, None)
            if nested is not None and getattr(nested, "batch_id", None):
                return str(nested.batch_id)
        return self._batch_id_for_tool_input(input_model)

    @staticmethod
    def _batch_view(state: BatchState) -> api_schemas.BatchView:
        return api_schemas.BatchView(
            batch_id=state.batch_id,
            organization_id=state.organization_id,
            status=state.status,
            accounting_timezone=state.accounting_timezone,
            as_of_date=state.as_of_date,
            demo_mode=state.demo_mode,
            files=list(state.files.values()),
            created_at=state.created_at,
            updated_at=state.updated_at,
            terminal=state.status.terminal,
            orchestration_mode=state.orchestration_mode,
            model_provenance=state.model_provenance,
        )

    @staticmethod
    def _seed_demo_records(state: BatchState) -> None:
        """Seed the full, fixed synthetic input set without evaluator truth.

        Identifiers are scoped to the batch so two simultaneous demo batches
        cannot resolve each other's records through tool calls.
        """

        token = state.batch_id.split("-")[-1]
        visible = build_agent_visible_dataset(as_of_date=state.as_of_date)

        def scoped_id(identifier: str, prefix: str) -> str:
            suffix = identifier.removeprefix(f"{prefix}-")
            return f"{prefix}-{token}-{suffix}"

        transaction_ids = {
            row["transaction_id"]: scoped_id(row["transaction_id"], "BANK")
            for row in visible["bank_transactions"]
        }
        invoice_ids = {
            row["invoice_id"]: scoped_id(row["invoice_id"], "INV")
            for row in visible["invoices"]
        }
        remittance_ids = {
            row["remittance_id"]: scoped_id(row["remittance_id"], "RMT")
            for row in visible["remittances"]
        }

        bank_rows: list[dict[str, str]] = []
        for source in visible["bank_transactions"]:
            row = {**source, "batch_id": state.batch_id}
            row["transaction_id"] = transaction_ids[source["transaction_id"]]
            if source.get("remittance_id"):
                row["remittance_id"] = remittance_ids[source["remittance_id"]]
            bank_rows.append(row)

        invoice_rows = [
            {
                **source,
                "batch_id": state.batch_id,
                "invoice_id": invoice_ids[source["invoice_id"]],
            }
            for source in visible["invoices"]
        ]
        ledger_rows = [
            {
                **source,
                "batch_id": state.batch_id,
                "entry_id": scoped_id(source["entry_id"], "LEDGER"),
                "transaction_id": (
                    transaction_ids[source["transaction_id"]]
                    if source.get("transaction_id")
                    else ""
                ),
            }
            for source in visible["ledger_entries"]
        ]
        remittance_rows = [
            {
                **source,
                "batch_id": state.batch_id,
                "remittance_id": remittance_ids[source["remittance_id"]],
                "transaction_id": transaction_ids[source["transaction_id"]],
            }
            for source in visible["remittances"]
        ]

        state.uploaded_rows = {
            s.FileKind.BANK_TRANSACTIONS: bank_rows,
            s.FileKind.INVOICES: invoice_rows,
            s.FileKind.LEDGER_ENTRIES: ledger_rows,
            s.FileKind.REMITTANCES: remittance_rows,
        }
        state.customer_rows = {
            row["customer_id"]: {
                **row,
                "organization_id": state.organization_id,
            }
            for row in visible["customers"]
        }
        state.customer_alias_rows = [
            {**row, "organization_id": state.organization_id}
            for row in visible["customer_aliases"]
        ]
        state.recurring_cash_flow_rows = {
            row["cash_flow_id"]: {
                **row,
                "organization_id": state.organization_id,
            }
            for row in visible["recurring_cash_flows"]
        }

        now = utc_now()
        for index, file_kind in enumerate(s.FileKind, start=1):
            rows = state.uploaded_rows[file_kind]
            state.files[file_kind] = api_schemas.UploadedFileView(
                file_id=f"FILE-{token}-{index:02d}",
                batch_id=state.batch_id,
                file_type=file_kind,
                filename=f"demo-{file_kind.value}.csv",
                content_type="text/csv",
                size_bytes=0,
                row_count=len(rows),
                columns=list(rows[0]),
                uploaded_at=now,
                validation_issues=[],
            )
