function getRows(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return [];
  return [...table.querySelectorAll('tbody tr')].filter(row => row.dataset && Object.keys(row.dataset).length > 0);
}

function valueFor(row, key, type) {
  const raw = row.dataset[key] ?? '';
  if (type === 'number') {
    const n = Number(raw);
    return Number.isFinite(n) ? n : -1;
  }
  if (type === 'date') {
    const t = Date.parse(raw);
    return Number.isFinite(t) ? t : 0;
  }
  return String(raw).toLocaleLowerCase('ru-RU');
}

function clearSortMarkers(table) {
  table.querySelectorAll('th.sortable').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
  });
}

function sortByKey(tableId, key, type = 'text', direction = 'asc') {
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.querySelector('tbody');
  const rows = getRows(tableId);
  const dir = direction === 'desc' ? -1 : 1;

  rows.sort((a, b) => {
    const av = valueFor(a, key, type);
    const bv = valueFor(b, key, type);
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return 0;
  });

  rows.forEach(row => tbody.appendChild(row));
  clearSortMarkers(table);
  const th = table.querySelector(`th.sortable[data-key="${key}"]`);
  if (th) th.classList.add(direction === 'desc' ? 'sort-desc' : 'sort-asc');
}

function initSortable(root = document) {
  root.querySelectorAll('table.sortable-table th.sortable').forEach(th => {
    if (th.dataset.boundSort === '1') return;
    th.dataset.boundSort = '1';
    th.addEventListener('click', () => {
      const table = th.closest('table');
      if (!table || !table.id) return;

      const key = th.dataset.key;
      const type = th.dataset.type || 'text';
      const defaultDir = th.dataset.defaultDir || (type === 'text' ? 'asc' : 'desc');
      const current = th.classList.contains('sort-asc') ? 'asc' : (th.classList.contains('sort-desc') ? 'desc' : '');
      const next = current ? (current === 'asc' ? 'desc' : 'asc') : defaultDir;

      sortByKey(table.id, key, type, next);
    });
  });
}

function parseManualAromaList(select, key) {
  if (!select) return [];
  try {
    const raw = select.dataset[key] || '[]';
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
  } catch (err) {
    console.warn('Cannot parse manual aroma list', key, err);
    return [];
  }
}

function fillManualAromaOptions(labelType, aromaSelect) {
  if (!labelType || !aromaSelect) return;

  const previous = aromaSelect.value;
  const sampleAromas = parseManualAromaList(aromaSelect, 'sampleAromas');
  const nameAromas = parseManualAromaList(aromaSelect, 'nameAromas');
  const list = labelType.value === 'names' ? nameAromas : sampleAromas;

  aromaSelect.innerHTML = '';
  list.forEach((name) => {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    aromaSelect.appendChild(option);
  });

  if (previous && list.includes(previous)) {
    aromaSelect.value = previous;
  } else if (list.length) {
    aromaSelect.value = list[0];
  }
}

function syncManualForm() {
  const labelType = document.getElementById("label_type");
  const volumeBlock = document.getElementById("volume-block");
  const aromaSelect = document.getElementById("manual-aroma-select") || document.querySelector('select[name="aroma"]');
  if (!labelType || !volumeBlock) return;
  volumeBlock.style.display = labelType.value === "names" ? "grid" : "none";
  fillManualAromaOptions(labelType, aromaSelect);
}

function initManualForm(root = document) {
  const labelType = root.querySelector ? root.querySelector("#label_type") : document.getElementById("label_type");
  if (labelType && labelType.dataset.boundManual !== '1') {
    labelType.dataset.boundManual = '1';
    labelType.addEventListener("change", syncManualForm);
  }
  syncManualForm();

  root.querySelectorAll?.(".confirm-small-roll").forEach((button) => {
    if (button.dataset.boundConfirm === '1') return;
    button.dataset.boundConfirm = '1';
    button.addEventListener("click", (event) => {
      const ok = confirm("Внимание! Переход на маленькие этикетки!\n\nПожалуйста, прямо сейчас поменяйте рулон в принтере.\n\nНажмите OK — Сделано.\nНажмите Отмена — Не готово, отмена.");
      if (!ok) event.preventDefault();
    });
  });
}

// v22: phone-like slide between main modes. No server request on tab switch.
function activateLocalTab(tabName) {
  const track = document.getElementById('tab-track');
  if (!track) return false;
  const allowed = ['wb', 'manual'];
  const activeName = allowed.includes(tabName) ? tabName : 'wb';

  document.querySelectorAll('[data-local-tab]').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.localTab === activeName);
    if (tab.dataset.localTab === activeName) tab.setAttribute('aria-current', 'page');
    else tab.removeAttribute('aria-current');
  });

  track.classList.toggle('show-manual', activeName === 'manual');
  window.history.replaceState(null, '', activeName === 'wb' ? '#wb' : `#${activeName}`);
  return true;
}

function initTabs() {
  document.querySelectorAll('[data-local-tab]').forEach((link) => {
    if (link.dataset.boundTab === '1') return;
    link.dataset.boundTab = '1';
    link.addEventListener('click', (event) => {
      const tabName = link.dataset.localTab;
      if (activateLocalTab(tabName)) event.preventDefault();
    });
  });

  const hashTab = window.location.hash === '#manual' ? 'manual' : 'wb';
  activateLocalTab(hashTab);
}

