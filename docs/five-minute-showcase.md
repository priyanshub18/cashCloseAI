# CashClose AI — five-minute showcase

Open `http://localhost:3000/?showcase=1`. The guided experience contains ten
timed chapters totaling exactly 300 seconds. Click **Narration** if browser
speech should read the presenter track, or leave it off when recording a human
voiceover. Click **Record 5:00**, choose the CashClose browser tab, and enable
tab audio when narration should be captured. The tour restarts automatically
and downloads a WebM when the five minutes finish.

| Time | Chapter | Product proof |
| --- | --- | --- |
| 0:00–0:25 | The outcome | Verified value, precision, and the 30-day cash low |
| 0:25–0:50 | Why it matters | Fragmented sources, unsafe automation, and forecast risk |
| 0:50–1:30 | Architecture | One controller, three logical specialists, deterministic authority |
| 1:30–1:55 | Truth layer | Four CSV schemas, currency/Decimal checks, isolated ground truth |
| 1:55–2:35 | Agent planning | Two bounded Responses turns, selected tools, strategy, response IDs |
| 2:35–3:20 | Reconciliation | One payment through seven gates and a combined allocation |
| 3:20–3:45 | Safe abstention | Evidence, hard contradiction, and the human next action |
| 3:45–4:25 | Forecast | Confirmed, expected, risk-adjusted cash, and scenario control |
| 4:25–4:45 | Proof | Precision, recall, coverage, forecast error, and audit entries |
| 4:45–5:00 | The close | Agentic judgment with deterministic financial control |

## Recording preflight

1. Confirm Docker UI and API are healthy.
2. Confirm `/api/capabilities` reports `responses_mode_configured: true` if the
   recording will claim a real model-guided run.
3. Run a fresh **Agentic Responses** demo batch before opening the showcase so
   structured model provenance and current live metrics are loaded.
4. Use a 1440×900 or 1920×1080 browser window at 100% zoom.
5. Open `/?showcase=1`, enable Narration if wanted, then click **Record 5:00**
   and select the CashClose tab. The in-product recorder restarts chapter one.
6. Do not display `.env`, API keys, Docker configuration, or private ground
   truth in the recording.

## Truth boundary for the presenter

- The model makes exactly two batch-level Responses calls. It does not make one
  LLM call per transaction.
- Reconciliation, verification, and forecast “agents” are bounded logical
  roles behind one controller, not an uncontrolled model swarm.
- Decimal calculations, allocation constraints, verification, commits,
  Monte Carlo sampling, and metrics are deterministic code.
- The current hackathon service stores batch state in API process memory.
  PostgreSQL/Supabase and Redis/RQ are the production persistence and worker
  target, not a capability the demo should claim is already active.
- P10–P90 appears only when the selected forecast actually contains a Monte
  Carlo band.

## Optional live interaction cut

For a less scripted recording, use the normal workspace and show this sequence:

1. **New close batch → Agentic Responses → Run with Responses**.
2. Watch model planning start and complete in the live run drawer.
3. Open **Agent trace**, select a combined payment, and step through Candidates,
   Allocation, Evidence, Verify, and Commit.
4. Open a currency-mismatch exception and request human review.
5. Run the “Acme pays seven days late” receipt-delay scenario.
6. Finish on **Audit & evaluation** and download the report.

Only call the run “agentic” when the interface shows the Responses-guided mode
and structured planning provenance for that batch.
