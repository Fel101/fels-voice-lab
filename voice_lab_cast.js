// Voice Lab cast: reference versions, voice identities, performance controls and saved takes.
var castCharacters = [];
var referenceSourceFile = null, referenceSourceBlob = null, referenceBuffer = null, referenceObjectUrl = null, micStream = null, micContext = null, micProcessor = null, micChunks = [];
var referenceLoadRevision = 0, referencePreviewRevision = 0, referenceDecodePending = false, castMicTimer = null;
function setReferencePlayer(blob) {
  if(referenceObjectUrl){URL.revokeObjectURL(referenceObjectUrl);referenceObjectUrl=null;}
  if(blob){referenceObjectUrl=URL.createObjectURL(blob);$('referencePlayer').src=referenceObjectUrl;}
  else{$('referencePlayer').pause();$('referencePlayer').removeAttribute('src');$('referencePlayer').load();}
}
function clearReferenceEditor() {
  referenceLoadRevision++;
  referencePreviewRevision++;
  referenceSourceFile=null;referenceSourceBlob=null;referenceBuffer=null;
  if(micProcessor){micProcessor.onaudioprocess=null;micProcessor.disconnect();micProcessor=null;}
  if(micStream){micStream.getTracks().forEach(function(track){track.stop();});micStream=null;}
  if(micContext){micContext.close().catch(function(){});micContext=null;}
  clearTimeout(castMicTimer);castMicTimer=null;
  micChunks=[];$('micStart').hidden=false;$('micStop').hidden=true;$('micStart').disabled=false;
  referenceDecodePending=false;$('saveReference').disabled=false;$('saveReferenceVersion').disabled=false;$('previewReference').disabled=false;
  $('referenceFile').value='';$('referenceTranscript').value='';$('referenceOrigin').value='';$('referenceAllowedUse').value='';
  setReferencePlayer(null);
  $('referenceAnalysis').textContent='No recording loaded.';$('referenceStatus').textContent='';
  $('trimStart').value='0';$('trimEnd').value='';$('noiseReductionEnabled').checked=false;syncNoiseReductionFields();
  var canvas=$('referenceWave');canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);
}
var audioContext = null;
function activeCharacter() { return castCharacters.find(function (item) { return item.id === $('characterSelect').value; }) || null; }
function activeIdentity() {
  var character = activeCharacter();
  return character && character.identities.find(function (item) { return item.id === $('identitySelect').value; }) || null;
}
function performanceCharacter() { return castCharacters.find(function (item) { return item.id === $('performCharacter').value; }) || null; }
function performanceIdentity() {
  var character = performanceCharacter();
  return character && character.identities.find(function (item) { return item.id === $('performIdentity').value; }) || null;
}
function apiAudioContext() {
  if (!audioContext || audioContext.state==='closed') { var AudioCtor = window.AudioContext || window.webkitAudioContext; if (AudioCtor) { audioContext = new AudioCtor(); } }
  if (!audioContext) { throw new Error('This browser cannot decode audio for local reference preview.'); }
  if (audioContext.state === 'suspended') { audioContext.resume(); }
  return audioContext;
}
function decodeBlob(blob) {
  return blob.arrayBuffer().then(function (bytes) { return apiAudioContext().decodeAudioData(bytes); });
}
function bufferAnalysis(buffer) {
  var peak = 0, sum = 0, clipped = 0, count = 0;
  for (var channel = 0; channel < buffer.numberOfChannels; channel++) {
    var data = buffer.getChannelData(channel);
    for (var i = 0; i < data.length; i++) { var v = Math.abs(data[i]); peak = Math.max(peak, v); sum += v*v; if (v >= 0.999) { clipped++; } count++; }
  }
  return {peak:peak, rms:Math.sqrt(sum / Math.max(1,count)), clipped:clipped, seconds:buffer.duration};
}
function drawReferenceWave(buffer, startSeconds, endSeconds) {
  var canvas = $('referenceWave'), ctx = canvas.getContext('2d'), width = canvas.width, height = canvas.height;
  ctx.clearRect(0, 0, width, height); ctx.fillStyle = getComputedStyle(document.body).getPropertyValue('--bg'); ctx.fillRect(0,0,width,height);
  if (!buffer) { return; }
  var begin = Math.max(0, Math.floor((startSeconds || 0) * buffer.sampleRate));
  var finish = Math.min(buffer.length, Math.ceil((endSeconds || buffer.duration) * buffer.sampleRate));
  if (finish <= begin) { return; }
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue('--line'); ctx.beginPath(); ctx.moveTo(0,height/2); ctx.lineTo(width,height/2); ctx.stroke();
  var data = buffer.getChannelData(0), step = Math.max(1, Math.floor((finish - begin) / width));
  ctx.strokeStyle = getComputedStyle(document.body).getPropertyValue('--accent'); ctx.beginPath();
  for (var x = 0; x < width; x++) {
    var low = 1, high = -1, from = begin + x*step, to = Math.min(finish,from+step);
    for (var j=from;j<to;j++) { var sample=data[j]; low=Math.min(low,sample); high=Math.max(high,sample); }
    ctx.moveTo(x,(1-high)*height/2); ctx.lineTo(x,(1-low)*height/2);
  }
  ctx.stroke();
  var a = Math.max(0, begin/buffer.sampleRate), b = Math.min(buffer.duration,finish/buffer.sampleRate);
  ctx.fillStyle = 'rgba(138,79,255,0.12)'; ctx.fillRect(a/buffer.duration*width,0,Math.max(0,(b-a)/buffer.duration*width),height);
}
function wavBlob(buffer, startSeconds, endSeconds, targetRate, channelMode) {
  var start = Math.max(0,Math.floor(startSeconds*buffer.sampleRate)), end = Math.min(buffer.length,Math.ceil(endSeconds*buffer.sampleRate));
  var outRate = targetRate || buffer.sampleRate, count = Math.max(1,Math.floor((end-start)*outRate/buffer.sampleRate));
  var sourceChannels = [];
  for (var c=0;c<buffer.numberOfChannels;c++) { sourceChannels.push(buffer.getChannelData(c)); }
  var dataBytes = count*2, bytes = new ArrayBuffer(44+dataBytes), view = new DataView(bytes), at=0;
  function writeText(text) { for(var k=0;k<text.length;k++) view.setUint8(at++,text.charCodeAt(k)); }
  writeText('RIFF'); view.setUint32(at,36+dataBytes,true); at+=4; writeText('WAVE'); writeText('fmt '); view.setUint32(at,16,true); at+=4;
  view.setUint16(at,1,true); at+=2; view.setUint16(at,1,true); at+=2; view.setUint32(at,outRate,true); at+=4;
  view.setUint32(at,outRate*2,true); at+=4; view.setUint16(at,2,true); at+=2; view.setUint16(at,16,true); at+=2;
  writeText('data'); view.setUint32(at,dataBytes,true); at+=4;
  var left=sourceChannels[0], right=sourceChannels[Math.min(1,sourceChannels.length-1)], ratio=buffer.sampleRate/outRate;
  for(var n=0;n<count;n++) {
    var pos=start+n*ratio, idx=Math.min(end-1,Math.floor(pos)), frac=pos-idx, idx2=Math.min(end-1,idx+1);
    var a=left[idx]+(left[idx2]-left[idx])*frac, b=right[idx]+(right[idx2]-right[idx])*frac;
    var sample=channelMode==='left'?a:channelMode==='right'?b:sourceChannels.length===1?a:(a+b)/2;
    sample=Math.max(-1,Math.min(1,sample)); view.setInt16(at,sample<0?sample*32768:sample*32767,true); at+=2;
  }
  return new Blob([bytes],{type:'audio/wav'});
}
function activeTrim() {
  if (!referenceBuffer) { throw new Error('Choose or record a local reference first.'); }
  var start=Number($('trimStart').value), end=Number($('trimEnd').value);
  if (!Number.isFinite(start)||!Number.isFinite(end)||start<0||end<=start||end>referenceBuffer.duration+0.02) { throw new Error('Choose a valid trim range within the source waveform.'); }
  if (end-start>60) { throw new Error('This reference exceeds the 60-second native Breeze / studio safety limit. Trim it before saving.'); }
  return {start:start,end:end};
}
function previewReferenceClip() {
  var revision=referenceLoadRevision,previewRevision=++referencePreviewRevision;
  try {
    var range=activeTrim(),identity=activeIdentity();
    if(!identity||identity.kind!=='reference'){throw new Error('Choose a reference-cloning identity in Cast first.');}
    if(!referenceSourceBlob){throw new Error('Choose or record a local reference first.');}
    var selected=selectedReference(identity,$('referenceVersion'));
    var metadata={filename:referenceSourceFile?referenceSourceFile.name:(selected&&selected.source_name)||'source.wav',edits:fillReferenceMetadata()};
    var encoded=encodeReferenceMetadata(metadata);
    $('referenceStatus').textContent=$('noiseReductionEnabled').checked?'Creating denoised preview...':'Creating processed preview...';
    fetch('/api/identities/'+identity.id+'/references/preview',{method:'POST',headers:{'Content-Type':referenceSourceBlob.type||'application/octet-stream','X-Reference-Metadata':encoded},body:referenceSourceBlob})
      .then(function(response){if(!response.ok){return response.json().then(function(result){throw new Error(result.error||response.status);});}return response.blob().then(function(blob){return {blob:blob,headers:response.headers};});})
      .then(function(result){if(revision!==referenceLoadRevision||previewRevision!==referencePreviewRevision)return;setReferencePlayer(result.blob);
        drawReferenceWave(referenceBuffer,range.start,range.end);
        var details='Preview: '+result.headers.get('X-Reference-Duration')+' s, mono, '+result.headers.get('X-Reference-Sample-Rate')+' Hz, RMS '+result.headers.get('X-Reference-RMS')+' dBFS.';details+=referenceQualityText(decodeReferenceQuality(result.headers.get('X-Reference-Quality')));
        if($('noiseReductionEnabled').checked){details+=' Steady-noise reduction is active; the original remains unchanged.';}
        $('referenceAnalysis').textContent=details;$('referenceStatus').textContent='Preview ready. Listen before saving this reference version.';})
      .catch(function(error){if(revision===referenceLoadRevision&&previewRevision===referencePreviewRevision)$('referenceStatus').textContent='Could not create processed preview: '+error.message;});
  } catch(error) { $('referenceStatus').textContent=error.message; }
}
function loadReferenceSource(blob, file, revision) {
  var token=revision===undefined?++referenceLoadRevision:revision;
  if(token!==referenceLoadRevision)return Promise.reject(new Error('The reference selection changed.'));
  referenceSourceFile=null;referenceSourceBlob=null;referenceBuffer=null;
  referenceDecodePending=true;referencePreviewRevision++;$('saveReference').disabled=true;$('saveReferenceVersion').disabled=true;$('previewReference').disabled=true;
  if(file)setReferencePlayer(null);
  return decodeBlob(blob).then(function(buffer) {
    if(token!==referenceLoadRevision){throw new Error('The reference selection changed; choose the current recording.');}
    referenceSourceFile=file||null;referenceSourceBlob=blob;
    referenceBuffer=buffer; $('trimStart').value='0'; $('trimEnd').value=buffer.duration.toFixed(2);
    if(file) {
      $('noiseReductionEnabled').checked=false;$('noiseProfileStart').value='0';$('noiseProfileEnd').value=Math.min(0.30,buffer.duration).toFixed(2);syncNoiseReductionFields();
      setReferencePlayer(file);
      var stats=bufferAnalysis(buffer); $('referenceAnalysis').textContent='Loaded '+file.name+' / '+buffer.duration.toFixed(2)+' s / '+buffer.sampleRate+' Hz / '+buffer.numberOfChannels+' channel(s)'+(stats.clipped?' / clipped samples '+stats.clipped:'');
    }
    drawReferenceWave(buffer,0,buffer.duration); return buffer;
  }).finally(function(){if(token===referenceLoadRevision){referenceDecodePending=false;$('saveReference').disabled=false;$('saveReferenceVersion').disabled=false;$('previewReference').disabled=false;}});
}
function fillFixedVoices() {
  if (!$('voice') || !$('fixedVoice')) { return; }
  $('fixedVoice').textContent=''; Array.from($('voice').options).forEach(function(item){option($('fixedVoice'),item.value,item.textContent);});
  var defaultVoice=Array.from($('fixedVoice').options).find(function(item){return item.value==='af_heart';});
  if(defaultVoice){defaultVoice.selected=true;}
}
function updateIdentityEngineKind() {
  $('fixedVoiceRow').hidden=$('identityEngine').value!=='kokoro';
  $('customVoiceIdentityRow').hidden=$('identityEngine').value!=='qwen3-tts-1.7b-custom-voice';
  $('phononIdentityRow').hidden=$('identityEngine').value!=='phonon-31m-en';
  if($('identityEngine').value==='phonon-31m-en'&&!phononVoices.length)refreshPhononVoices().catch(function(){});
}
function applyCustomVoiceIdentityOptions(info) {
  var speakers=info.supported_speakers||[],languages=info.supported_languages||[];
  $('presetSpeaker').textContent='';$('presetLanguage').textContent='';
  speakers.forEach(function(value){option($('presetSpeaker'),value,value);});
  languages.forEach(function(value){option($('presetLanguage'),value,languageOptionLabel('qwen3-tts-1.7b-custom-voice',value));});
  if(!speakers.length||!languages.length){$('customVoiceIdentityStatus').textContent='The loaded checkpoint reported no selectable speaker/language metadata.';return;}
  $('customVoiceIdentityStatus').textContent='Loaded '+(info.checkpoint||'CustomVoice')+' / revision '+(info.checkpoint_revision||'not reported')+' / '+speakers.length+' speakers / '+languages.length+' languages.';
}
function loadCustomVoiceIdentityOptions() {
  $('loadCustomVoiceIdentity').disabled=true;$('customVoiceIdentityStatus').textContent='Loading the pinned local CustomVoice checkpoint to query its speakers and languages...';
  return fetchJson('/api/load',{engine:'qwen3-tts-1.7b-custom-voice'}).then(applyCustomVoiceIdentityOptions)
    .catch(function(error){$('customVoiceIdentityStatus').textContent='CustomVoice metadata unavailable: '+error.message;})
    .then(function(){$('loadCustomVoiceIdentity').disabled=false;});
}
function refreshCast(preferredCharacter, preferredIdentity) {
  var charBefore=preferredCharacter||$('characterSelect').value, idBefore=preferredIdentity||$('identitySelect').value;
  return fetchJson('/api/cast').then(function(result){
    castCharacters=result.characters||[];
    [$('characterSelect'),$('performCharacter')].forEach(function(select){
      var before=select.id==='performCharacter'?select.value:charBefore; select.textContent='';
      castCharacters.forEach(function(c){option(select,c.id,c.name);});
      if(castCharacters.some(function(c){return c.id===before;})){select.value=before;} else if(castCharacters.length){select.value=castCharacters[0].id;}
    });
    renderActiveCharacter(idBefore); renderPerformanceCharacter();
    if(window.VoiceLabLibrary) return window.VoiceLabLibrary.refresh();
  }).catch(function(error){$('referenceStatus').textContent='Cast library unavailable: '+error.message;});
}
function renderActiveCharacter(preferredIdentity) {
  var character=activeCharacter(); $('identitySelect').textContent='';
  if(!character){clearReferenceEditor();option($('identitySelect'),'','Create or open a character first',true);$('characterNotes').value='';$('referenceEditor').hidden=true;return;}
  $('characterNotes').value=character.description||'';
  character.identities.forEach(function(identity){option($('identitySelect'),identity.id,VoiceLabLibrary.identityLabel(identity));});
  if(character.identities.some(function(i){return i.id===preferredIdentity;})){$('identitySelect').value=preferredIdentity;}
  else if(character.identities.length){$('identitySelect').value=character.identities[0].id;}
  renderActiveIdentity();
}
function selectedReference(identity, selector) {
  return identity && identity.references.find(function(ref){return ref.id===selector.value;}) || null;
}
function renderActiveIdentity() {
  clearReferenceEditor();
  var identity=activeIdentity(); $('referenceEditor').hidden=!identity||identity.kind!=='reference';
  $('identityState').textContent=identity?identity.engine_id+' / '+identity.status+(identity.approved_reference_id?' / approved reference v'+(identity.references.find(function(r){return r.id===identity.approved_reference_id;})||{}).version:''):'';
  $('approveFixedIdentity').hidden=!identity||['fixed_voice','preset_voice'].indexOf(identity.kind)<0;
  if(identity&&identity.kind==='preset_voice'){
    $('approveFixedIdentity').textContent='Approve preset identity';
    $('identityState').textContent=identity.engine_options.checkpoint+' / revision '+identity.engine_options.checkpoint_revision+' / '+identity.engine_options.speaker+' / '+identity.engine_options.language+' / '+identity.status;
  }else{$('approveFixedIdentity').textContent='Approve fixed voice';}
  if(identity&&identity.kind==='fixed_voice') {
    var savedVoices=(identity.engine_options.voice||'').split(',');
    Array.from($('fixedVoice').options).forEach(function(item){item.selected=savedVoices.indexOf(item.value)>=0;});
  }
  $('referenceVersion').textContent=''; $('approveReference').textContent='';
  if(identity&&identity.kind==='reference') {
    identity.references.forEach(function(ref){option($('referenceVersion'),ref.id,'v'+ref.version+' — '+ref.duration_seconds.toFixed(2)+' s — '+ref.transcript.slice(0,60)+(ref.retired?' — retired':''));if(!ref.retired)option($('approveReference'),ref.id,'v'+ref.version+' — '+ref.transcript.slice(0,60));});
    if(identity.references.length){$('referenceVersion').value=identity.references[0].id;$('approveReference').value=identity.approved_reference_id||(identity.references.find(function(ref){return !ref.retired;})||{}).id||'';loadSavedReference(identity.references[0]);}
    else { $('referenceAnalysis').textContent='No version saved yet.'; }
    $('saveReference').hidden=false;$('saveReferenceVersion').hidden=!identity.references.length;
  }
  updateReferenceRetirement();
  renderPerformanceCharacter();
}
function loadSavedReference(reference) {
  if(!reference){return;}
  var revision=++referenceLoadRevision;
  $('referenceTranscript').value=reference.transcript||''; $('referenceOrigin').value=reference.origin||''; $('referenceAllowedUse').value=reference.allowed_use||'';
  $('trimStart').value=reference.edit.start_seconds||0; $('trimEnd').value=reference.edit.end_seconds||reference.duration_seconds;
  $('referenceRate').value=String(reference.edit.target_sample_rate||reference.sample_rate); $('referenceChannel').value=reference.edit.channel||'mono';
  var noise=reference.edit.noise_reduction||{};$('noiseReductionEnabled').checked=noise.enabled===true;
  $('noiseProfileStart').value=noise.profile_start_seconds===undefined?0:noise.profile_start_seconds;
  $('noiseProfileEnd').value=noise.profile_end_seconds===undefined?0.30:noise.profile_end_seconds;
  $('noiseReductionStrength').value=noise.strength===undefined?0.75:noise.strength;syncNoiseReductionFields();
  fetch(reference.audio_url).then(function(r){if(!r.ok)throw new Error(r.status);return r.blob();}).then(function(blob){if(revision===referenceLoadRevision){setReferencePlayer(blob);}}).catch(function(error){if(revision===referenceLoadRevision)$('referenceStatus').textContent='Could not load audio: '+error.message;});
  fetch('/api/references/'+reference.id+'/source').then(function(r){if(!r.ok)throw new Error(r.status);return r.blob();}).then(function(blob){return loadReferenceSource(blob,null,revision);})
    .then(function(buffer){$('trimStart').value=reference.edit.start_seconds||0;$('trimEnd').value=reference.edit.end_seconds||reference.duration_seconds;$('referenceRate').value=String(reference.edit.target_sample_rate||reference.sample_rate);$('referenceChannel').value=reference.edit.channel||'mono';drawReferenceWave(buffer,Number($('trimStart').value),Number($('trimEnd').value));$('referenceAnalysis').textContent='v'+reference.version+' / '+reference.duration_seconds.toFixed(2)+' s / '+reference.sample_rate+' Hz / '+reference.channels+' mono channel(s) / '+reference.audio_sha256.slice(0,16)+(reference.warnings.length?' / '+reference.warnings.join(' '):'');})
    .catch(function(error){if(revision===referenceLoadRevision)$('referenceStatus').textContent='Could not preview saved reference: '+error.message;});
}
function renderPerformanceCharacter() {
  var character=performanceCharacter(), select=$('performIdentity'), before=select.value; select.textContent='';
  if(!character){option(select,'','Open a character first',true);$('performReference').textContent='';return;}
  var visible=character.identities.filter(function(identity){return !identity.retired;});
  visible.forEach(function(identity){option(select,identity.id,VoiceLabLibrary.identityLabel(identity));});
  if(visible.some(function(i){return i.id===before;})){select.value=before;} else if(visible.length){select.value=visible[0].id;}
  renderPerformanceIdentity();
}
function renderPerformanceIdentity() {
  var character=performanceCharacter(), identity=performanceIdentity();$('performReference').textContent='';
  if(!identity){option($('performReference'),'','No voice identity',true);$('performSupport').textContent='Create a voice candidate in Cast first.';$('performDirectionWrap').hidden=true;$('nativeTags').hidden=true;$('performSettingsPanel').hidden=true;return;}
  var options=identity.engine_options||{}, isPreset=identity.kind==='preset_voice';
  $('performReference').parentElement.hidden=identity.kind!=='reference';
  if(identity.kind==='reference'){
    (identity.references||[]).filter(function(ref){return !ref.retired;}).forEach(function(ref){option($('performReference'),ref.id,'v'+ref.version+(ref.id===identity.approved_reference_id?' — approved':' — candidate')+' / '+ref.duration_seconds.toFixed(2)+' s');});
    if(identity.approved_reference_id){$('performReference').value=identity.approved_reference_id;} else if(identity.references&&identity.references.length){$('performReference').value=identity.references[0].id;}
  }
  var schema=performanceCapabilitiesByEngine[identity.engine_id]||{controls:[],direction:{status:'unsupported'}};
  var languageOptions=isPreset?(options.supported_languages||[options.language||'English']):(schema.languages||['English']);
  var identityChanged=lastPerformanceIdentityId!==identity.id;
  var previousLanguage=$('performLanguage').value,defaultLanguage=isPreset?options.language:(languageOptions.indexOf('English')>=0?'English':languageOptions[0]);
  $('performLanguage').textContent='';languageOptions.forEach(function(value){option($('performLanguage'),value,languageOptionLabel(identity.engine_id,value));});
  $('performLanguage').value=!identityChanged&&languageOptions.indexOf(previousLanguage)>=0?previousLanguage:defaultLanguage;
  if(identityChanged){$('performDirection').value=options.default_direction||'';lastPerformanceIdentityId=identity.id;}
  var directionStatus=(schema.direction||{}).status||'unsupported';
  var directionSupported=['supported','worker_verified','experimental'].indexOf(directionStatus)>=0;
  var seeded=(schema.controls||[]).some(function(control){return control.id==='seed';});
  $('performSeed').parentElement.hidden=!seeded;$('variantCount').parentElement.hidden=!seeded;
  $('performDirectionWrap').hidden=!directionSupported;$('actingPreset').parentElement.hidden=!directionSupported;
  $('performSupport').textContent=directionStatus==='experimental'?'Delivery instruction is experimental for this checkpoint.':
    isPreset?'Checkpoint preset: '+options.speaker+' / '+$('performLanguage').value+'. No reference recording is used.':
    identity.engine_id==='turbo'?'Reference clips must exceed 5 seconds. Use [laugh], [chuckle], or [cough] tags in text.':
    directionSupported?'Delivery instruction is supported by this checkpoint.':
    identity.engine_id==='orig'?'Text direction is unsupported for this checkpoint.':
    identity.kind==='fixed_voice'?'Fixed speaker identity; no reference clone controls.':'Reference-cloned character identity.';
  renderPerformanceSettings(identity,schema);
  var native=identity.engine_id==='turbo'||identity.engine_id==='breeze-tts-2'||identity.engine_id==='drama-3'; $('nativeTags').hidden=!native;
  $('nativeTags').textContent='Insert model-native event: ';
  if(native){(identity.engine_id==='turbo'?['[laugh]','[chuckle]','[cough]']:identity.engine_id==='drama-3'?['[laughs]','[sighs]','[whispers]']:['(laugh)','(sigh)','(cough)']).forEach(function(tag){var b=document.createElement('button');b.type='button';b.textContent=tag;b.addEventListener('click',function(){insertTag(tag);});$('nativeTags').appendChild(b);});}
  loadPronunciations(character.id);
}
function renderPerformanceSettings(identity,schema){
  var container=$('performSettingsControls');container.textContent='';var controls=schema.controls||[];
  $('performSettingsPanel').hidden=!controls.length;
  controls.forEach(function(control){
    if(control.id==='seed')return;
    var label=document.createElement('label'),title=control.label+(control.status==='experimental'?' (experimental)':'');
    label.className='field compact';label.textContent=title;var input;
    if(control.type==='select'){
      input=document.createElement('select');(control.choices||[]).forEach(function(value){option(input,value,value);});input.value=control.default;
    }else{
      input=document.createElement('input');input.type='number';input.value=control.default;input.min=control.minimum;input.max=control.maximum;input.step=control.step;
    }
    input.dataset.settingKey=control.id;input.setAttribute('aria-label',title+' for '+identity.engine_id);
    label.appendChild(input);container.appendChild(label);
  });
}
function insertTag(tag){var field=$('performText'),a=field.selectionStart,b=field.selectionEnd;field.value=field.value.slice(0,a)+tag+field.value.slice(b);field.focus();field.selectionStart=field.selectionEnd=a+tag.length;}
function saveIdentityUpdate(changes) {
  var identity=activeIdentity();if(!identity){return Promise.reject(new Error('Choose a voice identity first.'));}
  return fetchJson('/api/identities/'+identity.id+'/update',changes);
}
function syncNoiseReductionFields(){$('noiseProfileFields').hidden=!$('noiseReductionEnabled').checked;$('noiseReductionStrengthValue').textContent=Number($('noiseReductionStrength').value).toFixed(2);}
function decodeReferenceQuality(value){if(!value)return null;try{return JSON.parse(atob(value.replace(/-/g,'+').replace(/_/g,'/')));}catch(error){return null;}}
function referenceQualityText(quality){if(!quality)return '';var text=' Quality '+quality.score+'/100, '+quality.verdict+'.';if(quality.warnings&&quality.warnings.length)text+=' '+quality.warnings.join(' ');return text;}
function encodeReferenceMetadata(metadata){return btoa(Array.from(new TextEncoder().encode(JSON.stringify(metadata)),function(byte){return String.fromCharCode(byte);}).join('')).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');}
function fillReferenceMetadata(){
  var target=Number($('referenceRate').value),edits={start_seconds:Number($('trimStart').value),end_seconds:Number($('trimEnd').value),
    target_sample_rate:target||referenceBuffer.sampleRate,channel:$('referenceChannel').value,
    noise_reduction:{enabled:$('noiseReductionEnabled').checked,profile_start_seconds:Number($('noiseProfileStart').value),
      profile_end_seconds:Number($('noiseProfileEnd').value),strength:Number($('noiseReductionStrength').value)}};
  return edits;
}
function saveUploadedReference(){
  var identity=activeIdentity();if(!identity||!referenceSourceFile){$('referenceStatus').textContent='Choose a source file or record audio first.';return;}
  var range;try{range=activeTrim();}catch(error){$('referenceStatus').textContent=error.message;return;}
  var analysis=bufferAnalysis(referenceBuffer);if(analysis.rms<0.001){$('referenceStatus').textContent='Source is silent/too quiet; record a clear passage.';return;}
  var metadata={filename:referenceSourceFile.name||'microphone-capture.wav',transcript:$('referenceTranscript').value,
    origin:$('referenceOrigin').value,allowed_use:$('referenceAllowedUse').value,edits:fillReferenceMetadata()};
  $('saveReference').disabled=true;$('referenceStatus').textContent='Saving the original audio and immutable processed reference version...';
  var encoded=encodeReferenceMetadata(metadata);
  fetch('/api/identities/'+identity.id+'/references',{method:'POST',headers:{'Content-Type':referenceSourceFile.type||'application/octet-stream','X-Reference-Metadata':encoded},body:referenceSourceFile})
    .then(function(r){return r.json().then(function(j){if(!r.ok)throw new Error(j.error||r.status);return j.reference;});})
    .then(function(ref){$('referenceStatus').textContent='Saved immutable v'+ref.version+' / '+ref.duration_seconds.toFixed(2)+' s / '+ref.sample_rate+' Hz / RMS '+ref.rms_dbfs.toFixed(1)+' dBFS'+(ref.warnings.length?' / '+ref.warnings.join(' '):'')+referenceQualityText(ref.quality);referenceSourceFile=null;return refreshCast(identity.character_id,identity.id);})
    .catch(function(error){$('referenceStatus').textContent=error.message;}).then(function(){$('saveReference').disabled=false;});
}
function saveEditedReferenceVersion(){
  var identity=activeIdentity(),previous=selectedReference(identity,$('referenceVersion'));if(!previous){return;}
  var body={transcript:$('referenceTranscript').value,origin:$('referenceOrigin').value,allowed_use:$('referenceAllowedUse').value};
  try{Object.assign(body,fillReferenceMetadata());}catch(error){$('referenceStatus').textContent=error.message;return;}
  fetchJson('/api/references/'+previous.id+'/versions',body).then(function(result){$('referenceStatus').textContent='Saved immutable v'+result.reference.version+'; the approved version remains unchanged until you approve this one.';return refreshCast(identity.character_id,identity.id);})
    .catch(function(error){$('referenceStatus').textContent=error.message;});
}
function beginMicCapture(){
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){$('referenceStatus').textContent='Microphone capture is not available in this browser.';return;}
  var revision=referenceLoadRevision;$('micStart').disabled=true;
  navigator.mediaDevices.getUserMedia({audio:{channelCount:2,echoCancellation:false,noiseSuppression:false,autoGainControl:false}}).then(function(stream){
    if(revision!==referenceLoadRevision){stream.getTracks().forEach(function(track){track.stop();});return;}
    micStream=stream;micContext=apiAudioContext();var source=micContext.createMediaStreamSource(stream);micProcessor=micContext.createScriptProcessor(4096,Math.min(2,source.channelCount),1);micChunks=[];
    micProcessor.onaudioprocess=function(event){var chunk=[];for(var c=0;c<event.inputBuffer.numberOfChannels;c++){chunk.push(new Float32Array(event.inputBuffer.getChannelData(c)));}micChunks.push(chunk);};
    var silent=micContext.createGain();silent.gain.value=0;source.connect(micProcessor);micProcessor.connect(silent);silent.connect(micContext.destination);
    $('micStart').hidden=true;$('micStop').hidden=false;$('referenceStatus').textContent='Recording locally. Select Stop recording when the spoken passage is complete.';
    castMicTimer=setTimeout(stopMicCapture,59000);
  }).catch(function(error){if(revision===referenceLoadRevision)$('referenceStatus').textContent='Microphone could not start: '+error.message;})
    .finally(function(){if(revision===referenceLoadRevision)$('micStart').disabled=false;});
}
function pcmCaptureWav(chunks, sampleRate){
  var channels=chunks.length?chunks[0].length:1,total=chunks.reduce(function(sum,item){return sum+item[0].length;},0),dataBytes=total*channels*2;
  var bytes=new ArrayBuffer(44+dataBytes),view=new DataView(bytes),pos=0;function write(s){for(var i=0;i<s.length;i++)view.setUint8(pos++,s.charCodeAt(i));}
  write('RIFF');view.setUint32(pos,36+dataBytes,true);pos+=4;write('WAVE');write('fmt ');view.setUint32(pos,16,true);pos+=4;view.setUint16(pos,1,true);pos+=2;view.setUint16(pos,channels,true);pos+=2;view.setUint32(pos,sampleRate,true);pos+=4;view.setUint32(pos,sampleRate*channels*2,true);pos+=4;view.setUint16(pos,channels*2,true);pos+=2;view.setUint16(pos,16,true);pos+=2;write('data');view.setUint32(pos,dataBytes,true);pos+=4;
  for(var frame=0;frame<total;frame++){var chunkIndex=0,offset=frame;while(offset>=chunks[chunkIndex][0].length){offset-=chunks[chunkIndex][0].length;chunkIndex++;}for(var ch=0;ch<channels;ch++){var sample=Math.max(-1,Math.min(1,chunks[chunkIndex][ch][offset]));view.setInt16(pos,sample<0?sample*32768:sample*32767,true);pos+=2;}}
  return new Blob([bytes],{type:'audio/wav'});
}
function stopMicCapture(){
  if(!micStream)return;
  clearTimeout(castMicTimer);castMicTimer=null;
  if(micProcessor){micProcessor.disconnect();micProcessor.onaudioprocess=null;}if(micStream){micStream.getTracks().forEach(function(track){track.stop();});}
  var wav=pcmCaptureWav(micChunks,micContext.sampleRate),file=new File([wav],'microphone-capture.wav',{type:'audio/wav'});
  micProcessor=null;micStream=null;micChunks=[];$('micStart').hidden=false;$('micStop').hidden=true;
  loadReferenceSource(wav,file).then(function(){previewReferenceClip();$('referenceStatus').textContent='Microphone capture is ready; enter the exact words, source, and allowed use before saving.';})
    .catch(function(error){$('referenceStatus').textContent='Could not prepare captured audio: '+error.message;});
}
var takeCheckLabels={silent_audio:'Silent',clipping:'Possible clipping',long_pause:'Long pause inside the take',duration_suspiciously_short:'Shorter than its text'};
function appendTakeChecks(parent,checks){
  var note=document.createElement('p');note.className='meta take-checks';
  if(!checks||checks.status==='not_recorded'){note.textContent='Waveform checks were not recorded for this take.';}
  else if(checks.status==='unavailable'){note.className+=' flagged';note.textContent='Waveform checks could not run: '+(checks.error||'unknown error')+'.';}
  else if(!checks.flags.length){note.textContent='Automatic waveform checks found no flags. They do not judge the words, identity or acting.';}
  else{note.className+=' flagged';note.textContent='Automatic waveform checks flagged: '+checks.flags.map(function(flag){
    return flag==='long_pause'?takeCheckLabels[flag]+' ('+checks.longest_pause_seconds.toFixed(1)+' s)':(takeCheckLabels[flag]||flag);}).join('; ')+'. Listen before selecting.';}
  parent.appendChild(note);
}
var loudnessNotes={silent:'No loudness reading (the take is silent)',too_short:'No loudness reading (under 0.4 s is too short)',too_quiet:'No loudness reading (too quiet)'};
function appendTakeLoudness(parent,loudness){
  var note=document.createElement('p');note.className='meta take-checks';
  if(!loudness||loudness.status==='not_measured'){note.textContent='Loudness has not been measured for this take yet.';}
  else if(loudness.status==='unavailable'){note.textContent='Loudness could not be measured: '+(loudness.reason||'unknown error')+'.';}
  else{
    var parts=[loudness.integrated_lufs===null?(loudnessNotes[loudness.note]||'No loudness reading'):'Loudness '+loudness.integrated_lufs.toFixed(1)+' LUFS'];
    if(loudness.sample_peak_dbfs!==null)parts.push('sample peak '+loudness.sample_peak_dbfs.toFixed(1)+' dBFS');
    if(loudness.true_peak_dbtp!==null)parts.push('true peak '+loudness.true_peak_dbtp.toFixed(1)+' dBTP');
    note.textContent=parts.join(' · ')+'.';
    if(loudness.true_peak_dbtp!==null&&loudness.true_peak_dbtp>-1)note.textContent+=' The true peak is above -1 dBTP, so MP3, AAC or Ogg export can clip.';
  }
  parent.appendChild(note);
}
function retirementLabel(item){
  var state=item.retirement;
  if(!state)return '';
  return state.reason+(state.inherited?' It is retired because '+state.source_label+' is retired.':'');
}
function retireAction(kind,id,label){
  if(!window.confirm('Retire '+label+'? New performances, scene rows, comparisons, conversions and exports that use it are blocked. Nothing is deleted, and you can restore it.'))return Promise.resolve(null);
  return fetchJson('/api/retirements',{kind:kind,id:id,reason:''}).then(function(result){
    var parts=[];
    if(result.affected.reference)parts.push(result.affected.reference+' recording version(s)');
    if(result.affected.take)parts.push(result.affected.take+' take(s)');
    return 'Retired '+label+'. Blocked with it: '+(parts.join(', ')||'nothing else')+'. Listening stays available for review.';
  });
}
function restoreAction(kind,id,label){
  return fetchJson('/api/retirements/restore',{kind:kind,id:id}).then(function(){return 'Restored '+label+'. Restoring never re-approves a voice.';});
}
function purgeAction(kind,id,label){
  return fetchJson('/api/purges/preview?kind='+encodeURIComponent(kind)+'&id='+encodeURIComponent(id)).then(function(preview){
    var counts=preview.would_purge,parts=[];
    ['identity','reference','take'].forEach(function(name){if(counts[name])parts.push(counts[name]+' '+name+(counts[name]===1?'':'s'));});
    if(!window.confirm('Permanently delete '+label+'? This removes '+parts.join(', ')+' and their audio from Cast. It cannot be undone.'))return null;
    return fetchJson('/api/purges',{kind:kind,id:id,confirm:true}).then(function(report){
      var text='Purged '+parts.join(', ')+'.';
      if(report.external_copies.length)text+=' Copies outside Cast still hold this audio: '+report.external_copies.join(', ')+(report.external_copies_truncated?' (list truncated)':'')+'. Cast does not delete them; remove them yourself.';
      return text;
    });
  });
}
function retirementButton(label,action,after,show){
  var button=document.createElement('button');button.type='button';button.textContent=label;
  button.addEventListener('click',function(){
    button.disabled=true;
    Promise.resolve().then(action).then(function(message){
      if(message===null)return;
      show(message);
      return after();
    }).catch(function(error){show(error.message);}).then(function(){button.disabled=false;});
  });
  return button;
}
function appendTakeRetirement(article,take,after,show){
  var row=document.createElement('div');row.className='cast-actions';
  var note=document.createElement('p');note.className='meta take-checks';
  if(take.retired){
    note.className+=' flagged';note.textContent=retirementLabel(take)+' New use is blocked; listening stays available for review.';
    if(!take.retirement.inherited)row.appendChild(retirementButton('Restore take',function(){return restoreAction('take',take.id,'this take');},after,show));
    row.appendChild(retirementButton('Purge take…',function(){return purgeAction('take',take.id,'this take');},after,show));
  }else row.appendChild(retirementButton('Retire take',function(){return retireAction('take',take.id,'this take');},after,show));
  article.appendChild(note);article.appendChild(row);
}
function selectedReferenceVersion(){
  var identity=activeIdentity();
  return identity&&(identity.references||[]).find(function(ref){return ref.id===$('referenceVersion').value;})||null;
}
function updateReferenceRetirement(){
  var ref=selectedReferenceVersion();
  $('retireReference').hidden=!ref||ref.retired;
  $('restoreReference').hidden=!ref||!ref.retired||!!(ref.retirement&&ref.retirement.inherited);
  $('purgeReference').hidden=!ref||!ref.retired;
  $('referenceRetirement').textContent=ref&&ref.retired?retirementLabel(ref):'';
}
function renderTakes(takes){
  var container=$('takeList');container.textContent='';if(!takes.length){container.textContent='No saved takes for this line yet.';return;}
  takes.slice().reverse().forEach(function(take){var article=document.createElement('article'),title=document.createElement('strong');title.textContent=take.character_name+' / '+take.identity_label+' / '+take.line_id+' / '+take.status+' / '+take.duration_seconds.toFixed(2)+' s';article.appendChild(title);
    var player=document.createElement('audio');player.controls=true;player.src=take.audio_url;article.appendChild(player);
    var detail=document.createElement('p');detail.className='meta';detail.textContent='Text sent: '+take.spoken_text+'\n'+take.model.backend+' / '+take.model.checkpoint+' / revision '+take.model.checkpoint_revision+' / '+(take.model.mode||'')+(take.edit?'\n'+takeEditNote(take):'');article.appendChild(detail);appendTakeChecks(article,take.waveform_checks);appendTakeLoudness(article,take.loudness);
    var provenance=document.createElement('details'),provenanceTitle=document.createElement('summary'),provenanceBody=document.createElement('pre');
    provenanceTitle.textContent='Effective settings and provenance';provenanceBody.className='meta';
    provenanceBody.textContent=JSON.stringify({effective_settings:take.model.effective_settings||{},voice_identity:take.model.voice_identity||null,
      reference_version_id:take.reference_version_id||null,provenance:take.provenance||{}},null,2);
    provenance.appendChild(provenanceTitle);provenance.appendChild(provenanceBody);article.appendChild(provenance);
    var actions=document.createElement('div');actions.className='cast-actions';[['selected','Select'],['candidate','Keep as candidate'],['rejected','Reject']].forEach(function(pair){var button=document.createElement('button');button.type='button';button.textContent=pair[1];button.addEventListener('click',function(){fetchJson('/api/takes/'+take.id+'/update',{status:pair[0],rating:pair[0]==='selected'?5:null}).then(loadCurrentTakes).catch(function(error){$('performError').textContent=error.message;});});actions.appendChild(button);});
    var newReference=document.createElement('button');newReference.type='button';newReference.textContent='Create new reference version from this take';newReference.addEventListener('click',function(){var identity=performanceIdentity();if(!identity||identity.id!==take.identity_id||identity.kind!=='reference'){ $('performError').textContent='Choose this take’s reference voice in Perform first.';return;}fetchJson('/api/identities/'+identity.id+'/references/from-take',{take_id:take.id}).then(function(result){$('performError').textContent='Created reference v'+result.reference.version+' with inherited use restrictions.';return refreshCast(identity.character_id,identity.id);}).catch(function(error){$('performError').textContent=error.message;});});actions.appendChild(newReference);var voiceMatch=document.createElement('button');voiceMatch.type='button';voiceMatch.textContent='Check voice match';var matchNote=document.createElement('p');matchNote.className='meta';voiceMatch.addEventListener('click',function(){matchNote.textContent='Measuring the voice match…';fetchJson('/api/takes/'+take.id+'/voice-match').then(function(result){var m=result.voice_match;matchNote.textContent=m.available?('Voice match (proxy) '+m.score.toFixed(2)+': '+({likely_same:'likely the reference voice',uncertain:'uncertain',likely_different:'likely a different voice'}[m.band])+'. Calibrated on one voice; this is not a listening test.'):m.reason;}).catch(function(error){matchNote.textContent=error.message;});});actions.appendChild(voiceMatch);article.appendChild(actions);article.appendChild(matchNote);appendTakeEditor(article,take,actions,loadCurrentTakes,function(message){$('performStatus').textContent=message;});if(take.retired)Array.from(actions.querySelectorAll('button')).forEach(function(b){if(b.textContent!=='Check voice match')b.disabled=true;});appendTakeRetirement(article,take,loadCurrentTakes,function(message){$('performStatus').textContent=message;});container.appendChild(article);});
}
var takeLoadRevision=0;
function loadCurrentTakes(){
  var request=++takeLoadRevision,identity=performanceIdentity(),line=$('lineId').value||'';
  $('takeList').textContent=identity?'Loading saved takes…':'';
  if(!identity){renderTakes([]);return Promise.resolve();}
  var query=new URLSearchParams({identity_id:identity.id,line_id:line});
  return fetchJson('/api/takes?'+query.toString()).then(function(result){
    var current=performanceIdentity();
    if(request===takeLoadRevision&&current&&current.id===identity.id&&($('lineId').value||'')===line)renderTakes(result.takes||[]);
  });
}
function performanceSettings(identity, seed){
  var schema=performanceCapabilitiesByEngine[identity.engine_id]||{controls:[]},settings={};
  (schema.controls||[]).forEach(function(control){
    if(control.id==='seed'){settings.seed=Number(seed);return;}
    var input=$('performSettingsControls').querySelector('[data-setting-key="'+control.id+'"]');
    var value=input?input.value:control.default;
    settings[control.id]=control.type==='select'?value:Number(value);
  });
  return settings;
}
function generatePerformanceSet(){
  var identity=performanceIdentity();if(!identity){$('performError').textContent='Choose an identity.';return;}
  var count=Math.max(1,Math.min(8,Math.floor(Number($('variantCount').value))||1)),seed=Number.isFinite(Number($('performSeed').value))?Number($('performSeed').value):42;
  var settings=performanceSettings(identity,seed);
  if(!Object.prototype.hasOwnProperty.call(settings,'seed'))count=1;
  var base={identity_id:identity.id,line_id:$('lineId').value,text:$('performText').value,language:$('performLanguage').value,
    direction:$('performDirection').value,reference_version_id:$('performReference').value||null,use_pronunciations:$('usePronunciations').checked,
    settings:settings};
  var run={count:count,index:1,done:0,started:performance.now(),stopRequested:false};performRun=run;
  $('performGenerate').disabled=true;$('performStop').hidden=count<2;$('performError').textContent='';
  var ticker=setInterval(function(){$('performStatus').textContent=performStatusText(run);},500);
  function next(i){
    if(i>=count||run.stopRequested)return Promise.resolve();
    run.index=i+1;run.started=performance.now();$('performStatus').textContent=performStatusText(run);
    var candidateSettings=Object.assign({},base.settings);
    if(Object.prototype.hasOwnProperty.call(candidateSettings,'seed'))candidateSettings.seed=(seed+i)%2147483648;
    var body=Object.assign({},base,{settings:candidateSettings});
    return fetch('/api/performance',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      .then(function(response){if(!response.ok)return response.json().then(function(j){throw new Error(j.error||response.status);});return response.blob();})
      .then(function(){run.done++;return loadCurrentTakes();}).then(function(){return next(i+1);});
  }
  next(0).then(function(){$('performStatus').textContent=run.done<count?'Stopped after '+run.done+' of '+count+' takes. The takes already made are saved.':'Candidate set complete; each take has its own WAV and model/reference provenance.';})
    .catch(function(error){$('performError').textContent=error.message+' Previously completed takes remain saved.';$('performStatus').textContent='Generation stopped before the set completed.';})
    .then(function(){clearInterval(ticker);if(performRun===run)performRun=null;$('performStop').hidden=true;$('performGenerate').disabled=false;});
}
var performRun=null;
function performStatusText(run){
  return 'Generating candidate '+run.index+' of '+run.count+(run.count>1?' in sequence':'')+'... '+Math.round((performance.now()-run.started)/1000)+' s'
    +(run.stopRequested?' (stopping after this take)':'');
}
function stopPerformanceSet(){
  if(performRun&&!performRun.stopRequested){performRun.stopRequested=true;$('performStatus').textContent=performStatusText(performRun);}
}
function loadPronunciations(characterId){fetchJson('/api/pronunciations?character_id='+encodeURIComponent(characterId)).then(function(result){$('pronunciationList').textContent=(result.pronunciations||[]).map(function(item){return item.source+' → '+item.replacement;}).join(' · ')||'No pronunciation entries yet.';});}
