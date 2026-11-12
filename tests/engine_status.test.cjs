// Run with node --test tests/engine_status.test.cjs. Exercises the Models page cards, their GPU/CPU switch, engine tests and the
// install window in serve.html.
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
    attributes: {},
    appendChild(child) {    // as the DOM does: a node that is added somewhere else leaves where it was
      if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(other => other !== child);
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    addEventListener(type, handler) { this.handlers[type] = handler; }};
  let text = '';
  Object.defineProperty(item, 'textContent', {
    get() { return text + this.children.map(child => child.textContent).join(''); },
    set(value) { text = String(value); this.children = []; },
  });
  return item;
}

function dialog() {
  return Object.assign(element('installDialog'), {open: false,
    showModal() { this.open = true; },
    close() { this.open = false; if (this.handlers.close) this.handlers.close(); }});
}

function harness(responses = {}) {
  const fields = {installDialog: dialog()}, calls = [], timers = [];
  const context = {
    document: {createElement: element},
    $: id => fields[id] || (fields[id] = element(id)),
    setTimeout(handler, delay) { timers.push({handler, delay}); return timers.length; },
    fetchJson(url, body) {
      calls.push({url, body, turboDisabledNow: context.modelCards && context.modelCards.turbo
        ? context.modelCards.turbo.button.disabled : null});
      const value = typeof responses[url] === 'function' ? responses[url](body) : responses[url] ?? {};
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    },
  };
  vm.createContext(context);
  vm.runInContext(sourceBetween('var modelCards={};', 'var castCharacters = [];'), context);
  context.wireInstallDialog();
  return {context, fields, calls, timers};
}

function card(name) {
  const radios = {gpu: Object.assign(element('input'), {type: 'radio', value: 'gpu', checked: false}),
    cpu: Object.assign(element('input'), {type: 'radio', value: 'cpu', checked: false})};
  const devices = element('devices');
  for (const value of ['gpu', 'cpu']) element('label').appendChild(radios[value]);
  return {name, card: element('card'), state: element('state'), details: element('details'), ram: element('ram'), devices, radios,
    note: element('note'), button: element('button'), extraButton: element('button'), action: 'test', extra: null,
    installGpu: false, pendingDevice: false, deviceError: ''};
}

const KOKORO_FILES = ['.venv/Scripts/python.exe', 'hf-cache/snapshot'];
const missingKokoro = {id: 'kokoro', state: 'not_installed', check: null, min_free_ram_gb: 8,
  installation: {installed: false, missing: KOKORO_FILES, installer: 'setup-kokoro.bat'}};
const readyKokoro = {id: 'kokoro', state: 'not_checked', check: null, min_free_ram_gb: 8, installation: {installed: true, missing: []}};

function job(extra = {}) {
  return {engine: 'kokoro', name: 'Kokoro 82M', nvidia: false, state: 'running', started_at: '2026-10-29T10:00:00+00:00',
    ended_at: null, seconds: 65, exit_code: null, lines: ['one', 'two'], log: 'out/install/kokoro-1.log', ...extra};
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
  assert.equal(c.engineStateText({state: 'usable'}, 'cpu', job({nvidia: true})), 'Installing the GPU build…');
});

