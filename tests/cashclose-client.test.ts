import assert from "node:assert/strict";
import test from "node:test";

import {
  CashCloseApiError,
  createCashCloseClient,
  parseBatchEventStreamText,
} from "../lib/cashclose-client.ts";
import {
  addMoney,
  compareMoney,
  formatMoney,
  money,
  moneyFromMinorUnits,
  moneyToMinorUnits,
  subtractMoney,
} from "../lib/money.ts";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

const batchView = {
  batch_id: "BATCH-0001",
  organization_id: "ORG-DEMO",
  status: "UPLOADED",
  accounting_timezone: "Asia/Kolkata",
  as_of_date: "2026-09-01",
  demo_mode: true,
  files: [],
  created_at: "2026-09-01T00:00:00Z",
  updated_at: "2026-09-01T00:00:00Z",
  terminal: false,
};

test("money helpers never route values through floating point", () => {
  assert.equal(money("00042.5"), "42.50");
  assert.equal(money("-0.00"), "0.00");
  assert.equal(moneyToMinorUnits(money("999999999999999999.99")), BigInt("99999999999999999999"));
  assert.equal(moneyFromMinorUnits(BigInt(-12345)), "-123.45");
  assert.equal(addMoney("0.10", "0.20"), "0.30");
  assert.equal(subtractMoney("100.00", "0.01"), "99.99");
  assert.equal(compareMoney("3.00", "2.99"), 1);
  assert.equal(formatMoney("1234567.80", "INR"), "INR 1,234,567.80");
  assert.throws(() => money("1e6"), /Invalid money value/);
  assert.throws(() => money("12.345"), /Invalid money value/);
});

test("client uses configured origin and preserves request/response money strings", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const client = createCashCloseClient({
    baseUrl: "https://api.example.test/",
    fetch: async (input, init) => {
      calls.push({ url: String(input), init });
      return jsonResponse(batchView, 201);
    },
  });

  const batch = await client.createDemoBatch({ as_of_date: "2026-09-01" });

  assert.equal(batch.batch_id, "BATCH-0001");
  assert.equal(calls[0]?.url, "https://api.example.test/api/batches");
  assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)), {
    as_of_date: "2026-09-01",
    demo_mode: true,
  });
  assert.equal(new Headers(calls[0]?.init?.headers).get("content-type"), "application/json");
});

test("domain errors and FastAPI validation errors are structured", async () => {
  const responses = [
    jsonResponse({ error: { code: "CONFLICT", message: "batch already ran" } }, 409),
    jsonResponse(
      {
        detail: [
          { loc: ["body", "amount"], msg: "Field required", type: "missing" },
        ],
      },
      422,
    ),
  ];
  const client = createCashCloseClient({
    baseUrl: "https://api.example.test",
    fetch: async () => responses.shift() as Response,
  });

  await assert.rejects(client.runBatch("BATCH-1"), (error: unknown) => {
    assert.ok(error instanceof CashCloseApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, "CONFLICT");
    assert.equal(error.retryable, false);
    return true;
  });
  await assert.rejects(client.createBatch(), (error: unknown) => {
    assert.ok(error instanceof CashCloseApiError);
    assert.equal(error.code, "VALIDATION_ERROR");
    assert.match(error.message, /body\.amount: Field required/);
    return true;
  });
});

test("CSV upload sends the exact multipart field names", async () => {
  let capturedForm: FormData | undefined;
  const client = createCashCloseClient({
    baseUrl: "https://api.example.test",
    fetch: async (_input, init) => {
      assert.ok(init?.body instanceof FormData);
      capturedForm = init.body;
      return jsonResponse(
        {
          file_id: "FILE-1",
          batch_id: "BATCH-1",
          file_type: "invoices",
          filename: "invoices.csv",
          content_type: "text/csv",
          size_bytes: 20,
          row_count: 1,
          columns: ["invoice_id"],
          uploaded_at: "2026-09-01T00:00:00Z",
          validation_issues: [],
        },
        201,
      );
    },
  });

  await client.uploadBatchFile("BATCH-1", {
    fileType: "invoices",
    file: new Blob(["invoice_id\nINV-1\n"], { type: "text/csv" }),
    filename: "invoices.csv",
  });

  assert.equal(capturedForm?.get("file_type"), "invoices");
  const uploaded = capturedForm?.get("file");
  assert.ok(uploaded instanceof Blob);
  assert.equal((uploaded as File).name, "invoices.csv");
});

