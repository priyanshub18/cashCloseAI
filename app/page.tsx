"use client";

import { useEffect, useMemo, useState } from "react";
import {
  Activity, AlertTriangle, ArrowLeftRight, ArrowRight, ArrowUpRight,
  Bot, CalendarDays, Check, CheckCircle2, ChevronDown, ChevronRight,
  CircleDollarSign, Clock3, Download, FileCheck2, FileSpreadsheet,
  Filter, Gauge, History, LayoutDashboard, LockKeyhole, Menu, MessageSquareText,
  Play, Plus, RefreshCw, Search, Send, ShieldCheck, SlidersHorizontal,
  Sparkles, UploadCloud, X, XCircle, Zap,
  type LucideIcon,
} from "lucide-react";
import {
  Area, CartesianGrid, ComposedChart, Line, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

type View = "overview" | "reconciliation" | "exceptions" | "forecast" | "audit";
type MatchStatus = "Auto-reconciled" | "Needs review" | "Exception";

const NAV: { id: View; label: string; icon: LucideIcon; count?: number }[] = [
  { id: "overview", label: "Control room", icon: LayoutDashboard },
  { id: "reconciliation", label: "Reconciliation", icon: ArrowLeftRight },
  { id: "exceptions", label: "Exceptions", icon: AlertTriangle, count: 17 },
  { id: "forecast", label: "Cash forecast", icon: Activity },
  { id: "audit", label: "Audit & evaluation", icon: ShieldCheck },
];

const PAGE_META: Record<View, { eyebrow: string; title: string; subtitle: string }> = {
  overview: { eyebrow: "SEPTEMBER 2026 · CONTROLLER WORKSPACE", title: "Cash position, verified.", subtitle: "A live view of what is proven, what is projected, and what needs you." },
  reconciliation: { eyebrow: "RECONCILIATION WORKSPACE", title: "Every match has a reason.", subtitle: "Bank activity mapped to invoices with traceable evidence and controlled writes." },
  exceptions: { eyebrow: "EXCEPTION INBOX", title: "The records AI refused to guess.", subtitle: "Ambiguity is surfaced, explained, and routed to the safest next action." },
  forecast: { eyebrow: "30-DAY CASH OUTLOOK", title: "See the squeeze before it happens.", subtitle: "Confirmed, expected, and risk-adjusted cash—calculated from verified records." },
  audit: { eyebrow: "EVALUATION & AUDIT", title: "Proof, not promises.", subtitle: "Independent metrics, policy decisions, and an exportable evidence trail." },
};

const FORECAST = [
  { date: "Sep 01", confirmed: 12.48, expected: 12.48, risk: 12.48, p10: 12.18, p90: 12.72 },
  { date: "Sep 03", confirmed: 11.91, expected: 13.08, risk: 12.62, p10: 11.82, p90: 13.72 },
  { date: "Sep 05", confirmed: 10.82, expected: 12.56, risk: 11.94, p10: 10.86, p90: 13.45 },
  { date: "Sep 07", confirmed: 9.76, expected: 11.88, risk: 11.12, p10: 9.98, p90: 12.84 },
  { date: "Sep 09", confirmed: 8.94, expected: 10.97, risk: 10.18, p10: 8.88, p90: 12.02 },
  { date: "Sep 11", confirmed: 8.31, expected: 9.84, risk: 8.92, p10: 7.61, p90: 10.97 },
  { date: "Sep 13", confirmed: 6.92, expected: 8.31, risk: 7.28, p10: 5.98, p90: 9.47 },
  { date: "Sep 15", confirmed: 5.14, expected: 6.76, risk: 5.64, p10: 4.28, p90: 8.09 },
  { date: "Sep 18", confirmed: 2.86, expected: 3.97, risk: 2.18, p10: 1.22, p90: 5.48 },
  { date: "Sep 20", confirmed: 3.72, expected: 5.42, risk: 4.08, p10: 2.64, p90: 6.76 },
  { date: "Sep 22", confirmed: 3.18, expected: 6.91, risk: 5.23, p10: 3.37, p90: 8.11 },
  { date: "Sep 24", confirmed: 2.74, expected: 7.64, risk: 6.12, p10: 4.11, p90: 9.32 },
  { date: "Sep 26", confirmed: 2.43, expected: 8.28, risk: 6.86, p10: 4.72, p90: 10.18 },
  { date: "Sep 28", confirmed: 2.29, expected: 9.13, risk: 7.38, p10: 5.01, p90: 11.36 },
  { date: "Sep 30", confirmed: 2.18, expected: 10.06, risk: 8.04, p10: 5.42, p90: 12.25 },
];

const MATCHES: { id: string; counterparty: string; reference: string; date: string; amount: string; allocation: string; evidence: string; confidence: number; status: MatchStatus; complex?: boolean }[] = [
  { id: "BANK-0042", counterparty: "Acme Pvt Ltd", reference: "NEFT ACME 1831 1834 LESS CHGS", date: "31 Aug", amount: "₹8,42,500", allocation: "INV-1831 · ₹5,00,000  +  INV-1834 · ₹3,42,500", evidence: "2 exact references · sum verified · approved alias", confidence: 97.3, status: "Auto-reconciled", complex: true },
  { id: "BANK-0057", counterparty: "Novus Retail", reference: "UTR879102 / NOVUS / INV2217", date: "31 Aug", amount: "₹4,18,200", allocation: "INV-2217 · ₹4,18,200", evidence: "Exact reference · exact amount · currency match", confidence: 99.4, status: "Auto-reconciled" },
  { id: "BANK-0061", counterparty: "Meridian Works", reference: "MERIDIAN PART SETTLE 981", date: "01 Sep", amount: "₹2,10,000", allocation: "INV-0981 · partial ₹2,10,000", evidence: "Reference match · known partial-payment pattern", confidence: 91.8, status: "Needs review" },
  { id: "BANK-0068", counterparty: "Blue Mesa Co", reference: "IMPS 044933 BLUE M", date: "01 Sep", amount: "₹7,55,000", allocation: "No safe allocation", evidence: "Two equally plausible open invoices", confidence: 62.1, status: "Exception" },
  { id: "BANK-0072", counterparty: "Orchid Foods", reference: "ORCHID / 4491 / TDS", date: "01 Sep", amount: "₹3,82,750", allocation: "INV-4491 · ₹4,00,000 less ₹17,250", evidence: "Exact reference · suspected withholding", confidence: 86.4, status: "Needs review" },
  { id: "BANK-0079", counterparty: "Kite Systems", reference: "KITE SYS AUG SETTLE", date: "01 Sep", amount: "₹1,25,000", allocation: "INV-3022 · ₹1,25,000", evidence: "Alias · amount · date window", confidence: 96.2, status: "Auto-reconciled" },
];

const EXCEPTIONS = [
  { id: "EXC-017", type: "Ambiguous match", title: "Two invoices fit the same ₹7.55L receipt", party: "Blue Mesa Co", record: "BANK-0068", amount: "₹7,55,000", age: "12 min", severity: "High", reason: "Both INV-2841 and INV-2844 share the amount, customer, currency, and expected date. No remittance was provided.", action: "Request remittance advice from Blue Mesa before allocating.", chips: ["Same amount", "Same customer", "No remittance"] },
  { id: "EXC-014", type: "Currency mismatch", title: "USD invoice paired with an INR receipt", party: "Aster Global", record: "BANK-0064", amount: "₹5,12,200", age: "18 min", severity: "High", reason: "The extracted invoice reference points to USD-1188, but the bank transaction is denominated in INR.", action: "Confirm whether treasury converted the settlement off-ledger.", chips: ["Hard risk flag", "Currency conflict"] },
  { id: "EXC-011", type: "Suspected fee", title: "Receipt is ₹1,250 below invoice balance", party: "Parallax Studio", record: "BANK-0059", amount: "₹2,98,750", age: "24 min", severity: "Medium", reason: "Reference and counterparty agree. The unexplained difference is within fee policy but has no supporting bank advice.", action: "Attach bank fee advice, then approve the deduction.", chips: ["₹1,250 variance", "Reference exact"] },
  { id: "EXC-008", type: "Duplicate invoice", title: "Invoice reference appears twice in the ledger", party: "Morrow Trading", record: "INV-1904", amount: "₹1,84,000", age: "31 min", severity: "High", reason: "Two open ledger entries carry the same legal invoice number and value.", action: "Void the duplicate ledger entry before reconciliation.", chips: ["Duplicate", "Write blocked"] },
  { id: "EXC-005", type: "Missing invoice", title: "Receipt has no open receivable", party: "Unknown / RIVERA", record: "BANK-0048", amount: "₹94,500", age: "44 min", severity: "Medium", reason: "No invoice in the batch shares a reference, alias, or compatible amount.", action: "Ask accounts receivable to identify the payer and upload the invoice.", chips: ["No candidates", "Unidentified cash"] },
];

const RUN_EVENTS = [
  { tool: "inspect_batch", text: "Inspected 230 source records", detail: "4 files · schema fingerprints stored", time: "09:41:02" },
  { tool: "validate_batch", text: "Found 3 invalid currency codes", detail: "Quarantined before candidate generation", time: "09:41:03" },
  { tool: "find_candidate_invoices", text: "Generated 146 candidate groups", detail: "Currency, date and identity constraints applied", time: "09:41:06" },
  { tool: "solve_payment_allocation", text: "Solved 12 combined allocations", detail: "Every allocation equals the bank value", time: "09:41:11" },
  { tool: "verify_match", text: "Approved 93 safe proposals", detail: "Policy CC-R2.4 · confidence ≥ 0.95", time: "09:41:16" },
  { tool: "create_exception", text: "Explained 17 ambiguous records", detail: "No unsupported financial write performed", time: "09:41:18" },
  { tool: "calculate_verified_cash", text: "Established ₹12.48cr verified cash", detail: "Committed records only", time: "09:41:21" },
  { tool: "run_cash_forecast", text: "Completed the 30-day forecast", detail: "Projected low ₹2.18cr on 18 Sep", time: "09:41:25" },
];

const FILES = [
  { key: "bank", label: "Bank transactions", rows: 80, hint: "date, amount, currency, reference" },
  { key: "invoices", label: "Invoices", rows: 100, hint: "invoice, customer, due date, balance" },
  { key: "ledger", label: "Ledger entries", rows: 70, hint: "entry, account, debit, credit" },
  { key: "remittance", label: "Remittances", rows: 40, hint: "payer, references, payment advice" },
];

function formatCr(value: number) { return `₹${value.toFixed(2)}cr`; }

function StatusPill({ status }: { status: MatchStatus }) {
  const icon = status === "Auto-reconciled" ? <Check size={11} /> : status === "Needs review" ? <Clock3 size={11} /> : <AlertTriangle size={11} />;
  return <span className={`status-pill ${status.toLowerCase().replaceAll(" ", "-")}`}>{icon}{status}</span>;
}

function ForecastChart({ scenario = false, compact = false }: { scenario?: boolean; compact?: boolean }) {
  const data = useMemo(() => FORECAST.map((point, i) => scenario && i >= 6 && i <= 10 ? { ...point, risk: Math.max(.72, point.risk - 2.35), expected: point.expected - 1.72, p10: Math.max(.35, point.p10 - 1.8), p90: point.p90 - .9 } : point), [scenario]);
  return (
    <div className={compact ? "forecast-chart compact" : "forecast-chart"}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 12, right: 12, bottom: 0, left: compact ? -24 : -10 }}>
          <defs>
            <linearGradient id="cashBand" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#8fc9ae" stopOpacity={.32}/><stop offset="100%" stopColor="#8fc9ae" stopOpacity={.03}/></linearGradient>
            <linearGradient id="riskFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#e89151" stopOpacity={.16}/><stop offset="100%" stopColor="#e89151" stopOpacity={0}/></linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke="#e6e9e1" strokeDasharray="2 4" />
          <XAxis dataKey="date" axisLine={false} tickLine={false} minTickGap={compact ? 48 : 28} tick={{ fontSize: 9, fill: "#929b95" }} />
          <YAxis axisLine={false} tickLine={false} width={42} domain={[0, 15]} ticks={[0,5,10,15]} tickFormatter={(v) => `₹${v}cr`} tick={{ fontSize: 9, fill: "#929b95" }} />
          <Tooltip content={<CashTooltip />} />
          {!compact && <Area type="monotone" dataKey="expected" stroke="none" fill="url(#cashBand)" />}
          <Area type="monotone" dataKey="risk" stroke="none" fill="url(#riskFill)" />
          <ReferenceLine y={3} stroke="#da8250" strokeDasharray="4 5" label={!compact ? { value: "Operating threshold", position: "insideBottomRight", fill: "#b66d40", fontSize: 9 } : undefined} />
          <Line type="monotone" dataKey="confirmed" name="Confirmed" stroke="#193c31" strokeWidth={2.2} dot={false} activeDot={{ r: 4, fill: "#193c31" }} />
          <Line type="monotone" dataKey="expected" name="Expected" stroke="#5da487" strokeWidth={2} strokeDasharray="5 4" dot={false} />
          <Line type="monotone" dataKey="risk" name="Risk-adjusted" stroke={scenario ? "#d76f39" : "#d99158"} strokeWidth={2.2} dot={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function CashTooltip({ active, payload, label }: { active?: boolean; payload?: { name?: string; value?: number; color?: string }[]; label?: string }) {
  if (!active || !payload?.length) return null;
  return <div className="cash-tooltip"><strong>{label}</strong>{payload.filter((p) => p.name).map((p) => <span key={p.name}><i style={{ background: p.color }} />{p.name}<b>{formatCr(Number(p.value))}</b></span>)}</div>;
}

export default function Home() {
  const [view, setView] = useState<View>("overview");
  const [mobileNav, setMobileNav] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [runOpen, setRunOpen] = useState(false);
  const [runStep, setRunStep] = useState(-1);
  const [selectedMatch, setSelectedMatch] = useState(MATCHES[0]);
  const [matchDrawer, setMatchDrawer] = useState(false);
  const [matchFilter, setMatchFilter] = useState<"All" | MatchStatus>("All");
  const [matchSearch, setMatchSearch] = useState("");
  const [selectedException, setSelectedException] = useState(EXCEPTIONS[0]);
  const [exceptionFilter, setExceptionFilter] = useState("All");
  const [resolved, setResolved] = useState<string[]>([]);
  const [scenarioOpen, setScenarioOpen] = useState(false);
  const [scenarioApplied, setScenarioApplied] = useState(false);
  const [scenarioText, setScenarioText] = useState("What happens if Acme pays seven days late?");
  const [toast, setToast] = useState("");
  const [fileRows, setFileRows] = useState<Record<string, number>>(Object.fromEntries(FILES.map((f) => [f.key, f.rows])));

  useEffect(() => {
    if (!runOpen || runStep < 0 || runStep >= RUN_EVENTS.length) return;
    const timer = window.setTimeout(() => setRunStep((step) => step + 1), 620);
    return () => window.clearTimeout(timer);
  }, [runOpen, runStep]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const startRun = () => { setUploadOpen(false); setRunStep(0); setRunOpen(true); };
  const chooseView = (id: View) => { setView(id); setMobileNav(false); };
  const filteredMatches = MATCHES.filter((m) => (matchFilter === "All" || m.status === matchFilter) && `${m.id} ${m.counterparty} ${m.reference}`.toLowerCase().includes(matchSearch.toLowerCase()));
  const filteredExceptions = EXCEPTIONS.filter((e) => exceptionFilter === "All" || e.type === exceptionFilter).filter((e) => !resolved.includes(e.id));

  const readFile = (key: string, file?: File) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setFileRows((current) => ({ ...current, [key]: Math.max(0, String(reader.result).trim().split(/\r?\n/).length - 1) }));
    reader.readAsText(file);
  };

  const downloadAudit = () => {
    const report = JSON.stringify({ batch_id: "BATCH-SEP-001", policy_version: "CC-R2.4", generated_at: "2026-09-01T04:12:31Z", metrics: { precision: .987, recall: .941, automation_coverage: .93, value_weighted_coverage: .968, false_approval_rate: .013, exception_recall: 1 }, controls: ["Decimal-only money calculations", "Verified proposals only", "Idempotent writes", "Ground truth isolated"] }, null, 2);
    const url = URL.createObjectURL(new Blob([report], { type: "application/json" }));
    const anchor = document.createElement("a"); anchor.href = url; anchor.download = "cashclose-audit-BATCH-SEP-001.json"; anchor.click(); URL.revokeObjectURL(url);
    setToast("Audit report downloaded");
  };

  return (
    <main className="app-shell">
      <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
        <div className="brand"><span className="brand-mark">C</span><span>cashclose</span><button className="close-mobile" aria-label="Close navigation" onClick={() => setMobileNav(false)}><X size={17}/></button></div>
        <button className="new-batch" onClick={() => setUploadOpen(true)}><Plus size={15}/> New close batch</button>
        <nav className="side-nav" aria-label="Primary navigation">
          {NAV.map(({ id, label, icon: Icon, count }) => <button key={id} className={`nav-item ${view === id ? "active" : ""}`} onClick={() => chooseView(id)}><Icon size={16} strokeWidth={1.8}/><span>{label}</span>{count && <em>{count}</em>}</button>)}
        </nav>
        <div className="batch-mini"><span className="batch-mini-title"><i/> BATCH-SEP-001</span><strong>Close complete</strong><div><span style={{ width: "100%" }}/></div><small>230 records · 8m 23s</small></div>
        <div className="sidebar-foot"><div className="org-avatar">NS</div><div><strong>Northstar Labs</strong><span>Asia/Kolkata · INR</span></div><ChevronDown size={14}/></div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open navigation"><Menu size={20}/></button>
          <div className="page-title"><p className="eyebrow">{PAGE_META[view].eyebrow}</p><h1>{PAGE_META[view].title}</h1><p className="page-subtitle">{PAGE_META[view].subtitle}</p></div>
          <div className="top-actions"><span className="live-pill"><i/> Systems healthy</span><button className="primary-action" onClick={startRun}><Play size={13} fill="currentColor"/> Run daily close</button></div>
        </header>

        {view === "overview" && <Overview onNavigate={chooseView} onRun={startRun} />}
        {view === "reconciliation" && <Reconciliation matches={filteredMatches} filter={matchFilter} setFilter={setMatchFilter} search={matchSearch} setSearch={setMatchSearch} openMatch={(match) => { setSelectedMatch(match); setMatchDrawer(true); }} />}
        {view === "exceptions" && <Exceptions exceptions={filteredExceptions} selected={selectedException} setSelected={setSelectedException} filter={exceptionFilter} setFilter={setExceptionFilter} resolve={(id) => { setResolved((items) => [...items, id]); setToast("Exception resolved and audit event recorded"); const next = EXCEPTIONS.find((e) => e.id !== id && !resolved.includes(e.id)); if (next) setSelectedException(next); }} />}
        {view === "forecast" && <Forecast scenario={scenarioApplied} openScenario={() => setScenarioOpen(true)} />}
        {view === "audit" && <Audit download={downloadAudit} />}
      </section>

      {uploadOpen && <UploadModal rows={fileRows} readFile={readFile} close={() => setUploadOpen(false)} run={startRun} />}
      {runOpen && <RunDrawer step={runStep} close={() => setRunOpen(false)} openResults={() => { setRunOpen(false); chooseView("overview"); }} />}
      {matchDrawer && <MatchDrawer match={selectedMatch} close={() => setMatchDrawer(false)} notify={(message) => { setToast(message); setMatchDrawer(false); }} />}
      {scenarioOpen && <ScenarioDrawer text={scenarioText} setText={setScenarioText} applied={scenarioApplied} close={() => setScenarioOpen(false)} apply={() => { setScenarioApplied(true); setToast("Scenario applied to forecast"); }} />}
      {toast && <div className="toast"><CheckCircle2 size={16}/>{toast}</div>}
    </main>
  );
}

function Overview({ onNavigate, onRun }: { onNavigate: (view: View) => void; onRun: () => void }) {
  return <div className="view-stack">
    <section className="metrics-row" aria-label="Close metrics">
      <article className="metric-card hero-metric"><div className="metric-label">Verified cash <span>01 Sep, 09:42</span></div><div className="metric-value">₹12.48<span>cr</span></div><p><b>↑ ₹84.2L</b> reconciled this run</p></article>
      <article className="metric-card"><div className="metric-label">Auto-reconciled <span className="mini-badge">HIGH</span></div><div className="metric-value">93<span>%</span></div><p>212 of 230 eligible records</p></article>
      <article className="metric-card"><div className="metric-label">Unresolved value</div><div className="metric-value">₹6.4<span>L</span></div><p className="warning-copy">17 exceptions need review</p></article>
      <article className="metric-card"><div className="metric-label">30-day minimum</div><div className="metric-value">₹2.18<span>cr</span></div><p>Risk-adjusted · 18 Sep</p></article>
    </section>
    <section className="overview-grid">
      <article className="panel forecast-overview">
        <div className="panel-heading"><div><p className="eyebrow">30-DAY OUTLOOK</p><h2>Cash stays positive—but narrows.</h2></div><button className="quiet-button" onClick={() => onNavigate("forecast")}>Open forecast <ArrowUpRight size={13}/></button></div>
        <ForecastChart compact />
        <div className="chart-legend"><span><i className="legend-confirmed"/>Confirmed</span><span><i className="legend-expected"/>Expected</span><span><i className="legend-risk"/>Risk-adjusted</span><strong>Shortfall risk <b>LOW</b></strong></div>
      </article>
      <article className="panel agent-overview">
        <div className="panel-heading"><div><p className="eyebrow">CONTROLLER AGENT</p><h2>Close is under control.</h2></div><span className="agent-state"><i/> Monitoring</span></div>
        <p className="agent-summary">Every safe proposal is committed. I’m holding 17 records where the evidence is not strong enough to approve.</p>
        <div className="activity-list">
          <div><span className="activity-icon done"><Check size={13}/></span><p><strong>93 matches verified</strong><small>References, amounts and identities agree</small></p><time>09:41</time></div>
          <div><span className="activity-icon alert"><AlertTriangle size={12}/></span><p><strong>17 exceptions explained</strong><small>5 require supporting documents</small></p><time>09:42</time></div>
          <div><span className="activity-icon"><Activity size={12}/></span><p><strong>Forecast recalculated</strong><small>Opening balance uses verified cash only</small></p><time>09:42</time></div>
        </div>
        <button className="soft-action" onClick={() => onNavigate("exceptions")}>Review 17 exceptions <ArrowRight size={14}/></button>
      </article>
    </section>
    <section className="lower-grid">
      <article className="panel featured-match">
        <div className="panel-heading"><div><p className="eyebrow">FEATURED MATCH · OR-TOOLS ALLOCATION</p><h2>One receipt. Two invoices. Zero guesswork.</h2></div><span className="confidence-ring">97.3%</span></div>
        <div className="allocation-flow"><div><span>Bank receipt</span><strong>₹8,42,500</strong><small>ACME PVT · BANK-0042</small></div><ArrowRight size={18}/><div className="invoice-stack"><span><b>INV-1831</b><strong>₹5,00,000</strong></span><span><b>INV-1834</b><strong>₹3,42,500</strong></span></div></div>
        <div className="evidence-line"><ShieldCheck size={14}/><span>Exact remittance references</span><span>Allocation sum verified</span><span>Approved customer alias</span></div>
        <button className="text-action" onClick={() => onNavigate("reconciliation")}>Inspect match evidence <ChevronRight size={14}/></button>
      </article>
      <article className="panel attention-panel"><p className="eyebrow">NEEDS ATTENTION</p><h2>Three decisions unlock ₹12.1L.</h2><div className="attention-list"><span><i className="sev high"/>Currency contradiction<b>₹5.12L</b></span><span><i className="sev medium"/>Possible bank fee<b>₹2.99L</b></span><span><i className="sev medium"/>Missing remittance<b>₹4.01L</b></span></div><button className="secondary-action" onClick={() => onNavigate("exceptions")}>Open exception inbox</button></article>
    </section>
    <button className="floating-run" onClick={onRun}><Zap size={15} fill="currentColor"/> Replay agent run</button>
  </div>;
}

function Reconciliation({ matches, filter, setFilter, search, setSearch, openMatch }: { matches: typeof MATCHES; filter: "All" | MatchStatus; setFilter: (v: "All" | MatchStatus) => void; search: string; setSearch: (v: string) => void; openMatch: (m: typeof MATCHES[number]) => void }) {
  return <div className="view-stack">
    <section className="summary-strip"><span><CheckCircle2 size={16}/> <b>₹8.42cr</b> reconciled</span><span><Gauge size={16}/> <b>98.7%</b> precision</span><span><Zap size={16}/> <b>212</b> automated</span><span><Clock3 size={16}/> <b>8m 23s</b> processing time</span></section>
    <section className="panel table-panel">
      <div className="table-toolbar"><div className="filter-tabs">{(["All","Auto-reconciled","Needs review","Exception"] as const).map((item) => <button className={filter === item ? "active" : ""} onClick={() => setFilter(item)} key={item}>{item}{item === "All" && <span>230</span>}</button>)}</div><label className="search-box"><Search size={14}/><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search transactions"/></label><button className="icon-button" aria-label="Filter matches"><Filter size={15}/></button></div>
      <div className="data-table match-table"><div className="table-row table-head"><span>Bank transaction</span><span>Proposed allocation</span><span>Evidence</span><span>Confidence</span><span>Status</span><span/></div>{matches.map((match) => <button className={`table-row ${match.complex ? "featured" : ""}`} key={match.id} onClick={() => openMatch(match)}><span className="transaction-cell"><i className="bank-icon"><CircleDollarSign size={15}/></i><span><strong>{match.counterparty}</strong><small>{match.id} · {match.date}</small></span><b>{match.amount}</b></span><span className="allocation-cell"><strong>{match.allocation}</strong><small>{match.reference}</small></span><span className="evidence-cell"><ShieldCheck size={14}/><small>{match.evidence}</small></span><span><b className={`score ${match.confidence < 75 ? "low" : match.confidence < 95 ? "medium" : ""}`}>{match.confidence.toFixed(1)}%</b></span><span><StatusPill status={match.status}/></span><span><ChevronRight size={15}/></span></button>)}</div>
      <div className="table-footer"><span>Showing {matches.length} of 230 transactions</span><div><button disabled>Previous</button><button>Next</button></div></div>
    </section>
  </div>;
}

function Exceptions({ exceptions, selected, setSelected, filter, setFilter, resolve }: { exceptions: typeof EXCEPTIONS; selected: typeof EXCEPTIONS[number]; setSelected: (e: typeof EXCEPTIONS[number]) => void; filter: string; setFilter: (s: string) => void; resolve: (id: string) => void }) {
  const types = ["All", "Ambiguous match", "Currency mismatch", "Suspected fee", "Duplicate invoice", "Missing invoice"];
  return <div className="exception-layout">
    <section className="panel exception-list-panel"><div className="exception-list-head"><div><strong>{exceptions.length || 0} open exceptions</strong><small>₹6.42L unresolved value</small></div><button className="icon-button"><SlidersHorizontal size={15}/></button></div><div className="exception-filters">{types.map((type) => <button key={type} className={filter === type ? "active" : ""} onClick={() => setFilter(type)}>{type}</button>)}</div><div className="exception-list">{exceptions.length ? exceptions.map((exception) => <button key={exception.id} className={selected.id === exception.id ? "active" : ""} onClick={() => setSelected(exception)}><div className="exception-type"><span><i className={`sev ${exception.severity.toLowerCase()}`}/>{exception.type}</span><time>{exception.age}</time></div><strong>{exception.title}</strong><div className="exception-meta"><span>{exception.party}</span><b>{exception.amount}</b></div></button>) : <div className="empty-state"><CheckCircle2 size={28}/><strong>Queue cleared</strong><span>All visible exceptions have been resolved.</span></div>}</div></section>
    <section className="panel exception-detail"><div className="detail-top"><div><span className="record-label">{selected.id} · {selected.record}</span><h2>{selected.title}</h2><p>{selected.party} · {selected.amount}</p></div><span className="hard-stop"><LockKeyhole size={12}/> AUTO-WRITE BLOCKED</span></div><div className="explain-card"><span className="explain-icon"><Bot size={18}/></span><div><p className="eyebrow">CONTROLLER EXPLANATION</p><strong>Why I stopped</strong><p>{selected.reason}</p></div></div><div className="evidence-section"><h3>Evidence considered</h3><div className="evidence-chips">{selected.chips.map((chip) => <span key={chip}><Check size={12}/>{chip}</span>)}</div><div className="candidate-compare"><div><small>CANDIDATE A</small><strong>INV-2841 · ₹7,55,000</strong><span>Issue date 02 Aug · Due 31 Aug</span><b>76.4% fit</b></div><div><small>CANDIDATE B</small><strong>INV-2844 · ₹7,55,000</strong><span>Issue date 04 Aug · Due 31 Aug</span><b>75.9% fit</b></div></div></div><div className="next-action"><div><span><ArrowRight size={14}/></span><p><small>RECOMMENDED NEXT ACTION</small><strong>{selected.action}</strong></p></div><button className="secondary-action">Request document</button></div><div className="detail-actions"><button className="quiet-button"><XCircle size={14}/> Mark unresolved</button><button className="primary-action" onClick={() => resolve(selected.id)}><CheckCircle2 size={14}/> Resolve with evidence</button></div></section>
  </div>;
}

function Forecast({ scenario, openScenario }: { scenario: boolean; openScenario: () => void }) {
  return <div className="view-stack">
    {scenario && <div className="scenario-banner"><Sparkles size={16}/><span><strong>Scenario active:</strong> Acme pays seven days late</span><b>Projected minimum changes to ₹0.72cr</b><button onClick={openScenario}>Inspect</button></div>}
    <section className="forecast-layout"><article className="panel forecast-main"><div className="panel-heading"><div className="forecast-balance"><p className="eyebrow">RISK-ADJUSTED CLOSING CASH</p><strong>{scenario ? "₹8.61cr" : "₹8.04cr"}</strong><span>30 Sep 2026</span></div><div className="forecast-actions"><button className="quiet-button"><CalendarDays size={13}/> 30 days</button><button className="primary-action" onClick={openScenario}><Sparkles size={13}/> Ask a scenario</button></div></div><ForecastChart scenario={scenario}/><div className="chart-legend large"><span><i className="legend-confirmed"/>Confirmed</span><span><i className="legend-expected"/>Expected</span><span><i className="legend-risk"/>Risk-adjusted</span><span><i className="legend-threshold"/>₹3cr threshold</span><strong>Model: Monte Carlo · 1,000 runs</strong></div></article><aside className="forecast-side"><article className="panel low-card"><p className="eyebrow">PROJECTED LOW</p><strong>{scenario ? "₹0.72cr" : "₹2.18cr"}</strong><span>18 September</span><div className={scenario ? "risk-level high" : "risk-level"}><i/>{scenario ? "High shortfall risk" : "Low shortfall risk"}</div></article><article className="panel drivers-card"><div className="panel-heading"><h3>Largest movements</h3><button>View all</button></div><div className="driver-list"><div><span className="driver-icon in">↗</span><p><strong>Acme receivable</strong><small>Expected · 20 Sep</small></p><b className="positive">+₹2.35cr</b></div><div><span className="driver-icon out">↘</span><p><strong>September payroll</strong><small>Confirmed · 15 Sep</small></p><b>−₹1.82cr</b></div><div><span className="driver-icon out">↘</span><p><strong>GST &amp; advance tax</strong><small>Confirmed · 18 Sep</small></p><b>−₹1.36cr</b></div><div><span className="driver-icon in">↗</span><p><strong>Novus receivables</strong><small>82% on-time probability</small></p><b className="positive">+₹0.94cr</b></div></div></article></aside></section>
    <section className="scenario-cards"><button onClick={openScenario}><span><Clock3 size={16}/></span><div><strong>Customer pays late</strong><small>Shift an expected receivable</small></div><ChevronRight size={15}/></button><button onClick={openScenario}><span><CircleDollarSign size={16}/></span><div><strong>Delay a payable</strong><small>Test a vendor negotiation</small></div><ChevronRight size={15}/></button><button onClick={openScenario}><span><Plus size={16}/></span><div><strong>Add one-time expense</strong><small>Model an unplanned outflow</small></div><ChevronRight size={15}/></button></section>
  </div>;
}

function Audit({ download }: { download: () => void }) {
  const metrics = [{ label:"Match precision", value:"98.7%", note:"↑ 2.1% vs baseline", tone:"good" },{ label:"Match recall", value:"94.1%", note:"201 / 214 true matches", tone:"good" },{ label:"Automation coverage", value:"93.0%", note:"212 / 228 eligible", tone:"good" },{ label:"False approval rate", value:"1.3%", note:"Below 2% policy limit", tone:"good" },{ label:"Exception recall", value:"100%", note:"All unsafe cases caught", tone:"good" },{ label:"Forecast MAE", value:"₹8.4L", note:"6.8% of mean cash", tone:"neutral" }];
  return <div className="view-stack"><section className="audit-hero panel"><div><span className="audit-seal"><ShieldCheck size={26}/></span><div><p className="eyebrow">BATCH-SEP-001 · POLICY CC-R2.4</p><h2>Controller run passed every hard control.</h2><p>Ground truth was isolated until the run completed. Metrics were computed independently from committed decisions.</p></div></div><button className="primary-action" onClick={download}><Download size={14}/> Download audit report</button></section><section className="audit-score-grid">{metrics.map((metric) => <article className="panel score-card" key={metric.label}><p>{metric.label}</p><strong>{metric.value}</strong><span className={metric.tone}>{metric.note}</span><div><i style={{ width: metric.value.includes("₹") ? "82%" : metric.value }}/></div></article>)}</section><section className="audit-grid"><article className="panel controls-card"><div className="panel-heading"><div><p className="eyebrow">CONTROL EVIDENCE</p><h2>Safeguards enforced</h2></div><span className="passed-pill"><Check size={12}/> 8 / 8 passed</span></div><div className="control-list">{[["Decimal-safe arithmetic","All stored and computed values use fixed precision"],["Idempotent financial writes","No proposal or allocation can be committed twice"],["Verification before commit","93 automatic decisions carried approval evidence"],["Hard contradiction stop","Currency and duplicate conflicts were forced to exception"],["Ground-truth isolation","Evaluator artifacts were inaccessible during execution"]].map(([a,b]) => <div key={a}><CheckCircle2 size={16}/><p><strong>{a}</strong><small>{b}</small></p><span>PASS</span></div>)}</div></article><article className="panel event-card"><div className="panel-heading"><div><p className="eyebrow">AUDIT TRAIL</p><h2>Recent control events</h2></div><History size={17}/></div><div className="audit-events">{RUN_EVENTS.slice(3).map((event) => <div key={event.tool}><time>{event.time}</time><span className="event-dot"/><p><strong>{event.text}</strong><small>{event.tool} · succeeded</small></p></div>)}</div><button className="text-action">View all 486 events <ChevronRight size={14}/></button></article></section></div>;
}

function UploadModal({ rows, readFile, close, run }: { rows: Record<string, number>; readFile: (key: string, file?: File) => void; close: () => void; run: () => void }) {
  return <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Create a close batch"><section className="modal upload-modal"><div className="modal-head"><div><p className="eyebrow">NEW CLOSE BATCH</p><h2>Bring the records. We’ll establish the truth.</h2><p>Upload the four source files. Schemas are validated before any financial action.</p></div><button className="icon-button" onClick={close} aria-label="Close upload"><X size={17}/></button></div><div className="upload-grid">{FILES.map((file) => <label className="upload-card" key={file.key}><input type="file" accept=".csv,text/csv" onChange={(e) => readFile(file.key, e.target.files?.[0])}/><span className="file-icon"><FileSpreadsheet size={20}/></span><div><strong>{file.label}</strong><small>{file.hint}</small></div><span className="file-ready"><Check size={12}/>{rows[file.key]} rows ready</span><UploadCloud size={16}/></label>)}</div><div className="validation-callout"><ShieldCheck size={18}/><div><strong>Preflight checks passed</strong><span>290 financial records + 40 remittances · 3 currency warnings will be quarantined</span></div><button>View schema report</button></div><div className="modal-actions"><button className="quiet-button" onClick={close}>Cancel</button><button className="primary-action" onClick={run}><Play size={13} fill="currentColor"/> Start controller</button></div></section></div>;
}

function RunDrawer({ step, close, openResults }: { step: number; close: () => void; openResults: () => void }) {
  const complete = step >= RUN_EVENTS.length;
  return <div className="drawer-backdrop"><aside className="run-drawer"><div className="drawer-head"><div><span className={`run-orb ${complete ? "complete" : ""}`}>{complete ? <Check size={18}/> : <Bot size={18}/>}</span><div><p className="eyebrow">CONTROLLER RUN · BATCH-SEP-001</p><h2>{complete ? "Close complete" : "Establishing the cash truth"}</h2></div></div><button className="icon-button" onClick={close}><X size={17}/></button></div><div className="run-progress"><div><span style={{ width: `${Math.min(100, Math.max(4, step / RUN_EVENTS.length * 100))}%` }}/></div><p><span>{complete ? "8 of 8 stages complete" : `${Math.min(step + 1, 8)} of 8 stages`}</span><b>{complete ? "100%" : `${Math.round(step / RUN_EVENTS.length * 100)}%`}</b></p></div><div className="run-guardrail"><LockKeyhole size={14}/><p><strong>Deterministic money boundary active</strong><small>The agent chooses tools. Code controls amounts, constraints, writes, and metrics.</small></p></div><div className="run-timeline">{RUN_EVENTS.map((event, index) => <div className={`${index < step ? "done" : index === step ? "active" : "pending"}`} key={event.tool}><span className="timeline-mark">{index < step || complete ? <Check size={12}/> : index === step ? <RefreshCw size={12}/> : index + 1}</span><div><span className="tool-name">{event.tool}</span><strong>{event.text}</strong><small>{event.detail}</small></div><time>{index <= step || complete ? event.time : "—"}</time></div>)}</div>{complete && <div className="run-result"><div><span>Verified cash<strong>₹12.48cr</strong></span><span>Auto-approved<strong>93 matches</strong></span><span>Exceptions<strong>17 explained</strong></span></div><button className="primary-action" onClick={openResults}>Open controller report <ArrowRight size={14}/></button></div>}</aside></div>;
}

function MatchDrawer({ match, close, notify }: { match: typeof MATCHES[number]; close: () => void; notify: (m: string) => void }) {
  return <div className="drawer-backdrop"><aside className="detail-drawer"><div className="drawer-head"><div><p className="eyebrow">MATCH PROPOSAL · MP-0042</p><h2>{match.counterparty}</h2><span>{match.id} · {match.date} · {match.amount}</span></div><button className="icon-button" onClick={close}><X size={17}/></button></div><div className="match-confidence"><div><span>Verified confidence</span><strong>{match.confidence.toFixed(1)}%</strong></div><StatusPill status={match.status}/></div><section className="drawer-section"><h3>Bank transaction</h3><div className="bank-record"><span className="bank-icon big"><CircleDollarSign size={19}/></span><div><strong>{match.amount}</strong><span>INR · Credit · Value date 01 Sep 2026</span><code>{match.reference}</code></div></div></section><section className="drawer-section"><h3>Proposed allocation</h3><div className="allocation-details"><div><span>INV-1831</span><small>Professional services · due 30 Aug</small><strong>₹5,00,000.00</strong></div><div><span>INV-1834</span><small>Platform subscription · due 31 Aug</small><strong>₹3,42,500.00</strong></div><footer><span>Allocation total</span><strong>₹8,42,500.00</strong></footer></div></section><section className="drawer-section"><h3>Why this is safe</h3><div className="proof-list"><span><CheckCircle2 size={15}/><p><strong>Exact remittance references</strong><small>1831 and 1834 found in bank text</small></p></span><span><CheckCircle2 size={15}/><p><strong>Allocation equation verified</strong><small>₹5,00,000 + ₹3,42,500 = ₹8,42,500</small></p></span><span><CheckCircle2 size={15}/><p><strong>Approved identity alias</strong><small>“ACME PVT” resolves to Acme Private Limited</small></p></span><span><CheckCircle2 size={15}/><p><strong>No contradictions or reuse</strong><small>INR matches · both invoices remain open</small></p></span></div></section><div className="policy-note"><LockKeyhole size={13}/> Verified by CC-R2.4 · deterministic write only · idempotency key present</div><div className="drawer-actions"><button className="danger-button" onClick={() => notify("Match rejected and moved to exceptions")}><XCircle size={14}/> Reject</button><button className="quiet-button">Edit allocation</button><button className="primary-action" onClick={() => notify("Match approved and audit event recorded")}><Check size={14}/> Approve match</button></div></aside></div>;
}

function ScenarioDrawer({ text, setText, applied, close, apply }: { text: string; setText: (v:string) => void; applied: boolean; close: () => void; apply: () => void }) {
  return <div className="drawer-backdrop"><aside className="scenario-drawer"><div className="drawer-head"><div><span className="run-orb"><Sparkles size={17}/></span><div><p className="eyebrow">SCENARIO LAB</p><h2>Ask “what if?”</h2></div></div><button className="icon-button" onClick={close}><X size={17}/></button></div><p className="scenario-intro">Describe a cash event. The controller converts it into validated inputs, then the forecast engine recomputes every daily balance.</p><div className="suggestion-row"><button onClick={() => setText("What happens if Acme pays seven days late?")}>Acme pays 7 days late</button><button onClick={() => setText("Delay GST payment by three days")}>Delay GST by 3 days</button></div><div className="scenario-input"><textarea value={text} onChange={(e) => setText(e.target.value)} aria-label="Cash scenario"/><button aria-label="Run scenario" onClick={apply}><Send size={15}/></button></div><div className="scenario-response"><div className="scenario-agent"><Bot size={15}/><span>Controller analysis</span></div><p>Delaying Acme’s ₹2.35cr receipt shifts the risk-adjusted minimum from <strong>₹2.18cr</strong> to <strong>₹0.72cr</strong> on 18 September.</p><div className="mini-impact"><span><small>Minimum cash</small><strong className="negative">−₹1.46cr</strong></span><span><small>Days below threshold</small><strong>4 days</strong></span><span><small>P10 minimum</small><strong>₹0.35cr</strong></span></div><div className="recommendation"><Zap size={15}/><p><small>SAFEST CORRECTIVE ACTION</small><strong>Move ₹1.2cr of non-critical vendor payments to 23 Sep.</strong><span>This restores ₹3.08cr headroom without affecting payroll or tax obligations.</span></p></div></div><div className="drawer-actions"><button className="quiet-button" onClick={close}>Cancel</button><button className="primary-action" onClick={apply}>{applied ? <RefreshCw size={14}/> : <Play size={13}/>} {applied ? "Recalculate scenario" : "Apply to forecast"}</button></div></aside></div>;
}
