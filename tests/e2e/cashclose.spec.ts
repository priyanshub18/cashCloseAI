import { expect, test } from "@playwright/test";

test("runs the controller and exercises every finance workspace", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  await page.setViewportSize({ width: 1440, height: 980 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Cash position, verified." })).toBeVisible();
  await expect(page.getByRole("button", { name: "API online" })).toBeVisible();

  await page.getByRole("button", { name: "Run controller" }).click();
  await expect(page.getByRole("heading", { name: "Choose the truth layer" })).toBeVisible();
  await expect(page.getByText("Docker API is ready")).toBeVisible();
  await page.getByRole("button", { name: "Run demo controller" }).click();
  await expect(page.getByRole("heading", { name: "Close complete" })).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Open controller results" }).click();
  await expect(page.getByText("Live Docker workspace")).toBeVisible();

  await page.getByRole("button", { name: "Reconciliation", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Every match has a reason." })).toBeVisible();
  await page.locator(".segmented").getByRole("button", { name: "Needs Review" }).click();
  await expect(page.locator(".data-table tbody tr").first()).toBeVisible();
  await page.locator(".data-table tbody tr").first().click();
  await expect(page.getByRole("heading", { name: "Match evidence" })).toBeVisible();
  await page.locator("#review-note").fill("Reference, currency, customer identity, and source evidence reviewed.");
  await page.getByRole("button", { name: "Approve match" }).click();
  await expect(page.getByRole("heading", { name: "Match evidence" })).not.toBeVisible();

  await page.getByRole("button", { name: /^Exceptions/ }).click();
  await expect(page.getByRole("heading", { name: "The records AI refused to guess." })).toBeVisible();
  await page.locator(".exception-list > button").first().click();
  await page.getByRole("button", { name: "Request human review" }).click();
  await expect(page.getByRole("button", { name: "Review requested" })).toBeDisabled();
  await page.locator(".resolution-box textarea").fill("Remittance received and linked to the source record.");
  await page.getByRole("button", { name: "Resolve exception" }).click();
  await expect(page.getByText(/Resolved: Remittance received/)).toBeVisible();

  await page.getByRole("button", { name: "Cash forecast", exact: true }).click();
  await expect(page.getByRole("heading", { name: "See the squeeze before it happens." })).toBeVisible();
  await page.getByRole("button", { name: "Run scenario" }).click();
  await expect(page.getByRole("heading", { name: "Stress-test cash" })).toBeVisible();
  await page.getByRole("button", { name: "Calculate scenario" }).click();
  await expect(page.getByText(/Scenario active:/)).toBeVisible();

  await page.getByRole("button", { name: "Audit & evaluation", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Proof, not promises." })).toBeVisible();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download report" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/^cashclose-audit-.*\.json$/);
  await page.screenshot({ path: "test-results/cashclose-desktop.png", fullPage: true });

  expect(consoleErrors, `browser console errors: ${consoleErrors.join("\n")}`).toEqual([]);
});

test("keeps navigation and primary actions usable on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Cash position, verified." })).toBeVisible();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("navigation", { name: "CashClose sections" })).toBeVisible();
  await page.getByRole("button", { name: /^Exceptions/ }).click();
  await expect(page.getByRole("heading", { name: "The records AI refused to guess." })).toBeVisible();
  await page.getByRole("button", { name: "Run controller" }).click();
  await expect(page.getByRole("heading", { name: "Choose the truth layer" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByRole("heading", { name: "Choose the truth layer" })).not.toBeVisible();
  await page.screenshot({ path: "test-results/cashclose-mobile.png", fullPage: true });
});

test("validates and runs all four uploaded CSV sources", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "API online" })).toBeVisible();

  await page.getByRole("button", { name: "See controller architecture" }).click();
  await expect(page.getByRole("heading", { name: "One controller. Bounded specialists." })).toBeVisible();
  await page.getByRole("button", { name: "Got it" }).click();

  await page.getByRole("button", { name: "New close batch" }).click();
  await page.getByRole("button", { name: "Upload CSVs" }).click();
  const templateDownload = page.waitForEvent("download");
  await page.getByRole("button", { name: "Template" }).first().click();
  expect((await templateDownload).suggestedFilename()).toBe("bank_transactions.csv");

  const fileInputs = page.locator('.upload-card input[type="file"]');
  await fileInputs.nth(0).setInputFiles("demo_data/input/bank_transactions.csv");
  await fileInputs.nth(1).setInputFiles("demo_data/input/invoices.csv");
  await fileInputs.nth(2).setInputFiles("demo_data/input/ledger_entries.csv");
  await fileInputs.nth(3).setInputFiles("demo_data/input/remittances.csv");
  await expect(page.getByText("All four schemas are ready")).toBeVisible();
  await page.getByRole("button", { name: "Upload & run" }).click();
  await expect(page.getByRole("heading", { name: "Close complete" })).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Open controller results" }).click();
  await expect(page.getByText("Live Docker workspace")).toBeVisible();
});
