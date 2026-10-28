const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {pageSource}=require('./page_source.cjs');
const html=pageSource();
function between(start,end){const at=html.indexOf(start);return html.slice(at,html.indexOf(end,at));}
const script=fs.readFileSync(path.join(__dirname,'..','voice_lab_take_edit.js'),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}

// Just enough of a page for the edit panel: elements that nest, listen and fire, and record what is drawn and played.
class El{
  constructor(tag,world){this.tag=tag;this.world=world;this.children=[];this.listeners={};this.attrs={};this.className='';this.textContent='';
    this.hidden=false;this.disabled=false;this.value='';this.checked=false;this.parentNode=null;this.src='';this.type='';}
  get isConnected(){let node=this;while(node.parentNode)node=node.parentNode;return node===this.world.page;}
  appendChild(child){if(child.parentNode)child.parentNode.removeChild(child);child.parentNode=this;this.children.push(child);return child;}
  removeChild(child){const at=this.children.indexOf(child);if(at>=0)this.children.splice(at,1);child.parentNode=null;return child;}
  addEventListener(type,fn){(this.listeners[type]=this.listeners[type]||[]).push(fn);}
  fire(type,event){for(const fn of this.listeners[type]||[])fn(event||{});}
  setAttribute(name,value){this.attrs[name]=String(value);}
  removeAttribute(name){delete this.attrs[name];if(name==='src')this.src='';}
  querySelectorAll(tag){const found=[];const walk=node=>{for(const child of node.children){if(child.tag===tag)found.push(child);walk(child);}};walk(this);return found;}
  pause(){this.world.paused++;}
  play(){this.world.played++;return this.world.playRejects?Promise.reject(new Error('The browser would not start playback.')):Promise.resolve();}
  load(){}
  getContext(){return this.world.ctx;}
  getBoundingClientRect(){return {left:this.world.rectLeft||0,width:900};}
  setPointerCapture(){}
}
function descendants(root){const all=[];const walk=node=>{for(const child of node.children){all.push(child);walk(child);}};walk(root);return all;}
function world(){
  const w={paused:0,played:0,urls:[],revoked:[],calls:[],drawn:[],path:[],uuid:0};
  w.ctx={fillStyle:'',strokeStyle:'',clearRect(){},beginPath(){},moveTo(x,y){w.path.push(['m',x,y]);},lineTo(x,y){w.path.push(['l',x,y]);},stroke(){},
    fillRect(...args){w.drawn.push({style:this.fillStyle,rect:args});}};
  w.page=new El('div',w);
  w.document={createElement:tag=>new El(tag,w),createTextNode:text=>{const node=new El('#text',w);node.textContent=text;return node;},body:{}};
  w.fetch=(url,options)=>{const call=Object.assign({url,options},deferred());w.calls.push(call);return call.promise;};
  w.URL={createObjectURL:blob=>{const url='blob:'+(w.urls.length+1);w.urls.push(url);return url;},revokeObjectURL:url=>w.revoked.push(url)};
  const context=vm.createContext({document:w.document,fetch:w.fetch,URL:w.URL,getComputedStyle:()=>({getPropertyValue:name=>name}),
    window:{crypto:{randomUUID:()=>'uuid-'+(++w.uuid)}}});
  vm.runInContext(between('function fetchJson(','function loadReferences('),context);
  vm.runInContext(script,context);
  w.context=context;
  return w;
}
function reply(call,{ok=true,status=200,json,blob,headers={}}={}){
  call.resolve({ok,status,json:async()=>json,blob:async()=>blob||{size:1},headers:{get:name=>name in headers?headers[name]:null}});
}
const WAVE={duration_seconds:2,sample_rate:24000,points:4,peaks:[[-0.1,0.1],[-0.3,0.3],[-0.3,0.3],[-0.1,0.1]],peak_dbfs:-10.46,rms_dbfs:-14,
  speech:{threshold_db:-50,start_seconds:0.4,end_seconds:1.6}};
const TAKE={id:'0123456789abcdef0123456789abcdef',duration_seconds:2,parent_take_id:null,edit:null};

