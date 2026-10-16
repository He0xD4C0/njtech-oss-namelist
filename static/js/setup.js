(() => {
  'use strict';
  let busy = false;
  let dirty = false;
  let polling = null;
  let operation = 0;
  let uncertain = false;
  const root = () => document.getElementById('setup-flow');
  if (!root()) return;

  function invalidatePoll() {
    operation += 1;
    polling?.abort();
    polling = null;
  }

  function warn(message) {
    const box = document.getElementById('setup-connection-error');
    box.hidden = false;
    box.querySelector('[data-error-text]').textContent = message;
    document.getElementById('setup-live').textContent = message;
  }

  function replace(data, navigate) {
    if (!data.html) throw new Error('Unexpected response');
    const holder = document.createElement('template');
    holder.innerHTML = data.html;
    const replacement = holder.content.querySelector('#setup-flow');
    if (!replacement) throw new Error('Missing setup content');
    root().replaceWith(replacement);
    if (typeof data.brand_logo_url === 'string') {
      document.querySelectorAll('[data-brand-logo]').forEach(image => {
        image.hidden = !data.brand_logo_url;
        if (data.brand_logo_url) image.src = data.brand_logo_url;
        else image.removeAttribute('src');
      });
      document.querySelectorAll('[data-brand-default]').forEach(item => { item.hidden = !!data.brand_logo_url; });
    }
    dirty = false;
    uncertain = false;
    if (navigate && data.url) history.pushState(null, '', data.url);
    const error = replacement.querySelector('[aria-invalid="true"]');
    if (navigate || error) (error || replacement.querySelector('#setup-task-title'))?.focus();
  }

  async function request(url, options = {}, depth = 0, current = () => true) {
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...options,
      headers: {Accept: 'application/json', ...options.headers}});
    if (!current()) return null;
    const data = await response.json();
    if (!current()) return null;
    if (data.redirect) {
      const target = new URL(data.redirect, location.origin);
      if (target.origin !== location.origin) throw new Error('Unexpected redirect');
      if (target.pathname !== '/setup/') { dirty = false; busy = false; location.assign(target.href); return null; }
      if (depth > 3) throw new Error('Redirect loop');
      return request(target.href, {signal: options.signal}, depth + 1, current);
    }
    if (!data.html) throw new Error('Unexpected response');
    return data;
  }

  function canLeave() { return !dirty || window.confirm(root().dataset.unsaved); }

  document.addEventListener('input', event => {
    if (event.target.closest('#setup-flow form')) { dirty = true; invalidatePoll(); }
    if (event.target.name === 'recipient') event.target.dataset.edited = 'true';
    if (event.target.name === 'smtp_from_email') {
      const recipient = root()?.querySelector('[name="recipient"]');
      if (recipient && !recipient.dataset.edited) recipient.value = event.target.value;
    }
  });
  document.addEventListener('change', event => {
    if (event.target.closest('#setup-flow form')) { dirty = true; invalidatePoll(); }
  });
  window.addEventListener('beforeunload', event => {
    if (dirty || busy) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('popstate', () => { invalidatePoll(); location.reload(); });

  document.addEventListener('click', async event => {
    const link = event.target.closest('[data-setup-link], [data-check-status]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (busy || (!link.hasAttribute('data-check-status') && !canLeave())) return;
    invalidatePoll();
    busy = true;
    try { const data = await request(link.href); if (data) replace(data, true); }
    catch { warn(root().dataset.networkError); }
    finally { busy = false; }
  });

  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-setup-form]');
    if (!form) return;
    event.preventDefault();
    if (busy || uncertain) return;
    if (form.hasAttribute('data-navigation-form') && !canLeave()) return;
    const data = new FormData(form);
    if (event.submitter?.name) data.set(event.submitter.name, event.submitter.value);
    // Only the active form is sent. No passwords, tokens or drafts are persisted.
    const buttons = [...form.querySelectorAll('button[type="submit"]')];
    const previouslyDisabled = buttons.map(button => button.disabled);
    const submitter = event.submitter || buttons[0];
    const label = submitter?.textContent;
    invalidatePoll();
    busy = true;
    form.setAttribute('aria-busy', 'true');
    buttons.forEach(button => { button.disabled = true; });
    if (submitter) submitter.textContent = root().dataset.busy;
    document.getElementById('setup-live').textContent = root().dataset.busy;
    try {
      const result = await request('/setup/', {method: 'POST', body: data});
      if (result) replace(result, true);
    } catch {
      uncertain = true;
      warn(root().dataset.networkError);
    } finally {
      busy = false;
      form.removeAttribute('aria-busy');
      buttons.forEach((button, index) => { button.disabled = previouslyDisabled[index] || uncertain; });
      if (submitter) submitter.textContent = label;
    }
  });

  async function poll() {
    if (document.hidden || busy || polling || dirty || root()?.dataset.waiting !== 'true') return;
    const controller = new AbortController();
    const original = root();
    const url = original.dataset.url;
    const version = operation;
    polling = controller;
    const current = () => !controller.signal.aborted && !document.hidden && !busy && !dirty &&
      operation === version && root() === original && root()?.dataset.url === url;
    try {
      const response = await fetch('/setup/?action=status', {signal: controller.signal, credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'}});
      if (!current()) return;
      const data = await response.json();
      if (!current()) return;
      if (!data.authorized) { warn(original.dataset.networkError); return; }
      if (data.revision !== original.dataset.revision) {
        const page = await request(url, {signal: controller.signal}, 0, current);
        if (current() && page) replace(page, false);
      }
    } catch { /* Reads and canceled polls never submit a task or clear a draft. */ }
    finally { if (polling === controller) polling = null; }
  }
  window.setInterval(poll, 5000);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) invalidatePoll();
    else poll();
  });
})();
