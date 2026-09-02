/** Types mirrored from the FastAPI OpenAPI document and SSE source contract. */

declare const moneyStringBrand: unique symbol;
declare const confidenceStringBrand: unique symbol;

/** Decimal money serialized by Pydantic. Never coerce this to a JS number. */
export type MoneyString = string & { readonly [moneyStringBrand]: "MoneyString" };

/** A decimal in the inclusive range [0, 1], serialized as a string. */
export type ConfidenceString = string & {
  readonly [confidenceStringBrand]: "ConfidenceString";
};

export type Identifier = string;
export type CurrencyCode = string;
export type DateString = string;
export type DateTimeString = string;

export const BATCH_STATUSES = [
  "UPLOADED",
  "VALIDATING",
  "NORMALIZING",
  "RECONCILING",
  "VERIFYING",
  "FORECASTING",
  "EVALUATING",
  "COMPLETED",
  "VALIDATION_FAILED",
  "PROCESSING_FAILED",
  "CANCELLED",
] as const;
export type BatchStatus = (typeof BATCH_STATUSES)[number];

export const TERMINAL_BATCH_STATUSES = [
  "COMPLETED",
  "VALIDATION_FAILED",
  "PROCESSING_FAILED",
  "CANCELLED",
] as const satisfies readonly BatchStatus[];

export type FileKind =
  | "bank_transactions"
  | "invoices"
  | "ledger_entries"
  | "remittances";

export type RecordStatus =
  | "UNPROCESSED"
  | "CANDIDATES_FOUND"
  | "PROPOSED"
  | "AUTO_RECONCILED"
  | "MANUALLY_RECONCILED"
  | "NEEDS_REVIEW"
  | "UNRESOLVED"
  | "REJECTED";
export type ProposalStatus =
  | "PROPOSED"
  | "VERIFIED"
  | "NEEDS_REVIEW"
  | "REJECTED"
  | "COMMITTED";
export type ReconciliationDecisionType =
  | "AUTO_RECONCILED"
  | "MANUALLY_RECONCILED"
  | "NEEDS_REVIEW"
  | "UNRESOLVED"
  | "REJECTED";
export type DecisionSource = "deterministic_policy" | "human";

export type ExceptionReason =
  | "AMBIGUOUS_MATCH"
  | "MISSING_INVOICE"
  | "DUPLICATE_INVOICE"
  | "PARTIAL_PAYMENT"
  | "CURRENCY_MISMATCH"
  | "SUSPECTED_FEE"
  | "SUSPECTED_WITHHOLDING"
  | "OVERPAYMENT"
  | "UNRECONCILABLE"
  | "MISSING_REMITTANCE"
  | "INVALID_RECORD"
  | "INSUFFICIENT_EVIDENCE";
export type ExceptionStatus = "OPEN" | "IN_REVIEW" | "RESOLVED";

export type AgentName =
  | "controller"
  | "reconciliation"
  | "verification"
  | "forecast"
  | "evaluation";
export type AgentEventStatus = "started" | "succeeded" | "warning" | "failed";

export type OrchestrationMode =
  | "responses-guided-with-deterministic-execution"
  | "deterministic-demo";

export interface HealthResponse {
  status: string;
  service: string;
}

/** Runtime facts reported by the API. The UI must not infer model availability. */
export interface RuntimeCapabilitiesView {
  responses_mode_configured: boolean;
  responses_model: string;
  deterministic_fallback: "deterministic-controller";
  default_orchestration_mode: OrchestrationMode;
  transaction_trace_enabled: boolean;
}

export interface ValidationIssue {
  code: string;
  severity: "warning" | "error";
  count: number;
  record_references?: string[];
}

export interface UploadedFileView {
  file_id: Identifier;
  batch_id: Identifier;
  file_type: FileKind;
  filename: string;
  content_type: string;
  size_bytes: number;
  row_count: number;
  columns: string[];
  uploaded_at: DateTimeString;
  validation_issues?: ValidationIssue[];
}