// A panel opened on a take: the world, the shown messages, the refresh count, and the controls found by their text or field.
async function openPanel(overrides={}){
  const w=world(),shown=[],refreshed=[];
  const article=w.page.appendChild(new El('article',w)),actions=article.appendChild(new El('div',w));
  const after=overrides.after||(()=>{refreshed.push(1);return Promise.resolve();});
  w.context.appendTakeEditor(article,TAKE,actions,after,message=>shown.push(message));
  const open=actions.children[0];
  open.fire('click');
  const panel=()=>descendants(article).find(node=>node.className==='take-edit');
  const speech=()=>descendants(panel()).find(node=>node.className==='meta take-edit-speech').textContent;
  const button=text=>descendants(panel()).find(node=>node.tag==='button'&&node.textContent===text);
  const field=name=>descendants(panel()).find(node=>node.attrs['data-edit-field']===name);
  const text=role=>descendants(panel()).find(node=>node.attrs.role===role);
  const loaded=overrides.wave===null?null:overrides.wave||WAVE;
  if(loaded)reply(w.calls[0],{json:loaded});
  await tick();
  return {w,article,actions,open,panel,button,field,text,speech,shown,refreshed};
}

test('the cast script shows an Edit take button and a note on every take, and the page loads the edit script',()=>{
  assert.match(html,/function appendTakeEditor\(/);
  const w=world(),articles=[];
  const container=w.page.appendChild(new El('div',w));
  const base={character_name:'Nell',identity_label:'Warm',line_id:'line-01',status:'candidate',duration_seconds:2,spoken_text:'Hold',
    model:{backend:'kokoro',checkpoint:'c',checkpoint_revision:'r',mode:'preset'},audio_url:'/a.wav',retired:false};
  const edited={...base,id:'b'.repeat(32),parent_take_id:'a'.repeat(32),duration_seconds:1.5,
    edit:{request:{auto_trim:false,fade_in_ms:20,fade_out_ms:0,gain_db:3},result:{start_seconds:0.3,end_seconds:1.8},parent_duration_seconds:2}};
  const retired={...base,id:'c'.repeat(32),retired:true};
  Object.assign(w.context,{$:()=>container,appendTakeChecks(){},appendTakeLoudness(){},appendTakeRetirement(){},loadCurrentTakes(){}});
  vm.runInContext(between('function renderTakes(takes){','var takeLoadRevision=0;'),w.context);
  w.context.renderTakes([{...base,id:'a'.repeat(32)},edited,retired]);
  const shown=container.children;   // newest first, as the list shows them: the retired take, the edit, the original
  assert.equal(shown.length,3);
  const labelled=(article,text)=>descendants(article).filter(node=>node.tag==='button'&&node.textContent===text);
  for(const article of shown)assert.equal(labelled(article,'Edit take…').length,1);
  assert.equal(labelled(shown[0],'Edit take…')[0].disabled,true,'a retired take cannot be edited');
  assert.equal(labelled(shown[1],'Edit take…')[0].disabled,false);
  const detail=article=>descendants(article).find(node=>node.className==='meta').textContent;
  assert.doesNotMatch(detail(shown[2]),/Edited from/);
  assert.equal(detail(shown[2]),'Text sent: Hold\nkokoro / c / revision r / preset','an original gains no line');
  assert.match(detail(shown[1]),/\nEdited from take bbbbbbbb|\nEdited from take aaaaaaaa/);
  assert.match(detail(shown[1]),/kept 0\.30 to 1\.80 s of the original 2\.00 s, fade in 20 ms, gain \+3 dB\./);
});

test('a blank field is left out, silence counts only while ticked, and a bad number names the field',()=>{
  const c=world().context,request=values=>plain(c.takeEditRequest(values));
  assert.deepEqual(request({auto_trim:false}),{});
  assert.deepEqual(request({auto_trim:false,start_seconds:'   ',gain_db:'1'}),{gain_db:1},'spaces are a blank field');
  assert.deepEqual(request({auto_trim:false,start_seconds:' 0.25 ',end_seconds:'',gain_db:'-3',fade_in_ms:'20',fade_out_ms:'0'}),
    {start_seconds:0.25,fade_in_ms:20,fade_out_ms:0,gain_db:-3});
  assert.deepEqual(request({auto_trim:false,silence_threshold_db:'-40',keep_silence_ms:'10',gain_db:'1'}),{gain_db:1});
  assert.deepEqual(request({auto_trim:true,silence_threshold_db:'-40',keep_silence_ms:'',gain_db:'1'}),
    {silence_threshold_db:-40,gain_db:1,auto_trim:true});
  for(const [values,message] of [[{start_seconds:'abc'},'The start must be a number.'],[{end_seconds:'1,5'},'The end must be a number.'],
    [{gain_db:'Infinity'},'The gain must be a number.'],[{fade_in_ms:'x'},'The fade in must be a number.'],
    [{fade_out_ms:'x'},'The fade out must be a number.'],[{auto_trim:true,silence_threshold_db:'x'},'The silence level must be a number.'],
    [{auto_trim:true,keep_silence_ms:'x'},'The silence to keep must be a number.']])
    assert.throws(()=>c.takeEditRequest(values),error=>error.message===message,message);
});

test('a pointer position maps to seconds inside the take, to the millisecond',()=>{
  const c=world().context;
  assert.equal(c.takeEditSecondsAt(450,900,2),1);
  assert.equal(c.takeEditSecondsAt(-20,900,2),0);
  assert.equal(c.takeEditSecondsAt(2000,900,2),2);
  assert.equal(c.takeEditSecondsAt(1,900,2),0.002);
  assert.equal(c.takeEditSecondsAt(100,0,2),0);
  assert.equal(c.takeEditSecondsAt(100,900,0),0);
});

test('a save request key is made once for each edit',()=>{
  const w=world();
  assert.equal(w.context.takeEditKey(),'take-edit-uuid-1');
  assert.equal(w.context.takeEditKey(),'take-edit-uuid-2');
  w.context.window.crypto=undefined;
  assert.match(w.context.takeEditKey(),/^take-edit-\d+-\d+$/);
});

test('the note says only what the edit did',()=>{
  const c=world().context,note=take=>c.takeEditNote(plain(take));
  assert.equal(note({edit:null}),'');
  const edit=(request,result,parent=2)=>({parent_take_id:'abcdef0123456789',edit:{request,result:{start_seconds:0,end_seconds:parent,...result},parent_duration_seconds:parent}});
  assert.equal(note(edit({gain_db:-3.5},{})),'Edited from take abcdef01: gain -3.5 dB.');
  assert.equal(note(edit({auto_trim:true,silence_threshold_db:-45,keep_silence_ms:80},{start_seconds:0.39,end_seconds:1.61})),
    'Edited from take abcdef01: kept 0.39 to 1.61 s of the original 2.00 s, silence quieter than -45 dB cut from both ends, 80 ms left.');
  assert.equal(note(edit({fade_in_ms:10,fade_out_ms:40},{})),'Edited from take abcdef01: fade in 10 ms, fade out 40 ms.');
  assert.equal(note(edit({end_seconds:1},{end_seconds:1})),'Edited from take abcdef01: kept 0.00 to 1.00 s of the original 2.00 s.');
  assert.equal(note(edit({end_seconds:1,fade_in_ms:0,fade_out_ms:0,gain_db:0},{end_seconds:1})),
    'Edited from take abcdef01: kept 0.00 to 1.00 s of the original 2.00 s.','a fade or gain of zero is not said');
  assert.equal(note(edit({},{})),'Edited from take abcdef01.');
});

test('the waveform marks the kept part and the speech',()=>{
  const w=world();
  w.context.drawTakeWave({width:900,height:120,getContext:()=>w.ctx},plain(WAVE),{start:0.5,end:1.5});
  const rects=()=>w.drawn.map(item=>item.rect.map(number=>Math.round(number*1e6)/1e6));
  assert.deepEqual(rects(),[[0,0,900,120],[225,0,450,120],[180,116,540,3]]);
  w.drawn.length=0;
  w.context.drawTakeWave({width:900,height:120,getContext:()=>w.ctx},null,null);
  assert.deepEqual(rects(),[[0,0,900,120]]);
});

test('opening the panel loads the waveform, then Hear and Save can be used; Close removes it',async()=>{
  const p=await openPanel({wave:null});
  assert.equal(p.w.calls[0].url,'/api/takes/'+TAKE.id+'/waveform?points=600');
  assert.equal(p.open.attrs['aria-expanded'],'true');
  assert.equal(p.button('Hear the edit').disabled,true);
  assert.equal(p.button('Save as new take').disabled,true);
  reply(p.w.calls[0],{json:WAVE});
  await tick();
  assert.equal(p.button('Hear the edit').disabled,false);
  assert.equal(p.button('Save as new take').disabled,false);
  assert.match(p.speech(),/^Speech from 0\.40 to 1\.60 s \(louder than -50 dB\)\.$/);
  assert.deepEqual(p.w.drawn.map(item=>item.rect.map(number=>Math.round(number*1e6)/1e6)),[[0,0,900,120],[0,0,900,120],[180,116,540,3]],
    'the waveform is drawn when it arrives');
  p.button('Close').fire('click');
  assert.equal(p.panel(),undefined);
  assert.equal(p.open.attrs['aria-expanded'],'false');
  p.open.fire('click');
  assert.ok(p.panel(),'it opens again');
});

test('a waveform that cannot be read is shown as the reason, and nothing can be sent',async()=>{
  const p=await openPanel({wave:null});
  reply(p.w.calls[0],{ok:false,status:400,json:{error:'The stored audio no longer matches this take’s checksum, so it cannot be edited.'}});
  await tick();
  assert.match(p.text('alert').textContent,/checksum/);
  assert.equal(p.speech(),'','the loading message goes');
  assert.equal(p.button('Hear the edit').disabled,true);
  assert.equal(p.button('Save as new take').disabled,true);
});

test('a take with no speech says so',async()=>{
  const p=await openPanel({wave:{...WAVE,speech:null}});
  assert.match(p.speech(),/No speech found/);
});

test('Hear sends the fields, plays the answer and says how long it is',async()=>{
  const p=await openPanel();
  p.field('start_seconds').value='0.3';p.field('end_seconds').value='1.8';p.field('gain_db').value='3';
  p.button('Hear the edit').fire('click');
  const call=p.w.calls[1];
  assert.equal(call.url,'/api/takes/'+TAKE.id+'/edit-preview');
  assert.equal(call.options.method,'POST');
  assert.deepEqual(plain(JSON.parse(call.options.body)),{start_seconds:0.3,end_seconds:1.8,gain_db:3});
  reply(call,{headers:{'X-Audio-Seconds':'1.50','X-Peak-Dbfs':'-7.46'}});
  await tick();
  const player=descendants(p.panel()).find(node=>node.tag==='audio');
  assert.equal(player.src,'blob:1');assert.equal(player.hidden,false);assert.equal(p.w.played,1);
  assert.equal(p.text('status').textContent,'The edited clip is 1.50 s, peaking at -7.46 dBFS.');
});

test('an answer for silence is called silent',async()=>{
  const p=await openPanel();
  p.field('end_seconds').value='0.3';
  p.button('Hear the edit').fire('click');
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'0.30','X-Peak-Dbfs':''}});
  await tick();
  assert.equal(p.text('status').textContent,'The edited clip is 0.30 s and silent.');
});

