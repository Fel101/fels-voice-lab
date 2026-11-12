// Voice Lab studio: model catalog, engine status and the saved-clip history.
var $ = function (id) { return document.getElementById(id); };
var hints = {
  kokoro: 'Kokoro has fixed voices and speed control. It does not accept reference audio or perform vocal events.',
  'phonon-31m-en': 'Phonon speaks English with one of 37 fixed preset voices, 12 of them characters. It does not accept reference audio, design a voice or take a direction.',
  turbo: 'Chatterbox Turbo clones a reference and supports tags such as [laugh], [chuckle] and [cough].',
  orig: 'Chatterbox supports reference cloning plus exaggeration and CFG controls. It ignores event tags.',
  'breeze-tts-2': 'Breeze supports voice design, reference cloning, text direction and parenthesized vocal events. Outputs are full WAV files.',
  'qwen3-tts-1.7b-voice-design': 'Qwen VoiceDesign creates a reusable voice from a spoken sample and natural-language voice description.',
  'qwen3-tts-1.7b-base': 'Qwen Base clones a local reference and transcript. Optional Base acting instruction is experimental.',
  'qwen3-tts-0.6b-base': 'Qwen Base clones a local reference and transcript. Optional Base acting instruction is experimental.',
  'qwen3-tts-1.7b-custom-voice': 'Qwen CustomVoice uses one of the actual speaker presets reported by the loaded checkpoint, with optional delivery instruction.',
  'voxcpm2': 'VoxCPM2 supports voice design, reference cloning, and transcript-conditioned continuation. Clone direction is experimental; output is native 48 kHz.'
};
var descriptions = {
  warm: 'A warm, soft-spoken young woman with a gentle, affectionate tone.',
  playful: 'A playful, energetic young woman with a bright, teasing tone and a hint of a laugh in her voice.',
  low: 'A calm, low-pitched woman with a slow, unhurried delivery.',
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
  [['speed','speedV',2],['exag','exagV',2],['cfg','cfgV',2],['cfgScale','cfgScaleV',1],['maxTokens','maxTokensV',0],
   ['temperature','temperatureV',2],['topK','topKV',0],['topP','topPV',2],['repetitionPenalty','repetitionPenaltyV',2],
   ['depthTemperature','depthTemperatureV',2],['depthTopK','depthTopKV',0],['depthTopP','depthTopPV',2],
   ['qwenMaxTokens','qwenMaxTokensV',0],['qwenTemperature','qwenTemperatureV',2],['qwenTopK','qwenTopKV',0],
   ['qwenTopP','qwenTopPV',2],['qwenRepetition','qwenRepetitionV',2],
   ['phononTemperature','phononTemperatureV',2],['phononPace','phononPaceV',1]].forEach(function (x) {
    $(x[1]).textContent = Number($(x[0]).value).toFixed(x[2]);
  });
}
function fetchJson(url, body) {
  return fetch(url, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})
    .then(function (r) { return r.json().then(function (j) { if (!r.ok) { throw new Error(j.error || r.status); } return j; }); });
}
function loadReferences(choosePath) {
  return fetchJson('/api/references').then(function (result) {
    referenceRows = result.references || [];
    VoiceLabWorkflows.render(referenceRows, choosePath);
  }).catch(function (e) { $('err').textContent = 'Could not list local references: ' + e.message; });
}
function unscreenedLanguages(engine) { return performanceUnscreenedByEngine[engine] || []; }
function languageOptionLabel(engine, language) { return unscreenedLanguages(engine).indexOf(language) >= 0 ? language + ' (unscreened)' : language; }
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
    if (!info.supported_speakers.length) { option($('qwenSpeaker'), '', 'Loaded model reported no speakers', true); }
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
      : 'No Phonon voices are installed. Press Install on its card in Models & features.';
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
  return new Promise(function (resolve, reject) {
    if (!window.indexedDB) { reject(new Error('This browser does not provide IndexedDB.')); return; }
    var request = window.indexedDB.open('voice-lab-studio', 1);
    request.onupgradeneeded = function () {
      var db = request.result;
      if (!db.objectStoreNames.contains('runs')) { db.createObjectStore('runs', {keyPath:'id', autoIncrement:true}); }
    };
    request.onsuccess = function () { resolve(request.result); };
    request.onerror = function () { reject(request.error || new Error('Could not open local generation history.')); };
  });
}
function persistHistory(blob, body, meta) {
  return openHistoryDb().then(function (db) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction('runs', 'readwrite'), store = tx.objectStore('runs');
      store.add({body:body, meta:meta, audio:blob, createdAt:Date.now()});
      tx.oncomplete = function () { db.close(); resolve(); };
      tx.onerror = function () { db.close(); reject(tx.error || new Error('Could not save the generated clip locally.')); };
      tx.onabort = function () { db.close(); reject(tx.error || new Error('Local generation history write was cancelled.')); };
    });
  }).then(function () {
    return openHistoryDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction('runs', 'readwrite'), store = tx.objectStore('runs'), request = store.getAll();
        request.onsuccess = function () {
          var rows = request.result.sort(function (a,b) { return a.id - b.id; });
          rows.slice(0, Math.max(0, rows.length - 20)).forEach(function (row) { store.delete(row.id); });
        };
        tx.oncomplete = function () { db.close(); resolve(); };
        tx.onerror = function () { db.close(); reject(tx.error || new Error('Could not trim local generation history.')); };
        tx.onabort = function () { db.close(); reject(tx.error || new Error('Local generation history update was cancelled.')); };
      });
    });
  });
}
function restoreHistory() {
  return openHistoryDb().then(function (db) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction('runs', 'readonly'), request = tx.objectStore('runs').getAll();
      request.onsuccess = function () {
        var rows = request.result.sort(function (a,b) { return a.id - b.id; });
        rows.forEach(function (row) { makeHistory(row.audio, row.body, row.meta); });
      };
      tx.oncomplete = function () { db.close(); resolve(); };
      tx.onerror = function () { db.close(); reject(tx.error || new Error('Could not restore local generation history.')); };
    });
  }).catch(function (error) { $('err').textContent = 'Local generation history is unavailable: ' + error.message; });
}
function fillCatalog() {
  return fetchJson('/api/catalog').then(function (result) {
    catalog = result;
    $('engine').textContent = '';
    (result.engines || []).forEach(function (entry) {
      var ready = entry.integration && entry.integration.service_api && !entry.integration.task_only;
      if (!(entry.integration && entry.integration.task_only)) {
        option($('engine'), entry.id, entry.name + (ready ? '' : ' (not integrated yet)'), !ready);
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
var engineStateNames={not_integrated:'Not integrated',not_installed:'Not installed',conversion_task:'Conversion task'};
function deviceLabel(device){return device==='cpu'?'CPU':device==='cuda'?'GPU':String(device||'');}
function engineStateText(row,device,job){
  // While an install runs the card's text stays put, so the cards around it do not move: its progress is in the install window.
  if(job&&job.state==='running'){return job.nvidia?'Installing the GPU build…':'Installing…';}
  // A model that could not use the graphics card when it loaded ran on the CPU: say so rather than the service's device.
  var ran=row.check&&row.check.ran_on==='cpu'?'cpu':device;
  if(row.state==='usable'){return 'Speaks now on '+deviceLabel(ran);}
  if(row.state==='check_failed'){return 'Test failed on '+deviceLabel(ran);}
  if(row.state==='not_checked'){return 'Installed, not tested on '+deviceLabel(device);}
  if(row.state==='check_outdated'){return 'Installed, test out of date on '+deviceLabel(device);}
  return engineStateNames[row.state]||row.state;
}
function utcStamp(value){return String(value||'').replace('T',' ').slice(0,16)+' UTC';}
function elapsedText(seconds){var total=Math.round(seconds||0),minutes=Math.floor(total/60);return minutes?minutes+' min '+(total%60)+' s':total+' s';}
function engineDetailText(row,device,job){
  // A running install leaves this text as it was (the state line and the button say it runs), so the card keeps its height.
  var check=row.check,install=row.installation||{};
  if(row.state==='not_installed'){
    var missing='Missing: '+(install.missing||[]).join('; ')+'.';
    if(job&&job.state==='failed'){return missing+' The install stopped (exit code '+job.exit_code+') after '+elapsedText(job.seconds)+'. Show output has the end of it, and all of it is in '+job.log+'. Press Install to try again: it continues where it stopped.';}
    if(install.installer){return missing+' Install adds it to this folder, as does '+install.installer+' in the lab folder; docs/SETUP.md says what it installs.';}
    if((row.gpu||{}).cpu&&!(row.gpu||{}).choosable){return missing+' The app cannot install it on this computer, for the reason below; docs/SETUP.md (Model runtimes) says what it needs.';}
    return 'The app cannot install this model yet: docs/SETUP.md (Model runtimes) says what it needs. '+missing;
  }
  if(row.state==='conversion_task'){return 'Tested from Voice conversion, because it needs source audio.';}
  if(row.state==='not_integrated'){return 'This build does not expose the engine.';}
  if(!check){return 'Press Test to load the model and speak one short sentence on this device.';}
  if(row.state==='check_outdated'){return 'The last test ('+utcStamp(check.checked_at)+') was made before this model or its packages changed, or before tests recorded that. Press Test again to confirm it still speaks.';}
  if(check.status==='passed'){
    var where='';
    // On the CPU though the graphics card was asked for: a CPU build of PyTorch, or (with a GPU build) a card PyTorch could not
    // find when the model loaded, as when a laptop's power-saving mode has switched its NVIDIA card off.
    var fellBack=(row.gpu||{}).build==='cpu'?', on the CPU: this model has the CPU build of PyTorch':', on the CPU: the graphics card was not available when it loaded (on a laptop, a power-saving mode can switch the card off)';
    if(check.ran_on&&check.ran_on!==device){where=check.ran_on==='cpu'&&device==='cuda'?fellBack:', on '+check.ran_on;}
    return 'Tested '+utcStamp(check.checked_at)+': '+check.total_s+' s to make '+check.audio_seconds+' s of audio'+where+'.';
  }
  return 'Failed '+utcStamp(check.checked_at)+': '+check.reason;
}
function deviceNoteText(row,job){
  // What the GPU/CPU switch means for this card now; '' when the switch says it all.
  var gpu=row.gpu||{},installed=!!(row.installation||{}).installed;
  if(gpu.cpu){return gpu.choosable?gpu.cpu:[gpu.reason,gpu.cpu].filter(Boolean).join(' ');}    // a model that needs the card
  if(!gpu.choosable){return gpu.reason||'';}
  if(installed&&row.switch==='gpu'&&gpu.build==='cpu'){
    var failed=job&&job.state==='failed'&&job.nvidia?' The last try stopped (exit code '+job.exit_code+'); all of its output is in '+job.log+'.':'';
    return 'Installed with the CPU build of PyTorch, so it runs on the CPU until its GPU build is installed.'+failed;
  }
  if(!installed){return row.switch==='gpu'?'Install puts in its GPU build, for the graphics card.':'Install puts in its CPU build.';}
  return '';
}
function ramText(row,ram){
  var need=row.min_free_ram_gb;
  if(typeof need!=='number'){return {text:'',short:false};}
  var text='Needs '+need+' GB of free RAM to load.';
  if(ram&&ram.total_gb<need){return {text:text+' This computer has only '+ram.total_gb+' GB of RAM, so it cannot load this model.',short:true};}
  if(ram&&ram.free_gb<need){return {text:text+' Only '+ram.free_gb+' GB is free right now; close other programs before you use it.',short:true};}
  return {text:text,short:false};
}
var engineRows={};
function markEngineOptions(){
  ['engine','identityEngine','cloneEngine'].forEach(function(id){
    Array.prototype.forEach.call(($(id)||{}).options||[],function(option){
      var row=engineRows[option.value],view=modelCards[option.value];
      if(row&&view&&row.integrated){option.textContent=view.name+(row.state==='not_installed'?' (not installed)':'');}
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
  text.textContent='This model is not installed, so Generate cannot use it yet. ';
  link.href='#models';link.textContent=(row.installation||{}).installer?'Install it in Models & features':'See what it needs in Models & features';
  hint.appendChild(text);hint.appendChild(link);
}
// The first-run note, on a Lab where no model can speak yet: it offers the first model the app can install, with its size, and
// starts the same install as that model's card. When an install it started finishes, the studio is made ready to speak:
// the model chosen, a sentence in the text box (the page's own, unless something else was typed) and the focus on Generate.
var firstRun={engine:null,action:null,ready:null},firstRunWired=false;
function gigabytes(bytes){return (bytes/1e9).toFixed(1)+' GB';}
function installSizeText(row){
  var size=row.install_size;
  return size?'It downloads about '+gigabytes(size.download_bytes)+' and uses '+gigabytes(size.disk_bytes)+' of disk. ':'';
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
function studioReady(job){
  // An install the note started has finished: close its window, which would keep the focus, and set the studio up to speak.
  firstRun.engine=null;firstRun.ready=job.name;
  if(installDialog.engine===job.engine){closeInstallDialog();}
  var engine=$('engine'),text=$('text');
  if(engine.value!==job.engine){engine.value=job.engine;if(typeof refresh==='function'){refresh();}}
  if(!String(text.value||'').trim()){text.value=text.defaultValue||'';}
  if(!$('studio').hidden){$('go').focus();}
}
function renderFirstRun(status){
  var rows=status.engines||[],job=status.install||null,box=$('firstRun'),button=$('firstRunButton');
  var installed=rows.some(function(row){return row.installation&&row.installation.installed&&!row.task_only;});
  var running=!!job&&job.state==='running';
  var offer=rows.filter(function(row){return row.state==='not_installed'&&(row.installation||{}).installer;})[0];
  if(installed&&firstRun.engine&&job&&job.engine===firstRun.engine&&job.state==='finished'){studioReady(job);}
  firstRun.action=null;
  if(firstRun.ready&&installed){
    box.hidden=false;button.hidden=true;$('firstRunModels').hidden=true;
    $('firstRunTitle').textContent=firstRun.ready+' is installed';
    $('firstRunText').textContent='Press Generate to hear it say the sentence below. The first time, the model takes a little while to load.';
    return;
  }
  firstRun.ready=null;
  box.hidden=installed||!(running||offer);
  if(box.hidden){return;}
  $('firstRunModels').hidden=false;button.hidden=false;
  if(running){
    firstRun.action={kind:'progress',engine:job.engine};
    $('firstRunTitle').textContent='Installing '+job.name+'…';
    $('firstRunText').textContent='Running for '+elapsedText(job.seconds)+'. Nothing can speak until it finishes; when it does, this page is ready to speak.';
    button.textContent='Show progress';button.disabled=false;
    return;
  }
  firstRun.action={kind:'install',engine:offer.id};
  var failed=job&&job.state==='failed'&&job.engine===offer.id;
  $('firstRunTitle').textContent=failed?offer.name+' did not install':'No voice model is installed yet';
  $('firstRunText').textContent=failed
    ?'The install stopped (exit code '+job.exit_code+') after '+elapsedText(job.seconds)+'; all of its output is in '+job.log+'. Press Install to try again: it continues where it stopped.'
    :'Install '+offer.name+' to hear the Lab speak. '+installSizeText(offer)+'Install shows what it will do first, and nothing changes until you agree.';
  button.textContent='Install '+offer.name;button.disabled=!!status.busy;
}
function placeCards(status){
  var main=$('modelOverview'),more=$('modelMore'),apart=0;
  (status.engines||[]).forEach(function(row){
    var view=modelCards[row.id];if(!view){return;}
    var manual=row.state==='not_installed'&&!(row.installation||{}).installer,target=manual?more:main;
    if(manual){apart+=1;}
    if(view.card.parentNode!==target){target.appendChild(view.card);}
  });
  $('modelsMoreBox').hidden=!apart;
  $('modelsMoreSummary').textContent=apart+(apart===1?' more model needs':' more models need')+' manual setup';
}
function cardNode(tag,className,text){var node=document.createElement(tag);if(className){node.className=className;}if(text!==undefined){node.textContent=text;}return node;}
function renderModelOverview(result){
  var container=$('modelOverview');container.textContent='';modelCards={};
  wireInstallDialog();wireFirstRun();
  (result.engines||[]).forEach(function(entry){
    var card=cardNode('article','model-card'),title=cardNode('h3','',entry.name),integrated=cardNode('p','eyebrow'),state=cardNode('p','engine-state'),details=cardNode('p','meta'),features=cardNode('p','meta'),ram=cardNode('p','meta');
    var devices=cardNode('div','device-switch'),choices=cardNode('span','device-choices'),note=cardNode('p','meta device-note'),actions=cardNode('div','model-actions'),button=cardNode('button','','Test'),extra=cardNode('button');
    var integration=entry.integration||{},caps=entry.capabilities||{},radios={};
    integrated.textContent=integration.service_api?(integration.task_only?'Integrated · conversion':'Integrated'):'Not integrated';
    features.textContent=['voice_design','voice_cloning','voice_direction','voice_selection'].filter(function(key){return caps[key]&&['supported','worker_verified'].indexOf(caps[key].status)>=0;}).map(function(key){return key.replace(/_/g,' ');}).join(' · ')||(integration.task_only?'Audio-to-audio voice conversion':'See model-specific controls');
    devices.setAttribute('role','radiogroup');devices.setAttribute('aria-label','Runs on');devices.hidden=true;
    devices.appendChild(cardNode('span','meta','Runs on'));devices.appendChild(choices);
    ['gpu','cpu'].forEach(function(value){
      var option=cardNode('label'),input=cardNode('input');
      input.type='radio';input.name='device-'+entry.id;input.value=value;
      input.addEventListener('change',function(){if(input.checked){return chooseDevice(entry.id,value);}});
      option.appendChild(input);option.appendChild(cardNode('span','',value==='gpu'?'GPU':'CPU'));
      choices.appendChild(option);radios[value]=input;
    });
    note.hidden=true;
    button.type='button';extra.type='button';extra.hidden=true;
    button.addEventListener('click',function(){var view=modelCards[entry.id];return view.action==='install'?planInstall(entry.id,view.installGpu):view.action==='progress'?showProgress(entry.id):checkEngine(entry.id);});
    extra.addEventListener('click',function(){return modelCards[entry.id].extra==='gpu'?planInstall(entry.id,true):showProgress(entry.id);});
    actions.appendChild(button);actions.appendChild(extra);
    [title,integrated,state,details,features,ram,devices,note,actions].forEach(function(node){card.appendChild(node);});
    container.appendChild(card);
    modelCards[entry.id]={name:entry.name,card:card,state:state,details:details,ram:ram,devices:devices,radios:radios,note:note,button:button,extraButton:extra,action:'test',extra:null,installGpu:false,pendingDevice:false,deviceError:''};
  });
}
var installRunning=false,installTimer=null;
function refreshEngineStatus(){
  return fetchJson('/api/engines/status').then(function(status){renderEngineStatus(status);}).catch(function(e){$('err').textContent='Could not read the model tests: '+e.message;watchInstall(installRunning);});
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
    view.ram.textContent=need.text;view.ram.className=need.short?'meta warn':'meta';
    view.button.textContent=view.action==='install'?'Install':view.action==='progress'?'Show progress':(row.check?'Test again':'Test');
    view.button.hidden=view.action==='none';
    view.button.disabled=view.action!=='progress'&&(!!status.busy||view.action==='none');
    renderDeviceSwitch(view,row,status,mine);
    var gpuBuild=!running&&!missing&&!!row.gpu&&row.switch==='gpu'&&row.gpu.build==='cpu'&&row.gpu.install;
    view.extra=gpuBuild?'gpu':(mine&&mine.state==='failed'&&missing?'output':null);
    view.extraButton.textContent=view.extra==='gpu'?'Install GPU build':'Show output';
    view.extraButton.hidden=!view.extra;
    view.extraButton.disabled=view.extra==='gpu'&&!!status.busy;
  });
  $('modelsRam').textContent=status.ram?'This computer has '+status.ram.total_gb+' GB of RAM, '+status.ram.free_gb+' GB free right now.':'';
  $('modelsDevice').textContent=status.device==='cuda'?'Each card\'s switch says whether that model runs on the graphics card (GPU) or the processor (CPU); a change applies from the model\'s next load.':'Every model runs on the CPU here: the service found no graphics card it can use. docs/TROUBLESHOOTING.md (Graphics cards) says more.';
  engineRows={};(status.engines||[]).forEach(function(row){engineRows[row.id]=row;});
  placeCards(status);markEngineOptions();showEngineHint();renderFirstRun(status);renderInstallDialog(job);
  if(typeof VoiceLabWorkflows!=='undefined'){VoiceLabWorkflows.refreshCloneEngine();}
  renderSpeedRows(status);
  watchInstall(installRunning);
}
function chooseDevice(engineId,value){
  var view=modelCards[engineId];if(!view){return;}
  view.pendingDevice=true;view.deviceError='';
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/device',{device:value}).catch(function(e){view.deviceError='Could not switch it: '+e.message;}).then(function(){view.pendingDevice=false;return refreshEngineStatus();});
}
function checkEngine(engineId){
  var view=modelCards[engineId];if(!view){return;}
  Object.keys(modelCards).forEach(function(key){modelCards[key].button.disabled=true;});
  view.button.textContent='Testing…';
  view.details.textContent='Testing now. Loading the model can take a minute or more; other model requests are refused until it finishes.';
  $('err').textContent='';
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/check',{}).catch(function(e){$('err').textContent='Test did not finish: '+e.message;}).then(refreshEngineStatus);
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
}
function openInstallDialog(engineId,nvidia,mode,title){
  var parts=dialogParts();
  installDialog={engine:engineId,nvidia:!!nvidia,mode:mode};
  parts.title.textContent=title;parts.state.textContent='';parts.plan.textContent='';parts.log.textContent='';
  parts.plan.hidden=mode!=='plan';parts.log.hidden=mode!=='progress';parts.go.hidden=true;parts.go.disabled=false;
  parts.close.textContent=mode==='plan'?'Cancel':'Hide';
  if(!parts.box.open&&parts.box.showModal){parts.box.showModal();}
  return parts;
}
function closeInstallDialog(){
  var box=$('installDialog');installDialog={engine:null,nvidia:false,mode:null};
  if(box.open&&box.close){box.close();}
}
function planInstall(engineId,nvidia){
  var view=modelCards[engineId];if(!view){return;}
  var parts=openInstallDialog(engineId,nvidia,'plan',(nvidia?'Install the GPU build of ':'Install ')+view.name);
  parts.state.textContent='Reading what the installer will do…';
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/install',{dry_run:true,nvidia:!!nvidia}).then(function(result){
    if(installDialog.engine!==engineId||installDialog.mode!=='plan'){return;}
    var plan=result.plan||{};
    parts.state.textContent=plan.ok?'This is what it will do. Nothing has changed yet.':'This computer cannot take the plan yet.';
    (plan.paragraphs||[]).forEach(function(text){addNote(parts.plan,text,'meta');});
    (plan.problems||[]).forEach(function(text){addNote(parts.plan,text,'meta warn');});
    parts.go.hidden=!plan.ok;parts.close.textContent=plan.ok?'Cancel':'Close';
  }).catch(function(e){if(installDialog.engine===engineId){parts.state.textContent='Could not read the install plan: '+e.message;parts.close.textContent='Close';}}).then(refreshEngineStatus);
}
function startInstall(){
  var engineId=installDialog.engine,nvidia=installDialog.nvidia,parts=dialogParts();
  if(!engineId||installDialog.mode!=='plan'){return;}
  parts.go.disabled=true;
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/install',{nvidia:nvidia}).then(function(){
    var view=modelCards[engineId];
    openInstallDialog(engineId,nvidia,'progress',(nvidia?'Installing the GPU build of ':'Installing ')+(view?view.name:engineId)).state.textContent='Starting…';
  }).catch(function(e){parts.state.textContent='The install did not start: '+e.message;parts.go.hidden=true;parts.close.textContent='Close';}).then(function(){parts.go.disabled=false;return refreshEngineStatus();});
}
function showProgress(engineId){
  var view=modelCards[engineId];if(!view){return;}
  openInstallDialog(engineId,false,'progress','Installing '+view.name).state.textContent='Reading its progress…';
  return refreshEngineStatus();
}
function renderInstallDialog(job){
  if(installDialog.mode!=='progress'||!job||job.engine!==installDialog.engine){return;}
  var parts=dialogParts(),name=job.name+(job.nvidia?' (GPU build)':'');
  if(job.state==='running'){
    parts.title.textContent='Installing '+name;
    parts.state.textContent='Running for '+elapsedText(job.seconds)+'. Downloads and packages can take several minutes, and nothing else can load or speak until it finishes. Hide leaves it running.';
    parts.close.textContent='Hide';
  }else if(job.state==='finished'){
    parts.title.textContent=name+' is installed';
    parts.state.textContent='It finished in '+elapsedText(job.seconds)+'. Press Test on its card to hear it.';
    parts.close.textContent='Close';
  }else{
    parts.title.textContent=name+' did not install';
    parts.state.textContent='The install stopped (exit code '+job.exit_code+') after '+elapsedText(job.seconds)+'. The end of its output is below, and all of it is in '+job.log+'. Press Install on its card to try again: it continues where it stopped.';
    parts.close.textContent='Close';
  }
  showInstallLog(parts.log,job.lines||[]);
}
function renderSpeedRows(status){
  var body=$('speedRows');body.textContent='';
  var rows=status.speed||[];
  if(!rows.length){
    var empty=document.createElement('tr'),cell=document.createElement('td');
    cell.colSpan=5;cell.textContent='No clips measured yet. Run a check or generate a clip to fill this table.';
    empty.appendChild(cell);body.appendChild(empty);return;
  }
  rows.forEach(function(row){
    var tr=document.createElement('tr');
    var name=modelCards[row.engine]?modelCards[row.engine].name:row.engine;
    var cells=[name,deviceLabel(row.device),String(row.clips),row.median_real_time_factor==null?'—':row.median_real_time_factor.toFixed(2),row.last_total_s.toFixed(2)+' s for '+row.last_audio_seconds.toFixed(2)+' s of audio'];
    cells.forEach(function(text){var td=document.createElement('td');td.textContent=text;tr.appendChild(td);});
    body.appendChild(tr);
  });
}
