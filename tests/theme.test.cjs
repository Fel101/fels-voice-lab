const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'theme.js'), 'utf8');

function fixture(fetch) {
  const root = {dataset:{theme:'dark'}};
  const picker = {value:'system',addEventListener(event, listener){this.change = listener;}};
  const status = {textContent:''};
  const context = vm.createContext({document:{documentElement:root,
    getElementById:id=>id==='themeChoice'?picker:status},fetch,Promise});
  vm.runInContext(source,context);
  return {root,picker,status};
}
const flush = () => new Promise(resolve=>setImmediate(resolve));

test('theme restores saved choice and applies changes before the save completes', async () => {
  const calls=[];
  const {root,picker,status}=fixture((url,options)=>{calls.push([url,JSON.parse(options.body)]);return Promise.resolve({ok:true});});
  assert.equal(picker.value,'dark');
  picker.value='light';picker.change();
  assert.equal(root.dataset.theme,'light');
  await flush();assert.deepEqual(calls,[['/api/preferences',{theme:'light'}]]);
  assert.equal(status.textContent,'');
});

test('every theme in the menu is applied and saved, and an unknown one is ignored', async () => {
  const calls=[];
  const {root,picker}=fixture((url,options)=>{calls.push(JSON.parse(options.body).theme);return Promise.resolve({ok:true});});
  for (const theme of ['forest','dark','light','system']) {picker.value=theme;picker.change();assert.equal(root.dataset.theme,theme);}
  await flush();assert.deepEqual(calls,['forest','dark','light','system']);
  picker.value='auto';picker.change();await flush();
  assert.equal(root.dataset.theme,'system');assert.equal(calls.length,4);
});

test('rapid changes save in order and recover after a failed write', async () => {
  const calls=[],replies=[];
  const {root,picker,status}=fixture((url,options)=>{
    calls.push(JSON.parse(options.body).theme);return new Promise(resolve=>replies.push(resolve));
  });
  picker.value='light';picker.change();picker.value='system';picker.change();
  await flush();assert.deepEqual(calls,['light']);
  replies.shift()({ok:false});await flush();
  assert.equal(status.textContent,'');assert.deepEqual(calls,['light','system']);
  replies.shift()({ok:false});await flush();
  assert.match(status.textContent,/saving failed/);assert.equal(root.dataset.theme,'system');
  picker.change();await flush();replies.shift()({ok:true});await flush();
  assert.equal(status.textContent,'');
});
