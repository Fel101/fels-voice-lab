const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const script=fs.readFileSync(path.join(__dirname,'..','voice_workflows.js'),'utf8');
function harness(){
  const nodes={},calls=[],requests=[];let ready,info={voices:[]};
  function node(id){
    if(nodes[id])return nodes[id];
    const n={id,value:'',textContent:'',checked:false,hidden:false,disabled:false,options:[],files:[],handlers:{},dataset:{},
      firstChild:{textContent:id},append(...items){this.options.push(...items);},
      appendChild(item){this.options.push(item);},insertAdjacentElement(){},setAttribute(){},removeAttribute(){this.src='';},
      pause(){},load(){},focus(){},addEventListener(type,fn){this.handlers[type]=fn;},
      trigger(type){return this.handlers[type]?.call(this);}};
    n.parentElement={firstChild:{textContent:id},insertAdjacentElement(){}};
    nodes[id]=n;return n;
  }
  const document={getElementById:node,createElement:()=>node('created-'+Object.keys(nodes).length),
    createTextNode:text=>({textContent:text}),querySelectorAll:()=>[],
    addEventListener(type,fn){if(type==='DOMContentLoaded')ready=fn;}};
  const entries=['drama-3','qwen3-tts-1.7b-base','turbo','orig','breeze-tts-2','voxcpm2'].map(id=>({id,name:id,integration:{service_api:true}}));
  const ctx=vm.createContext({document,location:{hash:''},navigator:{},TextEncoder,Uint8Array,crypto:webcrypto,
    File:class extends Blob{constructor(parts,name,options){super(parts,options);this.name=name;}},
    Blob,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},setTimeout,clearTimeout,btoa:s=>Buffer.from(s,'binary').toString('base64'),
    catalog:{engines:entries},referenceRows:[],addEventListener(){},
    option(select,value,text,disabled){select.options.push({value,text,disabled});if(select.options.length===1)select.value=value;},
    async fetchJson(url,body){calls.push({url,body});
      if(url==='/api/characters')return {character:{id:'character'}};
      if(url.includes('/identities'))return {identity:{id:'identity'}};
      if(url==='/api/info')return info;
      return {};
    },
    async fetch(url,options){requests.push({url,options});
      if(url.includes('/references')&&options?.method==='POST')return {ok:true,json:async()=>({reference:{id:'reference',version:1}})};
      if(url==='/api/performance')return {ok:true,blob:async()=>new Blob(['test'],{type:'audio/wav'})};
      return {ok:true,blob:async()=>new Blob(['source'])};
    },
    async decodeBlob(){return {duration:8,sampleRate:24000};},bufferAnalysis:()=>({rms:.2}),
    async refreshCast(){},async loadReferences(){},fillFixedVoices(){},selectedValues:()=>[]});
  ctx.window=ctx;
  vm.runInContext(script,ctx);ready();
  function fill(){
    node('cloneName').value='Test voice';node('cloneEngine').value='drama-3';
    node('cloneFile').files=[new ctx.File(['source'],'own-voice.wav',{type:'audio/wav'})];node('cloneFile').trigger('change');
    node('cloneTranscript').value='Exact recorded words.';node('cloneOrigin').value='My recording';
    node('cloneAllowedUse').value='Personal tests';node('clonePermission').checked=true;node('cloneText').value='New words.';
  }
  return {ctx,node,nodes,calls,requests,fill,setInfo(value){info=value;}};
}
test('upload saves a named immutable candidate and generates through the Cast API; retry reuses it',async()=>{
  const h=harness();h.fill();await h.node('cloneGenerate').trigger('click');
  assert.equal(h.calls.filter(c=>c.url==='/api/characters').length,1);
  assert.equal(h.requests.filter(r=>r.options?.headers?.['X-Reference-Metadata']).length,1);
  const upload=h.requests.find(r=>r.options?.headers?.['X-Reference-Metadata']);
  const metadata=JSON.parse(Buffer.from(upload.options.headers['X-Reference-Metadata'],'base64url').toString());
  assert.equal(metadata.transcript,'Exact recorded words.');assert.equal(metadata.edits.target_sample_rate,44100);
  const performance=h.requests.find(r=>r.url==='/api/performance');const body=JSON.parse(performance.options.body);
  assert.equal(body.identity_id,'identity');assert.equal(body.reference_version_id,'reference');assert.equal(body.text,'New words.');
  assert.equal(h.node('cloneResult').hidden,false);assert.equal(h.node('cloneControls').disabled,false);
  await h.node('cloneGenerate').trigger('click');
  assert.equal(h.calls.filter(c=>c.url==='/api/characters').length,1);
  assert.equal(h.requests.filter(r=>r.options?.headers?.['X-Reference-Metadata']).length,1);
  assert.equal(h.requests.filter(r=>r.url==='/api/performance').length,2);
  assert.ok(!h.calls.some(c=>c.url.includes('/update')),'no automatic approval');
});
test('permission and transcript errors create no orphan character or reference',async()=>{
  const h=harness();h.fill();h.node('clonePermission').checked=false;
  await h.node('cloneGenerate').trigger('click');assert.equal(h.calls.length,0);assert.equal(h.requests.length,0);
  h.node('clonePermission').checked=true;h.node('cloneTranscript').value='';
  await h.node('cloneSave').trigger('click');assert.equal(h.calls.length,0);assert.match(h.node('cloneStatus').textContent,/exact words/);
});
test('saved selection supplies its transcript and source notes; copying preserves inherited restrictions',async()=>{
  const h=harness();h.fill();
  const row={path:'/managed/ref.wav',name:'reference.wav',display_name:'Ada / neutral / v2',audio_url:'/preview',
    transcript:'Known words.',origin:'Generated by Breeze',allowed_use:'Non-commercial only',rights_status:'documented',duration_seconds:8};
  h.ctx.VoiceLabWorkflows.render([row]);h.node('cloneReference').value=row.path;h.node('cloneReference').trigger('change');
  assert.equal(h.node('cloneTranscript').value,'Known words.');assert.equal(h.node('clonePermission').checked,false);
  h.node('clonePermission').checked=true;h.node('cloneAllowedUse').value='My local audition';
  await h.node('cloneSave').trigger('click');
  const upload=h.requests.find(r=>r.options?.headers?.['X-Reference-Metadata']);
  const metadata=JSON.parse(Buffer.from(upload.options.headers['X-Reference-Metadata'],'base64url').toString());
  assert.match(metadata.allowed_use,/Non-commercial only/);
  h.node('qwenReference').value=row.path;h.node('qwenReference').trigger('change');
  assert.equal(h.node('qwenReferenceText').value,'Known words.');
  h.ctx.VoiceLabWorkflows.render([{...row,path:'/new.wav',transcript:'',origin:'',allowed_use:'',rights_status:'unknown'}]);
  h.node('qwenReference').value='/new.wav';h.node('qwenReference').trigger('change');
  assert.equal(h.node('qwenReferenceText').value,'');assert.match(h.node('qwenReferenceDetails').textContent,/Unknown/);
});
test('a studio clip hands off to a cloning model without inventing approval or dropping source restrictions',()=>{
  const h=harness();h.ctx.VoiceLabWorkflows.openFromClip(new Blob(['voice']),{engine:'breeze-tts-2',text:'Verify this speech.'});
  assert.equal(h.ctx.location.hash,'cloning');assert.equal(h.node('cloneEngine').value,'breeze-tts-2');
  assert.match(h.node('cloneAllowedUse').value,/Non-commercial/);assert.equal(h.node('clonePermission').checked,false);
  assert.match(h.node('cloneStatus').textContent,/Verify/);
});
test('Unicode reference metadata is encoded without corrupting origin or transcript',()=>{
  const h=harness(),metadata={transcript:'你好，世界。',origin:'Maël’s voice'};
  assert.deepEqual(JSON.parse(Buffer.from(h.ctx.VoiceLabWorkflows.metadataHeader(metadata),'base64url').toString()),metadata);
});

