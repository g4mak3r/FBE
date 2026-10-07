"use strict";

const sellerId = document.body.dataset.sellerId;
const sellerApi = "/api/sellers/" + encodeURIComponent(sellerId);
const feedback = document.getElementById("feedback");
const refreshSeconds = Math.max(5, Number(document.body.dataset.refreshSeconds) || 15);
const pageQuery = new URLSearchParams(location.search);

function showError(error) {
  feedback.classList.remove("catalog-success");
  feedback.textContent = error.message || "Не удалось выполнить действие";
  feedback.hidden = false;
  const dialog = document.querySelector("dialog[open]");
  if (dialog && !dialog.querySelector(".catalog-dialog-error")) {
    let local = dialog.querySelector(".inline-error");
    if (!local) { local = document.createElement("p"); local.className = "inline-error"; local.setAttribute("role", "alert"); dialog.prepend(local); }
    local.textContent = feedback.textContent;
  }
}
function showSuccess(message) {
  feedback.textContent = message; feedback.classList.add("catalog-success"); feedback.hidden = false;
}
async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method, headers: {"Content-Type": "application/json", "X-FBE-Flow": "1"},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data;
  try { data = await response.json(); } catch { throw new Error("Не удалось прочитать ответ. Повторите запрос."); }
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Проверьте введенные данные");
  return data;
}
function bindForm(id, handler) {
  const form = typeof id === "string" ? document.getElementById(id) : id;
  if (!form) return;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (form.dataset.busy === "1") return;
    const button = form.querySelector('[type="submit"]');
    form.dataset.busy = "1"; button.disabled = true; feedback.hidden = true;
    form.closest("dialog")?.querySelector(".inline-error")?.remove();
    try { await handler(new FormData(form)); } catch (error) { showError(error); }
    finally { form.dataset.busy = ""; button.disabled = false; }
  });
}
function flowView(channel) {
  let stored = {};
  try { stored = JSON.parse(sessionStorage.getItem("fbe:view:" + sellerId + ":" + channel) || "{}"); } catch {}
  const result = {...stored};
  for (const key of ["connection", "kind", "stage", "status", "warehouse", "search", "offset"]) {
    if (pageQuery.has(key)) result[key] = pageQuery.get(key);
  }
  return result;
}
function saveFlowView(channel, value) {
  try { sessionStorage.setItem("fbe:view:" + sellerId + ":" + channel, JSON.stringify(value)); } catch {}
}
function actionContext(element, connection, tin) {
  let context = element.querySelector(".action-context");
  if (!context) { context = document.createElement("p"); context.className = "action-context"; element.querySelector("h2").after(context); }
  context.textContent = document.body.dataset.sellerName + " · " + connection + (tin ? " · ИНН " + tin : "");
}

const sellerDialog = document.getElementById("seller-dialog");
for (const id of ["add-seller", "first-seller"]) document.getElementById(id)?.addEventListener("click", () => sellerDialog.showModal());
document.getElementById("cancel-seller")?.addEventListener("click", () => sellerDialog.close());
document.getElementById("seller-select")?.addEventListener("change", event => {
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  window.location.assign("/sellers/" + encodeURIComponent(event.target.value) + "/overview");
});
bindForm("seller-form", async fields => {
  const seller = await api("/api/sellers", "POST", {name: fields.get("name")});
  window.location.assign("/sellers/" + encodeURIComponent(seller.id) + "/settings?tab=integrations");
});
bindForm("connection-form", async fields => {
  await api(sellerApi + "/connections", "POST", {adapter_key: fields.get("adapter_key"), name: fields.get("name"), config: JSON.parse(fields.get("config"))});
  window.location.reload();
});
bindForm("settings-form", async fields => {
  await api(sellerApi + "/settings/" + encodeURIComponent(fields.get("key")), "PUT", {value: JSON.parse(fields.get("value"))});
  window.location.reload();
});
bindForm("store-name-form", async fields => {
  await api(sellerApi + "/settings/store.name", "PUT", {value: fields.get("name")});
  window.location.reload();
});
for (const link of document.querySelectorAll("[data-marketplace]")) link.addEventListener("click", async event => {
  event.preventDefault();
  try { await api(sellerApi + "/settings/sales.selected", "PUT", {value: link.dataset.marketplace}); window.location.assign(link.href); } catch (error) { showError(error); }
});
if (document.body.dataset.page === "sales" && ["wb", "ozon", "kit"].includes(pageQuery.get("channel"))) {
  api(sellerApi + "/settings/sales.selected", "PUT", {value: pageQuery.get("channel")}).catch(showError);
}
document.getElementById("navigation-toggle")?.addEventListener("click", event => {
  const open = document.body.classList.toggle("navigation-open");
  event.currentTarget.setAttribute("aria-expanded", String(open));
});
for (const button of document.querySelectorAll("[data-view-tab]")) button.addEventListener("click", () => {
  const scope = button.closest("[data-view-scope]");
  scope.querySelectorAll("[data-view-tab]").forEach(v => { v.classList.toggle("secondary", v !== button); v.setAttribute("aria-selected", String(v === button)); v.tabIndex = v === button ? 0 : -1; });
  scope.querySelectorAll("[data-view-panel]").forEach(v => { v.hidden = v.dataset.viewPanel !== button.dataset.viewTab; });
  try { sessionStorage.setItem("fbe:tab:" + sellerId + ":" + scope.dataset.viewScope, button.dataset.viewTab); } catch {}
});
for (const scope of document.querySelectorAll("[data-view-scope]")) {
  const tabs = [...scope.querySelectorAll("[data-view-tab]")];
  for (const [index, tab] of tabs.entries()) tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
    tabs[next].focus(); tabs[next].click();
  });
  let value = pageQuery.get("tab");
  if (!value) try { value = sessionStorage.getItem("fbe:tab:" + sellerId + ":" + scope.dataset.viewScope); } catch {}
  ([...scope.querySelectorAll("[data-view-tab]")].find(v => v.dataset.viewTab === value) || scope.querySelector("[data-view-tab]"))?.click();
}

