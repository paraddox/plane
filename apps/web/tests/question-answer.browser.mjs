import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
assert.equal(fixture.workspace, "coordination-dev");
const web = process.env.COORDINATION_WEB_URL || "http://127.0.0.1:3000";
assert.equal(new URL(web).hostname, "127.0.0.1");
const output = process.env.COORDINATION_BROWSER_OUTPUT;
assert.ok(output);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addCookies([
  { name: fixture.session_name, value: fixture.owner_session, url: web, httpOnly: true, sameSite: "Lax" },
]);
const root = `/api/workspaces/${fixture.workspace}/projects/${fixture.project}/coordination/`;
const overviewPath = root + `work-items/${fixture.issue}/overview/`;
const existing = await context.request.get(web + overviewPath);
assert.equal(existing.status(), 200, "Synthetic browser session must be valid");
const overview = await existing.json();
let question = {
  ...overview.questions[0],
  id: randomUUID(),
  text: "Which welcome content should be included?",
  respondent: fixture.owner,
  spec_revision: overview.contract.spec_revision,
  answer: "",
  answer_comment: null,
  resolved_at: null,
  blocking: true,
};
let rejectSave = false;
const submissions = [];
await context.route("**" + overviewPath, (route) =>
  route.fulfill({
    json: { ...overview, questions: [question], contract: { ...overview.contract, status: "awaiting_input" } },
  })
);
await context.route("**" + root + `work-items/${fixture.issue}/questions/${question.id}/answer/`, async (route) => {
  const { text } = route.request().postDataJSON();
  submissions.push(text);
  if (rejectSave) return route.fulfill({ status: 503, json: { detail: "Synthetic answer service unavailable." } });
  question = { ...question, answer: text, answer_comment: randomUUID() };
  return route.fulfill({ json: question });
});
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const panel = page.getByRole("region", { name: "Agent workflow", exact: true });
const card = panel.getByText(question.text, { exact: true }).locator("..");
const editor = card.getByRole("textbox", { name: `Answer: ${question.text}`, exact: true });
const posted = "Introduce the product for prospective customers.";
const updated = "Introduce the product and its agent coordination workflow.";

try {
  await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/${fixture.issue}/`);
  await editor.fill(posted);
  await card.getByRole("button", { name: "Post answer", exact: true }).click();
  await card
    .locator("p")
    .filter({ hasText: new RegExp(`^${posted}$`) })
    .waitFor();
  assert.equal(await editor.count(), 0, "The answer box must collapse after a successful post");
  await card.getByRole("button", { name: "Edit answer", exact: true }).click();
  assert.equal(await editor.inputValue(), posted);
  await editor.fill("Discard this draft.");
  await card.getByRole("button", { name: "Cancel", exact: true }).click();
  assert.equal(await editor.count(), 0);
  assert.equal(submissions.length, 1, "Cancel must not submit a replacement answer");
  await card.getByRole("button", { name: "Edit answer", exact: true }).click();
  assert.equal(await editor.inputValue(), posted);
  await editor.fill(updated);
  rejectSave = true;
  await card.getByRole("button", { name: "Save answer", exact: true }).click();
  await panel.getByRole("alert").filter({ hasText: "Synthetic answer service unavailable." }).waitFor();
  assert.equal(await editor.inputValue(), updated, "A failed save must preserve the edit draft");
  rejectSave = false;
  await card.getByRole("button", { name: "Save answer", exact: true }).click();
  await card
    .locator("p")
    .filter({ hasText: new RegExp(`^${updated}$`) })
    .waitFor();
  assert.equal(await editor.count(), 0);
  assert.equal(submissions.at(-1), updated);
  await page.reload();
  await card
    .locator("p")
    .filter({ hasText: new RegExp(`^${updated}$`) })
    .waitFor();
  assert.equal(await editor.count(), 0, "Reload must keep a posted answer collapsed");
  await card.getByRole("button", { name: "Edit answer", exact: true }).waitFor();
  await card.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await card.scrollIntoViewIfNeeded();
  assert.ok(await card.evaluate((element) => element.scrollWidth <= element.clientWidth));
  await page.screenshot({ path: `${output}/mobile.png`, fullPage: true });
  question = { ...question, resolved_at: new Date().toISOString() };
  await panel.getByRole("button", { name: "Refresh workflow", exact: true }).click();
  await card.getByText(/Resolved/).waitFor();
  assert.equal(await card.getByRole("button", { name: "Edit answer", exact: true }).count(), 0);
  assert.deepEqual(errors, []);
  const result = {
    result: "passed",
    checks: [
      "post collapse",
      "edit prefill",
      "cancel",
      "failed-save draft retention",
      "save collapse",
      "reload",
      "resolved read-only",
      "desktop/mobile",
    ],
    page_errors: errors,
  };
  await writeFile(`${output}/result.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  throw error;
} finally {
  await browser.close();
}