test('a refused preview shows the service’s reason and keeps the earlier clip out of the player',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='24';
  p.button('Hear the edit').fire('click');
  reply(p.w.calls[1],{ok:false,status:400,json:{error:'A gain of 24 dB would clip the loudest sample.'}});
  await tick();
  assert.equal(p.text('alert').textContent,'A gain of 24 dB would clip the loudest sample.');
  assert.equal(p.text('status').textContent,'');
  assert.equal(p.w.urls.length,0);
});

test('text that is not a number is refused before anything is sent',async()=>{
  const p=await openPanel();
  p.field('start_seconds').value='abc';
  p.button('Hear the edit').fire('click');
  assert.equal(p.text('alert').textContent,'The start must be a number.');
  p.button('Save as new take').fire('click');
  assert.equal(p.text('alert').textContent,'The start must be a number.');
  assert.equal(p.w.calls.length,1,'only the waveform was requested');
});

test('the silence fields are sent only while the silence box is ticked',async()=>{
  const p=await openPanel();
  const box=p.field('auto_trim');
  assert.equal(p.field('silence_threshold_db').disabled,true);
  assert.equal(p.field('keep_silence_ms').disabled,true);
  p.field('silence_threshold_db').value='-40';p.field('keep_silence_ms').value='20';
  p.button('Hear the edit').fire('click');
  assert.equal(p.w.calls[1].options.body,'{}');   // nothing to send is still a request: the service says it changes nothing
  box.checked=true;box.fire('change');
  assert.equal(p.field('silence_threshold_db').disabled,false);
  assert.equal(p.field('keep_silence_ms').disabled,false);
  p.button('Hear the edit').fire('click');
  assert.deepEqual(plain(JSON.parse(p.w.calls[2].options.body)),{silence_threshold_db:-40,keep_silence_ms:20,auto_trim:true});
});

