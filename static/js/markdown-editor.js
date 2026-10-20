document.addEventListener('DOMContentLoaded', () => {
  if (typeof EasyMDE === 'undefined') return;
  document.querySelectorAll('[data-markdown-editor]').forEach(element => {
    let previewVersion = 0;
    const editor = new EasyMDE({ element, spellChecker:false, autoDownloadFontAwesome:false, autosave:{enabled:false},
      promptTexts: document.documentElement.lang.startsWith('zh') ? {link:'请输入链接地址：', image:'请输入图片地址：'} : undefined,
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
    if (document.documentElement.lang.startsWith('zh')) {
      const titles = {'bold':'加粗', 'italic':'斜体', 'heading':'插入标题', 'quote':'引用',
        'unordered-list':'无序列表', 'ordered-list':'有序列表', 'link':'插入链接',
        'preview':'预览', 'side-by-side':'并排预览'};
      Object.entries(titles).forEach(([name, title]) => {
        const button = editor.toolbarElements[name];
        if (button) {
          button.title = title;
          button.setAttribute('aria-label', title);
        }
      });
      editor.toolbar_div.setAttribute('aria-label', '正文编辑工具');
    }
  });
});
