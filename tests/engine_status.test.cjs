// Run with node --test tests/engine_status.test.cjs. Exercises the Models page engine tests and installs in serve.html.
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
  const item = {tag, children: [], className: '', disabled: false, hidden: false, colSpan: 0, handlers: {}, parentNode: null,
    appendChild(child) {    // as the DOM does: a node that is added somewhere else leaves where it was
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(other => other !== child);
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    addEventListener(type, handler) { this.handlers[type] = handler; }};
  let text = '';
  Object.defineProperty(item, 'textContent', {
    get() { return text + this.children.map(child => child.textContent).join(''); },
    set(value) { text = String(value); this.children = []; },
  });
  return item;
}

function harness(responses = {}) {
  const fields = {}, calls = [], timers = [];
  const context = {
    document: {createElement: element},
    $: id => fields[id] || (fields[id] = element(id)),
    setTimeout(handler, delay) { timers.push({handler, delay}); return timers.length; },
    fetchJson(url, body) {
      calls.push({url, body, turboDisabledNow: context.modelCards && context.modelCards.turbo
        ? context.modelCards.turbo.button.disabled : null});
      const value = responses[url] ?? {};
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    },
  };
  vm.createContext(context);
  vm.runInContext(sourceBetween('var modelCards={};', 'var castCharacters = [];'), context);
  return {context, fields, calls, timers};
}

function card(name) {
  const view = {name, card: element('card'), state: element('state'), details: element('details'), ram: element('ram'),
    button: {disabled: false, hidden: false, textContent: ''}, box: element('box'), plan: element('plan'),
    go: element('go'), stop: element('stop'), log: element('log'), action: 'test', planOpen: false};
  view.box.hidden = view.log.hidden = true;    // as renderModelOverview makes them
  return view;
}

const KOKORO_FILES = ['.venv/Scripts/python.exe', 'hf-cache/snapshot'];
const missingKokoro = {id: 'kokoro', state: 'not_installed', check: null, min_free_ram_gb: 8,
  installation: {installed: false, missing: KOKORO_FILES, installer: 'setup-kokoro.bat'}};
const readyKokoro = {id: 'kokoro', state: 'not_checked', check: null, min_free_ram_gb: 8, installation: {installed: true, missing: []}};

function job(extra = {}) {
  return {engine: 'kokoro', name: 'Kokoro 82M', state: 'running', started_at: '2026-10-29T10:00:00+00:00', ended_at: null,
    seconds: 65, exit_code: null, lines: ['one', 'two'], log: 'out/install/kokoro-1.log', ...extra};
}

test('state text names the device and keeps install and conversion states apart', () => {
  const {context: c} = harness();
  assert.equal(c.engineStateText({state: 'usable'}, 'cpu'), 'Speaks now on CPU');
  assert.equal(c.engineStateText({state: 'usable'}, 'cuda'), 'Speaks now on GPU');
  assert.equal(c.engineStateText({state: 'check_failed'}, 'cpu'), 'Test failed on CPU');
  assert.equal(c.engineStateText({state: 'not_checked'}, 'cuda'), 'Installed, not tested on GPU');
  assert.equal(c.engineStateText({state: 'check_outdated'}, 'cuda'), 'Installed, test out of date on GPU');
  assert.equal(c.engineStateText({state: 'not_installed'}, 'cpu'), 'Not installed');
  assert.equal(c.engineStateText({state: 'conversion_task'}, 'cpu'), 'Conversion task');
  assert.equal(c.engineStateText({state: 'not_integrated'}, 'cpu'), 'Not integrated');
  assert.equal(c.engineStateText({state: 'not_installed'}, 'cpu', job()), 'Installing…');
});

