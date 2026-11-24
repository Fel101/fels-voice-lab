// Voice Lab scenes: script editor, batch renders, assembly previews and the export panel.
var activeSceneId = null,
  activeSceneJobId = null,
  scenePollTimer = null,
  sceneListCount = null,
  sampleState = null;
var sceneTimelineMaxMs = 30 * 60 * 1000;
function sceneCueTime(milliseconds) {
  var hours = Math.floor(milliseconds / 3600000);
  return (hours ? String(hours).padStart(2, '0') + ':' : '') +
    String(Math.floor(milliseconds % 3600000 / 60000)).padStart(2, '0') + ':' +
    (milliseconds % 60000 / 1000).toFixed(3).padStart(6, '0');
}
function sceneCueRange(cue, offset = 0) {
  return sceneCueTime(cue.start_ms + offset) + ' – ' + sceneCueTime(cue.end_ms + offset);
}
function parseSceneCueTime(value) {
  var text = String(value).trim(), seconds;
  var clock = /^(\d+):([0-5]\d(?:\.\d{1,3})?)$/.exec(text);
  if (clock) seconds = Number(clock[1]) * 60 + Number(clock[2]);
  else if (/^\d+(?:\.\d{1,3})?$/.test(text)) seconds = Number(text);
  if (seconds === undefined || seconds > sceneTimelineMaxMs / 1000)
    throw new Error(LabText.t('scenes.enter_a_time_such_as'));
  return Math.round(seconds * 1000);
}
function showSceneError(error) {
  var message = error.message || String(error);
  try {
    var scene = sceneBody();
    if (scene.timeline) scene.lines.forEach(function (line, index) {
      if (line.cue) message = message.replaceAll(line.line_id, LabText.t('scenes.line_2', {number: index + 1, timestamp: sceneCueTime(line.cue.start_ms)}));
    });
  } catch (_) {
    // The editor can contain unfinished JSON.
  }
  $('sceneError').textContent = message;
}
function sceneBody() {
  var value;
  try {
    value = JSON.parse($('sceneEditor').value);
  } catch (error) {
    throw new Error(LabText.t('scenes.scene_json_could_not_be', {message: error.message}));
  }
  if (!value || !Array.isArray(value.lines)) throw new Error(LabText.t('scenes.scene_manifest_needs_a_lines'));
  return value;
}
function markSceneDirty() {
  sceneDirty = true;
  $('sceneError').textContent = '';
  $('sceneStatus').textContent = LabText.t('scenes.unsaved_script_changes_save_before');
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
// Each saved line as JSON, by line id: a row whose line differs has unsaved edits, and its status waits for a save.
var sceneSavedLines = {};
function savedSceneLines(savedText) {
  var lines = {};
  try {
    (JSON.parse(savedText || 'null') || { lines: [] }).lines.forEach(function (line) {
      lines[line.line_id] = JSON.stringify(line);
    });
  } catch (error) {
    return {};
  }
  return lines;
}
function resetSceneHistory(savedText) {
  sceneSavedText = savedText;
  sceneSavedLines = savedSceneLines(savedText);
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
    ? LabText.t('scenes.unsaved_script_changes_save_before')
    : LabText.t('scenes.back_to_the_saved_script');
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
    if (key !== 'name') throw new Error(LabText.t('scenes.unknown_scene_field'));
    scene.name = value;
    return scene;
  }
  if (!scene.lines[index]) throw new Error(LabText.t('scenes.scene_line_no_longer_exists'));
  if (key === 'cue.start_ms' || key === 'cue.end_ms') {
    var line = scene.lines[index];
    if (!line.cue) throw new Error(LabText.t('scenes.open_a_timed_line_before'));
    var milliseconds = parseSceneCueTime(value);
    line.cue[key === 'cue.start_ms' ? 'start_ms' : 'end_ms'] = milliseconds;
    line.target_duration_ms = line.cue.end_ms - line.cue.start_ms;
    return scene;
  }
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
    throw new Error(LabText.t('scenes.unknown_line_field'));
  if (['pause_before_ms', 'pause_after_ms', 'take_count', 'target_duration_ms'].indexOf(key) >= 0) {
    if (key === 'target_duration_ms' && value === '') {
      scene.lines[index][key] = null;
      return scene;
    }
    var min = key === 'take_count' || key === 'target_duration_ms' ? 1 : 0,
      max = key === 'take_count' ? 10 : key === 'target_duration_ms' ? 120000 : 10000;
    if (value === '' || !Number.isInteger(Number(value)) || Number(value) < min || Number(value) > max)
      throw new Error(LabText.t('scenes.enter_a_whole_number_from', {min: min, max: max}));
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
  if (!control) throw new Error(LabText.t('scenes.this_checkpoint_does_not_support', {key: key}));
  if (control.type === 'select') {
    if ((control.choices || []).indexOf(value) < 0) throw new Error(LabText.t('scenes.choose_a_supported', {label: control.label}));
  } else {
    value = Number(value);
    if (!Number.isFinite(value) || value < control.minimum || value > control.maximum)
      throw new Error(LabText.t('scenes.must_be_between_and', {label: control.label, minimum: control.minimum, maximum: control.maximum}));
    if (control.step === 1 && !Number.isInteger(value)) throw new Error(LabText.t('scenes.must_be_a_whole_number', {label: control.label}));
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
  var scene = editSceneField(sceneBody(), index, key, value);
  updateSceneEditor(scene, 'field:' + index + ':' + key);
  if (index !== null) paintSceneRow(scene.lines[index]);
}
function changeSceneSetting(index, key, value) {
  var scene = editSceneSetting(sceneBody(), index, key, value);
  updateSceneEditor(scene, 'setting:' + index + ':' + key);
  paintSceneRow(scene.lines[index]);
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
    text: LabText.t('scenes.new_dialogue_line'),
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
  if (!found) throw new Error(LabText.t('scenes.choose_a_voice_from_the'));
  var old = scene.lines[index];
  if (!old) throw new Error(LabText.t('scenes.scene_line_no_longer_exists'));
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
    if (old.direction) resets.push(LabText.t('scenes.acting_direction'));
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
  if (scene.timeline && $('sceneDub').open) paintDubLanguages(scene);
  if (resets.length) $('sceneStatus').textContent += LabText.t('scenes.voice_change_reset_unsupported_values', {settings: resets.join(', ')});
}
function mutateSceneLine(index, action) {
  var scene = sceneBody(),
    line = scene.lines[index];
  if (!line) throw new Error(LabText.t('scenes.scene_line_no_longer_exists'));
  if (action === 'duplicate') {
    var copy = JSON.parse(JSON.stringify(line));
    copy.line_id = line.cue ? 'cue-' + crypto.randomUUID() : stableSceneLineId(scene.lines);
    if (copy.cue && !copy.cue.source_line_ids)
      copy.cue.source_line_ids = line.cue.source_text ? [line.line_id] : [];
    copy.selected_take_id = null;
    copy.selected_takes = {};
    scene.lines.splice(index + 1, 0, copy);
  } else if (action === 'remove') {
    if (scene.lines.length <= 1) throw new Error(LabText.t('scenes.a_scene_must_keep_at'));
    scene.lines.splice(index, 1);
  } else if (action === 'up' || action === 'down') {
    var target = index + (action === 'up' ? -1 : 1);
    if (target < 0 || target >= scene.lines.length) return scene;
    scene.lines.splice(index, 1);
    scene.lines.splice(target, 0, line);
  } else throw new Error(LabText.t('scenes.unknown_scene_line_action'));
  updateSceneEditor(scene);
  renderSceneForm();
  return scene;
}
function replaceTimedCue(scene, index, splitMs, offset) {
  var first = scene.lines[index],
    next = scene.lines[index + 1],
    merge = splitMs === null;
  if (!first || !first.cue) throw new Error(LabText.t('scenes.open_a_timed_cue_to'));
  if (merge) {
    if (!next || !next.cue || next.cue.start_ms < first.cue.start_ms)
      throw new Error(LabText.t('scenes.choose_a_cue_followed_by'));
    var controls = ['identity_id', 'reference_version_id', 'language', 'direction', 'use_pronunciations', 'line_type', 'settings'];
    if (controls.some(function (key) { return JSON.stringify(first[key]) !== JSON.stringify(next[key]); }))
      throw new Error(LabText.t('scenes.match_the_next_cue_s'));
  } else if (!Number.isInteger(splitMs) || splitMs <= first.cue.start_ms || splitMs >= first.cue.end_ms) {
    throw new Error(LabText.t('scenes.choose_a_split_time_between'));
  } else if (scene.lines.length >= 500) {
    throw new Error(LabText.t('scenes.a_scene_can_contain_up'));
  }
  var originals = merge ? [first, next] : [first],
    replacements = new Set(),
    sources = new Set();
  originals.forEach(function (line) {
    (line.cue.source_line_ids || (line.cue.source_text ? [line.line_id] : [])).forEach(function (id) { sources.add(id); });
    [line.line_id].concat(line.cue.replaces || []).forEach(function (id) { replacements.add(id); });
  });
  function lineFor(text, start, end) {
    text = text.trim();
    if (!text || text.length > 10000) throw new Error(LabText.t('scenes.each_new_cue_needs_1'));
    var line = Object.assign({}, first, {
      line_id: 'cue-' + crypto.randomUUID(),
      text: text,
      cue: { start_ms: start, end_ms: end, source_line_ids: Array.from(sources), replaces: Array.from(replacements) },
      target_duration_ms: end - start,
      selected_take_id: null,
      selected_takes: {},
    });
    delete line.segments;
    return line;
  }
  var newLines = merge
    ? [lineFor(first.text + '\n' + next.text, first.cue.start_ms, Math.max(first.cue.end_ms, next.cue.end_ms))]
    : [lineFor(first.text.slice(0, offset), first.cue.start_ms, splitMs),
       lineFor(first.text.slice(offset), splitMs, first.cue.end_ms)];
  scene.lines.splice(index, merge ? 2 : 1, ...newLines);
  return newLines;
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
  if (!character || !identity) throw new Error(LabText.t('scenes.create_a_character_voice_in'));
  var lineId = stableSceneLineId(scene.lines);
  var line = sceneDefaultLine(character, identity, lineId);
  if (scene.timeline) {
    line.line_id = lineId = 'cue-' + crypto.randomUUID();
    var start = scene.lines.reduce(function (latest, other) { return Math.max(latest, other.cue.end_ms); }, 0);
    if (start >= sceneTimelineMaxMs) throw new Error(LabText.t('scenes.edit_the_last_cue_to'));
    line.cue = { start_ms: start, end_ms: Math.min(start + 2000, sceneTimelineMaxMs) };
    line.pause_after_ms = 0;
    line.target_duration_ms = line.cue.end_ms - line.cue.start_ms;
  }
  scene.lines.push(line);
  sceneOpenLines.add(lineId); // A new line opens, ready to write.
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
  text.value = line.manuscript_spoken === line.text && line.manuscript_text !== undefined ? line.manuscript_text : line.text || '';
  text.setAttribute('data-scene-field', 'text');
  text.setAttribute('aria-label', LabText.t('scenes.dialogue', {line_label: line.cue ? LabText.t('scenes.numbered_line', {number: index+1}) : line.line_id}));
  text.addEventListener('input', function () {
    try {
      changeSceneField(index, 'text', this.value);
      this.setCustomValidity('');
    } catch (error) {
      this.setCustomValidity(error.message);
      showSceneError(error);
    }
  });
  sceneLabel(LabText.t('scenes.dialogue_2'), text, article, false);
  var assignment = document.createElement('div');
  assignment.className = 'cast-grid';
  var chars = document.createElement('select');
  castCharacters.forEach(function (item) {
    option(chars, item.id, item.name);
  });
  chars.value = character || '';
  sceneLabel(LabText.t('scenes.character'), chars, assignment, true);
  chars.addEventListener('change', function () {
    var selected = castCharacters.find(function (item) {
        return item.id === this.value;
      }, this),
      next = selected && (selected.identities || [])[0];
    if (!next) {
      showSceneError(new Error(LabText.t('scenes.add_a_voice_to_this')));
      return;
    }
    changeSceneIdentity(index, next.id);
  });
  var voices = document.createElement('select');
  if (identityInfo) {
    (identityInfo.character.identities || []).forEach(function (item) {
      option(voices, item.id, item.engine_id + ' / ' + item.label + ' — ' + LabText.statusName(item.status) + (item.retired ? LabText.t('scenes.retired') : ''));
    });
  }
  voices.value = line.identity_id || '';
  sceneLabel(LabText.t('scenes.voice'), voices, assignment, true);
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
          (ref.id === identity.approved_reference_id ? LabText.t('scenes.approved_version', {version: ref.version, seconds: ref.duration_seconds.toFixed(2)}) : LabText.t('scenes.candidate_version', {version: ref.version, seconds: ref.duration_seconds.toFixed(2)})),
        );
      });
    if (!references.options.length) option(references, '', LabText.t('scenes.no_saved_voice_recording'), true);
    references.value =
      line.reference_version_id || identity.approved_reference_id || ((identity.references || [])[0] || {}).id || '';
    sceneLabel(LabText.t('scenes.voice_recording'), references, assignment, true);
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
  sceneLabel(LabText.t('scenes.language'), language, controls, true);
  language.addEventListener('change', function () {
    changeSceneField(index, 'language', this.value);
  });
  var lineType = document.createElement('select');
  [
    ['dialogue', LabText.t('scenes.dialogue_2')],
    ['narration', LabText.t('scenes.narration')],
  ].forEach(function (item) {
    option(lineType, item[0], item[1]);
  });
  lineType.value = line.line_type || 'dialogue';
  sceneLabel(LabText.t('scenes.line_type'), lineType, controls, true);
  lineType.addEventListener('change', function () {
    changeSceneField(index, 'line_type', this.value);
  });
  var numericFields = line.cue ? ['take_count'] : ['pause_before_ms', 'pause_after_ms', 'take_count', 'target_duration_ms'];
  if (line.cue) {
    ['start_ms', 'end_ms'].forEach(function (key) {
      var input = document.createElement('input');
      input.type = 'text';
      input.required = true;
      input.value = sceneCueTime(line.cue[key]);
      input.placeholder = '00:03.500';
      var label = key === 'start_ms' ? LabText.t('scenes.cue_start') : LabText.t('scenes.cue_end');
      input.setAttribute('aria-label', LabText.t('scenes.for_line', {label: label, number: index + 1}));
      sceneLabel(label, input, controls, true);
      input.addEventListener('input', function () {
        try {
          changeSceneField(index, 'cue.' + key, this.value);
          this.setCustomValidity('');
          $('sceneError').textContent = '';
        } catch (error) {
          this.setCustomValidity(error.message);
          showSceneError(error);
        }
      });
    });
    var sourceRows = line.cue.source_text
      ? [{ start_ms: line.cue.source_start_ms, end_ms: line.cue.source_end_ms, text: line.cue.source_text }]
      : ((sceneBody().timeline || {}).source_cues || []).filter(function (source) {
          return (line.cue.source_line_ids || []).includes(source.line_id);
        });
    sourceRows.forEach(function (source) {
      var note = document.createElement('p');
      note.className = 'meta';
      var videoStart = sceneBody().timeline.source_offset_ms || 0;
      note.textContent = LabText.t('scenes.original_subtitle', {cue_range: sceneCueRange(source, videoStart), text: source.text});
      article.appendChild(note);
    });
    var cueTools = document.createElement('div');
    cueTools.className = 'row';
    var splitTime = document.createElement('input');
    splitTime.type = 'text';
    splitTime.value = sceneCueTime(Math.floor((line.cue.start_ms + line.cue.end_ms) / 2));
    sceneLabel(LabText.t('scenes.split_at'), splitTime, cueTools, true);
    function applyCueEdit(splitMs, offset) {
      try {
        var scene = sceneBody();
        replaceTimedCue(scene, index, splitMs, offset).forEach(function (cue) { sceneOpenLines.add(cue.line_id); });
        updateSceneEditor(scene);
        renderSceneForm();
      } catch (error) {
        showSceneError(error);
      }
    }
    cueTools.appendChild(sceneButton(LabText.t('scenes.split_at_text_cursor'), LabText.t('scenes.split_cue_at_text_cursor'), function () {
      try {
        applyCueEdit(parseSceneCueTime(splitTime.value), text.selectionStart);
      } catch (error) {
        showSceneError(error);
      }
    }));
    var merge = sceneButton(LabText.t('scenes.merge_with_next_cue'), LabText.t('scenes.merge_with_next_cue'), function () { applyCueEdit(null, 0); });
    merge.disabled = index + 1 >= sceneBody().lines.length;
    cueTools.appendChild(merge);
    article.appendChild(cueTools);
  }
  numericFields.forEach(function (key) {
    var input = document.createElement('input');
    input.type = 'number';
    input.step = '1';
    input.value = line[key] === null || line[key] === undefined ? '' : line[key];
    input.min = key === 'take_count' || key === 'target_duration_ms' ? '1' : '0';
    input.max = key === 'take_count' ? '10' : key === 'target_duration_ms' ? '120000' : '10000';
    input.required = key !== 'target_duration_ms';
    var labelText = {
      pause_before_ms: LabText.t('scenes.pause_before_ms'),
      pause_after_ms: LabText.t('scenes.pause_after_ms'),
      take_count: LabText.t('scenes.takes_per_line'),
      target_duration_ms: LabText.t('scenes.target_duration_ms_optional'),
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
  pronLabel.appendChild(document.createTextNode(LabText.t('scenes.apply_this_character_s_pronunciation')));
  controls.appendChild(pronLabel);
  article.appendChild(controls);
  var schema = performanceCapabilitiesByEngine[line.engine_id] || { controls: [], direction: { status: 'unsupported' } };
  var directionStatus = (schema.direction || {}).status || 'unsupported';
  if (['supported', 'worker_verified', 'experimental'].indexOf(directionStatus) >= 0) {
    var direction = document.createElement('textarea');
    direction.maxLength = 1000;
    direction.value = line.direction || '';
    direction.setAttribute('aria-label', LabText.t('scenes.acting_direction_2', {line_label: line.cue ? LabText.t('scenes.numbered_line', {number: index+1}) : line.line_id}));
    sceneLabel((directionStatus === 'experimental' ? LabText.t('scenes.experimental_direction') : LabText.t('scenes.acting_direction_3')), direction, article, false);
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
  settingsTitle.textContent = LabText.t('scenes.per_line_model_settings');
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
    var labelText = LabText.label('control.' + control.id, control.label) + (control.status === 'experimental' ? LabText.t('scenes.experimental') : '');
    input.setAttribute('aria-label', labelText + ' — ' + (line.cue ? 'line ' + (index + 1) : line.line_id));
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
  settingsHelp.textContent = LabText.t('scenes.each_take_keeps_the_values');
  settings.appendChild(settingsHelp);
  article.appendChild(settings);
  var actions = document.createElement('div');
  actions.className = 'cast-actions';
  [
    ['up', LabText.t('scenes.move_up')],
    ['down', LabText.t('scenes.move_down')],
    ['duplicate', LabText.t('scenes.duplicate_line')],
    ['remove', LabText.t('scenes.remove_line')],
  ].forEach(function (item) {
    var button = document.createElement('button');
    button.type = 'button';
    button.textContent = item[1];
    button.setAttribute('aria-label', item[1] + ' ' + (line.cue ? 'line ' + (index + 1) : line.line_id));
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
  var lineId = document.createElement('p');
  lineId.className = 'meta';
  lineId.textContent = line.cue
    ? LabText.t('scenes.line_3', {number: index + 1, cue_range: sceneCueRange(line.cue)})
    : LabText.t('scenes.line_name_exports_and_the', {line_id: line.line_id});
  article.appendChild(lineId);
}
// The scenes table: one row per line, its status from GET /api/scenes/{id}/lines (one answer for the whole scene). A row's
// takes and form are built the first time it opens, so a long scene opens at once.
var sceneLineStates = {},
  sceneTicked = new Set(),
  sceneOpenLines = new Set(),
  sceneRows = {},
  sceneJobSignature = null,
  scenePlayer = null,
  scenePlayerButton = null;
var sceneStatusWords = {
  unsaved: LabText.t('scenes.not_saved_yet'),
  not_rendered: LabText.t('scenes.not_rendered'),
  waiting: LabText.t('scenes.waiting'),
  rendering: LabText.t('scenes.rendering'),
  failed: LabText.t('scenes.failed'),
  out_of_date: LabText.t('scenes.out_of_date'),
  choose_take: LabText.t('scenes.choose_a_take'),
  done: LabText.t('scenes.done'),
};
var sceneStatusCounts = {
  unsaved: function (count) { return LabText.plural('scenes.unsaved_lines', count); },
  not_rendered: function (count) { return LabText.plural('scenes.not_rendered_lines', count); },
  waiting: function (count) { return LabText.plural('scenes.waiting_lines', count); },
  rendering: function (count) { return LabText.plural('scenes.rendering_lines', count); },
  failed: function (count) { return LabText.plural('scenes.failed_lines', count); },
  out_of_date: function (count) { return LabText.plural('scenes.out_of_date_lines', count); },
  choose_take: function (count) { return LabText.plural('scenes.choose_take_lines', count); },
  done: function (count) { return LabText.plural('scenes.done_lines', count); },
};
var sceneNeedsRender = ['not_rendered', 'out_of_date', 'failed'];
function sceneModelName(engineId) {
  var entry = ((catalog && catalog.engines) || []).find(function (item) {
    return item.id === engineId;
  });
  return entry ? entry.name : engineId || LabText.t('scenes.no_model');
}
function sceneShortText(text) {
  var value = String(text || '')
    .replace(/\s+/g, ' ')
    .trim();
  return value.length > 90 ? value.slice(0, 89) + '…' : value || LabText.t('scenes.empty_line');
}
function sceneRowState(line) {
  var server = sceneLineStates[line.line_id] || null;
  if (sceneSavedLines[line.line_id] !== JSON.stringify(line)) return { status: 'unsaved', reason: '', server: server };
  return server ? { status: server.status, reason: server.reason, server: server } : { status: '', reason: '', server: null };
}
function sceneOrderedRows() {
  return Object.keys(sceneRows)
    .map(function (key) {
      return sceneRows[key];
    })
    .sort(function (a, b) {
      return a.index - b.index;
    });
}
function sceneButton(text, label, handler) {
  var button = document.createElement('button');
  button.type = 'button';
  button.textContent = text;
  if (label) button.setAttribute('aria-label', label);
  button.addEventListener('click', handler);
  return button;
}
function renderSceneForm() {
  var container = $('sceneLineEditor');
  container.textContent = '';
  sceneRows = {};
  stopScenePlayer();
  $('sceneName').disabled = true;
  $('sceneLineAdd').disabled = true;
  $('sceneTableTools').hidden = true;
  $('sceneWork').hidden = !$('sceneEditor').value.trim();
  if ($('sceneWork').hidden) {
    $('sceneName').value = '';
    container.textContent = LabText.t('scenes.open_a_scene_or_start');
    return;
  }
  var scene;
  try {
    scene = sceneBody();
  } catch (error) {
    container.textContent = LabText.t('scenes.fix_the_advanced_json_to');
    showSceneError(error);
    return;
  }
  $('sceneName').value = scene.name || '';
  $('sceneName').disabled = false;
  $('sceneLineAdd').disabled = false;
  $('sceneError').textContent = '';
  if (!scene.lines.length) {
    container.textContent = LabText.t('scenes.this_script_has_no_dialogue');
    return;
  }
  var wrap = document.createElement('div'),
    table = document.createElement('table'),
    head = table.createTHead().insertRow();
  wrap.className = 'scene-table-wrap';
  table.className = 'scene-table';
  [
    ['tick', LabText.t('scenes.ticked')],
    ['number', '#'],
    ['character', LabText.t('scenes.character')],
    ['line', LabText.t('scenes.line')],
    ['voice', LabText.t('scenes.voice')],
    ['status', LabText.t('scenes.status')],
    ['actions', LabText.t('scenes.actions')],
  ].forEach(function (column) {
    var cell = document.createElement('th'),
      name = column[1];
    cell.scope = 'col';
    cell.className = 'scene-col-' + column[0];
    if (column[0] === 'tick' || column[0] === 'actions') {
      var hidden = document.createElement('span');
      hidden.className = 'visually-hidden';
      hidden.textContent = name;
      cell.appendChild(hidden);
    } else cell.textContent = name;
    head.appendChild(cell);
  });
  if (scene.lines.length === 1) sceneOpenLines.add(scene.lines[0].line_id); // A one-line script shows its form.
  scene.lines.forEach(function (line, index) {
    table.appendChild(buildSceneRow(line, index));
  });
  wrap.appendChild(table);
  container.appendChild(wrap);
  $('sceneTableTools').hidden = false;
  fillSceneCharacterFilter(scene);
  applySceneFilters();
}
function buildSceneRow(line, index) {
  var body = document.createElement('tbody'),
    main = body.insertRow(),
    detail = body.insertRow(),
    number = index + 1;
  body.className = 'scene-line';
  main.className = 'scene-row';
  var tick = document.createElement('input');
  tick.type = 'checkbox';
  tick.checked = sceneTicked.has(line.line_id);
  tick.setAttribute('aria-label', LabText.t('scenes.tick_line', {number: number}));
  tick.addEventListener('change', function () {
    if (this.checked) sceneTicked.add(line.line_id);
    else sceneTicked.delete(line.line_id);
    updateSceneTools();
  });
  main.insertCell().appendChild(tick);
  var count = main.insertCell();
  count.className = 'scene-number';
  count.textContent = String(number);
  var who = main.insertCell(),
    character = castCharacters.find(function (item) {
      return item.id === line.character_id;
    }),
    identityInfo = sceneIdentityById(line.identity_id),
    words = main.insertCell(),
    voice = main.insertCell();
  who.className = 'scene-character';
  who.textContent = character ? character.name : LabText.t('scenes.no_character');
  who.title = who.textContent;
  voice.className = 'meta scene-voice';
  voice.textContent =
    (identityInfo ? identityInfo.identity.label + ' · ' : '') +
    sceneModelName(line.engine_id || (identityInfo && identityInfo.identity.engine_id));
  voice.title = voice.textContent;
  var status = main.insertCell(),
    actions = main.insertCell();
  words.className = 'scene-text';
  status.className = 'scene-status';
  actions.className = 'scene-actions';
  var play = sceneButton(LabText.t('scenes.play'), LabText.t('scenes.play_line', {number: number}), function () {
      playSceneLine(line.line_id, play, number);
    }),
    render = sceneButton(LabText.t('scenes.render'), LabText.t('scenes.render_line', {number: number}), function () {
      startSceneBatch({ line_ids: [line.line_id] }).catch(showSceneError);
    }),
    open = sceneButton(LabText.t('scenes.open'), LabText.t('scenes.line_details', {number: number}), function () {
      setOpen(detail.hidden);
    });
  play.className = 'scene-play';
  open.className = 'scene-open';
  open.setAttribute('aria-expanded', 'false');
  actions.appendChild(play);
  actions.appendChild(render);
  actions.appendChild(open);
  detail.className = 'scene-detail';
  detail.hidden = true;
  var panel = detail.insertCell();
  panel.colSpan = 7;
  var row = {
    body: body,
    line: line,
    index: index,
    tick: tick,
    words: words,
    status: status,
    play: play,
    panel: panel,
    state: '',
    takes: null,
    takesShown: '',
  };
  function setOpen(value) {
    detail.hidden = !value;
    open.textContent = value ? LabText.t('scenes.close') : LabText.t('scenes.open');
    open.setAttribute('aria-expanded', String(value));
    if (value) sceneOpenLines.add(line.line_id);
    else sceneOpenLines.delete(line.line_id);
    if (value && !row.takes) {
      row.takes = document.createElement('div');
      row.takes.className = 'scene-takes';
      panel.appendChild(row.takes);
      renderSceneLineEditor(row.line, index, panel);
      paintSceneRow(row.line);
    }
  }
  sceneRows[line.line_id] = row;
  paintSceneRow(line);
  if (sceneOpenLines.has(line.line_id)) setOpen(true);
  return body;
}
function paintSceneRow(line) {
  var row = line && sceneRows[line.line_id];
  if (!row) return;
  var state = sceneRowState(line),
    segments = (state.server && state.server.segments) || [];
  row.line = line;
  row.state = state.status;
  row.body.setAttribute('data-status', state.status);
  row.words.textContent = (line.cue ? sceneCueRange(line.cue) + ' · ' : '') + sceneShortText(line.text);
  row.words.title = line.text || '';
  row.status.textContent = '';
  var word = document.createElement('span');
  word.className = 'scene-status-word';
  word.textContent = sceneStatusWords[state.status] || LabText.t('scenes.checking');
  row.status.appendChild(word);
  if (segments.length > 1 && state.status !== 'done' && state.status !== 'unsaved')
    row.status.appendChild(
      document.createTextNode(LabText.t('scenes.of_parts_done', {segments_done: state.server.segments_done, parts: segments.length})),
    );
  if (state.reason) {
    var why = document.createElement('span');
    why.className = 'meta scene-reason';
    why.textContent = state.reason;
    row.status.appendChild(why);
  }
  if (line.cue && state.status === 'done') {
    var chosenSeconds = segments.reduce(function (total, segment) {
      var take = (segment.takes || []).find(function (entry) { return entry.id === segment.chosen_take_id; });
      return total + (take ? Number(take.duration_seconds) : 0);
    }, 0);
    var overrun = chosenSeconds - (line.cue.end_ms - line.cue.start_ms) / 1000;
    if (overrun >= 0.001) {
      var warning = document.createElement('span');
      warning.className = 'meta scene-reason';
      var excess = overrun >= 0.05 ? overrun.toFixed(1) + ' s' : LabText.t('scenes.less_than_0_1_s');
      warning.textContent = LabText.t('scenes.too_long_by_render_again', {excess: excess});
      row.status.appendChild(warning);
    }
  }
  row.play.disabled = !segments.some(function (segment) {
    return segment.chosen_take_id;
  });
  // An open row's takes are drawn again only when they change, so a take being played is not cut off by a poll.
  var shown = state.status + JSON.stringify(state.server);
  if (row.takes && row.takesShown !== shown) {
    row.takesShown = shown;
    renderSceneTakes(row.takes, line, state);
  }
}
function paintSceneRows() {
  var scene;
  try {
    scene = sceneBody();
  } catch (error) {
    return;
  }
  scene.lines.forEach(paintSceneRow);
  applySceneFilters();
}
function sceneTakeTime(value) {
  var date = new Date(value);
  return isNaN(date.getTime()) ? String(value || '') : LabText.dateTime(date);
}
function renderSceneTakes(container, line, state) {
  container.textContent = '';
  var heading = document.createElement('h3');
  heading.textContent = LabText.t('scenes.takes');
  container.appendChild(heading);
  if (!state.server) {
    var none = document.createElement('p');
    none.className = 'meta';
    none.textContent = state.status === 'unsaved' ? LabText.t('scenes.save_the_scene_to_see') : LabText.t('scenes.checking');
    container.appendChild(none);
    return;
  }
  var segments = state.server.segments || [];
  segments.forEach(function (segment, part) {
    var block = document.createElement('div');
    block.className = 'scene-part';
    if (segments.length > 1) {
      var source = (line.segments || []).find(function (item) {
          return item.id === segment.segment_id;
        }),
        title = document.createElement('p');
      title.textContent =
        (LabText.t('scenes.part_of', {number: part + 1, parts: segments.length, status: sceneStatusWords[segment.status] || segment.status}) + (segment.reason ? '. ' + segment.reason : '') + (source ? ' “' + sceneShortText(source.text) + '”' : ''));
      block.appendChild(title);
      block.appendChild(
        sceneButton(LabText.t('scenes.render_this_part'), LabText.t('scenes.render_part_of_line', {number: part + 1, line_id: line.line_id || ''}), function () {
          startSceneBatch({ segment_ids: [line.line_id + '/' + segment.segment_id] }).catch(showSceneError);
        }),
      );
    }
    if (!segment.takes.length) {
      var empty = document.createElement('p');
      empty.className = 'meta';
      empty.textContent = LabText.t('scenes.no_takes_yet_render_makes');
      block.appendChild(empty);
    }
    segment.takes.forEach(function (take) {
      var item = document.createElement('div'),
        audio = document.createElement('audio'),
        about = document.createElement('p');
      item.className = 'scene-take';
      audio.controls = true;
      audio.preload = 'none';
      audio.src = take.audio_url;
      if (take.licence) appendLicenceBadge(item, take.licence);
      item.appendChild(audio);
      about.className = 'meta';
      about.textContent =
        ((take.chosen ? LabText.t('scenes.chosen_take_made', {date: sceneTakeTime(take.created_at), seconds: Number(take.duration_seconds).toFixed(1)}) : LabText.t('scenes.take_made', {date: sceneTakeTime(take.created_at), seconds: Number(take.duration_seconds).toFixed(1)})) + (take.reason ? ' ' + take.reason : ''));
      item.appendChild(about);
      if (take.waveform_checks && (take.waveform_checks.flags || []).length) appendTakeChecks(item, take.waveform_checks);
      if (take.fits && !take.chosen)
        item.appendChild(
          sceneButton(LabText.t('scenes.use_this_take'), '', function () {
            useSceneTake(line.line_id, segment.segment_id, take.id);
          }),
        );
      block.appendChild(item);
    });
    container.appendChild(block);
  });
}
function useSceneTake(lineId, segmentId, takeId) {
  if (sceneDirty) {
    showSceneError(new Error(LabText.t('scenes.save_or_discard_unsaved_script')));
    return;
  }
  fetchJson('/api/scenes/' + activeSceneId + '/select', { line_id: lineId, segment_id: segmentId, take_id: takeId })
    .then(function (value) {
      return renderScene(value.scene).then(function () {
        $('sceneStatus').textContent = LabText.t('scenes.take_chosen_the_scene_is', {revision: value.scene.revision});
      });
    })
    .catch(showSceneError);
}
function stopScenePlayer() {
  if (scenePlayer) scenePlayer.pause();
  if (scenePlayerButton) {
    scenePlayerButton.textContent = LabText.t('scenes.play');
    scenePlayerButton.setAttribute('aria-label', scenePlayerButton.getAttribute('aria-label').replace(/^Stop/, LabText.t('scenes.play')));
  }
  scenePlayer = null;
  scenePlayerButton = null;
}
// Play plays a line's chosen takes one after another (a long line has one per part); pressing it again stops.
function playSceneLine(lineId, button, number) {
  var again = scenePlayerButton === button;
  stopScenePlayer();
  if (again) return;
  var state = sceneLineStates[lineId],
    urls = ((state && state.segments) || [])
      .map(function (segment) {
        return segment.chosen_take_id ? '/api/takes/' + segment.chosen_take_id + '/audio' : null;
      })
      .filter(Boolean);
  if (!urls.length) return;
  var audio = new Audio(),
    at = 0;
  scenePlayer = audio;
  scenePlayerButton = button;
  button.textContent = LabText.t('scenes.stop');
  button.setAttribute('aria-label', LabText.t('scenes.stop_line', {number: number}));
  audio.addEventListener('ended', function () {
    if (scenePlayer !== audio) return;
    at += 1;
    if (at >= urls.length) {
      stopScenePlayer();
      return;
    }
    audio.src = urls[at];
    audio.play().catch(showSceneError);
  });
  audio.src = urls[0];
  audio.play().catch(function (error) {
    stopScenePlayer();
    showSceneError(error);
  });
}
function fillSceneCharacterFilter(scene) {
  var select = $('sceneFilterCharacter'),
    keep = select.value,
    seen = {};
  select.textContent = '';
  option(select, '', LabText.t('scenes.everyone'));
  scene.lines.forEach(function (line) {
    if (!line.character_id || seen[line.character_id]) return;
    seen[line.character_id] = true;
    var character = castCharacters.find(function (item) {
      return item.id === line.character_id;
    });
    option(select, line.character_id, character ? character.name : LabText.t('scenes.character_2', {character_id: line.character_id.slice(0, 8)}));
  });
  select.value = seen[keep] ? keep : '';
}
function applySceneFilters() {
  var character = $('sceneFilterCharacter').value,
    status = $('sceneFilterStatus').value,
    rows = sceneOrderedRows(),
    counts = {},
    shown = 0;
  rows.forEach(function (row) {
    row.body.hidden = !!((character && row.line.character_id !== character) || (status && row.state !== status));
    if (!row.body.hidden) shown += 1;
    if (row.state) counts[row.state] = (counts[row.state] || 0) + 1;
  });
  var parts = Object.keys(sceneStatusWords)
    .filter(function (key) {
      return counts[key];
    })
    .map(function (key) {
      return sceneStatusCounts[key](counts[key]);
    });
  var summary = parts.length
    ? LabText.plural('scenes.line_status_summary', rows.length, {status: parts.join(', ')})
    : LabText.plural('scenes.line_count_summary', rows.length);
  $('sceneTableSummary').textContent = shown < rows.length ? summary + ' ' + LabText.t('scenes.showing', {shown: shown}) : summary;
  updateSceneTools();
}
function updateSceneTools() {
  var rows = sceneOrderedRows(),
    shown = rows.filter(function (row) {
      return !row.body.hidden;
    }),
    ticked = shown.filter(function (row) {
      return sceneTicked.has(row.line.line_id);
    }),
    missing = rows.filter(function (row) {
      return sceneNeedsRender.indexOf(row.state) >= 0;
    }).length,
    choose = rows.filter(function (row) {
      return row.state === 'choose_take';
    }).length;
  $('sceneRenderTicked').textContent = LabText.t('scenes.render_ticked', {marked_lines: ticked.length});
  $('sceneRenderTicked').disabled = !ticked.length;
  $('sceneTickAll').checked = !!shown.length && ticked.length === shown.length;
  $('sceneTickAll').indeterminate = !!ticked.length && ticked.length < shown.length;
  $('sceneRenderMissing').textContent = LabText.t('scenes.render_what_is_missing_or_2', {missing_lines: missing ? ' (' + missing + ')' : ''});
  $('sceneChooseNewest').textContent = LabText.t('scenes.use_the_newest_take_where_2', {eligible_lines: choose ? ' (' + choose + ')' : ''});
}
function refreshSceneLineStates() {
  if (!activeSceneId) {
    sceneLineStates = {};
    paintSceneRows();
    return Promise.resolve();
  }
  var sceneId = activeSceneId;
  return fetchJson('/api/scenes/' + sceneId + '/lines').then(function (result) {
    if (sceneId !== activeSceneId) return;
    sceneLineStates = {};
    (result.lines || []).forEach(function (line) {
      sceneLineStates[line.line_id] = line;
    });
    paintSceneRows();
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
  $('sceneStatus').textContent = LabText.t('scenes.unsaved_scene_json_changes');
  sceneHistoryState();
});
function sceneJobLabel(job) {
  return job.status + ' / ' + job.completed + '/' + job.total + ' / ' + job.id.slice(0, 8);
}
var scenePreviewRead = 0;
function clearSceneOutputs() {
  scenePreviewRead++;
  $('sceneBatch').hidden = true;
  $('scenePlayers').hidden = true;
  ['scenePreviewPlayer', 'sceneMasteredPlayer'].forEach(function (id) {
    var player = $(id);
    if (!player.hasAttribute('src')) return;
    player.pause();
    player.removeAttribute('src');
    player.load();
  });
  $('scenePreviewMeta').textContent = '';
}
function restoreScenePreview(scene) {
  var read = scenePreviewRead;
  return fetchJson('/api/scenes/' + scene.scene_id + '/preview').then(function (result) {
    if (read === scenePreviewRead && result.preview) showScenePreview(result.preview);
  });
}
function renderScene(scene) {
  clearSceneOutputs();
  if (scene.scene_id !== activeSceneId) {
    sceneTicked = new Set();
    sceneOpenLines = new Set();
    sceneLineStates = {};
  }
  activeSceneId = scene.scene_id;
  renderSampleNotes();
  sceneJobSignature = null;
  sceneDirty = false;
  $('sceneEditor').value = JSON.stringify(scene, null, 2);
  resetSceneHistory($('sceneEditor').value);
  $('sceneError').textContent = '';
  renderSceneForm();
  $('sceneStatus').textContent =
    LabText.t('scenes.revision_ordered_lines_sha_256', {name: scene.name, revision: scene.revision, lines: scene.lines.length, checksum: scene.checksum});
  $('sceneExportJson').href = '/api/scenes/' + scene.scene_id + '/export.json';
  $('sceneExportCsv').href = '/api/scenes/' + scene.scene_id + '/export.csv';
  bindDubScene(scene);
  var videoStart = scene.timeline && scene.timeline.source_offset_ms;
  $('sceneTimelineNote').hidden = videoStart === undefined;
  $('sceneTimelineNote').textContent = videoStart === undefined ? '' :
    LabText.t('scenes.this_section_starts_at_in', {timestamp: sceneCueTime(videoStart)});
  $('sceneExportJson').hidden = false;
  $('sceneExportCsv').hidden = false;
  var jobs = fetchJson('/api/scenes/' + scene.scene_id + '/jobs').then(function (result) {
    if (scene.scene_id !== activeSceneId) return;
    var select = $('sceneJobSelect'),
      previous = activeSceneJobId;
    select.textContent = '';
    (result.jobs || []).forEach(function (job) {
      option(select, job.id, sceneJobLabel(job));
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
    $('sceneBatch').hidden = !activeSceneJobId;
    return activeSceneJobId ? refreshSceneJob() : Promise.resolve();
  });
  return Promise.all([jobs, refreshSceneLineStates(), restoreScenePreview(scene)]).then(function () {});
}
function scenePickerOrder(scenes, books) {
  // Keep each book where its most recently edited chapter was, with its chapters in reading order.
  var groups = new Map();
  scenes.forEach(function (scene) {
    var book = books[scene.scene_id];
    var key = book ? 'book:' + book.id : scene.scene_id;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(scene);
  });
  return Array.from(groups.values()).flatMap(function (rows) {
    if (rows.length > 1) rows.sort(function (a, b) {return books[a.scene_id].position - books[b.scene_id].position;});
    return rows;
  });
}
function canReplaceScene(discardedText) {
  return !sceneDirty || $('sceneEditor').value === discardedText;
}
function refreshSceneList(preferred, discardedText) {
  return fetchJson('/api/scenes').then(function (result) {
    var select = $('sceneSelect'),
      replace = canReplaceScene(discardedText),
      keep = (replace && preferred) || activeSceneId;
    sceneListCount = (result.scenes || []).length;
    $('sceneSaved').hidden = !sceneListCount;
    select.textContent = '';
    var books = result.chapter_books || {};
    scenePickerOrder(result.scenes || [], books).forEach(function (scene) {
      var book = books[scene.scene_id];
      var name = book ? book.title + ' · ' + scene.name : scene.name;
      option(select, scene.scene_id, LabText.t('scenes.r', {name: name, revision: scene.revision}));
    });
    if (!replace) {
      select.value = activeSceneId || '';
      return;
    }
    if (
      keep &&
      (result.scenes || []).some(function (scene) {
        return scene.scene_id === keep;
      })
    )
      select.value = keep;
    else if (select.options.length) select.selectedIndex = 0;
    var selected = select.value;
    if (selected)
      return fetchJson('/api/scenes/' + selected).then(function (value) {
        if (select.value !== selected) return;
        if (canReplaceScene(discardedText)) return renderScene(value.scene);
        select.value = activeSceneId || '';
      });
    activeSceneId = null;
    activeSceneJobId = null;
    clearSceneOutputs();
    sceneLineStates = {};
    $('sceneEditor').value = '';
    resetSceneHistory('');
    renderSceneForm();
    $('sceneJobRows').textContent = '';
    $('sceneExportJson').hidden = true;
    $('sceneExportCsv').hidden = true;
    bindDubScene(null);
    $('sceneTimelineNote').hidden = true;
    return Promise.resolve();
  }).then(function () {
    return refreshSample();
  });
}
// The sample scene (sample_project.py): offered on an empty Scenes, an empty Cast and the first-run note once Kokoro is
// installed, never made until asked; on the sample itself, the steps to hear it and one button that removes it all.
function refreshSample() {
  return fetchJson('/api/sample')
    .then(function (state) {
      sampleState = state;
    })
    .catch(function () {
      sampleState = null;
    })
    .then(renderSampleNotes);
}
function sampleOffered() {
  return !!(sampleState && !sampleState.sample && sampleState.can_make);
}
function renderSampleNotes() {
  var sample = sampleState && sampleState.sample,
    onSample = !!(sample && activeSceneId && sample.scene_id === activeSceneId),
    offer = sceneListCount === 0 && sampleOffered();
  $('sampleNote').hidden = !(onSample || offer);
  $('homeSampleLink').hidden = !sampleOffered();
  $('sampleOpen').hidden = onSample;
  $('sampleRemove').hidden = !onSample;
  $('sampleText').textContent = onSample
    ? LabText.t('scenes.this_is_the_sample_scene')
    : LabText.t('scenes.no_scene_yet_open_the');
  $('castSampleNote').hidden = !(castLoaded && castCharacters.length === 0 && sampleOffered());
  $('firstRunSample').hidden = !(firstRun.ready && !$('firstRun').hidden && sampleOffered());
}
function sampleButtons(disabled) {
  ['sampleOpen', 'sampleRemove', 'castSampleOpen', 'firstRunSample', 'homeSampleOpen'].forEach(function (id) {
    $(id).disabled = disabled;
  });
}
function openSample() {
  var onHome = !$('home').hidden;
  if (sceneDirty && !window.confirm(LabText.t('scenes.discard_unsaved_scene_edits_and'))) return Promise.resolve(null);
  var discardedText = $('sceneEditor').value;
  sampleButtons(true);
  $('sampleStatus').textContent = LabText.t('scenes.opening_the_sample_scene');
  if (onHome) $('homeSampleStatus').textContent = $('sampleStatus').textContent;
  return fetchJson('/api/sample', {})
    .then(function (result) {
      sampleState = { sample: result.sample, can_make: true, why_not: null };
      location.hash = '#scenes';
      $('homeSampleStatus').textContent = '';
      $('sampleStatus').textContent = result.made
        ? LabText.t('scenes.made_the_sample_scene_and')
        : LabText.t('scenes.opened_the_sample_scene');
      return refreshCast().then(function () {
        return refreshSceneList(result.sample.scene_id, discardedText);
      });
    })
    .catch(function (error) {
      $('sampleStatus').textContent = error.message;
      if (onHome) $('homeSampleStatus').textContent = error.message;
      else if (location.hash !== '#scenes') $('referenceStatus').textContent = error.message;
    })
    .then(function () {
      sampleButtons(false);
    });
}
function removeSample() {
  sampleButtons(true);
  return fetchJson('/api/sample/remove/preview')
    .then(function (preview) {
      if (preview.used_elsewhere.length) {
        $('sampleStatus').textContent =
          LabText.t('scenes.a_sample_character_speaks_in', {scenes: preview.used_elsewhere.join(', ')});
        return null;
      }
      var gone = preview.would_remove,
        parts = [LabText.plural('scenes.sample_character_count', gone.character), LabText.plural('scenes.sample_take_count', gone.take)];
      if (gone.reference) parts.push(LabText.plural('scenes.sample_recording_count', gone.reference));
      if (
        !window.confirm(
          LabText.t('scenes.remove_sample_confirm', {scene: preview.scene ? preview.scene.name : LabText.t('scenes.sample_name'), contents: parts.join(', ')})
        )
      )
        return null;
      return fetchJson('/api/sample/remove', { confirm: true });
    })
    .then(function (report) {
      if (!report) return null;
      var copies = report.external_copies.length;
      sceneDirty = false;
      return Promise.all([refreshCast(), refreshSceneList()]).then(function () {
        $('sampleStatus').textContent =
          LabText.plural('scenes.sample_removed', report.removed.take, {copies: copies ? LabText.plural('scenes.sample_export_copies', copies) : ''});
      });
    })
    .catch(function (error) {
      $('sampleStatus').textContent = error.message;
    })
    .then(function () {
      sampleButtons(false);
    });
}
function startSceneFromPerformer() {
  var character = performanceCharacter(),
    identity = performanceIdentity();
  if (!character || !identity) throw new Error(LabText.t('scenes.choose_a_character_and_voice'));
  if (sceneDirty && !window.confirm(LabText.t('scenes.discard_unsaved_scene_edits_and_2'))) return;
  var seed = Number($('performSeed').value);
  if (!Number.isFinite(seed)) seed = 42;
  var payload = {
    schema_version: 1,
    name: LabText.t('scenes.scene', {name: character.name}),
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
  activeSceneJobId = null;
  clearSceneOutputs();
  bindDubScene(null);
  $('sceneTimelineNote').hidden = true;
  $('sceneExportJson').hidden = $('sceneExportCsv').hidden = true;
  sceneDirty = true;
  $('sceneEditor').value = JSON.stringify(payload, null, 2);
  resetSceneHistory(null);
  renderSceneForm();
  $('sceneStatus').textContent = LabText.t('scenes.starter_scene_is_ready_validate');
}
function importSceneFile(file) {
  if (sceneDirty && !window.confirm(LabText.t('scenes.discard_unsaved_scene_edits_and_3'))) return Promise.resolve(null);
  var discardedText = $('sceneEditor').value;
  var format = file.name.toLowerCase().endsWith('.csv') ? 'csv' : 'json';
  return file
    .text()
    .then(function (content) {
      return fetchJson('/api/scenes/import', { format: format, content: content });
    })
    .then(function (result) {
      return result ? refreshSceneList(result.scene.scene_id, discardedText) : null;
    });
}
// A script in plain text or Fountain: read it (POST /api/scenes/import/preview), give each speaker a voice, then create the
// scene. Create the scene stays off while a speaker has no voice and is not left out, and the server refuses that too.
var sceneScript = null,
  sceneScriptFile = '';
function sceneScriptFormatOf(name) {
  var lower = String(name).toLowerCase();
  if (/\.(fountain|spmd)$/.test(lower)) return 'fountain';
  if (/\.srt$/.test(lower)) return 'srt';
  if (lower.endsWith('.transcript.json')) return 'transcript';
  if (/\.vtt$/.test(lower)) return 'vtt';
  return /\.txt$/.test(lower) ? 'text' : null;
}
function sceneScriptVoices() {
  var groups = [];
  castCharacters.forEach(function (character) {
    var voices = (character.identities || []).filter(function (identity) {
      return !identity.retired && identity.status !== 'rejected';
    });
    if (voices.length) groups.push({ character: character, voices: voices });
  });
  return groups;
}
function sceneScriptKey(name) {
  return String(name).replace(/\s+/g, ' ').trim().toLowerCase();
}
// A speaker whose name is a Cast character's starts on that character's approved voice, or its first one.
function sceneScriptGuess(name) {
  var match = sceneScriptVoices().find(function (group) {
    return sceneScriptKey(group.character.name) === sceneScriptKey(name);
  });
  if (!match) return '';
  var approved = match.voices.find(function (voice) {
    return voice.status === 'approved';
  });
  return (approved || match.voices[0]).id;
}
function sceneScriptPicker(name, narration, timed) {
  var select = document.createElement('select');
  select.dataset.speaker = narration ? '' : name;
  select.setAttribute('aria-label', narration ? LabText.t('scenes.voice_for_the_lines_with') : LabText.t('scenes.voice_for', {name: name}));
  option(select, '', LabText.t('scenes.choose_a_voice'));
  option(select, '-', LabText.t('scenes.leave_these_lines_out'));
  sceneScriptVoices().forEach(function (group) {
    var choices = document.createElement('optgroup');
    choices.label = group.character.name;
    group.voices.forEach(function (voice) {
      var item = document.createElement('option');
      item.value = voice.id;
      item.textContent =
        voice.label + ' · ' + sceneModelName(voice.engine_id) + (voice.status === 'approved' ? '' : ' (' + LabText.statusName(voice.status) + ')');
      choices.appendChild(item);
    });
    select.appendChild(choices);
  });
  select.value = narration ? (timed ? '' : '-') : sceneScriptGuess(name);
  select.addEventListener('change', sceneScriptReady);
  return select;
}
function sceneScriptRow(label, count, picker) {
  var row = document.createElement('tr');
  [label, String(count)].forEach(function (text) {
    var cell = document.createElement('td');
    cell.textContent = text;
    row.appendChild(cell);
  });
  var cell = document.createElement('td');
  cell.appendChild(picker);
  row.appendChild(cell);
  return row;
}
function sceneScriptReady() {
  var waiting = [];
  $('sceneScriptSpeakers')
    .querySelectorAll('select')
    .forEach(function (select) {
      if (!select.value) waiting.push(select.dataset.speaker || LabText.t('scenes.the_lines_with_no_speaker'));
    });
  var invalidTimed = sceneScript && sceneScript.duration_ms !== undefined && sceneScript.unread.length > 0;
  $('sceneScriptCreate').disabled = !sceneScript || !sceneScript.lines.length || !!invalidTimed || waiting.length > 0;
  $('sceneScriptWaiting').textContent = invalidTimed ? LabText.t('scenes.fix_the_subtitle_errors_and') : waiting.length
    ? LabText.t('scenes.choose_a_voice_for_or', {speakers: waiting.join(', ')})
    : '';
}
function clearSceneScript() {
  sceneScriptReadVersion++;
  sceneScript = null;
  $('sceneScriptMatch').hidden = true;
  $('sceneScriptSpeakers').textContent = '';
  sceneScriptReady();
}
function renderSceneScript() {
  var script = sceneScript,
    fountain = script.format === 'fountain',
    timed = script.duration_ms !== undefined,
    parts = [timed ? LabText.plural('scenes.cue_count', script.lines.length) : LabText.plural('scenes.line_count', script.lines.length),
      LabText.plural('scenes.speaker_count', script.speakers.length)];
  if (script.narration)
    parts.push(fountain ? LabText.plural('scenes.line_of_action_count', script.narration)
      : LabText.plural('scenes.line_with_no_name_count', script.narration));
  if (timed && script.sections && script.sections.length > 1)
    parts.push(LabText.plural('scenes.scene_section_count', script.sections.length));
  parts.push(LabText.plural('scenes.line_not_understood_count', script.unread.length));
  $('sceneScriptSummary').textContent = parts.join(', ') + '.';
  var unread = $('sceneScriptUnread');
  unread.textContent = '';
  script.unread.forEach(function (item) {
    var entry = document.createElement('li');
    entry.textContent = LabText.t('scenes.line_5', {number: item.number, text: item.text, reason: item.reason});
    unread.appendChild(entry);
  });
  $('sceneScriptUnreadBox').hidden = !script.unread.length;
  $('sceneScriptCueBox').hidden = !timed;
  var cues = $('sceneScriptCues');
  cues.textContent = '';
  if (timed) {
    $('sceneScriptCueBox').open = true;
    (script.warnings || []).forEach(function (warning) {
      var item = document.createElement('li');
      item.textContent = warning.message;
      cues.appendChild(item);
    });
    script.lines.forEach(function (line) {
      var item = document.createElement('li');
      item.textContent = sceneCueRange(line.cue) + ' · ' + (line.speaker || LabText.t('scenes.no_speaker')) + ' · ' + line.text;
      cues.appendChild(item);
    });
  }
  var body = $('sceneScriptSpeakers');
  body.textContent = '';
  script.speakers.forEach(function (speaker) {
    body.appendChild(sceneScriptRow(speaker.name, speaker.lines, sceneScriptPicker(speaker.name, false)));
  });
  if (script.narration)
    body.appendChild(
      sceneScriptRow(
        timed ? LabText.t('scenes.cues_with_no_speaker_label') : fountain ? LabText.t('scenes.action_read_by_a_narrator') : LabText.t('scenes.lines_with_no_name_read'),
        script.narration,
        sceneScriptPicker('', true, timed),
      ),
    );
  if (!$('sceneScriptName').value.trim()) $('sceneScriptName').value = script.title || sceneScriptFile;
  $('sceneScriptMatch').hidden = false;
  sceneScriptReady();
}
var sceneScriptReadVersion = 0;
function readSceneScript() {
  var content = $('sceneScriptText').value;
  $('sceneError').textContent = '';
  $('sceneScriptReport').textContent = '';
  clearSceneScript();
  var version = sceneScriptReadVersion;
  if (!content.trim()) return Promise.reject(new Error(LabText.t('scenes.paste_a_script_or_choose')));
  return fetchJson('/api/scenes/import/preview', { format: $('sceneScriptFormat').value, content: content }).then(
    function (result) {
      if (version !== sceneScriptReadVersion) return;
      sceneScript = result.script;
      renderSceneScript();
    },
  );
}
function draftSceneSubtitles() {
  var button = $('sceneSpeechDraft'), note = $('sceneSpeechNote');
  var file = $('sceneSpeechFile').files[0];
  if (!file) {
    note.textContent = LabText.t('scenes.choose_the_audio_file_to');
    return;
  }
  var oldText = $('sceneScriptText').value;
  if (oldText.trim() && !window.confirm(LabText.t('scenes.replace_the_text_in_the'))) return;
  button.disabled = true;
  note.textContent = LabText.t('scenes.listening_to_the_file_this');
  var form = new FormData();
  form.set('file', file);
  form.set('model', 'whisper-1');
  form.set('response_format', 'srt');
  fetch('/v1/audio/transcriptions', {method:'POST', body:form}).then(async function(response) {
    if (!response.ok) {
      var failed = await response.json();
      throw new Error(failed.error?.message || LabText.t('scenes.could_not_draft_subtitles_check'));
    }
    var text = await response.text();
    if (!text.trim()) throw new Error(LabText.t('scenes.no_words_were_heard_choose'));
    if ($('sceneScriptText').value !== oldText) {
      if (!window.confirm(LabText.t('scenes.replace_your_new_edits_in'))) return;
    }
    $('sceneScriptFormat').value = 'srt';
    $('sceneScriptText').value = text;
    await readSceneScript();
    note.textContent = LabText.t('scenes.draft_ready_check_the_words');
  }).catch(function(error) {
    note.textContent = error.message;
  }).finally(function() {
    button.disabled = false;
  });
}
$('sceneSpeechDraft').addEventListener('click', draftSceneSubtitles);
function loadSceneScriptFile(file) {
  return file.text().then(function (content) {
    $('sceneScriptFormat').value = sceneScriptFormatOf(file.name);
    $('sceneScriptText').value = content;
    $('sceneScriptName').value = '';
    sceneScriptFile = file.name.replace(/\.[^.]+$/, '');
    $('sceneScriptImport').open = true;
    return readSceneScript();
  });
}
function sceneScriptReportText(scene, report) {
  var rows = [scene.length > 1
    ? LabText.t('scenes.created_scene_sections_choose_any', {sections: scene.length})
    : LabText.t('scenes.created_with', {name: scene[0].name, lines: LabText.plural('scenes.line_count', report.lines)})];
  function note(title, items, describe) {
    if (!items.length) return;
    rows.push(title + ':');
    items.slice(0, 12).forEach(function (item) {
      rows.push((item.speaker ? LabText.t('scenes.speaker_line_note', {number: item.number, speaker: item.speaker, note: describe(item)}) : LabText.t('scenes.line_6', {number: item.number, note: describe(item)})));
    });
    if (items.length > 12) rows.push(LabText.t('scenes.and_more', {remaining_lines: items.length - 12}));
  }
  note(LabText.t('scenes.left_out'), report.left_out, function (item) {
    return item.reason + '. “' + sceneShortText(item.text) + '”';
  });
  note(LabText.t('scenes.acting_directions_left_out_because'), report.directions_left_out, function (item) {
    return '(' + item.direction + ')';
  });
  note(LabText.t('scenes.not_understood'), report.unread, function (item) {
    return item.reason + '. “' + item.text + '”';
  });
  return rows.join('\n');
}
function createSceneFromScript() {
  if (!sceneScript) return Promise.resolve(null);
  if (sceneDirty && !window.confirm(LabText.t('scenes.discard_unsaved_scene_edits_and_3'))) return Promise.resolve(null);
  var discardedText = $('sceneEditor').value;
  var speakers = Object.create(null),
    narrator = null;
  $('sceneScriptSpeakers')
    .querySelectorAll('select')
    .forEach(function (select) {
      var choice = select.value === '-' ? null : { identity_id: select.value };
      if (select.dataset.speaker) speakers[select.dataset.speaker] = choice;
      else narrator = choice;
    });
  var body = { format: sceneScript.format, content: $('sceneScriptText').value, speakers: speakers, narrator: narrator },
    name = $('sceneScriptName').value.trim();
  if (name) body.name = name;
  $('sceneScriptCreate').disabled = true;
  return fetchJson('/api/scenes/import', body)
    .then(function (result) {
      clearSceneScript(); // A second press would make the scene twice.
      $('sceneScriptReport').textContent = sceneScriptReportText(result.scenes || [result.scene], result.report);
      return refreshSceneList(result.scene.scene_id, discardedText);
    })
    .finally(sceneScriptReady);
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
  var part = parts[current];
  var progress = LabText.t('scenes.model_progress', {name: part.name, done: part.done, total: part.total});
  var remaining = parts.slice(current + 1).map(function (next) {
    return LabText.t('scenes.model_pending', {name: next.name, total: next.total});
  });
  if (remaining.length) return LabText.t('scenes.rendering_models_then', {progress: progress, remaining: LabText.list(remaining)});
  return parts.length > 1 ? LabText.t('scenes.rendering_in_model_order', {models: progress}) : LabText.t('scenes.rendering_one_model', {models: progress});
}
function refreshSceneJob() {
  if (!activeSceneJobId) return Promise.resolve();
  return fetchJson('/api/scenes/jobs/' + activeSceneJobId).then(function (result) {
    var job = result.job;
    $('sceneProgress').value = job.progress;
    var progress = (['queued', 'running'].indexOf(job.status) >= 0 ? batchOrderText(job) : '') + job.status;
    $('sceneStatus').textContent =
      LabText.t('scenes.completed_failed_queued_total', {progress: progress, completed: job.completed, failed: job.failed, queued: job.queued, total: job.total, percent: Math.round(job.progress * 100)});
    Array.prototype.forEach.call($('sceneJobSelect').options, function (item) {
      if (item.value === job.id) item.textContent = sceneJobLabel(job);
    });
    $('sceneJobManifest').href = '/api/scenes/jobs/' + job.id + '/manifest.json';
    $('sceneJobManifest').hidden = false;
    var container = $('sceneJobRows');
    container.textContent = '';
    job.rows.forEach(function (row) {
      var article = document.createElement('article'),
        title = document.createElement('strong');
      var cue = job.scene_snapshot?.lines[row.scene_order]?.cue;
      title.textContent = cue
        ? LabText.t('scenes.line_part_take', {line_number: row.scene_order + 1, timestamp: sceneCueTime(cue.start_ms), part: row.segment_index + 1, take_index: row.take_index, status: row.status})
        : LabText.t('scenes.render_scene_take', {render_number: row.ordinal + 1, scene_number: row.scene_order + 1, line_id: row.line_id, segment_id: row.segment_id, take_index: row.take_index, status: row.status});
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
        gone.textContent = LabText.t('scenes.this_take_was_deleted_from');
        article.appendChild(gone);
      }
      if (row.take_id && !row.purged) {
        if (row.licence) appendLicenceBadge(article, row.licence);
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
        choose.textContent = LabText.t('scenes.use_this_take');
        choose.disabled = !!row.retirement;
        choose.addEventListener('click', function () {
          if (sceneDirty) {
            showSceneError(new Error(LabText.t('scenes.save_or_discard_unsaved_script_2')));
            return;
          }
          fetchJson('/api/scenes/' + activeSceneId + '/select', {
            line_id: row.line_id,
            segment_id: row.segment_id,
            take_id: row.take_id,
          })
            .then(function (value) {
              $('sceneStatus').textContent = LabText.t('scenes.selected_take_saved_in_scene', {revision: value.scene.revision});
              return renderScene(value.scene);
            })
            .catch(showSceneError);
        });
        article.appendChild(choose);
      }
      container.appendChild(article);
    });
    var signature = [job.id, job.status, job.queued, job.running, job.completed, job.failed, job.cancelled].join(':');
    if (sceneJobSignature !== null && signature !== sceneJobSignature) refreshSceneLineStates().catch(showSceneError);
    sceneJobSignature = signature;
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
    return Promise.reject(new Error(LabText.t('scenes.correct_the_highlighted_script_field')));
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
// Saves the scene, then queues a batch: `request` is the body (line_ids or segment_ids, or nothing for every line), or a
// function that works it out from the saved scene's line statuses and may return null for nothing to render.
function startSceneBatch(request) {
  return saveAndGetScene()
    .then(function (scene) {
      return refreshSceneList(scene.scene_id);
    })
    .then(function () {
      var body = typeof request === 'function' ? request() : request || {};
      if (!body) return null;
      return fetchJson('/api/scenes/' + activeSceneId + '/jobs', body).then(function (result) {
        var select = $('sceneJobSelect'),
          item = document.createElement('option');
        activeSceneJobId = result.job.id;
        item.value = result.job.id;
        item.textContent = sceneJobLabel(result.job);
        select.insertBefore(item, select.firstChild);
        select.value = activeSceneJobId;
        $('sceneBatch').hidden = false;
        return Promise.all([refreshSceneJob(), refreshSceneLineStates()]);
      });
    });
}
function missingSceneLines() {
  var ids = sceneOrderedRows()
    .filter(function (row) {
      return sceneNeedsRender.indexOf(row.state) >= 0;
    })
    .map(function (row) {
      return row.line.line_id;
    });
  if (ids.length) return { line_ids: ids };
  $('sceneStatus').textContent = LabText.t('scenes.nothing_is_missing_or_out');
  return null;
}
function chooseNewestSceneTakes() {
  return saveAndGetScene()
    .then(function (scene) {
      return fetchJson('/api/scenes/' + scene.scene_id + '/choose-newest', {});
    })
    .then(function (result) {
      return refreshSceneList(result.scene.scene_id).then(function () {
        $('sceneStatus').textContent = result.lines
          ? LabText.plural('scenes.newest_take_chosen', result.lines)
          : LabText.t('scenes.no_line_was_waiting_for');
      });
    });
}
function sceneAction(action) {
  if (!activeSceneJobId) throw new Error(LabText.t('scenes.choose_a_batch_job_first'));
  // Stop now cancels the batch and also stops the line it is rendering, where Cancel lets that line finish.
  var stopNow = action === 'stopNow';
  var path = '/api/scenes/jobs/' + activeSceneJobId + '/' + (stopNow ? 'cancel' : action);
  return fetchJson(path, stopNow ? { stop_current: true } : {}).then(function (result) {
    return refreshSceneJob();
  });
}
function previewScene() {
  if (!activeSceneId) throw new Error(LabText.t('scenes.save_a_scene_before_assembly'));
  var save = sceneDirty ? saveAndGetScene() : Promise.resolve({ scene_id: activeSceneId });
  var read = ++scenePreviewRead, button = $('scenePreview');
  button.disabled = true;
  $('sceneStatus').textContent = LabText.t('scenes.building_the_scene_from_the');
  return save
    .then(function (scene) {
      return fetchJson('/api/scenes/' + scene.scene_id + '/preview', {});
    })
    .then(function (result) {
      if (read !== scenePreviewRead) return;
      showScenePreview(result.preview);
      $('sceneStatus').textContent = LabText.t('scenes.scene_built_listen_below');
    })
    .catch(function (error) {
      if (read !== scenePreviewRead) return;
      $('sceneStatus').textContent = '';
      throw error;
    })
    .finally(function () { button.disabled = false; });
}
function showScenePreview(preview) {
  $('scenePreviewPlayer').src = preview.audio_url;
  $('sceneMasteredPlayer').src = preview.mastered_audio_url;
  $('scenePlayers').hidden = false;
  var mastering = preview.mastering || {};
  $('scenePreviewMeta').textContent =
    LabText.t('scenes.s_hz_mastered_lufs_target', {mix_mode: preview.timing ? LabText.t('scenes.timed_cue_mix') : LabText.t('scenes.separate_calls'), seconds: preview.duration_seconds.toFixed(2), sample_rate: preview.sample_rate, target_lufs: Number(mastering.target_lufs).toFixed(0), peak_ceiling: Number(mastering.peak_ceiling_dbfs).toFixed(0)});
  if (preview.timing && preview.timing.warnings.length)
    $('scenePreviewMeta').textContent += ' ' + preview.timing.warnings.map(function (warning) {
      return warning.message;
    }).join(' ');
}
$('sceneRefresh').addEventListener('click', function () {
  if (sceneDirty && !window.confirm(LabText.t('scenes.discard_unsaved_scene_edits_and_4'))) return;
  refreshSceneList(undefined, $('sceneEditor').value).catch(showSceneError);
});
$('sceneSelect').addEventListener('change', function () {
  var target = this.value;
  if (sceneDirty && !window.confirm(LabText.t('scenes.discard_unsaved_scene_edits_and_5'))) {
    this.value = activeSceneId || '';
    return;
  }
  var discardedText = $('sceneEditor').value;
  if (target)
    fetchJson('/api/scenes/' + target)
      .then(function (result) {
        if ($('sceneSelect').value !== target) return;
        if (canReplaceScene(discardedText)) return renderScene(result.scene);
        $('sceneSelect').value = activeSceneId || '';
      })
      .catch(showSceneError);
});
$('sceneJobSelect').addEventListener('change', function () {
  activeSceneJobId = this.value || null;
  refreshSceneJob().catch(showSceneError);
});
$('sceneImportFile').addEventListener('change', function () {
  var file = this.files && this.files[0];
  if (file) (sceneScriptFormatOf(file.name) ? loadSceneScriptFile(file) : importSceneFile(file)).catch(showSceneError);
  this.value = '';
});
$('sceneScriptRead').addEventListener('click', function () {
  readSceneScript().catch(showSceneError);
});
$('sceneScriptCreate').addEventListener('click', function () {
  createSceneFromScript().catch(showSceneError);
});
['sceneScriptText', 'sceneScriptFormat'].forEach(function (id) {
  $(id).addEventListener(id === 'sceneScriptText' ? 'input' : 'change', clearSceneScript);
});
$('sceneStarter').addEventListener('click', function () {
  if (!performanceCharacter() || !performanceIdentity()) {
    location.hash = '#perform';
    requestAnimationFrame(function () {
      if (castCharacters.length) $('performCharacter').focus();
      else $('performEmpty').querySelector('a').focus();
    });
    return;
  }
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
  var rows = function () {
      return $('sceneLineEditor').querySelectorAll('tbody.scene-line');
    },
    index = Array.prototype.indexOf.call(rows(), article);
  try {
    mutateSceneLine(index, action);
  } catch (error) {
    showSceneError(error);
    return true;
  }
  var moved = rows()[action === 'up' ? index - 1 : index + 1],
    button = moved && moved.querySelector('.scene-open');
  if (button) button.focus();
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
      $('sceneSegmentPreview').textContent = LabText.t('scenes.save_the_scene_before_showing');
      return;
    }
    fetchJson('/api/scenes/' + activeSceneId + '/segments')
      .then(function (result) {
        $('sceneSegmentPreview').textContent = (result.segments || [])
          .map(function (line, index) {
            var cue = sceneBody().lines[index]?.cue;
            var label = cue ? LabText.t('scenes.segment_cue', {number: index + 1, time: sceneCueTime(cue.start_ms)}) : line.line_id;
            var segments = line.segments.map(function (segment) {
              return LabText.plural('scenes.segment_characters', segment.text.length, {segment_id: segment.id});
            });
            return LabText.plural('scenes.segment_summary', line.segments.length, {line: label, segments: segments.join(', ')});
          })
          .join('\n');
      })
      .catch(showSceneError);
  } catch (error) {
    showSceneError(error);
  }
});
$('sceneStart').addEventListener('click', function () {
  startSceneBatch({}).catch(showSceneError);
});
$('sceneRenderTicked').addEventListener('click', function () {
  var ids = sceneOrderedRows()
    .filter(function (row) {
      return !row.body.hidden && sceneTicked.has(row.line.line_id);
    })
    .map(function (row) {
      return row.line.line_id;
    });
  if (ids.length) startSceneBatch({ line_ids: ids }).catch(showSceneError);
});
$('sceneRenderMissing').addEventListener('click', function () {
  startSceneBatch(missingSceneLines).catch(showSceneError);
});
['sampleOpen', 'castSampleOpen', 'firstRunSample', 'homeSampleOpen'].forEach(function (id) {
  $(id).addEventListener('click', function () {
    openSample();
  });
});
$('sampleRemove').addEventListener('click', function () {
  removeSample();
});
$('sceneChooseNewest').addEventListener('click', function () {
  chooseNewestSceneTakes().catch(showSceneError);
});
$('sceneTickAll').addEventListener('change', function () {
  var on = this.checked;
  sceneOrderedRows().forEach(function (row) {
    if (row.body.hidden) return;
    if (on) sceneTicked.add(row.line.line_id);
    else sceneTicked.delete(row.line.line_id);
    row.tick.checked = on;
  });
  updateSceneTools();
});
Object.keys(sceneStatusWords).forEach(function (key) {
  option($('sceneFilterStatus'), key, sceneStatusWords[key]);
});
$('sceneFilterCharacter').addEventListener('change', applySceneFilters);
$('sceneFilterStatus').addEventListener('change', applySceneFilters);
['pause', 'resume', 'cancel', 'retry', 'stopNow'].forEach(function (action) {
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
  if (!isFinite(value)) throw new Error(LabText.t('scenes.must_be_a_number', {label: label}));
  if (field.min !== '' && value < Number(field.min)) throw new Error(LabText.t('scenes.must_be_or_more', {label: label, min: field.min}));
  if (field.max !== '' && value > Number(field.max)) throw new Error(LabText.t('scenes.must_be_or_less', {label: label, max: field.max}));
  return value;
}
function exportRequest() {
  if (!activeSceneId) throw new Error(LabText.t('scenes.save_a_scene_before_exporting'));
  if (sceneDirty) throw new Error(LabText.t('scenes.save_or_discard_unsaved_scene'));
  var formats = [].slice.call(document.querySelectorAll('input[name=exportFormat]:checked')).map(function (box) {
    return box.value;
  });
  if (!formats.length) throw new Error(LabText.t('scenes.choose_at_least_one_format'));
  var lufs = exportNumber('exportLufs', LabText.t('scenes.loudness_target'));
  return {
    scene_id: activeSceneId,
    formats: formats,
    filename_template: $('exportTemplate').value,
    wav_precision: $('exportWavPrecision').value,
    output_rate: exportNumber('exportRate', LabText.t('scenes.sample_rate')),
    trim_start_seconds: exportNumber('exportTrimStart', LabText.t('scenes.trim_start')) || 0,
    trim_end_seconds: exportNumber('exportTrimEnd', LabText.t('scenes.trim_end')) || 0,
    fade_in_ms: exportNumber('exportFadeIn', LabText.t('scenes.fade_in')) || 0,
    fade_out_ms: exportNumber('exportFadeOut', LabText.t('scenes.fade_out')) || 0,
    loudness_lufs: lufs,
    loudness_true_peak: lufs === null ? null : exportNumber('exportTruePeak', LabText.t('scenes.true_peak')),
    limiter_dbfs: exportNumber('exportLimiter', LabText.t('scenes.limiter_ceiling')),
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
    ? report.message || LabText.t('scenes.ffmpeg_found_every_format_is', {version: report.version || ''})
    : LabText.t('scenes.refresh_list_checks_again', {message: report.message});
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
      option(select, row.name, row.name + ' / ' + (row.scene_name || LabText.t('scenes.unknown_scene_name')) + ' / ' + row.status);
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
  $('exportStatus').textContent = LabText.t('scenes.checking_provenance_and_settings');
  fetchJson('/api/exports/preview', body)
    .then(function (result) {
      $('exportStatus').textContent = LabText.t('scenes.dry_run_only_no_files');
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
  $('exportStatus').textContent = LabText.t('scenes.writing_the_package_large_scenes');
  fetchJson('/api/exports', body)
    .then(function (result) {
      $('exportStatus').textContent = LabText.t('scenes.package_written_to_verify_it', {path: result.export.path});
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
    showExportError(new Error(LabText.t('scenes.choose_a_package_to_verify')));
    return;
  }
  $('exportError').textContent = '';
  $('exportStatus').textContent = LabText.t('scenes.verifying', {name: name});
  fetchJson('/api/exports/' + encodeURIComponent(name) + '/verify', {})
    .then(function (result) {
      $('exportStatus').textContent = LabText.t('scenes.verification_passed_for', {name: name});
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

var dubSceneId = null, dubState = null, dubPlayer = null, dubBusy = false, dubRefresh = 0;
var dubProviders = [], dubProviderDraft = null;
function dubUrl() {
  return '/api/scenes/' + dubSceneId + '/dub';
}
function dubNote(text) {
  $('dubStatus').textContent = text;
}
function dubMessage(error) {
  dubNote(error.message || String(error));
}
function bindDubScene(scene) {
  var next = scene && scene.timeline ? scene.scene_id : null;
  $('sceneDub').hidden = !next;
  var offset = next && scene.timeline.source_offset_ms;
  $('dubSourceOffset').textContent = offset ? LabText.t('scenes.use_a_clip_beginning_at', {timestamp: sceneCueTime(offset)}) : '';
  if (next !== dubSceneId) {
    dubSceneId = next;
    dubState = null;
    clearDubDraft();
    if (dubPlayer) {
      dubPlayer.removeAttribute('src');
      dubPlayer.load();
      dubPlayer.hidden = true;
      dubPlayer = null;
    }
    $('dubFrom').value = $('dubTo').value = '';
    $('dubKeep').checked = $('dubUseBed').checked = $('dubDuck').checked = false;
    $('dubBurn').value = '';
    $('dubAlternate').replaceChildren();
    dubNote('');
    paintDubAssets();
  }
  if (next && $('sceneDub').open) refreshDub().catch(dubMessage);
}
function dubOriginalText(line, scene) {
  var cue = line.cue;
  if (line.dub_source_text !== undefined) return line.dub_source_text;
  var ids = cue.source_line_ids || [line.line_id];
  var source = (scene.timeline.source_cues || []).filter(function (row) { return ids.includes(row.line_id); });
  return source.length ? source.map(function (row) { return row.text; }).join('\n') : (cue.source_text || '');
}
function dubSelectedLine(scene) {
  return scene.lines.find(function (line) { return line.line_id === $('dubCue').value; });
}
function paintDubCue(scene) {
  var line = dubSelectedLine(scene);
  if (!line) return;
  var controls = line.dub || {};
  $('dubSourceText').value = dubOriginalText(line, scene);
  $('dubTargetText').value = line.text;
  $('dubFit').value = controls.fit || 'natural';
  $('dubSpeed').value = controls.speed ?? 1;
  $('dubGain').value = controls.gain_db ?? 0;
  $('dubMute').checked = controls.mute === true;
}
function dubLinks(parent, base, files) {
  parent.replaceChildren();
  files.forEach(function (name) {
    var link = document.createElement('a');
    link.textContent = name;
    link.href = base + '/' + name;
    link.download = name;
    parent.appendChild(link);
  });
}
function paintDubWave() {
  var canvas = /** @type {HTMLCanvasElement} */ ($('dubWave'));
  var peaks = (dubState && dubState.source && dubState.source.waveform) || [];
  canvas.hidden = !peaks.length;
  $('dubSeekLabel').hidden = !dubPlayer;
  if (!peaks.length) return;
  var ctx = canvas.getContext('2d'), rect = canvas.getBoundingClientRect();
  var time = dubPlayer ? dubPlayer.currentTime : 0;
  var h = canvas.height, w = canvas.width, spacing = w / peaks.length;
  ctx.clearRect(0, 0, w, h);
  var progress = time / (dubState.source.duration_ms / 1000);
  peaks.forEach(function (peak, i) {
    var half = Math.max(1, peak * h / 2);
    ctx.fillStyle = i / peaks.length < progress ? '#818cf8' : '#64748b';
    ctx.fillRect(i * spacing, h / 2 - half, Math.max(1, spacing - 0.4), 2 * half);
  });
  if (rect.width && dubPlayer) {
    ctx.fillStyle = '#c7d2fe';
    ctx.fillRect(Math.min(w, progress * w), 0, 2, h);
  }
}
function paintDubAssets() {
  var source = dubState && dubState.source, bed = dubState && dubState.background, output = dubState && dubState.delivery;
  $('dubAttachments').textContent = [
    source ? source.name + ' · ' + sceneCueTime(source.duration_ms) + (source.transcript_source_verified ? LabText.t('scenes.source_hash_matched') : '') : '',
    bed ? LabText.t('scenes.background', {name: bed.name}) : '',
    output ? (dubState.scene_revision !== output.scene_revision ? LabText.t('scenes.delivery_previous_revision', {revision: output.scene_revision}) : LabText.t('scenes.delivery_revision', {revision: output.scene_revision})) : ''
  ].filter(Boolean).join(' / ');
  $('dubSourceFile').disabled = dubBusy || !!source;
  $('dubBedFile').disabled = dubBusy || !!bed;
  $('dubOriginal').disabled = !source;
  $('dubDubbed').disabled = !output;
  $('dubVerify').disabled = dubBusy || !output;
  $('dubKeep').disabled = !source || !source.video || !source.audio;
  $('dubBurn').disabled = !source || !source.video;
  $('dubUseBed').disabled = !bed;
  $('dubExport').disabled = dubBusy;
  dubLinks($('dubDeliveryFiles'), output ? dubUrl() + '/exports/' + output.id : '',
    output ? Object.keys(output.files).concat('manifest.json') : []);
  $('dubWarnings').replaceChildren();
  if (output) output.timing.warnings.forEach(function (warning) {
    var item = document.createElement('li');
    item.textContent = warning.message;
    $('dubWarnings').appendChild(item);
  });
  paintDubWave();
}
function refreshDub() {
  if (!dubSceneId || !$('sceneDub').open) return Promise.resolve();
  var serial = ++dubRefresh, id = dubSceneId, scene = sceneBody(), select = $('dubCue'), keep = select.value;
  select.replaceChildren();
  scene.lines.forEach(function (line) {
    option(select, line.line_id, sceneCueRange(line.cue) + ' · ' + sceneShortText(line.text));
  });
  if (scene.lines.some(function (line) { return line.line_id === keep; })) select.value = keep;
  paintDubLanguages(scene);
  paintDubCue(scene);
  var sourceCaptions = scene.timeline.source_cues && scene.timeline.source_cues.length ? ['source.srt', 'source.vtt'] : [];
  dubLinks($('dubReviewFiles'), dubUrl() + '/captions',
    ['target.txt', 'target.srt', 'target.vtt', 'target.ass', 'bilingual.srt', 'bilingual.vtt', 'bilingual.ass'].concat(sourceCaptions));
  return fetchJson(dubUrl()).then(function (state) {
    if (id !== dubSceneId || serial !== dubRefresh) return;
    dubState = state;
    paintDubAssets();
  });
}
function dubAction(message, action) {
  if (dubBusy || !dubSceneId) return;
  var id = dubSceneId;
  dubBusy = true;
  paintDubAssets();
  dubNote(message);
  Promise.resolve().then(function () { return action(id); }).catch(function (error) {
    if (id === dubSceneId) dubMessage(error);
  }).finally(function () {
    dubBusy = false;
    paintDubAssets();
  });
}
function dubPlay(useOutput) {
  var entry = dubState && (useOutput ? dubState.delivery : dubState.source);
  if (!entry) return;
  var file = useOutput ? (entry.files['dub.mp4'] ? 'dub.mp4' : 'dub.wav') : entry.preview;
  var url = dubUrl() + (useOutput ? '/exports/' + entry.id : '/source') + '/' + file;
  dubPlayUrl(url, file);
}
function dubPlayUrl(url, file) {
  var time = dubPlayer ? dubPlayer.currentTime : 0;
  var playing = dubPlayer && !dubPlayer.paused;
  if (dubPlayer) {
    dubPlayer.pause();
    dubPlayer.hidden = true;
  }
  dubPlayer = /** @type {HTMLMediaElement} */ ($(file.endsWith('.mp4') ? 'dubVideo' : 'dubAudio'));
  dubPlayer.hidden = false;
  dubPlayer.onloadedmetadata = function () {
    this.currentTime = Math.min(time, this.duration);
    $('dubSeek').max = this.duration;
    if (playing) this.play().catch(dubMessage);
    paintDubWave();
  };
  dubPlayer.ontimeupdate = function () {
    $('dubSeek').value = this.currentTime;
    paintDubWave();
  };
  dubPlayer.onerror = function () { dubNote(LabText.t('scenes.this_browser_could_not_play')); };
  dubPlayer.src = url;
  paintDubWave();
}
function dubSeekTo(seconds) {
  if (dubPlayer && Number.isFinite(dubPlayer.duration)) dubPlayer.currentTime = Math.min(seconds, dubPlayer.duration);
}
var dubLanguageDisplay = new Intl.DisplayNames(['en'], {type:'language', fallback:'none'});
var dubLanguageNames = {
  'en-us':'American English', 'en-gb':'British English', 'pt-br':'Brazilian Portuguese',
  'my':'Burmese', 'ht':'Haitian Creole', 'tl':'Filipino'
};
function dubLanguageName(tag) {
  if (dubLanguageNames[tag]) return dubLanguageNames[tag];
  try { return dubLanguageDisplay.of(tag); } catch (_) { return undefined; }
}
var dubLanguageChoices = Object.keys(dubLanguageNames);
var dubListedLanguages = new Set(dubLanguageChoices.map(dubLanguageName));
for (const firstLetter of 'abcdefghijklmnopqrstuvwxyz') {
  for (const secondLetter of 'abcdefghijklmnopqrstuvwxyz') {
    const languageTag = firstLetter + secondLetter, name = dubLanguageName(languageTag);
    // Retired codes such as iw and in name the same language as he and id, and tw is listed as Akan: one row each.
    if (!name || dubLanguageChoices.includes(languageTag) || dubListedLanguages.has(name) ||
      Intl.getCanonicalLocales(languageTag)[0] !== languageTag) continue;
    dubLanguageChoices.push(languageTag);
    dubListedLanguages.add(name);
  }
}
function dubVoiceLanguage(tag, supported, current) {
  var name = dubLanguageName(tag), base = (dubLanguageName(tag.split('-')[0]) || '').toLowerCase();
  var exact = supported.find(function (language) { return language.toLowerCase() === (name || '').toLowerCase(); });
  if (exact) return exact;
  if (!base) return undefined;
  var related = supported.filter(function (language) {
    var label = language.toLowerCase();
    return dubLanguageNames[tag] && tag.includes('-') ? label === base : (' ' + label + ' ').includes(' ' + base + ' ');
  });
  return related.includes(current) ? current : related[0];
}
function dubLineLanguages(line) {
  var match = sceneIdentityById(line.identity_id), voice = match && match.identity;
  var modelLanguages = sceneLanguageOptions(voice, line);
  return voice && voice.voice_languages ? modelLanguages.filter(function (name) {
    return voice.voice_languages.includes(name);
  }) : modelLanguages;
}
function dubLanguageLabel(tag) {
  return LabText.languageName(dubLanguageName(tag), tag);
}
function paintDubLanguages(scene) {
  var distinctVoices = new Map(scene.lines.map(function (line) {return [line.identity_id, line];}));
  var supported = Array.from(distinctVoices.values()).flatMap(dubLineLanguages);
  var transcript = (scene.transcript_source || {}).cue_languages || {};
  var original = transcript[(dubSelectedLine(scene) || {}).line_id];
  [[ $('dubFrom'), original ], [ $('dubTo'), scene.dub_target_language ]].forEach(function ([select, fallback]) {
    var selected = select.value || String(fallback || '').toLowerCase();
    var tags = Array.from(new Set(dubLanguageChoices.concat(selected || [])));
    var rows = tags.map(function (tag) {
      return {tag: tag, name: dubLanguageLabel(tag) || LabText.t('scenes.saved_language'), preferred: !!dubVoiceLanguage(tag, supported)};
    });
    rows.sort(function (a, b) {return Number(b.preferred) - Number(a.preferred) || a.name.localeCompare(b.name, LabText.language);});
    select.replaceChildren();
    option(select, '', LabText.t('scenes.choose_a_language'));
    rows.forEach(function (row) {option(select, row.tag, row.name);});
    select.value = selected;
  });
}
function dubSpokenLanguage(line, tag) {
  var match = dubVoiceLanguage(tag, dubLineLanguages(line), line.language);
  if (!match) throw new Error(LabText.t('scenes.choose_a_cast_voice_that', {language: dubLanguageLabel(tag) || LabText.t('scenes.selected_language')}));
  return match;
}
function dubSave() {
  var scene = sceneBody(), line = dubSelectedLine(scene), id = dubSceneId;
  if (!line || scene.scene_id !== id) throw new Error(LabText.t('scenes.open_a_timed_cue'));
  if (!$('dubSpeed').checkValidity() || !$('dubGain').checkValidity()) throw new Error(LabText.t('scenes.enter_a_valid_speed_and'));
  var source = $('dubSourceText').value.trim(), target = $('dubTargetText').value.trim();
  if (!target) throw new Error(LabText.t('scenes.enter_the_target_wording'));
  if (source !== dubOriginalText(line, scene)) line.dub_source_text = source;
  line.text = target;
  line.dub = {fit: $('dubFit').value, speed: Number($('dubSpeed').value),
    gain_db: Number($('dubGain').value), mute: $('dubMute').checked};
  var targetLanguage = $('dubTo').value.trim().toLowerCase();
  if (targetLanguage) {
    line.language = dubSpokenLanguage(line, targetLanguage);
    scene.dub_target_language = targetLanguage;
  }
  updateSceneEditor(scene, null);
  renderSceneForm();
  return saveAndGetScene().then(function () { if (id === dubSceneId) dubNote(LabText.t('scenes.line_saved')); });
}
$('sceneDub').addEventListener('toggle', function () { refreshDub().catch(dubMessage); });
$('dubCue').addEventListener('change', function () { clearDubDraft(); paintDubCue(sceneBody()); });
function clearDubDraft() {
  dubProviderDraft = null;
  $('dubDraftField').hidden = $('dubCopyDraft').hidden = true;
}
$('dubProviderPanel').addEventListener('toggle', async function () {
  if (!this.open) return;
  try {
    var result = await fetchJson('/api/automation/providers'), picker = $('dubProvider');
    var prior = picker.value;
    dubProviders = result.providers.filter(function (profile) { return profile.capabilities.includes('translate'); });
    picker.replaceChildren();
    option(picker, '', LabText.t('scenes.choose_a_provider'));
    dubProviders.forEach(function (profile) { option(picker, profile.id, profile.destination + ' · ' + profile.model); });
    if (dubProviders.some(function (profile) { return profile.id === prior; })) picker.value = prior;
  } catch (error) {
    dubMessage(error);
  }
});
['dubProvider', 'dubFrom', 'dubTo', 'dubSourceText'].forEach(function (id) {
  $(id).addEventListener(id === 'dubSourceText' ? 'input' : 'change', clearDubDraft);
});
$('dubRequestDraft').addEventListener('click', function () {
  var scene = sceneBody(), line = dubSelectedLine(scene);
  var selected = dubProviders.find(function (profile) { return profile.id === $('dubProvider').value; });
  if (!selected || !line) return dubNote(LabText.t('scenes.choose_a_provider_and_a'));
  if (sceneDirty || $('dubSourceText').value.trim() !== dubOriginalText(line, scene).trim())
    return dubNote(LabText.t('scenes.save_the_original_wording_before'));
  var source = $('dubFrom').value.trim(), target = $('dubTo').value.trim();
  if (!source || !target) return dubNote(LabText.t('scenes.choose_the_source_and_target'));
  if (!window.confirm(LabText.t('scenes.send_this_line_s_original', {destination: selected.destination}))) return;
  dubAction(LabText.t('scenes.waiting_for_the_approved_provider'), async function (id) {
    var result = await fetchJson(dubUrl() + '/translate', {expected_revision:scene.revision,
      provider:selected.id,approve_provider_revision:selected.revision,line_id:line.line_id,
      source_language:source,target_language:target});
    if (id !== dubSceneId || sceneBody().revision !== result.revision || $('dubCue').value !== line.line_id ||
        $('dubFrom').value.trim() !== source || $('dubTo').value.trim() !== target || $('dubProvider').value !== selected.id) return;
    dubProviderDraft = result.draft;
    $('dubProviderDraft').value = result.draft.text;
    $('dubDraftField').hidden = $('dubCopyDraft').hidden = false;
    dubNote(LabText.t('scenes.draft_ready_check_the_words_2'));
  });
});
$('dubCopyDraft').addEventListener('click', function () {
  if (dubProviderDraft) $('dubTargetText').value = dubProviderDraft.text;
});
$('dubSeekCue').addEventListener('click', function () {
  var line = dubSelectedLine(sceneBody());
  if (line) dubSeekTo(line.cue.start_ms / 1000);
});
$('dubSeek').addEventListener('input', function () { dubSeekTo(Number(this.value)); });
$('dubWave').addEventListener('click', function (event) {
  if (dubState && dubState.source && this.clientWidth) {
    dubSeekTo(event.offsetX / this.clientWidth * dubState.source.duration_ms / 1000);
  }
});
$('dubOriginal').addEventListener('click', function () { dubPlay(false); });
$('dubDubbed').addEventListener('click', function () { dubPlay(true); });
$('dubSaveCue').addEventListener('click', function () { dubAction(LabText.t('scenes.saving_cue'), function () { return dubSave(); }); });
['dubSourceFile', 'dubBedFile'].forEach(function (control) {
  $(control).addEventListener('change', function () {
    var file = this.files && this.files[0];
    this.value = '';
    if (!file) return;
    dubAction(LabText.t('scenes.preparing_the_local_media'), async function (id) {
      var role = control === 'dubBedFile' ? 'background' : 'source';
      var response = await fetch(dubUrl() + '/' + role + '?name=' + encodeURIComponent(file.name),
        {method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file});
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || LabText.t('scenes.media_preparation_failed'));
      if (id !== dubSceneId) return;
      await refreshDub();
      if (id === dubSceneId) dubNote(LabText.t('scenes.media_attached'));
    });
  });
});
function dubExportOptions(scene) {
  return {expected_revision: scene.revision, target_language: $('dubTo').value,
    keep_original: !$('dubKeep').disabled && $('dubKeep').checked,
    background: !$('dubUseBed').disabled && $('dubUseBed').checked,
    additional_deliveries: Array.from($('dubAlternate').selectedOptions, function (item) { return item.value; }),
    background_gain_db: Number($('dubBedGain').value), duck: $('dubDuck').checked,
    burn: $('dubBurn').disabled ? null : $('dubBurn').value || null};
}
$('dubExport').addEventListener('click', function () {
  dubAction(LabText.t('scenes.verifying_takes_and_writing_the'), async function (id) {
    if (sceneDirty) throw new Error(LabText.t('scenes.save_the_scene_and_select'));
    await fetchJson(dubUrl() + '/exports', dubExportOptions(sceneBody()));
    if (id !== dubSceneId) return;
    await refreshDub();
    if (id === dubSceneId) dubNote(LabText.t('scenes.dub_delivery_ready'));
  });
});
$('dubVerify').addEventListener('click', function () {
  dubAction(LabText.t('scenes.checking_delivery_hashes'), async function (id) {
    var result = await fetchJson(dubUrl() + '/exports/' + dubState.delivery.id + '/verify', {});
    if (id === dubSceneId) dubNote(LabText.t('scenes.delivery_files_verified', {files: result.files}));
  });
});

var dubQueuePoll = null;
function dubQueueAction(path, body) {
  return fetchJson('/api/dub-queue' + path, body).then(function () {
    $('dubQueueStatus').textContent = '';
    return refreshDubQueue();
  }).catch(function (error) {
    $('dubQueueStatus').textContent = error.message;
  });
}
function refreshDubQueue() {
  window.clearTimeout(dubQueuePoll);
  if (!$('dubQueue').open || !$('sceneDub').open || $('sceneDub').hidden) return Promise.resolve();
  return fetchJson('/api/dub-queue').then(function (state) {
    var jobs = $('dubQueueRows'), watches = $('dubWatchRows');
    jobs.replaceChildren();
    watches.replaceChildren();
    state.jobs.forEach(function (job) {
      var line = document.createElement('li');
      line.textContent = job.scene + ' · ' + job.export.target_language + ' · ' + job.status.replaceAll('_', ' ')
        + (job.error ? ' · ' + job.error : '') + ' ';
      if (job.status === 'complete') {
        var link = document.createElement('a');
        link.href = '/api/scenes/' + job.scene_id + '/dub/exports/' + job.delivery_id + '/manifest.json';
        link.textContent = LabText.t('scenes.manifest');
        line.appendChild(link);
      }
      var action = ['failed', 'cancelled'].includes(job.status) ? 'retry'
        : ['queued', 'waiting_for_takes'].includes(job.status) ? 'cancel' : '';
      if (action) line.appendChild(sceneButton(action === 'retry' ? LabText.t('scenes.retry') : LabText.t('scenes.cancel'), action === 'retry' ? LabText.t('scenes.retry_dubbing_job') : LabText.t('scenes.cancel_dubbing_job'),
        function () { dubQueueAction('/jobs/' + job.id + '/' + action, {}); }));
      jobs.appendChild(line);
    });
    state.watches.filter(function (watch) { return watch.enabled; }).forEach(function (watch) {
      var line = document.createElement('li');
      line.textContent = watch.path + (watch.error ? ' · ' + watch.error : '') + ' ';
      line.appendChild(sceneButton(LabText.t('scenes.disable'), LabText.t('scenes.disable_this_watch_folder'),
        function () { dubQueueAction('/watches/' + watch.id + '/disable', {}); }));
      watches.appendChild(line);
    });
  }).catch(function (error) {
    $('dubQueueStatus').textContent = error.message;
  }).finally(function () {
    window.clearTimeout(dubQueuePoll);
    dubQueuePoll = window.setTimeout(refreshDubQueue, 2500);
  });
}
function currentDubJob() {
  var scene = sceneBody();
  if (sceneDirty) throw new Error(LabText.t('scenes.save_the_scene_before_queuing'));
  var opts = dubExportOptions(scene);
  if (!opts.target_language.trim()) throw new Error(LabText.t('scenes.enter_a_target_language_code'));
  return {scene_id: scene.scene_id, export: opts, render_missing: $('dubQueueRender').checked};
}
$('dubQueue').addEventListener('toggle', function () { refreshDubQueue(); });
$('sceneDub').addEventListener('toggle', function () { refreshDubQueue(); });
$('dubQueueAdd').addEventListener('click', function () {
  try {
    dubQueueAction('', {items: [currentDubJob()]});
  } catch (error) {
    $('dubQueueStatus').textContent = error.message;
  }
});
$('dubWatchAdd').addEventListener('click', function () {
  dubQueueAction('/watches', {path: $('dubWatchPath').value.trim()});
});
$('dubWatchTemplate').addEventListener('click', function () {
  try {
    var job = currentDubJob(), link = document.createElement('a');
    var url = URL.createObjectURL(new Blob([JSON.stringify(job, null, 2) + '\n'], {type: 'application/json'}));
    link.href = url;
    link.download = 'delivery.dubjob.json';
    link.click();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  } catch (error) {
    $('dubQueueStatus').textContent = error.message;
  }
});