test('a model that could not use the graphics card is said to speak on the CPU, and why', () => {
  const {context: c} = harness();
  // A GPU build of PyTorch that found no card when it loaded, as on a laptop whose NVIDIA card a power-saving mode switched off.
  const fellBack = {state: 'usable', gpu: {build: 'cuda'}, check: {status: 'passed', checked_at: '2026-11-03T10:00:00+00:00',
    total_s: 0.8, audio_seconds: 2.7, ran_on: 'cpu'}};
  assert.equal(c.engineStateText(fellBack, 'cuda'), 'Speaks now on CPU');
  assert.equal(c.engineDetailText(fellBack, 'cuda'),
    'Tested 2026-11-03 10:00 UTC: 0.8 s to make 2.7 s of audio, on the CPU: the graphics card was not available when it ' +
    'loaded (on a laptop, a power-saving mode can switch the card off).');
  assert.equal(c.engineDetailText({...fellBack, gpu: {build: 'cpu'}}, 'cuda'),
    'Tested 2026-11-03 10:00 UTC: 0.8 s to make 2.7 s of audio, on the CPU: this model has the CPU build of PyTorch.');
  const onCard = {...fellBack, check: {...fellBack.check, ran_on: 'cuda'}};
  assert.equal(c.engineStateText(onCard, 'cuda'), 'Speaks now on GPU');
  assert.equal(c.engineDetailText(onCard, 'cuda'), 'Tested 2026-11-03 10:00 UTC: 0.8 s to make 2.7 s of audio.');
  assert.equal(c.engineStateText({...fellBack, state: 'check_failed'}, 'cuda'), 'Test failed on CPU');
  assert.equal(c.engineDetailText(fellBack, 'cpu'), 'Tested 2026-11-03 10:00 UTC: 0.8 s to make 2.7 s of audio.');
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
    'Missing: hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/f3ff. Install adds it to this folder, as does '
    + 'setup-kokoro.bat in the lab folder; docs/SETUP.md says what it installs.');
  assert.match(c.engineDetailText({state: 'not_installed', installation: {missing}}, 'cpu'),
    /^The app cannot install this model yet: .* Missing: hf-cache\/hub\/models--hexgrad--Kokoro-82M\/snapshots\/f3ff\.$/);
  assert.doesNotMatch(c.engineDetailText({state: 'not_checked', check: null, installation: {missing: [], installer: 'setup-kokoro.bat'}}, 'cpu'),
    /setup-kokoro|Install/);
});

