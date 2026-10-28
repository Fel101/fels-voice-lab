const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {pageSource}=require('./page_source.cjs');
const source=fs.readFileSync(path.join(__dirname,'..','voice_library.js'),'utf8');
const page=pageSource();
function harness(){
  const fields={},calls=[],ready=[];
  function create(tag){
    const item={tag,children:[],value:'',checked:false,hidden:false,handlers:{},attributes:{},classList:{items:[],add(name){this.items.push(name);}},
      appendChild(child){this.children.push(child);},append(...items){this.children.push(...items);},
      addEventListener(type,fn){this.handlers[type]=fn;},setAttribute(key,value){this.attributes[key]=value;},
      querySelectorAll(tag){return this.children.flatMap(child=>[...(child.tag===tag?[child]:[]),...(child.querySelectorAll?child.querySelectorAll(tag):[])]);},
      dispatchEvent(event){return this.handlers[event.type]?.call(this,event);},pause(){this.paused=true;},scrollIntoView(){this.scrolled=true;}};
    let text='';Object.defineProperty(item,'textContent',{get(){return text+this.children.map(c=>c.textContent||'').join('');},set(value){text=value;this.children=[];}});
    Object.defineProperty(item,'options',{get(){return this.children.filter(c=>c.tag==='option');}});
    return item;
  }
  function el(id){return fields[id]||(fields[id]=create(id));}
  const models=['drama-3','qwen3-tts-1.7b-base','voxcpm2','kokoro','orig'];
  const names=['Mara · Lantern keeper','Orin · Old cartographer','Veda · Sky courier','Tomas · Stone warden','Sable · Moon scholar',
    'Nell · Spark mechanic','Cedric · River archivist','Iris · Weather reader','Pippa · Clockwork guide','Bran · Harbor watch'];
  const voices=names.map((name,i)=>({id:'id'+i,identity_id:'id'+i,character_id:'char'+i,character_name:name,identity_label:'Audition candidate',
    identity_status:'candidate',identity_kind:i===8?'fixed_voice':'reference',
    engine_id:i<5?'drama-3':i<7?'qwen3-tts-1.7b-base':i===7?'voxcpm2':i===8?'kokoro':'orig',
    generation_engine_id:i<5?'drama-3':i<7?'qwen3-tts-1.7b-voice-design':i===7?'voxcpm2':i===8?'kokoro':'orig',
    preferred_reference_id:i===8?null:'ref'+i,preferred_reference_available:i!==8,reference_count:i===8?0:1,
    engine_options:i===8?{voice:'bf_alice,bf_emma'}:{},audition:{audio_url:'/take'+i,language:i===8?'British English':'English',settings:i===8?{speed:1.05}:{}}}));
  const data={voices};
  const ctx=vm.createContext({document:{getElementById:el,createElement:create,addEventListener(type,fn){ready.push(fn);}},location:{hash:''},
    Event:class{constructor(type){this.type=type;}},option(select,value,text){const option=create('option');option.value=value;option.textContent=text;select.appendChild(option);},
    async fetchJson(url){calls.push(url);return data;},async loadReferences(){calls.push('references');},
    async refreshCast(character,identity){calls.push(['cast',character,identity]);},renderPerformanceCharacter(){calls.push('performCharacter');},
    renderPerformanceIdentity(){calls.push('performIdentity');},async loadCurrentTakes(){calls.push('takes');},refresh(){calls.push('refresh');},updateValues(){calls.push('values');},
    referenceRows:voices.filter(v=>v.identity_kind==='reference').map(v=>({path:'/ref/'+v.id,reference_version_id:v.preferred_reference_id}))});
  ctx.window=ctx;vm.runInContext(source,ctx);
  vm.runInContext(page.slice(page.indexOf('function retirementLabel('),page.indexOf('function renderTakes(')),ctx);ready.forEach(fn=>fn());
  models.forEach(id=>ctx.option(el('engine'),id,id));['bf_alice','bf_emma','af_heart'].forEach(id=>ctx.option(el('voice'),id,id));
  ['British English','American English'].forEach(id=>ctx.option(el('kokoroLanguage'),id,id));
  ['English','British English'].forEach(id=>ctx.option(el('performLanguage'),id,id));
  ['English','Chinese'].forEach(id=>ctx.option(el('qwenLanguage'),id,id));
  voices.filter(v=>v.preferred_reference_id).forEach(v=>ctx.option(el('performReference'),v.preferred_reference_id,v.preferred_reference_id));
  return {ctx,el,calls,voices,data};
}
test('a retired saved voice shows its reason, restore and purge, and cannot start new use',async()=>{
  const h=harness();h.data.voices[0]={...h.data.voices[0],retired:true,retirement:{reason:'Owner withdrew permission.',inherited:false,source_label:'its voice'}};
  h.el('showRetiredVoices').checked=true;await h.ctx.VoiceLabLibrary.refresh();
  assert.deepEqual(h.calls.filter(call=>typeof call==='string'&&call.startsWith('/api/voice-library')),['/api/voice-library?include_retired=1']);
  const card=h.el('voiceGallery').children[0];
  const button=text=>card.querySelectorAll('button').find(item=>item.textContent===text);
  assert.equal(button('Use in Model studio').disabled,true);
  assert.equal(button('Perform lines').disabled,true);
  assert.equal(button('Edit voice').disabled,false);
  assert.ok(button('Restore voice'));assert.ok(button('Purge voice audio…'));assert.equal(button('Retire voice'),undefined);
  assert.match(card.textContent,/Owner withdrew permission\. New use is blocked/);
  assert.ok(card.classList.items.includes('retired'));
});

