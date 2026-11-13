// The page's side of the line for the model worker (voice_lab_queue.js): its model requests wait in line under ids it chose,
// and the Model worker panel says what runs and what waits, with Stop and Cancel.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const script=fs.readFileSync(path.join(__dirname,'..','voice_lab_queue.js'),'utf8');

class El{
  constructor(tag){this.tagName=tag;this.children=[];this.dataset={};this.hidden=false;this.disabled=false;this.className='';
    this.handlers={};this._text='';}
  set textContent(value){this._text=String(value);if(value==='')this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent).join('');}
  appendChild(child){this.children.push(child);return child;}
  addEventListener(type,fn){this.handlers[type]=fn;}
  click(){return this.handlers.click&&this.handlers.click();}
  querySelectorAll(selector){const name=selector.replace(/^\./,''),found=[];
    const walk=node=>node.children.forEach(child=>{if(child.className===name)found.push(child);walk(child);});walk(this);return found;}
}
function page(){
  const nodes={queuePanel:new El('div'),queueItems:new El('div'),queueError:new El('p')},calls=[],timers=[];
  nodes.queuePanel.hidden=true;
  const answers={};
  const fetch=(url,init)=>{const call={url,init};calls.push(call);
    return new Promise((resolve,reject)=>{call.resolve=resolve;call.reject=reject;if(answers[url])resolve(answers[url]);});};
  const context={document:{hidden:false,getElementById:id=>nodes[id]||null,createElement:tag=>new El(tag)},fetch,
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){},Date,Math,JSON,Object,Array,String,
    encodeURIComponent,catalog:{engines:[{id:'kokoro',name:'Kokoro 82M'}]}};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(script,context);
  return {context,nodes,calls,timers,answers,queue:context.VoiceLabQueue};
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const reply=(status,json)=>({ok:status<300,status,json:async()=>json});
// The script runs in a context of its own, so its arrays are copied into this one before they are compared.
const rows=(lines,...keys)=>Array.from(lines,line=>keys.map(key=>line[key]));

test('the routes that use the model worker are the ones the page sends in line',()=>{
  const {queue}=page();
  for(const url of ['/api/speak','/api/stream','/api/performance','/api/load','/api/unload','/api/convert',
    '/api/prepare-reference','/api/kokoro/phonemize','/api/engines/kokoro/check','http://127.0.0.1:8000/api/speak?x=1'])
    assert.ok(queue.workerRoute(url),url);
  for(const url of ['/api/performance/settings','/api/updates/check','/api/scenes/jobs/j/cancel','/api/queue','/api/takes'])
    assert.ok(!queue.workerRoute(url),url);
});

test('a model request waits in line under an id the page chose, and one that says otherwise keeps it',()=>{
  const {queue}=page();
  const request=queue.inLine({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:'hi'})});
  assert.equal(JSON.parse(request.init.body).wait_seconds,3600);
  assert.equal(JSON.parse(request.init.body).text,'hi');
  assert.match(request.id,/^[A-Za-z0-9._-]{1,64}$/);
  assert.equal(request.init.headers['X-Request-Id'],request.id);
  assert.equal(request.init.headers['Content-Type'],'application/json');
  const own=queue.inLine({method:'POST',headers:{'x-request-id':'mine-1'},body:JSON.stringify({wait_seconds:0})});
  assert.equal(own.id,'mine-1');
  assert.equal(JSON.parse(own.init.body).wait_seconds,0);
  assert.equal(queue.inLine({method:'POST',body:'not json'}),null);
  assert.equal(queue.inLine({method:'POST',body:'[1]'}),null);
  assert.equal(queue.inLine(undefined),null);
});

test('the page fetch sends model requests in line, counts them until they answer, and leaves others alone',async()=>{
  const {context,calls}=page();
  const answer=context.fetch('/api/speak',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"text":"hi"}'});
  const id=calls[0].init.headers['X-Request-Id'];
  assert.ok(id);
  assert.deepEqual([...context.VoiceLabQueue.pending()],[id]);
  calls[0].resolve(reply(200,{}));await answer;await settle();
  assert.deepEqual([...context.VoiceLabQueue.pending()],[]);
  const failed=context.fetch('/api/load',{method:'POST',body:'{}'});calls[1].reject(new Error('gone'));
  await assert.rejects(failed,/gone/);await settle();
  assert.deepEqual([...context.VoiceLabQueue.pending()],[]);
  const init={method:'POST',body:'{"name":"x"}'};
  context.fetch('/api/characters',init);
  assert.equal(calls[2].init,init);
});

