'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.events = {}; this._text = ''; this._value = ''; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; this._text = ''; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get options() { return this.children; }
  get value() { return this._value || (this.tagName === 'select' && this.children.length ? this.children[0].value : ''); }
  set value(value) { this._value = String(value); }
  addEventListener(name, handler) { this.events[name] = handler; }
  setAttribute(name, value) { this[name] = value; }
}

function setup(takes = []) {
  const nodes = new Map();
  const document = {
    getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element(/Select$/.test(id) ? 'select' : 'div')); return nodes.get(id); },
    createElement(name) { return new Element(name); },
    createTextNode(text) { const result = new Element('#text'); result.textContent = text; return result; },
  };
  const state = { takes, runs: [], sessions: [], requests: [], manifests: {} };
  const context = vm.createContext({ document, console, fetch(url, options = {}) {
    state.requests.push({ url, options });
    let data;
    if (url === '/api/takes') data = { takes: state.takes };
    else if (url === '/api/evaluations') data = { runs: state.runs };
    else if (url === '/api/evaluations/blind-sessions') data = { sessions: state.sessions };
    else if (url === '/api/evaluations/cast-import') {
      const payload = JSON.parse(options.body);
      const run = { id: 'cast-test', title: payload.title, suite_id: 'saved-cast-candidates', samples: payload.take_ids.map(id => ({ id })), manifest: {}, summary: {} };
      state.runs = [run]; state.manifests[run.id] = run; data = { run };
    } else if (url.endsWith('/ratings') && state.ratingSession) data = { session: state.ratingSession };
    else if (url.startsWith('/api/evaluations/runs/')) data = { run: state.manifests[url.split('/').pop()] };
    else throw new Error('Unexpected request ' + url);
    return Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
  } });
  const html = fs.readFileSync(path.join(__dirname, '../compare.html'), 'utf8');
  const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(match => match[1]).find(text => text.includes('renderCastTakes'));
  assert.ok(script, 'Compare inline application script exists');
  vm.runInContext(script, context);
  return { context, state, node: id => document.getElementById(id) };
}

async function settled() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); }
function descendants(node) { return node.children.flatMap(child => [child, ...descendants(child)]); }
function take(id, name) { return { id: id.repeat(32), character_name: name, identity_label: name + ' voice', engine_id: 'drama-3',
  text: 'The gate is open.', language: 'English', direction: '', duration_seconds: 2.5,
  provenance: { allowed_use: 'Local tests only' } }; }

test('saved performances show searchable, safely rendered labels, rights and real audio previews', async () => {
  const app = setup([take('a', '<img src=x>'), take('b', 'Birch')]);
  await settled();
  assert.match(app.node('castTakeList').textContent, /<img src=x>/);
  assert.match(app.node('castTakeList').textContent, /Local tests only/);
  const audio = descendants(app.node('castTakeList')).filter(node => node.tagName === 'audio');
  assert.equal(audio.length, 2);
  assert.equal(audio[0].preload, 'none');
  assert.match(audio[0].src, /^\/api\/takes\/[a-f0-9]{32}\/audio$/);
  app.node('takeFilter').value = 'Birch';
  app.node('takeFilter').events.input();
  assert.match(app.node('takeCount').textContent, /1\/2 shown/);
  assert.doesNotMatch(app.node('castTakeList').textContent, /<img/);
});

test('selected same-model candidates create an evaluation run using IDs and keep it selected', async () => {
  const app = setup([take('a', 'Ash'), take('b', 'Birch')]);
  await settled();
  const checkboxes = descendants(app.node('castTakeList')).filter(node => node.type === 'checkbox');
  for (const checkbox of checkboxes) { checkbox.checked = true; checkbox.events.change(); }
  assert.equal(app.node('importCast').disabled, false);
  app.node('comparisonTitle').value = 'New character voices';
  app.node('importCast').events.click();
  await settled();
  const request = app.state.requests.find(row => row.url === '/api/evaluations/cast-import');
  assert.deepEqual(JSON.parse(request.options.body), { title: 'New character voices', take_ids: ['b'.repeat(32), 'a'.repeat(32)] });
  assert.equal(app.node('runSelect').value, 'cast-test');
  assert.match(app.node('castImportStatus').textContent, /No model was loaded and no ratings were assigned/);
});

