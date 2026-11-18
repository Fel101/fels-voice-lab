// Voice Lab startup: model controls, voice conversion and the workspace router.
['engine','mode'].forEach(function (id) { $(id).addEventListener('change', function () { refresh(); refreshLoadedQwenInfo(); }); });
$('voxMode').addEventListener('change', refresh);
['speed','exag','cfg','cbMinP','cbTopK','cbTemperature','cbTopP','cbRepetition','cfgScale','maxTokens','temperature','topK','topP','repetitionPenalty','depthTemperature','depthTopK','depthTopP']
  .forEach(function (id) { $(id).addEventListener('input', updateValues); });
['qwenMaxTokens','qwenTemperature','qwenTopK','qwenTopP','qwenRepetition','phononTemperature','phononPace']
  .forEach(function (id) { $(id).addEventListener('input', updateValues); });
['voxCfg','voxSteps'].forEach(function (id) { $(id).addEventListener('input', function () {
  $('voxCfgV').textContent = Number($('voxCfg').value).toFixed(1);
  $('voxStepsV').textContent = $('voxSteps').value;
}); });
$('voicePreset').addEventListener('change', function () { if (descriptions[$('voicePreset').value]) { $('voiceDescription').value = descriptions[$('voicePreset').value]; } });
$('directionPreset').addEventListener('change', function () { if ($('directionPreset').value !== 'custom') { $('direction').value = $('directionPreset').value; } });
$('go').addEventListener('click', generate);
// The install window and the first-run note are in the page from the start, so they work before the first answer about
// the models arrives.
wireInstallDialog(); wireFirstRun();
$('stopWaiting').addEventListener('click', stopWaiting);
$('stream').addEventListener('click', streamGenerate);
$('stopStream').addEventListener('click', function(){stopStreamPlayback(activeStream);});
$('reroll').addEventListener('click', function () { var seed = Number($('seed').value) || 42; $('seed').value = seed >= 2147483647 ? 1 : seed + 1; generate(); });
$('load').addEventListener('click', function () {
  var body = {engine:$('engine').value}; if (body.engine === 'breeze-tts-2') { body.profile = $('profile').value; }
  $('load').disabled = true; $('err').textContent = ''; $('status').textContent = LabText.t('shell.loading_and_warming_model');
  fetchJson('/api/load',body).then(function (r) { applyQwenCapabilities(r, r.engine); if(r.engine==='qwen3-tts-1.7b-custom-voice')applyCustomVoiceIdentityOptions(r); $('status').textContent = [LabText.t('shell.model_ready', {engine: r.engine}), r.profile, r.backend,
    r.load_seconds ? LabText.t('shell.model_load_time', {seconds: r.load_seconds}) : '',
    r.supported_speakers?.length ? LabText.plural('shell.model_speakers', r.supported_speakers.length) : ''].filter(Boolean).join(' / '); })
    .catch(function (e) { $('err').textContent=e.message; $('status').textContent=''; }).then(function () { $('load').disabled=false; });
});
$('unload').addEventListener('click', function () { fetchJson('/api/unload',{}).then(function () { $('status').textContent=LabText.t('shell.models_unloaded'); }).catch(function(e){$('err').textContent=e.message;}); });

