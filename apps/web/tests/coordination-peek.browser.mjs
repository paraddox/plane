import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
assert.equal(fixture.workspace, "coordination-dev", "Use only the synthetic coordination fixture");
const web = process.env.COORDINATION_WEB_URL || "http://127.0.0.1:3000";
assert.equal(new URL(web).hostname, "127.0.0.1", "Use only the disposable loopback stack");
const output = process.env.COORDINATION_BROWSER_OUTPUT;
assert.ok(output, "Set COORDINATION_BROWSER_OUTPUT for verification screenshots");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addCookies([
  { name: fixture.session_name, value: fixture.owner_session, url: web, httpOnly: true, sameSite: "Lax" },
]);
const page = await context.newPage();
const errors = [];
const checks = [];
page.on("pageerror", (error) => errors.push(error.message));
const peek = page.locator("#full-screen-portal");
const panel = peek.getByRole("region", { name: "Agent workflow", exact: true });
const intent = peek.getByRole("region", { name: "Task requirements", exact: true });

async function checkEditor(mode) {
  await panel.waitFor();
  assert.equal(await panel.count(), 1);
  await intent.getByLabel("Requirements", { exact: true }).waitFor();
  assert.equal(await intent.getByRole("textbox", { name: "Acceptance criterion 1", exact: true }).count(), 1);
  assert.equal(await peek.getByLabel("Human owner", { exact: true }).count(), 0);
  assert.equal(
    await peek.getByRole("button", { name: /Configure task|Submit to agents|Save specification/ }).count(),
    0
  );
  await peek.getByLabel("Release", { exact: true }).waitFor();
  await intent.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/${mode}.png`, fullPage: true });
  checks.push(`${mode} native task fields`);
}

async function changeMode(name) {
  await peek.locator('button[aria-haspopup="listbox"]').first().click();
  await page.getByRole("option", { name, exact: true }).click();
}

try {
  await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/`);
  await page.locator(`#issue-${fixture.issue}`).click();
  await peek.getByPlaceholder("Work item title", { exact: true }).waitFor();
  await checkEditor("side-peek");
  const requirements = intent.getByLabel("Requirements", { exact: true });
  const original = await requirements.inputValue();
  const updated = `Preview persistence ${Date.now()}`;
  await requirements.fill(updated);
  await intent.getByLabel("Acceptance criterion 1", { exact: true }).click();
  await intent
    .getByRole("status")
    .filter({ hasText: /^Saved$/ })
    .waitFor();
  await changeMode("Modal");
  await checkEditor("modal");
  assert.equal(await intent.getByLabel("Requirements", { exact: true }).inputValue(), updated);
  await changeMode("Full Screen");
  await checkEditor("full-screen");
  assert.equal(await intent.getByLabel("Requirements", { exact: true }).inputValue(), updated);
  await intent.getByLabel("Requirements", { exact: true }).fill(original);
  await intent.getByLabel("Acceptance criterion 1", { exact: true }).click();
  await intent
    .getByRole("status")
    .filter({ hasText: /^Saved$/ })
    .waitFor();
  await changeMode("Side Peek");
  await page.setViewportSize({ width: 390, height: 844 });
  await checkEditor("mobile-side-peek");
  assert.ok(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth), "Workflow overflows");
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile page overflows");
  assert.deepEqual(errors, []);
  const result = { result: "passed", checks, page_errors: errors };
  await writeFile(`${output}/result.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  await writeFile(`${output}/failure.json`, JSON.stringify({ checks, errors }, null, 2));
  throw error;
} finally {
  await browser.close();
}
