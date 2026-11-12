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
  var desc = document.createElement('span'); desc.textContent = body.engine + (body.mode ? ' / ' + body.mode : '') + ' / seed ' + (body.seed === undefined ? '—' : body.seed) + ': ' + body.text.slice(0,70);
  var download = document.createElement('a'); download.href = url; download.download = body.engine + '-' + (body.mode || 'speech') + '-seed-' + (body.seed === undefined ? Date.now() : body.seed) + '.wav'; download.textContent = 'Save WAV';
  title.appendChild(desc); title.appendChild(download); row.appendChild(title);
  var audio = document.createElement('audio'); audio.controls = true; audio.src = url; row.appendChild(audio);
  var saveVoiceButton=document.createElement('button');saveVoiceButton.type='button';saveVoiceButton.textContent='Save as a reusable Cast voice';
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
    $('status').textContent = 'Reading the installed Kokoro voices...';
    VoiceLabWorkflows.refreshKokoroVoices().catch(function () {}).then(function () {
      $('go').disabled = false; $('reroll').disabled = false;
      requestSpeech();
    });
    return;
  }
  if ($('engine').value === 'phonon-31m-en' && !phononVoices.length) {
    // Same for Phonon: read the voices it installed, then speak (an empty list is refused with the install hint).
    $('go').disabled = true; $('reroll').disabled = true;
    $('status').textContent = 'Reading the installed Phonon voices...';
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
  if((body.engine==='orig'||body.engine==='turbo')&&body.clone&&!body.reference_path){$('err').textContent='Choose a reference recording, or turn cloning off to use the default speaker.';return;}
  var historyBody=VoiceLabWorkflows.freezeRequest(body);
  $('go').disabled = true; $('reroll').disabled = true; $('stopWaiting').hidden = false; $('err').textContent = '';
  var started = performance.now(), request = {controller:new AbortController(), stopped:false};
  activeRequest = request;
  function waiting() {
    $('status').textContent = 'Generating... ' + Math.round((performance.now() - started) / 1000) + ' s. The first request loads and warms the selected model.';
  }
  waiting();
  var ticker = setInterval(waiting, 500);
  fetch('/api/speak', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    signal:request.controller.signal}).then(function (r) {
    if (!r.ok) { return r.json().then(function (j) { throw new Error(j.error || r.status); }); }
    var meta = r.headers.get('X-Audio-Seconds') + ' s audio / ' + r.headers.get('X-Gen-Seconds') + ' s generation';
    var actual = r.headers.get('X-Decode-Profile-Effective');
    if (actual) { meta += ' / ' + (r.headers.get('X-Decode-Profile') || '') + ' requested, ' + actual + ' effective'; }
    var mode = r.headers.get('X-Generation-Mode'); if (mode) { meta += ' / ' + mode; }
    var backend = r.headers.get('X-Voice-Backend'); if (backend) { meta += ' / ' + backend; }
    var checkpoint = r.headers.get('X-Model-Checkpoint'); if (checkpoint) { meta += ' / ' + checkpoint; }
    var revision = r.headers.get('X-Checkpoint-Revision'); if (revision) { meta += ' / revision ' + revision; }
    var sampling = r.headers.get('X-Effective-Sampling-Settings'); if (sampling) { meta += ' / effective sampling ' + sampling; }
    var language = r.headers.get('X-Voice-Language'); if (language) { meta += ' / ' + language; }
    var speaker = r.headers.get('X-Voice-Speaker'); if (speaker) { meta += ' / ' + speaker; }
    var cacheHit = r.headers.get('X-Reference-Cache-Hit'); if (cacheHit) { meta += ' / reference cache hit: ' + cacheHit; }
    var savedAsset = r.headers.get('X-Voice-Asset-Path');
    if (savedAsset) { meta += ' / saved reference: ' + savedAsset; }
    return r.blob().then(function (blob) { return {blob:blob, meta:meta, savedAsset:savedAsset}; });
  }).then(function (result) {
    clearInterval(ticker);
    makeHistory(result.blob, historyBody, result.meta);
    $('status').textContent = result.meta + ' / round trip ' + ((performance.now()-started)/1000).toFixed(1) + ' s';
    var historySaved = persistHistory(result.blob, historyBody, result.meta).catch(function (error) {
      $('status').textContent += ' / local history was not saved: ' + error.message;
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
      $('status').textContent = 'Stopped waiting. The model may still be finishing that line, and a new request is turned away as busy until it does.'
        + (body.save_voice_as ? ' The reusable voice is still saved when it finishes.' : '');
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
  if (showStatus !== false) { $('status').textContent='Stream cancelled; pending playback cleared.'; }
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
  if (!AudioContextCtor) { $('err').textContent = 'This browser does not support Web Audio streaming.'; return; }
  var state = {controller:new AbortController(), audioContext:null, sources:new Set(), cancelled:false,
    nextStart:0, sampleCount:0, expectedSequence:0, sampleRate:null, pcmParts:[],
    gapCount:0, gapSeconds:0, firstAudioMs:null, playbackStartMs:null, startEvent:null, complete:null};
  activeStream = state;
  ['go','reroll','stream','load','unload'].forEach(function(id){$(id).disabled=true;});
  $('stopStream').hidden=false; $('err').textContent='';
  $('status').textContent='Loading the selected Qwen checkpoint if needed, then streaming generated PCM chunks.';
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
      $('status').textContent='Native Qwen generator ready / service first byte '+Number(event.service_first_byte_s).toFixed(2)+' s';
      return Promise.resolve();
    }
    if (event.type === 'error') { throw new Error(event.error || 'Streaming generation failed.'); }
    if (event.type === 'complete') { state.complete=event; return Promise.resolve(); }
    if (event.type !== 'audio') { throw new Error('Unknown streaming event: '+event.type); }
    return waitForBufferRoom().then(function(){
      if (state.cancelled) { return; }
      if (event.sample_format !== 'pcm_s16le' || event.sequence !== state.expectedSequence ||
          event.sample_start !== state.sampleCount || event.sample_rate !== state.sampleRate) {
        throw new Error('The stream contains a discontinuous audio chunk. Playback was stopped.');
      }
      var bytes=decodePcm16(event.data), count=event.sample_count;
      if (bytes.length !== count*2 || !count) { throw new Error('The stream chunk byte count is invalid.'); }
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
      $('status').textContent='Native Qwen streaming / '+state.expectedSequence+' chunks / '
        +(state.sampleCount/state.sampleRate).toFixed(2)+' s received / '+state.gapCount+' audio gaps';
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
    if(!response.body||!response.body.getReader){throw new Error('This browser cannot read a progressive response body.');}
    var reader=response.body.getReader(), decoder=new TextDecoder(), done=false;
    while(!done&&!state.cancelled){var part=await reader.read();done=part.done;
      await drainLines(decoder.decode(part.value||new Uint8Array(),{stream:!done}),done);}
    if(state.cancelled){return;}
    if(!state.complete){throw new Error('The stream ended without a completion record.');}
    if(state.complete.sample_count!==state.sampleCount||state.complete.chunk_count!==state.expectedSequence){
      throw new Error('The completion record does not match the audio chunks received.');}
    while(!state.cancelled&&state.audioContext.currentTime<state.nextStart){await new Promise(function(resolve){setTimeout(resolve,25);});}
    if(state.cancelled){return;}
    var wav=new Blob([wavHeader(state.sampleRate,state.sampleCount)].concat(state.pcmParts),{type:'audio/wav'});
    var completion=state.complete, meta='native faster-Qwen generator / generator first chunk '+Number(completion.generator_first_chunk_s).toFixed(2)+' s'
      +' / service first byte '+Number(completion.service_first_byte_s).toFixed(2)+' s'
      +' / service first audio '+Number(completion.service_first_audio_s).toFixed(2)+' s'
      +' / browser playback start (estimated) '+((state.playbackStartMs-requestStarted)/1000).toFixed(2)+' s'
      +' / audio gaps '+state.gapCount+' ('+state.gapSeconds.toFixed(2)+' s)'
      +' / '+Number(completion.audio_seconds).toFixed(2)+' s audio / '+Number(completion.generation_total_s).toFixed(2)+' s generation'
      +' / '+completion.chunk_count+' transport chunks';
    if(completion.backend){meta+=' / '+completion.backend;}
    if(completion.checkpoint){meta+=' / '+completion.checkpoint;}
    if(completion.reference_cache_hit!==null&&completion.reference_cache_hit!==undefined){meta+=' / reference cache hit: '+completion.reference_cache_hit;}
    if(completion.saved_asset){meta+=' / saved reference: '+completion.saved_asset;}
    makeHistory(wav,historyBody,meta); $('status').textContent=meta;
    persistHistory(wav,historyBody,meta).catch(function(error){$('status').textContent+=' / local history was not saved: '+error.message;});
    if(completion.saved_asset){$('qwenSaveVoice').checked=false;$('qwenReferenceText').value=body.text;loadReferences(completion.saved_asset);}
  })().catch(function(error){
    if(error.name==='AbortError'||state.cancelled){$('status').textContent='Stream cancelled; pending playback cleared.';}
    else {$('err').textContent=error.message;$('status').textContent='';stopStreamPlayback(state,false);}
  }).then(function(){
    if(state.audioContext&&state.audioContext.state!=='closed'){state.audioContext.close().catch(function(){});}
    if(activeStream===state){activeStream=null;}
    $('stopStream').hidden=true;
    ['go','reroll','stream','load','unload'].forEach(function(id){$(id).disabled=false;});
  });
}
