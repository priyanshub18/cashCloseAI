"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowLeftRight,
  ArrowRight,
  ArrowUpRight,
  BadgeCheck,
  Banknote,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDollarSign,
  Clock3,
  Copy,
  Database,
  Download,
  FileCheck2,
  FileSpreadsheet,
  Gauge,
  LayoutDashboard,
  Link2,
  Loader2,
  LockKeyhole,
  Menu,
  Play,
  Plus,
  RefreshCw,
  Search,
  Server,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  UploadCloud,
  UserCheck,
  WandSparkles,
  X,
  XCircle,
  Zap,
  type LucideIcon,
} from "lucide-react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  CashCloseApiError,
  createCashCloseClient,
  resolveCashCloseApiBaseUrl,
} from "../../lib/cashclose-client";
import type {
  AgentEvent,
  Allocation,
  AuditReportResult,
  BatchMetricsView,
  BatchView,
  EvaluationView,
  ExceptionRecord,
  FileKind,
  MatchList,
  MatchProposal,
  ReconciliationDecision,
  RecordDetailView,
  RuntimeCapabilitiesView,
  RunCashForecastResult,
  ScenarioRequest,
} from "../../lib/cashclose-types";
import {
  addMoney,
  formatMoney,
  money,
  moneyFromMinorUnits,
  moneyToMinorUnits,
  subtractMoney,
} from "../../lib/money";
import {
  DEMO_AUDIT,
  DEMO_BATCH,
  DEMO_EVENTS,
  DEMO_EVALUATION,
  DEMO_EXCEPTION_META,
  DEMO_EXCEPTIONS,
  DEMO_FORECAST,
  DEMO_MATCHES,
  DEMO_MATCH_META,
  DEMO_METRICS,
} from "../../lib/demo-workspace";

type View = "overview" | "trace" | "reconciliation" | "exceptions" | "forecast" | "audit";
type Connection = "checking" | "online" | "offline";
type Source = "preview" | "live";
type ExecutionMode = "agentic" | "deterministic";
type TraceStageId =
  | "observe"
  | "normalize"
  | "candidates"
  | "allocation"
  | "evidence"
  | "verify"
  | "outcome";

interface WorkspaceData {
  batch: BatchView;
  metrics: BatchMetricsView;
  matches: MatchList;
  exceptions: ExceptionRecord[];
  forecast: RunCashForecastResult;
  evaluation: EvaluationView;
  audit: AuditReportResult;
  events: AgentEvent[];
  records: RecordDetailView[];
}

interface ToastState {
  kind: "success" | "error" | "info";
  message: string;
}

interface UploadState {
  file: File;
  rows: number;
  columns: string[];
  issue: string | null;
}

const INITIAL_WORKSPACE: WorkspaceData = {
  batch: DEMO_BATCH,
  metrics: DEMO_METRICS,
  matches: DEMO_MATCHES,
  exceptions: DEMO_EXCEPTIONS,
  forecast: DEMO_FORECAST,
  evaluation: DEMO_EVALUATION,
  audit: DEMO_AUDIT,
  events: DEMO_EVENTS,
  records: buildPreviewRecords(),
};

const NAVIGATION: Array<{ id: View; label: string; icon: LucideIcon }> = [
  { id: "overview", label: "Controller", icon: LayoutDashboard },
  { id: "trace", label: "Agent trace", icon: Bot },
  { id: "reconciliation", label: "Reconciliation", icon: ArrowLeftRight },
  { id: "exceptions", label: "Exceptions", icon: AlertTriangle },
  { id: "forecast", label: "Cash forecast", icon: Activity },
  { id: "audit", label: "Audit & evaluation", icon: ShieldCheck },
];

const PAGE_META: Record<View, { eyebrow: string; title: string; subtitle: string }> = {
  overview: {
    eyebrow: "CONTROLLER WORKSPACE",
    title: "Cash position, verified.",
    subtitle: "One close cockpit for evidence, exceptions, and the next 30 days of cash.",
  },
  trace: {
    eyebrow: "TRANSACTION TRACE",
    title: "Follow every control gate.",
    subtitle: "Inspect operational tool actions, evidence, and outcomes for one transaction at a time.",
  },
  reconciliation: {
    eyebrow: "RECONCILIATION WORKSPACE",
    title: "Every match has a reason.",
    subtitle: "Inspect deterministic allocations, policy checks, and the evidence behind every write.",
  },
  exceptions: {
    eyebrow: "EXCEPTION INBOX",
    title: "The records AI refused to guess.",
    subtitle: "Resolve ambiguity without hiding risk or fabricating a financial answer.",
  },
  forecast: {
    eyebrow: "30-DAY CASH OUTLOOK",
    title: "See the squeeze before it happens.",
    subtitle: "Confirmed, expected, and risk-adjusted cash calculated from verified records.",
  },
  audit: {
    eyebrow: "EVALUATION & AUDIT",
    title: "Proof, not promises.",
    subtitle: "Independent metrics, controller events, policy decisions, and a downloadable trail.",
  },
};

const FILE_REQUIREMENTS: Record<FileKind, { label: string; columns: string[] }> = {
  bank_transactions: {
    label: "Bank transactions",
    columns: ["transaction_id", "transaction_date", "amount", "currency", "reference"],
  },
  invoices: {
    label: "Invoices",
    columns: ["invoice_id", "customer_id", "amount", "currency", "invoice_date", "due_date"],
  },
  ledger_entries: {
    label: "Ledger entries",
    columns: ["entry_id", "entry_date", "amount", "currency"],
  },
  remittances: {
    label: "Remittances",
    columns: ["remittance_id", "transaction_id", "raw_text"],
  },
};

const TRACE_STAGES: Array<{
  id: TraceStageId;
  label: string;
  agent: AgentEvent["agent_name"];
  defaultTool: string;
}> = [
  { id: "observe", label: "Observe", agent: "controller", defaultTool: "inspect_batch" },
  { id: "normalize", label: "Normalize", agent: "reconciliation", defaultTool: "normalize_reference" },
  { id: "candidates", label: "Candidates", agent: "reconciliation", defaultTool: "find_candidate_invoices" },
  { id: "allocation", label: "Allocation", agent: "reconciliation", defaultTool: "solve_payment_allocation" },
  { id: "evidence", label: "Evidence", agent: "reconciliation", defaultTool: "get_match_evidence" },
  { id: "verify", label: "Verify", agent: "verification", defaultTool: "verify_match" },
  { id: "outcome", label: "Commit or exception", agent: "controller", defaultTool: "commit_match / create_exception" },
];

