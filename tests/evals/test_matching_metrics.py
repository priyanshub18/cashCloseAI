from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from packages.evaluation.matching_metrics import calculate_match_metrics
from packages.synthetic_data.generator import generate_dataset


def _records(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))["records"]


def test_policy_aware_metrics_on_perfect_controller_output(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")
    truth_matches = _records(summary.private_ground_truth_dir / "expected_matches.json")
    truth_exceptions = _records(
        summary.private_ground_truth_dir / "expected_exceptions.json"
    )
    predictions = []
    for truth in truth_matches:
        prediction = copy.deepcopy(truth)
        prediction["decision"] = truth["expected_action"]
        predictions.append(prediction)
    predicted_exceptions = [record for record in truth_exceptions if record["unsafe"]]

    metrics = calculate_match_metrics(
        predictions,
        truth_matches,
        predicted_exceptions=predicted_exceptions,
        ground_truth_exceptions=truth_exceptions,
        eligible_record_count=80,
    )

    assert metrics.precision == Decimal("1.000000")
    assert metrics.recall == Decimal("1.000000")
    assert metrics.automation_coverage == Decimal("0.562500")
    assert metrics.false_approval_rate == Decimal("0.000000")
    assert metrics.exception_recall == Decimal("1.000000")
    assert metrics.automatic_matches == 45
    assert metrics.correct_automatic_matches == 45
    assert metrics.true_matches == 65
    assert metrics.unsafe_exceptions == 40
    assert Decimal("0") < metrics.value_weighted_coverage < Decimal("1")


def test_unsafe_automatic_approvals_count_as_false_approvals(tmp_path: Path) -> None:
    summary = generate_dataset(tmp_path / "dataset")
    truth_matches = _records(summary.private_ground_truth_dir / "expected_matches.json")
    predictions = []
    for truth in truth_matches:
        prediction = copy.deepcopy(truth)
        prediction["decision"] = "AUTO_RECONCILED"
        predictions.append(prediction)

    metrics = calculate_match_metrics(
        predictions,
        truth_matches,
        eligible_record_count=80,
    )

    assert metrics.correct_automatic_matches == 45
    assert metrics.automatic_matches == 65
    assert metrics.precision == Decimal("0.692308")
    assert metrics.false_approval_rate == Decimal("0.307692")
    assert metrics.recall == Decimal("1.000000")


def test_wrong_allocation_reduces_precision_and_recall() -> None:
    truth = [
        {
            "transaction_id": "BANK-1",
            "expected_action": "AUTO_RECONCILED",
            "transaction_amount": "100.00",
            "currency": "INR",
            "allocations": [
                {"invoice_id": "INV-1", "amount": "100.00", "currency": "INR"}
            ],
        }
    ]
    prediction = [
        {
            "transaction_id": "BANK-1",
            "decision": "AUTO_RECONCILED",
            "currency": "INR",
            "allocations": [
                {"invoice_id": "INV-2", "amount": "100.00", "currency": "INR"}
            ],
        }
    ]

    metrics = calculate_match_metrics(prediction, truth, eligible_record_count=1)

    assert metrics.precision == Decimal("0.000000")
    assert metrics.recall == Decimal("0.000000")
    assert metrics.false_approval_rate == Decimal("1.000000")


def test_duplicate_prediction_ids_are_rejected() -> None:
    prediction = [
        {"transaction_id": "BANK-1", "allocations": []},
        {"transaction_id": "BANK-1", "allocations": []},
    ]

    with pytest.raises(ValueError, match="duplicate evaluation record id"):
        calculate_match_metrics(prediction, [], eligible_record_count=1)


def test_zero_denominators_are_defined_as_zero() -> None:
    metrics = calculate_match_metrics([], [], eligible_record_count=0)

    assert metrics.precision == Decimal("0.000000")
    assert metrics.recall == Decimal("0.000000")
    assert metrics.automation_coverage == Decimal("0.000000")
    assert metrics.value_weighted_coverage == Decimal("0.000000")
    assert metrics.exception_recall == Decimal("0.000000")

