// Voice Lab studio: model catalog, engine status and the saved-clip history.
/** The element with this id, typed loosely: the page's ids name selects, inputs, players and dialogs alike.
 * @param {string} id @returns {any} */
var $ = function (id) { return document.getElementById(id); };
var hints = {
  kokoro: LabText.t('studio.kokoro_has_fixed_voices_and'),
  'phonon-31m-en': LabText.t('studio.phonon_speaks_english_with_one'),
  turbo: LabText.t('studio.chatterbox_turbo_clones_a_voice'),
  orig: LabText.t('studio.chatterbox_clones_a_voice_recording'),
  'breeze-tts-2': LabText.t('studio.breeze_designs_voices_clones_voice'),
  'qwen3-tts-1.7b-voice-design': LabText.t('studio.qwen_voicedesign_creates_a_reusable'),
  'qwen3-tts-1.7b-base': LabText.t('studio.qwen_base_clones_a_voice'),
  'qwen3-tts-0.6b-base': LabText.t('studio.qwen_base_clones_a_voice'),
  'qwen3-tts-1.7b-custom-voice': LabText.t('studio.qwen_customvoice_uses_one_of'),
  'voxcpm2': LabText.t('studio.voxcpm2_designs_voices_clones_voice')
};
var descriptions = {
  warm: LabText.t('studio.a_warm_soft_spoken_young'),
  playful: LabText.t('studio.a_playful_energetic_young_woman'),
  low: LabText.t('studio.a_calm_low_pitched_woman'),
  custom: ''
};
var phononVoices = [];
var lastEngine = null;
var activeStream = null;
var lastPerformanceSamplingEngine = null;
var catalog = null;
var referenceRows = [];
var performanceCapabilitiesByEngine = {};
var performanceUnscreenedByEngine = {};
var lastPerformanceIdentityId = null;
var sceneDirty = false;
function guardUnsavedSceneUnload(event){if(sceneDirty){event.preventDefault();event.returnValue='';}}
window.addEventListener('beforeunload',guardUnsavedSceneUnload);

