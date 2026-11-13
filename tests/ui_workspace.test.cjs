// Run with node --test tests/ui_workspace.test.cjs. Exercise actual inline functions.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {pageSource} = require('./page_source.cjs');
const source = pageSource();
// The page's scripts with all white space taken out, for checks that should not depend on how a script is formatted.
const compact = source.replace(/\s+/g, '');
function sourceBetween(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}

test('retire, restore, and purge confirm first and report exactly what they block or remove', async () => {
  const calls = [];
  let confirmAnswer = true;
  const c = vm.createContext({
    window: {confirm: () => confirmAnswer},
    encodeURIComponent,
    fetchJson: async (url, body) => {
      calls.push(JSON.parse(JSON.stringify({url, body: body === undefined ? null : body})));
      if (url.startsWith('/api/purges/preview')) return {would_purge: {identity: 0, reference: 1, take: 2}, external_copies: []};
      if (url === '/api/purges') return {purged: {identity: 0, reference: 1, take: 2}, external_copies: ['out/exports/copy.wav'], external_copies_truncated: false};
      return {affected: {reference: 1, take: 2}};
    },
  });
  vm.runInContext(sourceBetween('function retirementLabel(', 'function renderTakes('), c);
  assert.equal(c.retirementLabel({retirement: {reason: 'Bad take.', inherited: false}}), 'Bad take.');
  assert.equal(c.retirementLabel({retirement: {reason: 'Withdrawn.', inherited: true, source_label: 'its voice'}}),
    'Withdrawn. It is retired because its voice is retired.');
  const retired = await c.retireAction('take', 'take-1', 'this take');
  assert.equal(retired, 'Retired this take. Blocked with it: 1 recording version(s), 2 take(s). Listening stays available for review.');
  assert.deepEqual(calls.pop(), {url: '/api/retirements', body: {kind: 'take', id: 'take-1', reason: ''}});
  confirmAnswer = false;
  assert.equal(await c.retireAction('take', 'take-1', 'this take'), null);
  assert.equal(calls.length, 0);
  confirmAnswer = true;
  const purged = await c.purgeAction('reference', 'ref-1', 'this recording version');
  assert.equal(purged, 'Deleted for good: 1 voice recording, 2 takes. Copies outside Cast still hold this audio: out/exports/copy.wav. Cast does not delete them; remove them yourself.');
  assert.deepEqual(calls.map((call) => call.url), ['/api/purges/preview?kind=reference&id=ref-1', '/api/purges']);
  assert.deepEqual(calls[1].body, {kind: 'reference', id: 'ref-1', confirm: true});
});

