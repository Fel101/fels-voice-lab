// Display words and counts in the saved page language. User data stays in the values passed to these functions.
var LabText = (function () {
  'use strict';
  var config = JSON.parse(document.getElementById('pageText').textContent);
  var language = config.language, english = config.english, messages = config.messages;
  var rules = new Intl.PluralRules(language), englishRules = new Intl.PluralRules('en');

  // Language labels are display text. The original engine values still go to the API.
  var languageTags = {
    'american english': 'en-US', 'british english': 'en-GB', english: 'en',
    spanish: 'es', french: 'fr', hindi: 'hi', italian: 'it', japanese: 'ja',
    portuguese: 'pt', 'brazilian portuguese': 'pt-BR', chinese: 'zh', 'mandarin chinese': 'zh',
    german: 'de', korean: 'ko', russian: 'ru', arabic: 'ar', dutch: 'nl',
    polish: 'pl', turkish: 'tr', swedish: 'sv', danish: 'da', finnish: 'fi',
    norwegian: 'no', hebrew: 'he', greek: 'el', czech: 'cs', romanian: 'ro',
    ukrainian: 'uk', indonesian: 'id', malay: 'ms', vietnamese: 'vi', thai: 'th',
    hungarian: 'hu', persian: 'fa', catalan: 'ca', cantonese: 'yue',
  };
  var languageNames = new Intl.DisplayNames(language, {type: 'language', fallback: 'none'});
  var listFormat = new Intl.ListFormat(language);
  var pageLocale = language.toLowerCase();
  var regionalLocale = (globalThis.navigator?.languages || []).find(function (locale) {
    var tag = locale.toLowerCase();
    return tag === pageLocale || tag.startsWith(pageLocale + '-');
  }) || language;

  function languageName(label, tag) {
    if (language === 'en' || !label) return label;
    if (label.toLowerCase() === 'auto' || tag === 'auto') return t('language.automatic');
    var code = tag || languageTags[label.toLowerCase()];
    if (!code) return label;
    try {
      var name = languageNames.of(code);
      return name ? name.charAt(0).toLocaleUpperCase(language) + name.slice(1) : label;
    } catch (_) {
      return label;
    }
  }

  function dateTime(date) {
    return date.toLocaleString(regionalLocale);
  }

  function format(template, values) {
    return template.replace(/\{([a-z][a-zA-Z0-9_]*)\}/g, function (placeholder, name) {
      return values && Object.hasOwn(values, name) ? String(values[name]) : placeholder;
    });
  }

  function t(key, values) {
    var message = Object.hasOwn(messages, key) ? messages[key] : Object.hasOwn(english, key) ? english[key] : key;
    return format(message, values);
  }

  function label(key, fallback) {
    return Object.hasOwn(messages, key) ? t(key) : fallback;
  }

  function statusName(status) {
    return label('cast.status.' + status, status);
  }

  function list(items) {
    return listFormat.format(items);
  }

  function plural(key, count, values) {
    var local = messages[key], fallback = english[key];
    var phrase = local ? local[rules.select(count)] ?? local.other
      : fallback ? fallback[englishRules.select(count)] ?? fallback.other : key;
    return format(phrase, {...values, count: count});
  }

  function error(payload, fallback) {
    var key = {
      model_not_installed: 'error.model_not_installed',
      ffmpeg_not_installed: 'error.ffmpeg_not_installed',
      cancelled: 'error.cancelled'
    }[payload.code];
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

  return {t: t, plural: plural, error: error, label: label, statusName: statusName,
    language: language, languageName: languageName, dateTime: dateTime, list: list};
}());
