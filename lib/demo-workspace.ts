import { money } from "./money";
import type {
  AgentEvent,
  AuditReportResult,
  BatchMetricsView,
  BatchView,
  EvaluationView,
  ExceptionRecord,
  MatchList,
  RunCashForecastResult,
} from "./cashclose-types";

export interface MatchDisplayMeta {
  counterparty: string;
  reference: string;
  valueDate: string;
  method: string;
}

export interface ExceptionDisplayMeta {
  title: string;
  counterparty: string;
  amount: string;
  currency: string;
  severity: "high" | "medium" | "low";
  explanation: string;
  candidates: Array<{ id: string; amount: string; score: string; note: string }>;
}

export const DEMO_MATCH_META: Record<string, MatchDisplayMeta> = {
  "BANK-DEMO-0042": {
    counterparty: "Acme Industries",
    reference: "NEFT ACME 1831 1834 SETTLEMENT",
    valueDate: "2026-09-01",
    method: "Combined payment",
  },
  "BANK-DEMO-0057": {
    counterparty: "Novus Retail",
    reference: "WIRE NOVUS INV-2217 UTR879102",
    valueDate: "2026-09-01",
    method: "Exact reference",
  },
  "BANK-DEMO-0061": {
    counterparty: "Meridian Works",
    reference: "MERIDIAN PART SETTLE 981",
    valueDate: "2026-09-01",
    method: "Partial payment",
  },
  "BANK-DEMO-0072": {
    counterparty: "Orchid Foods",
    reference: "ORCHID 4491 WITHHOLDING",
    valueDate: "2026-09-01",
    method: "Deduction review",
  },
  "BANK-DEMO-0079": {
    counterparty: "Kite Systems",
    reference: "KITE SYS AUG SETTLE",
    valueDate: "2026-09-01",
    method: "Alias + amount",
  },
};

const createdAt = "2026-09-01T04:11:00Z";

export const DEMO_BATCH: BatchView = {
  batch_id: "BATCH-DEMO-001",
  organization_id: "ORG-NORTHSTAR",
  status: "COMPLETED",
  accounting_timezone: "Asia/Kolkata",
  as_of_date: "2026-09-01",
  demo_mode: true,
  files: [
    { file_id: "FILE-DEMO-01", batch_id: "BATCH-DEMO-001", file_type: "bank_transactions", filename: "bank_transactions.csv", content_type: "text/csv", size_bytes: 18420, row_count: 80, columns: ["transaction_id", "transaction_date", "amount", "currency", "reference"], uploaded_at: createdAt, validation_issues: [] },
    { file_id: "FILE-DEMO-02", batch_id: "BATCH-DEMO-001", file_type: "invoices", filename: "invoices.csv", content_type: "text/csv", size_bytes: 24610, row_count: 100, columns: ["invoice_id", "customer_id", "amount", "currency", "invoice_date", "due_date"], uploaded_at: createdAt, validation_issues: [] },
    { file_id: "FILE-DEMO-03", batch_id: "BATCH-DEMO-001", file_type: "ledger_entries", filename: "ledger_entries.csv", content_type: "text/csv", size_bytes: 15840, row_count: 70, columns: ["entry_id", "entry_date", "amount", "currency"], uploaded_at: createdAt, validation_issues: [] },
    { file_id: "FILE-DEMO-04", batch_id: "BATCH-DEMO-001", file_type: "remittances", filename: "remittances.csv", content_type: "text/csv", size_bytes: 9960, row_count: 40, columns: ["remittance_id", "transaction_id", "raw_text"], uploaded_at: createdAt, validation_issues: [] },
  ],
  created_at: createdAt,
  updated_at: "2026-09-01T04:19:23Z",
  terminal: true,
};

