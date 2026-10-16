(() => {
  'use strict';
  const states = new WeakMap();
  const state = box => {
    if (!states.has(box)) states.set(box, {poll: Boolean(emailField(box).value), busy: false, resumed: true});
    return states.get(box);
  };
  const emailField = box => box.closest('form').querySelector('[name="email"]');
  async function post(box, url, action = '') {
    const form = box.closest('form');
    const data = new FormData();
    for (const name of ['csrfmiddlewaretoken', 'cf-turnstile-response']) {
      const field = form.querySelector(`[name="${name}"]`);
      if (field) data.set(name, field.value);
    }
    data.set('email', emailField(box).value);
    data.set('purpose', box.dataset.purpose);
    data.set('action', action);
    const response = await fetch(url, {method: 'POST', body: data, credentials: 'same-origin', cache: 'no-store'});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || box.dataset.failed);
    return result;
  }
  function show(box, value) { box.querySelector('[data-mailbox-status]').textContent = value; }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-mailbox-send], [data-mailbox-cancel]');
    if (!button) return;
    const box = button.closest('[data-mailbox-proof]');
    const record = state(box);
    if (record.busy) return;
    const cancel = button.hasAttribute('data-mailbox-cancel');
    if (!cancel && !emailField(box).reportValidity()) return;
    record.busy = true;
    button.disabled = true;
    try {
      await post(box, box.dataset.requestUrl, cancel ? 'cancel' : '');
      record.poll = !cancel;
      record.resumed = false;
      box.querySelector('[data-mailbox-cancel]').hidden = cancel;
      box.querySelector('[data-mailbox-done]')?.setAttribute('hidden', '');
      show(box, cancel ? box.dataset.expired : box.dataset.pending);
    } catch (error) { show(box, error.message || box.dataset.failed); }
    finally {
      record.busy = false;
      button.disabled = false;
      if (!cancel && box.closest('form').querySelector('.cf-turnstile') && window.turnstile) window.turnstile.reset();
    }
  });
  document.addEventListener('input', event => {
    if (event.target.name !== 'email') return;
    const box = event.target.closest('form')?.querySelector('[data-mailbox-proof]');
    if (box) {
      state(box).poll = false;
      state(box).resumed = false;
      show(box, '');
      box.querySelector('[data-mailbox-done]')?.setAttribute('hidden', '');
    }
  });
  setInterval(async () => {
    if (document.hidden) return;
    for (const box of document.querySelectorAll('[data-mailbox-proof]')) {
      const record = state(box);
      if (!record.poll || record.busy) continue;
      record.busy = true;
      try {
        const result = await post(box, box.dataset.statusUrl);
        if (!result.matches || result.status === 'expired') {
          if (!record.resumed) show(box, box.dataset.expired);
          record.poll = false;
        } else if (result.status === 'verified') {
          const bound = ['add', 'recover'].includes(box.dataset.purpose);
          show(box, bound ? box.dataset.bound : box.dataset.verified);
          box.querySelector('[data-mailbox-done]')?.removeAttribute('hidden');
          box.querySelector('[data-mailbox-cancel]').hidden = true;
          record.poll = false;
        } else {
          show(box, box.dataset.pending);
          box.querySelector('[data-mailbox-cancel]').hidden = false;
        }
      } catch { show(box, box.dataset.failed); }
      finally { record.busy = false; }
    }
  }, 2500);
})();
