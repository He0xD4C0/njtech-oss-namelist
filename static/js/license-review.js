(() => {
  const root = document.querySelector('[data-approval-url]');
  const inspection = document.querySelector('[data-inspection-url]');
  const inspectForm = document.querySelector('[data-auto-inspect]');
  if (inspectForm) {
    fetch(inspectForm.action, {method: 'POST', body: new FormData(inspectForm), credentials: 'same-origin'})
      .then(response => { if (response.ok) location.reload(); }).catch(() => {});
  }
  async function poll() {
    try {
      if (root) {
        const response = await fetch(root.dataset.approvalUrl, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
        if (!response.ok) throw new Error('status');
        const data = await response.json();
        root.querySelector('[data-job-status]').textContent = `${data.stage} · ${data.status}`;
        root.querySelector('[data-job-reason]').textContent = data.reason || '任务已保存，等待后台执行。';
        root.querySelector('[data-job-commit]').textContent = data.commit || '尚未提交';
        root.querySelector('[data-job-policy]').textContent = data.policy || '尚未生效';
        root.querySelector('[data-job-retry]').hidden = !data.failed;
        root.querySelectorAll('[data-stage]').forEach(step => {
          if (step.dataset.stage === data.stage) step.setAttribute('aria-current', 'step');
          else step.removeAttribute('aria-current');
        });
        const list = root.querySelector('[data-job-reaudits]');
        if (data.reaudit.length) {
          list.replaceChildren(...data.reaudit.map(result => {
            const item = document.createElement('li');
            item.textContent = `${result.repository}：${result.status || '等待复审'}${result.reason ? ' · ' + result.reason : ''}`;
            return item;
          }));
        }
        root.querySelector('[data-poll-message]').textContent = '';
        if (!data.active || data.failed) return;
      } else if (inspection) {
        const response = await fetch(inspection.dataset.inspectionUrl, {credentials: 'same-origin'});
        if (!response.ok) throw new Error('status');
        if ((await response.json()).state !== 'running') { location.reload(); return; }
      } else return;
    } catch (_) {
      if (root) root.querySelector('[data-poll-message]').textContent = '暂时无法更新进度，正在重试。任务保存在后台。';
    }
    setTimeout(poll, 4000);
  }
  if (root || inspection) setTimeout(poll, 1500);
})();