async function openSupplyInline(supplyId) {
  const panel = document.getElementById('inline-supply-panel');
  if (!panel || !supplyId) return;

  const dashboard = document.getElementById('wb-panel');
  if (dashboard) dashboard.classList.add('supply-expanded');

  panel.classList.add('is-loading');
  panel.classList.add('is-open');
  panel.innerHTML = '<section class="card compact"><div class="loader-line">Загрузка поставки…</div></section>';

  try {
    const response = await fetch(`/supplies/${encodeURIComponent(supplyId)}/inline`, {
      headers: { 'X-Requested-With': 'fetch' }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const html = await response.text();
    panel.innerHTML = html;
    panel.classList.remove('is-loading');
    requestAnimationFrame(() => {
      panel.classList.add('is-open');
      if (window.matchMedia('(max-width: 1180px)').matches) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    initSortable(panel);
    initSelectAll(panel);
    initInlineSupplyClose(panel);
    initNeedsButtons(panel);
    initSupplyToolModals(panel);
    initMarkingNeeds(panel);
    initSupplyOrderActionPanel(panel);
  } catch (err) {
    panel.classList.remove('is-loading');
    panel.innerHTML = `<section class="card"><div class="banner error">Не удалось открыть поставку: ${err.message}</div></section>`;
  }
}

function initSupplyOpenLinks(root = document) {
  root.querySelectorAll('.supply-open-link[data-supply-id]').forEach(link => {
    if (link.dataset.boundSupplyOpen === '1') return;
    link.dataset.boundSupplyOpen = '1';
    link.addEventListener('click', (event) => {
      event.preventDefault();
      openSupplyInline(link.dataset.supplyId);
    });
  });
}

function initInlineSupplyClose(root = document) {
  root.querySelectorAll('[data-close-supply-panel]').forEach(button => {
    if (button.dataset.boundCloseSupply === '1') return;
    button.dataset.boundCloseSupply = '1';
    button.addEventListener('click', () => {
      const panel = document.getElementById('inline-supply-panel');
      if (!panel) return;
      panel.classList.remove('is-open');
      const dashboard = document.getElementById('wb-panel');
      if (dashboard) dashboard.classList.remove('supply-expanded');
      setTimeout(() => { panel.innerHTML = ''; }, 320);
    });
  });
}


function autoOpenSupplyFromPage() {
  const supplyId = document.body?.dataset?.openSupply;
  if (supplyId) openSupplyInline(supplyId);
}


document.addEventListener('DOMContentLoaded', () => {
  initSortable(document);
  initSelectAll(document);
  initManualForm(document);
  initTabs();
  initSupplyOpenLinks(document);
  initNewOrderActionPanel(document);
  autoOpenSupplyFromPage();
});

// v0.34: robust viewport-centered bottom popup for selected orders and easier row selection.
function selectedNewOrderIds() {
  const table = document.getElementById('new-orders-table');
  if (!table) return [];
  return [...table.querySelectorAll('tbody input[name="order_ids"]:checked')]
    .map(cb => cb.value)
    .filter(Boolean);
}

function populateHiddenOrderInputs(target, ids) {
  if (!target) return;
  target.innerHTML = '';
  ids.forEach(id => {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = 'order_ids';
    input.value = id;
    target.appendChild(input);
  });
}

function syncNewOrderSelectionVisuals() {
  const table = document.getElementById('new-orders-table');
  if (!table) return;

  const boxes = [...table.querySelectorAll('tbody input[name="order_ids"]')];
  boxes.forEach(cb => {
    const row = cb.closest('tr');
    if (row) row.classList.toggle('is-selected', cb.checked);
  });

  const master = table.querySelector('.select-all[data-target="new-orders-table"]');
  const checkedCount = boxes.filter(cb => cb.checked).length;
  if (master) {
    master.checked = boxes.length > 0 && checkedCount === boxes.length;
    master.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
  }
}

function updateNewOrderActionPanel(forceHide = false) {
  const panel = document.getElementById('order-action-panel');
  if (!panel) return;

  const ids = selectedNewOrderIds();
  const count = document.getElementById('selected-orders-count');
  if (count) count.textContent = String(ids.length);

  document.querySelectorAll('[data-order-hidden-target]').forEach(target => populateHiddenOrderInputs(target, ids));
  syncNewOrderSelectionVisuals();

  if (forceHide || ids.length === 0) {
    panel.classList.remove('is-open');
    window.setTimeout(() => {
      if (!panel.classList.contains('is-open')) panel.hidden = true;
    }, 180);
    return;
  }

  panel.hidden = false;
  requestAnimationFrame(() => panel.classList.add('is-open'));
}

function initNewOrderActionPanel(root = document) {
  const table = document.getElementById('new-orders-table');
  if (!table) return;

  // The tab slider uses transforms/will-change for animation. In browsers, that can make
  // position: fixed behave as if it were fixed to the transformed parent instead of the
  // viewport. Move the bottom action panel to <body> so it is always centered on screen.
  const panel = document.getElementById('order-action-panel');
  if (panel && panel.parentElement !== document.body) {
    document.body.appendChild(panel);
  }

  table.querySelectorAll('tbody input[name="order_ids"]').forEach(cb => {
    if (cb.dataset.boundOrderAction !== '1') {
      cb.dataset.boundOrderAction = '1';
      cb.addEventListener('change', () => updateNewOrderActionPanel(false));
    }
  });

  table.querySelectorAll('tbody tr').forEach(row => {
    if (row.dataset.boundRowSelect === '1') return;
    const cb = row.querySelector('input[name="order_ids"]');
    if (!cb) return;
    row.dataset.boundRowSelect = '1';
    row.addEventListener('click', (event) => {
      const tag = event.target?.tagName?.toLowerCase();
      if (['input', 'button', 'a', 'select', 'option', 'label', 'code'].includes(tag)) return;
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });

  root.querySelectorAll('[data-close-order-actions]').forEach(button => {
    if (button.dataset.boundOrderActionClose === '1') return;
    button.dataset.boundOrderActionClose = '1';
    button.addEventListener('click', () => updateNewOrderActionPanel(true));
  });

  document.querySelectorAll('#add-to-existing-supply-form, #create-and-add-supply-form').forEach(form => {
    if (form.dataset.boundSelectedOrdersSubmit === '1') return;
    form.dataset.boundSelectedOrdersSubmit = '1';
    form.addEventListener('submit', (event) => {
      const ids = selectedNewOrderIds();
      document.querySelectorAll('[data-order-hidden-target]').forEach(target => populateHiddenOrderInputs(target, ids));
      if (!ids.length) {
        event.preventDefault();
        alert('Выберите хотя бы один заказ.');
      }
    });
  });

  updateNewOrderActionPanel(false);
}

// v0.38: local production needs / accounting preview.
function ensureNeedsModal() {
  let overlay = document.getElementById('needs-modal-overlay');
  if (overlay) return overlay;

  overlay = document.createElement('div');
  overlay.id = 'needs-modal-overlay';
  overlay.className = 'needs-modal-overlay';
  overlay.hidden = true;
  overlay.innerHTML = '<div class="needs-modal-shell"><div class="loader-line">Считаю потребность…</div></div>';
  document.body.appendChild(overlay);

  overlay.addEventListener('click', (event) => {
    if (event.target === overlay || event.target.closest('[data-close-needs-modal]')) closeNeedsModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeNeedsModal();
  });
  return overlay;
}

function openNeedsModalLoading() {
  const overlay = ensureNeedsModal();
  const shell = overlay.querySelector('.needs-modal-shell');
  shell.innerHTML = '<div class="loader-line">Считаю потребность…</div>';
  overlay.hidden = false;
  requestAnimationFrame(() => overlay.classList.add('is-open'));
  return shell;
}

function closeNeedsModal() {
  const overlay = document.getElementById('needs-modal-overlay');
  if (!overlay) return;
  overlay.classList.remove('is-open');
  window.setTimeout(() => { if (!overlay.classList.contains('is-open')) overlay.hidden = true; }, 160);
}

async function showNeedsFromUrl(url) {
  const shell = openNeedsModalLoading();
  try {
    const response = await fetch(url, { headers: { 'X-Requested-With': 'fetch' } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    shell.innerHTML = await response.text();
  } catch (err) {
    shell.innerHTML = `<div class="needs-modal-card"><div class="banner error">Не удалось посчитать учет: ${err.message}</div><button type="button" class="secondary" data-close-needs-modal>Закрыть</button></div>`;
  }
}

function postNeedsByArticles(articles, subtitle) {
  if (!articles.length) {
    alert('Не нашёл артикулы продавца для учета.');
    return;
  }
  const shell = openNeedsModalLoading();
  const formData = new FormData();
  articles.forEach(article => formData.append('articles', article));
  formData.append('subtitle', subtitle || 'Выбранные позиции');

  fetch('/needs/articles', {
    method: 'POST',
    body: formData,
    headers: { 'X-Requested-With': 'fetch' }
  })
    .then(response => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.text();
    })
    .then(html => { shell.innerHTML = html; })
    .catch(err => {
      shell.innerHTML = `<div class="needs-modal-card"><div class="banner error">Не удалось посчитать учет: ${err.message}</div><button type="button" class="secondary" data-close-needs-modal>Закрыть</button></div>`;
    });
}

function selectedNewOrderArticles() {
  const result = [];
  document.querySelectorAll('[data-new-order-checkbox]:checked').forEach(input => {
    const row = input.closest('tr');
    const article = (row?.dataset.article || '').trim();
    if (article) result.push(article);
  });
  return result;
}

function tableArticles(selector) {
  const table = document.querySelector(selector);
  if (!table) return [];
  const result = [];
  table.querySelectorAll('tbody tr[data-article]').forEach(row => {
    const article = (row.dataset.article || '').trim();
    if (article) result.push(article);
  });
  return result;
}

function showNeedsForSelectedOrders() {
  const articles = selectedNewOrderArticles();
  if (!articles.length) {
    alert('Выберите хотя бы один заказ.');
    return;
  }
  postNeedsByArticles(articles, 'Выбранные новые заказы');
}

function showNeedsForTable(selector, subtitle) {
  const articles = tableArticles(selector);
  postNeedsByArticles(articles, subtitle || 'Позиции в поставке');
}

function initNeedsButtons(root = document) {
  root.querySelectorAll?.('[data-needs-url]').forEach(button => {
    if (button.dataset.boundNeeds === '1') return;
    button.dataset.boundNeeds = '1';
    button.addEventListener('click', () => showNeedsFromUrl(button.dataset.needsUrl));
  });

  root.querySelectorAll?.('[data-needs-table]').forEach(button => {
    if (button.dataset.boundNeedsTable === '1') return;
    button.dataset.boundNeedsTable = '1';
    button.addEventListener('click', () => showNeedsForTable(button.dataset.needsTable, button.dataset.needsSubtitle));
  });

  root.querySelectorAll?.('[data-needs-selected]').forEach(button => {
    if (button.dataset.boundNeedsSelected === '1') return;
    button.dataset.boundNeedsSelected = '1';
    button.addEventListener('click', showNeedsForSelectedOrders);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initNeedsButtons(document);
});


// v0.42: small WB analytics card in the top header.
function setAnalyticsLoading(isLoading) {
  const refresh = document.getElementById('analytics-refresh');
  if (!refresh) return;
  refresh.disabled = !!isLoading;
  refresh.classList.toggle('is-loading', !!isLoading);
}

function renderTopAnalytics(data) {
  const updated = document.getElementById('analytics-updated');
  const ordersSum = document.getElementById('analytics-orders-sum');
  const ordersCount = document.getElementById('analytics-orders-count');
  const buyoutsSum = document.getElementById('analytics-buyouts-sum');
  const buyoutsCount = document.getElementById('analytics-buyouts-count');
  const message = document.getElementById('analytics-message');

  if (updated) updated.textContent = data.updated_label || data.updated || 'обновлено';
  if (ordersSum) ordersSum.textContent = data.orders_sum_label || '—';
  if (ordersCount) ordersCount.textContent = `${data.orders_count || 0} шт.`;
  if (buyoutsSum) buyoutsSum.textContent = data.buyouts_sum_label || '—';
  if (buyoutsCount) buyoutsCount.textContent = `${data.buyouts_count || 0} шт.`;
  if (message) {
    message.textContent = data.message || '';
    message.classList.toggle('warn', !data.ok || !!data.stale);
  }
}

async function loadTopAnalytics(force = false) {
  const card = document.getElementById('top-analytics-card');
  if (!card) return;
  const baseUrl = card.dataset.analyticsUrl || '/api/analytics/today';
  const url = force ? `${baseUrl}?force=1` : baseUrl;
  setAnalyticsLoading(true);
  try {
    const response = await fetch(url, { headers: { 'Accept': 'application/json' } });
    const data = await response.json();
    renderTopAnalytics(data);
  } catch (err) {
    renderTopAnalytics({ ok: false, updated_label: 'обновлено: ошибка', orders_sum_label: '—', orders_count: 0, buyouts_sum_label: '—', buyouts_count: 0, message: `Не удалось загрузить аналитику: ${err}` });
  } finally {
    setAnalyticsLoading(false);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const refresh = document.getElementById('analytics-refresh');
  if (refresh) refresh.addEventListener('click', () => loadTopAnalytics(true));
  // v0.42: do not auto-fetch analytics after page render.
  // The template already contains cached values; this prevents a second WB Statistics request
  // on every browser refresh and avoids 429 Too Many Requests.
});


// v0.43: bottom popup for already-added orders inside an opened supply.
function selectedSupplyOrderIds(panel) {
  if (!panel) return [];
  const selector = panel.dataset.sourceTable || '';
  const table = selector ? document.querySelector(selector) : null;
  if (!table) return [];
  return [...table.querySelectorAll('tbody input[name="order_ids"]:checked')]
    .map(cb => cb.value)
    .filter(Boolean);
}

function populateSupplyHiddenInputs(panel, ids) {
  if (!panel) return;
  panel.querySelectorAll('[data-supply-hidden-target]').forEach(target => {
    target.innerHTML = '';
    ids.forEach(id => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'order_ids';
      input.value = id;
      target.appendChild(input);
    });
  });
}

function syncSupplySelectionVisuals(panel) {
  if (!panel) return;
  const table = document.querySelector(panel.dataset.sourceTable || '');
  if (!table) return;
  const boxes = [...table.querySelectorAll('tbody input[name="order_ids"]')];
  const checkedCount = boxes.filter(cb => cb.checked).length;
  boxes.forEach(cb => {
    const row = cb.closest('tr');
    if (row) row.classList.toggle('is-selected-supply', cb.checked);
  });
  const master = table.querySelector('.select-all');
  if (master) {
    master.checked = boxes.length > 0 && checkedCount === boxes.length;
    master.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
  }
}

function updateSupplyOrderActionPanel(panel, forceHide = false) {
  if (!panel) return;
  const ids = selectedSupplyOrderIds(panel);
  const count = panel.querySelector('[data-supply-selected-count]');
  if (count) count.textContent = String(ids.length);
  populateSupplyHiddenInputs(panel, ids);
  syncSupplySelectionVisuals(panel);

  if (forceHide || ids.length === 0) {
    panel.classList.remove('is-open');
    window.setTimeout(() => {
      if (!panel.classList.contains('is-open')) panel.hidden = true;
    }, 180);
    return;
  }
  panel.hidden = false;
  requestAnimationFrame(() => panel.classList.add('is-open'));
}

function initSupplyOrderActionPanel(root = document) {
  root.querySelectorAll?.('.supply-selected-panel[data-source-table]').forEach(panel => {
    if (panel.parentElement !== document.body) document.body.appendChild(panel);
    const table = document.querySelector(panel.dataset.sourceTable || '');
    if (!table) return;

    table.querySelectorAll('tbody input[name="order_ids"]').forEach(cb => {
      if (cb.dataset.boundSupplyAction !== '1') {
        cb.dataset.boundSupplyAction = '1';
        cb.addEventListener('change', () => updateSupplyOrderActionPanel(panel, false));
      }
    });

    table.querySelectorAll('tbody tr').forEach(row => {
      if (row.dataset.boundSupplyRowSelect === '1') return;
      const cb = row.querySelector('input[name="order_ids"]');
      if (!cb) return;
      row.dataset.boundSupplyRowSelect = '1';
      row.addEventListener('click', event => {
        const tag = event.target?.tagName?.toLowerCase();
        if (['input', 'button', 'a', 'select', 'option', 'label', 'code'].includes(tag)) return;
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      });
    });

    panel.querySelectorAll('[data-close-supply-actions]').forEach(button => {
      if (button.dataset.boundSupplyActionClose === '1') return;
      button.dataset.boundSupplyActionClose = '1';
      button.addEventListener('click', () => updateSupplyOrderActionPanel(panel, true));
    });

    panel.querySelectorAll('form').forEach(form => {
      if (form.dataset.boundSupplySubmit === '1') return;
      form.dataset.boundSupplySubmit = '1';
      form.addEventListener('submit', event => {
        const ids = selectedSupplyOrderIds(panel);
        populateSupplyHiddenInputs(panel, ids);
        if (!ids.length) {
          event.preventDefault();
          alert('Выберите хотя бы одно задание в поставке.');
          return;
        }
        if (form.classList.contains('supply-move-form')) {
          const select = form.querySelector('select[name="target_supply_id"]');
          if (!select || !select.value) {
            event.preventDefault();
            alert('Выберите поставку, куда перенести задания.');
          }
        }
      });
    });

    updateSupplyOrderActionPanel(panel, false);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initSelectAll(document);
  initSupplyOrderActionPanel(document);
});


// v0.44: fixed master checkbox behavior, compact workflow modals, new-tab PDF forms.
function initSelectAll(root = document) {
  root.querySelectorAll('.select-all[data-target]').forEach(master => {
    if (master.dataset.boundSelect === '1') return;
    master.dataset.boundSelect = '1';
    master.addEventListener('change', () => {
      const table = document.getElementById(master.dataset.target);
      if (!table) return;
      const shouldCheck = !!master.checked;
      master.indeterminate = false;
      const boxes = [...table.querySelectorAll('tbody input[type="checkbox"]')];
      boxes.forEach(cb => { cb.checked = shouldCheck; });
      boxes.forEach(cb => cb.dispatchEvent(new Event('change', { bubbles: true })));
      updateNewOrderActionPanel(false);
      document.querySelectorAll('.supply-selected-panel').forEach(panel => updateSupplyOrderActionPanel(panel, false));
    });
  });
}

function portalSupplyToolModal(modal) {
  if (!modal || modal.parentElement === document.body) return;
  const placeholder = document.createComment(`fbe-modal-placeholder:${modal.id || 'modal'}`);
  modal.__fbePortalPlaceholder = placeholder;
  modal.parentNode.insertBefore(placeholder, modal);
  document.body.appendChild(modal);
}

function restoreSupplyToolModal(modal) {
  if (!modal) return;
  const placeholder = modal.__fbePortalPlaceholder;
  if (placeholder && placeholder.isConnected) {
    placeholder.replaceWith(modal);
  }
  modal.__fbePortalPlaceholder = null;
}

function syncModalBodyLock() {
  const hasOpenModal = !!document.querySelector('.supply-tool-modal.is-open');
  document.body.classList.toggle('supply-modal-open', hasOpenModal);
}

function openSupplyToolModal(selector) {
  const modal = document.querySelector(selector);
  if (!modal) return;

  // Supply panels are animated with transforms. A fixed modal nested inside such a
  // panel becomes fixed to the panel instead of the browser viewport. Temporarily
  // move it to <body> so Честный Знак and other tools are true screen overlays.
  portalSupplyToolModal(modal);
  modal.hidden = false;
  requestAnimationFrame(() => {
    modal.classList.add('is-open');
    syncModalBodyLock();
  });

  const firstInput = modal.querySelector('input, select, button[type="submit"], [data-modal-close]');
  if (firstInput && typeof firstInput.focus === 'function') {
    window.setTimeout(() => firstInput.focus({ preventScroll: true }), 80);
  }
}

function closeSupplyToolModal(modal) {
  if (!modal) return;
  modal.classList.remove('is-open');
  window.setTimeout(() => {
    if (modal.classList.contains('is-open')) return;
    modal.hidden = true;
    restoreSupplyToolModal(modal);
    syncModalBodyLock();
  }, 180);
}

function initSupplyToolModals(root = document) {
  root.querySelectorAll?.('[data-modal-open]').forEach(button => {
    if (button.dataset.boundToolModalOpen === '1') return;
    button.dataset.boundToolModalOpen = '1';
    button.addEventListener('click', () => openSupplyToolModal(button.dataset.modalOpen));
  });

  root.querySelectorAll?.('.supply-tool-modal').forEach(modal => {
    if (modal.dataset.boundToolModal === '1') return;
    modal.dataset.boundToolModal = '1';
    modal.addEventListener('click', event => {
      if (event.target === modal || event.target.closest('[data-modal-close]')) {
        closeSupplyToolModal(modal);
      }
    });
    modal.querySelectorAll('form[data-reopen-modal]').forEach(form => {
      if (form.dataset.boundReopenModal === '1') return;
      form.dataset.boundReopenModal = '1';
      form.addEventListener('submit', () => {
        try { sessionStorage.setItem('fbeReopenModal', form.dataset.reopenModal || ''); } catch (_) {}
      });
    });
    modal.querySelectorAll('form[target="_blank"]').forEach(form => {
      if (form.dataset.boundBlankSubmit === '1') return;
      form.dataset.boundBlankSubmit = '1';
      form.addEventListener('submit', () => {
        window.setTimeout(() => closeSupplyToolModal(modal), 250);
      });
    });
  });
}

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  document.querySelectorAll('.supply-tool-modal.is-open').forEach(closeSupplyToolModal);
});

document.addEventListener('DOMContentLoaded', () => {
  initSelectAll(document);
  initSupplyToolModals(document);
  try {
    const selector = sessionStorage.getItem('fbeReopenModal');
    if (selector) {
      sessionStorage.removeItem('fbeReopenModal');
      window.setTimeout(() => openSupplyToolModal(selector), 120);
    }
  } catch (_) {}
});

// 0.69.0: exact KIZ lifecycle, per-GTIN compliance data and transparent True API diagnostics.
async function markingApiRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'fetch',
      ...(options.headers || {})
    }
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch (_) {
    payload = { ok: false, error: `HTTP ${response.status}` };
  }
  if (!response.ok || payload?.ok === false) {
    const parts = [payload?.error || payload?.message || `HTTP ${response.status}`];
    if (payload?.request_id) parts.push(`requestId: ${payload.request_id}`);
    if (payload?.details) {
      const details = typeof payload.details === 'string' ? payload.details : JSON.stringify(payload.details, null, 2);
      parts.push(`Ответ True API: ${details}`);
    }
    if (payload?.diagnostic_file) parts.push(`Диагностика сохранена: ${payload.diagnostic_file}`);
    throw new Error(parts.filter(Boolean).join('\n'));
  }
  return payload;
}

function markingMessage(shell, text, kind = 'ok') {
  const box = shell.querySelector('[data-marking-message]');
  if (!box) return;
  box.hidden = false;
  box.className = `marking-live-message ${kind}`;
  box.textContent = text;
}

async function refreshMarkingShell(shell, loadingText = 'Перечитываю каталог SQLite, локальную базу и статусы WB…') {
  const url = shell.dataset.markingUrl;
  if (!url) return;
  const previous = shell.innerHTML;
  shell.innerHTML = `<div class="marking-needs-loading">${loadingText}</div>`;
  try {
    const response = await fetch(`${url}?t=${Date.now()}`, {
      headers: { 'X-Requested-With': 'fetch' }
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    shell.innerHTML = await response.text();
    bindMarkingWorkspace(shell);
  } catch (err) {
    shell.innerHTML = previous;
    bindMarkingWorkspace(shell);
    throw err;
  }
}

async function runMarkingStatusJob(shell, supplyId, { autoWatch = false, watchAttempt = 0 } = {}) {
  const started = await markingApiRequest('/api/marking/global/refresh', {
    method: 'POST', body: JSON.stringify({ supply_ids: [String(supplyId)] })
  });
  if (!started.job_id) return started;
  while (true) {
    const job = await markingApiRequest(`/api/jobs/${encodeURIComponent(started.job_id)}`);
    markingMessage(shell, job.message || 'Проверяю серверы Честного Знака…', job.state === 'error' ? 'error' : 'ok');
    if (job.state === 'done' || job.state === 'error') {
      await refreshMarkingShell(shell, 'Обновляю локальный статус…');
      const content = shell.querySelector('[data-marking-needs-content]');
      const processing = Number(content?.dataset?.markingProcessing || 0);
      if (job.state === 'error') {
        const text = job.error || job.message || 'Ошибка обновления статусов';
        markingMessage(shell, text, 'error');
        throw new Error(text);
      }
      const result = job.result || job;
      if (autoWatch && processing > 0 && watchAttempt < 12) {
        const nextAttempt = watchAttempt + 1;
        const delay = Math.min(10000, 1500 + nextAttempt * 1200);
        markingMessage(
          shell,
          `${result.message || job.message || 'Документ еще обрабатывается.'} Повторная проверка автоматически через ${Math.ceil(delay / 1000)} с.`,
          'ok'
        );
        await new Promise(resolve => setTimeout(resolve, delay));
        return runMarkingStatusJob(shell, supplyId, { autoWatch: true, watchAttempt: nextAttempt });
      }
      if (autoWatch && processing > 0) {
        markingMessage(
          shell,
          'Автоматическая проверка приостановлена после 12 попыток. Документ сохранен; нажмите «Проверить статусы» позже.',
          'ok'
        );
        return result;
      }
      markingMessage(shell, result.message || job.message || 'Статусы актуальны.', 'ok');
      return result;
    }
    await new Promise(resolve => setTimeout(resolve, 700));
  }
}

async function followBackgroundMarkingJob(shell, jobId, loadingText = 'Обновляю состояние Честного Знака…') {
  if (!jobId) throw new Error('FBE не получил идентификатор фоновой операции');
  while (true) {
    const job = await markingApiRequest(`/api/jobs/${encodeURIComponent(jobId)}`);
    const state = String(job.state || 'running');
    markingMessage(shell, job.message || 'Операция выполняется…', state === 'error' ? 'error' : 'ok');
    if (state === 'done' || state === 'error') {
      await refreshMarkingShell(shell, loadingText);
      if (state === 'error') {
        const text = job.error || job.message || 'Фоновая операция завершилась ошибкой';
        markingMessage(shell, text, 'error');
        throw new Error(text);
      }
      const result = job.result || job;
      markingMessage(shell, result.message || job.message || 'Готово.', 'ok');
      return result;
    }
    await new Promise(resolve => setTimeout(resolve, 650));
  }
}

const markingLiveWatchers = window.__fbeMarkingLiveWatchers || new Set();
window.__fbeMarkingLiveWatchers = markingLiveWatchers;
function ensureMarkingLiveWatcher(shell, supplyId) {
  const key = String(supplyId);
  if (markingLiveWatchers.has(key)) return;
  markingLiveWatchers.add(key);
  window.setTimeout(async () => {
    try {
      await runMarkingStatusJob(shell, supplyId, { autoWatch: true });
    } catch (err) {
      markingMessage(shell, err.message, 'error');
    } finally {
      markingLiveWatchers.delete(key);
    }
  }, 800);
}

function bindMarkingWorkspace(shell) {
  const content = shell.querySelector('[data-marking-needs-content]');
  const supplyId = content?.dataset.supplyId;
  if (!content || !supplyId) return;

  content.querySelectorAll('[data-marking-order]').forEach(orderRow => {
    const orderId = Number(orderRow.dataset.orderId || 0);
    const input = orderRow.querySelector('[data-marking-scan-input]');
    const validateButton = orderRow.querySelector('[data-marking-validate]');
    const scanButton = orderRow.querySelector('[data-marking-scan]');
    const scanPreview = orderRow.querySelector('[data-marking-scan-preview]');
    const layoutBadge = orderRow.querySelector('[data-marking-layout-badge]');
    const sendButton = orderRow.querySelector('[data-marking-send-wb]');
    const removeButton = orderRow.querySelector('[data-marking-remove]');
    let validatedCode = '';

    const renderScanPreview = (result) => {
      if (!scanPreview) return;
      scanPreview.replaceChildren();
      const title = document.createElement('b');
      title.textContent = result.layout_corrected
        ? 'Код корректен · раскладка RU автоматически исправлена на EN'
        : 'Код корректен · GTIN совпадает';
      const details = document.createElement('span');
      details.textContent = `GTIN ${result.gtin || '—'} · серийный номер ${result.serial || '—'}`;
      const warning = document.createElement('small');
      warning.textContent = 'Код еще не записан. Нажмите «Закрепить локально».';
      scanPreview.append(title, details, warning);
      scanPreview.hidden = false;
      scanPreview.classList.remove('error');
    };

    const resetScanValidation = () => {
      validatedCode = '';
      if (scanButton) scanButton.disabled = true;
      if (scanPreview) {
        scanPreview.hidden = true;
        scanPreview.replaceChildren();
        scanPreview.classList.remove('error');
      }
    };

    const requestEnglishLayout = async () => {
      if (!layoutBadge) return;
      layoutBadge.textContent = 'EN…';
      layoutBadge.classList.remove('layout-warning', 'layout-ok');
      try {
        const response = await fetch('/api/input-layout/english', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
          body: '{}'
        });
        const result = await response.json().catch(() => ({}));
        if (result.ok) {
          layoutBadge.textContent = 'EN включена';
          layoutBadge.classList.add('layout-ok');
        } else {
          layoutBadge.textContent = 'EN автокоррекция';
          layoutBadge.classList.add('layout-warning');
        }
      } catch (_) {
        layoutBadge.textContent = 'EN автокоррекция';
        layoutBadge.classList.add('layout-warning');
      }
    };

    const runValidate = async () => {
      if (!input || !validateButton || !orderId) return;
      const code = input.value;
      if (!code) {
        markingMessage(shell, 'Сначала отсканируйте или вставьте полный код маркировки.', 'error');
        input.focus();
        return;
      }
      resetScanValidation();
      validateButton.disabled = true;
      input.disabled = true;
      try {
        const result = await markingApiRequest(`/api/supplies/${encodeURIComponent(supplyId)}/marking/validate`, {
          method: 'POST',
          body: JSON.stringify({ order_id: orderId, code })
        });
        validatedCode = result.normalized_code || code;
        input.value = validatedCode;
        renderScanPreview(result);
        if (scanButton) scanButton.disabled = false;
        markingMessage(shell, result.message || 'КИЗ проверен. Подтвердите локальную привязку.', 'ok');
      } catch (err) {
        if (scanPreview) {
          scanPreview.replaceChildren();
          const text = document.createElement('b');
          text.textContent = err.message;
          scanPreview.append(text);
          scanPreview.hidden = false;
          scanPreview.classList.add('error');
        }
        markingMessage(shell, err.message, 'error');
        input.focus();
        input.select();
      } finally {
        validateButton.disabled = false;
        input.disabled = false;
      }
    };

    const runAssign = async () => {
      if (!input || !scanButton || !orderId) return;
      if (!validatedCode || input.value !== validatedCode) {
        resetScanValidation();
        markingMessage(shell, 'После изменения строки КИЗ нужно проверить заново.', 'error');
        return;
      }
      const confirmed = window.confirm('Закрепить проверенный КИЗ локально за этим сборочным заданием?');
      if (!confirmed) return;
      scanButton.disabled = true;
      if (validateButton) validateButton.disabled = true;
      input.disabled = true;
      try {
        const result = await markingApiRequest(`/api/supplies/${encodeURIComponent(supplyId)}/marking/scan`, {
          method: 'POST',
          body: JSON.stringify({ order_id: orderId, code: validatedCode })
        });
        await refreshMarkingShell(shell, 'КИЗ закреплен. Обновляю задание…');
        markingMessage(shell, result.message || 'КИЗ закреплен локально.', 'ok');
      } catch (err) {
        scanButton.disabled = false;
        if (validateButton) validateButton.disabled = false;
        input.disabled = false;
        markingMessage(shell, err.message, 'error');
        input.focus();
        input.select();
      }
    };

    if (validateButton && validateButton.dataset.boundMarkingValidate !== '1') {
      validateButton.dataset.boundMarkingValidate = '1';
      validateButton.addEventListener('click', runValidate);
    }
    if (scanButton && scanButton.dataset.boundMarkingScan !== '1') {
      scanButton.dataset.boundMarkingScan = '1';
      scanButton.addEventListener('click', runAssign);
    }
    if (input && input.dataset.boundMarkingInput !== '1') {
      input.dataset.boundMarkingInput = '1';
      input.addEventListener('focus', requestEnglishLayout);
      input.addEventListener('pointerdown', requestEnglishLayout);
      input.addEventListener('input', resetScanValidation);
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          runValidate();
        }
      });
    }

    if (sendButton && sendButton.dataset.boundMarkingWb !== '1') {
      sendButton.dataset.boundMarkingWb = '1';
      sendButton.addEventListener('click', async () => {
        const confirmed = window.confirm('Закрепить сохраненный КИЗ за этим сборочным заданием в WB?');
        if (!confirmed) return;
        sendButton.disabled = true;
        try {
          const result = await markingApiRequest(`/api/supplies/${encodeURIComponent(supplyId)}/marking/${orderId}/send-wb`, {
            method: 'POST',
            body: '{}'
          });
          await refreshMarkingShell(shell, 'КИЗ отправлен. Получаю статус WB…');
          markingMessage(shell, result.message || 'КИЗ отправлен в WB.', 'ok');
        } catch (err) {
          sendButton.disabled = false;
          markingMessage(shell, err.message, 'error');
        }
      });
    }

    if (removeButton && removeButton.dataset.boundMarkingRemove !== '1') {
      removeButton.dataset.boundMarkingRemove = '1';
      removeButton.addEventListener('click', async () => {
        const confirmed = window.confirm('Удалить локальную привязку КИЗа? Сам код в Честном Знаке не изменится.');
        if (!confirmed) return;
        removeButton.disabled = true;
        try {
          const result = await markingApiRequest(`/api/supplies/${encodeURIComponent(supplyId)}/marking/${orderId}/remove`, {
            method: 'POST',
            body: '{}'
          });
          await refreshMarkingShell(shell, 'Удаляю локальную привязку…');
          markingMessage(shell, result.message || 'Локальная привязка удалена.', 'ok');
        } catch (err) {
          removeButton.disabled = false;
          markingMessage(shell, err.message, 'error');
        }
      });
    }
  });

  const saveLegalButton = content.querySelector('[data-marking-save-legal]');
  if (saveLegalButton && saveLegalButton.dataset.boundMarkingSaveLegal !== '1') {
    saveLegalButton.dataset.boundMarkingSaveLegal = '1';
    saveLegalButton.addEventListener('click', async () => {
      const omsId = content.querySelector('[data-suz-oms-id]')?.value?.trim() || '';
      const connectionId = content.querySelector('[data-suz-connection-id]')?.value?.trim() || '';
      const participantInn = content.querySelector('[data-circulation-participant-inn]')?.value?.trim() || '';
      saveLegalButton.disabled = true;
      try {
        const result = await markingApiRequest(
          `/api/supplies/${encodeURIComponent(supplyId)}/marking/legal-settings`,
          { method: 'POST', body: JSON.stringify({ oms_id: omsId, connection_id: connectionId, participant_inn: participantInn }) }
        );
        await refreshMarkingShell(shell, 'Сохраняю юридические настройки…');
        markingMessage(shell, result.message || 'Юридические настройки сохранены.', 'ok');
      } catch (err) {
        saveLegalButton.disabled = false;
        markingMessage(shell, err.message, 'error');
      }
    });
  }

  const checkStatusButton = content.querySelector('[data-marking-check-status]');
  if (checkStatusButton && checkStatusButton.dataset.boundMarkingCheckStatus !== '1') {
    checkStatusButton.dataset.boundMarkingCheckStatus = '1';
    checkStatusButton.addEventListener('click', async () => {
      checkStatusButton.disabled = true;
      markingMessage(shell, 'Проверка запущена — страницу можно продолжать использовать.', 'ok');
      try {
        const result = await runMarkingStatusJob(shell, supplyId, { autoWatch: true });
        markingMessage(shell, result?.message || 'Статусы обновлены.', result?.errors?.length ? 'error' : 'ok');
      } catch (err) {
        markingMessage(shell, err.message, 'error');
      } finally {
        checkStatusButton.disabled = false;
      }
    });
  }

  const directPrintButton = content.querySelector('[data-marking-print-direct]');
  if (directPrintButton && directPrintButton.dataset.boundMarkingPrintDirect !== '1') {
    directPrintButton.dataset.boundMarkingPrintDirect = '1';
    directPrintButton.addEventListener('click', async () => {
      const confirmed = window.confirm('Сразу отправить этикетки 30×20 мм на принтер без окна печати?');
      if (!confirmed) return;
      directPrintButton.disabled = true;
      markingMessage(shell, 'Формирую этикетки и отправляю их на Xprinter…', 'ok');
      try {
        const result = await markingApiRequest(
          `/api/supplies/${encodeURIComponent(supplyId)}/marking/print-direct`,
          { method: 'POST', body: '{}' }
        );
        await refreshMarkingShell(shell, 'Печать отправлена. Обновляю статус…');
        markingMessage(shell, result.message || 'Этикетки отправлены на печать.', 'ok');
      } catch (err) {
        directPrintButton.disabled = false;
        markingMessage(shell, err.message, 'error');
      }
    });
  }

  const applicationBlock = content.querySelector('[data-application-report-block]');
  const applicationButton = applicationBlock?.querySelector('[data-marking-application-report]');
  if (applicationButton && applicationButton.dataset.boundApplicationReport !== '1') {
    applicationButton.dataset.boundApplicationReport = '1';
    applicationButton.addEventListener('click', async () => {
      const participantInn = content.querySelector('[data-circulation-participant-inn]')?.value?.trim() || '';
      const productionDate = content.querySelector('[data-circulation-production-date]')?.value || '';
      const confirmed = window.confirm(
        'КИЗы физически напечатаны и нанесены на дезодоранты?\n\n' +
        'Отправить отдельный отчет о нанесении в СУЗ? FBE примет reportId и продолжит проверять статус в фоне.'
      );
      if (!confirmed) return;
      applicationButton.disabled = true;
      markingMessage(shell, 'Отправляю отчет о нанесении. После приема СУЗ интерфейс сразу освободится…', 'ok');
      try {
        const started = await markingApiRequest(
          `/api/supplies/${encodeURIComponent(supplyId)}/marking/application/start`,
          { method: 'POST', body: JSON.stringify({ confirmed: true, participant_inn: participantInn, production_date: productionDate, compliance_by_gtin: {} }) }
        );
        await followBackgroundMarkingJob(shell, started.job_id, 'Отчет принят СУЗ. Включаю фоновую проверку статуса…');
        ensureMarkingLiveWatcher(shell, supplyId);
      } catch (err) {
        applicationButton.disabled = false;
        markingMessage(shell, err.message, 'error');
      }
    });
  }

  const circulationBlock = content.querySelector('[data-circulation-block]');
  if (circulationBlock) {
    const introduceButton = circulationBlock.querySelector('[data-circulation-introduce]');
    if (introduceButton && introduceButton.dataset.boundCirculationIntroduce !== '1') {
      introduceButton.dataset.boundCirculationIntroduce = '1';
      introduceButton.addEventListener('click', async () => {
        const participantInn = content.querySelector('[data-circulation-participant-inn]')?.value?.trim() || '';
        const productionDate = content.querySelector('[data-circulation-production-date]')?.value || '';
        const requiresOverride = introduceButton.dataset.requiresComplianceOverride === '1';
        const overrideCheckbox = circulationBlock.querySelector('[data-ignore-compliance-conflicts]');
        const ignoreComplianceConflicts = Boolean(overrideCheckbox?.checked);
        if (requiresOverride && !ignoreComplianceConflicts) {
          markingMessage(shell, 'Подтвердите предупреждение о повторяющемся GTIN.', 'error');
          overrideCheckbox?.focus();
          return;
        }
        const confirmed = window.confirm(
          'Ввести ВСЕ готовые КИЗы этой поставки в оборот?\n\n' +
          `ИНН производителя: ${participantInn || 'не указан'}\n` +
          `Дата производства: ${productionDate || 'не указана'}\n\n` +
          'FBE отправит необходимые документы и будет сам проверять их до результата.'
        );
        if (!confirmed) return;
        introduceButton.disabled = true;
        markingMessage(shell, 'Формирую документы ввода в оборот и запускаю отслеживание…', 'ok');
        try {
          const started = await markingApiRequest(
            `/api/supplies/${encodeURIComponent(supplyId)}/marking/circulation/introduce/start`,
            {
              method: 'POST',
              body: JSON.stringify({
                confirmed: true,
                ignore_compliance_conflicts: ignoreComplianceConflicts,
                participant_inn: participantInn,
                production_date: productionDate,
                compliance_by_gtin: {}
              })
            }
          );
          await followBackgroundMarkingJob(shell, started.job_id, 'Проверяю, сколько КИЗов уже подтверждено в обороте…');
          ensureMarkingLiveWatcher(shell, supplyId);
        } catch (err) {
          introduceButton.disabled = false;
          markingMessage(shell, err.message, 'error');
        }
      });
    }
  }

  const sendWbAllButton = content.querySelector('[data-marking-send-wb-all]');
  if (sendWbAllButton && sendWbAllButton.dataset.boundMarkingSendWbAll !== '1') {
    sendWbAllButton.dataset.boundMarkingSendWbAll = '1';
    sendWbAllButton.addEventListener('click', async () => {
      const confirmed = window.confirm(
        'Передать все готовые КИЗы этой поставки в WB?\n\n' +
        'Передача доступна после подтвержденного ввода кодов в оборот.'
      );
      if (!confirmed) return;
      sendWbAllButton.disabled = true;
      markingMessage(shell, 'Передаю точные строки КИЗ в WB и проверяю метаданные заданий…', 'ok');
      try {
        let result = await markingApiRequest(
          `/api/supplies/${encodeURIComponent(supplyId)}/marking/send-wb-all/start`,
          { method: 'POST', body: '{}' }
        );
        if (result.job_id) {
          result = await followBackgroundMarkingJob(shell, result.job_id, 'Передаю КИЗы WB в фоне…');
        }
        await refreshMarkingShell(shell, 'КИЗы переданы. Получаю подтверждение WB…');
        const kind = result.errors?.length ? 'error' : 'ok';
        markingMessage(shell, result.message || 'Передача КИЗов в WB завершена.', kind);
      } catch (err) {
        sendWbAllButton.disabled = false;
        markingMessage(shell, err.message, 'error');
      }
    });
  }

  const suzBlock = content.matches?.('[data-suz-block]') ? content : content.querySelector('[data-suz-block]');
  if (suzBlock) {
    const loadCertificatesButton = suzBlock.querySelector('[data-suz-load-certificates]');
    const certificateSelect = suzBlock.querySelector('[data-suz-certificate-select]');
    const saveCertificateButton = suzBlock.querySelector('[data-suz-save-certificate]');
    const testButton = suzBlock.querySelector('[data-suz-test]');
    const createButton = suzBlock.querySelector('[data-suz-create-order]');
    const reserveInput = suzBlock.querySelector('[data-suz-reserve]');
    const syncAllButton = suzBlock.querySelector('[data-suz-sync-all]');

    if (loadCertificatesButton && loadCertificatesButton.dataset.boundSuzCertLoad !== '1') {
      loadCertificatesButton.dataset.boundSuzCertLoad = '1';
      loadCertificatesButton.addEventListener('click', async () => {
        loadCertificatesButton.disabled = true;
        markingMessage(shell, 'Ищу сертификаты с закрытым ключом в Windows…', 'ok');
        try {
          const result = await markingApiRequest('/api/suz/certificates');
          const certificates = result.certificates || [];
          if (!certificateSelect) return;
          certificateSelect.replaceChildren();
          if (!certificates.length) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = 'Подходящие сертификаты не найдены';
            certificateSelect.append(option);
            certificateSelect.disabled = true;
            if (saveCertificateButton) saveCertificateButton.disabled = true;
            markingMessage(shell, 'Не найден действующий сертификат с закрытым ключом.', 'error');
            return;
          }
          certificates.forEach(cert => {
            const option = document.createElement('option');
            option.value = cert.thumbprint || '';
            const subject = cert.subject || 'Сертификат';
            const expires = cert.not_after ? `до ${cert.not_after.slice(0, 10)}` : '';
            option.textContent = `${subject} · ${expires} · ${(cert.thumbprint || '').slice(-12)}`;
            certificateSelect.append(option);
          });
          certificateSelect.disabled = false;
          if (saveCertificateButton) saveCertificateButton.disabled = false;
          markingMessage(shell, `Найдено сертификатов: ${certificates.length}`, 'ok');
        } catch (err) {
          markingMessage(shell, err.message, 'error');
        } finally {
          loadCertificatesButton.disabled = false;
        }
      });
    }

    if (saveCertificateButton && saveCertificateButton.dataset.boundSuzCertSave !== '1') {
      saveCertificateButton.dataset.boundSuzCertSave = '1';
      saveCertificateButton.addEventListener('click', async () => {
        const thumbprint = certificateSelect?.value || '';
        if (!thumbprint) {
          markingMessage(shell, 'Выберите сертификат.', 'error');
          return;
        }
        saveCertificateButton.disabled = true;
        try {
          const result = await markingApiRequest('/api/suz/certificate', {
            method: 'POST',
            body: JSON.stringify({ thumbprint })
          });
          await refreshMarkingShell(shell, 'Сохраняю сертификат УКЭП…');
          markingMessage(shell, result.message || 'Сертификат сохранен.', 'ok');
        } catch (err) {
          saveCertificateButton.disabled = false;
          markingMessage(shell, err.message, 'error');
        }
      });
    }

    if (testButton && testButton.dataset.boundSuzTest !== '1') {
      testButton.dataset.boundSuzTest = '1';
      testButton.addEventListener('click', async () => {
        testButton.disabled = true;
        markingMessage(shell, 'Запрашиваю challenge, подписываю УКЭП и проверяю OMS…', 'ok');
        try {
          const result = await markingApiRequest('/api/suz/test', { method: 'POST', body: '{}' });
          const versions = [result.api_version, result.oms_version].filter(Boolean).join(' / ');
          const successMessage = `${result.message || 'Подключение подтверждено'}${versions ? ` · ${versions}` : ''}`;
          await refreshMarkingShell(shell, 'Подключение подтверждено. Обновляю доступность заказа…');
          markingMessage(shell, successMessage, 'ok');
        } catch (err) {
          markingMessage(shell, err.message, 'error');
        } finally {
          testButton.disabled = false;
        }
      });
    }

    if (reserveInput && reserveInput.dataset.boundSuzReservePlan !== '1') {
      reserveInput.dataset.boundSuzReservePlan = '1';
      const updatePlan = () => {
        const reserve = Math.max(0, Math.min(10, Number(reserveInput.value || 0)));
        let total = 0;
        suzBlock.querySelectorAll('[data-order-plan-group]').forEach(node => {
          const amount = Number(node.dataset.base || 0) + reserve * Number(node.dataset.gtins || 0);
          node.textContent = String(amount);
          total += amount;
        });
        const totalNode = suzBlock.querySelector('[data-order-plan-total]');
        if (totalNode) totalNode.textContent = String(total);
        const balanceDemand = suzBlock.querySelector('[data-marking-balance-demand]');
        if (balanceDemand) balanceDemand.textContent = `${total} КИЗ`;
      };
      reserveInput.addEventListener('input', updatePlan);
      updatePlan();
    }

    if (createButton && createButton.dataset.boundSuzCreate !== '1') {
      createButton.dataset.boundSuzCreate = '1';
      createButton.addEventListener('click', async () => {
        const reserve = Number(reserveInput?.value || 0);
        const paymentType = Number(suzBlock.dataset.suzPaymentType || 1);
        const perfume = Number(suzBlock.querySelector('[data-order-plan-group="perfumery"]')?.textContent || 0);
        const chemistry = Number(suzBlock.querySelector('[data-order-plan-group="chemistry"]')?.textContent || 0);
        const confirmed = window.confirm(
          'Заказать коды маркировки в Честном Знаке?\n\n' +
          `Парфюмерия — ${perfume}\n` +
          `Дезодоранты — ${chemistry}\n` +
          `Резерв на каждый GTIN — ${reserve}\n\n` +
          'После отправки FBE сам будет показывать PENDING → ACTIVE → получено X/Y и забирать появляющиеся коды.'
        );
        if (!confirmed) return;
        createButton.disabled = true;
        markingMessage(shell, 'Создаю заказ СУЗ…', 'ok');
        try {
          const started = await markingApiRequest(`/api/supplies/${encodeURIComponent(supplyId)}/suz/order/start`, {
            method: 'POST',
            body: JSON.stringify({ reserve_per_gtin: reserve, payment_type: paymentType })
          });
          await followBackgroundMarkingJob(shell, started.job_id, 'Пересчитываю полученные КИЗы…');
          ensureMarkingLiveWatcher(shell, supplyId);
        } catch (err) {
          createButton.disabled = false;
          markingMessage(shell, err.message, 'error');
        }
      });
    }

    if (syncAllButton && syncAllButton.dataset.boundSuzSyncAll !== '1') {
      syncAllButton.dataset.boundSuzSyncAll = '1';
      syncAllButton.addEventListener('click', async () => {
        syncAllButton.disabled = true;
        markingMessage(shell, 'Обновляю статусы заказов СУЗ…', 'ok');
        try {
          const result = await markingApiRequest(
            `/api/supplies/${encodeURIComponent(supplyId)}/suz/orders/sync-all`,
            { method: 'POST', body: '{}' }
          );
          await refreshMarkingShell(shell, 'Коды получены. Пересчитываю свободный пул…');
          markingMessage(shell, result.message || 'Статусы заказов СУЗ обновлены.', result.errors?.length ? 'error' : 'ok');
        } catch (err) {
          syncAllButton.disabled = false;
          markingMessage(shell, err.message, 'error');
        }
      });
    }

    suzBlock.querySelectorAll('[data-suz-sync-order]').forEach(button => {
      if (button.dataset.boundSuzSync === '1') return;
      button.dataset.boundSuzSync = '1';
      button.addEventListener('click', async () => {
        const row = button.closest('[data-suz-order-id]');
        const localOrderId = Number(row?.dataset.suzOrderId || 0);
        if (!localOrderId) return;
        button.disabled = true;
        markingMessage(shell, 'Обновляю статус заказа СУЗ…', 'ok');
        try {
          const result = await markingApiRequest(
            `/api/supplies/${encodeURIComponent(supplyId)}/suz/orders/${localOrderId}/sync`,
            { method: 'POST', body: '{}' }
          );
          await refreshMarkingShell(shell, 'Коды получены. Пересчитываю свободный пул…');
          markingMessage(shell, result.message || 'Статус заказа СУЗ обновлен.', 'ok');
        } catch (err) {
          button.disabled = false;
          markingMessage(shell, err.message, 'error');
        }
      });
    });
  }
  if (Number(content.dataset.markingProcessing || 0) > 0) {
    ensureMarkingLiveWatcher(shell, supplyId);
  }
}

function initMarkingNeeds(root = document) {
  root.querySelectorAll?.('[data-marking-shell]').forEach(shell => {
    bindMarkingWorkspace(shell);
    if (shell.dataset.boundMarking === '1') return;
    shell.dataset.boundMarking = '1';
    const modal = shell.closest('.supply-tool-modal');
    const refreshButton = modal?.querySelector('[data-marking-refresh]');
    if (!refreshButton) return;

    refreshButton.addEventListener('click', async () => {
      refreshButton.disabled = true;
      try {
        await refreshMarkingShell(shell);
        markingMessage(shell, 'Потребность, заказы СУЗ и печатный контур обновлены.', 'ok');
      } catch (err) {
        markingMessage(shell, `Не удалось обновить Честный Знак: ${err.message}`, 'error');
      } finally {
        refreshButton.disabled = false;
      }
    });
  });
}

document.addEventListener('DOMContentLoaded', () => initMarkingNeeds(document));


// FBE 0.82.0: compact supply history switcher.
document.addEventListener('DOMContentLoaded', () => {
  const tabs = Array.from(document.querySelectorAll('[data-supply-view]'));
  if (!tabs.length) return;
  const panels = Array.from(document.querySelectorAll('[data-supply-view-panel]'));
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.supplyView;
      tabs.forEach(item => {
        const active = item === tab;
        item.classList.toggle('active', active);
        item.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      panels.forEach(panel => {
        const active = panel.dataset.supplyViewPanel === target;
        panel.classList.toggle('active', active);
        panel.hidden = !active;
      });
    });
  });
});