test('detail text lists what is missing and what the last test measured', () => {
  const {context: c} = harness();
  assert.equal(c.engineDetailText({state: 'not_installed', installation: {missing: ['.venv/python.exe', 'hf/snapshot']}}, 'cpu'),
    'The app cannot install this model yet: docs/SETUP.md (Model runtimes) says what it needs. Missing: .venv/python.exe; hf/snapshot.');
  assert.equal(c.engineDetailText({state: 'not_checked', check: null}, 'cpu'),
    'Press Test to load the model and speak one short sentence on this device.');
  const passed = {state: 'usable', check: {status: 'passed', checked_at: '2026-10-22T10:00:00+00:00',
    total_s: 0.5, audio_seconds: 2.5, ran_on: 'Vulkan1'}};
  assert.equal(c.engineDetailText(passed, 'cpu'),
    'Tested 2026-10-22 10:00 UTC: 0.5 s to make 2.5 s of audio, on Vulkan1.');
  assert.equal(c.engineDetailText({state: 'check_failed', check: {status: 'failed', checked_at: '2026-10-22T11:30:00+00:00', reason: 'boom'}}, 'cpu'),
    'Failed 2026-10-22 11:30 UTC: boom');
  assert.equal(c.engineDetailText({state: 'conversion_task'}, 'cpu'),
    'Tested from Voice conversion, because it needs source audio.');
  assert.match(c.engineDetailText({state: 'check_outdated', check: {status: 'passed', checked_at: '2026-10-22T10:00:00+00:00'}}, 'cpu'),
    /last test \(2026-10-22 10:00 UTC\) was made before this model or its packages changed.*Press Test again/);
});

test('a missing engine with an installer is told to press Install, and one without is told the app cannot install it', () => {
  const {context: c} = harness();
  const missing = ['hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/f3ff'];
  assert.equal(c.engineDetailText({state: 'not_installed', installation: {missing, installer: 'setup-kokoro.bat'}}, 'cpu'),
    'Missing: hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/f3ff. Press Install to add it to this folder, or run '
    + 'setup-kokoro.bat in the lab folder. docs/SETUP.md says what it installs.');
  assert.match(c.engineDetailText({state: 'not_installed', installation: {missing}}, 'cpu'),
    /^The app cannot install this model yet: .* Missing: hf-cache\/hub\/models--hexgrad--Kokoro-82M\/snapshots\/f3ff\.$/);
  assert.doesNotMatch(c.engineDetailText({state: 'not_checked', check: null, installation: {missing: [], installer: 'setup-kokoro.bat'}}, 'cpu'),
    /setup-kokoro|Install/);
});

test('an install in progress and a failed one are described with their time, exit code and log', () => {
  const {context: c} = harness();
  const row = {state: 'not_installed', installation: {missing: ['x'], installer: 'setup-kokoro.bat'}};
  assert.equal(c.elapsedText(0), '0 s');
  assert.equal(c.elapsedText(45.4), '45 s');
  assert.equal(c.elapsedText(60), '1 min 0 s');
  assert.equal(c.engineDetailText(row, 'cpu', job()),
    'Installing for 1 min 5 s. Nothing else can run until it finishes, and downloads and packages can take several minutes. '
    + 'The end of its output is below.');
  assert.equal(c.engineDetailText(row, 'cpu', job({state: 'failed', exit_code: 1, seconds: 123})),
    'Missing: x. The install stopped (exit code 1) after 2 min 3 s. The end of its output is below, and all of it is in '
    + 'out/install/kokoro-1.log. Press Install to try again: it continues where it stopped.');
});

test('the RAM line says what a model needs and warns when this computer cannot or does not have it', () => {
  const {context: c} = harness();
  assert.deepEqual({...c.ramText({min_free_ram_gb: 8}, {total_gb: 15.7, free_gb: 9.4})},
    {text: 'Needs 8 GB of free RAM to load.', short: false});
  assert.deepEqual({...c.ramText({min_free_ram_gb: 10}, {total_gb: 8, free_gb: 5})},
    {text: 'Needs 10 GB of free RAM to load. This computer has only 8 GB of RAM, so it cannot load this model.', short: true});
  assert.deepEqual({...c.ramText({min_free_ram_gb: 10}, {total_gb: 16, free_gb: 6.5})},
    {text: 'Needs 10 GB of free RAM to load. Only 6.5 GB is free right now; close other programs before you use it.', short: true});
  assert.deepEqual({...c.ramText({min_free_ram_gb: 26}, null)}, {text: 'Needs 26 GB of free RAM to load.', short: false});
  assert.deepEqual({...c.ramText({min_free_ram_gb: null}, {total_gb: 16, free_gb: 9})}, {text: '', short: false});
  assert.deepEqual({...c.ramText({}, {total_gb: 16, free_gb: 9})}, {text: '', short: false});
});

