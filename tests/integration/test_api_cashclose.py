from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from packages.synthetic_data.generator import generate_dataset


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


def deterministic_match_inputs(
    service: CashCloseService, batch_id: str
) -> tuple[s.ReconciliationRecord, s.SolvePaymentAllocationResult, s.GetMatchEvidenceResult]:
    records = service.invoke(
        "get_unprocessed_records",
        s.GetUnprocessedRecordsInput(batch_id=batch_id, limit=100),
    )
    assert isinstance(records, s.GetUnprocessedRecordsResult)
    for record in records.records:
        candidates = service.invoke(
            "find_candidate_invoices",
            s.FindCandidateInvoicesInput(
                batch_id=batch_id,
                transaction_id=record.record_id,
            ),
        )
        assert isinstance(candidates, s.FindCandidateInvoicesResult)
        if not candidates.candidates:
            continue
        allocation = service.invoke(
            "solve_payment_allocation",
            s.SolvePaymentAllocationInput(
                batch_id=batch_id,
                transaction_id=record.record_id,
                candidate_invoice_ids=[item.invoice_id for item in candidates.candidates],
            ),
        )
        evidence = service.invoke(
            "get_match_evidence",
            s.GetMatchEvidenceInput(
                batch_id=batch_id,
                transaction_id=record.record_id,
                candidate_ids=[item.invoice_id for item in candidates.candidates],
            ),
        )
        assert isinstance(allocation, s.SolvePaymentAllocationResult)
        assert isinstance(evidence, s.GetMatchEvidenceResult)
        if allocation.feasible and allocation.alternatives == 1 and not evidence.risk_flags:
            return record, allocation, evidence
    raise AssertionError("the fixed demo seed must contain an exact deterministic match")


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
    assert body["controller"]["automatic_matches"] == 45
    assert body["controller"]["exceptions_created"] == 35
    assert body["orchestration_mode"] == "deterministic-demo"
    assert {item["file_type"]: item["row_count"] for item in body["batch"]["files"]} == {
        "bank_transactions": 80,
        "invoices": 100,
        "ledger_entries": 70,
        "remittances": 40,
    }

    matches = client.get(f"/api/batches/{batch_id}/matches").json()
    committed = [item for item in matches["proposals"] if item["status"] == "COMMITTED"]
    assert len(committed) == 45
    assert any(len(item["allocations"]) > 1 for item in committed)
    assert any(item["proposal"]["status"] == "NEEDS_REVIEW" for item in matches["reviews"])

    exceptions = client.get(f"/api/batches/{batch_id}/exceptions").json()
    assert any(item["reason_code"] == "AMBIGUOUS_MATCH" for item in exceptions["items"])
    assert all(item["next_action"] for item in exceptions["items"])
    assert all(item["amount"] and item["currency"] for item in exceptions["items"])
    assert all(item["candidate_invoices"] for item in exceptions["items"])

    forecast = client.get(f"/api/batches/{batch_id}/forecast").json()
    assert len(forecast["positions"]) == 30
    assert forecast["currency"] == "INR"
    assert forecast["positions"][0]["p10"] is not None
    assert forecast["shortfall_date"] is not None

    metrics = client.get(f"/api/batches/{batch_id}/metrics").json()
    assert metrics["matching"]["precision"] == "1.0000"
    assert metrics["matching"]["automation_coverage"] == "0.5625"
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

    snapshot = client.get(
        f"/api/batches/{batch_id}/events/snapshot", params={"after_sequence": 2}
    )
    assert snapshot.status_code == 200
    assert snapshot.json()["terminal"] is True
    assert all(item["sequence"] > 2 for item in snapshot.json()["items"])
    assert snapshot.json()["next_sequence"] >= snapshot.json()["items"][-1]["sequence"]


