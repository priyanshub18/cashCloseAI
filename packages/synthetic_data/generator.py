"""Generate the reproducible CashClose demo and hidden evaluation truth.

The generator intentionally uses only the Python standard library and Decimal.
It writes agent-visible inputs and evaluator-only truth into sibling directories:

    <output>/input/
    <output>/private_ground_truth/

Nothing in an input CSV reveals a planted scenario label or expected decision.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Supports both ``python -m`` and direct script execution.
    from .scenarios import EXPECTED_SCENARIO_COUNTS, ScenarioName
except ImportError:  # pragma: no cover - exercised only by direct CLI execution.
    from scenarios import EXPECTED_SCENARIO_COUNTS, ScenarioName


DEFAULT_SEED = 20260901
DEFAULT_AS_OF_DATE = date(2026, 9, 1)
DEFAULT_OUTPUT_DIR = Path("work/synthetic_demo")
BATCH_ID = "BATCH-DEMO-001"
ORGANIZATION_ID = "ORG-DEMO-001"
ACCOUNTING_TIMEZONE = "Asia/Kolkata"
MONEY_QUANTUM = Decimal("0.01")


CUSTOMER_NAMES: tuple[str, ...] = (
    "Acme Private Limited",
    "BluePeak Retail Limited",
    "Cedar Labs India Private Limited",
    "Delta Industrial Systems Limited",
    "Evergreen Foods Private Limited",
    "Falcon Mobility Limited",
    "Granite Works India Private Limited",
    "Harborline Logistics Limited",
    "Indigo Health Systems Private Limited",
    "Juniper Digital Commerce Limited",
    "Keystone Energy Services Limited",
    "Lotus Consumer Products Private Limited",
    "Meridian Analytics India Limited",
    "Northstar Components Private Limited",
    "Orchid Hospitality Services Limited",
    "Pioneer Office Solutions Private Limited",
    "Quartz Media Networks Limited",
    "Riverbend Engineering Private Limited",
    "Summit Learning Systems Limited",
    "Trident Packaging India Private Limited",
)

CUSTOMER_ALIASES: tuple[str, ...] = (
    "ACME PVT",
    "BLUEPEAK RETAIL",
    "CEDAR LABS INDIA",
    "DELTA INDUSTRIAL",
    "EVERGREEN FOODS",
    "FALCON MOBILITY",
    "GRANITE WORKS",
    "HARBORLINE LOGISTICS",
    "INDIGO HEALTH",
    "JUNIPER COMMERCE",
)


@dataclass(frozen=True, slots=True)
class GenerationSummary:
    output_dir: Path
    input_dir: Path
    private_ground_truth_dir: Path
    seed: int
    row_counts: dict[str, int]
    scenario_counts: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "input_dir": str(self.input_dir),
            "private_ground_truth_dir": str(self.private_ground_truth_dir),
            "seed": self.seed,
            "row_counts": self.row_counts,
            "scenario_counts": self.scenario_counts,
        }


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _money_text(value: Decimal | int | str) -> str:
    return format(_money(value), "f")


def _random_amount(rng: random.Random) -> Decimal:
    # Amount is generated in integer minor units, avoiding binary floating point.
    minor_units = rng.randrange(400_000, 25_000_000, 500)
    return _money(Decimal(minor_units) / Decimal(100))


def _customer_id(number: int) -> str:
    return f"CUST-{number:03d}"


def _invoice_id(number: int) -> str:
    return f"INV-{number:04d}"


def _invoice_number(number: int) -> str:
    return f"INV-2026-{number:04d}"


def _transaction_id(number: int) -> str:
    return f"BANK-{number:04d}"


def _remittance_id(number: int) -> str:
    return f"RMT-{number:04d}"


def _iso_timestamp(day: date, hour: int = 7, minute: int = 30) -> str:
    return datetime.combine(
        day,
        time(hour=hour, minute=minute),
        tzinfo=timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty CSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65_536), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_customers() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    customers: list[dict[str, str]] = []
    aliases: list[dict[str, str]] = []
    for index, name in enumerate(CUSTOMER_NAMES, start=1):
        customer_id = _customer_id(index)
        probability = Decimal("0.96") - Decimal((index - 1) % 8) * Decimal("0.025")
        customers.append(
            {
                "customer_id": customer_id,
                "organization_id": ORGANIZATION_ID,
                "canonical_name": name,
                "default_currency": "INR",
                "mean_payment_delay_days": str((index * 3) % 13),
                "payment_delay_stddev_days": str(2 + (index % 5)),
                "payment_probability": format(probability, "f"),
                "status": "ACTIVE",
            }
        )
        if index <= len(CUSTOMER_ALIASES):
            aliases.append(
                {
                    "alias_id": f"ALIAS-{index:03d}",
                    "organization_id": ORGANIZATION_ID,
                    "customer_id": customer_id,
                    "alias": CUSTOMER_ALIASES[index - 1],
                    "normalized_alias": CUSTOMER_ALIASES[index - 1].lower(),
                    "approved": "true",
                }
            )
    return customers, aliases


def _make_invoice(
    *,
    number: int,
    customer_number: int,
    amount: Decimal,
    issue_date: date,
    currency: str = "INR",
    invoice_number: str | None = None,
) -> dict[str, str]:
    amount_text = _money_text(amount)
    return {
        "invoice_id": _invoice_id(number),
        "batch_id": BATCH_ID,
        "customer_id": _customer_id(customer_number),
        "invoice_number": invoice_number or _invoice_number(number),
        "invoice_date": issue_date.isoformat(),
        "issue_date": issue_date.isoformat(),
        "due_date": (issue_date + timedelta(days=30)).isoformat(),
        "amount": amount_text,
        "original_amount": amount_text,
        "remaining_balance": amount_text,
        "open_amount": amount_text,
        "currency": currency,
        "status": "OPEN",
        "payment_reference": f"PAY-{number:04d}",
    }


def _build_invoices(
    rng: random.Random,
    as_of_date: date,
) -> tuple[list[dict[str, str]], dict[int, dict[str, str]]]:
    invoices_by_number: dict[int, dict[str, str]] = {}

    for number in range(1, 81):
        customer_number = ((number - 1) % len(CUSTOMER_NAMES)) + 1
        if 26 <= number <= 35:  # Ten alias cases use the ten approved aliases.
            customer_number = number - 25
        if 46 <= number <= 65:  # Each adjacent pair belongs to one customer.
            pair_number = (number - 46) // 2
            customer_number = (pair_number % len(CUSTOMER_NAMES)) + 1
        issue_date = as_of_date - timedelta(days=rng.randint(18, 75))
        invoices_by_number[number] = _make_invoice(
            number=number,
            customer_number=customer_number,
            amount=_random_amount(rng),
            issue_date=issue_date,
        )

    # Five ambiguous transactions each have two indistinguishable candidates.
    for pair_number in range(5):
        first_number = 81 + pair_number * 2
        second_number = first_number + 1
        amount = _random_amount(rng)
        customer_number = 11 + pair_number
        issue_date = as_of_date - timedelta(days=35 + pair_number)
        for invoice_number in (first_number, second_number):
            invoices_by_number[invoice_number] = _make_invoice(
                number=invoice_number,
                customer_number=customer_number,
                amount=amount,
                issue_date=issue_date,
            )

    # Five duplicate rows (96-100), each duplicating the external identity of 91-95.
    for offset in range(5):
        source_number = 91 + offset
        duplicate_number = 96 + offset
        customer_number = 16 + offset
        amount = _random_amount(rng)
        issue_date = as_of_date - timedelta(days=25 + offset)
        source = _make_invoice(
            number=source_number,
            customer_number=customer_number,
            amount=amount,
            issue_date=issue_date,
        )
        duplicate = _make_invoice(
            number=duplicate_number,
            customer_number=customer_number,
            amount=amount,
            issue_date=issue_date,
            invoice_number=source["invoice_number"],
        )
        duplicate["payment_reference"] = source["payment_reference"]
        invoices_by_number[source_number] = source
        invoices_by_number[duplicate_number] = duplicate

    invoices = [invoices_by_number[number] for number in range(1, 101)]
    return invoices, invoices_by_number


def _make_bank_transaction(
    *,
    number: int,
    customer_id: str,
    counterparty: str,
    reference: str,
    amount: Decimal,
    currency: str,
    transaction_date: date,
    remittance_id: str = "",
    payment_reference: str = "",
) -> dict[str, str]:
    return {
        "transaction_id": _transaction_id(number),
        "batch_id": BATCH_ID,
        "customer_id": customer_id,
        "transaction_date": transaction_date.isoformat(),
        "booking_date": transaction_date.isoformat(),
        "value_date": transaction_date.isoformat(),
        "amount": _money_text(amount),
        "currency": currency,
        "direction": "CREDIT",
        "counterparty": counterparty,
        "reference": reference,
        "payment_reference": payment_reference,
        "remittance_id": remittance_id,
        "status": "UNPROCESSED",
    }


def _allocation(invoice: dict[str, str], amount: Decimal | None = None) -> dict[str, str]:
    return {
        "invoice_id": invoice["invoice_id"],
        "amount": _money_text(amount if amount is not None else invoice["open_amount"]),
        "currency": invoice["currency"],
    }


def _truth_match(
    transaction: dict[str, str],
    *,
    scenarios: Iterable[ScenarioName],
    allocations: Sequence[dict[str, str]],
    expected_action: str,
    adjustments: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    return {
        "transaction_id": transaction["transaction_id"],
        "scenarios": [scenario.value for scenario in scenarios],
        "is_reconcilable": True,
        "expected_action": expected_action,
        "transaction_amount": transaction["amount"],
        "currency": transaction["currency"],
        "allocations": list(allocations),
        "adjustments": list(adjustments),
    }


def _truth_exception(
    *,
    record_id: str,
    record_type: str,
    reason_code: str,
    scenarios: Iterable[ScenarioName],
    evidence: Sequence[str],
    next_action: str,
    related_record_ids: Sequence[str] = (),
    unsafe: bool = True,
    severity: str = "ERROR",
) -> dict[str, Any]:
    return {
        "record_id": record_id,
        "record_type": record_type,
        "reason_code": reason_code,
        "scenarios": [scenario.value for scenario in scenarios],
        "unsafe": unsafe,
        "severity": severity,
        "evidence": list(evidence),
        "next_action": next_action,
        "related_record_ids": list(related_record_ids),
    }


def _reference_variation(invoice_number: str, counterparty: str, variant: int) -> str:
    numeric_tail = invoice_number.rsplit("-", maxsplit=1)[-1]
    patterns = (
        f"NEFT {counterparty} {numeric_tail} SETTLEMENT",
        f"RTGS/{invoice_number.replace('-', '')}/{counterparty}",
        f"WIRE {counterparty} INV {numeric_tail} PAID",
    )
    return patterns[variant % len(patterns)]


def _build_transactions_and_truth(
    rng: random.Random,
    as_of_date: date,
    invoices_by_number: dict[int, dict[str, str]],
) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    customers_by_id = {
        _customer_id(index): name for index, name in enumerate(CUSTOMER_NAMES, start=1)
    }
    alias_by_customer_id = {
        _customer_id(index): alias
        for index, alias in enumerate(CUSTOMER_ALIASES, start=1)
    }
    transactions: list[dict[str, str]] = []
    remittances: list[dict[str, str]] = []
    expected_matches: list[dict[str, Any]] = []
    expected_exceptions: list[dict[str, Any]] = []

    def transaction_day() -> date:
        return as_of_date - timedelta(days=rng.randint(0, 12))

    def add_remittance(
        transaction: dict[str, str],
        number: int,
        raw_text: str,
    ) -> None:
        remittances.append(
            {
                "remittance_id": _remittance_id(number),
                "batch_id": BATCH_ID,
                "transaction_id": transaction["transaction_id"],
                "received_at": _iso_timestamp(date.fromisoformat(transaction["transaction_date"])),
                "sender": transaction["counterparty"],
                "raw_text": raw_text,
                "source": "EMAIL",
            }
        )

    # 1-20: deterministic exact reference, amount, currency, and customer matches.
    for number in range(1, 21):
        invoice = invoices_by_number[number]
        counterparty = customers_by_id[invoice["customer_id"]]
        transaction = _make_bank_transaction(
            number=number,
            customer_id=invoice["customer_id"],
            counterparty=counterparty,
            reference=f"{invoice['payment_reference']} {invoice['invoice_number']}",
            amount=_money(invoice["open_amount"]),
            currency=invoice["currency"],
            transaction_date=transaction_day(),
            remittance_id=_remittance_id(number),
            payment_reference=invoice["payment_reference"],
        )
        transactions.append(transaction)
        expected_matches.append(
            _truth_match(
                transaction,
                scenarios=(ScenarioName.EXACT_MATCH,),
                allocations=(_allocation(invoice),),
                expected_action="AUTO_RECONCILED",
            )
        )
        add_remittance(
            transaction,
            number,
            f"Payment advice for {invoice['invoice_number']}; {invoice['currency']} "
            f"{invoice['open_amount']} in full.",
        )

    # 21-35: format-noisy references. 26-35 use approved aliases; 31-35 have
    # intentionally missing remittance advice but remain safely matchable.
    for number in range(21, 36):
        invoice = invoices_by_number[number]
        canonical_name = customers_by_id[invoice["customer_id"]]
        uses_alias = number >= 26
        counterparty = (
            alias_by_customer_id[invoice["customer_id"]] if uses_alias else canonical_name
        )
        has_remittance = number <= 30
        scenarios = [ScenarioName.REFERENCE_VARIATION]
        if uses_alias:
            scenarios.append(ScenarioName.CUSTOMER_ALIAS)
        if not has_remittance:
            scenarios.append(ScenarioName.MISSING_REMITTANCE)
        transaction = _make_bank_transaction(
            number=number,
            customer_id=invoice["customer_id"],
            counterparty=counterparty,
            reference=_reference_variation(invoice["invoice_number"], counterparty, number),
            amount=_money(invoice["open_amount"]),
            currency=invoice["currency"],
            transaction_date=transaction_day(),
            remittance_id=_remittance_id(number) if has_remittance else "",
        )
        transactions.append(transaction)
        expected_matches.append(
            _truth_match(
                transaction,
                scenarios=scenarios,
                allocations=(_allocation(invoice),),
                expected_action="AUTO_RECONCILED",
            )
        )
        if has_remittance:
            add_remittance(
                transaction,
                number,
                f"Settlement reference {invoice['invoice_number'].replace('-', ' ')} from "
                f"{counterparty}.",
            )
        else:
            expected_exceptions.append(
                _truth_exception(
                    record_id=transaction["transaction_id"],
                    record_type="BANK_TRANSACTION",
                    reason_code="MISSING_REMITTANCE",
                    scenarios=(ScenarioName.MISSING_REMITTANCE,),
                    evidence=(
                        "No remittance record is linked to the bank transaction.",
                        "Normalized invoice reference, amount, currency, and customer still agree.",
                    ),
                    next_action="Continue only if deterministic reference checks pass.",
                    unsafe=False,
                    severity="WARNING",
                )
            )

    # 36-45: partial payments. The allocation equals cash received, not the full invoice.
    for number in range(36, 46):
        invoice = invoices_by_number[number]
        counterparty = customers_by_id[invoice["customer_id"]]
        ratio = Decimal(40 + rng.randint(0, 35)) / Decimal(100)
        paid_amount = _money(_money(invoice["open_amount"]) * ratio)
        has_remittance = number <= 40
        transaction = _make_bank_transaction(
            number=number,
            customer_id=invoice["customer_id"],
            counterparty=counterparty,
            reference=f"PART PAYMENT {invoice['invoice_number']}",
            amount=paid_amount,
            currency=invoice["currency"],
            transaction_date=transaction_day(),
            remittance_id=_remittance_id(number) if has_remittance else "",
        )
        transactions.append(transaction)
        expected_matches.append(
            _truth_match(
                transaction,
                scenarios=(ScenarioName.PARTIAL_PAYMENT,),
                allocations=(_allocation(invoice, paid_amount),),
                expected_action="NEEDS_REVIEW",
            )
        )
        expected_exceptions.append(
            _truth_exception(
                record_id=transaction["transaction_id"],
                record_type="BANK_TRANSACTION",
                reason_code="PARTIAL_PAYMENT",
                scenarios=(ScenarioName.PARTIAL_PAYMENT,),
                evidence=(
                    f"Payment {_money_text(paid_amount)} is below invoice balance {invoice['open_amount']}.",
                    f"Invoice reference {invoice['invoice_number']} is present.",
                ),
                next_action="Confirm that the residual invoice balance should remain open.",
            )
        )
        if has_remittance:
            add_remittance(
                transaction,
                number,
                f"Part payment {_money_text(paid_amount)} against {invoice['invoice_number']}.",
            )

    # 46-55: ten payments, each settling two invoices for one customer.
    for transaction_number in range(46, 56):
        pair_offset = transaction_number - 46
        first_invoice = invoices_by_number[46 + pair_offset * 2]
        second_invoice = invoices_by_number[47 + pair_offset * 2]
        total = _money(first_invoice["open_amount"]) + _money(second_invoice["open_amount"])
        counterparty = customers_by_id[first_invoice["customer_id"]]
        has_remittance = transaction_number <= 50
        transaction = _make_bank_transaction(
            number=transaction_number,
            customer_id=first_invoice["customer_id"],
            counterparty=counterparty,
            reference=(
                f"NEFT {counterparty} {first_invoice['invoice_number']} "
                f"{second_invoice['invoice_number']} SETTLEMENT"
            ),
            amount=total,
            currency=first_invoice["currency"],
            transaction_date=transaction_day(),
            remittance_id=_remittance_id(transaction_number) if has_remittance else "",
        )
        transactions.append(transaction)
        allocations = (_allocation(first_invoice), _allocation(second_invoice))
        expected_matches.append(
            _truth_match(
                transaction,
                scenarios=(ScenarioName.COMBINED_PAYMENT,),
                allocations=allocations,
                expected_action="AUTO_RECONCILED",
            )
        )
        if has_remittance:
            add_remittance(
                transaction,
                transaction_number,
                f"Combined settlement for {first_invoice['invoice_number']} and "
                f"{second_invoice['invoice_number']} totaling INR {_money_text(total)}.",
            )

    # 56-60: invoice is settled net of a small bank/processing fee.
    for transaction_number, invoice_number in zip(range(56, 61), range(66, 71), strict=True):
        invoice = invoices_by_number[invoice_number]
        counterparty = customers_by_id[invoice["customer_id"]]
        fee = _money(Decimal(rng.randrange(25_00, 250_00, 25)) / Decimal(100))
        received = _money(_money(invoice["open_amount"]) - fee)
        transaction = _make_bank_transaction(
            number=transaction_number,
            customer_id=invoice["customer_id"],
            counterparty=counterparty,
            reference=f"{invoice['invoice_number']} SETTLEMENT LESS CHGS",
            amount=received,
            currency=invoice["currency"],
            transaction_date=transaction_day(),
        )
        transactions.append(transaction)
        expected_matches.append(
            _truth_match(
                transaction,
                scenarios=(ScenarioName.FEE_DEDUCTION,),
                allocations=(_allocation(invoice),),
                expected_action="NEEDS_REVIEW",
                adjustments=(
                    {
                        "type": "BANK_FEE",
                        "amount": _money_text(fee),
                        "currency": invoice["currency"],
                        "effect_on_transaction": "DEDUCT",
                    },
                ),
            )
        )
        expected_exceptions.append(
            _truth_exception(
                record_id=transaction["transaction_id"],
                record_type="BANK_TRANSACTION",
                reason_code="SUSPECTED_FEE",
                scenarios=(ScenarioName.FEE_DEDUCTION,),
                evidence=(
                    f"Invoice exceeds cash received by {_money_text(fee)} {invoice['currency']}.",
                    "Bank narrative states LESS CHGS.",
                ),
                next_action="Approve the permitted fee adjustment or request remittance evidence.",
            )
        )

    # 61-65: payment exceeds the only evidenced invoice and leaves a residual.
    for transaction_number, invoice_number in zip(range(61, 66), range(71, 76), strict=True):
        invoice = invoices_by_number[invoice_number]
        counterparty = customers_by_id[invoice["customer_id"]]
        excess = _money(Decimal(rng.randrange(50_00, 500_00, 50)) / Decimal(100))
        received = _money(_money(invoice["open_amount"]) + excess)
        transaction = _make_bank_transaction(
            number=transaction_number,
            customer_id=invoice["customer_id"],
            counterparty=counterparty,
            reference=f"ADVANCE PLUS {invoice['invoice_number']}",
            amount=received,
            currency=invoice["currency"],
            transaction_date=transaction_day(),
        )
        transactions.append(transaction)
        expected_matches.append(
            _truth_match(
                transaction,
                scenarios=(ScenarioName.OVERPAYMENT,),
                allocations=(_allocation(invoice),),
                expected_action="NEEDS_REVIEW",
                adjustments=(
                    {
                        "type": "UNALLOCATED_OVERPAYMENT",
                        "amount": _money_text(excess),
                        "currency": invoice["currency"],
                        "effect_on_transaction": "RESIDUAL",
                    },
                ),
            )
        )
        expected_exceptions.append(
            _truth_exception(
                record_id=transaction["transaction_id"],
                record_type="BANK_TRANSACTION",
                reason_code="OVERPAYMENT",
                scenarios=(ScenarioName.OVERPAYMENT,),
                evidence=(
                    f"Cash received exceeds the referenced invoice by {_money_text(excess)} INR.",
                ),
                next_action="Allocate the invoice and place the residual on customer account.",
            )
        )

    # 66-70: exact-looking references with a hard currency contradiction.
    for transaction_number, invoice_number in zip(range(66, 71), range(76, 81), strict=True):
        invoice = invoices_by_number[invoice_number]
        counterparty = customers_by_id[invoice["customer_id"]]
        transaction = _make_bank_transaction(
            number=transaction_number,
            customer_id=invoice["customer_id"],
            counterparty=counterparty,
            reference=f"{invoice['invoice_number']} FULL SETTLEMENT",
            amount=_money(invoice["open_amount"]),
            currency="USD",
            transaction_date=transaction_day(),
        )
        transactions.append(transaction)
        expected_exceptions.append(
            _truth_exception(
                record_id=transaction["transaction_id"],
                record_type="BANK_TRANSACTION",
                reason_code="CURRENCY_MISMATCH",
                scenarios=(ScenarioName.CURRENCY_MISMATCH,),
                evidence=(
                    f"Bank currency USD conflicts with invoice currency {invoice['currency']}.",
                    f"Reference points to {invoice['invoice_number']} but currency is a hard constraint.",
                ),
                next_action="Obtain FX or corrected-bank evidence; do not auto-reconcile.",
                related_record_ids=(invoice["invoice_id"],),
            )
        )

    # 71-75: two equally plausible invoices per payment; no fact selects one.
    for transaction_number in range(71, 76):
        pair_offset = transaction_number - 71
        first_invoice = invoices_by_number[81 + pair_offset * 2]
        second_invoice = invoices_by_number[82 + pair_offset * 2]
        counterparty = customers_by_id[first_invoice["customer_id"]]
        transaction = _make_bank_transaction(
            number=transaction_number,
            customer_id=first_invoice["customer_id"],
            counterparty=counterparty,
            reference=f"WIRE {counterparty} ACCOUNT SETTLEMENT",
            amount=_money(first_invoice["open_amount"]),
            currency=first_invoice["currency"],
            transaction_date=transaction_day(),
        )
        transactions.append(transaction)
        expected_exceptions.append(
            _truth_exception(
                record_id=transaction["transaction_id"],
                record_type="BANK_TRANSACTION",
                reason_code="AMBIGUOUS_MATCH",
                scenarios=(ScenarioName.AMBIGUOUS_MATCH,),
                evidence=(
                    "Two open invoices have the same customer, amount, currency, and dates.",
                    "The bank reference contains neither invoice number.",
                ),
                next_action="Request remittance advice identifying the intended invoice.",
                related_record_ids=(first_invoice["invoice_id"], second_invoice["invoice_id"]),
            )
        )

    # 76-80: no invoice, known alias, or remittance can explain the payment.
    for transaction_number in range(76, 81):
        unknown_number = transaction_number - 75
        transaction = _make_bank_transaction(
            number=transaction_number,
            customer_id="",
            counterparty=f"UNKNOWN COUNTERPARTY {unknown_number}",
            reference=f"INCOMING TRANSFER NO DETAILS {9000 + transaction_number}",
            amount=_random_amount(rng),
            currency="INR",
            transaction_date=transaction_day(),
        )
        transactions.append(transaction)
        expected_exceptions.append(
            _truth_exception(
                record_id=transaction["transaction_id"],
                record_type="BANK_TRANSACTION",
                reason_code="UNRECONCILABLE",
                scenarios=(ScenarioName.UNRECONCILABLE,),
                evidence=(
                    "No open invoice is compatible on identity and reference.",
                    "No remittance advice is available.",
                ),
                next_action="Ask treasury to identify the sender and request remittance advice.",
            )
        )

    # Five duplicate invoice rows are separate unsafe input-record exceptions.
    for offset in range(5):
        source = invoices_by_number[91 + offset]
        duplicate = invoices_by_number[96 + offset]
        expected_exceptions.append(
            _truth_exception(
                record_id=duplicate["invoice_id"],
                record_type="INVOICE",
                reason_code="DUPLICATE_INVOICE",
                scenarios=(ScenarioName.DUPLICATE_INVOICE,),
                evidence=(
                    "Customer, external invoice number, amount, currency, and issue date are identical.",
                ),
                next_action="Confirm the source-system duplicate and reject one record.",
                related_record_ids=(source["invoice_id"],),
            )
        )

    return transactions, remittances, expected_matches, expected_exceptions


def _build_ledger_entries(
    transactions: Sequence[dict[str, str]],
    rng: random.Random,
    as_of_date: date,
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for number, transaction in enumerate(transactions[:50], start=1):
        entries.append(
            {
                "entry_id": f"LEDGER-{number:04d}",
                "batch_id": BATCH_ID,
                "entry_date": transaction["transaction_date"],
                "account_code": "1100-ACCOUNTS-RECEIVABLE",
                "direction": "CREDIT",
                "amount": transaction["amount"],
                "currency": transaction["currency"],
                "reference": transaction["reference"],
                "counterparty": transaction["counterparty"],
                "transaction_id": transaction["transaction_id"],
                "status": "UNMATCHED",
            }
        )

    vendors = (
        "People Operations Payroll",
        "Metro Business Park",
        "India Tax Authority",
        "Cloud Infrastructure Vendor",
        "Corporate Card Clearing",
    )
    for offset in range(20):
        number = 51 + offset
        entries.append(
            {
                "entry_id": f"LEDGER-{number:04d}",
                "batch_id": BATCH_ID,
                "entry_date": (as_of_date - timedelta(days=rng.randint(0, 20))).isoformat(),
                "account_code": f"{5000 + (offset % 5) * 100}-OPERATING-EXPENSE",
                "direction": "DEBIT",
                "amount": _money_text(_random_amount(rng)),
                "currency": "INR",
                "reference": f"AP RUN {as_of_date:%Y%m}-{offset + 1:03d}",
                "counterparty": vendors[offset % len(vendors)],
                "transaction_id": "",
                "status": "POSTED",
            }
        )
    return entries


def _build_recurring_cash_flows(as_of_date: date) -> list[dict[str, str]]:
    definitions = (
        ("PAYROLL", "OUTFLOW", "3100000.00", "MONTHLY", 18, "People Operations Payroll"),
        ("OFFICE_RENT", "OUTFLOW", "180000.00", "MONTHLY", 3, "Metro Business Park"),
        ("GST_PAYMENT", "OUTFLOW", "220000.00", "MONTHLY", 20, "India Tax Authority"),
        ("CLOUD_HOSTING", "OUTFLOW", "90000.00", "MONTHLY", 7, "Cloud Infrastructure Vendor"),
        ("INSURANCE", "OUTFLOW", "65000.00", "MONTHLY", 10, "Business Insurance Company"),
        ("CRM_SUBSCRIPTION", "OUTFLOW", "28000.00", "MONTHLY", 12, "CRM Software Vendor"),
        ("ERP_SUBSCRIPTION", "OUTFLOW", "42000.00", "MONTHLY", 14, "ERP Software Vendor"),
        ("WAREHOUSE_RENT", "OUTFLOW", "110000.00", "MONTHLY", 5, "Logistics Park"),
        ("COURIER", "OUTFLOW", "35000.00", "WEEKLY", 4, "National Courier Company"),
        ("CONTRACTORS", "OUTFLOW", "175000.00", "MONTHLY", 15, "Contractor Clearing"),
        ("LOAN_REPAYMENT", "OUTFLOW", "250000.00", "MONTHLY", 30, "Commercial Bank"),
        ("UTILITIES", "OUTFLOW", "48000.00", "MONTHLY", 9, "Utilities Provider"),
        ("MARKETING", "OUTFLOW", "120000.00", "MONTHLY", 22, "Media Buying Partner"),
        ("SUPPORT_RETAINER", "INFLOW", "200000.00", "MONTHLY", 24, CUSTOMER_NAMES[0]),
        ("LICENCE_ROYALTY", "INFLOW", "500000.00", "MONTHLY", 28, CUSTOMER_NAMES[1]),
    )
    rows: list[dict[str, str]] = []
    for index, (flow_type, direction, amount, frequency, day_rule, counterparty) in enumerate(
        definitions,
        start=1,
    ):
        next_due = as_of_date.replace(day=min(day_rule, 28))
        if next_due <= as_of_date:
            next_due = (as_of_date.replace(day=28) + timedelta(days=4)).replace(
                day=min(day_rule, 28)
            )
        rows.append(
            {
                "cash_flow_id": f"RCF-{index:03d}",
                "organization_id": ORGANIZATION_ID,
                "flow_type": flow_type,
                "direction": direction,
                "amount": amount,
                "currency": "INR",
                "frequency": frequency,
                "day_rule": str(day_rule),
                "next_due_date": next_due.isoformat(),
                "counterparty": counterparty,
                "committed": "true" if direction == "OUTFLOW" else "false",
                "active": "true",
            }
        )
    return rows


def _build_future_actual_cash(as_of_date: date) -> list[dict[str, str]]:
    opening_cash = _money("1650000.00")
    inflows = {
        2: _money("400000.00"),
        5: _money("300000.00"),
        8: _money("500000.00"),
        12: _money("250000.00"),
        15: _money("450000.00"),
        21: _money("600000.00"),
        24: _money("200000.00"),
        28: _money("500000.00"),
    }
    outflows = {
        3: _money("180000.00"),
        7: _money("120000.00"),
        10: _money("300000.00"),
        14: _money("250000.00"),
        18: _money("3100000.00"),
        20: _money("220000.00"),
        25: _money("750000.00"),
        27: _money("90000.00"),
        30: _money("250000.00"),
    }
    rows: list[dict[str, str]] = []
    prior_close = opening_cash
    for day_number in range(1, 31):
        opening = prior_close
        inflow = inflows.get(day_number, _money(0))
        outflow = outflows.get(day_number, _money(0))
        closing = _money(opening + inflow - outflow)
        rows.append(
            {
                "date": (as_of_date + timedelta(days=day_number)).isoformat(),
                "currency": "INR",
                "opening_cash": _money_text(opening),
                "actual_inflows": _money_text(inflow),
                "actual_outflows": _money_text(outflow),
                "actual_closing_cash": _money_text(closing),
                "is_shortfall": str(closing < 0).lower(),
            }
        )
        prior_close = closing
    return rows


def _scenario_counts(
    expected_matches: Sequence[dict[str, Any]],
    expected_exceptions: Sequence[dict[str, Any]],
) -> dict[str, int]:
    record_ids_by_scenario: dict[str, set[str]] = {
        scenario: set() for scenario in EXPECTED_SCENARIO_COUNTS
    }
    for record in (*expected_matches, *expected_exceptions):
        record_id = record.get("transaction_id") or record.get("record_id")
        for scenario in record["scenarios"]:
            record_ids_by_scenario.setdefault(scenario, set()).add(str(record_id))
    return {
        scenario: len(record_ids)
        for scenario, record_ids in sorted(record_ids_by_scenario.items())
    }


def generate_dataset(
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    seed: int = DEFAULT_SEED,
    as_of_date: date = DEFAULT_AS_OF_DATE,
) -> GenerationSummary:
    """Generate agent-visible inputs plus evaluator-only ground truth.

    Re-running with the same seed and as-of date overwrites the same known files
    byte-for-byte. Unknown files in the destination are deliberately untouched.
    """

    output_path = Path(output_dir)
    input_dir = output_path / "input"
    private_dir = output_path / "private_ground_truth"
    rng = random.Random(seed)

    customers, aliases = _build_customers()
    invoices, invoices_by_number = _build_invoices(rng, as_of_date)
    transactions, remittances, expected_matches, expected_exceptions = (
        _build_transactions_and_truth(rng, as_of_date, invoices_by_number)
    )
    ledger_entries = _build_ledger_entries(transactions, rng, as_of_date)
    recurring_cash_flows = _build_recurring_cash_flows(as_of_date)
    future_actual_cash = _build_future_actual_cash(as_of_date)

    input_files: dict[str, Sequence[dict[str, Any]]] = {
        "bank_transactions.csv": transactions,
        "invoices.csv": invoices,
        "ledger_entries.csv": ledger_entries,
        "remittances.csv": remittances,
        "customers.csv": customers,
        "customer_aliases.csv": aliases,
        "recurring_cash_flows.csv": recurring_cash_flows,
    }
    for filename, rows in input_files.items():
        _write_csv(input_dir / filename, rows)

    _write_json(
        private_dir / "expected_matches.json",
        {
            "schema_version": "1.0",
            "batch_id": BATCH_ID,
            "seed": seed,
            "as_of_date": as_of_date.isoformat(),
            "records": expected_matches,
        },
    )
    _write_json(
        private_dir / "expected_exceptions.json",
        {
            "schema_version": "1.0",
            "batch_id": BATCH_ID,
            "seed": seed,
            "as_of_date": as_of_date.isoformat(),
            "records": expected_exceptions,
        },
    )
    _write_csv(private_dir / "future_actual_cash.csv", future_actual_cash)

    observed_scenario_counts = _scenario_counts(expected_matches, expected_exceptions)
    if observed_scenario_counts != dict(sorted(EXPECTED_SCENARIO_COUNTS.items())):
        raise AssertionError(
            "generated scenario coverage diverged from its contract: "
            f"expected={EXPECTED_SCENARIO_COUNTS}, observed={observed_scenario_counts}"
        )

    row_counts = {
        "bank_transactions": len(transactions),
        "invoices": len(invoices),
        "ledger_entries": len(ledger_entries),
        "remittances": len(remittances),
        "customers": len(customers),
        "customer_aliases": len(aliases),
        "recurring_cash_flows": len(recurring_cash_flows),
        "expected_matches": len(expected_matches),
        "expected_exceptions": len(expected_exceptions),
        "future_actual_cash_days": len(future_actual_cash),
    }

    generated_paths = [
        *(input_dir / filename for filename in input_files),
        private_dir / "expected_matches.json",
        private_dir / "expected_exceptions.json",
        private_dir / "future_actual_cash.csv",
    ]
    manifest = {
        "schema_version": "1.0",
        "dataset": "CashClose deterministic demo",
        "organization_id": ORGANIZATION_ID,
        "batch_id": BATCH_ID,
        "accounting_timezone": ACCOUNTING_TIMEZONE,
        "seed": seed,
        "as_of_date": as_of_date.isoformat(),
        "generated_at": _iso_timestamp(as_of_date, hour=0, minute=0),
        "row_counts": row_counts,
        "scenario_counts": observed_scenario_counts,
        "files": {
            str(path.relative_to(output_path)): {"sha256": _sha256(path)}
            for path in sorted(generated_paths)
        },
        "security": {
            "agent_visible_directory": "input",
            "evaluator_only_directory": "private_ground_truth",
            "ground_truth_must_not_be_mounted_into_agent_runtime": True,
        },
    }
    _write_json(output_path / "manifest.json", manifest)

    return GenerationSummary(
        output_dir=output_path,
        input_dir=input_dir,
        private_ground_truth_dir=private_dir,
        seed=seed,
        row_counts=row_counts,
        scenario_counts=observed_scenario_counts,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"destination root (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--as-of-date",
        type=date.fromisoformat,
        default=DEFAULT_AS_OF_DATE,
        metavar="YYYY-MM-DD",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    summary = generate_dataset(args.output_dir, seed=args.seed, as_of_date=args.as_of_date)
    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

