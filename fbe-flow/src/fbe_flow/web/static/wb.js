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
    const active = connection, page = await api(base() + "/records/warehouses?limit=200");
    if (active !== connection) return;
    warehouseNames = new Map(page.items.map(v => [v.external_id, v.name]));
    const field = filterForm.elements.warehouse, prompt = make("option", "Все склады"); prompt.value = "";
    field.replaceChildren(prompt, ...page.items.map(v => { const option = make("option", v.name); option.value = v.external_id; return option; }));
    if (warehouse && ![...field.options].some(v => v.value === warehouse)) { const option = make("option", "Склад " + warehouse); option.value = warehouse; field.append(option); }
    field.value = warehouse;
  }
  let links = [], parameters = {}, currentAction = null, product = null, currentOrder = null;
  let nkOffset = 0, nkPage = [], codeOffset = 0, refreshSequence = 0;
  const selected = new Map(), selectedCodes = new Set();
  const base = () => `${root}/${encodeURIComponent(connection)}`;
  const actionNames = { sgtin: "Передача кодов ЧЗ", supply_create: "Создание поставки", supply_add: "Состав поставки", supply_deliver: "Передача в доставку", supply_delete: "Удаление пустой поставки" };
  const actionStates = { draft: "Подготовлено", queued: "В очереди", submitting: "Отправляется", accepted: "WB принял запрос", pending: "Ожидает подтверждения WB", confirmed: "Подтверждено WB", rejected: "Отклонено", unknown: "Ответ потерян — требуется сверка", conflict: "Результат расходится — требуется проверка", partial: "Часть заданий добавлена", cancelled: "Отменено" };
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
    document.getElementById("wb-selection").textContent = `${selected.size} заданий выбрано`;
    document.getElementById("wb-order-tools").hidden = kind !== "orders" || !selected.size;
    document.getElementById("wb-add-orders").disabled = !selected.size || !!parameters.read_only;
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
        const cell = make("td"), checkbox = make("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(item.id); checkbox.setAttribute("aria-label", `Выбрать задание ${item.external_id}`);
        checkbox.disabled = !["new", "confirm"].includes(item.status) || source.deliveryType !== "fbs";
        if (checkbox.disabled) { selected.delete(item.id); checkbox.checked = false; } else if (selected.has(item.id)) selected.set(item.id, item);
        checkbox.addEventListener("change", () => { if (checkbox.checked) selected.set(item.id, item); else selected.delete(item.id); selectionCount(); }); cell.append(checkbox);
        const title = make("td"); title.append(clickButton(`#${item.external_id}`, () => orderDetails(item)), make("small", source.article || "Без артикула"));
        const status = make("td", orderStates[item.status] || item.status); status.append(make("small", `WB: ${item.attributes.wb_status || "—"}`));
        const sourceCell = make("td", item.supply_external_id || "Без поставки"); sourceCell.append(make("small", warehouseNames.get(String(item.warehouse_external_id)) || "Склад " + (item.warehouse_external_id || "—")));
        const marking = make("td", undefined, "wb-marking-cell");
        if (item.marking.length) for (const value of item.marking) marking.append(clickButton(value.code, () => openAction(value.action_id)));
        else {
          const assign = clickButton("Назначить коды", () => openCodes(item));
          assign.disabled = item.status !== "confirm" || !!parameters.read_only;
          marking.append(assign);
          if (item.status === "new") marking.append(make("small", "Сначала добавьте задание в поставку"));
        }
        row.append(cell, title, status, sourceCell, marking);
      } else {
        const title = make("td", source.name || item.external_id); title.append(make("small", item.external_id));
        const status = make("td", supplyStates[item.status] || item.status);
        const actions = make("td");
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
          actions.append(clickButton("Передать в доставку", () => prepare("supply_deliver", { supply_id: item.id })));
          actions.append(clickButton("Удалить пустую", () => prepare("supply_delete", { supply_id: item.id })));
        }
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
    if (![...select.options].some(v => v.value === item.id)) { const option = make("option", `${item.attributes.source.name || item.external_id} · ${item.external_id}`); option.value = item.id; select.append(option); }
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
    parameters = overview.parameters; links = newLinks;
    document.querySelector('#wb-create-supply [type="submit"]').disabled = !!parameters.read_only;
    const sync = overview.snapshots.sync?.value;
    document.getElementById("wb-sync").disabled = ["queued", "running"].includes(sync?.state);
    document.getElementById("wb-sync-status").textContent = sync ? `Синхронизация: ${states[sync.state] || sync.state} · ${syncPhases[sync.phase] || ""}${sync.error ? ` · ${sync.error}` : ""}` : "Ещё не синхронизировано";
    document.getElementById("wb-summary").textContent = `Обновлено: ${overview.snapshots.last_sync ? new Date(overview.snapshots.last_sync.updated_at).toLocaleString("ru-RU") : "еще не обновлялось"}${parameters.read_only ? " · Только чтение" : ""}`;
    renderRecords(page);
    saveFlowView("wb", {connection,kind,offset,search,stage,status,warehouse});
    const list = document.getElementById("wb-links");
    list.replaceChildren(...links.map(link => { const row = make("article", `${link.product_title} · chrtID ${link.chrt_id} → ${link.chz_title} · GTIN ${link.gtin} · ${link.product_group}`, "connection"); row.append(clickButton("Удалить связь", async () => { await api(`${endpoint}/links/${link.id}`, "DELETE", {}); if (version === revision) await refresh(); })); return row; }));
    if (!links.length) list.append(make("p", "Откройте «Товары» и свяжите нужный размер с карточкой НК.", "muted"));
    document.getElementById("wb-actions").replaceChildren(...actions.map(action => { const row = make("article", undefined, "connection"); row.append(clickButton(`${actionNames[action.kind]} · ${actionStates[action.state] || action.state}`, () => openAction(action.id))); row.append(make("small", new Date(action.created_at).toLocaleString("ru-RU"))); return row; }));
    const select = document.getElementById("wb-supply-select"), previous = select.value, old = [...select.options].find(v => v.value === previous);
    const placeholder = make("option", "Выберите поставку (другие — в разделе «Поставки»)"); placeholder.value = "";
    select.replaceChildren(placeholder, ...supplies.items.filter(v => v.status === "open").map(v => { const option = make("option", `${v.attributes.source.name || v.external_id} · ${v.external_id}`); option.value = v.id; return option; }));
    if (old && ![...select.options].some(v => v.value === previous)) select.append(old);
    select.value = previous;
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
  bindForm("wb-create-supply", fields => prepare("supply_create", { name: fields.get("name") }));
  document.getElementById("wb-add-orders").addEventListener("click", async () => { try { await prepare("supply_add", { supply_id: document.getElementById("wb-supply-select").value, order_ids: [...selected.keys()] }); selected.clear(); selectionCount(); } catch (error) { showError(error); } });

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
  selector.addEventListener("change", () => { connection = selector.value; revision++; selected.clear(); selectedCodes.clear(); closeDialogs(); offset = 0; links = []; refresh().catch(showError); });
  async function poll() { try { await refresh(); } catch (error) { showError(error); } window.setTimeout(poll, refreshSeconds * 1000); }
  document.querySelectorAll("#wb-tabs button").forEach(v => v.classList.toggle("secondary", v.dataset.kind !== kind));
  if (pageQuery.get("history") === "1") document.getElementById("wb-history").open = true;
  loadWarehouseFilter().catch(showError);
  poll();
})();
