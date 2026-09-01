"""Agent-facing contracts and orchestration for CashClose AI.

The package deliberately keeps model planning separate from deterministic financial
actions.  API and worker processes can therefore share exactly the same validated
tool boundary.
"""

from .controller import ControllerPolicy, DeterministicController
from .openai_adapter import OpenAIResponsesAdapter
from .tools import TOOL_CONTRACTS, FinancialToolPort, responses_tool_definitions

__all__ = [
    "ControllerPolicy",
    "DeterministicController",
    "FinancialToolPort",
    "OpenAIResponsesAdapter",
    "TOOL_CONTRACTS",
    "responses_tool_definitions",
]
