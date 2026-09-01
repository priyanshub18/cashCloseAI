"""Deterministic match verifier; no model arithmetic is permitted here."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from .schemas import MatchProposal, ProposalStatus, VerificationResult


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    version: str = "cashclose-auto-v1"
    auto_reconcile_threshold: Decimal = Decimal("0.9500")
    maximum_deduction: Decimal = Decimal("500.00")
    maximum_deduction_ratio: Decimal = Decimal("0.0500")
    minimum_evidence_items: int = 2


HARD_RISK_FLAGS = frozenset(
    {
        "CURRENCY_MISMATCH",
        "DUPLICATE_PAYMENT",
        "INVOICE_ALREADY_PAID",
        "ALLOCATION_EXCEEDS_BALANCE",
        "MULTIPLE_EQUAL_CANDIDATES",
        "MISSING_AMOUNT",
        "INVALID_CURRENCY",
    }
)


class MatchVerifier:
    def __init__(self, policy: VerificationPolicy | None = None) -> None:
        self.policy = policy or VerificationPolicy()

    def verify(self, proposal: MatchProposal) -> VerificationResult:
        reasons: list[str] = []
        hard_flags = sorted(set(proposal.risk_flags).intersection(HARD_RISK_FLAGS))

        if proposal.status is not ProposalStatus.PROPOSED:
            reasons.append("proposal is not in PROPOSED state")
        if proposal.confidence < self.policy.auto_reconcile_threshold:
            reasons.append("confidence is below the automatic reconciliation threshold")
        if hard_flags:
            reasons.append("one or more hard risk flags are present")
        if len(proposal.evidence) < self.policy.minimum_evidence_items:
            reasons.append("insufficient independent evidence")
        if len({allocation.invoice_id for allocation in proposal.allocations}) != len(proposal.allocations):
            reasons.append("an invoice appears more than once in the allocation")
        if proposal.permitted_deduction > self.policy.maximum_deduction:
            reasons.append("deduction exceeds the configured absolute limit")
        if proposal.transaction_amount and (
            proposal.permitted_deduction / proposal.transaction_amount
        ) > self.policy.maximum_deduction_ratio:
            reasons.append("deduction exceeds the configured transaction ratio")

        evidence_types = {item.evidence_type for item in proposal.evidence}
        has_identity_evidence = bool(
            evidence_types.intersection({"exact_reference", "exact_remittance", "customer_alias"})
        )
        has_amount_evidence = bool(
            evidence_types.intersection({"exact_amount", "allocation_equality", "solver_constraint"})
        )
        if not has_identity_evidence:
            reasons.append("no approved identity or reference evidence")
        if not has_amount_evidence:
            reasons.append("no deterministic amount or allocation evidence")

        return VerificationResult(
            proposal_id=proposal.proposal_id,
            approved=not reasons,
            policy_version=self.policy.version,
            confidence_threshold=self.policy.auto_reconcile_threshold,
            checked_at=datetime.now(timezone.utc),
            reasons=reasons,
            hard_risk_flags=hard_flags,
        )