test('a running install leaves the text of its card as it was, and a failed one says where its output went', () => {
  const {context: c} = harness();
  const row = {state: 'not_installed', installation: {missing: ['x'], installer: 'setup-kokoro.bat'}};
  assert.equal(c.elapsedText(0), '0 s');
  assert.equal(c.elapsedText(45.4), '45 s');
  assert.equal(c.elapsedText(60), '1 min 0 s');
  assert.equal(c.engineDetailText(row, 'cpu', job()), c.engineDetailText(row, 'cpu', null));
  assert.equal(c.engineDetailText(row, 'cpu', job({seconds: 900})), c.engineDetailText(row, 'cpu', job()));
  const usable = {state: 'usable', check: {status: 'passed', checked_at: 't', total_s: 1, audio_seconds: 2, ran_on: 'cpu'}};
  assert.equal(c.engineDetailText(usable, 'cpu', job({nvidia: true})), c.engineDetailText(usable, 'cpu', null));
  assert.equal(c.engineDetailText(row, 'cpu', job({state: 'failed', exit_code: 1, seconds: 123})),
    'Missing: x. The install stopped (exit code 1) after 2 min 3 s. Show output has the end of it, and all of it is in '
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
    /^Missing: \.venv\/Scripts\/python\.exe; hf-cache\/snapshot\. Install adds it to this folder, as does setup-kokoro\.bat/);
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

test('model cards keep integration apart from their buttons, and every card has its switch and buttons from the start', () => {
  const {context: c, fields} = harness();
  c.renderModelOverview({engines: [
    {id: 'kokoro', name: 'Kokoro 82M', integration: {service_api: true}, capabilities: {voice_selection: {status: 'supported'}}},
    {id: 'chatterbox-vc', name: 'Chatterbox VC', integration: {service_api: true, task_only: true}, capabilities: {}}]});
  const [kokoro, conversion] = fields.modelOverview.children;
  assert.equal(kokoro.children[1].textContent, 'Integrated');
  const actions = kokoro.children.find(child => child.className === 'model-actions');
  assert.deepEqual(actions.children.map(child => [child.tag, child.textContent, child.hidden]),
    [['button', 'Test', false], ['button', '', true]]);
  const devices = kokoro.children.find(child => child.className === 'device-switch');
  assert.equal(devices.attributes.role, 'radiogroup');
  assert.equal(devices.hidden, true);    // until the status says whether the service has a graphics card
  assert.deepEqual(['gpu', 'cpu'].map(value => [c.modelCards.kokoro.radios[value].type, c.modelCards.kokoro.radios[value].name]),
    [['radio', 'device-kokoro'], ['radio', 'device-kokoro']]);
  assert.equal(conversion.children[1].textContent, 'Integrated · conversion');
  // A conversion task has no test, but it has an Install button for when it is missing.
  assert.ok(conversion.children.find(child => child.className === 'model-actions').children.some(child => child.tag === 'button'));
  assert.equal(page.includes('model-card wide'), false);
});

const plan = {engine: 'kokoro', nvidia: false, ok: true, paragraphs: ['Kokoro 82M on the CPU', '1. Python packages: 89 to install.'],
  problems: []};

test('Install opens the install window with the plan, and the card itself does not change size', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c, calls, fields} = harness({'/api/engines/kokoro/install': {plan}, '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus(status);
  const before = c.modelCards.kokoro.card.className;
  await c.planInstall('kokoro', false);
  assert.deepEqual(calls.map(call => [call.url, JSON.stringify(call.body)]),
    [['/api/engines/kokoro/install', '{"dry_run":true,"nvidia":false}'], ['/api/engines/status', undefined]]);
  assert.equal(fields.installDialog.open, true);
  assert.equal(fields.installTitle.textContent, 'Install Kokoro');
  assert.equal(fields.installState.textContent, 'This is what it will do. Nothing has changed yet.');
  assert.deepEqual(fields.installPlan.children.map(child => child.textContent), plan.paragraphs);
  assert.equal(fields.installGo.hidden, false);
  assert.equal(fields.installClose.textContent, 'Cancel');
  const view = c.modelCards.kokoro;
  assert.equal(view.card.className, before);
  assert.equal(view.button.hidden, false);
  assert.equal(view.button.textContent, 'Install');
  fields.installClose.handlers.click();
  assert.equal(fields.installDialog.open, false);
});

test('a plan that cannot go ahead shows why and offers only to close', async () => {
  const refused = {engine: 'kokoro', ok: false, paragraphs: ['Kokoro 82M on the CPU'],
    problems: ['This computer cannot take the plan yet:', '- About 3 GB of free disk space is needed here.']};
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c, fields} = harness({'/api/engines/kokoro/install': {plan: refused}, '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.planInstall('kokoro', false);
  assert.equal(fields.installGo.hidden, true);
  assert.equal(fields.installClose.textContent, 'Close');
  assert.equal(fields.installState.textContent, 'This computer cannot take the plan yet.');
  assert.deepEqual(fields.installPlan.children.map(child => [child.className, child.textContent]),
    [['meta', 'Kokoro 82M on the CPU'], ['meta warn', 'This computer cannot take the plan yet:'],
      ['meta warn', '- About 3 GB of free disk space is needed here.']]);
});

test('a plan that could not be read says so in the install window', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c, fields} = harness({'/api/engines/kokoro/install': new Error('The lab\'s Python is missing'), '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.planInstall('kokoro', false);
  assert.equal(fields.installState.textContent, "Could not read the install plan: The lab's Python is missing");
  assert.equal(fields.installGo.hidden, true);
  assert.equal(fields.installClose.textContent, 'Close');
});

test('Install now posts once, then the window follows the install while the card keeps the same text', async () => {
  const lines = Array.from({length: 40}, (_, index) => `line ${index + 1}`);
  const running = {device: 'cpu', busy: true, speed: [], install: job({lines}), engines: [missingKokoro]};
  const {context: c, calls, fields, timers} = harness({'/api/engines/kokoro/install': body => body.dry_run ? {plan} : {install: running.install},
    '/api/engines/status': running});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.planInstall('kokoro', false);
  await c.startInstall();
  assert.deepEqual(calls.filter(call => !call.url.endsWith('status')).map(call => JSON.stringify(call.body)),
    ['{"dry_run":true,"nvidia":false}', '{"nvidia":false}']);
  assert.equal(fields.installTitle.textContent, 'Installing Kokoro 82M');
  assert.match(fields.installState.textContent, /^Running for 1 min 5 s\./);
  assert.equal(fields.installLog.hidden, false);
  assert.equal(fields.installLog.textContent, lines.join('\n'));
  assert.equal(fields.installClose.textContent, 'Hide');
  const view = c.modelCards.kokoro;
  const shown = () => [view.state.textContent, view.details.textContent, view.button.textContent, view.button.hidden,
    view.button.disabled, view.extraButton.hidden, view.card.className];
  const first = shown();
  assert.deepEqual(first, ['Installing…', c.engineDetailText(missingKokoro, 'cpu', null), 'Show progress', false, false, true, '']);
  c.renderEngineStatus({...running, install: job({seconds: 125, lines: [...lines, 'line 41']})});
  assert.deepEqual(shown(), first);    // a poll moves the window's text, never the card's
  assert.match(fields.installState.textContent, /^Running for 2 min 5 s\./);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, 1500);
});

test('Show progress reopens the window for an install that is running', async () => {
  const running = {device: 'cpu', busy: true, speed: [], install: job(), engines: [missingKokoro]};
  const {context: c, fields} = harness({'/api/engines/status': running});
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus(running);
  assert.equal(c.modelCards.kokoro.action, 'progress');
  await c.showProgress('kokoro');
  assert.equal(fields.installDialog.open, true);
  assert.equal(fields.installLog.textContent, 'one\ntwo');
  assert.match(fields.installState.textContent, /^Running for 1 min 5 s/);
});

test('the install output follows its newest line unless the reader has scrolled up', async () => {
  const {context: c, fields} = harness({'/api/engines/status': {device: 'cpu', busy: true, speed: [], install: job(), engines: [missingKokoro]}});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.showProgress('kokoro');
  const log = fields.installLog;
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

test('a running install is polled again, once at a time, and the window says when it has finished', async () => {
  const running = {device: 'cpu', busy: true, speed: [], install: job(), engines: [missingKokoro]};
  const done = {device: 'cpu', busy: false, speed: [], install: job({state: 'finished', exit_code: 0, ended_at: 'x', seconds: 200}),
    engines: [readyKokoro]};
  const {context: c, calls, timers, fields} = harness({'/api/engines/status': running});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.showProgress('kokoro');
  c.renderEngineStatus(running);
  assert.equal(timers.length, 1);
  await timers[0].handler();
  assert.equal(calls.filter(call => call.url === '/api/engines/status').length, 2);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(timers.length, 2);
  c.renderEngineStatus(done);
  assert.equal(timers.length, 2);
  assert.equal(c.modelCards.kokoro.state.textContent, 'Installed, not tested on CPU');
  assert.equal(c.modelCards.kokoro.button.textContent, 'Test');
  assert.equal(c.modelCards.kokoro.button.hidden, false);
  assert.equal(fields.installTitle.textContent, 'Kokoro 82M is installed');
  assert.equal(fields.installState.textContent, 'It finished in 3 min 20 s. Press Test on its card to hear it.');
  assert.equal(fields.installClose.textContent, 'Close');
});

test('a failed install offers Install again and Show output, which opens the end of its output', async () => {
  const failed = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro],
    install: job({state: 'failed', exit_code: 1, seconds: 123, lines: ['error: pip stopped with exit code 1.']})};
  const {context: c, fields} = harness({'/api/engines/status': failed});
  c.modelCards = {kokoro: card('Kokoro')};
  c.renderEngineStatus(failed);
  const view = c.modelCards.kokoro;
  assert.equal(view.state.textContent, 'Not installed');
  assert.match(view.details.textContent, /The install stopped \(exit code 1\) after 2 min 3 s\./);
  assert.deepEqual([view.button.textContent, view.button.hidden, view.button.disabled], ['Install', false, false]);
  assert.deepEqual([view.extra, view.extraButton.textContent, view.extraButton.hidden], ['output', 'Show output', false]);
  await c.showProgress('kokoro');
  assert.equal(fields.installTitle.textContent, 'Kokoro 82M did not install');
  assert.equal(fields.installLog.textContent, 'error: pip stopped with exit code 1.');
  assert.match(fields.installState.textContent, /all of it is in out\/install\/kokoro-1\.log/);
});

test('another card is held while an install runs, and shows nothing of it', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro'), turbo: card('Turbo')};
  c.renderEngineStatus({device: 'cpu', busy: true, speed: [], install: job(), engines: [
    missingKokoro, {id: 'turbo', state: 'not_checked', installation: {missing: []}, check: null, min_free_ram_gb: 10}]});
  assert.equal(c.modelCards.turbo.state.textContent, 'Installed, not tested on CPU');
  assert.equal(c.modelCards.turbo.button.disabled, true);
  assert.equal(c.modelCards.turbo.extraButton.hidden, true);
});

test('an install that could not start says so in the window and still refreshes the status', async () => {
  const status = {device: 'cpu', busy: false, speed: [], engines: [missingKokoro]};
  const {context: c, fields, calls} = harness({'/api/engines/kokoro/install': body => body.dry_run ? {plan}
    : new Error('The GPU worker is busy while a clip is made.'), '/api/engines/status': status});
  c.modelCards = {kokoro: card('Kokoro')};
  await c.planInstall('kokoro', false);
  await c.startInstall();
  assert.equal(fields.installState.textContent, 'The install did not start: The GPU worker is busy while a clip is made.');
  assert.equal(calls[calls.length - 1].url, '/api/engines/status');
  assert.equal(fields.installGo.disabled, false);
  assert.equal(fields.installGo.hidden, true);
});

// The GPU/CPU switch on each card.
function realCard(c) {    // a card made the way the page makes it, with its handlers
  c.renderModelOverview({engines: [{id: 'kokoro', name: 'Kokoro 82M', integration: {service_api: true}, capabilities: {}}]});
  return c.modelCards.kokoro;
}
const gpuService = (engines, extra = {}) => ({device: 'cuda', busy: false, speed: [], engines, ...extra});
const onCard = (switchTo, gpu, extra = {}) => ({id: 'kokoro', name: 'Kokoro 82M', integrated: true, state: 'usable', min_free_ram_gb: 8,
  installation: {installed: true, missing: []}, device: switchTo === 'gpu' && gpu.build !== 'cpu' ? 'cuda' : 'cpu',
  switch: switchTo, device_choice: null, gpu, check: {status: 'passed', checked_at: 't', total_s: 0.5, audio_seconds: 2,
    ran_on: switchTo === 'gpu' && gpu.build !== 'cpu' ? 'cuda' : 'cpu'}, ...extra});

test('the switch shows where each model runs, and the state line names that device', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M')};
  c.renderEngineStatus(gpuService([onCard('gpu', {choosable: true, reason: null, build: 'cuda', install: false})]));
  const view = c.modelCards.kokoro;
  assert.equal(view.devices.hidden, false);
  assert.deepEqual([view.radios.gpu.checked, view.radios.cpu.checked], [true, false]);
  assert.deepEqual([view.radios.gpu.disabled, view.radios.cpu.disabled], [false, false]);
  assert.equal(view.radios.gpu.parentNode.className, 'on');
  assert.equal(view.state.textContent, 'Speaks now on GPU');
  assert.equal(view.note.hidden, true);
  assert.match(fields.modelsDevice.textContent, /switch says whether that model runs on the graphics card/);
  c.renderEngineStatus(gpuService([onCard('cpu', {choosable: true, reason: null, build: 'cuda', install: false},
    {device_choice: 'cpu'})]));
  assert.deepEqual([view.radios.gpu.checked, view.radios.cpu.checked], [false, true]);
  assert.equal(view.state.textContent, 'Speaks now on CPU');
});