test('a preview that arrives after the fields changed is dropped',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';
  p.button('Hear the edit').fire('click');
  p.field('gain_db').value='6';p.field('gain_db').fire('input');
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.equal(p.w.urls.length,0);
  assert.equal(p.text('status').textContent,'');
  const player=descendants(p.panel()).find(node=>node.tag==='audio');
  assert.equal(player.hidden,true);
});

test('only the latest preview may fill the player, and the clip it replaces is released',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  p.field('gain_db').value='4';p.button('Hear the edit').fire('click');
  reply(p.w.calls[2],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-6'}});
  await tick();
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.deepEqual(p.w.urls,['blob:1']);
  assert.equal(p.text('status').textContent,'The edited clip is 2.00 s, peaking at -6 dBFS.');
  p.field('gain_db').value='5';p.field('gain_db').fire('input');
  assert.deepEqual(p.w.revoked,['blob:1'],'a change releases the clip it made out of date');
});

test('a preview that arrives after the panel was closed is dropped',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  p.button('Close').fire('click');
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.equal(p.w.urls.length,0);
});

test('a waveform that arrives after the panel was closed is dropped',async()=>{
  const p=await openPanel({wave:null});
  p.button('Close').fire('click');
  reply(p.w.calls[0],{json:WAVE});
  await tick();
  assert.equal(p.panel(),undefined);
  assert.equal(p.w.drawn.length,0,'nothing is drawn for a panel that is gone');
});