test('take retirement controls show the correct buttons for active, retired, and inherited takes', async () => {
  const makeElement = () => ({children: [], className: '', textContent: '', disabled: false, type: '',
    addEventListener(event, handler) { this.handler = handler; },
    appendChild(child) { this.children.push(child); return child; }});
  const c = vm.createContext({document: {createElement: makeElement}, Promise, window: {confirm: () => true},
    fetchJson: async () => ({})});
  vm.runInContext(sourceBetween('function retirementLabel(', 'function renderTakes('), c);
  const buttonsFor = (take) => {
    const article = makeElement();
    c.appendTakeRetirement(article, take, async () => {}, () => {});
    const [note, row] = article.children;
    return {note: note.textContent, buttons: row.children.map((button) => button.textContent)};
  };
  assert.deepEqual(buttonsFor({id: 't', retired: false, retirement: null}).buttons, ['Retire take']);
  const own = buttonsFor({id: 't', retired: true, retirement: {reason: 'Bad take.', inherited: false}});
  assert.deepEqual(own.buttons, ['Restore take', 'Delete this take for good…']);
  assert.match(own.note, /Bad take\. New use is blocked/);
  const inherited = buttonsFor({id: 't', retired: true, retirement: {reason: 'Withdrawn.', inherited: true, source_label: 'its voice'}});
  assert.deepEqual(inherited.buttons, ['Delete this take for good…']);
  assert.match(inherited.note, /because its voice is retired/);
  assert.match(compact, /appendTakeRetirement\(article,take,loadCurrentTakes/);
  assert.match(compact, /appendTakeRetirement\(article,\{id:row\.take_id,retired:!!row\.retirement,retirement:row\.retirement\},refreshSceneJob/);
  assert.match(compact, /choose\.disabled=!!row\.retirement/);
  assert.match(compact, /if\(row\.purged\)/);
});

test('take waveform checks are plain text beside each Perform and Scenes take', () => {
  const c = vm.createContext({document: {createElement() {
    return {children: [], className: '', textContent: '', appendChild(child) { this.children.push(child); }};
  }}});
  vm.runInContext(sourceBetween('var takeCheckLabels=', 'function renderTakes('), c);
  const parent = {children: [], appendChild(child) { this.children.push(child); }};
  c.appendTakeChecks(parent, {status: 'checked', flags: ['long_pause', 'silent_audio'], longest_pause_seconds: 1.5});
  assert.equal(parent.children[0].textContent,
    'Automatic waveform checks flagged: Long pause inside the take (1.5 s); Silent. Listen before selecting.');
  assert.equal(parent.children[0].className, 'meta take-checks flagged');
  c.appendTakeChecks(parent, {status: 'checked', flags: []});
  assert.match(parent.children[1].textContent, /found no flags/);
  assert.equal(parent.children[1].className, 'meta take-checks');
  c.appendTakeChecks(parent, {status: 'not_recorded', flags: []});
  assert.match(parent.children[2].textContent, /not recorded/);
  c.appendTakeChecks(parent, {status: 'unavailable', flags: [], error: 'disk full'});
  assert.equal(parent.children[3].textContent, 'Waveform checks could not run: disk full.');
  assert.equal(parent.children[3].className, 'meta take-checks flagged');
  assert.match(compact, /appendTakeChecks\(article,take\.waveform_checks\)/);
  assert.match(compact, /appendTakeChecks\(article,row\.waveform_checks\)/);
});

test('cloning is a full workspace; navigation shows exactly one panel', () => {
  const ids = ['studio','cloning','cast','perform','scenes','conversion','library','models','system'];
  const opened = [];
  const fields = Object.fromEntries(ids.map(id=>[id,{hidden:false,classList:{add(){}},
    querySelector:()=>({textContent:id})}]));
  const links = ids.map(id=>({current:false,getAttribute:()=> '#'+id,
    setAttribute(){this.current=true;},removeAttribute(){this.current=false;}}));
  const c = vm.createContext({$:id=>fields[id],location:{hash:'#cloning'},
    loadReferences(){},refreshConversionAssets(){},refreshEngineStatus(){},refreshSystemPage(){opened.push('system');},
    document:{documentElement:{classList:{add(){}}},querySelectorAll:()=>links},
    window:{scrollTo(){}}});
  vm.runInContext(sourceBetween('function showWorkspace()', "window.addEventListener('hashchange',showWorkspace)"),c);
  for(const active of ['cloning','cast','perform','studio','models','system','unknown']){
    c.location.hash='#'+active;c.showWorkspace();
    const expected=active==='unknown'?'studio':active;
    assert.deepEqual(ids.filter(id=>!fields[id].hidden),[expected]);
    assert.deepEqual(ids.filter((id,index)=>links[index].current),[expected]);
    assert.equal(c.document.title,"Fel's Voice Lab · "+expected);
  }
  assert.deepEqual(opened,['system'],'only the System workspace reads the system report, and only when it is opened');
});

test('Chatterbox cloning sends the selected recording rather than a hardcoded preset',()=>{
  const fields={},field=id=>fields[id]||(fields[id]={value:'',checked:false});
  field('engine').value='turbo';field('text').value='Test.';field('clone').checked=true;
  field('chatterboxReference').value='/managed/new-character.wav';
  const c=vm.createContext({$:field,selectedValues:()=>[],qwenModeFor:()=>null});
  vm.runInContext(sourceBetween('function bodyForRequest()', 'function makeHistory('),c);
  assert.equal(c.bodyForRequest().reference_path,'/managed/new-character.wav');
  field('clone').checked=false;assert.equal(c.bodyForRequest().reference_path,undefined);
  field('engine').value='orig';field('clone').checked=true;
  assert.equal(c.bodyForRequest().reference_path,'/managed/new-character.wav');
});

test('recent clip rendering retains previous articles and the supported zero seed',()=>{
  const elements=[];
  function element(){return {children:[],dataset:{},className:'',appendChild(item){this.children.push(item);},addEventListener(){}};}
  const history=element();history.insertBefore=function(row){this.children.unshift(row);};
  history.querySelector=function(selector){return selector==='article'&&this.children.length?this.children[0]:null;};
  Object.defineProperty(history,'textContent',{set(){this.children=[];}});
  const player={};const c=vm.createContext({$:id=>id==='hist'?history:player,
    document:{createElement(){const el=element();elements.push(el);return el;}},
    URL:{createObjectURL:()=> 'blob:audio'},Date,VoiceLabWorkflows:{openFromClip(){}}});
  vm.runInContext(sourceBetween('function makeHistory(', 'function generate('),c);
  c.makeHistory({}, {engine:'drama-3',seed:0,text:'First'}, 'first metadata');
  c.makeHistory({}, {engine:'drama-3',seed:1,text:'Second'}, 'second metadata');
  assert.equal(history.children.length,2);
  assert.ok(elements.some(el=>el.textContent?.includes('seed 0: First')));
});

test('Drama 3 studio sends supported design/clone controls and the exact transcript', () => {
  const fields={},field=id=>fields[id]||(fields[id]={value:'',checked:false});
  for(const [id,value] of Object.entries({engine:'drama-3',text:'Keep the gate closed.',dramaMode:'design',
    dramaSeed:'0',dramaTemperature:'1',dramaTopK:'30',dramaTopP:'.9',dramaMaxSeconds:'20',
    dramaDescription:'A clear adult voice',dramaDirection:'quietly',dramaReference:'ref.wav',dramaTranscript:'Exact words.'}))field(id).value=value;
  const c=vm.createContext({$:field,selectedValues:()=>[],qwenModeFor:()=>null});
  vm.runInContext(sourceBetween('function bodyForRequest()', 'function makeHistory('),c);
  let request=c.bodyForRequest();assert.equal(request.seed,0);assert.equal(request.voice_description,'A clear adult voice');
  assert.equal(request.max_seconds,20);assert.equal(request.repetition_penalty,undefined);assert.equal(request.speed,undefined);
  field('dramaMode').value='clone';request=c.bodyForRequest();
  assert.equal(request.reference_path,'ref.wav');assert.equal(request.reference_text,'Exact words.');
  assert.equal(request.voice_description,'');assert.equal(request.direction,'quietly');
});
const context = vm.createContext({});
vm.runInContext(sourceBetween('function editSceneField(', 'function changeSceneField('), context);
const edit = context.editSceneField;
function fixture() {
  return {scene_id:'scene',revision:12,checksum:'saved',name:'Test',lines:[{
    line_id:'a',text:'Original',identity_id:'voice',reference_version_id:'ref',
    selected_takes:{'seg-001':'take'},settings:{seed:99,cfg_value:2},
    segments:[{id:'seg-001',text:'Original'}],future_field:{preserve:true}
  },{line_id:'b',text:'Unchanged'}]};
}
test('dialogue edits retain revision, pinned identity, settings and unknown fields', () => {
  const scene=fixture(),expected=fixture();expected.lines[0].text='New line';
  assert.deepEqual(edit(scene,0,'text','New line'),expected);
});
test('numeric edits preserve zeros and validate backend bounds', () => {
  const scene=fixture();edit(scene,0,'pause_after_ms','0');edit(scene,0,'take_count','10');
  assert.equal(scene.lines[0].pause_after_ms,0);assert.equal(scene.lines[0].take_count,10);
  for(const value of ['', '-1', '10001', '1.2', 'NaN']) assert.throws(()=>edit(scene,0,'pause_before_ms',value));
  for(const value of ['', '0', '11']) assert.throws(()=>edit(scene,0,'take_count',value));
});
test('form can assign a character voice while protecting absent lines and revisions', () => {
  const scene=fixture();edit(scene,0,'character_id','character-b');edit(scene,0,'identity_id','other');
  assert.equal(scene.lines[0].character_id,'character-b');assert.equal(scene.lines[0].identity_id,'other');
  assert.throws(()=>edit(fixture(),5,'text','other'));
  assert.throws(()=>edit(fixture(),null,'revision',99));
  edit(scene,null,'name','Renamed');assert.equal(scene.name,'Renamed');assert.equal(scene.revision,12);
});
test('line operations retain stable IDs, clear duplicated selections and keep scene order', () => {
  const scene=fixture();
  const c=vm.createContext({JSON,Set,Error,sceneBody:()=>scene,
    updateSceneEditor:()=>{},renderSceneForm:()=>{}});
  vm.runInContext(sourceBetween('function stableSceneLineId(', 'function sceneIdentityById('),c);
  vm.runInContext(sourceBetween('function mutateSceneLine(', 'function addSceneLine('),c);
  assert.equal(c.stableSceneLineId(scene.lines),'line-001');
  c.mutateSceneLine(0,'duplicate');
  assert.deepEqual(scene.lines.map(line=>line.line_id),['a','line-001','b']);
  assert.equal(Object.keys(scene.lines[1].selected_takes).length,0);
  c.mutateSceneLine(2,'up');
  assert.deepEqual(scene.lines.map(line=>line.line_id),['a','b','line-001']);
  c.mutateSceneLine(2,'remove');
  assert.deepEqual(scene.lines.map(line=>line.line_id),['a','b']);
  c.mutateSceneLine(1,'remove');
  assert.throws(()=>c.mutateSceneLine(0,'remove'));
});
test('unsaved scenes request unload protection and small screens reflow the workspaces', () => {
  const c=vm.createContext({sceneDirty:true,window:{addEventListener(){}}});
  vm.runInContext(sourceBetween('function guardUnsavedSceneUnload(', 'function option('),c);
  let prevented=false;const event={preventDefault(){prevented=true;}};
  c.guardUnsavedSceneUnload(event);assert.equal(prevented,true);assert.equal(event.returnValue,'');
  c.sceneDirty=false;prevented=false;delete event.returnValue;c.guardUnsavedSceneUnload(event);assert.equal(prevented,false);
  assert.ok(source.includes('<meta name="viewport" content="width=device-width,initial-scale=1">'));
  assert.ok(source.includes('@media(max-width:700px){main{display:block'));
  assert.ok(source.includes('.workspace-nav{position:static;flex-direction:row;flex-wrap:wrap'));
});
test('performer starter copies the actual engine and preserves seed zero', () => {
  const fields={performReference:{value:''},performText:{value:'Line'},performLanguage:{value:'English'},
    performDirection:{value:'Deliver clearly.'},performSeed:{value:'0'},usePronunciations:{checked:false},
    sceneEditor:{value:''},sceneStatus:{textContent:''}};
  const c=vm.createContext({sceneDirty:false,activeSceneId:null,$:id=>fields[id],
    performanceCharacter:()=>({id:'character',name:'Mira'}),
    performanceIdentity:()=>({id:'voice',kind:'preset_voice',engine_id:'qwen3-tts-1.7b-custom-voice'}),
    performanceSettings:(identity,seed)=>({seed}),renderSceneForm(){},resetSceneHistory(){},window:{confirm:()=>true}});
  vm.runInContext(sourceBetween('function startSceneFromPerformer(', 'function importSceneFile('),c);
  c.startSceneFromPerformer();const scene=JSON.parse(fields.sceneEditor.value);
  assert.equal(scene.lines[0].engine_id,'qwen3-tts-1.7b-custom-voice');assert.equal(scene.lines[0].settings.seed,0);
});
test('saved takes expose effective settings and identity provenance', () => {
  assert.ok(source.includes("provenanceTitle.textContent='Effective settings and provenance'"));
  assert.ok(source.includes('effective_settings:take.model.effective_settings||{}'));
  assert.ok(source.includes('voice_identity:take.model.voice_identity||null'));
});
test('scene editor explains blank, empty, and invalid script states', () => {
  assert.ok(source.includes('Open a scene or start one from the current performer.'));
  assert.ok(source.includes('This script has no dialogue lines. Add a line to continue.'));
  assert.ok(source.includes('Fix the advanced JSON to use the script form.'));
  assert.ok(compact.includes("$('sceneError').textContent='';"));
});
test('failed generation retains failure status and reenables controls', async () => {
  const fields={};const field=id=>fields[id]||(fields[id]={value:'',checked:false,disabled:false,textContent:''});
  const c=vm.createContext({$:field,performanceIdentity:()=>({id:'voice',character_id:'c',engine_id:'kokoro'}),
    performanceSettings:()=>({speed:1}),fetch:()=>Promise.reject(new Error('worker unavailable')),
    loadCurrentTakes:()=>Promise.resolve(),performance:{now:()=>0},setInterval:()=>1,clearInterval(){}});
  vm.runInContext(sourceBetween('function generatePerformanceSet(', 'function loadPronunciations('),c);
  c.generatePerformanceSet();await new Promise(resolve=>setImmediate(resolve));
  assert.match(field('performStatus').textContent,/stopped/);
  assert.match(field('performError').textContent,/worker unavailable/);
  assert.equal(field('performGenerate').disabled,false);
});

function reassignmentFixture(schema,nextEngine='new-engine') {
  const scene=fixture();Object.assign(scene.lines[0],{character_id:'old-character',engine_id:'orig',
    line_type:'narration',language:'French',direction:'Whisper urgently.',pause_before_ms:0,pause_after_ms:875,
    take_count:4,target_duration_ms:9500,use_pronunciations:true,selected_take_id:'take',
    voice_identity:{id:'voice'},settings:{seed:0,cfg_value:0.75}});
  const status={textContent:''},identity={id:'new-voice',kind:'reference',engine_id:nextEngine,
    approved_reference_id:'new-ref',references:[]};
  const c=vm.createContext({sceneBody:()=>scene,sceneIdentityById:()=>({character:{id:'new-character'},identity}),
    performanceCapabilitiesByEngine:{[nextEngine]:schema},performanceUnscreenedByEngine:{},updateSceneEditor(){},renderSceneForm(){},$:()=>status});
  vm.runInContext(sourceBetween('function changeSceneIdentity(', 'function mutateSceneLine('),c);
  vm.runInContext(sourceBetween('function sceneLanguageOptions(', 'function sceneLabel('),c);
  return {c,scene,status,identity};
}

test('actual voice reassignment preserves authored line content, timing, pronunciation and compatible settings', () => {
  const schema={languages:['English','French'],direction:{status:'supported'},controls:[
    {id:'seed',type:'number',minimum:0,maximum:1000,step:1,default:42},
    {id:'cfg_value',type:'number',minimum:0,maximum:2,step:0.05,default:0.5}]};
  const {c,scene}=reassignmentFixture(schema,'orig'),before=JSON.parse(JSON.stringify(scene.lines[0]));
  c.changeSceneIdentity(0,'new-voice');const line=scene.lines[0];
  for(const key of ['line_id','text','line_type','language','direction','pause_before_ms','pause_after_ms',
    'take_count','target_duration_ms','use_pronunciations','future_field','settings'])
    assert.equal(JSON.stringify(line[key]),JSON.stringify(before[key]),key);
  assert.equal(line.character_id,'new-character');assert.equal(line.identity_id,'new-voice');
  assert.equal(line.reference_version_id,'new-ref');assert.equal(line.selected_take_id,null);
  assert.equal(JSON.stringify(line.selected_takes),'{}');assert.equal(line.voice_identity,undefined);
  assert.equal(scene.revision,12);
});

test('engine reassignment resets only unsupported generation values and reports them', () => {
  const {c,scene,status}=reassignmentFixture({languages:['English'],direction:{status:'unsupported'},controls:[
    {id:'seed',label:'Seed',type:'number',minimum:0,maximum:1000,step:1,default:42}]});
  c.changeSceneIdentity(0,'new-voice');const line=scene.lines[0];
  assert.equal(line.text,'Original');assert.equal(line.pause_after_ms,875);assert.equal(line.line_type,'narration');
  assert.equal(line.language,'English');assert.equal(line.direction,'');assert.equal(line.settings.seed,0);
  assert.equal(line.settings.cfg_value,undefined);assert.match(status.textContent,/language.*acting direction.*cfg_value/);
  line.selected_take_id='fresh-take';c.changeSceneIdentity(0,'new-voice');
  assert.equal(line.selected_take_id,'fresh-take');
});

test('both Character and Voice dropdowns preserve authored lines through their actual change handlers', () => {
  for(const selectorLabel of ['Character','Voice']){
    const {c,scene,identity}=reassignmentFixture({languages:['French'],direction:{status:'supported'},controls:[]});
    const labels={};
    function element(tag){return {tag,children:[],options:[],events:{},setAttribute(){},setCustomValidity(){},
      appendChild(child){this.children.push(child);},addEventListener(name,handler){this.events[name]=handler;}};}
    c.castCharacters=[{id:'new-character',name:'Other',identities:[identity]}];
    c.document={createElement:element,createTextNode:text=>({text})};
    c.option=(select,value)=>select.options.push({value});
    c.showSceneError=error=>{throw error;};
    c.sceneLabel=(text,widget,parent)=>{labels[text]=widget;parent.appendChild(widget);};
    vm.runInContext(sourceBetween('function unscreenedLanguages(', 'function applyQwenCapabilities('),c);
    vm.runInContext(sourceBetween('function renderSceneLineEditor(', 'function renderSceneForm('),c);
    c.renderSceneLineEditor(scene.lines[0],0,element('article'));
    const selector=labels[selectorLabel];selector.value=selectorLabel==='Character'?'new-character':'new-voice';
    selector.events.change.call(selector);
    assert.equal(scene.lines[0].identity_id,'new-voice');assert.equal(scene.lines[0].text,'Original');
    assert.equal(scene.lines[0].target_duration_ms,9500);assert.equal(scene.lines[0].line_type,'narration');
    assert.equal(scene.lines[0].take_count,4);assert.equal(scene.lines[0].use_pronunciations,true);
  }
});

test('unscreened picker languages are labelled in every language picker, and the value stays the language', () => {
  const c = vm.createContext({performanceUnscreenedByEngine:{'qwen3-tts-1.7b-base':['Portuguese']}});
  vm.runInContext(sourceBetween('function unscreenedLanguages(', 'function applyQwenCapabilities('), c);
  assert.equal(c.languageOptionLabel('qwen3-tts-1.7b-base', 'Portuguese'), 'Portuguese (unscreened)');
  assert.equal(c.languageOptionLabel('qwen3-tts-1.7b-base', 'German'), 'German');
  assert.equal(c.languageOptionLabel('kokoro', 'Spanish'), 'Spanish');
  assert.equal(c.unscreenedLanguages('kokoro').length, 0);
});

test('preset switch applies its saved language to the request; refreshing retains an explicit override', async () => {
  let identity={id:'french',kind:'preset_voice',engine_id:'custom',engine_options:{speaker:'Serena',
    language:'French',supported_languages:['English','French'],default_direction:'Calm.'}};
  const fields={},field=id=>fields[id]||(fields[id]={value:'',textContent:'',parentElement:{}}),requests=[];
  field('performLanguage').value='English';
  const c=vm.createContext({$:field,performanceCharacter:()=>({id:'character'}),performanceIdentity:()=>identity,
    lastPerformanceIdentityId:'english',performanceCapabilitiesByEngine:{custom:{controls:[],direction:{status:'supported'}}},performanceUnscreenedByEngine:{},
    option(){},renderPerformanceSettings(){},loadPronunciations(){},performanceSettings:()=>({}),
    fetch:(_url,request)=>{requests.push(JSON.parse(request.body));return Promise.resolve({ok:true,blob:()=>Promise.resolve({})});},
    loadCurrentTakes:()=>Promise.resolve(),performance:{now:()=>0},setInterval:()=>1,clearInterval(){}});
  vm.runInContext(sourceBetween('function unscreenedLanguages(', 'function applyQwenCapabilities('),c);
  vm.runInContext(sourceBetween('function renderPerformanceIdentity()', 'function renderPerformanceSettings('),c);
  c.renderPerformanceIdentity();assert.equal(field('performLanguage').value,'French');
  assert.match(field('performSupport').textContent,/Serena \/ French/);
  field('performLanguage').value='English';c.renderPerformanceIdentity();
  assert.equal(field('performLanguage').value,'English');assert.match(field('performSupport').textContent,/Serena \/ English/);
  identity={...identity,id:'french-two'};c.renderPerformanceIdentity();
  assert.equal(field('performLanguage').value,'French');
  vm.runInContext(sourceBetween('function generatePerformanceSet(', 'function loadPronunciations('),c);
  c.generatePerformanceSet();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests[0].language,'French');assert.equal(requests[0].identity_id,'french-two');
});

// Waiting on a model: how many seconds have passed, how many variants to make, and how to stop.
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
const okResponse = () => Promise.resolve({ok: true, blob: async () => ({})});

function performFixture({variants = '1', seeded = true, fetch} = {}) {
  const fields = {};
  const field = id => fields[id] || (fields[id] = {value: '', checked: false, disabled: false, hidden: false, textContent: ''});
  field('variantCount').value = variants;
  field('performSeed').value = '42';
  const calls = [];
  let clock = 0, tick = null;
  const c = vm.createContext({$: field, performanceIdentity: () => ({id: 'voice', character_id: 'c', engine_id: 'qwen3-tts-1.7b-base'}),
    performanceSettings: (identity, seed) => seeded ? {seed} : {speed: 1},
    fetch: (url, request) => { calls.push(JSON.parse(request.body)); return fetch ? fetch(calls.length) : okResponse(); },
    loadCurrentTakes: () => Promise.resolve(), performance: {now: () => clock},
    setInterval: callback => { tick = callback; return 7; }, clearInterval() { tick = null; }});
  vm.runInContext(sourceBetween('function generatePerformanceSet(', 'function loadPronunciations('), c);
  return {c, field, calls, ticking: () => tick !== null, advance(seconds) { clock += seconds * 1000; if (tick) tick(); }};
}

test('the variant count sets how many takes are made, each on the next seed', async () => {
  const fixture = performFixture({variants: '3'});
  fixture.c.generatePerformanceSet(); await settle();
  assert.deepEqual(fixture.calls.map(body => body.settings.seed), [42, 43, 44]);
  assert.match(fixture.field('performStatus').textContent, /^Takes made\. /);
  assert.equal(fixture.field('performGenerate').disabled, false);
  assert.equal(fixture.field('performStop').hidden, true);
  assert.equal(fixture.ticking(), false);
});

test('the variant count is held between one and eight, and a model without a seed makes one take', async () => {
  for (const [typed, expected] of [['99', 8], ['0', 1], ['abc', 1], ['2.9', 2], ['', 1]]) {
    const fixture = performFixture({variants: typed});
    fixture.c.generatePerformanceSet(); await settle();
    assert.equal(fixture.calls.length, expected, `typed ${JSON.stringify(typed)}`);
  }
  const unseeded = performFixture({variants: '8', seeded: false});
  unseeded.c.generatePerformanceSet(); await settle();
  assert.equal(unseeded.calls.length, 1);
});

test('Stop after this take lets the take in progress finish and skips the rest', async () => {
  let release;
  const first = new Promise(resolve => { release = resolve; });
  const fixture = performFixture({variants: '3', fetch: count => count === 1 ? first.then(okResponse) : okResponse()});
  fixture.c.generatePerformanceSet();
  assert.equal(fixture.field('performStop').hidden, false, 'a set of several takes can be stopped');
  assert.equal(fixture.field('performGenerate').disabled, true);
  fixture.c.stopPerformanceSet();
  assert.match(fixture.field('performStatus').textContent, /stopping after this take/);
  release(); await settle();
  assert.equal(fixture.calls.length, 1);
  assert.match(fixture.field('performStatus').textContent, /Stopped after 1 of 3 takes\. The takes already made are saved\./);
  assert.equal(fixture.field('performGenerate').disabled, false);
  assert.equal(fixture.field('performStop').hidden, true);
  assert.equal(fixture.ticking(), false);
  fixture.c.stopPerformanceSet();  // nothing is running now, so this does nothing
});

test('while a take is made the status counts the seconds, and a single take offers no stop', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const fixture = performFixture({variants: '1', fetch: () => pending.then(okResponse)});
  fixture.c.generatePerformanceSet();
  assert.equal(fixture.field('performStop').hidden, true);
  assert.match(fixture.field('performStatus').textContent, /Making take 1 of 1\.\.\. 0 s$/);
  fixture.advance(12);
  assert.match(fixture.field('performStatus').textContent, /Making take 1 of 1\.\.\. 12 s$/);
  release(); await settle();
  assert.match(fixture.field('performStatus').textContent, /^Takes made\. /);
  assert.equal(fixture.ticking(), false);
});

