// Voice Lab scenes: script editor, batch renders, assembly previews and the export panel.
var activeSceneId = null,
  activeSceneJobId = null,
  scenePollTimer = null;
function showSceneError(error) {
  $('sceneError').textContent = error.message || String(error);
}
function sceneBody() {
  var value;
  try {
    value = JSON.parse($('sceneEditor').value);
  } catch (error) {
    throw new Error('Scene JSON could not be parsed: ' + error.message);
  }
  if (!value || !Array.isArray(value.lines)) throw new Error('Scene manifest needs a lines array.');
  return value;
}
function markSceneDirty() {
  sceneDirty = true;
  $('sceneError').textContent = '';
  $('sceneStatus').textContent = 'Unsaved script changes. Save before rendering or assembling selected takes.';
}
// Scene undo keeps the script text that each change replaced. Keystrokes in one field, less than a second apart, are one step.
var sceneUndo = [],
  sceneRedo = [],
  sceneUndoGroup = null,
  sceneUndoAt = 0,
  sceneSavedText = null,
  sceneLiveText = '';
function sceneHistoryState() {
  $('sceneUndo').disabled = !sceneUndo.length;
  $('sceneRedo').disabled = !sceneRedo.length;
}
function resetSceneHistory(savedText) {
  sceneSavedText = savedText;
  sceneLiveText = $('sceneEditor').value;
  sceneUndo = [];
  sceneRedo = [];
  sceneUndoGroup = null;
  sceneHistoryState();
}
function recordSceneChange(group, previous) {
  var now = Date.now();
  if (group && group === sceneUndoGroup && now - sceneUndoAt < 1000) {
    sceneUndoAt = now;
    return;
  }
  sceneUndo.push(previous);
  if (sceneUndo.length > 100) sceneUndo.shift();
  sceneRedo = [];
  sceneUndoGroup = group || null;
  sceneUndoAt = now;
  sceneHistoryState();
}
function restoreSceneText(text) {
  sceneUndoGroup = null;
  $('sceneEditor').value = text;
  sceneLiveText = text;
  sceneDirty = text !== sceneSavedText;
  renderSceneForm();
  $('sceneError').textContent = '';
  $('sceneStatus').textContent = sceneDirty
    ? 'Unsaved script changes. Save before rendering or assembling selected takes.'
    : 'Back to the saved script.';
  sceneHistoryState();
}
function undoScene() {
  if (!sceneUndo.length) return;
  sceneRedo.push($('sceneEditor').value);
  restoreSceneText(sceneUndo.pop());
}
function redoScene() {
  if (!sceneRedo.length) return;
  sceneUndo.push($('sceneEditor').value);
  restoreSceneText(sceneRedo.pop());
}
function editSceneField(scene, index, key, value) {
  if (index === null) {
    if (key !== 'name') throw new Error('Unknown scene field.');
    scene.name = value;
    return scene;
  }
  if (!scene.lines[index]) throw new Error('Scene line no longer exists.');
  if (
    [
      'text',
      'direction',
      'language',
      'pause_before_ms',
      'pause_after_ms',
      'take_count',
      'target_duration_ms',
      'line_type',
      'use_pronunciations',
      'character_id',
      'identity_id',
      'reference_version_id',
    ].indexOf(key) < 0
  )
    throw new Error('Unknown line field.');
  if (['pause_before_ms', 'pause_after_ms', 'take_count', 'target_duration_ms'].indexOf(key) >= 0) {
    if (key === 'target_duration_ms' && value === '') {
      scene.lines[index][key] = null;
      return scene;
    }
    var min = key === 'take_count' || key === 'target_duration_ms' ? 1 : 0,
      max = key === 'take_count' ? 10 : key === 'target_duration_ms' ? 120000 : 10000;
    if (value === '' || !Number.isInteger(Number(value)) || Number(value) < min || Number(value) > max)
      throw new Error('Enter a whole number from ' + min + ' to ' + max + '.');
    value = Number(value);
  }
  scene.lines[index][key] = value;
  return scene;
}
function editSceneSetting(scene, index, key, value) {
  var line = scene.lines[index],
    schema = performanceCapabilitiesByEngine[line.engine_id] || { controls: [] };
  var control = (schema.controls || []).find(function (item) {
    return item.id === key;
  });
  if (!control) throw new Error('This checkpoint does not support the ' + key + ' setting.');
  if (control.type === 'select') {
    if ((control.choices || []).indexOf(value) < 0) throw new Error('Choose a supported ' + control.label + '.');
  } else {
    value = Number(value);
    if (!Number.isFinite(value) || value < control.minimum || value > control.maximum)
      throw new Error(control.label + ' must be between ' + control.minimum + ' and ' + control.maximum + '.');
    if (control.step === 1 && !Number.isInteger(value)) throw new Error(control.label + ' must be a whole number.');
  }
  line.settings = Object.assign({}, line.settings || {});
  line.settings[key] = value;
  return scene;
}
function updateSceneEditor(scene, group) {
  var previous = $('sceneEditor').value,
    next = JSON.stringify(scene, null, 2);
  if (next !== previous) recordSceneChange(group, previous);
  $('sceneEditor').value = next;
  sceneLiveText = next;
  markSceneDirty();
}
function changeSceneField(index, key, value) {
  updateSceneEditor(editSceneField(sceneBody(), index, key, value), 'field:' + index + ':' + key);
}
function changeSceneSetting(index, key, value) {
  updateSceneEditor(editSceneSetting(sceneBody(), index, key, value), 'setting:' + index + ':' + key);
}
function stableSceneLineId(lines) {
  var used = new Set(
      lines.map(function (line) {
        return line.line_id;
      }),
    ),
    largest = 0;
  lines.forEach(function (line) {
    var match = /^line-(\d+)$/.exec(line.line_id || '');
    if (match) largest = Math.max(largest, Number(match[1]));
  });
  var next = largest + 1,
    id;
  do {
    id = 'line-' + String(next++).padStart(3, '0');
  } while (used.has(id));
  return id;
}
function sceneIdentityById(identityId) {
  for (var i = 0; i < castCharacters.length; i++) {
    var identity = (castCharacters[i].identities || []).find(function (item) {
      return item.id === identityId;
    });
    if (identity) return { character: castCharacters[i], identity: identity };
  }
  return null;
}
function sceneDefaultLine(character, identity, lineId) {
  var options = identity.engine_options || {},
    schema = performanceCapabilitiesByEngine[identity.engine_id] || { controls: [] },
    settings = {};
  (schema.controls || []).forEach(function (control) {
    settings[control.id] = control.default;
  });
  return {
    line_id: lineId,
    line_type: 'dialogue',
    character_id: character.id,
    identity_id: identity.id,
    engine_id: identity.engine_id,
    reference_version_id:
      identity.kind === 'reference'
        ? identity.approved_reference_id || ((identity.references || [])[0] || {}).id || null
        : null,
    text: 'New dialogue line.',
    language: identity.kind === 'preset_voice' ? options.language : 'English',
    direction: options.default_direction || '',
    pause_before_ms: 0,
    pause_after_ms: 250,
    take_count: 1,
    selected_take_id: null,
    selected_takes: {},
    target_duration_ms: null,
    settings: settings,
    use_pronunciations: false,
  };
}
function changeSceneIdentity(index, identityId) {
  var scene = sceneBody(),
    found = sceneIdentityById(identityId);
  if (!found) throw new Error('Choose a voice identity from the selected character.');
  var old = scene.lines[index];
  if (!old) throw new Error('Scene line no longer exists.');
  if (old.identity_id === identityId) return;
  var identity = found.identity,
    schema = performanceCapabilitiesByEngine[identity.engine_id] || { controls: [], direction: { status: 'unsupported' } };
  var next = Object.assign({}, old, {
      character_id: found.character.id,
      identity_id: identity.id,
      engine_id: identity.engine_id,
      reference_version_id:
        identity.kind === 'reference'
          ? identity.approved_reference_id || ((identity.references || [])[0] || {}).id || null
          : null,
      selected_take_id: null,
      selected_takes: {},
      settings: {},
    }),
    resets = [];
  delete next.voice_identity; // The server records the new identity snapshot on save.
  var languages = sceneLanguageOptions(identity, next),
    language = languages.find(function (value) {
      return String(old.language || '').toLowerCase() === value.toLowerCase();
    });
  next.language =
    language || (identity.engine_options || {}).language || (languages.indexOf('English') >= 0 ? 'English' : languages[0]);
  if (!language) resets.push('language');
  if (['supported', 'worker_verified', 'experimental'].indexOf((schema.direction || {}).status) < 0) {
    next.direction = '';
    if (old.direction) resets.push('acting direction');
  }
  var oldSettings = old.settings || {};
  (schema.controls || []).forEach(function (control) {
    var value = oldSettings[control.id],
      valid =
        control.type === 'select'
          ? (control.choices || []).indexOf(value) >= 0
          : typeof value === 'number' &&
            Number.isFinite(value) &&
            value >= control.minimum &&
            value <= control.maximum &&
            (control.step !== 1 || Number.isInteger(value));
    next.settings[control.id] = valid ? value : control.default;
    if (!valid && Object.prototype.hasOwnProperty.call(oldSettings, control.id)) resets.push(control.label);
  });
  Object.keys(oldSettings).forEach(function (key) {
    if (!Object.prototype.hasOwnProperty.call(next.settings, key)) resets.push(key);
  });
  scene.lines[index] = next;
  updateSceneEditor(scene);
  renderSceneForm();
  if (resets.length) $('sceneStatus').textContent += ' Voice change reset unsupported values: ' + resets.join(', ') + '.';
}
function mutateSceneLine(index, action) {
  var scene = sceneBody(),
    line = scene.lines[index];
  if (!line) throw new Error('Scene line no longer exists.');
  if (action === 'duplicate') {
    var copy = JSON.parse(JSON.stringify(line));
    copy.line_id = stableSceneLineId(scene.lines);
    copy.selected_take_id = null;
    copy.selected_takes = {};
    scene.lines.splice(index + 1, 0, copy);
  } else if (action === 'remove') {
    if (scene.lines.length <= 1) throw new Error('A scene must keep at least one dialogue line.');
    scene.lines.splice(index, 1);
  } else if (action === 'up' || action === 'down') {
    var target = index + (action === 'up' ? -1 : 1);
    if (target < 0 || target >= scene.lines.length) return scene;
    scene.lines.splice(index, 1);
    scene.lines.splice(target, 0, line);
  } else throw new Error('Unknown scene-line action.');
  updateSceneEditor(scene);
  renderSceneForm();
  return scene;
}
function addSceneLine() {
  var scene = sceneBody(),
    character = performanceCharacter(),
    identity = performanceIdentity();
  if (!character || !identity) {
    character = castCharacters.find(function (item) {
      return item.identities && item.identities.length;
    });
    identity = character && character.identities[0];
  }
  if (!character || !identity) throw new Error('Create a character voice in Cast before adding dialogue.');
  scene.lines.push(sceneDefaultLine(character, identity, stableSceneLineId(scene.lines)));
  updateSceneEditor(scene);
  renderSceneForm();
}
function sceneLanguageOptions(identity, line) {
  var options =
    identity && identity.kind === 'preset_voice'
      ? identity.engine_options.supported_languages || [identity.engine_options.language]
      : (performanceCapabilitiesByEngine[line.engine_id] || {}).languages || ['English'];
  return options.filter(Boolean);
}
function sceneLabel(text, widget, parent, compact) {
  var label = document.createElement('label');
  label.className = compact ? 'field compact' : 'field';
  label.appendChild(document.createTextNode(text));
  label.appendChild(widget);
  parent.appendChild(label);
  return label;
}
function renderSceneLineEditor(line, index, article) {
  var identityInfo = sceneIdentityById(line.identity_id),
    character = line.character_id;
  var text = document.createElement('textarea');
  text.maxLength = 10000;
  text.required = true;
  text.value = line.text || '';
  text.setAttribute('data-scene-field', 'text');
  text.setAttribute('aria-label', 'Dialogue — ' + line.line_id);
  text.addEventListener('input', function () {
    try {
      changeSceneField(index, 'text', this.value);
      this.setCustomValidity('');
    } catch (error) {
      this.setCustomValidity(error.message);
      showSceneError(error);
    }
  });
  sceneLabel('Dialogue', text, article, false);
  var assignment = document.createElement('div');
  assignment.className = 'cast-grid';
  var chars = document.createElement('select');
  castCharacters.forEach(function (item) {
    option(chars, item.id, item.name);
  });
  chars.value = character || '';
  sceneLabel('Character', chars, assignment, true);
  chars.addEventListener('change', function () {
    var selected = castCharacters.find(function (item) {
        return item.id === this.value;
      }, this),
      next = selected && (selected.identities || [])[0];
    if (!next) {
      showSceneError(new Error('Add a voice identity to this character before assigning a line.'));
      return;
    }
    changeSceneIdentity(index, next.id);
  });
  var voices = document.createElement('select');
  if (identityInfo) {
    (identityInfo.character.identities || []).forEach(function (item) {
      option(voices, item.id, item.engine_id + ' / ' + item.label + ' — ' + item.status + (item.retired ? ' — retired' : ''));
    });
  }
  voices.value = line.identity_id || '';
  sceneLabel('Voice identity', voices, assignment, true);
  voices.addEventListener('change', function () {
    try {
      changeSceneIdentity(index, this.value);
    } catch (error) {
      showSceneError(error);
    }
  });
  article.appendChild(assignment);
  identityInfo = sceneIdentityById(line.identity_id);
  var identity = identityInfo && identityInfo.identity;
  if (identity && identity.kind === 'reference') {
    var references = document.createElement('select');
    (identity.references || [])
      .filter(function (ref) {
        return !ref.retired;
      })
      .forEach(function (ref) {
        option(
          references,
          ref.id,
          'v' +
            ref.version +
            (ref.id === identity.approved_reference_id ? ' — approved' : ' — candidate') +
            ' / ' +
            ref.duration_seconds.toFixed(2) +
            ' s',
        );
      });
    if (!references.options.length) option(references, '', 'No saved reference version', true);
    references.value =
      line.reference_version_id || identity.approved_reference_id || ((identity.references || [])[0] || {}).id || '';
    sceneLabel('Reference version', references, assignment, true);
    references.addEventListener('change', function () {
      changeSceneField(index, 'reference_version_id', this.value || null);
    });
  }
  var controls = document.createElement('div');
  controls.className = 'row';
  var language = document.createElement('select');
  sceneLanguageOptions(identity, line).forEach(function (value) {
    option(language, value, languageOptionLabel(line.engine_id, value));
  });
  var validLanguages = sceneLanguageOptions(identity, line);
  language.value =
    validLanguages.indexOf(line.language) >= 0
      ? line.language
      : identity && identity.kind === 'preset_voice'
        ? identity.engine_options.language
        : validLanguages[0] || 'English';
  sceneLabel('Language', language, controls, true);
  language.addEventListener('change', function () {
    changeSceneField(index, 'language', this.value);
  });
  var lineType = document.createElement('select');
  [
    ['dialogue', 'Dialogue'],
    ['narration', 'Narration'],
  ].forEach(function (item) {
    option(lineType, item[0], item[1]);
  });
  lineType.value = line.line_type || 'dialogue';
  sceneLabel('Line type', lineType, controls, true);
  lineType.addEventListener('change', function () {
    changeSceneField(index, 'line_type', this.value);
  });
  ['pause_before_ms', 'pause_after_ms', 'take_count', 'target_duration_ms'].forEach(function (key) {
    var input = document.createElement('input');
    input.type = 'number';
    input.step = '1';
    input.value = line[key] === null || line[key] === undefined ? '' : line[key];
    input.min = key === 'take_count' || key === 'target_duration_ms' ? '1' : '0';
    input.max = key === 'take_count' ? '10' : key === 'target_duration_ms' ? '120000' : '10000';
    input.required = key !== 'target_duration_ms';
    var labelText = {
      pause_before_ms: 'Pause before (ms)',
      pause_after_ms: 'Pause after (ms)',
      take_count: 'Takes per line',
      target_duration_ms: 'Target duration (ms, optional)',
    }[key];
    sceneLabel(labelText, input, controls, true);
    input.addEventListener('input', function () {
      try {
        changeSceneField(index, key, this.value);
        this.setCustomValidity('');
      } catch (error) {
        this.setCustomValidity(error.message);
        showSceneError(error);
      }
    });
  });
  var pronunciation = document.createElement('input');
  pronunciation.type = 'checkbox';
  pronunciation.checked = !!line.use_pronunciations;
  pronunciation.addEventListener('change', function () {
    changeSceneField(index, 'use_pronunciations', this.checked);
  });
  var pronLabel = document.createElement('label');
  pronLabel.appendChild(pronunciation);
  pronLabel.appendChild(document.createTextNode(' Apply this character’s pronunciation dictionary'));
  controls.appendChild(pronLabel);
  article.appendChild(controls);
  var schema = performanceCapabilitiesByEngine[line.engine_id] || { controls: [], direction: { status: 'unsupported' } };
  var directionStatus = (schema.direction || {}).status || 'unsupported';
  if (['supported', 'worker_verified', 'experimental'].indexOf(directionStatus) >= 0) {
    var direction = document.createElement('textarea');
    direction.maxLength = 1000;
    direction.value = line.direction || '';
    direction.setAttribute('aria-label', 'Acting direction — ' + line.line_id);
    sceneLabel('Acting direction' + (directionStatus === 'experimental' ? ' (experimental)' : ''), direction, article, false);
    direction.addEventListener('input', function () {
      try {
        changeSceneField(index, 'direction', this.value);
        this.setCustomValidity('');
      } catch (error) {
        this.setCustomValidity(error.message);
        showSceneError(error);
      }
    });
  }
  var settings = document.createElement('details'),
    settingsTitle = document.createElement('summary');
  settingsTitle.textContent = 'Per-line model settings';
  settings.appendChild(settingsTitle);
  if (!(line.settings && typeof line.settings === 'object')) line.settings = {};
  (schema.controls || []).forEach(function (control) {
    var input;
    if (control.type === 'select') {
      input = document.createElement('select');
      (control.choices || []).forEach(function (value) {
        option(input, value, value);
      });
      input.value = line.settings[control.id] === undefined ? control.default : line.settings[control.id];
    } else {
      input = document.createElement('input');
      input.type = 'number';
      input.min = control.minimum;
      input.max = control.maximum;
      input.step = control.step;
      input.required = true;
      input.value = line.settings[control.id] === undefined ? control.default : line.settings[control.id];
    }
    var labelText = control.label + (control.status === 'experimental' ? ' (experimental)' : '');
    input.setAttribute('aria-label', labelText + ' — ' + line.line_id);
    sceneLabel(labelText, input, settings, true);
    input.addEventListener('input', function () {
      try {
        changeSceneSetting(index, control.id, this.value);
        this.setCustomValidity('');
      } catch (error) {
        this.setCustomValidity(error.message);
        showSceneError(error);
      }
    });
  });
  var settingsHelp = document.createElement('p');
  settingsHelp.className = 'meta';
  settingsHelp.textContent = 'The take stores effective backend values. Unsupported settings are not shown.';
  settings.appendChild(settingsHelp);
  article.appendChild(settings);
  var actions = document.createElement('div');
  actions.className = 'cast-actions';
  [
    ['up', 'Move up'],
    ['down', 'Move down'],
    ['duplicate', 'Duplicate line'],
    ['remove', 'Remove line'],
  ].forEach(function (item) {
    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = item[1];
    button.setAttribute('aria-label', item[1] + ' ' + line.line_id);
    button.disabled =
      (item[0] === 'up' && index === 0) ||
      (item[0] === 'down' && index === sceneBody().lines.length - 1) ||
      (item[0] === 'remove' && sceneBody().lines.length <= 1);
    button.addEventListener('click', function () {
      try {
        mutateSceneLine(index, item[0]);
      } catch (error) {
        showSceneError(error);
      }
    });
    actions.appendChild(button);
  });
  article.appendChild(actions);
}
function renderSceneForm() {
  var container = $('sceneLineEditor');
  container.textContent = '';
  $('sceneName').disabled = true;
  $('sceneLineAdd').disabled = true;
  if (!$('sceneEditor').value.trim()) {
    $('sceneName').value = '';
    container.textContent = 'Open a scene or start one from the current performer.';
    return;
  }
  var scene;
  try {
    scene = sceneBody();
  } catch (error) {
    container.textContent = 'Fix the advanced JSON to use the script form.';
    showSceneError(error);
    return;
  }
  $('sceneName').value = scene.name || '';
  $('sceneName').disabled = false;
  $('sceneLineAdd').disabled = false;
  $('sceneError').textContent = '';
  if (!scene.lines.length) {
    container.textContent = 'This script has no dialogue lines. Add a line to continue.';
    return;
  }
  scene.lines.forEach(function (line, index) {
    var article = document.createElement('details'),
      heading = document.createElement('summary'),
      identity = document.createElement('p');
    article.className = 'scene-line';
    article.open = index === 0;
    heading.textContent = index + 1 + '. ' + line.line_id + ' — ' + String(line.text || 'Empty line').slice(0, 90);
    article.appendChild(heading);
    var character = castCharacters.find(function (item) {
        return item.id === line.character_id;
      }),
      identityInfo = sceneIdentityById(line.identity_id);
    identity.className = 'meta';
    identity.textContent =
      (character ? character.name : 'Character ' + (line.character_id || 'unassigned').slice(0, 8)) +
      ' · ' +
      (identityInfo
        ? identityInfo.identity.engine_id + ' / ' + identityInfo.identity.label
        : line.engine_id || 'Voice identity missing') +
      ' · ' +
      (line.language || 'English');
    article.appendChild(identity);
    var built = false;
    function build() {
      if (!built) {
        built = true;
        renderSceneLineEditor(line, index, article);
      }
    }
    if (article.open) build();
    article.addEventListener('toggle', function () {
      if (article.open) build();
    });
    container.appendChild(article);
  });
}
$('sceneName').addEventListener('input', function () {
  try {
    changeSceneField(null, 'name', this.value);
  } catch (error) {
    showSceneError(error);
  }
});
$('sceneEditor').addEventListener('input', function () {
  recordSceneChange('json', sceneLiveText);
  sceneLiveText = this.value;
  sceneDirty = true;
  renderSceneForm();
  $('sceneStatus').textContent = 'Unsaved scene JSON changes.';
  sceneHistoryState();
});
function renderScene(scene) {
  activeSceneId = scene.scene_id;
  sceneDirty = false;
  $('sceneEditor').value = JSON.stringify(scene, null, 2);
  resetSceneHistory($('sceneEditor').value);
  $('sceneError').textContent = '';
  renderSceneForm();
  $('sceneStatus').textContent =
    scene.name + ' / revision ' + scene.revision + ' / ' + scene.lines.length + ' ordered lines / SHA-256 ' + scene.checksum;
  $('sceneExportJson').href = '/api/scenes/' + scene.scene_id + '/export.json';
  $('sceneExportCsv').href = '/api/scenes/' + scene.scene_id + '/export.csv';
  $('sceneExportJson').hidden = false;
  $('sceneExportCsv').hidden = false;
  return fetchJson('/api/scenes/' + scene.scene_id + '/jobs').then(function (result) {
    var select = $('sceneJobSelect'),
      previous = activeSceneJobId;
    select.textContent = '';
    (result.jobs || []).forEach(function (job) {
      option(select, job.id, job.status + ' / ' + job.completed + '/' + job.total + ' / ' + job.id.slice(0, 8));
    });
    if (
      previous &&
      (result.jobs || []).some(function (job) {
        return job.id === previous;
      })
    )
      select.value = previous;
    else if (select.options.length) select.selectedIndex = 0;
    activeSceneJobId = select.value || null;
    return activeSceneJobId ? refreshSceneJob() : Promise.resolve();
  });
}
function refreshSceneList(preferred) {
  return fetchJson('/api/scenes').then(function (result) {
    var select = $('sceneSelect'),
      keep = preferred || activeSceneId;
    select.textContent = '';
    (result.scenes || []).forEach(function (scene) {
      option(select, scene.scene_id, scene.name + ' / r' + scene.revision);
    });
    if (
      keep &&
      (result.scenes || []).some(function (scene) {
        return scene.scene_id === keep;
      })
    )
      select.value = keep;
    else if (select.options.length) select.selectedIndex = 0;
    if (select.value)
      return fetchJson('/api/scenes/' + select.value).then(function (value) {
        return renderScene(value.scene);
      });
    activeSceneId = null;
    activeSceneJobId = null;
    $('sceneEditor').value = '';
    resetSceneHistory('');
    renderSceneForm();
    $('sceneJobRows').textContent = '';
    $('sceneExportJson').hidden = true;
    $('sceneExportCsv').hidden = true;
    return Promise.resolve();
  });
}
function startSceneFromPerformer() {
  var character = performanceCharacter(),
    identity = performanceIdentity();
  if (!character || !identity) throw new Error('Choose a character and voice identity in Perform first.');
  if (sceneDirty && !window.confirm('Discard unsaved scene edits and start a new script from Perform?')) return;
  var seed = Number($('performSeed').value);
  if (!Number.isFinite(seed)) seed = 42;
  var payload = {
    schema_version: 1,
    name: character.name + ' scene',
    lines: [
      {
        line_id: 'line-001',
        line_type: 'dialogue',
        character_id: character.id,
        identity_id: identity.id,
        engine_id: identity.engine_id,
        reference_version_id:
          identity.kind === 'reference' ? $('performReference').value || identity.approved_reference_id || null : null,
        text: $('performText').value,
        language: $('performLanguage').value,
        direction: $('performDirection').value,
        pause_before_ms: 0,
        pause_after_ms: 250,
        take_count: 1,
        selected_take_id: null,
        selected_takes: {},
        target_duration_ms: null,
        settings: performanceSettings(identity, seed),
        use_pronunciations: $('usePronunciations').checked,
      },
    ],
  };
  activeSceneId = null;
  sceneDirty = true;
  $('sceneEditor').value = JSON.stringify(payload, null, 2);
  resetSceneHistory(null);
  renderSceneForm();
  $('sceneStatus').textContent = 'Starter scene is ready; validate and save it before rendering.';
}
function importSceneFile(file) {
  if (sceneDirty && !window.confirm('Discard unsaved scene edits and import this script?')) return Promise.resolve(null);
  var format = file.name.toLowerCase().endsWith('.csv') ? 'csv' : 'json';
  return file
    .text()
    .then(function (content) {
      return fetchJson('/api/scenes/import', { format: format, content: content });
    })
    .then(function (result) {
      return result ? refreshSceneList(result.scene.scene_id) : null;
    });
}
// A batch renders one model's lines before the next model loads (scene_store.create_job), and job.models lists the models in
// that order, so the status line can say which is rendering and what comes next.
function batchOrderText(job) {
  var parts = job.models || [],
    current = -1;
  parts.some(function (part, index) {
    if (part.done < part.total) {
      current = index;
      return true;
    }
    return false;
  });
  if (current < 0) return '';
  var text = parts[current].name + ': ' + parts[current].done + ' of ' + parts[current].total;
  parts.slice(current + 1).forEach(function (part) {
    text += ', then ' + part.name + ': ' + part.total;
  });
  return (parts.length > 1 ? 'Rendering one model at a time: ' : 'Rendering with ') + text + '. ';
}
function refreshSceneJob() {
  if (!activeSceneJobId) return Promise.resolve();
  return fetchJson('/api/scenes/jobs/' + activeSceneJobId).then(function (result) {
    var job = result.job;
    $('sceneProgress').value = job.progress;
    $('sceneStatus').textContent =
      (['queued', 'running'].indexOf(job.status) >= 0 ? batchOrderText(job) : '') +
      job.status +
      ' / ' +
      job.completed +
      ' completed / ' +
      job.failed +
      ' failed / ' +
      job.queued +
      ' queued / ' +
      job.total +
      ' total / ' +
      Math.round(job.progress * 100) +
      '%';
    $('sceneJobManifest').href = '/api/scenes/jobs/' + job.id + '/manifest.json';
    $('sceneJobManifest').hidden = false;
    var container = $('sceneJobRows');
    container.textContent = '';
    job.rows.forEach(function (row) {
      var article = document.createElement('article'),
        title = document.createElement('strong');
      title.textContent =
        'Render ' +
        (row.ordinal + 1) +
        ' / scene ' +
        (row.scene_order + 1) +
        ' — ' +
        row.line_id +
        ' / ' +
        row.segment_id +
        ' / take ' +
        row.take_index +
        ' / ' +
        row.status;
      article.appendChild(title);
      if (row.error) {
        var failure = document.createElement('p');
        failure.className = 'meta';
        failure.textContent = row.error;
        article.appendChild(failure);
      }
      if (row.purged) {
        var gone = document.createElement('p');
        gone.className = 'meta';
        gone.textContent = "This take's audio was purged from Cast. The row remains as a record.";
        article.appendChild(gone);
      }
      if (row.take_id && !row.purged) {
        var audio = document.createElement('audio');
        audio.controls = true;
        audio.src = '/api/takes/' + row.take_id + '/audio';
        article.appendChild(audio);
        appendTakeChecks(article, row.waveform_checks);
        appendTakeLoudness(article, row.loudness);
        appendTakeRetirement(
          article,
          { id: row.take_id, retired: !!row.retirement, retirement: row.retirement },
          refreshSceneJob,
          function (message) {
            $('sceneStatus').textContent = message;
          },
        );
        var choose = document.createElement('button');
        choose.type = 'button';
        choose.textContent = 'Select for scene';
        choose.disabled = !!row.retirement;
        choose.addEventListener('click', function () {
          if (sceneDirty) {
            showSceneError(new Error('Save or discard unsaved script edits before selecting a take.'));
            return;
          }
          fetchJson('/api/scenes/' + activeSceneId + '/select', {
            line_id: row.line_id,
            segment_id: row.segment_id,
            take_id: row.take_id,
          })
            .then(function (value) {
              $('sceneStatus').textContent = 'Selected take saved in scene revision ' + value.scene.revision + '.';
              return renderScene(value.scene);
            })
            .catch(showSceneError);
        });
        article.appendChild(choose);
      }
      container.appendChild(article);
    });
    if (['queued', 'running'].indexOf(job.status) >= 0) {
      clearTimeout(scenePollTimer);
      scenePollTimer = setTimeout(refreshSceneJob, 1000);
    }
    return job;
  });
}
function saveAndGetScene() {
  var invalid = Array.from($('sceneLineEditor').querySelectorAll('input,textarea')).find(function (input) {
    return !input.checkValidity();
  });
  if (invalid) {
    invalid.reportValidity();
    return Promise.reject(new Error('Correct the highlighted script field before saving.'));
  }
  var body;
  try {
    body = sceneBody();
  } catch (error) {
    return Promise.reject(error);
  }
  var expected = body.revision;
  return (
    activeSceneId
      ? fetchJson('/api/scenes/' + activeSceneId, Object.assign({}, body, { expected_revision: expected })).then(
          function (result) {
            return result.scene;
          },
        )
      : fetchJson('/api/scenes', body).then(function (result) {
          return result.scene;
        })
  ).then(function (scene) {
    activeSceneId = scene.scene_id;
    sceneDirty = false;
    $('sceneEditor').value = JSON.stringify(scene, null, 2);
    resetSceneHistory($('sceneEditor').value);
    return scene;
  });
}
function startSceneBatch(lineIds) {
  return saveAndGetScene()
    .then(function (scene) {
      return refreshSceneList(scene.scene_id).then(function () {
        return fetchJson('/api/scenes/' + scene.scene_id + '/jobs', lineIds && lineIds.length ? { line_ids: lineIds } : {});
      });
    })
    .then(function (result) {
      activeSceneJobId = result.job.id;
      $('sceneJobSelect').value = activeSceneJobId;
      return refreshSceneJob();
    });
}
function sceneAction(action) {
  if (!activeSceneJobId) throw new Error('Choose a batch job first.');
  return fetchJson('/api/scenes/jobs/' + activeSceneJobId + '/' + action, {}).then(function (result) {
    return refreshSceneJob();
  });
}
function previewScene() {
  if (!activeSceneId) throw new Error('Save a scene before assembly.');
  var save = sceneDirty ? saveAndGetScene() : Promise.resolve({ scene_id: activeSceneId });
  return save
    .then(function (scene) {
      return fetchJson('/api/scenes/' + scene.scene_id + '/preview', {});
    })
    .then(function (result) {
      $('scenePreviewPlayer').src = result.preview.audio_url;
      $('sceneMasteredPlayer').src = result.preview.mastered_audio_url;
      var mastering = result.preview.mastering || {};
      $('scenePreviewMeta').textContent =
        'Separate sequential calls with explicit silence / ' +
        result.preview.duration_seconds.toFixed(2) +
        ' s / ' +
        result.preview.sample_rate +
        ' Hz / mastered ' +
        Number(mastering.target_lufs).toFixed(0) +
        ' LUFS target, ' +
        Number(mastering.peak_ceiling_dbfs).toFixed(0) +
        ' dBFS sample-peak ceiling / no compression or crossfades.';
    });
}
$('sceneRefresh').addEventListener('click', function () {
  if (sceneDirty && !window.confirm('Discard unsaved scene edits and reload the saved scene?')) return;
  refreshSceneList().catch(showSceneError);
});
$('sceneSelect').addEventListener('change', function () {
  var target = this.value;
  if (sceneDirty && !window.confirm('Discard unsaved scene edits and open another scene?')) {
    this.value = activeSceneId || '';
    return;
  }
  if (target)
    fetchJson('/api/scenes/' + target)
      .then(function (result) {
        return renderScene(result.scene);
      })
      .catch(showSceneError);
});
$('sceneJobSelect').addEventListener('change', function () {
  activeSceneJobId = this.value || null;
  refreshSceneJob().catch(showSceneError);
});
$('sceneImportFile').addEventListener('change', function () {
  var file = this.files && this.files[0];
  if (file) importSceneFile(file).catch(showSceneError);
  this.value = '';
});
$('sceneStarter').addEventListener('click', function () {
  try {
    startSceneFromPerformer();
  } catch (error) {
    showSceneError(error);
  }
});
$('sceneLineAdd').addEventListener('click', function () {
  try {
    addSceneLine();
  } catch (error) {
    showSceneError(error);
  }
});
$('sceneUndo').addEventListener('click', undoScene);
$('sceneRedo').addEventListener('click', redoScene);
// Keyboard shortcuts for the scene editor, active only while the Scenes workspace is showing. Ctrl+Z inside a text box is that box's own undo.
function sceneTextEntry(element) {
  return (
    !!element &&
    (element.isContentEditable ||
      element.tagName === 'TEXTAREA' ||
      (element.tagName === 'INPUT' &&
        ['text', 'search', 'url', 'email', 'tel', 'number', 'password'].indexOf(element.type) >= 0))
  );
}
function moveSceneLineFrom(element, action) {
  var article = element && element.closest ? element.closest('.scene-line') : null;
  if (!article) return false;
  var container = $('sceneLineEditor'),
    index = Array.prototype.indexOf.call(container.children, article);
  try {
    mutateSceneLine(index, action);
  } catch (error) {
    showSceneError(error);
    return true;
  }
  var moved = container.children[action === 'up' ? index - 1 : index + 1],
    heading = moved && moved.querySelector('summary');
  if (heading) heading.focus();
  return true;
}
document.addEventListener('keydown', function (event) {
  if ($('scenes').hidden) return;
  var key = event.key.toLowerCase(),
    shortcut = (event.ctrlKey || event.metaKey) && !event.altKey;
  if (shortcut && key === 's' && $('sceneEditor').value.trim()) {
    event.preventDefault();
    $('sceneSave').click();
  } else if (shortcut && key === 'z' && !sceneTextEntry(event.target)) {
    event.preventDefault();
    if (event.shiftKey) redoScene();
    else undoScene();
  } else if (shortcut && key === 'y' && !sceneTextEntry(event.target)) {
    event.preventDefault();
    redoScene();
  } else if (
    event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    (event.key === 'ArrowUp' || event.key === 'ArrowDown') &&
    moveSceneLineFrom(event.target, event.key === 'ArrowUp' ? 'up' : 'down')
  ) {
    event.preventDefault();
  }
});
$('sceneSave').addEventListener('click', function () {
  saveAndGetScene()
    .then(function (scene) {
      return refreshSceneList(scene.scene_id);
    })
    .catch(showSceneError);
});
$('sceneSegments').addEventListener('click', function () {
  try {
    sceneBody();
    if (!activeSceneId || sceneDirty) {
      $('sceneSegmentPreview').textContent = 'Save the current scene before validating and previewing its generated segments.';
      return;
    }
    fetchJson('/api/scenes/' + activeSceneId + '/segments')
      .then(function (result) {
        $('sceneSegmentPreview').textContent = (result.segments || [])
          .map(function (line) {
            return (
              line.line_id +
              ': ' +
              line.segments.length +
              ' segment(s) — ' +
              line.segments
                .map(function (segment) {
                  return segment.id + ' (' + segment.text.length + ' chars)';
                })
                .join(', ')
            );
          })
          .join('\n');
      })
      .catch(showSceneError);
  } catch (error) {
    showSceneError(error);
  }
});
$('sceneStart').addEventListener('click', function () {
  startSceneBatch(null).catch(showSceneError);
});
$('sceneRegenerate').addEventListener('click', function () {
  var ids = $('sceneRegenerateIds')
    .value.split(',')
    .map(function (value) {
      return value.trim();
    })
    .filter(Boolean);
  if (!ids.length) {
    showSceneError(new Error('Enter one or more stable line IDs or line_id/segment_id selectors.'));
    return;
  }
  var lines = ids.filter(function (value) {
      return value.indexOf('/') < 0;
    }),
    segments = ids.filter(function (value) {
      return value.indexOf('/') >= 0;
    });
  if (lines.length && segments.length) {
    showSceneError(new Error('Regenerate either whole lines or individual segments in one batch.'));
    return;
  }
  saveAndGetScene()
    .then(function (scene) {
      return refreshSceneList(scene.scene_id).then(function () {
        var body = {};
        if (lines.length) body.line_ids = lines;
        if (segments.length) body.segment_ids = segments;
        return fetchJson('/api/scenes/' + scene.scene_id + '/jobs', body);
      });
    })
    .then(function (result) {
      activeSceneJobId = result.job.id;
      $('sceneJobSelect').value = activeSceneJobId;
      return refreshSceneJob();
    })
    .catch(showSceneError);
});
['pause', 'resume', 'cancel', 'retry'].forEach(function (action) {
  $('scene' + action.charAt(0).toUpperCase() + action.slice(1)).addEventListener('click', function () {
    try {
      sceneAction(action).catch(showSceneError);
    } catch (error) {
      showSceneError(error);
    }
  });
});
$('sceneJobRefresh').addEventListener('click', function () {
  refreshSceneJob().catch(showSceneError);
});
$('scenePreview').addEventListener('click', function () {
  previewScene().catch(showSceneError);
});
// Export package panel: the scene package exporter through /api/exports. Requests use the saved scene revision only.
function exportNumber(id, label) {
  var field = $(id),
    text = field.value.trim();
  if (text === '') return null;
  var value = Number(text);
  if (!isFinite(value)) throw new Error(label + ' must be a number.');
  if (field.min !== '' && value < Number(field.min)) throw new Error(label + ' must be ' + field.min + ' or more.');
  if (field.max !== '' && value > Number(field.max)) throw new Error(label + ' must be ' + field.max + ' or less.');
  return value;
}
function exportRequest() {
  if (!activeSceneId) throw new Error('Save a scene before exporting it.');
  if (sceneDirty) throw new Error('Save or discard unsaved scene edits before exporting.');
  var formats = [].slice.call(document.querySelectorAll('input[name=exportFormat]:checked')).map(function (box) {
    return box.value;
  });
  if (!formats.length) throw new Error('Choose at least one format.');
  var lufs = exportNumber('exportLufs', 'Loudness target');
  return {
    scene_id: activeSceneId,
    formats: formats,
    filename_template: $('exportTemplate').value,
    output_rate: exportNumber('exportRate', 'Sample rate'),
    trim_start_seconds: exportNumber('exportTrimStart', 'Trim start') || 0,
    trim_end_seconds: exportNumber('exportTrimEnd', 'Trim end') || 0,
    fade_in_ms: exportNumber('exportFadeIn', 'Fade in') || 0,
    fade_out_ms: exportNumber('exportFadeOut', 'Fade out') || 0,
    loudness_lufs: lufs,
    loudness_true_peak: lufs === null ? null : exportNumber('exportTruePeak', 'True peak'),
    limiter_dbfs: exportNumber('exportLimiter', 'Limiter ceiling'),
  };
}
function showExportError(error) {
  $('exportError').textContent = error.message || String(error);
}
function showExportLog(text) {
  $('exportLog').textContent = text || '';
}
// What FFmpeg can do decides what the panel offers: without it nothing can be written or checked, and without an
// encoder only that format is switched off. The server says which; the page never guesses from the browser.
function applyExportFfmpeg(report) {
  var note = $('exportFfmpeg');
  if (!report) {
    note.textContent = '';
    return;
  }
  var ready = !!report.ready,
    boxes = [].slice.call(document.querySelectorAll('input[name=exportFormat]'));
  note.className = ready && !report.message ? 'meta' : 'meta take-checks flagged';
  note.textContent = ready
    ? report.message || 'FFmpeg ' + (report.version || '') + ' found; every format is available.'
    : report.message + ' Refresh list checks again.';
  ['exportPreview', 'exportCreate', 'exportVerify'].forEach(function (id) {
    $(id).disabled = !ready;
  });
  boxes.forEach(function (box) {
    var usable = !!(report.formats && report.formats[box.value]);
    box.disabled = !usable;
    if (!usable) box.checked = false;
  });
  if (
    ready &&
    !boxes.some(function (box) {
      return box.checked;
    })
  ) {
    var wav = boxes.filter(function (box) {
      return box.value === 'wav';
    })[0];
    if (wav && !wav.disabled) wav.checked = true;
  }
  renderFfmpegOffer('export', report); // voice_lab_system.js: the Install FFmpeg button and its progress
}
function refreshExports(preferred, recheck) {
  return fetchJson('/api/exports' + (recheck ? '?recheck=1' : '')).then(function (result) {
    var select = $('exportSelect'),
      keep = preferred || select.value;
    select.textContent = '';
    (result.exports || []).forEach(function (row) {
      option(select, row.name, row.name + ' / ' + (row.scene_name || 'unknown scene') + ' / ' + row.status);
    });
    if (keep) select.value = keep;
    applyExportFfmpeg(result.ffmpeg);
    return result.exports || [];
  });
}
$('exportPreview').addEventListener('click', function () {
  var body;
  try {
    body = exportRequest();
  } catch (error) {
    showExportError(error);
    return;
  }
  $('exportError').textContent = '';
  $('exportStatus').textContent = 'Checking provenance and settings…';
  fetchJson('/api/exports/preview', body)
    .then(function (result) {
      $('exportStatus').textContent = 'Dry run only; no files were written.';
      showExportLog(result.log);
    })
    .catch(function (error) {
      $('exportStatus').textContent = '';
      showExportError(error);
    });
});
$('exportCreate').addEventListener('click', function () {
  var body;
  try {
    body = exportRequest();
  } catch (error) {
    showExportError(error);
    return;
  }
  var button = this;
  button.disabled = true;
  $('exportError').textContent = '';
  showExportLog('');
  $('exportStatus').textContent = 'Writing the package. Large scenes can take a minute or more.';
  fetchJson('/api/exports', body)
    .then(function (result) {
      $('exportStatus').textContent = 'Package written to ' + result.export.path + '. Verify it before sharing.';
      showExportLog(result.log);
      return refreshExports(result.export.name);
    })
    .catch(function (error) {
      $('exportStatus').textContent = '';
      showExportError(error);
    })
    .then(function () {
      button.disabled = false;
    });
});
$('exportVerify').addEventListener('click', function () {
  var name = $('exportSelect').value;
  if (!name) {
    showExportError(new Error('Choose a package to verify.'));
    return;
  }
  $('exportError').textContent = '';
  $('exportStatus').textContent = 'Verifying ' + name + '…';
  fetchJson('/api/exports/' + encodeURIComponent(name) + '/verify', {})
    .then(function (result) {
      $('exportStatus').textContent = 'Verification passed for ' + name + '.';
      showExportLog(result.log);
    })
    .catch(function (error) {
      $('exportStatus').textContent = '';
      showExportError(error);
    });
});
$('exportRefresh').addEventListener('click', function () {
  refreshExports(undefined, true).catch(showExportError);
});
$('sceneExport').addEventListener('toggle', function () {
  if (this.open) refreshExports().catch(showExportError);
});
