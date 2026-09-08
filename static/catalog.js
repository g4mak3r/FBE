const qs = (selector, root = document) => root.querySelector(selector);
const qsa = (selector, root = document) => [...root.querySelectorAll(selector)];

function toast(message, kind = 'ok') {
  const node = qs('[data-catalog-toast]');
  if (!node) return;
  node.textContent = message;
  node.className = `catalog-toast ${kind}`;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 4500);
}

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  let data = {};
  try { data = await response.json(); } catch (_) {}
  if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function openDialog(dialog) {
  if (dialog && !dialog.open) dialog.showModal();
}

function closeDialog(dialog) {
  if (dialog?.open) dialog.close();
}

qsa('[data-close-dialog]').forEach(button => {
  button.addEventListener('click', () => closeDialog(button.closest('dialog')));
});

const productDialog = qs('#product-dialog');
const productForm = qs('#product-form');
let currentTechnical = {};

function normalized(value) {
  return String(value || '').trim().toLowerCase().replaceAll('ё', 'е');
}

function normalizeMarkingProfile(value) {
  const text = String(value || '').trim().toUpperCase();
  const aliases = {
    'АВТОМАТИЧЕСКИ': 'AUTO', 'ПАРФЮМЕРИЯ': 'PERFUMERY',
    'ДЕЗОДОРАНТЫ И КОСМЕТИКА': 'CHEMISTRY', 'ДЕЗОДОРАНТЫ': 'CHEMISTRY',
    'БЕЗ МАРКИРОВКИ': 'NONE', 'НЕ ТРЕБУЕТ МАРКИРОВКИ': 'NONE', 'НЕ ТРЕБУЕТСЯ МАРКИРОВКА': 'NONE', 'ПОЛЬЗОВАТЕЛЬСКИЙ ПРОФИЛЬ': 'CUSTOM',
  };
  return aliases[text] || text;
}

function inferMarkingProfile(category, subcategory, product, existing = {}, requested = 'AUTO', typedTnved = '') {
  const text = `${category || ''} ${subcategory || ''} ${product || ''}`.toLowerCase().replaceAll('ё', 'е');
  const categoryText = normalized(category);
  const profileCode = normalizeMarkingProfile(requested);
  const result = {
    profile: profileCode ? 'Не определен' : 'Выберите профиль',
    gtin: existing.gtin || '',
    group: existing.product_group || '',
    templateId: Number(existing.template_id || 0),
    cisType: existing.cis_type || 'UNIT',
    tnved: String(typedTnved || existing.tnved_code || '').trim(),
    kiz: Boolean(existing.kiz_required),
  };

  const apply = code => {
    if (code === 'PERFUMERY') {
      result.profile = 'Парфюмерия'; result.group = 'perfumery'; result.templateId = 9; result.cisType = 'UNIT'; result.kiz = true;
      const subcategoryText = normalized(subcategory);
      const productText = normalized(product);
      if (!result.tnved) {
        if (subcategoryText.includes('духи масляные') || subcategoryText === 'духи' || productText.startsWith('духи масляные')) result.tnved = '3303001000';
        else if (subcategoryText.includes('туалетная вода') || productText.startsWith('туалетная вода')) result.tnved = '3303009000';
      }
    } else if (code === 'CHEMISTRY') {
      result.profile = 'Дезодоранты и косметика'; result.group = 'chemistry'; result.templateId = 46; result.cisType = 'UNIT'; result.kiz = true;
    } else if (code === 'NONE') {
      result.profile = 'Не требует маркировки'; result.group = ''; result.templateId = 0; result.cisType = 'UNIT'; result.kiz = false;
    }
    return result;
  };

  if (!profileCode) return result;
  if (['PERFUMERY', 'CHEMISTRY', 'NONE'].includes(profileCode)) return apply(profileCode);
  if (profileCode === 'CUSTOM') { result.profile = 'Пользовательский профиль'; return result; }

  const subcategoryText = normalized(subcategory);
  const productText = normalized(product);
  if (text.includes('дезодорант') || categoryText === 'дезодоранты') return apply('CHEMISTRY');
  const perfumeSubcategory = subcategoryText.includes('духи')
    || subcategoryText.includes('туалетная вода')
    || subcategoryText.includes('парфюмерная вода');
  const perfumeProductFallback = !subcategoryText && (
    productText.startsWith('духи ') || productText.startsWith('духи масляные')
    || productText.startsWith('туалетная вода') || productText.startsWith('парфюмерная вода')
  );
  if (categoryText === 'парфюмерия' || perfumeSubcategory || perfumeProductFallback) return apply('PERFUMERY');
  if (result.group || result.templateId || result.kiz) result.profile = 'Пользовательский профиль';
  return result;
}

