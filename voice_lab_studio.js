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
  if(job&&job.state==='running'){return 'Installing…';}
  // A model whose own PyTorch cannot use the graphics card runs on the CPU: say so rather than the service's device.
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
  var check=row.check,install=row.installation||{};
  if(job&&job.state==='running'){return 'Installing for '+elapsedText(job.seconds)+'. Nothing else can run until it finishes, and downloads and packages can take several minutes. The end of its output is below.';}
  if(row.state==='not_installed'){
    var missing='Missing: '+(install.missing||[]).join('; ')+'.';
    if(job&&job.state==='failed'){return missing+' The install stopped (exit code '+job.exit_code+') after '+elapsedText(job.seconds)+'. The end of its output is below, and all of it is in '+job.log+'. Press Install to try again: it continues where it stopped.';}
    if(install.installer){return missing+' Press Install to add it to this folder, or run '+install.installer+' in the lab folder. docs/SETUP.md says what it installs.';}
    return 'The app cannot install this model yet: docs/SETUP.md (Model runtimes) says what it needs. '+missing;
  }
  if(row.state==='conversion_task'){return 'Tested from Voice conversion, because it needs source audio.';}
  if(row.state==='not_integrated'){return 'This build does not expose the engine.';}
  if(!check){return 'Press Test to load the model and speak one short sentence on this device.';}
  if(row.state==='check_outdated'){return 'The last test ('+utcStamp(check.checked_at)+') was made before this model or its packages changed, or before tests recorded that. Press Test again to confirm it still speaks.';}
  if(check.status==='passed'){
    var where='';
    if(check.ran_on&&check.ran_on!==device){where=check.ran_on==='cpu'&&device==='cuda'?', on the CPU: this model\'s PyTorch cannot use the graphics card':', on '+check.ran_on;}
    return 'Tested '+utcStamp(check.checked_at)+': '+check.total_s+' s to make '+check.audio_seconds+' s of audio'+where+'.';
  }
  return 'Failed '+utcStamp(check.checked_at)+': '+check.reason;
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
function renderFirstRun(status){
  var rows=status.engines||[],job=status.install||null,box=$('firstRun');
  var installed=rows.some(function(row){return row.installation&&row.installation.installed&&!row.task_only;});
  var running=!!job&&job.state==='running';
  var offer=rows.filter(function(row){return row.state==='not_installed'&&(row.installation||{}).installer;})[0];
  box.hidden=installed||!(running||offer);
  if(box.hidden){return;}
  $('firstRunTitle').textContent=running?'Installing '+job.name+'…':'No voice model is installed yet';
  $('firstRunText').textContent=running?'Follow it in Models & features. Nothing can speak until it finishes.':'Press Install on the '+offer.name+' card in Models & features. It shows what it will do first.';
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
function renderModelOverview(result){
  var container=$('modelOverview');container.textContent='';modelCards={};
  (result.engines||[]).forEach(function(entry){
    var card=document.createElement('article'),title=document.createElement('h3'),integrated=document.createElement('p'),state=document.createElement('p'),details=document.createElement('p'),features=document.createElement('p'),ram=document.createElement('p'),box=document.createElement('div'),plan=document.createElement('div'),go=document.createElement('button'),stop=document.createElement('button'),log=document.createElement('pre'),button=document.createElement('button');
    card.className='model-card';title.textContent=entry.name;integrated.className='eyebrow';state.className='engine-state';details.className='meta';features.className='meta';ram.className='meta';
    var integration=entry.integration||{},caps=entry.capabilities||{};
    integrated.textContent=integration.service_api?(integration.task_only?'Integrated · conversion':'Integrated'):'Not integrated';
    features.textContent=['voice_design','voice_cloning','voice_direction','voice_selection'].filter(function(key){return caps[key]&&['supported','worker_verified'].indexOf(caps[key].status)>=0;}).map(function(key){return key.replace(/_/g,' ');}).join(' · ')||(integration.task_only?'Audio-to-audio voice conversion':'See model-specific controls');
    button.type='button';button.textContent='Test';
    go.type='button';go.className='go';go.textContent='Install now';stop.type='button';stop.textContent='Cancel';
    box.className='install-box';box.hidden=true;log.className='install-log';log.hidden=true;
    button.addEventListener('click',function(){if(modelCards[entry.id].action==='install'){planInstall(entry.id);}else{checkEngine(entry.id);}});
    go.addEventListener('click',function(){startInstall(entry.id);});
    stop.addEventListener('click',function(){closePlan(entry.id);});
    box.appendChild(plan);box.appendChild(go);box.appendChild(stop);
    card.appendChild(title);card.appendChild(integrated);card.appendChild(state);card.appendChild(details);card.appendChild(features);card.appendChild(ram);card.appendChild(box);card.appendChild(log);
    if(!integration.task_only){card.appendChild(button);}
    container.appendChild(card);
    modelCards[entry.id]={name:entry.name,card:card,state:state,details:details,ram:ram,button:button,box:box,plan:plan,go:go,stop:stop,log:log,action:'test',planOpen:false};
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
  log.textContent=lines.slice(-12).join('\n');
  if(follow){log.scrollTop=log.scrollHeight;}
}
function renderEngineStatus(status){
  var job=status.install||null;
  installRunning=!!job&&job.state==='running';
  (status.engines||[]).forEach(function(row){
    var view=modelCards[row.id];if(!view){return;}
    var mine=job&&job.engine===row.id?job:null,running=!!mine&&mine.state==='running',missing=row.state==='not_installed',install=row.installation||{},need=ramText(row,status.ram);
    view.action=missing?(install.installer?'install':'none'):(row.state==='not_integrated'||row.state==='conversion_task'?'none':'test');
    view.state.textContent=engineStateText(row,status.device,mine);
    view.details.textContent=engineDetailText(row,status.device,mine);
    view.ram.textContent=need.text;view.ram.className=need.short?'meta warn':'meta';
    view.button.textContent=view.action==='install'?'Install':(row.check?'Test again':'Test');
    view.button.hidden=view.action==='none'||running||view.planOpen;
    view.button.disabled=!!status.busy||view.action==='none';
    if(running){view.planOpen=false;view.box.hidden=true;}
    view.log.hidden=!(mine&&(running||(mine.state==='failed'&&missing)));
    view.card.className='model-card'+(view.planOpen||!view.log.hidden?' wide':'');
    showInstallLog(view.log,mine?mine.lines||[]:[]);
  });
  $('modelsRam').textContent=status.ram?'This computer has '+status.ram.total_gb+' GB of RAM, '+status.ram.free_gb+' GB free right now.':'';
  engineRows={};(status.engines||[]).forEach(function(row){engineRows[row.id]=row;});
  placeCards(status);markEngineOptions();showEngineHint();renderFirstRun(status);
  if(typeof VoiceLabWorkflows!=='undefined'){VoiceLabWorkflows.refreshCloneEngine();}
  renderSpeedRows(status);
  watchInstall(installRunning);
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
function planInstall(engineId){
  var view=modelCards[engineId];if(!view){return;}
  Object.keys(modelCards).forEach(function(key){modelCards[key].button.disabled=true;});
  view.details.textContent='Reading what the installer will do…';
  $('err').textContent='';
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/install',{dry_run:true}).then(function(result){
    var plan=result.plan||{};
    view.plan.textContent='';
    (plan.paragraphs||[]).forEach(function(text){addNote(view.plan,text,'meta');});
    (plan.problems||[]).forEach(function(text){addNote(view.plan,text,'meta warn');});
    view.go.hidden=!plan.ok;view.stop.textContent=plan.ok?'Cancel':'Close';view.box.hidden=false;view.planOpen=true;
  }).catch(function(e){$('err').textContent='Could not read the install plan: '+e.message;}).then(refreshEngineStatus);
}
function closePlan(engineId){
  var view=modelCards[engineId];if(!view){return;}
  view.planOpen=false;view.box.hidden=true;
  return refreshEngineStatus();
}
function startInstall(engineId){
  var view=modelCards[engineId];if(!view){return;}
  view.go.disabled=true;
  $('err').textContent='';
  return fetchJson('/api/engines/'+encodeURIComponent(engineId)+'/install',{}).catch(function(e){$('err').textContent='The install did not start: '+e.message;}).then(function(){view.go.disabled=false;view.planOpen=false;view.box.hidden=true;return refreshEngineStatus();});
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