function confidencePercent(value: string | undefined): string {
  if (!value) return "—";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function compactMoney(value: string, currency: string): string {
  const units = moneyToMinorUnits(value);
  const negative = units < BigInt(0);
  const absolute = negative ? -units : units;
  const prefix = currency === "INR" ? "₹" : currency === "USD" ? "$" : `${currency} `;
  const sign = negative ? "−" : "";
  const crore = BigInt(1_000_000_000);
  const lakh = BigInt(10_000_000);
  const thousand = BigInt(100_000);
  if (absolute >= crore) return `${sign}${prefix}${decimalRatio(absolute, crore)}cr`;
  if (currency === "INR" && absolute >= lakh) return `${sign}${prefix}${decimalRatio(absolute, lakh)}L`;
  if (absolute >= thousand) return `${sign}${prefix}${decimalRatio(absolute, thousand)}k`;
  return formatMoney(value, currency).replace(`${currency} `, prefix);
}

function decimalRatio(value: bigint, divisor: bigint): string {
  const hundredths = (value * BigInt(100)) / divisor;
  return `${hundredths / BigInt(100)}.${(hundredths % BigInt(100)).toString().padStart(2, "0")}`;
}

function formatDate(value: string, options: Intl.DateTimeFormatOptions = {}): string {
  const date = new Date(`${value.length === 10 ? `${value}T00:00:00Z` : value}`);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("en-IN", {
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
    ...options,
  }).format(date);
}

function formatDuration(milliseconds: number): string {
  if (milliseconds === 0) return "<1ms";
  if (milliseconds < 1000) return `${milliseconds}ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

function titleCase(value: string): string {
  return value
    .toLowerCase()
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function sumAllocations(allocations: Allocation[]): string {
  return moneyFromMinorUnits(
    allocations.reduce((total, allocation) => total + moneyToMinorUnits(allocation.amount), BigInt(0)),
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof CashCloseApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}

function proposalDecision(matches: MatchList, proposal: MatchProposal): ReconciliationDecision | undefined {
  return matches.items.find((item) => item.proposal_id === proposal.proposal_id);
}

function buildPreviewRecords(): RecordDetailView[] {
  const proposalRecords = DEMO_MATCHES.proposals.map((proposal): RecordDetailView => {
    const decision = proposalDecision(DEMO_MATCHES, proposal);
    const meta = DEMO_MATCH_META[proposal.transaction_id];
    return {
      record: {
        record_id: proposal.transaction_id,
        record_type: "bank_transaction",
        status: decision?.decision ?? "PROPOSED",
        amount: proposal.transaction_amount,
        currency: proposal.currency,
        counterparty: meta?.counterparty ?? "Counterparty pending",
        reference: meta?.reference ?? proposal.transaction_id,
        effective_date: meta?.valueDate ?? DEMO_BATCH.as_of_date,
      },
      candidates: proposal.allocations.map((allocation) => ({
        invoice_id: allocation.invoice_id,
        invoice_number: allocation.invoice_id,
        remaining_balance: allocation.amount,
        currency: allocation.currency,
        reference_similarity: proposal.confidence,
        counterparty_similarity: proposal.confidence,
        hard_risk_flags: proposal.risk_flags,
      })),
      evidence: {
        transaction_id: proposal.transaction_id,
        evidence: proposal.evidence,
        confidence: proposal.confidence,
        risk_flags: proposal.risk_flags,
      },
      proposal,
      verification: {
        proposal_id: proposal.proposal_id,
        approved: decision?.decision === "AUTO_RECONCILED" || decision?.decision === "MANUALLY_RECONCILED",
        policy_version: decision?.policy_version ?? "CC-R2.4",
        confidence_threshold: "0.9500" as MatchProposal["confidence"],
        checked_at: decision?.committed_at ?? proposal.created_at,
        reasons: proposal.risk_flags?.map(titleCase) ?? [],
        hard_risk_flags: proposal.risk_flags,
      },
      decision,
    };
  });
  const proposalIds = new Set(proposalRecords.map((item) => item.record.record_id));
  const exceptionRecords = DEMO_EXCEPTIONS
    .filter((item) => !proposalIds.has(item.record_id))
    .map((item): RecordDetailView => {
      const meta = DEMO_EXCEPTION_META[item.exception_id];
      return {
        record: {
          record_id: item.record_id,
          record_type: item.record_id.startsWith("BANK-") ? "bank_transaction" : "ledger_entry",
          status: item.status === "IN_REVIEW" ? "NEEDS_REVIEW" : item.status === "RESOLVED" ? "REJECTED" : "UNRESOLVED",
          amount: (item.amount ?? money(meta?.amount ?? "0")) as RecordDetailView["record"]["amount"],
          currency: item.currency ?? meta?.currency ?? "USD",
          counterparty: item.counterparty ?? meta?.counterparty ?? "Counterparty pending",
          reference: item.reference ?? item.record_id,
          effective_date: DEMO_BATCH.as_of_date,
        },
        candidates: item.candidate_invoices,
        evidence: {
          transaction_id: item.record_id,
          evidence: item.evidence,
          confidence: "0.0000" as MatchProposal["confidence"],
          risk_flags: [item.reason_code],
        },
        exception: item,
      };
    });
  return [...proposalRecords, ...exceptionRecords];
}

function traceStageForEvent(event: AgentEvent): TraceStageId | null {
  const key = `${event.tool_name ?? ""} ${event.event_type}`.toLowerCase();
  if (/commit_match|create_exception|request_human_review|resolve_exception|decision|exception/.test(key)) return "outcome";
  if (/verify_match|verif/.test(key)) return "verify";
  if (/get_match_evidence|propose_match|parse_remittance|evidence|proposal/.test(key)) return "evidence";
  if (/solve_payment_allocation|allocation/.test(key)) return "allocation";
  if (/find_candidate|candidate/.test(key)) return "candidates";
  if (/normalize_|resolve_customer_alias|currency_and_amount|normaliz/.test(key)) return "normalize";
  if (/inspect_batch|validate_batch|batch_inspected|validation/.test(key)) return "observe";
  return null;
}

function eventsForRecord(events: AgentEvent[], detail: RecordDetailView): AgentEvent[] {
  const references = new Set([
    `record:${detail.record.record_id}`,
    detail.proposal ? `proposal:${detail.proposal.proposal_id}` : "",
    detail.decision ? `decision:${detail.decision.decision_id}` : "",
    detail.exception ? `exception:${detail.exception.exception_id}` : "",
  ].filter(Boolean));
  return events.filter((event) => {
    if (event.input_reference && references.has(event.input_reference)) return true;
    if (event.tool_result_reference && references.has(event.tool_result_reference)) return true;
    if (event.input_reference === `record:${detail.record.record_id}`) return true;
    return event.message.includes(detail.record.record_id);
  });
}

function previewEventsForRecord(detail: RecordDetailView): AgentEvent[] {
  const recordReference = `record:${detail.record.record_id}`;
  const createdAt = detail.proposal?.created_at ?? detail.exception?.created_at ?? DEMO_BATCH.created_at;
  const outcomes: Array<{ stage: TraceStageId; tool: string; agent: AgentEvent["agent_name"]; message: string; status?: AgentEvent["status"]; result: string }> = [
    { stage: "observe", tool: "inspect_batch", agent: "controller", message: `Observed ${detail.record.record_id} in the validated batch`, result: recordReference },
    { stage: "normalize", tool: "normalize_reference", agent: "reconciliation", message: "Normalized counterparty and payment reference", result: `normalized:${detail.record.record_id}` },
    { stage: "candidates", tool: "find_candidate_invoices", agent: "reconciliation", message: `Constrained the search to ${detail.candidates?.length ?? 0} eligible invoice candidate${detail.candidates?.length === 1 ? "" : "s"}`, status: detail.candidates?.length ? "succeeded" : "warning", result: `candidate-set:${detail.record.record_id}` },
    { stage: "allocation", tool: "solve_payment_allocation", agent: "reconciliation", message: detail.proposal ? `Solved ${detail.proposal.allocations.length} allocation${detail.proposal.allocations.length === 1 ? "" : "s"} with deterministic constraints` : "No safe allocation satisfied policy constraints", status: detail.proposal ? "succeeded" : "warning", result: `allocation:${detail.record.record_id}` },
    { stage: "evidence", tool: "get_match_evidence", agent: "reconciliation", message: `Assembled ${detail.evidence?.evidence.length ?? 0} evidence item${detail.evidence?.evidence.length === 1 ? "" : "s"}`, status: detail.evidence?.evidence.length ? "succeeded" : "warning", result: detail.proposal ? `proposal:${detail.proposal.proposal_id}` : `evidence:${detail.record.record_id}` },
    { stage: "verify", tool: "verify_match", agent: "verification", message: detail.verification?.approved ? `Passed policy ${detail.verification.policy_version}` : "Verification did not authorize an automatic commit", status: detail.verification?.approved ? "succeeded" : "warning", result: detail.proposal ? `verification:${detail.proposal.proposal_id}` : `verification:${detail.record.record_id}` },
    { stage: "outcome", tool: detail.exception ? "create_exception" : "commit_match", agent: "controller", message: detail.exception ? `Created ${titleCase(detail.exception.reason_code)} exception with a next action` : `Recorded ${titleCase(detail.decision?.decision ?? detail.record.status)}`, status: detail.exception ? "warning" : "succeeded", result: detail.exception ? `exception:${detail.exception.exception_id}` : `decision:${detail.decision?.decision_id ?? detail.record.record_id}` },
  ];
  return outcomes.map((item, index) => ({
    sequence: 10_000 + index,
    batch_id: DEMO_BATCH.batch_id,
    agent_name: item.agent,
    event_type: "tool_completed",
    message: item.message,
    input_reference: recordReference,
    tool_name: item.tool,
    tool_result_reference: item.result,
    timestamp: createdAt,
    latency_ms: 0,
    status: item.status ?? "succeeded",
  }));
}

function applyPreviewScenario(
  base: RunCashForecastResult,
  request: ScenarioRequest,
): RunCashForecastResult {
  const positions = base.positions.map((position) => ({ ...position }));
  if (request.action_type === "one_time_outflow") {
    for (const position of positions) {
      position.expected = subtractMoney(position.expected, request.amount);
      position.risk_adjusted = subtractMoney(position.risk_adjusted, request.amount);
      if (position.p10) position.p10 = subtractMoney(position.p10, request.amount);
      if (position.p50) position.p50 = subtractMoney(position.p50, request.amount);
      if (position.p90) position.p90 = subtractMoney(position.p90, request.amount);
    }
  } else {
    const amount = request.action_type === "customer_payment_delay" ? money("75000") : money("45000");
    const start = Math.min(4, positions.length - 1);
    const end = Math.min(start + request.delay_days, positions.length - 1);
    for (let index = start; index < end; index += 1) {
      if (request.action_type === "customer_payment_delay") {
        positions[index].expected = subtractMoney(positions[index].expected, amount);
        positions[index].risk_adjusted = subtractMoney(positions[index].risk_adjusted, amount);
      } else {
        positions[index].expected = addMoney(positions[index].expected, amount);
        positions[index].risk_adjusted = addMoney(positions[index].risk_adjusted, amount);
      }
    }
  }
  const minimum = positions.reduce(
    (current, position) =>
      moneyToMinorUnits(position.expected) < moneyToMinorUnits(current.expected) ? position : current,
    positions[0],
  );
  return {
    ...base,
    forecast_id: `${base.forecast_id}-SCENARIO`,
    scenario: {
      scenario_name: request.name,
      action_type: request.action_type,
      customer_name: request.action_type === "customer_payment_delay" ? request.customer_name : null,
      payable_name: request.action_type === "payable_delay" ? request.payable_name : null,
      delay_days: request.action_type === "one_time_outflow" ? 0 : request.delay_days,
      one_time_outflow: request.action_type === "one_time_outflow" ? request.amount : money("0"),
      currency: request.currency ?? base.currency,
    },
    positions,
    minimum_expected_cash: minimum.expected,
    minimum_expected_cash_date: minimum.date,
    shortfall_date: positions.find((position) => moneyToMinorUnits(position.expected) < BigInt(0))?.date ?? null,
  };
}

export function CashCloseApp() {
  const client = useMemo(() => createCashCloseClient(), []);
  const [view, setView] = useState<View>("overview");
  const [workspace, setWorkspace] = useState<WorkspaceData>(INITIAL_WORKSPACE);
  const [source, setSource] = useState<Source>("preview");
  const [connection, setConnection] = useState<Connection>("checking");
  const [capabilities, setCapabilities] = useState<RuntimeCapabilitiesView | null>(null);
  const [orchestrationMode, setOrchestrationMode] = useState<string>("deterministic-demo");
  const [mobileNav, setMobileNav] = useState(false);
  const [newBatchOpen, setNewBatchOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [runState, setRunState] = useState<"idle" | "running" | "complete" | "failed">("idle");
  const [runError, setRunError] = useState<string | null>(null);
  const [runEvents, setRunEvents] = useState<AgentEvent[]>([]);
  const [selectedProposalId, setSelectedProposalId] = useState<string | null>(null);
  const [selectedExceptionId, setSelectedExceptionId] = useState<string | null>(null);
  const [scenarioOpen, setScenarioOpen] = useState(false);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const [architectureOpen, setArchitectureOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);
  const restoreAttempted = useRef(false);

  const notify = useCallback((message: string, kind: ToastState["kind"] = "success") => {
    setToast({ message, kind });
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 3600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const loadBatch = useCallback(
    async (batchId: string, quiet = false) => {
      if (!quiet) setBusy("refresh");
      try {
        const [batch, metrics, matches, exceptions, forecast, evaluation, audit, records, trace] =
          await Promise.all([
            client.getBatch(batchId),
            client.getBatchMetrics(batchId),
            client.getMatches(batchId),
            client.getExceptions(batchId),
            client.getForecast(batchId),
            client.getEvaluation(batchId),
            client.getAudit(batchId),
            client.getRecords(batchId),
            client.getBatchTrace(batchId, { limit: 2000 }),
          ]);
        setWorkspace({
          batch,
          metrics,
          matches,
          exceptions: exceptions.items,
          forecast,
          evaluation,
          audit,
          events: trace.items,
          records: records.items,
        });
        setRunEvents(trace.items);
        setSource("live");
        setConnection("online");
        window.localStorage.setItem("cashclose.activeBatchId", batch.batch_id);
        const savedMode = window.localStorage.getItem(`cashclose.orchestrationMode:${batch.batch_id}`);
        if (savedMode) setOrchestrationMode(savedMode);
        if (!quiet) notify(`Loaded live batch ${batch.batch_id}`);
      } catch (error) {
        if (!quiet) notify(errorMessage(error), "error");
        throw error;
      } finally {
        if (!quiet) setBusy(null);
      }
    },
    [client, notify],
  );

  useEffect(() => {
    if (restoreAttempted.current) return;
    restoreAttempted.current = true;
    void (async () => {
      try {
        await client.health({ timeoutMs: 3500 });
        setConnection("online");
        const runtime = await client.getCapabilities({ timeoutMs: 3500 }).catch(() => null);
        setCapabilities(runtime);
        const batchId = window.localStorage.getItem("cashclose.activeBatchId");
        if (batchId) await loadBatch(batchId, true).catch(() => undefined);
      } catch {
        setConnection("offline");
      }
    })();
  }, [client, loadBatch]);

  const pumpEvents = useCallback(
    async (batchId: string, controller: AbortController) => {
      try {
        for await (const event of client.watchBatchEvents(batchId, {
          signal: controller.signal,
          transport: "auto",
          pollIntervalMs: 300,
        })) {
          setRunEvents((current) =>
            current.some((existing) => existing.sequence === event.sequence)
              ? current
              : [...current, event].sort((a, b) => a.sequence - b.sequence),
          );
          setWorkspace((current) => ({
            ...current,
            events: current.events.some((existing) => existing.sequence === event.sequence)
              ? current.events
              : [...current.events, event].sort((a, b) => a.sequence - b.sequence),
          }));
        }
      } catch (error) {
        if (!(error instanceof CashCloseApiError && error.code === "REQUEST_ABORTED")) {
          notify(`Live timeline switched off: ${errorMessage(error)}`, "info");
        }
      }
    },
    [client, notify],
  );

  const runDemo = useCallback(async (requestedMode: ExecutionMode = "deterministic") => {
    setNewBatchOpen(false);
    setRunOpen(true);
    setRunState("running");
    setRunError(null);
    setRunEvents([]);
    setBusy("run");
    const eventController = new AbortController();
    const useModelPlanner = requestedMode === "agentic" && capabilities?.responses_mode_configured === true;
    setOrchestrationMode(useModelPlanner ? "responses-requested" : "deterministic-demo");
    try {
      const created = await client.createDemoBatch({
        organization_id: "ORG-HACKATHON",
        accounting_timezone: "Asia/Kolkata",
        as_of_date: "2026-09-01",
      });
      const initialRecords = await client.getRecords(created.batch_id);
      setConnection("online");
      setSource("live");
      setWorkspace((current) => ({ ...current, batch: created, events: [], records: initialRecords.items }));
      window.localStorage.setItem("cashclose.activeBatchId", created.batch_id);
      const eventPromise = pumpEvents(created.batch_id, eventController);
      const run = await client.runBatch(
        created.batch_id,
        { horizon_days: 30, use_model_planner: useModelPlanner },
        { timeoutMs: 0 },
      );
      setOrchestrationMode(run.orchestration_mode);
      window.localStorage.setItem(`cashclose.orchestrationMode:${created.batch_id}`, run.orchestration_mode);
      await eventPromise;
      await loadBatch(created.batch_id, true);
      setRunState("complete");
      notify("Controller completed the close with an auditable result");
    } catch (error) {
      eventController.abort();
      setRunState("failed");
      const message = errorMessage(error);
      setRunError(message);
      notify(message, "error");
    } finally {
      setBusy(null);
    }
  }, [capabilities, client, loadBatch, notify, pumpEvents]);

  const runUploadedBatch = useCallback(
    async (files: Record<FileKind, UploadState>, requestedMode: ExecutionMode) => {
      setRunOpen(true);
      setRunState("running");
      setRunError(null);
      setRunEvents([]);
      setBusy("run");
      const eventController = new AbortController();
      const useModelPlanner = requestedMode === "agentic" && capabilities?.responses_mode_configured === true;
      setOrchestrationMode(useModelPlanner ? "responses-requested" : "deterministic-demo");
      try {
        const created = await client.createBatch({
          organization_id: "ORG-HACKATHON",
          accounting_timezone: "Asia/Kolkata",
          as_of_date: new Date().toISOString().slice(0, 10),
          demo_mode: false,
        });
        setWorkspace((current) => ({ ...current, batch: created, events: [], records: [] }));
        setSource("live");
        setConnection("online");
        window.localStorage.setItem("cashclose.activeBatchId", created.batch_id);
        for (const [fileType, upload] of Object.entries(files) as Array<[FileKind, UploadState]>) {
          await client.uploadBatchFile(created.batch_id, { fileType, file: upload.file });
        }
        const validation = await client.getBatchValidation(created.batch_id);
        if (!validation.can_run) {
          throw new Error(validation.validation.issues?.map((issue) => issue.code).join(", ") || "Batch validation failed");
        }
        const initialRecords = await client.getRecords(created.batch_id);
        setWorkspace((current) => ({ ...current, records: initialRecords.items }));
        const eventPromise = pumpEvents(created.batch_id, eventController);
        const run = await client.runBatch(
          created.batch_id,
          { horizon_days: 30, use_model_planner: useModelPlanner },
          { timeoutMs: 0 },
        );
        setOrchestrationMode(run.orchestration_mode);
        window.localStorage.setItem(`cashclose.orchestrationMode:${created.batch_id}`, run.orchestration_mode);
        await eventPromise;
        await loadBatch(created.batch_id, true);
        setNewBatchOpen(false);
        setRunState("complete");
        notify("Uploaded batch completed successfully");
      } catch (error) {
        eventController.abort();
        setRunState("failed");
        const message = errorMessage(error);
        setRunError(message);
        notify(message, "error");
      } finally {
        setBusy(null);
      }
    },
    [capabilities, client, loadBatch, notify, pumpEvents],
  );

  const updatePreviewMatch = useCallback((proposalId: string, decision: "MANUALLY_RECONCILED" | "REJECTED") => {
    setWorkspace((current) => {
      const proposal = current.matches.proposals.find((item) => item.proposal_id === proposalId);
      if (!proposal) return current;
      const existing = current.matches.items.find((item) => item.proposal_id === proposalId);
      const nextDecision: ReconciliationDecision = {
        decision_id: existing?.decision_id ?? `DEC-PREVIEW-${proposalId}`,
        batch_id: current.batch.batch_id,
        transaction_id: proposal.transaction_id,
        decision,
        confidence: proposal.confidence,
        decision_source: "human",
        policy_version: existing?.policy_version ?? "CC-R2.4",
        proposal_id: proposalId,
        committed_at: decision === "MANUALLY_RECONCILED" ? new Date().toISOString() : null,
        idempotency_key: decision === "MANUALLY_RECONCILED" ? `preview:${proposalId}` : null,
      };
      return {
        ...current,
        matches: {
          ...current.matches,
          proposals: current.matches.proposals.map((item) =>
            item.proposal_id === proposalId
              ? { ...item, status: decision === "MANUALLY_RECONCILED" ? "COMMITTED" : "REJECTED" }
              : item,
          ),
          items: existing
            ? current.matches.items.map((item) => (item.proposal_id === proposalId ? nextDecision : item))
            : [...current.matches.items, nextDecision],
        },
      };
    });
  }, []);

  const approveProposal = useCallback(
    async (proposal: MatchProposal, note: string) => {
      setBusy(`approve:${proposal.proposal_id}`);
      try {
        if (source === "live") {
          await client.approveMatch(proposal.proposal_id, {
            expected_revision: proposal.revision ?? 1,
            idempotency_key: `${workspace.batch.batch_id}:${proposal.proposal_id}:human:${crypto.randomUUID()}`,
            approval_note: note || "Reviewed against source evidence in CashClose UI.",
          });
          await loadBatch(workspace.batch.batch_id, true);
          notify("Match approved through the idempotent review tool");
        } else {
          updatePreviewMatch(proposal.proposal_id, "MANUALLY_RECONCILED");
          notify("Preview match approved in this session. Run a live demo for an audited write.", "info");
        }
        setSelectedProposalId(null);
      } catch (error) {
        notify(errorMessage(error), "error");
      } finally {
        setBusy(null);
      }
    },
    [client, loadBatch, notify, source, updatePreviewMatch, workspace.batch.batch_id],
  );

  const rejectProposal = useCallback(
    async (proposal: MatchProposal, reason: string) => {
      setBusy(`reject:${proposal.proposal_id}`);
      try {
        if (source === "live") {
          await client.rejectMatch(proposal.proposal_id, {
            expected_revision: proposal.revision ?? 1,
            rejection_reason: reason || "Rejected after evidence review.",
          });
          await loadBatch(workspace.batch.batch_id, true);
          notify("Match rejected and exception trail updated");
        } else {
          updatePreviewMatch(proposal.proposal_id, "REJECTED");
          notify("Preview match rejected in this session", "info");
        }
        setSelectedProposalId(null);
      } catch (error) {
        notify(errorMessage(error), "error");
      } finally {
        setBusy(null);
      }
    },
    [client, loadBatch, notify, source, updatePreviewMatch, workspace.batch.batch_id],
  );

  const editProposal = useCallback(
    async (proposal: MatchProposal, allocations: Allocation[], reason: string) => {
      setBusy(`edit:${proposal.proposal_id}`);
      try {
        if (source === "live") {
          await client.editMatch(proposal.proposal_id, {
            expected_revision: proposal.revision ?? 1,
            allocations,
            permitted_deduction: proposal.permitted_deduction ?? money("0"),
            edit_reason: reason || "Allocation edited during controller review.",
          });
          await loadBatch(workspace.batch.batch_id, true);
          notify("Allocation revised and re-verified by deterministic policy");
        } else {
          setWorkspace((current) => ({
            ...current,
            matches: {
              ...current.matches,
              proposals: current.matches.proposals.map((item) =>
                item.proposal_id === proposal.proposal_id
                  ? {
                      ...item,
                      allocations,
                      total_allocated: sumAllocations(allocations) as MatchProposal["total_allocated"],
                      revision: (item.revision ?? 1) + 1,
                    }
                  : item,
              ),
            },
          }));
          notify("Preview allocation revised with exact decimal arithmetic", "info");
        }
      } catch (error) {
        notify(errorMessage(error), "error");
      } finally {
        setBusy(null);
      }
    },
    [client, loadBatch, notify, source, workspace.batch.batch_id],
  );

  const resolveException = useCallback(
    async (item: ExceptionRecord, resolution: string) => {
      setBusy(`resolve:${item.exception_id}`);
      try {
        if (source === "live") {
          await client.resolveException(item.exception_id, { resolution });
          await loadBatch(workspace.batch.batch_id, true);
          notify("Exception resolved and recorded in the audit trail");
        } else {
          setWorkspace((current) => ({
            ...current,
            exceptions: current.exceptions.map((candidate) =>
              candidate.exception_id === item.exception_id
                ? { ...candidate, status: "RESOLVED", resolution, resolved_at: new Date().toISOString() }
                : candidate,
            ),
          }));
          notify("Preview exception resolved in this session", "info");
        }
      } catch (error) {
        notify(errorMessage(error), "error");
      } finally {
        setBusy(null);
      }
    },
    [client, loadBatch, notify, source, workspace.batch.batch_id],
  );

  const requestExceptionReview = useCallback(
    async (item: ExceptionRecord) => {
      setBusy(`review:${item.exception_id}`);
      try {
        if (source === "live") {
          await client.requestExceptionReview(item.exception_id);
          await loadBatch(workspace.batch.batch_id, true);
          notify("Exception assigned to human review");
        } else {
          setWorkspace((current) => ({
            ...current,
            exceptions: current.exceptions.map((candidate) =>
              candidate.exception_id === item.exception_id ? { ...candidate, status: "IN_REVIEW" } : candidate,
            ),
          }));
          notify("Preview exception moved to human review", "info");
        }
      } catch (error) {
        notify(errorMessage(error), "error");
      } finally {
        setBusy(null);
      }
    },
    [client, loadBatch, notify, source, workspace.batch.batch_id],
  );

  const runScenario = useCallback(
    async (request: ScenarioRequest) => {
      setBusy("scenario");
      try {
        const forecast =
          source === "live"
            ? await client.runScenario(workspace.batch.batch_id, request)
            : applyPreviewScenario(DEMO_FORECAST, request);
        setWorkspace((current) => ({ ...current, forecast }));
        setScenarioOpen(false);
        notify(source === "live" ? "Scenario recalculated by the forecast engine" : "Preview scenario calculated deterministically", source === "live" ? "success" : "info");
      } catch (error) {
        notify(errorMessage(error), "error");
      } finally {
        setBusy(null);
      }
    },
    [client, notify, source, workspace.batch.batch_id],
  );

  const resetScenario = useCallback(async () => {
    setBusy("scenario-reset");
    try {
      const forecast = source === "live" ? await client.getForecast(workspace.batch.batch_id) : DEMO_FORECAST;
      setWorkspace((current) => ({ ...current, forecast }));
      notify("Base forecast restored");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(null);
    }
  }, [client, notify, source, workspace.batch.batch_id]);

  const downloadAudit = useCallback(async () => {
    setBusy("download-audit");
    try {
      if (source === "live") {
        const result = await client.downloadAudit(workspace.batch.batch_id);
        downloadBlob(result.blob, result.filename);
      } else {
        downloadBlob(
          new Blob([JSON.stringify(workspace.audit, null, 2)], { type: "application/json" }),
          `cashclose-audit-${workspace.batch.batch_id}.json`,
        );
      }
      notify("Audit report downloaded");
    } catch (error) {
      notify(errorMessage(error), "error");
    } finally {
      setBusy(null);
    }
  }, [client, notify, source, workspace.audit, workspace.batch.batch_id]);

  const switchView = (next: View) => {
    setView(next);
    setMobileNav(false);
  };

  const openExceptions = workspace.exceptions.filter((item) => item.status !== "RESOLVED").length;
  const selectedProposal = workspace.matches.proposals.find((item) => item.proposal_id === selectedProposalId) ?? null;
  const selectedException = workspace.exceptions.find((item) => item.exception_id === selectedExceptionId) ?? null;

  return (
    <main className="cashclose-shell">
      <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`}>
        <button className="brand" onClick={() => switchView("overview")} aria-label="Open controller overview">
          <span className="brand-mark">C</span>
          <span className="brand-copy"><strong>cashclose</strong><small>AI controller</small></span>
        </button>
        <button className="icon-button sidebar-close" onClick={() => setMobileNav(false)} aria-label="Close navigation"><X size={20}/></button>

        <button className="new-batch-button" onClick={() => setNewBatchOpen(true)}>
          <Plus size={17}/><span>New close batch</span>
        </button>

        <nav className="primary-nav" aria-label="CashClose sections">
          <p>Workspace</p>
          {NAVIGATION.map(({ id, label, icon: Icon }) => (
            <button key={id} className={view === id ? "active" : ""} onClick={() => switchView(id)}>
              <Icon size={18}/><span>{label}</span>
              {id === "exceptions" && openExceptions > 0 ? <em>{openExceptions}</em> : null}
            </button>
          ))}
        </nav>

        <section className="sidebar-batch" aria-label="Active batch">
          <div><span className={`status-dot ${workspace.batch.status === "COMPLETED" ? "success" : "working"}`}/><small>ACTIVE BATCH</small></div>
          <strong>{workspace.batch.batch_id}</strong>
          <span>{workspace.batch.status.replaceAll("_", " ")}</span>
          <div className="progress-track"><i style={{ width: workspace.batch.terminal ? "100%" : "46%" }}/></div>
          <p>{workspace.metrics.records_processed} records · {formatDuration(workspace.metrics.processing_time_ms)}</p>
        </section>

        <button className="sidebar-org" onClick={() => setConnectionOpen(true)}>
          <span className="org-avatar">NL</span>
          <span><strong>Northstar Labs</strong><small>Asia/Kolkata · {workspace.metrics.matching.currency}</small></span>
          <ChevronRight size={16}/>
        </button>
      </aside>

      {mobileNav ? <button className="mobile-scrim" onClick={() => setMobileNav(false)} aria-label="Close navigation overlay"/> : null}

      <section className="workspace">
        <header className="workspace-header">
          <button className="icon-button mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open navigation"><Menu size={22}/></button>
          <div className="workspace-title">
            <p className="eyebrow">{PAGE_META[view].eyebrow}</p>
            <h1>{PAGE_META[view].title}</h1>
            <p>{PAGE_META[view].subtitle}</p>
          </div>
          <div className="header-actions">
            <button className={`connection-pill ${connection}`} onClick={() => setConnectionOpen(true)}>
              <span/><span>{connection === "checking" ? "Checking API" : connection === "online" ? "API online" : "Preview mode"}</span>
            </button>
            <button className="button secondary header-refresh" onClick={() => source === "live" ? void loadBatch(workspace.batch.batch_id) : setConnectionOpen(true)} disabled={busy === "refresh"}>
              <RefreshCw size={16} className={busy === "refresh" ? "spin" : ""}/><span>Refresh</span>
            </button>
            <button className="button primary" aria-label="Run controller" onClick={() => setNewBatchOpen(true)}>
              <Play size={16} fill="currentColor"/><span>Run controller</span>
            </button>
          </div>
        </header>

        <div className="source-banner" role="status">
          <div>
            {source === "live" ? <Database size={16}/> : <Sparkles size={16}/>} 
            <span><strong>{source === "live" ? "Live Docker workspace" : "Interactive truth-set preview"}</strong> — {source === "live" ? "all values are loaded from the FastAPI controller" : "explore every workflow, then run the controller to create audited backend records"}.</span>
          </div>
          {source === "preview" ? <button onClick={() => setNewBatchOpen(true)}>Configure live run <ArrowRight size={15}/></button> : <button onClick={() => switchView("trace")}>Open agent trace <ArrowRight size={15}/></button>}
        </div>

        {view === "overview" ? (
          <OverviewView
            workspace={workspace}
            onNavigate={switchView}
            onRun={() => setNewBatchOpen(true)}
            onArchitecture={() => setArchitectureOpen(true)}
            onOpenProposal={(id) => setSelectedProposalId(id)}
          />
        ) : null}
        {view === "trace" ? (
          <TraceView
            workspace={workspace}
            events={runState === "running" ? runEvents : workspace.events}
            source={source}
            runState={runState}
            orchestrationMode={orchestrationMode}
            onOpenProposal={setSelectedProposalId}
            onOpenException={setSelectedExceptionId}
          />
        ) : null}
        {view === "reconciliation" ? (
          <ReconciliationView
            workspace={workspace}
            onOpen={(id) => setSelectedProposalId(id)}
            notify={notify}
          />
        ) : null}
        {view === "exceptions" ? (
          <ExceptionsView
            exceptions={workspace.exceptions}
            onSelect={setSelectedExceptionId}
            onResolve={resolveException}
            onReview={requestExceptionReview}
            busy={busy}
          />
        ) : null}
        {view === "forecast" ? (
          <ForecastView
            forecast={workspace.forecast}
            onScenario={() => setScenarioOpen(true)}
            onReset={() => void resetScenario()}
            busy={busy}
            notify={notify}
          />
        ) : null}
        {view === "audit" ? (
          <AuditView
            workspace={workspace}
            onDownload={() => void downloadAudit()}
            busy={busy}
            notify={notify}
          />
        ) : null}
      </section>

      {newBatchOpen ? (
        <NewBatchDialog
          connection={connection}
          capabilities={capabilities}
          busy={busy === "run"}
          onClose={() => setNewBatchOpen(false)}
          onDemo={(mode) => void runDemo(mode)}
          onUpload={(files, mode) => void runUploadedBatch(files, mode)}
          notify={notify}
        />
      ) : null}
      {runOpen ? (
        <RunPanel
          state={runState}
          error={runError}
          events={runEvents.length ? runEvents : workspace.events}
          batch={workspace.batch}
          orchestrationMode={orchestrationMode}
          processingTimeMs={workspace.metrics.processing_time_ms}
          onClose={() => setRunOpen(false)}
          onTrace={() => { setRunOpen(false); switchView("trace"); }}
          onResults={() => { setRunOpen(false); switchView("overview"); }}
        />
      ) : null}
      {selectedProposal ? (
        <MatchPanel
          proposal={selectedProposal}
          decision={proposalDecision(workspace.matches, selectedProposal)}
          source={source}
          busy={busy}
          onClose={() => setSelectedProposalId(null)}
          onApprove={approveProposal}
          onReject={rejectProposal}
          onEdit={editProposal}
        />
      ) : null}
      {selectedException ? (
        <ExceptionQuickPanel
          item={selectedException}
          busy={busy}
          onClose={() => setSelectedExceptionId(null)}
          onResolve={resolveException}
          onReview={requestExceptionReview}
        />
      ) : null}
      {scenarioOpen ? (
        <ScenarioPanel
          currency={workspace.forecast.currency}
          busy={busy === "scenario"}
          onClose={() => setScenarioOpen(false)}
          onRun={runScenario}
        />
      ) : null}
      {connectionOpen ? (
        <ConnectionDialog
          connection={connection}
          source={source}
          apiUrl={resolveCashCloseApiBaseUrl()}
          batchId={workspace.batch.batch_id}
          onClose={() => setConnectionOpen(false)}
          onRun={() => { setConnectionOpen(false); setNewBatchOpen(true); }}
          onPreview={() => {
            setWorkspace(INITIAL_WORKSPACE);
            setSource("preview");
            window.localStorage.removeItem("cashclose.activeBatchId");
            setConnectionOpen(false);
            notify("Switched to the isolated interactive preview", "info");
          }}
        />
      ) : null}
      {architectureOpen ? <ArchitectureDialog onClose={() => setArchitectureOpen(false)}/> : null}
      {toast ? <Toast item={toast} onClose={() => setToast(null)}/> : null}
    </main>
  );
}

