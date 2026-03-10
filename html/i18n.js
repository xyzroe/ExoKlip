/* ExoKlip i18n — lightweight local-only translation engine */
(function () {
  'use strict';

  const SUPPORTED = ['en', 'ru', 'uk', 'de', 'pt', 'cs'];

  /* Detect language: stored preference → browser language → English */
  let _lang = localStorage.getItem('exoklip_lang') ||
              (navigator.language || '').slice(0, 2).toLowerCase();
  if (!SUPPORTED.includes(_lang)) _lang = 'en';

  /* Translate key with optional {placeholder} substitution */
  function t(key, vars) {
    const D = window.LANGS || {};
    let s = (D[_lang] && D[_lang][key] !== undefined) ? D[_lang][key]
          : (D.en   && D.en[key]   !== undefined) ? D.en[key]
          : key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        s = s.replace('{' + k + '}', v);
      }
    }
    return s;
  }

  /* Apply translations to all data-i18n* elements in DOM */
  function apply() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
      el.title = t(el.getAttribute('data-i18n-title'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
      el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
    });
    /* sync the selector widget */
    const sel = document.getElementById('lang-select');
    if (sel) sel.value = _lang;
    document.documentElement.lang = _lang;
  }

  /* Switch language and reload page (simplest way to re-render all JS-generated content) */
  function setLang(l) {
    if (!SUPPORTED.includes(l)) return;
    localStorage.setItem('exoklip_lang', l);
    location.reload();
  }

  /* Public API */
  window.i18n = {
    t,
    apply,
    setLang,
    SUPPORTED,
    get lang() { return _lang; },
  };

  /* Global shortcut used by app.js */
  window.t = t;

  /* Run apply() once DOM is ready */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }
})();