// FBE 0.82.0: small period switch inside "Передано WB".
document.addEventListener('DOMContentLoaded', () => {
  const tabs = Array.from(document.querySelectorAll('[data-history-range]'));
  if (!tabs.length) return;
  const panels = Array.from(document.querySelectorAll('[data-history-panel]'));
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.historyRange;
      tabs.forEach(item => item.classList.toggle('active', item === tab));
      panels.forEach(panel => { panel.hidden = panel.dataset.historyPanel !== target; });
    });
  });
});

// FBE 0.83.1: guarded multi-supply transfer wizard.
document.addEventListener('DOMContentLoaded', () => {
  const modal = document.getElementById('transfer-supplies-modal');
  const openButton = document.querySelector('[data-transfer-supplies-open]');
  if (!modal || !openButton) return;

  const state = { selectedIds: [], cargoReadyIds: [], deliveredIds: [] };
  const selectedBoxes = () => Array.from(document.querySelectorAll('[data-transfer-supply-checkbox]:checked'));
  const selectedIds = () => selectedBoxes().map(box => String(box.value || '').trim()).filter(Boolean);
  const esc = value => String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));

  async function waitTransferJob(started, target, loadingText) {
    if (!started?.job_id) return started || {};
    while (true) {
      const job = await markingApiRequest(`/api/jobs/${encodeURIComponent(started.job_id)}`);
      const state = String(job.state || 'running');
      target.innerHTML = `<div class="transfer-loading">${esc(job.message || loadingText || 'Операция выполняется…')}</div>`;
      if (state === 'done' || state === 'error') {
        if (state === 'error') throw new Error(job.error || job.message || 'Фоновая операция завершилась ошибкой');
        return job.result || job;
      }
      await new Promise(resolve => setTimeout(resolve, 650));
    }
  }

  function setStage(name, visible) {
    const stage = modal.querySelector(`[data-transfer-stage="${name}"]`);
    if (!stage) return;
    stage.hidden = !visible;
    stage.classList.toggle('transfer-stage-active', visible);
  }

  function renderSupplyChecks(rows) {
    return (rows || []).map(row => {
      const blockers = (row.blockers || []).map(x => `<li>${esc(x)}</li>`).join('');
      const warnings = (row.warnings || []).map(x => `<li>${esc(x)}</li>`).join('');
      return `<article class="transfer-result-card ${row.ready ? 'ok' : 'error'}">
        <div class="transfer-result-head"><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code></div>
        <div class="transfer-result-meta">${esc(row.order_count)} заказов · грузомест уже создано: ${esc(row.existing_cargo_count || 0)}</div>
        ${blockers ? `<ul class="transfer-error-list">${blockers}</ul>` : '<div class="transfer-ok-line">WB-проверка пройдена</div>'}
        ${warnings ? `<ul class="transfer-warning-list">${warnings}</ul>` : ''}
      </article>`;
    }).join('');
  }

  function openModal() {
    const ids = selectedIds();
    if (!ids.length) {
      window.alert('Выберите одну или несколько поставок на сборке.');
      return;
    }
    state.selectedIds = ids;
    state.cargoReadyIds = [];
    state.deliveredIds = [];
    // Use the same modal lifecycle as the rest of FBE. Merely clearing `hidden`
    // leaves .supply-tool-modal at opacity:0/pointer-events:none until .is-open is added.
    openSupplyToolModal('#transfer-supplies-modal');
    modal.querySelector('[data-transfer-selected-summary]').textContent = `Выбрано поставок: ${ids.length}`;
    modal.querySelector('[data-transfer-confirm-assembled]').checked = false;
    modal.querySelector('[data-transfer-confirm-labels]').checked = false;
    modal.querySelector('[data-transfer-preflight-results]').innerHTML = '';
    modal.querySelector('[data-transfer-cargo-results]').innerHTML = '';
    modal.querySelector('[data-transfer-deliver-results]').innerHTML = '';
    modal.querySelector('[data-transfer-supply-qr-results]').innerHTML = '';
    setStage('preflight', true);
    setStage('cargo', false);
    setStage('deliver', false);
    setStage('supply-qr', false);
  }

  function closeModal() {
    closeSupplyToolModal(modal);
  }

  openButton.addEventListener('click', openModal);
  modal.querySelectorAll('[data-transfer-supplies-close]').forEach(button => button.addEventListener('click', closeModal));

  const selectAll = document.querySelector('[data-transfer-select-all]');
  if (selectAll) {
    selectAll.addEventListener('change', () => {
      document.querySelectorAll('[data-transfer-supply-checkbox]').forEach(box => { box.checked = selectAll.checked; });
    });
  }
  document.querySelectorAll('[data-transfer-supply-checkbox]').forEach(box => {
    box.addEventListener('change', () => {
      const boxes = Array.from(document.querySelectorAll('[data-transfer-supply-checkbox]'));
      if (selectAll) {
        selectAll.checked = boxes.length > 0 && boxes.every(item => item.checked);
        selectAll.indeterminate = boxes.some(item => item.checked) && !selectAll.checked;
      }
    });
  });

  const preflightButton = modal.querySelector('[data-transfer-preflight]');
  preflightButton.addEventListener('click', async () => {
    const confirmed = modal.querySelector('[data-transfer-confirm-assembled]').checked;
    const target = modal.querySelector('[data-transfer-preflight-results]');
    preflightButton.disabled = true;
      target.innerHTML = '<div class="transfer-loading">Проверяю выбранные поставки непосредственно у WB…</div>';
    try {
      const started = await markingApiRequest('/api/supplies/transfer/preflight/start', {
        method: 'POST',
        body: JSON.stringify({
          supply_ids: state.selectedIds,
          confirmed_assembled: confirmed,
        }),
      });
      const result = await waitTransferJob(started, target, 'Проверяю выбранные поставки непосредственно у WB…');
      target.innerHTML = renderSupplyChecks(result.supplies || []);
      if (result.all_ready) {
        const cargoGrid = modal.querySelector('[data-transfer-cargo-grid]');
        cargoGrid.innerHTML = (result.supplies || []).map(row => {
          const existing = Number(row.existing_cargo_count || 0);
          const maxCargo = Math.max(1, Number(row.max_cargo || 1));
          const initial = Math.max(1, existing);
          return `<label class="transfer-cargo-row">
            <span><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code><small>${esc(row.order_count)} заказов${existing ? ` · уже ${existing} грузомест` : ''} · максимум WB: ${maxCargo}</small></span>
            <input type="number" min="${Math.max(1, existing)}" max="${maxCargo}" value="${initial}" data-transfer-cargo-input data-supply-id="${esc(row.supply_id)}">
          </label>`;
        }).join('');
        setStage('cargo', true);
        modal.querySelector('[data-transfer-stage="cargo"]').scrollIntoView({behavior: 'smooth', block: 'nearest'});
      } else {
        setStage('cargo', false);
        setStage('deliver', false);
      }
    } catch (error) {
      target.innerHTML = `<div class="transfer-error-box">${esc(error.message)}</div>`;
    } finally {
      preflightButton.disabled = false;
    }
  });

  const cargoButton = modal.querySelector('[data-transfer-create-cargo]');
  cargoButton.addEventListener('click', async () => {
    const inputs = Array.from(modal.querySelectorAll('[data-transfer-cargo-input]'));
    const items = inputs.map(input => ({
      supply_id: input.dataset.supplyId,
      amount: Number(input.value || 0),
    }));
    const target = modal.querySelector('[data-transfer-cargo-results]');
    cargoButton.disabled = true;
    target.innerHTML = '<div class="transfer-loading">Создаю грузоместа, забираю QR по одному и сохраняю локальные копии…</div>';
    try {
      const started = await markingApiRequest('/api/supplies/transfer/cargo/start', {
        method: 'POST',
        body: JSON.stringify({items}),
      });
      const result = await waitTransferJob(started, target, 'Создаю грузоместа, забираю QR по одному и сохраняю локальные копии…');
      state.cargoReadyIds = (result.results || []).filter(row => row.ok).map(row => row.supply_id);
      target.innerHTML = (result.results || []).map(row => row.ok
        ? `<article class="transfer-result-card ok"><div class="transfer-result-head"><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code></div><div class="transfer-ok-line">QR грузомест сохранены: ${esc(row.cargo_count)} шт.</div><a class="button-link transfer-pdf-link" href="${esc(row.pdf_url)}" target="_blank">Открыть PDF грузомест</a><small>Первая этикетка PDF — название и код этой поставки.</small></article>`
        : `<article class="transfer-result-card error"><div class="transfer-result-head"><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code></div><div class="transfer-error-box">${esc(row.error || 'Ошибка')}</div></article>`
      ).join('') + (result.batch_pdf_url ? `<a class="button-link transfer-pdf-link transfer-batch-link" href="${esc(result.batch_pdf_url)}" target="_blank">Открыть ОДИН PDF всех грузомест</a><small class="transfer-batch-note">В нем каждая поставка начинается с собственной разделительной этикетки, затем идут только ее QR.</small>` : '');
      if (state.cargoReadyIds.length) {
        setStage('deliver', true);
        modal.querySelector('[data-transfer-stage="deliver"]').scrollIntoView({behavior: 'smooth', block: 'nearest'});
      }
    } catch (error) {
      target.innerHTML = `<div class="transfer-error-box">${esc(error.message)}</div>`;
    } finally {
      cargoButton.disabled = false;
    }
  });

  const deliverButton = modal.querySelector('[data-transfer-deliver]');
  deliverButton.addEventListener('click', async () => {
    const confirmed = modal.querySelector('[data-transfer-confirm-labels]').checked;
    const target = modal.querySelector('[data-transfer-deliver-results]');
    deliverButton.disabled = true;
    target.innerHTML = '<div class="transfer-loading">Передаю выбранные поставки WB…</div>';
    try {
      const started = await markingApiRequest('/api/supplies/transfer/deliver/start', {
        method: 'POST',
        body: JSON.stringify({
          supply_ids: state.cargoReadyIds,
          confirmed_labels_applied: confirmed,
        }),
      });
      const result = await waitTransferJob(started, target, 'Передаю выбранные поставки WB…');
      state.deliveredIds = (result.results || []).filter(row => row.ok).map(row => row.supply_id);
      target.innerHTML = (result.results || []).map(row => row.ok
        ? `<article class="transfer-result-card ok"><div class="transfer-result-head"><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code></div><div class="transfer-ok-line">Передано WB</div></article>`
        : `<article class="transfer-result-card error"><div class="transfer-result-head"><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code></div><div class="transfer-error-box">${esc(row.error || 'Ошибка передачи')}</div></article>`
      ).join('');
      if (state.deliveredIds.length) {
        setStage('supply-qr', true);
        modal.querySelector('[data-transfer-stage="supply-qr"]').scrollIntoView({behavior: 'smooth', block: 'nearest'});
      }
    } catch (error) {
      target.innerHTML = `<div class="transfer-error-box">${esc(error.message)}</div>`;
    } finally {
      deliverButton.disabled = false;
    }
  });

  const supplyQrButton = modal.querySelector('[data-transfer-supply-qr]');
  supplyQrButton.addEventListener('click', async () => {
    const target = modal.querySelector('[data-transfer-supply-qr-results]');
    supplyQrButton.disabled = true;
    target.innerHTML = '<div class="transfer-loading">Получаю и сохраняю финальные QR-коды поставок…</div>';
    try {
      const started = await markingApiRequest('/api/supplies/transfer/supply-qr/start', {
        method: 'POST',
        body: JSON.stringify({supply_ids: state.deliveredIds}),
      });
      const result = await waitTransferJob(started, target, 'Получаю и сохраняю финальные QR-коды поставок…');
      target.innerHTML = (result.results || []).map(row => row.ok
        ? `<article class="transfer-result-card ok"><div class="transfer-result-head"><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code></div><div class="transfer-ok-line">QR поставки сохранен</div><a class="button-link transfer-pdf-link" href="${esc(row.qr_url)}" target="_blank">Открыть QR поставки</a></article>`
        : `<article class="transfer-result-card error"><div class="transfer-result-head"><b>${esc(row.name || row.supply_id)}</b><code>${esc(row.supply_id)}</code></div><div class="transfer-error-box">${esc(row.error || 'QR пока недоступен')}</div></article>`
      ).join('') + (result.batch_pdf_url ? `<a class="button-link transfer-pdf-link transfer-batch-link" href="${esc(result.batch_pdf_url)}" target="_blank">Открыть ОДИН PDF QR поставок</a>` : '') + (result.all_ok ? '<a class="button-link transfer-refresh-link" href="/?refresh=1">Готово — обновить список поставок</a>' : '');
    } catch (error) {
      target.innerHTML = `<div class="transfer-error-box">${esc(error.message)}</div>`;
    } finally {
      supplyQrButton.disabled = false;
    }
  });
});