function OverviewView({
  workspace,
  onNavigate,
  onRun,
  onArchitecture,
  onOpenProposal,
}: {
  workspace: WorkspaceData;
  onNavigate: (view: View) => void;
  onRun: () => void;
  onArchitecture: () => void;
  onOpenProposal: (id: string) => void;
}) {
  const { metrics, forecast, matches, events } = workspace;
  const featured = matches.proposals.find((proposal) => proposal.allocations.length > 1) ?? matches.proposals[0];
  const openReviews = matches.proposals.filter((proposal) =>
    (proposalDecision(matches, proposal)?.decision ?? proposal.status) === "NEEDS_REVIEW",
  ).length;
  return (
    <div className="page-stack">
      <section className="metric-grid" aria-label="Batch metrics">
        <MetricCard label="Verified value reconciled" value={compactMoney(metrics.matching.value_reconciled, metrics.matching.currency)} note="Committed matches only" icon={CircleDollarSign} tone="hero"/>
        <MetricCard label="Automation coverage" value={confidencePercent(metrics.matching.automation_coverage)} note={`${metrics.records_processed} records processed`} icon={Zap}/>
        <MetricCard label="Match precision" value={confidencePercent(metrics.matching.precision)} note={`${confidencePercent(metrics.matching.false_approval_rate)} false approvals`} icon={BadgeCheck}/>
        <MetricCard label="Unresolved value" value={compactMoney(metrics.matching.unresolved_value, metrics.matching.currency)} note={`${workspace.exceptions.filter((item) => item.status !== "RESOLVED").length} exceptions open`} icon={AlertTriangle} tone="warning"/>
        <MetricCard label="30-day cash minimum" value={compactMoney(metrics.forecast_cash_minimum, metrics.matching.currency)} note={formatDate(metrics.forecast_cash_minimum_date)} icon={ArrowDownRight} tone={moneyToMinorUnits(metrics.forecast_cash_minimum) < BigInt(0) ? "danger" : "default"}/>
      </section>

      <section className="overview-grid">
        <article className="panel forecast-overview">
          <PanelHeader eyebrow="VERIFIED CASH OUTLOOK" title="Cash forecast" action={<button className="text-button" onClick={() => onNavigate("forecast")}>Open forecast <ArrowRight size={15}/></button>}/>
          <ChartLegend forecast={forecast}/>
          <CashChart forecast={forecast} compact/>
          <footer className="forecast-callout">
            <span className={forecast.shortfall_date ? "alert-icon" : "safe-icon"}>{forecast.shortfall_date ? <AlertTriangle size={18}/> : <Check size={18}/>}</span>
            <div><strong>{forecast.shortfall_date ? `Shortfall begins ${formatDate(forecast.shortfall_date)}` : "No shortfall in the forecast window"}</strong><p>Expected low: {formatMoney(forecast.minimum_expected_cash, forecast.currency)} on {formatDate(forecast.minimum_expected_cash_date)}.</p></div>
            <button className="button secondary" onClick={() => onNavigate("forecast")}>Stress test</button>
          </footer>
        </article>

        <article className="panel controller-brief">
          <PanelHeader eyebrow="CONTROLLER BRIEF" title="The close is explainable" action={<span className="verified-label"><ShieldCheck size={15}/> Policy enforced</span>}/>
          <p className="brief-lead">The controller reconciled high-confidence cash, escalated ambiguity, and recalculated the 30-day position using verified records only.</p>
          <div className="brief-facts">
            <div><CheckCircle2 size={18}/><span><strong>{matches.items.filter((item) => item.decision === "AUTO_RECONCILED").length} automatic decisions</strong><small>Passed verification before commit</small></span></div>
            <div><UserCheck size={18}/><span><strong>{openReviews} proposals need review</strong><small>Evidence is ready in the workspace</small></span></div>
            <div><AlertTriangle size={18}/><span><strong>{workspace.exceptions.filter((item) => item.status !== "RESOLVED").length} explicit exceptions</strong><small>No unresolved record was hidden</small></span></div>
          </div>
          <button className="button primary full" onClick={onRun}><Play size={16}/> Run another close</button>
          <button className="text-button centered" onClick={onArchitecture}>See controller architecture <ArrowRight size={15}/></button>
        </article>
      </section>

      <section className="lower-grid">
        <article className="panel featured-allocation">
          <PanelHeader eyebrow="FEATURED ALLOCATION" title="One receipt. Two invoices. Zero guesswork." action={featured ? <button className="text-button" onClick={() => onOpenProposal(featured.proposal_id)}>Inspect evidence <ArrowRight size={15}/></button> : null}/>
          {featured ? (
            <>
              <div className="allocation-visual">
                <div className="bank-node"><Banknote size={20}/><span><small>{featured.transaction_id}</small><strong>{formatMoney(featured.transaction_amount, featured.currency)}</strong><em>Bank receipt</em></span></div>
                <div className="allocation-connector"><span>OR-TOOLS</span><i/></div>
                <div className="invoice-nodes">
                  {featured.allocations.map((allocation) => <div key={allocation.invoice_id}><FileCheck2 size={18}/><span><small>{allocation.invoice_id}</small><strong>{formatMoney(allocation.amount, allocation.currency)}</strong></span><CheckCircle2 size={17}/></div>)}
                </div>
              </div>
              <div className="evidence-strip">
                {featured.evidence.slice(0, 3).map((item) => <span key={item.evidence_id}><Check size={14}/>{item.summary}</span>)}
              </div>
            </>
          ) : <EmptyState title="No match proposals yet" detail="Run a close batch to generate deterministic candidate allocations." action={<button className="button primary" onClick={onRun}>Run controller</button>}/>} 
        </article>

        <article className="panel activity-panel">
          <PanelHeader eyebrow="AGENT TRACE" title="What the controller did" action={<button className="text-button" onClick={() => onNavigate("audit")}>Full audit <ArrowRight size={15}/></button>}/>
          <div className="compact-timeline">
            {events.slice(-5).map((event) => <div key={event.sequence}><span className={`event-marker ${event.status}`}>{event.status === "warning" ? <AlertTriangle size={13}/> : <Check size={13}/>}</span><span><strong>{event.message}</strong><small><code>{event.tool_name ?? event.event_type}</code> · {formatDuration(event.latency_ms)}</small></span><time>{new Date(event.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })}</time></div>)}
          </div>
        </article>
      </section>
    </div>
  );
}

