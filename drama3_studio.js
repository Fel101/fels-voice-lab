// Drama studio for Voice Lab. All markup is inserted as text, never HTML.
export function insertCue(text, start, end, token) {
  return {text:text.slice(0,start)+token+' '+text.slice(start),cursor:start+token.length+1};
}
export function plainScript(text) {
  return text.replace(/<\|speaker:\d+\|>|\[[^\[\]\r\n]{1,160}\]/g,' ').replace(/\s+/g,' ').trim();
}
const terminal = status => ['complete','cancelled','failed','interrupted'].includes(status);
export async function mountDramaStudio(root, {base='/api/drama3'}={}) {
  if (root.dataset.dramaMounted) return;
  root.dataset.dramaMounted='true';root.classList.add('drama-studio');
  const el=(tag,text,attrs={})=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;
    for(const [key,value] of Object.entries(attrs))n.setAttribute(key,value);return n;};
  if (!document.getElementById('dramaStudioStyles')) {
    const css=el('style',`.drama-studio{max-width:100%;min-width:0}.drama-studio .ds-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}.drama-studio label{display:flex;flex-direction:column;gap:5px;margin:8px 0}.drama-studio input,.drama-studio select,.drama-studio textarea{box-sizing:border-box;max-width:100%;width:100%}.drama-studio textarea{min-height:160px;resize:vertical}.drama-studio .ds-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.drama-studio .ds-cast{display:grid;grid-template-columns:110px minmax(100px,1fr) auto;gap:8px;margin:8px 0}.drama-studio .ds-check{flex-direction:row;align-items:center}.drama-studio .ds-check input{width:auto}.drama-studio .ds-status{white-space:pre-wrap;overflow-wrap:anywhere}.drama-studio audio{max-width:100%;width:100%}.drama-studio button{cursor:pointer}.drama-studio-dialog{box-sizing:border-box;width:min(1000px,95vw);max-height:90vh;overflow:auto;color:inherit;background:var(--bg,#20232b);border:1px solid #777;border-radius:12px;padding:20px}.drama-studio-dialog::backdrop{background:#0009}@media(max-width:520px){.drama-studio .ds-cast{grid-template-columns:80px minmax(100px,1fr)}.drama-studio .ds-cast button{grid-column:2}}`);
    css.id='dramaStudioStyles';document.head.append(css);
  }
  const status=el('p',LabText.t('studio.loading_drama_studio'),{'role':'status','class':'ds-status'});root.append(status);
  async function api(path,body) {
    const response=await fetch(base+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||LabText.t('studio.drama_studio_request_failed'));return data;
  }
  let info;try {info=await api('/info');}catch(e){status.textContent=e.message;delete root.dataset.dramaMounted;return;}
  const button=(text,action)=>{const n=el('button',text,{type:'button'});n.onclick=()=>Promise.resolve().then(action).catch(e=>status.textContent=e.message);return n;};
  const label=(text,node)=>{const n=el('label');n.append(el('span',text),node);return n;};
  const select=items=>{const n=el('select');for(const [value,text] of items)n.append(el('option',text,{value}));return n;};
  const heading=el('h3',LabText.t('studio.drama_scene_narration_studio'));
  const help=el('p',LabText.t('studio.cpu_production_mode_up_to'));
  const script=el('textarea',null,{'aria-label':LabText.t('studio.drama_script'),maxlength:String(info.max_text),placeholder:LabText.t('studio.drama_example_script')});
  const cueBar=el('div',null,{class:'ds-row','aria-label':LabText.t('studio.inline_acting_cues')});
  const preview=el('p','',{'class':'ds-status','aria-label':LabText.t('studio.spoken_words_preview')});
  function updatePlain(){preview.textContent=LabText.t('studio.words_for_captions_checks', {plainScript: plainScript(script.value)});}
  script.oninput=updatePlain;
  function insert(token){const r=insertCue(script.value,script.selectionStart,script.selectionEnd,token);script.value=r.text;
    script.focus();script.setSelectionRange(r.cursor,r.cursor);updatePlain();}
  for(const cue of info.cues)cueBar.append(button('['+cue+']',()=>insert('['+cue+']')));
  const custom=el('input',null,{'aria-label':LabText.t('studio.custom_acting_cue'),maxlength:'160',placeholder:LabText.t('studio.custom_acting_direction')});
  cueBar.append(custom,button(LabText.t('studio.insert_cue'),()=>{if(!custom.value.trim()||/[\[\]<>\r\n]/.test(custom.value))throw new Error(LabText.t('studio.use_plain_text_for_the'));insert('['+custom.value.trim()+']');}));
  const cast=el('div',null,{'aria-label':LabText.t('studio.drama_cast_slots')}),castRows=[];
  function sourceOptions(chosen='') {return [['',LabText.t('studio.model_selected_voice')],...info.sources.map(s=>[s.id,s.label]),
    ...(chosen&&!info.sources.some(s=>s.id===chosen)?[[chosen,LabText.t('studio.previously_selected_voice_refresh_or')]]:[])];}
  function addCast(speaker,source='') {
    if(castRows.length>=16)throw new Error(LabText.t('studio.all_16_cast_slots_are'));
    const row=el('div',null,{class:'ds-cast'}),slot=select(Array.from({length:16},(_,i)=>[String(i),LabText.t('studio.speaker', {i: i})])),voice=select(sourceOptions(source));
    slot.value=String(speaker);voice.value=source;slot.setAttribute('aria-label',LabText.t('studio.speaker_index'));voice.setAttribute('aria-label',LabText.t('studio.saved_cast_voice'));
    const entry={row,slot,voice};castRows.push(entry);
    const remove=button(LabText.t('studio.remove'),()=>{row.remove();castRows.splice(castRows.indexOf(entry),1);});
    row.append(slot,voice,remove);cast.append(row);
  }
  const castActions=el('div',null,{class:'ds-row'});
  castActions.append(button(LabText.t('studio.add_cast_slot'),()=>{let i=0;while(castRows.some(r=>Number(r.slot.value)===i))i++;addCast(i);}),
    button(LabText.t('studio.insert_speaker_tag'),()=>insert('<|speaker:'+Number(castRows.at(-1)?.slot.value||0)+'|>')),
    button(LabText.t('studio.refresh_saved_voices'),async()=>{info=await api('/info');for(const row of castRows){const chosen=row.voice.value;row.voice.replaceChildren(...sourceOptions(chosen).map(([v,t])=>el('option',t,{value:v})));row.voice.value=chosen;}paintHistory(info.jobs);}));
  const allow=el('input',null,{type:'checkbox'}),listen=el('input',null,{type:'checkbox'});listen.checked=true;
  const allowLabel=label(LabText.t('studio.allow_model_selected_voices_for'),allow);allowLabel.className='ds-check';
  const listenLabel=label(LabText.t('studio.play_audio_as_it_arrives'),listen);listenLabel.className='ds-check';
  const language=select(info.languages.map(l=>[l.code,l.label]));language.setAttribute('aria-label',LabText.t('studio.script_language'));
  const description=el('input',null,{maxlength:'1000','aria-label':LabText.t('studio.global_voice_description'),placeholder:LabText.t('studio.optional_voice_description_for_generated')});
  const direction=el('input',null,{maxlength:'1000','aria-label':LabText.t('studio.acting_direction_for_the_whole'),placeholder:LabText.t('studio.optional_direction_for_the_whole')});
  const controls=el('div',null,{class:'ds-grid'}),settings={};
  for(const [key,title,value,min,max,step] of [['seed',LabText.t('studio.seed'),42,0,2147483647,1],['temperature',LabText.t('studio.temperature'),1,.05,2,.05],
    ['top_k',LabText.t('studio.top_k'),30,1,4097,1],['top_p',LabText.t('studio.top_p'),.9,.05,1,.05],['max_seconds',LabText.t('studio.seconds_per_chunk'),47.6,.5,95.1,.1]]) {
    const input=el('input',null,{type:'number',value:String(value),min:String(min),max:String(max),step:String(step),'aria-label':title});settings[key]=input;controls.append(label(title,input));
  }
  let current=null,offset=0,generation=0,context=null,nextTime=0,nodes=[];
  function stopListening(){for(const n of nodes){try{n.stop();}catch{}}nodes=[];nextTime=0;}
  async function audioContext(){context ||= new AudioContext();await context.resume();}
  listen.onchange=()=>{if(!listen.checked)stopListening();else audioContext().catch(e=>status.textContent=e.message);};
  function play(encoded) {
    if(!listen.checked||!context||context.state!=='running'||!encoded)return;
    const raw=atob(encoded),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0)),view=new DataView(bytes.buffer),samples=context.createBuffer(1,bytes.length/2,44100),channel=samples.getChannelData(0);
    for(let i=0;i<channel.length;i++)channel[i]=view.getInt16(i*2,true)/32768;
    const source=context.createBufferSource();source.buffer=samples;source.connect(context.destination);nextTime=Math.max(nextTime,context.currentTime+.05);
    source.start(nextTime);nextTime+=samples.duration;nodes.push(source);source.onended=()=>{nodes=nodes.filter(n=>n!==source);};
  }
  const outputs=el('div'),actions=el('div',null,{class:'ds-row'}),history=select([['',LabText.t('studio.recent_takes')]]);history.setAttribute('aria-label',LabText.t('studio.recent_drama_takes'));
  function paintHistory(jobs){history.replaceChildren(el('option',LabText.t('studio.recent_takes'),{value:''}),...jobs.map(j=>el('option',new Date(j.created*1000).toLocaleString(LabText.language)+' · '+j.status,{value:j.id})));}
  paintHistory(info.jobs);
  function completed(row){outputs.replaceChildren();if(row.status!=='complete')return;
    outputs.append(el('audio',null,{controls:'',preload:'none',src:base+'/jobs/'+row.id+'/audio','aria-label':LabText.t('studio.completed_drama_take')}));
    const links=el('div',null,{class:'ds-row'});
    for(const [kind,title] of [['audio',LabText.t('studio.download_wav')],['metadata',LabText.t('studio.model_cast_receipt')],['package',LabText.t('studio.download_take_zip')]])links.append(el('a',title,{href:base+'/jobs/'+row.id+'/'+kind,download:''}));
    outputs.append(links,el('p',LabText.t('studio.mixed_cast_audio_individual_speaker')));
    if(row.quality?.flags?.length)outputs.append(el('p',LabText.t('studio.review_flags', {join: row.quality.flags.join(', ')}),{role:'alert'}));
  }
  function restoreRequest(row) {
    if(!row.request)return;
    const r=row.request;script.value=r.text;description.value=r.description||'';direction.value=r.direction||'';
    language.value=r.language||'auto';allow.checked=!!r.allow_unassigned;
    for(const [key,input] of Object.entries(settings))if(r.settings?.[key]!=null)input.value=String(r.settings[key]);
    for(const row of castRows)row.row.remove();castRows.length=0;
    for(const slot of r.cast||[])addCast(slot.speaker,slot.source_id);
    updatePlain();
  }
  async function poll(id,version,restore=false) {
    try {
      while(root.isConnected&&version===generation) {
        const data=await api('/jobs/'+id+'?offset='+offset);if(version!==generation)return;
        current=data.job;offset=data.offset;
        if(restore){restoreRequest(current);restore=false;}
        if(!['cancelled','cancelling','failed','interrupted'].includes(current.status))play(data.pcm);
        status.textContent=LabText.t('studio.s_of_preview_received', {value: current.status+(current.error?' — '+current.error:''), toFixed: (data.available_bytes/88200).toFixed(1)});
        if(current.first_audio_seconds!=null)status.textContent+=LabText.t('studio.first_audio_s', {first_audio_seconds: current.first_audio_seconds});
        if(terminal(current.status)&&offset>=data.available_bytes){start.disabled=false;stop.disabled=true;completed(current);return;}
        await new Promise(resolve=>setTimeout(resolve,500));
      }
    } catch(e){status.textContent=LabText.t('studio.refresh_recent_takes_to_reconnect', {message: e.message});start.disabled=false;stop.disabled=true;}
  }
  const start=button(LabText.t('studio.generate_audition'),async()=>{
    start.disabled=true;status.textContent=LabText.t('studio.preparing_the_pinned_cpu_runtime');
    try {
    if(listen.checked)await audioContext();stopListening();outputs.replaceChildren();
    const values=Object.fromEntries(Object.entries(settings).map(([k,n])=>[k,Number(n.value)]));
    const slots=castRows.filter(r=>r.voice.value).map(r=>({speaker:Number(r.slot.value),source_id:r.voice.value}));
    const data=await api('/jobs',{text:script.value,cast:slots,settings:values,language:language.value,
      description:description.value,direction:direction.value,allow_unassigned:allow.checked});
    current=data.job;offset=0;start.disabled=true;stop.disabled=false;poll(current.id,++generation);
    }catch(e){start.disabled=false;throw e;}
  });
  const stop=button(LabText.t('studio.stop_take'),async()=>{if(!current)return;stopListening();listen.checked=false;await api('/jobs/'+current.id+'/cancel',{});status.textContent=LabText.t('studio.stopping_the_owned_cpu_worker');});stop.disabled=true;
  history.onchange=()=>{if(!history.value)return;stopListening();listen.checked=false;outputs.replaceChildren();offset=0;current={id:history.value};stop.disabled=false;start.disabled=true;poll(history.value,++generation,true);};
  actions.append(start,stop,history);
  root.replaceChildren(heading,help,label(LabText.t('studio.script_language_does_not_translate'),language),label(LabText.t('studio.voice_description'),description),label(LabText.t('studio.global_delivery'),direction),
    el('h4',LabText.t('studio.cast')),cast,castActions,allowLabel,el('h4',LabText.t('studio.script_acting')),cueBar,
    el('p',LabText.t('studio.insert_an_acting_direction_or')),
    label(LabText.t('studio.dialogue_or_narration'),script),preview,controls,listenLabel,actions,status,outputs,
    el('p',LabText.t('studio.jobs_and_scripts_are_retained')));
  status.textContent=LabText.t('studio.approved_voices_with_a_voice', {length: info.sources.length});
  return {stopListening(){listen.checked=false;stopListening();}};
}