test('the panel says how many are ahead and what the worker is busy with',()=>{
  const {queue}=page();
  const batch={id:'scene-row-7',what:'Take for a scene batch',engine:'kokoro',priority:'low',seconds:12.4,can_stop:true};
  const lines=queue.describe({running:batch,waiting:[{id:'other',priority:'normal'},{id:'mine'},{id:'row'}]},{mine:true});
  assert.deepEqual(rows(lines,'id','action','text'),[
    ['scene-row-7','stop','Now: Take for a scene batch · Kokoro 82M · 12 s'],
    ['mine','cancel','Waiting: 2 ahead of you (a scene batch is rendering)'],
    ['others',null,'2 more requests wait']]);
  const install={id:'i',what:'Install',engine:null,priority:'normal',seconds:3,can_stop:false,detail:'Breeze TTS 2 is being installed'};
  assert.deepEqual(rows(queue.describe({running:install,waiting:[{id:'mine'}]},{mine:true}),'action','text'),[
    [null,'Now: Install · 3 s'],['cancel','Waiting: 1 ahead of you (Breeze TTS 2 is being installed)']]);
  const clip={id:'c',what:'Clip',engine:'kokoro',priority:'normal',seconds:1,can_stop:true};
  assert.equal(queue.describe({running:clip,waiting:[]},{c:true})[0].text,'Now: Clip · Kokoro 82M · 1 s (yours)');
  assert.equal(queue.describe({running:clip,waiting:[{id:'m'}]},{m:true})[1].text,'Waiting: 1 ahead of you (a clip is being made)');
  assert.equal(queue.describe({running:null,waiting:[]},{}).length,0);
  assert.equal(queue.describe(null,{}).length,0);
});

test('the panel shows Stop beside what runs and Cancel beside a waiting request, and each asks the service',async()=>{
  const {context,nodes,calls,timers}=page();
  void context.fetch('/api/performance',{method:'POST',body:'{"text":"a"}'});
  const id=calls[0].init.headers['X-Request-Id'];
  calls.length=0;
  timers.at(-1).fn();  // the panel asks what runs
  assert.equal(calls[0].url,'/api/queue');
  calls[0].resolve(reply(200,{running:{id:'scene-row-1',what:'Take for a scene batch',engine:'kokoro',priority:'low',
    seconds:2,can_stop:true},waiting:[{id,what:'Take',priority:'normal'}],max_waiting:20}));
  await settle();
  assert.equal(nodes.queuePanel.hidden,false);
  const items=nodes.queueItems.children;
  assert.match(items[0].textContent,/^Now: Take for a scene batch · Kokoro 82M · 2 sStop/);
  assert.match(items[0].textContent,/the model loads again for the next request/);
  assert.equal(items[1].textContent,'Waiting: 1 ahead of you (a scene batch is rendering)Cancel');
  const stop=items[0].children[1];
  stop.click();
  assert.equal(calls[1].url,'/api/queue/scene-row-1/cancel');
  assert.equal(calls[1].init.method,'POST');
  assert.equal(stop.disabled,true);
  assert.equal(stop.textContent,'Stopping…');
  const cancel=items[1].children[1];
  cancel.click();
  assert.equal(calls[2].url,'/api/queue/'+id+'/cancel');
  assert.equal(cancel.textContent,'Cancelling…');
  calls[1].resolve(reply(200,{cancelled:'running'}));
  calls[2].resolve(reply(400,{error:'Install cannot be stopped.'}));
  await settle();await settle();
  timers.at(-1).fn();  // the next look at the queue shows what failed
  calls[3].resolve(reply(200,{running:null,waiting:[],max_waiting:20}));
  await settle();
  assert.equal(nodes.queueError.textContent,'Install cannot be stopped.');
  assert.equal(nodes.queuePanel.hidden,false);
  assert.equal(nodes.queueItems.children.length,0);
});

test('the panel stays through the moment between two lines of a batch, and hides when the worker stays free',async()=>{
  const {nodes,calls,timers}=page();
  const look=async state=>{timers.at(-1).fn();calls.at(-1).resolve(reply(200,state));await settle();};
  const row=n=>({running:{id:'scene-row-'+n,what:'Take for a scene batch',engine:'kokoro',priority:'low',seconds:1,can_stop:true},
    waiting:[],max_waiting:20});
  const free={running:null,waiting:[],max_waiting:20};
  await look(free);
  assert.equal(nodes.queuePanel.hidden,true);
  await look(row(1));
  assert.equal(nodes.queuePanel.hidden,false);
  await look(free);
  assert.equal(nodes.queuePanel.hidden,false);
  assert.match(nodes.queueItems.textContent,/scene batch/);
  await look(row(2));
  assert.equal(nodes.queuePanel.hidden,false);
  await look(free);await look(free);
  assert.equal(nodes.queuePanel.hidden,true);
});
