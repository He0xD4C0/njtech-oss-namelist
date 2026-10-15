document.addEventListener('DOMContentLoaded', () => {
  const language = document.querySelector('[name="policy_language"]');
  if (!language) return;
  const update = () => {
    document.querySelectorAll('a[href^="/policies/"]').forEach(link => {
      const url = new URL(link.getAttribute('href'), location.origin);
      url.searchParams.set('language', language.value);
      link.href = url.toString();
    });
  };
  language.addEventListener('change', update);
  update();
});