test('the library shows all ten named cards and previews, including the fixed blend',async()=>{
  const h=harness();await h.ctx.VoiceLabLibrary.refresh();
  assert.equal(h.el('voiceGallery').children.length,10);
  assert.equal(h.el('voiceGallery').querySelectorAll('audio').length,10);
  assert.deepEqual(h.el('voiceGallery').querySelectorAll('h3').map(n=>n.textContent),['Mara','Orin','Veda','Tomas','Sable','Nell','Cedric','Iris','Pippa','Bran']);
  const text=h.el('voiceGallery').textContent;assert.match(text,/Created with Qwen VoiceDesign 1.7B/);assert.match(text,/Generate dialogue with Qwen Base 1.7B/);
  assert.match(text,/Saved Kokoro blend: bf_alice,bf_emma/);assert.match(h.el('voiceLibraryStatus').textContent,/^10 voices\nListen below/);
});
test('search matches model and role, and a refresh keeps every voice',async()=>{
  const h=harness();await h.ctx.VoiceLabLibrary.refresh();h.el('voiceSearch').value='Fish';h.ctx.VoiceLabLibrary.render();
  assert.equal(h.el('voiceGallery').children.length,5);h.el('voiceSearch').value='clockwork';h.ctx.VoiceLabLibrary.render();assert.equal(h.el('voiceGallery').children.length,1);
  h.el('voiceSearch').value='';h.ctx.VoiceLabLibrary.render();assert.equal(h.el('voiceGallery').children.length,10);
  await h.ctx.VoiceLabLibrary.refresh();assert.equal(h.el('voiceGallery').children.length,10);
});
test('Pippa uses her saved blend and British language without inventing a reference or generating',async()=>{
  const h=harness();await h.ctx.VoiceLabLibrary.openVoice(h.voices[8],'studio');
  assert.equal(h.el('engine').value,'kokoro');assert.deepEqual(h.el('voice').options.filter(o=>o.selected).map(o=>o.value),['bf_alice','bf_emma']);
  assert.equal(h.el('kokoroLanguage').value,'British English');assert.equal(h.el('speed').value,1.05);assert.equal(h.ctx.location.hash,'studio');
  assert.ok(!h.calls.includes('references'));assert.ok(!h.calls.some(c=>typeof c==='string'&&c.includes('generate')));
  h.el('engine').dispatchEvent(new h.ctx.Event('change'));assert.equal(h.el('studioVoiceSelection').textContent,'');
});
test('each reference voice selects its saved reuse model and real reference; missing reference leaves engine unchanged',async()=>{
  const h=harness();for(const voice of h.voices.filter(v=>v.identity_kind==='reference')){
    h.el('qwenLanguage').value='Chinese';await h.ctx.VoiceLabLibrary.openVoice(voice,'studio');assert.equal(h.el('engine').value,voice.engine_id);
    if(voice.engine_id.startsWith('qwen'))assert.equal(h.el('qwenLanguage').value,'English');
    const selector=voice.engine_id==='drama-3'?'dramaReference':voice.engine_id.startsWith('qwen')?'qwenReference':voice.engine_id==='voxcpm2'?'voxReference':'chatterboxReference';
    assert.equal(h.el(selector).value,'/ref/'+voice.id);
  }
  assert.equal(h.el('clone').checked,true);h.ctx.referenceRows=[];h.el('engine').value='kokoro';
  await assert.rejects(h.ctx.VoiceLabLibrary.openVoice(h.voices[0],'studio'),/unavailable/);assert.equal(h.el('engine').value,'kokoro');
});
test('Perform lines selects actual character, identity and reference; edit opens correct existing voice',async()=>{
  const h=harness();await h.ctx.VoiceLabLibrary.openVoice(h.voices[2],'perform');
  assert.equal(h.el('performCharacter').value,'char2');assert.equal(h.el('performIdentity').value,'id2');assert.equal(h.el('performReference').value,'ref2');assert.equal(h.ctx.location.hash,'perform');
  assert.ok(h.calls.includes('takes'),'old visible take cards are replaced for this identity');
  await h.ctx.VoiceLabLibrary.openVoice(h.voices[8],'perform');assert.equal(h.el('performLanguage').value,'British English');
  await h.ctx.VoiceLabLibrary.openVoice(h.voices[7],'edit');assert.equal(h.el('castEditor').open,true);assert.equal(h.el('castEditor').scrolled,true);
  assert.ok(h.calls.some(c=>Array.isArray(c)&&c[0]==='cast'&&c[1]==='char7'&&c[2]==='id7'));
});