test('Save sends the key, then a repeat of the same edit sends the same key, and a change starts a new one',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';
  p.button('Save as new take').fire('click');
  const first=plain(JSON.parse(p.w.calls[1].options.body));
  assert.equal(p.w.calls[1].url,'/api/takes/'+TAKE.id+'/edit');
  assert.deepEqual(first,{gain_db:3,idempotency_key:first.idempotency_key});
  assert.match(first.idempotency_key,/^take-edit-uuid-\d+$/);
  assert.equal(p.button('Save as new take').disabled,true,'one save at a time');
  assert.equal(p.button('Hear the edit').disabled,true);
  reply(p.w.calls[1],{ok:false,status:500,json:{error:'The library is busy.'}});
  await tick();
  assert.equal(p.text('alert').textContent,'The library is busy.');
  assert.equal(p.button('Save as new take').disabled,false);
  p.button('Save as new take').fire('click');
  assert.equal(JSON.parse(p.w.calls[2].options.body).idempotency_key,first.idempotency_key,'a retry of the same edit repeats the key');
  reply(p.w.calls[2],{ok:false,status:500,json:{error:'The library is busy.'}});
  await tick();
  p.field('gain_db').value='4';p.field('gain_db').fire('input');
  p.button('Save as new take').fire('click');
  assert.notEqual(JSON.parse(p.w.calls[3].options.body).idempotency_key,first.idempotency_key,'another edit is another request');
});

test('a saved edit is announced, the list is refreshed, and the next save is a new request',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';
  p.button('Save as new take').fire('click');
  const key=JSON.parse(p.w.calls[1].options.body).idempotency_key;
  reply(p.w.calls[1],{status:201,json:{take:{duration_seconds:1.5},replayed:false}});
  await tick();
  assert.deepEqual(p.shown,['Saved as a new take, 1.50 s. The original is unchanged.']);
  assert.equal(p.refreshed.length,1);
  p.button('Save as new take').fire('click');
  assert.notEqual(JSON.parse(p.w.calls[2].options.body).idempotency_key,key);
});

test('a replayed save says it was already saved',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';p.button('Save as new take').fire('click');
  reply(p.w.calls[1],{json:{take:{duration_seconds:1.5},replayed:true}});
  await tick();
  assert.deepEqual(p.shown,['That edit was already saved as a new take.']);
});

test('a refresh that fails after a good save is reported as the refresh, not the save',async()=>{
  const p=await openPanel({after:()=>Promise.reject(new Error('Could not list takes.'))});
  p.field('gain_db').value='3';p.button('Save as new take').fire('click');
  reply(p.w.calls[1],{status:201,json:{take:{duration_seconds:1.5},replayed:false}});
  await tick();
  assert.deepEqual(p.shown,['Saved as a new take, 1.50 s. The original is unchanged.','Could not list takes.']);
  assert.equal(p.text('alert').textContent,'');
});

