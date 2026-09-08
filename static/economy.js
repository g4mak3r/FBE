(() => {
  "use strict";

  function numericValue(cell) {
    const raw = cell?.dataset?.sortValue ?? cell?.textContent ?? "";
    const normalized = String(raw)
      .replace(/\s/g, "")
      .replace(/₽|%/g, "")
      .replace(/,/g, ".")
      .replace(/[^0-9+\-.]/g, "");
    const value = Number(normalized);
    return Number.isFinite(value) ? value : Number.NEGATIVE_INFINITY;
  }

  function textValue(cell) {
    return String(cell?.dataset?.sortValue ?? cell?.textContent ?? "")
      .trim()
      .toLocaleLowerCase("ru-RU");
  }

  document.querySelectorAll(".sortable-table").forEach((table) => {
    const tbody = table.tBodies[0];
    if (!tbody) return;
    table.querySelectorAll(".sort-button").forEach((button) => {
      button.addEventListener("click", () => {
        const column = Number(button.dataset.column || 0);
        const type = button.dataset.type || "text";
        const wasActive = button.classList.contains("active");
        const nextDirection = wasActive && button.classList.contains("desc") ? "asc" : "desc";
        table.querySelectorAll(".sort-button").forEach((other) => {
          other.classList.remove("active", "asc", "desc");
        });
        button.classList.add("active", nextDirection);
        const rows = Array.from(tbody.rows).filter((row) => !row.querySelector(".empty-state"));
        rows.sort((a, b) => {
          const aCell = a.cells[column];
          const bCell = b.cells[column];
          let result;
          if (type === "number") {
            result = numericValue(aCell) - numericValue(bCell);
          } else {
            result = textValue(aCell).localeCompare(textValue(bCell), "ru-RU", { numeric: true });
          }
          return nextDirection === "asc" ? result : -result;
        });
        rows.forEach((row) => tbody.appendChild(row));
      });
    });
  });

  document.querySelectorAll("form.sync-form").forEach((form) => {
    form.addEventListener("submit", () => {
      if (form.classList.contains("is-busy")) return;
      form.classList.add("is-busy");
      form.querySelectorAll("button[type='submit']").forEach((button) => {
        button.dataset.originalText = button.textContent;
        button.textContent = button.dataset.busyText || "Синхронизация…";
        button.disabled = true;
      });
      const note = document.createElement("div");
      note.className = "sync-wait-note";
      note.textContent = "Задача продолжится в фоне с учетом лимитов WB; страницу можно закрыть.";
      form.appendChild(note);
    });
  });

  async function followEconomyJob() {
    const jobId = String(document.body?.dataset?.economyJob || "").trim();
    if (!jobId) return;
    const banner = document.getElementById("economy-job-state");
    const delay = (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    const finish = (kind, message) => {
      const url = new URL(window.location.href);
      url.searchParams.delete("job_id");
      url.searchParams.delete("msg");
      url.searchParams.delete("error");
      url.searchParams.set(kind, message || (kind === "msg" ? "Обновление завершено." : "Ошибка обновления."));
      window.location.replace(url.toString());
    };

    // A full finance history may legitimately take a long time because WB
    // requires pauses between pages. Poll only local job state and stop after
    // one hour instead of creating an unbounded browser loop.
    for (let attempt = 0; attempt < 720; attempt += 1) {
      try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        const job = await response.json().catch(() => ({}));
        if (!response.ok || !job.ok) {
          finish("error", job.error || `Не удалось получить состояние задачи (HTTP ${response.status}).`);
          return;
        }
        if (banner) banner.textContent = job.message || "Обновление экономики выполняется в фоне…";
        if (job.state === "done") {
          const result = job.result || {};
          if (result.ok === false || result.error) finish("error", result.error || result.message);
          else finish("msg", result.message || job.message);
          return;
        }
        if (job.state === "error") {
          finish("error", job.error || job.message);
          return;
        }
      } catch (error) {
        if (banner) banner.textContent = `Задача выполняется; временно не удалось обновить индикатор: ${error}`;
      }
      await delay(attempt < 20 ? 2000 : 5000);
    }
    if (banner) banner.textContent = "Задача продолжает выполняться в фоне. Обновите страницу позже.";
  }

  followEconomyJob();
})();
