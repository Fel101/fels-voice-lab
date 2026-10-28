// Voice Lab take editing: trim, silence, fades and gain on a saved take. An edit is heard first and saved as a new take that names
// the original; the original is never changed.
var TAKE_EDIT_POINTS = 600;
var TAKE_EDIT_FIELDS = [
  {name:'start_seconds', label:'Start (s)', noun:'The start', placeholder:'0', step:'0.01', row:0},
  {name:'end_seconds', label:'End (s)', noun:'The end', placeholder:'end of take', step:'0.01', row:0},
  {name:'silence_threshold_db', label:'Quieter than (dB)', noun:'The silence level', placeholder:'-50', step:'1', row:1, silence:true},
  {name:'keep_silence_ms', label:'Silence to keep (ms)', noun:'The silence to keep', placeholder:'80', step:'10', row:1, silence:true},
  {name:'fade_in_ms', label:'Fade in (ms)', noun:'The fade in', placeholder:'0', step:'10', row:2},
  {name:'fade_out_ms', label:'Fade out (ms)', noun:'The fade out', placeholder:'0', step:'10', row:2},
  {name:'gain_db', label:'Gain (dB)', noun:'The gain', placeholder:'0', step:'0.5', row:2}
];
function takeEditNumber(text, label) {
  var trimmed = String(text == null ? '' : text).trim();
  if (trimmed === '') { return null; }
  var number = Number(trimmed);
  if (!isFinite(number)) { throw new Error(label + ' must be a number.'); }
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
    parts.push('kept ' + result.start_seconds.toFixed(2) + ' to ' + result.end_seconds.toFixed(2) + ' s of the original ' +
      edit.parent_duration_seconds.toFixed(2) + ' s');
  }
  if (request.auto_trim) {
    parts.push('silence quieter than ' + request.silence_threshold_db + ' dB cut from both ends, ' + request.keep_silence_ms + ' ms left');
  }
  if (request.fade_in_ms) { parts.push('fade in ' + request.fade_in_ms + ' ms'); }
  if (request.fade_out_ms) { parts.push('fade out ' + request.fade_out_ms + ' ms'); }
  if (request.gain_db) { parts.push('gain ' + takeEditSigned(request.gain_db) + ' dB'); }
  return 'Edited from take ' + String(take.parent_take_id).slice(0, 8) + (parts.length ? ': ' + parts.join(', ') : '') + '.';
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
  var button = takeEditElement('button', '', 'Edit take…'), slot = takeEditElement('div'), editor = null;
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
  root.setAttribute('role', 'group'); root.setAttribute('aria-label', 'Edit this take');
  root.appendChild(takeEditElement('p', 'meta', 'Drag across the waveform to choose the part to keep, or type the times. Listen to the result, then save it. ' +
    'It is saved as a new take; this one is not changed.'));
  var canvas = takeEditElement('canvas', 'wave'), speech = takeEditElement('p', 'meta take-edit-speech', 'Loading the waveform…');
  canvas.width = 900; canvas.height = 120; canvas.setAttribute('aria-label', 'Waveform of this take. Drag across it to choose the part to keep.');
  root.appendChild(canvas); root.appendChild(speech);
  var silence = document.createElement('input');
  silence.type = 'checkbox'; silence.setAttribute('data-edit-field', 'auto_trim');
  TAKE_EDIT_FIELDS.forEach(function (field) {
    var input = document.createElement('input');
    input.type = 'number'; input.step = field.step; input.placeholder = field.placeholder; input.setAttribute('data-edit-field', field.name);
    inputs[field.name] = input;
    if (field.name === 'silence_threshold_db') {
      var box = takeEditElement('label');
      box.appendChild(silence); box.appendChild(document.createTextNode(' Cut the silence at both ends')); rows[1].appendChild(box);
    }
    rows[field.row].appendChild(takeEditLabelled(field.label, input));
  });
  rows.forEach(function (row) { root.appendChild(row); });
  var buttons = takeEditElement('div', 'cast-actions'), hear = takeEditElement('button', '', 'Hear the edit'),
    save = takeEditElement('button', 'go', 'Save as new take'), close = takeEditElement('button', '', 'Close');
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
    fail(null); say('Making the edited clip…');
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
        say('The edited clip is ' + result.seconds + ' s' + (result.peak ? ', peaking at ' + result.peak + ' dBFS.' : ' and silent.'));
      })
      .catch(function (error) { if (!stale(revision)) { say(''); fail(error); } });
  });
  save.addEventListener('click', function () {
    var request;
    try { request = takeEditRequest(values()); } catch (error) { fail(error); return; }
    request.idempotency_key = state.key;
    save.disabled = hear.disabled = true; fail(null); say('Saving…');
    fetchJson('/api/takes/' + take.id + '/edit', request).then(function (result) {
      state.key = takeEditKey();
      show(result.replayed ? 'That edit was already saved as a new take.'
        : 'Saved as a new take, ' + result.take.duration_seconds.toFixed(2) + ' s. The original is unchanged.');
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
    speech.textContent = wave.speech ? 'Speech from ' + wave.speech.start_seconds.toFixed(2) + ' to ' + wave.speech.end_seconds.toFixed(2) +
      ' s (louder than ' + wave.speech.threshold_db + ' dB).' : 'No speech found: this take is quieter than the default silence level.';
  }).catch(function (error) { speech.textContent = ''; fail(error); });
  return {close:closeEditor};
}
