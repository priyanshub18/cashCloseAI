import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("https://cashclose.example/", { headers: { accept: "text/html", host: "cashclose.example", "x-forwarded-host": "cashclose.example", "x-forwarded-proto": "https" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the CashClose controller", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<title>CashClose AI — Verified cash, explained<\/title>/i);
  assert.match(html, /Cash position, verified\./);
  assert.match(html, /One receipt\. Two invoices\. Zero guesswork\./);
  assert.match(html, /Run controller/);
  assert.match(html, /Interactive truth-set preview/);
  assert.match(html, /The close is explainable/);
  assert.match(html, /https:\/\/cashclose\.example\/og-v2\.jpg/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("ships the product asset and removes starter preview code", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  const app = await readFile(new URL("../components/cashclose/CashCloseApp.tsx", import.meta.url), "utf8");
  assert.match(app, /ReconciliationView/);
  assert.match(app, /ScenarioPanel/);
  assert.match(app, /runUploadedBatch/);
  assert.match(app, /approveProposal/);
  assert.match(app, /resolveException/);
  assert.match(app, /downloadAudit/);
  assert.match(app, /Agentic Responses/);
  assert.match(app, /OPENAI_API_KEY is not loaded by this API/);
  assert.match(app, /onRefreshCapabilities/);
  assert.match(app, /use_model_planner: useModelPlanner/);
  assert.match(app, /Launch demo guide/);
  assert.match(app, /ProductShowcase/);
  assert.match(app, /InfrastructureView/);
  assert.match(app, /Built like a finance system, not a chatbot/);
  assert.doesNotMatch(app, /Deterministic demo/i);
  assert.doesNotMatch(app, /Deterministic controller/i);
  assert.match(app, /SHOWCASE_TOTAL_SECONDS/);
  assert.match(app, /Record 5:00/);
  assert.match(app, /navigator\.mediaDevices\.getDisplayMedia/);
  assert.match(app, /cashclose-ai-five-minute-showcase/);
  const showcaseDurations = [...app.matchAll(/durationSeconds: (\d+),/g)].map((match) => Number(match[1]));
  assert.equal(showcaseDurations.reduce((total, seconds) => total + seconds, 0), 300);
  assert.match(app, /two bounded model turns/i);
  assert.match(app, /isolated in-memory batch state/i);
  assert.match(app, /Exact daily cash positions/);
  assert.match(page, /CashCloseApp/);
  assert.match(layout, /openGraph/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
  await access(new URL("../public/og-v2.jpg", import.meta.url));
  await assert.rejects(access(new URL("app/_sites-preview/SkeletonPreview.tsx", root)));
});