test('every card shows its RAM need and the section says how much this computer has', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro'), breeze: card('Breeze')};
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], ram: {total_gb: 15.7, free_gb: 9.4}, engines: [
    readyKokoro,
    {id: 'breeze', state: 'not_installed', check: null, min_free_ram_gb: 12, installation: {missing: ['x']}}]});
  assert.equal(c.modelCards.kokoro.ram.textContent, 'Needs 8 GB of free RAM to load.');
  assert.equal(c.modelCards.kokoro.ram.className, 'meta');
  assert.equal(c.modelCards.breeze.ram.textContent, 'Needs 12 GB of free RAM to load. Only 9.4 GB is free right now; close other programs before you use it.');
  assert.equal(c.modelCards.breeze.ram.className, 'meta warn');
  assert.equal(fields.modelsRam.textContent, 'This computer has 15.7 GB of RAM, 9.4 GB free right now.');
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [{id: 'kokoro', state: 'not_checked', installation: {missing: []}}]});
  assert.equal(c.modelCards.kokoro.ram.textContent, '');
  assert.equal(fields.modelsRam.textContent, '');
});

test('a missing Kokoro card offers Install, and a missing card without an installer has no button', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro'), breeze: card('Breeze')};
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [
    missingKokoro, {id: 'breeze', state: 'not_installed', check: null, installation: {installed: false, missing: ['x']}}]});
  assert.equal(c.modelCards.kokoro.state.textContent, 'Not installed');
  assert.match(c.modelCards.kokoro.details.textContent,
    /^Missing: \.venv\/Scripts\/python\.exe; hf-cache\/snapshot\. Press Install to add it to this folder, or run setup-kokoro\.bat/);
  assert.equal(c.modelCards.kokoro.action, 'install');
  assert.equal(c.modelCards.kokoro.button.textContent, 'Install');
  assert.equal(c.modelCards.kokoro.button.hidden, false);
  assert.equal(c.modelCards.kokoro.button.disabled, false);
  assert.equal(c.modelCards.breeze.action, 'none');
  assert.equal(c.modelCards.breeze.button.hidden, true);
  assert.equal(c.modelCards.breeze.button.disabled, true);
  assert.match(c.modelCards.breeze.details.textContent, /The app cannot install this model yet/);
});

test('an out-of-date test does not claim the engine speaks and still offers a new test', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus({device: 'cuda', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'check_outdated', check_outdated: true, installation: {missing: []},
      check: {status: 'passed', checked_at: '2026-10-22T10:00:00+00:00', total_s: 0.5, audio_seconds: 2, ran_on: 'cuda'}}]});
  assert.equal(c.modelCards.kokoro.state.textContent, 'Installed, test out of date on GPU');
  assert.doesNotMatch(c.modelCards.kokoro.state.textContent, /Speaks now/);
  assert.equal(c.modelCards.kokoro.button.disabled, false);
  assert.equal(c.modelCards.kokoro.button.textContent, 'Test again');
});

test('a busy service disables every button, and usable engines offer a new test', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro'), breeze: card('Breeze'), turbo: card('Turbo')};
  c.renderEngineStatus({device: 'cpu', busy: true, speed: [], engines: [
    {id: 'kokoro', state: 'not_checked', installation: {missing: []}, check: null},
    {id: 'breeze', state: 'not_installed', installation: {missing: ['x']}, check: null},
    {id: 'turbo', state: 'not_installed', installation: {missing: ['y'], installer: 'setup-turbo.bat'}, check: null}]});
  assert.equal(c.modelCards.kokoro.button.disabled, true);
  assert.equal(c.modelCards.breeze.button.disabled, true);
  assert.equal(c.modelCards.turbo.button.disabled, true);
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'usable', installation: {missing: []},
      check: {status: 'passed', checked_at: 't', total_s: 0.5, audio_seconds: 2, ran_on: 'cpu'}},
    {id: 'breeze', state: 'not_installed', installation: {missing: ['x']}, check: null}]});
  assert.equal(c.modelCards.kokoro.button.disabled, false);
  assert.equal(c.modelCards.kokoro.button.textContent, 'Test again');
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

