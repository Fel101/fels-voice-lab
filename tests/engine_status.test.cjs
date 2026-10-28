// Run with node --test tests/engine_status.test.cjs. Exercises the Models page engine checks in serve.html.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {pageSource} = require('./page_source.cjs');

const page = pageSource();

function sourceBetween(start, end) {
  const from = page.indexOf(start);
  const to = page.indexOf(end, from + start.length);
  if (from < 0 || to < 0) throw new Error(`Missing source block: ${start}`);
  return page.slice(from, to);
}

function element(tag) {
  const item = {tag, children: [], className: '', disabled: false, colSpan: 0, handlers: {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(type, handler) { this.handlers[type] = handler; }};
  let text = '';
  Object.defineProperty(item, 'textContent', {
    get() { return text + this.children.map(child => child.textContent).join(''); },
    set(value) { text = String(value); this.children = []; },
  });
  return item;
}

function harness(responses = {}) {
  const fields = {}, calls = [];
  const context = {
    document: {createElement: element},
    $: id => fields[id] || (fields[id] = element(id)),
    fetchJson(url, body) {
      calls.push({url, body, turboDisabledNow: context.modelCards && context.modelCards.turbo
        ? context.modelCards.turbo.button.disabled : null});
      const value = responses[url] ?? {};
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    },
  };
  vm.createContext(context);
  vm.runInContext(sourceBetween('var modelCards={};', 'var castCharacters = [];'), context);
  return {context, fields, calls};
}

function card(name) {
  return {name, state: element('state'), details: element('details'), button: {disabled: false, textContent: ''}};
}

test('state text names the device and keeps install and conversion states apart', () => {
  const {context: c} = harness();
  assert.equal(c.engineStateText({state: 'usable'}, 'cpu'), 'Speaks now on CPU');
  assert.equal(c.engineStateText({state: 'usable'}, 'cuda'), 'Speaks now on GPU');
  assert.equal(c.engineStateText({state: 'check_failed'}, 'cpu'), 'Check failed on CPU');
  assert.equal(c.engineStateText({state: 'not_checked'}, 'cuda'), 'Installed, not checked on GPU');
  assert.equal(c.engineStateText({state: 'check_outdated'}, 'cuda'), 'Installed, check out of date on GPU');
  assert.equal(c.engineStateText({state: 'not_installed'}, 'cpu'), 'Not installed');
  assert.equal(c.engineStateText({state: 'conversion_task'}, 'cpu'), 'Conversion task');
  assert.equal(c.engineStateText({state: 'not_integrated'}, 'cpu'), 'Not integrated');
});

test('detail text lists what is missing and what the last check measured', () => {
  const {context: c} = harness();
  assert.equal(c.engineDetailText({state: 'not_installed', installation: {missing: ['.venv/python.exe', 'hf/snapshot']}}, 'cpu'),
    'Missing: .venv/python.exe; hf/snapshot.');
  assert.equal(c.engineDetailText({state: 'not_checked', check: null}, 'cpu'),
    'Click Check to load the model and speak one short sentence on this device.');
  const passed = {state: 'usable', check: {status: 'passed', checked_at: '2026-10-22T10:00:00+00:00',
    total_s: 0.5, audio_seconds: 2.5, ran_on: 'Vulkan1'}};
  assert.equal(c.engineDetailText(passed, 'cpu'),
    'Checked 2026-10-22 10:00 UTC: 0.5 s to make 2.5 s of audio, on Vulkan1.');
  assert.equal(c.engineDetailText({state: 'check_failed', check: {status: 'failed', checked_at: '2026-10-22T11:30:00+00:00', reason: 'boom'}}, 'cpu'),
    'Failed 2026-10-22 11:30 UTC: boom');
  assert.equal(c.engineDetailText({state: 'conversion_task'}, 'cpu'),
    'Checked from Voice conversion, because it needs source audio.');
  assert.match(c.engineDetailText({state: 'check_outdated', check: {status: 'passed', checked_at: '2026-10-22T10:00:00+00:00'}}, 'cpu'),
    /last check \(2026-10-22 10:00 UTC\) was made before this model or its packages changed.*Click Check again/);
});

test('a missing engine with a guided installer is told which launcher to run, and one without is not', () => {
  const {context: c} = harness();
  const missing = ['hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/f3ff'];
  assert.equal(c.engineDetailText({state: 'not_installed', installation: {missing, installer: 'setup-kokoro.bat'}}, 'cpu'),
    'Missing: hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/f3ff. To install it, run setup-kokoro.bat in the lab folder, '
    + 'then reload this page. docs/SETUP.md says what it installs.');
  assert.equal(c.engineDetailText({state: 'not_installed', installation: {missing}}, 'cpu'),
    'Missing: hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/f3ff.');
  assert.doesNotMatch(c.engineDetailText({state: 'not_checked', check: null, installation: {missing: [], installer: 'setup-kokoro.bat'}}, 'cpu'),
    /setup-kokoro/);
});

test('a missing Kokoro card shows the launcher beside its list of missing files and keeps its check button off', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'not_installed', check: null,
      installation: {installed: false, missing: ['.venv/Scripts/python.exe', 'hf-cache/snapshot'], installer: 'setup-kokoro.bat'}}]});
  assert.equal(c.modelCards.kokoro.state.textContent, 'Not installed');
  assert.match(c.modelCards.kokoro.details.textContent,
    /^Missing: \.venv\/Scripts\/python\.exe; hf-cache\/snapshot\. To install it, run setup-kokoro\.bat in the lab folder, then reload this page\./);
  assert.equal(c.modelCards.kokoro.button.disabled, true);
});