test('candidate names remain absent in blind trials and resolve after explicit reveal', async () => {
  const app = setup();
  await settled();
  const session = { session_id: 's', run_id: 'cast-existing', state: 'blind', evaluator: 'Listener', trials: [
    { trial_id: 't', case_id: 'shared-line-01', text: 'The gate is open.', rating: null,
      left: { audio_url: '/blind/a' }, right: { audio_url: '/blind/b' } },
  ] };
  app.context.showSession(session);
  assert.doesNotMatch(app.node('session').textContent, /Ash|Birch/);
  assert.equal(app.state.requests.filter(row => row.url.includes('/runs/')).length, 0);
  app.state.manifests['cast-existing'] = { manifest: { candidate_labels: { 'candidate-a': 'Ash / Fish', 'candidate-b': 'Birch / Fish' } } };
  session.state = 'revealed'; session.trials[0].revealed = { left_engine: 'candidate-a', right_engine: 'candidate-b' };
  await app.context.showSession(session);
  assert.match(app.node('session').textContent, /A = Ash \/ Fish; B = Birch \/ Fish/);
  assert.equal(session.trials[0].revealed.left_engine, 'candidate-a', 'API session is not rewritten');
});

test('A and B receive independent scores and preference, with reveal disabled until both are saved', async () => {
  const app = setup();
  await settled();
  const session = { session_id: 's', run_id: 'cast-existing', state: 'blind', evaluator: 'Fixture', trials: [
    { trial_id: 't', case_id: 'shared-line-01', text: 'The gate is open.', rating: null, rating_complete: false,
      left: { audio_url: '/blind/a' }, right: { audio_url: '/blind/b' } },
  ] };
  app.context.showSession(session);
  const all = descendants(app.node('session'));
  const selects = all.filter(node => node.tagName === 'select');
  const save = all.find(node => node.tagName === 'button' && node.textContent === 'Save A and B ratings');
  const reveal = all.find(node => node.tagName === 'button' && node.textContent === 'Reveal model mapping and ratings');
  assert.equal(selects.length, 11, 'five A scores, five B scores and optional preference');
  assert.equal(reveal.disabled, true);
  for (let i = 0; i < 5; i++) selects[i].value = String(i + 1);
  save.events.click();
  assert.match(app.node('error').textContent, /all five scores for A and all five scores for B/);
  assert.equal(app.state.requests.filter(row => row.url.endsWith('/ratings')).length, 0);
  for (let i = 0; i < 5; i++) selects[i + 5].value = String(5 - i);
  selects[10].value = 'B';
  const fields = ['intelligibility', 'identity_consistency', 'acting_fit', 'naturalness', 'artifacts'];
  const left = Object.fromEntries(fields.map((field, i) => [field, i + 1]));
  const right = Object.fromEntries(fields.map((field, i) => [field, 5 - i]));
  app.state.ratingSession = JSON.parse(JSON.stringify(session));
  Object.assign(app.state.ratingSession.trials[0], { rating_complete: true, rating_format: 'per-side',
    rating: { left, right, preference: 'B', notes: 'Synthetic UI fixture' } });
  all.find(node => node.tagName === 'textarea').value = 'Synthetic UI fixture';
  save.events.click();
  await settled();
  const saved = app.state.requests.find(row => row.url.endsWith('/ratings'));
  assert.deepEqual(JSON.parse(saved.options.body), { trial_id: 't', ratings: { left, right, preference: 'B' }, notes: 'Synthetic UI fixture' });
  const refreshed = descendants(app.node('session'));
  assert.equal(refreshed.find(node => node.tagName === 'button' && node.textContent === 'Reveal model mapping and ratings').disabled, false);
  const restored = refreshed.filter(node => node.tagName === 'select');
  assert.equal(restored[0].value, '1');
  assert.equal(restored[5].value, '5');
  assert.equal(restored[10].value, 'B');
});

test('legacy pair ratings are explicit history and never prefill either side or enable reveal', async () => {
  const app = setup();
  await settled();
  const legacy = { intelligibility: 4, identity_consistency: 3, acting_fit: 2, naturalness: 4, artifacts: 5,
    notes: 'Historical score with no side recorded' };
  app.context.showSession({ session_id: 's', run_id: 'old', state: 'blind', evaluator: 'Old fixture', trials: [
    { trial_id: 't', case_id: 'old-line', text: 'Hello.', rating: legacy, legacy_rating: legacy,
      rating_complete: false, rating_format: 'legacy-pair', left: { audio_url: '/a' }, right: { audio_url: '/b' } },
  ] });
  assert.match(app.node('session').textContent, /Legacy pair-level rating — no A\/B attribution/);
  assert.match(app.node('session').textContent, /Historical score with no side recorded/);
  const all = descendants(app.node('session'));
  assert.ok(all.filter(node => node.tagName === 'select').every(select => select.value === ''));
  assert.equal(all.find(node => node.tagName === 'button' && node.textContent === 'Reveal model mapping and ratings').disabled, true);
});
