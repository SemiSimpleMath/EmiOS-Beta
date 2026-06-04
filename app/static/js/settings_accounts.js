/* /settings/accounts — configure modal + per-row mint flow.
   Pairs with POST /api/accounts/<id>/configure. */

(function () {
  const modal = document.getElementById('configure-modal');
  const titleEl = document.getElementById('configure-modal-title');
  const contextEl = document.getElementById('configure-context');
  const secretLabelEl = document.getElementById('configure-secret-label');
  const secretEl = document.getElementById('configure-secret');
  const secretHintEl = document.getElementById('configure-secret-hint');
  const testEl = document.getElementById('configure-test-first');
  const submitEl = document.getElementById('configure-submit');
  const resultEl = document.getElementById('configure-result');

  let activeRow = null;

  function openModal(row) {
    activeRow = row;
    const platform = row.dataset.platform;
    const accountId = row.dataset.accountId;
    const handle = row.dataset.handle;
    const secretLabel = row.dataset.secretLabel || 'Secret';
    const secretHint = row.dataset.secretHint || '';

    titleEl.textContent = `Configure ${platform} — ${accountId}`;
    contextEl.innerHTML = `
      <div><strong>Account:</strong> <code>${escapeHtml(accountId)}</code></div>
      <div><strong>Platform:</strong> ${escapeHtml(platform)}</div>
      <div><strong>Handle:</strong> ${handle ? `<code>${escapeHtml(handle)}</code>` : '<em>(not set)</em>'}</div>
    `;
    secretLabelEl.textContent = secretLabel;
    secretHintEl.textContent = secretHint;
    secretEl.value = '';
    testEl.checked = true;
    resultEl.classList.add('hidden');
    resultEl.textContent = '';
    submitEl.disabled = false;

    modal.classList.remove('hidden');
    setTimeout(() => secretEl.focus(), 50);
  }

  function closeModal() {
    modal.classList.add('hidden');
    secretEl.value = '';
    activeRow = null;
  }

  async function submitConfigure() {
    if (!activeRow) return;
    const accountId = activeRow.dataset.accountId;
    const secret = secretEl.value.trim();
    if (!secret) {
      showResult('error', 'Secret is empty.');
      return;
    }
    submitEl.disabled = true;
    showResult('info', 'Minting pod...');
    try {
      const resp = await fetch(`/api/accounts/${encodeURIComponent(accountId)}/configure`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret, test_first: testEl.checked }),
      });
      const data = await resp.json().catch(() => ({ ok: false, message: 'Server returned non-JSON' }));
      if (!resp.ok || !data.ok) {
        showResult('error', data.message || `HTTP ${resp.status}`);
        submitEl.disabled = false;
        return;
      }
      showResult('success', `${data.message} — reloading...`);
      setTimeout(() => window.location.reload(), 800);
    } catch (e) {
      showResult('error', `Network error: ${e}`);
      submitEl.disabled = false;
    }
  }

  function showResult(kind, msg) {
    resultEl.classList.remove('hidden', 'result-info', 'result-success', 'result-error');
    resultEl.classList.add(`result-${kind}`);
    resultEl.textContent = msg;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // Event delegation: open on per-row button, close on backdrop / X / Cancel, submit on Mint.
  document.addEventListener('click', function (ev) {
    const target = ev.target.closest('[data-action]');
    if (!target) return;
    const action = target.dataset.action;
    if (action === 'open-configure') {
      const row = target.closest('.acct-row');
      if (row) openModal(row);
    } else if (action === 'close-configure') {
      closeModal();
    }
  });
  submitEl.addEventListener('click', submitConfigure);
  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape' && !modal.classList.contains('hidden')) closeModal();
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey) && !modal.classList.contains('hidden')) {
      submitConfigure();
    }
  });
})();