def test_transaction_trace_contains_complete_safe_tool_provenance(client: TestClient) -> None:
    batch_id = create_demo_batch(client)
    assert client.post(f"/api/batches/{batch_id}/run", json={}).status_code == 200
    decisions = client.get(f"/api/batches/{batch_id}/matches").json()["items"]
    record_id = decisions[0]["transaction_id"]

    response = client.get(
        f"/api/batches/{batch_id}/trace",
        params={"record_id": record_id, "limit": 100},
    )
    assert response.status_code == 200, response.text
    trace = response.json()
    assert trace["record_id"] == record_id
    assert trace["terminal"] is True
    assert trace["total_matching"] == len(trace["items"])
    expected_tools = {
        "normalize_counterparty",
        "normalize_reference",
        "validate_currency_and_amount",
        "find_candidate_invoices",
        "solve_payment_allocation",
        "get_match_evidence",
        "propose_match",
        "verify_match",
        "commit_match",
    }
    assert {event["tool_name"] for event in trace["items"]} == expected_tools
    for event in trace["items"]:
        assert event["event_type"] == "tool_completed"
        assert event["status"] == "succeeded"
        assert event["agent_name"] in {"reconciliation", "verification"}
        assert event["input_reference"]
        assert event["tool_result_reference"]
        assert event["latency_ms"] >= 0
        assert "chain_of_thought" not in event["message"]
        assert "raw_text" not in event["message"]

    filtered = client.get(
        f"/api/batches/{batch_id}/trace",
        params={
            "record_id": record_id,
            "agent_name": "reconciliation",
            "tool_name": "solve_payment_allocation",
            "status": "succeeded",
        },
    ).json()
    assert filtered["total_matching"] == 1
    assert filtered["items"][0]["tool_result_reference"] == f"allocation:{record_id}"

    unsafe_record_id = next(
        item["record_id"]
        for item in client.get(f"/api/batches/{batch_id}/exceptions").json()["items"]
        if item["record_id"].startswith("BANK-") and item["proposal_id"] is None
    )
    unsafe_trace = client.get(
        f"/api/batches/{batch_id}/trace",
        params={"record_id": unsafe_record_id, "limit": 100},
    ).json()
    assert "create_exception" in {event["tool_name"] for event in unsafe_trace["items"]}


