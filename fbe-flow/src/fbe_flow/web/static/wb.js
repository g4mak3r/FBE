"use strict";

(() => {
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const root = `${sellerApi}/wb`, selector = document.getElementById("wb-connection");
  bindForm("wb-connect", async fields => {
    await api(`${root}/connections`, "POST", { name: fields.get("name"), token: fields.get("token") });
    document.getElementById("wb-connect").reset();
    window.location.reload();
  });
  if (!selector) return;
  const view = flowView("wb");
  if ([...selector.options].some(v => v.value === view.connection)) selector.value = view.connection;
  let connection = selector.value, revision = 0, kind = ["orders", "supplies", "products", "warehouses"].includes(view.kind) ? view.kind : "orders", offset = Math.max(0, Number(view.offset) || 0), search = view.search || "";
  let stage = view.stage || "", status = view.status || "", warehouse = view.warehouse || "", warehouseNames = new Map();
  const filterForm = document.getElementById("wb-search");
  for (const key of ["search", "stage", "status"]) filterForm.elements[key].value = ({search,stage,status})[key];
  async function loadWarehouseFilter() {
    const active = connection, version = revision, page = await api(base() + "/records/warehouses?limit=200");
    if (active !== connection || version !== revision) return;
    warehouseNames = new Map(page.items.map(v => [v.external_id, v.name]));
    const field = filterForm.elements.warehouse, prompt = make("option", "Все склады"); prompt.value = "";
    field.replaceChildren(prompt, ...page.items.map(v => { const option = make("option", v.name); option.value = v.external_id; return option; }));
    if (warehouse && ![...field.options].some(v => v.value === warehouse)) { const option = make("option", "Склад " + warehouse); option.value = warehouse; field.append(option); }
    field.value = warehouse;
    document.getElementById("wb-warehouse-chips").replaceChildren(...[["","Все склады"],...warehouseNames].map(([value,label])=>{const button=clickButton(label,async()=>{warehouse=value;field.value=value;offset=0;selected.clear();revision++;await refresh();document.querySelectorAll("#wb-warehouse-chips button").forEach(v=>v.setAttribute("aria-pressed",String(v.dataset.warehouse===warehouse)));});button.dataset.warehouse=value;button.setAttribute("aria-pressed",String(value===warehouse));return button;}));
  }
  let links = [], parameters = {}, currentAction = null, product = null, currentOrder = null;
  let pageItems = [], supplyItems = [], deliverySupply = null;
  let nkOffset = 0, nkPage = [], codeOffset = 0, refreshSequence = 0;
  const selected = new Map(), selectedCodes = new Set();
  const base = () => `${root}/${encodeURIComponent(connection)}`;
  const actionNames = { expiration: "Срок годности", sgtin: "Передача кодов ЧЗ", supply_create: "Создание поставки", supply_add: "Состав поставки", supply_deliver: "Передача в доставку", supply_delete: "Удаление пустой поставки" };
  const actionStates = { draft: "Подготовлено", queued: "В очереди", submitting: "Отправляется", accepted: "WB принял запрос", pending: "Ожидает подтверждения WB", confirmed: "Подтверждено WB", rejected: "Отклонено", unknown: "Ответ потерян — требуется сверка", conflict: "Результат расходится — требуется проверка", partial: "Часть заданий добавлена", cancelled: "Отменено" };
  const syncStates = {queued: "В очереди", running: "Обновляется", succeeded: "Обновлено", failed: "Ошибка чтения", interrupted: "Чтение прервано"};
  const orderStates = { new: "Новое", confirm: "На сборке", complete: "В доставке", cancel: "Отменено" };
  const supplyStates = { open: "На сборке", closed: "Ожидает отгрузки", scanned: "Передано WB", deleted: "Удалено" };
  function statusOptions(previewStage = stage, previewStatus = status) {
    const states = previewStage === "shipping" || (!previewStage && kind === "supplies") ? supplyStates : orderStates;
    const field = filterForm.elements.status, prompt = make("option", "Все статусы"); prompt.value = "";
    field.replaceChildren(prompt, ...Object.entries(states).map(([value, label]) => { const option = make("option", label); option.value = value; return option; }));
    field.value = Object.hasOwn(states, previewStatus) ? previewStatus : "";
  }
  filterForm.elements.stage.addEventListener("change", () => statusOptions(filterForm.elements.stage.value, ""));
  function orderDetails(item) {
    const source = item.attributes.source || {}, dialog = document.getElementById("wb-order-dialog");
    actionContext(dialog, selector.selectedOptions[0].textContent, parameters.tin);
    document.getElementById("wb-order-title").textContent = "Задание #" + item.external_id;
    document.getElementById("wb-order-details").replaceChildren(...[
      ["Товар", source.article || "Без артикула"], ["Статус", orderStates[item.status] || item.status],
      ["Склад", warehouseNames.get(String(item.warehouse_external_id)) || item.warehouse_external_id || "Не указан"],
      ["Поставка", item.supply_external_id || "Не назначена"], ["Создано", source.createdAt || "—"]
    ].map(([label, value]) => make("p", label + ": " + value)));
    document.getElementById("wb-order-source").textContent = JSON.stringify(item.attributes, null, 2);
    dialog.showModal();
  }
  if (!view.status && !view.stage && kind === "orders") { status = "new"; filterForm.elements.status.value = status; }
  statusOptions();
  const syncPhases = { warehouses: "Склады", products: "Товары", supplies: "Поставки", orders: "История заданий", new: "Новые задания" };
  const closeDialogs = () => { document.querySelectorAll('dialog[id^="wb-"]').forEach(v => v.close()); currentAction = null; };
  document.querySelectorAll(".wb-close").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
  function clickButton(text, handler, secondary = true) {
    const button = make("button", text, secondary ? "secondary" : ""); button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true; feedback.hidden = true;
      try { await handler(); } catch (error) { showError(error); }
      finally { button.disabled = false; }
    });
    return button;
  }
  function selectionCount() {
    document.getElementById("wb-selection").textContent = selected.size ? `${selected.size} заказов выбрано` : "Выберите заказы";
    document.getElementById("packing-empty").hidden = selected.size > 0;
    const values = [...selected.values()], assembling = values.length > 0 && values.every(v => v.status === "confirm");
    for (const id of ["packing-expiration", "packing-codes"]) document.getElementById(id).disabled = !assembling || !!parameters.read_only;
    document.getElementById("packing-labels").disabled = !assembling;
    document.getElementById("packing-print-codes").disabled = !assembling || !values.every(v => v.marking.length && v.marking.every(c => c.state === "confirmed"));
    document.getElementById("packing-help").textContent = selected.size ? "Пройдите шаги сверху вниз. Результаты сохраняются для каждого заказа." : "Отметьте нужные карточки или выберите всю страницу.";
    document.getElementById("packing-expiry-status").textContent = `${values.filter(v => v.packing?.expiration?.state === "confirmed").length} из ${values.length} · подтверждено WB`;
    document.getElementById("packing-label-status").textContent = `${values.filter(v => v.packing?.printed.includes("orders")).length} из ${values.length} · печать подтверждена`;
    document.getElementById("packing-code-status").textContent = `${values.filter(v => v.marking.length && v.marking.every(c => c.state === "confirmed")).length} из ${values.length} · КИЗы приняты WB`;
    const eligible = pageItems.filter(v => ["new", "confirm"].includes(v.status) && v.attributes.source?.deliveryType === "fbs");
    const all = document.getElementById("wb-select-page"); all.checked = eligible.length > 0 && eligible.every(v => selected.has(v.id)); all.indeterminate = !all.checked && eligible.some(v => selected.has(v.id));
    document.querySelectorAll(".order-card").forEach(v => v.classList.toggle("is-selected", selected.has(v.dataset.orderId)));
    document.getElementById("wb-order-tools").hidden = kind !== "orders" || !selected.size;
    document.getElementById("wb-add-orders").disabled = !selected.size || !document.getElementById("wb-supply-select").value || !!parameters.read_only;
  }
  async function prepare(actionKind, payload) {
    const version = revision, endpoint = base();
    const action = await api(`${endpoint}/actions`, "POST", {kind: actionKind, payload});
    if (version !== revision) return;
    closeDialogs(); renderAction(action); document.getElementById("wb-action-dialog").showModal();
    await refresh();
  }
  function renderAction(action) {
    actionContext(document.getElementById("wb-action-dialog"), selector.selectedOptions[0].textContent, parameters.tin);
    currentAction = action.id;
    document.getElementById("wb-action-title").textContent = actionNames[action.kind];
    document.getElementById("wb-action-status").textContent = `${actionStates[action.state] || action.state}${action.error_code ? ` · ${action.error_code}` : ""}`;
    const body = action.body;
    document.getElementById("wb-action-preview").textContent = action.kind === "sgtin"
      ? `Задание #${body.order_external_id} · GTIN ${body.gtin} · Группа ${body.product_group}\n${body.cis.length} кодов:\n${body.cis.join("\n")}`
      : action.kind === "expiration" ? `Задание #${body.order_external_id} · ${body.expiration} · ${body.batch_name}`
      : action.kind === "supply_create" ? `Новая поставка: ${body.name}\nНазвание в WB: ${body.remote_name}`
      : `Поставка ${body.supply_id}${body.orders ? `\nЗадания: ${body.orders.join(", ")}` : ""}`;
    document.getElementById("wb-action-body").textContent = JSON.stringify(body, null, 2);
    document.getElementById("wb-action-result").textContent = action.result.reason || (action.result.decision ? `Проверка WB: ${action.result.decision}` : "");
    document.getElementById("wb-action-events").replaceChildren(...(action.events || []).map(event => make("p", `${new Date(event.created_at).toLocaleString("ru-RU")} · ${actionStates[event.state] || event.state}`, "chz-event")));
    document.getElementById("wb-action-send").hidden = action.state !== "draft" || parameters.read_only;
    document.getElementById("wb-action-cancel").hidden = !["draft", "rejected"].includes(action.state) || action.acknowledged;
    document.getElementById("wb-action-reconcile").hidden = ["draft", "queued", "submitting", "confirmed", "cancelled"].includes(action.state);
    document.getElementById("wb-action-retry").hidden = action.kind !== "sgtin" || action.state !== "unknown" || action.acknowledged || parameters.read_only;
  }
  async function openAction(id) {
    const version = revision;
    const action = await api(`${root}/actions/${encodeURIComponent(id)}`);
    if (version !== revision) return;
    renderAction(action); document.getElementById("wb-action-dialog").showModal();
  }
  for (const [id, suffix] of [["wb-action-send", "send"], ["wb-action-reconcile", "reconcile"], ["wb-action-cancel", "cancel"], ["wb-action-retry", "retry-codes"]]) {
    document.getElementById(id).addEventListener("click", async event => {
      const button = event.target, actionId = currentAction, version = revision;
      button.disabled = true;
      try {
        await api(`${root}/actions/${encodeURIComponent(actionId)}/${suffix}`, "POST", {});
        const action = await api(`${root}/actions/${encodeURIComponent(actionId)}`);
        if (version === revision && currentAction === actionId) renderAction(action);
        if (version === revision) await refresh();
      } catch (error) { if (version === revision) showError(error); }
      finally { button.disabled = false; }
    });
  }
  function renderRecords(page) {
    pageItems = page.items;
    document.querySelector(".select-page").hidden = kind !== "orders";
    const labels = { products: ["Товар", "Размеры / штрихкоды", "Честный Знак"], warehouses: ["Склад", "ID", "Источник"],
      orders: ["Выбрать", "Задание", "Статус", "Поставка / склад", "Коды ЧЗ"], supplies: ["Поставка", "Статус", "Действия"] };
    const head = make("tr"); head.append(...labels[kind].map(v => make("th", v)));
    document.getElementById("wb-head").replaceChildren(head);
    const scroll = {x: window.scrollX, y: window.scrollY};
    const rows = page.items.map(item => {
      const row = make("tr"), source = item.attributes.source || {};
      if (kind === "products") {
        const title = make("td", item.title); title.append(make("small", `WB ${item.external_id} · ${item.sku || ""}`));
        const identifiers = make("td", (source.sizes || []).map(v => `${v.techSize || "Размер"} · chrtID ${v.chrtID} · ${(v.skus || []).join(", ")}`).join("; "));
        const action = make("td"); action.append(clickButton("Связать с ЧЗ", () => openLink(item)));
        row.append(title, identifiers, action);
      } else if (kind === "warehouses") {
        row.append(make("td", item.name), make("td", item.external_id), make("td", "Wildberries"));
      } else if (kind === "orders") {
        row.className = "order-card"; row.dataset.orderId = item.id;
        const cell = make("td"), checkbox = make("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(item.id); checkbox.setAttribute("aria-label", `Выбрать задание ${item.external_id}`);
        checkbox.disabled = !["new", "confirm"].includes(item.status) || source.deliveryType !== "fbs";
        if (checkbox.disabled) { selected.delete(item.id); checkbox.checked = false; } else if (selected.has(item.id)) selected.set(item.id, item);
        checkbox.addEventListener("change", () => { if (checkbox.checked) selected.set(item.id, item); else selected.delete(item.id); selectionCount(); });
        const top = make("div", undefined, "order-top"), number = clickButton(`#${item.external_id}`, () => orderDetails(item)); number.className = "order-number";
        top.append(checkbox, number);
        if (source.createdAt) top.append(make("small", new Date(source.createdAt).toLocaleDateString("ru-RU", {day:"numeric",month:"short"})));
        const productRow = make("div", undefined, "order-product"), thumb = make("div", "▧", "product-thumb"), copy = make("div");
        if (item.packing?.image) { const img = make("img"); img.src = item.packing.image; img.alt = ""; img.loading = "lazy"; img.referrerPolicy = "no-referrer"; img.addEventListener("error",()=>img.remove(),{once:true}); thumb.replaceChildren(img); }
        copy.append(make("h3", item.packing?.title || source.article || "Товар"), make("small", `${source.article || "Без артикула"}${source.color ? " · " + source.color : ""}`)); productRow.append(thumb, copy);
        const detail = make("div", undefined, "order-details-row"); detail.append(make("span", warehouseNames.get(String(item.warehouse_external_id)) || "Склад не указан"), make("span", orderStates[item.status] || item.status, "order-status"));
        const badges = make("div", undefined, "order-badges"), expiry = item.packing?.expiration;
        if (item.supply_external_id) { const supply = supplyItems.find(v=>v.external_id === item.supply_external_id); badges.append(make("span", cleanSupplyName(supply?.attributes.source?.name || item.supply_external_id), "order-badge")); }
        if (expiry) badges.append(make("span", `Годен до ${expiry.expiration}`, "order-badge " + (expiry.state === "confirmed" ? "ready" : "attention")));
        if (item.packing?.printed.includes("orders")) badges.append(make("span", "✓ Этикетка", "order-badge ready"));
        if (!item.packing?.product_id) badges.append(make("span", "Нет связи с ассортиментом", "order-badge attention"));
        const marking = make("div", undefined, "wb-marking-cell");
        if (item.marking.length) {
          const ok = item.marking.every(v=>v.state === "confirmed");
          marking.append(clickButton(ok ? `✓ КИЗы приняты · ${item.marking.length}` : `КИЗы · ${actionStates[item.marking[0].state] || "Ожидаем"}`, () => openAction(item.marking[0].action_id)));
        } else if (item.status === "confirm") { const assign = clickButton("Выбрать КИЗы вручную", () => openCodes(item)); assign.disabled = !!parameters.read_only; marking.append(assign); }
        cell.append(top, productRow, detail, badges, marking); row.append(cell);
      } else {
        row.className = "supply-card";
        const title = make("td"); title.append(make("h3", cleanSupplyName(source.name || item.external_id)), make("small", item.external_id));
        const status = make("td", supplyStates[item.status] || item.status);
        const actions = make("td", undefined, "supply-actions");
        actions.append(clickButton("Состав", async () => {
          const version = revision, result = await api(`${base()}/supplies/${item.id}/details`);
          if (version !== revision) return;
          const dialog = document.getElementById("wb-action-dialog"); currentAction = null;
          document.getElementById("wb-action-title").textContent = `Состав ${item.external_id}`;
          document.getElementById("wb-action-status").textContent = `${result.orders.length} заданий по актуальному ответу WB`;
          document.getElementById("wb-action-preview").textContent = result.orders.map(v => `#${v.external_id} · ${v.cached?.attributes.source.article || "Товар вне локальной истории"} · ${orderStates[v.status] || v.status} · Метаданные: ${Object.entries(v.metadata).map(([k, x]) => `${k}: ${x.decision || "не подтверждено"}`).join(", ") || "не требуются"}`).join("\n");
          document.getElementById("wb-action-body").textContent = result.orders.map(v => `#${v.external_id} · ${v.cached?.attributes.source.article || "Детали товара вне локальной истории"} · ${orderStates[v.status] || v.status}\n${JSON.stringify(v.metadata)}`).join("\n\n");
          document.getElementById("wb-action-result").textContent = ""; document.getElementById("wb-action-events").replaceChildren();
          for (const id of ["wb-action-send", "wb-action-cancel", "wb-action-reconcile", "wb-action-retry"]) document.getElementById(id).hidden = true;
          dialog.showModal();
        }));
        if (item.status === "open" && !parameters.read_only) {
          actions.append(clickButton("Выбрать для заданий", () => { selectSupply(item); setKind("orders"); }));
          actions.append(clickButton("Проверить и отгрузить", () => openDelivery(item), false));
          actions.append(clickButton("Удалить пустую", () => prepare("supply_delete", { supply_id: item.id })));
        }
        if (["closed", "scanned"].includes(item.status)) actions.append(clickButton("Распечатать QR поставки", () => printLabels("supply", item.id), false));
        row.append(title, status, actions);
      }
      return row;
    });
    if (!rows.length) { const row = make("tr"), cell = make("td", "Нет записей. Обновите WB или измените поиск."); cell.colSpan = labels[kind].length; row.append(cell); rows.push(row); }
    document.getElementById("wb-records").replaceChildren(...rows);
    window.scrollTo(scroll.x, scroll.y);
    document.getElementById("wb-count").textContent = page.total ? `${offset + 1}–${offset + page.items.length} из ${page.total}` : "0 записей";
    document.getElementById("wb-prev").disabled = offset === 0;
    document.getElementById("wb-next").disabled = offset + page.items.length >= page.total;

    selectionCount();
  }
  function selectSupply(item) {
    const select = document.getElementById("wb-supply-select");
    if (![...select.options].some(v => v.value === item.id)) { const option = make("option", `${item.attributes.source?.name || item.external_id} · ${item.external_id}`); option.value = item.id; select.append(option); }
    select.value = item.id;
  }
  async function refresh() {
    const sequence = ++refreshSequence;
    const version = revision, endpoint = base(), activeKind = kind, activeOffset = offset, activeSearch = search;
    const [overview, page, newLinks, actions, supplies] = await Promise.all([
      api(`${endpoint}/overview`), api(`${endpoint}/records/${activeKind}?offset=${activeOffset}&search=${encodeURIComponent(activeSearch)}&stage=${encodeURIComponent(stage)}&status=${encodeURIComponent(status)}&warehouse=${encodeURIComponent(warehouse)}`),
      api(`${endpoint}/links`), api(`${endpoint}/actions`), api(`${endpoint}/records/supplies?limit=200`),
    ]);
    if (sequence !== refreshSequence || version !== revision || activeKind !== kind || activeOffset !== offset || activeSearch !== search) return;
    parameters = overview.parameters; links = newLinks; supplyItems = supplies.items;
    for (const field of document.querySelectorAll("[data-stage-count]")) field.textContent = field.dataset.stageCount === "supplies" ? overview.counts.supplies : overview.stages?.[field.dataset.stageCount] || 0;
    document.querySelectorAll("[data-order-status]").forEach(v => v.setAttribute("aria-current", String(v.dataset.orderStatus === (kind === "supplies" ? "supplies" : status || "new"))));
    document.querySelector('#wb-create-supply [type="submit"]').disabled = !!parameters.read_only;
    const sync = overview.snapshots.sync?.value;
    document.getElementById("wb-sync").disabled = ["queued", "running"].includes(sync?.state);
    document.getElementById("wb-sync-status").textContent = sync ? `Синхронизация: ${syncStates[sync.state] || "Проверьте обновление"} · ${syncPhases[sync.phase] || ""}${sync.error ? ` · ${sync.error}` : ""}` : "Ещё не синхронизировано";
    document.getElementById("wb-summary").textContent = `Обновлено: ${overview.snapshots.last_sync ? new Date(overview.snapshots.last_sync.updated_at).toLocaleString("ru-RU") : "еще не обновлялось"}${parameters.read_only ? " · Только чтение" : ""}`;
    renderRecords(page);
    saveFlowView("wb", {connection,kind,offset,search,stage,status,warehouse});
    const list = document.getElementById("wb-links");
    list.replaceChildren(...links.map(link => { const row = make("article", `${link.product_title} · chrtID ${link.chrt_id} → ${link.chz_title} · GTIN ${link.gtin} · ${link.product_group}`, "connection"); row.append(clickButton("Удалить связь", async () => { await api(`${endpoint}/links/${link.id}`, "DELETE", {}); if (version === revision) await refresh(); })); return row; }));
    if (!links.length) list.append(make("p", "Откройте «Товары» и свяжите нужный размер с карточкой НК.", "muted"));
    document.getElementById("wb-actions").replaceChildren(...actions.map(action => { const row = make("article", undefined, "connection"); row.append(clickButton(`${actionNames[action.kind]} · ${actionStates[action.state] || action.state}`, () => openAction(action.id))); row.append(make("small", new Date(action.created_at).toLocaleString("ru-RU"))); return row; }));
    const select = document.getElementById("wb-supply-select"), previous = select.value, old = [...select.options].find(v => v.value === previous);
    const placeholder = make("option", "Выберите поставку (другие — в разделе «Поставки»)"); placeholder.value = "";
    select.replaceChildren(placeholder, ...supplies.items.filter(v => v.status === "open").map(v => { const option = make("option", `${v.attributes.source?.name || v.external_id} · ${v.external_id}`); option.value = v.id; return option; }));
    if (old && ![...select.options].some(v => v.value === previous)) select.append(old);
    select.value = previous;
    updateSupplyPicker();
    if (currentAction && document.getElementById("wb-action-dialog").open) {
      const actionId = currentAction, action = await api(`${root}/actions/${encodeURIComponent(actionId)}`);
      if (version === revision && currentAction === actionId) renderAction(action);
    }
  }
  async function setKind(value) {
    kind = value; offset = 0; search = ""; stage = ""; status = ""; warehouse = ""; filterForm.reset(); statusOptions(); selected.clear(); selectionCount(); revision++;
    document.querySelectorAll("#wb-tabs button").forEach(v => v.classList.toggle("secondary", v.dataset.kind !== kind));
    try { await refresh(); } catch (error) { showError(error); }
  }
  document.querySelectorAll("#wb-tabs button").forEach(button => button.addEventListener("click", () => setKind(button.dataset.kind)));
  document.getElementById("wb-prev").addEventListener("click", async () => { offset = Math.max(0, offset - 100); try { await refresh(); } catch (error) { showError(error); } });
  document.getElementById("wb-next").addEventListener("click", async () => { offset += 100; try { await refresh(); } catch (error) { showError(error); } });
  bindForm("wb-search", async fields => {
    search = fields.get("search"); stage = fields.get("stage"); status = fields.get("status"); warehouse = fields.get("warehouse"); offset = 0; revision++;
    if (stage === "shipping") kind = "supplies";
    else if (stage) kind = "orders";
    if (!["orders", "supplies"].includes(kind)) { stage = ""; status = ""; warehouse = ""; }
    selected.clear(); selectionCount();
    document.querySelectorAll("#wb-tabs button").forEach(v => v.classList.toggle("secondary", v.dataset.kind !== kind));
    await refresh();
  });
  document.getElementById("wb-sync").addEventListener("click", async event => { event.target.disabled = true; try { await api(`${base()}/sync`, "POST", {}); await refresh(); } catch (error) { showError(error); event.target.disabled = false; } });
  bindForm("wb-token", async fields => { const version = revision; await api(`${base()}/credentials`, "PUT", {token: fields.get("token")}); document.getElementById("wb-token").reset(); if (version === revision) await refresh(); });
  bindForm("wb-create-supply", async fields => {
    const version = revision, ids = [...selected.keys()];
    document.getElementById("packing-create-dialog").close();
    const action = await runAction("supply_create", {name: fields.get("name")});
    if (version !== revision) return;
    await refresh();
    let supply = supplyItems.find(v => v.external_id === action.result.supply_id);
    if (!supply) { await new Promise(r=>setTimeout(r,700)); await refresh(); supply = supplyItems.find(v=>v.external_id === action.result.supply_id); }
    if (!supply) throw new Error("Поставка создана. Обновите WB, чтобы добавить заказы.");
    selectSupply(supply); updateSupplyPicker();
    if (ids.length) { await runAction("supply_add", {supply_id:supply.id,order_ids:ids}); selected.clear(); await refresh(); }
  });
  document.getElementById("wb-add-orders").addEventListener("click", async event => {
    event.target.disabled = true;
    try { await runAction("supply_add", {supply_id:document.getElementById("wb-supply-select").value,order_ids:[...selected.keys()]}); selected.clear(); await refresh(); } catch (error) { showError(error); } finally { selectionCount(); }
  });


  async function openLink(item) {
    const version = revision, values = await api(`${sellerApi}/connections`);
    if (version !== revision) return;
    product = item; nkOffset = 0;
    const form = document.getElementById("wb-link-form"), source = item.attributes.source;
    document.getElementById("wb-link-title").textContent = `${item.title} · WB ${item.external_id}`;
    form.elements.chrt_id.replaceChildren(...(source.sizes || []).map(v => { const option = make("option", `${v.techSize || "Размер"} · chrtID ${v.chrtID}`); option.value = v.chrtID; return option; }));
    form.elements.chz_connection_id.replaceChildren(...values.filter(v => v.adapter_key === "chz" && v.external_account_id === `production:${parameters.tin}`).map(v => { const option = make("option", `${v.name} · ${v.external_account_id}`); option.value = v.id; return option; }));
    if (!form.elements.chz_connection_id.value) throw new Error("Подключите производственный аккаунт ЧЗ с тем же ИНН и синхронизируйте НК.");
    await loadNk(version); if (version === revision) document.getElementById("wb-link-dialog").showModal();
  }
  async function loadNk(version = revision) {
    const form = document.getElementById("wb-link-form"), chz = form.elements.chz_connection_id.value;
    const [page, overview] = await Promise.all([api(`${sellerApi}/marking/${chz}/products?offset=${nkOffset}&search=${encodeURIComponent(document.getElementById("wb-nk-search").value)}`), api(`${sellerApi}/marking/${chz}/overview`)]);
    if (version !== revision || chz !== form.elements.chz_connection_id.value) return;
    nkPage = page.items.filter(v => v.id && v.present && v.detail_available);
    form.elements.chz_product_id.replaceChildren(...nkPage.map(v => { const option = make("option", v.title); option.value = v.id; return option; }));
    form.elements.product_group.replaceChildren(...(overview.snapshots.account?.value.productGroups || []).map(v => { const option = make("option", v); option.value = v; return option; }));
    document.getElementById("wb-nk-count").textContent = `${page.total} карточек`;
    document.getElementById("wb-nk-prev").disabled = nkOffset === 0;
    document.getElementById("wb-nk-next").disabled = nkOffset + page.items.length >= page.total;
    setGtin();
  }
  function setGtin() {
    const form = document.getElementById("wb-link-form"), item = nkPage.find(v => v.id === form.elements.chz_product_id.value);
    form.elements.gtin.replaceChildren(...(item?.identifiers.gtin || []).map(v => { const option = make("option", v); option.value = v; return option; }));
  }
  document.getElementById("wb-link-form").elements.chz_product_id.addEventListener("change", setGtin);
  document.getElementById("wb-link-form").elements.chz_connection_id.addEventListener("change", async () => { nkOffset = 0; try { await loadNk(); } catch (error) { showError(error); } });
  for (const [id, delta] of [["wb-nk-find", 0], ["wb-nk-prev", -100], ["wb-nk-next", 100]]) document.getElementById(id).addEventListener("click", async () => { nkOffset = delta ? Math.max(0, nkOffset + delta) : 0; try { await loadNk(); } catch (error) { showError(error); } });
  bindForm("wb-link-form", async fields => { const version = revision; await api(`${base()}/links`, "POST", { product_id: product.id, chrt_id: fields.get("chrt_id"), chz_connection_id: fields.get("chz_connection_id"), chz_product_id: fields.get("chz_product_id"), gtin: fields.get("gtin"), product_group: fields.get("product_group") }); if (version === revision) { document.getElementById("wb-link-dialog").close(); await refresh(); } });

  async function openCodes(item) {
    const source = item.attributes.source;
    const link = links.find(v => v.chrt_id === String(source.chrtId) && v.product_external_id === String(source.nmId));
    if (!link) throw new Error("Сначала свяжите этот размер WB с товаром Честного Знака.");
    currentOrder = { ...item, link }; codeOffset = 0; selectedCodes.clear();
    document.getElementById("wb-codes-title").textContent = `Задание #${item.external_id} · ${link.product_title} · GTIN ${link.gtin}`;
    const version = revision; await loadCodes(); if (version === revision) document.getElementById("wb-codes-dialog").showModal();
  }
  async function loadCodes() {
    const version = revision, order = currentOrder;
    const page = await api(`${base()}/orders/${order.id}/available-codes?offset=${codeOffset}`);
    if (version !== revision || currentOrder?.id !== order.id) return;
    const rows = page.items.map(v => { const row = make("tr"), cell = make("td"), checkbox = make("input"); checkbox.type = "checkbox"; checkbox.checked = selectedCodes.has(v.id); checkbox.setAttribute("aria-label", `Выбрать код ${v.code}`); checkbox.addEventListener("change", () => { if (checkbox.checked) selectedCodes.add(v.id); else selectedCodes.delete(v.id); }); cell.append(checkbox); row.append(cell, make("td", v.code, "chz-code"), make("td", v.external_status || "Статус ещё не проверен")); return row; });
    if (!rows.length) { const row = make("tr"), cell = make("td", "Нет свободных полных КМ для этого GTIN. Получите и введите коды в оборот в разделе ЧЗ."); cell.colSpan = 3; row.append(cell); rows.push(row); }
    document.getElementById("wb-codes-list").replaceChildren(...rows);
    document.getElementById("wb-codes-count").textContent = `${page.total} свободных кодов`;
    document.getElementById("wb-codes-prev").disabled = codeOffset === 0;
    document.getElementById("wb-codes-next").disabled = codeOffset + page.items.length >= page.total;
  }
  for (const [id, delta] of [["wb-codes-prev", -100], ["wb-codes-next", 100]]) document.getElementById(id).addEventListener("click", async () => { codeOffset = Math.max(0, codeOffset + delta); try { await loadCodes(); } catch (error) { showError(error); } });
  bindForm("wb-codes-form", () => prepare("sgtin", { order_id: currentOrder.id, code_ids: [...selectedCodes] }));
  selector.addEventListener("change", async () => {
    connection = selector.value; const version = ++revision;
    warehouse = ""; warehouseNames.clear(); parameters = {read_only: true};
    selected.clear(); selectedCodes.clear(); closeDialogs(); offset = 0; links = []; selectionCount();
    document.querySelector('#wb-create-supply [type="submit"]').disabled = true;
    const row = make("tr"), cell = make("td", "Загружаем задания аккаунта…"); cell.colSpan = 5; row.append(cell);
    document.getElementById("wb-records").replaceChildren(row);
    try { await loadWarehouseFilter(); if (version === revision) await refresh(); }
    catch (error) { if (version === revision) showError(error); }
  });
  async function poll() { try { await refresh(); } catch (error) { showError(error); } window.setTimeout(poll, refreshSeconds * 1000); }
  document.querySelectorAll("#wb-tabs button").forEach(v => v.classList.toggle("secondary", v.dataset.kind !== kind));
  if (pageQuery.get("history") === "1") document.getElementById("wb-history").open = true;
  loadWarehouseFilter().then(poll).catch(error => { showError(error); poll(); });
  function cleanSupplyName(value) { return value.replace(/ \[FBE [a-f0-9-]+\]$/, ""); }
  function updateSupplyPicker() {
    const select = document.getElementById("wb-supply-select"), supply = supplyItems.find(v=>v.id === select.value);
    document.getElementById("packing-pick-supply").textContent = supply ? cleanSupplyName(supply.attributes.source?.name || supply.external_id) : "Выбрать поставку +";
    document.getElementById("wb-add-orders").disabled = !selected.size || !select.value || !!parameters.read_only;
  }
  function packingResult(text, error=false) { const p=make("p",text,"packing-result"+(error?" error":""));document.getElementById("packing-results").replaceChildren(p); }
  async function runAction(actionKind,payload) {
    const version=revision, endpoint=base();
    const action=await api(endpoint+"/actions","POST",{kind:actionKind,payload});
    if(version!==revision)throw new Error("Аккаунт изменен. Действие осталось в истории, отправка не выполнена.");
    await api(`${root}/actions/${action.id}/send`,"POST",{});
    packingResult(`${actionNames[actionKind]}: выполняется…`);
    for(let i=0;i<40;i++) {
      await new Promise(r=>setTimeout(r,750));
      const result=await api(`${root}/actions/${action.id}`);
      if(version!==revision)throw new Error("Аккаунт изменен. Результат доступен в истории исходного аккаунта.");
      if(result.state==="confirmed") {packingResult(`${actionNames[actionKind]}: подтверждено WB`);return result;}
      if(["rejected","unknown","conflict","partial","cancelled"].includes(result.state)) {renderAction(result);document.getElementById("wb-action-dialog").showModal();throw new Error(result.result.reason || actionStates[result.state]);}
    }
    throw new Error("WB еще обрабатывает действие. Результат появится в истории; повторно отправлять не нужно.");
  }
  function bindPacking(id,fn) { document.getElementById(id).addEventListener("click",async event=>{event.currentTarget.disabled=true;try{await fn();}catch(e){showError(e);packingResult(e.message,true);}finally{event.target.disabled=false;selectionCount();updateSupplyPicker();}}); }
  document.getElementById("wb-select-page").addEventListener("change",event=>{
    for(const item of pageItems) if(["new","confirm"].includes(item.status)&&item.attributes.source?.deliveryType==="fbs") {if(event.target.checked)selected.set(item.id,item);else selected.delete(item.id);}
    document.querySelectorAll(".order-card input[type=checkbox]").forEach(v=>v.checked=selected.has(v.closest(".order-card").dataset.orderId));selectionCount();updateSupplyPicker();
  });
  document.querySelectorAll("[data-order-status]").forEach(button=>button.addEventListener("click",async()=>{
    kind=button.dataset.orderStatus==="supplies"?"supplies":"orders";status=kind==="orders"?button.dataset.orderStatus:"";stage="";offset=0;revision++;selected.clear();statusOptions();filterForm.elements.stage.value="";selectionCount();
    document.querySelectorAll("#wb-tabs button").forEach(v=>v.classList.toggle("secondary",v.dataset.kind!==kind));
    try{await refresh();}catch(e){showError(e);}
  }));
  bindPacking("packing-pick-supply",async()=>{
    const candidates=supplyItems.filter(v=>v.status==="open");
    document.getElementById("packing-supplies").replaceChildren(...candidates.map(supply=>{
      const button=clickButton(cleanSupplyName(supply.attributes.source?.name||supply.external_id),()=>{selectSupply(supply);updateSupplyPicker();document.getElementById("packing-supply-dialog").close();});button.className="supply-choice";button.append(make("small",supply.external_id));return button;
    }));
    if(!candidates.length)document.getElementById("packing-supplies").append(make("p","Нет открытых поставок. Создайте новую для выбранных заказов."));
    document.getElementById("packing-supply-dialog").showModal();
  });
  function newSupply() {
    document.getElementById("packing-supply-dialog").close();
    const warehouses=[...new Set([...selected.values()].map(v=>warehouseNames.get(String(v.warehouse_external_id))||"Склад"))];
    if(warehouses.length>1)throw new Error("Выберите заказы одного склада. Для остальных создайте отдельную поставку.");
    const now=new Date(), hour=now.getHours(), shift=hour<12?"Утро":hour<18?"День":"Вечер";
    document.querySelector('#wb-create-supply [name="name"]').value=`${warehouses[0]||"WB"} · ${now.toLocaleDateString("ru-RU")} · ${shift} ${now.toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit"})}`.slice(0,95);
    document.getElementById("packing-create-dialog").showModal();
  }
  bindPacking("packing-new-supply",newSupply);bindPacking("packing-create-from-picker",newSupply);
  bindPacking("packing-expiration",()=>{
    const rows=[...selected.values()].map(item=>{
      const row=make("section",undefined,"expiry-row");row.append(make("h3",`#${item.external_id} · ${item.packing?.title||item.attributes.source.article}`));
      const batches=item.packing?.batches||[], options=make("div",undefined,"batch-options");
      if(!batches.length) { const link=make("a","Добавить партию в ассортименте");link.href=`/sellers/${encodeURIComponent(sellerId)}/catalog`;row.append(link); }
      for(const batch of batches) {
        const label=make("label",undefined,"batch-option"), radio=make("input");radio.type="radio";radio.name=item.id;radio.value=batch.id;radio.required=true;
        radio.disabled=!batch.expires_on||new Date(batch.expires_on+"T23:59:59")-Date.now()<30*86400000;radio.checked=!radio.disabled&&(batches.length===1||batch.id===item.packing?.expiration?.batch_id);
        label.append(radio,make("span",`${batch.name} · годен до ${batch.expires_on||"дата не указана"}${radio.disabled?" · недоступна для WB":""}`));options.append(label);
      }
      row.append(options);return row;
    });
    document.getElementById("packing-expiry-items").replaceChildren(...rows);document.getElementById("packing-expiry-dialog").showModal();
  });
  bindForm("packing-expiry-form",async fields=>{
    const items=[...selected.keys()].map(order_id=>({order_id,batch_id:fields.get(order_id)}));
    if(items.some(v=>!v.batch_id))throw new Error("Выберите партию для каждого заказа. Заказы без партий можно убрать из выбора.");
    const version=revision,result=await api(base()+"/packing/expiration","POST",{items});
    if(version!==revision)return;document.getElementById("packing-expiry-dialog").close();showBulkResult(result);await refresh();
  });
  function showBulkResult(values) {
    document.getElementById("packing-results").replaceChildren(...values.map(v=>{const item=selected.get(v.order_id);return make("p",`#${item?.external_id||v.order_id} · ${v.error||"В очереди. Ожидаем подтверждение WB"}`,"packing-result"+(v.error?" error":""));}));
  }
  bindPacking("packing-codes",async()=>{const version=revision,result=await api(base()+"/packing/codes","POST",{order_ids:[...selected.keys()]});if(version!==revision)return;showBulkResult(result);await refresh();});
  async function printLabels(labelKind,supply_id=null) {
    // Open synchronously so the browser does not block the print tab after fetch.
    const tab=window.open("about:blank","_blank");if(tab)tab.opener=null;
    try {const result=await api(base()+"/packing/print","POST",{kind:labelKind,order_ids:labelKind==="supply"?[]:[...selected.keys()],supply_id});const url=`/sellers/${encodeURIComponent(sellerId)}/printing/wb/${encodeURIComponent(result.id)}`;
      if(tab)tab.location.href=url;else{const link=make("a","Открыть этикетки");link.href=url;link.target="_blank";link.rel="noopener";document.getElementById("packing-results").replaceChildren(link);} }
    catch(e){if(tab)tab.close();throw e;}
  }
  bindPacking("packing-labels",()=>printLabels("orders"));bindPacking("packing-print-codes",()=>printLabels("codes"));
  async function openDelivery(item) {
    const version=revision, result=await api(`${base()}/supplies/${item.id}/details`);if(version!==revision)return;
    deliverySupply=item;document.getElementById("packing-delivery-title").textContent=cleanSupplyName(item.attributes.source?.name||item.external_id);
    const blockers=result.orders.filter(v=>v.status!=="confirm"||Object.values(v.metadata).some(m=>m.decision!=="filled"));
    document.getElementById("packing-delivery-content").replaceChildren(make("p",`${result.orders.length} заказов · ${blockers.length?blockers.length+" требуют внимания":"метаданные подтверждены WB"}`),...result.orders.map(v=>{const missing=Object.entries(v.metadata).filter(([,m])=>m.decision!=="filled").map(([key])=>({expiration:"срок годности",sgtin:"КИЗы"})[key]||key);return make("div",`#${v.external_id} · ${v.cached?.attributes.source.article||"Товар"} · ${missing.length?"Проверить: "+missing.join(", "):orderStates[v.status]||v.status}`,"delivery-order"+(missing.length?" blocked":""));}));
    const check=document.getElementById("packing-physical");check.checked=false;check.disabled=blockers.length>0||!result.orders.length;document.getElementById("packing-deliver").disabled=true;document.getElementById("packing-delivery-dialog").showModal();
  }
  document.getElementById("packing-physical").addEventListener("change",event=>document.getElementById("packing-deliver").disabled=!event.target.checked);
  bindPacking("packing-deliver",async()=>{if(!document.getElementById("packing-physical").checked||!deliverySupply)return;const item=deliverySupply;document.getElementById("packing-delivery-dialog").close();await runAction("supply_deliver",{supply_id:item.id});await refresh();});
})();