function updateTechnicalSummary() {
  if (!productForm) return;
  const profile = inferMarkingProfile(
    productForm.elements.category?.value,
    productForm.elements.subcategory?.value,
    productForm.elements.product?.value,
    currentTechnical,
    productForm.elements.marking_profile?.value,
    productForm.elements.tnved_code?.value,
  );
  qs('[data-tech-profile]').textContent = profile.profile;
  qs('[data-tech-group]').textContent = profile.group || '—';
  qs('[data-tech-template]').textContent = profile.group ? `${profile.templateId} / ${profile.cisType}` : '—';
  qs('[data-tech-kiz]').textContent = profile.kiz ? 'КИЗ обязателен' : 'Не маркируется';
  if (productForm.elements.tnved_code && !productForm.elements.tnved_code.value && profile.tnved) {
    productForm.elements.tnved_code.placeholder = profile.tnved;
  }
}

function financeNumber(name) {
  const value = String(productForm?.elements[name]?.value || '').replace(',', '.').replace(/\s/g, '');
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : 0;
}

function formatRub(value) {
  return `${new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 }).format(value)} ₽`;
}

function updateFinanceSummary() {
  if (!productForm) return;
  const field = productForm.elements.cost_price_rub;
  if (!field) return;
  const value = String(field.value || '').replace(',', '.').replace(/\s/g, '');
  if (value && !Number.isFinite(Number(value))) field.setCustomValidity('Введите себестоимость числом');
  else field.setCustomValidity('');
}

function resetProductForm() {
  productForm.reset();
  productForm.elements.original_article.value = '';
  productForm.elements.active.checked = true;
  currentTechnical = {};
  productForm.elements.marking_profile.value = '';
  qs('[data-product-dialog-title]').textContent = 'Новый товар';
  updateTechnicalSummary();
  updateFinanceSummary();
}

['category', 'subcategory', 'product', 'marking_profile', 'gtin', 'tnved_code'].forEach(name => {
  productForm?.elements[name]?.addEventListener('input', updateTechnicalSummary);
});

productForm?.elements.cost_price_rub?.addEventListener('input', updateFinanceSummary);

qs('[data-new-product]')?.addEventListener('click', () => {
  resetProductForm();
  openDialog(productDialog);
});

qsa('[data-edit-product]').forEach(button => {
  button.addEventListener('click', async () => {
    try {
      const article = button.dataset.editProduct;
      const data = await jsonRequest(`/api/catalog/products/${encodeURIComponent(article)}`);
      resetProductForm();
      const product = data.product;
      currentTechnical = product;
      productForm.elements.original_article.value = product.seller_article || '';
      Object.entries(product).forEach(([key, value]) => {
        const field = productForm.elements[key];
        if (!field) return;
        if (field.type === 'checkbox') field.checked = Boolean(value);
        else field.value = value ?? '';
      });
      qs('[data-product-dialog-title]').textContent = product.seller_article;
      updateTechnicalSummary();
      updateFinanceSummary();
      openDialog(productDialog);
    } catch (error) { toast(error.message, 'error'); }
  });
});

productForm?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = new FormData(productForm);
  const payload = Object.fromEntries(form.entries());
  payload.active = productForm.elements.active.checked;
  try {
    await jsonRequest('/api/catalog/products', { method: 'POST', body: JSON.stringify(payload) });
    closeDialog(productDialog);
    toast('Товар сохранен');
    setTimeout(() => location.reload(), 350);
  } catch (error) { toast(error.message, 'error'); }
});

qsa('[data-toggle-active]').forEach(button => {
  button.addEventListener('click', async () => {
    const article = button.dataset.toggleActive;
    const current = button.dataset.active === '1';
    const action = current ? 'архивировать' : 'восстановить';
    if (!confirm(`Точно ${action} ${article}?`)) return;
    try {
      await jsonRequest(`/api/catalog/products/${encodeURIComponent(article)}/active`, {
        method: 'POST', body: JSON.stringify({ active: !current }),
      });
      location.reload();
    } catch (error) { toast(error.message, 'error'); }
  });
});