test('refreshing the same source preserves a corrected transcript',()=>{
  const h=harness(),row={path:'/managed/ref.wav',name:'ref.wav',audio_url:'/preview',transcript:'Original words.'};
  h.ctx.VoiceLabWorkflows.render([row]);h.node('qwenReference').value=row.path;h.node('qwenReference').trigger('change');
  h.node('qwenReferenceText').value='My verified correction.';
  h.ctx.VoiceLabWorkflows.render([row]);assert.equal(h.node('qwenReferenceText').value,'My verified correction.');
});

test('reference picker groups saved character voices and hides superseded versions',()=>{
  const h=harness(),base={duration_seconds:8,generation_engine_name:'Fish Audio Drama 3'};
  const rows=[{...base,path:'/v1.wav',character_id:'char-mara',identity_id:'mara',character_name:'Mara · Lantern keeper',is_preferred_reference:false},
    {...base,path:'/mara.wav',character_id:'char-mara',identity_id:'mara',character_name:'Mara · Lantern keeper',is_preferred_reference:true},
    {...base,path:'/orin.wav',character_id:'char-orin',character_name:'Orin · Old cartographer'}];
  h.ctx.VoiceLabWorkflows.render(rows);
  assert.deepEqual(Array.from(h.ctx.VoiceLabWorkflows.visibleReferenceRows(''),r=>r.path),['/mara.wav','/orin.wav']);
  assert.ok(h.ctx.VoiceLabWorkflows.visibleReferenceRows('/v1.wav').some(r=>r.path==='/v1.wav'),'explicit old selection preserved');
  assert.equal(h.ctx.VoiceLabWorkflows.label(rows[1]),'Mara · Lantern keeper — Fish Audio Drama 3 · 8.0 s');
  const selector=h.node('qwenReference');
  assert.ok(selector.options.some(group=>group.label==='Saved character voices'&&group.options.length===2));
  const versions=Object.values(h.nodes).find(n=>n.dataset.referenceFilter==='0');versions.checked=true;versions.trigger('change');
  assert.ok(h.ctx.VoiceLabWorkflows.visibleReferenceRows('').some(r=>r.path==='/v1.wav'));
});

