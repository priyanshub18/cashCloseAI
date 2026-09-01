import type {
  AgentEvent,
  AgentEventPage,
  ApiErrorEnvelope,
  ApproveMatchRequest,
  AuditDownload,
  AuditReportResult,
  BatchEventMessage,
  BatchEventPollResult,
  BatchEventStreamEnd,
  BatchMetricsView,
  BatchSummary,
  BatchValidationView,
  BatchView,
  CreateBatchRequest,
  DemoBootstrapOptions,
  DemoBootstrapResult,
  EditMatchRequest,
  EditMatchResult,
  EvaluationView,
  ExceptionList,
  ExceptionRecord,
  FastApiValidationIssue,
  FileKind,
  HealthResponse,
  HumanReviewResult,
  MatchList,
  RecordDetailView,
  RecordList,
  RejectMatchRequest,
  ResolveExceptionRequest,
  RunBatchRequest,
  RunBatchResponse,
  RunCashForecastResult,
  ScenarioRequest,
  UploadedFileView,
  UploadBatchFileInput,
} from "./cashclose-types";

export const DEFAULT_CASHCLOSE_API_URL = "http://127.0.0.1:8000";

export type FetchLike = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export interface CashCloseClientOptions {
  /** API origin only; endpoint paths already include `/api`. Empty means same-origin. */
  baseUrl?: string;
  fetch?: FetchLike;
  defaultHeaders?: HeadersInit;
  credentials?: RequestCredentials;
  timeoutMs?: number;
  /** Optional future auth hook. Current FastAPI CORS does not allow Authorization cross-origin. */
  getAccessToken?: () => string | null | undefined | Promise<string | null | undefined>;
}

export interface ApiRequestOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
  timeoutMs?: number;
}

export interface StreamBatchEventsOptions extends ApiRequestOptions {
  afterSequence?: number;
  follow?: boolean;
}

export interface WatchBatchEventsOptions extends ApiRequestOptions {
  afterSequence?: number;
  transport?: "auto" | "stream" | "poll";
  pollIntervalMs?: number;
  onTransportChange?: (
    transport: "stream" | "poll",
    reason?: CashCloseApiError,
  ) => void;
}

interface OpenResponse {
  response: Response;
  method: string;
  url: string;
  cleanup: () => void;
}

interface AbortContext {
  signal: AbortSignal | undefined;
  cleanup: () => void;
  timedOut: () => boolean;
}

interface RawSseEvent {
  eventName: string;
  id?: string;
  data: string;
}

export interface CashCloseApiErrorOptions {
  status?: number | null;
  code?: string;
  details?: unknown;
  method?: string;
  url?: string;
  requestId?: string | null;
  retryable?: boolean;
  cause?: unknown;
}

export class CashCloseApiError extends Error {
  readonly status: number | null;
  readonly code: string;
  readonly details: unknown;
  readonly method?: string;
  readonly url?: string;
  readonly requestId: string | null;
  readonly retryable: boolean;
  readonly cause?: unknown;

  constructor(message: string, options: CashCloseApiErrorOptions = {}) {
    super(message);
    this.name = "CashCloseApiError";
    this.status = options.status ?? null;
    this.code = options.code ?? "API_ERROR";
    this.details = options.details;
    this.method = options.method;
    this.url = options.url;
    this.requestId = options.requestId ?? null;
    this.retryable =
      options.retryable ??
      (this.status === null ||
        this.status === 408 ||
        this.status === 429 ||
        this.status >= 500);
    this.cause = options.cause;
  }
}

export function isCashCloseApiError(error: unknown): error is CashCloseApiError {
  return error instanceof CashCloseApiError;
}

export function resolveCashCloseApiBaseUrl(explicit?: string): string {
  const environmentValue =
    typeof process !== "undefined"
      ? process.env.NEXT_PUBLIC_CASHCLOSE_API_URL
      : undefined;
  const selected = explicit ?? environmentValue ?? DEFAULT_CASHCLOSE_API_URL;
  return selected.trim().replace(/\/+$/, "");
}

function encodePathPart(value: string, name: string): string {
  const trimmed = value.trim();
  if (!trimmed) throw new TypeError(`${name} cannot be empty`);
  return encodeURIComponent(trimmed);
}