test('model cards keep integration apart from the test button, and conversion has none', () => {
  const {context: c, fields} = harness();
  c.renderModelOverview({engines: [
    {id: 'kokoro', name: 'Kokoro 82M', integration: {service_api: true}, capabilities: {voice_selection: {status: 'supported'}}},
    {id: 'chatterbox-vc', name: 'Chatterbox VC', integration: {service_api: true, task_only: true}, capabilities: {}}]});
  const [kokoro, conversion] = fields.modelOverview.children;
  assert.equal(kokoro.children[1].textContent, 'Integrated');
  const button = kokoro.children.find(child => child.tag === 'button');
  assert.equal(button.textContent, 'Test');
  assert.equal(kokoro.children.filter(child => child.tag === 'button').length, 1);
  assert.equal(c.modelCards.kokoro.box.hidden, true);
  assert.equal(c.modelCards.kokoro.log.hidden, true);
  assert.equal(c.modelCards.kokoro.go.textContent, 'Install now');
  assert.equal(c.modelCards.kokoro.stop.textContent, 'Cancel');
  assert.equal(conversion.children[1].textContent, 'Integrated · conversion');
  assert.equal(conversion.children.some(child => child.tag === 'button'), false);
});

test('a test posts once, holds the other cards while it runs, then refreshes the status', async () => {
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
  assert.equal(c.modelCards.kokoro.button.textContent, 'Test again');
  assert.equal(c.modelCards.turbo.button.disabled, false);
});

test('a failed test reports its error and still refreshes the status', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [
    {id: 'kokoro', state: 'check_failed', installation: {missing: []},
      check: {status: 'failed', checked_at: 't', reason: 'worker exited'}}]};
  const {context: c, fields, calls} = harness({'/api/engines/kokoro/check': new Error('worker exited'),
    '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.checkEngine('kokoro');
  assert.equal(fields.err.textContent, 'Test did not finish: worker exited');
  assert.equal(calls[calls.length - 1].url, '/api/engines/status');
  assert.equal(c.modelCards.kokoro.state.textContent, 'Test failed on CPU');
});