function generateFixture(fetch, requestBody = {engine: 'kokoro', text: 'hello'}) {
  const fields = {};
  const field = id => fields[id] || (fields[id] = {value: '', checked: false, disabled: false, hidden: false, textContent: ''});
  let clock = 0, tick = null;
  const history = [];
  const c = vm.createContext({$: field, bodyForRequest: () => ({...requestBody}),
    VoiceLabWorkflows: {freezeRequest: body => body}, AbortController, fetch, performance: {now: () => clock},
    setInterval: callback => { tick = callback; return 7; }, clearInterval() { tick = null; },
    makeHistory: (blob, body, meta) => history.push(meta), persistHistory: () => Promise.resolve(), loadReferences() {}});
  vm.runInContext(sourceBetween('var activeRequest = null;', 'function wavHeader('), c);
  return {c, field, history, ticking: () => tick !== null, advance(seconds) { clock += seconds * 1000; if (tick) tick(); }};
}

test('Generate counts the seconds waited and Stop waiting abandons the request without an error', async () => {
  const fixture = generateFixture((url, request) => new Promise((resolve, reject) => {
    request.signal.addEventListener('abort', () => reject(Object.assign(new Error('The operation was aborted.'), {name: 'AbortError'})));
  }));
  fixture.c.generate();
  assert.equal(fixture.field('go').disabled, true);
  assert.equal(fixture.field('stopWaiting').hidden, false);
  assert.match(fixture.field('status').textContent, /^Generating\.\.\. 0 s\. The first request loads and warms the selected model\.$/);
  fixture.advance(12);
  assert.match(fixture.field('status').textContent, /^Generating\.\.\. 12 s\./);
  fixture.c.stopWaiting(); await settle();
  assert.match(fixture.field('status').textContent, /^Stopped waiting\./);
  assert.equal(fixture.field('err').textContent, '');
  assert.equal(fixture.field('go').disabled, false);
  assert.equal(fixture.field('reroll').disabled, false);
  assert.equal(fixture.field('stopWaiting').hidden, true);
  assert.equal(fixture.ticking(), false);
  fixture.c.stopWaiting();  // nothing is running now, so this does nothing
});

test('stopping a request that asked to save a reusable voice says the voice is still saved', async () => {
  const fixture = generateFixture((url, request) => new Promise((resolve, reject) => {
    request.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), {name: 'AbortError'})));
  }), {engine: 'breeze-tts-2', mode: 'design', text: 'hello', save_voice_as: 'my_voice'});
  fixture.c.generate(); fixture.c.stopWaiting(); await settle();
  assert.match(fixture.field('status').textContent, /Stopped waiting\..*The reusable voice is still saved when it finishes\.$/);
});

test('a finished Generate keeps its result: the clock stops and the controls come back', async () => {
  const fixture = generateFixture(() => Promise.resolve({ok: true, headers: {get: name => name === 'X-Audio-Seconds' ? '1.50' : name === 'X-Gen-Seconds' ? '0.40' : null},
    blob: async () => ({})}));
  fixture.c.generate(); await settle();
  assert.deepEqual(fixture.history, ['1.50 s audio / 0.40 s generation']);
  const shown = fixture.field('status').textContent;
  assert.match(shown, /1\.50 s audio \/ 0\.40 s generation \/ round trip 0\.0 s/);
  fixture.advance(30);
  assert.equal(fixture.field('status').textContent, shown, 'a stopped clock cannot overwrite the result');
  assert.equal(fixture.field('go').disabled, false);
  assert.equal(fixture.field('stopWaiting').hidden, true);
});

test('a failed Generate shows its error and not the stopped-waiting message', async () => {
  const fixture = generateFixture(() => Promise.reject(new Error('worker unavailable')));
  fixture.c.generate(); await settle();
  assert.equal(fixture.field('err').textContent, 'worker unavailable');
  assert.equal(fixture.field('status').textContent, '');
  assert.equal(fixture.field('go').disabled, false);
  assert.equal(fixture.ticking(), false);
});

// A page that was open while Kokoro was installed has an empty voice picker. Generate reads the voices again, once, then speaks.
const spokenResponse = () => Promise.resolve({ok: true, headers: {get: () => '1'}, blob: async () => ({})});
function openedBeforeTheInstall(fixture, {engine = 'kokoro', installed = ['af_heart', 'bf_emma'], refresh} = {}) {
  const voice = fixture.field('voice'), refreshed = [];
  fixture.field('engine').value = engine;
  voice.options = [];
  fixture.c.bodyForRequest = () => ({engine, text: 'hello', voice: voice.options.filter(item => item.selected).map(item => item.value).join(',')});
  fixture.c.VoiceLabWorkflows.refreshKokoroVoices = refresh || (async () => {
    refreshed.push(true);
    voice.options = installed.map((value, index) => ({value, selected: index === 0}));
    return voice.options.length;
  });
  return {voice, refreshed};
}

test('Generate on a page opened before Kokoro was installed reads the voices again, then speaks with the first one', async () => {
  const sent = [];
  const fixture = generateFixture((url, request) => { sent.push(JSON.parse(request.body)); return spokenResponse(); });
  const {refreshed} = openedBeforeTheInstall(fixture);
  fixture.c.generate();
  assert.equal(fixture.field('go').disabled, true);
  assert.equal(fixture.field('reroll').disabled, true);
  assert.equal(fixture.field('status').textContent, 'Reading the installed Kokoro voices...');
  assert.deepEqual(sent, [], 'nothing is sent while the voices are read');
  await settle();
  assert.equal(refreshed.length, 1);
  assert.deepEqual(sent.map(body => body.voice), ['af_heart']);
  assert.equal(fixture.field('err').textContent, '');
  assert.equal(fixture.field('go').disabled, false);
  assert.equal(fixture.field('reroll').disabled, false);
  assert.equal(fixture.history.length, 1);
});

test('Generate does not read the Kokoro voices again when the picker has them', async () => {
  const sent = [];
  const fixture = generateFixture((url, request) => { sent.push(JSON.parse(request.body)); return spokenResponse(); });
  const {voice, refreshed} = openedBeforeTheInstall(fixture);
  voice.options = [{value: 'bf_emma', selected: true}];
  fixture.c.generate();
  assert.equal(sent.length, 1, 'the request goes out at once');
  await settle();
  assert.deepEqual(refreshed, []);
  assert.deepEqual(sent.map(body => body.voice), ['bf_emma']);
});

test('Generate for another engine never reads the Kokoro voices', async () => {
  for (const engine of ['turbo', 'qwen3-tts-1.7b-voice-design', 'breeze-tts-2']) {
    const sent = [];
    const fixture = generateFixture((url, request) => { sent.push(JSON.parse(request.body)); return spokenResponse(); });
    const {refreshed} = openedBeforeTheInstall(fixture, {engine});
    fixture.c.generate(); await settle();
    assert.deepEqual(refreshed, [], engine);
    assert.equal(sent.length, 1, engine);
  }
});

