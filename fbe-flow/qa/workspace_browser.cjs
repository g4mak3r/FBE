"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { chromium } = require("playwright");

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "fbe-workspace-browser-"));
  const metadata = path.join(temporary, "metadata.json");
  const server = spawn(process.env.FBE_QA_PYTHON || "python", ["-m", "qa.catalog_server", metadata], { cwd: path.resolve(__dirname, ".."), stdio: ["ignore", "pipe", "pipe"] });
  let log = "", browser, page; const errors = [];
  server.stdout.on("data", d => { log += d.toString(); }); server.stderr.on("data", d => { log += d.toString(); });
  const artifacts = path.join(__dirname, "artifacts"); fs.mkdirSync(artifacts, { recursive: true });
  try {
    for (let attempt = 0; attempt < 120; attempt++) {
      if (server.exitCode !== null) throw new Error("QA server exited: " + log);
      try { if (fs.existsSync(metadata) && (await fetch("http://127.0.0.1:8767/health")).ok) break; } catch {}
      if (attempt === 119) throw new Error("QA server startup timed out: " + log);
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    const meta = JSON.parse(fs.readFileSync(metadata, "utf8"));
    const root = "http://127.0.0.1:8767", seller = root + "/sellers/" + meta.seller, api = root + "/api/sellers/" + meta.seller;
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1365, height: 900 }, extraHTTPHeaders: { "X-FBE-Flow": "1" }, acceptDownloads: true });
    page = await context.newPage(); page.on("pageerror", e => errors.push(e.message));
    async function json(url) { const r = await context.request.get(url); assert.equal(r.status(), 200, await r.text()); return r.json(); }
    async function layout() { const v = await page.evaluate(() => [innerWidth, document.documentElement.scrollWidth]); assert.ok(v[1] <= v[0] + 1, "Page overflow " + v); }
    async function shot(name) { await layout(); await page.screenshot({ path: path.join(artifacts, name + ".jpg"), type: "jpeg", quality: 55 }); }
    await page.goto(seller + "/overview");
    await page.locator('[data-widget="assembly"] .widget-link').first().waitFor();
    assert.equal(await page.locator("#primary-navigation a").count(), 5);
    assert.equal(await page.locator("#worker-status").count(), 0);
    const dashboard = await json(api + "/workspace");
    assert.equal(dashboard.widgets[0].rows.find(r => r.channel === "wb").count, 2);
    assert.equal(dashboard.widgets[0].rows.find(r => r.channel === "ozon").count, 1);
    await shot("workspace-desktop");
    await page.locator("#workspace-configure").click();
    let first = page.locator(".widget-editor").first();
    await first.getByLabel("Канал виджета").selectOption("wb");
    const wbWarehouse = dashboard.warehouses.find(w => w.adapter_key === "wb");
    first = page.locator(".widget-editor").first();
    await first.getByLabel("Склад виджета").selectOption(wbWarehouse.id);
    await first.getByLabel("Статус виджета").selectOption("new");
    await page.locator("#workspace-new-kind").selectOption("codes");
    await page.locator("#workspace-add").click();
    await page.locator(".widget-editor").last().getByRole("button", { name: "Выше", exact: true }).click();
    await page.locator('#workspace-layout-form [type="submit"]').click();
    await page.waitForFunction(() => !document.getElementById("workspace-dialog").open);
    await page.reload(); await page.locator('[data-widget="assembly"] .widget-count').waitFor();
    assert.equal((await json(api + "/workspace")).layout.widgets[2].kind, "codes");
    assert.equal(await page.locator('[data-widget="assembly"] .widget-count').textContent(), "1");
    await page.locator('[data-widget="assembly"] .widget-link').click();
    await page.locator("#wb-records tr").filter({ hasText: "#101" }).waitFor();
    assert.equal(await page.locator("#wb-records tr").count(), 1);
    assert.ok((await page.locator("#wb-records").textContent()).includes("Основной WB"));
    await page.waitForFunction(() => document.getElementById("wb-supply-select").options.length > 1);
    await page.getByRole("button", { name: "#101", exact: true }).click();
    await page.locator("#wb-order-dialog .action-context").getByText(/Проверка ассортимента/).waitFor();
    await page.locator("#wb-order-dialog .wb-close").click();
    assert.equal(await page.locator("#wb-order-tools").isVisible(), false);
    await page.locator('#wb-records input[type="checkbox"]').check();
    assert.equal(await page.locator("#wb-order-tools").isVisible(), true);
    await shot("sales-wb-desktop");
    await page.locator('[data-marketplace="ozon"]').click();
    await page.locator("#commerce-records tr").first().waitFor();
    await page.locator('#commerce-search [name="stage"]').selectOption("shipping");
    await page.locator('#commerce-search [type="submit"]').click();
    await page.waitForFunction(() => document.querySelectorAll("#commerce-records tr").length === 1 && document.getElementById("commerce-records").textContent.includes("QA-100-1"));
    assert.equal(await page.locator("#commerce-records tr").count(), 1);
    await page.goto(seller + "/settings?tab=integrations");
    const ozonCard = page.locator(".integration-card").filter({ has: page.locator('[data-connection="' + meta.ozon + '"]') });
    await ozonCard.locator("summary").first().click();
    await ozonCard.getByRole("button", { name: "Проверить доступ", exact: true }).click();
    await ozonCard.getByText("Аккаунт подтвердил доступ", { exact: true }).waitFor();
    await ozonCard.locator('.credential-form [name="token"]').fill("rotated-qa-token");
    await ozonCard.locator('.credential-form [type="submit"]').click();
    await ozonCard.getByText("Доступ обновлен", { exact: true }).waitFor();
    await page.locator('#store-name-form [name="name"]').fill("Мой магазин");
    await Promise.all([page.waitForEvent("load"), page.locator('#store-name-form [type="submit"]').click()]);
    await shot("settings-desktop");
    await page.goto(seller + "/settings?tab=application");
    await page.locator('#application-form [name="operator"]').fill("Оператор QA");
    await page.locator('#application-form [name="density"]').selectOption("comfortable");
    await page.locator('#application-form [name="refresh_seconds"]').fill("30");
    await Promise.all([page.waitForEvent("load"), page.locator('#application-form [type="submit"]').click()]);
    assert.equal(await page.locator("body").getAttribute("data-density"), "comfortable");
    await page.locator("#backup-create").click(); await page.locator("#backup-result a").waitFor();
    const downloadEvent = page.waitForEvent("download"); await page.locator("#backup-result a").click();
    assert.ok((await downloadEvent).suggestedFilename().endsWith(".sqlite3"));
    await page.goto(seller + "/settings?tab=printing");
    await page.locator('#printing-form [name="width_mm"]').fill("58");
    await page.locator('#printing-form [name="height_mm"]').fill("40");
    await page.locator('#printing-form [type="submit"]').click();
    await page.getByText("Размер этикетки сохранен", { exact: true }).waitFor();
    const popupEvent = page.waitForEvent("popup"); await page.locator("#print-test-link").click();
    const popup = await popupEvent; await popup.waitForLoadState();
    assert.equal(await popup.locator(".test-label").count(), 1); await popup.close();
    await page.goto(seller + "/sales?channel=kit"); await page.locator("#commerce-records").getByText("KIT-2001", { exact: true }).waitFor();
    await page.getByRole("link", { name: "Мой магазин", exact: true }).waitFor();
    await page.goto(seller + "/marking");
    await page.locator('#chz-catalog-pick [name="search"]').fill("001");
    await page.locator('#chz-catalog-pick [type="submit"]').click();
    await page.waitForFunction(() => document.getElementById("chz-catalog-product").options.length > 1);
    await page.locator("#chz-catalog-product").selectOption(meta.initial);
    await page.locator("#chz-catalog-selection").getByText(/GTIN/).waitFor();
    await page.getByRole("tab", { name: "Каталог НК", exact: true }).click();
    await page.locator("#chz-products").waitFor(); await shot("marking-desktop");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(seller + "/overview"); await page.locator('[data-widget="assembly"]').waitFor(); await shot("workspace-mobile");
    await page.locator("#navigation-toggle").click(); await page.getByRole("link", { name: "Настройки", exact: true }).click();
    await page.getByRole("link", { name: "Интеграции", exact: true }).click(); await shot("settings-mobile");
    await page.locator("#seller-select").selectOption(meta.other); await page.waitForURL("**/sellers/" + meta.other + "/overview");
    await page.getByText("Подключите первую систему", { exact: true }).waitFor();
    assert.equal((await json(root + "/api/sellers/" + meta.other + "/workspace")).layout.widgets.length, 3);
    assert.equal(await page.getByText("WB — проверка", { exact: true }).count(), 0);
    await page.goto(root + "/"); assert.ok(page.url().includes(meta.other));
    assert.deepEqual(errors, []);
    console.log("Workspace browser acceptance passed: real counts/deep links, widget filters/order/persistence, WB selection/detail, Ozon filters, settings/token renewal, custom KIT name, preferences/backup/print, catalog-driven ChZ, desktop/mobile and organization isolation.");
  } catch (error) {
    if (page) await page.screenshot({ path: path.join(artifacts, "workspace-failure.jpg"), type: "jpeg", quality: 65 }).catch(() => {});
    console.error(log); throw error;
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
    await Promise.race([new Promise(resolve => server.once("exit", resolve)), new Promise(resolve => setTimeout(resolve, 2000))]);
    if (server.exitCode === null) server.kill("SIGKILL");
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
