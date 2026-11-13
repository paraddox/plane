import assert from "node:assert/strict";
import { mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const fixture = JSON.parse(await readFile(process.env.COORDINATION_FIXTURE, "utf8"));
const web = process.env.COORDINATION_WEB_URL || fixture.base_url;
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
context.on("page", (tab) => tab.on("pageerror", (error) => errors.push(error.message)));
page.on("pageerror", (error) => errors.push(error.message));
const inbox = `/${fixture.workspace}/notifications/`;
const back = (tab) => tab.getByRole("button", { name: "Go back", exact: true });

// Opening a test notification must not change the human user's unread Inbox.
await context.route(`**/api/workspaces/${fixture.workspace}/users/notifications/*/read/`, (route) =>
  route.fulfill({ status: 200, json: {} })
);

try {
  const origins = [
    `/${fixture.workspace}/projects/${fixture.project}/issues/?navigation-test=1#work-items`,
    `/${fixture.workspace}/settings/projects/${fixture.project}/coordination/`,
  ];
  const checkReturn = async (origin) => {
    await page.goto(web + origin);
    assert.equal(await back(page).count(), 0);
    await page.locator(`a[href="${inbox}"]`).first().click();
    await back(page).waitFor();
    await page.getByText("Mentions", { exact: true }).click();
    await page.reload();
    await back(page).click();
    await page.waitForURL(web + origin);
  };
  await checkReturn(origins[0]);
  await checkReturn(origins[1]);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(web + origins[0]);
  await page.locator(`a[href="${inbox}"]`).first().click();
  await back(page).waitFor({ state: "visible" });
  const box = await back(page).boundingBox();
  await page.screenshot({ path: `${output}/inbox-mobile.png`, fullPage: true });
  assert.ok(box && box.x >= 0 && box.x + box.width <= 390 && box.y >= 0, JSON.stringify(box));
  await page.getByPlaceholder("Search commands...").click();
  await page.getByRole("listbox").waitFor();
  await page.screenshot({ path: `${output}/inbox-search-mobile.png`, fullPage: true, animations: "disabled" });
  const searchBox = await page.getByRole("listbox").boundingBox();
  assert.ok(
    searchBox && searchBox.x >= 0 && searchBox.x + searchBox.width <= 390 && searchBox.y >= 0 && searchBox.height > 0,
    JSON.stringify(searchBox)
  );
  await page.keyboard.press("Escape");
  await page.getByRole("listbox").waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "Search commands", exact: true }).focus();
  await page.keyboard.press("Enter");
  await page.getByRole("listbox").waitFor();
  await page.keyboard.press("Escape");
  await page.getByRole("listbox").waitFor({ state: "hidden" });
  await page
    .getByText(/Main-to-worker handoff/)
    .first()
    .click();
  await page.getByPlaceholder("Work item title").waitFor();
  await back(page).waitFor({ state: "visible" });
  await page.screenshot({ path: `${output}/inbox-notification-mobile.png`, fullPage: true });
  await back(page).click();
  await page.waitForURL(web + origins[0]);

  const direct = await context.newPage();
  await direct.goto(web + inbox);
  await back(direct).click();
  await direct.waitForURL(`${web}/${fixture.workspace}/`);
  await direct.close();

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator(`a[href="${inbox}"]`).first().click();
  await back(page).waitFor();
  await page.screenshot({ path: `${output}/inbox-desktop.png`, fullPage: true });
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      result: "passed",
      origins: origins.length,
      reload: true,
      mobile: true,
      notification: true,
      direct: true,
    })
  );
} finally {
  await browser.close();
}