test('when no voices turn up, Generate asks once and shows what the service says; pressing it again reads once more', async () => {
  const sent = [];
  const notInstalled = 'Kokoro 82M is not installed. Open Models & features and press Install on its card, or run setup-kokoro.bat in the lab folder, then try again.';
  const fixture = generateFixture((url, request) => { sent.push(JSON.parse(request.body)); return Promise.reject(new Error(notInstalled)); });
  const {refreshed} = openedBeforeTheInstall(fixture, {installed: []});
  fixture.c.generate(); await settle();
  assert.equal(refreshed.length, 1);
  assert.deepEqual(sent.map(body => body.voice), ['']);
  assert.equal(fixture.field('err').textContent, notInstalled);
  assert.equal(fixture.field('go').disabled, false);
  fixture.c.generate(); await settle();
  assert.equal(refreshed.length, 2);
  assert.equal(sent.length, 2);
});

test('a voice list that cannot be read does not stop Generate from asking the service', async () => {
  const sent = [];
  const fixture = generateFixture((url, request) => { sent.push(JSON.parse(request.body)); return spokenResponse(); });
  openedBeforeTheInstall(fixture, {refresh: () => Promise.reject(new Error('service stopped'))});
  fixture.c.generate(); await settle();
  assert.equal(sent.length, 1);
  assert.equal(fixture.field('go').disabled, false);
  assert.equal(fixture.field('reroll').disabled, false);
  assert.equal(fixture.field('err').textContent, '');
});

test('the Generate buttons come back when speaking does not start after the voices were read', async () => {
  const fixture = generateFixture(() => spokenResponse());
  openedBeforeTheInstall(fixture);
  fixture.c.bodyForRequest = () => ({engine: 'turbo', text: 'hello', clone: true});  // cloning, with no recording chosen
  fixture.c.generate(); await settle();
  assert.match(fixture.field('err').textContent, /^Choose a voice recording/);
  assert.equal(fixture.field('go').disabled, false);
  assert.equal(fixture.field('reroll').disabled, false);
});

test('Phonon sends its preset voice, seed, temperature and pace shift, and nothing it cannot take', () => {
  const fields={},field=id=>fields[id]||(fields[id]={value:'',checked:false});
  for(const [id,value] of Object.entries({engine:'phonon-31m-en',text:'Keep the gate closed.',phononVoice:'Zoey',phononSeed:'7',
    phononTemperature:'0.5',phononPace:'-0.2',speed:'1.2',chatterboxReference:'ref.wav',dramaDirection:'quietly'}))field(id).value=value;
  field('clone').checked=true;
  const c=vm.createContext({$:field,selectedValues:()=>['af_heart'],qwenModeFor:()=>null});
  vm.runInContext(sourceBetween('function bodyForRequest()', 'function makeHistory('),c);
  assert.deepEqual(JSON.parse(JSON.stringify(c.bodyForRequest())),
    {engine:'phonon-31m-en',text:'Keep the gate closed.',voice:'Zoey',seed:7,temperature:0.5,duration_delta:-0.2});
});

test('Generate for Phonon reads the installed voices when the picker has none, then speaks; with voices it speaks at once', async () => {
  const sent = [];
  const fixture = generateFixture((url, request) => { sent.push(JSON.parse(request.body)); return spokenResponse(); },
    {engine: 'phonon-31m-en', text: 'hello', voice: 'Harper'});
  let reads = 0;
  fixture.field('engine').value = 'phonon-31m-en';
  fixture.c.phononVoices = [];
  fixture.c.refreshPhononVoices = async () => { reads += 1; fixture.c.phononVoices = [{name: 'Harper'}]; return 1; };
  fixture.c.generate();
  assert.equal(fixture.field('status').textContent, 'Reading the installed Phonon voices...');
  assert.equal(fixture.field('go').disabled, true);
  assert.equal(fixture.field('reroll').disabled, true);
  assert.deepEqual(sent, [], 'nothing is sent while the voices are read');
  await settle();
  assert.equal(reads, 1);
  assert.deepEqual(sent.map(body => body.voice), ['Harper']);
  assert.equal(fixture.field('go').disabled, false);
  fixture.c.generate();
  assert.equal(sent.length, 2, 'the request goes out at once');
  await settle();
  assert.equal(reads, 1, 'a full picker is not read again');
});

test('a Phonon voice list that cannot be read does not stop Generate from asking the service', async () => {
  const sent = [];
  const fixture = generateFixture((url, request) => { sent.push(JSON.parse(request.body)); return spokenResponse(); },
    {engine: 'phonon-31m-en', text: 'hello', voice: ''});
  fixture.field('engine').value = 'phonon-31m-en';
  fixture.c.phononVoices = [];
  fixture.c.refreshPhononVoices = () => Promise.reject(new Error('service stopped'));
  fixture.c.generate(); await settle();
  assert.equal(sent.length, 1);
  assert.equal(fixture.field('go').disabled, false);
  assert.equal(fixture.field('err').textContent, '');
});

test('the Phonon pickers group the installed voices, keep a choice that is still there and fall back to Harper', async () => {
  const makeElement = () => {
    const element = {children: [], value: '', title: '', label: '', appendChild(child) { this.children.push(child); return child; }};
    let text = '';
    Object.defineProperty(element, 'textContent', {get: () => text, set(value) { text = value; if (value === '') element.children = []; }});
    return element;
  };
  const picks = {phononVoice: makeElement(), phononIdentityVoice: makeElement(), phononVoiceInfo: makeElement(), phononIdentityInfo: makeElement()};
  const voices = [
    {name: 'Delia', group: 'American English', gender: 'female', age: 'Middle-age', character: false, description: 'Warm.'},
    {name: 'Harper', group: 'American English', gender: 'female', age: 'Adult', character: false, description: 'Modern and friendly.'},
    {name: 'Alfie', group: 'British English', gender: 'male', age: 'Young Adult', character: false, description: 'Bright.'},
    {name: 'Azgorath', group: 'Characters', gender: null, age: null, character: true, description: 'An ancient demon.'}];
  const c = vm.createContext({$: id => picks[id], document: {createElement: makeElement}, phononVoices: [],
    fetchJson: async url => { assert.equal(url, '/api/info'); return {phonon_voices: voices}; }});
  vm.runInContext(sourceBetween('function phononVoiceLabel(', 'function openHistoryDb('), c);
  assert.equal(await c.refreshPhononVoices(), 4);
  const picker = picks.phononVoice;
  assert.deepEqual(picker.children.map(group => [group.label, group.children.map(item => item.textContent)]), [
    ['American English', ['Delia (female, middle-age)', 'Harper (female, adult)']],
    ['British English', ['Alfie (male, young adult)']], ['Characters', ['Azgorath (character)']]]);
  assert.equal(picker.value, 'Harper', 'the default voice');
  assert.equal(picks.phononIdentityVoice.children.length, 3, 'the Cast picker is filled from the same list');
  assert.equal(picks.phononVoiceInfo.textContent, 'American English: Modern and friendly.');
  picker.value = 'Azgorath'; c.fillPhononVoices(voices);
  assert.equal(picker.value, 'Azgorath', 'a choice that is still installed is kept');
  assert.equal(picks.phononVoiceInfo.textContent, 'Characters: An ancient demon.');
  picker.value = 'Gone'; c.fillPhononVoices(voices.slice(2));
  assert.equal(picker.value, 'Alfie', 'without Harper the first voice is the default');
  c.fillPhononVoices([]);
  assert.equal(picker.value, '');
  assert.equal(picker.children.length, 0);
  assert.equal(picks.phononVoiceInfo.textContent, 'No Phonon voices are installed. Press Install on its card in Models & features.');
  assert.equal(picks.phononIdentityInfo.textContent, picks.phononVoiceInfo.textContent);
});

test('the page has the Phonon panel, a Phonon Cast identity row and the wiring that uses them', () => {
  for (const id of ['phononPanel', 'phononVoice', 'phononSeed', 'phononTemperature', 'phononPace', 'phononIdentityRow', 'phononIdentityVoice']) {
    assert.ok(source.includes(`id="${id}"`), `${id} is on the page`);
  }
  assert.match(source, /body\.speaker=\$\('phononIdentityVoice'\)\.value;body\.language='English'/);
  assert.match(source, /\$\('phononPanel'\)\.hidden = !phonon/);
  assert.match(source, /\$\('phononIdentityRow'\)\.hidden/);
});

test('the page offers the stop controls, the variant count, and the version', () => {
  for (const id of ['stopWaiting', 'performStop', 'variantCount', 'appVersion']) {
    assert.ok(source.includes(`id="${id}"`), `${id} is on the page`);
  }
  assert.ok(!source.includes('sweepThree'), 'the old three-variant checkbox is gone');
  assert.match(source, /\$\('stopWaiting'\)\.addEventListener\('click', stopWaiting\)/);
  assert.match(source, /\$\('performStop'\)\.addEventListener\('click',stopPerformanceSet\)/);
});

// Take loudness: LUFS, sample peak and true peak beside each take, in plain words.
function loudnessLine(loudness) {
  const c = vm.createContext({document: {createElement() { return {children: [], className: '', textContent: '', appendChild(child) { this.children.push(child); }}; }}});
  vm.runInContext(sourceBetween('var takeCheckLabels=', 'function renderTakes('), c);
  const parent = {children: [], appendChild(child) { this.children.push(child); }};
  c.appendTakeLoudness(parent, loudness);
  assert.equal(parent.children.length, 1);
  assert.equal(parent.children[0].className, 'meta take-checks');
  return parent.children[0].textContent;
}

test('a take shows its loudness, sample peak and true peak', () => {
  assert.equal(loudnessLine({status: 'measured', integrated_lufs: -23.04, sample_peak_dbfs: -3.1, true_peak_dbtp: -2.84, note: null}),
    'Loudness -23.0 LUFS · sample peak -3.1 dBFS · true peak -2.8 dBTP.');
  assert.equal(loudnessLine({status: 'measured', integrated_lufs: -17, sample_peak_dbfs: -0.5, true_peak_dbtp: -0.5, note: null}),
    'Loudness -17.0 LUFS · sample peak -0.5 dBFS · true peak -0.5 dBTP. The true peak is above -1 dBTP, so MP3, AAC or Ogg export can clip.');
  assert.doesNotMatch(loudnessLine({status: 'measured', integrated_lufs: -20, sample_peak_dbfs: -3, true_peak_dbtp: -1, note: null}), /can clip/,
    'exactly -1 dBTP is on the line, not over it');
});

test('a take with no loudness figure says why and still shows the peaks it has', () => {
  assert.equal(loudnessLine({status: 'measured', integrated_lufs: null, sample_peak_dbfs: -3, true_peak_dbtp: -2.5, note: 'too_short'}),
    'No loudness reading (under 0.4 s is too short) · sample peak -3.0 dBFS · true peak -2.5 dBTP.');
  assert.equal(loudnessLine({status: 'measured', integrated_lufs: null, sample_peak_dbfs: null, true_peak_dbtp: null, note: 'silent'}),
    'No loudness reading (the take is silent).');
  assert.equal(loudnessLine({status: 'measured', integrated_lufs: null, sample_peak_dbfs: -110, true_peak_dbtp: -110, note: 'too_quiet'}),
    'No loudness reading (too quiet) · sample peak -110.0 dBFS · true peak -110.0 dBTP.');
  assert.equal(loudnessLine({status: 'measured', integrated_lufs: null, sample_peak_dbfs: null, true_peak_dbtp: null, note: 'new_reason'}),
    'No loudness reading.');
});

test('a take not measured yet, or one that could not be, is said plainly', () => {
  assert.equal(loudnessLine({status: 'not_measured', meter: null}), 'Loudness has not been measured for this take yet.');
  assert.equal(loudnessLine(undefined), 'Loudness has not been measured for this take yet.');
  assert.equal(loudnessLine({status: 'unavailable', reason: 'Local cast media file is missing.'}),
    'Loudness could not be measured: Local cast media file is missing..');
  assert.equal(loudnessLine({status: 'unavailable'}), 'Loudness could not be measured: unknown error.');
});