function validationMessage(issues: FastApiValidationIssue[]): string {
  return issues
    .map((issue) => {
      const location = issue.loc.length ? issue.loc.join(".") : "request";
      return `${location}: ${issue.msg}`;
    })
    .join("; ");
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

function attachmentFilename(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header)?.[1];
  if (encoded) {
    try {
      return decodeURIComponent(encoded.replace(/^"|"$/g, ""));
    } catch {
      return encoded.replace(/^"|"$/g, "");
    }
  }
  return /filename="([^"]+)"/i.exec(header)?.[1] ??
    /filename=([^;]+)/i.exec(header)?.[1]?.trim() ??
    fallback;
}

async function errorFromResponse(
  response: Response,
  method: string,
  url: string,
): Promise<CashCloseApiError> {
  const text = await response.text();
  const payload = safeJson(text) as ApiErrorEnvelope | undefined;
  const requestId = response.headers.get("x-request-id");

  if (payload?.error?.message) {
    return new CashCloseApiError(payload.error.message, {
      status: response.status,
      code: payload.error.code ?? `HTTP_${response.status}`,
      details: payload,
      method,
      url,
      requestId,
    });
  }

  if (Array.isArray(payload?.detail)) {
    return new CashCloseApiError(validationMessage(payload.detail), {
      status: response.status,
      code: "VALIDATION_ERROR",
      details: payload,
      method,
      url,
      requestId,
    });
  }

  return new CashCloseApiError(
    text.trim() || response.statusText || `HTTP ${response.status}`,
    {
      status: response.status,
      code: `HTTP_${response.status}`,
      details: payload ?? text.slice(0, 10_000),
      method,
      url,
      requestId,
    },
  );
}

function createAbortContext(
  parent: AbortSignal | undefined,
  timeoutMs: number,
): AbortContext {
  if (!parent && timeoutMs <= 0) {
    return { signal: undefined, cleanup: () => undefined, timedOut: () => false };
  }

  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  let didTimeOut = false;
  const abortFromParent = () => controller.abort(parent?.reason);

  if (parent) {
    if (parent.aborted) abortFromParent();
    else parent.addEventListener("abort", abortFromParent, { once: true });
  }
  if (timeoutMs > 0) {
    timeout = setTimeout(() => {
      didTimeOut = true;
      controller.abort(new DOMException("Request timed out", "TimeoutError"));
    }, timeoutMs);
  }

  return {
    signal: controller.signal,
    cleanup: () => {
      if (timeout) clearTimeout(timeout);
      parent?.removeEventListener("abort", abortFromParent);
    },
    timedOut: () => didTimeOut,
  };
}

function splitRawSseEvents(buffer: string): { blocks: string[]; remainder: string } {
  const blocks: string[] = [];
  let remainder = buffer;
  while (true) {
    const match = /\r\n\r\n|\n\n|\r\r/.exec(remainder);
    if (!match || match.index === undefined) break;
    blocks.push(remainder.slice(0, match.index));
    remainder = remainder.slice(match.index + match[0].length);
  }
  return { blocks, remainder };
}

function parseRawSseBlock(block: string): RawSseEvent | null {
  let eventName = "message";
  let id: string | undefined;
  const data: string[] = [];
  for (const line of block.replace(/\r\n|\r/g, "\n").split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const separator = line.indexOf(":");
    const field = separator < 0 ? line : line.slice(0, separator);
    let value = separator < 0 ? "" : line.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "id") id = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  return { eventName, id, data: data.join("\n") };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function decodeSseEvent(raw: RawSseEvent): BatchEventMessage {
  if (raw.eventName === "agent_event") {
    const payload = safeJson(raw.data);
    if (
      !isObject(payload) ||
      !Number.isInteger(payload.sequence) ||
      typeof payload.batch_id !== "string" ||
      typeof payload.agent_name !== "string" ||
      typeof payload.event_type !== "string" ||
      typeof payload.message !== "string" ||
      typeof payload.timestamp !== "string" ||
      typeof payload.status !== "string"
    ) {
      throw new CashCloseApiError("Malformed agent_event payload", {
        status: 200,
        code: "INVALID_EVENT_STREAM",
        details: raw.data,
      });
    }
    return { type: "agent_event", id: raw.id, event: payload as unknown as AgentEvent };
  }

  if (raw.eventName === "stream_end") {
    const payload = safeJson(raw.data);
    if (
      !isObject(payload) ||
      typeof payload.batch_id !== "string" ||
      !Number.isInteger(payload.last_sequence) ||
      typeof payload.terminal !== "boolean"
    ) {
      throw new CashCloseApiError("Malformed stream_end payload", {
        status: 200,
        code: "INVALID_EVENT_STREAM",
        details: raw.data,
      });
    }
    return {
      type: "stream_end",
      id: raw.id,
      end: payload as unknown as BatchEventStreamEnd,
    };
  }

  return {
    type: "unknown",
    eventName: raw.eventName,
    id: raw.id,
    data: raw.data,
  };
}

/** Parse a complete SSE payload. Useful for tests and non-streaming environments. */
export function parseBatchEventStreamText(text: string): BatchEventMessage[] {
  const { blocks, remainder } = splitRawSseEvents(text);
  if (remainder.trim()) blocks.push(remainder);
  return blocks
    .map(parseRawSseBlock)
    .filter((event): event is RawSseEvent => event !== null)
    .map(decodeSseEvent);
}

async function* readBatchEventStream(response: Response): AsyncGenerator<BatchEventMessage> {
  if (!response.body) {
    yield* parseBatchEventStreamText(await response.text());
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const split = splitRawSseEvents(buffer);
      buffer = split.remainder;
      for (const block of split.blocks) {
        const raw = parseRawSseBlock(block);
        if (raw) yield decodeSseEvent(raw);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const raw = parseRawSseBlock(buffer);
      if (raw) yield decodeSseEvent(raw);
    }
  } finally {
    reader.releaseLock();
  }
}

function isAbortApiError(error: unknown): boolean {
  return isCashCloseApiError(error) && error.code === "REQUEST_ABORTED";
}

function waitForPoll(milliseconds: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(timeout);
        resolve();
      },
      { once: true },
    );
  });
}

