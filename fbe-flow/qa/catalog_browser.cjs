"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { chromium } = require("playwright");

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "fbe-catalog-browser-"));
  const metadata = path.join(temporary, "metadata.json");
  const server = spawn(process.env.FBE_QA_PYTHON || "python", ["-m", "qa.catalog_server", metadata], { cwd: path.resolve(__dirname, ".."), stdio: ["ignore", "pipe", "pipe"] });
  let serverLog = "", browser, page; const errors = [];
  server.stdout.on("data", (d) => { serverLog += d.toString(); });
  server.stderr.on("data", (d) => { serverLog += d.toString(); });
  try {
    for (let attempt = 0; attempt < 120; attempt++) {
      if (server.exitCode !== null) throw new Error("QA server exited: " + serverLog);
      try { if (fs.existsSync(metadata) && (await fetch("http://127.0.0.1:8767/health")).ok) break; } catch {}
      if (attempt === 119) throw new Error("QA server startup timed out: " + serverLog);
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    const meta = JSON.parse(fs.readFileSync(metadata, "utf8"));
    const root = "http://127.0.0.1:8767", api = root + "/api/sellers/" + meta.seller + "/catalog";
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1365, height: 900 }, acceptDownloads: true, extraHTTPHeaders: { "X-FBE-Flow": "1" } });
    page = await context.newPage();
    page.on("pageerror", (e) => errors.push(e.message));
    async function json(url) { const r = await context.request.get(url); assert.equal(r.status(), 200, await r.text()); return r.json(); }
    async function checkLayout() { const size = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth })); assert.ok(size.scroll <= size.width + 1, "Horizontal page overflow: " + JSON.stringify(size)); }
    async function screenshot(name) {
      const output = path.join(__dirname, "artifacts"); fs.mkdirSync(output, { recursive: true });
      const content = await page.screenshot({ path: path.join(output, name + ".jpg"), type: "jpeg", quality: 45 });
      if (process.env.FBE_QA_EMIT_IMAGES === "1") { const raw = content.toString("base64"); for (let i = 0; i < raw.length; i += 4000) console.log("FBE_SCREENSHOT " + name + " " + i + " " + raw.slice(i, i + 4000)); }
    }
    await page.goto(root + "/sellers/" + meta.seller + "/catalog");
    await page.locator("#catalog-products tr").first().waitFor();
    await checkLayout();
    await page.locator("#catalog-new").click();
    const productForm = page.locator("#catalog-product-form");
    await productForm.locator('[name="title"]').fill('Товар <img src=x onerror=alert(1)>');
    await productForm.locator('[name="sku"]').fill("QA-003");
    await productForm.locator('[name="gtins"]').fill(meta.gtin);
    await productForm.locator('[name="tnved"]').fill("3303001000");
    await productForm.locator('[name="okpd2"]').fill("20.42.11");
    await productForm.locator('[name="product_group"]').fill(meta.group);
    await productForm.locator('[name="length_mm"]').fill("125");
    await productForm.locator('[name="gross_weight_g"]').fill("250");
    await page.locator("#catalog-attribute-new").click();
    const attribute = page.locator("#catalog-attributes .catalog-attribute");
    await attribute.locator("input").nth(0).fill("__proto__");
    await attribute.locator("select").selectOption("json");
    await attribute.locator("input").nth(1).fill('{"safe":true}');
    await productForm.locator('[type="submit"]').click();
    await page.waitForFunction(() => document.getElementById("feedback").textContent === "Товар сохранён");
    const created = (await json(api + "/products?search=QA-003")).items[0];
    assert.deepEqual(created.attributes.__proto__, { safe: true });
    assert.equal(await page.locator("#catalog-product-dialog img").count(), 0);
    await page.locator("#catalog-document-new").click();
    const docForm = page.locator("#catalog-document-form");
    await docForm.locator('[name="number"]').fill("ДоС QA 001");
    await docForm.locator('[name="file"]').setInputFiles({ name: "qa.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\nQA fixture") });
    await docForm.locator('[type="submit"]').click();
    await page.waitForFunction(() => !document.getElementById("catalog-document-dialog").open);
    await page.locator("#catalog-documents").getByText("ДоС QA 001", { exact: true }).waitFor();
    await page.locator("#catalog-batch-new").click();
    const batchForm = page.locator("#catalog-batch-form");
    await batchForm.locator('[name="name"]').fill("Партия QA");
    await batchForm.locator('[name="quantity"]').fill("1");
    await batchForm.locator('[type="submit"]').click();
    await page.waitForFunction(() => !document.getElementById("catalog-batch-dialog").open);
    await page.locator("#catalog-batches").getByRole("button", { name: "Экземпляры и нанесение" }).click();
    await page.locator("#catalog-codes input[type=checkbox]").first().check();
    await page.locator("#catalog-code-submit").click();
    await page.waitForFunction(() => document.getElementById("catalog-codes").textContent.includes("Кодов пока нет"));
    await page.locator("#catalog-code-mode").selectOption("assigned");
    await page.locator("#catalog-codes input[type=checkbox]").first().check();
    await page.locator('#catalog-code-event-form [name="kind"]').selectOption("applied");
    await page.locator('#catalog-code-event-form [name="actor"]').fill("Оператор QA");
    await page.locator("#catalog-code-submit").click();
    await page.locator("#catalog-codes").getByText(/Маркировка нанесена/).waitFor();
    await page.waitForFunction(() => document.getElementById("feedback").textContent === "Событие оператора записано");
    assert.ok((await page.locator("#catalog-codes").textContent()).includes("EMITTED"));
    await page.locator('[data-close="catalog-codes-dialog"]').click();
    await checkLayout(); await screenshot("catalog-desktop-dialog");
    await page.locator('[data-close="catalog-product-dialog"]').click();
    await page.locator('[data-catalog-tab="rules"]').click();
    await page.locator("#catalog-rule-new").click();
    const ruleForm = page.locator("#catalog-rule-form");
    await ruleForm.locator('[name="title"]').fill("Правило QA");
    await ruleForm.locator('[name="product_group"]').fill(meta.group);
    await ruleForm.locator('[name="tnved_prefixes"]').fill("3303");
    await ruleForm.locator('[name="valid_from"]').fill("2020-01-01");
    await ruleForm.locator('[name="source_url"]').fill("https://example.test/qa");
    await ruleForm.locator('[name="source_note"]').fill("Изолированная проверка, не нормативное правило");
    await ruleForm.locator('[type="submit"]').click();
    await page.waitForFunction(() => !document.getElementById("catalog-rule-dialog").open);
    await page.locator('#catalog-schema-connection').selectOption(meta.ozon);
    await page.locator('#catalog-schema-form [name="category"]').fill("12:34");
    await page.locator('#catalog-schema-form [type="submit"]').click();
    await page.locator("#catalog-schemas").getByRole("button", { name: "Значения словаря" }).click();
    await page.locator("#catalog-dictionary-values").getByText("1 · Синий").waitFor();
    await page.locator("#catalog-dictionary-next").click();
    await page.locator("#catalog-dictionary-values").getByText("2 · Синий").waitFor();
    await page.locator('[data-close="catalog-dictionary-dialog"]').click();
    await page.locator('[data-catalog-tab="products"]').click();
    await page.locator("#catalog-products tr").filter({ hasText: "QA-003" }).getByRole("button", { name: "Открыть" }).click();
    await page.locator("#catalog-chz-connection").selectOption(meta.chz);
    await page.locator("#catalog-check").click();
    await page.locator("#catalog-check-result").getByText("Соответствие подтверждено по проверенным данным", { exact: true }).waitFor();
    await page.locator('[data-close="catalog-product-dialog"]').click();
    const dl = page.waitForEvent("download");
    await page.locator("#catalog-export-all").click();
    const sourceFile = await (await dl).path(), changedFile = path.join(temporary, "changed.xlsx");
    const changed = spawnSync(process.env.FBE_QA_PYTHON || "python", ["qa/edit_catalog_book.py", sourceFile, changedFile, created.id], { cwd: path.resolve(__dirname, ".."), encoding: "utf8" });
    assert.equal(changed.status, 0, changed.stderr);
    await page.locator('[data-catalog-tab="exchange"]').click();
    await page.locator('#catalog-upload [name="file"]').setInputFiles(changedFile);
    await page.locator('#catalog-upload [type="submit"]').click();
    await page.locator("#catalog-workbook").waitFor({ state: "visible" });
    await page.locator("#catalog-preview").click();
    await page.waitForFunction(() => document.getElementById("catalog-import-summary").textContent.includes("изменений: 1"));
    await page.locator("#catalog-apply").click();
    await page.waitForFunction(() => document.getElementById("catalog-import-summary").textContent.includes("Применён"));
    assert.equal(await page.locator("#catalog-apply").isDisabled(), true);
    assert.equal((await json(api + "/products/" + created.id)).brand, "Из XLSX");
    await checkLayout(); await screenshot("catalog-desktop-import");
    await page.locator('[data-catalog-tab="sources"]').click();
    await page.locator("#catalog-sources > article").filter({ has: page.getByRole("heading", { name: "WB источник", exact: true }) }).getByRole("button", { name: "Создать товар из варианта" }).first().click();
    await page.locator('#catalog-product-form [name="title"]').fill("WB товар QA");
    await page.locator('#catalog-product-form [type="submit"]').click();
    await page.waitForFunction(() => document.getElementById("catalog-product-heading").textContent === "WB товар QA");
    await page.locator('[data-close="catalog-product-dialog"]').click();
    assert.equal((await json(api + "/products?search=WB001")).items.length, 1);
    await page.locator("#catalog-source-search").getByRole("button", { name: "Найти" }).click();
    await page.locator("#catalog-sources").getByRole("button", { name: "Прочитать полные характеристики Ozon" }).click();
    await page.locator("#catalog-sources").getByText("После чтения", { exact: true }).first().waitFor();
    assert.equal((await json(api + "/products/" + created.id)).brand, "Из XLSX");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(root + "/sellers/" + meta.seller + "/catalog");
    await page.locator("#catalog-products tr").filter({ hasText: "QA-003" }).waitFor();
    await checkLayout(); await screenshot("catalog-mobile-list");
    await page.locator("#catalog-products tr").filter({ hasText: "QA-003" }).getByRole("button", { name: "Открыть" }).click();
    await checkLayout(); await screenshot("catalog-mobile-dialog");
    await page.locator('[data-close="catalog-product-dialog"]').click();
    const foreign = root + "/api/sellers/" + meta.other + "/catalog";
    assert.equal((await json(foreign + "/products")).total, 0);
    assert.equal((await context.request.get(foreign + "/products/" + created.id)).status(), 404);
    await page.locator("#seller-select").selectOption(meta.other);
    await page.waitForURL("**/sellers/" + meta.other + "/overview");
    await page.getByRole("link", { name: "Ассортимент", exact: true }).click();
    await page.getByText("Товаров пока нет", { exact: true }).waitFor();
    assert.equal(await page.getByText("QA-003", { exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    console.log("Browser acceptance passed: product, typed attributes, documents/files, batch application, rules/ChZ, schema dictionary, XLSX atomic import, WB variants, Ozon details, desktop/mobile and seller isolation.");
  } catch (error) {
    if (page) {
      try {
        const output = path.join(__dirname, "artifacts"); fs.mkdirSync(output, { recursive: true });
        const content = await page.screenshot({ path: path.join(output, "catalog-failure.jpg"), type: "jpeg", quality: 45 });
        if (process.env.FBE_QA_EMIT_IMAGES === "1") { const raw = content.toString("base64"); for (let i = 0; i < raw.length; i += 4000) console.log("FBE_SCREENSHOT catalog-failure " + i + " " + raw.slice(i, i + 4000)); }
        console.error("Browser diagnostics:", JSON.stringify({ errors, ui: await page.locator("#feedback, dialog[open] .catalog-dialog-error:not([hidden])").allTextContents() }));
      } catch (captureError) { console.error("Failure capture:", captureError.message); }
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
    await new Promise((resolve) => { if (server.exitCode !== null) return resolve(); server.once("exit", resolve); setTimeout(resolve, 5000).unref(); });
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
