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
$('stopWaiting').addEventListener('click', stopWaiting);
$('stream').addEventListener('click', streamGenerate);
$('stopStream').addEventListener('click', function(){stopStreamPlayback(activeStream);});
$('reroll').addEventListener('click', function () { var seed = Number($('seed').value) || 42; $('seed').value = seed >= 2147483647 ? 1 : seed + 1; generate(); });
$('load').addEventListener('click', function () {
  var body = {engine:$('engine').value}; if (body.engine === 'breeze-tts-2') { body.profile = $('profile').value; }
  $('load').disabled = true; $('err').textContent = ''; $('status').textContent = 'Loading and warming model...';
  fetchJson('/api/load',body).then(function (r) { applyQwenCapabilities(r, r.engine); if(r.engine==='qwen3-tts-1.7b-custom-voice')applyCustomVoiceIdentityOptions(r); $('status').textContent = r.engine + ' ready' + (r.profile ? ' / ' + r.profile : '') + (r.backend ? ' / ' + r.backend : '') + (r.load_seconds ? ' / ' + r.load_seconds + ' s load' : '') + (r.supported_speakers && r.supported_speakers.length ? ' / ' + r.supported_speakers.length + ' model speakers' : ''); })
    .catch(function (e) { $('err').textContent=e.message; $('status').textContent=''; }).then(function () { $('load').disabled=false; });
});
$('unload').addEventListener('click', function () { fetchJson('/api/unload',{}).then(function () { $('status').textContent='Models unloaded.'; }).catch(function(e){$('err').textContent=e.message;}); });

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
  $('status').textContent='Device: '+i.device+' / free RAM '+i.free_ram_gb+' GB';
}).catch(function(){});
$('convertVoice').addEventListener('click',function(){
  var source=$('conversionSource').selectedOptions[0],target=$('conversionTarget').selectedOptions[0];
  if(!source||!target){$('conversionError').textContent='Choose a source performance and a target reference.';return;}
  if(source.value===target.value){$('conversionError').textContent='Source and target must be separate files.';return;}
  $('convertVoice').disabled=true;$('conversionError').textContent='';$('conversionManifest').textContent='';
  $('conversionStatus').textContent='Loading the isolated Chatterbox VC worker and converting...';
  fetchJson('/api/convert',{source_path:source.value,target_reference_path:target.value,
    target_reference_id:target.dataset.referenceId||null})
    .then(function(result){var conversion=result.conversion;$('conversionPlayer').src=conversion.audio_url;
      $('conversionStatus').textContent='Conversion saved: '+conversion.audio_path+' / '+conversion.manifest.output.duration_seconds.toFixed(2)+' s / Perth watermark retained.';
      var link=document.createElement('a');link.href=conversion.audio_url.replace(/converted\.wav$/,'manifest.json');link.textContent='Open provenance manifest';
      $('conversionManifest').textContent='';$('conversionManifest').appendChild(link);})
    .catch(function(error){$('conversionError').textContent=error.message;$('conversionStatus').textContent='';})
    .then(function(){$('convertVoice').disabled=false;});
});
function refreshConversionAssets(){
var previousSource=$('conversionSource').value,previousTarget=$('conversionTarget').value;
return Promise.all([fetchJson('/api/conversion/sources'),fetchJson('/api/conversion/targets')]).then(function(values){
  var sources=$('conversionSource'),targets=$('conversionTarget');sources.textContent='';targets.textContent='';
  (values[0].sources||[]).forEach(function(item){option(sources,item.path,item.relative_path+' ('+item.duration_seconds+' s)');});
  (values[1].targets||[]).forEach(function(item){var label=item.relative_path+' ('+item.duration_seconds+' s)';
    if(item.character_name){label+=' — '+item.character_name+' / '+item.identity_label+' v'+(item.version||'?');}
    option(targets,item.path,label);targets.options[targets.options.length-1].dataset.referenceId=item.reference_version_id||'';});
  if(!sources.options.length){option(sources,'','No local source WAVs found',true);}
  if(!targets.options.length){option(targets,'','No local target references found',true);}
if(Array.from(sources.options).some(function(item){return item.value===previousSource;}))sources.value=previousSource;
  if(Array.from(targets.options).some(function(item){return item.value===previousTarget;}))targets.value=previousTarget;
}).catch(function(error){$('conversionError').textContent='Could not list local conversion inputs: '+error.message;});
}
refreshConversionAssets();
$('previewKokoroPhonemes').addEventListener('click',function(){
  $('kokoroPhonemes').textContent='Checking installed pronunciation frontend...';
  fetchJson('/api/kokoro/phonemize',{text:$('text').value,language:$('kokoroLanguage').value})
    .then(function(result){$('kokoroPhonemes').textContent=result.language_name+' ('+result.language+')\n'+result.segments.map(function(segment){return segment.graphemes+'\n'+segment.phonemes;}).join('\n\n');})
    .catch(function(error){$('kokoroPhonemes').textContent=error.message;});
});
function showWorkspace(){
  var ids=['studio','cloning','cast','perform','scenes','conversion','library','models','system'];
  var requested=location.hash.slice(1),active=ids.indexOf(requested)>=0?requested:'studio';
  ids.forEach(function(id){$(id).classList.add('workspace');$(id).hidden=id!==active;});
  document.querySelectorAll('.workspace-nav a[href^="#"]').forEach(function(link){
    if(link.getAttribute('href')==='#'+active)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  });
  document.documentElement.classList.add('workspace-ready');
  document.title="Fel's Voice Lab · "+$(active).querySelector('h2').textContent;
  window.scrollTo(0,0);
  if(active==='conversion')refreshConversionAssets();
  if(active==='cloning')loadReferences();
  if(active==='models')refreshEngineStatus();
  if(active==='system')refreshSystemPage();
}
window.addEventListener('hashchange',showWorkspace);showWorkspace();
fillCatalog().then(function () { refresh(); return loadReferences(); }).then(refreshLoadedQwenInfo).then(function(){return refreshCast();}).then(renderSceneForm).catch(function(e){$('err').textContent=e.message;});
refreshSceneList().catch(showSceneError);
restoreHistory();
refresh(); updateValues(); refreshLoadedQwenInfo();
