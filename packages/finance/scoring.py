"""Interpretable reconciliation confidence scoring and decision policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from .candidate_generation import MatchCandidate
from .normalization import parse_decimal


class Decision(StrEnum):
    AUTO_RECONCILE = "AUTO_RECONCILE"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ABSTAIN = "ABSTAIN"
    EXCEPTION = "EXCEPTION"


class RiskFlag(StrEnum):
    MULTIPLE_PLAUSIBLE_CANDIDATES = "MULTIPLE_PLAUSIBLE_CANDIDATES"
    CURRENCY_INCONSISTENT = "CURRENCY_INCONSISTENT"
    ONLY_AMOUNT_MATCHES = "ONLY_AMOUNT_MATCHES"
    INVOICE_ALREADY_PARTIALLY_ALLOCATED = "INVOICE_ALREADY_PARTIALLY_ALLOCATED"
    OUTSIDE_EXPECTED_DATE_WINDOW = "OUTSIDE_EXPECTED_DATE_WINDOW"
    INVOICE_ALREADY_COMMITTED = "INVOICE_ALREADY_COMMITTED"
    NO_OPEN_BALANCE = "NO_OPEN_BALANCE"
    DUPLICATE_PAYMENT_ID = "DUPLICATE_PAYMENT_ID"
    CONTRADICTORY_REFERENCE = "CONTRADICTORY_REFERENCE"


HARD_RISK_FLAGS = frozenset(
    {
        RiskFlag.CURRENCY_INCONSISTENT,
        RiskFlag.INVOICE_ALREADY_COMMITTED,
        RiskFlag.NO_OPEN_BALANCE,
        RiskFlag.DUPLICATE_PAYMENT_ID,
        RiskFlag.CONTRADICTORY_REFERENCE,
    }
)


@dataclass(frozen=True, slots=True)
class ConfidenceWeights:
    reference_similarity: Decimal | str = Decimal("0.30")
    amount_compatibility: Decimal | str = Decimal("0.25")
    counterparty_similarity: Decimal | str = Decimal("0.15")
    date_compatibility: Decimal | str = Decimal("0.10")
    remittance_evidence: Decimal | str = Decimal("0.10")
    currency_compatibility: Decimal | str = Decimal("0.05")
    historical_pattern: Decimal | str = Decimal("0.05")

    def __post_init__(self) -> None:
        parsed = {
            name: parse_decimal(getattr(self, name), field_name=name)
            for name in self.__dataclass_fields__
        }
        if any(value < 0 for value in parsed.values()):
            raise ValueError("confidence weights cannot be negative")
        if sum(parsed.values(), Decimal("0")) != Decimal("1"):
            raise ValueError("confidence weights must sum to exactly 1")
        for name, value in parsed.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class ConfidencePolicy:
    auto_reconcile_threshold: Decimal | str = Decimal("0.95")
    human_review_threshold: Decimal | str = Decimal("0.75")
    ambiguity_margin: Decimal | str = Decimal("0.03")
    weights: ConfidenceWeights = field(default_factory=ConfidenceWeights)

    def __post_init__(self) -> None:
        automatic = parse_decimal(
            self.auto_reconcile_threshold, field_name="auto_reconcile_threshold"
        )
        review = parse_decimal(
            self.human_review_threshold, field_name="human_review_threshold"
        )
        margin = parse_decimal(self.ambiguity_margin, field_name="ambiguity_margin")
        if not Decimal("0") <= review <= automatic <= Decimal("1"):
            raise ValueError("thresholds must satisfy 0 <= review <= auto <= 1")
        if not Decimal("0") <= margin <= Decimal("1"):
            raise ValueError("ambiguity_margin must be between 0 and 1")
        object.__setattr__(self, "auto_reconcile_threshold", automatic)
        object.__setattr__(self, "human_review_threshold", review)
        object.__setattr__(self, "ambiguity_margin", margin)


@dataclass(frozen=True, slots=True)
class CandidateFeatures:
    reference_similarity: Decimal | str
    amount_compatibility: Decimal | str
    counterparty_similarity: Decimal | str
    date_compatibility: Decimal | str
    remittance_evidence: Decimal | str = Decimal("0")
    currency_compatibility: Decimal | str = Decimal("1")
    historical_pattern: Decimal | str = Decimal("0")

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = parse_decimal(getattr(self, name), field_name=name)
            if not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)

    @classmethod
    def from_candidate(
        cls,
        candidate: MatchCandidate,
        *,
        remittance_evidence: Decimal | int | str = Decimal("0"),
        historical_pattern: Decimal | int | str = Decimal("0"),
    ) -> "CandidateFeatures":
        return cls(
            reference_similarity=candidate.reference_similarity,
            amount_compatibility=candidate.amount_compatibility,
            counterparty_similarity=candidate.counterparty_similarity,
            date_compatibility=candidate.date_compatibility,
            remittance_evidence=remittance_evidence,
            currency_compatibility=candidate.currency_compatibility,
            historical_pattern=historical_pattern,
        )


@dataclass(frozen=True, slots=True)
class ScoreResult:
    raw_score: Decimal
    penalty_total: Decimal
    confidence: Decimal
    decision: Decision
    contributions: Mapping[str, Decimal]
    penalties: Mapping[str, Decimal]
    risk_flags: tuple[RiskFlag, ...]
    hard_risk_flags: tuple[RiskFlag, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_score": format(self.raw_score, "f"),
            "penalty_total": format(self.penalty_total, "f"),
            "confidence": format(self.confidence, "f"),
            "decision": self.decision.value,
            "contributions": {
                key: format(value, "f") for key, value in self.contributions.items()
            },
            "penalties": {
                key: format(value, "f") for key, value in self.penalties.items()
            },
            "risk_flags": [flag.value for flag in self.risk_flags],
            "hard_risk_flags": [flag.value for flag in self.hard_risk_flags],
        }


PENALTIES: Mapping[RiskFlag, Decimal] = {
    RiskFlag.MULTIPLE_PLAUSIBLE_CANDIDATES: Decimal("0.30"),
    RiskFlag.CURRENCY_INCONSISTENT: Decimal("0.25"),
    RiskFlag.ONLY_AMOUNT_MATCHES: Decimal("0.20"),
    RiskFlag.INVOICE_ALREADY_PARTIALLY_ALLOCATED: Decimal("0.15"),
    RiskFlag.OUTSIDE_EXPECTED_DATE_WINDOW: Decimal("0.10"),
    # Hard flags need no additional numerical punishment to force an exception;
    # these values still make the audit breakdown intuitive.
    RiskFlag.INVOICE_ALREADY_COMMITTED: Decimal("0.30"),
    RiskFlag.NO_OPEN_BALANCE: Decimal("0.30"),
    RiskFlag.DUPLICATE_PAYMENT_ID: Decimal("0.30"),
    RiskFlag.CONTRADICTORY_REFERENCE: Decimal("0.30"),
}


def _as_features(
    value: CandidateFeatures | MatchCandidate | Mapping[str, Any],
    *,
    remittance_evidence: Decimal | int | str,
    historical_pattern: Decimal | int | str,
) -> CandidateFeatures:
    if isinstance(value, CandidateFeatures):
        return value
    if isinstance(value, MatchCandidate):
        return CandidateFeatures.from_candidate(
            value,
            remittance_evidence=remittance_evidence,
            historical_pattern=historical_pattern,
        )
    return CandidateFeatures(
        reference_similarity=value["reference_similarity"],
        amount_compatibility=value["amount_compatibility"],
        counterparty_similarity=value["counterparty_similarity"],
        date_compatibility=value["date_compatibility"],
        remittance_evidence=value.get("remittance_evidence", remittance_evidence),
        currency_compatibility=value.get("currency_compatibility", "1"),
        historical_pattern=value.get("historical_pattern", historical_pattern),
    )


def infer_risk_flags(
    features: CandidateFeatures,
    candidate: MatchCandidate | None = None,
) -> set[RiskFlag]:
    flags: set[RiskFlag] = set()
    if features.currency_compatibility < Decimal("1"):
        flags.add(RiskFlag.CURRENCY_INCONSISTENT)
    evidence_other_than_amount = max(
        features.reference_similarity,
        features.counterparty_similarity,
        features.remittance_evidence,
        features.historical_pattern,
    )
    if features.amount_compatibility >= Decimal("0.99") and evidence_other_than_amount < Decimal("0.50"):
        flags.add(RiskFlag.ONLY_AMOUNT_MATCHES)
    if features.date_compatibility < Decimal("0.50"):
        flags.add(RiskFlag.OUTSIDE_EXPECTED_DATE_WINDOW)
    if candidate is not None:
        if candidate.invoice.is_partial:
            flags.add(RiskFlag.INVOICE_ALREADY_PARTIALLY_ALLOCATED)
        if candidate.invoice.already_committed:
            flags.add(RiskFlag.INVOICE_ALREADY_COMMITTED)
        if candidate.invoice.open_amount.amount == 0:
            flags.add(RiskFlag.NO_OPEN_BALANCE)
    return flags


def calculate_raw_score(
    features: CandidateFeatures,
    weights: ConfidenceWeights | None = None,
) -> tuple[Decimal, dict[str, Decimal]]:
    active_weights = weights or ConfidenceWeights()
    contributions = {
        name: getattr(features, name) * getattr(active_weights, name)
        for name in features.__dataclass_fields__
    }
    raw_score = sum(contributions.values(), Decimal("0")).quantize(Decimal("0.001"))
    return raw_score, {
        name: value.quantize(Decimal("0.001")) for name, value in contributions.items()
    }


def classify_confidence(
    confidence: Decimal | int | str,
    *,
    risk_flags: Iterable[RiskFlag | str] = (),
    policy: ConfidencePolicy | None = None,
) -> Decision:
    active_policy = policy or ConfidencePolicy()
    value = parse_decimal(confidence, field_name="confidence")
    flags = {RiskFlag(flag) for flag in risk_flags}
    if flags.intersection(HARD_RISK_FLAGS):
        return Decision.EXCEPTION
    if value >= active_policy.auto_reconcile_threshold:
        return Decision.AUTO_RECONCILE
    if value >= active_policy.human_review_threshold:
        return Decision.HUMAN_REVIEW
    return Decision.ABSTAIN


def score_candidate(
    candidate_or_features: CandidateFeatures | MatchCandidate | Mapping[str, Any],
    *,
    remittance_evidence: Decimal | int | str = Decimal("0"),
    historical_pattern: Decimal | int | str = Decimal("0"),
    risk_flags: Iterable[RiskFlag | str] = (),
    ambiguous: bool = False,
    policy: ConfidencePolicy | None = None,
) -> ScoreResult:
    """Score one match candidate and apply deterministic policy penalties."""

    active_policy = policy or ConfidencePolicy()
    features = _as_features(
        candidate_or_features,
        remittance_evidence=remittance_evidence,
        historical_pattern=historical_pattern,
    )
    candidate = candidate_or_features if isinstance(candidate_or_features, MatchCandidate) else None
    flags = infer_risk_flags(features, candidate)
    flags.update(RiskFlag(flag) for flag in risk_flags)
    if ambiguous:
        flags.add(RiskFlag.MULTIPLE_PLAUSIBLE_CANDIDATES)

    raw_score, contributions = calculate_raw_score(features, active_policy.weights)
    ordered_flags = tuple(sorted(flags, key=lambda flag: flag.value))
    penalties = {flag.value: PENALTIES[flag] for flag in ordered_flags}
    penalty_total = sum(penalties.values(), Decimal("0")).quantize(Decimal("0.001"))
    confidence = max(Decimal("0"), raw_score - penalty_total).quantize(Decimal("0.001"))
    hard_flags = tuple(flag for flag in ordered_flags if flag in HARD_RISK_FLAGS)
    decision = classify_confidence(confidence, risk_flags=ordered_flags, policy=active_policy)
    return ScoreResult(
        raw_score=raw_score,
        penalty_total=penalty_total,
        confidence=confidence,
        decision=decision,
        contributions=contributions,
        penalties=penalties,
        risk_flags=ordered_flags,
        hard_risk_flags=hard_flags,
    )


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: MatchCandidate
    score: ScoreResult


def score_candidates(
    candidates: Sequence[MatchCandidate],
    *,
    remittance_evidence: Mapping[str, Decimal | int | str] | None = None,
    historical_patterns: Mapping[str, Decimal | int | str] | None = None,
    policy: ConfidencePolicy | None = None,
) -> list[RankedCandidate]:
    """Score candidates and penalize candidates tied within the policy margin."""

    active_policy = policy or ConfidencePolicy()
    remittance = remittance_evidence or {}
    history = historical_patterns or {}
    initial: list[tuple[MatchCandidate, ScoreResult]] = []
    for candidate in candidates:
        initial.append(
            (
                candidate,
                score_candidate(
                    candidate,
                    remittance_evidence=remittance.get(candidate.invoice_id, "0"),
                    historical_pattern=history.get(candidate.invoice_id, "0"),
                    policy=active_policy,
                ),
            )
        )
    initial.sort(key=lambda item: (-item[1].raw_score, item[0].invoice_id))
    if len(initial) >= 2:
        top_score = initial[0][1].raw_score
        near_top_ids = {
            candidate.invoice_id
            for candidate, result in initial
            if top_score - result.raw_score <= active_policy.ambiguity_margin
        }
        # Ambiguity is a transaction-level condition: a slightly lower-ranked
        # third candidate must not become auto-eligible merely because the two
        # strongest candidates were penalized.
        ambiguous_ids = (
            {candidate.invoice_id for candidate, _ in initial}
            if len(near_top_ids) > 1
            else set()
        )
    else:
        ambiguous_ids = set()

    ranked = [
        RankedCandidate(
            candidate,
            score_candidate(
                candidate,
                remittance_evidence=remittance.get(candidate.invoice_id, "0"),
                historical_pattern=history.get(candidate.invoice_id, "0"),
                ambiguous=candidate.invoice_id in ambiguous_ids,
                policy=active_policy,
            ),
        )
        for candidate, _ in initial
    ]
    ranked.sort(key=lambda item: (-item.score.confidence, item.candidate.invoice_id))
    return ranked