test('same-character candidate labels and retained older versions remain distinguishable',()=>{
  const h=harness(),base={character_id:'char',character_name:'Ada',engine_id:'drama-3',engine_name:'Fish Audio Drama 3',duration_seconds:8};
  const rows=[{...base,path:'/a1.wav',identity_id:'voice-a',identity_label:'Warm',version:1,is_preferred_reference:false},
    {...base,path:'/a2.wav',identity_id:'voice-a',identity_label:'Warm',version:2,is_preferred_reference:true},
    {...base,path:'/b.wav',identity_id:'voice-b',identity_label:'Bright',version:1,is_preferred_reference:true}];
  h.ctx.VoiceLabWorkflows.render(rows);
  const labels=rows.map(r=>h.ctx.VoiceLabWorkflows.label(r));
  assert.equal(new Set(labels).size,3);assert.match(labels[0],/Warm.*v1/);assert.match(labels[1],/Warm.*v2/);assert.match(labels[2],/Bright/);
});

test('frozen history retains a restricted parent after the source disappears and user edits use notes',async()=>{
  const h=harness(),row={path:'/managed/breeze.wav',name:'ref.wav',audio_url:'/preview',transcript:'Exact words.',origin:'Breeze design',allowed_use:'Non-commercial only'};
  h.ctx.VoiceLabWorkflows.render([row]);
  const snapshot=h.ctx.VoiceLabWorkflows.freezeRequest({engine:'qwen3-tts-1.7b-base',reference_path:row.path,text:'Cloned words.'});
  h.ctx.VoiceLabWorkflows.render([]);
  h.ctx.VoiceLabWorkflows.openFromClip(new Blob(['clone']),snapshot);
  assert.match(h.node('cloneAllowedUse').value,/Non-commercial only/);
  h.node('cloneName').value='Child voice';h.node('clonePermission').checked=true;
  h.node('cloneAllowedUse').value='Personal test';h.node('cloneOrigin').value='My newer label';
  await h.node('cloneSave').trigger('click');
  const upload=h.requests.find(r=>r.options?.headers?.['X-Reference-Metadata']);
  const metadata=JSON.parse(Buffer.from(upload.options.headers['X-Reference-Metadata'],'base64url').toString());
  assert.match(metadata.allowed_use,/Non-commercial only/);assert.match(metadata.origin,/Breeze design/);
});

