(() => {
  const widgets = new Map();
  const render = () => {
    if (!window.turnstile) return;
    const theme = document.documentElement.dataset.bsTheme || 'light';
    document.querySelectorAll('.cf-turnstile[data-sitekey]').forEach(element => {
      const existing = widgets.get(element);
      if (existing?.theme === theme) return;
      if (existing) window.turnstile.remove(existing.id);
      element.dataset.theme = theme;
      const id = window.turnstile.render(element, {sitekey: element.dataset.sitekey, theme, size: 'flexible'});
      widgets.set(element, {id, theme});
    });
  };
  window.siteTurnstileReady = render;
  document.addEventListener('site:theme', render);
})();
