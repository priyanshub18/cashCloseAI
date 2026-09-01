from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

from packages.synthetic_data.generator import DEFAULT_SEED, generate_dataset
from packages.synthetic_data.scenarios import EXPECTED_SCENARIO_COUNTS


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json_records(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def test_generator_is_byte_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_dataset(first, seed=DEFAULT_SEED)
    generate_dataset(second, seed=DEFAULT_SEED)

    first_files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    second_files = sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())

    assert first_files == second_files
    for relative_path in first_files:
        assert (first / relative_path).read_bytes() == (second / relative_path).read_bytes()


def test_row_and_scenario_contract(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")

    assert summary.row_counts == {
        "bank_transactions": 80,
        "invoices": 100,
        "ledger_entries": 70,
        "remittances": 40,
        "customers": 20,
        "customer_aliases": 10,
        "recurring_cash_flows": 15,
        "expected_matches": 65,
        "expected_exceptions": 45,
        "future_actual_cash_days": 30,
    }
    assert summary.scenario_counts == EXPECTED_SCENARIO_COUNTS


def test_agent_visible_inputs_do_not_leak_truth_labels(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")

    forbidden_columns = {"scenario", "scenarios", "expected_action", "reason_code", "unsafe"}
    for csv_path in summary.input_dir.glob("*.csv"):
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            fieldnames = set(csv.DictReader(handle).fieldnames or ())
        assert fieldnames.isdisjoint(forbidden_columns), csv_path.name


def test_money_is_decimal_text_with_currency(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")
    input_dir = summary.input_dir

    for filename, amount_columns in {
        "bank_transactions.csv": ("amount",),
        "invoices.csv": ("amount", "original_amount", "remaining_balance", "open_amount"),
        "ledger_entries.csv": ("amount",),
        "recurring_cash_flows.csv": ("amount",),
    }.items():
        for row in _read_csv(input_dir / filename):
            assert len(row["currency"]) == 3
            for column in amount_columns:
                amount = Decimal(row[column])
                assert amount == amount.quantize(Decimal("0.01"))


def test_combined_fee_and_overpayment_arithmetic(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")
    truth = _read_json_records(summary.private_ground_truth_dir / "expected_matches.json")

    combined = [record for record in truth if "combined_payment" in record["scenarios"]]
    fees = [record for record in truth if "fee_deduction" in record["scenarios"]]
    overpayments = [record for record in truth if "overpayment" in record["scenarios"]]

    assert len(combined) == 10
    assert all(
        sum(Decimal(allocation["amount"]) for allocation in record["allocations"])
        == Decimal(record["transaction_amount"])
        for record in combined
    )
    assert all(
        sum(Decimal(allocation["amount"]) for allocation in record["allocations"])
        - Decimal(record["adjustments"][0]["amount"])
        == Decimal(record["transaction_amount"])
        for record in fees
    )
    assert all(
        sum(Decimal(allocation["amount"]) for allocation in record["allocations"])
        + Decimal(record["adjustments"][0]["amount"])
        == Decimal(record["transaction_amount"])
        for record in overpayments
    )


def test_unsafe_cases_and_duplicate_invoice_evidence(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")
    exceptions = _read_json_records(
        summary.private_ground_truth_dir / "expected_exceptions.json"
    )
    invoices = {
        row["invoice_id"]: row for row in _read_csv(summary.input_dir / "invoices.csv")
    }

    unsafe = [record for record in exceptions if record["unsafe"]]
    warning = [record for record in exceptions if not record["unsafe"]]
    assert len(unsafe) == 40
    assert len(warning) == 5

    duplicate_cases = [
        record for record in exceptions if record["reason_code"] == "DUPLICATE_INVOICE"
    ]
    assert len(duplicate_cases) == 5
    for case in duplicate_cases:
        duplicate = invoices[str(case["record_id"])]
        original = invoices[str(case["related_record_ids"][0])]
        comparable_fields = (
            "customer_id",
            "invoice_number",
            "issue_date",
            "amount",
            "currency",
        )
        assert all(duplicate[field] == original[field] for field in comparable_fields)


def test_forecast_truth_contains_a_30_day_shortfall(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")
    positions = _read_csv(summary.private_ground_truth_dir / "future_actual_cash.csv")

    assert len(positions) == 30
    assert any(row["is_shortfall"] == "true" for row in positions)
    for previous, current in zip(positions, positions[1:], strict=False):
        assert Decimal(previous["actual_closing_cash"]) == Decimal(current["opening_cash"])

