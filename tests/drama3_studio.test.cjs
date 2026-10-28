const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../drama3_studio.js'),'utf8');
const modulePromise=import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

class Element {
  constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.dataset={};this.value='';this.textContent='';this.isConnected=true;this.classList={add:()=>{}};}
  setAttribute(k,v){this.attrs[k]=v;if(k==='value')this.value=v;}
  append(...items){for(const n of items){n.parent=this;this.children.push(n);}if(this.tag==='select'&&this.children.length&&!this.value)this.value=this.children[0].value;}
  replaceChildren(...items){this.children=[];this.append(...items);}
  remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
  focus(){}
  setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}
}
const all=root=>[root,...root.children.flatMap(all)];
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('inline cue insertion preserves dialogue and all speaker/cue markup stays out of words',async()=>{
  const m=await modulePromise;
  assert.equal(m.insertCue('Stay close.',5,10,'[whispers]').text,'Stay [whispers] close.');
  assert.equal(m.plainScript('<|speaker:1|> [whispers] Stay close.'),'Stay close.');
});

for(const cancel of [false,true])test('actual studio '+(cancel?'cancels without offering a partial take':'streams previews and offers only a completed take'),async()=>{
  global.document={createElement:tag=>new Element(tag),getElementById:()=>null,head:new Element('head')};
  let posts=[],reads=0,stopped=false,plays=0;
  global.AudioContext=class {
    constructor(){this.state='running';this.currentTime=0;}
    async resume(){}
    createBuffer(_,n,rate){return {duration:n/rate,getChannelData:()=>new Float32Array(n)};}
    createBufferSource(){return {connect(){},start(){plays++;},stop(){}};}
  };
  const job={id:'f'.repeat(32),created:1,status:'running'};
  global.fetch=async(url,options={})=>{
    let data;
    if(url.endsWith('/info'))data={max_text:8000,cues:['whispers'],sources:[{id:'a',label:'Keeper'}],languages:[{code:'auto',label:'Auto'}],jobs:[]};
    else if(options.method==='POST'&&url.endsWith('/jobs')){posts.push(JSON.parse(options.body));data={job};}
    else if(url.endsWith('/cancel')){stopped=true;data={job:{...job,status:'cancelling'}};}
    else {reads++;data={job:{...job,status:stopped?'cancelled':reads===1?'running':'complete'},pcm:reads===1?'AQACAA==':'',offset:4,available_bytes:4,sample_rate:44100};}
    return {ok:true,json:async()=>data};
  };
  const root=new Element('section');const view=await (await modulePromise).mountDramaStudio(root);
  const find=text=>all(root).find(n=>n.tag==='button'&&n.textContent===text);
  const area=all(root).find(n=>n.tag==='textarea');area.value='Keep the gate closed.';
  all(root).find(n=>n.attrs['aria-label']==='Seed').value='0';
  await find('Add cast slot').onclick();all(root).find(n=>n.attrs['aria-label']==='Saved cast voice').value='a';
  await find('Generate & audition').onclick();await pause(10);
  assert.equal(posts[0].settings.seed,0);assert.equal(posts[0].cast[0].source_id,'a');assert.equal(posts[0].language,'auto');
  assert.equal(all(root).filter(n=>n.tag==='audio').length,0);
  assert.equal(plays,1);
  if(!cancel){view.stopListening();assert.equal(all(root).find(n=>n.tag==='label'&&n.children[0]?.textContent==='Play audio as it arrives').children[1].checked,false);}
  if(cancel)await find('Stop take').onclick();
  await pause(550);
  assert.equal(all(root).filter(n=>n.tag==='audio').length,cancel?0:1);
  assert.equal(all(root).filter(n=>n.tag==='a').length,cancel?0:3);
  assert.equal(find('Generate & audition').disabled,false);
});

test('recent take restores the pinned cast, script and seed for a fresh generation',async()=>{
  global.document={createElement:tag=>new Element(tag),getElementById:()=>null,head:new Element('head')};
  const job={id:'a'.repeat(32),created:1,status:'complete',request:{text:'<|speaker:1|> Stay close.',language:'auto',
    cast:[{speaker:1,source_id:'old-pinned-version'}],settings:{seed:0,temperature:.8},description:'A clear voice'}};
  global.fetch=async url=>({ok:true,json:async()=>url.endsWith('/info')?
    {max_text:8000,cues:[],sources:[],languages:[{code:'auto',label:'Auto'}],jobs:[job]}:
    {job,pcm:'',offset:0,available_bytes:0}});
  const root=new Element('section');await (await modulePromise).mountDramaStudio(root);
  const history=all(root).find(n=>n.attrs['aria-label']==='Recent Drama takes');history.value=job.id;history.onchange();await pause(10);
  assert.equal(all(root).find(n=>n.tag==='textarea').value,job.request.text);
  assert.equal(all(root).find(n=>n.attrs['aria-label']==='Seed').value,'0');
  assert.equal(all(root).find(n=>n.attrs['aria-label']==='Saved cast voice').value,'old-pinned-version');
});