test('Install reads the plan first, shows its paragraphs and problems, and keeps the button out of the way', async () => {
  const plan = {engine: 'kokoro', ok: true, paragraphs: ['Kokoro 82M on the CPU', '1. Python packages: 89 to install.'], problems: []};
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c, calls} = harness({'/api/engines/kokoro/install': {plan}, '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus(status);
  await c.planInstall('kokoro');
  assert.deepEqual(calls.map(call => [call.url, JSON.stringify(call.body)]),
    [['/api/engines/kokoro/install', '{"dry_run":true}'], ['/api/engines/status', undefined]]);
  const view = c.modelCards.kokoro;
  assert.deepEqual(view.plan.children.map(child => child.textContent), plan.paragraphs);
  assert.equal(view.box.hidden, false);
  assert.equal(view.go.hidden, false);
  assert.equal(view.stop.textContent, 'Cancel');
  assert.equal(view.button.hidden, true);
  assert.equal(view.card.className, 'model-card wide');
  await c.closePlan('kokoro');
  assert.equal(view.box.hidden, true);
  assert.equal(view.button.hidden, false);
  assert.equal(view.button.textContent, 'Install');
  assert.equal(view.card.className, 'model-card');
});

test('a plan that cannot go ahead shows why and offers only to close', async () => {
  const plan = {engine: 'kokoro', ok: false, paragraphs: ['Kokoro 82M on the CPU'],
    problems: ['This computer cannot take the plan yet:', '- About 3 GB of free disk space is needed here.']};
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c} = harness({'/api/engines/kokoro/install': {plan}, '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.planInstall('kokoro');
  const view = c.modelCards.kokoro;
  assert.equal(view.go.hidden, true);
  assert.equal(view.stop.textContent, 'Close');
  assert.deepEqual(view.plan.children.map(child => [child.className, child.textContent]),
    [['meta', 'Kokoro 82M on the CPU'], ['meta warn', 'This computer cannot take the plan yet:'],
      ['meta warn', '- About 3 GB of free disk space is needed here.']]);
});

test('a plan that could not be read says so', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c, fields} = harness({'/api/engines/kokoro/install': new Error('The lab\'s Python is missing'), '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.planInstall('kokoro');
  assert.equal(fields.err.textContent, "Could not read the install plan: The lab's Python is missing");
  assert.equal(c.modelCards.kokoro.box.hidden, true);
});

test('Install now posts once and then shows the running install with the end of its output', async () => {
  const running = {device: 'cpu', busy: true, speed: [], install: job({lines: Array.from({length: 20}, (_, index) => `line ${index + 1}`)}),
    engines: [missingKokoro]};
  const {context: c, calls, timers} = harness({'/api/engines/kokoro/install': {install: running.install}, '/api/engines/status': running});
  c.modelCards = {kokoro: card('Kokoro')};
  c.modelCards.kokoro.planOpen = true;
  c.modelCards.kokoro.box.hidden = false;
  await c.startInstall('kokoro');
  assert.deepEqual(calls.map(call => [call.url, JSON.stringify(call.body)]),
    [['/api/engines/kokoro/install', '{}'], ['/api/engines/status', undefined]]);
  const view = c.modelCards.kokoro;
  assert.equal(view.state.textContent, 'Installing…');
  assert.match(view.details.textContent, /^Installing for 1 min 5 s\./);
  assert.equal(view.button.hidden, true);
  assert.equal(view.box.hidden, true);
  assert.equal(view.log.hidden, false);
  assert.equal(view.log.textContent, Array.from({length: 12}, (_, index) => `line ${index + 9}`).join('\n'));
  assert.equal(view.card.className, 'model-card wide');
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 1500);
});

test('the install output follows its newest line unless the reader has scrolled up', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro')};
  const log = c.modelCards.kokoro.log;
  Object.defineProperty(log, 'scrollHeight', {get() {
    const text = this.textContent;
    return Math.max(100, text ? 20 * text.split('\n').length : 0);
  }});
  log.clientHeight = 100;
  log.scrollTop = 0;
  const numbered = count => Array.from({length: count}, (_, index) => `line ${index + 1}`);
  const render = count => c.renderEngineStatus({device: 'cpu', busy: true, speed: [], install: job({lines: numbered(count)}),
    engines: [missingKokoro]});
  render(20);
  assert.equal(log.scrollTop, log.scrollHeight);
  log.scrollTop = 20;
  render(21);
  assert.equal(log.scrollTop, 20);
  log.scrollTop = log.scrollHeight - log.clientHeight;
  render(22);
  assert.equal(log.scrollTop, log.scrollHeight);
});

test('a running install is polled again, once at a time, until it ends', async () => {
  const running = {device: 'cpu', busy: true, speed: [], install: job(), engines: [missingKokoro]};
  const done = {device: 'cpu', busy: false, speed: [], install: job({state: 'finished', exit_code: 0, ended_at: 'x'}),
    engines: [readyKokoro]};
  const {context: c, calls, timers} = harness({'/api/engines/status': running});
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus(running);
  c.renderEngineStatus(running);
  assert.equal(timers.length, 1);
  await timers[0].handler();
  assert.equal(calls.filter(call => call.url === '/api/engines/status').length, 1);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.length, 2);
  c.renderEngineStatus(done);
  assert.equal(timers.length, 2);
  assert.equal(c.modelCards.kokoro.state.textContent, 'Installed, not tested on CPU');
  assert.equal(c.modelCards.kokoro.log.hidden, true);
  assert.equal(c.modelCards.kokoro.log.textContent, '');    // so a log shown later starts empty and follows its newest line
  assert.equal(c.modelCards.kokoro.card.className, 'model-card');
  assert.equal(c.modelCards.kokoro.button.textContent, 'Test');
  assert.equal(c.modelCards.kokoro.button.hidden, false);
});

