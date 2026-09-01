"""Deterministic payment allocation with an optional OR-Tools CP-SAT path."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
from typing import Any, Iterable, Mapping, Sequence

from .candidate_generation import InvoiceRecord, MatchCandidate, TransactionRecord
from .normalization import (
    Money,
    NormalizationError,
    from_minor_units,
    parse_bool,
    parse_decimal,
    to_minor_units,
)

try:  # The fallback is used in lightweight local/test environments.
    from ortools.sat.python import cp_model
except ImportError:  # pragma: no cover - availability is environment-specific
    cp_model = None


class AllocationStatus(StrEnum):
    EXACT = "EXACT"
    WITH_DEDUCTION = "WITH_DEDUCTION"
    OVERPAYMENT = "OVERPAYMENT"
    PARTIAL = "PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"
    NO_SOLUTION = "NO_SOLUTION"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


@dataclass(frozen=True, slots=True)
class AllocationCandidate:
    invoice_id: str
    remaining_amount: Money
    confidence: Decimal | int | str = Decimal("0")
    already_committed: bool = False

    def __post_init__(self) -> None:
        if not self.invoice_id:
            raise NormalizationError("allocation candidate invoice_id is required")
        if not isinstance(self.remaining_amount, Money):
            raise NormalizationError("remaining_amount must be Money")
        if self.remaining_amount.amount <= 0:
            raise NormalizationError("remaining invoice balance must be positive")
        confidence = parse_decimal(self.confidence, field_name="confidence")
        if not Decimal("0") <= confidence <= Decimal("1"):
            raise ValueError("confidence must be between zero and one")
        object.__setattr__(self, "confidence", confidence)

    @classmethod
    def from_value(
        cls,
        value: "AllocationCandidate | InvoiceRecord | MatchCandidate | Mapping[str, Any]",
    ) -> "AllocationCandidate":
        if isinstance(value, cls):
            return value
        if isinstance(value, MatchCandidate):
            return cls(
                invoice_id=value.invoice_id,
                remaining_amount=value.invoice.open_amount,
                confidence=value.pre_score,
                already_committed=value.invoice.already_committed,
            )
        if isinstance(value, InvoiceRecord):
            return cls(
                invoice_id=value.invoice_id,
                remaining_amount=value.open_amount,
                already_committed=value.already_committed,
            )
        currency = str(value["currency"])
        amount = value.get(
            "open_amount", value.get("remaining_amount", value.get("amount"))
        )
        return cls(
            invoice_id=str(value.get("invoice_id") or value.get("id") or ""),
            remaining_amount=Money(amount, currency),
            confidence=value.get("confidence", value.get("score", "0")),
            already_committed=parse_bool(
                value.get("already_committed", False), field_name="already_committed"
            ),
        )


@dataclass(frozen=True, slots=True)
class Allocation:
    invoice_id: str
    amount: Money
    allocation_type: str

    def to_dict(self) -> dict[str, str]:
        return {
            "invoice_id": self.invoice_id,
            "amount": format(self.amount.amount, "f"),
            "currency": self.amount.currency,
            "allocation_type": self.allocation_type,
        }


@dataclass(frozen=True, slots=True)
class AllocationSolution:
    transaction_id: str
    transaction_amount: Money
    status: AllocationStatus
    allocations: tuple[Allocation, ...]
    total_allocated: Money
    deduction_amount: Money
    overpayment_amount: Money
    variance: Money
    solver: str
    risk_flags: tuple[str, ...] = ()
    explanation_codes: tuple[str, ...] = ()

    @property
    def is_solved(self) -> bool:
        return self.status in {
            AllocationStatus.EXACT,
            AllocationStatus.WITH_DEDUCTION,
            AllocationStatus.OVERPAYMENT,
            AllocationStatus.PARTIAL,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "transaction_amount": format(self.transaction_amount.amount, "f"),
            "currency": self.transaction_amount.currency,
            "status": self.status.value,
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "total_allocated": format(self.total_allocated.amount, "f"),
            "deduction_amount": format(self.deduction_amount.amount, "f"),
            "overpayment_amount": format(self.overpayment_amount.amount, "f"),
            "variance": format(self.variance.amount, "f"),
            "solver": self.solver,
            "risk_flags": list(self.risk_flags),
            "explanation_codes": list(self.explanation_codes),
        }


def _coerce_transaction(
    transaction: TransactionRecord | Mapping[str, Any],
) -> TransactionRecord:
    return (
        transaction
        if isinstance(transaction, TransactionRecord)
        else TransactionRecord.from_mapping(transaction)
    )


def _empty_solution(
    transaction: TransactionRecord,
    status: AllocationStatus,
    *,
    solver: str,
    risk_flags: tuple[str, ...] = (),
    explanation_codes: tuple[str, ...] = (),
) -> AllocationSolution:
    zero = Money.zero(transaction.amount.currency)
    return AllocationSolution(
        transaction_id=transaction.transaction_id,
        transaction_amount=transaction.amount,
        status=status,
        allocations=(),
        total_allocated=zero,
        deduction_amount=zero,
        overpayment_amount=zero,
        variance=Money(-transaction.amount.amount, transaction.amount.currency),
        solver=solver,
        risk_flags=risk_flags,
        explanation_codes=explanation_codes,
    )


def _subset_tie_key(indices: tuple[int, ...], candidates: Sequence[AllocationCandidate]) -> tuple[Any, ...]:
    return (
        len(indices),
        sum((Decimal("1") - candidates[index].confidence) for index in indices),
        tuple(candidates[index].invoice_id for index in indices),
    )


def _enumerate_half(
    candidates: Sequence[AllocationCandidate],
    offset: int,
) -> dict[int, tuple[int, ...]]:
    best: dict[int, tuple[int, ...]] = {0: ()}
    for size in range(1, len(candidates) + 1):
        for local_indices in combinations(range(len(candidates)), size):
            total = sum(to_minor_units(candidates[index].remaining_amount) for index in local_indices)
            global_indices = tuple(index + offset for index in local_indices)
            existing = best.get(total)
            if existing is None:
                best[total] = global_indices
    return best


def _solve_full_subset_fallback(
    payment_units: int,
    candidates: Sequence[AllocationCandidate],
    *,
    tolerance_units: int,
    max_deduction_units: int,
    max_overpayment_units: int,
) -> tuple[int, ...] | None:
    """Meet-in-the-middle exact subset solver, deterministic up to 32 inputs."""

    if len(candidates) > 32:
        raise ValueError(
            "deterministic subset fallback supports at most 32 candidates; "
            "tighten candidate generation or enable OR-Tools"
        )
    bounded = candidates
    split = len(bounded) // 2
    left = _enumerate_half(bounded[:split], 0)
    right = _enumerate_half(bounded[split:], split)
    left_sums = sorted(left)
    minimum = max(1, payment_units - max_overpayment_units - tolerance_units)
    maximum = payment_units + max_deduction_units + tolerance_units
    best_key: tuple[Any, ...] | None = None
    best_indices: tuple[int, ...] | None = None

    for right_sum, right_indices in right.items():
        lower = minimum - right_sum
        upper = maximum - right_sum
        start = bisect_left(left_sums, lower)
        target_index = bisect_left(left_sums, payment_units - right_sum)
        positions = {start, target_index - 1, target_index, target_index + 1}
        # Boundary candidates cover allowed deductions/overpayments when the
        # exact target is absent.
        end = bisect_left(left_sums, upper + 1)
        positions.update({end - 1, end})
        for position in positions:
            if position < 0 or position >= len(left_sums):
                continue
            left_sum = left_sums[position]
            total = left_sum + right_sum
            if total < minimum or total > maximum or total == 0:
                continue
            indices = tuple(sorted(left[left_sum] + right_indices))
            key = (
                abs(total - payment_units),
                *_subset_tie_key(indices, bounded),
            )
            if best_key is None or key < best_key:
                best_key = key
                best_indices = indices
    return best_indices


def _solve_full_subset_ortools(
    payment_units: int,
    candidates: Sequence[AllocationCandidate],
    *,
    tolerance_units: int,
    max_deduction_units: int,
    max_overpayment_units: int,
) -> tuple[int, ...] | None:
    if cp_model is None:
        return None
    model = cp_model.CpModel()
    selected = [model.new_bool_var(f"invoice_{index}") for index in range(len(candidates))]
    amounts = [to_minor_units(candidate.remaining_amount) for candidate in candidates]
    total = sum(variable * amount for variable, amount in zip(selected, amounts, strict=True))
    minimum = max(1, payment_units - max_overpayment_units - tolerance_units)
    maximum = payment_units + max_deduction_units + tolerance_units
    model.add(total >= minimum)
    model.add(total <= maximum)

    difference = model.new_int_var(-payment_units, maximum, "difference")
    absolute_difference = model.new_int_var(0, max(payment_units, maximum), "absolute_difference")
    model.add(difference == total - payment_units)
    model.add_abs_equality(absolute_difference, difference)
    count = sum(selected)
    rank_cost = sum(variable * index for index, variable in enumerate(selected))
    max_tie_cost = len(candidates) * (len(candidates) + 1) + len(candidates) ** 2
    model.minimize(
        absolute_difference * (max_tie_cost + 1)
        + count * (len(candidates) + 1)
        + rank_cost
    )

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = 2.0
    result = solver.solve(model)
    if result not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return None
    return tuple(index for index, variable in enumerate(selected) if solver.value(variable))


def solve_payment_allocation(
    transaction: TransactionRecord | Mapping[str, Any],
    candidates: Iterable[
        AllocationCandidate | InvoiceRecord | MatchCandidate | Mapping[str, Any]
    ],
    tolerance: Decimal | int | str = Decimal("0.00"),
    *,
    max_deduction: Decimal | int | str = Decimal("0.00"),
    max_overpayment: Decimal | int | str = Decimal("0.00"),
    allow_partial: bool = True,
    ambiguity_margin: Decimal | int | str = Decimal("0.03"),
    prefer_ortools: bool = True,
) -> AllocationSolution:
    """Solve a one-payment allocation without exceeding invoice balances.

    For fee/withholding cases, ``total allocated - deduction == bank amount``.
    For overpayments, ``total allocated + overpayment == bank amount``.  The
    caller sets both policy limits explicitly, so the solver cannot silently
    invent an adjustment.
    """

    transaction_record = _coerce_transaction(transaction)
    currency = transaction_record.amount.currency
    tolerance_money = Money(tolerance, currency)
    deduction_limit = Money(max_deduction, currency)
    overpayment_limit = Money(max_overpayment, currency)
    if any(value.amount < 0 for value in (tolerance_money, deduction_limit, overpayment_limit)):
        raise ValueError("allocation tolerances cannot be negative")
    margin = parse_decimal(ambiguity_margin, field_name="ambiguity_margin")
    if not Decimal("0") <= margin <= Decimal("1"):
        raise ValueError("ambiguity_margin must be between zero and one")

    raw_candidates = [AllocationCandidate.from_value(value) for value in candidates]
    ids = [candidate.invoice_id for candidate in raw_candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("candidate invoice ids must be unique")
    eligible = [candidate for candidate in raw_candidates if not candidate.already_committed]
    same_currency = [candidate for candidate in eligible if candidate.remaining_amount.currency == currency]
    if not same_currency and eligible:
        return _empty_solution(
            transaction_record,
            AllocationStatus.CURRENCY_MISMATCH,
            solver="validation",
            risk_flags=("CURRENCY_MISMATCH",),
        )
    if not same_currency:
        if raw_candidates and not eligible:
            return _empty_solution(
                transaction_record,
                AllocationStatus.NO_SOLUTION,
                solver="validation",
                risk_flags=("ALL_CANDIDATES_ALREADY_COMMITTED",),
                explanation_codes=("COMMITTED_INVOICES_CANNOT_BE_REUSED",),
            )
        return _empty_solution(
            transaction_record,
            AllocationStatus.NO_SOLUTION,
            solver="validation",
            explanation_codes=("NO_ELIGIBLE_CANDIDATES",),
        )

    same_currency.sort(key=lambda candidate: (-candidate.confidence, candidate.invoice_id))
    payment_units = to_minor_units(transaction_record.amount)
    solver_name = "deterministic_mitm"
    selected_indices: tuple[int, ...] | None = None
    if prefer_ortools and cp_model is not None:
        selected_indices = _solve_full_subset_ortools(
            payment_units,
            same_currency,
            tolerance_units=to_minor_units(tolerance_money),
            max_deduction_units=to_minor_units(deduction_limit),
            max_overpayment_units=to_minor_units(overpayment_limit),
        )
        solver_name = "ortools_cp_sat"
    if selected_indices is None:
        selected_indices = _solve_full_subset_fallback(
            payment_units,
            same_currency,
            tolerance_units=to_minor_units(tolerance_money),
            max_deduction_units=to_minor_units(deduction_limit),
            max_overpayment_units=to_minor_units(overpayment_limit),
        )
        if solver_name == "ortools_cp_sat":
            solver_name = "ortools_cp_sat+deterministic_mitm"

    if selected_indices:
        selected_candidates = [same_currency[index] for index in selected_indices]
        allocations = tuple(
            Allocation(candidate.invoice_id, candidate.remaining_amount, "FULL")
            for candidate in selected_candidates
        )
        allocated_units = sum(to_minor_units(allocation.amount) for allocation in allocations)
        variance_units = allocated_units - payment_units
        tolerance_units = to_minor_units(tolerance_money)
        deduction_units = variance_units if variance_units > tolerance_units else 0
        overpayment_units = -variance_units if variance_units < -tolerance_units else 0
        if deduction_units:
            status = AllocationStatus.WITH_DEDUCTION
            flags = ("DEDUCTION_REQUIRES_APPROVED_REASON",)
            codes = ("FULL_INVOICES_PLUS_PERMITTED_DEDUCTION",)
        elif overpayment_units:
            status = AllocationStatus.OVERPAYMENT
            flags = ("OVERPAYMENT",)
            codes = ("FULL_INVOICES_PLUS_PERMITTED_OVERPAYMENT",)
        else:
            status = AllocationStatus.EXACT
            flags = ()
            codes = ("FULL_BALANCE_SUBSET_EQUALS_TRANSACTION",)
        return AllocationSolution(
            transaction_id=transaction_record.transaction_id,
            transaction_amount=transaction_record.amount,
            status=status,
            allocations=allocations,
            total_allocated=from_minor_units(allocated_units, currency),
            deduction_amount=from_minor_units(max(0, deduction_units), currency),
            overpayment_amount=from_minor_units(max(0, overpayment_units), currency),
            variance=from_minor_units(variance_units, currency),
            solver=solver_name,
            risk_flags=flags,
            explanation_codes=codes,
        )

    if allow_partial:
        tolerance_units = to_minor_units(tolerance_money)
        partial_candidates = [
            candidate
            for candidate in same_currency
            if to_minor_units(candidate.remaining_amount) >= payment_units
        ]
        if partial_candidates:
            partial_candidates.sort(key=lambda candidate: (-candidate.confidence, candidate.invoice_id))
            if (
                len(partial_candidates) > 1
                and partial_candidates[0].confidence - partial_candidates[1].confidence <= margin
            ):
                return _empty_solution(
                    transaction_record,
                    AllocationStatus.AMBIGUOUS,
                    solver="deterministic_partial",
                    risk_flags=("MULTIPLE_PLAUSIBLE_PARTIAL_ALLOCATIONS",),
                )
            selected = partial_candidates[0]
            amount = transaction_record.amount
            return AllocationSolution(
                transaction_id=transaction_record.transaction_id,
                transaction_amount=transaction_record.amount,
                status=AllocationStatus.PARTIAL,
                allocations=(Allocation(selected.invoice_id, amount, "PARTIAL"),),
                total_allocated=amount,
                deduction_amount=Money.zero(currency),
                overpayment_amount=Money.zero(currency),
                variance=Money.zero(currency),
                solver="deterministic_partial",
                risk_flags=("PARTIAL_PAYMENT_REQUIRES_POLICY_CHECK",),
                explanation_codes=("PAYMENT_ALLOCATED_BELOW_REMAINING_BALANCE",),
            )

    return _empty_solution(
        transaction_record,
        AllocationStatus.NO_SOLUTION,
        solver=solver_name,
        explanation_codes=("NO_POLICY_COMPLIANT_ALLOCATION",),
    )


def verify_allocation(solution: AllocationSolution) -> tuple[bool, tuple[str, ...]]:
    """Recheck arithmetic invariants before a proposed allocation is committed."""

    issues: list[str] = []
    currencies = {allocation.amount.currency for allocation in solution.allocations}
    if currencies and currencies != {solution.transaction_amount.currency}:
        issues.append("CURRENCY_MISMATCH")
    recalculated = sum(
        (allocation.amount.amount for allocation in solution.allocations), Decimal("0")
    )
    if recalculated != solution.total_allocated.amount:
        issues.append("TOTAL_ALLOCATED_MISMATCH")
    expected_payment = (
        solution.total_allocated.amount
        - solution.deduction_amount.amount
        + solution.overpayment_amount.amount
        - solution.variance.amount
    )
    # ``variance`` records tolerated sub-minor/policy variance and is otherwise
    # identical to the deduction/overpayment difference.  For explicit
    # adjustments the direct accounting equation is more useful.
    if solution.status == AllocationStatus.WITH_DEDUCTION:
        expected_payment = solution.total_allocated.amount - solution.deduction_amount.amount
    elif solution.status == AllocationStatus.OVERPAYMENT:
        expected_payment = solution.total_allocated.amount + solution.overpayment_amount.amount
    elif solution.status in {AllocationStatus.EXACT, AllocationStatus.PARTIAL}:
        expected_payment = solution.total_allocated.amount - solution.variance.amount
    if solution.is_solved and expected_payment != solution.transaction_amount.amount:
        issues.append("ALLOCATION_DOES_NOT_BALANCE")
    if any(allocation.amount.amount <= 0 for allocation in solution.allocations):
        issues.append("NON_POSITIVE_ALLOCATION")
    return not issues, tuple(issues)


class GlobalAllocationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NO_SOLUTION = "NO_SOLUTION"


@dataclass(frozen=True, slots=True)
class GlobalAllocation:
    transaction_id: str
    invoice_id: str
    amount: Money

    def to_dict(self) -> dict[str, str]:
        return {
            "transaction_id": self.transaction_id,
            "invoice_id": self.invoice_id,
            "amount": format(self.amount.amount, "f"),
            "currency": self.amount.currency,
        }


@dataclass(frozen=True, slots=True)
class GlobalAllocationSolution:
    status: GlobalAllocationStatus
    allocations: tuple[GlobalAllocation, ...]
    unallocated_transactions: Mapping[str, Money]
    remaining_invoice_balances: Mapping[str, Money]
    solver: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "unallocated_transactions": {
                identifier: value.to_dict()
                for identifier, value in self.unallocated_transactions.items()
            },
            "remaining_invoice_balances": {
                identifier: value.to_dict()
                for identifier, value in self.remaining_invoice_balances.items()
            },
            "solver": self.solver,
        }


@dataclass(slots=True)
class _FlowEdge:
    target: int
    reverse_index: int
    capacity: int
    original_capacity: int
    transaction_id: str | None = None
    invoice_id: str | None = None


def _add_flow_edge(
    graph: list[list[_FlowEdge]],
    source: int,
    target: int,
    capacity: int,
    *,
    transaction_id: str | None = None,
    invoice_id: str | None = None,
) -> None:
    forward = _FlowEdge(
        target,
        len(graph[target]),
        capacity,
        capacity,
        transaction_id,
        invoice_id,
    )
    reverse = _FlowEdge(source, len(graph[source]), 0, 0)
    graph[source].append(forward)
    graph[target].append(reverse)


def _max_flow_allocations(
    transactions: Sequence[TransactionRecord],
    invoices: Mapping[str, AllocationCandidate],
    edges: Mapping[str, Sequence[AllocationCandidate]],
) -> dict[tuple[str, str], int]:
    """Deterministic Dinic fallback for global many-to-many allocation."""

    transaction_nodes = {
        transaction.transaction_id: index + 1
        for index, transaction in enumerate(transactions)
    }
    invoice_start = len(transaction_nodes) + 1
    invoice_nodes = {
        invoice_id: invoice_start + index
        for index, invoice_id in enumerate(sorted(invoices))
    }
    sink = invoice_start + len(invoice_nodes)
    graph: list[list[_FlowEdge]] = [[] for _ in range(sink + 1)]
    source = 0
    for transaction in transactions:
        _add_flow_edge(
            graph,
            source,
            transaction_nodes[transaction.transaction_id],
            to_minor_units(transaction.amount),
        )
        ranked = sorted(
            edges.get(transaction.transaction_id, ()),
            key=lambda candidate: (-candidate.confidence, candidate.invoice_id),
        )
        for candidate in ranked:
            if candidate.invoice_id not in invoice_nodes:
                continue
            capacity = min(
                to_minor_units(transaction.amount),
                to_minor_units(candidate.remaining_amount),
            )
            _add_flow_edge(
                graph,
                transaction_nodes[transaction.transaction_id],
                invoice_nodes[candidate.invoice_id],
                capacity,
                transaction_id=transaction.transaction_id,
                invoice_id=candidate.invoice_id,
            )
    for invoice_id in sorted(invoices):
        _add_flow_edge(
            graph,
            invoice_nodes[invoice_id],
            sink,
            to_minor_units(invoices[invoice_id].remaining_amount),
        )

    while True:
        levels = [-1] * len(graph)
        levels[source] = 0
        queue = [source]
        for node in queue:
            for edge in graph[node]:
                if edge.capacity > 0 and levels[edge.target] < 0:
                    levels[edge.target] = levels[node] + 1
                    queue.append(edge.target)
        if levels[sink] < 0:
            break
        next_edge = [0] * len(graph)

        def send(node: int, available: int) -> int:
            if node == sink:
                return available
            while next_edge[node] < len(graph[node]):
                edge_index = next_edge[node]
                edge = graph[node][edge_index]
                if edge.capacity > 0 and levels[edge.target] == levels[node] + 1:
                    pushed = send(edge.target, min(available, edge.capacity))
                    if pushed:
                        edge.capacity -= pushed
                        graph[edge.target][edge.reverse_index].capacity += pushed
                        return pushed
                next_edge[node] += 1
            return 0

        while send(source, 2**63 - 1):
            pass

    allocations: dict[tuple[str, str], int] = {}
    for transaction_id, node in transaction_nodes.items():
        for edge in graph[node]:
            if edge.transaction_id is None or edge.invoice_id is None:
                continue
            used = edge.original_capacity - edge.capacity
            if used > 0:
                allocations[(transaction_id, edge.invoice_id)] = used
    return allocations


def _ortools_global_allocations(
    transactions: Sequence[TransactionRecord],
    invoices: Mapping[str, AllocationCandidate],
    edges: Mapping[str, Sequence[AllocationCandidate]],
) -> dict[tuple[str, str], int] | None:
    if cp_model is None:
        return None
    model = cp_model.CpModel()
    variables: dict[tuple[str, str], Any] = {}
    used_edges: dict[tuple[str, str], Any] = {}
    confidences: dict[tuple[str, str], int] = {}
    transaction_by_id = {
        transaction.transaction_id: transaction for transaction in transactions
    }
    for transaction_id in sorted(transaction_by_id):
        transaction = transaction_by_id[transaction_id]
        for candidate in edges.get(transaction_id, ()):
            if candidate.invoice_id not in invoices:
                continue
            key = (transaction_id, candidate.invoice_id)
            capacity = min(
                to_minor_units(transaction.amount),
                to_minor_units(candidate.remaining_amount),
            )
            variable = model.new_int_var(0, capacity, f"allocation_{transaction_id}_{candidate.invoice_id}")
            used = model.new_bool_var(f"used_{transaction_id}_{candidate.invoice_id}")
            model.add(variable <= capacity * used)
            model.add(variable >= used)
            variables[key] = variable
            used_edges[key] = used
            confidences[key] = int(candidate.confidence * Decimal("1000"))
    if not variables:
        return {}
    for transaction_id, transaction in transaction_by_id.items():
        model.add(
            sum(
                variable
                for (edge_transaction_id, _), variable in variables.items()
                if edge_transaction_id == transaction_id
            )
            <= to_minor_units(transaction.amount)
        )
    for invoice_id, invoice in invoices.items():
        model.add(
            sum(
                variable
                for (_, edge_invoice_id), variable in variables.items()
                if edge_invoice_id == invoice_id
            )
            <= to_minor_units(invoice.remaining_amount)
        )
    total = sum(variables.values())
    model.maximize(total)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_time_in_seconds = 3.0
    status = solver.solve(model)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return None
    maximum_total = solver.value(total)
    model.add(total == maximum_total)
    model.maximize(
        sum(variables[key] * confidences[key] for key in variables)
        - sum(used_edges.values())
    )
    second_status = solver.solve(model)
    if second_status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return None
    return {
        key: solver.value(variable)
        for key, variable in variables.items()
        if solver.value(variable) > 0
    }


def solve_global_allocation(
    transactions: Iterable[TransactionRecord | Mapping[str, Any]],
    candidates_by_transaction: Mapping[
        str,
        Iterable[AllocationCandidate | InvoiceRecord | MatchCandidate | Mapping[str, Any]],
    ],
    *,
    prefer_ortools: bool = True,
) -> GlobalAllocationSolution:
    """Globally allocate many payments to many invoices under hard balances.

    Candidate edges must be generated before this call. The solver maximizes
    reconciled value while ensuring a transaction and an invoice are never
    allocated beyond their available amounts.
    """

    transaction_records = [_coerce_transaction(value) for value in transactions]
    transaction_ids = [value.transaction_id for value in transaction_records]
    if len(transaction_ids) != len(set(transaction_ids)):
        raise ValueError("transaction ids must be unique")
    transaction_by_id = {value.transaction_id: value for value in transaction_records}
    unknown_keys = set(candidates_by_transaction).difference(transaction_by_id)
    if unknown_keys:
        raise ValueError(f"candidate map contains unknown transactions: {sorted(unknown_keys)}")

    edge_map: dict[str, tuple[AllocationCandidate, ...]] = {}
    invoices: dict[str, AllocationCandidate] = {}
    for transaction_id, values in candidates_by_transaction.items():
        transaction = transaction_by_id[transaction_id]
        transaction_candidates: list[AllocationCandidate] = []
        seen_invoice_ids: set[str] = set()
        for value in values:
            candidate = AllocationCandidate.from_value(value)
            if candidate.invoice_id in seen_invoice_ids:
                raise ValueError(
                    f"duplicate candidate edge: {transaction_id}/{candidate.invoice_id}"
                )
            seen_invoice_ids.add(candidate.invoice_id)
            if candidate.already_committed:
                continue
            if candidate.remaining_amount.currency != transaction.amount.currency:
                continue
            existing = invoices.get(candidate.invoice_id)
            if existing and existing.remaining_amount != candidate.remaining_amount:
                raise ValueError(
                    f"inconsistent remaining balance for invoice {candidate.invoice_id}"
                )
            invoices[candidate.invoice_id] = candidate
            transaction_candidates.append(candidate)
        edge_map[transaction_id] = tuple(transaction_candidates)

    allocations_by_edge: dict[tuple[str, str], int] = {}
    solver_names: set[str] = set()
    currencies = sorted({transaction.amount.currency for transaction in transaction_records})
    for currency in currencies:
        currency_transactions = [
            transaction
            for transaction in transaction_records
            if transaction.amount.currency == currency
        ]
        currency_invoices = {
            invoice_id: invoice
            for invoice_id, invoice in invoices.items()
            if invoice.remaining_amount.currency == currency
        }
        currency_edges = {
            transaction.transaction_id: tuple(
                candidate
                for candidate in edge_map.get(transaction.transaction_id, ())
                if candidate.remaining_amount.currency == currency
            )
            for transaction in currency_transactions
        }
        solved = None
        if prefer_ortools and cp_model is not None:
            solved = _ortools_global_allocations(
                currency_transactions, currency_invoices, currency_edges
            )
            if solved is not None:
                solver_names.add("ortools_cp_sat")
        if solved is None:
            solved = _max_flow_allocations(
                currency_transactions, currency_invoices, currency_edges
            )
            solver_names.add("deterministic_max_flow")
        allocations_by_edge.update(solved)

    allocations = tuple(
        GlobalAllocation(
            transaction_id=transaction_id,
            invoice_id=invoice_id,
            amount=from_minor_units(
                units, transaction_by_id[transaction_id].amount.currency
            ),
        )
        for (transaction_id, invoice_id), units in sorted(allocations_by_edge.items())
        if units > 0
    )
    allocated_by_transaction: dict[str, int] = {}
    allocated_by_invoice: dict[str, int] = {}
    for (transaction_id, invoice_id), units in allocations_by_edge.items():
        allocated_by_transaction[transaction_id] = (
            allocated_by_transaction.get(transaction_id, 0) + units
        )
        allocated_by_invoice[invoice_id] = allocated_by_invoice.get(invoice_id, 0) + units
    unallocated_transactions = {
        transaction.transaction_id: from_minor_units(
            to_minor_units(transaction.amount)
            - allocated_by_transaction.get(transaction.transaction_id, 0),
            transaction.amount.currency,
        )
        for transaction in transaction_records
        if allocated_by_transaction.get(transaction.transaction_id, 0)
        < to_minor_units(transaction.amount)
    }
    remaining_invoices = {
        invoice_id: from_minor_units(
            to_minor_units(invoice.remaining_amount)
            - allocated_by_invoice.get(invoice_id, 0),
            invoice.remaining_amount.currency,
        )
        for invoice_id, invoice in invoices.items()
        if allocated_by_invoice.get(invoice_id, 0)
        < to_minor_units(invoice.remaining_amount)
    }
    total_allocated = sum(allocations_by_edge.values())
    total_payments = sum(to_minor_units(value.amount) for value in transaction_records)
    if total_allocated == 0:
        status = GlobalAllocationStatus.NO_SOLUTION
    elif total_allocated == total_payments:
        status = GlobalAllocationStatus.COMPLETE
    else:
        status = GlobalAllocationStatus.PARTIAL
    return GlobalAllocationSolution(
        status=status,
        allocations=allocations,
        unallocated_transactions=unallocated_transactions,
        remaining_invoice_balances=remaining_invoices,
        solver="+".join(sorted(solver_names)) if solver_names else "validation",
    )
