# CashClose AI

CashClose AI is an agentic reconciliation controller that establishes verified
cash, forecasts the next 30 days, and explains every exception.

> The controller decides what to investigate and which approved tool to use.
> Deterministic code owns money calculations, allocation constraints, writes,
> verification, and evaluation metrics.

The repository contains a polished interactive hackathon demo, a runnable
FastAPI service, 30 strict agent-tool contracts, a Decimal-safe finance engine,
a PostgreSQL/Supabase schema, and a fixed-seed evaluation dataset with isolated
ground truth.

## What is working

- Controller dashboard with verified cash, automation, unresolved value, and
  30-day minimum cash
- Reconciliation workspace with evidence, confidence, one-to-many allocation,
  approval, rejection, and edit controls
- Exception inbox that explains why automatic writes were blocked
- Confirmed, expected, and risk-adjusted forecast lines using Recharts
- Interactive “Acme pays seven days late” scenario and corrective action
- Replayable controller timeline that exposes tool actions—not hidden reasoning
- Audit/evaluation scorecard and downloadable audit JSON
- CSV upload preflight experience for bank, invoice, ledger, and remittance data
- Full requested REST API and Server-Sent Events endpoint
- Optional Responses API planner using `gpt-5.6-terra`; no key is required for
  the deterministic demo
- Pydantic validation around every tool input and result
- Idempotent commit, verifier approval, currency checks, invoice-reuse
  prevention, and hard-risk exception routing
- Synthetic 335-row visible dataset plus evaluator-only truth artifacts

## Repository map

```text
app/                         Interactive Next.js/vinext controller UI
apps/api/                    FastAPI routes and deterministic demo service
apps/worker/                 Redis/RQ worker entry points
packages/agents/             Controller, verifier, prompts, tool schemas
packages/finance/            Matching, scoring, allocation, cash, forecast
packages/evaluation/         Independent financial metrics and report builder
packages/synthetic_data/     Fixed-seed generator and scenario contract
migrations/                  PostgreSQL/Supabase truth-layer schema
demo_data/input/             Agent-visible CSVs
demo_data/private_ground_truth/  Evaluator-only expected outputs
tests/                       UI render, unit, integration, and eval tests
```

## Quick start

### Interactive dashboard

```bash
npm install
npm run dev
```

Open `http://localhost:3000`. All interface paths work without credentials.

### API

```bash
python3 -m pip install -e '.[dev]'
python3 -m apps.api
```

The OpenAPI UI is available at `http://localhost:8000/docs`.

Create and run a deterministic demo batch:

```bash
curl -X POST http://localhost:8000/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"organization_id":"ORG-DEMO","accounting_timezone":"Asia/Kolkata","as_of_date":"2026-09-01","demo_mode":true}'

curl -X POST http://localhost:8000/api/batches/BATCH-0001/run \
  -H 'Content-Type: application/json' \
  -d '{"horizon_days":30,"use_model_planner":false}'
```

Set `OPENAI_API_KEY` and send `"use_model_planner": true` only when you want
the optional Responses planner. The adapter defaults to `gpt-5.6-terra`, uses
strict function schemas, disables parallel financial calls, and cannot access
evaluator ground truth. Confirm model access for the target OpenAI account.

Official references: [GPT-5.6 model guidance](https://developers.openai.com/api/docs/guides/latest-model),
[GPT-5.6 Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), and
[Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create).

### Local infrastructure

```bash
docker compose up --build
```

This starts the production web workspace at `http://localhost:3000`, the FastAPI service
and OpenAPI docs at `http://localhost:8000` and `http://localhost:8000/docs`,
PostgreSQL on `localhost:54322`, and Redis on `localhost:6379`. Apply
`migrations/0001_cashclose_truth_layer.sql` through Supabase or `psql` before
connecting a persistent repository implementation.

## Synthetic truth layer

Regenerate the exact demo with seed `20260901`:

```bash
npm run generate:demo
```

Generated source records live in `demo_data/input/`. Expected matches,
exceptions, and future actual cash live separately in
`demo_data/private_ground_truth/`. Do not mount that private directory into an
agent or API runtime; only the independent evaluator should read it.

The generator plants exact matches, reference variations, customer aliases,
partial payments, combined payments, duplicates, fee deductions, overpayments,
currency mismatches, missing remittances, ambiguity, and unreconcilable records.

## Tests

```bash
npm test
node --test tests/cashclose-client.test.ts
python3 -m pytest -q
```

The frontend gates include the production build, server render, typed API
client, SSE fallback, exact-money helpers, manual review requests, and audit
downloads. The Python suite covers deterministic arithmetic, matching and
allocation, forecasting, tool guardrails, idempotency, API isolation,
synthetic scenario counts, and evaluator metrics.

## Financial safety boundary

- Amounts use `Decimal`; every amount carries a currency.
- Models never execute SQL or mutate rows directly.
- `commit_match` requires verifier approval and an idempotency key.
- Committed transactions and invoices cannot be reused.
- Solver and evidence provenance are mandatory for automatic matches.
- Any hard contradiction creates an exception regardless of confidence.
- Forecast opening cash uses verified/committed values only.
- Ground truth is absent from the agent-visible tool registry.
- Controller tool calls and retries are bounded.
- This project never initiates payments.

The in-memory service is deliberately replaceable by a PostgreSQL/Supabase
repository without changing the agent-tool boundary. Production deployment
still needs organization auth, row-level security policies, Storage wiring,
secret management, and calibration against real historical data.