fetch('/api/info').then(function(r){return r.json();}).then(function(i){
  $('voice').textContent=''; (i.voices||[]).forEach(function(v){option($('voice'),v,VoiceLabWorkflows.kokoroLabel(v));});
  var firstVoice=Array.from($('voice').options).find(function(item){return item.value==='af_heart';});
  if(firstVoice){firstVoice.selected=true;}
  fillFixedVoices();
  fillPhononVoices(i.phonon_voices);
  $('kokoroLanguage').textContent=''; (i.kokoro_languages||[]).filter(function(item){return item.available;})
    .forEach(function(item){option($('kokoroLanguage'),item.name,item.name+' ('+item.code+')');});
  $('kokoroLanguage').value='American English';
  if(activeIdentity()&&activeIdentity().kind==='fixed_voice'){renderActiveIdentity();}
  $('status').textContent=LabText.t('shell.device_free_ram_gb', {device: i.device, free_ram_gb: i.free_ram_gb});
}).catch(function(){});
$('convertVoice').addEventListener('click',function(){
  var source=$('conversionSource').selectedOptions[0],target=$('conversionTarget').selectedOptions[0];
  if(!source||!target){$('conversionError').textContent=LabText.t('shell.choose_a_performance_to_convert');return;}
  if(source.value===target.value){$('conversionError').textContent=LabText.t('shell.source_and_target_must_be');return;}
  $('convertVoice').disabled=true;$('conversionError').textContent='';$('conversionManifest').textContent='';
  $('conversionStatus').textContent=LabText.t('shell.loading_the_isolated_chatterbox_vc');
  fetchJson('/api/convert',{source_path:source.value,target_reference_path:target.value,
    target_reference_id:target.dataset.referenceId||null})
    .then(function(result){var conversion=result.conversion;$('conversionPlayer').src=conversion.audio_url;
      $('conversionStatus').textContent=LabText.t('shell.conversion_saved_s_perth_watermark', {audio_path: conversion.audio_path, seconds: conversion.manifest.output.duration_seconds.toFixed(2)});
      var link=document.createElement('a');link.href=conversion.audio_url.replace(/converted\.wav$/,'manifest.json');link.textContent=LabText.t('shell.open_provenance_manifest');
      $('conversionManifest').textContent='';$('conversionManifest').appendChild(link);})
    .catch(function(error){$('conversionError').textContent=error.message;$('conversionStatus').textContent='';})
    .then(function(){$('convertVoice').disabled=false;});
});
// A file from this computer for either side of a conversion. The service keeps it in out\conversions\uploads, and its list
// then offers it under its original name. A target voice needs the permission, origin and allowed use first, as Voice cloning
// asks, and the tick is cleared after each file so that every target is confirmed on its own.
function uploadConversionFile(role,input){
  var file=input.files&&input.files[0],list=$(role==='target'?'conversionTarget':'conversionSource');
  if(!file){return Promise.resolve(null);}
  var metadata={filename:file.name,role:role},problem='';
  if(role==='target'){
    metadata.origin=$('conversionTargetOrigin').value.trim();metadata.allowed_use=$('conversionTargetAllowedUse').value.trim();
    metadata.permission=$('conversionTargetPermission').checked;
    if(!metadata.permission||!metadata.origin||!metadata.allowed_use){
      problem=LabText.t('shell.before_you_choose_a_target');}
  }
  if(!problem&&!/\.(wav|flac|ogg|mp3|m4a)$/i.test(file.name)){problem=LabText.t('shell.choose_wav_flac_ogg_mp3');}
  if(!problem&&file.size>32*1024*1024){problem=LabText.t('shell.the_file_is_larger_than');}
  $('conversionError').textContent=problem;
  if(problem){input.value='';return Promise.resolve(null);}
  $('conversionStatus').textContent=LabText.t('shell.reading', {name: file.name});
  return fetch('/api/conversion/uploads',{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream',
      'X-Reference-Metadata':encodeReferenceMetadata(metadata)},body:file})
    .then(function(response){return response.json().then(function(body){
      if(!response.ok){throw new Error(body.error||(LabText.t('shell.the_service_answered', {status: response.status})));}return body.upload;});})
    .then(function(upload){return refreshConversionAssets().then(function(){
      list.value=upload.path;
      if(role==='target'){$('conversionTargetPermission').checked=false;}
      $('conversionStatus').textContent=(role==='target' ? LabText.t('shell.added_target_voice', {original_name: upload.original_name, duration_seconds: upload.duration_seconds}) : LabText.t('shell.added_source_performance', {original_name: upload.original_name, duration_seconds: upload.duration_seconds}));
      return upload;});})
    .catch(function(error){$('conversionError').textContent=error.message;$('conversionStatus').textContent='';return null;})
    .then(function(result){input.value='';return result;});
}
function conversionLabel(item){
  return (item.original_name?item.original_name+LabText.t('shell.from_this_computer'):item.relative_path+' (')+item.duration_seconds+LabText.t('shell.s');
}
function refreshConversionAssets(){
var previousSource=$('conversionSource').value,previousTarget=$('conversionTarget').value;
return Promise.all([fetchJson('/api/conversion/sources'),fetchJson('/api/conversion/targets')]).then(function(values){
  var sources=$('conversionSource'),targets=$('conversionTarget');sources.textContent='';targets.textContent='';
  (values[0].sources||[]).forEach(function(item){option(sources,item.path,conversionLabel(item));});
  (values[1].targets||[]).forEach(function(item){var label=conversionLabel(item);
    if(item.character_name){label+=' — '+item.character_name+' / '+item.identity_label+LabText.t('shell.v')+(item.version||'?');}
    option(targets,item.path,label);targets.options[targets.options.length-1].dataset.referenceId=item.reference_version_id||'';});
  if(!sources.options.length){option(sources,'',LabText.t('shell.no_local_source_wavs_found'),true);}
  if(!targets.options.length){option(targets,'',LabText.t('shell.no_saved_voice_recordings_found'),true);}
if(Array.from(sources.options).some(function(item){return item.value===previousSource;}))sources.value=previousSource;
  if(Array.from(targets.options).some(function(item){return item.value===previousTarget;}))targets.value=previousTarget;
}).catch(function(error){$('conversionError').textContent=LabText.t('shell.could_not_list_local_conversion', {message: error.message});});
}
refreshConversionAssets();
$('conversionSourceFile').addEventListener('change',function(){uploadConversionFile('source',this);});
$('conversionTargetFile').addEventListener('change',function(){uploadConversionFile('target',this);});
$('previewKokoroPhonemes').addEventListener('click',function(){
  $('kokoroPhonemes').textContent=LabText.t('shell.checking_installed_pronunciation_frontend');
  fetchJson('/api/kokoro/phonemize',{text:$('text').value,language:$('kokoroLanguage').value})
    .then(function(result){$('kokoroPhonemes').textContent=result.language_name+' ('+result.language+')\n'+result.segments.map(function(segment){return segment.graphemes+'\n'+segment.phonemes;}).join('\n\n');})
    .catch(function(error){$('kokoroPhonemes').textContent=error.message;});
});
function showWorkspace(){
  var ids=['home','studio','cloning','cast','perform','scenes','books','projects','audio-tools','automation','conversion','library','models','system'];
  var requested=location.hash.slice(1),active=ids.indexOf(requested)>=0?requested:'home';
  ids.forEach(function(id){$(id).classList.add('workspace');$(id).hidden=id!==active;});
  document.querySelectorAll('.workspace-nav a[href^="#"]').forEach(function(link){
    if(link.getAttribute('href')==='#'+active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  });
  document.documentElement.classList.add('workspace-ready');
  document.title=LabText.t('shell.fel_s_voice_lab_2', {workspace: $(active).querySelector('h2').textContent});
  window.scrollTo(0,0);
  placeFirstRun(active);
  if(active==='home')refreshHome();
  if(active==='conversion')refreshConversionAssets();
  if(active==='cloning')loadReferences();
  if(active==='models')refreshEngineStatus();
  if(active==='automation')refreshAutomation().catch(automationError);
  if(active==='system')refreshSystemPage();
  if(active==='books')refreshBooks().catch(bookError);
  if(active==='projects')refreshProjects().catch(projectsError);
}
window.addEventListener('hashchange',showWorkspace);showWorkspace();
fillCatalog().then(function () { refresh(); return loadReferences(); }).then(refreshLoadedQwenInfo).then(function(){return refreshCast();}).then(renderSceneForm).catch(function(e){$('err').textContent=e.message;});
refreshSceneList().catch(showSceneError);
restoreHistory();
refresh(); updateValues(); refreshLoadedQwenInfo();
