"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require("playwright");

(async () => {
  const base = process.env.SEO_UI_BASE_URL || "http://127.0.0.1:8000";
  const envText = fs.existsSync(".env") ? fs.readFileSync(".env", "utf8") : "";
  const token = process.env.API_TOKEN || (envText.match(/^API_TOKEN=(.+)$/m) || [])[1];
  assert(token, "A private operator token is required");
  const browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1080}});
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  try {
    await page.goto(base);
    await page.locator("#token").fill(token.trim());
    await page.locator("#connect-form button").click();
    await page.locator("#workspace").waitFor({state: "visible"});
    await page.waitForFunction(() => document.getElementById("status").textContent.startsWith("Updated"));
    assert.equal(await page.locator("#source-badge").textContent(), "Fixture data");
    assert((await page.locator("#overview").textContent()).includes("Unknown"));
    assert.equal(await page.locator("#token").inputValue(), "");
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    fs.mkdirSync("artifacts", {recursive: true});
    await page.screenshot({path: "artifacts/dashboard-desktop.png", fullPage: true});
    await page.locator('button[data-view="pages"]').click();
    await page.locator("#table tbody tr").first().waitFor();
    await page.locator("#table button").first().click();
    assert(await page.locator("#record-dialog").isVisible());
    await page.locator("#close-dialog").click();
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('button[data-view="overview"]').click();
    await page.waitForFunction(() => document.getElementById("status").textContent.startsWith("Updated"));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    await page.screenshot({path: "artifacts/dashboard-mobile.png", fullPage: true});
    await page.locator("#disconnect").click();
    assert(await page.locator("#connection").isVisible());
    assert.equal(errors.length, 0, "Dashboard script errors");
    console.log("Dashboard browser checks passed; screenshots saved.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.name + ": " + error.message); process.exitCode = 1; });

