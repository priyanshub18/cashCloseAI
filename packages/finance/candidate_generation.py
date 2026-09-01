"""Bounded candidate generation for reconciliation.

This module intentionally narrows the search space without deciding whether a
match is safe.  Final confidence and policy decisions live in ``scoring.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence

from .normalization import (
    Money,
    NormalizationError,
    extract_invoice_references,
    normalize_counterparty,
    normalize_date,
    normalize_direction,
    normalize_reference,
    parse_bool,
    parse_decimal,
    validate_currency_and_amount,
)

try:  # RapidFuzz is an optimization, not a correctness dependency.
    from rapidfuzz.fuzz import ratio as _rapidfuzz_ratio
except ImportError:  # pragma: no cover - exercised where RapidFuzz is absent
    _rapidfuzz_ratio = None


ELIGIBLE_INVOICE_STATUSES = frozenset(
    {"OPEN", "UNPAID", "PARTIALLY_PAID", "PARTIAL", "OUTSTANDING"}
)


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    transaction_id: str
    amount: Money
    booking_date: date
    currency: str | None = None
    counterparty: str = ""
    reference: str = ""
    direction: str = "INFLOW"
    batch_id: str | None = None
    customer_id: str | None = None
    remittance_text: str = ""
    status: str = "UNPROCESSED"

    def __post_init__(self) -> None:
        if not self.transaction_id:
            raise NormalizationError("transaction_id is required")
        money = self.amount if isinstance(self.amount, Money) else Money(self.amount, self.currency or "")
        if self.currency is not None and money.currency != self.currency.strip().upper():
            raise NormalizationError("transaction currency differs from amount currency")
        if money.amount <= 0:
            raise NormalizationError("transaction amount must be greater than zero")
        object.__setattr__(self, "amount", money)
        object.__setattr__(self, "currency", money.currency)
        object.__setattr__(self, "booking_date", normalize_date(self.booking_date))
        object.__setattr__(self, "direction", normalize_direction(self.direction))
        object.__setattr__(self, "status", self.status.strip().upper())

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "TransactionRecord":
        currency = str(row["currency"])
        amount = validate_currency_and_amount(row["amount"], currency, allow_zero=False)
        return cls(
            transaction_id=str(row.get("transaction_id") or row.get("id") or ""),
            batch_id=_optional_string(row.get("batch_id")),
            customer_id=_optional_string(row.get("customer_id")),
            booking_date=row.get("booking_date") or row.get("value_date") or row.get("date"),
            amount=amount,
            currency=currency,
            direction=str(row.get("direction", "INFLOW")),
            counterparty=str(row.get("counterparty") or ""),
            reference=str(row.get("reference") or ""),
            remittance_text=str(row.get("remittance_text") or row.get("raw_text") or ""),
            status=str(row.get("status", "UNPROCESSED")),
        )


@dataclass(frozen=True, slots=True)
class InvoiceRecord:
    invoice_id: str
    invoice_number: str
    issue_date: date
    original_amount: Money
    open_amount: Money
    due_date: date | None = None
    customer_id: str | None = None
    customer_name: str = ""
    batch_id: str | None = None
    status: str = "OPEN"
    already_committed: bool = False

    def __post_init__(self) -> None:
        if not self.invoice_id or not self.invoice_number:
            raise NormalizationError("invoice_id and invoice_number are required")
        original = self.original_amount
        opened = self.open_amount
        if not isinstance(original, Money) or not isinstance(opened, Money):
            raise NormalizationError("invoice amounts must be Money values")
        if original.currency != opened.currency:
            raise NormalizationError("invoice amount currencies must match")
        if original.amount <= 0 or opened.amount < 0:
            raise NormalizationError("invoice amounts must be non-negative and original must be positive")
        if opened.amount > original.amount:
            raise NormalizationError("open amount cannot exceed original amount")
        object.__setattr__(self, "issue_date", normalize_date(self.issue_date))
        if self.due_date is not None:
            object.__setattr__(self, "due_date", normalize_date(self.due_date))
        object.__setattr__(self, "status", self.status.strip().upper())

    @property
    def currency(self) -> str:
        return self.open_amount.currency

    @property
    def is_partial(self) -> bool:
        return self.open_amount.amount < self.original_amount.amount

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "InvoiceRecord":
        currency = str(row["currency"])
        original_amount = validate_currency_and_amount(
            row.get("original_amount", row.get("amount")), currency, allow_zero=False
        )
        open_amount = validate_currency_and_amount(
            row.get("open_amount", row.get("remaining_amount", row.get("amount"))),
            currency,
        )
        due_date = row.get("due_date")
        return cls(
            invoice_id=str(row.get("invoice_id") or row.get("id") or ""),
            invoice_number=str(row.get("invoice_number") or row.get("reference") or ""),
            batch_id=_optional_string(row.get("batch_id")),
            customer_id=_optional_string(row.get("customer_id")),
            customer_name=str(row.get("customer_name") or row.get("counterparty") or ""),
            issue_date=row.get("issue_date") or row.get("invoice_date") or row.get("date"),
            due_date=due_date if due_date not in {None, ""} else None,
            original_amount=original_amount,
            open_amount=open_amount,
            status=str(row.get("status", "OPEN")),
            already_committed=parse_bool(
                row.get("already_committed", False), field_name="already_committed"
            ),
        )


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    max_invoice_age_days: int = 365
    future_invoice_grace_days: int = 0
    minimum_amount_ratio: Decimal | str = Decimal("0.05")
    minimum_counterparty_similarity: Decimal | str = Decimal("0.35")
    allow_currency_mismatch_candidates: bool = False
    max_candidates: int = 25

    def __post_init__(self) -> None:
        amount_ratio = parse_decimal(self.minimum_amount_ratio, field_name="minimum_amount_ratio")
        counterparty = parse_decimal(
            self.minimum_counterparty_similarity,
            field_name="minimum_counterparty_similarity",
        )
        if not Decimal("0") <= amount_ratio <= Decimal("1"):
            raise ValueError("minimum_amount_ratio must be between 0 and 1")
        if not Decimal("0") <= counterparty <= Decimal("1"):
            raise ValueError("minimum_counterparty_similarity must be between 0 and 1")
        if self.max_invoice_age_days < 0 or self.future_invoice_grace_days < 0:
            raise ValueError("date windows cannot be negative")
        if self.max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        object.__setattr__(self, "minimum_amount_ratio", amount_ratio)
        object.__setattr__(self, "minimum_counterparty_similarity", counterparty)


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    transaction_id: str
    invoice: InvoiceRecord
    reference_similarity: Decimal
    amount_compatibility: Decimal
    counterparty_similarity: Decimal
    date_compatibility: Decimal
    currency_compatibility: Decimal
    exact_reference: bool
    customer_identity_match: bool
    days_from_invoice: int
    reasons: tuple[str, ...]

    @property
    def invoice_id(self) -> str:
        return self.invoice.invoice_id

    @property
    def pre_score(self) -> Decimal:
        # Only for deterministic ordering; the policy confidence is calculated
        # by scoring.score_candidate.
        return (
            Decimal("0.35") * self.reference_similarity
            + Decimal("0.25") * self.amount_compatibility
            + Decimal("0.20") * self.counterparty_similarity
            + Decimal("0.15") * self.date_compatibility
            + Decimal("0.05") * self.currency_compatibility
        ).quantize(Decimal("0.0001"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "invoice_id": self.invoice_id,
            "reference_similarity": format(self.reference_similarity, "f"),
            "amount_compatibility": format(self.amount_compatibility, "f"),
            "counterparty_similarity": format(self.counterparty_similarity, "f"),
            "date_compatibility": format(self.date_compatibility, "f"),
            "currency_compatibility": format(self.currency_compatibility, "f"),
            "exact_reference": self.exact_reference,
            "customer_identity_match": self.customer_identity_match,
            "days_from_invoice": self.days_from_invoice,
            "reasons": list(self.reasons),
            "pre_score": format(self.pre_score, "f"),
        }


def _optional_string(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _coerce_transaction(value: TransactionRecord | Mapping[str, Any]) -> TransactionRecord:
    return value if isinstance(value, TransactionRecord) else TransactionRecord.from_mapping(value)


def _coerce_invoice(value: InvoiceRecord | Mapping[str, Any]) -> InvoiceRecord:
    return value if isinstance(value, InvoiceRecord) else InvoiceRecord.from_mapping(value)


def text_similarity(left: str, right: str) -> Decimal:
    """Return a deterministic value from zero to one for normalized text."""

    if not left or not right:
        return Decimal("0")
    if left == right:
        return Decimal("1")
    if _rapidfuzz_ratio is not None:
        value = Decimal(str(_rapidfuzz_ratio(left, right))) / Decimal("100")
    else:
        value = Decimal(str(SequenceMatcher(None, left, right).ratio()))
    return max(Decimal("0"), min(Decimal("1"), value)).quantize(Decimal("0.0001"))


def reference_similarity(transaction_reference: str, invoice_reference: str) -> Decimal:
    if not transaction_reference or not invoice_reference:
        return Decimal("0")
    transaction_compact = normalize_reference(transaction_reference)
    invoice_compact = normalize_reference(invoice_reference)
    if invoice_compact in transaction_compact:
        return Decimal("1")

    invoice_numbers = extract_invoice_references(invoice_reference)
    transaction_numbers = extract_invoice_references(transaction_reference)
    if invoice_numbers and set(invoice_numbers).intersection(transaction_numbers):
        return Decimal("1")
    return text_similarity(transaction_compact, invoice_compact)


def amount_compatibility(transaction: Money, invoice_open: Money) -> Decimal:
    if transaction.currency != invoice_open.currency:
        return Decimal("0")
    larger = max(transaction.amount, invoice_open.amount)
    if larger == 0:
        return Decimal("0")
    return (min(transaction.amount, invoice_open.amount) / larger).quantize(Decimal("0.0001"))


def date_compatibility(transaction_date: date, invoice: InvoiceRecord) -> Decimal:
    days = (transaction_date - invoice.issue_date).days
    if days < 0:
        return Decimal("0")
    if invoice.due_date is None:
        return max(
            Decimal("0"), Decimal("1") - Decimal(days) / Decimal("365")
        ).quantize(Decimal("0.0001"))
    days_after_due = (transaction_date - invoice.due_date).days
    if days_after_due <= 30:
        return Decimal("1")
    return max(
        Decimal("0"), Decimal("1") - Decimal(days_after_due - 30) / Decimal("335")
    ).quantize(Decimal("0.0001"))


def generate_candidates(
    transaction: TransactionRecord | Mapping[str, Any],
    invoices: Iterable[InvoiceRecord | Mapping[str, Any]],
    policy: CandidatePolicy | None = None,
    *,
    customer_aliases: Mapping[str, str] | None = None,
) -> list[MatchCandidate]:
    """Generate and rank a bounded set of plausible invoice candidates."""

    transaction_record = _coerce_transaction(transaction)
    active_policy = policy or CandidatePolicy()
    alias_map = {
        normalize_counterparty(alias): customer_id
        for alias, customer_id in (customer_aliases or {}).items()
    }
    resolved_customer_id = transaction_record.customer_id
    if transaction_record.counterparty:
        resolved_customer_id = resolved_customer_id or alias_map.get(
            normalize_counterparty(transaction_record.counterparty)
        )

    combined_reference = " ".join(
        value for value in (transaction_record.reference, transaction_record.remittance_text) if value
    )
    generated: list[MatchCandidate] = []
    for invoice_value in invoices:
        invoice = _coerce_invoice(invoice_value)
        if invoice.open_amount.amount <= 0 or invoice.already_committed:
            continue
        if invoice.status not in ELIGIBLE_INVOICE_STATUSES:
            continue
        if (
            transaction_record.batch_id
            and invoice.batch_id
            and transaction_record.batch_id != invoice.batch_id
        ):
            continue

        currency_compatible = transaction_record.amount.currency == invoice.currency
        if not currency_compatible and not active_policy.allow_currency_mismatch_candidates:
            continue

        days_from_invoice = (transaction_record.booking_date - invoice.issue_date).days
        if days_from_invoice < -active_policy.future_invoice_grace_days:
            continue
        if days_from_invoice > active_policy.max_invoice_age_days:
            continue

        ref_similarity = reference_similarity(combined_reference, invoice.invoice_number)
        amount_similarity = amount_compatibility(transaction_record.amount, invoice.open_amount)
        name_similarity = Decimal("0")
        if transaction_record.counterparty and invoice.customer_name:
            name_similarity = text_similarity(
                normalize_counterparty(transaction_record.counterparty),
                normalize_counterparty(invoice.customer_name),
            )
        identity_match = bool(
            resolved_customer_id
            and invoice.customer_id
            and resolved_customer_id == invoice.customer_id
        )

        # Retain candidates when any strong evidence family survives.  This
        # allows partial/combined payments without admitting every invoice.
        if (
            ref_similarity < Decimal("0.50")
            and amount_similarity < active_policy.minimum_amount_ratio
            and name_similarity < active_policy.minimum_counterparty_similarity
            and not identity_match
        ):
            continue

        reasons: list[str] = []
        exact_reference = ref_similarity == Decimal("1")
        if exact_reference:
            reasons.append("EXACT_REFERENCE")
        elif ref_similarity >= Decimal("0.70"):
            reasons.append("SIMILAR_REFERENCE")
        if amount_similarity == Decimal("1"):
            reasons.append("EXACT_AMOUNT")
        elif amount_similarity >= Decimal("0.50"):
            reasons.append("COMPATIBLE_AMOUNT")
        if identity_match:
            reasons.append("APPROVED_ALIAS")
        elif name_similarity >= Decimal("0.80"):
            reasons.append("SIMILAR_COUNTERPARTY")
        if currency_compatible:
            reasons.append("SAME_CURRENCY")

        generated.append(
            MatchCandidate(
                transaction_id=transaction_record.transaction_id,
                invoice=invoice,
                reference_similarity=ref_similarity,
                amount_compatibility=amount_similarity,
                counterparty_similarity=Decimal("1") if identity_match else name_similarity,
                date_compatibility=date_compatibility(transaction_record.booking_date, invoice),
                currency_compatibility=Decimal("1") if currency_compatible else Decimal("0"),
                exact_reference=exact_reference,
                customer_identity_match=identity_match,
                days_from_invoice=days_from_invoice,
                reasons=tuple(reasons),
            )
        )

    generated.sort(
        key=lambda candidate: (
            -int(candidate.exact_reference),
            -int(candidate.customer_identity_match),
            -candidate.pre_score,
            candidate.invoice.due_date or date.max,
            candidate.invoice_id,
        )
    )
    return generated[: active_policy.max_candidates]


def find_candidate_invoices(
    transaction: TransactionRecord | Mapping[str, Any],
    invoices: Sequence[InvoiceRecord | Mapping[str, Any]],
    policy: CandidatePolicy | None = None,
    *,
    customer_aliases: Mapping[str, str] | None = None,
) -> list[MatchCandidate]:
    """Tool-friendly alias for :func:`generate_candidates`."""

    return generate_candidates(
        transaction,
        invoices,
        policy,
        customer_aliases=customer_aliases,
    )
