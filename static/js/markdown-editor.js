document.addEventListener('DOMContentLoaded', () => {
  if (typeof EasyMDE === 'undefined') return;
  document.querySelectorAll('[data-markdown-editor]').forEach(element => {
    let previewVersion = 0;
    new EasyMDE({ element, spellChecker:false, autoDownloadFontAwesome:false, autosave:{enabled:false},
      toolbar:['bold','italic','heading','|','quote','unordered-list','ordered-list','link','|','preview','side-by-side'],
      previewRender:(markdown, preview) => {
        const version = ++previewVersion;
        const token = document.querySelector('[name=csrfmiddlewaretoken]')?.value;
        const form = new URLSearchParams({body:markdown, csrfmiddlewaretoken:token || ''});
        fetch('/manage/markdown-preview/', {method:'POST', body:form, credentials:'same-origin', headers:{'X-Requested-With':'XMLHttpRequest'}})
          .then(response => { if (!response.ok) throw new Error('Preview failed'); return response.json(); })
          .then(result => { if (version === previewVersion) preview.innerHTML = result.html; })
          .catch(() => { if (version === previewVersion) preview.textContent = document.documentElement.lang.startsWith('en') ? 'Preview unavailable.' : '预览暂不可用。'; });
        return '…';
      }
    });
  });
});
