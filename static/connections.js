(() => {
  const api = async (url, options = {}) => {
    const response = await fetch(url, { cache: 'no-store', ...options });
    let payload = {};
    try { payload = await response.json(); } catch (_) {}
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || payload.detail || `HTTP ${response.status}`);
    }
    return payload;
  };

  const formBody = (obj) => {
    const body = new URLSearchParams();
    Object.entries(obj).forEach(([key, value]) => body.set(key, value ?? ''));
    return body;
  };

  const restartInto = async (mode, previousInstance) => {
    const overlay = document.createElement('div');
    overlay.className = 'fbe-restart-overlay';
    overlay.innerHTML = '<div class="fbe-restart-card"><strong>Переключаем режим FBE…</strong><span>Локальный сервер перезапускается с отдельным рабочим пространством.</span></div>';
    document.body.appendChild(overlay);
    const started = Date.now();
    while (Date.now() - started < 20000) {
      await new Promise(r => setTimeout(r, 650));
      try {
        const info = await api('/api/fbe-info');
        if (!info.restart_pending && info.instance_id !== previousInstance && (info.mode || (info.mock_mode ? 'demo' : 'real')) === mode) {
          location.href = '/';
          return;
        }
      } catch (_) {}
    }
    overlay.querySelector('strong').textContent = 'Требуется перезапуск FBE';
    overlay.querySelector('span').textContent = 'Закройте сервер и запустите start_fbe.bat снова. Данные профиля сохранены.';
  };

  const build = async () => {
    const brand = document.querySelector('.brand-block');
    if (!brand) return;

    let state;
    try { state = await api('/api/connections/status', {signal: AbortSignal.timeout(12000)}); }
    catch (err) {
      const placeholder = brand.querySelector('.profile-loading');
      if (placeholder) {
        placeholder.textContent = 'Профиль недоступен. ';
        const retry = document.createElement('button');
        retry.type = 'button'; retry.className = 'secondary small-button'; retry.textContent = 'Повторить';
        retry.addEventListener('click', () => { placeholder.textContent = 'Загрузка профиля…'; build(); });
        placeholder.appendChild(retry);
      }
      return;
    }

    brand.querySelector('.profile-loading')?.remove();
    const mode = state.active_mode || state.mode || 'real';
    const wb = state.wb || {};
    const marking = state.marking || {};
    const localTemplates = state.local_templates || {};

    const button = document.createElement('button');
    button.type = 'button';
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.className = `connection-profile-button ${mode === 'demo' ? 'is-demo' : (wb.connected ? 'is-connected' : 'is-disconnected')}`;
    if (mode === 'demo') {
      button.innerHTML = '<span class="connection-dot"></span><span><b>DEMO</b><small>FBE Demo Company</small></span>';
    } else if (wb.connected) {
      const title = wb.name || wb.tradeMark || 'Wildberries';
      const tin = wb.tin ? `ИНН ${wb.tin}` : 'Wildberries подключен';
      button.innerHTML = `<span class="connection-dot"></span><span><b>${escapeHtml(title)}</b><small>${escapeHtml(tin)}</small><small>${localTemplates.pending ? `Шаблоны BarTender: ${localTemplates.count} к переносу` : 'WB подключен'}</small></span>`;
    } else {
      button.innerHTML = '<span class="connection-dot"></span><span><b>Подключить Wildberries</b><small>Профиль компании</small></span>';
    }
    brand.appendChild(button);

    if (mode === 'demo') {
      const demoPill = document.querySelector('[data-shell-mode]');
      if (demoPill) { demoPill.classList.add('is-demo'); demoPill.textContent = 'DEMO MODE · тестовая печать'; }
      document.querySelectorAll('form[method="post"], form[method="POST"]').forEach(form => {
        const action = String(form.getAttribute('action') || '');
        if (action.includes('/marking') || action.includes('/suz')) {
          form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(control => {
            control.disabled = true;
            control.title = 'В DEMO MODE внешние операции Честного Знака отключены';
          });
        }
      });
    }

    const modal = document.createElement('div');
    modal.className = 'connection-modal-backdrop';
    modal.hidden = true;
    modal.innerHTML = `
      <section class="connection-modal" role="dialog" aria-modal="true" aria-label="Подключения FBE">
        <div class="connection-modal-head">
          <div><strong>Подключения FBE</strong><small>Учетные данные хранятся только локально на этом компьютере.</small></div>
          <button type="button" class="connection-modal-close" aria-label="Закрыть">×</button>
        </div>
        <div class="connection-mode-card ${mode === 'demo' ? 'is-demo' : ''}">
          <div><b>${mode === 'demo' ? 'Демонстрационный режим' : 'Рабочий режим'}</b><small>${mode === 'demo' ? 'Синтетические данные. WB / True API / СУЗ не вызываются.' : 'Используются локальные рабочие данные и подключенные API.'}</small></div>
          <button type="button" class="secondary" data-switch-mode="${mode === 'demo' ? 'real' : 'demo'}">${mode === 'demo' ? 'Вернуться в рабочий режим' : 'Включить DEMO'}</button>
        </div>
        <div class="connection-grid">
          <div class="connection-card">
            <div class="connection-card-title"><span>Wildberries</span><span class="connection-status ${wb.connected ? 'ok' : ''}">${wb.connected ? 'Подключен' : 'Не подключен'}</span></div>
            ${wb.connected ? `
              <div class="connection-company"><b>${escapeHtml(wb.name || wb.tradeMark || 'Wildberries')}</b><span>${wb.tin ? `ИНН ${escapeHtml(wb.tin)}` : ''}</span><span>${wb.tradeMark ? `Бренд: ${escapeHtml(wb.tradeMark)}` : ''}</span></div>
              <button type="button" class="secondary" data-wb-check>Проверить соединение</button><button type="button" class="secondary" data-wb-change>Сменить аккаунт</button><form data-wb-connect hidden><label><span>Новый API Token WB</span><input type="password" name="token" autocomplete="off" required></label><button class="primary" type="submit">Подключить Wildberries</button></form><form data-wb-disconnect><button class="destructive" type="submit">Отключить аккаунт</button></form>
            ` : `
              <form data-wb-connect>
                <label><span>API Token WB</span><input type="password" name="token" autocomplete="off" placeholder="Вставьте токен без Bearer" required></label>
                <button class="primary" type="submit">Подключить Wildberries</button>
              </form>
            `}
            <div class="connection-message" role="status" aria-live="polite" data-wb-message></div>
          </div>
          <div class="connection-card">
            <div class="connection-card-title"><span>Честный Знак</span><span class="connection-status ${marking.configured ? 'ok' : ''}">${marking.configured ? 'Настроен' : 'Нужна настройка'}</span></div>
            <form data-marking-connect>
              <label><span>ИНН участника</span><input name="inn" inputmode="numeric" value="${escapeAttr(marking.inn || wb.tin || '')}" placeholder="ИНН ИП / организации"></label>
              <label><span>OMS ID</span><input name="oms_id" autocomplete="off" placeholder="OMS ID"></label>
              <label><span>Connection ID</span><input name="connection_id" autocomplete="off" placeholder="Connection ID"></label>
              <label><span>Отпечаток сертификата УКЭП</span><input name="cert_thumbprint" autocomplete="off" placeholder="Thumbprint (можно выбрать позже в ЧЗ)"></label>
              <button class="secondary" type="submit">Сохранить настройки ЧЗ</button>
            </form>
            <div class="connection-message" role="status" aria-live="polite" data-marking-message></div>
          </div>
          ${localTemplates.pending ? `
          <div class="connection-card is-wide" data-template-import-card>
            <div class="connection-card-title"><span>Шаблоны BarTender</span><span class="connection-status ${localTemplates.can_import ? 'ok' : ''}">${localTemplates.count} найдено</span></div>
            <div class="connection-company">
              <b>Найдены локальные шаблоны из предыдущей установки</b>
              <span>${localTemplates.seller_bound && !localTemplates.seller_match ? 'Они привязаны к другому seller SID и не будут перенесены в этот профиль.' : (wb.connected ? 'Можно безопасно перенести их в текущий WB-профиль. Существующие .btw не перезаписываются.' : 'Сначала подключите Wildberries, чтобы выбрать рабочий профиль.')}</span>
            </div>
            <button type="button" class="secondary" data-import-templates ${localTemplates.can_import ? '' : 'disabled'}>Перенести шаблоны в этот профиль</button>
            <div class="connection-message" role="status" aria-live="polite" data-template-message></div>
          </div>` : ''}
        </div>
      </section>`;
    document.body.appendChild(modal);

    let previousFocus;
    const open = () => {
      previousFocus = document.activeElement;
      modal.hidden = false; document.body.classList.add('connection-modal-open');
      button.setAttribute('aria-expanded', 'true');
      modal.querySelector('.connection-modal-close').focus();
    };
    const close = () => {
      modal.hidden = true; document.body.classList.remove('connection-modal-open');
      button.setAttribute('aria-expanded', 'false');
      (previousFocus || button).focus();
    };
    modal.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
      if (event.key !== 'Tab') return;
      const items = [...modal.querySelectorAll('button, input, select, textarea, a[href], [tabindex="0"]')]
        .filter(el => !el.disabled && el.getClientRects().length);
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    button.addEventListener('click', open);
    modal.querySelector('.connection-modal-close')?.addEventListener('click', close);
    modal.addEventListener('click', (event) => { if (event.target === modal) close(); });

    modal.querySelector('[data-switch-mode]')?.addEventListener('click', async (event) => {
      const target = event.currentTarget.getAttribute('data-switch-mode');
      event.currentTarget.disabled = true;
      try {
        const result = await api('/api/connections/mode', { method: 'POST', headers: {'Content-Type':'application/x-www-form-urlencoded'}, body: formBody({mode: target}) });
        if (result.restart) await restartInto(target, result.previous_instance);
        else location.reload();
      } catch (err) { event.currentTarget.disabled = false; alert(err.message); }
    });

    modal.querySelector('[data-wb-connect]')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const msg = modal.querySelector('[data-wb-message]');
      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true; msg.textContent = 'Проверяем токен и профиль продавца…'; msg.className = 'connection-message';
      try {
        const data = new FormData(form);
        const result = await api('/api/connections/wb', { method:'POST', body: new URLSearchParams(data) });
        msg.textContent = 'Wildberries подключен.'; msg.className = 'connection-message ok';
        form.reset();
        await restartInto('real', result.previous_instance);
      } catch (err) {
        msg.textContent = err.message; msg.className = 'connection-message error'; submit.disabled = false;
      }
    });

    modal.querySelector('[data-wb-disconnect]')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!confirm('Отключить текущий токен Wildberries? Локальные рабочие данные удалены не будут.')) return;
      try { const result = await api('/api/connections/wb/disconnect', {method:'POST'}); await restartInto('real', result.previous_instance); }
      catch (err) { alert(err.message); }
    });

    modal.querySelector('[data-marking-connect]')?.addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const msg = modal.querySelector('[data-marking-message]');
      const data = new FormData(form);
      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true; msg.textContent = 'Сохраняем локальные настройки…'; msg.className = 'connection-message';
      try {
        await api('/api/connections/marking', {method:'POST', body:new URLSearchParams(data)});
        msg.textContent = 'Настройки сохранены локально.'; msg.className = 'connection-message ok';
      } catch (err) { msg.textContent = err.message; msg.className = 'connection-message error'; }
      finally { submit.disabled = false; }
    });

    modal.querySelector('[data-wb-change]')?.addEventListener('click', () => {
      modal.querySelector('[data-wb-connect]').hidden = false;
    });
    modal.querySelector('[data-wb-check]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      const msg = modal.querySelector('[data-wb-message]');
      try { await api('/api/connections/wb/check', {method:'POST'}); msg.textContent = 'Соединение проверено.'; }
      catch (err) { msg.textContent = err.message; }
      finally { button.disabled = false; }
    });
    modal.querySelector('[data-import-templates]')?.addEventListener('click', async (event) => {
      const importButton = event.currentTarget;
      const msg = modal.querySelector('[data-template-message]');
      if (!confirm(`Перенести ${localTemplates.count} локальных шаблонов BarTender в текущий WB-профиль? Существующие шаблоны не будут перезаписаны.`)) return;
      importButton.disabled = true;
      msg.textContent = 'Переносим локальные шаблоны…';
      msg.className = 'connection-message';
      try {
        const response = await api('/api/connections/templates/import', {method:'POST'});
        const result = response.result || {};
        const conflicts = (result.conflicts || []).length;
        const imported = (result.imported || []).length;
        const existing = (result.existing || []).length;
        if (conflicts) {
          msg.textContent = `Перенесено: ${imported}. Уже совпадали: ${existing}. Конфликтов без перезаписи: ${conflicts}.`;
          msg.className = 'connection-message error';
          importButton.disabled = false;
        } else {
          msg.textContent = `Готово. Перенесено: ${imported}; уже были: ${existing}.`;
          msg.className = 'connection-message ok';
          setTimeout(() => location.reload(), 900);
        }
      } catch (err) {
        msg.textContent = err.message;
        msg.className = 'connection-message error';
        importButton.disabled = false;
      }
    });
    if (mode === 'demo') {
      modal.querySelectorAll('.connection-grid input, .connection-grid button').forEach(el => { el.disabled = true; });
    }

    if ((state.first_run || (localTemplates.pending && mode === 'real')) && location.pathname === '/') {
      setTimeout(open, 350);
    }
  };

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const escapeAttr = escapeHtml;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', build);
  else build();
})();
