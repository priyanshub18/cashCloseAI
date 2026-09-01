"""HTTP request and response schemas."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from packages.agents import schemas as agent_schemas


Identifier = agent_schemas.Identifier
CurrencyCode = agent_schemas.CurrencyCode
Money = agent_schemas.Money


class ApiSchema(agent_schemas.StrictSchema):
    pass


class CreateBatchRequest(ApiSchema):
    organization_id: Identifier = "ORG-DEMO"
    accounting_timezone: Annotated[str, StringConstraints(min_length=1, max_length=100)] = "Asia/Kolkata"
    as_of_date: date = Field(default_factory=date.today)
    demo_mode: bool = True


class UploadedFileView(ApiSchema):
    file_id: Identifier
    batch_id: Identifier
    file_type: agent_schemas.FileKind
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    content_type: Annotated[str, Field(min_length=1, max_length=100)]
    size_bytes: int = Field(ge=0, le=25_000_000)
    row_count: int = Field(ge=0)
    columns: list[str]
    uploaded_at: datetime
    validation_issues: list[agent_schemas.ValidationIssue] = Field(default_factory=list)


class BatchView(ApiSchema):
    batch_id: Identifier
    organization_id: Identifier
    status: agent_schemas.BatchStatus
    accounting_timezone: str
    as_of_date: date
    demo_mode: bool
    files: list[UploadedFileView]
    created_at: datetime
    updated_at: datetime
    terminal: bool


class RunBatchRequest(ApiSchema):
    horizon_days: int = Field(default=30, ge=1, le=365)
    use_model_planner: bool = False


class RunBatchResponse(ApiSchema):
    batch: BatchView
    controller: agent_schemas.ControllerRunResult
    orchestration_mode: str


class MatchList(ApiSchema):
    items: list[agent_schemas.ReconciliationDecision]
    proposals: list[agent_schemas.MatchProposal]


class ExceptionList(ApiSchema):
    items: list[agent_schemas.ExceptionRecord]


class ResolveExceptionRequest(ApiSchema):
    resolution: Annotated[str, Field(min_length=3, max_length=1000)]


class ScenarioActionType(StrEnum):
    CUSTOMER_PAYMENT_DELAY = "customer_payment_delay"
    ONE_TIME_OUTFLOW = "one_time_outflow"


class ScenarioRequest(ApiSchema):
    name: Annotated[str, Field(min_length=1, max_length=100)]
    action_type: ScenarioActionType
    customer_name: str | None = Field(default=None, min_length=1, max_length=200)
    delay_days: int = Field(default=0, ge=0, le=90)
    amount: Money | None = Field(default=None, ge=Decimal("0"))
    currency: CurrencyCode = "USD"

    @model_validator(mode="after")
    def validate_action(self) -> "ScenarioRequest":
        if self.action_type is ScenarioActionType.CUSTOMER_PAYMENT_DELAY:
            if not self.customer_name or self.delay_days < 1:
                raise ValueError("customer payment delay requires customer_name and delay_days >= 1")
            if self.amount is not None:
                raise ValueError("amount is not accepted for customer payment delay")
        if self.action_type is ScenarioActionType.ONE_TIME_OUTFLOW:
            if self.amount is None or self.amount <= 0:
                raise ValueError("one-time outflow requires a positive amount")
            if self.customer_name is not None or self.delay_days:
                raise ValueError("customer fields are not accepted for a one-time outflow")
        return self


class BatchMetricsView(ApiSchema):
    matching: agent_schemas.MatchMetricsResult
    records_processed: int = Field(ge=0)
    forecast_cash_minimum: Money
    forecast_cash_minimum_date: date
    processing_time_ms: int = Field(ge=0)


class EvaluationView(ApiSchema):
    evaluation_id: Identifier
    batch_id: Identifier
    match_metrics: agent_schemas.MatchMetricsResult
    forecast_metrics: agent_schemas.ForecastMetricsResult
    ground_truth_visible_to_agent: bool = False
    completed_at: datetime


class ErrorDetail(ApiSchema):
    code: str
    message: str


class ErrorResponse(ApiSchema):
    error: ErrorDetail