export const DEMO_MATCHES: MatchList = {
  items: [
    { decision_id: "DEC-DEMO-0042", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0042", decision: "AUTO_RECONCILED", confidence: "0.9730" as never, decision_source: "deterministic_policy", policy_version: "CC-R2.4", proposal_id: "MP-DEMO-0042", committed_at: "2026-09-01T04:17:16Z", idempotency_key: "BATCH-DEMO-001:MP-DEMO-0042:v2" },
    { decision_id: "DEC-DEMO-0057", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0057", decision: "AUTO_RECONCILED", confidence: "0.9940" as never, decision_source: "deterministic_policy", policy_version: "CC-R2.4", proposal_id: "MP-DEMO-0057", committed_at: "2026-09-01T04:17:17Z", idempotency_key: "BATCH-DEMO-001:MP-DEMO-0057:v2" },
    { decision_id: "DEC-DEMO-0061", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0061", decision: "NEEDS_REVIEW", confidence: "0.9180" as never, decision_source: "deterministic_policy", policy_version: "CC-R2.4", proposal_id: "MP-DEMO-0061" },
    { decision_id: "DEC-DEMO-0072", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0072", decision: "NEEDS_REVIEW", confidence: "0.8640" as never, decision_source: "deterministic_policy", policy_version: "CC-R2.4", proposal_id: "MP-DEMO-0072" },
    { decision_id: "DEC-DEMO-0079", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0079", decision: "AUTO_RECONCILED", confidence: "0.9620" as never, decision_source: "deterministic_policy", policy_version: "CC-R2.4", proposal_id: "MP-DEMO-0079", committed_at: "2026-09-01T04:17:18Z", idempotency_key: "BATCH-DEMO-001:MP-DEMO-0079:v2" },
  ],
  proposals: [
    { proposal_id: "MP-DEMO-0042", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0042", allocations: [{ invoice_id: "INV-1831", amount: money("50000"), currency: "USD" }, { invoice_id: "INV-1834", amount: money("34250"), currency: "USD" }], total_allocated: money("84250"), transaction_amount: money("84250"), currency: "USD", permitted_deduction: money("0"), confidence: "0.9730" as never, evidence: [{ evidence_id: "EVID-42-REF", evidence_type: "exact_remittance", summary: "Invoice references 1831 and 1834 were extracted from the remittance.", source_reference: "remittance:REM-0042" }, { evidence_id: "EVID-42-SUM", evidence_type: "allocation_equality", summary: "The two remaining invoice balances equal the bank receipt exactly.", source_reference: "solver:BANK-DEMO-0042" }, { evidence_id: "EVID-42-ALIAS", evidence_type: "approved_alias", summary: "ACME PVT resolves to the approved Acme Industries customer identity.", source_reference: "alias:CUST-001" }], risk_flags: [], status: "COMMITTED", created_at: createdAt },
    { proposal_id: "MP-DEMO-0057", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0057", allocations: [{ invoice_id: "INV-2217", amount: money("41820"), currency: "USD" }], total_allocated: money("41820"), transaction_amount: money("41820"), currency: "USD", permitted_deduction: money("0"), confidence: "0.9940" as never, evidence: [{ evidence_id: "EVID-57-REF", evidence_type: "exact_reference", summary: "The legal invoice number appears in the bank reference.", source_reference: "bank:BANK-DEMO-0057" }, { evidence_id: "EVID-57-AMT", evidence_type: "exact_amount", summary: "Currency and amount match the remaining invoice balance.", source_reference: "invoice:INV-2217" }], risk_flags: [], status: "COMMITTED", created_at: createdAt },
    { proposal_id: "MP-DEMO-0061", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0061", allocations: [{ invoice_id: "INV-0981", amount: money("21000"), currency: "USD" }], total_allocated: money("21000"), transaction_amount: money("21000"), currency: "USD", permitted_deduction: money("0"), confidence: "0.9180" as never, evidence: [{ evidence_id: "EVID-61-PART", evidence_type: "partial_payment", summary: "Reference matches, but only part of the open balance is covered.", source_reference: "invoice:INV-0981" }], risk_flags: ["PARTIAL_ALLOCATION_REQUIRES_REVIEW"], status: "PROPOSED", created_at: createdAt },
    { proposal_id: "MP-DEMO-0072", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0072", allocations: [{ invoice_id: "INV-4491", amount: money("40000"), currency: "USD" }], total_allocated: money("40000"), transaction_amount: money("38275"), currency: "USD", permitted_deduction: money("1725"), confidence: "0.8640" as never, evidence: [{ evidence_id: "EVID-72-TAX", evidence_type: "withholding_hint", summary: "Reference is exact; the difference is consistent with withholding tax.", source_reference: "bank:BANK-DEMO-0072" }], risk_flags: ["WITHHOLDING_EVIDENCE_REQUIRED"], status: "PROPOSED", created_at: createdAt },
    { proposal_id: "MP-DEMO-0079", batch_id: DEMO_BATCH.batch_id, transaction_id: "BANK-DEMO-0079", allocations: [{ invoice_id: "INV-3022", amount: money("12500"), currency: "USD" }], total_allocated: money("12500"), transaction_amount: money("12500"), currency: "USD", permitted_deduction: money("0"), confidence: "0.9620" as never, evidence: [{ evidence_id: "EVID-79-ALIAS", evidence_type: "approved_alias", summary: "The bank counterparty matches a reviewed customer alias.", source_reference: "alias:CUST-014" }, { evidence_id: "EVID-79-AMT", evidence_type: "exact_amount", summary: "Amount and currency match one open invoice in the permitted date window.", source_reference: "invoice:INV-3022" }], risk_flags: [], status: "COMMITTED", created_at: createdAt },
  ],
};

export const DEMO_EXCEPTION_META: Record<string, ExceptionDisplayMeta> = {
  "EXC-DEMO-001": { title: "Two invoices are equally plausible", counterparty: "Blue Mesa Co", amount: "50000.00", currency: "USD", severity: "high", explanation: "Both candidates share the same customer, currency, amount and expected date. No remittance advice identifies the intended invoice, so the controller abstained.", candidates: [{ id: "INV-2841", amount: "50000.00", score: "76.4%", note: "Issued 02 Aug · due 31 Aug" }, { id: "INV-2844", amount: "50000.00", score: "75.9%", note: "Issued 04 Aug · due 31 Aug" }] },
  "EXC-DEMO-002": { title: "Invoice currency contradicts the receipt", counterparty: "Aster Global", amount: "18450.00", currency: "INR", severity: "high", explanation: "The extracted invoice reference points to a USD receivable, while the bank receipt is denominated in INR. Currency conflicts are a hard stop regardless of confidence.", candidates: [{ id: "USD-1188", amount: "18450.00", score: "88.1%", note: "Reference exact · currency conflict" }] },
  "EXC-DEMO-003": { title: "Receipt is below the invoice balance", counterparty: "Parallax Studio", amount: "29875.00", currency: "USD", severity: "medium", explanation: "Reference and counterparty agree, but the difference needs supporting fee advice before it can be posted as a permitted deduction.", candidates: [{ id: "INV-1402", amount: "30000.00", score: "89.6%", note: "$125 variance · fee suspected" }] },
  "EXC-DEMO-004": { title: "Duplicate legal invoice number", counterparty: "Morrow Trading", amount: "18400.00", currency: "USD", severity: "high", explanation: "Two open ledger entries carry the same invoice number and value. Committing either could duplicate settlement, so both are blocked.", candidates: [{ id: "INV-1904-A", amount: "18400.00", score: "82.0%", note: "Ledger entry 4418" }, { id: "INV-1904-B", amount: "18400.00", score: "82.0%", note: "Ledger entry 4472" }] },
};

export const DEMO_EXCEPTIONS: ExceptionRecord[] = [
  { exception_id: "EXC-DEMO-001", batch_id: DEMO_BATCH.batch_id, record_id: "BANK-DEMO-0077", reason_code: "AMBIGUOUS_MATCH", evidence: [{ evidence_id: "EVID-77-AMT", evidence_type: "exact_amount", summary: "Two open invoices equal the transaction amount.", source_reference: "candidate-set:BANK-DEMO-0077" }, { evidence_id: "EVID-77-PARTY", evidence_type: "customer_identity", summary: "Both candidates belong to the normalized counterparty.", source_reference: "customer:CUST-017" }], next_action: "Request remittance advice from Blue Mesa before allocating.", status: "OPEN", created_at: "2026-09-01T04:17:19Z" },
  { exception_id: "EXC-DEMO-002", batch_id: DEMO_BATCH.batch_id, record_id: "BANK-DEMO-0064", reason_code: "CURRENCY_MISMATCH", evidence: [{ evidence_id: "EVID-64-CUR", evidence_type: "currency_conflict", summary: "Bank transaction is INR; referenced invoice is USD.", source_reference: "invoice:USD-1188" }], next_action: "Confirm whether treasury converted the settlement off-ledger.", status: "OPEN", created_at: "2026-09-01T04:17:20Z" },
  { exception_id: "EXC-DEMO-003", batch_id: DEMO_BATCH.batch_id, record_id: "BANK-DEMO-0059", reason_code: "SUSPECTED_FEE", evidence: [{ evidence_id: "EVID-59-VAR", evidence_type: "amount_variance", summary: "The $125 difference is inside fee tolerance but unsupported.", source_reference: "bank:BANK-DEMO-0059" }], next_action: "Attach bank fee advice, then approve the deduction.", status: "IN_REVIEW", created_at: "2026-09-01T04:17:21Z" },
  { exception_id: "EXC-DEMO-004", batch_id: DEMO_BATCH.batch_id, record_id: "INV-1904", reason_code: "DUPLICATE_INVOICE", evidence: [{ evidence_id: "EVID-1904-DUP", evidence_type: "duplicate_record", summary: "Two open ledger records share the legal invoice number.", source_reference: "ledger:INV-1904" }], next_action: "Void the duplicate ledger entry before reconciliation.", status: "OPEN", created_at: "2026-09-01T04:17:22Z" },
];

const forecastRows = [
  ["2026-09-02", "598500", "598500", "598500", "580500", "598500", "607500"],
  ["2026-09-04", "570500", "570500", "570500", "550800", "570500", "580350"],
  ["2026-09-06", "542500", "752500", "731500", "710100", "731500", "763200"],
  ["2026-09-08", "514500", "724500", "703500", "680400", "703500", "736050"],
  ["2026-09-10", "326500", "536500", "515500", "490700", "515500", "548900"],
  ["2026-09-12", "298500", "508500", "487500", "461000", "487500", "521750"],
  ["2026-09-14", "270500", "480500", "459500", "431300", "459500", "494600"],
  ["2026-09-16", "242500", "617500", "560200", "530300", "560200", "632450"],
  ["2026-09-18", "214500", "589500", "532200", "500600", "532200", "605300"],
  ["2026-09-20", "-33500", "341500", "284200", "250900", "284200", "358150"],
  ["2026-09-22", "-61500", "313500", "256200", "221200", "256200", "331000"],
  ["2026-09-24", "-89500", "405500", "306200", "269500", "306200", "423850"],
  ["2026-09-26", "-357500", "137500", "38200", "-200", "38200", "156700"],
  ["2026-09-28", "-385500", "109500", "10200", "-29900", "10200", "129550"],
  ["2026-09-30", "-413500", "81500", "-17800", "-59600", "-17800", "102400"],
  ["2026-10-01", "-427500", "67500", "-31800", "-74450", "-31800", "88825"],
] as const;

export const DEMO_FORECAST: RunCashForecastResult = {
  forecast_id: "FCST-DEMO-001",
  batch_id: DEMO_BATCH.batch_id,
  currency: "USD",
  as_of_date: DEMO_BATCH.as_of_date,
  horizon_days: 30,
  scenario: { scenario_name: "base", delay_days: 0, one_time_outflow: money("0"), currency: "USD" },
  positions: forecastRows.map(([date, confirmed, expected, risk, p10, p50, p90]) => ({ date, confirmed: money(confirmed), expected: money(expected), risk_adjusted: money(risk), p10: money(p10), p50: money(p50), p90: money(p90) })),
  minimum_expected_cash: money("-31800"),
  minimum_expected_cash_date: "2026-10-01",
  shortfall_date: "2026-09-29",
};

export const DEMO_METRICS: BatchMetricsView = {
  matching: { batch_id: DEMO_BATCH.batch_id, precision: "0.9890" as never, recall: "0.9420" as never, automation_coverage: "0.9300" as never, value_weighted_coverage: "0.9680" as never, false_approval_rate: "0.0110" as never, exception_recall: "1.0000" as never, value_reconciled: money("7845300"), unresolved_value: money("184500"), currency: "USD" },
  records_processed: 290,
  forecast_cash_minimum: money("-31800"),
  forecast_cash_minimum_date: "2026-10-01",
  processing_time_ms: 8230,
};

const eventSeed = [
  ["controller", "batch_inspected", "inspect_batch", "Inspected 290 financial records across four source files"],
  ["controller", "validation_completed", "validate_batch", "Quarantined 3 invalid currency values before matching"],
  ["reconciliation", "normalization_completed", "normalize_reference", "Normalized counterparties and bank references"],
  ["reconciliation", "candidates_generated", "find_candidate_invoices", "Generated 146 constrained candidate groups"],
  ["reconciliation", "allocations_solved", "solve_payment_allocation", "Solved 12 combined and partial payment allocations"],
  ["verification", "proposals_verified", "verify_match", "Approved 93 proposals that cleared policy CC-R2.4"],
  ["controller", "exceptions_created", "create_exception", "Explained 17 records where evidence was insufficient"],
  ["forecast", "cash_verified", "calculate_verified_cash", "Established verified opening cash from committed records only"],
  ["forecast", "forecast_completed", "run_monte_carlo_forecast", "Completed 1,000 deterministic forecast simulations"],
  ["evaluation", "evaluation_completed", "calculate_match_metrics", "Calculated precision, recall, coverage and forecast error"],
] as const;

export const DEMO_EVENTS: AgentEvent[] = eventSeed.map(([agent, type, tool, message], index) => ({ sequence: index + 1, batch_id: DEMO_BATCH.batch_id, agent_name: agent as AgentEvent["agent_name"], event_type: type, message, tool_name: tool, input_reference: `batch:${DEMO_BATCH.batch_id}`, tool_result_reference: `event:${index + 1}`, timestamp: `2026-09-01T04:${String(11 + index).padStart(2, "0")}:0${index % 10}Z`, latency_ms: 84 + index * 37, status: index === 1 || index === 6 ? "warning" : "succeeded" }));

export const DEMO_EVALUATION: EvaluationView = {
  evaluation_id: "EVAL-DEMO-001",
  batch_id: DEMO_BATCH.batch_id,
  match_metrics: DEMO_METRICS.matching,
  forecast_metrics: { forecast_id: DEMO_FORECAST.forecast_id, mae: money("18420.35"), currency: "USD", evaluated_days: 30 },
  ground_truth_visible_to_agent: false,
  completed_at: "2026-09-01T04:19:23Z",
};

export const DEMO_AUDIT: AuditReportResult = {
  report_id: "AUDIT-DEMO-001",
  batch_id: DEMO_BATCH.batch_id,
  policy_version: "CC-R2.4",
  generated_at: "2026-09-01T04:19:23Z",
  entries: DEMO_EVENTS.map((event) => ({ sequence: event.sequence, action: event.tool_name ?? event.event_type, actor: event.agent_name, reference: `batch:${DEMO_BATCH.batch_id}`, timestamp: event.timestamp })),
};