export interface CreateBatchRequest {
  organization_id?: Identifier;
  accounting_timezone?: string;
  as_of_date?: DateString;
  demo_mode?: boolean;
}

export interface BatchView {
  batch_id: Identifier;
  organization_id: Identifier;
  status: BatchStatus;
  accounting_timezone: string;
  as_of_date: DateString;
  demo_mode: boolean;
  files: UploadedFileView[];
  created_at: DateTimeString;
  updated_at: DateTimeString;
  terminal: boolean;
}

/** The API currently uses BatchView as its only batch-summary representation. */
export type BatchSummary = BatchView;

export interface ValidateBatchResult {
  batch_id: Identifier;
  valid: boolean;
  issues?: ValidationIssue[];
}

export interface BatchValidationView {
  batch_id: Identifier;
  required_file_types: FileKind[];
  uploaded_file_types: FileKind[];
  missing_file_types: FileKind[];
  demo_fixture_available: boolean;
  validation: ValidateBatchResult;
  can_run: boolean;
}

export interface RunBatchRequest {
  horizon_days?: number;
  use_model_planner?: boolean;
}

export interface ControllerRunResult {
  batch_id: Identifier;
  status: BatchStatus;
  tool_calls: number;
  automatic_matches: number;
  exceptions_created: number;
  forecast_id?: Identifier | null;
  report_id?: Identifier | null;
}

export interface RunBatchResponse {
  batch: BatchView;
  controller: ControllerRunResult;
  orchestration_mode: string;
}

export interface Allocation {
  invoice_id: Identifier;
  amount: MoneyString;
  currency: CurrencyCode;
}

export interface EvidenceItem {
  evidence_id: Identifier;
  evidence_type: string;
  summary: string;
  source_reference: string;
}

export interface MatchProposal {
  proposal_id: Identifier;
  batch_id: Identifier;
  transaction_id: Identifier;
  allocations: Allocation[];
  total_allocated: MoneyString;
  transaction_amount: MoneyString;
  currency: CurrencyCode;
  permitted_deduction?: MoneyString;
  confidence: ConfidenceString;
  evidence: EvidenceItem[];
  risk_flags?: string[];
  status?: ProposalStatus;
  revision?: number;
  created_at: DateTimeString;
  updated_at?: DateTimeString | null;
}

export interface VerificationResult {
  proposal_id: Identifier;
  approved: boolean;
  policy_version: string;
  confidence_threshold: ConfidenceString;
  checked_at: DateTimeString;
  reasons?: string[];
  hard_risk_flags?: string[];
}

export interface ReconciliationDecision {
  decision_id: Identifier;
  batch_id: Identifier;
  transaction_id: Identifier;
  decision: ReconciliationDecisionType;
  confidence: ConfidenceString;
  decision_source: DecisionSource;
  model_name?: string | null;
  policy_version: string;
  proposal_id?: Identifier | null;
  committed_at?: DateTimeString | null;
  idempotency_key?: string | null;
}

export interface MatchList {
  items: ReconciliationDecision[];
  proposals: MatchProposal[];
  reviews?: MatchReviewView[];
}

export interface ReconciliationRecord {
  record_id: Identifier;
  record_type: "bank_transaction" | "ledger_entry";
  status: RecordStatus;
  amount: MoneyString;
  currency: CurrencyCode;
  counterparty: string;
  reference: string;
  effective_date: DateString;
}

export interface CandidateInvoice {
  invoice_id: Identifier;
  invoice_number: string;
  remaining_balance: MoneyString;
  currency: CurrencyCode;
  reference_similarity: ConfidenceString;
  counterparty_similarity: ConfidenceString;
  hard_risk_flags?: string[];
}

export interface MatchEvidenceResult {
  transaction_id: Identifier;
  evidence: EvidenceItem[];
  confidence: ConfidenceString;
  risk_flags?: string[];
}