function MetricCard({ label, value, note, icon: Icon, tone = "default" }: { label: string; value: string; note: string; icon: LucideIcon; tone?: "default" | "hero" | "warning" | "danger" }) {
  return <article className={`metric-card ${tone}`}><div><span>{label}</span><Icon size={18}/></div><strong>{value}</strong><p>{note}</p></article>;
}

function PanelHeader({ eyebrow, title, action }: { eyebrow: string; title: string; action?: ReactNode }) {
  return <header className="panel-header"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2></div>{action}</header>;
}

function ChartLegend({ forecast }: { forecast: RunCashForecastResult }) {
  return <div className="chart-legend"><span><i className="confirmed"/>Confirmed</span><span><i className="expected"/>Expected</span><span><i className="risk"/>Risk-adjusted</span>{forecast.positions.some((item) => item.p10 && item.p90) ? <span><i className="range"/>P10–P90</span> : null}</div>;
}

function CashChart({ forecast, compact = false }: { forecast: RunCashForecastResult; compact?: boolean }) {
  const data = useMemo(() => forecast.positions.map((position) => ({
    date: position.date,
    label: formatDate(position.date),
    confirmed: Number(position.confirmed) / 1000,
    expected: Number(position.expected) / 1000,
    risk: Number(position.risk_adjusted) / 1000,
    p10: position.p10 ? Number(position.p10) / 1000 : undefined,
    p90: position.p90 ? Number(position.p90) / 1000 : undefined,
  })), [forecast]);
  const values = data.flatMap((row) => [row.confirmed, row.expected, row.risk, row.p10, row.p90].filter((value): value is number => value !== undefined));
  const min = Math.min(0, ...values);
  const max = Math.max(1, ...values);
  const padding = Math.max((max - min) * 0.12, 10);
  return <div className={`cash-chart ${compact ? "compact" : ""}`} aria-label="Thirty day cash position chart">
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 16, right: 14, bottom: 0, left: compact ? -14 : 0 }}>
        <defs>
          <linearGradient id={`range-${compact}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#5f8cff" stopOpacity=".18"/><stop offset="100%" stopColor="#5f8cff" stopOpacity=".02"/></linearGradient>
          <linearGradient id={`risk-${compact}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#e38343" stopOpacity=".18"/><stop offset="100%" stopColor="#e38343" stopOpacity="0"/></linearGradient>
        </defs>
        <CartesianGrid vertical={false} stroke="#dfe5df" strokeDasharray="3 5"/>
        <XAxis dataKey="label" axisLine={false} tickLine={false} minTickGap={compact ? 52 : 32} tick={{ fontSize: 12, fill: "#738078" }}/>
        <YAxis domain={[min - padding, max + padding]} axisLine={false} tickLine={false} width={58} tickFormatter={(value) => `${forecast.currency === "USD" ? "$" : "₹"}${Math.round(Number(value))}k`} tick={{ fontSize: 12, fill: "#738078" }}/>
        <Tooltip content={<ForecastTooltip currency={forecast.currency}/>}/>
        <Area type="monotone" dataKey="p90" stroke="none" fill={`url(#range-${compact})`} connectNulls/>
        <Area type="monotone" dataKey="risk" stroke="none" fill={`url(#risk-${compact})`}/>
        <ReferenceLine y={0} stroke="#cf684f" strokeDasharray="5 5"/>
        <Line type="monotone" dataKey="confirmed" name="Confirmed" stroke="#18392f" strokeWidth={2.5} dot={false} activeDot={{ r: 5 }}/>
        <Line type="monotone" dataKey="expected" name="Expected" stroke="#5f8cff" strokeWidth={2.3} strokeDasharray="6 4" dot={false}/>
        <Line type="monotone" dataKey="risk" name="Risk-adjusted" stroke="#e38343" strokeWidth={2.5} dot={false}/>
      </ComposedChart>
    </ResponsiveContainer>
  </div>;
}

function ForecastTooltip({ active, payload, label, currency }: { active?: boolean; payload?: Array<{ name?: string; value?: number; color?: string }>; label?: string; currency: string }) {
  if (!active || !payload?.length) return null;
  return <div className="chart-tooltip"><strong>{label}</strong>{payload.filter((item) => item.name && !item.name.startsWith("p")).map((item) => <span key={item.name}><i style={{ background: item.color }}/>{item.name}<b>{currency === "USD" ? "$" : "₹"}{Number(item.value).toFixed(1)}k</b></span>)}</div>;
}

function TraceView({
  workspace,
  events,
  source,
  runState,
  orchestrationMode,
  onOpenProposal,
  onOpenException,
}: {
  workspace: WorkspaceData;
  events: AgentEvent[];
  source: Source;
  runState: "idle" | "running" | "complete" | "failed";
  orchestrationMode: string;
  onOpenProposal: (id: string) => void;
  onOpenException: (id: string) => void;
}) {
  const records = workspace.records.filter((item) => item.record.record_type === "bank_transaction");
  const [search, setSearch] = useState("");
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(records[0]?.record.record_id ?? null);
  const [selectedStage, setSelectedStage] = useState<TraceStageId>("observe");
  const filtered = records.filter((item) =>
    `${item.record.record_id} ${item.record.counterparty} ${item.record.reference} ${item.record.status}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const selected = filtered.find((item) => item.record.record_id === selectedRecordId) ?? filtered[0] ?? (search ? null : records[0]) ?? null;

  const recordEvents = selected
    ? source === "preview"
      ? previewEventsForRecord(selected)
      : eventsForRecord(events, selected)
    : [];
  const stageStates = TRACE_STAGES.map((stage) => {
    const stageEvents = recordEvents.filter((event) => traceStageForEvent(event) === stage.id);
    const latest = stageEvents.at(-1);
    const inferredComplete = selected ? traceStageHasStructuredResult(stage.id, selected) : false;
    const status = latest?.status === "failed"
      ? "failed"
      : latest?.status === "warning"
        ? "warning"
        : latest?.status === "started"
          ? "active"
          : latest || inferredComplete
            ? "complete"
            : "pending";
    return {
      ...stage,
      events: stageEvents,
      latest,
      status,
      latency: stageEvents.reduce((total, event) => total + event.latency_ms, 0),
    };
  });
  const activeStage = stageStates.find((stage) => stage.id === selectedStage) ?? stageStates[0];
  const globalFeed = [...events].sort((a, b) => b.sequence - a.sequence).slice(0, 10);
  const agentic = orchestrationMode.startsWith("responses");
  const responsesPending = orchestrationMode === "responses-requested";

  return <div className="page-stack trace-page">
    <section className="trace-context panel">
      <div className={`execution-badge ${agentic ? "agentic" : "deterministic"}`}>
        {agentic ? <Sparkles size={17}/> : <Database size={17}/>}
        <span><strong>{agentic ? `OpenAI Responses${responsesPending ? " requested" : ""}` : "Deterministic controller"}</strong><small>{agentic ? responsesPending ? "Request accepted by the UI; recorded tool events remain the execution proof" : "Planner selected tools; deterministic code executed finance actions" : "Rule-driven orchestration; no model calls claimed"}</small></span>
      </div>
      <div className="trace-boundary"><LockKeyhole size={17}/><span><strong>Operational trace only</strong><small>Tool inputs, evidence references, outcomes, and measured latency—never hidden reasoning.</small></span></div>
      {runState === "running" ? <span className="live-chip" role="status"><Loader2 className="spin" size={14}/> Live</span> : <span className="trace-source-chip">{source === "live" ? "Backend record" : "Preview fixture"}</span>}
    </section>

    <section className="trace-workspace panel">
      <aside className="trace-records" aria-label="Transaction selector">
        <header><div><p className="eyebrow">TRANSACTIONS</p><strong>{records.length} bank records</strong></div><span>{filtered.length}</span></header>
        <label className="search-field trace-search"><Search size={16}/><span className="sr-only">Search transactions</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="ID, counterparty, reference…"/></label>
        <div className="trace-record-list">
          {filtered.map((item) => <button
            key={item.record.record_id}
            className={selected?.record.record_id === item.record.record_id ? "active" : ""}
            onClick={() => { setSelectedRecordId(item.record.record_id); setSelectedStage("observe"); }}
            aria-current={selected?.record.record_id === item.record.record_id ? "true" : undefined}
          >
            <span className={`record-state ${traceRecordTone(item)}`}>{traceRecordTone(item) === "safe" ? <Check size={13}/> : traceRecordTone(item) === "risk" ? <AlertTriangle size={13}/> : <Clock3 size={13}/>}</span>
            <span><strong>{item.record.counterparty}</strong><code>{item.record.record_id}</code><small>{item.record.reference}</small></span>
            <span><strong>{compactMoney(item.record.amount, item.record.currency)}</strong><small>{titleCase(item.record.status)}</small></span>
          </button>)}
          {!filtered.length ? <EmptyState title="No transactions found" detail="Try another ID, counterparty, or reference."/> : null}
        </div>
      </aside>

      {selected && activeStage ? <div className="trace-detail">
        <header className="trace-record-header">
          <span className="trace-record-icon"><Banknote size={22}/></span>
          <div><p className="eyebrow">{selected.record.record_id}</p><h2>{selected.record.counterparty}</h2><code>{selected.record.reference}</code></div>
          <div className="trace-record-amount"><strong>{formatMoney(selected.record.amount, selected.record.currency)}</strong><span>{formatDate(selected.record.effective_date, { year: "numeric" })}</span></div>
          <div className="trace-record-result"><DecisionPill status={selected.decision?.decision ?? selected.exception?.status ?? selected.record.status}/>{selected.proposal ? <span>Confidence <strong>{confidencePercent(selected.proposal.confidence)}</strong></span> : <span>Confidence <strong>Abstained</strong></span>}</div>
        </header>

        <nav className="trace-pipeline" aria-label="Transaction processing stages">
          {stageStates.map((stage, index) => <button key={stage.id} className={`${stage.status} ${selectedStage === stage.id ? "selected" : ""}`} onClick={() => setSelectedStage(stage.id)} aria-current={selectedStage === stage.id ? "step" : undefined}>
            <span className="stage-index">{stage.status === "complete" ? <Check size={14}/> : stage.status === "warning" || stage.status === "failed" ? <AlertTriangle size={14}/> : stage.status === "active" ? <Loader2 className="spin" size={14}/> : index + 1}</span>
            <span><strong>{stage.label}</strong><small>{titleCase(stage.status)}</small></span>
            {index < stageStates.length - 1 ? <i/> : null}
          </button>)}
        </nav>

        {source === "preview" ? <p className="fixture-disclosure"><Sparkles size={15}/><span><strong>Preview evidence reconstruction.</strong> These stages are derived from the stored demo fixture; they are not replayed backend or model calls.</span></p> : null}

        <div className="trace-inspection-grid">
          <article className="trace-stage-card">
            <header><div><p className="eyebrow">SELECTED CONTROL GATE</p><h3>{activeStage.label}</h3></div><span className={`stage-status ${activeStage.status}`}>{activeStage.status === "complete" ? <CheckCircle2 size={14}/> : activeStage.status === "warning" || activeStage.status === "failed" ? <AlertTriangle size={14}/> : <Clock3 size={14}/>} {titleCase(activeStage.status)}</span></header>
            <dl className="trace-metadata">
              <div><dt>Agent owner</dt><dd>{titleCase(activeStage.latest?.agent_name ?? activeStage.agent)}</dd></div>
              <div><dt>Tool</dt><dd><code>{activeStage.latest?.tool_name ?? activeStage.defaultTool}</code></dd></div>
              <div><dt>Latency</dt><dd>{source === "preview" ? "Preview fixture" : activeStage.events.length ? formatDuration(activeStage.latency) : "Not recorded"}</dd></div>
              <div><dt>Result reference</dt><dd><code>{activeStage.latest?.tool_result_reference ?? "Awaiting result"}</code></dd></div>
            </dl>
            <div className="trace-stage-outcome"><strong>Outcome</strong><p>{traceStageSummary(activeStage.id, selected, activeStage.latest)}</p></div>
            <TraceStageEvidence stage={activeStage.id} detail={selected}/>
            {activeStage.events.length ? <div className="stage-event-log"><strong>Recorded actions</strong>{activeStage.events.map((event) => <div key={event.sequence}><span className={`event-marker ${event.status}`}>{event.status === "failed" || event.status === "warning" ? <AlertTriangle size={12}/> : <Check size={12}/>}</span><p>{event.message}<small><code>{event.input_reference ?? `event:${event.sequence}`}</code></small></p><time>{source === "preview" ? "fixture" : formatDuration(event.latency_ms)}</time></div>)}</div> : null}
          </article>

          <aside className="trace-live-feed" aria-live="polite">
            <header><div><p className="eyebrow">{runState === "running" ? "LIVE FEED" : "BATCH FEED"}</p><h3>{runState === "running" ? "Controller actions" : "Latest recorded actions"}</h3></div>{runState === "running" ? <Loader2 className="spin" size={17}/> : <Activity size={17}/>}</header>
            <div>{globalFeed.map((event) => <article key={event.sequence} className={event.status}>
              <span className="feed-sequence">{event.sequence}</span>
              <p><strong>{event.message}</strong><small>{titleCase(event.agent_name)} · <code>{event.tool_name ?? event.event_type}</code></small><em>{event.tool_result_reference ?? event.input_reference ?? `event:${event.sequence}`}</em></p>
              <time>{source === "preview" ? "fixture" : `${formatDuration(event.latency_ms)} · ${new Date(event.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" })}`}</time>
            </article>)}</div>
            {!globalFeed.length ? <EmptyState title="Waiting for the first action" detail="Validated tool events will appear here as the controller runs."/> : null}
          </aside>
        </div>

        <footer className="trace-actions">
          <span><ShieldCheck size={16}/> Structured records remain the source of truth.</span>
          {selected.proposal ? <button className="button secondary" onClick={() => onOpenProposal(selected.proposal!.proposal_id)}>Open match evidence <ArrowRight size={15}/></button> : null}
          {selected.exception ? <button className="button secondary" onClick={() => onOpenException(selected.exception!.exception_id)}>Open exception <ArrowRight size={15}/></button> : null}
        </footer>
      </div> : <EmptyState title="No transaction trace available" detail="Run a batch or choose a transaction with recorded processing data."/>}
    </section>
  </div>;
}