def test_runtime_capabilities_gate_optional_responses_mode(client: TestClient) -> None:
    capabilities = client.get("/api/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json() == {
        "responses_mode_configured": False,
        "responses_model": "gpt-5.6-terra",
        "deterministic_fallback": "deterministic-controller",
        "default_orchestration_mode": "deterministic-demo",
        "transaction_trace_enabled": True,
    }

    batch_id = create_demo_batch(client)
    unavailable = client.post(
        f"/api/batches/{batch_id}/run",
        json={"use_model_planner": True},
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "MODEL_PLANNER_NOT_CONFIGURED"

    configured_service = CashCloseService(
        responses_adapter=OpenAIResponsesAdapter(api_key=None, client=object())
    )
    configured = TestClient(create_app(configured_service)).get("/api/capabilities").json()
    assert configured["responses_mode_configured"] is True
    assert configured["default_orchestration_mode"] == (
        "responses-guided-with-deterministic-execution"
    )


def test_validation_preflight_reports_upload_readiness_and_missing_file_types(
    client: TestClient,
) -> None:
    batch_id = create_demo_batch(client)
    initial = client.get(f"/api/batches/{batch_id}/validation")
    assert initial.status_code == 200
    assert initial.json()["can_run"] is True
    assert initial.json()["missing_file_types"] == []

    create = client.post(
        "/api/batches",
        json={"organization_id": "ORG-TEST", "demo_mode": False, "as_of_date": "2026-09-01"},
    )
    batch_id = create.json()["batch_id"]

    content = (
        b"transaction_id,amount,currency,transaction_date,reference\n"
        b"BANK-1,100.00,USD,2026-09-01,INV-1\n"
    )
    upload = client.post(
        f"/api/batches/{batch_id}/files",
        data={"file_type": "bank_transactions"},
        files={"file": ("bank.csv", content, "text/csv")},
    )
    assert upload.status_code == 201
    preflight = client.get(f"/api/batches/{batch_id}/validation").json()
    assert preflight["can_run"] is False
    assert preflight["validation"]["valid"] is False
    assert set(preflight["missing_file_types"]) == {
        "invoices",
        "ledger_entries",
        "remittances",
    }


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


def test_generated_four_file_batch_runs_through_http_and_exposes_all_records(
    client: TestClient, tmp_path
) -> None:
    dataset = generate_dataset(tmp_path / "cashclose-fixture")
    create = client.post(
        "/api/batches",
        json={"organization_id": "ORG-TEST", "demo_mode": False, "as_of_date": "2026-09-01"},
    )
    batch_id = create.json()["batch_id"]
    for file_type in (
        "bank_transactions",
        "invoices",
        "ledger_entries",
        "remittances",
    ):
        path = dataset.input_dir / f"{file_type}.csv"
        with path.open("rb") as source:
            response = client.post(
                f"/api/batches/{batch_id}/files",
                data={"file_type": file_type},
                files={"file": (path.name, source, "text/csv")},
            )
        assert response.status_code == 201, response.text
        assert response.json()["validation_issues"] == []

    validation = client.get(f"/api/batches/{batch_id}/validation")
    assert validation.status_code == 200
    assert validation.json()["can_run"] is True
    assert validation.json()["missing_file_types"] == []

    run = client.post(f"/api/batches/{batch_id}/run", json={"horizon_days": 30})
    assert run.status_code == 200, run.text
    assert run.json()["batch"]["status"] == "COMPLETED"
    records = client.get(f"/api/batches/{batch_id}/records")
    assert records.status_code == 200
    assert len(records.json()["items"]) == 80
    assert client.get(f"/api/batches/{batch_id}/matches").json()["proposals"]
    assert client.get(f"/api/batches/{batch_id}/exceptions").json()["items"]

    # Currency is inferred from the verified base forecast when the UI omits it.
    scenario = client.post(
        f"/api/batches/{batch_id}/scenarios",
        json={
            "name": "Acme pays seven days late",
            "action_type": "customer_payment_delay",
            "customer_name": "Acme",
            "delay_days": 7,
        },
    )
    assert scenario.status_code == 200, scenario.text
    assert scenario.json()["currency"] == "INR"


def test_identical_uploaded_source_ids_are_isolated_across_concurrent_batch_runs(
    client: TestClient, tmp_path
) -> None:
    dataset = generate_dataset(tmp_path / "collision-fixture")
    batch_ids: list[str] = []
    for _ in range(2):
        create = client.post(
            "/api/batches",
            json={
                "organization_id": "ORG-TEST",
                "demo_mode": False,
                "as_of_date": "2026-09-01",
            },
        )
        assert create.status_code == 201
        batch_id = create.json()["batch_id"]
        batch_ids.append(batch_id)
        for file_type in (
            "bank_transactions",
            "invoices",
            "ledger_entries",
            "remittances",
        ):
            path = dataset.input_dir / f"{file_type}.csv"
            response = client.post(
                f"/api/batches/{batch_id}/files",
                data={"file_type": file_type},
                files={"file": (path.name, path.read_bytes(), "text/csv")},
            )
            assert response.status_code == 201, response.text

    def run_batch(batch_id: str):
        return client.post(f"/api/batches/{batch_id}/run", json={"horizon_days": 30})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(run_batch, batch_ids))

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["batch"]["status"] == "COMPLETED" for response in responses)
    record_id_sets = []
    for batch_id in batch_ids:
        records = client.get(f"/api/batches/{batch_id}/records").json()["items"]
        assert len(records) == 80
        assert all(item["record"]["record_id"].startswith("BANK-") for item in records)
        record_id_sets.append({item["record"]["record_id"] for item in records})
        decisions = client.get(f"/api/batches/{batch_id}/matches").json()["items"]
        assert decisions
        assert all(item["batch_id"] == batch_id for item in decisions)
    assert record_id_sets[0] == record_id_sets[1]


def test_processing_failure_emits_safe_terminal_timeline_event(
    service: CashCloseService, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = service.create_batch(
        CreateBatchRequest(as_of_date=date(2026, 9, 1), demo_mode=True)
    )

    def reject_candidate_lookup(_payload):
        raise GuardrailError("candidate policy rejected the test record")

    monkeypatch.setattr(service, "_tool_find_candidate_invoices", reject_candidate_lookup)
    with pytest.raises(GuardrailError, match="candidate policy rejected"):
        service.run_batch(batch.batch_id, RunBatchRequest())

    assert service.get_batch(batch.batch_id).status is s.BatchStatus.PROCESSING_FAILED
    failure = service.event_page(batch.batch_id).items[-1]
    assert failure.event_type == "run_failed"
    assert failure.status is s.EventStatus.FAILED
    assert failure.message == "candidate policy rejected the test record"
    failed_tools = service.get_agent_trace(
        batch.batch_id,
        tool_name="find_candidate_invoices",
        status=s.EventStatus.FAILED,
    )
    assert failed_tools.total_matching == 1
    assert failed_tools.items[0].event_type == "tool_failed"
    assert failed_tools.items[0].tool_result_reference == "result:failed"


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


def test_exception_can_enter_human_review_queue(client: TestClient) -> None:
    batch_id = create_demo_batch(client)
    client.post(f"/api/batches/{batch_id}/run", json={})
    exception = next(
        item
        for item in client.get(f"/api/batches/{batch_id}/exceptions").json()["items"]
        if item["status"] == "OPEN"
    )
    response = client.post(f"/api/exceptions/{exception['exception_id']}/review")
    assert response.status_code == 200
    assert response.json()["status"] == "IN_REVIEW"


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
        },
    )
    assert scenario.status_code == 200, scenario.text
    scenario_body = scenario.json()
    assert scenario_body["scenario"]["delay_days"] == 7
    assert scenario_body["forecast_id"] != base["forecast_id"]
    assert scenario_body["currency"] == "INR"
    assert any(
        scenario_position["expected"] != base_position["expected"]
        for scenario_position, base_position in zip(
            scenario_body["positions"], base["positions"], strict=True
        )
    )
    assert client.get(f"/api/batches/{batch_id}/forecast").json()["forecast_id"] == base[
        "forecast_id"
    ]
    mismatched_currency = client.post(
        f"/api/batches/{batch_id}/scenarios",
        json={
            "name": "Invalid reporting currency",
            "action_type": "customer_payment_delay",
            "customer_name": "Acme",
            "delay_days": 7,
            "currency": "USD",
        },
    )
    assert mismatched_currency.status_code == 409
    assert mismatched_currency.json()["error"]["code"] == "GUARDRAIL_REJECTED"


