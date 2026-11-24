// Voice Lab take editing: trim, silence, fades and gain on a saved take. An edit is heard first and saved as a new take that names
// the original; the original is never changed.
var TAKE_EDIT_POINTS = 600;
var TAKE_EDIT_FIELDS = [
  {name:'start_seconds', label:LabText.t('cast.start_s'), noun:LabText.t('cast.the_start'), placeholder:'0', step:'0.01', row:0},
  {name:'end_seconds', label:LabText.t('cast.end_s'), noun:LabText.t('cast.the_end'), placeholder:LabText.t('cast.end_of_take'), step:'0.01', row:0},
  {name:'silence_threshold_db', label:LabText.t('cast.quieter_than_db'), noun:LabText.t('cast.the_silence_level'), placeholder:'-50', step:'1', row:1, silence:true},
  {name:'keep_silence_ms', label:LabText.t('cast.silence_to_keep_ms'), noun:LabText.t('cast.the_silence_to_keep'), placeholder:'80', step:'10', row:1, silence:true},
  {name:'fade_in_ms', label:LabText.t('cast.fade_in_ms'), noun:LabText.t('cast.the_fade_in'), placeholder:'0', step:'10', row:2},
  {name:'fade_out_ms', label:LabText.t('cast.fade_out_ms'), noun:LabText.t('cast.the_fade_out'), placeholder:'0', step:'10', row:2},
  {name:'gain_db', label:LabText.t('cast.gain_db'), noun:LabText.t('cast.the_gain'), placeholder:'0', step:'0.5', row:2}
];
function takeEditNumber(text, label) {
  var trimmed = String(text == null ? '' : text).trim();
  if (trimmed === '') { return null; }
  var number = Number(trimmed);
  if (!isFinite(number)) { throw new Error(LabText.t('cast.must_be_a_number', {label: label})); }
  return number;
}
// The JSON body for the edit routes from the panel's fields. A blank field is left out, so the service's default applies; the
// silence fields count only while the silence box is ticked.
function takeEditRequest(values) {
  var request = {};
  TAKE_EDIT_FIELDS.forEach(function (field) {
    if (field.silence && !values.auto_trim) { return; }
    var number = takeEditNumber(values[field.name], field.noun);
    if (number !== null) { request[field.name] = number; }
  });
  if (values.auto_trim) { request.auto_trim = true; }
  return request;
}
function takeEditSecondsAt(x, width, duration) {
  if (!(width > 0) || !(duration > 0)) { return 0; }
  return Math.round(Math.min(duration, Math.max(0, x / width * duration)) * 1000) / 1000;
}
function takeEditKey() {
  var random = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now()) + '-' + String(Math.random()).slice(2);
  return 'take-edit-' + random;
}
function takeEditSigned(number) { return (number > 0 ? '+' : '') + number; }
// One sentence on how an edited take differs from its original, or '' for a take that is not an edit.
function takeEditNote(take) {
  var edit = take && take.edit;
  if (!edit) { return ''; }
  var request = edit.request || {}, result = edit.result || {}, parts = [];
  if (result.start_seconds > 0 || result.end_seconds < edit.parent_duration_seconds - 0.0005) {
    parts.push(LabText.t('cast.kept_to_s_of_the', {start_seconds: result.start_seconds.toFixed(2), end_seconds: result.end_seconds.toFixed(2), original_seconds: edit.parent_duration_seconds.toFixed(2)}));
  }
  if (request.auto_trim) {
    parts.push(LabText.t('cast.silence_quieter_than_db_cut', {silence_threshold_db: request.silence_threshold_db, keep_silence_ms: request.keep_silence_ms}));
  }
  if (request.fade_in_ms) { parts.push(LabText.t('cast.fade_in_ms_2', {fade_in_ms: request.fade_in_ms})); }
  if (request.fade_out_ms) { parts.push(LabText.t('cast.fade_out_ms_2', {fade_out_ms: request.fade_out_ms})); }
  if (request.gain_db) { parts.push(LabText.t('cast.gain_db_2', {decibels: takeEditSigned(request.gain_db)})); }
  return (parts.length ? LabText.t('cast.edited_take_details', {take_id: String(take.parent_take_id).slice(0, 8), details: parts.join(', ')}) : LabText.t('cast.edited_from_take', {take_id: String(take.parent_take_id).slice(0, 8)}));
}
function drawTakeWave(canvas, wave, kept) {
  var ctx = canvas.getContext('2d'), width = canvas.width, height = canvas.height, style = getComputedStyle(document.body);
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = style.getPropertyValue('--bg'); ctx.fillRect(0, 0, width, height);
  if (!wave || !wave.peaks.length) { return; }
  ctx.strokeStyle = style.getPropertyValue('--line'); ctx.beginPath(); ctx.moveTo(0, height / 2); ctx.lineTo(width, height / 2); ctx.stroke();
  ctx.strokeStyle = style.getPropertyValue('--accent'); ctx.beginPath();
  wave.peaks.forEach(function (pair, index) {
    var x = (index + 0.5) / wave.peaks.length * width;
    ctx.moveTo(x, (1 - pair[1]) * height / 2); ctx.lineTo(x, (1 - pair[0]) * height / 2);
  });
  ctx.stroke();
  if (kept) {
    ctx.fillStyle = 'rgba(138,79,255,0.12)';
    ctx.fillRect(kept.start / wave.duration_seconds * width, 0, (kept.end - kept.start) / wave.duration_seconds * width, height);
  }
  if (wave.speech) {
    ctx.fillStyle = style.getPropertyValue('--accent');
    ctx.fillRect(wave.speech.start_seconds / wave.duration_seconds * width, height - 4,
      (wave.speech.end_seconds - wave.speech.start_seconds) / wave.duration_seconds * width, 3);
  }
}
function takeEditElement(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text) { node.textContent = text; }
  return node;
}
function takeEditLabelled(label, input) {
  var wrap = takeEditElement('label', 'field compact');
  wrap.appendChild(takeEditElement('span', '', label)); wrap.appendChild(input);
  return wrap;
}
// Adds an "Edit take…" button to a take's actions. It opens a panel under the take and closes it again.
function appendTakeEditor(article, take, actions, after, show) {
  var button = takeEditElement('button', '', LabText.t('cast.edit_take')), slot = takeEditElement('div'), editor = null;
  button.type = 'button'; button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', function () {
    if (editor) { editor.close(); return; }
    editor = openTakeEditor(slot, take, after, show, function () { editor = null; button.setAttribute('aria-expanded', 'false'); });
    button.setAttribute('aria-expanded', 'true');
  });
  actions.appendChild(button); article.appendChild(slot);
}
function openTakeEditor(slot, take, after, show, onClose) {
  var state = {wave:null, previewUrl:null, revision:0, key:takeEditKey(), closed:false, drag:null};
  var root = takeEditElement('div', 'take-edit'), inputs = {}, rows = [takeEditElement('div', 'row'), takeEditElement('div', 'row'), takeEditElement('div', 'row')];
  root.setAttribute('role', 'group'); root.setAttribute('aria-label', LabText.t('cast.edit_this_take'));
  root.appendChild(takeEditElement('p', 'meta', LabText.t('cast.drag_across_the_waveform_to')));
  var canvas = takeEditElement('canvas', 'wave'), speech = takeEditElement('p', 'meta take-edit-speech', LabText.t('cast.loading_the_waveform'));
  canvas.width = 900; canvas.height = 120; canvas.setAttribute('aria-label', LabText.t('cast.waveform_of_this_take_drag'));
  root.appendChild(canvas); root.appendChild(speech);
  var silence = document.createElement('input');
  silence.type = 'checkbox'; silence.setAttribute('data-edit-field', 'auto_trim');
  TAKE_EDIT_FIELDS.forEach(function (field) {
    var input = document.createElement('input');
    input.type = 'number'; input.step = field.step; input.placeholder = field.placeholder; input.setAttribute('data-edit-field', field.name);
    inputs[field.name] = input;
    if (field.name === 'silence_threshold_db') {
      var box = takeEditElement('label');
      box.appendChild(silence); box.appendChild(document.createTextNode(LabText.t('cast.cut_the_silence_at_both'))); rows[1].appendChild(box);
    }
    rows[field.row].appendChild(takeEditLabelled(field.label, input));
  });
  rows.forEach(function (row) { root.appendChild(row); });
  var buttons = takeEditElement('div', 'cast-actions'), hear = takeEditElement('button', '', LabText.t('cast.hear_the_edit')),
    save = takeEditElement('button', 'go', LabText.t('cast.save_as_new_take')), close = takeEditElement('button', '', LabText.t('cast.close'));
  [hear, save, close].forEach(function (button) { button.type = 'button'; buttons.appendChild(button); });
  var player = takeEditElement('audio'), status = takeEditElement('p', 'meta'), problem = takeEditElement('p');
  player.controls = true; player.hidden = true; problem.setAttribute('role', 'alert'); status.setAttribute('role', 'status');
  [buttons, player, status, problem].forEach(function (node) { root.appendChild(node); });
  hear.disabled = true; save.disabled = true;

  function values() {
    var out = {auto_trim: silence.checked};
    TAKE_EDIT_FIELDS.forEach(function (field) { out[field.name] = inputs[field.name].value; });
    return out;
  }
  function syncSilence() { inputs.silence_threshold_db.disabled = inputs.keep_silence_ms.disabled = !silence.checked; }
  function kept() {
    var start = inputs.start_seconds.value === '' ? 0 : Number(inputs.start_seconds.value),
      end = inputs.end_seconds.value === '' ? take.duration_seconds : Number(inputs.end_seconds.value);
    return isFinite(start) && isFinite(end) && end > start ? {start:start, end:end} : null;
  }
  function redraw() { drawTakeWave(canvas, state.wave, kept()); }
  function stale(revision) { return !root.isConnected || revision !== state.revision; }
  function say(message) { status.textContent = message || ''; }
  function fail(error) { problem.textContent = error ? error.message : ''; }
  function clearPreview() {
    if (state.previewUrl) { URL.revokeObjectURL(state.previewUrl); state.previewUrl = null; }
    player.pause(); player.removeAttribute('src'); player.hidden = true;
  }
  // Any change to the edit makes an earlier preview wrong, drops a preview still on its way, and starts a new save request.
  function changed() { state.revision++; state.key = takeEditKey(); clearPreview(); fail(null); say(''); redraw(); }
  TAKE_EDIT_FIELDS.forEach(function (field) { inputs[field.name].addEventListener('input', changed); });
  silence.addEventListener('change', function () { syncSilence(); changed(); });
  syncSilence();

  function pointerSeconds(event) {
    var rect = canvas.getBoundingClientRect();
    return takeEditSecondsAt(event.clientX - rect.left, rect.width, state.wave.duration_seconds);
  }
  canvas.addEventListener('pointerdown', function (event) {
    if (!state.wave || event.button > 0) { return; }
    state.drag = {from:pointerSeconds(event), moved:false, before:[inputs.start_seconds.value, inputs.end_seconds.value]};
    if (canvas.setPointerCapture) { canvas.setPointerCapture(event.pointerId); }
  });
  canvas.addEventListener('pointermove', function (event) {
    var drag = state.drag;
    if (!drag) { return; }
    var at = pointerSeconds(event);
    if (!drag.moved && Math.abs(at - drag.from) < 0.01) { return; }
    drag.moved = true;
    inputs.start_seconds.value = String(Math.min(drag.from, at)); inputs.end_seconds.value = String(Math.max(drag.from, at)); redraw();
  });
  canvas.addEventListener('pointerup', function () {
    var drag = state.drag;
    state.drag = null;
    if (drag && drag.moved) { changed(); }
  });
  canvas.addEventListener('pointercancel', function () {
    var drag = state.drag;
    state.drag = null;
    if (drag) { inputs.start_seconds.value = drag.before[0]; inputs.end_seconds.value = drag.before[1]; redraw(); }
  });

  hear.addEventListener('click', function () {
    var request;
    try { request = takeEditRequest(values()); } catch (error) { fail(error); return; }
    var revision = ++state.revision;
    fail(null); say(LabText.t('cast.making_the_edited_clip'));
    fetch('/api/takes/' + take.id + '/edit-preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(request)})
      .then(function (response) {
        if (!response.ok) { return response.json().then(function (j) { throw new Error(j.error || response.status); }); }
        return response.blob().then(function (blob) {
          return {blob:blob, seconds:response.headers.get('X-Audio-Seconds'), peak:response.headers.get('X-Peak-Dbfs')};
        });
      })
      .then(function (result) {
        if (stale(revision)) { return; }
        clearPreview(); state.previewUrl = URL.createObjectURL(result.blob); player.src = state.previewUrl; player.hidden = false;
        var playing = player.play();
        if (playing && playing.catch) { playing.catch(function () {}); }
        say((result.peak ? LabText.t('cast.edited_clip_peak', {seconds: result.seconds, peak: result.peak}) : LabText.t('cast.edited_clip_silent', {seconds: result.seconds})));
      })
      .catch(function (error) { if (!stale(revision)) { say(''); fail(error); } });
  });
  save.addEventListener('click', function () {
    var request;
    try { request = takeEditRequest(values()); } catch (error) { fail(error); return; }
    request.idempotency_key = state.key;
    save.disabled = hear.disabled = true; fail(null); say(LabText.t('cast.saving'));
    fetchJson('/api/takes/' + take.id + '/edit', request).then(function (result) {
      state.key = takeEditKey();
      show(result.replayed ? LabText.t('cast.that_edit_was_already_saved')
        : LabText.t('cast.saved_as_a_new_take', {seconds: result.take.duration_seconds.toFixed(2)}));
      return Promise.resolve(after()).catch(function (error) { show(error.message); });  // the save worked; only the refresh failed
    }, function (error) {
      if (root.isConnected) { say(''); fail(error); } else { show(error.message); }
    }).then(function () { save.disabled = hear.disabled = false; });
  });
  function closeEditor() {
    if (state.closed) { return; }
    state.closed = true; clearPreview();
    if (root.parentNode) { root.parentNode.removeChild(root); }
    onClose();
  }
  close.addEventListener('click', closeEditor);
  slot.appendChild(root);

  fetchJson('/api/takes/' + take.id + '/waveform?points=' + TAKE_EDIT_POINTS).then(function (wave) {
    if (!root.isConnected) { return; }
    state.wave = wave; hear.disabled = save.disabled = false; redraw();
    speech.textContent = wave.speech ? LabText.t('cast.speech_from_to_s_louder', {start_seconds: wave.speech.start_seconds.toFixed(2), end_seconds: wave.speech.end_seconds.toFixed(2), threshold_db: wave.speech.threshold_db}) : LabText.t('cast.no_speech_found_this_take');
  }).catch(function (error) { speech.textContent = ''; fail(error); });
  return {close:closeEditor};
}
