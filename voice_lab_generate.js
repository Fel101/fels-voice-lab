// Voice Lab generation: requests, the history list and streamed audio playback.
function bodyForRequest() {
  var engine = $('engine').value;
  /** @type {Record<string, any>} */
  var body = {engine:engine, text:$('text').value, voice:selectedValues($('voice')).join(','),
    language:$('kokoroLanguage').value, speed:$('speed').value, clone:$('clone').checked};
  var qwenMode = qwenModeFor(engine);
  if ((engine === 'orig' || engine === 'turbo') && body.clone) body.reference_path = $('chatterboxReference').value;
  if (qwenMode) {
    body = {engine:engine, mode:qwenMode, text:$('text').value, language:$('qwenLanguage').value,
      seed:Number($('qwenSeed').value), max_new_tokens:Number($('qwenMaxTokens').value),
      temperature:Number($('qwenTemperature').value), top_k:Number($('qwenTopK').value),
      top_p:Number($('qwenTopP').value), repetition_penalty:Number($('qwenRepetition').value)};
    if (qwenMode === 'design') {
      body.voice_description = $('qwenDescription').value;
      if ($('qwenSaveVoice').checked) { body.save_voice_as = $('qwenVoiceAssetName').value.trim(); }
    }
    if (qwenMode === 'clone') {
      body.reference_path = $('qwenReference').value; body.reference_text = $('qwenReferenceText').value;
      body.xvec_only = $('qwenXvecOnly').checked; body.direction = $('qwenCloneDirection').value;
    }
    if (qwenMode === 'custom_voice') { body.speaker = $('qwenSpeaker').value; body.direction = $('qwenCustomDirection').value; }
    return body;
  }
  if (engine === 'drama-3') {
    var dramaMode=$('dramaMode').value;
    return {engine:engine,text:$('text').value,mode:dramaMode,seed:Number($('dramaSeed').value),
      temperature:Number($('dramaTemperature').value),top_k:Number($('dramaTopK').value),top_p:Number($('dramaTopP').value),
      max_seconds:Number($('dramaMaxSeconds').value),direction:$('dramaDirection').value,
      voice_description:dramaMode==='design'?$('dramaDescription').value:'',
      reference_path:dramaMode==='clone'?$('dramaReference').value:null,reference_text:dramaMode==='clone'?$('dramaTranscript').value:''};
  }
  if (engine === 'phonon-31m-en') {
    return {engine:engine, text:$('text').value, voice:$('phononVoice').value, seed:Number($('phononSeed').value),
      temperature:Number($('phononTemperature').value), duration_delta:Number($('phononPace').value)};
  }
  if (engine === 'voxcpm2') {
    var voxMode = $('voxMode').value;
    body = {engine:engine, mode:voxMode, text:$('text').value, seed:Number($('voxSeed').value),
      cfg_value:Number($('voxCfg').value), inference_timesteps:Number($('voxSteps').value)};
    if (voxMode === 'design') {
      body.voice_description = $('voxDescription').value;
      if ($('voxSaveVoice').checked) { body.save_voice_as = $('voxVoiceAssetName').value.trim(); }
    } else if (voxMode === 'clone') {
      body.reference_path = $('voxReference').value; body.direction = $('voxDirection').value;
    } else {
      body.reference_path = $('voxPromptReference').value; body.prompt_path = $('voxPromptReference').value;
      body.prompt_text = $('voxPromptText').value;
    }
    return body;
  }
  if (engine === 'orig') {
    body.exaggeration=Number($('exag').value); body.cfg_weight=Number($('cfg').value);
    body.temperature=Number($('cbTemperature').value); body.top_p=Number($('cbTopP').value);
    body.repetition_penalty=Number($('cbRepetition').value); body.min_p=Number($('cbMinP').value);
  } else if (engine === 'turbo') {
    body.temperature=Number($('cbTemperature').value); body.top_k=Number($('cbTopK').value);
    body.top_p=Number($('cbTopP').value); body.repetition_penalty=Number($('cbRepetition').value);
  }
  if (engine === 'breeze-tts-2') {
    body.mode = $('mode').value; body.profile = $('profile').value; body.seed = Number($('seed').value);
    body.voice_description = $('voiceDescription').value; body.direction = $('direction').value;
    body.reference_path = $('reference').value; body.reference_text = $('referenceText').value;
    body.cfg_scale = Number($('cfgScale').value); body.max_new_tokens = Number($('maxTokens').value);
    body.temperature = Number($('temperature').value); body.top_k = Number($('topK').value); body.top_p = Number($('topP').value);
    body.repetition_penalty = Number($('repetitionPenalty').value); body.depth_temperature = Number($('depthTemperature').value);
    body.depth_top_k = Number($('depthTopK').value); body.depth_top_p = Number($('depthTopP').value);
    if ($('saveVoice').checked) { body.save_voice_as = $('voiceAssetName').value.trim(); }
  }
  return body;
}
function makeHistory(blob, body, meta) {
  var h = $('hist'); if (!h.querySelector('article')) { h.textContent = ''; }
  var url = URL.createObjectURL(blob), row = document.createElement('article'), title = document.createElement('div'); title.className = 'title';
  row.dataset.audioUrl=url;
  var desc = document.createElement('span'); desc.textContent = LabText.t('studio.seed_2', {model: body.engine + (body.mode ? ' / ' + body.mode : ''), seed: body.seed === undefined ? '—' : body.seed, dialogue: body.text.slice(0,70)});
  var download = document.createElement('a'); download.href = url; download.download = body.engine + '-' + (body.mode || 'speech') + '-seed-' + (body.seed === undefined ? Date.now() : body.seed) + '.wav'; download.textContent = LabText.t('studio.save_wav');
  title.appendChild(desc); title.appendChild(download); row.appendChild(title);
  var audio = document.createElement('audio'); audio.controls = true; audio.src = url; row.appendChild(audio);
  var saveVoiceButton=document.createElement('button');saveVoiceButton.type='button';saveVoiceButton.textContent=LabText.t('studio.save_as_a_reusable_cast');
  saveVoiceButton.addEventListener('click',function(){VoiceLabWorkflows.openFromClip(blob,body);});row.appendChild(saveVoiceButton);
  var note = document.createElement('div'); note.className = 'meta'; note.textContent = meta; row.appendChild(note); h.insertBefore(row,h.firstChild);
  while(h.children.length>20){var old=h.lastElementChild;if(old.dataset.audioUrl)URL.revokeObjectURL(old.dataset.audioUrl);old.remove();}
  $('player').src = url;
}
var activeRequest = null;
function stopWaiting() {
  if (activeRequest) { activeRequest.stopped = true; activeRequest.controller.abort(); }
}
function generate() {
  if ($('engine').value === 'kokoro' && !$('voice').options.length) {
    // A page that was open while Kokoro was installed has no voices to send. Read them again, once, then speak.
    $('go').disabled = true; $('reroll').disabled = true;
    $('status').textContent = LabText.t('studio.reading_the_installed_kokoro_voices');
    VoiceLabWorkflows.refreshKokoroVoices().catch(function () {}).then(function () {
      $('go').disabled = false; $('reroll').disabled = false;
      requestSpeech();
    });
    return;
  }
  if ($('engine').value === 'phonon-31m-en' && !phononVoices.length) {
    // Same for Phonon: read the voices it installed, then speak (an empty list is refused with the install hint).
    $('go').disabled = true; $('reroll').disabled = true;
    $('status').textContent = LabText.t('studio.reading_the_installed_phonon_voices');
    refreshPhononVoices().catch(function () {}).then(function () {
      $('go').disabled = false; $('reroll').disabled = false;
      requestSpeech();
    });
    return;
  }
  requestSpeech();
}
function requestSpeech() {
  var body = bodyForRequest();
  if((body.engine==='orig'||body.engine==='turbo')&&body.clone&&!body.reference_path){$('err').textContent=LabText.t('studio.choose_a_voice_recording_or');return;}
  var historyBody=VoiceLabWorkflows.freezeRequest(body);
  $('go').disabled = true; $('reroll').disabled = true; $('stopWaiting').hidden = false; $('err').textContent = '';
  var started = performance.now(), request = {controller:new AbortController(), stopped:false};
  activeRequest = request;
  function waiting() {
    $('status').textContent = LabText.t('studio.generating_s_the_first_request', {seconds: Math.round((performance.now() - started) / 1000)});
  }
  waiting();
  var ticker = setInterval(waiting, 500);
  fetch('/api/speak', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    signal:request.controller.signal}).then(function (r) {
    if (!r.ok) { return r.json().then(function (j) { throw new Error(j.error || r.status); }); }
    var meta = LabText.t('studio.generation_timing', {
      audio_seconds: r.headers.get('X-Audio-Seconds'), generation_seconds: r.headers.get('X-Gen-Seconds')
    });
    var actual = r.headers.get('X-Decode-Profile-Effective');
    if (actual) { meta += ' / ' + LabText.t('studio.decode_profiles', {requested: r.headers.get('X-Decode-Profile') || '', effective: actual}); }
    var mode = r.headers.get('X-Generation-Mode'); if (mode) { meta += ' / ' + mode; }
    var backend = r.headers.get('X-Voice-Backend'); if (backend) { meta += ' / ' + backend; }
    var checkpoint = r.headers.get('X-Model-Checkpoint'); if (checkpoint) { meta += ' / ' + checkpoint; }
    var revision = r.headers.get('X-Checkpoint-Revision'); if (revision) { meta += LabText.t('studio.revision', {revision: revision}); }
    var sampling = r.headers.get('X-Effective-Sampling-Settings'); if (sampling) { meta += LabText.t('studio.effective_sampling', {sampling: sampling}); }
    // Leave one-letter model codes out. The language picker already shows the selected language.
    var language = r.headers.get('X-Voice-Language');
    if (language && language.length > 1) { meta += ' / ' + language; }
    var speaker = r.headers.get('X-Voice-Speaker'); if (speaker) { meta += ' / ' + speaker; }
    var cacheHit = r.headers.get('X-Reference-Cache-Hit'); if (cacheHit) { meta += LabText.t('studio.voice_recording_cached', {cache_hit: cacheHit}); }
    var savedAsset = r.headers.get('X-Voice-Asset-Path');
    if (savedAsset) { meta += LabText.t('studio.saved_voice_recording_2', {recording_path: savedAsset}); }
    return r.blob().then(function (blob) { return {blob:blob, meta:meta, savedAsset:savedAsset}; });
  }).then(function (result) {
    clearInterval(ticker);
    makeHistory(result.blob, historyBody, result.meta);
    $('status').textContent = LabText.t('studio.round_trip_s', {meta: result.meta, seconds: ((performance.now()-started)/1000).toFixed(1)});
    var historySaved = persistHistory(result.blob, historyBody, result.meta).catch(function (error) {
      $('status').textContent += LabText.t('studio.local_history_was_not_saved', {message: error.message});
    });
    if (result.savedAsset) {
      $('saveVoice').checked = false;
      $('voxSaveVoice').checked = false;
      $('referenceText').value = body.text;
      $('qwenSaveVoice').checked = false;
      $('qwenReferenceText').value = body.text;
      if (body.engine === 'voxcpm2') { $('voxPromptText').value = body.text; }
      loadReferences(result.savedAsset);
    }
    return historySaved;
  }).catch(function (e) {
    clearInterval(ticker);
    if (request.stopped) {
      $('status').textContent = LabText.t('studio.stopped_waiting_if_the_model', {saved_voice_notice: body.save_voice_as ? LabText.t('studio.reusable_voice_still_saved') : ''});
    } else { $('err').textContent = e.message; $('status').textContent = ''; }
  }).then(function () {
    clearInterval(ticker);
    if (activeRequest === request) { activeRequest = null; }
    $('stopWaiting').hidden = true; $('go').disabled = false; $('reroll').disabled = false;
  });
}