export interface MatchReviewView {
  proposal: MatchProposal;
  transaction: ReconciliationRecord;
  verification?: VerificationResult | null;
  decision?: ReconciliationDecision | null;
  linked_exception?: ExceptionRecord | null;
  allowed_actions?: string[];
}

export interface RecordDetailView {
  record: ReconciliationRecord;
  candidates?: CandidateInvoice[];
  evidence?: MatchEvidenceResult | null;
  proposal?: MatchProposal | null;
  verification?: VerificationResult | null;
  decision?: ReconciliationDecision | null;
  exception?: ExceptionRecord | null;
}

export interface RecordList {
  items: RecordDetailView[];
}

export interface ExceptionRecord {
  exception_id: Identifier;
  batch_id: Identifier;
  record_id: Identifier;
  proposal_id?: Identifier | null;
  reason_code: ExceptionReason;
  evidence: EvidenceItem[];
  next_action: string;
  status: ExceptionStatus;
  created_at: DateTimeString;
  resolved_at?: DateTimeString | null;
  resolution?: string | null;
  amount?: MoneyString | null;
  currency?: CurrencyCode | null;
  counterparty?: string | null;
  reference?: string | null;
  candidate_invoices?: CandidateInvoice[];
}

export interface ExceptionList {
  items: ExceptionRecord[];
}

export interface ResolveExceptionRequest {
  resolution: string;
}

export interface ScenarioParameters {
  scenario_name?: string;
  action_type?: "base" | "customer_payment_delay" | "payable_delay" | "one_time_outflow";
  customer_name?: string | null;
  payable_name?: string | null;
  delay_days?: number;
  one_time_outflow?: MoneyString;
  currency?: CurrencyCode;
}

export interface CustomerPaymentDelayScenarioRequest {
  name: string;
  action_type: "customer_payment_delay";
  customer_name: string;
  delay_days: number;
  amount?: never;
  payable_name?: never;
  currency?: CurrencyCode;
}

export interface PayableDelayScenarioRequest {
  name: string;
  action_type: "payable_delay";
  payable_name: string;
  delay_days: number;
  amount?: never;
  customer_name?: never;
  currency?: CurrencyCode;
}

export interface OneTimeOutflowScenarioRequest {
  name: string;
  action_type: "one_time_outflow";
  amount: MoneyString;
  currency: CurrencyCode;
  customer_name?: never;
  payable_name?: never;
  delay_days?: 0;
}

export type ScenarioRequest =
  | CustomerPaymentDelayScenarioRequest
  | PayableDelayScenarioRequest
  | OneTimeOutflowScenarioRequest;

export interface EditMatchRequest {
  expected_revision: number;
  allocations: Allocation[];
  permitted_deduction?: MoneyString;
  edit_reason: string;
}

export interface EditMatchResult {
  proposal: MatchProposal;
  verification: VerificationResult;
}

export interface ApproveMatchRequest {
  expected_revision: number;
  idempotency_key: string;
  approval_note: string;
}

export interface RejectMatchRequest {
  expected_revision: number;
  rejection_reason: string;
}

export interface HumanReviewResult {
  proposal: MatchProposal;
  decision?: ReconciliationDecision | null;
  exception?: ExceptionRecord | null;
  idempotent_replay?: boolean;
}

export interface ForecastPosition {
  date: DateString;
  confirmed: MoneyString;
  expected: MoneyString;
  risk_adjusted: MoneyString;
  p10?: MoneyString | null;
  p50?: MoneyString | null;
  p90?: MoneyString | null;
}

export interface RunCashForecastResult {
  forecast_id: Identifier;
  batch_id: Identifier;
  currency: CurrencyCode;
  as_of_date: DateString;
  horizon_days: number;
  scenario: ScenarioParameters;
  positions: ForecastPosition[];
  minimum_expected_cash: MoneyString;
  minimum_expected_cash_date: DateString;
  shortfall_date?: DateString | null;
}