test('dragging across the waveform chooses the part to keep; a click changes nothing',async()=>{
  const p=await openPanel();
  const canvas=descendants(p.panel()).find(node=>node.tag==='canvas');
  p.field('start_seconds').value='0.1';p.field('end_seconds').value='1.9';
  canvas.fire('pointerdown',{button:0,clientX:225,pointerId:1});     // 0.5 s of 2.0 s
  canvas.fire('pointermove',{clientX:226});                          // under a hundredth of a second: still a click
  canvas.fire('pointerup',{});
  assert.deepEqual([p.field('start_seconds').value,p.field('end_seconds').value],['0.1','1.9']);
  canvas.fire('pointerdown',{button:0,clientX:675,pointerId:1});     // 1.5 s
  canvas.fire('pointermove',{clientX:225});                          // dragged back to 0.5 s
  canvas.fire('pointermove',{clientX:900});                          // and on to the end
  assert.deepEqual([p.field('start_seconds').value,p.field('end_seconds').value],['1.5','2']);
  canvas.fire('pointermove',{clientX:225});
  canvas.fire('pointerup',{});
  assert.deepEqual([p.field('start_seconds').value,p.field('end_seconds').value],['0.5','1.5']);
  assert.ok(p.w.drawn.some(item=>item.rect[0]===225&&item.rect[2]===450),'the kept part is shaded');
  canvas.fire('pointerdown',{button:0,clientX:90,pointerId:1});
  canvas.fire('pointermove',{clientX:180});
  canvas.fire('pointercancel',{});
  assert.deepEqual([p.field('start_seconds').value,p.field('end_seconds').value],['0.5','1.5'],'a cancelled drag puts the fields back');
  canvas.fire('pointerdown',{button:2,clientX:90,pointerId:1});
  canvas.fire('pointermove',{clientX:180});
  assert.deepEqual([p.field('start_seconds').value,p.field('end_seconds').value],['0.5','1.5'],'only the main button drags');
});

test('a drag that ends makes an earlier preview out of date',async()=>{
  const p=await openPanel();
  const canvas=descendants(p.panel()).find(node=>node.tag==='canvas');
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  canvas.fire('pointerdown',{button:0,clientX:100,pointerId:1});
  canvas.fire('pointermove',{clientX:400});
  canvas.fire('pointerup',{});
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.equal(p.w.urls.length,0);
});

test('closing releases the clip being played',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  p.button('Close').fire('click');
  assert.deepEqual(p.w.revoked,['blob:1']);
});

test('the fields sit in three rows: the range, the silence, then the fades and gain',async()=>{
  const p=await openPanel();
  const rows=descendants(p.panel()).filter(node=>node.className==='row');
  assert.equal(rows.length,3);
  const named=row=>descendants(row).filter(node=>node.attrs['data-edit-field']).map(node=>node.attrs['data-edit-field']);
  assert.deepEqual(named(rows[0]),['start_seconds','end_seconds']);
  assert.deepEqual(named(rows[1]),['auto_trim','silence_threshold_db','keep_silence_ms']);
  assert.deepEqual(named(rows[2]),['fade_in_ms','fade_out_ms','gain_db']);
});

test('the Edit take button opens the panel and closes it again',async()=>{
  const p=await openPanel();
  assert.ok(p.panel());
  p.open.fire('click');
  assert.equal(p.panel(),undefined);
  assert.equal(p.open.attrs['aria-expanded'],'false');
  p.open.fire('click');
  assert.ok(p.panel());
  assert.equal(p.open.attrs['aria-expanded'],'true');
});

test('the waveform is drawn as one line for each slice, and one with no slices draws only the background',()=>{
  const w=world(),canvas={width:900,height:120,getContext:()=>w.ctx};
  w.context.drawTakeWave(canvas,plain(WAVE),null);
  const round=list=>list.map(([kind,x,y])=>[kind,Math.round(x*1e6)/1e6,Math.round(y*1e6)/1e6]);
  assert.deepEqual(round(w.path),[['m',0,60],['l',900,60],['m',112.5,54],['l',112.5,66],['m',337.5,42],['l',337.5,78],
    ['m',562.5,42],['l',562.5,78],['m',787.5,54],['l',787.5,66]]);
  w.drawn.length=0;
  w.context.drawTakeWave(canvas,{...plain(WAVE),peaks:[]},null);
  assert.deepEqual(w.drawn.map(item=>item.rect),[[0,0,900,120]],'no slices, no speech mark');
});

