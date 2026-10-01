"use strict";

(() => {
  const root = `${sellerApi}/marking`;
  const select = document.getElementById("chz-connection");
  let connection = select?.value, offset = 0, search = "", revision = 0;
  let refreshPromise = null, rerun = false;
  const states = { queued: "В очереди", running: "Выполняется", succeeded: "Завершена", failed: "Ошибка", interrupted: "Прервана" };
  const statuses = { draft: "Черновик", moderation: "На модерации", errors: "Требует изменений", notsigned: "Ожидает подписи", published: "Опубликована", archived: "В архиве" };
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
  const cardDialog = document.getElementById("chz-card");
  const openCard = (title, value, hint) => {
    document.getElementById("chz-card-title").textContent = title;
    document.getElementById("chz-card-status").textContent = hint;
    document.getElementById("chz-card-json").textContent = JSON.stringify(value, null, 2);
    if (!cardDialog.open) cardDialog.showModal();
  };
  act("chz-card-close", () => cardDialog.close());
  select.addEventListener("change", () => {
    connection = select.value; revision++; offset = 0; cardDialog.close();
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
    const elements = page.items.map(item => {
      const row = make("tr"), titleCell = make("td");
      const title = make("button", item.title, "text-button"), source = item.attributes.source || {};
      let hint = item.present ? "Карточка присутствует в последней выдаче" : "Карточка отсутствует в последней полной выдаче НК";
      if (!item.detail_available) hint += ". Детали недоступны; сохраненная копия может быть устаревшей";
      title.addEventListener("click", () => openCard(item.title, source, `${hint}. Обновление: ${item.updated_at ? new Date(item.updated_at).toLocaleString("ru-RU") : "нет"}`));
      titleCell.append(title, make("small", `НК #${item.external_id}`));
      if (!item.present || !item.detail_available) titleCell.append(make("small", hint));
      row.append(titleCell, make("td", (item.identifiers.gtin || []).join(", ") || "—"),
        make("td", (source.categories || []).map(c => c.cat_name || c.cat_id).join(", ") || "—"),
        make("td", (source.good_detailed_status || [source.good_status || "—"]).map(s => statuses[s] || s).join(", ")),
        make("td", `Эмиссия: ${flag(source.good_mark_flag)} · Оборот: ${flag(source.good_turn_flag)}`));
      return row;
    });
    if (!elements.length) { const row = make("tr"), cell = make("td", "Карточки не найдены. Запустите синхронизацию или измените поиск."); cell.colSpan = 5; row.append(cell); elements.push(row); }
    document.getElementById("chz-products").replaceChildren(...elements);
    document.getElementById("chz-count").textContent = `${page.total} карточек`;
    document.getElementById("chz-prev").disabled = offset === 0;
    document.getElementById("chz-next").disabled = offset + page.items.length >= page.total;
  }
  function renderOverview(overview) {
    const data = overview.snapshots, sync = data.sync?.value;
    const active = sync && ["queued", "running"].includes(sync.state);
    document.getElementById("chz-sync").disabled = !!active;
    document.getElementById("chz-force-sync").disabled = !!active;
    document.getElementById("chz-sync-status").textContent = sync
      ? `Синхронизация: ${states[sync.state] || sync.state} · ${sync.offset || 0}/${sync.total ?? "…"}${sync.error ? ` · ${sync.error}` : ""}`
      : "Каталог еще не обновлялся";
    document.getElementById("chz-last-sync").textContent = data.last_sync
      ? `Последний полный обход: ${new Date(data.last_sync.updated_at).toLocaleString("ru-RU")}` : "Полный обход еще не завершен";
    document.getElementById("chz-references-json").textContent = JSON.stringify({ account: data.account?.value, categories: data.categories?.value }, null, 2);
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
        const [page, overview, info] = await Promise.all([
          api(`${base}/products?offset=${offset}&search=${encodeURIComponent(search)}`),
          api(`${base}/overview`), api(`${sellerApi}/connections/${encodeURIComponent(active)}`),
        ]);
        if (version !== revision || active !== connection) return;
        renderProducts(page); renderOverview(overview);
        document.getElementById("chz-environment").textContent = info.environment === "production" ? "Промышленная среда" : "Тестовая среда";
      } catch (error) { if (version === revision) showError(error); }
      finally { refreshPromise = null; if (rerun) { rerun = false; queueMicrotask(refreshData); } }
    })();
    return refreshPromise;
  }
  async function poll() { if (!document.hidden) await refreshData(); window.setTimeout(poll, 4000); }
  poll();
})();
