import assert from "node:assert/strict";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
const web = process.env.COORDINATION_WEB_URL || "http://127.0.0.1:3000";
const backend = process.env.COORDINATION_API_URL || "http://127.0.0.1:8000";
for (const url of [web, backend])
  assert.equal(new URL(url).hostname, "127.0.0.1", "Use only the disposable loopback stack");
const output = process.env.COORDINATION_BROWSER_OUTPUT;
assert.ok(output, "Set COORDINATION_BROWSER_OUTPUT for verification screenshots");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addCookies([
  { name: fixture.session_name, value: fixture.owner_session, url: backend, httpOnly: true, sameSite: "Lax" },
]);
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const root = `/api/workspaces/${fixture.workspace}/projects/${fixture.project}/coordination/`;
const machine = root.replace("/api/", "/api/v1/");
const task = `work-items/${fixture.issue}/`;
async function api(path, { key, method = "GET", data, expected = 200 } = {}) {
  const response = await context.request.fetch(backend + path, {
    method,
    data,
    headers: key ? { "X-Api-Key": key } : {},
  });
  assert.equal(response.status(), expected, `${method} ${path}: ${response.status()}`);
  return expected === 204 ? null : response.json();
}
async function fits(view, label) {
  const overflow = await view.evaluate(() => ({ width: innerWidth, pageWidth: document.documentElement.scrollWidth }));
  assert.ok(overflow.pageWidth <= overflow.width, `${label} overflows: ${JSON.stringify(overflow)}`);
}
try {
  await page.goto(`${web}/${fixture.workspace}/settings/projects/${fixture.project}/coordination/`);
  await page.getByRole("heading", { name: "Execution Policy", exact: true }).waitFor();
  await page.getByLabel("Agent execution enabled").uncheck();
  await page.getByLabel("Agent execution enabled").check();
  await page.getByRole("button", { name: "Save policy", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "Saved" }).waitFor();
  const releaseIdentifier = `BROWSER-${Date.now()}`;
  await page.getByLabel("Identifier", { exact: true }).fill(releaseIdentifier);
  await page.getByLabel("Release name", { exact: true }).fill("Browser Delivery");
  await page.getByLabel("Description", { exact: true }).fill("Synthetic local release");
  await page.getByLabel("Release status").selectOption("active");
  await page.getByRole("button", { name: "Create release", exact: true }).click();
  await page.getByRole("button", { name: `Edit release ${releaseIdentifier}` }).waitFor();
  await fits(page, "Desktop settings");
  await page.getByRole("heading", { name: "Agent Coordination", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/settings-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("heading", { name: "Agent Coordination", exact: true }).scrollIntoViewIfNeeded();
  await fits(page, "Mobile settings");
  await page.screenshot({ path: `${output}/settings-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });

  const releases = await api(root + "releases/");
  const release = releases.find((row) => row.identifier === releaseIdentifier);
  assert.ok(release);
  await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/${fixture.issue}/`);
  const panel = page.getByRole("region", { name: "Agent workflow", exact: true });
  const intent = page.getByRole("region", { name: "Task requirements", exact: true });
  await intent
    .getByLabel("Requirements", { exact: true })
    .fill("Deliver the API and web behavior with traceable acceptance evidence.");
  await intent
    .getByRole("textbox", { name: "Acceptance criterion 1", exact: true })
    .fill("The acceptance tests pass for the reviewed code revision.");
  await intent.getByRole("textbox", { name: "Acceptance criterion 1", exact: true }).press("Tab");
  await intent
    .getByRole("status")
    .filter({ hasText: /^Saved$/ })
    .waitFor();
  const releaseUpdated = page.waitForResponse(
    (response) => response.request().method() === "PATCH" && response.url().endsWith(`/issues/${fixture.issue}/`)
  );
  let received;
  let finish;
  const receivedPatch = new Promise((resolve) => {
    received = resolve;
  });
  const finishPatch = new Promise((resolve) => {
    finish = resolve;
  });
  const taskRoute = `**/api/workspaces/${fixture.workspace}/projects/${fixture.project}/issues/${fixture.issue}/`;
  const holdRelease = async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    const response = await route.fetch();
    received();
    await finishPatch;
    await route.fulfill({ response });
  };
  await page.route(taskRoute, holdRelease);
  await page.getByLabel("Release", { exact: true }).selectOption(release.id);
  await receivedPatch;
  try {
    assert.equal(
      await panel.getByLabel("Authorize automatic merge").isDisabled(),
      true,
      "Approval controls must wait for native task edits to finish"
    );
  } finally {
    finish();
  }
  assert.equal((await releaseUpdated).status(), 200);
  await page.unroute(taskRoute, holdRelease);
  await page.locator('select[aria-label="Release"]:not([disabled])').waitFor();
  const authorized = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" && response.url().endsWith(`/work-items/${fixture.issue}/auto-merge/`)
  );
  await panel.getByLabel("Authorize automatic merge").check();
  assert.equal((await authorized).status(), 200);
  const reviewerSection = panel.locator("details").first();
  if ((await reviewerSection.getAttribute("open")) === null) await reviewerSection.locator("summary").click();
  const reviewerCheckbox = panel.getByRole("checkbox", { name: "Designated Reviewer", exact: true });
  if (!(await reviewerCheckbox.isChecked())) {
    const reviewersSaved = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" && response.url().endsWith(`/work-items/${fixture.issue}/reviewers/`)
    );
    await reviewerCheckbox.check();
    assert.equal((await reviewersSaved).status(), 200);
  }
  await panel.getByText(/^Ready for agent \/ Spec \d+$/).waitFor();
  let overview = await api(root + task + "overview/");
  assert.equal(overview.contract.status, "ready");
  assert.equal(overview.contract.auto_merge_authorized, true);
  assert.equal(overview.reviewers.length, 1);
  const run = await api(machine + task + "claim/", {
    key: fixture.dispatcher_token,
    method: "POST",
    expected: 201,
    data: { agent: fixture.worker, worker_id: "browser-worker", repository: "example/product", base_branch: "main" },
  });
  const credential = await api(machine + `runs/${run.id}/credentials/`, {
    key: fixture.dispatcher_token,
    method: "POST",
    expected: 201,
    data: {},
  });
  const key = credential.token;
  const question = await api(machine + task + "questions/", {
    key,
    method: "POST",
    expected: 201,
    data: { text: `Should the result include both API and web changes? (attempt ${run.attempt})`, blocking: true },
  });
  await panel.getByRole("button", { name: "Refresh workflow", exact: true }).click();
  const answerText = `Yes, include both and their acceptance evidence (attempt ${run.attempt}).`;
  await panel.getByRole("textbox", { name: `Answer: ${question.text}`, exact: true }).fill(answerText);
  await panel.getByRole("button", { name: "Post answer", exact: true }).click();
  await panel.getByText(answerText, { exact: true }).waitFor();
  overview = await api(machine + task + "overview/", { key });
  const answeredQuestion = overview.questions.find((value) => value.id === question.id);
  await api(machine + task + `questions/${question.id}/resolve/`, {
    key,
    method: "POST",
    data: { answer_comment: answeredQuestion.answer_comment },
  });
  await api(machine + `runs/${run.id}/head/`, {
    key,
    method: "POST",
    data: { head_sha: "a".repeat(40), pull_request: 7 },
  });
  const screenshotSummary = `Agent screenshot from acceptance verification (attempt ${run.attempt})`;
  await Promise.all(
    [
      {
        name: "acceptance.png",
        type: "image/png",
        buffer: await page.screenshot(),
        kind: "screenshot",
        summary: screenshotSummary,
      },
      {
        name: "validation.txt",
        type: "text/plain",
        buffer: Buffer.from("Synthetic acceptance evidence"),
        kind: "document",
        summary: "Agent validation document",
      },
    ].map(async (artifact) => {
      const attachmentPath = `/api/v1/workspaces/${fixture.workspace}/projects/${fixture.project}/work-items/${fixture.issue}/attachments/`;
      const uploaded = await api(attachmentPath, {
        key,
        method: "POST",
        data: { name: artifact.name, type: artifact.type, size: artifact.buffer.length },
      });
      const transferred = await context.request.post(uploaded.upload_data.url, {
        multipart: {
          ...uploaded.upload_data.fields,
          file: { name: artifact.name, mimeType: artifact.type, buffer: artifact.buffer },
        },
      });
      assert.equal(transferred.ok(), true, "Presigned S3 transfer failed");
      await api(attachmentPath + `${uploaded.asset_id}/`, {
        key,
        method: "PATCH",
        data: { is_uploaded: true },
        expected: 204,
      });
      const evidence = await api(machine + `runs/${run.id}/evidence/`, {
        key,
        method: "POST",
        expected: 201,
        data: {
          head_sha: "a".repeat(40),
          kind: artifact.kind,
          outcome: "info",
          summary: artifact.summary,
          asset: uploaded.asset_id,
        },
      });
      const downloaded = await context.request.get(backend + evidence.asset_url);
      assert.equal(downloaded.ok(), true, "Artifact download failed");
      assert.deepEqual(await downloaded.body(), artifact.buffer);
    })
  );
  await api(machine + `runs/${run.id}/evidence/`, {
    key,
    method: "POST",
    expected: 201,
    data: {
      head_sha: "a".repeat(40),
      kind: "test",
      outcome: "passed",
      summary: "Acceptance checks passed in the isolated development stack.",
      criterion_ids: [overview.contract.definition_of_done[0].id],
    },
  });
  await api(machine + `runs/${run.id}/review-ready/`, { key, method: "POST", data: {} });
  await panel.getByRole("button", { name: "Refresh workflow", exact: true }).click();
  await panel.getByRole("button", { name: "Approve merge", exact: true }).click();
  await panel.getByText("Product Owner: Approved / Code 1", { exact: true }).waitFor();
  await fits(page, "Desktop task");
  const image = panel.getByRole("img", { name: screenshotSummary, exact: true });
  await image.scrollIntoViewIfNeeded();
  assert.equal(
    await image.evaluate(
      (element) =>
        new Promise((resolve, reject) => {
          if (element.complete) return resolve(element.naturalWidth > 0);
          element.addEventListener("load", () => resolve(element.naturalWidth > 0), { once: true });
          element.addEventListener("error", () => reject(new Error("Evidence screenshot did not render")), {
            once: true,
          });
        })
    ),
    true
  );
  await page.screenshot({ path: `${output}/task-evidence.png`, fullPage: true });
  await panel.getByRole("heading", { name: "Execution", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/task-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => {
    const sidebar = document.querySelector("#main-sidebar");
    return sidebar instanceof HTMLElement && sidebar.style.width === "0px" && getComputedStyle(sidebar).opacity === "0";
  });
  await panel.getByRole("heading", { name: "Execution", exact: true }).scrollIntoViewIfNeeded();
  await fits(page, "Mobile task");
  await page.screenshot({ path: `${output}/task-mobile.png`, fullPage: true });

  const reviewerContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await reviewerContext.addCookies([
    { name: fixture.session_name, value: fixture.reviewer_session, url: backend, httpOnly: true, sameSite: "Lax" },
  ]);
  const reviewerPage = await reviewerContext.newPage();
  reviewerPage.on("pageerror", (error) => errors.push(error.message));
  await reviewerPage.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/${fixture.issue}/`);
  const reviewerPanel = reviewerPage.getByRole("region", { name: "Agent workflow", exact: true });
  await reviewerPanel.getByRole("button", { name: "Approve merge", exact: true }).click();
  await reviewerPanel.getByText("Designated Reviewer: Approved / Code 1", { exact: true }).waitFor();
  assert.equal(await reviewerPanel.getByLabel("Authorize automatic merge").isDisabled(), true);
  overview = await api(root + task + "overview/");
  assert.equal(overview.decisions.filter((row) => row.run === run.id).length, 2);
  assert.equal(overview.contract.status, "review");
  assert.equal(overview.questions.find((row) => row.id === question.id).resolved_at !== null, true);
  assert.equal(overview.evidence.filter((row) => row.run === run.id).length, 3);
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      result: "passed",
      scenarios: [
        "project policy",
        "project release",
        "specification",
        "advance authorization",
        "required reviewer",
        "automatic readiness and claim",
        "native edit and approval revision fencing",
        "human clarification",
        "code evidence",
        "owner approval",
        "reviewer approval",
        "desktop and mobile layouts",
      ],
      output,
    })
  );
} catch (error) {
  await page.screenshot({ path: `${output}/failure.png`, fullPage: true });
  console.error(
    JSON.stringify({ url: page.url(), errors, text: (await page.locator("body").innerText()).slice(0, 6000) })
  );
  throw error;
} finally {
  await browser.close();
}