test('reference-free designs do not inherit an unrelated picker selection',()=>{
  const h=harness();h.ctx.VoiceLabWorkflows.render([{path:'/unrelated.wav',origin:'Unrelated',allowed_use:'Not for export'}]);
  const body=h.ctx.VoiceLabWorkflows.freezeRequest({engine:'breeze-tts-2',mode:'design',reference_path:'/unrelated.wav',text:'New design.'});
  assert.equal(body._reference_provenance,undefined);
  h.ctx.VoiceLabWorkflows.openFromClip(new Blob(['audio']),body);
  assert.doesNotMatch(h.node('cloneOrigin').value,/Unrelated/);assert.doesNotMatch(h.node('cloneAllowedUse').value,/Not for export/);
  assert.match(h.node('cloneAllowedUse').value,/Non-commercial/);
});

// The Kokoro voice picker is read again from the service by the refresh button and by Generate (when the picker is empty).
function voicePicker(h){
  const voice=h.node('voice'),filled=[];
  Object.defineProperty(voice,'textContent',{get:()=> '',set(value){if(value==='')voice.options.length=0;}});
  Object.defineProperty(voice,'selectedOptions',{get:()=>voice.options.filter(item=>item.selected)});
  h.ctx.selectedValues=select=>select.selectedOptions.map(item=>item.value);
  h.ctx.fillFixedVoices=()=>filled.push(voice.options.map(item=>item.value).join(','));
  return {voice,filled,chosen:()=>voice.selectedOptions.map(item=>item.value)};
}

test('the voice picker is filled from the service, with af_heart chosen when nothing was, and the fixed-voice picker follows',async()=>{
  const h=harness(),{voice,filled,chosen}=voicePicker(h);h.setInfo({voices:['af_alloy','af_heart','bf_emma']});
  assert.equal(await h.ctx.VoiceLabWorkflows.refreshKokoroVoices(),3);
  assert.deepEqual(voice.options.map(item=>item.value),['af_alloy','af_heart','bf_emma']);
  assert.equal(voice.options[0].text,h.ctx.VoiceLabWorkflows.kokoroLabel('af_alloy'));
  assert.deepEqual(chosen(),['af_heart']);assert.deepEqual(filled,['af_alloy,af_heart,bf_emma']);
  assert.equal(h.calls.filter(call=>call.url==='/api/info').length,1);
});

test('reading the voices again keeps the voices that were chosen and only then falls back to af_heart, or to the first',async()=>{
  const h=harness(),{voice,chosen}=voicePicker(h),refresh=()=>h.ctx.VoiceLabWorkflows.refreshKokoroVoices();
  h.setInfo({voices:['af_heart','bf_emma','bm_george']});await refresh();
  voice.options.forEach(item=>{item.selected=item.value==='bf_emma'||item.value==='bm_george';});
  h.setInfo({voices:['af_heart','bf_emma','bm_george','am_adam']});await refresh();
  assert.deepEqual(chosen(),['bf_emma','bm_george'],'a blend that was chosen stays chosen');
  h.setInfo({voices:['af_heart','am_adam']});await refresh();
  assert.deepEqual(chosen(),['af_heart'],'the chosen voices are gone, so the default comes back');
  h.setInfo({voices:['am_adam','bf_emma']});await refresh();
  assert.deepEqual(chosen(),['am_adam'],'without af_heart the first voice is chosen');
});

