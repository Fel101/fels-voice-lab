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
  const status=el('p','Loading Drama studio…',{'role':'status','class':'ds-status'});root.append(status);
  async function api(path,body) {
    const response=await fetch(base+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();if(!response.ok)throw new Error(data.error||'Drama studio request failed');return data;
  }
  let info;try {info=await api('/info');}catch(e){status.textContent=e.message;delete root.dataset.dramaMounted;return;}
  const button=(text,action)=>{const n=el('button',text,{type:'button'});n.onclick=()=>Promise.resolve().then(action).catch(e=>status.textContent=e.message);return n;};
  const label=(text,node)=>{const n=el('label');n.append(el('span',text),node);return n;};
  const select=items=>{const n=el('select');for(const [value,text] of items)n.append(el('option',text,{value}));return n;};
  const heading=el('h3','Drama scene & narration studio');
  const help=el('p','CPU production mode · up to 16 voice slots · 8,000 characters / ten minutes of audio. A cold take can take several minutes to start. Language follows the script.');
  const script=el('textarea',null,{'aria-label':'Drama script',maxlength:String(info.max_text),placeholder:'<|speaker:0|> [whispers] Keep your lantern close.\n<|speaker:1|> The gate is open.'});
  const cueBar=el('div',null,{class:'ds-row','aria-label':'Inline acting cues'});
  const preview=el('p','',{'class':'ds-status','aria-label':'Spoken words preview'});
  function updatePlain(){preview.textContent='Words for captions/checks: '+plainScript(script.value);}
  script.oninput=updatePlain;
  function insert(token){const r=insertCue(script.value,script.selectionStart,script.selectionEnd,token);script.value=r.text;
    script.focus();script.setSelectionRange(r.cursor,r.cursor);updatePlain();}
  for(const cue of info.cues)cueBar.append(button('['+cue+']',()=>insert('['+cue+']')));
  const custom=el('input',null,{'aria-label':'Custom acting cue',maxlength:'160',placeholder:'Custom delivery cue'});
  cueBar.append(custom,button('Insert cue',()=>{if(!custom.value.trim()||/[\[\]<>\r\n]/.test(custom.value))throw new Error('Use plain text for the delivery cue.');insert('['+custom.value.trim()+']');}));
  const cast=el('div',null,{'aria-label':'Drama cast slots'}),castRows=[];
  function sourceOptions(chosen='') {return [['','Model-selected voice'],...info.sources.map(s=>[s.id,s.label]),
    ...(chosen&&!info.sources.some(s=>s.id===chosen)?[[chosen,'Previously selected voice (refresh or replace)']]:[])];}
  function addCast(speaker,source='') {
    if(castRows.length>=16)throw new Error('All 16 cast slots are already present.');
    const row=el('div',null,{class:'ds-cast'}),slot=select(Array.from({length:16},(_,i)=>[String(i),'Speaker '+i])),voice=select(sourceOptions(source));
    slot.value=String(speaker);voice.value=source;slot.setAttribute('aria-label','Speaker index');voice.setAttribute('aria-label','Saved cast voice');
    const entry={row,slot,voice};castRows.push(entry);
    const remove=button('Remove',()=>{row.remove();castRows.splice(castRows.indexOf(entry),1);});
    row.append(slot,voice,remove);cast.append(row);
  }
  const castActions=el('div',null,{class:'ds-row'});
  castActions.append(button('Add cast slot',()=>{let i=0;while(castRows.some(r=>Number(r.slot.value)===i))i++;addCast(i);}),
    button('Insert speaker tag',()=>insert('<|speaker:'+Number(castRows.at(-1)?.slot.value||0)+'|>')),
    button('Refresh saved voices',async()=>{info=await api('/info');for(const row of castRows){const chosen=row.voice.value;row.voice.replaceChildren(...sourceOptions(chosen).map(([v,t])=>el('option',t,{value:v})));row.voice.value=chosen;}paintHistory(info.jobs);}));
  const allow=el('input',null,{type:'checkbox'}),listen=el('input',null,{type:'checkbox'});listen.checked=true;
  const allowLabel=label('Allow model-selected voices for unassigned speaking slots',allow);allowLabel.className='ds-check';
  const listenLabel=label('Play audio as it arrives',listen);listenLabel.className='ds-check';
  const language=select(info.languages.map(l=>[l.code,l.label]));language.setAttribute('aria-label','Script language');
  const description=el('input',null,{maxlength:'1000','aria-label':'Global voice description',placeholder:'Optional voice description for generated voices'});
  const direction=el('input',null,{maxlength:'1000','aria-label':'Global delivery cue',placeholder:'Optional direction for the whole script'});
  const controls=el('div',null,{class:'ds-grid'}),settings={};
  for(const [key,title,value,min,max,step] of [['seed','Seed',42,0,2147483647,1],['temperature','Temperature',1,.05,2,.05],
    ['top_k','Top-k',30,1,4097,1],['top_p','Top-p',.9,.05,1,.05],['max_seconds','Seconds per chunk',47.6,.5,95.1,.1]]) {
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
  const outputs=el('div'),actions=el('div',null,{class:'ds-row'}),history=select([['','Recent takes']]);history.setAttribute('aria-label','Recent Drama takes');
  function paintHistory(jobs){history.replaceChildren(el('option','Recent takes',{value:''}),...jobs.map(j=>el('option',new Date(j.created*1000).toLocaleString()+' · '+j.status,{value:j.id})));}
  paintHistory(info.jobs);
  function completed(row){outputs.replaceChildren();if(row.status!=='complete')return;
    outputs.append(el('audio',null,{controls:'',preload:'none',src:base+'/jobs/'+row.id+'/audio','aria-label':'Completed Drama take'}));
    const links=el('div',null,{class:'ds-row'});
    for(const [kind,title] of [['audio','Download WAV'],['metadata','Model & cast receipt'],['package','Download take ZIP']])links.append(el('a',title,{href:base+'/jobs/'+row.id+'/'+kind,download:''}));
    outputs.append(links,el('p','Mixed cast audio. Individual speaker stems and word timing are not generated. Review the words, speaker identity and acting before using the take.'));
    if(row.quality?.flags?.length)outputs.append(el('p','Review flags: '+row.quality.flags.join(', '),{role:'alert'}));
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
        status.textContent=current.status+(current.error?' — '+current.error:'')+'\n'+(data.available_bytes/88200).toFixed(1)+' s of preview received.';
        if(current.first_audio_seconds!=null)status.textContent+=' First audio: '+current.first_audio_seconds+' s.';
        if(terminal(current.status)&&offset>=data.available_bytes){start.disabled=false;stop.disabled=true;completed(current);return;}
        await new Promise(resolve=>setTimeout(resolve,500));
      }
    } catch(e){status.textContent=e.message+' — refresh recent takes to reconnect.';start.disabled=false;stop.disabled=true;}
  }
  const start=button('Generate & audition',async()=>{
    start.disabled=true;status.textContent='Preparing the pinned CPU runtime and saved cast…';
    try {
    if(listen.checked)await audioContext();stopListening();outputs.replaceChildren();
    const values=Object.fromEntries(Object.entries(settings).map(([k,n])=>[k,Number(n.value)]));
    const slots=castRows.filter(r=>r.voice.value).map(r=>({speaker:Number(r.slot.value),source_id:r.voice.value}));
    const data=await api('/jobs',{text:script.value,cast:slots,settings:values,language:language.value,
      description:description.value,direction:direction.value,allow_unassigned:allow.checked});
    current=data.job;offset=0;start.disabled=true;stop.disabled=false;poll(current.id,++generation);
    }catch(e){start.disabled=false;throw e;}
  });
  const stop=button('Stop take',async()=>{if(!current)return;stopListening();listen.checked=false;await api('/jobs/'+current.id+'/cancel',{});status.textContent='Stopping the owned CPU worker…';});stop.disabled=true;
  history.onchange=()=>{if(!history.value)return;stopListening();listen.checked=false;outputs.replaceChildren();offset=0;current={id:history.value};stop.disabled=false;start.disabled=true;poll(history.value,++generation,true);};
  actions.append(start,stop,history);
  root.replaceChildren(heading,help,label('Script language (does not translate)',language),label('Voice description',description),label('Global delivery',direction),
    el('h4','Cast'),cast,castActions,allowLabel,el('h4','Script & acting'),cueBar,
    el('p','Insert a cue or speaker tag at the cursor. Delivery cues apply from that point; use another cue to change delivery. Cues may be ignored or spoken.'),
    label('Dialogue or narration',script),preview,controls,listenLabel,actions,status,outputs,
    el('p','Jobs and scripts are retained locally. You can reopen recent takes after closing this panel. Previous-take editing is unavailable in the installed runtime. Built with Fish Audio.'));
  status.textContent=info.sources.length+' approved reference voices available. Use the regular voice workflow to save and approve more voices.';
  return {stopListening(){listen.checked=false;stopListening();}};
}