test('a failed install keeps the end of its output on the card and offers Install again', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [missingKokoro],
    install: job({state: 'failed', exit_code: 1, seconds: 123, lines: ['error: pip stopped with exit code 1.']})});
  const view = c.modelCards.kokoro;
  assert.equal(view.state.textContent, 'Not installed');
  assert.match(view.details.textContent, /The install stopped \(exit code 1\) after 2 min 3 s\./);
  assert.equal(view.log.hidden, false);
  assert.equal(view.log.textContent, 'error: pip stopped with exit code 1.');
  assert.equal(view.card.className, 'model-card wide');
  assert.equal(view.button.textContent, 'Install');
  assert.equal(view.button.hidden, false);
  assert.equal(view.button.disabled, false);
});

test('another card is held while an install runs, and shows nothing of it', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro'), turbo: card('Turbo')};
  c.renderEngineStatus({device: 'cpu', busy: true, speed: [], install: job(), engines: [
    missingKokoro, {id: 'turbo', state: 'not_checked', installation: {missing: []}, check: null, min_free_ram_gb: 10}]});
  assert.equal(c.modelCards.turbo.state.textContent, 'Installed, not tested on CPU');
  assert.equal(c.modelCards.turbo.button.disabled, true);
  assert.equal(c.modelCards.turbo.log.hidden, true);
  assert.equal(c.modelCards.turbo.log.textContent, '');
  assert.equal(c.modelCards.turbo.card.className, 'model-card');
});

test('an install that could not start says so and still refreshes the status', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c, fields, calls} = harness({'/api/engines/kokoro/install': new Error('The GPU worker is busy while a clip is made.'),
    '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.startInstall('kokoro');
  assert.equal(fields.err.textContent, 'The install did not start: The GPU worker is busy while a clip is made.');
  assert.equal(calls[calls.length - 1].url, '/api/engines/status');
  assert.equal(c.modelCards.kokoro.go.disabled, false);
});

const NAMED = {name: 'Kokoro 82M', integrated: true};
const kokoroMissing = {...missingKokoro, ...NAMED};
const kokoroReady = {...readyKokoro, ...NAMED};
const manual = (id, name, extra = {}) => ({id, name, integrated: true, state: 'not_installed', check: null, min_free_ram_gb: 12,
  installation: {installed: false, missing: [`.venv-${id}/Scripts/python.exe`]}, ...extra});
const snapshot = (engines, extra = {}) => ({device: 'cpu', busy: false, speed: [], engines, ...extra});

test('a model the app cannot install goes under More models, which counts them, and comes back when it is installed', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M'), breeze: card('Breeze'), qwen: card('Qwen')};
  c.renderEngineStatus(snapshot([kokoroMissing, manual('breeze', 'Breeze')]));
  assert.equal(c.modelCards.kokoro.card.parentNode, fields.modelOverview);
  assert.equal(c.modelCards.breeze.card.parentNode, fields.modelMore);
  assert.equal(fields.modelsMoreBox.hidden, false);
  assert.equal(fields.modelsMoreSummary.textContent, '1 more model needs manual setup');
  c.renderEngineStatus(snapshot([kokoroMissing, manual('breeze', 'Breeze'), manual('qwen', 'Qwen')]));
  assert.equal(fields.modelsMoreSummary.textContent, '2 more models need manual setup');
  assert.deepEqual(fields.modelMore.children, [c.modelCards.breeze.card, c.modelCards.qwen.card]);
  c.renderEngineStatus(snapshot([kokoroMissing, manual('breeze', 'Breeze', {state: 'not_checked', installation: {installed: true, missing: []}}),
    manual('qwen', 'Qwen')]));
  assert.equal(c.modelCards.breeze.card.parentNode, fields.modelOverview);
  assert.deepEqual(fields.modelMore.children, [c.modelCards.qwen.card]);
  assert.equal(fields.modelsMoreSummary.textContent, '1 more model needs manual setup');
});

