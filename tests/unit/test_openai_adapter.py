from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from apps.api.schemas import CreateBatchRequest, RunBatchRequest
from apps.api.service import CashCloseService
from packages.agents import schemas as s
from packages.agents.openai_adapter import (
    OBSERVATION_TOOL_NAMES,
    STRATEGY_TOOL_NAME,
    OpenAIPlannerResponseError,
    OpenAIResponsesAdapter,
    ResponsesAdapterConfig,
)


MODEL = "gpt-5.6-terra-test"
BATCH_ID = "BATCH-TEST-001"


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("unexpected Responses API turn")
        return self._responses.pop(0)


class FakeOpenAIClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = FakeResponses(responses)


def function_call(call_id: str, name: str, arguments: dict[str, Any]) -> Any:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=json.dumps(arguments),
    )


def model_response(
    response_id: str,
    calls: list[Any],
    *,
    output_text: str | None = None,
) -> Any:
    return SimpleNamespace(
        id=response_id,
        model=MODEL,
        status="completed",
        output=calls,
        output_text=output_text,
    )


def two_turn_client(batch_id: str = BATCH_ID) -> FakeOpenAIClient:
    return FakeOpenAIClient(
        [
            model_response(
                "resp_observe_001",
                [
                    function_call(
                        "call_inspect_001", "inspect_batch", {"batch_id": batch_id}
                    ),
                    function_call(
                        "call_validate_001", "validate_batch", {"batch_id": batch_id}
                    ),
                ],
                output_text="Internal prose must not become run provenance.",
            ),
            model_response(
                "resp_strategy_001",
                [
                    function_call(
                        "call_strategy_001",
                        STRATEGY_TOOL_NAME,
                        {
                            "batch_id": batch_id,
                            "record_order": "source_order",
                            "candidate_search": "balanced",
                            "forecast_method": "deterministic",
                            "monte_carlo_simulations": 100,
                        },
                    )
                ],
            ),
        ]
    )


def test_two_turn_planner_executes_only_read_only_tools_and_records_provenance() -> None:
    client = two_turn_client()
    adapter = OpenAIResponsesAdapter(client=client)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        executed.append((tool_name, arguments))
        if tool_name == "inspect_batch":
            return {
                "batch_id": BATCH_ID,
                "total_records": 220,
                "counts_by_type": {"bank_transaction": 80},
                "uploaded_file_count": 4,
            }
        return {"batch_id": BATCH_ID, "valid": True, "issues": []}

    guided = adapter.orchestrate_run(
        {"batch_id": BATCH_ID, "status": "UPLOADED"},
        execute_tool=execute,
    )

    assert [name for name, _ in executed] == ["inspect_batch", "validate_batch"]
    assert guided.strategy.forecast_method == "deterministic"
    assert guided.provenance.provider == "openai"
    assert guided.provenance.requested_model == "gpt-5.6-terra"
    assert guided.provenance.model == MODEL
    assert guided.provenance.response_ids == ["resp_observe_001", "resp_strategy_001"]
    assert [choice.tool_name for choice in guided.provenance.tool_choices] == [
        "inspect_batch",
        "validate_batch",
        STRATEGY_TOOL_NAME,
    ]
    assert "Internal prose" not in guided.provenance.model_dump_json()

    assert len(client.responses.requests) == 2
    observation_request, strategy_request = client.responses.requests
    assert {definition["name"] for definition in observation_request["tools"]} == set(
        OBSERVATION_TOOL_NAMES
    )
    assert observation_request["tool_choice"] == "required"
    assert observation_request["parallel_tool_calls"] is False
    assert observation_request["store"] is True
    assert strategy_request["previous_response_id"] == "resp_observe_001"
    assert strategy_request["store"] is True
    assert strategy_request["tools"] == [adapter.strategy_tool_definition()]
    assert strategy_request["tool_choice"] == {
        "type": "function",
        "name": STRATEGY_TOOL_NAME,
    }
    assert [item["call_id"] for item in strategy_request["input"]] == [
        "call_inspect_001",
        "call_validate_001",
    ]
    assert all(item["type"] == "function_call_output" for item in strategy_request["input"])


def test_planner_rejects_batch_scope_change_before_executing_tool() -> None:
    client = FakeOpenAIClient(
        [
            model_response(
                "resp_wrong_batch",
                [
                    function_call(
                        "call_wrong_batch",
                        "inspect_batch",
                        {"batch_id": "BATCH-NOT-AUTHORIZED"},
                    )
                ],
            )
        ]
    )
    adapter = OpenAIResponsesAdapter(client=client)
    executed: list[str] = []

    with pytest.raises(OpenAIPlannerResponseError, match="authorized batch_id"):
        adapter.orchestrate_run(
            {"batch_id": BATCH_ID},
            execute_tool=lambda name, arguments: executed.append(name) or {},
        )

    assert executed == []
    assert len(client.responses.requests) == 1


