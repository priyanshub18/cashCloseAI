"""Deterministic reconciliation and exception metrics.

All financial values and rates use Decimal. A correct automatic match must have
the exact expected allocation and adjustment set *and* be eligible for automatic
reconciliation under the private truth policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Sequence


RATE_QUANTUM = Decimal("0.000001")
MONEY_QUANTUM = Decimal("0.01")
AUTOMATIC_DECISIONS = frozenset({"AUTO_RECONCILED", "AUTO_APPROVED", "COMMITTED"})
RESOLVED_DECISIONS = frozenset(
    {*AUTOMATIC_DECISIONS, "HUMAN_APPROVED", "MANUALLY_RECONCILED", "RESOLVED"}
)
DISCOVERED_DECISIONS = frozenset(
    {*RESOLVED_DECISIONS, "PROPOSED", "NEEDS_REVIEW", "MATCH_FOUND"}
)

Record = Mapping[str, Any]
RecordSource = Sequence[Record] | Mapping[str, Any] | str | Path


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except Exception as exc:  # Decimal raises several subclasses for bad input.
        raise ValueError(f"{field} must be a valid decimal amount: {value!r}") from exc


def _rate(numerator: int | Decimal, denominator: int | Decimal) -> Decimal:
    numerator_decimal = Decimal(numerator)
    denominator_decimal = Decimal(denominator)
    if denominator_decimal == 0:
        return Decimal("0").quantize(RATE_QUANTUM)
    return (numerator_decimal / denominator_decimal).quantize(
        RATE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def load_records(source: RecordSource) -> list[Record]:
    """Load a JSON record list or a ``{"records": [...]}`` envelope."""

    payload: Any = source
    if isinstance(source, (str, Path)):
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        payload = payload.get("records", payload.get("results"))
    if payload is None or isinstance(payload, (str, bytes)) or not isinstance(payload, Sequence):
        raise ValueError("record source must be a list or an object containing a records list")
    records = list(payload)
    if not all(isinstance(record, Mapping) for record in records):
        raise ValueError("every record must be an object")
    return records


def _record_id(record: Record, *, exception: bool = False) -> str:
    candidate_keys = (
        ("record_id", "transaction_id", "invoice_id", "id")
        if exception
        else ("transaction_id", "record_id", "id")
    )
    for key in candidate_keys:
        value = record.get(key)
        if value:
            return str(value)
    raise ValueError(f"record has no stable identifier: {record!r}")


def _index_unique(records: Sequence[Record], *, exception: bool = False) -> dict[str, Record]:
    indexed: dict[str, Record] = {}
    for record in records:
        record_id = _record_id(record, exception=exception)
        if record_id in indexed:
            raise ValueError(f"duplicate evaluation record id: {record_id}")
        indexed[record_id] = record
    return indexed


def _allocation_signature(record: Record) -> tuple[tuple[str, str, Decimal], ...]:
    allocations = record.get("allocations", record.get("expected_allocations", ()))
    if allocations is None:
        allocations = ()
    if isinstance(allocations, (str, bytes)) or not isinstance(allocations, Sequence):
        raise ValueError("allocations must be a list")
    totals: dict[tuple[str, str], Decimal] = {}
    for allocation in allocations:
        if not isinstance(allocation, Mapping):
            raise ValueError("each allocation must be an object")
        invoice_id = allocation.get("invoice_id")
        if not invoice_id:
            raise ValueError("allocation.invoice_id is required")
        currency = str(allocation.get("currency") or record.get("currency") or "").upper()
        key = (str(invoice_id), currency)
        totals[key] = totals.get(key, Decimal("0.00")) + _decimal(
            allocation.get("amount"),
            field="allocation.amount",
        )
    return tuple(
        sorted(
            (invoice_id, currency, amount.quantize(MONEY_QUANTUM))
            for (invoice_id, currency), amount in totals.items()
        )
    )


def _adjustment_signature(record: Record) -> tuple[tuple[str, str, str, Decimal], ...]:
    adjustments = record.get("adjustments", ()) or ()
    if isinstance(adjustments, (str, bytes)) or not isinstance(adjustments, Sequence):
        raise ValueError("adjustments must be a list")
    normalized: list[tuple[str, str, str, Decimal]] = []
    for adjustment in adjustments:
        if not isinstance(adjustment, Mapping):
            raise ValueError("each adjustment must be an object")
        normalized.append(
            (
                str(adjustment.get("type", "")).upper(),
                str(adjustment.get("currency") or record.get("currency") or "").upper(),
                str(adjustment.get("effect_on_transaction", "")).upper(),
                _decimal(adjustment.get("amount"), field="adjustment.amount"),
            )
        )
    return tuple(sorted(normalized))


def _match_is_correct(prediction: Record, truth: Record) -> bool:
    return (
        _allocation_signature(prediction) == _allocation_signature(truth)
        and _adjustment_signature(prediction) == _adjustment_signature(truth)
    )


def _decision(record: Record) -> str:
    # A raw list of committed match records may omit decision; treating it as
    # automatic is explicit and keeps the evaluator convenient for tool output.
    return str(record.get("decision", "AUTO_RECONCILED")).upper()


def _transaction_value(record: Record) -> Decimal:
    explicit = record.get("transaction_amount", record.get("reconcilable_value"))
    if explicit is not None:
        return _decimal(explicit, field="transaction_amount")
    return sum(
        (amount for _, _, amount in _allocation_signature(record)),
        start=Decimal("0.00"),
    ).quantize(MONEY_QUANTUM)


def _unsafe_exception_keys(records: Sequence[Record]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for record in records:
        if not bool(record.get("unsafe", True)):
            continue
        record_type = str(record.get("record_type", "BANK_TRANSACTION")).upper()
        keys.add((record_type, _record_id(record, exception=True)))
    return keys


def _predicted_exception_keys(records: Sequence[Record]) -> set[tuple[str, str]]:
    return {
        (
            str(record.get("record_type", "BANK_TRANSACTION")).upper(),
            _record_id(record, exception=True),
        )
        for record in records
    }


@dataclass(frozen=True, slots=True)
class MatchMetrics:
    precision: Decimal
    recall: Decimal
    automation_coverage: Decimal
    value_weighted_coverage: Decimal
    false_approval_rate: Decimal
    exception_recall: Decimal
    automatic_matches: int
    correct_automatic_matches: int
    discovered_matches: int
    correct_discovered_matches: int
    true_matches: int
    eligible_records: int
    unsafe_exceptions: int
    correctly_escalated_exceptions: int
    correctly_reconciled_value: Decimal
    total_reconcilable_value: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "precision": format(self.precision, "f"),
            "recall": format(self.recall, "f"),
            "automation_coverage": format(self.automation_coverage, "f"),
            "value_weighted_coverage": format(self.value_weighted_coverage, "f"),
            "false_approval_rate": format(self.false_approval_rate, "f"),
            "exception_recall": format(self.exception_recall, "f"),
            "automatic_matches": self.automatic_matches,
            "correct_automatic_matches": self.correct_automatic_matches,
            "discovered_matches": self.discovered_matches,
            "correct_discovered_matches": self.correct_discovered_matches,
            "true_matches": self.true_matches,
            "eligible_records": self.eligible_records,
            "unsafe_exceptions": self.unsafe_exceptions,
            "correctly_escalated_exceptions": self.correctly_escalated_exceptions,
            "correctly_reconciled_value": format(self.correctly_reconciled_value, "f"),
            "total_reconcilable_value": format(self.total_reconcilable_value, "f"),
        }


def calculate_match_metrics(
    predicted_matches: RecordSource,
    ground_truth_matches: RecordSource,
    *,
    predicted_exceptions: RecordSource = (),
    ground_truth_exceptions: RecordSource = (),
    eligible_record_count: int | None = None,
) -> MatchMetrics:
    """Calculate exact, policy-aware matching metrics.

    ``ground_truth_matches`` may include matches that require human review. Such a
    match contributes to recall when discovered but only contributes to automatic
    precision when its ``expected_action`` is ``AUTO_RECONCILED``.
    """

    prediction_records = load_records(predicted_matches)
    truth_records = [
        record
        for record in load_records(ground_truth_matches)
        if bool(record.get("is_reconcilable", True))
    ]
    predicted_exception_records = load_records(predicted_exceptions)
    truth_exception_records = load_records(ground_truth_exceptions)

    predictions_by_id = _index_unique(prediction_records)
    truth_by_id = _index_unique(truth_records)

    automatic_ids = {
        record_id
        for record_id, record in predictions_by_id.items()
        if _decision(record) in AUTOMATIC_DECISIONS
    }
    discovered_ids = {
        record_id
        for record_id, record in predictions_by_id.items()
        if _decision(record) in DISCOVERED_DECISIONS
    }
    resolved_ids = {
        record_id
        for record_id, record in predictions_by_id.items()
        if _decision(record) in RESOLVED_DECISIONS
    }

    correctly_allocated_ids = {
        record_id
        for record_id in predictions_by_id.keys() & truth_by_id.keys()
        if _match_is_correct(predictions_by_id[record_id], truth_by_id[record_id])
    }
    correct_automatic_ids = {
        record_id
        for record_id in automatic_ids & correctly_allocated_ids
        if str(truth_by_id[record_id].get("expected_action", "AUTO_RECONCILED")).upper()
        == "AUTO_RECONCILED"
    }
    correct_discovered_ids = discovered_ids & correctly_allocated_ids
    correct_resolved_ids = resolved_ids & correctly_allocated_ids

    unsafe_truth_keys = _unsafe_exception_keys(truth_exception_records)
    predicted_exception_keys = _predicted_exception_keys(predicted_exception_records)
    correct_exception_keys = unsafe_truth_keys & predicted_exception_keys

    if eligible_record_count is None:
        eligible_transaction_ids = set(truth_by_id)
        eligible_transaction_ids.update(
            record_id
            for record_type, record_id in unsafe_truth_keys
            if record_type == "BANK_TRANSACTION"
        )
        eligible_record_count = len(eligible_transaction_ids)
    if eligible_record_count < 0:
        raise ValueError("eligible_record_count cannot be negative")

    total_reconcilable_value = sum(
        (_transaction_value(record) for record in truth_by_id.values()),
        start=Decimal("0.00"),
    ).quantize(MONEY_QUANTUM)
    correctly_reconciled_value = sum(
        (_transaction_value(truth_by_id[record_id]) for record_id in correct_resolved_ids),
        start=Decimal("0.00"),
    ).quantize(MONEY_QUANTUM)

    automatic_count = len(automatic_ids)
    correct_automatic_count = len(correct_automatic_ids)
    incorrect_automatic_count = automatic_count - correct_automatic_count

    return MatchMetrics(
        precision=_rate(correct_automatic_count, automatic_count),
        recall=_rate(len(correct_discovered_ids), len(truth_by_id)),
        automation_coverage=_rate(automatic_count, eligible_record_count),
        value_weighted_coverage=_rate(
            correctly_reconciled_value,
            total_reconcilable_value,
        ),
        false_approval_rate=_rate(incorrect_automatic_count, automatic_count),
        exception_recall=_rate(len(correct_exception_keys), len(unsafe_truth_keys)),
        automatic_matches=automatic_count,
        correct_automatic_matches=correct_automatic_count,
        discovered_matches=len(discovered_ids),
        correct_discovered_matches=len(correct_discovered_ids),
        true_matches=len(truth_by_id),
        eligible_records=eligible_record_count,
        unsafe_exceptions=len(unsafe_truth_keys),
        correctly_escalated_exceptions=len(correct_exception_keys),
        correctly_reconciled_value=correctly_reconciled_value,
        total_reconcilable_value=total_reconcilable_value,
    )

