import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
const web = process.env.COORDINATION_WEB_URL || "http://127.0.0.1:3000";
const backend = process.env.COORDINATION_API_URL || "http://127.0.0.1:8000";
for (const url of [web, backend]) assert.equal(new URL(url).hostname, "127.0.0.1");
assert.ok(fixture.second_project, "Seed the two-project coordination browser fixture");
const output = process.env.COORDINATION_BROWSER_OUTPUT;
assert.ok(output, "Set COORDINATION_BROWSER_OUTPUT for browser evidence");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await context.addCookies([
  { name: fixture.session_name, value: fixture.owner_session, url: backend, httpOnly: true, sameSite: "Lax" },
]);
const page = await context.newPage();
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
const route = (project) => `/${fixture.workspace}/settings/projects/${project}/coordination/`;
const policy = async (project) => {
  const response = await context.request.get(
    `${backend}/api/workspaces/${fixture.workspace}/projects/${project}/coordination/policy/`
  );
  assert.equal(response.status(), 200);
  return response.json();
};
try {
  const before = await policy(fixture.second_project);
  await page.goto(web + route(fixture.second_project));
  await page.getByLabel("Repository (owner/name)").first().waitFor();
  assert.equal(await page.getByLabel("Repository (owner/name)").first().inputValue(), "example/second");
  const timeOrigin = await page.evaluate(() => performance.timeOrigin);
  const navigate = async (project) => {
    await page.evaluate((path) => window.__reactRouterDataRouter.navigate(path), route(project));
    await page.waitForURL(web + route(project));
    await page.getByLabel("Credential lifetime (seconds)").waitFor();
    assert.equal(await page.evaluate(() => performance.timeOrigin), timeOrigin, "Navigation must stay in the SPA");
  };
  await navigate(fixture.project);
  if ((await page.getByLabel("Repository (owner/name)").count()) === 0)
    await page.getByRole("button", { name: "Add repository", exact: true }).click();
  await page.getByLabel("Repository (owner/name)").first().fill("example/unsaved-first");
  await page.getByLabel("Base branch", { exact: true }).first().fill("first-only");
  await page.getByLabel("Credential lifetime (seconds)").fill("300");
  await page.getByLabel("Execution lease (seconds)").fill("600");
  await page.getByLabel("Identifier", { exact: true }).fill("UNSAVED");
  await page.getByLabel("Release name", { exact: true }).fill("First project draft");
  await navigate(fixture.second_project);
  assert.equal(await page.getByLabel("Repository (owner/name)").first().inputValue(), "example/second");
  assert.equal(await page.getByLabel("Base branch", { exact: true }).first().inputValue(), "develop");
  assert.equal(await page.getByLabel("Credential lifetime (seconds)").inputValue(), "600");
  assert.equal(await page.getByLabel("Execution lease (seconds)").inputValue(), "1200");
  assert.equal(await page.getByLabel("Identifier", { exact: true }).inputValue(), "");
  assert.equal(await page.getByLabel("Release name", { exact: true }).inputValue(), "");
  const contextReference = `https://example.com/second/${randomUUID()}`;
  await page.getByLabel("Context references").fill(contextReference);
  await page.getByRole("button", { name: "Save policy", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "Saved" }).waitFor();
  const after = await policy(fixture.second_project);
  assert.deepEqual(
    after,
    { ...before, context_references: [contextReference] },
    "Saving the second project must not copy the first project's draft"
  );
  await page.screenshot({ path: `${output}/policy-navigation-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${output}/policy-navigation-mobile.png`, fullPage: true });
  const unexpected = errors.filter(
    (message) =>
      !message.startsWith("Hydration failed because") && !message.startsWith("There was an error while hydrating")
  );
  assert.deepEqual(unexpected, []);
  process.stdout.write(
    JSON.stringify({
      result: "passed",
      scenarios: ["cached SPA navigation", "policy isolation", "release draft reset"],
      hydration_warnings: errors.length,
      output,
    })
  );
} catch (error) {
  await page.screenshot({ path: `${output}/policy-navigation-failure.png`, fullPage: true });
  throw error;
} finally {
  await browser.close();
}