function traceRecordTone(detail: RecordDetailView): "safe" | "risk" | "review" {
  if (detail.exception || detail.record.status === "UNRESOLVED" || detail.record.status === "REJECTED") return "risk";
  if (detail.record.status === "AUTO_RECONCILED" || detail.record.status === "MANUALLY_RECONCILED") return "safe";
  return "review";
}

function traceStageHasStructuredResult(stage: TraceStageId, detail: RecordDetailView): boolean {
  if (stage === "observe") return true;
  if (stage === "normalize") return detail.record.status !== "UNPROCESSED";
  if (stage === "candidates") return Boolean(detail.candidates?.length || detail.proposal || detail.exception);
  if (stage === "allocation") return Boolean(detail.proposal || detail.exception);
  if (stage === "evidence") return Boolean(detail.evidence?.evidence.length || detail.proposal?.evidence.length || detail.exception?.evidence.length);
  if (stage === "verify") return Boolean(detail.verification || detail.exception);
  return Boolean(detail.decision || detail.exception);
}

function traceStageSummary(stage: TraceStageId, detail: RecordDetailView, event?: AgentEvent): string {
  if (event?.message) return event.message;
  if (stage === "observe") return `${detail.record.record_id} was read as a ${titleCase(detail.record.record_type)} for ${formatMoney(detail.record.amount, detail.record.currency)}.`;
  if (stage === "normalize") return `The persisted counterparty and reference are ${detail.record.counterparty} and ${detail.record.reference}.`;
  if (stage === "candidates") return `${detail.candidates?.length ?? 0} invoice candidates remain after currency, status, date, and identity constraints.`;
  if (stage === "allocation") return detail.proposal ? `${detail.proposal.allocations.length} allocation${detail.proposal.allocations.length === 1 ? "" : "s"} total ${formatMoney(detail.proposal.total_allocated, detail.proposal.currency)}.` : "No allocation was safe enough to propose.";
  if (stage === "evidence") return `${detail.evidence?.evidence.length ?? detail.proposal?.evidence.length ?? detail.exception?.evidence.length ?? 0} evidence items are attached to the record.`;
  if (stage === "verify") return detail.verification ? `${detail.verification.approved ? "Approved" : "Not approved"} under ${detail.verification.policy_version}; the automatic threshold is ${confidencePercent(detail.verification.confidence_threshold)}.` : "No automatic approval was issued.";
  if (detail.exception) return `${titleCase(detail.exception.reason_code)} was recorded. Next action: ${detail.exception.next_action}`;
  return `${titleCase(detail.decision?.decision ?? detail.record.status)} was persisted through the controlled write boundary.`;
}

function TraceStageEvidence({ stage, detail }: { stage: TraceStageId; detail: RecordDetailView }) {
  if (stage === "candidates") return <div className="trace-data-list"><strong>Candidate set</strong>{detail.candidates?.length ? detail.candidates.map((candidate) => <div key={candidate.invoice_id}><span><FileSpreadsheet size={15}/><strong>{candidate.invoice_number}</strong></span><span>{formatMoney(candidate.remaining_balance, candidate.currency)} · ref {confidencePercent(candidate.reference_similarity)}</span></div>) : <p>No eligible invoice candidates were persisted.</p>}</div>;
  if (stage === "allocation") return <div className="trace-data-list"><strong>Deterministic allocation</strong>{detail.proposal?.allocations.length ? detail.proposal.allocations.map((allocation) => <div key={allocation.invoice_id}><span><FileCheck2 size={15}/><strong>{allocation.invoice_id}</strong></span><span>{formatMoney(allocation.amount, allocation.currency)}</span></div>) : <p>The solver did not return a commit-eligible allocation.</p>}</div>;
  if (stage === "evidence") {
    const evidence = detail.evidence?.evidence ?? detail.proposal?.evidence ?? detail.exception?.evidence ?? [];
    return <div className="trace-data-list"><strong>Evidence ledger</strong>{evidence.length ? evidence.map((item) => <div key={item.evidence_id}><span><Link2 size={15}/><strong>{titleCase(item.evidence_type)}</strong></span><span>{item.summary}</span></div>) : <p>No evidence items were persisted.</p>}</div>;
  }
  if (stage === "verify") return <div className="trace-data-list"><strong>Policy result</strong><div><span><ShieldCheck size={15}/><strong>{detail.verification?.policy_version ?? "Verification gate"}</strong></span><span>{detail.verification?.approved ? "Approved for controlled commit" : "Automatic commit not authorized"}</span></div>{detail.verification?.reasons?.map((reason) => <p key={reason}>{reason}</p>)}</div>;
  if (stage === "outcome") return <div className="trace-data-list"><strong>Terminal treatment</strong><div><span>{detail.exception ? <AlertTriangle size={15}/> : <BadgeCheck size={15}/>}<strong>{titleCase(detail.exception?.reason_code ?? detail.decision?.decision ?? detail.record.status)}</strong></span><span>{detail.exception?.next_action ?? detail.decision?.policy_version ?? "Recorded state"}</span></div></div>;
  return null;
}

function ReconciliationView({ workspace, onOpen, notify }: { workspace: WorkspaceData; onOpen: (id: string) => void; notify: (message: string, kind?: ToastState["kind"]) => void }) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("ALL");
  const [page, setPage] = useState(1);
  const pageSize = 8;
  const rows = useMemo(() => workspace.matches.proposals.map((proposal) => {
    const review = workspace.matches.reviews?.find((item) => item.proposal.proposal_id === proposal.proposal_id);
    const meta = DEMO_MATCH_META[proposal.transaction_id];
    const decision = proposalDecision(workspace.matches, proposal);
    return {
      proposal,
      status: decision?.decision ?? proposal.status ?? "PROPOSED",
      counterparty: review?.transaction.counterparty ?? meta?.counterparty ?? "Counterparty pending",
      reference: review?.transaction.reference ?? meta?.reference ?? proposal.transaction_id,
      date: review?.transaction.effective_date ?? meta?.valueDate ?? workspace.batch.as_of_date,
    };
  }), [workspace]);
  const filtered = rows.filter((row) => {
    const query = `${row.proposal.transaction_id} ${row.counterparty} ${row.reference} ${row.proposal.allocations.map((item) => item.invoice_id).join(" ")}`.toLowerCase();
    return (filter === "ALL" || row.status === filter) && query.includes(search.toLowerCase());
  });
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const visible = filtered.slice((Math.min(page, pages) - 1) * pageSize, Math.min(page, pages) * pageSize);
  const exportCsv = () => {
    const lines = ["transaction_id,counterparty,amount,currency,allocation,status,confidence", ...filtered.map((row) => [row.proposal.transaction_id, JSON.stringify(row.counterparty), row.proposal.transaction_amount, row.proposal.currency, JSON.stringify(row.proposal.allocations.map((item) => `${item.invoice_id}:${item.amount}`).join(";")), row.status, row.proposal.confidence].join(","))];
    downloadBlob(new Blob([lines.join("\n")], { type: "text/csv" }), `cashclose-matches-${workspace.batch.batch_id}.csv`);
    notify("Reconciliation CSV exported");
  };
  return <div className="page-stack">
    <section className="summary-bar">
      <div><span>Automatic</span><strong>{workspace.matches.items.filter((item) => item.decision === "AUTO_RECONCILED").length}</strong></div>
      <div><span>Needs review</span><strong>{workspace.matches.proposals.filter((proposal) => (proposalDecision(workspace.matches, proposal)?.decision ?? proposal.status) === "NEEDS_REVIEW").length}</strong></div>
      <div><span>Value reconciled</span><strong>{compactMoney(workspace.metrics.matching.value_reconciled, workspace.metrics.matching.currency)}</strong></div>
      <div><span>Policy threshold</span><strong>≥ 95.0%</strong></div>
      <div className="summary-message"><ShieldCheck size={19}/><span><strong>Commit guard active</strong><small>Only verified proposals can write</small></span></div>
    </section>

    <section className="panel table-panel">
      <div className="table-toolbar">
        <div className="search-field"><Search size={17}/><input value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} placeholder="Search transaction, counterparty, invoice…" aria-label="Search reconciliation records"/></div>
        <div className="segmented" aria-label="Match status filter">
          {["ALL", "AUTO_RECONCILED", "NEEDS_REVIEW", "REJECTED"].map((item) => <button key={item} className={filter === item ? "active" : ""} onClick={() => { setFilter(item); setPage(1); }}>{item === "ALL" ? "All" : titleCase(item)}</button>)}
        </div>
        <button className="button secondary" onClick={exportCsv}><Download size={16}/> Export CSV</button>
      </div>
      <div className="data-table-wrap">
        <table className="data-table">
          <thead><tr><th>Bank transaction</th><th>Amount</th><th>Proposed allocation</th><th>Evidence</th><th>Confidence</th><th>Status</th><th><span className="sr-only">Open</span></th></tr></thead>
          <tbody>
            {visible.map(({ proposal, status, counterparty, reference, date }) => <tr key={proposal.proposal_id} onClick={() => onOpen(proposal.proposal_id)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") onOpen(proposal.proposal_id); }}>
              <td><div className="record-cell"><span className="record-icon"><Banknote size={18}/></span><span><strong>{counterparty}</strong><small>{proposal.transaction_id} · {formatDate(date)}</small><code>{reference}</code></span></div></td>
              <td><strong>{formatMoney(proposal.transaction_amount, proposal.currency)}</strong></td>
              <td><div className="allocation-cell">{proposal.allocations.map((allocation) => <span key={allocation.invoice_id}><strong>{allocation.invoice_id}</strong><small>{formatMoney(allocation.amount, allocation.currency)}</small></span>)}</div></td>
              <td><span className="evidence-count"><Link2 size={14}/>{proposal.evidence.length} checks</span></td>
              <td><strong className={Number(proposal.confidence) >= .95 ? "positive" : "caution"}>{confidencePercent(proposal.confidence)}</strong></td>
              <td><DecisionPill status={status}/></td>
              <td><button className="icon-button" onClick={(event) => { event.stopPropagation(); onOpen(proposal.proposal_id); }} aria-label={`Inspect ${proposal.transaction_id}`}><ChevronRight size={18}/></button></td>
            </tr>)}
          </tbody>
        </table>
      </div>
      {visible.length === 0 ? <EmptyState title="No reconciliation records found" detail="Change the search or status filter to see more records."/> : null}
      <footer className="table-footer"><span>Showing {visible.length} of {filtered.length} proposals</span><div><button className="button ghost" disabled={page <= 1} onClick={() => setPage((current) => current - 1)}>Previous</button><span>Page {Math.min(page, pages)} of {pages}</span><button className="button ghost" disabled={page >= pages} onClick={() => setPage((current) => current + 1)}>Next</button></div></footer>
    </section>
  </div>;
}