test('with no voices installed the picker stays empty and the count says so',async()=>{
  const h=harness(),{voice,filled}=voicePicker(h);h.setInfo({voices:[]});
  assert.equal(await h.ctx.VoiceLabWorkflows.refreshKokoroVoices(),0);
  assert.equal(voice.options.length,0);assert.equal(voice.selectedOptions.length,0);assert.deepEqual(filled,['']);
});

test('the refresh button reads the voices again and says so, and a failure shows its error',async()=>{
  const h=harness(),{voice}=voicePicker(h);h.setInfo({voices:['af_heart','bf_emma']});
  await h.node('refreshKokoroVoices').trigger('click');
  assert.equal(voice.options.length,2);assert.equal(h.node('status').textContent,'Installed Kokoro speaker packs refreshed.');
  h.ctx.fetchJson=async()=>{throw new Error('service stopped');};
  await h.node('refreshKokoroVoices').trigger('click');
  assert.equal(h.node('err').textContent,'service stopped');
});

test('the cloning picker starts on a model this computer has installed, and keeps a model the person chose',()=>{
  const h=harness(),picker=h.node('cloneEngine');
  assert.equal(picker.value,'drama-3','before the Models page has been read, the first model');
  h.ctx.engineRows={turbo:{state:'not_checked',installation:{installed:true}},'drama-3':{state:'not_installed',installation:{installed:false}}};
  h.ctx.VoiceLabWorkflows.refreshCloneEngine();
  assert.equal(picker.value,'turbo');
  picker.value='orig';picker.trigger('change');
  h.ctx.VoiceLabWorkflows.render([]);h.ctx.VoiceLabWorkflows.refreshCloneEngine();
  assert.equal(picker.value,'orig','a model the person chose stays chosen when the list is filled again');
});

test('the cloning picker says when the chosen model is not installed',()=>{
  const h=harness(),help=h.node('cloneModelHelp');
  h.ctx.engineRows={'drama-3':{state:'not_installed',installation:{installed:false}}};
  h.ctx.VoiceLabWorkflows.refreshCloneEngine();
  assert.match(help.textContent,/^Not installed on this computer yet; Models & features says what it needs\. Fish Audio:/);
  h.ctx.engineRows={'drama-3':{state:'not_checked',installation:{installed:true}}};
  h.ctx.VoiceLabWorkflows.refreshCloneEngine();
  assert.match(help.textContent,/^Fish Audio:/);
});

test('a studio clip from a model that cannot clone goes to an installed cloning model',()=>{
  const h=harness();h.ctx.engineRows={voxcpm2:{state:'usable',installation:{installed:true}}};
  h.ctx.VoiceLabWorkflows.openFromClip(new Blob(['voice']),{engine:'kokoro',text:'Verify this speech.'});
  assert.equal(h.node('cloneEngine').value,'voxcpm2');
});

test('with no cloning model installed the picker starts on one the Lab can install, and says to press Install',()=>{
  const h=harness(),picker=h.node('cloneEngine');
  h.ctx.engineRows={'drama-3':{state:'not_installed',installation:{installed:false}},
    turbo:{state:'not_installed',installation:{installed:false,installer:'setup-chatterbox.bat'}}};
  h.ctx.VoiceLabWorkflows.refreshCloneEngine();
  assert.equal(picker.value,'turbo');
  assert.match(h.node('cloneModelHelp').textContent,/^Not installed on this computer yet; press Install on its card in Models & features\. Chatterbox Turbo:/);
});
