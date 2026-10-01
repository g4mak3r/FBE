"use strict";

// Capture the seller in this page's URL. Never keep mutable global server-side selection.
const sellerId = document.body.dataset.sellerId;
const sellerApi = `/api/sellers/${encodeURIComponent(sellerId)}`;
const feedback = document.getElementById("feedback");

function showError(error) {
  feedback.textContent = error.message;
  feedback.hidden = false;
}

async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-FBE-Flow": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Проверьте введенные данные");
  return data;
}

function bindForm(id, handler) {
  const form = document.getElementById(id);
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    feedback.hidden = true;
    try { await handler(new FormData(form)); }
    catch (error) { showError(error); }
    finally { button.disabled = false; }
  });
}

const sellerDialog = document.getElementById("seller-dialog");
for (const id of ["add-seller", "first-seller"]) {
  document.getElementById(id)?.addEventListener("click", () => sellerDialog.showModal());
}
document.getElementById("cancel-seller").addEventListener("click", () => sellerDialog.close());
document.getElementById("seller-select").addEventListener("change", (event) => {
  window.location.assign(`/sellers/${encodeURIComponent(event.target.value)}/overview`);
});
bindForm("seller-form", async (fields) => {
  const seller = await api("/api/sellers", "POST", { name: fields.get("name") });
  window.location.assign(`/sellers/${encodeURIComponent(seller.id)}/overview`);
});
bindForm("connection-form", async (fields) => {
  await api(`${sellerApi}/connections`, "POST", {
    adapter_key: fields.get("adapter_key"), name: fields.get("name"), config: JSON.parse(fields.get("config")),
  });
  window.location.reload();
});
bindForm("settings-form", async (fields) => {
  await api(`${sellerApi}/settings/${encodeURIComponent(fields.get("key"))}`, "PUT", { value: JSON.parse(fields.get("value")) });
  window.location.reload();
});
for (const button of document.querySelectorAll(".queue-operation")) {
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`${sellerApi}/operations`, "POST", { connection_id: button.dataset.connection, operation_key: button.dataset.operation });
      window.location.assign(`/sellers/${encodeURIComponent(sellerId)}/operations`);
    } catch (error) { showError(error); button.disabled = false; }
  });
}

const states = { queued: "В очереди", running: "Выполняется", succeeded: "Завершено", failed: "Ошибка", interrupted: "Прервано" };
function renderOperations(operations) {
  const list = document.getElementById("operations-list");
  if (!list) return;
  const elements = operations.map((operation) => {
    const row = document.createElement("article");
    row.className = "operation";
    const title = document.createElement("strong");
    title.textContent = `${operation.operation_key} · ${states[operation.status] || operation.status}`;
    const detail = document.createElement("p");
    const connection = operation.connection_id.slice(0, 8);
    detail.textContent = `Подключение ${connection} · ${new Date(operation.created_at).toLocaleString("ru-RU")}`;
    row.append(title, detail);
    if (operation.error_code) {
      const message = document.createElement("small");
      message.textContent = operation.status === "interrupted"
        ? "Процесс завершился во время операции. Проверьте результат во внешней системе перед повтором."
        : "Операция завершилась с ошибкой. Данные пакета не сохранены.";
      row.append(message);
    }
    return row;
  });
  if (!elements.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "Пока нет фоновых операций.";
    elements.push(empty);
  }
  list.replaceChildren(...elements);
}

async function refresh() {
  const status = document.getElementById("worker-status");
  try {
    const response = await fetch("/health");
    const health = await response.json();
    status.textContent = health.worker === "running" ? "Локальный worker работает" : "Worker остановлен";
    if (sellerId && document.getElementById("operations-list")) {
      renderOperations(await api(`${sellerApi}/operations`));
    }
  } catch { status.textContent = "Нет связи с платформой"; }
  // Schedule after completion, so slow requests cannot overlap or paint another seller's page.
  window.setTimeout(refresh, 3000);
}
refresh();