function DecisionPill({ status }: { status: string }) {
  const safe = status === "AUTO_RECONCILED" || status === "MANUALLY_RECONCILED" || status === "COMMITTED";
  const risk = status === "REJECTED" || status === "UNRESOLVED";
  return <span className={`decision-pill ${safe ? "safe" : risk ? "risk" : "review"}`}>{safe ? <Check size={13}/> : risk ? <XCircle size={13}/> : <Clock3 size={13}/>} {titleCase(status)}</span>;
}

function ExceptionsView({ exceptions, onSelect, onResolve, onReview, busy }: { exceptions: ExceptionRecord[]; onSelect: (id: string) => void; onResolve: (item: ExceptionRecord, resolution: string) => Promise<void>; onReview: (item: ExceptionRecord) => Promise<void>; busy: string | null }) {
  const [status, setStatus] = useState("OPEN");
  const [reason, setReason] = useState("ALL");
  const [search, setSearch] = useState("");
  const [detailId, setDetailId] = useState<string | null>(null);
  const active = exceptions.find((item) => item.exception_id === detailId) ?? exceptions.find((item) => item.status !== "RESOLVED") ?? exceptions[0];
  const reasons = Array.from(new Set(exceptions.map((item) => item.reason_code)));
  const filtered = exceptions.filter((item) => (status === "ALL" || item.status === status) && (reason === "ALL" || item.reason_code === reason) && `${item.exception_id} ${item.record_id} ${item.reason_code} ${item.counterparty ?? ""} ${item.reference ?? ""}`.toLowerCase().includes(search.toLowerCase()));
  return <div className="page-stack">
    <section className="exception-summary">
      <div className="exception-number"><AlertTriangle size={22}/><span><strong>{exceptions.filter((item) => item.status !== "RESOLVED").length}</strong><small>open exceptions</small></span></div>
      <div><strong>Abstention is a control, not a failure.</strong><p>Hard contradictions are blocked regardless of confidence. Every exception carries evidence and a recommended next action.</p></div>
      <div className="exception-legend"><span><i className="high"/>Hard risk</span><span><i className="medium"/>Evidence gap</span></div>
    </section>
    <section className="exception-workspace panel">
      <aside className="exception-inbox">
        <div className="exception-tools"><div className="search-field"><Search size={16}/><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search exceptions…" aria-label="Search exceptions"/></div><div className="select-row"><label>Status<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="ALL">All</option><option value="OPEN">Open</option><option value="IN_REVIEW">In review</option><option value="RESOLVED">Resolved</option></select></label><label>Reason<select value={reason} onChange={(event) => setReason(event.target.value)}><option value="ALL">All reasons</option>{reasons.map((item) => <option key={item} value={item}>{titleCase(item)}</option>)}</select></label></div></div>
        <div className="exception-list">
          {filtered.map((item) => {
            const meta = DEMO_EXCEPTION_META[item.exception_id];
            return <button key={item.exception_id} className={active?.exception_id === item.exception_id ? "active" : ""} onClick={() => {
              setDetailId(item.exception_id);
              if (window.matchMedia("(max-width: 820px)").matches) onSelect(item.exception_id);
            }}>
              <span className={`severity-mark ${meta?.severity ?? (item.reason_code === "CURRENCY_MISMATCH" ? "high" : "medium")}`}/>
              <span><span className="exception-topline"><em>{titleCase(item.reason_code)}</em><small>{formatDate(item.created_at)}</small></span><strong>{meta?.title ?? item.next_action}</strong><small>{item.counterparty ?? item.record_id} · {item.amount && item.currency ? formatMoney(item.amount, item.currency) : item.record_id}</small><DecisionPill status={item.status}/></span>
              <ChevronRight size={17}/>
            </button>;
          })}
          {!filtered.length ? <EmptyState title="Inbox is clear" detail="No exceptions match these filters."/> : null}
        </div>
      </aside>
      {active ? <ExceptionDetail key={active.exception_id} item={active} onResolve={onResolve} onReview={onReview} busy={busy}/> : <EmptyState title="Select an exception" detail="Choose a record to inspect its evidence and next action."/>}
    </section>
  </div>;
}

function ExceptionDetail({ item, onResolve, onReview, busy }: { item: ExceptionRecord; onResolve: (item: ExceptionRecord, resolution: string) => Promise<void>; onReview: (item: ExceptionRecord) => Promise<void>; busy: string | null }) {
  const meta = DEMO_EXCEPTION_META[item.exception_id];
  const [resolution, setResolution] = useState(item.resolution ?? "");
  return <article className="exception-detail">
    <header><div><p className="eyebrow">{item.exception_id} · {item.record_id}</p><h2>{meta?.title ?? titleCase(item.reason_code)}</h2><p>{meta?.explanation ?? `The controller stopped this record because ${titleCase(item.reason_code).toLowerCase()} requires human evidence.`}</p></div><DecisionPill status={item.status}/></header>
    <section className="record-summary-grid"><div><small>Counterparty</small><strong>{item.counterparty ?? meta?.counterparty ?? "Unresolved identity"}</strong></div><div><small>Amount</small><strong>{item.amount && item.currency ? formatMoney(item.amount, item.currency) : meta ? formatMoney(meta.amount, meta.currency) : "Not available"}</strong></div><div><small>Reason code</small><strong>{item.reason_code}</strong></div><div><small>Created</small><strong>{formatDate(item.created_at, { year: "numeric" })}</strong></div></section>
    {meta?.candidates?.length ? <section><h3>Candidate comparison</h3><div className="candidate-grid">{meta.candidates.map((candidate) => <div key={candidate.id}><span><FileSpreadsheet size={17}/><strong>{candidate.id}</strong></span><b>{formatMoney(candidate.amount, meta.currency)}</b><small>{candidate.note}</small><em>{candidate.score} score</em></div>)}</div></section> : null}
    <section><h3>Evidence collected</h3><div className="evidence-list">{item.evidence.map((evidence) => <div key={evidence.evidence_id}><span><Link2 size={16}/></span><p><strong>{titleCase(evidence.evidence_type)}</strong><small>{evidence.summary}</small><code>{evidence.source_reference}</code></p></div>)}</div></section>
    <section className="next-action"><WandSparkles size={19}/><div><small>RECOMMENDED NEXT ACTION</small><strong>{item.next_action}</strong></div></section>
    <section className="resolution-box"><label htmlFor={`resolution-${item.exception_id}`}>Resolution note</label><textarea id={`resolution-${item.exception_id}`} value={resolution} onChange={(event) => setResolution(event.target.value)} placeholder="Describe the evidence received and the accounting treatment…" disabled={item.status === "RESOLVED"}/></section>
    <footer>{item.status !== "RESOLVED" ? <><button className="button secondary" disabled={busy === `review:${item.exception_id}` || item.status === "IN_REVIEW"} onClick={() => void onReview(item)}>{busy === `review:${item.exception_id}` ? <Loader2 className="spin" size={16}/> : <UserCheck size={16}/>} {item.status === "IN_REVIEW" ? "Review requested" : "Request human review"}</button><button className="button primary" disabled={!resolution.trim() || busy === `resolve:${item.exception_id}`} onClick={() => void onResolve(item, resolution.trim())}>{busy === `resolve:${item.exception_id}` ? <Loader2 className="spin" size={16}/> : <Check size={16}/>} Resolve exception</button></> : <span className="resolved-note"><CheckCircle2 size={18}/> Resolved: {item.resolution}</span>}</footer>
  </article>;
}

function ForecastView({ forecast, onScenario, onReset, busy, notify }: { forecast: RunCashForecastResult; onScenario: () => void; onReset: () => void; busy: string | null; notify: (message: string, kind?: ToastState["kind"]) => void }) {
  const scenarioActive = forecast.scenario.action_type && forecast.scenario.action_type !== "base";
  const exportCsv = () => {
    const header = "date,confirmed,expected,risk_adjusted,p10,p50,p90";
    const rows = forecast.positions.map((item) => [item.date, item.confirmed, item.expected, item.risk_adjusted, item.p10 ?? "", item.p50 ?? "", item.p90 ?? ""].join(","));
    downloadBlob(new Blob([[header, ...rows].join("\n")], { type: "text/csv" }), `cashclose-forecast-${forecast.forecast_id}.csv`);
    notify("Forecast CSV exported");
  };
  return <div className="page-stack">
    {scenarioActive ? <section className="scenario-active"><Sparkles size={18}/><span><strong>Scenario active: {forecast.scenario.scenario_name}</strong><small>The chart and minimum below reflect this deterministic simulation.</small></span><button className="button secondary" onClick={onReset} disabled={busy === "scenario-reset"}>{busy === "scenario-reset" ? <Loader2 className="spin" size={16}/> : <RefreshCw size={16}/>} Restore base</button></section> : null}
    <section className="forecast-metric-grid">
      <article className="forecast-low"><p className="eyebrow">EXPECTED CASH MINIMUM</p><strong>{formatMoney(forecast.minimum_expected_cash, forecast.currency)}</strong><span>{formatDate(forecast.minimum_expected_cash_date, { year: "numeric" })}</span><div className={forecast.shortfall_date ? "danger" : "safe"}>{forecast.shortfall_date ? <AlertTriangle size={16}/> : <CheckCircle2 size={16}/>} {forecast.shortfall_date ? `Shortfall from ${formatDate(forecast.shortfall_date)}` : "Above zero throughout"}</div></article>
      <article><span>Forecast horizon</span><strong>{forecast.horizon_days} days</strong><small>{formatDate(forecast.as_of_date)} to {formatDate(forecast.positions.at(-1)?.date ?? forecast.as_of_date)}</small></article>
      <article><span>Simulation range</span><strong>P10–P90</strong><small>Calculated confidence band</small></article>
      <article><span>Opening basis</span><strong>Verified cash</strong><small>Committed records only</small></article>
    </section>
    <section className="panel forecast-panel">
      <PanelHeader eyebrow="DAILY CLOSING CASH" title="Confirmed, expected & risk-adjusted" action={<div className="panel-actions"><button className="button secondary" onClick={exportCsv}><Download size={16}/> Export</button><button className="button primary" onClick={onScenario}><SlidersHorizontal size={16}/> Run scenario</button></div>}/>
      <ChartLegend forecast={forecast}/>
      <CashChart forecast={forecast}/>
    </section>
    <section className="forecast-insights">
      <article className="panel"><PanelHeader eyebrow="MAJOR MOVEMENTS" title="Cash drivers"/><div className="driver-list"><Driver icon={ArrowUpRight} tone="in" title="Expected customer receipts" detail="Probability-weighted open receivables" value="Calculated daily"/><Driver icon={ArrowDownRight} tone="out" title="Payroll and approved payables" detail="Committed obligations from the ledger" value="Hard outflows"/><Driver icon={AlertTriangle} tone="risk" title="Unresolved reconciliation" detail="Risk-adjusted until evidence is complete" value="Excluded from confirmed"/></div></article>
      <article className="panel scenario-launch"><span className="scenario-icon"><Gauge size={24}/></span><p className="eyebrow">SCENARIO LAB</p><h2>Move a receipt. Delay a payable. Add an outflow.</h2><p>The forecast engine recalculates every daily position with decimal-safe code. The controller only explains the result.</p><button className="button primary" onClick={onScenario}>Open scenario lab <ArrowRight size={16}/></button></article>
    </section>
  </div>;
}

function Driver({ icon: Icon, tone, title, detail, value }: { icon: LucideIcon; tone: string; title: string; detail: string; value: string }) {
  return <div><span className={`driver-icon ${tone}`}><Icon size={18}/></span><span><strong>{title}</strong><small>{detail}</small></span><b>{value}</b></div>;
}

function AuditView({ workspace, onDownload, busy, notify }: { workspace: WorkspaceData; onDownload: () => void; busy: string | null; notify: (message: string, kind?: ToastState["kind"]) => void }) {
  const [eventFilter, setEventFilter] = useState("ALL");
  const events = workspace.events.filter((event) => eventFilter === "ALL" || event.agent_name === eventFilter);
  const metrics = workspace.evaluation.match_metrics;
  const scorecards = [
    ["Precision", confidencePercent(metrics.precision), "Correct automatic matches"],
    ["Recall", confidencePercent(metrics.recall), "True matches discovered"],
    ["Automation", confidencePercent(metrics.automation_coverage), "Eligible records auto-resolved"],
    ["Value coverage", confidencePercent(metrics.value_weighted_coverage), "Reconcilable value resolved"],
    ["False approval", confidencePercent(metrics.false_approval_rate), "Lower is safer"],
    ["Exception recall", confidencePercent(metrics.exception_recall), "Unsafe records escalated"],
  ];
  const copyBatch = async () => {
    await navigator.clipboard.writeText(workspace.batch.batch_id);
    notify("Batch ID copied");
  };
  return <div className="page-stack">
    <section className="audit-hero panel"><span className="audit-seal"><ShieldCheck size={28}/></span><div><p className="eyebrow">INDEPENDENT EVALUATION COMPLETE</p><h2>The agent never saw the answer key.</h2><p>Outputs were compared with isolated ground truth after the batch reached a terminal state.</p></div><div className="audit-actions"><button className="button secondary" onClick={() => void copyBatch()}><Copy size={16}/> Copy batch ID</button><button className="button primary" onClick={onDownload} disabled={busy === "download-audit"}>{busy === "download-audit" ? <Loader2 className="spin" size={16}/> : <Download size={16}/>} Download report</button></div></section>
    <section className="score-grid">{scorecards.map(([label, value, detail]) => <article className="panel" key={label}><span>{label}</span><strong>{value}</strong><small>{detail}</small><div><i style={{ width: label === "False approval" ? `${Math.max(2, Number(metrics.false_approval_rate) * 100)}%` : value }}/></div></article>)}</section>
    <section className="audit-grid">
      <article className="panel controls-panel"><PanelHeader eyebrow="CONTROL MATRIX" title="Financial safety checks" action={<span className="passed-label"><CheckCircle2 size={15}/> 6 passed</span>}/><div className="control-list"><Control title="Decimal-only money arithmetic" detail="Amounts remain validated decimal strings; no floating-point writes."/><Control title="Verified writes only" detail="A proposal must pass threshold and hard-risk policy before commit."/><Control title="Idempotent commitments" detail="Every commit requires a unique idempotency key and cannot be replayed."/><Control title="Ground truth isolation" detail="Evaluation artifacts remain outside every controller tool boundary."/><Control title="Currency hard stop" detail="Any inconsistent currency becomes an exception regardless of score."/><Control title="Complete terminal coverage" detail="Every eligible record ends reconciled, reviewed, or explicitly unresolved."/></div></article>
      <article className="panel audit-trail"><PanelHeader eyebrow="AGENT EVENTS" title="Controller trace" action={<select value={eventFilter} onChange={(event) => setEventFilter(event.target.value)} aria-label="Filter agent events"><option value="ALL">All agents</option>{Array.from(new Set(workspace.events.map((event) => event.agent_name))).map((agent) => <option key={agent} value={agent}>{titleCase(agent)}</option>)}</select>}/><div className="audit-event-list">{events.map((event) => <details key={event.sequence}><summary><span className={`event-marker ${event.status}`}>{event.status === "warning" ? <AlertTriangle size={13}/> : <Check size={13}/>}</span><span><strong>{event.message}</strong><small>{titleCase(event.agent_name)} · {event.tool_name ?? event.event_type}</small></span><time>{formatDuration(event.latency_ms)}</time><ChevronDown size={16}/></summary><div><code>{event.tool_result_reference ?? `event:${event.sequence}`}</code><span>{new Date(event.timestamp).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })}</span></div></details>)}</div></article>
    </section>
    <section className="method-note"><LockKeyhole size={18}/><p><strong>Evaluation boundary:</strong> metrics are calculated by deterministic code after execution. The controller can reference the results, but cannot read private ground-truth tables.</p></section>
  </div>;
}