test('a model that cannot use the card has GPU switched off, with the reason under it', () => {
  const {context: c} = harness();
  c.modelCards = {phonon: card('Phonon')};
  c.renderEngineStatus(gpuService([{id: 'phonon', name: 'Phonon', integrated: true, state: 'usable', device: 'cuda', switch: 'cpu',
    installation: {installed: true, missing: []}, gpu: {choosable: false, reason: 'Phonon runs only on the CPU.', build: null, install: false},
    check: {status: 'passed', checked_at: 't', total_s: 0.2, audio_seconds: 2, ran_on: 'cpu'}}]));
  const view = c.modelCards.phonon;
  assert.deepEqual([view.radios.gpu.disabled, view.radios.cpu.checked], [true, true]);
  assert.equal(view.radios.gpu.parentNode.className, ' off');
  assert.deepEqual([view.note.hidden, view.note.textContent], [false, 'Phonon runs only on the CPU.']);
});

test('a service without a graphics card hides every switch and says so once', () => {
  const {context: c, fields} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M')};
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [onCard('cpu', {choosable: false,
    reason: 'This service runs every model on the CPU.', build: 'cpu', install: false})]});
  assert.equal(c.modelCards.kokoro.devices.hidden, true);
  assert.equal(c.modelCards.kokoro.note.hidden, true);
  assert.match(fields.modelsDevice.textContent, /^Every model runs on the CPU here/);
});

