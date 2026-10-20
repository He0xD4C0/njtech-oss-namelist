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
  const commandSearch = document.getElementById('search-input-command');
  if (commandSearch && window.htmx) {
    // Input events also cover pasted text and completed input-method composition.
    commandSearch.setAttribute('hx-trigger', 'input changed delay:500ms, search');
    window.htmx.process(commandSearch);
  }
  const configuration = document.querySelector('[data-config-selected]');
  if (configuration) {
    const navigation = configuration.querySelector('nav');
    const openGroup = id => {
      const group = document.getElementById(id);
      if (!group || !configuration.contains(group)) return;
      if (group.tagName === 'DETAILS') group.open = true;
      navigation.querySelectorAll('a').forEach(link => {
        if (link.hash === '#' + id) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    };
    const invalid = configuration.querySelector('[data-config-invalid]');
    const initial = invalid?.id || location.hash.slice(1) || configuration.dataset.configSelected;
    openGroup(initial);
    if (invalid) invalid.querySelector('[aria-invalid="true"]')?.focus();
    navigation.addEventListener('click', event => {
      const link = event.target.closest('a');
      if (link) openGroup(link.hash.slice(1));
    });
    window.addEventListener('hashchange', () => openGroup(location.hash.slice(1)));
    configuration.querySelectorAll('details').forEach(group => {
      group.addEventListener('toggle', () => {
        if (group.open) openGroup(group.id);
      });
    });
  }
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
