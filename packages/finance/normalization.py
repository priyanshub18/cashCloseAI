"""Deterministic normalization primitives for financial records.

Money enters the finance package through :class:`Money`.  The class deliberately
rejects binary floating point values, requires an explicit currency, and rounds
to the currency's minor unit using banker's rounding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import re
import unicodedata
from typing import Any, Iterable, Mapping


class NormalizationError(ValueError):
    """Raised when a financial value cannot be normalized safely."""


# ISO 4217 codes likely to occur in the demo and normal business data.  Keeping
# this explicit catches plausible-looking input mistakes such as ``USN`` or
# ``ZZZ`` rather than accepting any three letters.
KNOWN_CURRENCY_CODES = frozenset(
    {
        "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG",
        "AZN", "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND",
        "BOB", "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF",
        "CHF", "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK", "DJF",
        "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP",
        "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD",
        "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK",
        "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW",
        "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD",
        "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR",
        "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK",
        "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN",
        "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR",
        "SDG", "SEK", "SGD", "SHP", "SLE", "SOS", "SRD", "SSP", "STN",
        "SYP", "SZL", "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD",
        "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VES", "VND",
        "VUV", "WST", "XAF", "XCD", "XOF", "XPF", "YER", "ZAR", "ZMW",
    }
)

_ZERO_DECIMAL_CURRENCIES = frozenset(
    {"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"}
)
_THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
_DECIMAL_PATTERN = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_NON_ALPHANUMERIC_PATTERN = re.compile(r"[^A-Z0-9]+")


def normalize_currency(value: str, *, require_known: bool = True) -> str:
    """Return a validated uppercase ISO-style currency code."""

    if not isinstance(value, str):
        raise NormalizationError("currency must be a string")
    currency = value.strip().upper()
    if not _CURRENCY_PATTERN.fullmatch(currency):
        raise NormalizationError(f"invalid currency code: {value!r}")
    if require_known and currency not in KNOWN_CURRENCY_CODES:
        raise NormalizationError(f"unknown ISO 4217 currency code: {currency}")
    return currency


def money_quantum(currency: str) -> Decimal:
    """Return the smallest supported accounting unit for ``currency``."""

    normalized = normalize_currency(currency)
    if normalized in _ZERO_DECIMAL_CURRENCIES:
        return Decimal("1")
    if normalized in _THREE_DECIMAL_CURRENCIES:
        return Decimal("0.001")
    return Decimal("0.01")


def parse_decimal(value: Decimal | int | str, *, field_name: str = "value") -> Decimal:
    """Parse a base-10 value while rejecting binary floating point input."""

    if isinstance(value, bool) or isinstance(value, float):
        raise NormalizationError(
            f"{field_name} must be Decimal, int, or a decimal string; floats are unsafe for money"
        )
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or not _DECIMAL_PATTERN.fullmatch(raw):
            raise NormalizationError(f"invalid decimal {field_name}: {value!r}")
        try:
            parsed = Decimal(raw.replace(",", ""))
        except InvalidOperation as exc:  # pragma: no cover - regex rejects these
            raise NormalizationError(f"invalid decimal {field_name}: {value!r}") from exc
    else:
        raise NormalizationError(
            f"{field_name} must be Decimal, int, or a decimal string"
        )
    if not parsed.is_finite():
        raise NormalizationError(f"{field_name} must be finite")
    return parsed


def parse_bool(value: Any, *, field_name: str = "value", default: bool = False) -> bool:
    """Parse common CSV/API boolean values without Python's string truthiness."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n", ""}:
            return False
    raise NormalizationError(f"{field_name} must be a boolean")


def quantize_money(
    value: Decimal | int | str,
    currency: str,
    *,
    rounding: str = ROUND_HALF_EVEN,
) -> Decimal:
    """Quantize ``value`` to the currency's minor unit."""

    return parse_decimal(value, field_name="amount").quantize(
        money_quantum(currency), rounding=rounding
    )


@dataclass(frozen=True, slots=True)
class Money:
    """An immutable, currency-aware amount."""

    amount: Decimal | int | str
    currency: str

    def __post_init__(self) -> None:
        currency = normalize_currency(self.currency)
        amount = quantize_money(self.amount, currency)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "amount", amount)

    @classmethod
    def zero(cls, currency: str) -> "Money":
        return cls(Decimal("0"), currency)

    def _require_same_currency(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise NormalizationError(
                f"currency mismatch: {self.currency} and {other.currency}"
            )

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def scale(self, factor: Decimal | int | str) -> "Money":
        return Money(self.amount * parse_decimal(factor, field_name="factor"), self.currency)

    def to_dict(self) -> dict[str, str]:
        return {"amount": format(self.amount, "f"), "currency": self.currency}


def to_minor_units(value: Money) -> int:
    """Convert a money amount to exact integer minor units."""

    quantum = money_quantum(value.currency)
    return int(value.amount / quantum)


def from_minor_units(units: int, currency: str) -> Money:
    if isinstance(units, bool) or not isinstance(units, int):
        raise NormalizationError("minor units must be an integer")
    return Money(Decimal(units) * money_quantum(currency), currency)


_LEGAL_SUFFIXES = frozenset(
    {
        "CO", "COMPANY", "CORP", "CORPORATION", "INC", "INCORPORATED",
        "LLC", "LLP", "LIMITED", "LTD", "PLC", "PRIVATE", "PTE", "PVT",
    }
)


def _ascii_upper(value: str) -> str:
    if not isinstance(value, str):
        raise NormalizationError("text value must be a string")
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).upper()