test('an out-of-date check does not claim the engine speaks and still offers a re-check', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus({device: 'cuda', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'check_outdated', check_outdated: true, installation: {missing: []},
      check: {status: 'passed', checked_at: '2026-10-22T10:00:00+00:00', total_s: 0.5, audio_seconds: 2, ran_on: 'cuda'}}]});
  assert.equal(c.modelCards.kokoro.state.textContent, 'Installed, check out of date on GPU');
  assert.doesNotMatch(c.modelCards.kokoro.state.textContent, /Speaks now/);
  assert.equal(c.modelCards.kokoro.button.disabled, false);
  assert.equal(c.modelCards.kokoro.button.textContent, 'Check again');
});

test('busy services and missing installs disable checks, and usable engines offer a re-check', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro'), breeze: card('Breeze')};
  c.renderEngineStatus({device: 'cpu', busy: true, speed: [], engines: [
    {id: 'kokoro', state: 'not_checked', installation: {missing: []}, check: null},
    {id: 'breeze', state: 'not_installed', installation: {missing: ['x']}, check: null}]});
  assert.equal(c.modelCards.kokoro.button.disabled, true);
  assert.equal(c.modelCards.breeze.button.disabled, true);
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'usable', installation: {missing: []},
      check: {status: 'passed', checked_at: 't', total_s: 0.5, audio_seconds: 2, ran_on: 'cpu'}},
    {id: 'breeze', state: 'not_installed', installation: {missing: ['x']}, check: null}]});
  assert.equal(c.modelCards.kokoro.button.disabled, false);
  assert.equal(c.modelCards.kokoro.button.textContent, 'Check again');
  assert.equal(c.modelCards.kokoro.state.textContent, 'Speaks now on CPU');
  assert.equal(c.modelCards.breeze.button.disabled, true);
});

test('speed table shows an empty message, then one row per device with its real-time factor', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M')};
  c.renderSpeedRows({device: 'cpu', speed: []});
  assert.equal(fields.speedRows.children.length, 1);
  assert.match(fields.speedRows.children[0].textContent, /No clips measured yet/);
  c.renderSpeedRows({device: 'cpu', speed: [
    {engine: 'kokoro', device: 'cpu', clips: 3, median_real_time_factor: 0.25, last_total_s: 0.5, last_audio_seconds: 2},
    {engine: 'turbo', device: 'cuda', clips: 1, median_real_time_factor: null, last_total_s: 1.234, last_audio_seconds: 4}]});
  const rows = fields.speedRows.children;
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].children.map(cell => cell.textContent),
    ['Kokoro 82M', 'CPU', '3', '0.25', '0.50 s for 2.00 s of audio']);
  assert.deepEqual(rows[1].children.map(cell => cell.textContent),
    ['turbo', 'GPU', '1', '—', '1.23 s for 4.00 s of audio']);
});

test('model cards keep integration apart from the check button, and conversion has none', () => {
  const {context: c, fields} = harness();
  c.renderModelOverview({engines: [
    {id: 'kokoro', name: 'Kokoro 82M', integration: {service_api: true}, capabilities: {voice_selection: {status: 'supported'}}},
    {id: 'chatterbox-vc', name: 'Chatterbox VC', integration: {service_api: true, task_only: true}, capabilities: {}}]});
  const [kokoro, conversion] = fields.modelOverview.children;
  assert.equal(kokoro.children[1].textContent, 'Integrated');
  assert.equal(kokoro.children[5].tag, 'button');
  assert.equal(kokoro.children[5].textContent, 'Check it speaks');
  assert.equal(conversion.children[1].textContent, 'Integrated · conversion');
  assert.equal(conversion.children.some(child => child.tag === 'button'), false);
});

test('a check posts once, holds the other cards while it runs, then refreshes the status', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'usable', installation: {missing: []},
      check: {status: 'passed', checked_at: 't', total_s: 0.5, audio_seconds: 2, ran_on: 'cpu'}},
    {id: 'turbo', state: 'not_checked', installation: {missing: []}, check: null}]};
  const {context: c, calls} = harness({'/api/engines/kokoro/check': {check: {status: 'passed'}},
    '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro'), turbo: card('Turbo')};
  await c.checkEngine('kokoro');
  assert.deepEqual(calls.map(call => call.url), ['/api/engines/kokoro/check', '/api/engines/status']);
  assert.equal(JSON.stringify(calls[0].body), '{}');
  assert.equal(calls[0].turboDisabledNow, true);
  assert.equal(c.modelCards.kokoro.button.textContent, 'Check again');
  assert.equal(c.modelCards.turbo.button.disabled, false);
});

test('a failed check reports its error and still refreshes the status', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'check_failed', installation: {missing: []},
      check: {status: 'failed', checked_at: 't', reason: 'worker exited'}}]};
  const {context: c, fields, calls} = harness({'/api/engines/kokoro/check': new Error('worker exited'),
    '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.checkEngine('kokoro');
  assert.equal(fields.err.textContent, 'Check did not finish: worker exited');
  assert.equal(calls[calls.length - 1].url, '/api/engines/status');
  assert.equal(c.modelCards.kokoro.state.textContent, 'Check failed on CPU');
});