test('the shaded part follows the fields: a blank end is the end of the take, and a range that is empty or backwards shades nothing',async()=>{
  const p=await openPanel();
  const draw=values=>{
    for(const [name,value] of Object.entries(values))p.field(name).value=value;
    p.w.drawn.length=0;
    p.field('gain_db').fire('input');
    return p.w.drawn.map(item=>item.rect.map(number=>Math.round(number*1e6)/1e6));
  };
  const background=[0,0,900,120],speech=[180,116,540,3];
  assert.deepEqual(draw({}),[background,[0,0,900,120],speech],'both blank: the whole take');
  assert.deepEqual(draw({start_seconds:'0.5'}),[background,[225,0,675,120],speech],'a blank end is the end of the take');
  assert.deepEqual(draw({start_seconds:'',end_seconds:'1.5'}),[background,[0,0,675,120],speech],'a blank start is the start of the take');
  assert.deepEqual(draw({start_seconds:'1',end_seconds:'1'}),[background,speech],'an empty range shades nothing');
  assert.deepEqual(draw({start_seconds:'1.5',end_seconds:'0.5'}),[background,speech],'a backwards range shades nothing');
  assert.deepEqual(draw({start_seconds:'abc',end_seconds:''}),[background,speech],'text that is not a number shades nothing');
});

test('Hear and Save say what they are doing, and a complaint goes when the fields change or the call is tried again',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';
  p.button('Hear the edit').fire('click');
  assert.equal(p.text('status').textContent,'Making the edited clip…');
  reply(p.w.calls[1],{ok:false,status:400,json:{error:'Nothing to hear.'}});
  await tick();
  assert.equal(p.text('alert').textContent,'Nothing to hear.');
  p.field('gain_db').value='4';p.field('gain_db').fire('input');
  assert.equal(p.text('alert').textContent,'','a change clears the complaint');
  p.button('Save as new take').fire('click');
  assert.equal(p.text('status').textContent,'Saving…');
  reply(p.w.calls[2],{ok:false,status:500,json:{error:'The library is busy.'}});
  await tick();
  assert.equal(p.text('status').textContent,'','a refused save is no longer "Saving…"');
  assert.equal(p.text('alert').textContent,'The library is busy.');
  p.button('Hear the edit').fire('click');
  assert.equal(p.text('alert').textContent,'','trying again clears the complaint');
});

test('a second preview releases the first, and a change stops and hides the clip being played',async()=>{
  const p=await openPanel();
  const player=()=>descendants(p.panel()).find(node=>node.tag==='audio');
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  p.button('Hear the edit').fire('click');
  reply(p.w.calls[2],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.deepEqual(p.w.urls,['blob:1','blob:2']);
  assert.deepEqual(p.w.revoked,['blob:1'],'the clip it replaces is released');
  assert.equal(player().src,'blob:2');
  const stopped=p.w.paused;
  p.field('gain_db').value='4';p.field('gain_db').fire('input');
  assert.equal(p.w.paused,stopped+1,'the old clip stops playing');
  assert.equal(player().src,'','it no longer points at the old clip');
  assert.equal(player().hidden,true);
});

test('a browser that will not start playback is not a complaint',async()=>{
  const p=await openPanel();
  p.w.playRejects=true;
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();await tick();
  assert.equal(p.text('alert').textContent,'');
  assert.equal(p.text('status').textContent,'The edited clip is 2.00 s, peaking at -7 dBFS.');
});

test('a refused preview of an edit that has since changed is not shown',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='24';p.button('Hear the edit').fire('click');
  p.field('gain_db').value='3';p.field('gain_db').fire('input');
  reply(p.w.calls[1],{ok:false,status:400,json:{error:'A gain of 24 dB would clip the loudest sample.'}});
  await tick();
  assert.equal(p.text('alert').textContent,'');
});

test('a preview that arrives after the take list was drawn again is dropped',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  p.w.page.removeChild(p.article);                       // the list was reloaded: this panel is no longer on the page
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.equal(p.w.urls.length,0);
  assert.equal(p.text('alert').textContent,'');
});

test('ticking the silence box makes an earlier preview out of date',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  const box=p.field('auto_trim');box.checked=true;box.fire('change');
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.equal(p.w.urls.length,0);
});

test('a refused save that is answered after the panel was closed is still reported',async()=>{
  const p=await openPanel();
  p.field('gain_db').value='3';p.button('Save as new take').fire('click');
  p.button('Close').fire('click');
  reply(p.w.calls[1],{ok:false,status:500,json:{error:'The library is busy.'}});
  await tick();
  assert.deepEqual(p.shown,['The library is busy.']);
});