export class CashCloseClient {
  readonly baseUrl: string;
  private readonly fetchImpl: FetchLike;
  private readonly defaultHeaders: Headers;
  private readonly credentials: RequestCredentials;
  private readonly timeoutMs: number;
  private readonly getAccessToken?: CashCloseClientOptions["getAccessToken"];

  constructor(options: CashCloseClientOptions = {}) {
    this.baseUrl = resolveCashCloseApiBaseUrl(options.baseUrl);
    const fetchImpl = options.fetch ?? globalThis.fetch?.bind(globalThis);
    if (!fetchImpl) throw new Error("CashCloseClient requires a fetch implementation");
    this.fetchImpl = fetchImpl;
    this.defaultHeaders = new Headers(options.defaultHeaders);
    this.credentials = options.credentials ?? "same-origin";
    this.timeoutMs = options.timeoutMs ?? 20_000;
    this.getAccessToken = options.getAccessToken;
  }

  private url(path: string): string {
    return this.baseUrl ? `${this.baseUrl}${path}` : path;
  }

  private async open(
    path: string,
    init: RequestInit,
    options: ApiRequestOptions = {},
  ): Promise<OpenResponse> {
    const method = (init.method ?? "GET").toUpperCase();
    const url = this.url(path);
    const headers = new Headers(this.defaultHeaders);
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
    new Headers(options.headers).forEach((value, key) => headers.set(key, value));

    const accessToken = await this.getAccessToken?.();
    if (accessToken && !headers.has("authorization")) {
      headers.set("authorization", `Bearer ${accessToken}`);
    }

    const abort = createAbortContext(
      options.signal,
      options.timeoutMs ?? this.timeoutMs,
    );
    try {
      const response = await this.fetchImpl(url, {
        ...init,
        method,
        headers,
        credentials: init.credentials ?? this.credentials,
        signal: abort.signal,
      });
      if (!response.ok) {
        const apiError = await errorFromResponse(response, method, url);
        abort.cleanup();
        throw apiError;
      }
      return { response, method, url, cleanup: abort.cleanup };
    } catch (error) {
      abort.cleanup();
      if (isCashCloseApiError(error)) throw error;
      if (options.signal?.aborted) {
        throw new CashCloseApiError("Request was aborted", {
          code: "REQUEST_ABORTED",
          method,
          url,
          retryable: false,
          cause: error,
        });
      }
      if (abort.timedOut()) {
        throw new CashCloseApiError("Request timed out", {
          code: "REQUEST_TIMEOUT",
          method,
          url,
          retryable: true,
          cause: error,
        });
      }
      throw new CashCloseApiError("Unable to reach the CashClose API", {
        code: "NETWORK_ERROR",
        method,
        url,
        retryable: true,
        cause: error,
      });
    }
  }

