"""Bounded controller orchestration for a CashClose batch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from . import schemas as s
from .tools import FinancialToolPort


@dataclass(frozen=True, slots=True)
class ControllerPolicy:
    maximum_tool_calls: int = 80
    maximum_records_per_run: int = 500
    maximum_same_strategy_retries: int = 1


class ControllerLimitError(RuntimeError):
    pass


class DeterministicController:
    """A reproducible controller used by the demo and as the model fallback.

    It decides the next tool from structured results, while every amount, score,
    mutation and terminal-state check remains inside the tool implementation.
    """

    def __init__(self, tools: FinancialToolPort, policy: ControllerPolicy | None = None) -> None:
        self.tools = tools
        self.policy = policy or ControllerPolicy()
        self._tool_calls = 0

    def _call(self, name: str, arguments: BaseModel | dict[str, Any]) -> BaseModel:
        if self._tool_calls >= self.policy.maximum_tool_calls:
            raise ControllerLimitError("controller tool-call limit reached")
        self._tool_calls += 1
        return self.tools.invoke(name, arguments)

    def run(self, batch_id: str, *, as_of_date: date, horizon_days: int = 30) -> s.ControllerRunResult:
        self._tool_calls = 0
        automatic_matches = 0
        exceptions_created = 0

        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.CONTROLLER,
            event_type="run_started",
            message="Controller started a bounded reconciliation run",
            status=s.EventStatus.STARTED,
        )
        self.tools.transition(batch_id, s.BatchStatus.VALIDATING)
        inspection = self._call("inspect_batch", s.InspectBatchInput(batch_id=batch_id))
        assert isinstance(inspection, s.InspectBatchResult)
        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.CONTROLLER,
            event_type="batch_inspected",
            message=f"Inspected {inspection.total_records} records",
            tool_name="inspect_batch",
            tool_result_reference=f"batch:{batch_id}:inspection",
        )
        validation = self._call("validate_batch", s.ValidateBatchInput(batch_id=batch_id))
        assert isinstance(validation, s.ValidateBatchResult)
        errors = [issue for issue in validation.issues if issue.severity == "error"]
        if errors:
            self.tools.transition(batch_id, s.BatchStatus.VALIDATION_FAILED)
            self.tools.emit(
                batch_id,
                agent_name=s.AgentName.CONTROLLER,
                event_type="validation_failed",
                message=f"Validation stopped the batch with {len(errors)} blocking issue groups",
                status=s.EventStatus.FAILED,
                tool_name="validate_batch",
            )
            return s.ControllerRunResult(
                batch_id=batch_id,
                status=s.BatchStatus.VALIDATION_FAILED,
                tool_calls=self._tool_calls,
                automatic_matches=0,
                exceptions_created=0,
            )

        warning_count = sum(issue.count for issue in validation.issues)
        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.CONTROLLER,
            event_type="batch_validated",
            message=f"Validated batch with {warning_count} non-blocking data warnings",
            status=s.EventStatus.WARNING if warning_count else s.EventStatus.SUCCEEDED,
            tool_name="validate_batch",
        )
        self.tools.transition(batch_id, s.BatchStatus.NORMALIZING)
        records_result = self._call(
            "get_unprocessed_records",
            s.GetUnprocessedRecordsInput(batch_id=batch_id, limit=self.policy.maximum_records_per_run),
        )
        assert isinstance(records_result, s.GetUnprocessedRecordsResult)
        for record in records_result.records:
            self._call("normalize_counterparty", s.NormalizeRecordInput(record_id=record.record_id))
            self._call("normalize_reference", s.NormalizeRecordInput(record_id=record.record_id))
            self._call("validate_currency_and_amount", s.ValidateCurrencyAmountInput(record_id=record.record_id))
        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.RECONCILIATION,
            event_type="records_normalized",
            message=f"Normalized {len(records_result.records)} eligible records using validated transforms",
            tool_name="normalize_reference",
        )

        self.tools.transition(batch_id, s.BatchStatus.RECONCILING)
        proposal_ids: list[str] = []
        for record in records_result.records:
            candidates = self._call(
                "find_candidate_invoices",
                s.FindCandidateInvoicesInput(transaction_id=record.record_id, limit=20),
            )
            assert isinstance(candidates, s.FindCandidateInvoicesResult)
            if not candidates.candidates:
                evidence = [
                    s.EvidenceItem(
                        evidence_id=f"EVID-{record.record_id}-EMPTY",
                        evidence_type="candidate_search",
                        summary="No invoice survived deterministic candidate filters",
                        source_reference=f"transaction:{record.record_id}",
                    )
                ]
                self._call(
                    "create_exception",
                    s.CreateExceptionInput(
                        batch_id=batch_id,
                        record_id=record.record_id,
                        reason_code=s.ExceptionReason.MISSING_INVOICE,
                        evidence=evidence,
                        next_action="Request the missing invoice or remittance from accounts receivable",
                    ),
                )
                exceptions_created += 1
                continue

            allocation = self._call(
                "solve_payment_allocation",
                s.SolvePaymentAllocationInput(
                    transaction_id=record.record_id,
                    candidate_invoice_ids=[candidate.invoice_id for candidate in candidates.candidates],
                ),
            )
            assert isinstance(allocation, s.SolvePaymentAllocationResult)
            evidence = self._call(
                "get_match_evidence",
                s.GetMatchEvidenceInput(
                    transaction_id=record.record_id,
                    candidate_ids=[candidate.invoice_id for candidate in candidates.candidates],
                ),
            )
            assert isinstance(evidence, s.GetMatchEvidenceResult)

            unsafe = (
                not allocation.feasible
                or allocation.alternatives > 1
                or bool(evidence.risk_flags)
                or evidence.confidence < Decimal("0.7500")
            )
            if unsafe:
                reason = (
                    s.ExceptionReason.AMBIGUOUS_MATCH
                    if allocation.alternatives > 1 or "MULTIPLE_EQUAL_CANDIDATES" in evidence.risk_flags
                    else s.ExceptionReason.INSUFFICIENT_EVIDENCE
                )
                self._call(
                    "create_exception",
                    s.CreateExceptionInput(
                        batch_id=batch_id,
                        record_id=record.record_id,
                        reason_code=reason,
                        evidence=evidence.evidence,
                        next_action="Review candidate invoices and attach authoritative remittance evidence",
                    ),
                )
                exceptions_created += 1
                continue

            proposal = self._call(
                "propose_match",
                s.ProposeMatchInput(
                    batch_id=batch_id,
                    transaction_id=record.record_id,
                    transaction_amount=record.amount,
                    currency=record.currency,
                    allocations=allocation.allocations,
                    permitted_deduction=allocation.permitted_deduction,
                    confidence=evidence.confidence,
                    evidence=evidence.evidence,
                    risk_flags=evidence.risk_flags,
                ),
            )
            assert isinstance(proposal, s.ProposeMatchResult)
            proposal_ids.append(proposal.proposal.proposal_id)

        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.RECONCILIATION,
            event_type="candidates_processed",
            message=f"Prepared {len(proposal_ids)} proposals and explained {exceptions_created} exceptions",
            tool_name="propose_match",
        )

        self.tools.transition(batch_id, s.BatchStatus.VERIFYING)
        for proposal_id in proposal_ids:
            verified = self._call("verify_match", s.VerifyMatchInput(proposal_id=proposal_id))
            assert isinstance(verified, s.VerifyMatchResult)
            if not verified.verification.approved:
                self._call(
                    "create_exception",
                    s.CreateExceptionInput(
                        batch_id=batch_id,
                        record_id=verified.proposal.transaction_id,
                        reason_code=s.ExceptionReason.INSUFFICIENT_EVIDENCE,
                        evidence=verified.proposal.evidence,
                        next_action="Review verifier rejections before considering a manual match",
                    ),
                )
                exceptions_created += 1
                continue
            commit = self._call(
                "commit_match",
                s.CommitMatchInput(
                    proposal_id=proposal_id,
                    idempotency_key=f"{batch_id}:{proposal_id}:policy-v1",
                ),
            )
            assert isinstance(commit, s.CommitMatchResult)
            if not commit.idempotent_replay:
                automatic_matches += 1

        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.VERIFICATION,
            event_type="verification_completed",
            message=f"Auto-approved {automatic_matches} proposals after deterministic verification",
            tool_name="commit_match",
        )

        self.tools.transition(batch_id, s.BatchStatus.FORECASTING)
        self._call(
            "calculate_verified_cash",
            s.CalculateVerifiedCashInput(batch_id=batch_id, as_of_date=as_of_date),
        )
        forecast = self._call(
            "run_monte_carlo_forecast",
            s.RunMonteCarloForecastInput(
                batch_id=batch_id,
                horizon_days=horizon_days,
                scenario=s.ScenarioParameters(),
                simulations=500,
                random_seed=20260901,
            ),
        )
        assert isinstance(forecast, s.RunCashForecastResult)
        forecast_message = (
            f"Detected a projected shortfall on {forecast.shortfall_date.isoformat()}"
            if forecast.shortfall_date
            else f"Forecast minimum is {forecast.minimum_expected_cash} {forecast.currency}"
        )
        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.FORECAST,
            event_type="forecast_completed",
            message=forecast_message,
            tool_name="run_monte_carlo_forecast",
            tool_result_reference=f"forecast:{forecast.forecast_id}",
        )

        self.tools.transition(batch_id, s.BatchStatus.EVALUATING)
        self._call("calculate_match_metrics", s.CalculateMatchMetricsInput(batch_id=batch_id))
        audit = self._call("generate_audit_report", s.GenerateAuditReportInput(batch_id=batch_id))
        assert isinstance(audit, s.AuditReportResult)
        finalized = self._call("finalize_batch", s.FinalizeBatchInput(batch_id=batch_id))
        assert isinstance(finalized, s.FinalizeBatchResult)
        final_status = s.BatchStatus.COMPLETED if finalized.finalized else s.BatchStatus.PROCESSING_FAILED
        self.tools.transition(batch_id, final_status)
        self.tools.emit(
            batch_id,
            agent_name=s.AgentName.CONTROLLER,
            event_type="run_completed" if finalized.finalized else "run_failed",
            message=(
                "Completed reconciliation, forecast, evaluation and audit"
                if finalized.finalized
                else f"Could not finalize {len(finalized.nonterminal_record_ids)} nonterminal records"
            ),
            status=s.EventStatus.SUCCEEDED if finalized.finalized else s.EventStatus.FAILED,
            tool_name="finalize_batch",
        )
        return s.ControllerRunResult(
            batch_id=batch_id,
            status=final_status,
            tool_calls=self._tool_calls,
            automatic_matches=automatic_matches,
            exceptions_created=exceptions_created,
            forecast_id=forecast.forecast_id,
            report_id=audit.report_id,
        )
