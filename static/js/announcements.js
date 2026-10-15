(() => {
  const template = document.getElementById('site-announcement-popups');
  if (template) document.body.append(template.content.cloneNode(true));
  const dialogs = [...document.querySelectorAll('.announcement-dialog')];
  let active = 0;
  const showNext = () => {
    if (active < dialogs.length) dialogs[active].showModal();
  };
  for (const dialog of dialogs) {
    const form = dialog.querySelector('.announcement-receipt');
    let saving = false;
    const record = async (action) => {
      if (saving) return;
      saving = true;
      const buttons = [...dialog.querySelectorAll('button')];
      buttons.forEach(button => { button.disabled = true; });
      const error = form.querySelector('.announcement-error');
      error.hidden = true;
      const data = new FormData(form);
      data.set('action', action);
      try {
        // A submit button named "action" shadows HTMLFormElement.action.
        const response = await fetch(form.getAttribute('action'), { method:'POST', body:data, credentials:'same-origin',
          headers:{'X-Requested-With':'XMLHttpRequest'} });
        if (response.status === 410) {
          dialog.close();
          active += 1;
          showNext();
          return;
        }
        if (!response.ok) {
          error.textContent = response.status === 409 ? error.dataset.changed : error.dataset.failed;
          error.hidden = false;
          return;
        }
        const result = await response.json();
        if (!result.ok) throw new Error('Receipt not saved');
        dialog.close();
        active += 1;
        showNext();
      } catch (_) {
        error.textContent = error.dataset.failed;
        error.hidden = false;
      } finally {
        saving = false;
        buttons.forEach(button => { button.disabled = false; });
      }
    };
    form.addEventListener('submit', event => { event.preventDefault(); record('confirm'); });
    dialog.querySelectorAll('[data-receipt-action]').forEach(button => {
      button.addEventListener('click', () => record(button.dataset.receiptAction));
    });
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      if (dialog.dataset.announcementKind === 'normal') record('dismiss');
    });
    dialog.addEventListener('click', event => {
      if (dialog.dataset.announcementKind !== 'normal' || event.target !== dialog) return;
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) record('dismiss');
    });
  }
  if (dialogs.length && typeof HTMLDialogElement !== 'undefined') showNext();
})();
