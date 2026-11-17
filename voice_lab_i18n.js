// Display words and counts in the saved page language. User data stays in the values passed to these functions.
var LabText = (function () {
  'use strict';
  var config = JSON.parse(document.getElementById('pageText').textContent);
  var language = config.language, english = config.english, messages = config.messages;
  var rules = new Intl.PluralRules(language), englishRules = new Intl.PluralRules('en');

  function format(template, values) {
    return template.replace(/\{([a-z][a-zA-Z0-9_]*)\}/g, function (placeholder, name) {
      return values && Object.hasOwn(values, name) ? String(values[name]) : placeholder;
    });
  }

  function t(key, values) {
    var message = Object.hasOwn(messages, key) ? messages[key] : Object.hasOwn(english, key) ? english[key] : key;
    return format(message, values);
  }

  function plural(key, count, values) {
    var local = messages[key], fallback = english[key];
    var phrase = local ? local[rules.select(count)] ?? local.other
      : fallback ? fallback[englishRules.select(count)] ?? fallback.other : key;
    return format(phrase, {...values, count: count});
  }

  function error(payload, fallback) {
    var key = 'error.' + payload.code;
    // The English service text can carry more specific advice about this model or request.
    return language !== 'en' && Object.hasOwn(messages, key) ? t(key) : payload.error || fallback;
  }

  document.addEventListener('DOMContentLoaded', function () {
    var picker = /** @type {HTMLSelectElement} */ (document.getElementById('languageChoice'));
    var status = document.getElementById('languageStatus');
    if (!picker) return;
    picker.addEventListener('change', async function () {
      if (picker.value === language) return;
      var discardScene = typeof sceneDirty !== 'undefined' && sceneDirty;
      if (discardScene && !window.confirm(t('language.discard_scene'))) {
        picker.value = language;
        return;
      }
      status.textContent = '';
      picker.disabled = true;
      try {
        var response = await fetch('/api/preferences', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({language: picker.value})
        });
        if (!response.ok) throw new Error();
        // Loading the page again gives static and dynamic words the same language before anything paints.
        if (discardScene) sceneDirty = false;
        picker.disabled = false;
        window.location.reload();
      } catch (failure) {
        picker.value = language;
        picker.disabled = false;
        status.textContent = t('language.save_failed');
      }
    });
  });

  return {t: t, plural: plural, error: error};
}());
