from datetime import date
from decimal import Decimal

import pytest

from packages.finance.allocation_solver import (
    AllocationCandidate,
    AllocationStatus,
    GlobalAllocationStatus,
    solve_global_allocation,
    solve_payment_allocation,
    verify_allocation,
)
from packages.finance.candidate_generation import (
    CandidatePolicy,
    find_candidate_invoices,
)
from packages.finance.normalization import Money
from packages.finance.scoring import (
    CandidateFeatures,
    Decision,
    RiskFlag,
    score_candidate,
    score_candidates,
)


def _transaction(amount: str = "84250.00", reference: str = "INV 0031 INV 0032") -> dict[str, str]:
    return {
        "transaction_id": "BANK-0042",
        "batch_id": "B-1",
        "booking_date": "2026-08-21",
        "amount": amount,
        "currency": "INR",
        "direction": "CREDIT",
        "counterparty": "Acme Pvt Ltd",
        "reference": reference,
        "status": "UNPROCESSED",
    }


def _invoice(
    invoice_id: str,
    invoice_number: str,
    amount: str,
    *,
    currency: str = "INR",
    customer_id: str = "CUS-ACME",
    customer_name: str = "ACME PRIVATE LIMITED",
) -> dict[str, object]:
    return {
        "invoice_id": invoice_id,
        "batch_id": "B-1",
        "customer_id": customer_id,
        "customer_name": customer_name,
        "invoice_number": invoice_number,
        "issue_date": "2026-08-01",
        "due_date": "2026-08-31",
        "original_amount": amount,
        "open_amount": amount,
        "currency": currency,
        "status": "OPEN",
    }


def test_candidate_generation_filters_and_ranks_exact_references() -> None:
    invoices = [
        _invoice("INV-31", "INV-0031", "50000.00"),
        _invoice("INV-32", "INV-0032", "34250.00"),
        _invoice("INV-USD", "INV-0031", "50000.00", currency="USD"),
        {**_invoice("INV-OLD", "OLD-9999", "84250.00"), "issue_date": "2024-01-01"},
    ]
    candidates = find_candidate_invoices(
        _transaction(),
        invoices,
        CandidatePolicy(max_invoice_age_days=180),
        customer_aliases={"Acme Pvt": "CUS-ACME"},
    )

    assert [candidate.invoice_id for candidate in candidates] == ["INV-31", "INV-32"]
    assert all(candidate.exact_reference for candidate in candidates)
    assert all(candidate.customer_identity_match for candidate in candidates)
    assert all(candidate.currency_compatibility == Decimal("1") for candidate in candidates)


def test_amount_only_candidate_is_retained_but_cannot_auto_reconcile() -> None:
    transaction = {
        **_transaction(amount="100865.00", reference="INCOMING TRANSFER NO DETAILS 9076"),
        "customer_id": "",
        "counterparty": "UNKNOWN COUNTERPARTY",
    }
    candidates = find_candidate_invoices(
        transaction,
        [_invoice("INV-NOISE", "INV-2026-0046", "100600.00", customer_id="OTHER")],
    )
    assert len(candidates) == 1
    result = score_candidate(candidates[0])
    assert RiskFlag.ONLY_AMOUNT_MATCHES in result.risk_flags
    assert result.decision != Decision.AUTO_RECONCILE


def test_confidence_policy_is_interpretable_and_hard_flags_override_score() -> None:
    perfect = CandidateFeatures(
        reference_similarity="1",
        amount_compatibility="1",
        counterparty_similarity="1",
        date_compatibility="1",
        remittance_evidence="1",
        currency_compatibility="1",
        historical_pattern="1",
    )
    result = score_candidate(perfect)
    assert result.raw_score == Decimal("1.000")
    assert result.confidence == Decimal("1.000")
    assert result.decision == Decision.AUTO_RECONCILE

    contradicted = score_candidate(
        perfect, risk_flags=[RiskFlag.CURRENCY_INCONSISTENT]
    )
    assert contradicted.decision == Decision.EXCEPTION
    assert contradicted.hard_risk_flags == (RiskFlag.CURRENCY_INCONSISTENT,)


