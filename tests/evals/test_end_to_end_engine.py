from __future__ import annotations

from datetime import date
from decimal import Decimal

from apps.api.schemas import CreateBatchRequest, RunBatchRequest
from apps.api.service import CashCloseService
from packages.agents import schemas as s
from packages.evaluation.matching_metrics import calculate_match_metrics
from packages.finance import AllocationStatus
from packages.synthetic_data.generator import generate_dataset


def test_uploaded_reference_batch_matches_private_truth_only_after_run(tmp_path) -> None:
    generated = generate_dataset(tmp_path / "reference")
    service = CashCloseService()
    batch = service.create_batch(
        CreateBatchRequest(as_of_date=date(2026, 9, 1), demo_mode=False)
    )
    for file_kind in s.FileKind:
        path = generated.input_dir / f"{file_kind.value}.csv"
        service.upload_csv(
            batch.batch_id,
            file_type=file_kind,
            filename=path.name,
            content_type="text/csv",
            content=path.read_bytes(),
        )

    run = service.run_batch(batch.batch_id, RunBatchRequest(horizon_days=30))
    assert run.controller.status is s.BatchStatus.COMPLETED
    state = service._batches[batch.batch_id]

    # The independent evaluator serializes only tool outputs. Private truth is
    # loaded below, after controller execution, and never enters BatchState.
    predictions: list[dict[str, object]] = []
    for transaction_id, solution in state.finance_allocations.items():
        candidate_by_id = {
            candidate.invoice_id: candidate
            for candidate in state.finance_candidates.get(transaction_id, [])
        }
        if not solution.is_solved or not solution.allocations:
            continue
        if not all(
            candidate_by_id[allocation.invoice_id].exact_reference
            for allocation in solution.allocations
        ):
            continue
        adjustments: list[dict[str, str]] = []
        if solution.status is AllocationStatus.WITH_DEDUCTION:
            adjustments.append(
                {
                    "type": "BANK_FEE",
                    "amount": format(solution.deduction_amount.amount, "f"),
                    "currency": solution.deduction_amount.currency,
                    "effect_on_transaction": "DEDUCT",
                }
            )
        elif solution.status is AllocationStatus.OVERPAYMENT:
            adjustments.append(
                {
                    "type": "UNALLOCATED_OVERPAYMENT",
                    "amount": format(solution.overpayment_amount.amount, "f"),
                    "currency": solution.overpayment_amount.currency,
                    "effect_on_transaction": "RESIDUAL",
                }
            )
        predictions.append(
            {
                "transaction_id": transaction_id,
                "decision": (
                    "AUTO_RECONCILED"
                    if transaction_id in state.decisions
                    else "NEEDS_REVIEW"
                ),
                "transaction_amount": format(solution.transaction_amount.amount, "f"),
                "currency": solution.transaction_amount.currency,
                "allocations": [allocation.to_dict() for allocation in solution.allocations],
                "adjustments": adjustments,
            }
        )

    predicted_exceptions = [
        {
            "record_id": exception.record_id,
            "record_type": (
                "INVOICE" if exception.record_id in state.invoice_rows else "BANK_TRANSACTION"
            ),
        }
        for exception in state.exceptions.values()
    ]
    metrics = calculate_match_metrics(
        predictions,
        generated.private_ground_truth_dir / "expected_matches.json",
        predicted_exceptions=predicted_exceptions,
        ground_truth_exceptions=(
            generated.private_ground_truth_dir / "expected_exceptions.json"
        ),
        eligible_record_count=80,
    )

    assert metrics.precision == 1
    assert metrics.recall == 1
    assert metrics.automation_coverage == Decimal("0.562500")
    assert metrics.value_weighted_coverage == Decimal("0.782348")
    assert metrics.false_approval_rate == 0
    assert metrics.exception_recall == 1
    assert metrics.automatic_matches == metrics.correct_automatic_matches == 45
    assert metrics.discovered_matches == metrics.correct_discovered_matches == 65
    assert metrics.correctly_escalated_exceptions == metrics.unsafe_exceptions == 40
