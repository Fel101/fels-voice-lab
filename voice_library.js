/* Named saved voices, including reference clones and fixed speaker blends. */
(function (global) {
  'use strict';
  var library={voices:[]}, revision=0, busyNavigation=false;
  /** @param {string} id @returns {any} */
  function el(id){return document.getElementById(id);}
  function node(tag,text,className){var item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;}
  function engineName(id,fallback){
    var names={'drama-3':'Fish Audio Drama 3','kokoro':'Kokoro 82M','phonon-31m-en':'Phonon 31M English','orig':'Chatterbox Original','turbo':'Chatterbox Turbo',
      'qwen3-tts-1.7b-voice-design':'Qwen VoiceDesign 1.7B','qwen3-tts-1.7b-base':'Qwen Base 1.7B',
      'qwen3-tts-0.6b-base':'Qwen Base 0.6B','qwen3-tts-1.7b-custom-voice':'Qwen CustomVoice 1.7B','voxcpm2':'VoxCPM2','breeze-tts-2':'Breeze TTS 2'};
    return names[id]||fallback||id||LabText.t('cast.source_model_not_recorded');
  }
  function identityLabel(identity){return engineName(identity.engine_id)+' · '+identity.label+' ('+identity.status+')';}
  function visibleVoices(){
    var query=el('voiceSearch').value.trim().toLowerCase();
    return library.voices.filter(function(voice){
      return !query||[voice.character_name,voice.character_description,voice.identity_label,engineName(voice.engine_id,voice.engine_name),
        engineName(voice.generation_engine_id,voice.generation_engine_name)].join(' ').toLowerCase().indexOf(query)>=0;
    });
  }
  function status(message){el('voiceLibraryStatus').textContent=message;}
  function selectValue(select,value){
    if(value&&Array.from(select.options).some(function(item){return item.value===value;}))select.value=value;
  }
  function addChoice(select,value){
    if(value&&!Array.from(select.options).some(function(item){return item.value===value;}))global.option(select,value,value);
    selectValue(select,value);
  }
  async function openVoice(voice,target){
    if(target==='studio'){
      var reference;
      if(voice.identity_kind==='reference'){
        await global.loadReferences();
        reference=(global.referenceRows||[]).find(function(row){return row.reference_version_id===voice.preferred_reference_id;});
        if(!reference)throw new Error(LabText.t('cast.the_saved_voice_recording_is'));
      }
      if(!Array.from(el('engine').options).some(function(option){return option.value===voice.engine_id;}))
        throw new Error(LabText.t('cast.this_voice_s_model_is'));
      var options=voice.engine_options||{},audition=voice.audition||{};
      if(voice.identity_kind==='fixed_voice'){
        var voices=(options.voice||'').split(',').filter(Boolean);
        if(!voices.length||voices.some(function(value){return !Array.from(el('voice').options).some(function(item){return item.value===value;});}))
          throw new Error(LabText.t('cast.a_saved_speaker_pack_is'));
      }
      el('engine').value=voice.engine_id;global.refresh();
      if(voice.identity_kind==='fixed_voice'){
        Array.from(el('voice').options).forEach(function(item){item.selected=voices.indexOf(item.value)>=0;});
        selectValue(el('kokoroLanguage'),audition.language||options.language);
        if(audition.settings&&audition.settings.speed!==undefined)el('speed').value=audition.settings.speed;
      }else if(voice.identity_kind==='preset_voice'&&voice.engine_id==='phonon-31m-en'){
        selectValue(el('phononVoice'),options.speaker);global.showPhononVoiceInfo();
        ['seed:phononSeed','temperature:phononTemperature','duration_delta:phononPace'].forEach(function(pair){
          var key=pair.split(':')[0],control=el(pair.split(':')[1]);
          if(audition.settings&&audition.settings[key]!==undefined)control.value=audition.settings[key];
        });
      }else if(voice.identity_kind==='preset_voice'){
        addChoice(el('qwenSpeaker'),options.speaker);addChoice(el('qwenLanguage'),options.language);
        el('qwenCustomDirection').value=options.default_direction||'';
      }else{
        var selector;
        if(voice.engine_id==='drama-3'){el('dramaMode').value='clone';selector='dramaReference';}
        else if(voice.engine_id.indexOf('qwen')===0){selector='qwenReference';el('qwenXvecOnly').checked=false;selectValue(el('qwenLanguage'),audition.language);}
        else if(voice.engine_id==='voxcpm2'){el('voxMode').value='clone';selector='voxReference';}
        else if(voice.engine_id==='breeze-tts-2'){el('mode').value='clone';selector='reference';}
        else {el('clone').checked=true;selector='chatterboxReference';}
        global.refresh();el(selector).value=reference.path;el(selector).dispatchEvent(new Event('change'));
      }
      global.refresh();global.updateValues();
      el('studioVoiceSelection').textContent=(voice.identity_kind==='fixed_voice' ? LabText.t('cast.selected_blend', {character_name: voice.character_name, model: engineName(voice.engine_id,voice.engine_name)}) : LabText.t('cast.selected', {character_name: voice.character_name, model: engineName(voice.engine_id,voice.engine_name), status: voice.identity_status}));
      location.hash='studio';
      return;
    }
    await global.refreshCast(voice.character_id,voice.identity_id);
    if(target==='edit'){
      el('castEditor').open=true;location.hash='cast';el('castEditor').scrollIntoView({behavior:'smooth',block:'start'});
    }else{
      el('performCharacter').value=voice.character_id;global.renderPerformanceCharacter();
      el('performIdentity').value=voice.identity_id;global.renderPerformanceIdentity();
      selectValue(el('performReference'),voice.preferred_reference_id);
      selectValue(el('performLanguage'),voice.audition&&voice.audition.language);
      await global.loadCurrentTakes();
      location.hash='perform';
      el('performStatus').textContent=LabText.t('cast.ready_to_write_dialogue_for', {character_name: voice.character_name, identity_status: voice.identity_status});
    }
  }
  function action(voice,title,target){
    var button=node('button',title);button.type='button';button.disabled=busyNavigation;button.addEventListener('click',async function(){
      if(busyNavigation)return;setNavigationBusy(true);
      try{await openVoice(voice,target);}catch(error){status(error.message);}finally{setNavigationBusy(false);}
    });return button;
  }
  function setNavigationBusy(value){busyNavigation=value;el('voiceGallery').querySelectorAll('button').forEach(function(button){button.disabled=value||!!button._referenceMissing||!!button._retired;});}
  function retirementControls(voice){
    var report='',show=function(message){report=message;status(message);};
    // The refresh redraws the cards and writes the voice count into the same line, so the report goes back above it.
    var after=function(){return refresh().then(function(){if(report)status(report+'\n'+el('voiceLibraryStatus').textContent);});};
    if(!voice.retired)return [global.retirementButton(LabText.t('cast.retire_voice'),function(){return global.retireAction('identity',voice.identity_id,LabText.t('cast.this_voice'));},after,show)];
    var controls=[];
    if(!voice.retirement.inherited)controls.push(global.retirementButton(LabText.t('cast.restore_voice'),function(){return global.restoreAction('identity',voice.identity_id,'this voice');},after,show));
    controls.push(global.retirementButton(LabText.t('cast.delete_this_voice_for_good'),function(){return global.purgeAction('identity',voice.identity_id,'this voice');},after,show));
    return controls;
  }
  function render(){
    var gallery=el('voiceGallery');if(!gallery)return;
    gallery.querySelectorAll('audio').forEach(function(audio){audio.pause();});gallery.textContent='';
    var voices=visibleVoices(),summary=voices.length+' '+(voices.length===1?'voice':'voices');
    status(summary+(voices.length?LabText.t('cast.listen_below_then_choose_where'):LabText.t('cast.no_voices_match_clear_the')));
    voices.forEach(function(voice){
      var card=node('article',undefined,'saved-voice'),parts=voice.character_name.split(' · ');
      // A preset voice is its model's own: until a take says which model made its audition, that model made it.
      var preset=voice.identity_kind==='fixed_voice'||voice.identity_kind==='preset_voice',blend=String((voice.engine_options||{}).voice||'').split(',').filter(Boolean);
      var maker=voice.generation_engine_id||(preset?voice.engine_id:null),makerName=voice.generation_engine_id?voice.generation_engine_name:(preset?voice.engine_name:null);
      card.appendChild(node('span',(voice.identity_status||'candidate')+(voice.identity_kind==='fixed_voice'?(blend.length>1?LabText.t('cast.speaker_blend'):LabText.t('cast.preset_voice_2')):''),'voice-kind'));
      card.appendChild(node('h3',parts[0]));card.appendChild(node('p',parts.slice(1).join(' · ')||voice.identity_label,'voice-role'));
      card.appendChild(node('p',LabText.t('cast.created_with', {model: engineName(maker,makerName)})));
      if(maker!==voice.engine_id)card.appendChild(node('p',LabText.t('cast.generate_dialogue_with', {model: engineName(voice.engine_id,voice.engine_name)}),'meta'));
      var audition=voice.audition;
      if(audition&&audition.audio_url){
        var audio=node('audio');audio.controls=true;audio.preload='none';audio.src=audition.audio_url;
        audio.setAttribute('aria-label',LabText.t('cast.listen_to', {character_name: voice.character_name}));
        audio.addEventListener('play',function(){gallery.querySelectorAll('audio').forEach(function(other){if(other!==audio)other.pause();});});card.appendChild(audio);
      }else card.appendChild(node('p',LabText.t('cast.no_saved_audition_yet_generate'),'meta'));
      if(voice.identity_kind==='fixed_voice')card.appendChild(node('p',blend.length>1 ? LabText.t('cast.kokoro_blend_info', {voices: blend.join(',')}) : LabText.t('cast.kokoro_preset_info', {voice: blend.join(',')}),'meta'));
      else if(voice.identity_kind==='reference')card.appendChild(node('p',LabText.plural('cast.recording_versions_available', voice.reference_count)+' '+(voice.preferred_reference_available?LabText.t('cast.ready_to_use'):LabText.t('cast.voice_recording_unavailable')),'meta'));
      var buttons=node('div',undefined,'cast-actions');
      var studio=action(voice,LabText.t('cast.use_in_model_studio'),'studio');studio._referenceMissing=voice.identity_kind==='reference'&&!voice.preferred_reference_available;studio.disabled=busyNavigation||studio._referenceMissing;
      var perform=action(voice,LabText.t('cast.perform_lines'),'perform');
      studio._retired=perform._retired=!!voice.retired;studio.disabled=studio.disabled||!!voice.retired;perform.disabled=busyNavigation||!!voice.retired;
      buttons.append(studio,perform,action(voice,LabText.t('cast.edit_voice'),'edit'));
      if (!voice.retired && voice.identity_status === 'approved') {
        var share=node('button',LabText.t('cast.export_this_voice'));share.type='button';
        share.setAttribute('aria-label',LabText.t('cast.export_this_voice_2', {character_name: voice.character_name}));
        share.addEventListener('click',async function(){
          if(busyNavigation)return;
          var recorded=voice.identity_kind==='reference'&&!String(voice.origin||'').startsWith('Original synthetic voice design via ');
          if(recorded&&!global.confirm(LabText.t('cast.this_voice_file_includes_the')))return;
          setNavigationBusy(true);status(LabText.t('cast.checking_the_voice_file'));
          try{
            var result=await global.fetchJson('/api/portable/export',{kind:'persona',identity_id:voice.identity_id,permission:recorded});
            var download=node('a',LabText.t('cast.download_voice_file'));download.href=result.archive.download;
            download.download=voice.character_name.replace(/[<>:"/\\|?*]/g,'-')+'.voice.zip';
            status(LabText.t('cast.voice_file_ready'));el('voiceLibraryStatus').appendChild(download);
          }catch(error){status(error.message);}finally{setNavigationBusy(false);}
        });
        buttons.appendChild(share);
      }
      card.appendChild(buttons);
      if(voice.retired){card.classList.add('retired');card.appendChild(node('p',global.retirementLabel(voice)+LabText.t('cast.new_use_is_blocked_listening_2'),'meta'));}
      var controls=node('div',undefined,'cast-actions');retirementControls(voice).forEach(function(button){controls.appendChild(button);});card.appendChild(controls);
      var details=node('details');details.appendChild(node('summary',LabText.t('cast.voice_description_and_use_notes')));
      details.appendChild(node('p',voice.character_description||LabText.t('cast.no_description_saved'),'meta'));
      details.appendChild(node('p',LabText.t('cast.origin_record', {origin: voice.origin || LabText.t('cast.origin_undocumented')}),'meta'));
      details.appendChild(node('p',LabText.t('cast.allowed_use_2', {allowed_use: voice.allowed_use||LabText.t('cast.use_terms_undocumented')}),'meta'));
      card.appendChild(details);gallery.appendChild(card);
    });
  }
  async function refresh(){
    if(!el('voiceGallery'))return;
    var request=++revision;
    try{
      var result=await global.fetchJson('/api/voice-library'+(el('showRetiredVoices').checked?'?include_retired=1':''));if(request!==revision)return;
      library={voices:result.voices||[]};render();
    }catch(error){if(request===revision)status(LabText.t('cast.saved_voices_unavailable', {message: error.message}));}
  }
  document.addEventListener('DOMContentLoaded',function(){
    el('showRetiredVoices').addEventListener('change',refresh);
    el('voiceSearch').addEventListener('input',render);el('refreshVoices').addEventListener('click',refresh);
    ['engine','voice','kokoroLanguage','mode','dramaMode','voxMode','qwenSpeaker','qwenLanguage','clone',
      'reference','qwenReference','voxReference','voxPromptReference','dramaReference','chatterboxReference'].forEach(function(id){
      el(id).addEventListener('change',clearStudioSelection);
    });
  });
  function clearStudioSelection(){el('studioVoiceSelection').textContent='';}
  global.VoiceLabLibrary={refresh:refresh,render:render,openVoice:openVoice,visibleVoices:visibleVoices,engineName:engineName,identityLabel:identityLabel,clearStudioSelection:clearStudioSelection};
})(window);