export interface MatchMetricsResult {
  batch_id: Identifier;
  precision: ConfidenceString;
  recall: ConfidenceString;
  automation_coverage: ConfidenceString;
  value_weighted_coverage: ConfidenceString;
  false_approval_rate: ConfidenceString;
  exception_recall: ConfidenceString;
  value_reconciled: MoneyString;
  unresolved_value: MoneyString;
  currency: CurrencyCode;
}

export interface BatchMetricsView {
  matching: MatchMetricsResult;
  records_processed: number;
  forecast_cash_minimum: MoneyString;
  forecast_cash_minimum_date: DateString;
  processing_time_ms: number;
}

export interface ForecastMetricsResult {
  forecast_id: Identifier;
  mae: MoneyString;
  currency: CurrencyCode;
  evaluated_days: number;
}

export interface EvaluationView {
  evaluation_id: Identifier;
  batch_id: Identifier;
  match_metrics: MatchMetricsResult;
  forecast_metrics: ForecastMetricsResult;
  ground_truth_visible_to_agent?: false;
  completed_at: DateTimeString;
}

export interface AuditEntry {
  sequence: number;
  action: string;
  actor: string;
  reference: string;
  timestamp: DateTimeString;
}

export interface AuditReportResult {
  report_id: Identifier;
  batch_id: Identifier;
  policy_version: string;
  generated_at: DateTimeString;
  entries: AuditEntry[];
}

/** SSE-only contract; FastAPI currently omits it from generated OpenAPI. */
export interface AgentEvent {
  sequence: number;
  batch_id: Identifier;
  agent_name: AgentName;
  event_type: string;
  message: string;
  input_reference?: string | null;
  tool_name?: string | null;
  tool_result_reference?: string | null;
  timestamp: DateTimeString;
  latency_ms: number;
  status: AgentEventStatus;
}

export interface AgentEventPage {
  items: AgentEvent[];
  next_sequence: number;
  terminal: boolean;
}

export interface AgentTraceView {
  batch_id: Identifier;
  record_id?: Identifier | null;
  agent_name?: AgentName | null;
  tool_name?: string | null;
  status?: AgentEventStatus | null;
  items: AgentEvent[];
  next_sequence: number;
  total_matching: number;
  terminal: boolean;
}

/** SSE-only terminal marker emitted by `/events`. */
export interface BatchEventStreamEnd {
  batch_id: Identifier;
  last_sequence: number;
  terminal: boolean;
}

export interface AgentEventMessage {
  type: "agent_event";
  id?: string;
  event: AgentEvent;
}

export interface StreamEndMessage {
  type: "stream_end";
  id?: string;
  end: BatchEventStreamEnd;
}

export interface UnknownEventMessage {
  type: "unknown";
  eventName: string;
  id?: string;
  data: string;
}

export type BatchEventMessage =
  | AgentEventMessage
  | StreamEndMessage
  | UnknownEventMessage;

export interface BatchEventPollResult {
  events: AgentEvent[];
  end: BatchEventStreamEnd | null;
  lastSequence: number;
}

export interface FastApiValidationIssue {
  loc: Array<string | number>;
  msg: string;
  type: string;
}

export interface FastApiValidationError {
  detail?: FastApiValidationIssue[];
}

export interface ApiErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
  };
  detail?: FastApiValidationIssue[];
}

export interface UploadBatchFileInput {
  fileType: FileKind;
  file: Blob;
  filename?: string;
}

export interface DemoBootstrapOptions {
  batch?: Omit<CreateBatchRequest, "demo_mode">;
  run?: RunBatchRequest;
}

export interface DemoBootstrapResult {
  createdBatch: BatchView;
  run: RunBatchResponse;
}

export interface AuditDownload {
  blob: Blob;
  filename: string;
  contentType: string;
}
