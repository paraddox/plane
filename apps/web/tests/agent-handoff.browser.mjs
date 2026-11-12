/* eslint-disable no-await-in-loop -- Cursor pages and feedback steps are ordered protocol operations. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const modulePath = process.env.PLAYWRIGHT_MODULE || "playwright";
const { chromium } = require(modulePath);
const { expect } = require(modulePath + "/test");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
const web = fixture.base_url;
assert.equal(new URL(web).hostname, "127.0.0.1");
assert.equal(new URL(fixture.callback_status_url).hostname, "127.0.0.1");
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
const project = `/api/workspaces/${fixture.workspace}/projects/${fixture.project}/`;
const coordination = project + "coordination/";
const csrfResponse = await context.request.get(web + "/auth/get-csrf-token/");
assert.equal(csrfResponse.status(), 200);
const { csrf_token: csrf } = await csrfResponse.json();

async function api(path, { method = "GET", data, status = 200 } = {}) {
  const response = await context.request.fetch(web + path, {
    method,
    data,
    headers: { "X-CSRFTOKEN": csrf, Origin: web, Referer: web },
  });
  assert.equal(response.status(), status, `${method} ${path}: ${await response.text()}`);
  return status === 204 ? null : response.json();
}

async function receiver() {
  const response = await context.request.get(fixture.callback_status_url);
  assert.equal(response.status(), 200);
  const status = await response.json();
  assert.deepEqual(status.errors, [], "External test runtime must not have unhandled notifications");
  return status;
}

async function eventFor(issue, kind, match) {
  let cursor = 0;
  while (true) {
    const eventPage = await api(coordination + `events/?after=${cursor}`);
    const event = eventPage.events.find((row) => row.issue === issue && row.kind === kind && match(row.payload));
    if (event) return event.id;
    assert.equal(eventPage.has_more, true, `Expected committed ${kind} event`);
    cursor = eventPage.next_cursor;
  }
}

async function receiptFor(issue, eventId, mainCalls) {
  await expect
    .poll(
      async () => {
        const status = await receiver();
        assert.equal(status.api_calls.main, mainCalls, "Feedback must not require another main-agent API call");
        return status.assignments
          .find((row) => row.issue_id === issue)
          ?.receipts.some((row) => row.event_id === eventId);
      },
      { timeout: fixture.http_timeout * 1000 }
    )
    .toBe(true);
  return receiver();
}

try {
  const created = await api(project + "issues/", {
    method: "POST",
    status: 201,
    data: {
      name: `Main-to-worker handoff ${Date.now()}`,
      requirements: "Verify direct feedback delivery.",
      definition_of_done: [{ description: "The assigned worker receives feedback without the main agent." }],
    },
  });
  const issue = created.id;
  await expect
    .poll(async () => (await receiver()).assignments.find((row) => row.issue_id === issue)?.question_id, {
      timeout: fixture.http_timeout * 1000,
    })
    .toBeTruthy();
  const assigned = (await receiver()).assignments.find((row) => row.issue_id === issue);
  const mainCalls = assigned.main_calls_at_handoff;
  assert.equal((await receiver()).api_calls.main, mainCalls);
  const overviewPath = coordination + `work-items/${issue}/overview/`;
  let overview = await api(overviewPath);
  const question = overview.questions.find((row) => row.id === assigned.question_id);
  assert.ok(question);
  const url = `${web}/${fixture.workspace}/projects/${fixture.project}/issues/${issue}/`;
  await page.goto(url);
  const panel = page.getByRole("region", { name: "Agent workflow", exact: true });
  const editor = panel.getByRole("textbox", { name: `Answer: ${question.text}`, exact: true });
  await editor.fill("Use the project context and emphasize direct agent collaboration.");
  await panel.getByRole("button", { name: "Post answer", exact: true }).click();
  await expect(editor).toHaveCount(0);
  const answerEvent = await eventFor(issue, "question.answered", (data) => data.question_id === question.id);
  await receiptFor(issue, answerEvent, mainCalls);
  overview = await api(overviewPath);
  assert.ok(overview.questions.find((row) => row.id === question.id).resolved_at);

  const comment = await api(project + `issues/${issue}/comments/`, {
    method: "POST",
    status: 201,
    data: { comment_html: "<p>Additional human feedback for the assigned worker.</p>" },
  });
  const commentEvent = await eventFor(issue, "comment.created", (data) => data.comment_id === comment.id);
  await receiptFor(issue, commentEvent, mainCalls);

  const module = await api(project + "modules/", {
    method: "POST",
    status: 201,
    data: { name: `Feedback scope ${issue}` },
  });
  await api(project + `issues/${issue}/modules/`, {
    method: "POST",
    status: 201,
    data: { modules: [module.id] },
  });
  const moduleAdded = await eventFor(issue, "task.modules_changed", (data) => data.added.includes(module.id));
  await receiptFor(issue, moduleAdded, mainCalls);
  await api(project + `modules/${module.id}/issues/${issue}/`, { method: "DELETE", status: 204 });
  const moduleRemoved = await eventFor(issue, "task.modules_changed", (data) => data.removed.includes(module.id));
  await receiptFor(issue, moduleRemoved, mainCalls);

  const files = [
    { name: "human-feedback.png", mimeType: "image/png", buffer: await page.screenshot() },
    {
      name: "human-feedback.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("Additional acceptance context for the worker."),
    },
  ];
  const assets = [];
  for (const file of files) {
    const attachments = `/api/assets/v2/workspaces/${fixture.workspace}/projects/${fixture.project}/issues/${issue}/attachments/`;
    const upload = await api(attachments, {
      method: "POST",
      data: { name: file.name, type: file.mimeType, size: file.buffer.length },
    });
    assert.equal(new URL(upload.upload_data.url).hostname, "127.0.0.1", "Uploads must stay on this local stack");
    const transferred = await context.request.post(upload.upload_data.url, {
      multipart: { ...upload.upload_data.fields, file },
    });
    assert.equal(transferred.ok(), true);
    await api(attachments + upload.asset_id + "/", { method: "PATCH", data: { is_uploaded: true }, status: 204 });
    const event = await eventFor(issue, "attachment.uploaded", (data) => data.asset_id === upload.asset_id);
    const status = await receiptFor(issue, event, mainCalls);
    const receipt = status.assignments
      .find((row) => row.issue_id === issue)
      .receipts.find((row) => row.event_id === event);
    assert.equal(receipt.asset_sha256, createHash("sha256").update(file.buffer).digest("hex"));
    assets.push(upload.asset_id);
  }

  const before = await api(overviewPath);
  const edited = await api(project + `issues/${issue}/`, {
    method: "PATCH",
    data: {
      requirements: "Updated requirements after worker handoff.",
      expected_revision: before.contract.spec_revision,
    },
  });
  const changedEvent = await eventFor(issue, "task.specification_changed", () => true);
  await receiptFor(issue, changedEvent, mainCalls);
  const final = await api(overviewPath);
  assert.equal(final.runs[0].id, assigned.run_id, "Specification edits must preserve assignment");
  assert.equal(final.runs[0].spec_revision, edited.spec_revision);
  assert.equal(final.contract.status, "in_progress");
  await page.reload();
  await expect(page.getByRole("textbox", { name: "Requirements", exact: true })).toHaveValue(
    "Updated requirements after worker handoff."
  );
  await page.screenshot({ path: `${output}/desktop.png`, fullPage: true });
  assert.deepEqual(errors, []);
  const report = {
    issue,
    run: assigned.run_id,
    url,
    main_calls_at_handoff: mainCalls,
    main_calls_after_feedback: (await receiver()).api_calls.main,
    assets,
    spec_revision: final.contract.spec_revision,
    answer_acknowledged: true,
    direct_comment_delivery: true,
    direct_module_delivery: true,
    attachment_content_verified: true,
    assignment_preserved: true,
    page_errors: errors,
    receiver: await receiver(),
  };
  await writeFile(`${output}/report.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await context.close();
  await browser.close();
}
