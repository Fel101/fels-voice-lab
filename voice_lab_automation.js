// The recipe editor sends the same typed step list as the automation API.
var automationOperations = {
  speak: ['text','audio',LabText.t('automation.speak_text'),{voice:[LabText.t('automation.voice_name'),'', 'text'],speed:[LabText.t('automation.speed'),'1','number'],provider:[LabText.t('automation.provider'),'local-voice-lab',['local-voice-lab']]}],
  transcribe: ['audio','text',LabText.t('automation.transcribe_audio'),{language:[LabText.t('automation.language_code_optional'),'','text']}],
  translate: ['text','text',LabText.t('automation.translate_text'),{source_language:[LabText.t('automation.source_language'),'en','text'],
    target_language:[LabText.t('automation.target_language'),'es','text'],provider:[LabText.t('automation.provider'),'',['']]}],
  normalize: ['audio','audio',LabText.t('automation.master_audio'),{target_lufs:[LabText.t('automation.target_lufs'),'-18','number'],
    ceiling_dbtp:[LabText.t('automation.true_peak_ceiling'),'-1','number'],precision:[LabText.t('automation.wav_encoding'),'PCM_24',['PCM_16','PCM_24','FLOAT']]}],
  convert: ['audio','audio',LabText.t('automation.convert_voice'),{identity_id:[LabText.t('automation.approved_cast_voice'),'',[]]}],
  dub: ['scene','dub_job',LabText.t('automation.send_to_dub_queue'),{target_language:[LabText.t('automation.target_language'),'es','text'],
    format:[LabText.t('automation.output_format'),'wav',['wav','mp4']],render_missing:[LabText.t('automation.render_missing_cues'),true,'checkbox']}],
};
var automationRecipe = {id:'', revision:0, /** @type {any[]} */ steps:[]};
var configuredAutomationProviders = [], automationConversionVoices = [];
var automationSaved = [], automationBusy = false, simulationUrl = '';
function automationError(error) { $('automationStatus').textContent = error.message || String(error); }
function automationDocument() {
  return {schema_version:1,name:$('automationName').value,input_kind:$('automationKind').value,steps:automationRecipe.steps.map(function(row){
    return {operation:row.operation,source_step:row.source_step,settings:row.settings};
  }),
    ...(automationRecipe.id?{id:automationRecipe.id,revision:automationRecipe.revision}:{})};
}
function automationForm() {
  var kind = $('automationKind').value, area = $('automationSteps');
  area.textContent = '';
  $('automationTextRow').hidden = kind !== 'text';
  $('automationFileRow').hidden = kind !== 'audio';
  $('automationSceneRow').hidden = kind !== 'scene';
  $('automationKind').disabled = !!automationRecipe.steps.length;
  var inputKind = {text: LabText.t('automation.text_kind'), audio: LabText.t('automation.audio_kind'), scene: LabText.t('automation.scene_kind')};
  var sources = [{label:LabText.t('automation.original', {kind: inputKind[kind] || kind}),kind:kind,index:0}];
  automationRecipe.steps.forEach(function(row,index) {
    var desc = automationOperations[row.operation];
    var fieldset = document.createElement('fieldset'), title = document.createElement('legend');
    title.textContent = LabText.t('automation.step', {number: index+1, operation: desc[2]}); fieldset.append(title);
    var edge = document.createElement('p');
    edge.className='meta';
    edge.textContent = sources[row.source_step ?? index].label+' → '+desc[2];
    fieldset.append(edge);
    sources.push({label:LabText.t('automation.step', {number: index+1, operation: desc[2]}),kind:desc[1],index:index+1});
    Object.entries(desc[3]).forEach(function([name, details]) {
      var label = document.createElement('label'); label.className = 'field'; label.textContent = details[0];
      if (Array.isArray(details[2])) {
        var select = document.createElement('select');
        var choices = details[2];
        if(name==='provider') choices=choices.concat(configuredAutomationProviders.filter(function(provider){
          return provider.capabilities.includes(row.operation);
        }).map(function(provider){return provider.id;}));
        if (name === 'identity_id') {
          var saved = row.settings[name];
          option(select, '', automationConversionVoices.length ? LabText.t('automation.choose_a_voice') : LabText.t('automation.approve_a_voice_recording_in'));
          if (saved && !automationConversionVoices.some(function(voice) {return voice.id === saved;})) {
            option(select, saved, LabText.t('automation.saved_voice_is_unavailable'), true);
          }
          automationConversionVoices.forEach(function(voice) {option(select, voice.id, voice.label);});
        } else choices.forEach(function(value) {option(select,value,value);});
        select.value = row.settings[name];
        select.addEventListener('change',function(){ row.settings[name]=select.value; });
        label.append(select);
      } else {
        var field = document.createElement('input'); field.type = details[2];
        if (name==='voice') field.setAttribute('list','automationVoices');
        if (field.type==='number') field.step='any';
        if (field.type==='checkbox') field.checked=!!row.settings[name]; else field.value=String(row.settings[name] ?? '');
        field.addEventListener('input',function(){
          row.settings[name]=field.type==='checkbox'?field.checked:field.type==='number'?Number(field.value):field.value;
        });
        label.append(field);
      }
      fieldset.append(label);
    });
    if (index===automationRecipe.steps.length-1) {
      var remove = document.createElement('button'); remove.type='button'; remove.textContent=LabText.t('automation.remove_step');
      remove.addEventListener('click',function(){automationRecipe.steps.pop();automationForm();});
      fieldset.append(remove);
    }
    area.append(fieldset);
  });
  var select=$('automationSource'), prior=select.value;
  select.textContent='';
  var usable=sources.filter(function(node){
    return Object.values(automationOperations).some(function(spec){return spec[0]===node.kind;});
  });
  usable.forEach(function(node){option(select,String(node.index),node.label);});
  var latest=usable[usable.length-1];
  select.value=usable.some(function(node){return String(node.index)===prior;})?prior:(latest?String(latest.index):'');
  if (latest) fillAutomationOperations(sources[Number(select.value)].kind);
}
function fillAutomationOperations(kind) {
  var ops = $('automationOptions'); ops.textContent='';
  Object.keys(automationOperations).filter(function(key){return automationOperations[key][0]===kind;})
    .forEach(function(key){option(ops,key,automationOperations[key][2]);});
  updateAutomationActions();
}
function updateAutomationActions() {
  ['automationSave','automationPreview','automationRun'].forEach(function(id){
    $(id).disabled = automationBusy;
  });
  $('automationAdd').disabled = automationBusy || !$('automationOptions').options.length || automationRecipe.steps.length>=8;
}
async function refreshAutomation() {
  var [recipes, projects, voices, profiles, cast] = await Promise.all([
    fetchJson('/api/automation/recipes'), fetchJson('/api/projects?kind=dub'), fetchJson('/api/voices'),
    fetchJson('/api/automation/providers'), fetchJson('/api/cast')
  ]);
  configuredAutomationProviders=profiles.providers;
  automationSaved=recipes.recipes;
  automationConversionVoices = cast.characters.flatMap(function(character) {
    return character.identities.filter(function(voice) {return voice.approved_recording_available;})
      .map(function(voice) {return {id: voice.id, label: character.name + ' · ' + voice.label};});
  });
  automationForm();
  var chooser=$('automationChoice'); chooser.textContent=''; option(chooser,'',LabText.t('automation.new_recipe'));
  automationSaved.forEach(function(row){option(chooser,row.id,row.name);});
  chooser.value=automationRecipe.id;
  var scenes=$('automationScene'); scenes.textContent='';
  projects.projects.forEach(function(row){option(scenes,row.id,row.title);});
  var names=$('automationVoices'); names.textContent='';
  voices.voices.filter(function(v){return v.ready;}).forEach(function(v){option(names,v.name,v.name);});
  await refreshAutomationRuns();
}
$('automationProviderForm').addEventListener('submit',async function(event) {
  event.preventDefault();
  var status=$('automationProviderNote'), capabilities=[];
  if ($('automationProviderTranslate').checked) capabilities.push('translate');
  if ($('automationProviderSpeak').checked) capabilities.push('speak');
  var name=$('automationProviderName').value.trim().toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'');
  var key=$('automationProviderKey');
  try {
    if (!name || !capabilities.length) throw new Error(LabText.t('automation.enter_a_name_and_select'));
    var result=await fetchJson('/api/automation/providers',{profile:{id:'provider-'+name,
      base_url:$('automationProviderUrl').value.trim(),model:$('automationProviderModel').value.trim(),capabilities:capabilities},
      key:key.value});
    key.value='';
    status.textContent=LabText.t('automation.profile_saved_for', {destination: result.provider.destination});
    await refreshAutomation();
  } catch(error) {
    status.textContent=error.message;
  }
});
async function refreshAutomationRuns() {
  var payload=await fetchJson('/api/automation/runs'), target=$('automationRuns');
  target.textContent='';
  payload.runs.forEach(function(run) {
    var panel=document.createElement('details'), heading=document.createElement('summary'), list=document.createElement('ul');
    heading.textContent=run.recipe.name+' · '+run.status.replaceAll('_',' ');
    panel.append(heading);
    if(run.status==='running') {
      var stop=document.createElement('button'); stop.type='button'; stop.textContent=LabText.t('automation.cancel_unfinished_items');
      stop.addEventListener('click',async function(){
        try {await fetchJson('/api/automation/runs/'+run.id+'/cancel',{});await refreshAutomationRuns();}
        catch(error){automationError(error);}
      });
      panel.append(stop);
    }
    run.items.forEach(function(row){
      var li=document.createElement('li');
      li.textContent=(LabText.t('automation.item', {ordinal: row.ordinal+1, state: row.state}) + (row.error ? ' · ' + row.error : ''));
      row.steps.forEach(function(step){
        if(step.kind==='audio') {
          var link=document.createElement('a'); link.href='/api/automation/assets/'+step.value;
          link.download='step-'+step.sequence+'-audio.wav'; link.textContent=LabText.t('automation.download_step_audio', {sequence: step.sequence});
          li.append(link);
        }
        if(step.kind==='error'||step.kind==='blocked') li.append(LabText.t('automation.step_2', {sequence: step.sequence, error: step.error}));
        if(step.kind==='text') li.append(' · '+step.value);
        if(step.kind==='dub_job') li.append(LabText.t('automation.dub_job', {job: step.value}));
      });
      list.append(li);
    });
    panel.append(list); target.append(panel);
  });
}
async function localAutomationInputs() {
  var kind=$('automationKind').value;
  if (kind==='text') return $('automationText').value.split(/\r?\n/).map(function(row){return row.trim();}).filter(Boolean);
  if (kind==='scene') return $('automationScene').value ? [$('automationScene').value] : [];
  var files = Array.from(/** @type {HTMLInputElement} */ ($('automationFiles')).files || []);
  var values=[];
  for (const file of files) {
    var response=await fetch('/api/automation/assets?filename='+encodeURIComponent(file.name),{method:'POST',
      headers:{'Content-Type':'application/octet-stream'},body:file});
    var result=await response.json();
    if(!response.ok) throw new Error(result.error || LabText.t('automation.audio_upload_failed'));
    values.push(result.asset_id);
  }
  return values;
}
async function automationAction(action) {
  if(automationBusy) return;
  automationBusy=true;
  updateAutomationActions();
  try { await action(); } catch(error){automationError(error);}
  finally {
    automationBusy=false;
    updateAutomationActions();
  }
}
$('automationSource').addEventListener('change',function(){
  var index=Number($('automationSource').value);
  fillAutomationOperations(index===0?$('automationKind').value:automationOperations[automationRecipe.steps[index-1].operation][1]);
});
$('automationKind').addEventListener('change',automationForm);
$('automationChoice').addEventListener('change',function(){
  var saved=automationSaved.find(function(row){return row.id===$('automationChoice').value;});
  automationRecipe=saved?structuredClone(saved):{id:'',revision:0,steps:[]};
  $('automationName').value=saved?saved.name:'';
  $('automationKind').value=saved?saved.input_kind:'text';
  automationForm();
});
$('automationAdd').addEventListener('click',function(){
  var op=$('automationOptions').value, desc=automationOperations[op];
  if(!desc) return;
  automationRecipe.steps.push({operation:op,source_step:Number($('automationSource').value),
    settings:Object.fromEntries(Object.entries(desc[3])
    .map(function([key,field]){return [key,field[2]==='number'?Number(field[1]):field[1]];}))});
  $('automationSource').value='';
  automationForm();
});
$('automationSave').addEventListener('click',function(){
  automationAction(async function(){
    var result=await fetchJson('/api/automation/recipes',automationDocument());
    automationRecipe=result.recipe; $('automationStatus').textContent=LabText.t('automation.recipe_saved');
    await refreshAutomation();
  });
});
$('automationPreview').addEventListener('click',function(){
  automationAction(async function(){
    var inputs=await localAutomationInputs(), result=await fetchJson('/api/automation/preview',{recipe:automationDocument(),inputs:inputs});
    var list=$('automationPlan'); list.hidden=false; list.textContent='';
    result.plan.recipe.steps.forEach(function(step,index) {
      var li=document.createElement('li');
      li.textContent=(LabText.t('automation.step_3', {source: step.source_step ? LabText.t('automation.source_step', {number: step.source_step}) : LabText.t('automation.original_input'), number: index+1, operation: automationOperations[step.operation][2], provider: step.provider, destination: step.destination}) + (step.transfer ? ' · ' + LabText.t('automation.sends_to', {transfer:step.transfer,destination:step.connector.destination}) : ''));
      list.append(li);
    });
    $('automationStatus').textContent=LabText.t('automation.inputs', {inputs: result.plan.inputs.length, transfer_note: result.plan.external_transfer?
      LabText.t('automation.review_external_destinations'):LabText.t('automation.keeps_processing_local')});
  });
});
$('automationRun').addEventListener('click',function(){
  automationAction(async function(){
    if(!automationRecipe.id) throw new Error(LabText.t('automation.save_the_recipe_before_running'));
    var inputs=await localAutomationInputs();
    var body={recipe_id:automationRecipe.id,revision:automationRecipe.revision,inputs:inputs};
    var preview=await fetchJson('/api/automation/preview',body), approved=[];
    if(preview.plan.provider_approvals.length) {
      var list=preview.plan.provider_approvals.map(function(row){return LabText.t('automation.will_receive', {destination: row.destination, data: row.data});});
      if(!window.confirm(LabText.t('automation.run_this_recipe_with_the', {destinations: list.join('\n')}))) return;
      approved=preview.plan.provider_approvals.map(function(row){return row.revision;});
    }
    await fetchJson('/api/automation/runs',{...body,approve_provider_revisions:approved});
    $('automationStatus').textContent=LabText.t('automation.run_queued');
    await refreshAutomationRuns();
  });
});
$('automationRefresh').addEventListener('click',function(){refreshAutomationRuns().catch(automationError);});
$('simulationListen').addEventListener('click',async function(){
  var state=$('simulationWords'), file=/** @type {HTMLInputElement} */ ($('simulationFile')).files[0];
  if(!file){state.textContent=LabText.t('automation.choose_a_recording_first'); return;}
  try {
    state.textContent=LabText.t('automation.transcribing_locally');
    var form=new FormData();form.append(LabText.t('automation.model_2'),LabText.t('automation.whisper_1'));form.append(LabText.t('automation.file'),file);
    var reply=await fetch('/v1/audio/transcriptions',{method:'POST',body:form}), result=await reply.json();
    if(!reply.ok) throw new Error(result.error.message);
    state.textContent=result.text;
  } catch(error){state.textContent=error.message;}
});
$('simulationPlay').addEventListener('click',async function(){
  var state=$('simulationStatus');
  try {
    state.textContent=LabText.t('automation.preparing_the_draft_reply');
    var reply=await fetch('/v1/audio/speech',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:'tts-1',voice:$('simulationVoice').value,input:$('simulationReply').value,response_format:'wav'})});
    if(!reply.ok) throw new Error((await reply.json()).error.message);
    if(simulationUrl) URL.revokeObjectURL(simulationUrl);
    simulationUrl=URL.createObjectURL(await reply.blob());
    var audio=/** @type {HTMLAudioElement} */ ($('simulationPlayer'));
    audio.src=simulationUrl; audio.hidden=false;
    state.textContent=LabText.t('automation.reply_ready_to_play');
  } catch(error){state.textContent=error.message;}
});
automationForm();
