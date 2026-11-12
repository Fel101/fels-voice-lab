// Run with node --test tests/scene_history.test.cjs. Exercises the scene editor's undo history from the page script.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {pageSource} = require('./page_source.cjs');

const source = pageSource();
function sourceBetween(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}

function sceneWith(name) {
  return JSON.stringify({scene_id: 'a'.repeat(32), name, revision: 1, lines: [{line_id: 'line-001', text: 'Hello.'}]}, null, 2);
}

function harness(text) {
  const clock = {now: 0};
  const fields = {sceneEditor: {value: text}, sceneStatus: {textContent: ''}, sceneError: {textContent: ''},
    sceneUndo: {disabled: true}, sceneRedo: {disabled: true}};
  const c = vm.createContext({$: (id) => fields[id], Date: {now: () => clock.now}, renderSceneForm() {}, paintSceneRow() {}});
  vm.runInContext(sourceBetween('function showSceneError(', 'function editSceneSetting('), c);
  vm.runInContext(sourceBetween('function updateSceneEditor(', 'function stableSceneLineId('), c);
  return {c, fields, clock, name: () => JSON.parse(fields.sceneEditor.value).name};
}

test('a change is one undo step, and undo and redo move between the saved script and the edit', () => {
  const h = harness(sceneWith('Saved'));
  h.c.resetSceneHistory(h.fields.sceneEditor.value);
  h.c.changeSceneField(null, 'name', 'Changed');
  assert.equal(h.name(), 'Changed');
  assert.equal(h.c.sceneDirty, true);
  assert.equal(h.fields.sceneUndo.disabled, false);
  h.c.undoScene();
  assert.equal(h.name(), 'Saved');
  assert.equal(h.c.sceneDirty, false);
  assert.equal(h.fields.sceneStatus.textContent, 'Back to the saved script.');
  assert.equal(h.fields.sceneRedo.disabled, false);
  h.c.redoScene();
  assert.equal(h.name(), 'Changed');
  assert.equal(h.c.sceneDirty, true);
});

test('keystrokes in one field less than a second apart are one undo step; a pause starts another', () => {
  const h = harness(sceneWith('Saved'));
  h.c.resetSceneHistory(h.fields.sceneEditor.value);
  for (const [at, name] of [[0, 'C'], [400, 'Ch'], [800, 'Cha'], [1200, 'Char']]) {
    h.clock.now = at;
    h.c.changeSceneField(null, 'name', name);
  }
  assert.equal(h.c.sceneUndo.length, 1);
  h.clock.now = 3000;
  h.c.changeSceneField(null, 'name', 'Chart');
  assert.equal(h.c.sceneUndo.length, 2);
  h.c.undoScene();
  assert.equal(h.name(), 'Char');
  h.c.undoScene();
  assert.equal(h.name(), 'Saved');
});

test('a different field starts a new step, and a new change clears what was undone', () => {
  const h = harness(sceneWith('Saved'));
  h.c.resetSceneHistory(h.fields.sceneEditor.value);
  h.c.changeSceneField(null, 'name', 'First');
  h.c.changeSceneField(0, 'text', 'Edited.');
  assert.equal(h.c.sceneUndo.length, 2);
  h.c.undoScene();
  assert.equal(JSON.parse(h.fields.sceneEditor.value).lines[0].text, 'Hello.');
  assert.equal(h.name(), 'First');
  assert.equal(h.c.sceneRedo.length, 1);
  h.c.changeSceneField(null, 'name', 'Second');
  assert.equal(h.c.sceneRedo.length, 0);
  assert.equal(h.fields.sceneRedo.disabled, true);
});

test('undo keeps at most one hundred steps', () => {
  const h = harness(sceneWith('Saved'));
  h.c.resetSceneHistory(h.fields.sceneEditor.value);
  for (let index = 0; index < 105; index += 1) {
    h.clock.now += 2000;
    h.c.changeSceneField(null, 'name', `Name ${index}`);
  }
  assert.equal(h.c.sceneUndo.length, 100);
});

test('a new starter script is never clean, so undoing back to it stays unsaved', () => {
  const h = harness(sceneWith('Starter'));
  h.c.resetSceneHistory(null);
  h.c.changeSceneField(null, 'name', 'Edited');
  h.c.undoScene();
  assert.equal(h.name(), 'Starter');
  assert.equal(h.c.sceneDirty, true);
});

