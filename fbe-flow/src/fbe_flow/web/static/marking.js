"use strict";

(() => {
  const root = `${sellerApi}/marking`;
  const select = document.getElementById("chz-connection");
  if (select && [...select.options].some(v => v.value === pageQuery.get("connection"))) select.value = pageQuery.get("connection");
  let connection = select?.value, offset = 0, search = "", revision = 0;
  let refreshPromise = null, rerun = false;
  let parameters = {}, currentPage = [], codePage = [], codesOffset = 0, activeDocument = null;
  let attributeModel = [], groupRevision = "";
  const chosen = new Map(), chosenCodes = new Map();
  const states = { queued: "В очереди", running: "Выполняется", succeeded: "Завершена", failed: "Ошибка", interrupted: "Прервана" };
  const statuses = { draft: "Черновик", moderation: "На модерации", errors: "Требует изменений", notsigned: "Ожидает подписи", published: "Опубликована", archived: "В архиве" };
  const documentStates = { prepared: "Подготовлен", submitting: "Отправляется", accepted: "Принят ЧЗ", processing: "Обрабатывается", succeeded: "Завершён", partial: "Частичный результат", rejected: "Отклонён", unknown: "Результат неизвестен — требуется сверка", cancelled: "Отменён" };
  const endpoint = () => `${root}/${encodeURIComponent(connection)}`;
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  };
  const report = text => { feedback.textContent = text; feedback.hidden = false; };
  const act = (id, fn) => {
    document.getElementById(id)?.addEventListener("click", async event => {
      const button = event.currentTarget; button.disabled = true;
      try { feedback.hidden = true; await fn(); }
      catch (error) { showError(error); }
      finally { button.disabled = false; }
    });
  };
  const queue = async (key, payload = {}) => {
    await api(`${sellerApi}/operations`, "POST", { connection_id: connection, operation_key: key, payload });
    report("Задание добавлено в очередь.");
  };
  bindForm("chz-connect", async fields => {
    await api(`${root}/connections`, "POST", Object.fromEntries(fields));
    document.querySelector('#chz-connect [name="true_token"]').value = "";
    window.location.reload();
  });
  act("chz-certificates", async () => {
    const values = await api(`${root}/certificates`);
    const target = document.getElementById("chz-certificate");
    target.replaceChildren(make("option", "Выбрать сертификат")); target.firstChild.value = "";
    for (const value of values) {
      const option = make("option", `${value.subject} · ${value.expires}`);
      option.value = value.thumbprint; target.append(option);
    }
    if (!values.length) report("В текущем пользователе Windows нет действующих сертификатов с закрытым ключом.");
  });
  if (!select) return;
  let catalogProducts = [];
  bindForm("chz-catalog-pick", async fields => {
    const result = await api(sellerApi + "/catalog/products?limit=50&search=" + encodeURIComponent(fields.get("search")));
    catalogProducts = result.items;
    const picker = document.getElementById("chz-catalog-product"), prompt = make("option", "Выбрать товар"); prompt.value = "";
    picker.replaceChildren(prompt, ...catalogProducts.map(p => { const option = make("option", p.sku + " · " + p.title); option.value = p.id; return option; }));
    if (!result.items.length) report("Товар не найден в ассортименте");
  });
  document.getElementById("chz-catalog-product").addEventListener("change", event => {
    const p = catalogProducts.find(v => v.id === event.target.value); if (!p) return;
    const gtin = p.gtins[0] || "";
    document.getElementById("chz-catalog-selection").textContent = p.sku + " · GTIN " + (gtin || "не задан") + " · " + (p.product_group || "группа не задана");
    for (const id of ["chz-lookup", "chz-order-buffer"]) { const input = document.getElementById(id)?.elements.namedItem("gtin"); if (input) input.value = gtin; }
    for (const group of document.querySelectorAll(".chz-group")) if ([...group.options].some(v => v.value === p.product_group)) group.value = p.product_group;
    const order = document.getElementById("chz-order");
    if (order?.elements.namedItem("catalog_gtin")) { order.elements.namedItem("catalog_gtin").value = gtin; if (!order.elements.request_key.value) order.elements.request_key.value = crypto.randomUUID(); }
  });
  const cardDialog = document.getElementById("chz-card");
  const openCard = (title, value, hint) => {
    document.getElementById("chz-card-title").textContent = title;
    document.getElementById("chz-card-status").textContent = hint;
    const fields = document.getElementById("chz-card-fields"); fields.replaceChildren();
    for (const [key, label] of [["good_name", "Наименование"], ["gtin", "GTIN"], ["good_status", "Статус НК"], ["tnved", "ТН ВЭД"], ["brand", "Бренд"]]) if (value[key]) fields.append(make("p", label + ": " + (Array.isArray(value[key]) ? value[key].join(", ") : value[key])));
    document.getElementById("chz-card-json").textContent = JSON.stringify(value, null, 2);
    if (!cardDialog.open) cardDialog.showModal();
  };
  act("chz-card-close", () => cardDialog.close());
  select.addEventListener("change", () => {
    connection = select.value; revision++; offset = 0; cardDialog.close();
    chosen.clear(); chosenCodes.clear(); codesOffset = 0; groupRevision = "";
    activeDocument = null; document.getElementById("chz-document").close(); selectionCount();
    for (const input of document.querySelectorAll("#chz-suz-config input")) { delete input.dataset.edited; input.value = ""; }
    attributeModel = []; document.getElementById("chz-attribute").replaceChildren(make("option", "Сначала загрузите атрибуты выбранных карточек"));
    document.getElementById("chz-products").replaceChildren();
    refreshData();
  });
  act("chz-sync", async () => { await api(`${endpoint()}/sync`, "POST", { force: false }); await refreshData(); });
  act("chz-force-sync", async () => { await api(`${endpoint()}/sync`, "POST", { force: true }); await refreshData(); });
  act("chz-references", () => queue("nk.references"));
  bindForm("chz-renew", async fields => {
    await api(`${endpoint()}/credentials`, "PUT", Object.fromEntries(fields));
    document.querySelector('#chz-renew [name="true_token"]').value = "";
    report("Доступ проверен и обновлен");
  });
  bindForm("chz-search", async fields => { search = fields.get("search"); offset = 0; revision++; await refreshData(); });
  bindForm("chz-lookup", fields => queue("nk.lookup", { gtin: fields.get("gtin") }));
  act("chz-prev", async () => { offset = Math.max(0, offset - 100); revision++; await refreshData(); });
  act("chz-next", async () => { offset += 100; revision++; await refreshData(); });
  const flag = value => value === true ? "да" : value === false ? "нет" : "нет данных";

  function renderProducts(page) {
    currentPage = page.items;
    const elements = page.items.map(item => {
      const row = make("tr"), titleCell = make("td");
      const pickCell = make("td"), pick = make("input"); pick.type = "checkbox"; pick.setAttribute("aria-label", `Выбрать ${item.title}`);
      pick.disabled = !item.id || !item.present || !item.detail_available;
      if (chosen.has(item.id) && !pick.disabled) chosen.set(item.id, item);
      if (pick.disabled) chosen.delete(item.id);
      pick.checked = chosen.has(item.id);
      pick.addEventListener("change", () => { if (pick.checked) chosen.set(item.id, item); else chosen.delete(item.id); selectionCount(); });
      pickCell.append(pick);
      const title = make("button", item.title, "text-button"), source = item.attributes.source || {};
      let hint = item.present ? "Карточка присутствует в последней выдаче" : "Карточка отсутствует в последней полной выдаче НК";
      if (!item.detail_available) hint += ". Детали недоступны; сохраненная копия может быть устаревшей";
      title.addEventListener("click", () => openCard(item.title, source, `${hint}. Обновление: ${item.updated_at ? new Date(item.updated_at).toLocaleString("ru-RU") : "нет"}`));
      titleCell.append(title, make("small", `НК #${item.external_id}`));
      if (!item.present || !item.detail_available) titleCell.append(make("small", hint));
      row.append(pickCell, titleCell, make("td", (item.identifiers.gtin || []).join(", ") || "—"),
        make("td", (source.categories || []).map(c => c.cat_name || c.cat_id).join(", ") || "—"),
        make("td", (source.good_detailed_status || [source.good_status || "—"]).map(s => statuses[s] || s).join(", ")),
        make("td", `Эмиссия: ${flag(source.good_mark_flag)} · Оборот: ${flag(source.good_turn_flag)}`));
      return row;
    });
    if (!elements.length) { const row = make("tr"), cell = make("td", "Карточки не найдены. Запустите синхронизацию или измените поиск."); cell.colSpan = 6; row.append(cell); elements.push(row); }
    document.getElementById("chz-products").replaceChildren(...elements);
    document.getElementById("chz-count").textContent = `${page.total} карточек`;
    document.getElementById("chz-prev").disabled = offset === 0;
    document.getElementById("chz-next").disabled = offset + page.items.length >= page.total;
    selectionCount();
  }
  function renderOverview(overview) {
    parameters = overview.parameters;
    const groups = overview.snapshots.account?.value.productGroups || [];
    const key = JSON.stringify(groups);
    if (key !== groupRevision) {
      for (const element of document.querySelectorAll(".chz-group")) {
        const old = element.value;
        const prompt = make("option", "Выбрать группу аккаунта"); prompt.value = "";
        element.replaceChildren(prompt, ...groups.map(group => { const option = make("option", group); option.value = group; return option; }));
        if (groups.includes(old)) element.value = old;
      }
      groupRevision = key;
    }
    const suzConfig = document.getElementById("chz-suz-config");
    for (const name of ["oms_id", "oms_connection"]) {
      const input = suzConfig?.elements.namedItem(name);
      if (input && document.activeElement !== input && !input.dataset.edited) input.value = parameters[name] || "";
    }
    if (document.getElementById("chz-suz-state")) document.getElementById("chz-suz-state").textContent = parameters.oms_id && parameters.oms_connection && parameters.has_certificate
      ? `СУЗ настроен: ${parameters.oms_id}` : "Для СУЗ выберите УКЭП и укажите OMS ID и omsConnection.";
    const data = overview.snapshots, sync = data.sync?.value;
    const active = sync && ["queued", "running"].includes(sync.state);
    document.getElementById("chz-sync").disabled = !!active;
    document.getElementById("chz-force-sync").disabled = !!active;
    document.getElementById("chz-sync-status").textContent = sync
      ? `Синхронизация: ${states[sync.state] || sync.state} · ${sync.offset || 0}/${sync.total ?? "…"}${sync.error ? ` · ${sync.error}` : ""}`
      : "Каталог еще не обновлялся";
    document.getElementById("chz-last-sync").textContent = data.last_sync
      ? `Последний полный обход: ${new Date(data.last_sync.updated_at).toLocaleString("ru-RU")}` : "Полный обход еще не завершен";
    if (document.getElementById("chz-references-json")) document.getElementById("chz-references-json").textContent = JSON.stringify({ account: data.account?.value, categories: data.categories?.value }, null, 2);
    document.getElementById("chz-order-status").textContent = JSON.stringify({ buffers: data.suz_status?.value, blocks: data.suz_blocks?.value }, null, 2);
    document.getElementById("chz-receipt-status").textContent = JSON.stringify(data.suz_receipt?.value || {}, null, 2);
    const lookup = document.getElementById("chz-lookup-result");
    lookup.replaceChildren();
    if (data.lookup) {
      lookup.append(make("small", `Последний поиск НК: ${new Date(data.lookup.updated_at).toLocaleString("ru-RU")}`));
      if (!data.lookup.value.length) lookup.append(make("p", "НК не вернул доступных карточек"));
      for (const card of data.lookup.value) {
        const button = make("button", card.good_name || `НК #${card.good_id}`, "text-button");
        button.addEventListener("click", () => openCard(card.good_name || "Карточка НК", card, "Результат поиска по GTIN. Принадлежность аккаунту не утверждается."));
        lookup.append(button);
      }
    }
  }
  async function refreshData() {
    if (refreshPromise) { rerun = true; return refreshPromise; }
    const version = revision, active = connection, base = endpoint();
    refreshPromise = (async () => {
      try {
        const [page, overview, info, codes, documents] = await Promise.all([
          api(`${base}/products?offset=${offset}&search=${encodeURIComponent(search)}`),
          api(`${base}/overview`), api(`${sellerApi}/connections/${encodeURIComponent(active)}`),
          api(`${base}/codes?offset=${codesOffset}`), api(`${base}/documents`),
        ]);
        if (version !== revision || active !== connection) return;
        renderProducts(page); renderOverview(overview);
        renderCodes(codes); renderDocuments(documents);
        document.getElementById("chz-environment").textContent = info.environment === "production" ? "Промышленная среда" : "Тестовая среда";
        if (activeDocument && document.getElementById("chz-document").open) {
          const updated = await api(`${root}/documents/${encodeURIComponent(activeDocument.id)}`);
          if (version === revision && active === connection) renderDocument(updated);
        }
      } catch (error) { if (version === revision) showError(error); }
      finally { refreshPromise = null; if (rerun) { rerun = false; queueMicrotask(refreshData); } }
    })();
    return refreshPromise;
  }
  function selectionCount() {
    document.getElementById("chz-selected").textContent = `Выбрано: ${chosen.size}`;
    document.getElementById("chz-codes-selected").textContent = `Выбрано: ${chosenCodes.size}`;
  }
  const selectedIds = () => { if (!chosen.size) throw new Error("Выберите карточки"); return [...chosen.keys()]; };
  const selectedCodeIds = () => { if (!chosenCodes.size) throw new Error("Выберите коды"); return [...chosenCodes.keys()]; };
  const parse = (raw, type) => { let value; try { value = JSON.parse(raw); } catch { throw new Error("Проверьте синтаксис JSON"); } if (type === "array" ? !Array.isArray(value) : !value || typeof value !== "object" || Array.isArray(value)) throw new Error(type === "array" ? "Нужен массив JSON" : "Нужен объект JSON"); return value; };
  async function prepareAction(action, payload) {
    const current = connection;
    const value = await api(`${endpoint()}/prepare`, "POST", { action, payload });
    if (current === connection) { renderDocument(value); document.getElementById("chz-document").showModal(); await refreshData(); }
  }
  act("chz-select-page", async () => { for (const item of currentPage) if (item.id && item.present && item.detail_available) chosen.set(item.id, item); await refreshData(); });
  act("chz-clear-selection", async () => { chosen.clear(); await refreshData(); });
  act("chz-prepare-sign", () => prepareAction("sign", { product_ids: selectedIds(), publication_agreement: document.getElementById("chz-publication-agreement").checked }));
  act("chz-edit-model", async () => {
    const current = connection, ids = selectedIds();
    const model = await api(`${endpoint()}/edit-model`, "POST", { product_ids: ids });
    if (current !== connection) return;
    attributeModel = model.attributes;
    const element = document.getElementById("chz-attribute");
    const prompt = make("option", "Выбрать атрибут"); prompt.value = "";
    element.replaceChildren(prompt, ...attributeModel.map(attr => { const option = make("option", `${attr.attr_name} · #${attr.attr_id}`); option.value = attr.attr_id; return option; }));
    report(`Загружены общие атрибуты ${ids.length} карточек.`);
  });
  document.getElementById("chz-attribute").addEventListener("change", event => {
    const attr = attributeModel.find(v => String(v.attr_id) === event.target.value);
    const type = document.getElementById("chz-attribute-type"), none = make("option", "Не указан"); none.value = "";
    type.replaceChildren(none, ...(attr?.attr_value_type || []).map(value => { const option = make("option", value); option.value = value; return option; }));
    document.getElementById("chz-presets").replaceChildren(...(attr?.attr_preset || []).map(value => { const option = make("option"); option.value = value; return option; }));
    document.getElementById("chz-attribute-hint").textContent = attr ? `${attr.attr_type === "m" ? "Обязательный" : "Доступный"} · ${attr.attr_multiplicity ? "несколько значений" : "одно значение"}${attr.attr_preset_only ? " · только справочник НК" : ""}${attr.preset_url ? " · подробный справочник: " + attr.preset_url : ""}` : "";
  });
  bindForm("chz-edit", fields => {
    const change = { attr_id: fields.get("attr_id"), attr_value: fields.get("attr_value") };
    if (fields.get("attr_value_type")) change.attr_value_type = fields.get("attr_value_type");
    return prepareAction("edit", { product_ids: selectedIds(), attributes: [change], moderation: fields.has("moderation") });
  });
  bindForm("chz-edit-json", fields => prepareAction("edit", { product_ids: selectedIds(), attributes: parse(fields.get("attributes"), "array"), moderation: fields.has("moderation") }));
  for (const input of document.querySelectorAll("#chz-suz-config input")) input.addEventListener("input", () => { input.dataset.edited = "1"; });
  bindForm("chz-suz-config", async fields => { await api(`${endpoint()}/suz`, "PUT", Object.fromEntries(fields)); report("Подключение СУЗ проверено и сохранено"); await refreshData(); });
  bindForm("chz-order", fields => {
    const raw = fields.get("products").trim();
    const products = raw ? parse(raw, "array") : [{gtin: fields.get("catalog_gtin"), quantity: Number(fields.get("catalog_quantity")), templateId: Number(fields.get("catalog_template")), serialNumberType: "OPERATOR", cisType: "UNIT"}];
    if (!raw && (!/^[0-9]{14}$/.test(products[0].gtin) || !Number.isInteger(products[0].templateId) || products[0].templateId < 1)) throw new Error("Выберите товар с GTIN и укажите ID шаблона своей группы СУЗ");
    return prepareAction("suz_order", {product_group: fields.get("product_group"), request_key: fields.get("request_key"), document: {productGroup: fields.get("product_group"), products, attributes: parse(fields.get("attributes") || "{}", "object")}});
  });
  bindForm("chz-order-buffer", async fields => {
    const payload = { product_group: fields.get("product_group"), order_id: fields.get("order_id"), gtin: fields.get("gtin"), quantity: Number(fields.get("quantity")) };
    const action = fields.get("action");
    if (action === "status" || action === "blocks") { await queue(action === "status" ? "suz.status" : "suz.blocks", payload); await refreshData(); }
    else await prepareAction(action === "codes" ? "suz_codes" : "suz_close", payload);
  });
  bindForm("chz-utilisation-json", fields => prepareAction("suz_utilisation", { product_group: fields.get("product_group"), document: parse(fields.get("document"), "object") }));
  bindForm("chz-suz-receipt", fields => queue("suz.receipt", { receipt_id: fields.get("receipt_id") }));
  bindForm("chz-check-codes", fields => queue("codes.check", { product_group: fields.get("product_group"), codes: fields.get("codes").split(/\r?\n/).filter(v => v.length) }));
  bindForm("chz-utilisation-selected", async fields => {
    const current = connection;
    const value = await api(`${endpoint()}/utilisation`, "POST", { code_ids: selectedCodeIds(), product_group: fields.get("product_group"), attributes: parse(fields.get("attributes"), "object") });
    if (current === connection) { renderDocument(value); document.getElementById("chz-document").showModal(); await refreshData(); }
  });
  act("chz-codes-prev", async () => { codesOffset = Math.max(0, codesOffset - 100); revision++; await refreshData(); });
  act("chz-codes-next", async () => { codesOffset += 100; revision++; await refreshData(); });
  act("chz-codes-all", async () => { for (const item of codePage) chosenCodes.set(item.id, item); await refreshData(); });
  act("chz-codes-clear", async () => { chosenCodes.clear(); await refreshData(); });
  const download = (value, filename) => { const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json;charset=utf-8" })); const link = make("a"); link.href = url; link.download = filename; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); };
  function renderCodes(page) {
    codePage = page.items;
    const rows = page.items.map(item => {
      const row = make("tr"), cell = make("td"), checkbox = make("input"); checkbox.type = "checkbox"; checkbox.checked = chosenCodes.has(item.id); checkbox.setAttribute("aria-label", `Выбрать КИ ${item.code}`);
      if (checkbox.checked) chosenCodes.set(item.id, item);
      checkbox.addEventListener("change", () => { if (checkbox.checked) chosenCodes.set(item.id, item); else chosenCodes.delete(item.id); selectionCount(); }); cell.append(checkbox);
      const code = make("button", item.code, "text-button chz-code"); code.addEventListener("click", () => openCard("КИ " + item.code, item.attributes, "Последняя проверка: " + new Date(item.updated_at).toLocaleString("ru-RU")));
      const codeCell = make("td"); codeCell.append(code);
      const blockCell = make("td", item.block_id || "—");
      if (item.has_full_code && item.block_id) {
        const button = make("button", "Скачать блок JSON", "text-button");
        button.addEventListener("click", async () => { try { download(await api(`${endpoint()}/blocks/${encodeURIComponent(item.block_id)}/export`), `chz-block-${item.block_id}.json`); } catch (error) { showError(error); } }); blockCell.append(button);
      }
      row.append(cell, codeCell, make("td", item.gtin || "—"), make("td", item.attributes.errorCode ? `Ошибка ${item.attributes.errorCode}` : item.external_status || "Не проверен", "chz-code"), blockCell); return row;
    });
    if (!rows.length) { const row = make("tr"), cell = make("td", "Коды появятся после получения блока СУЗ или проверки в True API."); cell.colSpan = 5; row.append(cell); rows.push(row); }
    document.getElementById("chz-codes").replaceChildren(...rows);
    document.getElementById("chz-codes-count").textContent = `${page.total} КИ`;
    document.getElementById("chz-codes-prev").disabled = codesOffset === 0;
    document.getElementById("chz-codes-next").disabled = codesOffset + page.items.length >= page.total;
    selectionCount();
  }
  function renderDocuments(values) {
    document.getElementById("chz-documents").replaceChildren(...values.map(value => {
      const row = make("div", undefined, "chz-document-row"), title = make("button", value.title, "text-button");
      title.addEventListener("click", async () => { try { const doc = await api(`${root}/documents/${encodeURIComponent(value.id)}`); if (doc.connection_id !== connection) return; renderDocument(doc); document.getElementById("chz-document").showModal(); } catch (error) { showError(error); } });
      row.append(title, make("p", `${documentStates[value.state] || value.state}${value.external_status ? " · " + value.external_status : ""}${value.external_id ? " · ID " + value.external_id : ""}`), make("small", new Date(value.updated_at).toLocaleString("ru-RU"))); return row;
    }));
    if (!values.length) document.getElementById("chz-documents").append(make("p", "Подготовленных документов ещё нет.", "muted"));
  }
  function renderDocument(value) {
    actionContext(document.getElementById("chz-document"), select.selectedOptions[0].textContent, parameters.inn);
    const previous = activeDocument?.id; activeDocument = value;
    document.getElementById("chz-document-title").textContent = value.title;
    document.getElementById("chz-document-state").textContent = `${documentStates[value.state] || value.state}${value.active_operation ? " · Задание в очереди или выполняется" : ""}${value.external_status ? " · " + value.external_status : ""}${value.error_code ? " · " + value.error_code : ""}`;
    const preview = document.getElementById("chz-document-preview"); preview.replaceChildren();
    if (parameters.environment === "production" && value.state === "prepared") preview.append(make("p", "Отправка выполнит операцию в промышленном Честном Знаке для выбранного аккаунта.", "chz-warning"));
    if (value.state === "unknown") preview.append(make("p", "ЧЗ мог принять запрос. Повторная отправка заблокирована. Сверьте документ или блок по внешнему ID; для НК проверяются текущие данные.", "chz-warning"));
    if (value.state === "partial") preview.append(make("p", "Обработана часть позиций. Смотрите ошибки по каждой позиции; подготовьте отдельный документ для отклонённых целей.", "chz-warning"));
    for (const card of value.body.preview || []) {
      preview.append(make("h3", card.title || `НК #${card.good_id}`));
      const table = make("table"), head = make("tr"); for (const text of ["Атрибут", "Было", "Будет"]) head.append(make("th", text)); table.append(head);
      for (const change of card.changes) { const row = make("tr"); row.append(make("td", change.name), make("td", (change.before || []).join(", ") || "—"), make("td", `${change.after.delete ? "Удалить " : ""}${change.after.attr_value}`)); table.append(row); }
      const scroll = make("div", undefined, "table-scroll"); scroll.append(table); preview.append(scroll);
    }
    document.getElementById("chz-document-json").textContent = JSON.stringify({ id: value.id, body: value.body, result: value.result, wire_saved: value.wire_saved }, null, 2);
    document.getElementById("chz-document-events").replaceChildren(...value.events.map(event => make("div", `${new Date(event.created_at).toLocaleString("ru-RU")} · ${documentStates[event.state] || event.state}${event.data.error_code ? " · " + event.data.error_code : ""}`, "chz-event")));
    const actions = document.getElementById("chz-document-actions"); actions.replaceChildren();
    const action = (label, path, secondary = false) => { const button = make("button", label, secondary ? "secondary" : ""); button.type = "button"; button.disabled = value.active_operation; button.addEventListener("click", async () => { button.disabled = true; try { await api(`${root}/documents/${encodeURIComponent(value.id)}/${path}`, "POST", {}); const updated = await api(`${root}/documents/${encodeURIComponent(value.id)}`); if (activeDocument?.id === value.id) renderDocument(updated); await refreshData(); } catch (error) { showError(error); button.disabled = false; } }); actions.append(button); };
    if (value.state === "prepared") { action(value.kind === "suz_codes" ? "Получить новый блок КМ" : value.kind === "nk_feed" ? "Отправить изменения" : "Подписать и отправить", "submit"); action("Отменить черновик", "cancel", true); }
    if (["accepted", "processing", "unknown"].includes(value.state) && (value.external_id || value.kind.startsWith("nk_"))) action("Проверить результат", "poll", true);
    if (value.retryable) action("Подготовить повтор после отказа", "retry", true);
    const reconcile = document.getElementById("chz-reconcile"); reconcile.hidden = value.state !== "unknown";
    if (previous !== value.id) reconcile.reset();
    const input = reconcile.elements.namedItem("external_id"); input.required = !value.kind.startsWith("nk_");
    input.placeholder = value.kind === "suz_codes" ? "ID нового блока из списка СУЗ" : value.kind.startsWith("nk_") ? "Для НК оставьте пустым" : value.kind.startsWith("suz_") ? "ID заказа или отчёта СУЗ" : "ID документа в ГИС МТ";
  }
  bindForm("chz-reconcile", async fields => { const id = activeDocument.id; const value = await api(`${root}/documents/${encodeURIComponent(id)}/reconcile`, "POST", { external_id: fields.get("external_id") }); if (activeDocument?.id === id) renderDocument(value); await refreshData(); });
  act("chz-document-close", () => { document.getElementById("chz-document").close(); activeDocument = null; });
  bindForm("chz-true-document", fields => prepareAction("true", { type: fields.get("type"), product_group: fields.get("product_group"), document: parse(fields.get("document"), "object") }));
  act("chz-true-template", () => {
    selectedCodeIds(); const form = document.getElementById("chz-true-document"), intro = form.elements.namedItem("type").value === "LP_INTRODUCE_GOODS", group = form.elements.namedItem("product_group").value;
    if (!group || [...chosenCodes.values()].some(v => v.product_group !== group)) throw new Error("Выберите группу этих кодов");
    const today = new Date().toLocaleDateString("sv-SE");
    const body = intro ? { participant_inn: parameters.inn, producer_inn: parameters.inn, owner_inn: parameters.inn, production_date: today, production_type: "OWN_PRODUCTION", products: [...chosenCodes.values()].map(v => ({ uit_code: v.code })) } : { inn: parameters.inn, action: "OTHER", action_date: today, withdrawal_type_other: "", document_type: "OTHER", document_number: "", document_date: today, products: [...chosenCodes.values()].map(v => ({ cis: v.code })) };
    document.getElementById("chz-true-json").value = JSON.stringify(body, null, 2);
    report("Основа создана. Заполните причину, документы и дополнительные сведения вашей группы.");
  });
  document.getElementById("chz-true-file").addEventListener("change", async event => { try { const file = event.target.files[0]; if (!file) return; if (file.size > 5 * 1024 * 1024) throw new Error("Лимит файла 5 МиБ"); document.getElementById("chz-true-json").value = JSON.stringify(parse(await file.text(), "object"), null, 2); } catch (error) { showError(error); } });
  async function poll() { if (!document.hidden) await refreshData(); window.setTimeout(poll, refreshSeconds * 1000); }
  poll();
})();
