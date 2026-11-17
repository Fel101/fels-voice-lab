// The recipe editor sends the same typed step list as the automation API.
var automationOperations = {
  speak: ['text','audio','Speak text',{voice:['Voice name','', 'text'],speed:['Speed','1','number'],provider:['Provider','local-voice-lab',['local-voice-lab']]}],
  transcribe: ['audio','text','Transcribe audio',{language:['Language code (optional)','','text']}],
  translate: ['text','text','Translate text',{source_language:['Source language','en','text'],
    target_language:['Target language','es','text'],provider:['Provider','',['']]}],
  normalize: ['audio','audio','Master audio',{target_lufs:['Target LUFS','-18','number'],
    ceiling_dbtp:['True-peak ceiling','-1','number'],precision:['WAV encoding','PCM_24',['PCM_16','PCM_24','FLOAT']]}],
  convert: ['audio','audio','Convert voice',{identity_id:['Approved Cast voice','',[]]}],
  dub: ['scene','dub_job','Send to dub queue',{target_language:['Target language','es','text'],
    format:['Output format','wav',['wav','mp4']],render_missing:['Render missing cues',true,'checkbox']}],
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
  var sources = [{label:'Original '+kind,kind:kind,index:0}];
  automationRecipe.steps.forEach(function(row,index) {
    var desc = automationOperations[row.operation];
    var fieldset = document.createElement('fieldset'), title = document.createElement('legend');
    title.textContent = 'Step '+(index+1)+' · '+desc[2]; fieldset.append(title);
    var edge = document.createElement('p');
    edge.className='meta';
    edge.textContent = sources[row.source_step ?? index].label+' → '+desc[2];
    fieldset.append(edge);
    sources.push({label:'Step '+(index+1)+' · '+desc[2],kind:desc[1],index:index+1});
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
          option(select, '', automationConversionVoices.length ? 'Choose a voice' : 'Approve a voice recording in Cast');
          if (saved && !automationConversionVoices.some(function(voice) {return voice.id === saved;})) {
            option(select, saved, 'Saved voice is unavailable', true);
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
      var remove = document.createElement('button'); remove.type='button'; remove.textContent='Remove step';
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
  $('automationAdd').disabled = !ops.options.length || automationRecipe.steps.length>=8;
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
  var chooser=$('automationChoice'); chooser.textContent=''; option(chooser,'','New recipe');
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
    if (!name || !capabilities.length) throw new Error('Enter a name and select what this provider can do.');
    var result=await fetchJson('/api/automation/providers',{profile:{id:'provider-'+name,
      base_url:$('automationProviderUrl').value.trim(),model:$('automationProviderModel').value.trim(),capabilities:capabilities},
      key:key.value});
    key.value='';
    status.textContent='Profile saved for '+result.provider.destination+'.';
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
      var stop=document.createElement('button'); stop.type='button'; stop.textContent='Cancel unfinished items';
      stop.addEventListener('click',async function(){
        try {await fetchJson('/api/automation/runs/'+run.id+'/cancel',{});await refreshAutomationRuns();}
        catch(error){automationError(error);}
      });
      panel.append(stop);
    }
    run.items.forEach(function(row){
      var li=document.createElement('li');
      li.textContent='Item '+(row.ordinal+1)+' · '+row.state+(row.error?' · '+row.error:'');
      row.steps.forEach(function(step){
        if(step.kind==='audio') {
          var link=document.createElement('a'); link.href='/api/automation/assets/'+step.value;
          link.download='step-'+step.sequence+'-audio.wav'; link.textContent=' Download step '+step.sequence+' audio';
          li.append(link);
        }
        if(step.kind==='error'||step.kind==='blocked') li.append(' · Step '+step.sequence+' '+step.error);
        if(step.kind==='text') li.append(' · '+step.value);
        if(step.kind==='dub_job') li.append(' · Dub job '+step.value);
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
    if(!response.ok) throw new Error(result.error || 'Audio upload failed.');
    values.push(result.asset_id);
  }
  return values;
}
async function automationAction(action) {
  if(automationBusy) return;
  automationBusy=true;
  try { await action(); } catch(error){automationError(error);}
  finally {automationBusy=false;}
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
    automationRecipe=result.recipe; $('automationStatus').textContent='Recipe saved.';
    await refreshAutomation();
  });
});
$('automationPreview').addEventListener('click',function(){
  automationAction(async function(){
    var inputs=await localAutomationInputs(), result=await fetchJson('/api/automation/preview',{recipe:automationDocument(),inputs:inputs});
    var list=$('automationPlan'); list.hidden=false; list.textContent='';
    result.plan.recipe.steps.forEach(function(step,index) {
      var li=document.createElement('li');
      li.textContent=(step.source_step ? 'Step '+step.source_step : 'Original input')+' → Step '+(index+1)+' · '+
        automationOperations[step.operation][2]+' · '+step.provider+' · '+step.destination+
        (step.transfer?' · Sends '+step.transfer+' to '+step.connector.destination:'');
      list.append(li);
    });
    $('automationStatus').textContent=result.plan.inputs.length+' inputs. '+(result.plan.external_transfer?
      'Review the external destinations shown above before running.':'All processing stays local.');
  });
});
$('automationRun').addEventListener('click',function(){
  automationAction(async function(){
    if(!automationRecipe.id) throw new Error('Save the recipe before running it.');
    var inputs=await localAutomationInputs();
    var body={recipe_id:automationRecipe.id,revision:automationRecipe.revision,inputs:inputs};
    var preview=await fetchJson('/api/automation/preview',body), approved=[];
    if(preview.plan.provider_approvals.length) {
      var list=preview.plan.provider_approvals.map(function(row){return row.destination+' will receive '+row.data;});
      if(!window.confirm('Run this recipe with the selected providers?\n'+list.join('\n'))) return;
      approved=preview.plan.provider_approvals.map(function(row){return row.revision;});
    }
    await fetchJson('/api/automation/runs',{...body,approve_provider_revisions:approved});
    $('automationStatus').textContent='Run queued.';
    await refreshAutomationRuns();
  });
});
$('automationRefresh').addEventListener('click',function(){refreshAutomationRuns().catch(automationError);});
$('simulationListen').addEventListener('click',async function(){
  var state=$('simulationWords'), file=/** @type {HTMLInputElement} */ ($('simulationFile')).files[0];
  if(!file){state.textContent='Choose a recording first.'; return;}
  try {
    state.textContent='Transcribing locally…';
    var form=new FormData();form.append('model','whisper-1');form.append('file',file);
    var reply=await fetch('/v1/audio/transcriptions',{method:'POST',body:form}), result=await reply.json();
    if(!reply.ok) throw new Error(result.error.message);
    state.textContent=result.text;
  } catch(error){state.textContent=error.message;}
});
$('simulationPlay').addEventListener('click',async function(){
  var state=$('simulationStatus');
  try {
    state.textContent='Preparing the draft reply…';
    var reply=await fetch('/v1/audio/speech',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:'tts-1',voice:$('simulationVoice').value,input:$('simulationReply').value,response_format:'wav'})});
    if(!reply.ok) throw new Error((await reply.json()).error.message);
    if(simulationUrl) URL.revokeObjectURL(simulationUrl);
    simulationUrl=URL.createObjectURL(await reply.blob());
    var audio=/** @type {HTMLAudioElement} */ ($('simulationPlayer'));
    audio.src=simulationUrl; audio.hidden=false;
    state.textContent='Reply ready to play.';
  } catch(error){state.textContent=error.message;}
});
automationForm();
