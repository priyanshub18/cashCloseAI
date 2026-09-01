from decimal import Decimal

import pytest

from packages.finance.normalization import (
    Money,
    NormalizationError,
    extract_invoice_references,
    normalize_counterparty,
    normalize_currency,
    normalize_reference,
    parse_bool,
)


def test_money_rejects_float_and_quantizes_with_bankers_rounding() -> None:
    with pytest.raises(NormalizationError, match="floats are unsafe"):
        Money(10.01, "USD")

    assert Money("10.005", "USD").amount == Decimal("10.00")
    assert Money("10.015", "USD").amount == Decimal("10.02")
    assert Money("125.7", "JPY").amount == Decimal("126")
    assert Money("1.2344", "KWD").amount == Decimal("1.234")


def test_money_requires_known_currency_and_never_mixes_currencies() -> None:
    with pytest.raises(NormalizationError, match="unknown ISO"):
        normalize_currency("ZZZ")
    with pytest.raises(NormalizationError, match="currency mismatch"):
        _ = Money("1", "USD") + Money("1", "EUR")


def test_text_normalization_is_stable_and_reference_extraction_is_bounded() -> None:
    assert normalize_counterparty("  Ácme Pvt. Ltd. ") == "ACME"
    assert normalize_reference(" inv-00/1831 ") == "INV001831"
    assert extract_invoice_references(
        "NEFT ACME PVT INV-1831 / 1834 settlement less chgs"
    ) == ("1831", "1834")


def test_csv_boolean_strings_are_parsed_explicitly() -> None:
    assert parse_bool("true") is True
    assert parse_bool("false") is False
    with pytest.raises(NormalizationError, match="must be a boolean"):
        parse_bool("sometimes")