test('a model installed for the CPU and switched to GPU offers its GPU build, and Install of a new one follows the switch', async () => {
  const cpuBuild = onCard('gpu', {choosable: true, reason: null, build: 'cpu', install: true});
  const status = gpuService([cpuBuild]);
  const {context: c, calls, fields} = harness({'/api/engines/kokoro/install': {plan: {...plan, nvidia: true}},
    '/api/engines/status': status});
  const view = realCard(c);
  c.renderEngineStatus(status);
  assert.equal(view.state.textContent, 'Speaks now on CPU');
  assert.equal(view.note.textContent,
    'Installed with the CPU build of PyTorch, so it runs on the CPU until its GPU build is installed.');
  assert.deepEqual([view.extra, view.extraButton.textContent, view.extraButton.hidden], ['gpu', 'Install GPU build', false]);
  await view.extraButton.handlers.click();
  assert.equal(JSON.stringify(calls[0].body), '{"dry_run":true,"nvidia":true}');
  assert.equal(fields.installTitle.textContent, 'Install the GPU build of Kokoro 82M');
  // A model that is not installed yet: Install puts in the build the switch shows.
  const fresh = {...missingKokoro, name: 'Kokoro 82M', integrated: true, device: 'cpu', switch: 'gpu', device_choice: null,
    gpu: {choosable: true, reason: null, build: null, install: true}};
  c.renderEngineStatus(gpuService([fresh]));
  assert.equal(view.note.textContent, 'Install puts in its GPU build, for the graphics card.');
  assert.equal(view.extraButton.hidden, true);
  await view.button.handlers.click();
  assert.equal(JSON.stringify(calls.filter(call => call.url.endsWith('/install')).pop().body), '{"dry_run":true,"nvidia":true}');
  c.renderEngineStatus(gpuService([{...fresh, switch: 'cpu', device_choice: 'cpu', gpu: {...fresh.gpu, install: false}}]));
  assert.equal(view.note.textContent, 'Install puts in its CPU build.');
  await view.button.handlers.click();
  assert.equal(JSON.stringify(calls.filter(call => call.url.endsWith('/install')).pop().body), '{"dry_run":true,"nvidia":false}');
});

