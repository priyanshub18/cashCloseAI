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
from typing import Any, Iterable

from .schemas import ModelPlan, PlannedToolCall
from .tools import responses_tool_definitions, validate_tool_input


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


class OpenAIResponsesAdapter:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        config: ResponsesAdapterConfig | None = None,
        client: Any | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.config = config or ResponsesAdapterConfig()
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
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.config.model,
            "instructions": self.controller_instructions(),
            "input": json.dumps(observation, separators=(",", ":"), default=str),
            "tools": responses_tool_definitions(
                include_side_effects=self.config.include_side_effect_tools
            ),
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "reasoning": {"effort": self.config.reasoning_effort},
            "max_output_tokens": self.config.max_output_tokens,
            "store": False,
        }
        if previous_response_id:
            request["previous_response_id"] = previous_response_id
        return request

    def plan(
        self,
        observation: dict[str, Any],
        *,
        previous_response_id: str | None = None,
    ) -> ModelPlan:
        response = self._get_client().responses.create(
            **self.build_request(observation, previous_response_id=previous_response_id)
        )
        return self._parse_response(response)

    def continue_with_tool_outputs(
        self,
        *,
        previous_response_id: str,
        outputs: Iterable[tuple[str, dict[str, Any]]],
    ) -> ModelPlan:
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
            tools=responses_tool_definitions(
                include_side_effects=self.config.include_side_effect_tools
            ),
            tool_choice="auto",
            parallel_tool_calls=False,
            reasoning={"effort": self.config.reasoning_effort},
            max_output_tokens=self.config.max_output_tokens,
            store=False,
        )
        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: Any) -> ModelPlan:
        calls: list[PlannedToolCall] = []
        for item in getattr(response, "output", []):
            if getattr(item, "type", None) != "function_call":
                continue
            try:
                raw_arguments = json.loads(item.arguments)
                validated = validate_tool_input(item.name, raw_arguments)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OpenAIPlannerResponseError(
                    f"model returned invalid arguments for {getattr(item, 'name', 'unknown')}"
                ) from exc
            calls.append(
                PlannedToolCall(
                    call_id=item.call_id,
                    tool_name=item.name,
                    arguments=validated.model_dump(mode="json"),
                )
            )
        output_text = getattr(response, "output_text", None) or None
        return ModelPlan(
            response_id=response.id,
            calls=calls,
            final_message=output_text,
        )

