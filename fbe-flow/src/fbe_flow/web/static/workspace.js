"use strict";
(() => {
  const root = document.getElementById("workspace-widgets"); if (!root || !sellerId) return;
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = String(text); if (cls) n.className = cls; return n; };
  const button = (text, fn) => { const b = node("button", text, "secondary"); b.type = "button"; b.addEventListener("click", fn); return b; };
  const dialog = document.getElementById("workspace-dialog");
  const names = {all: "Все каналы", wb: "WB", ozon: "Ozon", kit: "Магазин", chz: "Честный Знак"};
  const freshness = {never: "Еще не обновлялось", updating: "Обновляется · данные могут быть неполными", error: "Обновление завершилось ошибкой", complete: "Данные обновлены", stale: "Данные могут быть устаревшими"};
  let data, draft = [], refreshing = false;
  const select = (values, current, label, changed) => {
    const s = node("select"); s.setAttribute("aria-label", label);
    s.replaceChildren(...values.map(v => { const o = node("option", v[1]); o.value = v[0]; return o; }));
    s.value = current; s.addEventListener("change", () => changed(s.value)); return s;
  };
  function render() {
    const openSources = new Set([...root.querySelectorAll("details[open]")].map(item => item.dataset.sourceKey));
    const active = document.activeElement, focusedSource = active?.closest("[data-source-key]")?.dataset.sourceKey;
    const widgets = data.widgets.map(widget => {
      const card = node("section", undefined, "panel workspace-widget"); card.dataset.widget = widget.id;
      card.append(node("h2", widget.title));
      if (widget.message) card.append(node("p", widget.message, "inline-error"));
      for (const [index, row] of widget.rows.entries()) {
        const article = node("article", undefined, "widget-row");
        const link = node("a", row.name, "widget-link"); link.href = row.href;
        if (row.count !== undefined) link.append(node("strong", row.count === null ? "—" : row.count, "widget-count"));
        article.append(link);
        if (row.hint && row.hint !== "Заказы") article.append(node("small", row.hint, "muted"));
        const source = node("details", undefined, "widget-source");
        source.dataset.sourceKey = widget.id + ":" + index; source.open = openSources.has(source.dataset.sourceKey);
        const timestamp = row.updated_at ? new Date(row.updated_at).toLocaleString("ru-RU", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}) : "";
        const summary = node("summary", row.freshness === "complete" ? "Обновлено " + timestamp : row.freshness ? freshness[row.freshness] : "Подробнее");
        if (["error", "stale", "never"].includes(row.freshness)) summary.className = "widget-warning";
        source.append(summary);
        if (row.freshness) {
          if (timestamp && row.freshness !== "complete") source.append(node("small", "Последние данные: " + timestamp));
          const controls = node("div", undefined, "actions");
          const refresh = button("Обновить " + names[row.channel], async event => {
            const b = event.currentTarget; b.disabled = true;
            try {
              const endpoint = sellerApi + (row.channel === "wb" ? "/wb/" : "/commerce/") + encodeURIComponent(row.connection_id);
              await api(endpoint + "/sync", "POST", {}); showSuccess("Обновление " + names[row.channel] + " запущено"); await load();
            } catch (error) { showError(error); } finally { if (b.isConnected) b.disabled = false; }
          });
          refresh.disabled = row.freshness === "updating"; controls.append(refresh); source.append(controls);
        }
        if (row.examples?.length) {
          const list = node("ul", undefined, "widget-examples");
          for (const example of row.examples) { const li = node("li", example.number); if (example.deadline) li.append(node("small", " · " + example.deadline)); list.append(li); }
          source.append(list);
        }
        if (row.freshness || row.examples?.length) article.append(source);
        card.append(article);
      }
      if (!widget.rows.length && !widget.message) card.append(node("p", widget.kind === "attention" ? "По сохраненным данным все спокойно." : widget.kind === "documents" ? "Нет истекающих документов." : widget.kind === "codes" ? "Кодов пока нет." : "Подключите канал в настройках.", "muted"));
      return card;
    });
    if (!widgets.length) widgets.push(node("p", "Добавьте нужные виджеты кнопкой «Виджеты».", "muted"));
    root.replaceChildren(...widgets);
    if (focusedSource) [...root.querySelectorAll("[data-source-key]")].find(item => item.dataset.sourceKey === focusedSource)?.querySelector(active.tagName === "BUTTON" ? "button" : "summary")?.focus({preventScroll: true});
    document.getElementById("workspace-updated").textContent = "На " + new Date(data.generated_at).toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
  }
  async function load() {
    if (refreshing) return; refreshing = true;
    try { data = await api(sellerApi + "/workspace"); render(); }
    catch (error) { showError(error); }
    finally { refreshing = false; }
  }
  function renderDraft() {
    const rows = draft.map((widget, index) => {
      const row = node("section", undefined, "widget-editor"); row.append(node("h3", data.available_widgets[widget.kind]));
      const channels = Object.entries(names).filter(([key]) => widget.kind === "codes" ? ["all", "chz"].includes(key) : !["assembly", "shipping"].includes(widget.kind) || key !== "chz");
      if (widget.kind !== "documents") row.append(select(channels, widget.channel, "Канал виджета", value => { widget.channel = value; widget.warehouse_id = ""; widget.status = ""; renderDraft(); }));
      if (["assembly", "shipping"].includes(widget.kind)) {
        const warehouses = data.warehouses.filter(w => widget.channel === "all" || widget.channel === w.adapter_key);
        row.append(select([["", "Все склады"], ...warehouses.map(w => [w.id, w.name + " · " + names[w.adapter_key]])], widget.warehouse_id, "Склад виджета", value => { widget.warehouse_id = value; widget.status = ""; renderDraft(); }));
        const statusChannel = widget.channel === "all" ? warehouses.find(w => w.id === widget.warehouse_id)?.adapter_key : widget.channel;
        const states = Object.entries(data.stage_statuses[widget.kind]).filter(([key]) => key === statusChannel).flatMap(([key, values]) => values.map(v => [v.value, v.label + (widget.channel === "all" ? " · " + names[key] : "")]));
        if (widget.status && !states.some(v => v[0] === widget.status)) states.push([widget.status, widget.status]);
        const statusSelect = select([["", "Все статусы выбранного этапа"], ...states], widget.status, "Статус виджета", value => { widget.status = value; });
        statusSelect.disabled = !statusChannel; row.append(statusSelect);
      }
      const actions = node("div", undefined, "actions");
      const up = button("Выше", () => { [draft[index - 1], draft[index]] = [draft[index], draft[index - 1]]; renderDraft(); }); up.disabled = index === 0;
      const down = button("Ниже", () => { [draft[index], draft[index + 1]] = [draft[index + 1], draft[index]]; renderDraft(); }); down.disabled = index === draft.length - 1;
      actions.append(up, down, button("Скрыть", () => { draft.splice(index, 1); renderDraft(); })); row.append(actions); return row;
    });
    document.getElementById("workspace-layout").replaceChildren(...rows);
    document.getElementById("workspace-add").disabled = draft.length >= 12;
  }
  document.getElementById("workspace-configure").addEventListener("click", async () => {
    if (!data) await load(); if (!data) return;
    draft = data.layout.widgets.map(v => ({...v})); renderDraft(); dialog.showModal();
  });
  document.getElementById("workspace-close").addEventListener("click", () => dialog.close());
  document.getElementById("workspace-add").addEventListener("click", () => {
    if (draft.length >= 12) return;
    draft.push({id: "widget-" + crypto.randomUUID(), kind: document.getElementById("workspace-new-kind").value, channel: "all", warehouse_id: "", status: ""}); renderDraft();
  });
  document.getElementById("workspace-defaults").addEventListener("click", () => {
    draft = ["assembly", "shipping", "attention"].map(kind => ({id: kind, kind, channel: "all", warehouse_id: "", status: ""})); renderDraft();
  });
  bindForm("workspace-layout-form", async () => {
    await api(sellerApi + "/settings/workspace.layout", "PUT", {value: {widgets: draft}});
    dialog.close(); await load(); showSuccess("Рабочее пространство сохранено");
  });
  document.getElementById("workspace-refresh").addEventListener("click", load);
  async function poll() { if (!document.hidden) await load(); window.setTimeout(poll, refreshSeconds * 1000); }
  poll();
})();
