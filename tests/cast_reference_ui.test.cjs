const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const {pageSource}=require('./page_source.cjs');
const html=pageSource();
function between(start,end){const at=html.indexOf(start);return html.slice(at,html.indexOf(end,at));}
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function fields(){const map={};return id=>map[id]||(map[id]={value:'',disabled:false,textContent:'',hidden:false,
  getContext:()=>({clearRect(){}}),pause(){},load(){},removeAttribute(){}});}
test('decoding a new file immediately invalidates the old upload and ignores a late old decode',async()=>{
  const $=fields(),first=deferred(),second=deferred(),a={name:'a.wav'},b={name:'b.wav'};
  const c=vm.createContext({$,referenceLoadRevision:0,referencePreviewRevision:0,referenceDecodePending:false,
    referenceSourceFile:{name:'previous.wav'},referenceSourceBlob:{},referenceBuffer:{duration:1},
    decodeBlob:blob=>blob===a?first.promise:second.promise,setReferencePlayer(){},
    syncNoiseReductionFields(){},bufferAnalysis:()=>({clipped:0}),drawReferenceWave(){}});
  vm.runInContext(between('function loadReferenceSource(', 'function fillFixedVoices('),c);
  const old=c.loadReferenceSource(a,a);old.catch(()=>{});
  assert.equal(c.referenceSourceFile,null);assert.equal(c.referenceBuffer,null);assert.equal($('saveReference').disabled,true);
  const current=c.loadReferenceSource(b,b);second.resolve({duration:8,sampleRate:24000,numberOfChannels:1});await current;
  first.resolve({duration:3,sampleRate:24000,numberOfChannels:1});await assert.rejects(old,/chosen recording changed/);
  assert.equal(c.referenceSourceFile,b);assert.equal(c.referenceBuffer.duration,8);assert.equal($('saveReference').disabled,false);
});
test('a pending processed preview cannot repaint a different reference',async()=>{
  const $=fields(),reply=deferred();let repainted=0;
  const c=vm.createContext({$,referenceLoadRevision:4,referenceSourceBlob:{type:'audio/wav'},referenceSourceFile:{name:'test.wav'},
    referenceBuffer:{},referencePreviewRevision:0,activeTrim:()=>({start:0,end:8}),activeIdentity:()=>({id:'identity',kind:'reference'}),
    selectedReference:()=>null,fillReferenceMetadata:()=>({}),encodeReferenceMetadata:()=>'',
    fetch:()=>reply.promise,setReferencePlayer(){repainted++;},drawReferenceWave(){}});
  vm.runInContext(between('function previewReferenceClip()', 'function loadReferenceSource('),c);c.previewReferenceClip();
  c.referenceLoadRevision=5;reply.resolve({ok:true,blob:async()=>({}),headers:{get:()=> '8'}});
  await new Promise(r=>setImmediate(r));assert.equal(repainted,0);assert.doesNotMatch($('referenceStatus').textContent,/Preview ready/);
});
test('only the latest processed preview may replace audio for one source',async()=>{
  const $=fields(),first=deferred(),second=deferred();let requests=0;const painted=[];
  const c=vm.createContext({$,referenceLoadRevision:4,referencePreviewRevision:0,referenceSourceBlob:{},referenceSourceFile:{name:'test.wav'},referenceBuffer:{},
    activeTrim:()=>({start:0,end:8}),activeIdentity:()=>({id:'identity',kind:'reference'}),selectedReference:()=>null,
    fillReferenceMetadata:()=>({}),encodeReferenceMetadata:()=>'',fetch:()=>++requests===1?first.promise:second.promise,
    setReferencePlayer(blob){painted.push(blob);},drawReferenceWave(){}});
  vm.runInContext(between('function previewReferenceClip()', 'function loadReferenceSource('),c);
  c.previewReferenceClip();c.previewReferenceClip();
  const response=value=>({ok:true,blob:async()=>value,headers:{get:()=> '8'}});
  second.resolve(response('new'));await new Promise(r=>setImmediate(r));
  first.resolve(response('old'));await new Promise(r=>setImmediate(r));assert.deepEqual(painted,['new']);
});
test('switching identities during microphone permission closes the late stream',async()=>{
  const $=fields(),permission=deferred();let stopped=0;
  const c=vm.createContext({$,referenceLoadRevision:2,navigator:{mediaDevices:{getUserMedia:()=>permission.promise}}});
  vm.runInContext(between('function beginMicCapture()', 'function pcmCaptureWav('),c);c.beginMicCapture();
  c.referenceLoadRevision++;permission.resolve({getTracks:()=>[{stop(){stopped++;}}]});
  await new Promise(r=>setImmediate(r));assert.equal(stopped,1);assert.equal(c.micStream,undefined);
});
test('a closed preview AudioContext is replaced before another recording or decode',()=>{
  let created=0;const c=vm.createContext({audioContext:{state:'closed'},window:{AudioContext:class{constructor(){created++;this.state='running';}}}});
  vm.runInContext(between('function apiAudioContext()', 'function decodeBlob('),c);
  assert.equal(c.apiAudioContext().state,'running');assert.equal(created,1);c.apiAudioContext();assert.equal(created,1);
});
