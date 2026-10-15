// Extend Unfold's existing Alpine theme state; do not install a second theme controller.
document.addEventListener('alpine:init', () => {
  const unfoldTheme = window.theme;
  if (!unfoldTheme || !window.SiteTheme) return;
  window.theme = function (...args) {
    const state = unfoldTheme(...args);
    const initialize = state.init;
    state.init = function () {
      initialize.call(this);
      this.$watch('adminTheme', value => window.SiteTheme.set(value));
      this.$watch('sidebarOpen', value => {
        document.querySelectorAll('[data-admin-navigation-toggle]').forEach(button => button.setAttribute('aria-expanded', String(value)));
      });
      this.adminTheme = window.SiteTheme.get();
      document.addEventListener('site:theme', event => {
        if (this.adminTheme !== event.detail.mode) this.adminTheme = event.detail.mode;
      });
      window.SiteTheme.apply();
      window.addEventListener('storage', event => {
        if (event.key === 'adminTheme') this.adminTheme = window.SiteTheme.get();
      });
    };
    return state;
  };
});

document.addEventListener('DOMContentLoaded', () => {
  // Unfold's compact sidebar trigger is a span in the locked version.
  document.querySelectorAll('[x-on\\:click="sidebarToggle()"]').forEach(button => {
    button.dataset.adminNavigationToggle = '';
    button.setAttribute('role', 'button');
    button.setAttribute('tabindex', '0');
    button.setAttribute('aria-label', document.documentElement.lang.startsWith('en') ? 'Toggle administration navigation' : '展开管理导航');
    button.setAttribute('aria-controls', 'nav-sidebar');
    button.setAttribute('aria-expanded', String(document.getElementById('nav-sidebar')?.checkVisibility() || false));
    button.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        button.click();
      }
    });
  });
});
