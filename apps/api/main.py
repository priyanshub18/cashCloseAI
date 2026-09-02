"""FastAPI routes for CashClose AI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from packages.agents import schemas as agent_schemas
from packages.agents.openai_adapter import OpenAIAdapterNotConfigured

from .schemas import (
    BatchMetricsView,
    BatchValidationView,
    BatchView,
    AgentEventPage,
    AgentTraceView,
    ApproveMatchRequest,
    CreateBatchRequest,
    EvaluationView,
    EditMatchRequest,
    ExceptionList,
    MatchList,
    RecordDetailView,
    RecordList,
    RejectMatchRequest,
    ResolveExceptionRequest,
    RuntimeCapabilitiesView,
    RunBatchRequest,
    RunBatchResponse,
    ScenarioRequest,
    UploadedFileView,
)
from .service import (
    CashCloseService,
    ConflictError,
    DomainError,
    GuardrailError,
    NotFoundError,
    UploadValidationError,
)


def create_app(service: CashCloseService | None = None) -> FastAPI:
    application = FastAPI(
        title="CashClose AI API",
        version="0.1.0",
        description=(
            "Agentic cash reconciliation with deterministic financial calculations, "
            "verified writes, exception handling, forecasting, and audit evidence."
        ),
    )
    application.state.cashclose = service or CashCloseService()
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "Last-Event-ID"],
    )

    @application.exception_handler(NotFoundError)
    async def handle_not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(404, exc)

    @application.exception_handler(UploadValidationError)
    async def handle_upload_error(_: Request, exc: UploadValidationError) -> JSONResponse:
        return _error_response(422, exc)

    @application.exception_handler(OpenAIAdapterNotConfigured)
    async def handle_openai_configuration(
        _: Request, exc: OpenAIAdapterNotConfigured
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "code": "MODEL_PLANNER_NOT_CONFIGURED",
                    "message": str(exc),
                }
            },
        )

    @application.exception_handler(GuardrailError)
    async def handle_guardrail(_: Request, exc: GuardrailError) -> JSONResponse:
        return _error_response(409, exc)

    @application.exception_handler(ConflictError)
    async def handle_conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return _error_response(409, exc)

    @application.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return _error_response(400, exc)

    @application.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "cashclose-api"}

    @application.get(
        "/api/capabilities",
        response_model=RuntimeCapabilitiesView,
        tags=["system"],
    )
    def get_capabilities() -> RuntimeCapabilitiesView:
        return _service(application).get_runtime_capabilities()

    @application.post("/api/batches", response_model=BatchView, status_code=201, tags=["batches"])
    def create_batch(payload: CreateBatchRequest) -> BatchView:
        return _service(application).create_batch(payload)

    @application.post(
        "/api/batches/{batch_id}/files",
        response_model=UploadedFileView,
        status_code=201,
        tags=["batches"],
    )
    async def upload_batch_file(
        batch_id: str,
        file_type: agent_schemas.FileKind = Form(...),
        file: UploadFile = File(...),
    ) -> UploadedFileView:
        content = await file.read()
        return _service(application).upload_csv(
            batch_id,
            file_type=file_type,
            filename=file.filename or f"{file_type.value}.csv",
            content_type=file.content_type or "text/csv",
            content=content,
        )

    @application.post(
        "/api/batches/{batch_id}/run",
        response_model=RunBatchResponse,
        tags=["batches"],
    )
    def run_batch(batch_id: str, payload: RunBatchRequest) -> RunBatchResponse:
        return _service(application).run_batch(batch_id, payload)

    @application.get("/api/batches/{batch_id}", response_model=BatchView, tags=["batches"])
    def get_batch(batch_id: str) -> BatchView:
        return _service(application).get_batch(batch_id)

    @application.get(
        "/api/batches/{batch_id}/validation",
        response_model=BatchValidationView,
        tags=["batches"],
    )
    def get_batch_validation(batch_id: str) -> BatchValidationView:
        return _service(application).get_validation(batch_id)

    @application.get("/api/batches/{batch_id}/events", tags=["batches"])
    async def stream_batch_events(
        request: Request,
        batch_id: str,
        after_sequence: int = Query(default=0, ge=0),
        follow: bool = Query(default=False),
    ) -> StreamingResponse:
        # Resolve the batch before returning a streaming response so 404s retain a
        # normal JSON error body instead of surfacing inside the stream iterator.
        _service(application).get_batch(batch_id)
        last_event_id = request.headers.get("last-event-id")
        if last_event_id and last_event_id.isdigit():
            after_sequence = max(after_sequence, int(last_event_id))
        return StreamingResponse(
            _event_stream(
                _service(application),
                batch_id=batch_id,
                after_sequence=after_sequence,
                follow=follow,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get(
        "/api/batches/{batch_id}/events/snapshot",
        response_model=AgentEventPage,
        tags=["batches"],
    )
    def get_batch_event_snapshot(
        batch_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> AgentEventPage:
        return _service(application).event_page(batch_id, after_sequence)

    @application.get(
        "/api/batches/{batch_id}/trace",
        response_model=AgentTraceView,
        tags=["batches"],
    )
    def get_batch_trace(
        batch_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=500, ge=1, le=2_000),
        record_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        ),
        agent_name: agent_schemas.AgentName | None = Query(default=None),
        tool_name: str | None = Query(default=None, min_length=1, max_length=100),
        status: agent_schemas.EventStatus | None = Query(default=None),
    ) -> AgentTraceView:
        return _service(application).get_agent_trace(
            batch_id,
            after_sequence=after_sequence,
            limit=limit,
            record_id=record_id,
            agent_name=agent_name,
            tool_name=tool_name,
            status=status,
        )

    @application.get(
        "/api/batches/{batch_id}/records",
        response_model=RecordList,
        tags=["reconciliation"],
    )
    def get_records(batch_id: str) -> RecordList:
        return _service(application).list_records(batch_id)

    @application.get(
        "/api/batches/{batch_id}/records/{record_id}",
        response_model=RecordDetailView,
        tags=["reconciliation"],
    )
    def get_record(batch_id: str, record_id: str) -> RecordDetailView:
        return _service(application).get_record_detail(batch_id, record_id)

    @application.get(
        "/api/batches/{batch_id}/metrics",
        response_model=BatchMetricsView,
        tags=["batches"],
    )
    def get_metrics(batch_id: str) -> BatchMetricsView:
        return _service(application).get_metrics(batch_id)

    @application.get(
        "/api/batches/{batch_id}/matches", response_model=MatchList, tags=["reconciliation"]
    )
    def get_matches(batch_id: str) -> MatchList:
        return _service(application).list_matches(batch_id)

    @application.get(
        "/api/batches/{batch_id}/exceptions",
        response_model=ExceptionList,
        tags=["reconciliation"],
    )
    def get_exceptions(batch_id: str) -> ExceptionList:
        return _service(application).list_exceptions(batch_id)

    @application.patch(
        "/api/matches/{proposal_id}",
        response_model=agent_schemas.EditMatchResult,
        tags=["reconciliation"],
    )
    def edit_match(
        proposal_id: str, payload: EditMatchRequest
    ) -> agent_schemas.EditMatchResult:
        return _service(application).edit_match(proposal_id, payload)

    @application.post(
        "/api/matches/{proposal_id}/approve",
        response_model=agent_schemas.HumanReviewResult,
        tags=["reconciliation"],
    )
    def approve_match(
        proposal_id: str, payload: ApproveMatchRequest
    ) -> agent_schemas.HumanReviewResult:
        return _service(application).approve_match(proposal_id, payload)

    @application.post(
        "/api/matches/{proposal_id}/reject",
        response_model=agent_schemas.HumanReviewResult,
        tags=["reconciliation"],
    )
    def reject_match(
        proposal_id: str, payload: RejectMatchRequest
    ) -> agent_schemas.HumanReviewResult:
        return _service(application).reject_match(proposal_id, payload)

    @application.post(
        "/api/exceptions/{exception_id}/review",
        response_model=agent_schemas.ExceptionRecord,
        tags=["reconciliation"],
    )
    def request_exception_review(exception_id: str) -> agent_schemas.ExceptionRecord:
        return _service(application).request_exception_review(exception_id)

    @application.post(
        "/api/exceptions/{exception_id}/resolve",
        response_model=agent_schemas.ExceptionRecord,
        tags=["reconciliation"],
    )
    def resolve_exception(
        exception_id: str, payload: ResolveExceptionRequest
    ) -> agent_schemas.ExceptionRecord:
        return _service(application).resolve_exception(exception_id, payload)

    @application.get(
        "/api/batches/{batch_id}/forecast",
        response_model=agent_schemas.RunCashForecastResult,
        tags=["forecast"],
    )
    def get_forecast(batch_id: str) -> agent_schemas.RunCashForecastResult:
        return _service(application).get_forecast(batch_id)

    @application.post(
        "/api/batches/{batch_id}/scenarios",
        response_model=agent_schemas.RunCashForecastResult,
        tags=["forecast"],
    )
    def run_scenario(
        batch_id: str, payload: ScenarioRequest
    ) -> agent_schemas.RunCashForecastResult:
        return _service(application).run_scenario(batch_id, payload)

    @application.get(
        "/api/batches/{batch_id}/audit",
        response_model=agent_schemas.AuditReportResult,
        tags=["evaluation"],
    )
    def get_audit(batch_id: str) -> agent_schemas.AuditReportResult:
        return _service(application).get_audit(batch_id)

    @application.get(
        "/api/batches/{batch_id}/audit/download",
        tags=["evaluation"],
    )
    def download_audit(batch_id: str) -> Response:
        report = _service(application).get_audit(batch_id)
        return Response(
            content=report.model_dump_json(indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="cashclose-audit-{batch_id}.json"'
                )
            },
        )

    @application.get(
        "/api/batches/{batch_id}/evaluation",
        response_model=EvaluationView,
        tags=["evaluation"],
    )
    def get_evaluation(batch_id: str) -> EvaluationView:
        return _service(application).get_evaluation(batch_id)

    return application


def _service(application: FastAPI) -> CashCloseService:
    return application.state.cashclose


def _error_response(status_code: int, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


async def _event_stream(
    service: CashCloseService,
    *,
    batch_id: str,
    after_sequence: int,
    follow: bool,
) -> AsyncIterator[str]:
    cursor = after_sequence
    while True:
        events = service.events_after(batch_id, cursor)
        for event in events:
            cursor = event.sequence
            payload = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
            yield f"id: {event.sequence}\nevent: agent_event\ndata: {payload}\n\n"
        batch = service.get_batch(batch_id)
        if not follow or batch.terminal:
            end = json.dumps(
                {"batch_id": batch_id, "last_sequence": cursor, "terminal": batch.terminal},
                separators=(",", ":"),
            )
            yield f"event: stream_end\ndata: {end}\n\n"
            return
        yield ": heartbeat\n\n"
        await asyncio.sleep(0.25)


app = create_app()
