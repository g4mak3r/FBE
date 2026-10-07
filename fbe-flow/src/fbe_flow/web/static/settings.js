"use strict";
(() => {
  const save = (key, value) => api(sellerApi + "/settings/" + key, "PUT", {value});
  const make = (tag, text) => { const v = document.createElement(tag); if (text !== undefined) v.textContent = text; return v; };
  bindForm("organization-form", async fields => {
    await api(sellerApi + "/organization", "PUT", Object.fromEntries(fields));
    location.reload();
  });
  bindForm("application-form", async fields => {
    await save("application.preferences", {operator: fields.get("operator").trim(), density: fields.get("density"), refresh_seconds: Number(fields.get("refresh_seconds"))});
    location.reload();
  });
  bindForm("printing-form", async fields => {
    await save("printing.preferences", {width_mm: Number(fields.get("width_mm")), height_mm: Number(fields.get("height_mm"))});
    showSuccess("Размер этикетки сохранен");
    document.getElementById("print-test-link").removeAttribute("aria-disabled");
  });
  document.getElementById("printing-form")?.addEventListener("input", () => document.getElementById("print-test-link").setAttribute("aria-disabled", "true"));
  document.getElementById("print-test-link")?.addEventListener("click", event => {
    if (event.currentTarget.getAttribute("aria-disabled") === "true") { event.preventDefault(); showError(new Error("Сначала сохраните размер этикетки")); }
  });
  for (const key of ["wb", "ozon", "kit", "chz"]) {
    bindForm("settings-connect-" + key, async fields => {
      const form = document.getElementById("settings-connect-" + key);
      let body = Object.fromEntries(fields);
      let endpoint = sellerApi + (key === "chz" ? "/marking" : key === "wb" ? "/wb" : "/commerce");
      if (["ozon", "kit"].includes(key)) body = {...body, adapter_key: key, read_only: fields.has("read_only")};
      await api(endpoint + "/connections", "POST", body);
      form.reset(); location.reload();
    });
  }
  const cardMessage = (card, text, bad = false) => {
    const node = card.querySelector(".connection-feedback");
    node.textContent = text; node.classList.toggle("inline-error", bad);
  };
  const path = (key, id) => sellerApi + (key === "chz" ? "/marking/" : key === "wb" ? "/wb/" : "/commerce/") + encodeURIComponent(id);
  for (const form of document.querySelectorAll(".credential-form")) {
    bindForm(form, async fields => {
      const card = form.closest(".integration-card");
      try {
        await api(path(form.dataset.adapter, form.dataset.connection) + "/credentials", "PUT", Object.fromEntries(fields));
        form.reset(); cardMessage(card, "Доступ обновлен");
      } catch (error) { cardMessage(card, error.message, true); throw error; }
    });
  }
  for (const form of document.querySelectorAll(".suz-settings-form")) {
    const card = form.closest(".integration-card");
    api(path("chz", form.dataset.connection) + "/overview").then(value => {
      for (const key of ["oms_id", "oms_connection"]) if (document.activeElement !== form.elements[key] && !form.elements[key].dataset.edited) form.elements[key].value = value.parameters[key] || "";
    }).catch(error => cardMessage(card, error.message, true));
    form.addEventListener("input", event => { event.target.dataset.edited = "1"; });
    bindForm(form, async fields => {
      try { await api(path("chz", form.dataset.connection) + "/suz", "PUT", Object.fromEntries(fields)); cardMessage(card, "СУЗ настроен"); }
      catch (error) { cardMessage(card, error.message, true); throw error; }
    });
  }
  for (const button of document.querySelectorAll(".certificates-load")) button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const values = await api(sellerApi + "/marking/certificates");
      const select = button.closest("form").querySelector(".certificate-select"), old = select.value;
      const prompt = make("option", "Выбрать сертификат"); prompt.value = "";
      select.replaceChildren(prompt, ...values.map(value => { const option = make("option", value.subject + " · " + value.expires); option.value = value.thumbprint; return option; }));
      if ([...select.options].some(v => v.value === old)) select.value = old;
      if (!values.length) showError(new Error("В текущем пользователе Windows нет действующих сертификатов с закрытым ключом"));
    } catch (error) { showError(error); } finally { button.disabled = false; }
  });
  for (const button of document.querySelectorAll(".connection-check")) button.addEventListener("click", async () => {
    if (button.disabled) return;
    const card = button.closest(".integration-card");
    button.disabled = true; cardMessage(card, "Проверяем доступ…");
    try { await api(sellerApi + "/connections/" + encodeURIComponent(button.dataset.connection) + "/check", "POST", {}); cardMessage(card, "Аккаунт подтвердил доступ"); }
    catch (error) { cardMessage(card, error.message, true); }
    finally { button.disabled = false; }
  });
  const backupButton = document.getElementById("backup-create");
  backupButton?.addEventListener("click", async () => {
    backupButton.disabled = true;
    try {
      const value = await api(sellerApi + "/backup", "POST", {});
      const link = make("a", "Скачать копию базы"); link.className = "text-link";
      link.href = sellerApi + "/backup/" + encodeURIComponent(value.filename); link.download = value.filename;
      document.getElementById("backup-result").replaceChildren(make("p", "Копия создана · " + Math.ceil(value.size_bytes / 1024) + " КБ"), link);
    } catch (error) { showError(error); } finally { backupButton.disabled = false; }
  });
  async function diagnostics() {
    const node = document.getElementById("worker-status"); if (!node) return;
    try { const r = await fetch("/health"), value = await r.json(); node.textContent = value.status === "ok" ? "Приложение доступно · служба " + (value.worker === "running" ? "работает" : "отключена") : "Служба остановлена. Перезапустите приложение."; }
    catch { node.textContent = "Нет связи с приложением"; }
  }
  document.getElementById("diagnostics-refresh")?.addEventListener("click", diagnostics);
  diagnostics();
})();
