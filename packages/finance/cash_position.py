"""Verified cash-position calculation using committed, validated movements only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .normalization import (
    Money,
    NormalizationError,
    normalize_currency,
    normalize_date,
    normalize_direction,
    parse_bool,
)


VERIFIED_STATUSES = frozenset(
    {"VERIFIED", "COMMITTED", "RECONCILED", "AUTO_RECONCILED", "APPROVED"}
)


@dataclass(frozen=True, slots=True)
class CashMovement:
    movement_id: str
    effective_date: date
    amount: Money
    direction: str
    verified: bool = False
    committed: bool = False
    source: str = "BANK_TRANSACTION"

    def __post_init__(self) -> None:
        if not self.movement_id:
            raise NormalizationError("movement_id is required")
        if not isinstance(self.amount, Money):
            raise NormalizationError("movement amount must be Money")
        if self.amount.amount <= 0:
            raise NormalizationError("movement amount must be greater than zero")
        object.__setattr__(self, "effective_date", normalize_date(self.effective_date))
        object.__setattr__(self, "direction", normalize_direction(self.direction))

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "CashMovement":
        status = str(row.get("status", "")).strip().upper()
        verified = parse_bool(
            row.get("verified"), field_name="verified", default=status in VERIFIED_STATUSES
        )
        committed = parse_bool(
            row.get("committed"), field_name="committed", default=status in VERIFIED_STATUSES
        )
        currency = str(row["currency"])
        return cls(
            movement_id=str(
                row.get("movement_id")
                or row.get("transaction_id")
                or row.get("entry_id")
                or row.get("id")
                or ""
            ),
            effective_date=row.get("effective_date")
            or row.get("booking_date")
            or row.get("entry_date")
            or row.get("date"),
            amount=Money(row["amount"], currency),
            direction=str(row.get("direction", "INFLOW")),
            verified=verified,
            committed=committed,
            source=str(row.get("source", "BANK_TRANSACTION")),
        )


@dataclass(frozen=True, slots=True)
class CashBalance:
    currency: str
    opening_balance: Money
    verified_inflows: Money
    verified_outflows: Money
    closing_balance: Money
    movement_count: int

    @property
    def net_movement(self) -> Money:
        return self.verified_inflows - self.verified_outflows

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "opening_balance": format(self.opening_balance.amount, "f"),
            "verified_inflows": format(self.verified_inflows.amount, "f"),
            "verified_outflows": format(self.verified_outflows.amount, "f"),
            "net_movement": format(self.net_movement.amount, "f"),
            "closing_balance": format(self.closing_balance.amount, "f"),
            "movement_count": self.movement_count,
        }


@dataclass(frozen=True, slots=True)
class VerifiedCashPosition:
    as_of_date: date
    balances: tuple[CashBalance, ...]
    included_movement_ids: tuple[str, ...]
    excluded_movement_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def get_balance(self, currency: str) -> CashBalance:
        normalized = normalize_currency(currency)
        for balance in self.balances:
            if balance.currency == normalized:
                return balance
        raise KeyError(normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "balances": [balance.to_dict() for balance in self.balances],
            "included_movement_ids": list(self.included_movement_ids),
            "excluded_movement_ids": list(self.excluded_movement_ids),
            "warnings": list(self.warnings),
        }


def _coerce_opening_balances(
    values: Money | Mapping[str, Money | Decimal | int | str | Mapping[str, Any]] | Iterable[Money],
) -> dict[str, Money]:
    if isinstance(values, Money):
        return {values.currency: values}
    if isinstance(values, Mapping):
        balances: dict[str, Money] = {}
        for currency_value, amount_value in values.items():
            currency = normalize_currency(str(currency_value))
            if isinstance(amount_value, Money):
                money = amount_value
            elif isinstance(amount_value, Mapping):
                money = Money(amount_value["amount"], str(amount_value.get("currency", currency)))
            else:
                money = Money(amount_value, currency)
            if money.currency != currency:
                raise NormalizationError("opening balance key and Money currency differ")
            balances[currency] = money
        return balances
    balances = {}
    for money in values:
        if not isinstance(money, Money):
            raise NormalizationError("opening balance iterable must contain Money values")
        if money.currency in balances:
            raise NormalizationError(f"duplicate opening currency: {money.currency}")
        balances[money.currency] = money
    return balances


def calculate_verified_cash(
    opening_balances: Money
    | Mapping[str, Money | Decimal | int | str | Mapping[str, Any]]
    | Iterable[Money],
    movements: Iterable[CashMovement | Mapping[str, Any]],
    as_of_date: date | str,
    *,
    require_committed: bool = True,
    strict: bool = False,
) -> VerifiedCashPosition:
    """Calculate cash using only verified (and normally committed) movements.

    Unverified, future-dated, duplicate, and uncommitted movements are excluded
    and identified in the result.  In ``strict`` mode those conditions fail the
    calculation instead.
    """

    cutoff = normalize_date(as_of_date, field_name="as_of_date")
    opening = _coerce_opening_balances(opening_balances)
    inflows: dict[str, Decimal] = {
        currency: Decimal("0") for currency in opening
    }
    outflows: dict[str, Decimal] = {
        currency: Decimal("0") for currency in opening
    }
    counts: dict[str, int] = {currency: 0 for currency in opening}
    included: list[str] = []
    excluded: list[str] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()

    for value in movements:
        movement = value if isinstance(value, CashMovement) else CashMovement.from_mapping(value)
        reason: str | None = None
        if movement.movement_id in seen_ids:
            reason = "DUPLICATE_MOVEMENT"
        elif movement.effective_date > cutoff:
            reason = "FUTURE_DATED"
        elif not movement.verified:
            reason = "NOT_VERIFIED"
        elif require_committed and not movement.committed:
            reason = "NOT_COMMITTED"
        seen_ids.add(movement.movement_id)

        if reason:
            excluded.append(movement.movement_id)
            warnings.append(f"{movement.movement_id}:{reason}")
            if strict:
                raise ValueError(f"cannot calculate verified cash: {movement.movement_id} is {reason}")
            continue

        currency = movement.amount.currency
        if currency not in opening:
            opening[currency] = Money.zero(currency)
            inflows[currency] = Decimal("0")
            outflows[currency] = Decimal("0")
            counts[currency] = 0
        if movement.direction == "INFLOW":
            inflows[currency] += movement.amount.amount
        else:
            outflows[currency] += movement.amount.amount
        counts[currency] += 1
        included.append(movement.movement_id)

    balances = tuple(
        CashBalance(
            currency=currency,
            opening_balance=opening[currency],
            verified_inflows=Money(inflows[currency], currency),
            verified_outflows=Money(outflows[currency], currency),
            closing_balance=Money(
                opening[currency].amount + inflows[currency] - outflows[currency],
                currency,
            ),
            movement_count=counts[currency],
        )
        for currency in sorted(opening)
    )
    return VerifiedCashPosition(
        as_of_date=cutoff,
        balances=balances,
        included_movement_ids=tuple(included),
        excluded_movement_ids=tuple(excluded),
        warnings=tuple(warnings),
    )