function wavHeader(sampleRate, sampleCount) {
  var dataBytes = sampleCount * 2, buffer = new ArrayBuffer(44), view = new DataView(buffer);
  function text(offset, value) { for (var i=0;i<value.length;i++) { view.setUint8(offset+i,value.charCodeAt(i)); } }
  text(0,'RIFF'); view.setUint32(4,36+dataBytes,true); text(8,'WAVE'); text(12,'fmt ');
  view.setUint32(16,16,true); view.setUint16(20,1,true); view.setUint16(22,1,true);
  view.setUint32(24,sampleRate,true); view.setUint32(28,sampleRate*2,true);
  view.setUint16(32,2,true); view.setUint16(34,16,true); text(36,'data'); view.setUint32(40,dataBytes,true);
  return buffer;
}
function decodePcm16(encoded) {
  var binary = atob(encoded), bytes = new Uint8Array(binary.length);
  for (var i=0;i<binary.length;i++) { bytes[i] = binary.charCodeAt(i); }
  return bytes;
}
function stopStreamPlayback(state, showStatus) {
  if (!state || state.cancelled) { return; }
  state.cancelled = true;
  if (showStatus !== false) { $('status').textContent=LabText.t('studio.stream_cancelled_pending_playback_cleared'); }
  if (state.controller) { state.controller.abort(); }
  if (state.sources) {
    state.sources.forEach(function (source) { try { source.onended = null; source.stop(0); } catch (_) {} });
    state.sources.clear();
  }
  if (state.audioContext && state.audioContext.state !== 'closed') {
    state.audioContext.close().catch(function () {});
  }
}
function streamGenerate() {
  if (activeStream) { return; }
  var body = bodyForRequest(), AudioContextCtor = window.AudioContext || window.webkitAudioContext;
  var historyBody=VoiceLabWorkflows.freezeRequest(body);
  if (!AudioContextCtor) { $('err').textContent = LabText.t('studio.this_browser_does_not_support'); return; }
  var state = {controller:new AbortController(), audioContext:null, sources:new Set(), cancelled:false,
    nextStart:0, sampleCount:0, expectedSequence:0, sampleRate:null, pcmParts:[],
    gapCount:0, gapSeconds:0, firstAudioMs:null, playbackStartMs:null, startEvent:null, complete:null};
  activeStream = state;
  ['go','reroll','stream','load','unload'].forEach(function(id){$(id).disabled=true;});
  $('stopStream').hidden=false; $('err').textContent='';
  $('status').textContent=LabText.t('studio.loading_the_selected_qwen_checkpoint');
  var requestStarted = performance.now();
  function waitForBufferRoom() {
    return new Promise(/** @param {(value?: any) => void} resolve */ function(resolve) {
      function check() {
        if (state.cancelled || !state.audioContext || state.nextStart-state.audioContext.currentTime <= 1.5) { resolve(); }
        else { setTimeout(check,20); }
      }
      check();
    });
  }
  function handleEvent(event) {
    if (event.type === 'start') {
      state.startEvent=event; state.sampleRate=event.sample_rate;
      $('status').textContent=LabText.t('studio.native_qwen_generator_ready_service', {first_byte_seconds: Number(event.service_first_byte_s).toFixed(2)});
      return Promise.resolve();
    }
    if (event.type === 'error') { throw new Error(event.error || LabText.t('studio.streaming_generation_failed')); }
    if (event.type === 'complete') { state.complete=event; return Promise.resolve(); }
    if (event.type !== 'audio') { throw new Error(LabText.t('studio.unknown_streaming_event', {type: event.type})); }
    return waitForBufferRoom().then(function(){
      if (state.cancelled) { return; }
      if (event.sample_format !== 'pcm_s16le' || event.sequence !== state.expectedSequence ||
          event.sample_start !== state.sampleCount || event.sample_rate !== state.sampleRate) {
        throw new Error(LabText.t('studio.the_stream_contains_a_discontinuous'));
      }
      var bytes=decodePcm16(event.data), count=event.sample_count;
      if (bytes.length !== count*2 || !count) { throw new Error(LabText.t('studio.the_stream_chunk_byte_count')); }
      if (state.sampleCount===0) { state.firstAudioMs=performance.now()-requestStarted; }
      var now=state.audioContext.currentTime, startAt=Math.max(now+0.015,state.nextStart);
      if (state.sampleCount>0 && startAt-state.nextStart>0.025) {
        state.gapCount++; state.gapSeconds+=startAt-state.nextStart;
      }
      var audioBuffer=state.audioContext.createBuffer(1,count,event.sample_rate), channel=audioBuffer.getChannelData(0);
      var view=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength);
      for (var i=0;i<count;i++) { channel[i]=view.getInt16(i*2,true)/32768; }
      var source=state.audioContext.createBufferSource(); source.buffer=audioBuffer; source.connect(state.audioContext.destination);
      state.sources.add(source); source.onended=function(){state.sources.delete(source);}; source.start(startAt);
      if (state.playbackStartMs===null) {
        var outputLatency=(state.audioContext.baseLatency||0)+(state.audioContext.outputLatency||0);
        state.playbackStartMs=performance.now()+Math.max(0,startAt-now)*1000+outputLatency*1000;
      }
      state.nextStart=startAt+count/event.sample_rate;
      state.pcmParts.push(bytes); state.sampleCount+=count; state.expectedSequence++;
      $('status').textContent=LabText.t('studio.native_qwen_streaming_chunks_s', {chunks: state.expectedSequence, audio_seconds: (state.sampleCount/state.sampleRate).toFixed(2), gaps: state.gapCount});
    });
  }
  function drainLines(text, final) {
    state.textBuffer=(state.textBuffer||'')+text;
    var lines=state.textBuffer.split('\n'); state.textBuffer=lines.pop();
    var chain=Promise.resolve();
    lines.forEach(function(line){if(line.trim()){chain=chain.then(function(){return handleEvent(JSON.parse(line));});}});
    if(final&&state.textBuffer.trim()){var tail=state.textBuffer;state.textBuffer='';chain=chain.then(function(){return handleEvent(JSON.parse(tail));});}
    return chain;
  }
  (async function(){
    state.audioContext=new AudioContextCtor(); await state.audioContext.resume();
    var response=await fetch('/api/stream',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body),signal:state.controller.signal});
    if(!response.ok){var failure=await response.json();throw new Error(failure.error||response.status);}
    if(!response.body||!response.body.getReader){throw new Error(LabText.t('studio.this_browser_cannot_read_a'));}
    var reader=response.body.getReader(), decoder=new TextDecoder(), done=false;
    while(!done&&!state.cancelled){var part=await reader.read();done=part.done;
      await drainLines(decoder.decode(part.value||new Uint8Array(),{stream:!done}),done);}
    if(state.cancelled){return;}
    if(!state.complete){throw new Error(LabText.t('studio.the_stream_ended_without_a'));}
    if(state.complete.sample_count!==state.sampleCount||state.complete.chunk_count!==state.expectedSequence){
      throw new Error(LabText.t('studio.the_completion_record_does_not'));}
    while(!state.cancelled&&state.audioContext.currentTime<state.nextStart){await new Promise(function(resolve){setTimeout(resolve,25);});}
    if(state.cancelled){return;}
    var wav=new Blob([wavHeader(state.sampleRate,state.sampleCount)].concat(state.pcmParts),{type:'audio/wav'});
    var completion=state.complete, meta=LabText.t('studio.native_faster_qwen_generator_generator', {first_chunk_seconds: Number(completion.generator_first_chunk_s).toFixed(2), first_byte_seconds: Number(completion.service_first_byte_s).toFixed(2), first_audio_seconds: Number(completion.service_first_audio_s).toFixed(2), playback_seconds: ((state.playbackStartMs-requestStarted)/1000).toFixed(2), gaps: state.gapCount, gap_seconds: state.gapSeconds.toFixed(2), audio_seconds: Number(completion.audio_seconds).toFixed(2), generation_seconds: Number(completion.generation_total_s).toFixed(2), chunk_count: completion.chunk_count});
    if(completion.backend){meta+=' / '+completion.backend;}
    if(completion.checkpoint){meta+=' / '+completion.checkpoint;}
    if(completion.reference_cache_hit!==null&&completion.reference_cache_hit!==undefined){meta+=' / voice recording cached: '+completion.reference_cache_hit;}
    if(completion.saved_asset){meta+=' / saved voice recording: '+completion.saved_asset;}
    makeHistory(wav,historyBody,meta); $('status').textContent=meta;
    persistHistory(wav,historyBody,meta).catch(function(error){$('status').textContent+=LabText.t('studio.local_history_was_not_saved', {message: error.message});});
    if(completion.saved_asset){$('qwenSaveVoice').checked=false;$('qwenReferenceText').value=body.text;loadReferences(completion.saved_asset);}
  })().catch(function(error){
    if(error.name==='AbortError'||state.cancelled){$('status').textContent=LabText.t('studio.stream_cancelled_pending_playback_cleared');}
    else {$('err').textContent=error.message;$('status').textContent='';stopStreamPlayback(state,false);}
  }).then(function(){
    if(state.audioContext&&state.audioContext.state!=='closed'){state.audioContext.close().catch(function(){});}
    if(activeStream===state){activeStream=null;}
    $('stopStream').hidden=true;
    ['go','reroll','stream','load','unload'].forEach(function(id){$(id).disabled=false;});
  });
}