test('a pending card selection disables all navigation actions until it completes',async()=>{
  const h=harness();await h.ctx.VoiceLabLibrary.refresh();let release;
  h.ctx.loadReferences=()=>new Promise(resolve=>{release=resolve;});
  const buttons=h.el('voiceGallery').querySelectorAll('button');
  const first=buttons[0].dispatchEvent(new h.ctx.Event('click'));
  assert.ok(buttons.every(button=>button.disabled));
  await buttons[8*3].dispatchEvent(new h.ctx.Event('click'));
  release();await first;assert.equal(h.el('engine').value,'drama-3');
  assert.ok(h.el('voiceGallery').querySelectorAll('button').every(button=>!button.disabled));
});

test('a slower take response cannot replace a different voice or the latest request',async()=>{
  const html=pageSource();
  const code=html.slice(html.indexOf('var takeLoadRevision=0;'),html.indexOf('function performanceSettings('));
  let current={id:'mara'},line='line-01';const pending=[],rendered=[],list={};
  const ctx=vm.createContext({performanceIdentity:()=>current,$:id=>id==='lineId'?{value:line}:list,URLSearchParams,
    fetchJson:()=>new Promise(resolve=>pending.push(resolve)),renderTakes:rows=>rendered.push(rows)});
  vm.runInContext(code,ctx);const first=ctx.loadCurrentTakes();current={id:'orin'};const second=ctx.loadCurrentTakes();
  pending[1]({takes:['Orin']});await second;pending[0]({takes:['Mara']});await first;
  assert.equal(rendered.length,1);assert.equal(rendered[0][0],'Orin');
  const third=ctx.loadCurrentTakes();line='new-line';pending[2]({takes:['old line']});await third;assert.equal(rendered.length,1);
});
