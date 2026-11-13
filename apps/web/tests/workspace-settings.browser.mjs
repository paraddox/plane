import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
const web = process.env.COORDINATION_WEB_URL || fixture.base_url;
assert.equal(new URL(web).hostname, "127.0.0.1");
const project = process.env.SETTINGS_PROJECT_ID || fixture.project;
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

try {
  await page.goto(`${web}/${fixture.workspace}/settings/`);
  await page.getByRole("link", { name: "General", exact: true }).waitFor();
  assert.equal(await page.getByRole("link", { name: "Billing & Plans", exact: true }).count(), 0);
  assert.equal(await page.locator('a[href*="/settings/billing"]').count(), 0);
  await page.getByRole("link", { name: "Members", exact: true }).waitFor();
  await page.screenshot({ path: `${output}/workspace-settings.png`, fullPage: true });

  await page.goto(`${web}/${fixture.workspace}/settings/billing/`);
  await page.getByText(/Sorry, the page you are looking for cannot be found/).waitFor();
  assert.equal(await page.getByRole("heading", { name: "Billing & Plans", exact: true }).count(), 0);

  const releasesUrl = `${web}/${fixture.workspace}/settings/projects/${project}/coordination/`;
  await page.goto(releasesUrl);
  const policy = page.getByRole("region", { name: "Execution policy", exact: true });
  await policy.getByLabel("Agent execution enabled").waitFor();
  assert.equal(await policy.getByLabel("Repository (owner/name)", { exact: true }).count(), 0);
  assert.equal(await policy.getByLabel("Base branch", { exact: true }).count(), 0);
  assert.equal(await policy.getByLabel("Credential lifetime (seconds)", { exact: true }).count(), 0);
  assert.equal(await policy.getByLabel("Execution lease (seconds)", { exact: true }).count(), 0);
  assert.equal(await policy.getByLabel("Required CI checks", { exact: true }).count(), 1);
  const releases = page.getByRole("region", { name: "Project releases", exact: true });
  await releases.getByRole("heading", { name: "Project Releases", exact: true }).waitFor();
  await releases.getByRole("button", { name: "Create release", exact: true }).waitFor();
  assert.equal(await releases.getByLabel("Identifier", { exact: true }).count(), 1);
  assert.equal(await releases.getByLabel("Release name", { exact: true }).count(), 1);
  await releases.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${output}/project-releases-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await releases.scrollIntoViewIfNeeded();
  assert.ok(await releases.evaluate((element) => element.scrollWidth <= element.clientWidth));
  await page.screenshot({ path: `${output}/project-releases-mobile.png`, fullPage: true });
  await policy.scrollIntoViewIfNeeded();
  assert.ok(await policy.evaluate((element) => element.scrollWidth <= element.clientWidth));
  await page.screenshot({ path: `${output}/agent-policy-mobile.png`, fullPage: true });
  await page.goto(`${web}/settings/profile/api-tokens/`);
  await page.getByRole("button", { name: "Add personal access token", exact: true }).waitFor();
  assert.deepEqual(errors, []);
  const report = {
    result: "passed",
    billing_navigation_removed: true,
    billing_page_removed: true,
    release_creation_form_available: true,
    repository_and_timed_credential_settings_removed: true,
    personal_api_key_page_available: true,
    releases_url: releasesUrl,
    page_errors: errors,
  };
  await writeFile(`${output}/result.json`, JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
