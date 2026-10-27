import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mkdirSync } from "node:fs";
import path from "node:path";

const token = process.env.CHRONOS_API_TOKEN ?? "local-development-token-do-not-use-outside-compose-0001";
const screenshotDir = path.resolve(process.cwd(), "../docs/verification/screenshots/phase-6");
mkdirSync(screenshotDir, { recursive: true });

async function capture(page: Page, name: string) {
  await page.screenshot({ path: path.join(screenshotDir, name), fullPage: true, animations: "disabled" });
}

async function authenticateInSession(page: Page) {
  await page.addInitScript((value) => sessionStorage.setItem("chronos.api-token", value), token);
}

test("connection screen and same-origin proxy enforce bearer authentication", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Connect to your control plane" })).toBeVisible();
  const noAuth = await page.request.get("/api/chronos/v1/queue/stats");
  expect(noAuth.status()).toBe(401);
  await page.locator("#api-token").fill("this-is-an-invalid-token-with-enough-length");
  await page.getByRole("button", { name: "Connect securely" }).click();
  await expect(page.getByText("That token was not accepted by Chronos.", { exact: true })).toBeVisible();
  await page.locator("#api-token").fill(token);
  await page.getByRole("button", { name: "Connect securely" }).click();
  await expect(page.getByRole("heading", { name: "Queue at a glance" })).toBeVisible();
});

test("operator journey covers queue, replay, cancellation, schedules, workers, metrics and settings", async ({ page }) => {
  await authenticateInSession(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Queue at a glance" })).toBeVisible();
  await expect(page.getByText("Ready to run", { exact: true })).toBeVisible();
  await expect(page.getByText("Worker fleet", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Switch to light theme" }).click();
  await expect(page.locator("html")).toHaveClass(/light/);
  await capture(page, "overview-light.png");
  await page.getByRole("button", { name: "Switch to dark theme" }).click();
  await capture(page, "overview-dark.png");
  const dashboardA11y = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(dashboardA11y.violations, JSON.stringify(dashboardA11y.violations.map((item) => ({ id: item.id, impact: item.impact, nodes: item.nodes.map((node) => node.target) }))).slice(0, 3000)).toEqual([]);

  await page.getByRole("link", { name: "Jobs", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Jobs", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: /^#\d+$/ }).first()).toBeVisible();
  await capture(page, "jobs.png");
  await page.getByRole("link", { name: "Create job" }).first().click();
  await expect(page.getByRole("heading", { name: "Create a job" })).toBeVisible();
  const formA11y = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
  expect(formA11y.violations.map((item) => item.id)).toEqual([]);
  await capture(page, "create-job.png");
  await page.locator("#payload").fill(JSON.stringify({ records: [{ category: "journey", amount: 41 }], work_ms: 40 }));
  await page.getByRole("button", { name: /Submit job/ }).click();
  await expect(page).toHaveURL(/\/jobs\/[0-9]+$/);
  await expect(page.getByRole("heading", { name: /Job #[0-9]+/ })).toBeVisible();
  const sourceJob = page.url().split("/").pop();
  await expect(page.getByText("Payload", { exact: true })).toBeVisible();
  await capture(page, "job-details.png");
  await page.getByRole("link", { name: "Jobs", exact: true }).click();
  await page.getByRole("textbox", { name: "Search jobs" }).fill(`#${sourceJob}`);
  await expect(page.getByRole("link", { name: `#${sourceJob}` })).toBeVisible();
  await page.goto(`/jobs/${sourceJob}`);

  // Replay uses the API only after explicit confirmation and an audit reason.
  page.on("dialog", async (dialog) => {
    if (dialog.type() === "confirm") await dialog.accept();
    else await dialog.accept("e2e operator approved replay");
  });
  await expect(page.getByRole("button", { name: "Replay" })).toBeVisible({ timeout: 25000 });
  await page.getByRole("button", { name: "Replay" }).click();
  await expect.poll(() => page.url().split("/").pop(), { timeout: 10000 }).not.toBe(sourceJob);
  await expect(page.getByText("Replay source")).toBeVisible({ timeout: 10000 });

  // A delayed submission is cancellable; the UI confirms and records a reason.
  await page.getByRole("link", { name: "Create job" }).first().click();
  const future = new Date(Date.now() + 60 * 60 * 1000);
  future.setMinutes(future.getMinutes() - future.getTimezoneOffset());
  await page.locator("#run-at").fill(future.toISOString().slice(0, 16));
  await page.locator("#payload").fill(JSON.stringify({ records: [{ category: "cancel", amount: 1 }] }));
  await page.getByRole("button", { name: /Schedule job/ }).click();
  await expect(page).toHaveURL(/\/jobs\/[0-9]+$/);
  await expect(page.getByRole("button", { name: "Cancel" })).toBeVisible();
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.getByText("canceled", { exact: true }).first()).toBeVisible({ timeout: 10000 });

  await page.getByRole("link", { name: "Schedules", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Schedules" })).toBeVisible();
  await page.locator("#cron").fill("0 9 * * 1-5");
  await page.locator("#timezone").fill("America/New_York");
  await page.getByRole("button", { name: /Create recurring schedule/ }).click();
  await expect(page.getByText("Recurring schedule created")).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: "Disable" }).last().click();
  await expect(page.getByText("Schedule disabled")).toBeVisible();
  await expect(page.getByText("Disabled", { exact: true }).last()).toBeVisible();
  await capture(page, "schedules.png");

  await page.getByRole("link", { name: "Workers", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Worker fleet" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Process registry" })).toBeVisible();
  await expect(page.getByText("worker-1", { exact: true })).toBeVisible();
  await capture(page, "workers.png");

  await page.getByRole("link", { name: "Dead letter" }).click();
  await expect(page.getByRole("heading", { name: "Dead letter queue" })).toBeVisible();
  await capture(page, "dead-letter.png");

  await page.getByRole("link", { name: "Observability", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Observability" })).toBeVisible();
  await expect(page.getByText("Execution latency percentiles")).toBeVisible();
  await capture(page, "observability.png");

  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await page.locator("#default-attempts").fill("4");
  await page.locator("#default-runtime").fill("35");
  await page.getByRole("button", { name: "Save queue defaults" }).click();
  await expect(page.getByText("Queue defaults updated")).toBeVisible();
  await expect(page.getByText("Queue defaults updated")).toBeHidden({ timeout: 10000 });
  await capture(page, "settings.png");

  // Verify responsive navigation and prevent accidental horizontal overflow.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Queue at a glance" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.getByRole("button", { name: "Open navigation" }).click();
  await expect(page.getByRole("dialog", { name: "Primary navigation" })).toBeVisible();
  await capture(page, "overview-mobile.png");
  await page.getByRole("button", { name: "Close navigation" }).click();
});