  private async requestJson<T>(
    path: string,
    init: RequestInit = {},
    options: ApiRequestOptions = {},
  ): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("accept", "application/json");
    const opened = await this.open(path, { ...init, headers }, options);
    try {
      const text = await opened.response.text();
      if (!text) return undefined as T;
      const parsed = safeJson(text);
      if (parsed === undefined) {
        throw new CashCloseApiError("API returned malformed JSON", {
          status: opened.response.status,
          code: "INVALID_JSON_RESPONSE",
          details: text.slice(0, 10_000),
          method: opened.method,
          url: opened.url,
          retryable: false,
        });
      }
      return parsed as T;
    } finally {
      opened.cleanup();
    }
  }

  private postJson<T>(
    path: string,
    body: unknown,
    options: ApiRequestOptions = {},
  ): Promise<T> {
    return this.requestJson<T>(
      path,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
      options,
    );
  }

  private patchJson<T>(
    path: string,
    body: unknown,
    options: ApiRequestOptions = {},
  ): Promise<T> {
    return this.requestJson<T>(
      path,
      {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      },
      options,
    );
  }

  health(options?: ApiRequestOptions): Promise<HealthResponse> {
    return this.requestJson<HealthResponse>("/health", {}, options);
  }

  createBatch(
    request: CreateBatchRequest = {},
    options?: ApiRequestOptions,
  ): Promise<BatchView> {
    return this.postJson<BatchView>("/api/batches", request, options);
  }

  createDemoBatch(
    request: Omit<CreateBatchRequest, "demo_mode"> = {},
    options?: ApiRequestOptions,
  ): Promise<BatchView> {
    return this.createBatch({ ...request, demo_mode: true }, options);
  }

  async uploadBatchFile(
    batchId: string,
    input: UploadBatchFileInput,
    options: ApiRequestOptions = {},
  ): Promise<UploadedFileView> {
    const form = new FormData();
    form.set("file_type", input.fileType);
    const filename =
      input.filename ??
      ("name" in input.file && typeof input.file.name === "string"
        ? input.file.name
        : `${input.fileType}.csv`);
    form.set("file", input.file, filename);
    return this.requestJson<UploadedFileView>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/files`,
      { method: "POST", body: form },
      options,
    );
  }

  runBatch(
    batchId: string,
    request: RunBatchRequest = {},
    options?: ApiRequestOptions,
  ): Promise<RunBatchResponse> {
    return this.postJson<RunBatchResponse>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/run`,
      request,
      options,
    );
  }

  runDemoBatch(
    batchId: string,
    request: Omit<RunBatchRequest, "use_model_planner"> = {},
    options?: ApiRequestOptions,
  ): Promise<RunBatchResponse> {
    return this.runBatch(batchId, { ...request, use_model_planner: false }, options);
  }

  getBatch(batchId: string, options?: ApiRequestOptions): Promise<BatchView> {
    return this.requestJson<BatchView>(
      `/api/batches/${encodePathPart(batchId, "batchId")}`,
      {},
      options,
    );
  }

  /** No separate summary route exists; this intentionally returns GET BatchView. */
  getBatchSummary(batchId: string, options?: ApiRequestOptions): Promise<BatchSummary> {
    return this.getBatch(batchId, options);
  }

  getBatchValidation(
    batchId: string,
    options?: ApiRequestOptions,
  ): Promise<BatchValidationView> {
    return this.requestJson<BatchValidationView>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/validation`,
      {},
      options,
    );
  }

  getBatchMetrics(
    batchId: string,
    options?: ApiRequestOptions,
  ): Promise<BatchMetricsView> {
    return this.requestJson<BatchMetricsView>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/metrics`,
      {},
      options,
    );
  }

  getMatches(batchId: string, options?: ApiRequestOptions): Promise<MatchList> {
    return this.requestJson<MatchList>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/matches`,
      {},
      options,
    );
  }

  getRecords(batchId: string, options?: ApiRequestOptions): Promise<RecordList> {
    return this.requestJson<RecordList>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/records`,
      {},
      options,
    );
  }

  getRecord(
    batchId: string,
    recordId: string,
    options?: ApiRequestOptions,
  ): Promise<RecordDetailView> {
    return this.requestJson<RecordDetailView>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/records/${encodePathPart(recordId, "recordId")}`,
      {},
      options,
    );
  }

  getExceptions(
    batchId: string,
    options?: ApiRequestOptions,
  ): Promise<ExceptionList> {
    return this.requestJson<ExceptionList>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/exceptions`,
      {},
      options,
    );
  }

  resolveException(
    exceptionId: string,
    request: ResolveExceptionRequest,
    options?: ApiRequestOptions,
  ): Promise<ExceptionRecord> {
    return this.postJson<ExceptionRecord>(
      `/api/exceptions/${encodePathPart(exceptionId, "exceptionId")}/resolve`,
      request,
      options,
    );
  }

  requestExceptionReview(
    exceptionId: string,
    options?: ApiRequestOptions,
  ): Promise<ExceptionRecord> {
    return this.requestJson<ExceptionRecord>(
      `/api/exceptions/${encodePathPart(exceptionId, "exceptionId")}/review`,
      { method: "POST" },
      options,
    );
  }

  editMatch(
    proposalId: string,
    request: EditMatchRequest,
    options?: ApiRequestOptions,
  ): Promise<EditMatchResult> {
    return this.patchJson<EditMatchResult>(
      `/api/matches/${encodePathPart(proposalId, "proposalId")}`,
      request,
      options,
    );
  }

  approveMatch(
    proposalId: string,
    request: ApproveMatchRequest,
    options?: ApiRequestOptions,
  ): Promise<HumanReviewResult> {
    return this.postJson<HumanReviewResult>(
      `/api/matches/${encodePathPart(proposalId, "proposalId")}/approve`,
      request,
      options,
    );
  }

  rejectMatch(
    proposalId: string,
    request: RejectMatchRequest,
    options?: ApiRequestOptions,
  ): Promise<HumanReviewResult> {
    return this.postJson<HumanReviewResult>(
      `/api/matches/${encodePathPart(proposalId, "proposalId")}/reject`,
      request,
      options,
    );
  }

  getForecast(
    batchId: string,
    options?: ApiRequestOptions,
  ): Promise<RunCashForecastResult> {
    return this.requestJson<RunCashForecastResult>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/forecast`,
      {},
      options,
    );
  }

  runScenario(
    batchId: string,
    request: ScenarioRequest,
    options?: ApiRequestOptions,
  ): Promise<RunCashForecastResult> {
    return this.postJson<RunCashForecastResult>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/scenarios`,
      request,
      options,
    );
  }

  getEvaluation(
    batchId: string,
    options?: ApiRequestOptions,
  ): Promise<EvaluationView> {
    return this.requestJson<EvaluationView>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/evaluation`,
      {},
      options,
    );
  }

  getAudit(
    batchId: string,
    options?: ApiRequestOptions,
  ): Promise<AuditReportResult> {
    return this.requestJson<AuditReportResult>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/audit`,
      {},
      options,
    );
  }

  async downloadAudit(
    batchId: string,
    options: ApiRequestOptions = {},
  ): Promise<AuditDownload> {
    const encodedBatchId = encodePathPart(batchId, "batchId");
    const opened = await this.open(
      `/api/batches/${encodedBatchId}/audit/download`,
      { headers: { accept: "application/json" } },
      options,
    );
    try {
      return {
        blob: await opened.response.blob(),
        filename: attachmentFilename(
          opened.response.headers.get("content-disposition"),
          `cashclose-audit-${batchId}.json`,
        ),
        contentType:
          opened.response.headers.get("content-type") ?? "application/json",
      };
    } finally {
      opened.cleanup();
    }
  }

  async *streamBatchEvents(
    batchId: string,
    options: StreamBatchEventsOptions = {},
  ): AsyncGenerator<BatchEventMessage> {
    const afterSequence = options.afterSequence ?? 0;
    if (!Number.isInteger(afterSequence) || afterSequence < 0) {
      throw new TypeError("afterSequence must be a non-negative integer");
    }
    const query = new URLSearchParams({
      after_sequence: String(afterSequence),
      follow: String(options.follow ?? true),
    });
    const headers = new Headers(options.headers);
    headers.set("accept", "text/event-stream");
    if (afterSequence > 0) headers.set("last-event-id", String(afterSequence));
    const opened = await this.open(
      `/api/batches/${encodePathPart(batchId, "batchId")}/events?${query}`,
      { headers },
      { ...options, timeoutMs: options.timeoutMs ?? 0 },
    );
    try {
      const contentType = opened.response.headers.get("content-type");
      if (contentType && !contentType.toLowerCase().startsWith("text/event-stream")) {
        throw new CashCloseApiError(`Expected text/event-stream, received ${contentType}`, {
          status: opened.response.status,
          code: "INVALID_EVENT_STREAM",
          method: opened.method,
          url: opened.url,
          retryable: false,
        });
      }
      yield* readBatchEventStream(opened.response);
    } finally {
      opened.cleanup();
    }
  }

  async getBatchEvents(
    batchId: string,
    options: Omit<StreamBatchEventsOptions, "follow"> = {},
  ): Promise<BatchEventPollResult> {
    const events: AgentEvent[] = [];
    let end: BatchEventStreamEnd | null = null;
    let lastSequence = options.afterSequence ?? 0;
    for await (const message of this.streamBatchEvents(batchId, {
      ...options,
      follow: false,
    })) {
      if (message.type === "agent_event") {
        events.push(message.event);
        lastSequence = Math.max(lastSequence, message.event.sequence);
      } else if (message.type === "stream_end") {
        end = message.end;
        lastSequence = Math.max(lastSequence, message.end.last_sequence);
      }
    }
    if (!end) {
      throw new CashCloseApiError("Finite event response did not include stream_end", {
        status: 200,
        code: "INVALID_EVENT_STREAM",
        retryable: true,
      });
    }
    return { events, end, lastSequence };
  }

  getBatchEventSnapshot(
    batchId: string,
    options: Omit<ApiRequestOptions, "body"> & { afterSequence?: number } = {},
  ): Promise<AgentEventPage> {
    const afterSequence = options.afterSequence ?? 0;
    if (!Number.isInteger(afterSequence) || afterSequence < 0) {
      throw new TypeError("afterSequence must be a non-negative integer");
    }
    const query = new URLSearchParams({ after_sequence: String(afterSequence) });
    return this.requestJson<AgentEventPage>(
      `/api/batches/${encodePathPart(batchId, "batchId")}/events/snapshot?${query}`,
      {},
      options,
    );
  }

  /**
   * Yield agent events continuously. In `auto` mode a failed long-lived stream
   * resumes from its last sequence using finite SSE polling, avoiding duplicates.
   */
  async *watchBatchEvents(
    batchId: string,
    options: WatchBatchEventsOptions = {},
  ): AsyncGenerator<AgentEvent> {
    const transport = options.transport ?? "auto";
    const pollIntervalMs = options.pollIntervalMs ?? 750;
    if (!Number.isInteger(pollIntervalMs) || pollIntervalMs < 100) {
      throw new TypeError("pollIntervalMs must be an integer of at least 100ms");
    }
    let cursor = options.afterSequence ?? 0;

    if (transport !== "poll") {
      options.onTransportChange?.("stream");
      try {
        for await (const message of this.streamBatchEvents(batchId, {
          afterSequence: cursor,
          follow: true,
          signal: options.signal,
          headers: options.headers,
          timeoutMs: options.timeoutMs ?? 0,
        })) {
          if (message.type === "agent_event") {
            cursor = Math.max(cursor, message.event.sequence);
            yield message.event;
          } else if (message.type === "stream_end") {
            return;
          }
        }
        return;
      } catch (error) {
        if (transport === "stream" || isAbortApiError(error)) throw error;
        const apiError = isCashCloseApiError(error)
          ? error
          : new CashCloseApiError("Event stream failed", {
              code: "EVENT_STREAM_ERROR",
              cause: error,
            });
        options.onTransportChange?.("poll", apiError);
      }
    } else {
      options.onTransportChange?.("poll");
    }

    while (!options.signal?.aborted) {
      const page = await this.getBatchEventSnapshot(batchId, {
        afterSequence: cursor,
        signal: options.signal,
        headers: options.headers,
        timeoutMs: options.timeoutMs,
      });
      for (const event of page.items) {
        cursor = Math.max(cursor, event.sequence);
        yield event;
      }
      cursor = Math.max(cursor, page.next_sequence);
      if (page.terminal) return;
      await waitForPoll(pollIntervalMs, options.signal);
    }
  }

  async bootstrapDemo(
    options: DemoBootstrapOptions = {},
    requestOptions?: ApiRequestOptions,
  ): Promise<DemoBootstrapResult> {
    const createdBatch = await this.createDemoBatch(options.batch, requestOptions);
    const run = await this.runDemoBatch(createdBatch.batch_id, options.run, requestOptions);
    return { createdBatch, run };
  }
}

export function createCashCloseClient(
  options: CashCloseClientOptions = {},
): CashCloseClient {
  return new CashCloseClient(options);
}

export function isTerminalBatchStatus(status: BatchView["status"]): boolean {
  return (
    status === "COMPLETED" ||
    status === "VALIDATION_FAILED" ||
    status === "PROCESSING_FAILED" ||
    status === "CANCELLED"
  );
}

export function expectedDemoFilename(fileType: FileKind): string {
  return `${fileType}.csv`;
}
