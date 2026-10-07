"use strict";

(() => {
  const root = document.getElementById("commerce-root");
  if (!root) return;
  const adapter = root.dataset.adapter, label = adapter === "ozon" ? "Ozon" : "KIT";
  const endpoint = sellerApi + "/commerce";
  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const option = (text, value) => { const v = make("option", text); v.value = value; return v; };
  bindForm("commerce-connect", async fields => {
    const body = {adapter_key: adapter, name: fields.get("name"), token: fields.get("token"), read_only: fields.has("read_only")};
    if (adapter === "ozon") body.client_id = fields.get("client_id");
    else body.tin = fields.get("tin");
    await api(endpoint + "/connections", "POST", body);
    document.getElementById("commerce-connect").reset();
    window.location.reload();
  });
  const selector = document.getElementById("commerce-connection");
  if (!selector) return;
  const view = flowView(adapter);
  if ([...selector.options].some(v => v.value === view.connection)) selector.value = view.connection;
  let connection = selector.value, revision = 0, refreshSequence = 0;
  let kind = ["orders", "products", "warehouses", "supplies", "returns"].includes(view.kind) && (adapter === "ozon" || !["supplies", "returns"].includes(view.kind)) ? view.kind : "orders";
  let offset = Math.max(0, Number(view.offset) || 0), search = view.search || "", status = view.status || "", readOnly = false;
  let stage = view.stage || "", warehouse = view.warehouse || "";
  const filterForm = document.getElementById("commerce-search");
  filterForm.elements.search.value = search; filterForm.elements.stage.value = stage;
  let links = [], currentAction = null, codeOrder = null, codeItem = null;
  let product = null, nkOffset = 0, nkPage = [], codeOffset = 0, tin = "";
  let cancelOrder = null, bulkKind = null;
  const selectedCodes = new Set();
  const base = () => endpoint + "/" + encodeURIComponent(connection);
  const names = {codes: "Назначение КМ", ship: "Сборка отправления", cancel: "Отмена заказа", confirm: "Подтверждение заказа", complete: "Завершение собственной доставки", prices: "Изменение цены", stocks: "Изменение остатков"};
  const states = {draft: "Подготовлено", queued: "В очереди", submitting: "Отправляется", accepted: "Запрос принят", pending: "Ожидает проверки", confirmed: "Подтверждено чтением", rejected: "Отклонено", unknown: "Ответ потерян — нужна сверка", conflict: "Результат расходится", partial: "Часть изменений подтверждена", cancelled: "Подготовка отменена", awaiting_manual: "КМ нужно внести в кабинете KIT"};
  const orderStates = {awaiting_packaging: "На сборке", awaiting_deliver: "Готово к отгрузке", delivering: "В доставке", delivered: "Доставлено", cancelled: "Отменено",
    NEW: "Новый", PENDING_PAYMENT: "Ожидает оплаты", ORDER_PLACED: "Оформлен", WAIT_FOR_CONFIRMATION: "Нужно подтвердить", CREATING_INITIAL_RECEIPT: "Создается первый чек", SETUP_DELIVERY: "Настройка доставки", WAIT_FOR_DELIVERY: "Ожидает доставки", CANCELLATION_IN_PROGRESS: "Отмена в процессе", DELIVERY_CANCELLED: "Доставка отменена", FULL_REFUND: "Полный возврат", PARTIAL_REFUND: "Частичный возврат", CREATING_FINAL_RECEIPTS: "Создаются финальные чеки", DELIVERED: "Доставлен", CANCELLED: "Отменен", COMPLETED: "Завершен"};
  const paymentStates = {PAYMENT_PENDING_OR_UNPAID: "Ожидает оплаты", PAYMENT_PAID: "Оплачен", PAYMENT_REFUNDED: "Платеж возвращен", PAYMENT_FINALLY_PAID: "Полный расчет"};
  const number = (order) => order.attributes.source.order_number || order.external_id;
  function button(text, handler, write = false) {
    const node = make("button", text, "secondary");
    node.type = "button"; node.disabled = write && readOnly;
    node.addEventListener("click", async () => {
      const version = revision; node.disabled = true; feedback.hidden = true;
      try { await handler(); } catch (error) { if (version === revision) showError(error); }
      finally { node.disabled = write && readOnly; }
    });
    return node;
  }
  function closeDialogs() {
    document.querySelectorAll('dialog[id^="commerce-"]').forEach(v => v.close());
    currentAction = null;
  }
  document.querySelectorAll(".commerce-close").forEach(v => v.addEventListener("click", () => v.closest("dialog").close()));
  function renderAction(action) {
    actionContext(document.getElementById("commerce-action-dialog"), selector.selectedOptions[0].textContent, tin);
    currentAction = action;
    document.getElementById("commerce-action-title").textContent = names[action.kind];
    document.getElementById("commerce-action-status").textContent = (states[action.state] || action.state) + (action.error_code ? " · " + action.error_code : "");
    const body = action.body;
    document.getElementById("commerce-action-preview").textContent = action.kind === "codes"
      ? "Заказ " + (body.order_label || body.order_external_id) + " · GTIN " + body.gtin + " · Группа " + body.product_group + "\n" + body.cis.length + " кодов:\n" + body.cis.join("\n")
      : body.order_external_id ? "Заказ " + (body.order_label || body.order_external_id) + "\n" + (body.items || []).map(v => v.variant + " × " + v.quantity).join("\n")
      : JSON.stringify(body.wire, null, 2);
    document.getElementById("commerce-action-result").textContent = action.result.reason || "";
    document.getElementById("commerce-action-data").textContent = JSON.stringify({body, result: action.result, acknowledged: action.acknowledged}, null, 2);
    document.getElementById("commerce-action-events").replaceChildren(...(action.events || []).map(v => make("p", new Date(v.created_at).toLocaleString("ru-RU") + " · " + (states[v.state] || v.state), "chz-event")));
    const send = document.getElementById("commerce-action-send");
    send.hidden = action.state !== "draft" || readOnly;
    send.textContent = action.kind === "codes" && adapter === "kit" ? "Проверить ЧЗ и подготовить КМ для KIT" : action.kind === "cancel" ? "Отменить заказ в " + label : "Выполнить в " + label;
    document.getElementById("commerce-action-reconcile").hidden = ["draft", "queued", "submitting", "cancelled", "confirmed"].includes(action.state);
    document.getElementById("commerce-action-cancel").hidden = action.state !== "draft" && !(action.state === "rejected" && (action.result.before_send || action.result.definite_rejection) && !action.acknowledged);
    document.getElementById("commerce-copy-codes").hidden = action.kind !== "codes";
  }
  async function openAction(id) {
    const version = revision, result = await api(endpoint + "/actions/" + encodeURIComponent(id));
    if (version !== revision) return;
    renderAction(result); document.getElementById("commerce-action-dialog").showModal();
  }
  async function prepare(actionKind, payload) {
    const version = revision, value = await api(base() + "/actions", "POST", {kind: actionKind, payload});
    if (version !== revision) return;
    closeDialogs(); renderAction(value); document.getElementById("commerce-action-dialog").showModal();
    await refresh();
  }
  for (const [id, suffix] of [["commerce-action-send", "send"], ["commerce-action-reconcile", "reconcile"], ["commerce-action-cancel", "cancel"]]) {
    document.getElementById(id).addEventListener("click", async event => {
      const version = revision, action = currentAction, node = event.currentTarget;
      node.disabled = true;
      try {
        await api(endpoint + "/actions/" + action.id + "/" + suffix, "POST", {});
        const updated = await api(endpoint + "/actions/" + action.id);
        if (version === revision && currentAction?.id === action.id) renderAction(updated);
        if (version === revision) await refresh();
      } catch (error) { if (version === revision) showError(error); }
      finally { node.disabled = false; }
    });
  }
  document.getElementById("commerce-copy-codes").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(currentAction.body.sgtins.join("\n")); }
    catch { showError(new Error("Копирование недоступно. Полные КМ доступны в данных действия.")); }
  });
  function renderRecords(page) {
    const labels = {orders: ["Заказ", "Статус / оплата", "Состав", "Действия"], products: ["Товар", "Цена / остатки", "ЧЗ / действия"], warehouses: ["Склад", "ID", "Статус"], supplies: ["Отгрузка", "Статус", "Состав"], returns: ["Возврат", "Товар", "Состояние"]};
    const header = make("tr"); header.append(...labels[kind].map(v => make("th", v)));
    document.getElementById("commerce-head").replaceChildren(header);
    const scroll = {x: window.scrollX, y: window.scrollY};
    const rows = page.items.map(item => {
      const row = make("tr"), source = item.attributes?.source || {};
      if (kind === "orders") {
        const title = make("td", "#" + number(item)); title.append(make("small", item.external_id));
        const state = make("td", orderStates[item.status] || item.status);
        if (source.payment) state.append(make("small", paymentStates[source.payment.status] || source.payment.status || "Нет статуса оплаты"));
        const contents = make("td", (item.attributes.items || []).map(v => v.title + " × " + v.quantity).join("; "));
        const actions = make("td", undefined, "commerce-actions-cell");
        actions.append(button("Открыть заказ", () => openOrder(item)));
        if (adapter === "ozon" && item.status === "awaiting_packaging") actions.append(button("Подготовить сборку", () => prepare("ship", {order_id: item.id}), true));
        if (adapter === "kit" && item.status === "WAIT_FOR_CONFIRMATION") actions.append(button("Подтвердить заказ", () => prepare("confirm", {order_id: item.id}), true));
        if (adapter === "kit" && item.status === "WAIT_FOR_DELIVERY") actions.append(button("Завершить свою доставку", () => prepare("complete", {order_id: item.id}), true));
        if (!["CANCELLED", "COMPLETED", "DELIVERED", "FULL_REFUND", "PARTIAL_REFUND", "cancelled", "delivered"].includes(item.status)) actions.append(button("Отменить заказ…", () => openCancel(item), true));
        row.append(title, state, contents, actions);
      } else if (kind === "products") {
        const title = make("td", item.title); title.append(make("small", (item.sku || "") + " · " + item.external_id));
        const summary = make("td", adapter === "kit" ? (source.pricing?.final_price || source.pricing?.price || "—") + " ₽" : (source.price || "—") + " ₽");
        summary.append(make("small", adapter === "kit" ? (source.stocks || []).map(v => v.quantity + " / резерв " + v.reserved).join("; ") : "SKU " + item.attributes.variant));
        const actions = make("td", undefined, "commerce-actions-cell");
        actions.append(button("Связать с ЧЗ", () => openLink(item)));
        if (adapter === "kit") actions.append(button("Изменить цену", () => openBulk(item, "prices"), true), button("Изменить остаток", () => openBulk(item, "stocks"), true));
        row.append(title, summary, actions);
      } else if (kind === "warehouses") row.append(make("td", item.name), make("td", item.external_id), make("td", source.status || "—"));
      else if (kind === "supplies") row.append(make("td", item.external_id), make("td", item.status), make("td", String(source.postings_count || 0) + " отправлений"));
      else row.append(make("td", item.id + " · " + (item.posting_number || "")), make("td", (item.product?.name || "") + " × " + (item.product?.quantity || 0)), make("td", item.visual?.status?.display_name || item.return_reason_name || "—"));
      return row;
    });
    if (!rows.length) { const row = make("tr"), cell = make("td", "Данных пока нет. Обновите интеграцию или измените поиск."); cell.colSpan = labels[kind].length; row.append(cell); rows.push(row); }
    document.getElementById("commerce-records").replaceChildren(...rows);
    window.scrollTo(scroll.x, scroll.y);
    document.getElementById("commerce-count").textContent = page.total ? String(offset + 1) + "–" + String(offset + page.items.length) + " из " + page.total : "0 записей";
    document.getElementById("commerce-prev").disabled = offset === 0;
    document.getElementById("commerce-next").disabled = offset + page.items.length >= page.total;
  }
  async function openOrder(order) {
    const version = revision, detail = await api(base() + "/orders/" + order.id + "/details");
    if (version !== revision) return;
    order = {...order, attributes: {...order.attributes, source: detail.source}};
    const contents = [];
    if (adapter === "kit") {
      contents.push(make("p", "Оплата: " + (paymentStates[detail.source.payment?.status] || detail.source.payment?.status || "Нет данных") + " · Сумма: " + (detail.source.total_final_price || "—") + " ₽"));
      for (const chunk of detail.source.delivery_chunks || []) {
        const info = chunk.delivery_info || {};
        contents.push(make("p", "Грузоместо " + chunk.id + " · " + (info.method || "") + " · " + (info.courier_delivery_service_type || info.pickup_point_delivery_service_type || "") + " · " + (info.human_status || info.raw_status || "") + " · Трек " + (info.tracking_number || "—")));
      }
    }
    for (const item of detail.items) {
      const node = make("article"), link = links.find(v => v.variant === item.variant);
      node.append(make("strong", (link?.product_title || item.title) + " × " + item.quantity));
      if (item.refused_count) node.append(make("p", "Отказ: " + item.refused_count + " единиц. Возврат оформляется в KIT."));
      const assigned = (order.marking || []).filter(v => v.item_id === item.item_id);
      if (assigned.length) assigned.forEach(v => node.append(button(v.code + " · " + (states[v.state] || v.state), () => openAction(v.action_id))));
      else {
        const b = button("Назначить КМ", () => openCodes(order, item), true);
        b.disabled = readOnly || !link; node.append(b);
        if (!link) node.append(make("small", "Сначала свяжите товар с ЧЗ во вкладке «Товары»."));
      }
      if (item.truthful_label) node.append(make("small", "КМ в KIT: " + item.truthful_label));
      contents.push(node);
    }
    document.getElementById("commerce-order-title").textContent = "Заказ #" + number(order);
    document.getElementById("commerce-order-details").replaceChildren(...contents);
    document.getElementById("commerce-order-source").textContent = JSON.stringify(detail.source, null, 2);
    document.getElementById("commerce-order-dialog").showModal();
  }
  async function openCancel(order) {
    const version = revision;
    cancelOrder = order;
    if (adapter === "ozon") {
      const values = await api(base() + "/cancel-reasons");
      if (version !== revision) return;
      document.getElementById("commerce-cancel-form").elements.reason.replaceChildren(...values.filter(v => v.is_available_for_cancellation).map(v => option(v.title, v.id)));
    }
    document.getElementById("commerce-cancel-title").textContent = "Заказ #" + number(order);
    document.getElementById("commerce-cancel-dialog").showModal();
  }
  bindForm("commerce-cancel-form", fields => {
    const payload = {order_id: cancelOrder.id};
    if (adapter === "ozon") { payload.cancel_reason_id = Number(fields.get("reason")); payload.cancel_reason_message = fields.get("message"); }
    return prepare("cancel", payload);
  });
  async function openLink(item) {
    const version = revision, values = await api(sellerApi + "/connections");
    if (version !== revision) return;
    product = item; nkOffset = 0;
    document.getElementById("commerce-link-title").textContent = item.title;
    const form = document.getElementById("commerce-link-form");
    form.elements.chz_connection_id.replaceChildren(...values.filter(v => v.adapter_key === "chz" && v.external_account_id === "production:" + tin).map(v => option(v.name, v.id)));
    if (!form.elements.chz_connection_id.value) throw new Error("Подключите производственный аккаунт ЧЗ с тем же ИНН и обновите НК.");
    await loadNk(version);
    if (version === revision) document.getElementById("commerce-link-dialog").showModal();
  }
  async function loadNk(version = revision) {
    const form = document.getElementById("commerce-link-form"), chz = form.elements.chz_connection_id.value;
    const [page, account] = await Promise.all([api(sellerApi + "/marking/" + chz + "/products?offset=" + nkOffset + "&search=" + encodeURIComponent(document.getElementById("commerce-nk-search").value)), api(sellerApi + "/marking/" + chz + "/overview")]);
    if (version !== revision || chz !== form.elements.chz_connection_id.value) return;
    nkPage = page.items.filter(v => v.id && v.present && v.detail_available);
    form.elements.chz_product_id.replaceChildren(...nkPage.map(v => option(v.title, v.id)));
    form.elements.product_group.replaceChildren(...(account.snapshots.account?.value.productGroups || []).map(v => option(v, v)));
    document.getElementById("commerce-nk-count").textContent = page.total + " карточек";
    document.getElementById("commerce-nk-prev").disabled = nkOffset === 0;
    document.getElementById("commerce-nk-next").disabled = nkOffset + page.items.length >= page.total;
    setGtin();
  }
  function setGtin() {
    const form = document.getElementById("commerce-link-form"), item = nkPage.find(v => v.id === form.elements.chz_product_id.value);
    form.elements.gtin.replaceChildren(...(item?.identifiers.gtin || []).map(v => option(v, v)));
  }
  document.getElementById("commerce-link-form").elements.chz_product_id.addEventListener("change", setGtin);
  document.getElementById("commerce-link-form").elements.chz_connection_id.addEventListener("change", () => { nkOffset = 0; loadNk().catch(showError); });
  for (const [id, delta] of [["commerce-nk-find", 0], ["commerce-nk-prev", -100], ["commerce-nk-next", 100]]) document.getElementById(id).addEventListener("click", () => { nkOffset = delta ? Math.max(0, nkOffset + delta) : 0; loadNk().catch(showError); });
  bindForm("commerce-link-form", async fields => {
    const version = revision;
    await api(base() + "/links", "POST", {product_id: product.id, chz_connection_id: fields.get("chz_connection_id"), chz_product_id: fields.get("chz_product_id"), gtin: fields.get("gtin"), product_group: fields.get("product_group")});
    if (version === revision) { document.getElementById("commerce-link-dialog").close(); await refresh(); }
  });
  async function openCodes(order, item) {
    codeOrder = order; codeItem = item; codeOffset = 0; selectedCodes.clear();
    document.getElementById("commerce-codes-title").textContent = "Заказ #" + number(order) + " · " + item.quantity + " КМ";
    const version = revision; await loadCodes();
    if (version === revision) document.getElementById("commerce-codes-dialog").showModal();
  }
  async function loadCodes() {
    const version = revision, order = codeOrder, item = codeItem;
    const page = await api(base() + "/orders/" + order.id + "/items/" + encodeURIComponent(item.item_id) + "/available-codes?offset=" + codeOffset);
    if (version !== revision || codeOrder?.id !== order.id || codeItem?.item_id !== item.item_id) return;
    const rows = page.items.map(v => {
      const row = make("tr"), cell = make("td"), check = make("input");
      check.type = "checkbox"; check.checked = selectedCodes.has(v.id);
      check.setAttribute("aria-label", "Выбрать код " + v.code);
      check.addEventListener("change", () => { if (check.checked) selectedCodes.add(v.id); else selectedCodes.delete(v.id); });
      cell.append(check); row.append(cell, make("td", v.code, "chz-code"), make("td", v.external_status || "Еще не проверен")); return row;
    });
    if (!rows.length) { const row = make("tr"), cell = make("td", "Нет свободных полных КМ этого GTIN и группы."); cell.colSpan = 3; row.append(cell); rows.push(row); }
    document.getElementById("commerce-codes-list").replaceChildren(...rows);
    document.getElementById("commerce-codes-count").textContent = page.total + " свободных · Нужно " + page.required;
    document.getElementById("commerce-codes-prev").disabled = codeOffset === 0;
    document.getElementById("commerce-codes-next").disabled = codeOffset + page.items.length >= page.total;
  }
  for (const [id, delta] of [["commerce-codes-prev", -100], ["commerce-codes-next", 100]]) document.getElementById(id).addEventListener("click", () => { codeOffset = Math.max(0, codeOffset + delta); loadCodes().catch(showError); });
  bindForm("commerce-codes-form", () => prepare("codes", {order_id: codeOrder.id, item_id: codeItem.item_id, code_ids: [...selectedCodes]}));
  async function openBulk(item, actionKind) {
    product = item; bulkKind = actionKind;
    const form = document.getElementById("commerce-bulk-form"), version = revision;
    document.getElementById("commerce-bulk-title").textContent = names[actionKind];
    document.getElementById("commerce-bulk-product").textContent = item.title;
    for (const id of ["commerce-price-field", "commerce-discount-field"]) document.getElementById(id).hidden = actionKind !== "prices";
    for (const id of ["commerce-warehouse-field", "commerce-quantity-field"]) document.getElementById(id).hidden = actionKind !== "stocks";
    form.elements.price.required = actionKind === "prices"; form.elements.quantity.required = actionKind === "stocks";
    form.elements.price.value = item.attributes.source.pricing?.price || "";
    form.elements.manual_discount_price.value = item.attributes.source.pricing?.manual_discount_price || "";
    if (actionKind === "stocks") {
      const page = await api(base() + "/records/warehouses?limit=200");
      if (version !== revision) return;
      form.elements.warehouse_id.replaceChildren(...page.items.filter(v => v.attributes.source.status !== "ARCHIVED").map(v => option(v.name, v.id)));
      form.elements.quantity.value = "";
    }
    document.getElementById("commerce-bulk-dialog").showModal();
  }
  bindForm("commerce-bulk-form", fields => prepare(bulkKind, {items: [bulkKind === "prices"
    ? {product_id: product.id, price: fields.get("price"), manual_discount_price: fields.get("manual_discount_price") || null}
    : {product_id: product.id, warehouse_id: fields.get("warehouse_id"), quantity: Number(fields.get("quantity"))}]}));
  bindForm("commerce-token", async fields => {
    const version = revision, path = base();
    await api(path + "/credentials", "PUT", {token: fields.get("token")});
    document.getElementById("commerce-token").reset();
    if (version === revision) await refresh();
  });
  document.getElementById("commerce-sync").addEventListener("click", async event => {
    const button = event.currentTarget; button.disabled = true;
    try { await api(base() + "/sync", "POST", {}); await refresh(); } catch (error) { showError(error); button.disabled = false; }
  });
  for (const node of document.querySelectorAll("#commerce-tabs [data-kind]")) node.addEventListener("click", () => {
    kind = node.dataset.kind; offset = 0; stage = ""; status = ""; warehouse = ""; filterForm.elements.stage.value = ""; filterForm.elements.status.value = ""; filterForm.elements.warehouse.value = ""; revision++;
    document.querySelectorAll("#commerce-tabs button").forEach(v => v.classList.toggle("secondary", v !== node));
    refresh().catch(showError);
  });
  bindForm("commerce-search", fields => { search = fields.get("search"); status = fields.get("status"); stage = fields.get("stage"); warehouse = fields.get("warehouse"); if (stage) kind = "orders"; if (!["orders", "supplies"].includes(kind)) { stage = ""; status = ""; warehouse = ""; } offset = 0; revision++; document.querySelectorAll("#commerce-tabs button").forEach(v => v.classList.toggle("secondary", v.dataset.kind !== kind)); return refresh(); });
  for (const [id, delta] of [["commerce-prev", -100], ["commerce-next", 100]]) document.getElementById(id).addEventListener("click", () => { offset = Math.max(0, offset + delta); revision++; refresh().catch(showError); });
  selector.addEventListener("change", () => { connection = selector.value; revision++; offset = 0; warehouse = ""; loadWarehouseFilter().catch(showError); selectedCodes.clear(); links = []; closeDialogs(); refresh().catch(showError); });
  async function refresh() {
    const version = revision, sequence = ++refreshSequence, path = base();
    const [overview, records, linked, actions] = await Promise.all([api(path + "/overview"), api(path + "/records/" + kind + "?offset=" + offset + "&search=" + encodeURIComponent(search) + "&status=" + encodeURIComponent(status) + "&stage=" + encodeURIComponent(stage) + "&warehouse=" + encodeURIComponent(warehouse)), api(path + "/links"), api(path + "/actions")]);
    if (version !== revision || sequence !== refreshSequence) return;
    readOnly = overview.parameters.read_only; tin = overview.parameters.tin; links = linked;
    const sync = overview.snapshots.sync?.value, last = overview.snapshots.last_sync;
    document.getElementById("commerce-sync-status").textContent = sync ? ({queued: "В очереди", running: "Обновляется", succeeded: "Обновлено", failed: "Ошибка чтения", interrupted: "Чтение прервано"}[sync.state] || sync.state) + " · " + (sync.coverage === "complete" ? "Полное чтение" : "Данные прочитаны частично") : "Еще не обновлялось";
    document.getElementById("commerce-summary").textContent = "Обновлено: " + (last ? new Date(last.updated_at).toLocaleString("ru-RU") : "еще не обновлялось") + (readOnly ? " · Только чтение" : "");
    document.getElementById("commerce-links").replaceChildren(...(links.length ? links.map(v => {
      const row = make("article", undefined, "connection");
      row.append(make("strong", v.product_title + " → " + v.chz_title), make("p", "GTIN " + v.gtin + " · " + v.product_group));
      row.append(button("Удалить связь", async () => { await api(base() + "/links/" + v.id, "DELETE", {}); await refresh(); }));
      return row;
    }) : [make("p", "Связей пока нет.", "muted")]));
    document.getElementById("commerce-actions").replaceChildren(...(actions.length ? actions.map(v => {
      const row = make("article", undefined, "operation");
      row.append(make("strong", names[v.kind] + " · " + (states[v.state] || v.state)), make("p", (v.body.order_label || v.body.order_external_id || "") + " · " + new Date(v.created_at).toLocaleString("ru-RU")), button("Открыть действие", () => openAction(v.id)));
      return row;
    }) : [make("p", "Действий пока нет.", "muted")]));
    renderRecords(records);
    saveFlowView(adapter, {connection,kind,offset,search,status,stage,warehouse});
    document.getElementById("commerce-sync").disabled = ["queued", "running"].includes(sync?.state);
    if (currentAction && document.getElementById("commerce-action-dialog").open) {
      const action = await api(endpoint + "/actions/" + currentAction.id);
      if (version === revision && sequence === refreshSequence && currentAction?.id === action.id) renderAction(action);
    }
  }
  async function poll() {
    try { await refresh(); } catch (error) { showError(error); }
    window.setTimeout(poll, refreshSeconds * 1000);
  }
  async function loadWarehouseFilter() {
    const active = connection, page = await api(base() + "/records/warehouses?limit=200"); if (active !== connection) return;
    const field = filterForm.elements.warehouse, prompt = option("Все склады", "");
    field.replaceChildren(prompt, ...page.items.map(v => option(v.name, v.external_id)));
    if (warehouse && ![...field.options].some(v => v.value === warehouse)) field.append(option("Склад " + warehouse, warehouse));
    field.value = warehouse;
  }
  const allowedStates = adapter === "ozon" ? ["awaiting_packaging", "awaiting_deliver", "delivering", "delivered", "cancelled"] : Object.keys(orderStates).filter(v => v === v.toUpperCase());
  filterForm.elements.status.replaceChildren(option("Все статусы", ""), ...allowedStates.map(v => option(orderStates[v], v)));
  filterForm.elements.status.value = status;
  document.querySelectorAll("#commerce-tabs button").forEach(v => v.classList.toggle("secondary", v.dataset.kind !== kind));
  if (pageQuery.get("history") === "1") document.getElementById("commerce-history").open = true;
  loadWarehouseFilter().catch(showError);
  poll();
})();
