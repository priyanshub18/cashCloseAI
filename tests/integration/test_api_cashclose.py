from __future__ import annotations

from datetime import date, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.schemas import CreateBatchRequest, RunBatchRequest
from apps.api.service import CashCloseService, GuardrailError
from packages.agents import schemas as s
from packages.agents.openai_adapter import OpenAIResponsesAdapter
from packages.agents.tools import responses_tool_definitions


@pytest.fixture
def service() -> CashCloseService:
    return CashCloseService(responses_adapter=OpenAIResponsesAdapter(api_key=None))


@pytest.fixture
def client(service: CashCloseService) -> TestClient:
    return TestClient(create_app(service))


def create_demo_batch(client: TestClient) -> str:
    response = client.post(
        "/api/batches",
        json={
            "organization_id": "ORG-TEST",
            "accounting_timezone": "Asia/Kolkata",
            "as_of_date": "2026-09-01",
            "demo_mode": True,
        },
    )
    assert response.status_code == 201
    return response.json()["batch_id"]


def test_complete_demo_lifecycle_exposes_matches_forecast_metrics_and_audit(
    client: TestClient,
) -> None:
    batch_id = create_demo_batch(client)
    run = client.post(
        f"/api/batches/{batch_id}/run",
        json={"horizon_days": 30, "use_model_planner": False},
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["batch"]["status"] == "COMPLETED"
    assert body["controller"]["automatic_matches"] == 1
    assert body["controller"]["exceptions_created"] == 1
    assert body["orchestration_mode"] == "deterministic-demo"

    matches = client.get(f"/api/batches/{batch_id}/matches").json()
    assert matches["items"][0]["transaction_id"].endswith("-0042")
    assert matches["proposals"][0]["status"] == "COMMITTED"
    assert matches["proposals"][0]["total_allocated"] == "84250.00"

    exceptions = client.get(f"/api/batches/{batch_id}/exceptions").json()
    assert exceptions["items"][0]["reason_code"] == "AMBIGUOUS_MATCH"
    assert exceptions["items"][0]["next_action"]

    forecast = client.get(f"/api/batches/{batch_id}/forecast").json()
    assert len(forecast["positions"]) == 30
    assert forecast["positions"][0]["p10"] is not None
    assert forecast["shortfall_date"] is not None

    metrics = client.get(f"/api/batches/{batch_id}/metrics").json()
    assert metrics["matching"]["precision"] == "0.9890"
    assert Decimal(metrics["forecast_cash_minimum"]) < 0

    evaluation = client.get(f"/api/batches/{batch_id}/evaluation").json()
    assert evaluation["ground_truth_visible_to_agent"] is False
    assert evaluation["forecast_metrics"]["evaluated_days"] == 30

    audit = client.get(f"/api/batches/{batch_id}/audit").json()
    assert audit["policy_version"] == "cashclose-auto-v1"
    assert any(entry["action"] == "commit_match" for entry in audit["entries"])


def test_sse_event_feed_contains_only_actions_evidence_and_outcomes(client: TestClient) -> None:
    batch_id = create_demo_batch(client)
    assert client.post(f"/api/batches/{batch_id}/run", json={}).status_code == 200

    response = client.get(f"/api/batches/{batch_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: agent_event" in response.text
    assert "batch_inspected" in response.text
    assert "verification_completed" in response.text
    assert "event: stream_end" in response.text
    assert "chain_of_thought" not in response.text


def test_csv_upload_reports_schema_problems_before_run(client: TestClient) -> None:
    create = client.post(
        "/api/batches",
        json={"organization_id": "ORG-TEST", "demo_mode": False, "as_of_date": "2026-09-01"},
    )
    batch_id = create.json()["batch_id"]
    upload = client.post(
        f"/api/batches/{batch_id}/files",
        data={"file_type": "bank_transactions"},
        files={"file": ("bank.csv", b"transaction_id,amount\nBANK-1,100.00\n", "text/csv")},
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()
    assert body["row_count"] == 1
    assert body["validation_issues"][0]["code"] == "MISSING_REQUIRED_COLUMNS"

    run = client.post(f"/api/batches/{batch_id}/run", json={})
    assert run.status_code == 200
    assert run.json()["batch"]["status"] == "VALIDATION_FAILED"


def test_exception_can_be_resolved_with_an_explicit_auditable_resolution(
    client: TestClient,
) -> None:
    batch_id = create_demo_batch(client)
    client.post(f"/api/batches/{batch_id}/run", json={})
    exception_id = client.get(f"/api/batches/{batch_id}/exceptions").json()["items"][0][
        "exception_id"
    ]

    response = client.post(
        f"/api/exceptions/{exception_id}/resolve",
        json={"resolution": "Controller attached remittance received from the customer"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "RESOLVED"
    assert response.json()["resolved_at"].endswith("Z")


def test_scenario_delays_acme_receipt_without_mutating_base_forecast(client: TestClient) -> None:
    batch_id = create_demo_batch(client)
    client.post(f"/api/batches/{batch_id}/run", json={})
    base = client.get(f"/api/batches/{batch_id}/forecast").json()
    scenario = client.post(
        f"/api/batches/{batch_id}/scenarios",
        json={
            "name": "Acme pays seven days late",
            "action_type": "customer_payment_delay",
            "customer_name": "Acme",
            "delay_days": 7,
            "currency": "USD",
        },
    )
    assert scenario.status_code == 200, scenario.text
    scenario_body = scenario.json()
    assert scenario_body["scenario"]["delay_days"] == 7
    assert scenario_body["forecast_id"] != base["forecast_id"]
    assert Decimal(scenario_body["positions"][4]["expected"]) < Decimal(
        base["positions"][4]["expected"]
    )


def test_commit_match_is_idempotent_and_rejects_unverified_writes(
    service: CashCloseService,
) -> None:
    batch = service.create_batch(
        CreateBatchRequest(
            organization_id="ORG-TEST",
            accounting_timezone="UTC",
            as_of_date=date(2026, 9, 1),
            demo_mode=True,
        )
    )
    run = service.run_batch(batch.batch_id, request=RunBatchRequest())
    assert run.controller.status is s.BatchStatus.COMPLETED
    committed = service.list_matches(batch.batch_id).items[0]
    assert committed.proposal_id is not None
    replay = service.invoke(
        "commit_match",
        s.CommitMatchInput(
            proposal_id=committed.proposal_id,
            idempotency_key=f"{batch.batch_id}:{committed.proposal_id}:policy-v1",
        ),
    )
    assert isinstance(replay, s.CommitMatchResult)
    assert replay.idempotent_replay is True

    fresh = CashCloseService()
    second = fresh.create_batch(
        CreateBatchRequest(as_of_date=date(2026, 9, 1), demo_mode=True)
    )
    records = fresh.invoke(
        "get_unprocessed_records",
        s.GetUnprocessedRecordsInput(batch_id=second.batch_id, limit=10),
    )
    assert isinstance(records, s.GetUnprocessedRecordsResult)
    transaction_id = next(
        record.record_id for record in records.records if record.record_id.endswith("-0042")
    )
    candidates = fresh.invoke(
        "find_candidate_invoices",
        s.FindCandidateInvoicesInput(transaction_id=transaction_id),
    )
    assert isinstance(candidates, s.FindCandidateInvoicesResult)
    allocation = fresh.invoke(
        "solve_payment_allocation",
        s.SolvePaymentAllocationInput(
            transaction_id=transaction_id,
            candidate_invoice_ids=[candidate.invoice_id for candidate in candidates.candidates],
        ),
    )
    assert isinstance(allocation, s.SolvePaymentAllocationResult)
    evidence = fresh.invoke(
        "get_match_evidence",
        s.GetMatchEvidenceInput(
            transaction_id=transaction_id,
            candidate_ids=[candidate.invoice_id for candidate in candidates.candidates],
        ),
    )
    assert isinstance(evidence, s.GetMatchEvidenceResult)
    proposal = fresh.invoke(
        "propose_match",
        s.ProposeMatchInput(
            batch_id=second.batch_id,
            transaction_id=transaction_id,
            transaction_amount=Decimal("84250.00"),
            currency="USD",
            allocations=allocation.allocations,
            permitted_deduction=allocation.permitted_deduction,
            confidence=evidence.confidence,
            evidence=evidence.evidence,
            risk_flags=evidence.risk_flags,
        ),
    )
    assert isinstance(proposal, s.ProposeMatchResult)
    with pytest.raises(GuardrailError, match="previously verified"):
        fresh.invoke(
            "commit_match",
            s.CommitMatchInput(
                proposal_id=proposal.proposal.proposal_id,
                idempotency_key="unverified:test:0001",
            ),
        )
def test_tool_catalog_hides_evaluator_ground_truth_and_adapter_is_optional() -> None:
    names = {item["name"] for item in responses_tool_definitions()}
    assert "commit_match" in names
    assert "compare_with_ground_truth" not in names
    assert "calculate_forecast_metrics" not in names
    assert all(item["strict"] is True for item in responses_tool_definitions())

    adapter = OpenAIResponsesAdapter(api_key=None)
    assert adapter.is_configured is False
    request = adapter.build_request({"batch_id": "BATCH-TEST", "status": "UPLOADED"})
    assert request["model"] == "gpt-5.6-terra"
    assert request["store"] is False
    assert "compare_with_ground_truth" not in {tool["name"] for tool in request["tools"]}


def test_proposal_cannot_replace_stored_transaction_amount_with_model_output(
    service: CashCloseService,
) -> None:
    batch = service.create_batch(CreateBatchRequest(as_of_date=date(2026, 9, 1)))
    records = service.invoke(
        "get_unprocessed_records",
        s.GetUnprocessedRecordsInput(batch_id=batch.batch_id, limit=10),
    )
    assert isinstance(records, s.GetUnprocessedRecordsResult)
    transaction = next(item for item in records.records if item.record_id.endswith("-0042"))
    candidates = service.invoke(
        "find_candidate_invoices",
        s.FindCandidateInvoicesInput(transaction_id=transaction.record_id),
    )
    assert isinstance(candidates, s.FindCandidateInvoicesResult)
    ids = [candidate.invoice_id for candidate in candidates.candidates]
    allocation = service.invoke(
        "solve_payment_allocation",
        s.SolvePaymentAllocationInput(
            transaction_id=transaction.record_id, candidate_invoice_ids=ids
        ),
    )
    evidence = service.invoke(
        "get_match_evidence",
        s.GetMatchEvidenceInput(transaction_id=transaction.record_id, candidate_ids=ids),
    )
    assert isinstance(allocation, s.SolvePaymentAllocationResult)
    assert isinstance(evidence, s.GetMatchEvidenceResult)

    with pytest.raises(GuardrailError, match="stored transaction"):
        service.invoke(
            "propose_match",
            s.ProposeMatchInput(
                batch_id=batch.batch_id,
                transaction_id=transaction.record_id,
                transaction_amount=Decimal("84249.00"),
                currency="USD",
                allocations=allocation.allocations,
                permitted_deduction=allocation.permitted_deduction,
                confidence=evidence.confidence,
                evidence=evidence.evidence,
                risk_flags=evidence.risk_flags,
            ),
        )


def test_http_models_reject_unknown_fields(client: TestClient) -> None:
    response = client.post(
        "/api/batches",
        json={"organization_id": "ORG-TEST", "demo_mode": True, "invented": "field"},
    )
    assert response.status_code == 422


def test_two_demo_batches_keep_financial_records_and_writes_isolated(
    service: CashCloseService,
) -> None:
    first = service.create_batch(CreateBatchRequest(as_of_date=date(2026, 9, 1)))
    second = service.create_batch(CreateBatchRequest(as_of_date=date(2026, 9, 1)))
    service.run_batch(first.batch_id, RunBatchRequest())
    service.run_batch(second.batch_id, RunBatchRequest())

    first_match = service.list_matches(first.batch_id).items[0]
    second_match = service.list_matches(second.batch_id).items[0]
    assert first_match.transaction_id != second_match.transaction_id
    assert first_match.proposal_id != second_match.proposal_id
    assert first_match.batch_id == first.batch_id
    assert second_match.batch_id == second.batch_id


def test_timestamps_are_timezone_aware(service: CashCloseService) -> None:
    batch = service.create_batch(CreateBatchRequest(as_of_date=date(2026, 9, 1)))
    assert batch.created_at.tzinfo is timezone.utc
