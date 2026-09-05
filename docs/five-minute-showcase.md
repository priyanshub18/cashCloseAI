# CashClose AI — five-minute live demo script

This demo is intentionally product-first. Keep the application visible, use the
floating guide as presenter notes, and click through the real controls. Open
`http://localhost:3000/?showcase=1` or choose **Launch demo guide** in the
sidebar. The eight chapters total exactly 5:00.

## Before you present

1. Start the web workspace and API. The interactive preview still works if the
   API is unavailable, but a connected run is stronger evidence.
2. Use a 1440×900 or 1920×1080 browser window at 100% zoom.
3. If an OpenAI key is configured, run one controller batch first so the trace
   contains model-planning provenance. Otherwise present the validated controller
   workflow and do not imply that a model call occurred.
4. Open the guide, press **Start 5:00**, and advance manually if the audience
   asks questions. **Record 5:00** records the full browser tab—the working
   application stays visible throughout.
5. Never show environment variables, credentials, or private ground-truth files.

## Presenter script

### 0:00–0:25 — Controller

**On screen:** Controller overview.

> This is CashClose, a finance operations workspace that turns bank activity,
> invoices, remittances, and ledger records into a verified cash position. The
> controller has reconciled the safe items, isolated the ambiguous ones, and
> rebuilt the thirty-day outlook from committed records.

Point to verified value, match precision, unresolved value, and the thirty-day
cash minimum. Say: “These are workspace values—not presentation placeholders.”

### 0:25–1:00 — Run trace

**On screen:** Agent trace. Select one transaction and move across its stages.

> The agent trace is the operational record of the close. At the batch level,
> the controller can use two bounded model turns to choose observations and
> strategy. At the transaction level, every normalization, candidate search,
> allocation, evidence check, verification, and terminal decision is recorded
> with its tool, status, and latency.

Emphasize that this is an observable tool trace, not hidden chain of thought.
Only mention model planning when provenance appears on screen.

### 1:00–1:55 — Match evidence

**On screen:** Reconciliation. Click **Open example match** in the guide.

> This is the working reconciliation queue. Status, confidence, amount,
> counterparty, and allocation state can all be filtered and searched. This
> receipt is allocated across two invoices. The solver balances exact decimal
> amounts, while policy prevents invoice reuse, cross-currency matching, or a
> write without verifier approval.

In the evidence drawer, point to the bank transaction, invoice allocations,
evidence ledger, policy confidence, and committed status. Close the drawer
before advancing.

### 1:55–2:35 — Exception review

**On screen:** Exceptions. Click **Open example exception**.

> Unsafe automation is worse than a visible exception. Currency conflicts,
> duplicates, missing remittances, and ambiguous candidates are forced into this
> queue. The reviewer gets the reason, evidence, amount at risk, and a
> recommended action. The system never silently converts uncertainty into a
> match.

Show the currency-mismatch evidence. Click **Request review** to demonstrate
that the preview is interactive. Explain that a documented resolution is
required before an exception can be closed.

### 2:35–3:25 — Forecast scenario

**On screen:** Cash forecast. Click **Run a scenario**.

> Reconciliation now becomes a decision tool. Confirmed, expected, and
> risk-adjusted cash all start from verified opening cash. I can delay a customer
> receipt, move a payable, or add a one-time outflow. CashClose recalculates every
> daily position and surfaces the new minimum and shortfall date.

Keep the default “Acme pays seven days late” scenario and click **Calculate
scenario**. Point to the active-scenario banner and the changed low point. Say:
“The model may explain this result; the finance core calculates it.”

### 3:25–4:00 — Audit proof

**On screen:** Audit & evaluation.

> The quality claim is measurable. A separate evaluator, isolated from the
> agent, compares decisions with private ground truth. This screen reports
> precision, recall, automation coverage, exception recall, false approvals,
> forecast error, and the append-only action trail.

Point to false-approval rate and the ground-truth boundary. Mention that
**Download report** exports structured JSON suitable for review.

### 4:00–4:40 — Infrastructure

**On screen:** System design.

> The web workspace talks to a FastAPI control plane through typed endpoints and
> server-sent events. Agent choices are constrained by validated tool contracts.
> The Python finance core owns normalization, decimal arithmetic, allocation,
> verification, forecasting, and audit writes.

Trace the top row from validated sources to output.

> This demo uses isolated in-memory batch state. The production seam replaces
> that with Postgres, object storage, and Redis-backed workers. Financial
> authority stays in the validated finance core in both versions.

Use **Check runtime** to refresh connectivity and **Copy summary** if a judge
wants the architecture in text.

### 4:40–5:00 — Close

**On screen:** Controller overview.

> CashClose is not a dashboard mockup and it is not an autonomous payment bot.
> It is a working close controller: ingest, reconcile, abstain safely, forecast,
> and prove every decision. The result is faster finance operations without
> giving probabilistic software authority over the money.

Finish with the three outputs: reconciled value, explained exceptions, and a
decision-ready forecast.

## Claims boundary

- A model makes two batch-level Responses calls only in Agentic Responses mode.
- Reconciliation, verification, and forecasting are bounded logical roles
  behind one controller, not an uncontrolled agent swarm.
- Decimal calculations, allocation constraints, verification, commits,
  forecasting, and metrics remain owned by the validated finance core.
- Current demo state is held in the API process. Postgres, Storage, and
  Redis/RQ are the production target and are labeled as such in the interface.
- Private ground truth is available only to the evaluator.
- CashClose never initiates payments.
