"use strict";
// The CI browser gate also exports a small local static server for visual QA.
// Public mode reads the deployed lab; it never grants analytics consent.
const fs = require("node:fs/promises");
const { isIP } = require("node:net");

function validatePublicOrigin(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.pathname !== "/" || parsed.search || parsed.hash
      || (parsed.port && parsed.port !== "443") || isIP(parsed.hostname.replace(/^\[|\]$/g, ""))
      || !parsed.hostname.includes(".") || /\.(?:test|invalid|localhost|local|internal|home|arpa)$/.test(parsed.hostname)) {
    throw new Error("Use a credential-free public HTTPS lab origin without a path, query or fragment");
  }
  return parsed.origin;
}

function inventoryTargets(inventory, baseUrl) {
  if (inventory?.schema_version !== 1 || !Array.isArray(inventory.pages) || inventory.pages.length < 20 || inventory.pages.length > 30) {
    throw new Error("Use a bounded 20–30 page lab inventory");
  }
  const base = new URL(baseUrl);
  const seen = new Set();
  return inventory.pages.map(entry => {
    if (typeof entry.path !== "string" || entry.path.length > 500 || !/^\/(?:[a-z0-9]+(?:-[a-z0-9]+)*\/)*$/.test(entry.path) || seen.has(entry.path)) {
      throw new Error("Inventory paths must be unique root-relative directory routes");
    }
    seen.add(entry.path);
    const target = new URL(entry.path, base);
    if (target.origin !== base.origin || target.username || target.password) throw new Error("Inventory route escaped the lab origin");
    return { path: entry.path, url: target.href };
  });
}

async function run() {
  const assert = require("node:assert/strict");
  const { startStaticServer } = await import("./lab_static_server.mjs");
  const { chromium } = require("playwright");
  const directory = process.env.LAB_BUILD_DIR || "artifacts/test-lab-site";
  const local = process.env.LAB_PUBLIC_URL ? null : await startStaticServer(directory);
  const baseUrl = local ? local.baseUrl : validatePublicOrigin(process.env.LAB_PUBLIC_URL);
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route("**/*", route => {
      const target = new URL(route.request().url());
      return target.origin === new URL(baseUrl).origin && !target.username && !target.password
        ? route.continue() : route.abort("blockedbyclient");
    });
    const tab = await context.newPage();
    const scriptErrors = [], externalRequests = [];
    tab.on("pageerror", error => scriptErrors.push(error.message));
    tab.on("request", request => { if (new URL(request.url()).origin !== new URL(baseUrl).origin) externalRequests.push(request.url()); });
    const inventoryResponse = await tab.goto(baseUrl + "/inventory.json");
    assert.equal(inventoryResponse.status(), 200);
    const inventory = await inventoryResponse.json();
    const targets = inventoryTargets(inventory, baseUrl);
    for (const entry of targets) {
      const response = await tab.goto(entry.url, { waitUntil: "networkidle" });
      assert.equal(response.status(), 200, entry.path);
      assert.equal(new URL(tab.url()).origin, new URL(baseUrl).origin);
      assert.ok(await tab.locator("main").count(), entry.path);
      assert.ok(await tab.getByText("Demonstration / test project.", { exact: false }).count(), entry.path);
      assert.ok(await tab.title(), entry.path);
      assert.ok(await tab.locator('link[rel="canonical"]').getAttribute("href"), entry.path);
    }
    await fs.mkdir("artifacts", { recursive: true });
    await tab.goto(baseUrl + "/");
    await tab.screenshot({ path: "artifacts/test-lab-desktop.png", fullPage: true });
    await tab.goto(baseUrl + "/exercises/");
    const complete = tab.locator("#complete-checklist");
    assert.equal(await complete.isEnabled(), false);
    const checks = tab.locator('input[name="lab-step"]');
    assert.equal(await checks.count(), 3);
    for (let index = 0; index < 3; index++) await checks.nth(index).check();
    assert.equal(await complete.isEnabled(), true);
    await complete.click();
    assert.equal(await complete.isEnabled(), false);
    assert.match(await tab.locator("#exercise-result").innerText(), /local; no analytics event was sent/);
    assert.deepEqual(externalRequests, []);
    await tab.setViewportSize({ width: 390, height: 844 });
    await tab.screenshot({ path: "artifacts/test-lab-mobile.png", fullPage: true });
    assert.equal(await tab.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    const missing = await tab.goto(baseUrl + "/this-path-is-intentionally-absent/");
    assert.equal(missing.status(), 404);
    assert.deepEqual(scriptErrors, []);
    const report = { status: "passed", scope: local ? "local_http_render" : "public_render", base_url: baseUrl,
      pages_verified: inventory.pages.length, practice_event: "lab_checklist_complete", analytics_receipt_verified: false,
      unexpected_external_requests: 0, script_errors: 0, real_404: true, production_writes: 0,
      checked_at: new Date().toISOString() };
    await fs.writeFile("artifacts/test-lab-browser.json", JSON.stringify(report, null, 2) + "\n");
    console.log(JSON.stringify(report));
  } finally {
    await browser.close();
    if (local) await new Promise(resolve => local.server.close(resolve));
  }
}

module.exports = { run, validatePublicOrigin, inventoryTargets };
if (require.main === module) run().catch(error => { console.error(error.message); process.exitCode = 1; });