test('choosing a device posts it, holds the switch while it saves, and shows a refusal on the card', async () => {
  const before = gpuService([onCard('gpu', {choosable: true, reason: null, build: 'cuda', install: false})]);
  let answer = {device: {engine: 'kokoro', choice: 'cpu', device: 'cpu', unloaded: true}};
  const {context: c, calls} = harness({'/api/engines/kokoro/device': () => answer, '/api/engines/status': before});
  const view = realCard(c);
  c.renderEngineStatus(before);
  view.radios.cpu.checked = true;
  const saving = view.radios.cpu.handlers.change();
  assert.equal(view.pendingDevice, true);
  await saving;
  assert.deepEqual(calls.slice(0, 2).map(call => [call.url, JSON.stringify(call.body)]),
    [['/api/engines/kokoro/device', '{"device":"cpu"}'], ['/api/engines/status', undefined]]);
  assert.equal(view.pendingDevice, false);
  answer = new Error('The GPU worker is busy with another load or generation request.');
  view.radios.cpu.checked = true;
  await view.radios.cpu.handlers.change();
  assert.equal(view.note.textContent, 'Could not switch it: The GPU worker is busy with another load or generation request.');
  assert.equal(view.note.className, 'meta device-note warn');
  assert.deepEqual([view.radios.gpu.checked, view.radios.cpu.checked], [true, false]);    // the status puts it back
});

