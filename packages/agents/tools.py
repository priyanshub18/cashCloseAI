"""Validated tool catalog exposed to the controller and optional model planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from . import schemas as s


SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    side_effecting: bool = False
    agent_visible: bool = True

    def responses_definition(self) -> dict[str, Any]:
        """Return a strict custom-function declaration for the Responses API."""

        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.input_model.model_json_schema(),
            "strict": True,
        }


def _tool(
    name: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    *,
    side_effecting: bool = False,
    agent_visible: bool = True,
) -> ToolContract:
    return ToolContract(name, description, input_model, output_model, side_effecting, agent_visible)


_CONTRACT_LIST = [
    _tool("inspect_batch", "Return record and file counts for a batch.", s.InspectBatchInput, s.InspectBatchResult),
    _tool("validate_batch", "Validate uploaded schemas and finance invariants without mutating records.", s.ValidateBatchInput, s.ValidateBatchResult),
    _tool("get_batch_summary", "Return lifecycle and terminal-record counts.", s.GetBatchSummaryInput, s.BatchSummaryResult),
    _tool("get_unprocessed_records", "Return a bounded page of eligible unprocessed records.", s.GetUnprocessedRecordsInput, s.GetUnprocessedRecordsResult),
    _tool("finalize_batch", "Finalize only when every eligible record is terminal.", s.FinalizeBatchInput, s.FinalizeBatchResult, side_effecting=True),
    _tool("normalize_counterparty", "Normalize a record counterparty without changing its amount.", s.NormalizeRecordInput, s.NormalizeResult),
    _tool("normalize_reference", "Normalize a record reference without changing source evidence.", s.NormalizeRecordInput, s.NormalizeResult),
    _tool("resolve_customer_alias", "Resolve a counterparty through approved aliases.", s.ResolveCustomerAliasInput, s.ResolveCustomerAliasResult),
    _tool("validate_currency_and_amount", "Validate a stored currency and Decimal amount.", s.ValidateCurrencyAmountInput, s.ValidateCurrencyAmountResult),
    _tool("find_candidate_invoices", "Generate bounded invoice candidates using deterministic filters.", s.FindCandidateInvoicesInput, s.FindCandidateInvoicesResult),
    _tool("find_candidate_ledger_entries", "Generate bounded ledger candidates using deterministic filters.", s.FindCandidateLedgerEntriesInput, s.FindCandidateLedgerEntriesResult),
    _tool("parse_remittance_text", "Extract remittance hints; deterministic tools must verify them.", s.ParseRemittanceTextInput, s.ParseRemittanceTextResult),
    _tool("solve_payment_allocation", "Solve constrained payment allocation using stored Decimal values.", s.SolvePaymentAllocationInput, s.SolvePaymentAllocationResult),
    _tool("get_match_evidence", "Assemble persisted evidence and an interpretable policy score.", s.GetMatchEvidenceInput, s.GetMatchEvidenceResult),
    _tool("propose_match", "Persist a validated, uncommitted match proposal.", s.ProposeMatchInput, s.ProposeMatchResult, side_effecting=True),
    _tool("verify_match", "Apply verifier policy and hard contradiction checks.", s.VerifyMatchInput, s.VerifyMatchResult, side_effecting=True),
    _tool("commit_match", "Commit only a verified proposal; requires an idempotency key.", s.CommitMatchInput, s.CommitMatchResult, side_effecting=True),
    _tool("create_exception", "Create an explained exception with evidence and a next action.", s.CreateExceptionInput, s.CreateExceptionResult, side_effecting=True),
    _tool("list_related_exceptions", "List exceptions associated with a record.", s.ListRelatedExceptionsInput, s.ListRelatedExceptionsResult),
    _tool("request_human_review", "Move an exception into the human-review queue.", s.RequestHumanReviewInput, s.ExceptionMutationResult, side_effecting=True),
    _tool("resolve_exception", "Resolve an exception using an explicit human resolution.", s.ResolveExceptionInput, s.ExceptionMutationResult, side_effecting=True),
    _tool("calculate_verified_cash", "Calculate cash exclusively from verified stored values.", s.CalculateVerifiedCashInput, s.CalculateVerifiedCashResult),
    _tool("get_expected_receivables", "Return receivables for a bounded date range.", s.ExpectedReceivablesInput, s.CashFlowListResult),
    _tool("get_committed_payables", "Return committed payables for a bounded date range.", s.CommittedPayablesInput, s.CashFlowListResult),
    _tool("run_cash_forecast", "Run the deterministic daily cash forecast.", s.RunCashForecastInput, s.RunCashForecastResult),
    _tool("run_monte_carlo_forecast", "Run seeded deterministic Monte Carlo simulations.", s.RunMonteCarloForecastInput, s.RunCashForecastResult),
    _tool("simulate_cash_action", "Run a deterministic what-if action against verified cash.", s.SimulateCashActionInput, s.RunCashForecastResult),
    _tool("explain_forecast_movement", "Return calculated forecast drivers and evidence references.", s.ExplainForecastMovementInput, s.ExplainForecastMovementResult),
    _tool("calculate_match_metrics", "Calculate matching metrics from persisted decisions.", s.CalculateMatchMetricsInput, s.MatchMetricsResult),
    _tool("calculate_forecast_metrics", "Calculate forecast accuracy from evaluator-owned actuals.", s.CalculateForecastMetricsInput, s.ForecastMetricsResult, agent_visible=False),
    _tool("generate_audit_report", "Generate an evidence-linked batch audit report.", s.GenerateAuditReportInput, s.AuditReportResult),
    _tool("compare_with_ground_truth", "Evaluator-only comparison; never exposed to an agent.", s.CompareWithGroundTruthInput, s.CompareWithGroundTruthResult, agent_visible=False),
]

TOOL_CONTRACTS: dict[str, ToolContract] = {contract.name: contract for contract in _CONTRACT_LIST}


def responses_tool_definitions(*, include_side_effects: bool = True) -> list[dict[str, Any]]:
    """Return only agent-safe definitions, optionally excluding all write tools."""

    return [
        contract.responses_definition()
        for contract in _CONTRACT_LIST
        if contract.agent_visible and (include_side_effects or not contract.side_effecting)
    ]


class FinancialToolPort(Protocol):
    """Runtime port. Implementations own calculations, persistence and audit logging."""

    def invoke(self, tool_name: str, arguments: BaseModel | dict[str, Any]) -> BaseModel:
        ...

    def transition(self, batch_id: str, status: s.BatchStatus) -> None:
        ...

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
        ...


class ToolContractError(ValueError):
    """Raised when a planner requests a missing or malformed tool call."""


def validate_tool_input(tool_name: str, arguments: BaseModel | dict[str, Any]) -> BaseModel:
    contract = TOOL_CONTRACTS.get(tool_name)
    if contract is None:
        raise ToolContractError(f"unknown tool: {tool_name}")
    if isinstance(arguments, contract.input_model):
        return arguments
    payload = arguments.model_dump() if isinstance(arguments, BaseModel) else arguments
    return contract.input_model.model_validate(payload)


def validate_tool_output(tool_name: str, result: BaseModel | dict[str, Any]) -> BaseModel:
    contract = TOOL_CONTRACTS.get(tool_name)
    if contract is None:
        raise ToolContractError(f"unknown tool: {tool_name}")
    if isinstance(result, contract.output_model):
        return result
    payload = result.model_dump() if isinstance(result, BaseModel) else result
    return contract.output_model.model_validate(payload)