test('Perform and Scenes take cards both carry the loudness line', () => {
  assert.match(compact, /appendTakeLoudness\(article,take\.loudness\)/);
  assert.match(compact, /appendTakeLoudness\(article,row\.loudness\)/);
});

// The export panel offers only what this PC's FFmpeg can write.
// The FFmpeg install button and its progress line, shared by the System page and the export panel (voice_lab_system.js).
const FFMPEG_INSTALL_SOURCE = () => sourceBetween('// Install FFmpeg: the button here', 'function refreshSystemPage(');
function exportPanel(report, {job = null} = {}) {
  const make = (id, extra = {}) => ({id, disabled: false, checked: false, hidden: false, textContent: '', className: '', ...extra});
  const note = make('exportFfmpeg'), buttons = ['exportPreview', 'exportCreate', 'exportVerify'].map(id => make(id));
  const install = make('exportFfmpegInstall', {hidden: true}), progress = make('exportFfmpegProgress', {hidden: true});
  const others = ['systemFfmpegInstall', 'systemFfmpegProgress'].map(id => make(id));
  const boxes = ['wav', 'flac', 'ogg', 'mp3', 'm4a'].map(value => make('box-' + value, {value, checked: value === 'wav'}));
  const byId = Object.fromEntries([note, install, progress, ...others, ...buttons].map(item => [item.id, item]));
  const refreshed = [];
  const c = vm.createContext({$: id => byId[id], document: {querySelectorAll: () => boxes}, setTimeout: () => 1, clearTimeout() {},
    refreshSystem: again => refreshed.push(['system', again]), refreshExports: (scene, again) => refreshed.push(['exports', again])});
  vm.runInContext(sourceBetween('function applyExportFfmpeg(', 'function refreshExports(') + FFMPEG_INSTALL_SOURCE(), c);
  if (job) c.ffmpegJob = job;
  c.applyExportFfmpeg(report);
  return {note, buttons, install, progress, refreshed, context: c, boxes: Object.fromEntries(boxes.map(box => [box.value, box]))};
}
const everyFormat = {wav: true, flac: true, ogg: true, mp3: true, m4a: true};

test('with FFmpeg ready and every encoder present, nothing is switched off', () => {
  const panel = exportPanel({ready: true, found: true, version: '8.1.1', formats: everyFormat, message: null});
  assert.equal(panel.note.textContent, 'FFmpeg 8.1.1 found; every format is available.');
  assert.equal(panel.note.className, 'meta');
  assert.ok(panel.buttons.every(button => !button.disabled));
  assert.ok(Object.values(panel.boxes).every(box => !box.disabled));
  assert.equal(panel.boxes.wav.checked, true);
});

test('without FFmpeg the panel says so and nothing can be written or checked', () => {
  const message = 'FFmpeg was not found on this PC. Install FFmpeg (for example, in a terminal: winget install Gyan.FFmpeg) and try again.';
  const panel = exportPanel({ready: false, found: false, version: null, formats: Object.fromEntries(Object.keys(everyFormat).map(key => [key, false])), message});
  assert.equal(panel.note.textContent, message + ' Refresh list checks again.');
  assert.equal(panel.note.className, 'meta take-checks flagged');
  assert.ok(panel.buttons.every(button => button.disabled), 'preview, create and verify all need FFmpeg');
  assert.ok(Object.values(panel.boxes).every(box => box.disabled && !box.checked));
});

test('a build without an MP3 encoder switches off only MP3 and keeps the other choices', () => {
  const message = 'This FFmpeg build cannot write MP3, so that choice is off. Install a full FFmpeg build to turn it on.';
  const panel = exportPanel({ready: true, found: true, version: '7.0', formats: {...everyFormat, mp3: false}, message});
  assert.equal(panel.note.textContent, message);
  assert.equal(panel.note.className, 'meta take-checks flagged');
  assert.ok(panel.buttons.every(button => !button.disabled));
  assert.deepEqual(Object.entries(panel.boxes).filter(([, box]) => box.disabled).map(([name]) => name), ['mp3']);
});

const OFFER = {version: '9.0.2', builder: 'Gyan Doshi (www.gyan.dev), essentials build', license: 'GPL-3.0',
  download_bytes: 114768076, disk_bytes: 210757726};

test('Install FFmpeg is offered with its size while no FFmpeg writes every format, and hidden once one does', () => {
  const none = Object.fromEntries(Object.keys(everyFormat).map(key => [key, false]));
  const offered = exportPanel({ready: false, found: false, version: null, formats: none, message: 'FFmpeg was not found.', offer: OFFER});
  assert.equal(offered.install.hidden, false);
  assert.equal(offered.install.disabled, false);
  assert.equal(offered.install.textContent, 'Install FFmpeg');
  assert.equal(offered.progress.hidden, false);
  assert.equal(offered.progress.textContent, "Install FFmpeg puts FFmpeg 9.0.2 in the Lab's own folder: Gyan Doshi (www.gyan.dev), "
    + 'essentials build, licence GPL-3.0. It downloads 115 MB and uses 211 MB of disk.');
  const ready = exportPanel({ready: true, found: true, version: '9.0.2', formats: everyFormat, message: null, offer: null, own: true});
  assert.equal(ready.install.hidden, true);
  assert.equal(ready.progress.hidden, true);
});

test('while FFmpeg installs every Install FFmpeg button is busy and says what is happening and why it cannot stop', () => {
  const none = Object.fromEntries(Object.keys(everyFormat).map(key => [key, false]));
  const job = {engine: 'ffmpeg', name: 'FFmpeg', state: 'running', lines: ['Downloading FFmpeg 9.0.2 (109.5 MB):', '      40% (45.9 MB of 114.8 MB)']};
  const panel = exportPanel({ready: false, found: false, formats: none, message: 'FFmpeg was not found.', offer: OFFER}, {job});
  assert.equal(panel.install.disabled, true);
  assert.equal(panel.install.textContent, 'Installing FFmpeg…');
  assert.equal(panel.progress.textContent, 'Installing FFmpeg. 40% (45.9 MB of 114.8 MB) It cannot be stopped from here: closing the Lab '
    + 'stops it, and Install FFmpeg then continues where it stopped.');
  panel.context.renderFfmpegJob({...job, state: 'failed', lines: ['Downloading', 'error: The download stopped (no route). If this '
    + 'computer is offline, connect it first. What was downloaded is kept, and the next try continues from there.']});
  assert.equal(panel.install.disabled, false);
  assert.deepEqual(panel.refreshed, [['system', true], ['exports', true]], 'FFmpeg is asked again once the install ends');
  assert.equal(panel.progress.className, 'meta take-checks flagged');
  assert.equal(panel.progress.textContent, 'FFmpeg was not installed: The download stopped (no route). If this computer is offline, '
    + 'connect it first. What was downloaded is kept, and the next try continues from there. Press Install FFmpeg to try again.');
});

test('a format switched off is also unchecked, and WAV is checked again when nothing else is', () => {
  const c = {ready: true, found: true, version: '8', formats: {...everyFormat, wav: false}, message: null};
  const panel = exportPanel(c);
  assert.equal(panel.boxes.wav.checked, false);
  assert.equal(panel.boxes.wav.disabled, true);
  const back = exportPanel({ready: true, found: true, version: '8', formats: everyFormat, message: null});
  assert.equal(back.boxes.wav.checked, true, 'the default format is checked when nothing is');
  assert.equal(exportPanel(undefined).note.textContent, '');
});

test('the page offers MP3 and AAC export, M4A and MP3 import, and the FFmpeg note', () => {
  for (const id of ['exportFfmpeg']) assert.ok(source.includes(`id="${id}"`));
  assert.match(source, /name="exportFormat" value="mp3"/);
  assert.match(source, /name="exportFormat" value="m4a"/);
  assert.equal((source.match(/accept="\.wav,\.flac,\.ogg,\.mp3,\.m4a,audio\/wav,audio\/flac,audio\/ogg,audio\/mpeg,audio\/mp4"/g) || []).length, 4,
    'the clone upload, the Cast import and the two Voice conversion pickers take the same files');
  assert.match(source, /\/\\\.\(wav\|flac\|ogg\|mp3\|m4a\)\$\/i\.test\(file\.name\)/);
  assert.match(source, /refreshExports\(undefined,true\)/);
});

// The System workspace: what this PC is running, FFmpeg, disk, storage and the checked library backup.
function systemPanel({answers = {}, confirm = true, navigator = {}, copyAllowed = true} = {}) {
  const nodes = {}, calls = [], timers = [];
  const make = (id, tagName) => {
    const node = {id, tagName, children: [], className: '', disabled: false, hidden: false, dataset: {}, value: '', checked: false, _text: '',
      appendChild(child) { this.children.push(child); return child; }, addEventListener(type, handler) { this[type + 'Handler'] = handler; },
      setAttribute() {}, focus() { this.focused = true; }, select() {}};
    Object.defineProperty(node, 'textContent', {get() { return this._text; }, set(value) { this._text = value; this.children = []; }});
    return node;
  };
  ['systemFacts', 'systemFfmpeg', 'systemFfmpegInstall', 'systemFfmpegProgress', 'exportFfmpegInstall', 'exportFfmpegProgress', 'systemDisk', 'systemModels', 'systemDiagnostics', 'systemCopy', 'systemCopied', 'systemError',
    'systemStatus', 'systemMeasure', 'systemClean', 'systemStorage', 'systemCleanup', 'backupFolder', 'backupSections', 'backupSize',
    'backupProgress', 'backupStatus', 'backupList', 'backupCreate', 'restoreBox', 'restoreName', 'restoreFolder', 'restoreGo',
    'restoreUse', 'restoreHint', 'restoreCommand'].forEach(id => { nodes[id] = make(id, 'div'); });
  const inputs = () => nodes.backupSections.children.flatMap(label => label.children).filter(item => item.tagName === 'input');
  const context = vm.createContext({
    $: id => nodes[id], navigator,
    document: {createElement: tag => make(null, tag), createTextNode: text => ({textContent: text}), execCommand: () => copyAllowed,
      querySelector: () => inputs()[0] || null, querySelectorAll: () => inputs(), body: {appendChild() {}, removeChild() {}}},
    window: {confirm: message => { calls.push(['confirm', message]); return confirm; }},
    setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; }, clearTimeout() {},
    fetchJson: (url, body) => {
      calls.push([url, body === undefined ? undefined : JSON.parse(JSON.stringify(body))]);  // plain objects of this realm, so deepEqual can compare them
      const answer = typeof answers[url] === 'function' ? answers[url]() : answers[url];
      return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
    }});
  vm.runInContext(sourceBetween('var systemTaskTimer=', "$('systemRefresh').addEventListener"), context);
  return {nodes, calls, timers, context, inputs, posted: url => calls.filter(call => call[0] === url)};
}
const GB = 1024 ** 3;
const systemReport = (extra = {}) => ({
  app: {name: "Fel's Voice Lab", version: '0.3.0'}, system: {python: '3.12.4', platform: 'Windows-11', device: 'cuda', free_ram_gb: 21.5},
  library: {path: 'D:\\VoiceLab', schemas: {evaluations: 2, scenes: 1, cast: 8},
    counts: {characters: 3, identities: 5, references: 4, takes: 120, scenes: 2}},
  ffmpeg: {found: true, ready: true, version: '7.1', formats: {wav: true, flac: true, ogg: true, mp3: true, m4a: true}, message: null},
  disk: {library: {free_bytes: 200 * GB, total_bytes: 500 * GB, low: false}, install: {free_bytes: 2 * GB, total_bytes: 100 * GB, low: true}},
  engines: {device: 'cuda', counts: {usable: 2, not_checked: 1, check_outdated: 0, check_failed: 0, not_installed: 3, conversion_task: 1}, rows: []},
  log: {file: 'D:\\lab\\out\\logs\\service-1.log', folder: 'D:\\lab\\out\\logs', bytes: 1234},
  backups: {folder: 'D:\\VoiceLab\\backups', count: 0, latest: null}, diagnostics: "Fel's Voice Lab 0.3.0 diagnostics", ...extra});

