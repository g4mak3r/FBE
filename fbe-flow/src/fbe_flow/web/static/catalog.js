"use strict";
(() => {
  if (!document.getElementById("catalog-root")) return;
  const base = sellerApi + "/catalog", $ = (id) => document.getElementById(id);
  const labels = {
    title: "Наименование", sku: "Артикул FBE", family: "Семейство / модель", brand: "Бренд",
    manufacturer: "Производитель", country: "Страна производства", description: "Описание",
    composition: "Состав", volume_ml: "Объём, мл", net_weight_g: "Масса нетто, г",
    gross_weight_g: "Масса брутто, г", length_mm: "Длина упаковки, мм",
    width_mm: "Ширина упаковки, мм", height_mm: "Высота упаковки, мм",
    package_quantity: "Количество в упаковке", shelf_life_days: "Срок годности, дней",
    tnved: "ТН ВЭД (10 цифр)", okpd2: "ОКПД2", gtins: "GTIN (по одному в строке)",
    barcodes: "Штрихкоды (по одному в строке)", product_group: "Ключ группы ЧЗ",
    marking_attestation: "Заявление о нанесении для карточки", archived: "В архиве"
  };
  const numbers = new Set(["volume_ml", "net_weight_g", "gross_weight_g", "length_mm", "width_mm", "height_mm", "package_quantity", "shelf_life_days"]);
  const ints = new Set(["package_quantity", "shelf_life_days"]);
  let connections = [], productPage = null, sourcePage = null, offset = 0, sourceOffset = 0;
  let editorID = null, editorRevision = null, adoption = null, current = null, sourceTarget = null;
  let docEdit = null, batchEdit = null, batchTarget = null, codeOffset = 0, codePage = null;
  let ruleEdit = null, allDocuments = [], ruleItems = [], dictionaryTarget = null;
  let upload = null, inspection = null, importPlan = null;
  const selected = new Set(), selectedCodes = new Set();
  const split = (s) => String(s || "").split(/[\n;,]+/).map((v) => v.trim()).filter(Boolean);
  const node = (tag, text, cls) => {
    const n = document.createElement(tag); if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls; return n;
  };
  function errorMessage(data) {
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map((v) => (v.loc || []).slice(1).join(".") + ": " + v.msg).join("; ");
    return "Не удалось выполнить действие";
  }
  async function request(path, method = "GET", body) {
    const r = await fetch(base + path, { method, headers: { "Content-Type": "application/json", "X-FBE-Flow": "1" }, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await r.json(); if (!r.ok) throw new Error(errorMessage(data)); return data;
  }
  function notify(text) { feedback.textContent = text; feedback.hidden = false; }
  function fail(error) {
    const dialogs = Array.from(document.querySelectorAll("dialog[open]"));
    const panel = dialogs.at(-1)?.querySelector(".catalog-dialog-error");
    if (panel) { panel.textContent = error.message; panel.hidden = false; } else showError(error);
  }
  async function run(button, fn) {
    const wasDisabled = button?.disabled;
    if (button) { button.disabled = true; button.dataset.catalogBusy = "true"; }
    try { await fn(); } catch (e) { fail(e); } finally { if (button) { button.disabled = wasDisabled; delete button.dataset.catalogBusy; } syncActionButtons(); }
  }
  function syncActionButtons() {
    const set = (id, value) => { if (!$(id).dataset.catalogBusy) $(id).disabled = value; };
    set("catalog-prev", offset === 0); set("catalog-next", !productPage || offset + 50 >= productPage.total);
    set("catalog-source-prev", sourceOffset === 0); set("catalog-source-next", !sourcePage || sourceOffset + 30 >= sourcePage.total);
    set("catalog-code-prev", codeOffset === 0); set("catalog-code-next", !codePage || codeOffset + 100 >= codePage.total);
    set("catalog-export-selected", selected.size === 0); set("catalog-code-submit", selectedCodes.size === 0);
    set("catalog-apply", !importPlan || importPlan.state !== "prepared" || importPlan.errors.length > 0);
    set("catalog-cancel-import", !importPlan || importPlan.state !== "prepared");
    set("catalog-fill", !inspection || $("catalog-profile").value === "fbe" || selected.size === 0);
    set("catalog-dictionary-next", !dictionaryTarget?.has_next);
  }
  function on(id, fn, type = "click") { $(id).addEventListener(type, (e) => { if (type === "submit") e.preventDefault(); run(type === "submit" ? e.currentTarget.querySelector('[type="submit"]') : e.currentTarget, () => fn(e)); }); }
  function button(text, fn) { const b = node("button", text, "secondary"); b.type = "button"; b.addEventListener("click", () => run(b, fn)); return b; }
  function open(id) { const d = $(id); const e = d.querySelector(".catalog-dialog-error"); if (e) e.hidden = true; if (!d.open) d.showModal(); }
  function jsonDetails(parent, title, data) { const d = node("details"); d.append(node("summary", title), node("pre", JSON.stringify(data, null, 2))); parent.append(d); }
  function selectOptions(select, entries, placeholder) {
    const old = select.value; select.replaceChildren();
    if (placeholder !== undefined) { const o = node("option", placeholder); o.value = ""; select.append(o); }
    for (const [value, text] of entries) { const o = node("option", text); o.value = value; select.append(o); }
    if (entries.some((v) => String(v[0]) === old)) select.value = old;
  }
  function card(title, sub) { const a = node("article", undefined, "catalog-card"); a.append(node("h3", title)); if (sub) a.append(node("p", sub, "muted")); return a; }
  const tr = (values) => { const row = node("tr"); for (const v of values) { const c = node("td"); c.append(v instanceof Node ? v : node("span", v)); row.append(c); } return row; };
  function selection() { $("catalog-selection").textContent = "Выбрано: " + selected.size; $("catalog-export-selected").disabled = selected.size === 0; }
  async function download(path, name, body) {
    const r = await fetch(base + path, { method: body ? "POST" : "GET", headers: { "Content-Type": "application/json", "X-FBE-Flow": "1" }, body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) throw new Error(errorMessage(await r.json()));
    saveBlob(await r.blob(), name);
  }
  function saveBlob(blob, name) { const url = URL.createObjectURL(blob), a = node("a"); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  async function readFile(file, max = 32 * 1024 * 1024) {
    if (!file || file.size > max) throw new Error("Выберите файл размером до " + (max / 1024 / 1024) + " МБ");
    const bytes = new Uint8Array(await file.arrayBuffer()); let text = "";
    for (let i = 0; i < bytes.length; i += 32768) text += String.fromCharCode(...bytes.subarray(i, i + 32768));
    return { filename: file.name, content: btoa(text) };
  }
  async function stats() {
    const s = await request("/stats");
    $("catalog-stats").textContent = "Товаров: " + s.total + " · активных: " + s.active + " · без GTIN: " + s.without_gtin + " · без ТН ВЭД: " + s.without_tnved + " · исходных карточек без связей: " + s.unlinked_sources;
  }
  async function loadProducts() {
    const form = $("catalog-search"), q = new URLSearchParams({ offset, limit: 50, search: form.search.value, archived: form.archived.value });
    productPage = await request("/products?" + q);
    const body = $("catalog-products"); body.replaceChildren();
    for (const p of productPage.items) {
      const check = node("input"); check.type = "checkbox"; check.checked = selected.has(p.id); check.setAttribute("aria-label", "Выбрать " + p.sku);
      check.addEventListener("change", () => { if (check.checked) selected.add(p.id); else selected.delete(p.id); selection(); });
      body.append(tr([check, p.title, p.sku, p.gtins.join("\n"), (p.tnved || "Не задан") + "\n" + (p.product_group || "Группа не задана"), button("Открыть", () => editProduct(p.id))]));
    }
    if (!productPage.items.length) body.append(tr(["", "Товаров пока нет", "", "", "", ""]));
    $("catalog-page").textContent = productPage.total ? (offset + 1) + "–" + (offset + productPage.items.length) + " из " + productPage.total : "0 товаров";
    $("catalog-prev").disabled = offset === 0; $("catalog-next").disabled = offset + 50 >= productPage.total;
    $("catalog-select-page").checked = productPage.items.length > 0 && productPage.items.every((p) => selected.has(p.id)); selection();
  }
  on("catalog-search", async () => { offset = 0; await loadProducts(); }, "submit");
  on("catalog-prev", async () => { offset = Math.max(0, offset - 50); await loadProducts(); });
  on("catalog-next", async () => { offset += 50; await loadProducts(); });
  on("catalog-select-page", () => { for (const p of productPage.items) { if ($("catalog-select-page").checked) selected.add(p.id); else selected.delete(p.id); } return loadProducts(); }, "change");
  on("catalog-export-selected", () => download("/xlsx/export", "FBE-assortment-selected.xlsx", { product_ids: Array.from(selected) }));
  for (const tab of document.querySelectorAll("[data-catalog-tab]")) {
    tab.addEventListener("click", () => run(tab, async () => {
      for (const t of document.querySelectorAll("[data-catalog-tab]")) { const active = t === tab; t.setAttribute("aria-selected", String(active)); t.classList.toggle("secondary", !active); }
      for (const p of document.querySelectorAll("[data-catalog-panel]")) p.hidden = p.dataset.catalogPanel !== tab.dataset.catalogTab;
      if (tab.dataset.catalogTab === "sources") await loadSources();
      if (tab.dataset.catalogTab === "rules") await loadRules();
    }));
  }
  for (const b of document.querySelectorAll("[data-close]")) b.addEventListener("click", () => $(b.dataset.close).close());
  function addAttribute(key = "", value = "") {
    const row = node("div", undefined, "catalog-attribute"), name = node("input"), type = node("select"), input = node("input");
    name.value = key; name.placeholder = "Имя"; name.setAttribute("aria-label", "Имя характеристики");
    const kind = value === null ? "null" : typeof value === "object" ? "json" : typeof value;
    selectOptions(type, [["string", "Текст"], ["number", "Число"], ["boolean", "Логическое"], ["null", "Null"], ["json", "JSON"]]); type.value = kind;
    type.setAttribute("aria-label", "Тип характеристики"); input.value = kind === "string" ? value : JSON.stringify(value); input.setAttribute("aria-label", "Значение характеристики");
    row.append(name, type, input, button("Удалить", () => row.remove())); $("catalog-attributes").append(row);
  }
  function typedValue(kind, value) {
    if (kind === "string") return value; if (kind === "null") return null;
    let result; try { result = JSON.parse(value); } catch { throw new Error("Проверьте число, логическое значение или JSON"); }
    if (kind === "number" && (typeof result !== "number" || !Number.isFinite(result))) throw new Error("Ожидается конечное число");
    if (kind === "boolean" && typeof result !== "boolean") throw new Error("Ожидается true или false");
    return result;
  }
  function buildProductFields(data) {
    const container = $("catalog-product-fields"); container.replaceChildren();
    for (const [key, label] of Object.entries(labels)) {
      const l = node("label", label); let input;
      if (["archived", "marking_attestation"].includes(key)) {
        input = node("select"); selectOptions(input, key === "archived" ? [["false", "Нет"], ["true", "Да"]] : [["", "Не задано"], ["true", "Да"], ["false", "Нет"]]);
        input.value = data[key] === null || data[key] === undefined ? "" : String(data[key]);
        if (key === "archived" && !input.value) input.value = "false";
      } else {
        input = node(["description", "composition", "gtins", "barcodes"].includes(key) ? "textarea" : "input");
        input.value = Array.isArray(data[key]) ? data[key].join("\n") : data[key] ?? (key === "package_quantity" ? 1 : "");
        if (numbers.has(key)) { input.inputMode = ints.has(key) ? "numeric" : "decimal"; }
        if (["title", "sku"].includes(key)) { input.required = true; input.maxLength = 240; }
        if (key === "tnved") { input.pattern = "[0-9]{10}"; input.maxLength = 10; input.inputMode = "numeric"; }
      }
      input.name = key; l.append(input); container.append(l);
    }
    $("catalog-attributes").replaceChildren(); for (const [k, v] of Object.entries(data.attributes || {})) addAttribute(k, v);
  }
  async function editProduct(id, seed = {}, source = null) {
    current = id ? await request("/products/" + encodeURIComponent(id)) : null;
    editorID = id; editorRevision = current?.revision ?? null; adoption = source;
    buildProductFields(current || seed); $("catalog-product-heading").textContent = id ? current.title : source ? "Новый товар из карточки" : "Новый товар";
    $("catalog-product-related").hidden = !id; if (current) renderRelated(); open("catalog-product-dialog"); $("catalog-product-form").elements.namedItem("title").focus();
  }
  function productValue() {
    const value = Object.create(null), form = $("catalog-product-form");
    for (const key of Object.keys(labels)) {
      const raw = form.elements.namedItem(key).value.trim();
      if (["gtins", "barcodes"].includes(key)) value[key] = split(raw);
      else if (key === "archived") value[key] = raw === "true";
      else if (key === "marking_attestation") value[key] = raw === "" ? null : raw === "true";
      else if (numbers.has(key)) { const normal = raw.replace(",", "."); value[key] = raw === "" ? null : ints.has(key) ? Number(normal) : normal; }
      else value[key] = raw === "" ? null : raw;
    }
    value.attributes = Object.create(null);
    for (const row of $("catalog-attributes").children) {
      const [name, type, input] = row.children, key = name.value.trim();
      if (!key || Object.hasOwn(value.attributes, key)) throw new Error("Имена характеристик должны быть непустыми и уникальными");
      value.attributes[key] = typedValue(type.value, input.value);
    }
    return value;
  }
  on("catalog-new", () => editProduct(null)); on("catalog-attribute-new", () => addAttribute());
  on("catalog-product-form", async () => {
    const value = productValue();
    const saved = adoption ? await request("/sources/adopt", "POST", { product: value, source_product_id: adoption.id, variant: adoption.variant }) : editorID ? await request("/products/" + editorID, "PUT", { ...value, revision: editorRevision }) : await request("/products", "POST", value);
    await editProduct(saved.id); await Promise.all([loadProducts(), stats()]); notify("Товар сохранён");
  }, "submit");
  async function refreshRelated() { if (editorID) { current = await request("/products/" + editorID); renderRelated(); } }
  function renderCheck(result, stale = false) {
    const parent = $("catalog-check-result"); parent.replaceChildren();
    if (!result) { parent.append(node("p", "Сверка ещё не выполнена", "muted")); return; }
    parent.append(node("p", (stale ? "Результат устарел. Повторите сверку. " : "") + ({ matched: "Соответствие подтверждено по проверенным данным", needs_review: "Нужна проверка расхождений", unknown: "Правило не найдено", incomplete: "Недостаточно данных", ambiguous: "Правила противоречат друг другу" }[result.state] || result.state)));
    for (const issue of result.issues || []) parent.append(node("p", (issue.gtin ? issue.gtin + ": " : "") + issue.message));
    jsonDetails(parent, "Подробности проверки", result);
  }
  function renderRelated() {
    const links = $("catalog-product-links"); links.replaceChildren();
    for (const l of current.links) { const a = card(l.source_title || l.title || l.external_id || l.source_product_id, (l.adapter_key || "") + " · вариант " + (l.variant || "—")); a.append(button("Удалить связь", async () => { await request("/products/" + editorID + "/links/" + l.id, "DELETE"); await refreshRelated(); await stats(); })); links.append(a); }
    if (!current.links.length) links.append(node("p", "Связей пока нет", "muted"));
    const docs = $("catalog-documents"); docs.replaceChildren();
    for (const d of current.documents) {
      const a = card(d.number, ({ declaration: "Декларация", certificate: "Сертификат", sgr: "СГР", other: "Документ" }[d.kind]) + " · " + ({ declared: "Заявлен", verified: "Проверен вручную", revoked: "Отозван" }[d.status]) + (d.expires_on ? " · до " + d.expires_on : ""));
      a.append(button("Изменить", () => editDocument(d)));
      a.append(button("Убрать применимость", async () => { const data = documentValue(d); data.product_ids = d.product_ids.filter((id) => id !== editorID); await request("/documents/" + d.id, "PUT", { ...data, revision: d.revision }); await refreshRelated(); }));
      for (const file of d.files || []) { const aFile = node("a", file.filename, "chz-link"); aFile.href = base + "/files/" + encodeURIComponent(file.id); a.append(node("p")); a.append(aFile); } docs.append(a);
    }
    if (!current.documents.length) docs.append(node("p", "Документы не заданы", "muted"));
    const batches = $("catalog-batches"); batches.replaceChildren();
    for (const b of current.batches) { const a = card(b.name, "Единиц: " + b.quantity + " · кодов назначено: " + b.assigned + " · нанесено локально: " + (b.applied || 0)); a.append(button("Изменить", () => editBatch(b)), button("Экземпляры и нанесение", () => openCodes(b))); batches.append(a); }
    if (!current.batches.length) batches.append(node("p", "Партий пока нет", "muted"));
    renderCheck(current.check?.value, current.check?.stale);
    const history = $("catalog-history"); history.replaceChildren(); for (const e of current.events) jsonDetails(history, e.created_at + " · " + e.kind, e.data);
  }
  async function loadSources() {
    const form = $("catalog-source-search"), q = new URLSearchParams({ offset: sourceOffset, limit: 30, search: form.search.value, unlinked: form.unlinked.checked });
    if (form.connection_id.value) q.set("connection_id", form.connection_id.value);
    sourcePage = await request("/sources?" + q); const parent = $("catalog-sources"); parent.replaceChildren();
    for (const s of sourcePage.items) {
      const a = card(s.title, s.connection_name + " · " + s.external_id);
      if (s.adapter_key === "ozon") a.append(button("Прочитать полные характеристики Ozon", async () => { await request("/sources/" + s.id + "/refresh", "POST", {}); await loadSources(); }));
      for (const v of s.variants) {
        const part = node("div", undefined, "catalog-card"), link = s.links.find((l) => l.variant === v.key);
        part.append(node("p", "Вариант " + v.label + " · GTIN: " + (v.fields.gtins?.join(", ") || "не задан")));
        if (link) part.append(button("Открыть связанный товар", () => editProduct(link.product_id)));
        else {
          part.append(button("Создать товар из варианта", () => editProduct(null, v.fields, { id: s.id, variant: v.key })));
          part.append(button("Связать с существующим", async () => { sourceTarget = { source_product_id: s.id, variant: v.key }; $("catalog-link-search").value = v.fields.gtins?.[0] || ""; await findLinkProducts(); open("catalog-source-link-dialog"); }));
        }
        jsonDetails(part, "Поля источника", v.fields); a.append(part);
      }
      if (s.suggestions.length) a.append(node("p", "Совпадение GTIN: " + s.suggestions.map((v) => v.sku + " — " + v.title).join("; "), "muted"));
      parent.append(a);
    }
    if (!sourcePage.items.length) parent.append(node("p", "Карточки не найдены. Проверьте подключение и синхронизацию.", "muted"));
    $("catalog-source-page").textContent = sourcePage.total ? (sourceOffset + 1) + "–" + (sourceOffset + sourcePage.items.length) + " из " + sourcePage.total : "0 карточек";
    $("catalog-source-prev").disabled = sourceOffset === 0; $("catalog-source-next").disabled = sourceOffset + 30 >= sourcePage.total;
  }
  on("catalog-source-search", async () => { sourceOffset = 0; await loadSources(); }, "submit");
  on("catalog-source-prev", async () => { sourceOffset = Math.max(0, sourceOffset - 30); await loadSources(); });
  on("catalog-source-next", async () => { sourceOffset += 30; await loadSources(); });
  async function findLinkProducts() { const items = (await request("/products?limit=100&search=" + encodeURIComponent($("catalog-link-search").value))).items; selectOptions($("catalog-link-product"), items.map((p) => [p.id, p.sku + " — " + p.title]), "Выберите товар"); }
  on("catalog-link-find", findLinkProducts);
  on("catalog-source-link-form", async () => { await request("/products/" + $("catalog-link-product").value + "/links", "POST", sourceTarget); $("catalog-source-link-dialog").close(); await Promise.all([loadSources(), stats()]); }, "submit");
  const documentKeys = ["kind", "number", "issued_on", "expires_on", "issuer", "scope", "status", "verification_note", "product_ids"];
  function documentValue(d) { return Object.fromEntries(documentKeys.map((k) => [k, d[k]])); }
  function editDocument(d = null) { docEdit = d; const form = $("catalog-document-form"); form.reset(); for (const key of documentKeys.filter((k) => k !== "product_ids")) if (d) form.elements.namedItem(key).value = d[key] ?? ""; open("catalog-document-dialog"); }
  on("catalog-document-new", () => editDocument());
  on("catalog-document-form", async () => {
    const form = $("catalog-document-form"), data = Object.fromEntries(documentKeys.filter((k) => k !== "product_ids").map((k) => [k, form.elements.namedItem(k).value || null]));
    data.product_ids = docEdit ? docEdit.product_ids : [editorID];
    const saved = docEdit ? await request("/documents/" + docEdit.id, "PUT", { ...data, revision: docEdit.revision }) : await request("/documents", "POST", data);
    docEdit = saved;
    if (form.file.files[0]) await request("/documents/" + saved.id + "/files", "POST", await readFile(form.file.files[0], 10 * 1024 * 1024));
    $("catalog-document-dialog").close(); await refreshRelated(); notify("Документ сохранён");
  }, "submit");
  on("catalog-document-existing", async () => { allDocuments = await request("/documents"); selectOptions($("catalog-existing-document"), allDocuments.filter((d) => !d.product_ids.includes(editorID)).map((d) => [d.id, d.number]), "Выберите документ"); open("catalog-existing-document-dialog"); });
  on("catalog-existing-document-form", async () => { const d = allDocuments.find((d) => d.id === $("catalog-existing-document").value); if (!d) throw new Error("Выберите документ"); await request("/documents/" + d.id, "PUT", { ...documentValue(d), product_ids: [...d.product_ids, editorID], revision: d.revision }); $("catalog-existing-document-dialog").close(); await refreshRelated(); }, "submit");
  function editBatch(b = null) { batchEdit = b; const form = $("catalog-batch-form"); form.reset(); for (const key of ["name", "quantity", "manufactured_on", "expires_on", "note"]) if (b) form.elements.namedItem(key).value = b[key] ?? ""; open("catalog-batch-dialog"); }
  on("catalog-batch-new", () => editBatch());
  on("catalog-batch-form", async () => { const f = $("catalog-batch-form"), data = { product_id: editorID, name: f.name.value, quantity: Number(f.quantity.value), manufactured_on: f.manufactured_on.value || null, expires_on: f.expires_on.value || null, note: f.note.value || null }; if (batchEdit) await request("/batches/" + batchEdit.id, "PUT", { ...data, revision: batchEdit.revision }); else await request("/batches", "POST", data); $("catalog-batch-dialog").close(); await refreshRelated(); }, "submit");

  async function openCodes(batch) { batchTarget = batch; codeOffset = 0; selectedCodes.clear(); $("catalog-code-mode").value = "available"; $("catalog-codes-heading").textContent = "Партия: " + batch.name; await loadCodes(); open("catalog-codes-dialog"); }
  async function loadCodes() {
    const assigned = $("catalog-code-mode").value === "assigned", q = new URLSearchParams({ offset: codeOffset, limit: 100 });
    if (!assigned && $("catalog-code-connection").value) q.set("connection_id", $("catalog-code-connection").value);
    codePage = await request("/batches/" + batchTarget.id + (assigned ? "/codes?" : "/available-codes?") + q);
    const parent = $("catalog-codes"); parent.replaceChildren();
    for (const c of codePage.items) {
      const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = selectedCodes.has(c.id); checkbox.setAttribute("aria-label", "Выбрать код " + c.code);
      checkbox.addEventListener("change", () => { if (checkbox.checked) selectedCodes.add(c.id); else selectedCodes.delete(c.id); codeSelection(); });
      const events = (c.local_events || []).map((e) => ({ printed: "Напечатана", applied: "Нанесена", quality_checked: "Проверена" }[e.kind] || e.kind) + " · " + (e.data?.actor || "")).join("\n");
      parent.append(tr([checkbox, c.code, c.external_status || "Не прочитан", events || "—"]));
    }
    if (!codePage.items.length) parent.append(tr(["", "Кодов пока нет", "", ""]));
    $("catalog-code-connection-label").hidden = assigned; $("catalog-code-event-fields").hidden = !assigned;
    $("catalog-code-submit").textContent = assigned ? "Записать событие для выбранных" : "Назначить выбранные";
    $("catalog-code-event-form").actor.required = assigned;
    $("catalog-code-page").textContent = codePage.total ? (codeOffset + 1) + "–" + (codeOffset + codePage.items.length) + " из " + codePage.total : "0 кодов";
    $("catalog-code-prev").disabled = codeOffset === 0; $("catalog-code-next").disabled = codeOffset + 100 >= codePage.total; codeSelection();
  }
  function codeSelection() { $("catalog-code-selection").textContent = "Выбрано: " + selectedCodes.size; $("catalog-code-submit").disabled = selectedCodes.size === 0; }
  on("catalog-codes-refresh", loadCodes);
  on("catalog-code-prev", async () => { codeOffset = Math.max(0, codeOffset - 100); await loadCodes(); });
  on("catalog-code-next", async () => { codeOffset += 100; await loadCodes(); });
  for (const id of ["catalog-code-mode", "catalog-code-connection"]) on(id, async () => { codeOffset = 0; selectedCodes.clear(); await loadCodes(); }, "change");
  on("catalog-code-event-form", async () => {
    const ids = Array.from(selectedCodes); if (!ids.length) throw new Error("Выберите коды");
    const f = $("catalog-code-event-form"), assigned = $("catalog-code-mode").value === "assigned";
    await request("/batches/" + batchTarget.id + (assigned ? "/events" : "/codes"), "POST", assigned ? { code_ids: ids, kind: f.kind.value, actor: f.actor.value.trim(), note: f.note.value } : { code_ids: ids });
    selectedCodes.clear(); await loadCodes(); await refreshRelated(); notify(assigned ? "Событие оператора записано" : "Коды назначены партии");
  }, "submit");
  on("catalog-check", async () => { const connection = $("catalog-chz-connection").value; if (!connection) throw new Error("Выберите подключение ЧЗ"); const result = await request("/products/" + editorID + "/check", "POST", { connection_id: connection }); renderCheck(result); await refreshRelated(); });
  on("catalog-classify", async () => renderCheck(await request("/products/" + editorID + "/classification")));
  function addCondition(condition = {}) {
    const row = node("div", undefined, "catalog-condition"), field = node("input"), operator = node("select"), value = node("input");
    field.placeholder = "Поле, например volume_ml"; field.value = condition.field || ""; field.setAttribute("aria-label", "Поле условия");
    selectOptions(operator, [["eq", "="], ["ne", "≠"], ["gt", ">"], ["ge", "≥"], ["lt", "<"], ["le", "≤"]]); operator.value = condition.operator || "eq"; operator.setAttribute("aria-label", "Оператор");
    value.placeholder = 'Значение JSON: 100 или "текст"'; value.value = condition.value === undefined ? "" : JSON.stringify(condition.value); value.setAttribute("aria-label", "Значение условия");
    row.append(field, operator, value, button("Удалить", () => row.remove())); $("catalog-rule-conditions").append(row);
  }
  function editRule(rule = null) {
    ruleEdit = rule; const f = $("catalog-rule-form"); f.reset();
    if (rule) for (const [k, v] of Object.entries(rule)) { const input = f.elements.namedItem(k); if (!input) continue; if (k === "enabled") input.checked = v; else input.value = Array.isArray(v) ? v.join("\n") : v ?? ""; }
    $("catalog-rule-conditions").replaceChildren(); for (const c of rule?.conditions || []) addCondition(c); open("catalog-rule-dialog");
  }
  on("catalog-rule-new", () => editRule()); on("catalog-rule-condition-new", () => addCondition());
  on("catalog-rule-form", async () => {
    const f = $("catalog-rule-form"), data = {
      title: f.title.value, product_group: f.product_group.value, marking_required: f.marking_required.value === "true",
      tnved_prefixes: split(f.tnved_prefixes.value), okpd2_prefixes: split(f.okpd2_prefixes.value),
      valid_from: f.valid_from.value, valid_until: f.valid_until.value || null, source_url: f.source_url.value,
      source_note: f.source_note.value, priority: Number(f.priority.value), enabled: f.enabled.checked,
      conditions: Array.from($("catalog-rule-conditions").children).map((row) => ({ field: row.children[0].value.trim(), operator: row.children[1].value, value: typedValue("json", row.children[2].value) }))
    };
    if (ruleEdit) await request("/rules/" + ruleEdit.id, "PUT", { ...data, revision: ruleEdit.revision }); else await request("/rules", "POST", data);
    $("catalog-rule-dialog").close(); await loadRules(); notify("Правило сохранено");
  }, "submit");
  function renderSchema(parent, value, connectionID, category) {
    const a = card(value.title || value.category || category, "Категория: " + category);
    for (const attr of value.attributes) {
      const row = node("p", String(attr.id ?? attr.charcID) + " · " + attr.name + (attr.required || attr.is_required ? " · обязательно" : "") + (attr.type || attr.charcType ? " · " + (attr.type || attr.charcType) : ""));
      if (attr.dictionary_id) row.append(button("Значения словаря", async () => { dictionaryTarget = { connection_id: connectionID, category, attribute_id: Number(attr.id), cursor: 0 }; $("catalog-dictionary-values").replaceChildren(); await loadDictionary(); open("catalog-dictionary-dialog"); })); a.append(row);
    }
    if (value.tnved) jsonDetails(a, "ТН ВЭД и требования категории", value.tnved);
    jsonDetails(a, "Полная схема", value); parent.append(a);
  }
  async function loadRules() {
    const [rules, schemas] = await Promise.all([request("/rules"), request("/schemas")]); ruleItems = rules;
    const parent = $("catalog-rules"); parent.replaceChildren();
    for (const r of rules) { const a = card(r.title, r.product_group + " · с " + r.valid_from + (r.valid_until ? " до " + r.valid_until : "") + (r.enabled ? "" : " · выключено")); a.append(button("Изменить", () => editRule(r))); jsonDetails(a, "Источник и условия", r); parent.append(a); }
    if (!rules.length) parent.append(node("p", "Правила пока не заданы. Создайте проверенное правило с источником и сроком действия.", "muted"));
    const schemasParent = $("catalog-schemas"); schemasParent.replaceChildren(); for (const s of schemas) renderSchema(schemasParent, s.value, s.connection_id, s.category_key);
  }
  on("catalog-schema-form", async () => { const f = $("catalog-schema-form"); await request("/schemas/refresh", "POST", { connection_id: f.connection_id.value, category: f.category.value.trim() }); await loadRules(); }, "submit");
  async function loadDictionary() {
    const result = await request("/schemas/dictionary", "POST", dictionaryTarget), parent = $("catalog-dictionary-values");
    for (const value of result.items || result.values || result.result || []) parent.append(node("p", String(value.id ?? value.value_id ?? "") + " · " + (value.value ?? value.name ?? "")));
    dictionaryTarget.cursor = result.cursor ?? result.next_cursor ?? (result.result?.at(-1)?.id || 0); dictionaryTarget.has_next = Boolean(result.has_next);
    $("catalog-dictionary-next").disabled = !dictionaryTarget.has_next;
  }
  on("catalog-dictionary-next", loadDictionary);
  on("catalog-upload", async () => {
    upload = await readFile($("catalog-upload").file.files[0]); inspection = await request("/xlsx/inspect", "POST", upload); importPlan = null; $("catalog-import-preview").hidden = true;
    $("catalog-profile").value = inspection.native ? "fbe" : "custom";
    selectOptions($("catalog-sheet"), inspection.sheets.map((s) => [s.name, s.name]));
    if (inspection.native && inspection.sheets.some((s) => s.name === "Товары")) $("catalog-sheet").value = "Товары";
    $("catalog-workbook").hidden = false; buildMapping();
  }, "submit");
  function buildMapping() {
    const sheet = inspection.sheets.find((s) => s.name === $("catalog-sheet").value), native = $("catalog-profile").value === "fbe";
    $("catalog-header-row").value = sheet.header_row; $("catalog-data-row").value = native ? 3 : sheet.header_row + 1;
    $("catalog-header-row").disabled = native; $("catalog-data-row").disabled = native;
    $("catalog-mapping-note").textContent = native ? "FBE: импортируются все рабочие листы по стабильным ключам. Справочные статусы кодов доступны только для чтения." : "Неизвестные колонки можно сохранить в дополнительных характеристиках. Числа габаритов переводятся в мм, масса — в г.";
    const parent = $("catalog-mapping"); parent.replaceChildren();
    if (native) { $("catalog-fill").disabled = true; return; }
    for (const m of sheet.mapping) {
      const field = node("select"), attribute = node("input"), scale = node("select");
      selectOptions(field, [["", "Пропустить"], ...inspection.fields.map((v) => [v.key, v.label]), ["attributes.", "Дополнительная характеристика"]]);
      field.value = m.field.startsWith("attributes.") ? "attributes." : m.field; attribute.value = m.field.startsWith("attributes.") ? m.field.slice(11) : ""; attribute.disabled = field.value !== "attributes.";
      attribute.setAttribute("aria-label", "Имя характеристики " + m.column); field.setAttribute("aria-label", "Поле колонки " + m.column); scale.setAttribute("aria-label", "Множитель колонки " + m.column);
      selectOptions(scale, [["1", "×1"], ["10", "×10 (см → мм)"], ["100", "×100"], ["1000", "×1000 (кг → г)"], ["0.1", "×0,1"], ["0.01", "×0,01"], ["0.001", "×0,001"]]); scale.value = m.scale;
      field.addEventListener("change", () => { attribute.disabled = field.value !== "attributes."; });
      const row = tr([m.column + ": " + (m.header || "Без заголовка"), field, attribute, scale]); row.dataset.column = m.column; parent.append(row);
    }
    $("catalog-fill").disabled = selected.size === 0;
  }
  function workbookBody() {
    if (!upload) throw new Error("Загрузите файл");
    const native = $("catalog-profile").value === "fbe";
    const mapping = Array.from($("catalog-mapping").children).map((row) => {
      const field = row.children[1].firstElementChild.value, attribute = row.children[2].firstElementChild.value.trim();
      if (field === "attributes." && !attribute) throw new Error("Укажите имя дополнительной характеристики");
      return { column: Number(row.dataset.column), field: field === "attributes." ? field + attribute : field, scale: row.children[3].firstElementChild.value };
    });
    return { ...upload, profile: $("catalog-profile").value, sheet_name: $("catalog-sheet").value,
      header_row: native ? 2 : Number($("catalog-header-row").value), data_row: native ? 3 : Number($("catalog-data-row").value),
      mapping: native ? null : mapping };
  }
  on("catalog-sheet", () => buildMapping(), "change"); on("catalog-profile", () => buildMapping(), "change");
  function renderImport() {
    const plan = importPlan; $("catalog-import-preview").hidden = false;
    $("catalog-import-summary").textContent = "Состояние: " + plan.state + " · изменений: " + plan.operations.length + " · ошибок: " + plan.errors.length + " · предупреждений: " + plan.warnings.length;
    const parent = $("catalog-import-messages"); parent.replaceChildren();
    for (const v of [...plan.errors, ...plan.warnings].slice(0, 100)) parent.append(node("p", (v.sheet || "") + (v.row ? " · строка " + v.row : "") + (v.column ? " · колонка " + v.column : "") + ": " + v.message));
    if (plan.errors.length + plan.warnings.length > 100) parent.append(node("p", "Показаны первые 100 сообщений. Полный отчёт доступен в JSON."));
    const body = $("catalog-import-operations"); body.replaceChildren();
    for (const op of plan.operations.slice(0, 100)) { const changes = node("pre", JSON.stringify(op.changes && Object.keys(op.changes).length ? op.changes : op.data, null, 2)); body.append(tr([op.sheet + " · " + op.row, (op.new ? "Создание" : "Обновление") + " · " + op.kind, changes])); }
    $("catalog-apply").disabled = plan.state !== "prepared" || plan.errors.length > 0; $("catalog-cancel-import").disabled = plan.state !== "prepared";
  }
  on("catalog-preview", async () => { importPlan = await request("/xlsx/preview", "POST", workbookBody()); renderImport(); });
  on("catalog-apply", async () => { if (!importPlan || importPlan.errors.length) throw new Error("Сначала исправьте ошибки предпросмотра"); await request("/imports/" + importPlan.id + "/apply", "POST", { digest: importPlan.digest }); importPlan = await request("/imports/" + importPlan.id); renderImport(); await Promise.all([loadProducts(), stats()]); notify("Импорт применён полностью"); });
  on("catalog-cancel-import", async () => { await request("/imports/" + importPlan.id + "/cancel", "POST", {}); importPlan = await request("/imports/" + importPlan.id); renderImport(); });
  on("catalog-download-plan", () => saveBlob(new Blob([JSON.stringify(importPlan, null, 2)], { type: "application/json" }), "FBE-import-report.json"));
  on("catalog-fill", () => { if (!selected.size) throw new Error("Выберите товары на вкладке «Товары»"); return download("/xlsx/fill", "FBE-" + $("catalog-profile").value + "-filled.xlsx", { ...workbookBody(), product_ids: Array.from(selected) }); });
  async function initialize() {
    connections = await api(sellerApi + "/connections");
    selectOptions($("catalog-source-connection"), connections.map((c) => [c.id, c.name]), "Все подключения");
    selectOptions($("catalog-schema-connection"), connections.filter((c) => ["wb", "ozon"].includes(c.adapter_key)).map((c) => [c.id, c.name]), "Выберите подключение");
    const chz = connections.filter((c) => c.adapter_key === "chz").map((c) => [c.id, c.name]);
    selectOptions($("catalog-chz-connection"), chz, "Выберите ЧЗ"); selectOptions($("catalog-code-connection"), chz, "Все подключения");
    await Promise.all([loadProducts(), stats()]);
  }
  const tabs = Array.from(document.querySelectorAll("[data-catalog-tab]"));
  for (const [index, tab] of tabs.entries()) tab.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
    tabs[next].focus(); tabs[next].click();
  });
  initialize().then(syncActionButtons).catch(fail);
})();
