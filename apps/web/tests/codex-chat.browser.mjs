import assert from "node:assert/strict";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { randomUUID } from "node:crypto";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixturePath = process.env.CODEX_TEST_FIXTURE;
assert.ok(fixturePath, "A private, dedicated Codex test fixture is required");
const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
const web = fixture.base_url;
assert.equal(new URL(web).hostname, "127.0.0.1");
const output = process.env.CODEX_TEST_OUTPUT;
assert.ok(output);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
context.setDefaultTimeout(0);
await context.addCookies([
  { name: fixture.session_name, value: fixture.owner_session, url: web, httpOnly: true, sameSite: "Lax" },
]);
const csrf = await context.request.get(web + "/auth/get-csrf-token/");
const headers = { "X-CSRFTOKEN": (await csrf.json()).csrf_token, Origin: web };
const native = `/api/workspaces/${fixture.workspace}/projects/${fixture.project}/issues/`;
if (!fixture.issue) {
  const created = await context.request.post(web + native, {
    headers,
    data: {
      name: fixture.test_name || "Live Codex WebSocket welcome page",
      description_html:
        "<p>Create a standalone HTML welcome page in the isolated test workspace. Do not merge or open a PR.</p>",
      requirements:
        "Create welcome.html with inline CSS. Follow the authorized test fixture instructions for reporting and the simulated build.",
      definition_of_done: [{ description: "One standalone HTML file opens offline." }],
    },
  });
  assert.equal(created.status(), 201, await created.text());
  fixture.issue = (await created.json()).id;
  await writeFile(fixturePath, JSON.stringify(fixture), { mode: 0o600 });
}
const conversation = `/api/workspaces/${fixture.workspace}/projects/${fixture.project}/coordination/work-items/${fixture.issue}/conversation/`;
const page = await context.newPage();
const errors = [];
const marker = `LOCAL_CHAT_OK_${randomUUID()}`;
page.on("pageerror", (error) => errors.push(error.message));
try {
  await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/${fixture.issue}/`);
  await page.getByRole("button", { name: "Agent conversation", exact: true }).click();
  console.log("Conversation opened");
  const editor = page.getByRole("textbox", { name: "Message agent", exact: true });
  await editor.waitFor({ state: "visible" });
  console.log("Editor visible");
  await editor.fill(
    `Report the real implementation status and include ${marker} in your reply. Do not forward this status question as work.`
  );
  console.log("Message filled");
  await page.screenshot({ path: `${output}/before-send.png`, fullPage: true });
  await page.getByRole("button", { name: "Send", exact: true }).click();
  console.log("Send clicked");
  await page
    .getByRole("log")
    .locator("article")
    .filter({ has: page.locator("header").filter({ hasText: /^Agent/ }) })
    .getByText(marker, { exact: false })
    .first()
    .waitFor();
  console.log("New agent reply visible");
  await page.screenshot({ path: `${output}/desktop-chat.png`, fullPage: true });
  console.log("Desktop captured");
  if (fixture.test_feedback) {
    const name = `feedback-${randomUUID()}.txt`;
    await page.getByLabel("Conversation attachments", { exact: true }).setInputFiles({
      name,
      mimeType: "text/plain",
      buffer: Buffer.from("Use the title Welcome to MUE in the standalone welcome page."),
    });
    console.log("Attachment selected");
    await page.getByRole("dialog").getByText(name, { exact: true }).waitFor();
    console.log("Attachment uploaded");
    await editor.fill(
      `Forward this feedback to the implementation worker without interrupting its active command: use the title Welcome to MUE. Read the attached feedback file. Include FEEDBACK_QUEUED_${marker} in your reply after queuing it.`
    );
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await page
      .getByRole("log")
      .locator("article")
      .filter({ has: page.locator("header").filter({ hasText: /^Agent/ }) })
      .getByText(`FEEDBACK_QUEUED_${marker}`, { exact: false })
      .first()
      .waitFor();
  }
  await page.getByRole("button", { name: "Close conversation", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "Agent conversation", exact: true }).click();
  await page.getByRole("log").getByText(marker, { exact: false }).first().waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}/mobile-chat.png`, fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  const result = await context.request.get(web + conversation);
  assert.equal(result.status(), 200);
  const saved = await result.json();
  assert.ok(saved.messages.some((message) => message.role === "assistant" && message.body.includes(marker)));
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({ issue: fixture.issue, messages: saved.messages.length, state: saved.state, screenshots: output })
  );
} finally {
  await browser.close();
}
