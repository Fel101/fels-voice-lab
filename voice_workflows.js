/* Local reference auditions and the direct cloning workflow. No model or media downloads. */
(function (global) {
  'use strict';
  var rows = [], context = null, sourceFile = null, sourceUrl = null, resultUrl = null, sourceTerms = null;
  var recording = null, recordingTimer = null, recordingRevision = 0, busy = false, initialized = false;
  var showReferenceVersions = false;
  var cloneEngines = ['drama-3','qwen3-tts-1.7b-base','qwen3-tts-0.6b-base','voxcpm2','turbo','orig','breeze-tts-2'];
  // The cloning model this page last chose by itself. Any other value in the picker was chosen by the person, and stays.
  var cloneEngineDefault = null;
  var transcriptFields = {reference:'referenceText',qwenReference:'qwenReferenceText',
    voxPromptReference:'voxPromptText',dramaReference:'dramaTranscript'};
  var referenceSelectors = ['reference','qwenReference','voxReference','voxPromptReference','dramaReference','chatterboxReference'];
  /** @param {string} id @returns {any} */
  function el(id) { return document.getElementById(id); }
  function json(url, body) { return global.fetchJson(url, body); }
  function selectedRow(select) { return rows.find(function (row) { return row.path === select.value; }); }
  function sourceDetails(row) {
    if (!row) return LabText.t('studio.no_recording_selected');
    return (row.source_label || row.source || LabText.t('studio.local_recording')) +
      (row.duration_seconds ? ' · ' + row.duration_seconds.toFixed(2) + LabText.t('studio.s') : '') +
      (row.generation_engine_name ? LabText.t('studio.created_with', {generation_engine_name: row.generation_engine_name}) : '') +
      (row.engine_name ? LabText.t('studio.saved_voice_uses', {engine_name: row.engine_name}) : '') +
      (row.identity_status ? LabText.t('studio.voice_status', {identity_status: row.identity_status}) : '') +
      (row.version ? LabText.t('studio.recording_version', {version: row.version}) : '') +
      LabText.t('studio.origin') + (row.origin || LabText.t('studio.unknown_source_not_documented')) +
      LabText.t('studio.allowed_use') + (row.allowed_use || LabText.t('studio.unknown_do_not_assume_permission')) +
      (row.description ? LabText.t('studio.description', {description: row.description}) : '') +
      LabText.t('studio.file') + (row.display_name || row.name);
  }
  function label(row) {
    var peers=rows.filter(function(other){return row.character_id&&other.character_id===row.character_id;});
    var identities=Array.from(new Set(peers.map(function(other){return other.identity_id;})));
    var identitySuffix=identities.length>1?' · '+(row.identity_label||LabText.t('studio.voice')):'';
    if(identities.length>1&&peers.some(function(other){return other.identity_id!==row.identity_id&&other.identity_label===row.identity_label&&other.engine_id===row.engine_id;}))
      identitySuffix+=' ['+String(row.identity_id).slice(0,6)+']';
    var multipleVersions=row.identity_id&&peers.filter(function(other){return other.identity_id===row.identity_id;}).length>1;
    var model=row.generation_engine_name||row.engine_name;
    if(global.VoiceLabLibrary)model=global.VoiceLabLibrary.engineName(row.generation_engine_id||row.engine_id,model);
    return (row.character_name || row.label || row.display_name || row.name) +
      identitySuffix +
      (model ? ' — ' + model : '') +
      (row.duration_seconds ? ' · ' + row.duration_seconds.toFixed(1) + ' s' : '') +
      ((showReferenceVersions||multipleVersions) && row.version ? ' · v' + row.version : '');
  }
  function visibleReferenceRows(current) {
    return rows.filter(function (row) {
      return row.path === current || showReferenceVersions || row.is_preferred_reference !== false;
    }).sort(function (a,b) {
      return String(a.character_name || a.display_name || a.name).localeCompare(String(b.character_name || b.display_name || b.name));
    });
  }
  function fillReferenceSelector(select, before, placeholder) {
    select.textContent = ''; global.option(select, '', placeholder);
    var groups = {}, visible = visibleReferenceRows(before);
    visible.forEach(function (row) {
      var group = row.character_id ? LabText.t('studio.saved_character_voices') : LabText.t('studio.local_recordings_and_designs');
      if (!groups[group]) { var node=document.createElement('optgroup');node.label=group;groups[group]=node;select.appendChild(node); }
      global.option(groups[group], row.path, label(row));
    });
    if (visible.some(function (row) { return row.path === before; })) select.value = before;
  }
  function addReferenceFilters(container, select) {
    var filters=document.createElement('div');filters.className='reference-filters';
    /** @type {Array<[string, () => boolean, (value: boolean) => void]>} */ ([[LabText.t('studio.all_recording_versions'),function(){return showReferenceVersions;},function(value){showReferenceVersions=value;}]]).forEach(function(item,index){
      var wrap=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.checked=item[1]();
      input.dataset.referenceFilter=String(index);input.setAttribute('aria-label',LabText.t('studio.for', {value: item[0], id: select.id}));
      input.addEventListener('change',function(){item[2](input.checked);
        /** @type {NodeListOf<HTMLInputElement>} */ (document.querySelectorAll('[data-reference-filter="'+index+'"]')).forEach(function(other){other.checked=input.checked;});render(rows);});
      wrap.append(input,document.createTextNode(' '+item[0]));filters.appendChild(wrap);
    });container.appendChild(filters);
  }
  function kokoroLabel(id) {
    var languages={a:LabText.t('studio.american_english'),b:LabText.t('studio.british_english'),e:LabText.t('studio.spanish'),f:LabText.t('studio.french'),h:LabText.t('studio.hindi'),i:LabText.t('studio.italian'),j:LabText.t('studio.japanese'),p:LabText.t('studio.brazilian_portuguese'),z:LabText.t('studio.mandarin_chinese')};
    var name=id.slice(3).replace(/_/g,' ');name=name.charAt(0).toUpperCase()+name.slice(1);
    return name+' · '+(languages[id.charAt(0)]||'Kokoro')+' ['+id+']';
  }
  function openUpload(engine) {
    if (cloneEngines.indexOf(engine) >= 0) el('cloneEngine').value = engine;
    syncCloneEngine();
    location.hash = 'cloning';
    el('cloneFile').focus();
  }
  function updatePreview(select, copyTranscript) {
    var row = selectedRow(select), audio = el(select.id + 'Audio'), details = el(select.id + 'Details');
    if (audio) {
      audio.pause();
      if (row && row.audio_url) audio.src = row.audio_url;
      else { audio.removeAttribute('src'); audio.load(); }
    }
    if (details) details.textContent = sourceDetails(row);
    var transcript = transcriptFields[select.id];
    if (copyTranscript && transcript) el(transcript).value = row && row.transcript || '';
  }
  function addReferenceTools(select) {
    var tools = document.createElement('div'); tools.className = 'reference-tools';
    var audio = document.createElement('audio'); audio.id = select.id + 'Audio'; audio.controls = true;
    audio.preload = 'none'; audio.setAttribute('aria-label', LabText.t('studio.preview', {trim: select.parentElement.firstChild.textContent.trim()}));
    var disclosure=document.createElement('details'),summary=document.createElement('summary');summary.textContent=LabText.t('studio.source_use_notes_and_recording');
    var details = document.createElement('p'); details.id = select.id + 'Details'; details.className = 'meta reference-details';disclosure.append(summary,details);
    var button = document.createElement('button'); button.type = 'button'; button.textContent = LabText.t('studio.upload_record_a_new_voice');
    button.addEventListener('click', function () { openUpload(el('engine').value); });
    var browse=document.createElement('a');browse.href='#cast';browse.textContent=LabText.t('studio.browse_named_voices');
    tools.append(audio, disclosure, browse, document.createTextNode(' · '), button);addReferenceFilters(tools,select);select.parentElement.insertAdjacentElement('afterend', tools);
    select.addEventListener('change', function () { updatePreview(select, true); });
  }
  function render(nextRows, preferredPath) {
    rows = nextRows || [];
    if (!initialized) return;
    referenceSelectors.forEach(function (id) {
      var select = el(id), before = preferredPath || select.value;
      fillReferenceSelector(select,before,LabText.t('studio.choose_a_recording_to_preview'));
      if(select.value!==before&&global.VoiceLabLibrary)global.VoiceLabLibrary.clearStudioSelection();
      updatePreview(select, select.value !== before || !!preferredPath);
    });
    var selector = el('cloneReference'), previous = selector.value;
    fillReferenceSelector(selector,previous,LabText.t('studio.choose_a_saved_recording_or'));
    fillCloneEngines();
  }
  // The first cloning model in the picker that this computer has installed, as the Models page last read it; when none is,
  // the first one the Lab can install for you; and the first in the picker before the page has read anything.
  function preferredCloneEngine(select) {
    var status = global.engineRows || {}, offered = Array.from(select.options).map(function (item) { return item.value; });
    function first(test) {
      return offered.find(function (id) { return status[id] && test(status[id].installation || {}); });
    }
    return first(function (setup) { return setup.installed; }) || first(function (setup) { return !!setup.installer; }) ||
      offered[0] || '';
  }
  function fillCloneEngines() {
    var select = el('cloneEngine'), before = select.value;
    var entries = global.catalog && global.catalog.engines || [];
    select.textContent = '';
    cloneEngines.forEach(function (id) {
      var entry = entries.find(function (item) { return item.id === id; });
      if (entry && entry.integration && entry.integration.service_api) global.option(select, id, entry.name);
    });
    var offered = Array.from(select.options).some(function (item) { return item.value === before; });
    if (before && before !== cloneEngineDefault && offered) select.value = before;
    else select.value = cloneEngineDefault = preferredCloneEngine(select);
    if (typeof global.markEngineOptions === 'function') global.markEngineOptions();
    syncCloneEngine();
  }
  // The Models page calls this when it has read which models are installed: a picker nobody has changed moves to an installed
  // cloning model, and the note under it says whether the chosen one is installed.
  function refreshCloneEngine() {
    var select = el('cloneEngine');
    if (!initialized) return;
    if (select.options.length && select.value === cloneEngineDefault) select.value = cloneEngineDefault = preferredCloneEngine(select);
    syncCloneEngine();
  }
  function syncCloneEngine() {
    var engine = el('cloneEngine').value, status = (global.engineRows || {})[engine];
    el('cloneDirectionWrap').hidden = engine === 'orig' || engine === 'turbo';
    var missing = !status || status.state !== 'not_installed' ? '' : (status.installation || {}).installer ?
      LabText.t('studio.not_installed_on_this_computer') :
      LabText.t('studio.not_installed_on_this_computer_2');
    el('cloneModelHelp').textContent = missing + (engine === 'drama-3' ?
      LabText.t('studio.fish_audio_exact_transcript_required') :
      engine === 'breeze-tts-2' ? LabText.t('studio.breeze_exact_transcript_required_model') :
      engine.indexOf('qwen') === 0 ? LabText.t('studio.qwen_base_type_the_exact') :
      engine === 'voxcpm2' ? LabText.t('studio.voxcpm2_the_words_are_optional') :
      engine === 'turbo' ? LabText.t('studio.chatterbox_turbo_the_recording_must') :
      LabText.t('studio.chatterbox_original_the_recording_gives'));
  }
  function replaceSource(file) {
    if (sourceUrl) URL.revokeObjectURL(sourceUrl);
    sourceFile = file; sourceTerms = null; sourceUrl = URL.createObjectURL(file);
    el('cloneSourcePlayer').src = sourceUrl;
    el('cloneReference').value = '';
    el('cloneSourceInfo').textContent = LabText.t('studio.kb_listen_and_enter_its', {name: file.name, round: Math.round(file.size / 1024)});
    el('cloneTranscript').value = ''; el('cloneOrigin').value = ''; el('cloneAllowedUse').value = '';
    el('clonePermission').checked = false;
  }
  function chooseSavedSource() {
    var row = selectedRow(el('cloneReference'));
    if (!row) return;
    sourceFile = null; sourceTerms = null; el('cloneFile').value = '';
    if (sourceUrl) { URL.revokeObjectURL(sourceUrl); sourceUrl = null; }
    el('cloneSourcePlayer').src = row.audio_url;
    el('cloneSourceInfo').textContent = sourceDetails(row);
    el('cloneTranscript').value = row.transcript || '';
    el('cloneOrigin').value = row.origin || '';
    el('cloneAllowedUse').value = row.allowed_use || '';
    el('clonePermission').checked = false;
  }
  function openFromClip(blob, body) {
    replaceSource(new File([blob], body.engine + '-studio.wav', {type:'audio/wav'}));
    var engine = body.engine === 'qwen3-tts-1.7b-voice-design' ? 'qwen3-tts-1.7b-base' : body.engine;
    openUpload(cloneEngines.indexOf(engine) >= 0 ? engine : preferredCloneEngine(el('cloneEngine')));
    el('cloneName').value = body.save_voice_as || '';
    el('cloneTranscript').value = body.text || '';
    var parent = body._reference_provenance;
    if (!parent && body.reference_path && body.mode!=='design' && body.mode!=='custom_voice') parent = {origin:LabText.t('studio.where_this_recording_came_from'),allowed_use:LabText.t('studio.who_may_use_the_recording')};
    el('cloneOrigin').value = LabText.t('studio.generated_locally_with', {engine: body.engine, value: parent && parent.origin ? '; source: ' + parent.origin : ''});
    el('cloneAllowedUse').value = (parent && parent.allowed_use ? parent.allowed_use + '; ' : '') +
      (body.engine === 'breeze-tts-2' ? LabText.t('studio.non_commercial_only_breeze_model') :
        body.engine === 'drama-3' ? LabText.t('studio.subject_to_fish_audio_community') : LabText.t('studio.subject_to_the_source_model'));
    sourceTerms = {origin:el('cloneOrigin').value,allowed_use:el('cloneAllowedUse').value};
    el('cloneStatus').textContent = LabText.t('studio.studio_clip_ready_to_save');
  }
  function freezeRequest(body) {
    var snapshot = JSON.parse(JSON.stringify(body));
    if (body.reference_path && body.mode!=='design' && body.mode!=='custom_voice' && body.engine!=='kokoro') {
      var source = rows.find(function (row) { return row.path === body.reference_path; });
      snapshot._reference_provenance = source ? {origin:source.origin || LabText.t('studio.unknown_source'),allowed_use:source.allowed_use || 'Who may use the recording is unknown; check before you reuse or share it.',
        reference_version_id:source.reference_version_id || null} : {origin:'Unknown source',allowed_use:'Who may use the recording is unknown; check before you reuse or share it.'};
    }
    return snapshot;
  }
  function metadataHeader(metadata) {
    var bytes = new TextEncoder().encode(JSON.stringify(metadata)), binary = '';
    bytes.forEach(function (value) { binary += String.fromCharCode(value); });
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  async function saveCandidate() {
    var engine = el('cloneEngine').value, name = el('cloneName').value.trim();
    var transcript = el('cloneTranscript').value.trim(), origin = el('cloneOrigin').value.trim();
    var allowedUse = el('cloneAllowedUse').value.trim(), row = selectedRow(el('cloneReference'));
    if (!name) throw new Error(LabText.t('studio.name_the_voice_before_saving'));
    if (!el('clonePermission').checked || !origin || !allowedUse) throw new Error(LabText.t('studio.confirm_your_permission_and_record'));
    if (cloneEngines.indexOf(engine) < 0) throw new Error(LabText.t('studio.choose_a_cloning_model'));
    if (engine !== 'voxcpm2' && !transcript)
      throw new Error(LabText.t('studio.add_the_exact_words_spoken'));
    if (sourceTerms) {
      if (allowedUse.indexOf(sourceTerms.allowed_use) < 0) allowedUse += LabText.t('studio.inherited', {allowed_use: sourceTerms.allowed_use});
      if (origin.indexOf(sourceTerms.origin) < 0) origin += '; source: ' + sourceTerms.origin;
    }
    var file = sourceFile;
    if (!file && row) {
      var response = await fetch(row.audio_url);
      if (!response.ok) throw new Error(LabText.t('studio.could_not_read_the_chosen'));
      file = new File([await response.blob()], row.name || 'reference.wav', {type:'audio/wav'});
      // Existing restrictions cannot be silently dropped while copying a library reference.
      if (row.allowed_use && allowedUse.indexOf(row.allowed_use) < 0) allowedUse += '; inherited: ' + row.allowed_use;
      if (row.origin && origin.indexOf(row.origin) < 0) origin += '; source: ' + row.origin;
    }
    if (!file) throw new Error(LabText.t('studio.upload_record_or_choose_a'));
    if (file.size > 32 * 1024 * 1024) throw new Error(LabText.t('studio.the_recording_is_larger_than'));
    if (!/\.(wav|flac|ogg|mp3|m4a)$/i.test(file.name)) throw new Error(LabText.t('studio.choose_wav_flac_ogg_mp3'));
    var decoded = await global.decodeBlob(file);
    if (decoded.duration < 0.5 || decoded.duration > 60) throw new Error(LabText.t('studio.use_a_0_5_60'));
    if (engine === 'turbo' && decoded.duration <= 5) throw new Error(LabText.t('studio.turbo_needs_a_recording_longer'));
    if (global.bufferAnalysis(decoded).rms < 0.001) throw new Error(LabText.t('studio.the_recording_is_silent_or'));
    var key = name + '\n' + engine;
    if (!context || context.key !== key || !context.characterId) {
      context = {key:key};
      var created = await json('/api/characters', {name:name, description:LabText.t('studio.voice_cloned_from_a_recording'), tags:['cloning']});
      context.characterId = created.character.id;
    }
    if (!context.identityId) {
      var identity = await json('/api/characters/' + context.characterId + '/identities', {engine_id:engine,label:name,notes:LabText.t('studio.listen_before_approving_this_voice')});
      context.identityId = identity.identity.id;
    }
    // Hash actual bytes: retrying a failed generation reuses the saved immutable reference.
    var digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer())))
      .map(function (value) { return value.toString(16).padStart(2, '0'); }).join('');
    var signature = JSON.stringify([digest,transcript,origin,allowedUse]);
    if (!context.reference || context.signature !== signature) {
      var metadata = {filename:file.name,transcript:transcript,origin:origin,allowed_use:allowedUse,
        edits:{target_sample_rate:engine === 'drama-3' ? 44100 : engine === 'voxcpm2' ? 48000 : 24000,channel:'mono'}};
      var uploaded = await fetch('/api/identities/' + context.identityId + '/references', {method:'POST',
        headers:{'Content-Type':file.type || 'application/octet-stream','X-Reference-Metadata':metadataHeader(metadata)}, body:file});
      var saved = await uploaded.json();
      if (!uploaded.ok) throw new Error(saved.error || LabText.t('studio.could_not_save_the_recording'));
      context.reference = saved.reference; context.signature = signature;
    }
    await global.refreshCast(context.characterId, context.identityId);
    await global.loadReferences();
    el('cloneOpenCast').hidden = false;
    return context;
  }
  async function runClone(generate) {
    if (busy || recording) return;
    if (generate && !el('cloneText').value.trim()) { el('cloneStatus').textContent = LabText.t('studio.enter_the_new_words_to'); return; }
    busy = true; el('cloneSave').disabled = true; el('cloneGenerate').disabled = true;
    el('cloneControls').disabled = true;
    el('cloneStatus').textContent = LabText.t('studio.checking_and_saving_the_recording');
    try {
      var saved = await saveCandidate();
      el('cloneStatus').textContent = LabText.t('studio.saved_recording_v_review_it', {version: saved.reference.version});
      if (generate) {
        el('cloneStatus').textContent = LabText.t('studio.recording_saved_making_the_test');
        var engine = el('cloneEngine').value;
        var body = {identity_id:saved.identityId,reference_version_id:saved.reference.id,
          line_id:'cloning-test',text:el('cloneText').value,language:'English',settings:{},
          direction:engine === 'orig' || engine === 'turbo' ? '' : el('cloneDirection').value};
        var result = await fetch('/api/performance', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        if (!result.ok) { var failure = await result.json(); throw new Error(failure.error || result.status); }
        if (resultUrl) URL.revokeObjectURL(resultUrl);
        resultUrl = URL.createObjectURL(await result.blob()); el('cloneResult').src = resultUrl; el('cloneResult').hidden = false;
        el('cloneStatus').textContent = LabText.t('studio.test_saved_in_perform_lines');
      }
    } catch (error) { el('cloneStatus').textContent = error.message + (context && context.reference ? LabText.t('studio.your_saved_recording_is_kept') : ''); }
    finally { busy = false; el('cloneControls').disabled = false; el('cloneSave').disabled = false; el('cloneGenerate').disabled = false; }
  }
  async function beginRecording() {
    if (recording || busy || el('cloneMicStart').disabled) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { el('cloneStatus').textContent = LabText.t('studio.microphone_recording_is_unavailable_in'); return; }
    el('cloneMicStart').disabled = true; el('cloneSave').disabled = true; el('cloneGenerate').disabled = true;
    var revision=++recordingRevision;
    var stream, audio;
    try {
      stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:false,noiseSuppression:false,autoGainControl:false}});
      if(revision!==recordingRevision){stream.getTracks().forEach(function(track){track.stop();});return;}
      audio = new (global.AudioContext || global.webkitAudioContext)(); await audio.resume();
      if(revision!==recordingRevision){stream.getTracks().forEach(function(track){track.stop();});await audio.close();return;}
      var source = audio.createMediaStreamSource(stream), processor = audio.createScriptProcessor(4096,1,1), mute = audio.createGain();
      mute.gain.value = 0;
      recording = {stream:stream,audio:audio,source:source,processor:processor,mute:mute,chunks:[],frames:0};
      processor.onaudioprocess = function (event) {
        if (!recording) return;
        var channel = new Float32Array(event.inputBuffer.getChannelData(0)); recording.chunks.push([channel]); recording.frames += channel.length;
        if (recording.frames / audio.sampleRate >= 59) stopRecording();
      };
      source.connect(processor); processor.connect(mute); mute.connect(audio.destination);
      el('cloneMicStart').hidden = true; el('cloneMicStop').hidden = false;
      el('cloneSave').disabled = true; el('cloneGenerate').disabled = true;
      el('cloneStatus').textContent = LabText.t('studio.recording_locally_stop_when_you');
      recordingTimer = setTimeout(stopRecording, 59000);
    } catch (error) {
      if (stream) stream.getTracks().forEach(function (track) { track.stop(); });
      if (audio) audio.close();
      el('cloneStatus').textContent = LabText.t('studio.microphone_could_not_start', {message: error.message});
    } finally { el('cloneMicStart').disabled = false; if (!recording) {el('cloneSave').disabled=false;el('cloneGenerate').disabled=false;} }
  }
  function stopRecording() {
    var capture = recording;
    if (!capture) return;
    recording = null; clearTimeout(recordingTimer); recordingTimer = null;
    capture.processor.onaudioprocess = null; capture.processor.disconnect(); capture.source.disconnect(); capture.mute.disconnect();
    capture.stream.getTracks().forEach(function (track) { track.stop(); });
    var blob = global.pcmCaptureWav(capture.chunks,capture.audio.sampleRate); capture.audio.close();
    replaceSource(new File([blob], 'microphone-reference.wav', {type:'audio/wav'}));
    el('cloneMicStart').hidden = false; el('cloneMicStop').hidden = true;
    el('cloneSave').disabled = false; el('cloneGenerate').disabled = false;
    el('cloneStatus').textContent = LabText.t('studio.recording_ready_listen_enter_its');
  }
  // Read the installed Kokoro voices again into the voice picker, keeping what was chosen (af_heart, or the first, when nothing
  // was) and passing them on to the fixed-voice picker. Generate calls it when the picker is empty, so a page that was open
  // while Kokoro was installed works afterwards. Resolves to the number of voices.
  async function refreshKokoroVoices() {
    var info = await json('/api/info'), voice = el('voice'), selected = global.selectedValues(voice);
    voice.textContent = '';
    (info.kokoro_voices || info.voices || []).forEach(function (name) { global.option(voice, name, kokoroLabel(name)); });
    Array.from(voice.options).forEach(function (item) { item.selected = selected.indexOf(item.value) >= 0; });
    if (!voice.selectedOptions.length && voice.options.length) {
      (Array.from(voice.options).find(function (item) { return item.value === 'af_heart'; }) || voice.options[0]).selected = true;
    }
    global.fillFixedVoices();
    return voice.options.length;
  }
  function init() {
    initialized = true;
    referenceSelectors.forEach(function (id) { addReferenceTools(el(id)); });
    var cloneFilters=document.createElement('div');addReferenceFilters(cloneFilters,el('cloneReference'));el('cloneReference').parentElement.insertAdjacentElement('afterend',cloneFilters);
    el('cloneFile').addEventListener('change', function () { if (this.files[0]) replaceSource(this.files[0]); });
    el('cloneReference').addEventListener('change', chooseSavedSource);
    el('cloneEngine').addEventListener('change',syncCloneEngine);
    el('cloneSave').addEventListener('click', function () { return runClone(false); });
    el('cloneGenerate').addEventListener('click', function () { return runClone(true); });
    el('cloneMicStart').addEventListener('click', beginRecording); el('cloneMicStop').addEventListener('click', stopRecording);
    el('refreshReferenceLibrary').addEventListener('click', function () { global.loadReferences(); });
    el('clone').addEventListener('change', function () { el('chatterboxReferenceWrap').hidden = !this.checked; });
    el('refreshKokoroVoices').addEventListener('click', async function () {
      try { await refreshKokoroVoices(); el('status').textContent = LabText.t('studio.installed_kokoro_speaker_packs_refreshed'); }
      catch (error) { el('err').textContent = error.message; }
    });
    global.addEventListener('beforeunload',function () { if (recording) recording.stream.getTracks().forEach(function (track) { track.stop(); }); });
    global.addEventListener('hashchange',function(){if(location.hash!=='#cloning'){recordingRevision++;if(recording)stopRecording();}});
    render(global.referenceRows || []);
  }
  global.VoiceLabWorkflows = {render:render,openUpload:openUpload,openFromClip:openFromClip,freezeRequest:freezeRequest,sourceDetails:sourceDetails,label:label,kokoroLabel:kokoroLabel,refreshKokoroVoices:refreshKokoroVoices,metadataHeader:metadataHeader,visibleReferenceRows:visibleReferenceRows,refreshCloneEngine:refreshCloneEngine};
  document.addEventListener('DOMContentLoaded', init);
})(window);