const taskStates = {queued: "В очереди", running: "Выполняется", succeeded: "Завершено", failed: "Ошибка", interrupted: "Прервано"};
let activityConnections = [], activityBusy = false;
function renderOperations(operations) {
  const list = document.getElementById("operations-list");
  if (!list) return;
  const active = operations.filter(v => ["queued", "running"].includes(v.status)).length;
  const badge = document.getElementById("activity-count"); badge.hidden = !active; badge.textContent = String(active);
  const elements = operations.map(operation => {
    const connection = activityConnections.find(c => c.id === operation.connection_id);
    const spec = connection?.operations.find(s => s.key === operation.operation_key);
    const row = document.createElement("article"); row.className = "operation";
    const title = document.createElement("strong"); title.textContent = (spec?.label || "Задача") + " · " + (taskStates[operation.status] || "Проверка результата");
    const detail = document.createElement("p"); detail.textContent = (connection?.name || "Подключение") + " · " + new Date(operation.created_at).toLocaleString("ru-RU");
    row.append(title, detail);
    if (["failed", "interrupted"].includes(operation.status)) {
      const message = document.createElement("p"); message.className = "inline-error";
      message.textContent = operation.status === "interrupted" ? "Выполнение прервано. Откройте рабочий раздел и сверьте результат перед повтором." : "Не удалось выполнить задачу. Откройте рабочий раздел для проверки.";
      row.append(message);
    }
    if (connection && ["wb", "ozon", "kit", "chz"].includes(connection.adapter_key)) {
      const link = document.createElement("a"); link.className = "text-link";
      link.href = "/sellers/" + encodeURIComponent(sellerId) + (connection.adapter_key === "chz" ? "/marking" : "/sales?channel=" + connection.adapter_key) + (connection.adapter_key === "chz" ? "?tab=history&" : "&") + "connection=" + encodeURIComponent(connection.id);
      link.textContent = "Открыть рабочий раздел"; row.append(link);
    }
    return row;
  });
  if (!elements.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "Задач пока нет."; elements.push(p); }
  list.replaceChildren(...elements);
}
async function refreshActivity() {
  if (!sellerId || activityBusy) return;
  activityBusy = true;
  try {
    if (!activityConnections.length) activityConnections = await api(sellerApi + "/connections");
    renderOperations(await api(sellerApi + "/operations"));
  } catch (error) {
    const list = document.getElementById("operations-list");
    if (list) list.textContent = "Не удалось прочитать задачи. Откройте панель повторно.";
  } finally { activityBusy = false; }
}
const activityDialog = document.getElementById("activity-dialog");
document.getElementById("activity-open")?.addEventListener("click", () => { activityDialog.showModal(); refreshActivity(); });
document.getElementById("activity-close")?.addEventListener("click", () => activityDialog.close());
if (pageQuery.get("activity") === "1") { activityDialog?.showModal(); refreshActivity(); }
async function pollActivity() {
  if (!document.hidden) await refreshActivity();
  window.setTimeout(pollActivity, refreshSeconds * 1000);
}
if (sellerId) pollActivity();
