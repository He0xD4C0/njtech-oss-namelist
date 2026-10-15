(() => {
  const root = document.documentElement;
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  const valid = value => ['light', 'dark', 'auto'].includes(value) ? value : 'auto';
  let mode = 'auto';
  try { mode = valid(JSON.parse(localStorage.getItem('adminTheme'))); } catch (_) { /* System default. */ }
  const apply = () => {
    const effective = mode === 'auto' ? (system.matches ? 'dark' : 'light') : mode;
    root.dataset.bsTheme = effective;
    root.classList.toggle('dark', effective === 'dark');
    root.classList.toggle('light', effective === 'light');
    root.style.colorScheme = effective;
    document.querySelectorAll('[data-theme-select]').forEach(select => { select.value = mode; });
    document.dispatchEvent(new CustomEvent('site:theme', {detail: {mode, effective}}));
  };
  window.SiteTheme = {
    get: () => mode,
    set: value => {
      mode = valid(value);
      try { localStorage.setItem('adminTheme', JSON.stringify(mode)); } catch (_) { /* Apply for this page. */ }
      apply();
    },
    apply,
  };
  apply();
  system.addEventListener('change', () => { if (mode === 'auto') apply(); });
  window.addEventListener('storage', event => {
    if (event.key !== 'adminTheme') return;
    try { mode = valid(JSON.parse(event.newValue)); } catch (_) { mode = 'auto'; }
    apply();
  });
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    document.querySelectorAll('[data-theme-select]').forEach(select => {
      select.addEventListener('change', () => window.SiteTheme.set(select.value));
    });
  });
})();