def normalize_counterparty(value: str, *, strip_legal_suffixes: bool = True) -> str:
    """Normalize a customer/vendor name without making an identity decision."""

    text = _ascii_upper(value).replace("&", " AND ")
    tokens = [token for token in _NON_ALPHANUMERIC_PATTERN.sub(" ", text).split() if token]
    if strip_legal_suffixes:
        while tokens and tokens[-1] in _LEGAL_SUFFIXES:
            tokens.pop()
    normalized = " ".join(tokens)
    if not normalized:
        raise NormalizationError("counterparty cannot be empty after normalization")
    return normalized


def normalize_reference(value: str) -> str:
    """Return a compact uppercase reference suitable for exact comparison."""

    compact = _NON_ALPHANUMERIC_PATTERN.sub("", _ascii_upper(value))
    if not compact:
        raise NormalizationError("reference cannot be empty after normalization")
    return compact


def reference_tokens(value: str) -> tuple[str, ...]:
    """Return stable tokens, including compact letter/number references."""

    text = _NON_ALPHANUMERIC_PATTERN.sub(" ", _ascii_upper(value))
    raw_tokens = [token for token in text.split() if token]
    tokens: list[str] = []
    for token in raw_tokens:
        tokens.append(token)
        pieces = re.findall(r"[A-Z]+|\d+", token)
        if len(pieces) > 1:
            tokens.extend(pieces)
    # Preserve input order while removing duplicates.
    return tuple(dict.fromkeys(tokens))


def extract_invoice_references(value: str) -> tuple[str, ...]:
    """Extract reference-like tokens from bank or remittance text.

    Numeric tokens shorter than three digits are intentionally ignored because
    they are usually dates, branch numbers, or noise.
    """

    candidates: list[str] = []
    tokens = reference_tokens(value)
    for index, token in enumerate(tokens):
        if token.isdigit() and len(token) >= 3:
            numeric = int(token)
            # A year shared by every invoice in a series (for example
            # INV-2026-0001) is not identifying evidence by itself.
            if not (1900 <= numeric <= 2100):
                candidates.append(token.lstrip("0") or "0")
        elif re.fullmatch(r"(?:INV|INVOICE|BILL|REF)\d{2,}", token):
            digits = re.search(r"\d+", token)
            if digits:
                candidates.append(digits.group().lstrip("0") or "0")
        elif token in {"INV", "INVOICE", "BILL", "REF"} and index + 1 < len(tokens):
            next_token = tokens[index + 1]
            if next_token.isdigit() and len(next_token) >= 2:
                next_number = int(next_token)
                if 1900 <= next_number <= 2100:
                    if index + 2 < len(tokens):
                        sequence_token = tokens[index + 2]
                        if sequence_token.isdigit() and len(sequence_token) >= 2:
                            candidates.append(sequence_token.lstrip("0") or "0")
                else:
                    candidates.append(next_token.lstrip("0") or "0")
    return tuple(dict.fromkeys(candidates))


def normalize_date(value: date | datetime | str, *, field_name: str = "date") -> date:
    """Normalize an ISO date/datetime to a timezone-independent calendar date."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise NormalizationError(f"{field_name} must be a date or ISO date string")
    raw = value.strip()
    try:
        # Accept a trailing Z while keeping the output as a calendar date.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.date()
    except ValueError:
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise NormalizationError(f"invalid {field_name}: {value!r}") from exc


def normalize_direction(value: str) -> str:
    """Normalize common debit/credit labels to INFLOW or OUTFLOW."""

    if not isinstance(value, str):
        raise NormalizationError("direction must be a string")
    normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
    inflow = {"INFLOW", "IN", "CREDIT", "CR", "RECEIPT", "RECEIVABLE"}
    outflow = {"OUTFLOW", "OUT", "DEBIT", "DR", "PAYMENT", "PAYABLE"}
    if normalized in inflow:
        return "INFLOW"
    if normalized in outflow:
        return "OUTFLOW"
    raise NormalizationError(f"invalid cash-flow direction: {value!r}")


def ensure_single_currency(values: Iterable[Money]) -> str:
    currencies = {value.currency for value in values}
    if not currencies:
        raise NormalizationError("at least one money value is required")
    if len(currencies) != 1:
        raise NormalizationError(f"currency mismatch: {sorted(currencies)}")
    return next(iter(currencies))


def validate_currency_and_amount(
    amount: Decimal | int | str,
    currency: str,
    *,
    allow_negative: bool = False,
    allow_zero: bool = True,
) -> Money:
    """Validate and normalize the amount/currency pair used by a tool call."""

    money = Money(amount, currency)
    if not allow_negative and money.amount < 0:
        raise NormalizationError("amount cannot be negative")
    if not allow_zero and money.amount == 0:
        raise NormalizationError("amount must be greater than zero")
    return money


def coerce_money(value: Money | Mapping[str, Any] | Decimal | int | str, currency: str | None = None) -> Money:
    """Coerce common API shapes to :class:`Money` without accepting floats."""

    if isinstance(value, Money):
        if currency is not None and value.currency != normalize_currency(currency):
            raise NormalizationError("currency does not match Money value")
        return value
    if isinstance(value, Mapping):
        return Money(value["amount"], value["currency"])
    if currency is None:
        raise NormalizationError("currency is required with a scalar amount")
    return Money(value, currency)