test('a model that runs only on the graphics card has CPU switched off, and says so even on a service without one', () => {
  const {context: c} = harness();
  c.modelCards = {qwen: card('Qwen3-TTS 1.7B Base')};
  const only = 'Qwen3-TTS runs only on a graphics card in the Lab: its fast decoder records CUDA graphs.';
  const qwen = {id: 'qwen', name: 'Qwen3-TTS 1.7B Base', integrated: true, state: 'not_installed', device: 'cuda', switch: 'gpu',
    installation: {installed: false, missing: ['x'], installer: 'setup-qwen.bat'}, check: null,
    gpu: {choosable: true, reason: null, build: null, install: true, cpu: only}};
  c.renderEngineStatus(gpuService([qwen]));
  const view = c.modelCards.qwen;
  assert.deepEqual([view.radios.gpu.checked, view.radios.gpu.disabled, view.radios.cpu.disabled], [true, false, true]);
  assert.equal(view.note.textContent, only);
  assert.equal(view.installGpu, true);
  c.renderEngineStatus(gpuService([{...qwen, gpu: {...qwen.gpu, choosable: false, install: false,
    reason: 'The app installs GPU builds for NVIDIA graphics cards only, and this computer has no NVIDIA driver.'}}]));
  assert.equal(view.radios.gpu.disabled, true);
  assert.match(view.note.textContent, /^The app installs GPU builds for NVIDIA graphics cards only.* Qwen3-TTS runs only on a graphics card/);
  c.renderEngineStatus({device: 'cpu', busy: false, speed: [], engines: [{...qwen, device: 'cpu', gpu: {...qwen.gpu, choosable: false}}]});
  assert.equal(view.devices.hidden, true);
  assert.deepEqual([view.note.hidden, view.note.textContent], [false, only]);
});

test('a model that runs only on a graphics card the app cannot install here offers no Install and says why', () => {
  const {context: c} = harness();
  c.modelCards = {qwen: card('Qwen3-TTS 1.7B Base')};
  const reason = 'The app installs GPU builds for NVIDIA graphics cards only, and this computer has no NVIDIA driver.';
  // The status leaves out the launcher of such a model: its installer would only refuse.
  c.renderEngineStatus(gpuService([{id: 'qwen', name: 'Qwen3-TTS 1.7B Base', integrated: true, state: 'not_installed',
    device: 'cuda', switch: 'gpu', installation: {installed: false, missing: ['hf-cache/hub/models--Qwen']}, check: null,
    gpu: {choosable: false, reason, build: null, install: false, cpu: 'Qwen3-TTS runs only on a graphics card in the Lab.'}}]));
  const view = c.modelCards.qwen;
  assert.equal(view.action, 'none');
  assert.equal(view.button.hidden, true);
  assert.equal(view.details.textContent, 'Missing: hf-cache/hub/models--Qwen. The app cannot install it on this computer, for the '
    + 'reason below; docs/SETUP.md (Model runtimes) says what it needs.');
  assert.equal(view.note.textContent, reason + ' Qwen3-TTS runs only on a graphics card in the Lab.');
});

test('a busy service holds every switch', () => {
  const {context: c} = harness();
  c.modelCards = {kokoro: card('Kokoro 82M')};
  c.renderEngineStatus(gpuService([onCard('gpu', {choosable: true, reason: null, build: 'cuda', install: false})], {busy: true}));
  assert.deepEqual([c.modelCards.kokoro.radios.gpu.disabled, c.modelCards.kokoro.radios.cpu.disabled], [true, true]);
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
