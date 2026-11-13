import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const modulePath = process.env.PLAYWRIGHT_MODULE || "playwright";
const { chromium } = require(modulePath);
const { expect } = require(modulePath + "/test");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
const web = fixture.base_url;
assert.equal(new URL(web).hostname, "127.0.0.1");
const project = process.env.AGENT_SETTINGS_PROJECT || fixture.project;
const prefix = `/api/workspaces/${fixture.workspace}/projects/${project}/coordination/`;
const output = process.env.COORDINATION_BROWSER_OUTPUT;
assert.ok(output);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: web });
await context.addCookies([
  { name: fixture.session_name, value: fixture.owner_session, url: web, httpOnly: true, sameSite: "Lax" },
]);
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const secretInput = page.getByLabel("Main agent API key", { exact: true });
const screenshot = (name) => page.screenshot({ path: `${output}/${name}.png`, fullPage: true, mask: [secretInput] });
const settingsUrl = `${web}/${fixture.workspace}/settings/projects/${project}/coordination/`;
let agent;
const issuedKeys = [];

async function keyStatus(raw) {
  const response = await context.request.get(web + prefix.replace("/api/", "/api/v1/") + "candidates/", {
    headers: { "X-Api-Key": raw },
  });
  return response.status();
}

async function failCreate(route) {
  if (route.request().method() === "POST")
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Registration unavailable" }),
    });
  else await route.continue();
}

try {
  await page.goto(settingsUrl);
  const section = page.getByRole("region", { name: "Registered agents", exact: true });
  const name = `UI main agent test ${Date.now()}`;
  const nameInput = section.getByRole("textbox", { name: "Main agent name", exact: true });
  await nameInput.fill(name);
  await page.route(web + prefix + "agents/", failCreate);
  await section.getByRole("button", { name: "Create main agent", exact: true }).click();
  await expect(section.getByRole("alert")).toHaveText("Registration unavailable");
  await expect(nameInput).toHaveValue(name);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await page.unroute(web + prefix + "agents/", failCreate);

  const createdResponse = page.waitForResponse(
    (response) => response.request().method() === "POST" && response.url() === web + prefix + "agents/"
  );
  await section.getByRole("button", { name: "Create main agent", exact: true }).click();
  const created = await createdResponse;
  assert.equal(created.status(), 201);
  assert.equal(created.headers()["cache-control"], "no-store");
  const result = await created.json();
  agent = result.agent;
  issuedKeys.push(result.credential);
  const dialog = page.getByRole("dialog", { name: "API key created", exact: true });
  await expect(dialog).toBeVisible();
  await expect(secretInput).toHaveAttribute("type", "password");
  assert.ok((await secretInput.inputValue()) === result.credential.token, "The displayed key matches issuance");
  await dialog.getByRole("button", { name: "Show API key", exact: true }).click();
  await expect(secretInput).toHaveAttribute("type", "text");
  await dialog.getByRole("button", { name: "Hide API key", exact: true }).click();
  await dialog.getByRole("button", { name: "Copy API key", exact: true }).click();
  assert.ok(
    (await page.evaluate(() => navigator.clipboard.readText())) === result.credential.token,
    "The copied key matches issuance"
  );
  assert.equal(await keyStatus(result.credential.token), 200);
  await screenshot("secret-dialog-desktop");
  await page.setViewportSize({ width: 390, height: 844 });
  const bounds = await dialog.boundingBox();
  assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 390);
  assert.ok(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth));
  await screenshot("secret-dialog-mobile");
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(secretInput).toHaveCount(0);
  await page.reload();
  await expect(secretInput).toHaveCount(0);
  await section.getByRole("button", { name: `Manage API keys for ${name}`, exact: true }).click();
  const keys = section.getByLabel(`API keys for ${name}`, { exact: true });
  await expect(keys.getByText(result.credential.id, { exact: true })).toBeVisible();
  const listed = await context.request.get(web + prefix + `agents/${agent.id}/credentials/`);
  assert.equal(listed.status(), 200);
  const metadata = await listed.json();
  assert.ok(metadata.every((key) => !("token" in key)));
  await screenshot("main-agent-mobile");

  const nextKeyResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url() === web + prefix + `agents/${agent.id}/credentials/`
  );
  await keys.getByRole("button", { name: "Create API key", exact: true }).click();
  const second = await (await nextKeyResponse).json();
  issuedKeys.push(second);
  await expect(dialog).toBeVisible();
  assert.ok((await secretInput.inputValue()) === second.token, "The new key matches issuance");
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  assert.equal(await keyStatus(second.token), 200);
  await keys.getByRole("button", { name: `Revoke API key ${second.id}`, exact: true }).click();
  const revoke = page.getByRole("dialog", { name: "Revoke API key?", exact: true });
  await expect(revoke).toBeVisible();
  await revoke.getByRole("button", { name: "Cancel", exact: true }).click();
  assert.equal(await keyStatus(second.token), 200);
  await keys.getByRole("button", { name: `Revoke API key ${second.id}`, exact: true }).click();
  const revokedResponse = page.waitForResponse(
    (response) => response.request().method() === "DELETE" && response.url().endsWith(`/${second.id}/`)
  );
  await revoke.getByRole("button", { name: "Revoke API key", exact: true }).click();
  assert.equal((await revokedResponse).status(), 204);
  await expect(revoke).toHaveCount(0);
  assert.equal(await keyStatus(second.token), 403);
  assert.equal(await keyStatus(result.credential.token), 200);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await keys.scrollIntoViewIfNeeded();
  await screenshot("main-agent-desktop");
  assert.deepEqual(errors, []);
  const report = {
    result: "passed",
    project,
    agent: agent.id,
    creation_failure_preserves_name: true,
    one_time_secret: true,
    copy_verified: true,
    scoped_key_usable: true,
    additional_key_and_revocation: true,
    desktop_mobile_fit: true,
    page_errors: errors,
  };
  await writeFile(`${output}/result.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} catch (error) {
  await screenshot("failure");
  throw error;
} finally {
  try {
    if (agent) {
      const csrfResponse = await context.request.get(web + "/auth/get-csrf-token/");
      const { csrf_token: csrf } = await csrfResponse.json();
      for (const key of issuedKeys) {
        // eslint-disable-next-line no-await-in-loop -- These revocations lock the same agent row.
        const response = await context.request.delete(web + prefix + `agents/${agent.id}/credentials/${key.id}/`, {
          headers: { "X-CSRFTOKEN": csrf, Origin: web, Referer: web },
        });
        assert.equal(response.status(), 204, "Revoke synthetic test keys without affecting existing agents");
      }
    }
  } finally {
    await context.close();
    await browser.close();
  }
}
