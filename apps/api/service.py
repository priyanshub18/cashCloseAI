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

from packages.agents.controller import DeterministicController
from packages.agents.openai_adapter import OpenAIAdapterNotConfigured, OpenAIResponsesAdapter
from packages.agents import schemas as s
from packages.agents.tools import TOOL_CONTRACTS, validate_tool_input, validate_tool_output
from packages.agents.verifier import MatchVerifier
from packages.finance import (
    calculate_verified_cash as finance_calculate_verified_cash,
    normalize_counterparty as finance_normalize_counterparty,
    normalize_reference as finance_normalize_reference,
    solve_payment_allocation as finance_solve_payment_allocation,
    validate_currency_and_amount as finance_validate_currency_and_amount,
)

from . import schemas as api_schemas


MAX_UPLOAD_BYTES = 10_000_000
TERMINAL_RECORD_STATUSES = {
    s.RecordStatus.AUTO_RECONCILED,
    s.RecordStatus.NEEDS_REVIEW,
    s.RecordStatus.UNRESOLVED,
    s.RecordStatus.REJECTED,
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
    records: dict[str, s.ReconciliationRecord] = field(default_factory=dict)
    candidate_results: dict[str, s.FindCandidateInvoicesResult] = field(default_factory=dict)
    allocation_results: dict[str, s.SolvePaymentAllocationResult] = field(default_factory=dict)
    evidence_results: dict[str, s.GetMatchEvidenceResult] = field(default_factory=dict)
    proposals: dict[str, s.MatchProposal] = field(default_factory=dict)
    verifications: dict[str, s.VerificationResult] = field(default_factory=dict)
    decisions: dict[str, s.ReconciliationDecision] = field(default_factory=dict)
    idempotency_results: dict[str, tuple[str, s.CommitMatchResult]] = field(default_factory=dict)
    exceptions: dict[str, s.ExceptionRecord] = field(default_factory=dict)
    forecasts: dict[str, s.RunCashForecastResult] = field(default_factory=dict)
    current_forecast_id: str | None = None
    match_metrics: s.MatchMetricsResult | None = None
    forecast_metrics: s.ForecastMetricsResult | None = None
    audit_report: s.AuditReportResult | None = None
    events: list[s.AgentEvent] = field(default_factory=list)
    tool_calls: list[tuple[str, datetime]] = field(default_factory=list)
    processing_started_at: float | None = None
    processing_time_ms: int = 0


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
        if request.use_model_planner:
            if not self.responses_adapter.is_configured:
                raise OpenAIAdapterNotConfigured(
                    "model planning was requested but OPENAI_API_KEY is not configured"
                )
            plan = self.responses_adapter.plan(
                {
                    "batch_id": batch_id,
                    "status": s.BatchStatus.UPLOADED.value,
                    "objective": "Choose the first safe inspection or validation tool",
                }
            )
            disallowed = [
                call.tool_name
                for call in plan.calls
                if call.tool_name not in {"inspect_batch", "validate_batch", "get_batch_summary"}
            ]
            if disallowed:
                raise GuardrailError(
                    "initial model plan crossed its observe-only authorization boundary"
                )
            mode = "responses-guided-with-deterministic-execution"
            self.emit(
                batch_id,
                agent_name=s.AgentName.CONTROLLER,
                event_type="model_plan_validated",
                message=f"Validated {len(plan.calls)} model-selected observe-only tool calls",
                tool_result_reference=f"response:{plan.response_id}",
            )

        controller = DeterministicController(self)
        try:
            result = controller.run(
                batch_id,
                as_of_date=self._get_batch(batch_id).as_of_date,
                horizon_days=request.horizon_days,
            )
        except Exception:
            with self._lock:
                state = self._get_batch(batch_id)
                if not state.status.terminal:
                    state.status = s.BatchStatus.PROCESSING_FAILED
                    state.updated_at = utc_now()
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
            return api_schemas.MatchList(
                items=list(state.decisions.values()),
                proposals=list(state.proposals.values()),
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

    def get_forecast(self, batch_id: str) -> s.RunCashForecastResult:
        with self._lock:
            state = self._get_batch(batch_id)
            if not state.current_forecast_id:
                raise ConflictError("the batch does not have a forecast yet")
            return state.forecasts[state.current_forecast_id]

    def run_scenario(
        self, batch_id: str, request: api_schemas.ScenarioRequest
    ) -> s.RunCashForecastResult:
        with self._lock:
            state = self._get_batch(batch_id)
            if state.status is not s.BatchStatus.COMPLETED:
                raise ConflictError("scenarios require a completed verified batch")
        parameters = s.ScenarioParameters(
            scenario_name=request.name,
            customer_name=request.customer_name,
            delay_days=request.delay_days,
            one_time_outflow=request.amount or Decimal("0.00"),
            currency=request.currency,
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
            return state.audit_report

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
            result = handler(validated_input)
            validated_output = validate_tool_output(tool_name, result)
            batch_id = self._batch_id_for_tool_call(validated_input, validated_output)
            if batch_id and batch_id in self._batches:
                self._batches[batch_id].tool_calls.append((tool_name, utc_now()))
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
                    latency_ms=0,
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
        if not state.demo_mode:
            missing = [kind.value for kind in s.FileKind if kind not in state.files]
            if missing:
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

    def _record(self, record_id: str) -> tuple[BatchState, s.ReconciliationRecord]:
        for state in self._batches.values():
            record = state.records.get(record_id)
            if record is not None:
                return state, record
        raise NotFoundError(f"record {record_id} was not found")

    def _tool_normalize_counterparty(self, payload: s.NormalizeRecordInput) -> s.NormalizeResult:
        _, record = self._record(payload.record_id)
        normalized = finance_normalize_counterparty(record.counterparty)
        return s.NormalizeResult(
            record_id=payload.record_id,
            normalized_value=normalized,
            transformations=["uppercase", "legal_suffix_removed", "whitespace_collapsed"],
        )

    def _tool_normalize_reference(self, payload: s.NormalizeRecordInput) -> s.NormalizeResult:
        _, record = self._record(payload.record_id)
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
        _, record = self._record(payload.record_id)
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
        state, record = self._record(payload.transaction_id)
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
        _, record = self._record(payload.transaction_id)
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
        state, record = self._record(payload.transaction_id)
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
        state, _ = self._record(payload.transaction_id)
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
        _, record = self._record(payload.transaction_id)
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
        status = s.ProposalStatus.VERIFIED if verification.approved else s.ProposalStatus.REJECTED
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
        _, record = self._record(payload.record_id)
        for existing in state.exceptions.values():
            if existing.record_id == payload.record_id and existing.status is not s.ExceptionStatus.RESOLVED:
                return s.CreateExceptionResult(exception=existing)
        exception = s.ExceptionRecord(
            exception_id=(
                f"EXC-{state.batch_id.split('-')[-1]}-{len(state.exceptions) + 1:04d}"
            ),
            batch_id=payload.batch_id,
            record_id=payload.record_id,
            reason_code=payload.reason_code,
            evidence=payload.evidence,
            next_action=payload.next_action,
            status=s.ExceptionStatus.OPEN,
            created_at=utc_now(),
        )
        state.exceptions[exception.exception_id] = exception
        record.status = s.RecordStatus.NEEDS_REVIEW
        state.updated_at = utc_now()
        return s.CreateExceptionResult(exception=exception)

    def _tool_list_related_exceptions(
        self, payload: s.ListRelatedExceptionsInput
    ) -> s.ListRelatedExceptionsResult:
        return s.ListRelatedExceptionsResult(
            exceptions=[
                exception
                for state in self._batches.values()
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

    def _tool_calculate_verified_cash(
        self, payload: s.CalculateVerifiedCashInput
    ) -> s.CalculateVerifiedCashResult:
        state = self._get_batch(payload.batch_id)
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
            for decision in state.decisions.values()
            if decision.decision is s.Decision.AUTO_RECONCILED
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
    ) -> s.RunCashForecastResult:
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
        if state.demo_mode:
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
            committed_value = sum(
                (state.records[item.transaction_id].amount for item in state.decisions.values()),
                Decimal("0.00"),
            )
            record_count = max(1, len(state.records))
            auto_count = len(state.decisions)
            coverage = (Decimal(auto_count) / Decimal(record_count)).quantize(Decimal("0.0001"))
            metrics = s.MatchMetricsResult(
                batch_id=payload.batch_id,
                precision=Decimal("1.0000") if auto_count else Decimal("0.0000"),
                recall=coverage,
                automation_coverage=coverage,
                value_weighted_coverage=coverage,
                false_approval_rate=Decimal("0.0000"),
                exception_recall=Decimal("1.0000") if state.exceptions else Decimal("0.0000"),
                value_reconciled=committed_value,
                unresolved_value=sum(
                    (state.records[item.record_id].amount for item in state.exceptions.values()),
                    Decimal("0.00"),
                ),
                currency="USD",
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
        entries = [
            s.AuditEntry(
                sequence=index,
                action=tool_name,
                actor="deterministic-controller",
                reference=f"batch:{payload.batch_id}",
                timestamp=timestamp,
            )
            for index, (tool_name, timestamp) in enumerate(state.tool_calls, start=1)
        ]
        report = s.AuditReportResult(
            report_id=f"AUDIT-{payload.batch_id}",
            batch_id=payload.batch_id,
            policy_version=self.verifier.policy.version,
            generated_at=utc_now(),
            entries=entries,
        )
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

    @staticmethod
    def _batch_id_for_tool_call(input_model: BaseModel, output_model: BaseModel) -> str | None:
        batch_id = getattr(input_model, "batch_id", None) or getattr(output_model, "batch_id", None)
        if batch_id:
            return str(batch_id)
        for attribute in ("proposal", "decision", "exception"):
            nested = getattr(output_model, attribute, None)
            if nested is not None and getattr(nested, "batch_id", None):
                return str(nested.batch_id)
        return None

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
        )

    @staticmethod
    def _seed_demo_records(state: BatchState) -> None:
        token = state.batch_id.split("-")[-1]
        combined_id = f"BANK-{token}-0042"
        ambiguous_id = f"BANK-{token}-0077"
        state.records = {
            combined_id: s.ReconciliationRecord(
                record_id=combined_id,
                record_type="bank_transaction",
                status=s.RecordStatus.UNPROCESSED,
                amount=Decimal("84250.00"),
                currency="USD",
                counterparty="ACME PVT LTD",
                reference="NEFT ACME PVT 1831 1834 SETTLEMENT",
                effective_date=state.as_of_date,
            ),
            ambiguous_id: s.ReconciliationRecord(
                record_id=ambiguous_id,
                record_type="bank_transaction",
                status=s.RecordStatus.UNPROCESSED,
                amount=Decimal("50000.00"),
                currency="USD",
                counterparty="NORTHWIND TRADING",
                reference="WIRE NORTHWIND SETTLEMENT",
                effective_date=state.as_of_date,
            ),
        }