def test_payable_delay_scenario_is_strictly_typed(client: TestClient) -> None:
    batch_id = create_demo_batch(client)
    client.post(f"/api/batches/{batch_id}/run", json={})
    response = client.post(
        f"/api/batches/{batch_id}/scenarios",
        json={
            "name": "Delay GST three days",
            "action_type": "payable_delay",
            "payable_name": "GST payment",
            "delay_days": 3,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["scenario"]["action_type"] == "payable_delay"


def test_one_time_outflow_requires_money_currency_pair(client: TestClient) -> None:
    batch_id = create_demo_batch(client)
    client.post(f"/api/batches/{batch_id}/run", json={})
    missing_currency = client.post(
        f"/api/batches/{batch_id}/scenarios",
        json={
            "name": "Unpriced emergency payment",
            "action_type": "one_time_outflow",
            "amount": "250000.00",
        },
    )
    assert missing_currency.status_code == 422

    scenario = client.post(
        f"/api/batches/{batch_id}/scenarios",
        json={
            "name": "Emergency vendor payment",
            "action_type": "one_time_outflow",
            "amount": "250000.00",
            "currency": "INR",
        },
    )
    assert scenario.status_code == 200, scenario.text
    assert scenario.json()["scenario"]["one_time_outflow"] == "250000.00"


def test_review_proposal_can_be_inspected_edited_and_idempotently_approved(
    client: TestClient,
) -> None:
    batch_id = create_demo_batch(client)
    client.post(f"/api/batches/{batch_id}/run", json={})
    matches = client.get(f"/api/batches/{batch_id}/matches").json()
    review = next(
        item for item in matches["reviews"] if item["proposal"]["status"] == "NEEDS_REVIEW"
    )
    assert review["transaction"]["counterparty"]
    assert review["transaction"]["currency"] == "INR"
    assert set(review["proposal"]["risk_flags"]).intersection(
        {"PARTIAL_PAYMENT", "SUSPECTED_FEE"}
    )
    assert set(review["allowed_actions"]) == {"edit", "approve", "reject"}
    proposal = review["proposal"]

    detail = client.get(
        f"/api/batches/{batch_id}/records/{proposal['transaction_id']}"
    )
    assert detail.status_code == 200
    assert detail.json()["proposal"]["proposal_id"] == proposal["proposal_id"]
    assert detail.json()["candidates"]
    assert detail.json()["evidence"]

    edited = client.patch(
        f"/api/matches/{proposal['proposal_id']}",
        json={
            "expected_revision": proposal["revision"],
            "allocations": proposal["allocations"],
            "permitted_deduction": proposal["permitted_deduction"],
            "edit_reason": "Reviewer confirmed the invoice allocation against the ERP record",
        },
    )
    assert edited.status_code == 200, edited.text
    edited_proposal = edited.json()["proposal"]
    assert edited_proposal["revision"] == proposal["revision"] + 1

    idempotency_key = f"human:{batch_id}:{proposal['proposal_id']}:approve"
    approved = client.post(
        f"/api/matches/{proposal['proposal_id']}/approve",
        json={
            "expected_revision": edited_proposal["revision"],
            "idempotency_key": idempotency_key,
            "approval_note": "ERP owner confirmed the outstanding receivable",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["proposal"]["status"] == "COMMITTED"
    assert approved.json()["decision"]["decision"] == "MANUALLY_RECONCILED"
    assert approved.json()["exception"]["status"] == "RESOLVED"

    replay = client.post(
        f"/api/matches/{proposal['proposal_id']}/approve",
        json={
            "expected_revision": edited_proposal["revision"],
            "idempotency_key": idempotency_key,
            "approval_note": "ERP owner confirmed the outstanding receivable",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    audit = client.get(f"/api/batches/{batch_id}/audit/download")
    assert audit.status_code == 200
    assert audit.headers["content-disposition"].startswith("attachment;")
    assert any(entry["action"] == "approve_match_review" for entry in audit.json()["entries"])


def test_review_proposal_can_be_rejected_with_revision_control(client: TestClient) -> None:
    batch_id = create_demo_batch(client)
    client.post(f"/api/batches/{batch_id}/run", json={})
    review = next(
        item
        for item in client.get(f"/api/batches/{batch_id}/matches").json()["reviews"]
        if item["proposal"]["status"] == "NEEDS_REVIEW"
    )
    proposal = review["proposal"]
    rejected = client.post(
        f"/api/matches/{proposal['proposal_id']}/reject",
        json={
            "expected_revision": proposal["revision"],
            "rejection_reason": "Customer confirmed the transfer belongs to another entity",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["proposal"]["status"] == "REJECTED"
    assert rejected.json()["decision"]["decision"] == "REJECTED"

    stale = client.post(
        f"/api/matches/{proposal['proposal_id']}/reject",
        json={
            "expected_revision": proposal["revision"],
            "rejection_reason": "Duplicate click",
        },
    )
    assert stale.status_code == 409


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
    transaction, allocation, evidence = deterministic_match_inputs(fresh, second.batch_id)
    proposal = fresh.invoke(
        "propose_match",
        s.ProposeMatchInput(
            batch_id=second.batch_id,
            transaction_id=transaction.record_id,
            transaction_amount=transaction.amount,
            currency=transaction.currency,
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
    definitions = responses_tool_definitions()
    names = {item["name"] for item in definitions}
    assert "commit_match" in names
    assert "compare_with_ground_truth" not in names
    assert "calculate_forecast_metrics" not in names
    assert "approve_match_review" not in names
    assert "reject_match_review" not in names
    assert "edit_match_review" not in names
    assert all(item["strict"] is True for item in definitions)
    scoped_tools = {
        "normalize_counterparty",
        "normalize_reference",
        "validate_currency_and_amount",
        "find_candidate_invoices",
        "find_candidate_ledger_entries",
        "parse_remittance_text",
        "solve_payment_allocation",
        "get_match_evidence",
        "list_related_exceptions",
    }
    for definition in definitions:
        if definition["name"] in scoped_tools:
            assert "batch_id" in definition["parameters"]["required"]

    adapter = OpenAIResponsesAdapter(api_key=None)
    assert adapter.is_configured is False
    request = adapter.build_request({"batch_id": "BATCH-TEST", "status": "UPLOADED"})
    assert request["model"] == "gpt-5.6-terra"
    # The bounded two-turn path uses previous_response_id, so the prior response
    # must remain available for the continuation. Planner payloads contain only
    # batch metadata and structured validation summaries.
    assert request["store"] is True
    assert "compare_with_ground_truth" not in {tool["name"] for tool in request["tools"]}


def test_proposal_cannot_replace_stored_transaction_amount_with_model_output(
    service: CashCloseService,
) -> None:
    batch = service.create_batch(CreateBatchRequest(as_of_date=date(2026, 9, 1)))
    transaction, allocation, evidence = deterministic_match_inputs(service, batch.batch_id)

    with pytest.raises(GuardrailError, match="stored transaction"):
        service.invoke(
            "propose_match",
            s.ProposeMatchInput(
                batch_id=batch.batch_id,
                transaction_id=transaction.record_id,
                transaction_amount=transaction.amount - Decimal("1.00"),
                currency=transaction.currency,
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


def test_browser_cors_preflight_allows_review_patch_and_bearer_auth(client: TestClient) -> None:
    response = client.options(
        "/api/matches/MP-TEST",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert "PATCH" in response.headers["access-control-allow-methods"]
    assert "Authorization" in response.headers["access-control-allow-headers"]


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