function Control({ title, detail }: { title: string; detail: string }) {
  return <div><CheckCircle2 size={18}/><span><strong>{title}</strong><small>{detail}</small></span><em>PASS</em></div>;
}

function MatchPanel({ proposal, decision, source, busy, onClose, onApprove, onReject, onEdit }: { proposal: MatchProposal; decision?: ReconciliationDecision; source: Source; busy: string | null; onClose: () => void; onApprove: (proposal: MatchProposal, note: string) => Promise<void>; onReject: (proposal: MatchProposal, reason: string) => Promise<void>; onEdit: (proposal: MatchProposal, allocations: Allocation[], reason: string) => Promise<void> }) {
  const [mode, setMode] = useState<"review" | "edit">("review");
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [allocations, setAllocations] = useState<Allocation[]>(proposal.allocations);
  const meta = DEMO_MATCH_META[proposal.transaction_id];
  const committed = decision?.decision === "AUTO_RECONCILED" || decision?.decision === "MANUALLY_RECONCILED" || proposal.status === "COMMITTED";
  return <Overlay onClose={onClose} side>
    <article className="side-panel match-panel">
      <DrawerHeader icon={<ArrowLeftRight size={20}/>} eyebrow={`${proposal.proposal_id} · REV ${proposal.revision ?? 1}`} title="Match evidence" subtitle={source === "live" ? "Connected to audited review tools" : "Interactive preview — no database write"} onClose={onClose}/>
      <div className="confidence-block"><div><small>POLICY CONFIDENCE</small><strong>{confidencePercent(proposal.confidence)}</strong></div><DecisionPill status={decision?.decision ?? proposal.status ?? "PROPOSED"}/></div>
      <section className="drawer-section"><h3>Bank transaction</h3><div className="bank-summary"><span><Banknote size={21}/></span><div><strong>{formatMoney(proposal.transaction_amount, proposal.currency)}</strong><small>{meta?.counterparty ?? proposal.transaction_id}</small><code>{meta?.reference ?? proposal.transaction_id}</code></div></div></section>
      <section className="drawer-section"><div className="section-title"><h3>Invoice allocation</h3>{!committed ? <button className="text-button" onClick={() => setMode(mode === "edit" ? "review" : "edit")}>{mode === "edit" ? "Cancel edit" : "Edit allocation"}</button> : null}</div><div className="allocation-editor">{allocations.map((allocation, index) => <div key={`${allocation.invoice_id}-${index}`}><span><FileCheck2 size={17}/>{mode === "edit" ? <input aria-label={`Invoice ${index + 1}`} value={allocation.invoice_id} onChange={(event) => setAllocations((current) => current.map((item, position) => position === index ? { ...item, invoice_id: event.target.value } : item))}/> : <strong>{allocation.invoice_id}</strong>}</span>{mode === "edit" ? <input aria-label={`Allocation amount ${index + 1}`} inputMode="decimal" value={allocation.amount} onChange={(event) => setAllocations((current) => current.map((item, position) => position === index ? { ...item, amount: event.target.value as Allocation["amount"] } : item))}/> : <strong>{formatMoney(allocation.amount, allocation.currency)}</strong>}</div>)}</div>{mode === "edit" ? <><label className="field-label">Edit reason<textarea value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why is this allocation changing?"/></label><button className="button primary full" disabled={!reason.trim() || busy === `edit:${proposal.proposal_id}`} onClick={() => { try { const normalized = allocations.map((item) => ({ ...item, amount: money(item.amount) })); void onEdit(proposal, normalized, reason); setMode("review"); } catch (error) { setReason(errorMessage(error)); } }}>{busy === `edit:${proposal.proposal_id}` ? <Loader2 className="spin" size={16}/> : <ShieldCheck size={16}/>} Save & re-verify</button></> : null}</section>
      <section className="drawer-section"><h3>Evidence ledger</h3><div className="evidence-list">{proposal.evidence.map((item) => <div key={item.evidence_id}><span><Check size={15}/></span><p><strong>{titleCase(item.evidence_type)}</strong><small>{item.summary}</small><code>{item.source_reference}</code></p></div>)}</div></section>
      {proposal.risk_flags?.length ? <section className="risk-flags"><AlertTriangle size={17}/><div><strong>Risk flags</strong>{proposal.risk_flags.map((flag) => <span key={flag}>{titleCase(flag)}</span>)}</div></section> : <section className="safe-policy"><ShieldCheck size={17}/><span><strong>No hard contradictions</strong><small>Amount, currency, reuse, and evidence policies passed.</small></span></section>}
      {!committed && mode === "review" ? <section className="review-note"><label htmlFor="review-note">Reviewer note</label><textarea id="review-note" value={note} onChange={(event) => setNote(event.target.value)} placeholder="Document the evidence supporting your decision…"/></section> : null}
      <footer className="drawer-footer">{committed ? <span className="committed-note"><CheckCircle2 size={18}/> Committed through policy {decision?.policy_version ?? "CC-R2.4"}</span> : <><button className="button danger-outline" disabled={busy === `reject:${proposal.proposal_id}`} onClick={() => void onReject(proposal, note || "Evidence did not support the proposed allocation.")}>{busy === `reject:${proposal.proposal_id}` ? <Loader2 className="spin" size={16}/> : <XCircle size={16}/>} Reject</button><button className="button primary" disabled={busy === `approve:${proposal.proposal_id}`} onClick={() => void onApprove(proposal, note)}>{busy === `approve:${proposal.proposal_id}` ? <Loader2 className="spin" size={16}/> : <Check size={16}/>} Approve match</button></>}</footer>
    </article>
  </Overlay>;
}

function ExceptionQuickPanel({ item, busy, onClose, onResolve, onReview }: { item: ExceptionRecord; busy: string | null; onClose: () => void; onResolve: (item: ExceptionRecord, resolution: string) => Promise<void>; onReview: (item: ExceptionRecord) => Promise<void> }) {
  const [resolution, setResolution] = useState("");
  return <Overlay onClose={onClose} side><article className="side-panel"><DrawerHeader icon={<AlertTriangle size={20}/>} eyebrow={item.exception_id} title={titleCase(item.reason_code)} subtitle={item.record_id} onClose={onClose}/><div className="quick-exception"><p>{DEMO_EXCEPTION_META[item.exception_id]?.explanation ?? "The controller could not establish enough safe evidence to commit this record."}</p><section className="next-action"><WandSparkles size={19}/><div><small>NEXT ACTION</small><strong>{item.next_action}</strong></div></section><h3>Evidence</h3><div className="evidence-list">{item.evidence.map((evidence) => <div key={evidence.evidence_id}><span><Link2 size={15}/></span><p><strong>{titleCase(evidence.evidence_type)}</strong><small>{evidence.summary}</small></p></div>)}</div><label className="field-label">Resolution<textarea value={resolution} onChange={(event) => setResolution(event.target.value)} placeholder="Add supporting evidence and treatment…"/></label></div><footer className="drawer-footer"><button className="button secondary" disabled={item.status === "IN_REVIEW" || busy === `review:${item.exception_id}`} onClick={() => void onReview(item)}><UserCheck size={16}/> Request review</button><button className="button primary" disabled={!resolution.trim() || busy === `resolve:${item.exception_id}`} onClick={() => void onResolve(item, resolution)}><Check size={16}/> Resolve</button></footer></article></Overlay>;
}

function NewBatchDialog({ connection, capabilities, busy, onClose, onDemo, onUpload, notify }: { connection: Connection; capabilities: RuntimeCapabilitiesView | null; busy: boolean; onClose: () => void; onDemo: (mode: ExecutionMode) => void; onUpload: (files: Record<FileKind, UploadState>, mode: ExecutionMode) => void; notify: (message: string, kind?: ToastState["kind"]) => void }) {
  const [tab, setTab] = useState<"demo" | "upload">("demo");
  const [files, setFiles] = useState<Partial<Record<FileKind, UploadState>>>({});
  const agenticAvailable = capabilities?.responses_mode_configured === true;
  const [executionMode, setExecutionMode] = useState<ExecutionMode>(agenticAvailable ? "agentic" : "deterministic");
  const selectedExecutionMode: ExecutionMode = agenticAvailable ? executionMode : "deterministic";
  const readFile = async (kind: FileKind, file?: File) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".csv")) {
      setFiles((current) => ({ ...current, [kind]: { file, rows: 0, columns: [], issue: "Use a CSV file." } }));
      return;
    }
    const text = await file.text();
    const lines = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/);
    const columns = (lines[0] ?? "").split(",").map((column) => column.trim().replace(/^"|"$/g, ""));
    const missing = FILE_REQUIREMENTS[kind].columns.filter((column) => !columns.includes(column));
    setFiles((current) => ({ ...current, [kind]: { file, rows: Math.max(0, lines.length - 1), columns, issue: missing.length ? `Missing: ${missing.join(", ")}` : null } }));
  };
  const ready = (Object.keys(FILE_REQUIREMENTS) as FileKind[]).every((kind) => files[kind] && !files[kind]?.issue);
  const downloadTemplate = (kind: FileKind) => {
    downloadBlob(new Blob([`${FILE_REQUIREMENTS[kind].columns.join(",")}\n`], { type: "text/csv" }), `${kind}.csv`);
    notify(`${FILE_REQUIREMENTS[kind].label} template downloaded`);
  };
  return <Overlay onClose={busy ? undefined : onClose}>
    <article className="modal new-batch-modal">
      <DrawerHeader icon={<Plus size={20}/>} eyebrow="NEW CLOSE BATCH" title="Choose the truth layer" subtitle="Run the reproducible hackathon set or validate your own four source files." onClose={onClose}/>
      <div className="modal-tabs"><button className={tab === "demo" ? "active" : ""} onClick={() => setTab("demo")}><Sparkles size={17}/> Demo truth set</button><button className={tab === "upload" ? "active" : ""} onClick={() => setTab("upload")}><UploadCloud size={17}/> Upload CSVs</button></div>
      <section className="execution-mode-section" aria-labelledby="execution-mode-title">
        <div><p className="eyebrow">EXECUTION MODE</p><h3 id="execution-mode-title">Choose how the controller plans</h3></div>
        <div className="execution-mode-options" role="radiogroup" aria-label="Controller execution mode">
          {agenticAvailable ? <button role="radio" aria-checked={selectedExecutionMode === "agentic"} className={selectedExecutionMode === "agentic" ? "active" : ""} onClick={() => setExecutionMode("agentic")}>
            <span><Sparkles size={19}/></span><span><strong>Agentic Responses</strong><small>{capabilities?.responses_model} chooses investigations and tools; deterministic code still owns every financial action.</small></span><i>{selectedExecutionMode === "agentic" ? <Check size={13}/> : null}</i>
          </button> : null}
          <button role="radio" aria-checked={selectedExecutionMode === "deterministic"} className={selectedExecutionMode === "deterministic" ? "active" : ""} onClick={() => setExecutionMode("deterministic")}>
            <span><Database size={19}/></span><span><strong>Deterministic demo</strong><small>Rule-driven orchestration with no model calls. Matching, money, writes, and metrics remain deterministic.</small></span><i>{selectedExecutionMode === "deterministic" ? <Check size={13}/> : null}</i>
          </button>
        </div>
        {!agenticAvailable ? <p className="capability-note"><LockKeyhole size={15}/><span>Agentic Responses is not configured by this backend. This run will use the deterministic controller and will not be labeled as a model run.</span></p> : null}
      </section>
      {tab === "demo" ? <div className="demo-launch">
        <div className="demo-hero"><span><Database size={25}/></span><div><p className="eyebrow">FIXED SEED · REPRODUCIBLE</p><h3>290 financial records with planted edge cases</h3><p>Exact matches, aliases, partial and combined payments, fees, duplicates, currency conflicts, ambiguity, and unreconcilable cash.</p></div></div>
        <div className="demo-counts"><span><strong>80</strong>bank transactions</span><span><strong>100</strong>invoices</span><span><strong>70</strong>ledger entries</span><span><strong>40</strong>remittances</span></div>
        <ul className="feature-checks"><li><Check size={15}/> Ground truth is kept outside the controller boundary</li><li><Check size={15}/> Allocation and cash calculations use deterministic code</li><li><Check size={15}/> {selectedExecutionMode === "agentic" ? "Responses plans tool selection; every write remains guarded" : "No OpenAI model call is made in this mode"}</li></ul>
        <div className={`api-readiness ${connection}`}><Server size={17}/><span><strong>{connection === "online" ? "Docker API is ready" : connection === "checking" ? "Checking Docker API…" : "Docker API is not reachable"}</strong><small>{connection === "offline" ? "Start docker compose, then try again. The preview remains usable." : "The run will create real backend records and audit events."}</small></span></div>
      </div> : <div className="upload-workflow">
        <div className="upload-grid">{(Object.entries(FILE_REQUIREMENTS) as Array<[FileKind, typeof FILE_REQUIREMENTS[FileKind]]>).map(([kind, config]) => { const selected = files[kind]; return <div className={`upload-card ${selected?.issue ? "invalid" : selected ? "ready" : ""}`} key={kind}><label><span className="upload-icon">{selected?.issue ? <AlertTriangle size={20}/> : selected ? <FileCheck2 size={20}/> : <FileSpreadsheet size={20}/>}</span><span><strong>{config.label}</strong><small>{selected ? `${selected.file.name} · ${selected.rows} rows` : config.columns.join(", ")}</small>{selected?.issue ? <em>{selected.issue}</em> : selected ? <em>{selected.columns.length} columns validated</em> : null}</span><input type="file" accept=".csv,text/csv" onChange={(event) => void readFile(kind, event.target.files?.[0])}/></label><button className="text-button" onClick={() => downloadTemplate(kind)}>Template</button></div>; })}</div>
        <div className={`validation-summary ${ready ? "ready" : "waiting"}`}>{ready ? <CheckCircle2 size={20}/> : <Clock3 size={20}/>}<span><strong>{ready ? "All four schemas are ready" : `${Object.keys(files).length} of 4 files selected`}</strong><small>{ready ? "Server validation runs again before the controller starts." : "Select each CSV and resolve every required-column issue."}</small></span></div>
      </div>}
      <footer className="modal-footer"><button className="button secondary" onClick={onClose} disabled={busy}>Cancel</button>{tab === "demo" ? <button className="button primary" onClick={() => onDemo(selectedExecutionMode)} disabled={busy || connection !== "online"}>{busy ? <Loader2 className="spin" size={16}/> : <Play size={16}/>} Run {selectedExecutionMode === "agentic" ? "with Responses" : "deterministic demo"}</button> : <button className="button primary" onClick={() => ready && onUpload(files as Record<FileKind, UploadState>, selectedExecutionMode)} disabled={!ready || busy || connection !== "online"}>{busy ? <Loader2 className="spin" size={16}/> : <UploadCloud size={16}/>} Upload & run</button>}</footer>
    </article>
  </Overlay>;
}