test('saving or loading a scene clears undo, so an undo cannot restore an older revision', () => {
  const h = harness(sceneWith('Saved'));
  h.c.resetSceneHistory(h.fields.sceneEditor.value);
  h.c.changeSceneField(null, 'name', 'Edited');
  assert.equal(h.c.sceneUndo.length, 1);
  h.c.resetSceneHistory(sceneWith('Saved, revision 2'));
  assert.equal(h.c.sceneUndo.length, 0);
  assert.equal(h.fields.sceneUndo.disabled, true);
});

test('while a batch runs, the status line says which model renders now and which come after it', () => {
  const c = vm.createContext({});
  vm.runInContext(sourceBetween('function batchOrderText(', 'function refreshSceneJob('), c);
  const kokoro = {engine_id: 'kokoro', name: 'Kokoro 82M', total: 12, done: 7};
  const qwen = {engine_id: 'qwen3-tts-1.7b-base', name: 'Qwen3-TTS 1.7B Base', total: 8, done: 0};
  assert.equal(c.batchOrderText({models: [kokoro, qwen]}),
    'Rendering one model at a time: Kokoro 82M: 7 of 12, then Qwen3-TTS 1.7B Base: 8. ');
  assert.equal(c.batchOrderText({models: [{...kokoro, done: 12}, {...qwen, done: 3}]}),
    'Rendering one model at a time: Qwen3-TTS 1.7B Base: 3 of 8. ');
  assert.equal(c.batchOrderText({models: [kokoro]}), 'Rendering with Kokoro 82M: 7 of 12. ');
  assert.equal(c.batchOrderText({models: [{...kokoro, done: 12}]}), '');    // finished: the counts that follow say so
  assert.equal(c.batchOrderText({}), '');    // a job from before the service listed its models
});

test('a row waits for a save while its line differs from the saved scene, then shows the server status', () => {
  const c = vm.createContext({});
  vm.runInContext(sourceBetween('function savedSceneLines(', 'function resetSceneHistory('), c);
  vm.runInContext(sourceBetween('function sceneRowState(', 'function sceneButton('), c);
  c.sceneSavedLines = c.savedSceneLines(JSON.stringify({lines: [{line_id: 'a', text: 'One.'}, {line_id: 'b', text: 'Two.'}]}));
  c.sceneLineStates = {a: {status: 'done', reason: ''}, b: {status: 'out_of_date', reason: 'The text changed.'}};
  assert.equal(c.sceneRowState({line_id: 'a', text: 'One.'}).status, 'done');
  assert.equal(c.sceneRowState({line_id: 'b', text: 'Two.'}).reason, 'The text changed.');
  assert.equal(c.sceneRowState({line_id: 'a', text: 'One, edited.'}).status, 'unsaved');
  assert.equal(c.sceneRowState({line_id: 'c', text: 'New.'}).status, 'unsaved');    // a line added since the save
  assert.equal(JSON.stringify(c.savedSceneLines('not a scene')), '{}');
  assert.equal(JSON.stringify(c.savedSceneLines(null)), '{}');    // a starter scene that was never saved
});

test('Render what is missing takes the lines not rendered, failed or out of date, in script order', () => {
  const fields = {sceneStatus: {textContent: ''}};
  const c = vm.createContext({$: (id) => fields[id]});
  vm.runInContext(sourceBetween('var sceneNeedsRender', 'function sceneModelName('), c);
  vm.runInContext(sourceBetween('function sceneOrderedRows(', 'function sceneButton('), c);
  vm.runInContext(sourceBetween('function missingSceneLines(', 'function chooseNewestSceneTakes('), c);
  const row = (id, index, state) => ({line: {line_id: id}, index, state});
  c.sceneRows = {z: row('z', 2, 'failed'), a: row('a', 0, 'not_rendered'), m: row('m', 1, 'done'),
    '7': row('7', 3, 'out_of_date'), q: row('q', 4, 'choose_take')};
  assert.equal(JSON.stringify(c.missingSceneLines()), JSON.stringify({line_ids: ['a', 'z', '7']}));
  c.sceneRows = {m: row('m', 0, 'done'), q: row('q', 1, 'choose_take')};
  assert.equal(c.missingSceneLines(), null);
  assert.match(fields.sceneStatus.textContent, /^Nothing is missing or out of date/);
});
