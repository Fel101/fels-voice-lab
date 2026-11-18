// Run with node --test tests/sample_ui.test.cjs. Exercises where the page offers the sample scene, and its removal, from the
// page script (voice_lab_scenes.js).
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {pageSource, englishPageText } = require('./page_source.cjs');
const labText = englishPageText();

const source = pageSource();
function sourceBetween(start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  if (from < 0 || to < 0) throw new Error(`Missing source block: ${start}`);
  return source.slice(from, to);
}

const SAMPLE = {scene_id: 'a'.repeat(32), scene_name: 'Sample: The parcel at the lighthouse',
  characters: [{id: 'b'.repeat(32), name: 'Edie (sample)'}, {id: 'c'.repeat(32), name: 'Tomas (sample)'}]};
const PREVIEW = {scene: {scene_id: SAMPLE.scene_id, name: SAMPLE.scene_name, jobs: 1},
  would_remove: {character: 2, identity: 2, reference: 0, take: 9}, used_elsewhere: [], external_copies: []};

function harness({state = {sample: null, can_make: true, why_not: null}, scenes = 0, active = null, characters = [],
  ready = null, confirm = true, preview = PREVIEW} = {}) {
  const fields = {}, calls = [], confirms = [];
  const field = (id) => fields[id] || (fields[id] = {id, hidden: false, disabled: false, textContent: ''});
  const answers = {
    '/api/sample': () => state,
    '/api/sample/remove/preview': () => preview,
    '/api/sample/remove': () => ({removed: {scene: 1, jobs: 1, character: 2, identity: 2, reference: 0, take: 9},
      external_copies: ['out/exports/x/line-001.wav']}),
  };
  const context = vm.createContext({
    LabText: labText,
    $: field, location: {hash: '#cast'}, sceneDirty: false, sceneListCount: scenes, sampleState: null,
    activeSceneId: active, castLoaded: true, castCharacters: characters, firstRun: {ready},
    window: {confirm(text) { confirms.push(text); return confirm; }},
    fetchJson(url, body) { calls.push({url, body}); return Promise.resolve(answers[url](body)); },
    refreshCast() { calls.push({url: 'refreshCast'}); return Promise.resolve(); },
    refreshSceneList(preferred) { calls.push({url: 'refreshSceneList', preferred}); return Promise.resolve(); },
  });
  field('firstRun').hidden = !ready;
  vm.runInContext(sourceBetween('function refreshSample()', 'function startSceneFromPerformer()'), context);
  return {context, fields, calls, confirms};
}

test('an empty Lab with Kokoro is offered the sample in Scenes, an empty Cast and the first-run note', async () => {
  const h = harness({ready: 'Kokoro 82M'});
  await h.context.refreshSample();
  assert.equal(h.fields.sampleNote.hidden, false);
  assert.equal(h.fields.sampleOpen.hidden, false);
  assert.equal(h.fields.sampleRemove.hidden, true);
  assert.match(h.fields.sampleText.textContent, /^No scene yet\. Open the sample scene/);
  assert.equal(h.fields.castSampleNote.hidden, false);
  assert.equal(h.fields.firstRunSample.hidden, false);
});

test('without Kokoro, or with scenes and characters of its own, the Lab offers nothing', async () => {
  for (const options of [{state: {sample: null, can_make: false, why_not: 'Kokoro 82M is not installed.'}, ready: 'Phonon'},
    {scenes: 1, characters: [{id: 'd'.repeat(32)}]}]) {
    const h = harness(options);
    await h.context.refreshSample();
    assert.equal(h.fields.sampleNote.hidden, true);
    assert.equal(h.fields.castSampleNote.hidden, true);
    assert.equal(h.fields.firstRunSample.hidden, true);
  }
});

test('the sample itself says how to hear it and offers to remove it; another scene shows nothing of it', async () => {
  const h = harness({state: {sample: SAMPLE, can_make: true, why_not: null}, scenes: 2, active: SAMPLE.scene_id});
  await h.context.refreshSample();
  assert.equal(h.fields.sampleNote.hidden, false);
  assert.equal(h.fields.sampleOpen.hidden, true);
  assert.equal(h.fields.sampleRemove.hidden, false);
  assert.match(h.fields.sampleText.textContent, /press Render all, and when the batch has finished, Use the newest take/);
  h.context.activeSceneId = 'e'.repeat(32);
  h.context.renderSampleNotes();
  assert.equal(h.fields.sampleNote.hidden, true);
});

test('opening the sample makes it, goes to Scenes and opens it there', async () => {
  const h = harness();
  h.context.fetchJson = (url, body) => {
    h.calls.push({url, body});
    return Promise.resolve({made: true, sample: SAMPLE});
  };
  await h.context.openSample();
  assert.deepEqual(h.calls.map((call) => call.url), ['/api/sample', 'refreshCast', 'refreshSceneList']);
  assert.equal(JSON.stringify(h.calls[0].body), '{}');
  assert.equal(h.calls[2].preferred, SAMPLE.scene_id);
  assert.equal(h.context.location.hash, '#scenes');
  assert.equal(h.fields.sampleStatus.textContent, 'Made the sample scene and its two characters, Edie and Tomas.');
  assert.equal(h.fields.sampleOpen.disabled, false);
});

test('removing asks first with what goes, and removes nothing when the answer is no', async () => {
  const h = harness({confirm: false});
  await h.context.removeSample();
  assert.equal(h.confirms.length, 1);
  assert.match(h.confirms[0], /^Remove the sample\? This deletes the scene “Sample: The parcel at the lighthouse” with its batches/);
  assert.match(h.confirms[0], /2 characters and their voices, 9 takes\. Your own characters and scenes are not touched/);
  assert.deepEqual(h.calls.map((call) => call.url), ['/api/sample/remove/preview']);
});

test('a yes removes it with confirm and says what went and what stayed', async () => {
  const h = harness();
  await h.context.removeSample();
  assert.deepEqual(h.calls.map((call) => call.url),
    ['/api/sample/remove/preview', '/api/sample/remove', 'refreshCast', 'refreshSceneList']);
  assert.equal(JSON.stringify(h.calls[1].body), '{"confirm":true}');
  assert.equal(h.fields.sampleStatus.textContent,
    'Removed the sample scene, its characters and 9 takes. Copies in export packages stay where they are (1 file).');
});

test('a sample voice used in another scene stops the removal before it asks', async () => {
  const h = harness({preview: {...PREVIEW, used_elsewhere: ['My scene']}});
  await h.context.removeSample();
  assert.equal(h.confirms.length, 0);
  assert.equal(h.fields.sampleStatus.textContent,
    'A sample character speaks in another scene: My scene. Give those lines another voice first.');
});
