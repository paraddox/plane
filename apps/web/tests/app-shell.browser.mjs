import assert from "node:assert/strict";
import { readFile, mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
const { version } = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
const web = process.env.COORDINATION_WEB_URL || "http://127.0.0.1:3000";
assert.equal(new URL(web).hostname, "127.0.0.1", "Use only the disposable loopback stack");
const output = process.env.COORDINATION_BROWSER_OUTPUT;
assert.ok(output, "Set COORDINATION_BROWSER_OUTPUT for verification screenshots");
await mkdir(output, { recursive: true });
const browser = await chromium.launch({ channel: "chrome", headless: true });
const errors = [];
const checks = [];

try {
  async function checkTheme(theme) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: theme });
    await context.addInitScript((value) => localStorage.setItem("theme", value), theme);
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => {
      if (["error", "warning"].includes(message.type()) && /hydrat|server HTML|did not match/i.test(message.text()))
        errors.push(message.text());
    });
    await page.goto(web);
    await page.locator('input[name="email"]').first().waitFor();
    assert.deepEqual([...new Set(errors)], [], `${theme} sign-in must hydrate cleanly`);
    checks.push(`${theme} sign-in hydration`);

    await context.addCookies([
      { name: fixture.session_name, value: fixture.owner_session, url: web, httpOnly: true, sameSite: "Lax" },
    ]);
    await page.goto(`${web}/${fixture.workspace}/projects/${fixture.project}/issues/${fixture.issue}/`);
    const panel = page.getByRole("region", { name: "Agent workflow", exact: true });
    await panel.waitFor();
    assert.deepEqual([...new Set(errors)], [], `${theme} task must hydrate cleanly`);
    assert.equal(await page.locator('a[href="https://github.com/makeplane/plane"]').count(), 0);
    assert.equal(await page.getByText("Community", { exact: true }).count(), 0);
    const versionLabel = page.locator("#main-sidebar").getByText(`v${version}`, { exact: true });
    await versionLabel.waitFor();
    assert.equal(await versionLabel.locator("xpath=ancestor::button | ancestor::a").count(), 0);
    await page.screenshot({ path: `${output}/${theme}-desktop.png`, fullPage: true });
    checks.push(`${theme} task hydration and plain version footer`);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => {
      const sidebar = document.querySelector("#main-sidebar");
      return (
        sidebar instanceof HTMLElement && sidebar.style.width === "0px" && getComputedStyle(sidebar).opacity === "0"
      );
    });
    await panel.scrollIntoViewIfNeeded();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile page overflows");
    await page.screenshot({ path: `${output}/${theme}-mobile.png`, fullPage: true });
    checks.push(`${theme} mobile layout`);

    await page.goto(`${web}/${fixture.workspace}/settings/projects/${fixture.project}/coordination/`);
    await page.getByRole("heading", { name: "Execution Policy", exact: true }).waitFor();
    assert.deepEqual([...new Set(errors)], [], `${theme} settings must hydrate cleanly`);
    checks.push(`${theme} settings hydration`);
    await context.close();
  }
  await checkTheme("light");
  await checkTheme("dark");
  const result = { result: "passed", checks, hydration_errors: errors.length, version };
  await writeFile(`${output}/result.json`, JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} catch (error) {
  await writeFile(`${output}/failure.json`, JSON.stringify({ checks, errors: [...new Set(errors)] }, null, 2));
  throw error;
} finally {
  await browser.close();
}
