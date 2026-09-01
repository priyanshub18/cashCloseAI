from __future__ import annotations

from pathlib import Path


MIGRATION = Path("migrations/0001_cashclose_truth_layer.sql")


def test_migration_defines_all_truth_layer_tables() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    expected_public_tables = {
        "organizations",
        "batches",
        "uploaded_files",
        "bank_transactions",
        "invoices",
        "ledger_entries",
        "remittances",
        "customers",
        "customer_aliases",
        "recurring_cash_flows",
        "match_candidates",
        "match_proposals",
        "match_allocations",
        "match_evidence",
        "reconciliation_decisions",
        "exceptions",
        "forecast_runs",
        "forecast_daily_positions",
        "forecast_scenarios",
        "agent_runs",
        "agent_events",
        "tool_calls",
        "audit_events",
        "evaluation_runs",
        "evaluation_results",
    }
    for table in expected_public_tables:
        assert f"create table public.{table}" in sql

    assert "create table private_evaluation.ground_truth_matches" in sql
    assert "create table private_evaluation.ground_truth_cash_positions" in sql
    assert "revoke all on schema private_evaluation from public" in sql


def test_migration_contains_financial_safety_constraints() -> None:
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "numeric(20, 2)" in sql
    assert "reconciliation_decisions_one_commit_per_transaction" in sql
    assert "reconciliation_decisions_idempotency_unique" in sql
    assert "audit_events_are_immutable" in sql
    assert "enforce_batch_status_transition" in sql
    assert "enable row level security" in sql