// FBE 0.83.12: on a cold start do not require a manual reload after the
// background WB supply synchronization finishes. The server hides stale rows;
// this lightweight poll reloads once the persisted supply registry is fresh.
document.addEventListener('DOMContentLoaded', () => {
  if (document.body?.dataset?.fbsSyncPending !== '1') return;
  const notice = document.querySelector('[data-fbs-sync-notice]');
  let attempts = 0;
  const maxAttempts = 30;

  const poll = async () => {
    attempts += 1;
    try {
      const response = await fetch('/api/fbs/operational-state', {
        headers: {'Accept': 'application/json'},
        cache: 'no-store',
      });
      const state = await response.json().catch(() => ({}));
      if (response.ok && state.fresh) {
        window.location.reload();
        return;
      }
      if (notice && !state.refreshing && attempts > 3) {
        const retryIn = Number(state.retry_in_seconds || 0);
        notice.querySelector('span:last-child').textContent = retryIn > 0
          ? `WB пока не вернул актуальное состояние. Следующая попытка через ${retryIn} с; старые поставки скрыты.`
          : 'WB пока не вернул актуальное состояние поставок. Запускаю следующую синхронизацию…';
      }
    } catch (_) {
      if (notice && attempts > 3) {
        notice.querySelector('span:last-child').textContent =
          'Связь с WB временно недоступна. Старые поставки не показываю; повторяю проверку…';
      }
    }

    if (attempts < maxAttempts) {
      window.setTimeout(poll, Math.min(2500, 900 + attempts * 100));
    } else if (notice) {
      const spinner = notice.querySelector('.supply-sync-spinner');
      if (spinner) spinner.remove();
      notice.querySelector('span:last-child').textContent =
        'Не удалось подтвердить актуальность поставок WB. Обновите страницу после восстановления связи.';
    }
  };

  window.setTimeout(poll, 500);
});