function RunPanel({ state, error, events, batch, orchestrationMode, processingTimeMs, onClose, onTrace, onResults }: { state: "idle" | "running" | "complete" | "failed"; error: string | null; events: AgentEvent[]; batch: BatchView; orchestrationMode: string; processingTimeMs: number; onClose: () => void; onTrace: () => void; onResults: () => void }) {
  const stageEvents = TRACE_STAGES.map((stage) => events.filter((event) => traceStageForEvent(event) === stage.id));
  const completedStages = stageEvents.filter((items) => items.some((event) => event.status === "succeeded" || event.status === "warning")).length;
  const progress = state === "complete" || state === "failed" ? 100 : Math.min(96, Math.round((completedStages / TRACE_STAGES.length) * 100));
  const agentic = orchestrationMode.startsWith("responses");
  const responsesPending = orchestrationMode === "responses-requested";
  const latestEvents = [...events].sort((a, b) => b.sequence - a.sequence).slice(0, 8);
  return <Overlay onClose={onClose} side>
    <article className="side-panel run-panel">
      <DrawerHeader icon={state === "complete" ? <CheckCircle2 size={20}/> : state === "failed" ? <XCircle size={20}/> : <Bot size={20}/>} eyebrow={batch.batch_id} title={state === "complete" ? "Close complete" : state === "failed" ? "Run stopped safely" : "Controller is working"} subtitle={state === "running" ? "Live operational events—no hidden reasoning or artificial delays." : batch.status.replaceAll("_", " ")} onClose={onClose}/>
      <div className={`run-mode-banner ${agentic ? "agentic" : "deterministic"}`}>{agentic ? <Sparkles size={17}/> : <Database size={17}/>}<span><strong>{agentic ? `OpenAI Responses${responsesPending ? " requested" : ""}` : "Deterministic controller"}</strong><small>{agentic ? responsesPending ? "Waiting for the backend to confirm the completed orchestration mode" : "Model-guided tool planning; deterministic financial execution" : "Rule-driven orchestration; no model calls"}</small></span>{state === "complete" ? <em>{formatDuration(processingTimeMs)} total</em> : <em>Live</em>}</div>
      <div className="run-progress"><div><i style={{ width: `${progress}%` }}/></div><span><strong>{progress}%</strong>{state === "running" ? `${events.length} recorded actions` : state === "complete" ? `Terminal state · ${formatDuration(processingTimeMs)} measured` : "Review run result"}</span></div>
      {state === "failed" && error ? <section className="run-error"><AlertTriangle size={18}/><span><strong>Why this run stopped</strong><small>{error}</small></span></section> : <section className="guardrail-note"><LockKeyhole size={18}/><span><strong>Financial guardrail active</strong><small>Models may choose investigations. Code owns arithmetic, constraints, verification, metrics, and writes.</small></span></section>}
      <div className="run-stage-list">{TRACE_STAGES.map((stage, index) => {
        const relevant = stageEvents[index];
        const event = relevant.at(-1);
        const failed = relevant.some((item) => item.status === "failed");
        const warned = relevant.some((item) => item.status === "warning");
        const complete = Boolean(event && event.status !== "started") || state === "complete";
        const active = !complete && state === "running" && (event?.status === "started" || index === completedStages);
        return <div key={stage.id} className={`${complete ? "complete" : ""} ${active ? "active" : ""} ${failed || warned ? "warning" : ""}`}><span>{failed || warned ? <AlertTriangle size={14}/> : complete ? <Check size={14}/> : active ? <Loader2 className="spin" size={14}/> : index + 1}</span><p><strong>{stage.label}</strong><small>{event?.message ?? "Waiting for a recorded tool result"}</small><code>{event?.tool_name ?? stage.defaultTool}{event ? ` · ${formatDuration(event.latency_ms)}` : ""}</code></p><time>{event ? new Date(event.timestamp).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Kolkata" }) : "—"}</time></div>;
      })}</div>
      <div className="additional-events run-live-events" aria-live="polite"><strong>Live tool feed</strong>{latestEvents.map((event) => <span key={event.sequence}>{event.status === "failed" || event.status === "warning" ? <AlertTriangle size={15}/> : <CheckCircle2 size={15}/>}<p>{event.message}<small>{titleCase(event.agent_name)} · {event.tool_name ?? event.event_type} · {formatDuration(event.latency_ms)}</small></p></span>)}{!latestEvents.length ? <small>Waiting for the first backend event…</small> : null}</div>
      <footer className="drawer-footer"><button className="button secondary" onClick={onTrace}>Open transaction trace</button>{state === "running" ? <span className="running-note"><Loader2 className="spin" size={17}/> Processing continues if this panel closes</span> : state === "failed" ? <button className="button primary" onClick={onClose}>Close</button> : <button className="button primary" onClick={onResults}>Open results <ArrowRight size={16}/></button>}</footer>
    </article>
  </Overlay>;
}

function ScenarioPanel({ currency, busy, onClose, onRun }: { currency: string; busy: boolean; onClose: () => void; onRun: (request: ScenarioRequest) => Promise<void> }) {
  const [type, setType] = useState<"customer_payment_delay" | "payable_delay" | "one_time_outflow">("customer_payment_delay");
  const [name, setName] = useState("Acme Industries");
  const [days, setDays] = useState(7);
  const [amount, setAmount] = useState("50000.00");
  const [validation, setValidation] = useState<string | null>(null);
  const submit = () => {
    try {
      setValidation(null);
      if (type === "one_time_outflow") {
        void onRun({ name: `Add ${amount} ${currency} outflow`, action_type: type, amount: money(amount), currency });
      } else if (!name.trim()) setValidation("Enter a customer or payable name.");
      else if (days < 1 || days > 30) setValidation("Delay must be between 1 and 30 days.");
      else if (type === "customer_payment_delay") void onRun({ name: `${name} pays ${days} days late`, action_type: type, customer_name: name.trim(), delay_days: days, currency });
      else void onRun({ name: `Delay ${name} by ${days} days`, action_type: type, payable_name: name.trim(), delay_days: days, currency });
    } catch (error) { setValidation(errorMessage(error)); }
  };
  return <Overlay onClose={busy ? undefined : onClose} side><article className="side-panel scenario-panel"><DrawerHeader icon={<SlidersHorizontal size={20}/>} eyebrow="DETERMINISTIC SCENARIO LAB" title="Stress-test cash" subtitle="Change one driver; the forecast engine recalculates every day." onClose={onClose}/><div className="scenario-content"><div className="scenario-type-grid"><button className={type === "customer_payment_delay" ? "active" : ""} onClick={() => { setType("customer_payment_delay"); setName("Acme Industries"); }}><ArrowDownRight size={18}/><strong>Receipt delay</strong><small>Move a customer inflow later</small></button><button className={type === "payable_delay" ? "active" : ""} onClick={() => { setType("payable_delay"); setName("Infrastructure vendor"); }}><ArrowUpRight size={18}/><strong>Payable delay</strong><small>Move an approved outflow later</small></button><button className={type === "one_time_outflow" ? "active" : ""} onClick={() => setType("one_time_outflow")}><CircleDollarSign size={18}/><strong>New outflow</strong><small>Add an unplanned cash action</small></button></div>{type === "one_time_outflow" ? <label className="field-label">One-time outflow ({currency})<input inputMode="decimal" value={amount} onChange={(event) => setAmount(event.target.value)} placeholder="50000.00"/></label> : <><label className="field-label">{type === "customer_payment_delay" ? "Customer" : "Payable"}<input value={name} onChange={(event) => setName(event.target.value)} placeholder={type === "customer_payment_delay" ? "Acme Industries" : "Infrastructure vendor"}/></label><label className="field-label">Delay in days<input type="number" min={1} max={30} value={days} onChange={(event) => setDays(Number(event.target.value))}/></label></>}<section className="scenario-rule"><ShieldCheck size={18}/><p><strong>The model does not calculate this forecast.</strong><small>Amounts, date shifts, percentiles, and daily closing cash are produced by deterministic functions.</small></p></section>{validation ? <p className="form-error"><AlertTriangle size={15}/>{validation}</p> : null}</div><footer className="drawer-footer"><button className="button secondary" onClick={onClose} disabled={busy}>Cancel</button><button className="button primary" onClick={submit} disabled={busy}>{busy ? <Loader2 className="spin" size={16}/> : <Gauge size={16}/>} Calculate scenario</button></footer></article></Overlay>;
}

function ConnectionDialog({ connection, source, apiUrl, batchId, onClose, onRun, onPreview }: { connection: Connection; source: Source; apiUrl: string; batchId: string; onClose: () => void; onRun: () => void; onPreview: () => void }) {
  return <Overlay onClose={onClose}><article className="modal connection-modal"><DrawerHeader icon={<Server size={20}/>} eyebrow="RUNTIME CONNECTION" title="Workspace status" subtitle="Know exactly which data you are viewing." onClose={onClose}/><div className="connection-body"><section className={`connection-card ${connection}`}><span><Server size={22}/></span><div><small>FASTAPI CONTROLLER</small><strong>{connection === "online" ? "Online and ready" : connection === "checking" ? "Checking connection" : "Not reachable"}</strong><code>{apiUrl}</code></div></section><section className="connection-facts"><div><span>Current source</span><strong>{source === "live" ? "Live backend records" : "Isolated preview fixture"}</strong></div><div><span>Active batch</span><strong>{batchId}</strong></div><div><span>Persistence</span><strong>{source === "live" ? "API / PostgreSQL boundary" : "In-memory browser session"}</strong></div></section><p className="connection-note"><LockKeyhole size={17}/><span>The browser stores only the active batch pointer. Financial records and decisions stay behind validated API tools.</span></p></div><footer className="modal-footer"><button className="button secondary" onClick={onPreview}>Use preview</button><button className="button primary" disabled={connection !== "online"} onClick={onRun}><Play size={16}/> Run live demo</button></footer></article></Overlay>;
}

function ArchitectureDialog({ onClose }: { onClose: () => void }) {
  return <Overlay onClose={onClose}><article className="modal architecture-modal"><DrawerHeader icon={<Bot size={20}/>} eyebrow="AGENTIC CONTROL PLANE" title="One controller. Bounded specialists." subtitle="The agent decides what to investigate; deterministic tools remain the financial authority." onClose={onClose}/><div className="architecture-map"><section className="controller-node"><span><Bot size={22}/></span><div><small>ORCHESTRATOR</small><strong>Controller agent</strong><p>Plans the run, selects tools, enforces terminal coverage.</p></div></section><div className="architecture-line"><i/><span>structured tool calls</span><i/></div><div className="specialist-grid"><section><ArrowLeftRight size={20}/><strong>Reconciliation</strong><small>Candidate search and evidence assembly</small></section><section><ShieldCheck size={20}/><strong>Verification</strong><small>Policy checks and contradiction detection</small></section><section><Activity size={20}/><strong>Forecast</strong><small>Scenario investigation and explanation</small></section></div><div className="architecture-line"><i/><span>validated schemas</span><i/></div><section className="deterministic-node"><div><Database size={21}/><span><strong>Deterministic finance core</strong><small>Decimal arithmetic · OR-Tools constraints · idempotent writes · metrics</small></span></div><BadgeCheck size={25}/></section></div><footer className="modal-footer"><button className="button primary" onClick={onClose}>Got it</button></footer></article></Overlay>;
}

function DrawerHeader({ icon, eyebrow, title, subtitle, onClose }: { icon: ReactNode; eyebrow: string; title: string; subtitle: string; onClose?: () => void }) {
  return <header className="drawer-header"><span className="drawer-icon">{icon}</span><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2><p>{subtitle}</p></div>{onClose ? <button className="icon-button" onClick={onClose} aria-label="Close"><X size={20}/></button> : null}</header>;
}

function Overlay({ children, onClose, side = false }: { children: ReactNode; onClose?: () => void; side?: boolean }) {
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose?.(); };
    document.addEventListener("keydown", keydown);
    document.body.classList.add("modal-open");
    return () => { document.removeEventListener("keydown", keydown); document.body.classList.remove("modal-open"); };
  }, [onClose]);
  return <div className={`overlay ${side ? "side-overlay" : ""}`} role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose?.(); }}>{children}</div>;
}

function EmptyState({ title, detail, action }: { title: string; detail: string; action?: ReactNode }) {
  return <div className="empty-state"><span><Search size={21}/></span><strong>{title}</strong><p>{detail}</p>{action}</div>;
}

function Toast({ item, onClose }: { item: ToastState; onClose: () => void }) {
  return <div className={`toast ${item.kind}`} role={item.kind === "error" ? "alert" : "status"}>{item.kind === "success" ? <CheckCircle2 size={19}/> : item.kind === "error" ? <AlertTriangle size={19}/> : <Sparkles size={19}/>}<span>{item.message}</span><button onClick={onClose} aria-label="Dismiss notification"><X size={17}/></button></div>;
}
