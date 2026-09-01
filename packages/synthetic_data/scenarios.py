"""Scenario contract for the reproducible CashClose evaluation dataset.

Primary transaction scenarios are disjoint and total 80 bank transactions.
Cross-cutting scenarios intentionally overlap selected transactions or invoices.
The public input files never contain these labels; they are written only to the
private ground-truth directory and the count-only manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ScenarioName(StrEnum):
    EXACT_MATCH = "exact_match"
    REFERENCE_VARIATION = "reference_variation"
    CUSTOMER_ALIAS = "customer_alias"
    PARTIAL_PAYMENT = "partial_payment"
    COMBINED_PAYMENT = "combined_payment"
    DUPLICATE_INVOICE = "duplicate_invoice"
    FEE_DEDUCTION = "fee_deduction"
    OVERPAYMENT = "overpayment"
    CURRENCY_MISMATCH = "currency_mismatch"
    MISSING_REMITTANCE = "missing_remittance"
    AMBIGUOUS_MATCH = "ambiguous_match"
    UNRECONCILABLE = "unreconcilable"


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: ScenarioName
    count: int
    expected_disposition: str
    hard_risk: bool = False
    cross_cutting: bool = False


PRIMARY_TRANSACTION_SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(ScenarioName.EXACT_MATCH, 20, "AUTO_RECONCILED"),
    ScenarioSpec(ScenarioName.REFERENCE_VARIATION, 15, "AUTO_RECONCILED"),
    ScenarioSpec(ScenarioName.PARTIAL_PAYMENT, 10, "NEEDS_REVIEW"),
    ScenarioSpec(ScenarioName.COMBINED_PAYMENT, 10, "AUTO_RECONCILED"),
    ScenarioSpec(ScenarioName.FEE_DEDUCTION, 5, "NEEDS_REVIEW"),
    ScenarioSpec(ScenarioName.OVERPAYMENT, 5, "NEEDS_REVIEW"),
    ScenarioSpec(ScenarioName.CURRENCY_MISMATCH, 5, "UNRESOLVED", hard_risk=True),
    ScenarioSpec(ScenarioName.AMBIGUOUS_MATCH, 5, "UNRESOLVED", hard_risk=True),
    ScenarioSpec(ScenarioName.UNRECONCILABLE, 5, "UNRESOLVED", hard_risk=True),
)


CROSS_CUTTING_SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        ScenarioName.CUSTOMER_ALIAS,
        10,
        "AUTO_RECONCILED",
        cross_cutting=True,
    ),
    ScenarioSpec(
        ScenarioName.MISSING_REMITTANCE,
        5,
        "CONTINUE_WITH_EVIDENCE",
        cross_cutting=True,
    ),
    ScenarioSpec(
        ScenarioName.DUPLICATE_INVOICE,
        5,
        "UNRESOLVED",
        hard_risk=True,
        cross_cutting=True,
    ),
)


SCENARIO_SPECS: tuple[ScenarioSpec, ...] = (
    *PRIMARY_TRANSACTION_SCENARIOS,
    *CROSS_CUTTING_SCENARIOS,
)

EXPECTED_SCENARIO_COUNTS: dict[str, int] = {
    spec.name.value: spec.count for spec in SCENARIO_SPECS
}

TOTAL_BANK_TRANSACTIONS = sum(spec.count for spec in PRIMARY_TRANSACTION_SCENARIOS)

if TOTAL_BANK_TRANSACTIONS != 80:  # Defensive: changing the plan must be explicit.
    raise RuntimeError("primary synthetic scenarios must total 80 bank transactions")

