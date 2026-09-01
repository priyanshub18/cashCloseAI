from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal

from apps.api.schemas import (
    ApproveMatchRequest,
    CreateBatchRequest,
    RejectMatchRequest,
    RunBatchRequest,
    ScenarioActionType,
    ScenarioRequest,
)
from apps.api.service import CashCloseService
from packages.agents import schemas as s
from packages.synthetic_data.generator import build_agent_visible_dataset


AS_OF_DATE = date(2026, 9, 1)


def _completed_demo() -> tuple[CashCloseService, str]:
    service = CashCloseService()
    batch = service.create_batch(
        CreateBatchRequest(as_of_date=AS_OF_DATE, demo_mode=True)
    )
    result = service.run_batch(batch.batch_id, RunBatchRequest(horizon_days=30))
    assert result.controller.status is s.BatchStatus.COMPLETED
    return service, batch.batch_id


def test_agent_visible_demo_builder_excludes_truth_and_has_contract_counts() -> None:
    visible = build_agent_visible_dataset(as_of_date=AS_OF_DATE)

    assert set(visible) == {
        "bank_transactions",
        "invoices",
        "ledger_entries",
        "remittances",
        "customers",
        "customer_aliases",
        "recurring_cash_flows",
    }
    assert {name: len(rows) for name, rows in visible.items()} == {
        "bank_transactions": 80,
        "invoices": 100,
        "ledger_entries": 70,
        "remittances": 40,
        "customers": 20,
        "customer_aliases": 10,
        "recurring_cash_flows": 15,
    }
    assert all("expected_action" not in row for rows in visible.values() for row in rows)
    assert all("scenario" not in row for rows in visible.values() for row in rows)


def test_full_demo_reconciliation_and_forecast_complete_with_expected_shape() -> None:
    service, batch_id = _completed_demo()
    state = service._batches[batch_id]

    assert {kind.value: len(rows) for kind, rows in state.uploaded_rows.items()} == {
        "bank_transactions": 80,
        "invoices": 100,
        "ledger_entries": 70,
        "remittances": 40,
    }
    assert Counter(record.status for record in state.records.values()) == {
        s.RecordStatus.AUTO_RECONCILED: 45,
        s.RecordStatus.NEEDS_REVIEW: 35,
    }
    assert Counter(proposal.status for proposal in state.proposals.values()) == {
        s.ProposalStatus.COMMITTED: 45,
        s.ProposalStatus.NEEDS_REVIEW: 15,
    }
    assert Counter(exception.reason_code for exception in state.exceptions.values()) == {
        s.ExceptionReason.PARTIAL_PAYMENT: 10,
        s.ExceptionReason.SUSPECTED_FEE: 5,
        s.ExceptionReason.OVERPAYMENT: 5,
        s.ExceptionReason.CURRENCY_MISMATCH: 5,
        s.ExceptionReason.AMBIGUOUS_MATCH: 5,
        s.ExceptionReason.UNRECONCILABLE: 5,
        s.ExceptionReason.DUPLICATE_INVOICE: 5,
    }

    metrics = state.match_metrics
    assert metrics is not None
    assert metrics.currency == "INR"
    assert metrics.automation_coverage == Decimal("0.5625")
    assert metrics.value_reconciled == Decimal("7118545.00")
    assert metrics.unresolved_value == Decimal("4345530.85")

    forecast = state.forecasts[state.base_forecast_id or ""]
    assert forecast.currency == "INR"
    assert len(forecast.positions) == 30
    assert all(position.p10 is not None for position in forecast.positions)
    assert forecast.shortfall_date == date(2026, 9, 18)


def test_cash_and_metrics_include_manual_approval_but_exclude_rejection() -> None:
    service, batch_id = _completed_demo()
    state = service._batches[batch_id]
    before_metrics = state.match_metrics
    assert before_metrics is not None
    before_cash = service.invoke(
        "calculate_verified_cash",
        s.CalculateVerifiedCashInput(batch_id=batch_id, as_of_date=AS_OF_DATE),
    )
    assert isinstance(before_cash, s.CalculateVerifiedCashResult)

    reviewable = [
        proposal
        for proposal in state.proposals.values()
        if proposal.status is s.ProposalStatus.NEEDS_REVIEW
    ]
    approved_proposal, rejected_proposal = reviewable[:2]
    approved_amount = state.records[approved_proposal.transaction_id].amount

    approved = service.approve_match(
        approved_proposal.proposal_id,
        ApproveMatchRequest(
            expected_revision=approved_proposal.revision,
            idempotency_key=f"human:{batch_id}:approve:test",
            approval_note="ERP owner confirmed the referenced partial allocation",
        ),
    )
    assert approved.decision is not None
    assert approved.decision.decision is s.Decision.MANUALLY_RECONCILED

    rejected = service.reject_match(
        rejected_proposal.proposal_id,
        RejectMatchRequest(
            expected_revision=rejected_proposal.revision,
            rejection_reason="Customer confirmed this receipt belongs to another invoice",
        ),
    )
    assert rejected.decision is not None
    assert rejected.decision.decision is s.Decision.REJECTED

    after_metrics = service.invoke(
        "calculate_match_metrics",
        s.CalculateMatchMetricsInput(batch_id=batch_id),
    )
    after_cash = service.invoke(
        "calculate_verified_cash",
        s.CalculateVerifiedCashInput(batch_id=batch_id, as_of_date=AS_OF_DATE),
    )
    assert isinstance(after_metrics, s.MatchMetricsResult)
    assert isinstance(after_cash, s.CalculateVerifiedCashResult)
    assert after_metrics.value_reconciled == before_metrics.value_reconciled + approved_amount
    assert after_metrics.unresolved_value == before_metrics.unresolved_value - approved_amount
    assert after_metrics.automation_coverage == before_metrics.automation_coverage
    assert after_cash.amount == before_cash.amount == Decimal("1650000.00")
    assert after_cash.source_transaction_count == before_cash.source_transaction_count + 1
    assert after_cash.excluded_unverified_count == before_cash.excluded_unverified_count - 1


def test_demo_scenario_changes_cash_path_without_replacing_base_forecast() -> None:
    service, batch_id = _completed_demo()
    base = service.get_forecast(batch_id)

    scenario = service.run_scenario(
        batch_id,
        ScenarioRequest(
            name="Acme pays seven days late",
            action_type=ScenarioActionType.CUSTOMER_PAYMENT_DELAY,
            customer_name="Acme",
            delay_days=7,
        ),
    )

    assert scenario.forecast_id != base.forecast_id
    assert scenario.currency == base.currency == "INR"
    assert service.get_forecast(batch_id).forecast_id == base.forecast_id
    changed_dates = [
        base_position.date
        for base_position, scenario_position in zip(
            base.positions, scenario.positions, strict=True
        )
        if base_position.expected != scenario_position.expected
    ]
    assert changed_dates == [date(2026, 9, day) for day in range(24, 31)]
    assert all(
        position.p10 <= position.p50 <= position.p90
        for position in scenario.positions
        if position.p10 is not None
        and position.p50 is not None
        and position.p90 is not None
    )