test('the More models fold stays hidden while every model is installed or can be installed', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M')};
  c.renderEngineStatus(snapshot([kokoroMissing]));
  assert.equal(fields.modelsMoreBox.hidden, true);
  assert.equal(c.modelCards.kokoro.card.parentNode, fields.modelOverview);
});

test('a studio with no model installed says so and points at the card that can install one', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M'), breeze: card('Breeze')};
  const none = snapshot([kokoroMissing, manual('breeze', 'Breeze')]);
  c.renderEngineStatus(none);
  assert.equal(fields.firstRun.hidden, false);
  assert.equal(fields.firstRunTitle.textContent, 'No voice model is installed yet');
  assert.equal(fields.firstRunText.textContent, 'Press Install on the Kokoro 82M card in Models & features. It shows what it will do first.');
  c.renderEngineStatus({...none, busy: true, install: job()});
  assert.equal(fields.firstRunTitle.textContent, 'Installing Kokoro 82M…');
  assert.equal(fields.firstRunText.textContent, 'Follow it in Models & features. Nothing can speak until it finishes.');
  const conversionOnly = {id: 'vc', name: 'Chatterbox Voice Conversion', integrated: true, task_only: true, state: 'conversion_task',
    installation: {installed: true, missing: []}};
  c.modelCards.vc = card('Chatterbox Voice Conversion');
  c.renderEngineStatus(snapshot([kokoroMissing, conversionOnly]));
  assert.equal(fields.firstRun.hidden, false);    // a conversion tool cannot speak a line
  c.renderEngineStatus(snapshot([kokoroReady, manual('breeze', 'Breeze')]));
  assert.equal(fields.firstRun.hidden, true);
});

test('the model pickers mark what is not installed, and the studio picker says what to do about it', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M'), breeze: card('Breeze TTS 2 BF16')};
  const options = () => [{value: 'kokoro', textContent: 'Kokoro 82M'}, {value: 'breeze', textContent: 'Breeze TTS 2 BF16'},
    {value: 'future', textContent: 'Future (not integrated yet)'}];
  fields.engine = Object.assign(element('engine'), {options: options(), value: 'breeze'});
  fields.identityEngine = Object.assign(element('identityEngine'), {options: options().slice(0, 2), value: 'kokoro'});
  fields.cloneEngine = Object.assign(element('cloneEngine'), {options: options().slice(1, 2), value: 'breeze'});
  c.renderEngineStatus(snapshot([kokoroReady, manual('breeze', 'Breeze TTS 2 BF16')]));
  const labels = select => select.options.map(option => option.textContent);
  assert.deepEqual(labels(fields.engine), ['Kokoro 82M', 'Breeze TTS 2 BF16 (not installed)', 'Future (not integrated yet)']);
  assert.deepEqual(labels(fields.identityEngine), ['Kokoro 82M', 'Breeze TTS 2 BF16 (not installed)']);
  assert.deepEqual(labels(fields.cloneEngine), ['Breeze TTS 2 BF16 (not installed)']);
  assert.equal(fields.engineHint.hidden, false);
  assert.equal(fields.engineHint.textContent,
    'This model is not installed, so Generate cannot use it yet. See what it needs in Models & features');
  assert.equal(fields.engineHint.children[1].href, '#models');
  fields.engine.value = 'kokoro';
  c.showEngineHint();
  assert.equal(fields.engineHint.hidden, true);
  assert.equal(fields.engineHint.textContent, '');
  c.renderEngineStatus(snapshot([kokoroMissing, manual('breeze', 'Breeze TTS 2 BF16', {state: 'not_checked',
    installation: {installed: true, missing: []}})]));
  assert.equal(labels(fields.engine)[1], 'Breeze TTS 2 BF16');
  assert.equal(labels(fields.engine)[0], 'Kokoro 82M (not installed)');
  assert.equal(fields.engineHint.textContent,
    'This model is not installed, so Generate cannot use it yet. Install it in Models & features');
});

test('the page has the line that says how much RAM this computer has', () => {
  assert.match(page, /id="modelsRam"/);
});
