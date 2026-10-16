(() => {
  'use strict';
  const panel = document.querySelector('[data-backup-admin]');
  if (!panel) return;
  let pending = false;
  let submitting = false;
  document.addEventListener('submit', event => {
    if (!event.target.closest('[data-backup-admin]')) return;
    if (submitting) { event.preventDefault(); return; }
    submitting = true;
    event.target.setAttribute('aria-busy', 'true');
    // Preserve the clicked action in the native form submission.
  });
  async function poll() {
    if (document.hidden || pending || submitting) return;
    pending = true;
    try {
      const response = await fetch('?action=backup-status', {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || response.redirected) return;
      const data = await response.json();
      if (document.hidden || submitting) return;
      panel.querySelector('[data-backup-label]').textContent = data.label;
      panel.querySelector('[data-backup-error]').textContent = data.error;
      panel.querySelector('[data-backup-success]').textContent = data.last_success || '—';
      const apply = panel.querySelector('[data-backup-apply]');
      const backup = panel.querySelector('[data-backup-now]');
      if (apply) apply.disabled = !data.tested || data.busy;
      if (backup) backup.disabled = !data.enabled || data.busy;
    } catch { /* Status reads never resubmit operations or modify unsaved fields. */ }
    finally { pending = false; }
  }
  setInterval(poll, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
})();
