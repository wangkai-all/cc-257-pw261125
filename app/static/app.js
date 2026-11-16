(function () {
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function show(el, message, ok) {
    if (!el) return;
    el.classList.remove('hidden');
    el.classList.toggle('ok', !!ok);
    el.classList.toggle('bad', !ok);
    el.innerHTML = message;
  }

  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // Tabs
  $$('.tabs').forEach(group => {
    group.addEventListener('click', ev => {
      const tab = ev.target.closest('.tab');
      if (!tab) return;
      $$('.tab', group).forEach(t => t.classList.toggle('active', t === tab));
      const panelId = tab.dataset.tab;
      const scope = group.parentElement;
      $$('.tabpanel', scope).forEach(p => p.classList.toggle('active', p.id === panelId));
    });
  });
  $$('.tabs').forEach(group => {
    const first = $('.tab.active', group) || $('.tab', group);
    if (first) first.click();
  });

  // Dropzone
  const dropzone = $('#dropzone');
  const fileInput = $('#file-input');
  if (dropzone && fileInput) {
    const nameEl = $('#file-name');
    const setFile = file => {
      if (!file) return;
      nameEl.textContent = file.name + ' · ' + file.size + ' bytes';
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
    };
    fileInput.addEventListener('change', () => setFile(fileInput.files[0]));
    ['dragenter', 'dragover'].forEach(ev => dropzone.addEventListener(ev, e => {
      e.preventDefault(); dropzone.classList.add('dragover');
    }));
    ['dragleave', 'drop'].forEach(ev => dropzone.addEventListener(ev, e => {
      e.preventDefault(); dropzone.classList.remove('dragover');
    }));
    dropzone.addEventListener('drop', e => setFile(e.dataTransfer.files[0]));
  }

  // Receive form
  const receiveForm = $('#receive-form');
  if (receiveForm) {
    receiveForm.addEventListener('submit', async ev => {
      ev.preventDefault();
      const resultEl = $('#receive-result');
      const data = new FormData(receiveForm);
      if (!data.get('idempotency_key')) data.delete('idempotency_key');
      show(resultEl, 'Receiving and interpreting…', true);
      try {
        const resp = await fetch('/api/receipts', { method: 'POST', body: data });
        const json = await resp.json();
        if (json.receipt_id) {
          const outcomes = (json.outcomes || []).map(o =>
            `<li><a href="/orders/${esc(o.order_id)}">${esc(o.result)}</a>
             ${o.state_hash ? '<span class="mono muted">' + esc(o.state_hash.slice(0, 12)) + '</span>' : ''}</li>`).join('');
          show(resultEl,
            `<strong>${esc(json.result)}</strong> · ${esc(json.document_number || 'no document')}
             ${json.revision ? 'rev ' + esc(json.revision) : ''}
             ${json.warnings && json.warnings.length ? '<div class="alert alert-warn"><ul>' +
               json.warnings.map(w => '<li>' + esc(w) + '</li>').join('') + '</ul></div>' : ''}
             ${json.error ? '<div class="alert alert-bad">' + esc(json.error) + '</div>' : ''}
             <ul>${outcomes}</ul>
             <a class="btn btn-quiet" href="/receipts/${esc(json.receipt_id)}">Open receipt</a>`,
            !!json.ok);
        } else {
          show(resultEl, `<strong>${esc(json.result || 'error')}</strong> ${esc(json.error || '')}`, !!json.ok);
        }
      } catch (err) {
        show(resultEl, 'Request failed: ' + esc(err), false);
      }
    });
  }

  async function post(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    return { ok: resp.ok, status: resp.status, json: await resp.json() };
  }

  // Order actions
  $$('[data-confirm]').forEach(btn => btn.addEventListener('click', async () => {
    const actor = prompt('Your name, for the confirmation record:');
    if (!actor) return;
    const note = prompt('Confirmation note (optional):') || null;
    const out = await post(`/api/orders/${btn.dataset.confirm}/confirm`, { actor, note });
    if (out.json.ok) {
      location.reload();
    } else {
      const el = $('#order-actions-result');
      show(el, '<strong>Confirmation refused.</strong><ul>' +
        (out.json.reasons || [out.json.error]).map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>', false);
    }
  }));

  $$('[data-reopen]').forEach(btn => btn.addEventListener('click', async () => {
    if (!confirm('Reopen this confirmed order? The confirmation stays in the audit trail.')) return;
    const actor = prompt('Your name:');
    if (!actor) return;
    const out = await post(`/api/orders/${btn.dataset.reopen}/reopen`, { actor });
    if (out.json.ok) location.reload();
    else show($('#order-actions-result'), esc(out.json.error || out.json.result), false);
  }));

  $$('[data-recompute]').forEach(btn => btn.addEventListener('click', async () => {
    const out = await post(`/api/orders/${btn.dataset.recompute}/recompute`, { actor: 'operator' });
    const el = $('#order-actions-result');
    show(el, '<strong>' + esc(out.json.result) + '</strong> ' + esc(out.json.reason || ''), out.ok);
    if (out.ok) setTimeout(() => location.reload(), 700);
  }));

  // Reprocess
  $$('[data-reprocess]').forEach(btn => btn.addEventListener('click', async () => {
    const select = $('#reprocess-mapping');
    const out = await post(`/api/receipts/${btn.dataset.reprocess}/reprocess`,
      { mapping_version_id: select ? Number(select.value) : null, actor: 'operator' });
    btn.disabled = true;
    btn.textContent = out.ok ? 'Re-interpreted' : 'Failed';
    if (out.ok) setTimeout(() => location.reload(), 500);
    else alert(esc(out.json.error || out.json.result || 'failed'));
  }));

  // Mapping actions
  $$('[data-activate-mapping]').forEach(btn => btn.addEventListener('click', async () => {
    const out = await post(`/api/mappings/${btn.dataset.activateMapping}/activate`,
      { reprocess: btn.dataset.reprocess === '1', actor: 'operator' });
    show($('#mapping-result'),
      out.ok ? '<strong>Activated.</strong> ' + (out.json.outcomes || []).length + ' receipts re-interpreted.'
             : '<strong>Failed.</strong> ' + esc(out.json.error), out.ok);
    if (out.ok) setTimeout(() => location.reload(), 700);
  }));

  $$('[data-next-mapping]').forEach(btn => btn.addEventListener('click', async () => {
    const out = await post(`/api/mappings/${btn.dataset.nextMapping}/register`,
      { activate: true, reprocess: true, actor: 'operator' });
    show($('#mapping-result'),
      out.ok ? '<strong>Next version registered and activated.</strong>'
             : '<strong>Failed.</strong> ' + esc(out.json.error), out.ok);
    if (out.ok) setTimeout(() => location.reload(), 900);
  }));

  // Attach unmatched
  $$('form[data-attach]').forEach(form => form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const ref = form.querySelector('input[name=order_reference]').value;
    const out = await post(`/api/unmatched/${form.dataset.attach}/attach`,
      { order_reference: ref, actor: 'operator' });
    if (out.ok) location.reload();
    else alert(esc(out.json.error || 'attach failed'));
  }));

  // Copy buttons
  $$('[data-copy]').forEach(btn => btn.addEventListener('click', async () => {
    const target = $(btn.dataset.copy);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.textContent);
      btn.textContent = 'Copied';
    } catch (err) {
      btn.textContent = 'Copy failed';
    }
  }));
})();