function selectedArticles() {
  return qsa('[data-product-select]:checked').map(cb => cb.value);
}

function syncSelection() {
  const selected = selectedArticles();
  qs('[data-selected-count]').textContent = selected.length;
  const bulk = qs('[data-open-bulk]');
  if (bulk) bulk.disabled = selected.length === 0;
  const master = qs('[data-master-select]');
  const all = qsa('[data-product-select]');
  if (master) {
    master.checked = all.length > 0 && selected.length === all.length;
    master.indeterminate = selected.length > 0 && selected.length < all.length;
  }
}

qsa('[data-product-select]').forEach(cb => cb.addEventListener('change', syncSelection));
qs('[data-master-select]')?.addEventListener('change', event => {
  qsa('[data-product-select]').forEach(cb => { cb.checked = event.target.checked; });
  syncSelection();
});
qs('[data-select-visible]')?.addEventListener('click', () => {
  qsa('[data-product-select]').forEach(cb => { cb.checked = true; });
  syncSelection();
});

const bulkDialog = qs('#bulk-dialog');
const bulkForm = qs('#bulk-form');
qs('[data-open-bulk]')?.addEventListener('click', () => {
  qs('[data-bulk-count]').textContent = selectedArticles().length;
  openDialog(bulkDialog);
});

bulkForm?.addEventListener('submit', async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(bulkForm).entries());
  try {
    const data = await jsonRequest('/api/catalog/bulk-compliance', {
      method: 'POST', body: JSON.stringify({ articles: selectedArticles(), values }),
    });
    closeDialog(bulkDialog);
    toast(`Обновлено товаров: ${data.updated}`);
    setTimeout(() => location.reload(), 400);
  } catch (error) { toast(error.message, 'error'); }
});

qs('.catalog-import-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = qs('button[type="submit"]', form);
  button.disabled = true;
  button.textContent = 'Импортирую…';
  try {
    const response = await fetch('/catalog/import', { method: 'POST', body: new FormData(form) });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || `HTTP ${response.status}`);
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const warningText = warnings.length ? `; предупреждений: ${warnings.length}` : '';
    toast(`Импортировано товаров: ${data.products_written}; переходников: ${data.samples_written}; именных ароматов: ${data.names_written || 0}; форматов: ${data.formats_written || 0}${warningText}`, warnings.length ? 'warn' : 'ok');
    if (warnings.length) console.warn('FBE catalog import warnings:', warnings);
    setTimeout(() => location.reload(), 700);
  } catch (error) {
    toast(error.message, 'error');
    button.disabled = false;
    button.textContent = 'Импортировать';
  }
});

qs('[data-backup-db]')?.addEventListener('click', async () => {
  try {
    const data = await jsonRequest('/api/catalog/backup', { method: 'POST', body: '{}' });
    toast(`Резервная копия создана: ${data.filename}`);
    window.location.href = '/catalog/backup/latest';
  } catch (error) { toast(error.message, 'error'); }
});

const sampleDialog = qs('#sample-dialog');
const sampleForm = qs('#sample-form');
function resetSampleForm() {
  sampleForm.reset();
  sampleForm.elements.original_name.value = '';
  sampleForm.elements.active.checked = true;
}
qs('[data-new-sample]')?.addEventListener('click', () => { resetSampleForm(); openDialog(sampleDialog); });
qsa('[data-edit-sample]').forEach(button => {
  button.addEventListener('click', () => {
    resetSampleForm();
    const data = JSON.parse(button.dataset.editSample || '{}');
    sampleForm.elements.original_name.value = data.name || '';
    Object.entries(data).forEach(([key, value]) => {
      const field = sampleForm.elements[key];
      if (!field) return;
      if (field.type === 'checkbox') field.checked = Boolean(value);
      else field.value = value ?? '';
    });
    openDialog(sampleDialog);
  });
});
sampleForm?.addEventListener('submit', async event => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(sampleForm).entries());
  payload.active = sampleForm.elements.active.checked;
  try {
    await jsonRequest('/api/catalog/samples', { method: 'POST', body: JSON.stringify(payload) });
    closeDialog(sampleDialog);
    toast('Аромат сохранен');
    setTimeout(() => location.reload(), 350);
  } catch (error) { toast(error.message, 'error'); }
});

syncSelection();