test('closing a panel that is already closed does nothing more',()=>{
  const w=world(),slot=w.page.appendChild(new El('div',w));
  let closed=0;
  const editor=w.context.openTakeEditor(slot,TAKE,()=>Promise.resolve(),()=>{},()=>{closed++;});
  editor.close();editor.close();
  assert.equal(closed,1);
});

test('a pointer position is measured from the left edge of the waveform, and follows the pointer as it moves',async()=>{
  const p=await openPanel();
  p.w.rectLeft=100;
  const canvas=descendants(p.panel()).find(node=>node.tag==='canvas');
  canvas.fire('pointerdown',{button:0,clientX:550,pointerId:1});       // 450 px along the waveform: 1.0 s
  p.w.drawn.length=0;
  canvas.fire('pointermove',{clientX:775});                            // 675 px along: 1.5 s
  assert.ok(p.w.drawn.some(item=>item.rect[0]===450&&item.rect[2]===225),'the kept part is shaded while the pointer moves');
  canvas.fire('pointerup',{});
  assert.deepEqual([p.field('start_seconds').value,p.field('end_seconds').value],['1','1.5']);
});

test('a drag cannot start before the waveform is there, and one that has ended does not go on',async()=>{
  const early=await openPanel({wave:null});
  const earlyCanvas=descendants(early.panel()).find(node=>node.tag==='canvas');
  earlyCanvas.fire('pointerdown',{button:0,clientX:225,pointerId:1});
  earlyCanvas.fire('pointermove',{clientX:450});
  earlyCanvas.fire('pointerup',{});
  assert.deepEqual([early.field('start_seconds').value,early.field('end_seconds').value],['','']);
  const p=await openPanel();
  const canvas=descendants(p.panel()).find(node=>node.tag==='canvas');
  canvas.fire('pointerdown',{button:0,clientX:225,pointerId:1});
  canvas.fire('pointermove',{clientX:450});
  canvas.fire('pointerup',{});
  canvas.fire('pointermove',{clientX:900});                            // the pointer is up: moving it changes nothing
  assert.deepEqual([p.field('start_seconds').value,p.field('end_seconds').value],['0.5','1']);
});

test('a click on the waveform is not a change: a preview on its way still arrives',async()=>{
  const p=await openPanel();
  const canvas=descendants(p.panel()).find(node=>node.tag==='canvas');
  p.field('gain_db').value='3';p.button('Hear the edit').fire('click');
  canvas.fire('pointerdown',{button:0,clientX:225,pointerId:1});
  canvas.fire('pointerup',{});
  reply(p.w.calls[1],{headers:{'X-Audio-Seconds':'2.00','X-Peak-Dbfs':'-7'}});
  await tick();
  assert.equal(p.w.urls.length,1);
});

test('an edit saved from the take list refreshes the list and says so on the Perform page',async()=>{
  const w=world(),container=w.page.appendChild(new El('div',w));
  let reloaded=0;
  Object.assign(w.context,{$:()=>container,appendTakeChecks(){},appendTakeLoudness(){},appendTakeRetirement(){},loadCurrentTakes(){reloaded++;}});
  vm.runInContext(between('function renderTakes(takes){','var takeLoadRevision=0;'),w.context);
  w.context.renderTakes([{id:TAKE.id,character_name:'Nell',identity_label:'Warm',line_id:'line-01',status:'candidate',duration_seconds:2,
    spoken_text:'Hold',model:{backend:'kokoro',checkpoint:'c',checkpoint_revision:'r',mode:'preset'},audio_url:'/a.wav',retired:false}]);
  const article=container.children[0];
  const find=(root,label)=>descendants(root).find(node=>node.tag==='button'&&node.textContent===label);
  find(article,'Edit take…').fire('click');
  reply(w.calls[0],{json:WAVE});await tick();
  const panel=descendants(article).find(node=>node.className==='take-edit');
  descendants(panel).find(node=>node.attrs['data-edit-field']==='gain_db').value='3';
  find(panel,'Save as new take').fire('click');
  reply(w.calls[1],{status:201,json:{take:{duration_seconds:1.5},replayed:false}});
  await tick();await tick();
  assert.equal(reloaded,1,'the take list is loaded again');
  assert.equal(container.textContent,'Saved as a new take, 1.50 s. The original is unchanged.');
});