test("SSE parser handles agent events, terminal markers, comments, and CRLF", () => {
  const payload = [
    ": heartbeat\r\n\r\n",
    "id: 4\r\nevent: agent_event\r\ndata: {\"sequence\":4,\"batch_id\":\"BATCH-1\",\"agent_name\":\"controller\",\"event_type\":\"validated\",\"message\":\"Validated records\",\"timestamp\":\"2026-09-01T00:00:00Z\",\"latency_ms\":2,\"status\":\"succeeded\"}\r\n\r\n",
    "event: stream_end\ndata: {\"batch_id\":\"BATCH-1\",\"last_sequence\":4,\"terminal\":true}\n\n",
  ].join("");

  const messages = parseBatchEventStreamText(payload);

  assert.equal(messages.length, 2);
  assert.equal(messages[0]?.type, "agent_event");
  assert.equal(messages[0]?.type === "agent_event" && messages[0].event.sequence, 4);
  assert.equal(messages[1]?.type, "stream_end");
});

test("watchBatchEvents resumes with JSON snapshot polling when streaming fails", async () => {
  let calls = 0;
  const transitions: string[] = [];
  const event = {
    sequence: 1,
    batch_id: "BATCH-1",
    agent_name: "controller",
    event_type: "complete",
    message: "Completed",
    timestamp: "2026-09-01T00:00:00Z",
    latency_ms: 1,
    status: "succeeded",
  };
  const client = createCashCloseClient({
    baseUrl: "https://api.example.test",
    fetch: async () => {
      calls += 1;
      if (calls === 1) throw new TypeError("stream transport unavailable");
      return jsonResponse({ items: [event], next_sequence: 1, terminal: true });
    },
  });

  const events = [];
  for await (const event of client.watchBatchEvents("BATCH-1", {
    onTransportChange: (transport) => transitions.push(transport),
  })) {
    events.push(event);
  }

  assert.deepEqual(transitions, ["stream", "poll"]);
  assert.equal(events.length, 1);
  assert.equal(events[0]?.sequence, 1);
  assert.equal(calls, 2);
});

test("manual review methods preserve optimistic revision and PATCH semantics", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const client = createCashCloseClient({
    baseUrl: "https://api.example.test",
    fetch: async (input, init) => {
      calls.push({ url: String(input), init });
      return jsonResponse({
        proposal: {
          proposal_id: "MP-1",
          batch_id: "BATCH-1",
          transaction_id: "BANK-1",
          allocations: [{ invoice_id: "INV-1", amount: "100.00", currency: "INR" }],
          total_allocated: "100.00",
          transaction_amount: "100.00",
          currency: "INR",
          confidence: "0.9000",
          evidence: [
            {
              evidence_id: "E-1",
              evidence_type: "reference",
              summary: "Reference agrees",
              source_reference: "BANK-1",
            },
          ],
          revision: 2,
          created_at: "2026-09-01T00:00:00Z",
        },
        verification: {
          proposal_id: "MP-1",
          approved: false,
          policy_version: "v1",
          confidence_threshold: "0.9500",
          checked_at: "2026-09-01T00:00:00Z",
        },
      });
    },
  });

  await client.editMatch("MP-1", {
    expected_revision: 1,
    allocations: [{ invoice_id: "INV-1", amount: money("100"), currency: "INR" }],
    edit_reason: "Corrected invoice allocation",
  });

  assert.equal(calls[0]?.url, "https://api.example.test/api/matches/MP-1");
  assert.equal(calls[0]?.init?.method, "PATCH");
  assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)), {
    expected_revision: 1,
    allocations: [{ invoice_id: "INV-1", amount: "100.00", currency: "INR" }],
    edit_reason: "Corrected invoice allocation",
  });
});

test("audit download keeps the attachment filename and Blob payload", async () => {
  const client = createCashCloseClient({
    baseUrl: "https://api.example.test",
    fetch: async () =>
      new Response('{"report_id":"AUDIT-1"}', {
        headers: {
          "content-type": "application/json",
          "content-disposition": 'attachment; filename="cashclose-audit-BATCH-1.json"',
        },
      }),
  });

  const download = await client.downloadAudit("BATCH-1");

  assert.equal(download.filename, "cashclose-audit-BATCH-1.json");
  assert.equal(download.contentType, "application/json");
  assert.equal(await download.blob.text(), '{"report_id":"AUDIT-1"}');
});