def test_planner_enforces_observation_call_bound_before_execution() -> None:
    calls = [
        function_call(f"call_{index}", "inspect_batch", {"batch_id": BATCH_ID})
        for index in range(4)
    ]
    client = FakeOpenAIClient([model_response("resp_too_many", calls)])
    adapter = OpenAIResponsesAdapter(client=client)
    executed: list[str] = []

    with pytest.raises(OpenAIPlannerResponseError, match="between one and three"):
        adapter.orchestrate_run(
            {"batch_id": BATCH_ID},
            execute_tool=lambda name, arguments: executed.append(name) or {},
        )

    assert executed == []
    assert len(client.responses.requests) == 1


def test_controller_model_can_be_overridden_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_CONTROLLER", "gpt-controller-env")

    adapter = OpenAIResponsesAdapter(api_key="test-key")
    explicit = OpenAIResponsesAdapter(
        api_key="test-key",
        config=ResponsesAdapterConfig(model="gpt-controller-explicit"),
    )

    assert adapter.config.model == "gpt-controller-env"
    assert explicit.config.model == "gpt-controller-explicit"


def test_service_model_path_completes_with_truthful_provenance_and_deterministic_writes() -> None:
    client = two_turn_client(batch_id="placeholder")
    adapter = OpenAIResponsesAdapter(client=client)
    service = CashCloseService(responses_adapter=adapter)
    batch = service.create_batch(
        CreateBatchRequest(as_of_date=date(2026, 9, 1), demo_mode=True)
    )
    actual_batch_id = batch.batch_id
    for response in client.responses._responses:
        for call in response.output:
            payload = json.loads(call.arguments)
            payload["batch_id"] = actual_batch_id
            call.arguments = json.dumps(payload)

    run = service.run_batch(
        actual_batch_id,
        RunBatchRequest(horizon_days=30, use_model_planner=True),
    )

    assert run.batch.status is s.BatchStatus.COMPLETED
    assert run.orchestration_mode == "responses-guided-with-deterministic-execution"
    provenance = run.controller.model_provenance
    assert provenance is not None
    assert provenance.provider == "openai"
    assert provenance.model == MODEL
    assert provenance.response_ids == ["resp_observe_001", "resp_strategy_001"]
    assert provenance.strategy.forecast_method == "deterministic"
    persisted_batch = service.get_batch(batch.batch_id)
    assert persisted_batch.orchestration_mode == run.orchestration_mode
    assert persisted_batch.model_provenance == provenance
    assert all(
        decision.decision_source == "deterministic_policy" and decision.model_name is None
        for decision in service._batches[actual_batch_id].decisions.values()
    )

    state = service._batches[actual_batch_id]
    forecast = state.forecasts[state.base_forecast_id or ""]
    assert all(position.p10 is None for position in forecast.positions)
    provenance_events = [event for event in state.events if event.event_type.startswith("model_")]
    planning_started = next(
        event for event in provenance_events if event.event_type == "model_planning_started"
    )
    planning_completed = next(
        event for event in provenance_events if event.event_type == "model_planning_completed"
    )
    assert planning_started.status is s.EventStatus.STARTED
    assert planning_completed.latency_ms >= 0
    assert planning_completed.input_reference == "response:resp_observe_001"
    assert planning_completed.tool_result_reference == "response:resp_strategy_001"
    assert MODEL in planning_completed.message
    assert len([event for event in provenance_events if event.event_type == "model_tool_selected"]) == 3
    strategy_event = next(
        event for event in provenance_events if event.event_type == "model_strategy_applied"
    )
    assert MODEL in strategy_event.message
    assert "forecast_method=deterministic" in strategy_event.message
    assert strategy_event.input_reference == "response:resp_strategy_001"


def test_deterministic_service_run_has_no_model_claim_or_provenance() -> None:
    service = CashCloseService(responses_adapter=OpenAIResponsesAdapter(api_key=None))
    batch = service.create_batch(
        CreateBatchRequest(as_of_date=date(2026, 9, 1), demo_mode=True)
    )

    run = service.run_batch(batch.batch_id, RunBatchRequest(use_model_planner=False))

    assert run.orchestration_mode == "deterministic-demo"
    assert run.controller.model_provenance is None
    persisted_batch = service.get_batch(batch.batch_id)
    assert persisted_batch.orchestration_mode == "deterministic-demo"
    assert persisted_batch.model_provenance is None
    assert not any(
        event.event_type.startswith("model_")
        for event in service._batches[batch.batch_id].events
    )
