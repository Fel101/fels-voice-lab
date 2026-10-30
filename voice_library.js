/* Named saved voices, including reference clones and fixed speaker blends. */
(function (global) {
  'use strict';
  var library={voices:[]}, revision=0, busyNavigation=false;
  function el(id){return document.getElementById(id);}
  function node(tag,text,className){var item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;}
  function engineName(id,fallback){
    var names={'drama-3':'Fish Audio Drama 3','kokoro':'Kokoro 82M','phonon-31m-en':'Phonon 31M English','orig':'Chatterbox Original','turbo':'Chatterbox Turbo',
      'qwen3-tts-1.7b-voice-design':'Qwen VoiceDesign 1.7B','qwen3-tts-1.7b-base':'Qwen Base 1.7B',
      'qwen3-tts-0.6b-base':'Qwen Base 0.6B','qwen3-tts-1.7b-custom-voice':'Qwen CustomVoice 1.7B','voxcpm2':'VoxCPM2','breeze-tts-2':'Breeze TTS 2'};
    return names[id]||fallback||id||'Source model not recorded';
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
        if(!reference)throw new Error('The saved reference is unavailable. Open Edit voice to inspect its versions.');
      }
      if(!Array.from(el('engine').options).some(function(option){return option.value===voice.engine_id;}))
        throw new Error('This voice’s model is not in the installed catalogue.');
      var options=voice.engine_options||{},audition=voice.audition||{};
      if(voice.identity_kind==='fixed_voice'){
        var voices=(options.voice||'').split(',').filter(Boolean);
        if(!voices.length||voices.some(function(value){return !Array.from(el('voice').options).some(function(item){return item.value===value;});}))
          throw new Error('A saved speaker pack is missing. Refresh installed Kokoro speaker packs first.');
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
      el('studioVoiceSelection').textContent='Selected '+voice.character_name+' · '+engineName(voice.engine_id,voice.engine_name)+
        (voice.identity_kind==='fixed_voice'?' · saved equal speaker blend.':' · '+voice.identity_status+' voice.');
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
      el('performStatus').textContent='Ready to write dialogue for '+voice.character_name+'. Listen and review this '+voice.identity_status+' voice before approving it.';
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
    var show=function(message){status(message);},after=function(){return refresh();};
    if(!voice.retired)return [global.retirementButton('Retire voice',function(){return global.retireAction('identity',voice.identity_id,'this voice');},after,show)];
    var controls=[];
    if(!voice.retirement.inherited)controls.push(global.retirementButton('Restore voice',function(){return global.restoreAction('identity',voice.identity_id,'this voice');},after,show));
    controls.push(global.retirementButton('Purge voice audio…',function(){return global.purgeAction('identity',voice.identity_id,'this voice');},after,show));
    return controls;
  }
  function render(){
    var gallery=el('voiceGallery');if(!gallery)return;
    gallery.querySelectorAll('audio').forEach(function(audio){audio.pause();});gallery.textContent='';
    var voices=visibleVoices(),summary=voices.length+' '+(voices.length===1?'voice':'voices');
    status(summary+(voices.length?'\nListen below, then choose where to use a voice.':'\nNo voices match. Clear the search to see more.'));
    voices.forEach(function(voice){
      var card=node('article',undefined,'saved-voice'),parts=voice.character_name.split(' · ');
      card.appendChild(node('span',(voice.identity_status||'candidate')+(voice.identity_kind==='fixed_voice'?' · speaker blend':''),'voice-kind'));
      card.appendChild(node('h3',parts[0]));card.appendChild(node('p',parts.slice(1).join(' · ')||voice.identity_label,'voice-role'));
      card.appendChild(node('p','Created with '+engineName(voice.generation_engine_id,voice.generation_engine_name)));
      if(voice.generation_engine_id!==voice.engine_id)card.appendChild(node('p','Generate dialogue with '+engineName(voice.engine_id,voice.engine_name),'meta'));
      var audition=voice.audition;
      if(audition&&audition.audio_url){
        var audio=node('audio');audio.controls=true;audio.preload='none';audio.src=audition.audio_url;
        audio.setAttribute('aria-label','Listen to '+voice.character_name);
        audio.addEventListener('play',function(){gallery.querySelectorAll('audio').forEach(function(other){if(other!==audio)other.pause();});});card.appendChild(audio);
      }else card.appendChild(node('p','No saved audition yet. Generate one in Perform lines.','meta'));
      if(voice.identity_kind==='fixed_voice')card.appendChild(node('p','Saved Kokoro blend: '+(voice.engine_options.voice||'')+'. No reference recording needed.','meta'));
      else if(voice.identity_kind==='reference')card.appendChild(node('p',voice.reference_count+' saved reference '+(voice.reference_count===1?'version':'versions')+'. '+(voice.preferred_reference_available?'Ready to use.':'Reference audio unavailable.'),'meta'));
      var buttons=node('div',undefined,'cast-actions');
      var studio=action(voice,'Use in Model studio','studio');studio._referenceMissing=voice.identity_kind==='reference'&&!voice.preferred_reference_available;studio.disabled=busyNavigation||studio._referenceMissing;
      var perform=action(voice,'Perform lines','perform');
      studio._retired=perform._retired=!!voice.retired;studio.disabled=studio.disabled||!!voice.retired;perform.disabled=busyNavigation||!!voice.retired;
      buttons.append(studio,perform,action(voice,'Edit voice','edit'));card.appendChild(buttons);
      if(voice.retired){card.classList.add('retired');card.appendChild(node('p',global.retirementLabel(voice)+' New use is blocked; listening stays available for review.','meta'));}
      var controls=node('div',undefined,'cast-actions');retirementControls(voice).forEach(function(button){controls.appendChild(button);});card.appendChild(controls);
      var details=node('details');details.appendChild(node('summary','Voice description and use notes'));
      details.appendChild(node('p',voice.character_description||'No description saved.','meta'));
      details.appendChild(node('p','Origin: '+(voice.origin||'Not documented.'),'meta'));
      details.appendChild(node('p','Allowed use: '+(voice.allowed_use||'Not documented. Verify before reusing or sharing.'),'meta'));
      card.appendChild(details);gallery.appendChild(card);
    });
  }
  async function refresh(){
    if(!el('voiceGallery'))return;
    var request=++revision;
    try{
      var result=await global.fetchJson('/api/voice-library'+(el('showRetiredVoices').checked?'?include_retired=1':''));if(request!==revision)return;
      library={voices:result.voices||[]};render();
    }catch(error){if(request===revision)status('Saved voices unavailable: '+error.message);}
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