test('sizes read in the largest unit that keeps the number above one', () => {
  const {context} = systemPanel();
  const cases = [[0, '0 bytes'], [1023, '1023 bytes'], [1024, '1.0 KB'], [1536, '1.5 KB'], [5 * GB, '5.0 GB'], [3 * 1024 ** 4, '3.0 TB'],
    [5000 * 1024 ** 4, '5000.0 TB'], [null, 'unknown'], [undefined, 'unknown']];
  for (const [count, words] of cases) assert.equal(context.formatBytes(count), words, String(count));
});

test('the installation facts name the version, the machine, the library and what is in it', () => {
  const {context} = systemPanel();
  assert.deepEqual(JSON.parse(JSON.stringify(context.systemFacts(systemReport()))), [
    ['Version', "Fel's Voice Lab 0.3.0"], ['Python and system', '3.12.4, Windows-11'], ['Device', 'cuda, 21.5 GB of memory free'],
    ['Library folder', 'D:\\VoiceLab'], ['Database schemas', 'evaluations 2, scenes 1, cast 8'],
    ['Library contents', '3 characters, 5 voices, 4 voice recordings, 120 takes, 2 scenes'],
    ['Service log', 'D:\\lab\\out\\logs\\service-1.log']]);
  const python = source => context.systemFacts(systemReport({system: {...systemReport().system, python: '3.12.10', python_source: source}}))[1][1];
  assert.equal(python("the Lab's own"), "3.12.10 (the Lab's own), Windows-11");
  assert.equal(python('C:\\Python312'), '3.12.10 (from C:\\Python312), Windows-11');
  const bare = JSON.parse(JSON.stringify(context.systemFacts(systemReport({log: null, library: {path: 'D:\\New', schemas: {}, counts: {characters: null, takes: 0}}}))));
  assert.deepEqual(bare.slice(4), [['Database schemas', 'no databases yet'], ['Library contents', '0 takes']]);
  assert.ok(!bare.some(row => row[0] === 'Service log'), 'no log, no row');
  const single = JSON.parse(JSON.stringify(context.systemFacts(systemReport({library: {path: 'D:\\One', schemas: {cast: null, scenes: 1},
    counts: {characters: 1, identities: 1, references: 1, takes: 1, scenes: 1}}}))));
  assert.deepEqual(single.slice(4), [['Database schemas', 'cast unreadable, scenes 1'],
    ['Library contents', '1 character, 1 voice, 1 voice recording, 1 take, 1 scene'], ['Service log', 'D:\\lab\\out\\logs\\service-1.log']]);
});

test('FFmpeg is described by what it can write, and what is missing is said in the server’s words', () => {
  const {context} = systemPanel();
  const ready = context.ffmpegSummary(systemReport().ffmpeg);
  assert.deepEqual({ok: ready.ok, text: ready.text}, {ok: true, text: 'FFmpeg 7.1 found. Can write: WAV yes, FLAC yes, Ogg yes, MP3 yes, AAC yes.'});
  const noMp3 = context.ffmpegSummary({found: true, ready: true, version: '7.0', message: 'This FFmpeg build cannot write MP3.',
    formats: {wav: true, flac: true, ogg: true, mp3: false, m4a: true}});
  assert.equal(noMp3.ok, false);
  assert.equal(noMp3.text, 'FFmpeg 7.0 found. Can write: WAV yes, FLAC yes, Ogg yes, MP3 no, AAC yes. This FFmpeg build cannot write MP3.');
  const noProbe = context.ffmpegSummary({found: true, ready: false, version: '7.0', formats: {}, message: 'ffprobe is missing.'});
  assert.equal(noProbe.ok, false);
  assert.match(noProbe.text, /^FFmpeg 7\.0 found, but ffprobe was not\. Can write: WAV no, FLAC no, Ogg no, MP3 no, AAC no\. ffprobe is missing\.$/);
  assert.deepEqual({...context.ffmpegSummary({found: false, message: 'FFmpeg was not found on this PC. Install it.'})}, {ok: false, text: 'FFmpeg was not found on this PC. Install it.'});
  assert.equal(context.ffmpegSummary(undefined).text, 'FFmpeg was not found on this PC.');
});

test('free space is flagged only when the server says it is low, and an unreadable drive is said so', () => {
  const {context} = systemPanel();
  assert.deepEqual({...context.diskSummary('Library drive', {free_bytes: 200 * GB, total_bytes: 500 * GB, low: false})},
    {low: false, text: 'Library drive: 200.0 GB free of 500.0 GB.'});
  assert.deepEqual({...context.diskSummary('Program drive', {free_bytes: 2 * GB, total_bytes: 100 * GB, low: true})},
    {low: true, text: 'Program drive: 2.0 GB free of 100.0 GB. That is low; a backup and new takes need room.'});
  assert.deepEqual({...context.diskSummary('Library drive', null)}, {low: false, text: 'Library drive: the free space could not be read.'});
});

test('models are summarised by state, a state the page has no words for keeps its own name, and none says none', () => {
  const {context} = systemPanel();
  const counts = {usable: 2, not_checked: 1, check_outdated: 0, check_failed: 0, not_installed: 3, conversion_task: 1, brand_new: 2};
  assert.equal(context.modelsSummary({device: 'cuda', counts}),
    '2 usable, 1 installed but not tested, 3 not installed, 1 conversion tools, 2 brand_new (device cuda).');
  assert.equal(context.modelsSummary({device: 'cpu', counts: {usable: 0}}), 'No models are integrated (device cpu).');
});

test('the report page shows the facts, flags what needs attention, and offers the copy only when there is text', () => {
  const panel = systemPanel();
  panel.context.renderSystem(systemReport());
  const {nodes} = panel;
  assert.deepEqual(nodes.systemFacts.children.map(child => [child.tagName, child.textContent]).slice(0, 4),
    [['dt', 'Version'], ['dd', "Fel's Voice Lab 0.3.0"], ['dt', 'Python and system'], ['dd', '3.12.4, Windows-11']]);
  assert.equal(nodes.systemFacts.children.length, 14);
  assert.equal(nodes.systemFfmpeg.className, 'meta');
  assert.equal(nodes.systemDisk.textContent,
    'Library drive: 200.0 GB free of 500.0 GB. Program drive: 2.0 GB free of 100.0 GB. That is low; a backup and new takes need room.');
  assert.equal(nodes.systemDisk.className, 'meta take-checks flagged');
  assert.equal(nodes.systemModels.textContent, '2 usable, 1 installed but not tested, 3 not installed, 1 conversion tools (device cuda).');
  assert.equal(nodes.systemDiagnostics.textContent, "Fel's Voice Lab 0.3.0 diagnostics");
  assert.equal(nodes.systemCopy.disabled, false);
  panel.context.renderSystem(systemReport({diagnostics: '', ffmpeg: {found: false, message: 'FFmpeg was not found on this PC.'}}));
  assert.equal(nodes.systemCopy.disabled, true);
  assert.equal(nodes.systemFfmpeg.className, 'meta take-checks flagged');
  assert.equal(nodes.systemFacts.children.length, 14, 'a refresh replaces the rows, it does not add to them');
});