function option(select, value, label, disabled) {
  var item = document.createElement('option'); item.value = value; item.textContent = label; item.disabled = !!disabled; select.appendChild(item);
}
function selectedValues(select) { return Array.from(select.selectedOptions || []).map(function (item) { return item.value; }); }
function qwenModeFor(engine) {
  if (engine === 'qwen3-tts-1.7b-voice-design') { return 'design'; }
  if (engine === 'qwen3-tts-1.7b-base' || engine === 'qwen3-tts-0.6b-base') { return 'clone'; }
  if (engine === 'qwen3-tts-1.7b-custom-voice') { return 'custom_voice'; }
  return null;
}
function refresh() {
  showEngineHint();
  var e = $('engine').value, mode = $('mode').value, qwenMode = qwenModeFor(e);
  var breeze = e === 'breeze-tts-2';
  var drama = e === 'drama-3';
  $('dramaPanel').hidden=!drama;
  $('dramaDescriptionWrap').hidden=$('dramaMode').value!=='design';
  $('dramaReferenceWrap').hidden=$('dramaMode').value!=='clone';
  var voxcpm = e === 'voxcpm2', voxMode = $('voxMode').value;
  var phonon = e === 'phonon-31m-en';
  $('phononPanel').hidden = !phonon;
  $('breezePanel').hidden = !breeze;
  $('qwenPanel').hidden = !qwenMode;
  $('voxPanel').hidden = !voxcpm;
  $('voiceWrap').hidden = e !== 'kokoro'; $('kokoroLanguageWrap').hidden = e !== 'kokoro';
  $('kokoroTools').hidden = e !== 'kokoro'; $('speedWrap').hidden = e !== 'kokoro';
  $('chatterboxSampling').hidden = e !== 'orig' && e !== 'turbo';
  $('origOpts').hidden = e !== 'orig'; $('turboOpts').hidden = e !== 'turbo';
  $('cloneWrap').hidden = e === 'kokoro' || phonon || breeze || !!qwenMode || voxcpm || drama;
  $('chatterboxReferenceWrap').hidden = !$('clone').checked;
  $('designFields').hidden = !breeze || mode !== 'design';
  $('referenceFields').hidden = !breeze || mode === 'design';
  $('directionFields').hidden = !breeze || mode !== 'direction';
  $('reroll').hidden = !breeze || mode !== 'design';
  $('stream').hidden = !qwenMode;
  $('cfgWrap').hidden = breeze && mode === 'clone';
  $('qwenDesignFields').hidden = qwenMode !== 'design';
  $('qwenCloneFields').hidden = qwenMode !== 'clone';
  $('qwenCustomFields').hidden = qwenMode !== 'custom_voice';
  $('voxDesignFields').hidden = !voxcpm || voxMode !== 'design';
  $('voxCloneFields').hidden = !voxcpm || voxMode !== 'clone';
  $('voxContinuationFields').hidden = !voxcpm || voxMode !== 'continuation';
  $('voxCloneDirectionWrap').hidden = !voxcpm || voxMode !== 'clone';
  if (e !== lastEngine) {
    $('clone').checked = e === 'turbo';
    if (e === 'orig') { $('cbTopP').value = '1'; }
    else if (e === 'turbo') { $('cbTopP').value = '0.95'; }
    lastEngine = e;
    updateValues();
    // A page that was open while Phonon was installed has no voices yet: read them when the model is chosen.
    if (phonon && !phononVoices.length) { refreshPhononVoices().catch(function () {}); }
  }
  $('chatterboxReferenceWrap').hidden = !$('clone').checked;
  $('hint').textContent = hints[e] || '';
}
function updateValues() {
  /** @type {Array<[string, string, number]>} */ ([['speed','speedV',2],['exag','exagV',2],['cfg','cfgV',2],['cfgScale','cfgScaleV',1],['maxTokens','maxTokensV',0],
   ['temperature','temperatureV',2],['topK','topKV',0],['topP','topPV',2],['repetitionPenalty','repetitionPenaltyV',2],
   ['depthTemperature','depthTemperatureV',2],['depthTopK','depthTopKV',0],['depthTopP','depthTopPV',2],
   ['qwenMaxTokens','qwenMaxTokensV',0],['qwenTemperature','qwenTemperatureV',2],['qwenTopK','qwenTopKV',0],
   ['qwenTopP','qwenTopPV',2],['qwenRepetition','qwenRepetitionV',2],
   ['phononTemperature','phononTemperatureV',2],['phononPace','phononPaceV',1]]).forEach(function (x) {
    $(x[1]).textContent = Number($(x[0]).value).toFixed(x[2]);
  });
}
function fetchJson(url, body) {
  return fetch(url, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})
    .then(function (r) { return r.json().then(function (j) { if (!r.ok) { throw new Error(LabText.error(j, r.status)); } return j; }); });
}
function loadReferences(choosePath) {
  return fetchJson('/api/references').then(function (result) {
    referenceRows = result.references || [];
    VoiceLabWorkflows.render(referenceRows, choosePath);
  }).catch(function (e) { $('err').textContent = LabText.t('studio.could_not_list_the_saved', {message: e.message}); });
}
function unscreenedLanguages(engine) { return performanceUnscreenedByEngine[engine] || []; }
function languageOptionLabel(engine, language) { return unscreenedLanguages(engine).indexOf(language) >= 0 ? language + LabText.t('studio.unscreened') : language; }
function applyQwenCapabilities(info, engine) {
  if (!info) { return; }
  var languages = info.supported_languages || [];
  if (languages.length) {
    var currentLanguage = $('qwenLanguage').value;
    $('qwenLanguage').textContent = '';
    languages.forEach(function (language) { option($('qwenLanguage'), language, languageOptionLabel(engine, language)); });
    $('qwenLanguage').value = languages.indexOf(currentLanguage) >= 0 ? currentLanguage : (languages.indexOf('English') >= 0 ? 'English' : languages[0]);
  }
  if (qwenModeFor($('engine').value) === 'custom_voice' && Array.isArray(info.supported_speakers)) {
    var currentSpeaker = $('qwenSpeaker').value;
    $('qwenSpeaker').textContent = '';
    info.supported_speakers.forEach(function (speaker) { option($('qwenSpeaker'), speaker, speaker); });
    if (!info.supported_speakers.length) { option($('qwenSpeaker'), '', LabText.t('studio.loaded_model_reported_no_speakers'), true); }
    else { $('qwenSpeaker').value = info.supported_speakers.indexOf(currentSpeaker) >= 0 ? currentSpeaker : info.supported_speakers[0]; }
  }
}
function refreshLoadedQwenInfo() {
  var engine = $('engine').value;
  if (!qwenModeFor(engine)) { return; }
  fetchJson('/api/health').then(function (health) {
    var worker = (health.workers || []).find(function (row) { return row.engine === engine && row.model_loaded; });
    if (worker) { applyQwenCapabilities(worker.model_info, engine); }
  }).catch(function () {});
}
function phononVoiceLabel(voice) {
  var detail = voice.character ? 'character' : [voice.gender, voice.age].filter(Boolean).join(', ').toLowerCase();
  return voice.name + (detail ? ' (' + detail + ')' : '');
}
function showPhononVoiceInfo() {
  [['phononVoice', 'phononVoiceInfo'], ['phononIdentityVoice', 'phononIdentityInfo']].forEach(function (pair) {
    var voice = phononVoices.find(function (item) { return item.name === $(pair[0]).value; });
    $(pair[1]).textContent = voice ? voice.group + ': ' + voice.description
      : LabText.t('studio.no_phonon_voices_are_installed');
  });
}
// Fill the studio picker and the Cast picker from /api/info's phonon_voices (dicts, grouped American, British, characters).
function fillPhononVoices(voices) {
  phononVoices = voices || [];
  var names = phononVoices.map(function (voice) { return voice.name; });
  ['phononVoice', 'phononIdentityVoice'].forEach(function (id) {
    var select = $(id), before = select.value, groups = {};
    select.textContent = '';
    phononVoices.forEach(function (voice) {
      if (!groups[voice.group]) {
        groups[voice.group] = document.createElement('optgroup'); groups[voice.group].label = voice.group; select.appendChild(groups[voice.group]);
      }
      var item = document.createElement('option'); item.value = voice.name; item.textContent = phononVoiceLabel(voice); item.title = voice.description || '';
      groups[voice.group].appendChild(item);
    });
    select.value = names.indexOf(before) >= 0 ? before : (names.indexOf('Harper') >= 0 ? 'Harper' : names[0] || '');
  });
  showPhononVoiceInfo();
}
function refreshPhononVoices() {
  return fetchJson('/api/info').then(function (info) { fillPhononVoices(info.phonon_voices); return phononVoices.length; });
}
function openHistoryDb() {
  return new Promise(/** @param {(value?: any) => void} resolve @param {(reason?: any) => void} reject */ function (resolve, reject) {
    if (!window.indexedDB) { reject(new Error(LabText.t('studio.this_browser_does_not_provide'))); return; }
    var request = window.indexedDB.open('voice-lab-studio', 1);
    request.onupgradeneeded = function () {
      var db = request.result;
      if (!db.objectStoreNames.contains('runs')) { db.createObjectStore('runs', {keyPath:'id', autoIncrement:true}); }
    };
    request.onsuccess = function () { resolve(request.result); };
    request.onerror = function () { reject(request.error || new Error(LabText.t('studio.could_not_open_local_generation'))); };
  });
}
function persistHistory(blob, body, meta) {
  return openHistoryDb().then(function (db) {
    return new Promise(/** @param {(value?: any) => void} resolve @param {(reason?: any) => void} reject */ function (resolve, reject) {
      var tx = db.transaction('runs', 'readwrite'), store = tx.objectStore('runs');
      store.add({body:body, meta:meta, audio:blob, createdAt:Date.now()});
      tx.oncomplete = function () { db.close(); resolve(); };
      tx.onerror = function () { db.close(); reject(tx.error || new Error(LabText.t('studio.could_not_save_the_generated'))); };
      tx.onabort = function () { db.close(); reject(tx.error || new Error(LabText.t('studio.local_generation_history_write_was'))); };
    });
  }).then(function () {
    return openHistoryDb().then(function (db) {
      return new Promise(/** @param {(value?: any) => void} resolve @param {(reason?: any) => void} reject */ function (resolve, reject) {
        var tx = db.transaction('runs', 'readwrite'), store = tx.objectStore('runs'), request = store.getAll();
        request.onsuccess = function () {
          var rows = request.result.sort(function (a,b) { return a.id - b.id; });
          rows.slice(0, Math.max(0, rows.length - 20)).forEach(function (row) { store.delete(row.id); });
        };
        tx.oncomplete = function () { db.close(); resolve(); };
        tx.onerror = function () { db.close(); reject(tx.error || new Error(LabText.t('studio.could_not_trim_local_generation'))); };
        tx.onabort = function () { db.close(); reject(tx.error || new Error(LabText.t('studio.local_generation_history_update_was'))); };
      });
    });
  });
}
function restoreHistory() {
  return openHistoryDb().then(function (db) {
    return new Promise(/** @param {(value?: any) => void} resolve @param {(reason?: any) => void} reject */ function (resolve, reject) {
      var tx = db.transaction('runs', 'readonly'), request = tx.objectStore('runs').getAll();
      request.onsuccess = function () {
        var rows = request.result.sort(function (a,b) { return a.id - b.id; });
        rows.forEach(function (row) { makeHistory(row.audio, row.body, row.meta); });
      };
      tx.oncomplete = function () { db.close(); resolve(); };
      tx.onerror = function () { db.close(); reject(tx.error || new Error(LabText.t('studio.could_not_restore_local_generation'))); };
    });
  }).catch(function (error) { $('err').textContent = LabText.t('studio.local_generation_history_is_unavailable', {message: error.message}); });
}
function fillCatalog() {
  return fetchJson('/api/catalog').then(function (result) {
    catalog = result;
    $('engine').textContent = '';
    (result.engines || []).forEach(function (entry) {
      var ready = entry.integration && entry.integration.service_api && !entry.integration.task_only;
      if (!(entry.integration && entry.integration.task_only)) {
        option($('engine'), entry.id, entry.name + (ready ? '' : LabText.t('studio.not_integrated_yet')), !ready);
      }
    });
    $('identityEngine').textContent = '';
    (result.engines || []).forEach(function (entry) {
      var caps = entry.capabilities || {}, clone = caps.voice_cloning || {}, selection = caps.voice_selection || {};
      var preset = (entry.id==='qwen3-tts-1.7b-custom-voice' || entry.id==='phonon-31m-en') && selection.status==='worker_verified';
      if (entry.integration && entry.integration.service_api &&
          ((clone.status === 'supported' || clone.status === 'worker_verified') || entry.id === 'kokoro' && selection.status === 'supported' || preset)) {
        option($('identityEngine'), entry.id, entry.name);
      }
    });
    $('engine').value = 'kokoro';
    fillFixedVoices();
    updateIdentityEngineKind();
    renderModelOverview(result);
    refreshEngineStatus();
    return fetchJson('/api/performance/settings').then(function (schema) {
      performanceCapabilitiesByEngine={};(schema.engines||[]).forEach(function(entry){performanceCapabilitiesByEngine[entry.engine_id]=entry;});
      performanceUnscreenedByEngine=schema.unscreened_languages||{};
    });
  });
}
var modelCards={};
var engineStateNames={not_integrated:LabText.t('studio.not_integrated'),not_installed:LabText.t('studio.not_installed_yet'),conversion_task:LabText.t('studio.conversion_task')};
function deviceLabel(device){return device==='cpu'?'CPU':device==='cuda'?'GPU':String(device||'');}
function engineStateText(row,device,job){
  // While an install runs the card's text stays put, so the cards around it do not move: its progress is in the install window.
  if(job&&job.state==='running'){return job.nvidia?LabText.t('studio.installing_the_gpu_build'):LabText.t('studio.installing');}
  // A model that could not use the graphics card when it loaded ran on the CPU: say so rather than the service's device.
  var ran=row.check&&row.check.ran_on==='cpu'?'cpu':device;
  if(row.state==='usable'){return LabText.t('studio.speaks_now_on', {deviceLabel: deviceLabel(ran)});}
  if(row.state==='check_failed'){return LabText.t('studio.test_failed_on', {deviceLabel: deviceLabel(ran)});}
  if(row.state==='not_checked'){return LabText.t('studio.installed_not_tested_on', {deviceLabel: deviceLabel(device)});}
  if(row.state==='check_outdated'){return LabText.t('studio.installed_test_out_of_date', {deviceLabel: deviceLabel(device)});}
  return engineStateNames[row.state]||row.state;
}
function utcStamp(value){return String(value||'').replace('T',' ').slice(0,16)+' UTC';}
function elapsedText(seconds){var total=Math.round(seconds||0),minutes=Math.floor(total/60);return minutes?minutes+' min '+(total%60)+' s':total+' s';}
function engineDetailText(row,device,job){
  // A running install leaves this text as it was (the state line and the button say it runs), so the card keeps its height.
  var check=row.check,install=row.installation||{};
  // A model that is not installed says what Install will fetch; the files it lacks and any log are under the card's Details.
  if(row.state==='not_installed'){
    var size=row.install_size;
    if(job&&job.state==='failed'){return LabText.t('studio.the_install_stopped_after_show', {elapsedText: elapsedText(job.seconds)});}
    if(install.installer){return size?LabText.t('studio.install_downloads_about_and_uses', {gigabytes: gigabytes(size.download_bytes), gigabytes2: gigabytes(size.disk_bytes)}):LabText.t('studio.install_shows_what_it_will');}
    if((row.gpu||{}).cpu&&!(row.gpu||{}).choosable){return LabText.t('studio.the_app_cannot_install_it');}
    return LabText.t('studio.the_app_cannot_install_this');
  }
  if(row.state==='conversion_task'){return LabText.t('studio.tested_from_voice_conversion_because');}
  if(row.state==='not_integrated'){return LabText.t('studio.this_build_does_not_offer');}
  if(!check){return LabText.t('studio.press_test_to_load_the');}
  if(row.state==='check_outdated'){return LabText.t('studio.the_last_test_was_made', {utcStamp: utcStamp(check.checked_at)});}
  if(check.status==='passed'){
    var where='';
    // On the CPU though the graphics card was asked for: a CPU build of PyTorch, or (with a GPU build) a card PyTorch could not
    // find when the model loaded, as when a laptop's power-saving mode has switched its NVIDIA card off.
    var fellBack=(row.gpu||{}).build==='cpu'?LabText.t('studio.on_the_cpu_this_model'):LabText.t('studio.on_the_cpu_the_graphics');
    if(check.ran_on&&check.ran_on!==device){where=check.ran_on==='cpu'&&device==='cuda'?fellBack:LabText.t('studio.on', {ran_on: check.ran_on});}
    return LabText.t('studio.tested_s_to_make_s', {utcStamp: utcStamp(check.checked_at), total_s: check.total_s, audio_seconds: check.audio_seconds, where: where});
  }
  return LabText.t('studio.failed', {utcStamp: utcStamp(check.checked_at), reason: check.reason});
}
// What a card keeps under Details, '' for nothing: the files a model lacks, how it is installed, and where a failed install's
// output went. Paths stay here, so the card itself reads as plain words.
function engineMoreText(row,job){
  var install=row.installation||{},parts=[];
  if(row.state==='not_installed'){
    if((install.missing||[]).length){parts.push(LabText.t('studio.missing', {join: install.missing.join('; ')}));}
    parts.push(install.installer?LabText.t('studio.install_adds_it_to_this', {installer: install.installer}):LabText.t('studio.docs_setup_md_model_runtimes'));
  }
  if(job&&job.state==='failed'){parts.push(LabText.t('studio.the_last_install_stopped_with', {exit_code: job.exit_code, log: job.log}));}
  return parts.join(' ');
}
function deviceNoteText(row,job){
  // What the GPU/CPU switch means for this card now; '' when the switch says it all.
  var gpu=row.gpu||{},installed=!!(row.installation||{}).installed;
  if(gpu.cpu){return gpu.choosable?gpu.cpu:[gpu.reason,gpu.cpu].filter(Boolean).join(' ');}    // a model that needs the card
  if(!gpu.choosable){return gpu.reason||'';}
  if(installed&&row.switch==='gpu'&&gpu.build==='cpu'){
    var failed=job&&job.state==='failed'&&job.nvidia?LabText.t('studio.the_last_try_stopped_details'):'';
    return LabText.t('studio.installed_with_the_cpu_build', {failed: failed});
  }
  if(!installed){return row.switch==='gpu'?LabText.t('studio.install_puts_in_its_gpu'):LabText.t('studio.install_puts_in_its_cpu');}
  return '';
}
function ramText(row,ram){
  var need=row.min_free_ram_gb;
  if(typeof need!=='number'){return {text:'',short:false};}
  var text=LabText.t('studio.needs_gb_of_free_ram', {need: need});
  if(ram&&ram.total_gb<need){return {text:text+LabText.t('studio.this_computer_has_only')+ram.total_gb+LabText.t('studio.gb_of_ram_so_it'),short:true};}
  if(ram&&ram.free_gb<need){return {text:text+LabText.t('studio.only')+ram.free_gb+LabText.t('studio.gb_is_free_right_now'),short:true};}
  return {text:text,short:false};
}
var engineRows={};
function markEngineOptions(){
  ['engine','identityEngine','cloneEngine'].forEach(function(id){
    Array.prototype.forEach.call(($(id)||{}).options||[],function(option){
      var row=engineRows[option.value],view=modelCards[option.value];
      if(row&&view&&row.integrated){option.textContent=view.name+(row.state==='not_installed'?LabText.t('studio.not_installed'):'');}
    });
  });
}
function showEngineHint(){
  var hint=$('engineHint'),row=engineRows[$('engine').value];
  if(!hint){return;}
  hint.hidden=!(row&&row.state==='not_installed');
  hint.textContent='';
  if(hint.hidden){return;}
  var text=document.createElement('span'),link=document.createElement('a');
  text.textContent=LabText.t('studio.this_model_is_not_installed');
  link.href='#models';link.textContent=(row.installation||{}).installer?LabText.t('studio.install_it_in_models_features'):LabText.t('studio.see_what_it_needs_in');
  hint.appendChild(text);hint.appendChild(link);
}
// The first-run note, on a Lab where no model can speak yet: it offers the first model the app can install, with its size, and
// starts the same install as that model's card. When an install it started finishes, the studio is made ready to speak:
// the model chosen, a sentence in the text box (the page's own, unless something else was typed) and the focus on Generate.
var firstRun={engine:null,action:null,ready:null},firstRunWired=false;
function gigabytes(bytes){return (bytes/1e9).toFixed(1)+' GB';}
function installSizeText(row){
  var size=row.install_size;
  return size?LabText.t('studio.it_downloads_about_and_uses', {gigabytes: gigabytes(size.download_bytes), gigabytes2: gigabytes(size.disk_bytes)}):'';
}
function wireFirstRun(){
  if(firstRunWired){return;}
  firstRunWired=true;
  $('firstRunButton').addEventListener('click',function(){
    var action=firstRun.action;if(!action){return;}
    if(action.kind==='progress'){return showProgress(action.engine);}
    firstRun.engine=action.engine;
    return planInstall(action.engine,(modelCards[action.engine]||{}).installGpu);
  });
  $('go').addEventListener('click',function(){if(firstRun.ready){firstRun.ready=null;$('firstRun').hidden=true;}});
}
function placeFirstRun(workspace){
  if(workspace==='home'||workspace==='studio'){
    var target=$(workspace+'FirstRun'), note=$('firstRun');
    if(note.parentElement!==target)target.appendChild(note);
  }
}
function studioReady(job){
  // An install the note started has finished: close its window, which would keep the focus, and set the studio up to speak.
  firstRun.engine=null;firstRun.ready=job.name;
  if(installDialog.engine===job.engine){closeInstallDialog();}
  if(typeof refreshSample==='function'){refreshSample();}
  var engine=$('engine'),text=$('text');
  if(engine.value!==job.engine){engine.value=job.engine;if(typeof refresh==='function'){refresh();}}
  if(!String(text.value||'').trim()){text.value=text.defaultValue||'';}
  if(!$('home').hidden){
    location.hash='#studio';
    window.addEventListener('hashchange',function(){$('go').focus();},{once:true});
  }else if(!$('studio').hidden){$('go').focus();}
}
function renderFirstRun(status){
  var rows=status.engines||[],job=status.install||null,box=$('firstRun'),button=$('firstRunButton');
  var installed=rows.some(function(row){return row.installation&&row.installation.installed&&!row.task_only;});
  var running=!!job&&job.state==='running';
  var offer=rows.filter(function(row){return row.state==='not_installed'&&(row.installation||{}).installer;})[0];
  if(installed&&firstRun.engine&&job&&job.engine===firstRun.engine&&job.state==='finished'){studioReady(job);}
  firstRun.action=null;$('firstRunSample').hidden=true;
  if(firstRun.ready&&installed){
    box.hidden=false;button.hidden=true;$('firstRunModels').hidden=true;
    $('firstRunTitle').textContent=LabText.t('studio.is_installed', {ready: firstRun.ready});
    $('firstRunText').textContent=LabText.t('studio.press_generate_to_hear_it');
    if(typeof renderSampleNotes==='function'){renderSampleNotes();}
    return;
  }
  firstRun.ready=null;
  box.hidden=installed||!(running||offer);
  if(box.hidden){return;}
  $('firstRunModels').hidden=false;button.hidden=false;
  if(running){
    firstRun.action={kind:'progress',engine:job.engine};
    $('firstRunTitle').textContent=LabText.t('studio.installing_2', {name: job.name});
    $('firstRunText').textContent=LabText.t('studio.running_for_nothing_can_speak', {elapsedText: elapsedText(job.seconds)});
    button.textContent=LabText.t('studio.show_progress');button.disabled=false;
    return;
  }
  firstRun.action={kind:'install',engine:offer.id};
  var failed=job&&job.state==='failed'&&job.engine===offer.id;
  $('firstRunTitle').textContent=failed?LabText.t('studio.did_not_install', {name: offer.name}):LabText.t('studio.no_voice_model_is_installed');
  $('firstRunText').textContent=failed
    ?LabText.t('studio.the_install_stopped_exit_code', {exit_code: job.exit_code, elapsedText: elapsedText(job.seconds), log: job.log})
    :LabText.t('studio.install_to_hear_the_lab', {name: offer.name, installSizeText: installSizeText(offer)});
  button.textContent=LabText.t('studio.install', {name: offer.name});button.disabled=!!status.busy;
}
function placeCards(status){
  var main=$('modelOverview'),more=$('modelMore');
  (status.engines||[]).forEach(function(row){
    var view=modelCards[row.id];if(!view){return;}
    var manual=row.state==='not_installed'&&!(row.installation||{}).installer,target=manual?more:main;
    if(view.card.parentNode!==target){target.appendChild(view.card);}
  });
  applyModelFilter();
}
function cardNode(tag,className,text){var node=document.createElement(tag);if(className){node.className=className;}if(text!==undefined){node.textContent=text;}return node;}
// A card's facts, in plain words from GET /api/catalog (model_facts.py): where it runs, its voices, acting directions, its
// languages and what its audio may be used for. The language screen's results and whether the model is integrated are under
// Details.
function modelFactNodes(facts){
  if(!facts){return [];}
  return [facts.runs.text,facts.voices.text,facts.direction.text,facts.languages.summary,facts.output.text].map(function(text){return cardNode('li','',text);});
}
function integrationText(integration){
  if(!integration.service_api){return LabText.t('studio.not_integrated_this_build_has');}
  return integration.task_only?LabText.t('studio.integrated_as_a_conversion_task'):LabText.t('studio.integrated_this_build_has_its');
}
function renderModelOverview(result){
  var container=$('modelOverview');container.textContent='';modelCards={};
  (result.engines||[]).forEach(function(entry){
    var facts=entry.facts||null,first=facts&&facts.first_model?cardNode('p','first-model',LabText.t('studio.good_first_model')):null;
    var card=cardNode('article','model-card'),title=cardNode('h3','',entry.name),state=cardNode('p','engine-state'),details=cardNode('p','meta'),features=cardNode('ul','model-facts'),ram=cardNode('p','meta');
    var more=cardNode('details','model-more'),moreText=cardNode('p','meta');
    more.appendChild(cardNode('summary','',LabText.t('studio.details')));more.appendChild(cardNode('p','meta',integrationText(entry.integration||{})));
    if(facts&&facts.languages.text){more.appendChild(cardNode('p','meta',facts.languages.text));}
    more.appendChild(moreText);moreText.hidden=true;
    var devices=cardNode('div','device-switch'),choices=cardNode('span','device-choices'),note=cardNode('p','meta device-note'),actions=cardNode('div','model-actions'),button=cardNode('button','',LabText.t('studio.test')),extra=cardNode('button'),uninstall=cardNode('button','',LabText.t('studio.uninstall'));
    var radios={};
    modelFactNodes(facts).forEach(function(node){features.appendChild(node);});features.hidden=!facts;
    devices.setAttribute('role','radiogroup');devices.setAttribute('aria-label',LabText.t('studio.runs_on'));devices.hidden=true;
    devices.appendChild(cardNode('span','meta',LabText.t('studio.runs_on')));devices.appendChild(choices);
    ['gpu','cpu'].forEach(function(value){
      var option=cardNode('label'),input=cardNode('input');
      input.type='radio';input.name='device-'+entry.id;input.value=value;
      input.addEventListener('change',function(){if(input.checked){return chooseDevice(entry.id,value);}});
      option.appendChild(input);option.appendChild(cardNode('span','',value==='gpu'?'GPU':'CPU'));
      choices.appendChild(option);radios[value]=input;
    });
    note.hidden=true;
    button.type='button';extra.type='button';extra.hidden=true;
    uninstall.type='button';uninstall.hidden=true;uninstall.setAttribute('aria-label',LabText.t('studio.uninstall_2', {name: entry.name}));
    uninstall.addEventListener('click',function(){return planUninstall(entry.id);});
    button.addEventListener('click',function(){var view=modelCards[entry.id];return view.action==='install'?planInstall(entry.id,view.installGpu):view.action==='progress'?showProgress(entry.id):checkEngine(entry.id);});
    extra.addEventListener('click',function(){return modelCards[entry.id].extra==='gpu'?planInstall(entry.id,true):showProgress(entry.id);});
    actions.appendChild(button);actions.appendChild(extra);actions.appendChild(uninstall);
    [title,first,state,details,more,features,ram,devices,note,actions].forEach(function(node){if(node){card.appendChild(node);}});
    container.appendChild(card);
    modelCards[entry.id]={name:entry.name,facts:facts,card:card,state:state,details:details,more:more,moreText:moreText,ram:ram,devices:devices,radios:radios,note:note,button:button,extraButton:extra,uninstallButton:uninstall,action:'test',extra:null,installGpu:false,pendingDevice:false,deviceError:''};
  });
  renderModelFilter(result);
  applyModelFilter();
}
// The three questions above the cards (roadmap issue 3): no graphics card, cloning a voice, and a language. A card fits by its
// facts alone, so the page holds no fact about any model.
var modelFilter={cpu:false,clone:false,language:''},modelFilterWired=false;
function modelFits(facts,filter){
  if(!filter.cpu&&!filter.clone&&!filter.language){return true;}
  if(!facts){return false;}
  if(filter.cpu&&!facts.runs.cpu){return false;}
  if(filter.clone&&!facts.voices.clones){return false;}
  return !filter.language||(facts.languages.filter||[]).indexOf(filter.language)>=0;
}
function wordList(items){return items.length<2?items.join(''):items.slice(0,-1).join(', ')+LabText.t('studio.and')+items[items.length-1];}
function filterLanguages(result){
  var seen={};
  (result.engines||[]).forEach(function(entry){((entry.facts||{}).languages||{filter:[]}).filter.forEach(function(name){seen[name]=true;});});
  return Object.keys(seen).sort();
}
function chooseFilterLanguage(value){
  modelFilter.language=value;
  Array.prototype.forEach.call($('modelFilterLanguages').children,function(label){label.className=label.children[0].value===value?'on':'';});
  applyModelFilter();
}
function renderModelFilter(result){
  var box=$('modelFilterLanguages'),languages=filterLanguages(result);
  if(languages.indexOf(modelFilter.language)<0){modelFilter.language='';}
  box.textContent='';
  [''].concat(languages).forEach(function(value){
    var label=cardNode('label'),input=cardNode('input');
    input.type='radio';input.name='modelLanguage';input.value=value;input.checked=value===modelFilter.language;
    input.addEventListener('change',function(){if(input.checked){chooseFilterLanguage(value);}});
    label.appendChild(input);label.appendChild(cardNode('span','',value||LabText.t('studio.any')));label.className=input.checked?'on':'';
    box.appendChild(label);
  });
  // With no facts to answer from (a catalog without them), no question is asked, so no card is hidden.
  $('modelFilter').hidden=!languages.length;
  if(!languages.length){modelFilter={cpu:false,clone:false,language:''};$('modelFilterCpu').checked=false;$('modelFilterClone').checked=false;}
  if(modelFilterWired){return;}
  modelFilterWired=true;
  $('modelFilterCpu').addEventListener('change',function(){modelFilter.cpu=!!$('modelFilterCpu').checked;applyModelFilter();});
  $('modelFilterClone').addEventListener('change',function(){modelFilter.clone=!!$('modelFilterClone').checked;applyModelFilter();});
}
function applyModelFilter(){
  var more=$('modelMore'),asked=modelFilter.cpu||modelFilter.clone||!!modelFilter.language,fits=[],apart=0;
  Object.keys(modelCards).forEach(function(id){
    var view=modelCards[id],fit=modelFits(view.facts,modelFilter);
    view.card.hidden=!fit;
    if(fit){fits.push(view.name);if(view.card.parentNode===more){apart+=1;}}
  });
  $('modelsMoreBox').hidden=!apart;
  $('modelsMoreSummary').textContent=LabText.t('studio.manual_setup', {value: apart+(apart===1?' more model needs':' more models need')});
  if(asked&&apart){$('modelsMoreBox').open=true;}
  var line=$('modelFilterMatch');
  line.hidden=!asked;
  line.textContent=!asked?'':!fits.length?LabText.t('studio.no_model_fits_all_of')
    :(fits.length===1?LabText.t('studio.1_model_fits'):fits.length+LabText.t('studio.models_fit'))+wordList(fits)+'.';
}
var installRunning=false,installTimer=null;
function refreshEngineStatus(){
  return fetchJson('/api/engines/status').then(function(status){renderEngineStatus(status);}).catch(function(e){$('err').textContent=LabText.t('studio.could_not_read_the_model', {message: e.message});watchInstall(installRunning);});
}
function watchInstall(running){
  if(!running||installTimer){return;}
  installTimer=setTimeout(function(){installTimer=null;refreshEngineStatus();},1500);
}
function showInstallLog(log,lines){
  if(log.hidden){log.textContent='';return;}
  var follow=log.scrollTop+log.clientHeight>=log.scrollHeight-4;
  log.textContent=lines.join('\n');
  if(follow){log.scrollTop=log.scrollHeight;}
}
function renderDeviceSwitch(view,row,status,job){
  var gpu=row.gpu,shown=!!gpu&&status.device==='cuda';
  view.devices.hidden=!shown;
  view.installGpu=!!(gpu&&gpu.install);
  if(!shown){var only=!!(gpu&&gpu.cpu);view.note.hidden=!only;view.note.textContent=only?gpu.cpu:'';view.note.className='meta device-note';return;}
  ['gpu','cpu'].forEach(function(value){
    var input=view.radios[value];
    if(!view.pendingDevice){input.checked=row.switch===value;}
    input.disabled=!!status.busy||view.pendingDevice||(value==='gpu'&&!gpu.choosable)||(value==='cpu'&&!!gpu.cpu);
    input.parentNode.className=(input.checked?'on':'')+(input.disabled?' off':'');
  });
  var text=view.deviceError||deviceNoteText(row,job);
  view.note.textContent=text;view.note.hidden=!text;view.note.className='meta device-note'+(view.deviceError?' warn':'');
}
function renderEngineStatus(status){
  var job=status.install||null;
  installRunning=!!job&&job.state==='running';
  (status.engines||[]).forEach(function(row){
    var view=modelCards[row.id];if(!view){return;}
    var mine=job&&job.engine===row.id?job:null,running=!!mine&&mine.state==='running',missing=row.state==='not_installed',install=row.installation||{},need=ramText(row,status.ram);
    // The device the card names: the CPU when its switch says so, which is also true of Phonon and Drama 3, whose workers keep
    // the service's device and choose the CPU themselves.
    var device=row.switch==='cpu'?'cpu':(row.device||status.device);
    view.action=running?'progress':missing?(install.installer?'install':'none'):(row.state==='not_integrated'||row.state==='conversion_task'?'none':'test');
    view.state.textContent=engineStateText(row,device,mine);
    view.details.textContent=engineDetailText(row,device,mine);
    var more=engineMoreText(row,mine);
    view.moreText.textContent=more;view.moreText.hidden=!more;
    view.ram.textContent=need.text;view.ram.className=need.short?'meta warn':'meta';
    view.button.textContent=view.action==='install'?LabText.t('studio.install_2'):view.action==='progress'?LabText.t('studio.show_progress'):(row.check?LabText.t('studio.test_again'):LabText.t('studio.test'));
    view.button.hidden=view.action==='none';
    view.button.disabled=view.action!=='progress'&&(!!status.busy||view.action==='none');
    renderDeviceSwitch(view,row,status,mine);
    var gpuBuild=!running&&!missing&&!!row.gpu&&row.switch==='gpu'&&row.gpu.build==='cpu'&&row.gpu.install;
    view.extra=gpuBuild?'gpu':(mine&&mine.state==='failed'&&missing?'output':null);
    view.extraButton.textContent=view.extra==='gpu'?LabText.t('studio.install_gpu_build'):LabText.t('studio.show_output');
    view.extraButton.hidden=!view.extra;
    view.extraButton.disabled=view.extra==='gpu'&&!!status.busy;
    view.uninstallButton.hidden=missing||running||row.state==='not_integrated';
    view.uninstallButton.disabled=!!status.busy||installRunning;
  });
  $('modelsRam').textContent=status.ram?LabText.t('studio.this_computer_has_gb_of', {total_gb: status.ram.total_gb, free_gb: status.ram.free_gb}):'';
  $('modelsDevice').textContent=status.device==='cuda'?LabText.t('studio.each_card_s_switch_says'):LabText.t('studio.every_model_runs_on_the');
  engineRows={};(status.engines||[]).forEach(function(row){engineRows[row.id]=row;});
  placeCards(status);markEngineOptions();showEngineHint();renderFirstRun(status);renderInstallDialog(job);
  if(typeof VoiceLabWorkflows!=='undefined'){VoiceLabWorkflows.refreshCloneEngine();}
  renderSpeedRows(status);
  watchInstall(installRunning);
}
function chooseDevice(engineId,value){
  var view=modelCards[engineId];if(!view){return;}
  view.pendingDevice=true;view.deviceError='';
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/device',{device:value}).catch(function(e){view.deviceError=LabText.t('studio.could_not_switch_it', {message: e.message});}).then(function(){view.pendingDevice=false;return refreshEngineStatus();});
}
function checkEngine(engineId){
  var view=modelCards[engineId];if(!view){return;}
  Object.keys(modelCards).forEach(function(key){modelCards[key].button.disabled=true;});
  view.button.textContent=LabText.t('studio.testing');
  view.details.textContent=LabText.t('studio.testing_now_loading_the_model');
  $('err').textContent='';
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/check',{}).catch(function(e){$('err').textContent=LabText.t('studio.test_did_not_finish', {message: e.message});}).then(refreshEngineStatus);
}
function addNote(parent,text,className){var note=document.createElement('p');note.className=className;note.textContent=text;parent.appendChild(note);}
// The install window: the plan before an install, then its progress. It sits over the page, so the cards never move for it.
var installDialog={engine:null,nvidia:false,mode:null},installDialogWired=false;
function dialogParts(){return {box:$('installDialog'),title:$('installTitle'),state:$('installState'),plan:$('installPlan'),log:$('installLog'),go:$('installGo'),close:$('installClose')};}
function wireInstallDialog(){
  if(installDialogWired){return;}
  installDialogWired=true;
  var parts=dialogParts();
  parts.go.addEventListener('click',function(){startInstall();});
  parts.close.addEventListener('click',function(){closeInstallDialog();});
  parts.box.addEventListener('close',function(){installDialog={engine:null,nvidia:false,mode:null};});
  var gone=uninstallParts();
  gone.go.addEventListener('click',function(){return startUninstall();});
  gone.close.addEventListener('click',function(){closeUninstall();});
  gone.box.addEventListener('cancel',function(event){if(uninstallState.busy){event.preventDefault();}});
}
function openInstallDialog(engineId,nvidia,mode,title){
  var parts=dialogParts();
  installDialog={engine:engineId,nvidia:!!nvidia,mode:mode};
  parts.title.textContent=title;parts.state.textContent='';parts.plan.textContent='';parts.log.textContent='';
  parts.plan.hidden=mode!=='plan';parts.log.hidden=mode!=='progress';parts.go.hidden=true;parts.go.disabled=false;
  parts.close.textContent=mode==='plan'?LabText.t('studio.cancel'):LabText.t('studio.hide');
  if(!parts.box.open&&parts.box.showModal){parts.box.showModal();}
  return parts;
}
function closeInstallDialog(){
  var box=$('installDialog');installDialog={engine:null,nvidia:false,mode:null};
  if(box.open&&box.close){box.close();}
}
function planInstall(engineId,nvidia){
  var view=modelCards[engineId];if(!view){return;}
  var parts=openInstallDialog(engineId,nvidia,'plan',(nvidia?LabText.t('studio.install_the_gpu_build_of'):LabText.t('studio.install_3'))+view.name);
  parts.state.textContent=LabText.t('studio.reading_what_the_installer_will');
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/install',{dry_run:true,nvidia:!!nvidia}).then(function(result){
    if(installDialog.engine!==engineId||installDialog.mode!=='plan'){return;}
    var plan=result.plan||{};
    parts.state.textContent=plan.ok?LabText.t('studio.this_is_what_it_will'):LabText.t('studio.this_computer_cannot_take_the');
    (plan.paragraphs||[]).forEach(function(text){addNote(parts.plan,text,'meta');});
    (plan.problems||[]).forEach(function(text){addNote(parts.plan,text,'meta warn');});
    parts.go.hidden=!plan.ok;parts.close.textContent=plan.ok?LabText.t('studio.cancel'):LabText.t('studio.close');
  }).catch(function(e){if(installDialog.engine===engineId){parts.state.textContent=LabText.t('studio.could_not_read_the_install', {message: e.message});parts.close.textContent=LabText.t('studio.close');}}).then(refreshEngineStatus);
}
function startInstall(){
  var engineId=installDialog.engine,nvidia=installDialog.nvidia,parts=dialogParts();
  if(!engineId||installDialog.mode!=='plan'){return;}
  parts.go.disabled=true;
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/install',{nvidia:nvidia}).then(function(){
    var view=modelCards[engineId];
    openInstallDialog(engineId,nvidia,'progress',(nvidia?LabText.t('studio.installing_the_gpu_build_of'):LabText.t('studio.installing_3'))+(view?view.name:engineId)).state.textContent=LabText.t('studio.starting');
  }).catch(function(e){parts.state.textContent=LabText.t('studio.the_install_did_not_start', {message: e.message});parts.go.hidden=true;parts.close.textContent=LabText.t('studio.close');}).then(function(){parts.go.disabled=false;return refreshEngineStatus();});
}
function showProgress(engineId){
  var view=modelCards[engineId];if(!view){return;}
  openInstallDialog(engineId,false,'progress',LabText.t('studio.installing_4', {name: view.name})).state.textContent=LabText.t('studio.reading_its_progress');
  return refreshEngineStatus();
}
// Uninstall: what it would delete first, then the deleting, in a window of its own.
var uninstallState={engine:null,busy:false};
function uninstallParts(){return {box:$('uninstallDialog'),title:$('uninstallTitle'),state:$('uninstallState'),plan:$('uninstallPlan'),go:$('uninstallGo'),close:$('uninstallClose')};}
function planUninstall(engineId){
  var view=modelCards[engineId];if(!view){return;}
  var parts=uninstallParts();
  uninstallState={engine:engineId,busy:false};
  parts.title.textContent=LabText.t('studio.uninstall_2', {name: view.name});parts.state.textContent=LabText.t('studio.reading_what_it_would_delete');parts.plan.textContent='';
  parts.go.hidden=true;parts.go.disabled=false;parts.close.disabled=false;parts.close.textContent=LabText.t('studio.cancel');
  if(!parts.box.open&&parts.box.showModal){parts.box.showModal();}
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/uninstall',{dry_run:true}).then(function(result){
    if(uninstallState.engine!==engineId){return;}
    var plan=result.plan||{};
    parts.state.textContent=plan.ok?LabText.t('studio.this_is_what_it_would'):LabText.t('studio.there_is_nothing_to_uninstall');
    (plan.paragraphs||[]).forEach(function(text){addNote(parts.plan,text,'meta');});
    (plan.problems||[]).forEach(function(text){addNote(parts.plan,text,'meta warn');});
    parts.go.hidden=!plan.ok;parts.close.textContent=plan.ok?LabText.t('studio.cancel'):LabText.t('studio.close');
  }).catch(function(e){if(uninstallState.engine===engineId){parts.state.textContent=LabText.t('studio.could_not_read_what_it', {message: e.message});parts.close.textContent=LabText.t('studio.close');}});
}
function startUninstall(){
  var engineId=uninstallState.engine,parts=uninstallParts(),view=modelCards[engineId],name=view?view.name:engineId;
  if(!engineId||uninstallState.busy){return;}
  uninstallState.busy=true;parts.go.disabled=true;parts.close.disabled=true;
  parts.state.textContent=LabText.t('studio.uninstalling_deleting_its_files_can', {name: name});
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/uninstall',{}).then(function(result){
    var done=result.uninstall||{},failed=done.failed||[],removed=done.removed||[],freed=done.freed?LabText.t('studio.freed', {freed_text: done.freed_text}):removed.length?LabText.t('studio.removed', {join: removed.join(', ')}):'';
    parts.plan.textContent='';parts.go.hidden=true;
    if(failed.length){
      parts.title.textContent=LabText.t('studio.was_not_fully_uninstalled', {name: name});
      parts.state.textContent=LabText.t('studio.these_could_not_be_deleted', {freed: freed, join: failed.map(function(item){return item.path+' ('+item.reason+')';}).join('; ')});
    }else{
      parts.title.textContent=LabText.t('studio.is_uninstalled', {name: name});
      parts.state.textContent=LabText.t('studio.install_on_its_card_puts', {freed: freed});
    }
  }).catch(function(e){parts.state.textContent=LabText.t('studio.was_not_uninstalled', {name: name, message: e.message});}).then(function(){
    uninstallState.busy=false;parts.go.disabled=false;parts.close.disabled=false;parts.close.textContent=LabText.t('studio.close');
    return refreshEngineStatus();
  });
}
function closeUninstall(){
  var box=$('uninstallDialog');if(uninstallState.busy){return;}
  uninstallState={engine:null,busy:false};
  if(box.open&&box.close){box.close();}
}
function renderInstallDialog(job){
  if(installDialog.mode!=='progress'||!job||job.engine!==installDialog.engine){return;}
  var parts=dialogParts(),name=job.name+(job.nvidia?LabText.t('studio.gpu_build'):'');
  if(job.state==='running'){
    parts.title.textContent=LabText.t('studio.installing_4', {name: name});
    parts.state.textContent=LabText.t('studio.running_for_downloads_and_packages', {elapsedText: elapsedText(job.seconds)});
    parts.close.textContent=LabText.t('studio.hide');
  }else if(job.state==='finished'){
    parts.title.textContent=LabText.t('studio.is_installed_2', {name: name});
    parts.state.textContent=LabText.t('studio.it_finished_in_press_test', {elapsedText: elapsedText(job.seconds)});
    parts.close.textContent=LabText.t('studio.close');
  }else{
    parts.title.textContent=LabText.t('studio.did_not_install', {name: name});
    parts.state.textContent=LabText.t('studio.the_install_stopped_exit_code_2', {exit_code: job.exit_code, elapsedText: elapsedText(job.seconds), log: job.log});
    parts.close.textContent=LabText.t('studio.close');
  }
  showInstallLog(parts.log,job.lines||[]);
}
function renderSpeedRows(status){
  var body=$('speedRows');body.textContent='';
  var rows=status.speed||[];
  if(!rows.length){
    var empty=document.createElement('tr'),cell=document.createElement('td');
    cell.colSpan=5;cell.textContent=LabText.t('studio.no_clips_measured_yet_run');
    empty.appendChild(cell);body.appendChild(empty);return;
  }
  rows.forEach(function(row){
    var tr=document.createElement('tr');
    var name=modelCards[row.engine]?modelCards[row.engine].name:row.engine;
    var cells=[name,deviceLabel(row.device),String(row.clips),row.median_real_time_factor==null?'—':row.median_real_time_factor.toFixed(2)+' s',row.last_total_s.toFixed(2)+LabText.t('studio.s_for')+row.last_audio_seconds.toFixed(2)+' s of audio'];
    cells.forEach(function(text){var td=document.createElement('td');td.textContent=text;tr.appendChild(td);});
    body.appendChild(tr);
  });
}
