"""Optional OpenAI Responses API planner.

The deterministic controller is the default.  This adapter is deliberately lazy:
importing the application or running its demo never requires an API key or a network
request.  When enabled, the model can select validated tools but cannot bypass the
toolbox's arithmetic, verification, or persistence guardrails.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Mapping

from pydantic import BaseModel

from . import schemas as s
from .tools import responses_tool_definitions, validate_tool_input, validate_tool_output


class OpenAIAdapterNotConfigured(RuntimeError):
    pass


class OpenAIPlannerResponseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResponsesAdapterConfig:
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"
    max_output_tokens: int = 2_000
    include_side_effect_tools: bool = True
    max_observation_calls: int = 3

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Responses planner model cannot be empty")
        if not 1 <= self.max_observation_calls <= 3:
            raise ValueError("max_observation_calls must be between one and three")


OBSERVATION_TOOL_NAMES = (
    "inspect_batch",
    "validate_batch",
    "get_batch_summary",
)
STRATEGY_TOOL_NAME = "select_controller_strategy"
ToolExecutor = Callable[[str, dict[str, Any]], BaseModel | dict[str, Any]]


class OpenAIResponsesAdapter:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        config: ResponsesAdapterConfig | None = None,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.config = config or ResponsesAdapterConfig(
            model=os.getenv("OPENAI_MODEL_CONTROLLER", "gpt-5.6-terra")
        )
        self._client = client

    @property
    def is_configured(self) -> bool:
        return self._client is not None or bool(self._api_key)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise OpenAIAdapterNotConfigured(
                "Set OPENAI_API_KEY only when model-guided orchestration is explicitly enabled"
            )
        from openai import OpenAI

        self._client = OpenAI(api_key=self._api_key)
        return self._client

    @staticmethod
    def controller_instructions() -> str:
        prompt_path = Path(__file__).with_name("prompts") / "controller.md"
        return prompt_path.read_text(encoding="utf-8")

    def build_request(
        self,
        observation: dict[str, Any],
        *,
        previous_response_id: str | None = None,
        allowed_tool_names: Collection[str] | None = None,
        require_tool: bool = False,
    ) -> dict[str, Any]:
        definitions = responses_tool_definitions(
            include_side_effects=self.config.include_side_effect_tools
        )
        if allowed_tool_names is not None:
            allowed = set(allowed_tool_names)
            definitions = [item for item in definitions if item["name"] in allowed]
            found = {item["name"] for item in definitions}
            if found != allowed:
                missing = ", ".join(sorted(allowed - found))
                raise ValueError(f"unavailable Responses tools requested: {missing}")
        request: dict[str, Any] = {
            "model": self.config.model,
            "instructions": self.controller_instructions(),
            "input": json.dumps(observation, separators=(",", ":"), default=str),
            "tools": definitions,
            "tool_choice": "required" if require_tool else "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": self.config.reasoning_effort},
            "max_output_tokens": self.config.max_output_tokens,
            # previous_response_id continuity requires application state.  The
            # planner only sends batch metadata and validation summaries, never
            # transaction rows, amounts, or model-authored financial actions.
            "store": True,
        }
        if previous_response_id:
            request["previous_response_id"] = previous_response_id
        return request

    def plan(
        self,
        observation: dict[str, Any],
        *,
        previous_response_id: str | None = None,
        allowed_tool_names: Collection[str] | None = None,
        require_tool: bool = False,
    ) -> s.ModelPlan:
        response = self._get_client().responses.create(
            **self.build_request(
                observation,
                previous_response_id=previous_response_id,
                allowed_tool_names=allowed_tool_names,
                require_tool=require_tool,
            )
        )
        return self._parse_response(response)

    def continue_with_tool_outputs(
        self,
        *,
        previous_response_id: str,
        outputs: Iterable[tuple[str, dict[str, Any]]],
        tool_definitions: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, str] = "auto",
        additional_input_models: Mapping[str, type[BaseModel]] | None = None,
    ) -> s.ModelPlan:
        items = [
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(output, separators=(",", ":"), default=str),
            }
            for call_id, output in outputs
        ]
        response = self._get_client().responses.create(
            model=self.config.model,
            instructions=self.controller_instructions(),
            previous_response_id=previous_response_id,
            input=items,
            tools=(
                tool_definitions
                if tool_definitions is not None
                else responses_tool_definitions(
                    include_side_effects=self.config.include_side_effect_tools
                )
            ),
            tool_choice=tool_choice,
            parallel_tool_calls=False,
            reasoning={"effort": self.config.reasoning_effort},
            max_output_tokens=self.config.max_output_tokens,
            store=True,
        )
        return self._parse_response(
            response,
            additional_input_models=additional_input_models,
        )

    @staticmethod
    def strategy_tool_definition() -> dict[str, Any]:
        return {
            "type": "function",
            "name": STRATEGY_TOOL_NAME,
            "description": (
                "Select bounded controller strategies. This chooses ordering and search "
                "breadth only; deterministic tools perform all financial calculations and writes."
            ),
            "parameters": s.ControllerStrategy.model_json_schema(),
            "strict": True,
        }

    def orchestrate_run(
        self,
        observation: dict[str, Any],
        *,
        execute_tool: ToolExecutor,
    ) -> s.ModelGuidedRunPlan:
        """Run exactly two bounded model turns and execute only read-only choices.

        Turn one lets the model choose one to three observation tools. Their
        validated outputs are returned to the model. Turn two must select one
        strict controller strategy. The strategy changes orchestration only;
        all amounts, matching, verification, writes, and metrics remain in the
        deterministic controller/toolbox.
        """

        batch_id = str(observation.get("batch_id", "")).strip()
        if not batch_id:
            raise ValueError("model-guided orchestration requires observation.batch_id")

        observation_plan = self.plan(
            observation,
            allowed_tool_names=OBSERVATION_TOOL_NAMES,
            require_tool=True,
        )
        if not 1 <= len(observation_plan.calls) <= self.config.max_observation_calls:
            raise OpenAIPlannerResponseError(
                "model must choose between one and three observation tools"
            )

        seen_call_ids: set[str] = set()
        seen_tools: set[str] = set()
        tool_outputs: list[tuple[str, dict[str, Any]]] = []
        choices: list[s.ModelToolChoice] = []
        for call in observation_plan.calls:
            if call.call_id in seen_call_ids or call.tool_name in seen_tools:
                raise OpenAIPlannerResponseError(
                    "model repeated an observation call in the bounded planning turn"
                )
            seen_call_ids.add(call.call_id)
            seen_tools.add(call.tool_name)
            if call.tool_name not in OBSERVATION_TOOL_NAMES:
                raise OpenAIPlannerResponseError(
                    f"model selected disallowed observation tool {call.tool_name}"
                )
            if call.arguments.get("batch_id") != batch_id:
                raise OpenAIPlannerResponseError(
                    "model observation tool call changed the authorized batch_id"
                )
            result = validate_tool_output(
                call.tool_name,
                execute_tool(call.tool_name, call.arguments),
            )
            serialized = result.model_dump(mode="json")
            tool_outputs.append((call.call_id, serialized))
            choices.append(
                s.ModelToolChoice(
                    response_id=observation_plan.response_id,
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                    outcome="executed",
                )
            )

        strategy_plan = self.continue_with_tool_outputs(
            previous_response_id=observation_plan.response_id,
            outputs=tool_outputs,
            tool_definitions=[self.strategy_tool_definition()],
            tool_choice={"type": "function", "name": STRATEGY_TOOL_NAME},
            additional_input_models={STRATEGY_TOOL_NAME: s.ControllerStrategy},
        )
        if len(strategy_plan.calls) != 1:
            raise OpenAIPlannerResponseError(
                "model must select exactly one controller strategy"
            )
        strategy_call = strategy_plan.calls[0]
        if strategy_call.tool_name != STRATEGY_TOOL_NAME:
            raise OpenAIPlannerResponseError("model returned an invalid strategy tool")
        strategy = s.ControllerStrategy.model_validate(strategy_call.arguments)
        if strategy.batch_id != batch_id:
            raise OpenAIPlannerResponseError(
                "model strategy changed the authorized batch_id"
            )
        if observation_plan.model != strategy_plan.model:
            raise OpenAIPlannerResponseError("model identity changed during planner continuation")
        if observation_plan.response_id == strategy_plan.response_id:
            raise OpenAIPlannerResponseError("planner continuation reused a response id")
        if strategy_call.call_id in seen_call_ids:
            raise OpenAIPlannerResponseError("planner continuation reused a tool call id")
        choices.append(
            s.ModelToolChoice(
                response_id=strategy_plan.response_id,
                call_id=strategy_call.call_id,
                tool_name=strategy_call.tool_name,
                arguments=strategy_call.arguments,
                outcome="strategy_selected",
            )
        )
        provenance = s.ModelOrchestrationProvenance(
            requested_model=self.config.model,
            model=strategy_plan.model,
            response_ids=[observation_plan.response_id, strategy_plan.response_id],
            tool_choices=choices,
            strategy=strategy,
        )
        return s.ModelGuidedRunPlan(strategy=strategy, provenance=provenance)

    @staticmethod
    def _parse_response(
        response: Any,
        *,
        additional_input_models: Mapping[str, type[BaseModel]] | None = None,
    ) -> s.ModelPlan:
        def response_field(name: str, default: Any = None) -> Any:
            return response.get(name, default) if isinstance(response, dict) else getattr(
                response, name, default
            )

        status = response_field("status", "completed")
        if status not in {None, "completed"}:
            raise OpenAIPlannerResponseError(f"Responses planner returned status {status}")
        response_id = response_field("id")
        model = response_field("model")
        if not response_id or not model:
            raise OpenAIPlannerResponseError(
                "Responses planner result omitted response id or model provenance"
            )
        calls: list[s.PlannedToolCall] = []
        validators = dict(additional_input_models or {})
        for item in response_field("output", []):
            item_type = (
                item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            )
            if item_type != "function_call":
                continue
            try:
                name = item.get("name") if isinstance(item, dict) else item.name
                call_id = item.get("call_id") if isinstance(item, dict) else item.call_id
                encoded_arguments = (
                    item.get("arguments") if isinstance(item, dict) else item.arguments
                )
                raw_arguments = (
                    json.loads(encoded_arguments)
                    if isinstance(encoded_arguments, str)
                    else encoded_arguments
                )
                if name in validators:
                    validated = validators[name].model_validate(raw_arguments)
                else:
                    validated = validate_tool_input(name, raw_arguments)
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OpenAIPlannerResponseError(
                    f"model returned invalid arguments for {locals().get('name', 'unknown')}"
                ) from exc
            calls.append(
                s.PlannedToolCall(
                    call_id=call_id,
                    tool_name=name,
                    arguments=validated.model_dump(mode="json"),
                )
            )
        output_text = response_field("output_text") or None
        return s.ModelPlan(
            response_id=response_id,
            model=model,
            calls=calls,
            final_message=output_text[:2_000] if output_text else None,
        )