test('Install FFmpeg on the System page starts the install, follows it, and asks FFmpeg again when it ends', async () => {
  const missing = {found: false, ready: false, version: null, formats: {wav: true, flac: false, ogg: false, mp3: false, m4a: false},
    message: 'FFmpeg was not found.', own: false, offer: OFFER};
  const running = {engine: 'ffmpeg', name: 'FFmpeg', kind: 'tool', state: 'running', lines: ['Unpacking FFmpeg.']};
  let job = running;
  const panel = systemPanel({answers: {'/api/system?recheck=1': systemReport({ffmpeg: {...missing, found: true, ready: true, own: true,
    offer: null, formats: {wav: true, flac: true, ogg: true, mp3: true, m4a: true}}}), '/api/ffmpeg/install': () => ({install: job})}});
  panel.context.renderFfmpegOffer('system', missing);
  const button = panel.nodes.systemFfmpegInstall, note = panel.nodes.systemFfmpegProgress;
  assert.equal(button.hidden, false);
  assert.match(note.textContent, /^Install FFmpeg puts FFmpeg 9\.0\.2 in the Lab's own folder/);
  await panel.context.startFfmpegInstall();
  assert.deepEqual(panel.posted('/api/ffmpeg/install'), [['/api/ffmpeg/install', {}]]);
  assert.equal(button.disabled, true);
  assert.match(note.textContent, /^Installing FFmpeg\. Unpacking FFmpeg\. It cannot be stopped from here/);
  assert.equal(panel.timers.at(-1).delay, 1000);
  job = {...running, state: 'finished', lines: ["FFmpeg 9.0.2 is installed in the Lab's ffmpeg folder."]};
  await panel.timers.at(-1).callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(panel.calls.some(call => call[0] === '/api/system?recheck=1'), 'the System page asks FFmpeg again');
  assert.equal(button.hidden, true, "the Lab's own FFmpeg writes every format, so nothing is offered");
  assert.equal(note.hidden, false);
  assert.equal(note.textContent, 'FFmpeg is installed.');
});

test('Check FFmpeg again asks the server to look again, and a failed read is shown, not thrown', async () => {
  const panel = systemPanel({answers: {'/api/system?recheck=1': systemReport(), '/api/system': new Error('the service is busy')}});
  await panel.context.refreshSystem(true);
  assert.deepEqual(panel.calls.map(call => call[0]), ['/api/system?recheck=1']);
  assert.equal(panel.nodes.systemError.textContent, '');
  await panel.context.refreshSystem(false);
  assert.equal(panel.nodes.systemError.textContent, 'Could not read the system report: the service is busy');
});

test('the report is copied exactly as the page shows it, with a fallback and an honest message when copying is blocked', async () => {
  const text = "Fel's Voice Lab 0.3.0 diagnostics\nSystem: Python 3.12";
  const written = [];
  const viaClipboard = systemPanel({navigator: {clipboard: {writeText: value => { written.push(value); return Promise.resolve(); }}}});
  viaClipboard.nodes.systemDiagnostics.textContent = text;
  await viaClipboard.context.copyDiagnostics();
  assert.deepEqual(written, [text]);
  assert.match(viaClipboard.nodes.systemCopied.textContent, /^Copied\. It holds no scripts, names or recordings, and your home folder reads as ~\. Read it under Show the report/);
  const refused = systemPanel({navigator: {clipboard: {writeText: () => Promise.reject(new Error('denied'))}}});
  refused.nodes.systemDiagnostics.textContent = text;
  await refused.context.copyDiagnostics();
  assert.match(refused.nodes.systemCopied.textContent, /^Copied\./, 'the older copy command is tried when the clipboard refuses');
  const blocked = systemPanel({copyAllowed: false});
  blocked.nodes.systemDiagnostics.textContent = text;
  await blocked.context.copyDiagnostics();
  assert.equal(blocked.nodes.systemCopied.textContent, 'Your browser blocked copying. Open Show the report, select the text and copy it yourself.');
});

const storageReport = (extra = {}) => ({
  library: [{key: 'cast', label: 'Cast voices, references and takes', bytes: 1024, files: 4, optional: false},
    {key: 'voices', label: 'Saved voices', bytes: 0, files: 0, optional: true}],
  other: [{key: 'backups', label: 'Backups', bytes: 2048}],
  cleanup: {count: 3, bytes: 1536, by_kind: {worker: {count: 2, bytes: 1024}, desktop: {count: 0, bytes: 0}, log: {count: 1, bytes: 512}}}, ...extra});

test('storage lists each folder with its size and whether a backup holds it', () => {
  const panel = systemPanel();
  assert.deepEqual(JSON.parse(JSON.stringify(panel.context.storageRows(storageReport()))), [
    {label: 'Cast voices, references and takes', bytes: 1024, backup: 'always'}, {label: 'Saved voices', bytes: 0, backup: 'optional'},
    {label: 'Backups', bytes: 2048, backup: 'no'}]);
  panel.context.renderStorage(storageReport());
  const rows = panel.nodes.systemStorage.children.map(row => row.children.map(cell => cell.textContent));
  assert.deepEqual(rows, [['Cast voices, references and takes', '1.0 KB', 'always'], ['Saved voices', '0 bytes', 'if ticked'], ['Backups', '2.0 KB', 'no']]);
  assert.equal(panel.nodes.systemCleanup.textContent,
    'Cleanup would remove 2 worker scratch folders, 1 old service log (1.5 KB). It never touches Cast, Scenes, exports or backups.');
  assert.equal(panel.nodes.systemClean.disabled, false);
  panel.context.renderStorage(storageReport({cleanup: {count: 0, bytes: 0, by_kind: {}}}));
  assert.equal(panel.nodes.systemCleanup.textContent, 'Nothing to clean up: no stopped-worker folders, old desktop sessions or old logs are waiting.');
  assert.equal(panel.nodes.systemClean.disabled, true, 'nothing to remove, nothing to press');
  assert.equal(panel.nodes.systemStorage.children.length, 3, 'measuring again replaces the rows');
});

test('measuring the folders waits for the button, shows the result and gives the button back even when it fails', async () => {
  const panel = systemPanel({answers: {'/api/system/storage': storageReport()}});
  const measuring = panel.context.measureStorage();
  assert.equal(panel.nodes.systemMeasure.disabled, true);
  assert.equal(panel.nodes.systemStatus.textContent, 'Measuring the folders…');
  await measuring;
  assert.equal(panel.nodes.systemMeasure.disabled, false);
  assert.equal(panel.nodes.systemStatus.textContent, '');
  assert.equal(panel.nodes.systemStorage.children.length, 3);
  const failing = systemPanel({answers: {'/api/system/storage': new Error('disk went away')}});
  await failing.context.measureStorage();
  assert.equal(failing.nodes.systemError.textContent, 'Could not measure the folders: disk went away');
  assert.equal(failing.nodes.systemMeasure.disabled, false);
  assert.equal(failing.nodes.systemStatus.textContent, '');
});

test('cleanup asks first, posts nothing when declined, and reports what it removed and what it could not', async () => {
  const declined = systemPanel({confirm: false});
  await declined.context.runCleanup();
  assert.deepEqual(declined.calls.map(call => call[0]), ['confirm']);
  assert.match(declined.calls[0][1], /Cast, Scenes, exports and backups are not touched/);
  const done = systemPanel({answers: {'/api/system/cleanup': {removed: 2, bytes: 1536, errors: []}, '/api/system/storage': storageReport()}});
  await done.context.runCleanup();
  assert.deepEqual(done.calls.map(call => call[0]), ['confirm', '/api/system/cleanup', '/api/system/storage']);
  assert.deepEqual(done.calls[1][1], {});
  assert.equal(done.nodes.systemStatus.textContent, 'Removed 2 items (1.5 KB).');
  const partial = systemPanel({answers: {'/api/system/cleanup': {removed: 1, bytes: 512, errors: ['x: busy']}, '/api/system/storage': storageReport()}});
  await partial.context.runCleanup();
  assert.equal(partial.nodes.systemStatus.textContent, 'Removed 1 item (512 bytes); 1 could not be removed.');
  const refused = systemPanel({answers: {'/api/system/cleanup': new Error('no')}});
  await refused.context.runCleanup();
  assert.equal(refused.nodes.systemError.textContent, 'Cleanup did not finish: no');
});

const running = {status: 'running', kind: 'backup', label: 'Backing up the library', files_done: 3, files_total: 10, bytes_done: 1024, bytes_total: 4096};

test('a running job reports its percentage, and a job with nothing to count has none', () => {
  const {context} = systemPanel();
  assert.equal(context.taskPercent(running), 25);
  assert.equal(context.taskPercent({...running, bytes_done: 9999}), 100);
  assert.equal(context.taskPercent({...running, bytes_total: 0}), null);
  assert.equal(context.taskPercent({status: 'done', bytes_done: 1, bytes_total: 1}), null);
  assert.equal(context.taskMessage(running), 'Backing up the library: 25% (3 of 10 files, 1.0 KB of 4.0 KB).');
  assert.equal(context.taskMessage({...running, bytes_total: 0}), 'Backing up the library: 0% (3 of 10 files, 1.0 KB of 0 bytes).');
});

test('a finished job says what it did in the server’s numbers, and a failed one says why', () => {
  const {context} = systemPanel();
  const done = (kind, result) => context.taskMessage({status: 'done', kind, result});
  assert.equal(done('backup', {name: 'voice-lab-library-1.zip', bytes: 2048, files: 12, warnings: ['2 items were left out.']}),
    'Backup saved as voice-lab-library-1.zip (2.0 KB, 12 files). It was read back and every file matched its checksum. 2 items were left out.');
  assert.equal(done('check', {ok: true, name: 'b.zip', problems: []}),
    'b.zip passed: every file matches its checksum and each database passes SQLite’s integrity check.');
  assert.equal(done('check', {ok: false, name: 'b.zip', problems: ['a.wav does not match its checksum.', 'c.json is missing.']}),
    'b.zip did not pass. a.wav does not match its checksum. c.json is missing.');
  assert.equal(done('restore', {state_root: 'D:\\lab\\restored\\r1', files: 9, counts: {takes: 4}, warnings: []}),
    'Restored to D:\\lab\\restored\\r1 (9 files, 4 takes). Every file matched its checksum and the databases opened. Your current library was not touched.');
  assert.equal(context.taskMessage({status: 'failed', error: 'There is not enough free space for the backup.'}), 'There is not enough free space for the backup.');
  assert.equal(context.taskMessage({status: 'idle'}), '');
});

test('the job area follows the job: progress while it runs, buttons back after, a flag for failure', () => {
  const panel = systemPanel();
  const {nodes, context} = panel;
  context.renderTask(running);
  assert.equal(nodes.backupProgress.hidden, false);
  assert.equal(nodes.backupProgress.value, 25);
  assert.deepEqual([nodes.backupCreate.disabled, nodes.restoreGo.disabled], [true, true]);
  assert.equal(nodes.backupStatus.className, 'meta');
  context.renderTask({status: 'failed', kind: 'backup', error: 'Out of room.'});
  assert.equal(nodes.backupProgress.hidden, true);
  assert.deepEqual([nodes.backupCreate.disabled, nodes.restoreGo.disabled], [false, false]);
  assert.equal(nodes.backupStatus.textContent, 'Out of room.');
  assert.equal(nodes.backupStatus.className, 'meta take-checks flagged');
  context.renderTask({status: 'done', kind: 'check', result: {ok: false, name: 'b.zip', problems: ['x']}});
  assert.equal(nodes.backupStatus.className, 'meta take-checks flagged');
  context.renderTask({status: 'done', kind: 'check', result: {ok: true, name: 'b.zip', problems: []}});
  assert.equal(nodes.backupStatus.className, 'meta');
  assert.deepEqual([nodes.restoreUse.hidden, nodes.restoreHint.hidden], [true, true]);
  context.renderTask({status: 'done', kind: 'restore', result: {folder: 'r1', state_root: 'D:\\lab\\restored\\r1', files: 1, counts: {takes: 0}}});
  assert.deepEqual([nodes.restoreUse.hidden, nodes.restoreHint.hidden], [false, false]);
  assert.equal(nodes.restoreUse.dataset.folder, 'r1', 'use-library is asked for the folder name, which is all the server accepts');
  assert.equal(nodes.restoreCommand.textContent, '.venv\\Scripts\\python.exe serve.py --state-root "D:\\lab\\restored\\r1"');
  context.renderTask({status: 'done', kind: 'backup', result: {name: 'x.zip', bytes: 1, files: 1}});
  assert.deepEqual([nodes.restoreUse.hidden, nodes.restoreHint.hidden], [true, true], 'the restored-library choice goes away with the restore');
});

const goodBackup = {name: 'voice-lab-library-20261025-120000.zip', bytes: 4096, modified: '2026-10-25T12:00:30+00:00', created_at: '2026-10-25T12:00:00+00:00',
  app_version: '0.3.0', files: 12, skipped: 0, missing_media: 0,
  sections: {cast: {label: 'Cast voices, references and takes', files: 8, bytes: 3000}, voices: {label: 'Saved voices', files: 4, bytes: 500}}};
const damagedBackup = {name: 'voice-lab-library-20261024-090000.zip', bytes: 10, modified: '2026-10-24T09:00:00+00:00', problem: 'This file is not a Fel’s Voice Lab library backup.'};
const backupsAnswer = (task = {status: 'idle'}, extra = {}) => ({
  folder: 'D:\\lab\\backups', suggested_folder: 'lab-restored-20261025', task, backups: [goodBackup, damagedBackup],
  plan: {sections: [{key: 'cast', label: 'Cast voices, references and takes', optional: false, included: true, files: 2, bytes: 1000},
    {key: 'voices', label: 'Saved voices', optional: true, included: true, files: 1, bytes: 500},
    {key: 'exports', label: 'Export packages', optional: true, included: true, files: 1, bytes: 2000}], files: 4, bytes: 3500}, ...extra});

test('the backup list shows each backup, what it holds, and what to do with it; a file that is not a backup can only be checked', () => {
  const panel = systemPanel();
  panel.context.renderBackups(backupsAnswer());
  const {nodes} = panel;
  assert.equal(nodes.backupFolder.textContent, 'D:\\lab\\backups');
  const [good, damaged] = nodes.backupList.children;
  assert.deepEqual(good.children.map(child => child.tagName), ['strong', 'p', 'p', 'div']);
  assert.equal(good.children[0].textContent, goodBackup.name);
  assert.match(good.children[1].textContent, /^4\.0 KB, 12 files, made .+ by version 0\.3\.0\.$/);
  assert.equal(good.children[2].textContent, 'Holds: Cast voices, references and takes, Saved voices.');
  assert.deepEqual(good.children[3].children.map(button => button.textContent), ['Check', 'Restore…']);
  assert.equal(damaged.children[1].textContent, 'This file does not read as a backup: This file is not a Fel’s Voice Lab library backup.');
  assert.deepEqual(damaged.children.at(-1).children.map(button => button.textContent), ['Check']);
  panel.context.renderBackups(backupsAnswer({status: 'done', kind: 'check', result: {ok: true, name: 'x.zip', problems: []}}, {backups: []}));
  assert.deepEqual(nodes.backupList.children.map(child => child.textContent), ['No backups yet.']);
});

test('backup rows cannot start a second job while one runs', () => {
  const panel = systemPanel();
  panel.context.renderBackups(backupsAnswer(running));
  const buttons = panel.nodes.backupList.children.flatMap(article => article.children.at(-1).children);
  assert.ok(buttons.length >= 3 && buttons.every(button => button.disabled));
  panel.context.renderBackups(backupsAnswer());
  assert.ok(panel.nodes.backupList.children.flatMap(article => article.children.at(-1).children).every(button => !button.disabled));
});

test('optional parts are ticked by default, the size follows the ticks, and only the ticked parts are asked for', async () => {
  const panel = systemPanel({answers: {'/api/system/backups': backupsAnswer()}});
  panel.context.renderBackups(backupsAnswer());
  const {nodes} = panel;
  assert.deepEqual(panel.inputs().map(input => [input.value, input.checked]), [['voices', true], ['exports', true]]);
  assert.match(nodes.backupSize.textContent, /^What is ticked holds about 3\.4 KB before compression\. The Cast, Scenes and Comparisons folders/);
  panel.inputs()[1].checked = false;
  panel.inputs()[1].changeHandler();
  assert.match(nodes.backupSize.textContent, /holds about 1\.5 KB/);
  assert.deepEqual([...panel.context.chosenBackupSections()], ['voices']);
  panel.context.renderBackups(backupsAnswer());
  assert.deepEqual(panel.inputs().map(input => input.checked), [true, false], 'a refresh keeps the choices');
  await panel.context.startBackup();
  assert.deepEqual(panel.posted('/api/system/backups')[0][1], {include: ['voices']});
  assert.equal(panel.posted('/api/system/backups').length, 2, 'the request, then the list read again');
});

test('Check and Restore send the backup’s name, and the restore form starts with an unused folder name', async () => {
  const answer = backupsAnswer();
  const panel = systemPanel({answers: {'/api/system/backups': answer, '/api/system/backups/verify': {task: running}, '/api/system/backups/restore': {task: running}}});
  panel.context.renderBackups(answer);
  const [check, restore] = panel.nodes.backupList.children[0].children[3].children;
  check.clickHandler();
  await settle();
  assert.deepEqual(panel.posted('/api/system/backups/verify'), [['/api/system/backups/verify', {name: goodBackup.name}]]);
  restore.clickHandler();
  const {restoreBox, restoreFolder, restoreName} = panel.nodes;
  assert.deepEqual([restoreBox.hidden, restoreName.textContent, restoreFolder.value, restoreBox.dataset.backup, restoreFolder.focused],
    [false, goodBackup.name, 'lab-restored-20261025', goodBackup.name, true]);
  restoreFolder.value = '  my copy  ';
  await panel.context.startRestore();
  assert.deepEqual(panel.posted('/api/system/backups/restore'), [['/api/system/backups/restore', {name: goodBackup.name, folder: 'my copy'}]]);
  assert.equal(restoreBox.hidden, true);
  restoreFolder.value = '   ';
  restoreBox.hidden = false;
  await panel.context.startRestore();
  assert.equal(panel.posted('/api/system/backups/restore').length, 1, 'no name, no request');
  assert.equal(panel.nodes.systemError.textContent, 'Type a name for the new folder.');
  assert.equal(restoreBox.hidden, false, 'the form stays open so the name can be typed');
});

test('a refused job shows the server’s reason on the page', async () => {
  const panel = systemPanel({answers: {'/api/system/backups': new Error('A backup is already running. Wait for it to finish.')}});
  await panel.context.startBackup();
  assert.equal(panel.nodes.systemError.textContent, 'A backup is already running. Wait for it to finish.');
});

test('while a job runs only its small status is polled; the full list is read again once it ends', async () => {
  const states = [running, {...running, files_done: 7, bytes_done: 3072}, {status: 'done', kind: 'backup', result: {name: 'x.zip', bytes: 5, files: 1, warnings: []}}];
  const panel = systemPanel({answers: {'/api/system/backups': () => backupsAnswer(panel.calls.filter(call => call[0] === '/api/system/backups').length === 1 ? running : states[2]),
    '/api/system/task': () => ({task: states[Math.min(panel.posted('/api/system/task').length, 2)]})}});
  await panel.context.refreshBackups();
  assert.equal(panel.timers.length, 1);
  assert.equal(panel.timers[0].delay, 1000);
  await panel.timers[0].callback();
  assert.equal(panel.timers.length, 2, 'still running: look again in a second');
  assert.equal(panel.posted('/api/system/backups').length, 1, 'the list and its library walk are not repeated while the job runs');
  assert.match(panel.nodes.backupStatus.textContent, /^Backing up the library: 75%/);
  await panel.timers[1].callback();
  assert.equal(panel.timers.length, 2, 'finished: no more polling');
  assert.equal(panel.posted('/api/system/backups').length, 2, 'the list is read once more so the new backup appears');
  assert.match(panel.nodes.backupStatus.textContent, /^Backup saved as x\.zip/);
});

test('opening a restored library asks first, names the folder the server knows, and says how to go back', async () => {
  const answers = {'/api/system/use-library': {state_root: 'D:\\lab\\restored\\r1', previous: null, settings_file: 'D:\\app\\desktop.local.json'}};
  const declined = systemPanel({confirm: false, answers});
  declined.nodes.restoreUse.dataset.folder = 'r1';
  await declined.context.useRestoredLibrary();
  assert.deepEqual(declined.calls.map(call => call[0]), ['confirm']);
  const panel = systemPanel({answers});
  panel.nodes.restoreUse.dataset.folder = 'r1';
  await panel.context.useRestoredLibrary();
  assert.deepEqual(panel.posted('/api/system/use-library'), [['/api/system/use-library', {folder: 'r1'}]]);
  assert.equal(panel.nodes.backupStatus.textContent,
    'Next start: the desktop app opens D:\\lab\\restored\\r1. Close and reopen Fel’s Voice Lab to switch. To go back, set state_root in D:\\app\\desktop.local.json to "." (or delete that line).');
  const again = systemPanel({answers: {'/api/system/use-library': {...answers['/api/system/use-library'], previous: 'D:\\old'}}});
  again.nodes.restoreUse.dataset.folder = 'r1';
  await again.context.useRestoredLibrary();
  assert.match(again.nodes.backupStatus.textContent, /to D:\\old \(or delete that line\)\.$/);
});

test('every control the System script reaches for is on the page, wired once, and the script loads before the app starts', () => {
  const fs = require('node:fs'), path = require('node:path');
  const html = fs.readFileSync(path.join(__dirname, '..', 'serve.html'), 'utf8');
  const script = fs.readFileSync(path.join(__dirname, '..', 'voice_lab_system.js'), 'utf8');
  const wanted = new Set([...script.matchAll(/\$\('([A-Za-z]+)'\)/g), ...script.matchAll(/systemNote\('([A-Za-z]+)'/g)].map(match => match[1]));
  for (const id of ['backupCreate', 'restoreGo']) wanted.add(id);
  assert.ok(wanted.size >= 25);
  for (const id of wanted) assert.equal((html.match(new RegExp(`id="${id}"`, 'g')) || []).length, 1, `${id} is on the page exactly once`);
  for (const [id, handler] of [['systemRefresh', 'function\\(\\)\\{refreshSystemPage\\(\\);\\}'], ['systemCopy', 'function\\(\\)\\{copyDiagnostics\\(\\);\\}'],
    ['systemFfmpegCheck', 'function\\(\\)\\{refreshSystem\\(true\\);\\}'], ['systemMeasure', 'measureStorage'], ['systemClean', 'runCleanup'],
    ['backupCreate', 'startBackup'], ['restoreGo', 'startRestore'], ['restoreUse', 'useRestoredLibrary']]) {
    assert.match(script, new RegExp(`\\$\\('${id}'\\)\\.addEventListener\\('click',${handler}\\)`), `${id} is wired`);
  }
  assert.match(html, /<a href="#system">System &amp; backups<\/a>/);
  assert.match(html, /<section class="card" id="system"><h2>System &amp; backups<\/h2>/);
  assert.ok(html.indexOf('/voice_lab_system.js') > html.indexOf('/voice_lab_generate.js') && html.indexOf('/voice_lab_system.js') < html.indexOf('/voice_lab_app.js'));
  assert.match(source, /if\(active==='system'\)refreshSystemPage\(\)/);
});

test('a file from this computer is uploaded and chosen in its list, and a target voice needs the permission first', async () => {
  const fields = {conversionError: {textContent: ''}, conversionStatus: {textContent: ''}, conversionSource: {value: ''},
    conversionTarget: {value: ''}, conversionTargetOrigin: {value: ''}, conversionTargetAllowedUse: {value: ''},
    conversionTargetPermission: {checked: false}};
  const calls = [];
  let answer = (role) => ({ok: true, status: 201, json: async () => ({upload: {
    path: `D:\\lab\\out\\conversions\\uploads\\${role}\\0a1b.wav`, original_name: role === 'target' ? 'voice.mp3' : 'take.wav',
    duration_seconds: role === 'target' ? 6.2 : 41.5}})});
  let refreshed = 0;
  const c = vm.createContext({$: (id) => fields[id], Promise, Error, JSON,
    encodeReferenceMetadata: (metadata) => JSON.stringify(metadata),
    fetch: async (url, options) => {
      calls.push({url, options});
      return answer(JSON.parse(options.headers['X-Reference-Metadata']).role);
    },
    refreshConversionAssets: async () => { refreshed += 1; }});
  vm.runInContext(sourceBetween('function uploadConversionFile(', 'function refreshConversionAssets('), c);
  const file = (name, size = 1000) => ({value: 'C:\\fakepath\\' + name, files: [{name, size, type: 'audio/wav'}]});

  // A source performance needs no permission: it is sent with its name and role, then chosen once the list has it.
  const performance = file('take.wav');
  const uploaded = await c.uploadConversionFile('source', performance);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/conversion/uploads');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.body, performance.files[0]);
  assert.deepEqual(JSON.parse(calls[0].options.headers['X-Reference-Metadata']), {filename: 'take.wav', role: 'source'});
  assert.equal(refreshed, 1);
  assert.equal(fields.conversionSource.value, uploaded.path);
  assert.equal(fields.conversionStatus.textContent, 'Added take.wav (41.5 s) as the source performance.');
  assert.equal(performance.value, '');  // the same file can be chosen again

  // A target voice is refused before anything is sent until the box is ticked and both terms are filled in.
  const target = file('voice.mp3');
  assert.equal(await c.uploadConversionFile('target', target), null);
  fields.conversionTargetPermission.checked = true;
  fields.conversionTargetOrigin.value = 'My own voice, recorded today';
  target.value = 'C:\\fakepath\\voice.mp3';
  assert.equal(await c.uploadConversionFile('target', target), null);  // the allowed use is still empty
  assert.equal(calls.length, 1);
  assert.match(fields.conversionError.textContent, /tick that you own it or have permission to clone the voice, and fill in its origin and allowed use/);
  assert.equal(target.value, '');
  fields.conversionTargetAllowedUse.value = 'Private tests only';
  target.value = 'C:\\fakepath\\voice.mp3';
  await c.uploadConversionFile('target', target);
  assert.equal(calls.length, 2);
  assert.deepEqual(JSON.parse(calls[1].options.headers['X-Reference-Metadata']), {filename: 'voice.mp3', role: 'target',
    origin: 'My own voice, recorded today', allowed_use: 'Private tests only', permission: true});
  assert.equal(fields.conversionTarget.value, 'D:\\lab\\out\\conversions\\uploads\\target\\0a1b.wav');
  assert.equal(fields.conversionError.textContent, '');
  assert.equal(fields.conversionTargetPermission.checked, false);  // the next target file is confirmed on its own

  // Other files are refused before sending, and the service's refusal is shown.
  assert.equal(await c.uploadConversionFile('source', file('notes.txt')), null);
  assert.equal(fields.conversionError.textContent, 'Choose WAV, FLAC, OGG, MP3 or M4A audio.');
  assert.equal(await c.uploadConversionFile('source', file('long.wav', 40 * 1024 * 1024)), null);
  assert.equal(fields.conversionError.textContent, 'The file is larger than 32 MB.');
  answer = () => ({ok: false, status: 400, json: async () => ({error: 'A source performance must be 0.5 to 60 seconds long.'})});
  assert.equal(await c.uploadConversionFile('source', file('long.wav')), null);
  assert.equal(fields.conversionError.textContent, 'A source performance must be 0.5 to 60 seconds long.');
  assert.equal(fields.conversionStatus.textContent, '');
  assert.equal(calls.length, 3);

  // The lists name an upload by the name it had on this computer.
  vm.runInContext(sourceBetween('function conversionLabel(', 'function refreshConversionAssets('), c);
  assert.equal(c.conversionLabel({original_name: 'voice.mp3', relative_path: 'out/conversions/uploads/target/0a1b.wav',
    duration_seconds: 6.2}), 'voice.mp3 (from this computer, 6.2 s)');
  assert.equal(c.conversionLabel({relative_path: 'reference/af_heart.wav', duration_seconds: 9.4}), 'reference/af_heart.wav (9.4 s)');
  assert.match(pageHtml(), /id="conversionSourceFile" type="file" accept="\.wav,\.flac,\.ogg,\.mp3,\.m4a/);
  assert.match(pageHtml(), /id="conversionTargetFile" type="file" accept="\.wav,\.flac,\.ogg,\.mp3,\.m4a/);
  assert.match(source, /\$\('conversionTargetFile'\)\.addEventListener\('change',function\(\)\{uploadConversionFile\('target',this\);\}\);/);
});

function pageHtml() {
  return source.slice(0, source.indexOf('</html>'));
}