def test_equal_candidates_receive_ambiguity_penalty() -> None:
    candidates = find_candidate_invoices(
        _transaction(amount="100.00", reference="payment"),
        [
            _invoice("INV-A", "DUP-100", "100.00"),
            _invoice("INV-B", "DUP-100", "100.00"),
        ],
        customer_aliases={"Acme": "CUS-ACME"},
    )
    ranked = score_candidates(
        candidates,
        remittance_evidence={candidate.invoice_id: "1" for candidate in candidates},
        historical_patterns={candidate.invoice_id: "1" for candidate in candidates},
    )
    assert len(ranked) == 2
    assert all(
        RiskFlag.MULTIPLE_PLAUSIBLE_CANDIDATES in item.score.risk_flags
        for item in ranked
    )
    assert all(item.score.decision != Decision.AUTO_RECONCILE for item in ranked)


def test_allocation_fallback_solves_combined_payment_exactly() -> None:
    solution = solve_payment_allocation(
        _transaction(),
        [
            AllocationCandidate("INV-31", Money("50000.00", "INR"), "0.99"),
            AllocationCandidate("INV-32", Money("34250.00", "INR"), "0.98"),
            AllocationCandidate("INV-99", Money("10000.00", "INR"), "0.60"),
        ],
        prefer_ortools=False,
    )

    assert solution.status == AllocationStatus.EXACT
    assert {allocation.invoice_id for allocation in solution.allocations} == {
        "INV-31",
        "INV-32",
    }
    assert solution.total_allocated.amount == Decimal("84250.00")
    assert solution.solver == "deterministic_mitm"
    assert verify_allocation(solution) == (True, ())


def test_allocation_handles_permitted_deduction_and_unique_partial() -> None:
    deduction = solve_payment_allocation(
        _transaction(amount="980.00", reference="INV-1 less fee"),
        [AllocationCandidate("INV-1", Money("1000.00", "INR"), "0.99")],
        max_deduction="20.00",
        prefer_ortools=False,
    )
    assert deduction.status == AllocationStatus.WITH_DEDUCTION
    assert deduction.deduction_amount.amount == Decimal("20.00")
    assert verify_allocation(deduction) == (True, ())

    partial = solve_payment_allocation(
        _transaction(amount="400.00", reference="part INV-2"),
        [AllocationCandidate("INV-2", Money("1000.00", "INR"), "0.99")],
        prefer_ortools=False,
    )
    assert partial.status == AllocationStatus.PARTIAL
    assert partial.allocations[0].amount.amount == Decimal("400.00")


def test_allocation_abstains_on_ambiguous_partial_and_rejects_duplicate_ids() -> None:
    candidates = [
        AllocationCandidate("INV-1", Money("1000", "INR"), "0.80"),
        AllocationCandidate("INV-2", Money("900", "INR"), "0.79"),
    ]
    result = solve_payment_allocation(
        _transaction(amount="400", reference="unknown"),
        candidates,
        prefer_ortools=False,
    )
    assert result.status == AllocationStatus.AMBIGUOUS
    assert result.allocations == ()

    with pytest.raises(ValueError, match="must be unique"):
        solve_payment_allocation(
            _transaction(amount="400", reference="unknown"),
            [candidates[0], candidates[0]],
            prefer_ortools=False,
        )


def test_global_solver_supports_multiple_payments_for_one_invoice() -> None:
    first = _transaction(amount="60.00", reference="part 1")
    second = {**_transaction(amount="40.00", reference="part 2"), "transaction_id": "BANK-0043"}
    shared_invoice = AllocationCandidate(
        "INV-100", Money("100.00", "INR"), "0.99"
    )
    result = solve_global_allocation(
        [first, second],
        {
            "BANK-0042": [shared_invoice],
            "BANK-0043": [shared_invoice],
        },
        prefer_ortools=False,
    )

    assert result.status == GlobalAllocationStatus.COMPLETE
    assert sum(
        allocation.amount.amount for allocation in result.allocations
    ) == Decimal("100.00")
    assert {allocation.transaction_id for allocation in result.allocations} == {
        "BANK-0042",
        "BANK-0043",
    }
    assert result.unallocated_transactions == {}
    assert result.remaining_invoice_balances == {}
    assert result.solver == "deterministic_max_flow"
