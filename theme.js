// Appearance belongs to the local app, so it survives a new desktop port.
(function () {
  'use strict';
  var root = document.documentElement;
  var picker = /** @type {HTMLSelectElement} */ (document.getElementById('themeChoice'));
  var status = document.getElementById('themeStatus');
  if (!picker) return;
  picker.value = root.dataset.theme || 'system';
  var pending = Promise.resolve();
  var revision = 0;
  picker.addEventListener('change', function () {
    var theme = picker.value;
    if (['system', 'light', 'dark', 'forest'].indexOf(theme) < 0) return;
    root.dataset.theme = theme;
    var current = ++revision;
    status.textContent = '';
    // Keep writes ordered when someone switches themes several times quickly.
    pending = pending.then(function () {
      return fetch('/api/preferences', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({theme: theme}), cache: 'no-store'
      }).then(function (response) {
        if (!response.ok) throw new Error(LabText.t('shell.theme_could_not_be_saved'));
      });
    }).catch(function () {
      if (current === revision) status.textContent = LabText.t('shell.theme_changed_for_this_window');
    });
  });
}());
