import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
assert.equal(fixture.workspace, "coordination-dev", "Only the synthetic fixture may be modified");
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
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const root = `/api/workspaces/${fixture.workspace}/projects/${fixture.project}/`;
const created = [];

async function createTask(mobile, filled) {
  await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
  await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/`);
  await page.getByRole("button", { name: mobile ? "Work item" : "Add work item", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Requirements", { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel("Acceptance criterion 1", { exact: true }).count(), 1);
  assert.equal(await dialog.getByLabel("Human owner", { exact: true }).count(), 0);
  const title = `Native ${filled ? "specified" : "incomplete"} task ${Date.now()}`;
  await dialog.getByPlaceholder("Title", { exact: true }).fill(title);
  if (filled) {
    await dialog.getByLabel("Requirements", { exact: true }).fill("One standalone welcome page.");
    await dialog.getByLabel("Acceptance criterion 1", { exact: true }).fill("Works offline.");
    await dialog.getByRole("button", { name: "Add criterion", exact: true }).click();
    await dialog.getByLabel("Acceptance criterion 2", { exact: true }).fill("Fits mobile screens.");
  }
  const release = dialog.getByLabel("Release", { exact: true });
  await release.waitFor();
  let releaseId = null;
  if (filled) {
    await release.locator("option").nth(1).waitFor({ state: "attached" });
    releaseId = await release.locator("option").nth(1).getAttribute("value");
    await release.selectOption(releaseId);
  }
  await dialog.getByLabel("Requirements", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/creation-${mobile ? "mobile" : "desktop"}.png`, fullPage: true });
  const responsePromise = page.waitForResponse(
    (response) => response.request().method() === "POST" && new URL(response.url()).pathname === root + "issues/"
  );
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  const response = await responsePromise;
  assert.equal(response.status(), 201);
  const task = await response.json();
  created.push(task.id);
  assert.equal(task.requirements, filled ? "One standalone welcome page." : "");
  assert.equal(task.definition_of_done.length, filled ? 2 : 0);
  assert.equal(task.spec_revision, 1);
  assert.equal(task.release_id, releaseId);
  await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/${task.id}/`);
  const intent = page.getByRole("region", { name: "Task requirements", exact: true });
  await intent
    .getByRole("status")
    .filter({ hasText: /^Saved$/ })
    .waitFor();
  assert.equal(await intent.getByLabel("Requirements", { exact: true }).inputValue(), task.requirements);
  assert.equal(
    await intent.getByLabel("Acceptance criterion 1", { exact: true }).inputValue(),
    filled ? "Works offline." : ""
  );
  await page.getByLabel("Release", { exact: true }).first().waitFor();
  assert.equal(await page.getByLabel("Release", { exact: true }).first().inputValue(), releaseId ?? "");
  const execution = page.getByRole("region", { name: "Agent workflow", exact: true });
  await execution.getByText(/^Ready for agent \/ Spec 1$/).waitFor();
  assert.equal(
    await page.getByRole("button", { name: /Configure task|Submit to agents|Save specification/ }).count(),
    0
  );
  await intent.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/detail-${mobile ? "mobile" : "desktop"}.png`, fullPage: true });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Page overflows");
  if (filled) {
    const updated = "Latest inline task requirements.";
    await intent.getByLabel("Requirements", { exact: true }).fill(updated);
    await intent.getByLabel("Acceptance criterion 1", { exact: true }).click();
    await intent
      .getByRole("status")
      .filter({ hasText: /^Saved$/ })
      .waitFor();
    await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/`);
    await page.locator(`#issue-${task.id}`).click({ button: "right" });
    await page
      .locator('.pointer-events-auto > [data-context-menu="true"]')
      .getByRole("button", { name: "Edit", exact: true })
      .last()
      .click();
    const edit = page.getByRole("dialog");
    await page.waitForFunction(
      (expected) =>
        Array.from(document.querySelectorAll('[role="dialog"] textarea')).some((element) => element.value === expected),
      updated
    );
    assert.equal(await edit.getByLabel("Requirements", { exact: true }).inputValue(), updated);
    assert.equal(await edit.getByLabel("Acceptance criterion 1", { exact: true }).inputValue(), "Works offline.");
    assert.equal(await edit.getByLabel("Release", { exact: true }).inputValue(), releaseId);
    const edited = "Requirements updated through the Edit dialog.";
    await edit.getByLabel("Requirements", { exact: true }).fill(edited);
    const savedEdit = page.waitForResponse(
      (patchResponse) =>
        patchResponse.request().method() === "PATCH" &&
        new URL(patchResponse.url()).pathname === root + `issues/${task.id}/`
    );
    await edit.getByRole("button", { name: "Update", exact: true }).click();
    const savedResponse = await savedEdit;
    assert.equal(savedResponse.status(), 200);
    assert.equal((await savedResponse.json()).requirements, edited);
    await edit.waitFor({ state: "hidden" });
    await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/${task.id}/`);
    await intent
      .getByRole("status")
      .filter({ hasText: /^Saved$/ })
      .waitFor();
    await page.waitForFunction(
      (expected) =>
        Array.from(document.querySelectorAll('[aria-label="Task requirements"] textarea')).some(
          (element) => element.value === expected
        ),
      edited
    );
    assert.equal(await intent.getByLabel("Requirements", { exact: true }).inputValue(), edited);
  }
}

async function saveDraft() {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/`);
  await page.getByRole("button", { name: "Add work item", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByPlaceholder("Title", { exact: true }).fill(`Native draft ${Date.now()}`);
  await dialog.getByLabel("Requirements", { exact: true }).fill("Preserve draft requirements.");
  await dialog.getByLabel("Acceptance criterion 1", { exact: true }).fill("Survives reopening.");
  await dialog.getByRole("button", { name: "Discard", exact: true }).click();
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === `/api/workspaces/${fixture.workspace}/draft-issues/`
  );
  await page.getByRole("button", { name: "Save to Drafts", exact: true }).click();
  const response = await saved;
  assert.equal(response.status(), 201);
  const draft = await response.json();
  assert.equal(draft.requirements, "Preserve draft requirements.");
  await page.goto(`${web}/${fixture.workspace}/drafts/`);
  await page.locator(`#issue-${draft.id}`).dblclick();
  await dialog.getByLabel("Requirements", { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel("Requirements", { exact: true }).inputValue(), draft.requirements);
  assert.equal(await dialog.getByLabel("Acceptance criterion 1", { exact: true }).inputValue(), "Survives reopening.");
  await page.screenshot({ path: `${output}/draft-reopened.png`, fullPage: true });
}

try {
  await createTask(false, true);
  await createTask(true, false);
  await saveDraft();
  assert.deepEqual(errors, []);
  const result = {
    result: "passed",
    created_tasks: created,
    checks: [
      "desktop specified creation",
      "mobile incomplete creation",
      "native direct detail",
      "list Edit freshness and save",
      "automatic readiness",
      "draft intent persistence",
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
