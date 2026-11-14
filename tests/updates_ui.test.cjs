// Run with node --test tests/updates_ui.test.cjs. The Updates part of System & backups: what it shows for each answer the
// service gives, that it asks GitHub only when told or when the once-a-day check is due, Update now's window, and the note an
// update leaves.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {pageSource} = require('./page_source.cjs');

const source = pageSource();
function sourceBetween(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}

const RELEASE = {tag: 'v0.2.0', name: "Fel's Voice Lab 0.2.0", url: 'https://github.com/Fel101/fels-voice-lab/releases/tag/v0.2.0'};

function element(tag) {
  return {tag, children: [], textContent: '', className: '', hidden: true, disabled: false, checked: false, href: '#',
    handlers: {},
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(type, handler) { this.handlers[type] = handler; }};
}

// What a list of the window's or the note's lines says, each line's own buttons and links after it.
function said(node) {
  return node.children.map((child) => [child.textContent, ...child.children.map((part) => part.textContent)].filter(Boolean).join(' | '));
}

function harness(replies, extra = {}) {
  const calls = [];
  const fields = {};
  for (const id of ['updateStatus', 'updateCheck', 'updateRelease', 'updateBadge', 'updateAuto', 'updateNow', 'updateNowHeader',
    'updateTitle', 'updateState', 'updatePlan', 'updateGo', 'updateClose', 'updateNotice', 'updateNoticeTitle', 'updateNoticeText',
    'updateNoticeAdvice', 'updateNoticeLink', 'openLabFolderStatus']) {
    fields[id] = element(id);
  }
  fields.updateDialog = Object.assign(element('dialog'), {open: false, showModal() { this.open = true; }, close() { this.open = false; }});
  const installs = [];
  const context = vm.createContext({
    $: (id) => fields[id],
    whenText: (iso) => 'at ' + iso,
    document: {createElement: element, createTextNode: (text) => ({textContent: text, children: []})},
    location: {hash: '#system'},
    window: {confirm: () => extra.confirm ?? true},
    modelCards: {kokoro: {installGpu: false}},
    planInstall: (engine, gpu) => installs.push([engine, gpu]),
    sceneDirty: !!extra.sceneDirty,
    fetchJson(url, body) {
      calls.push([url, body]);
      const reply = replies[url];
      if (reply instanceof Error) return Promise.reject(reply);
      return Promise.resolve(typeof reply === 'function' ? reply(body) : reply);
    },
  });
  vm.runInContext(sourceBetween('function systemNote(', 'function refreshSystem('), context);
  vm.runInContext(sourceBetween('// Updates:', 'function refreshSystemPage('), context);
  return {context, fields, calls, installs};
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
// Objects made inside the sandbox have its own prototypes, so calls are compared as plain data.
const plain = (value) => JSON.parse(JSON.stringify(value));

test('a newer release is named in the panel, linked, and shown in the header', async () => {
  const h = harness({'/api/updates/check': {current: '0.1.0', latest: '0.2.0', newer: true, release: RELEASE,
    message: 'Version 0.2.0 is available. This is version 0.1.0.', checked_at: '2026-11-21T09:00:00+00:00'}});
  await h.context.checkForUpdates(false);
  assert.deepEqual(plain(h.calls), [['/api/updates/check', {}]]);
  assert.equal(h.fields.updateStatus.textContent, 'Version 0.2.0 is available. This is version 0.1.0. Checked at 2026-11-21T09:00:00+00:00.');
  assert.equal(h.fields.updateRelease.hidden, false);
  assert.equal(h.fields.updateRelease.href, RELEASE.url);
  assert.equal(h.fields.updateRelease.textContent, 'What’s new in 0.2.0');
  assert.equal(h.fields.updateBadge.hidden, false);
  assert.equal(h.fields.updateBadge.textContent, 'Version 0.2.0 is available');
  assert.equal(h.fields.updateNow.hidden, false);
  assert.equal(h.fields.updateNowHeader.hidden, false);
  assert.equal(h.fields.updateCheck.disabled, false);
});

test('the newest version, no release yet and a failure leave the header alone', async () => {
  for (const [reply, words, flagged] of [
    [{current: '0.2.0', latest: '0.2.0', newer: false, release: RELEASE, message: 'This is the newest version, 0.2.0.'}, 'newest version', false],
    [{current: '0.1.0', latest: null, newer: false, release: null, message: 'No release of the Lab has been published on GitHub yet.'}, 'No release', false],
    [new Error('GitHub could not be reached (offline).'), 'Could not check for updates: GitHub could not be reached', true],
  ]) {
    const h = harness({'/api/updates/check': reply});
    await h.context.checkForUpdates(false);
    assert.match(h.fields.updateStatus.textContent, new RegExp(words));
    assert.equal(h.fields.updateStatus.className.includes('flagged'), flagged);
    assert.equal(h.fields.updateBadge.hidden, true);
    assert.equal(h.fields.updateRelease.hidden, true);
    assert.equal(h.fields.updateNow.hidden, true);
    assert.equal(h.fields.updateNowHeader.hidden, true);
  }
});

test('opening the page asks GitHub only when the once-a-day check is on and due', async () => {
  const quiet = harness({'/api/updates': {current: '0.1.0', check_on_open: false, due: false, last: null}});
  await quiet.context.refreshUpdates();
  assert.deepEqual(quiet.calls.map((call) => call[0]), ['/api/updates']);
  assert.equal(quiet.fields.updateAuto.checked, false);

  const last = {current: '0.1.0', latest: '0.2.0', newer: true, release: RELEASE, message: 'Version 0.2.0 is available.'};
  const remembered = harness({'/api/updates': {current: '0.1.0', check_on_open: true, due: false, last}});
  await remembered.context.refreshUpdates();
  assert.deepEqual(remembered.calls.map((call) => call[0]), ['/api/updates']);
  assert.equal(remembered.fields.updateBadge.hidden, false);

  const due = harness({'/api/updates': {current: '0.1.0', check_on_open: true, due: true, last: null},
    '/api/updates/check': new Error('offline')});
  await due.context.refreshUpdates();
  await flush();
  assert.deepEqual(due.calls.map((call) => call[0]), ['/api/updates', '/api/updates/check']);
  assert.equal(due.fields.updateStatus.textContent, '', 'a quiet check that fails says nothing until asked');
});

test('switching the daily check on saves it and checks at once; a refusal turns the box back', async () => {
  const h = harness({'/api/preferences': {theme: 'system', check_updates: true},
    '/api/updates/check': {current: '0.1.0', latest: '0.1.0', newer: false, release: RELEASE, message: 'This is the newest version, 0.1.0.'}});
  h.fields.updateAuto.checked = true;
  await h.context.saveUpdateChoice();
  assert.deepEqual(plain(h.calls), [['/api/preferences', {check_updates: true}], ['/api/updates/check', {}]]);

  const refused = harness({'/api/preferences': new Error('check_updates must be true or false.')});
  refused.fields.updateAuto.checked = true;
  await refused.context.saveUpdateChoice();
  assert.equal(refused.fields.updateAuto.checked, false);
  assert.match(refused.fields.updateStatus.textContent, /could not be saved/);
});

test('the Updates controls are on the page once and wired', () => {
  for (const id of ['updateStatus', 'updateCheck', 'updateRelease', 'updateBadge', 'updateAuto']) {
    assert.equal((source.match(new RegExp(`id="${id}"`, 'g')) || []).length, 1, id);
  }
  assert.match(source, /\$\('updateCheck'\)\.addEventListener\('click',function\(\)\{checkForUpdates\(false\);\}\)/);
  assert.match(source, /\$\('updateAuto'\)\.addEventListener\('change',saveUpdateChoice\)/);
  assert.match(source, /<a class="update-badge" id="updateBadge" href="#system" hidden><\/a>/);
});

const PLAN = {ok: true, newer: true, from: '0.4.0', to: '0.4.1', message: 'Version 0.4.1 is available. This is version 0.4.0.',
  release: {url: 'https://github.com/Fel101/fels-voice-lab/releases/tag/v0.4.1'}, method: 'zip',
  steps: ["The Lab's own files, from the release on GitHub: 3 to replace, 1 new, 1 to remove.", 'First a copy goes to out\\update.'],
  notes: ['Changed here since version 0.4.0: catalog/engines.json.']};

test('Update now shows the plan first, and nothing starts until Update now in its window is pressed', async () => {
  const h = harness({'/api/update/plan': {plan: PLAN, blocked: null, how: 'window'}, '/api/update/start': {how: 'window'}});
  await h.context.openUpdateNow();
  assert.equal(h.fields.updateDialog.open, true);
  assert.deepEqual(plain(h.calls), [['/api/update/plan', {}]]);
  assert.equal(h.fields.updateTitle.textContent, 'Update from 0.4.0 to 0.4.1');
  assert.equal(h.fields.updateState.textContent, 'This is what it will do. Nothing has changed yet.');
  assert.deepEqual(said(h.fields.updatePlan), [
    "1. The Lab's own files, from the release on GitHub: 3 to replace, 1 new, 1 to remove.", '2. First a copy goes to out\\update.',
    'Changed here since version 0.4.0: catalog/engines.json.',
    'The Lab closes, a window of its own shows the update, and the Lab opens again when it is done. Leave that window open until it has finished.',
    'What’s new in 0.4.1']);
  assert.equal(h.fields.updateGo.hidden, false);
  assert.equal(h.fields.updateGo.disabled, false);
  await h.context.startUpdateNow();
  assert.deepEqual(plain(h.calls).map((call) => call[0]), ['/api/update/plan', '/api/update/start']);
  assert.match(h.fields.updateState.textContent, /^The Lab closes, a window of its own shows the update/);
  assert.equal(h.fields.updateGo.hidden, true);
  h.context.closeUpdateNow();
  assert.equal(h.fields.updateDialog.open, true, 'the window stays while the Lab closes');
});

test('Update now waits with the reason while something is running, and says when there is nothing newer', async () => {
  const blocked = harness({'/api/update/plan': {plan: PLAN, blocked: 'Kokoro 82M is installing. Update now works once it has finished.', how: 'console'}});
  await blocked.context.openUpdateNow();
  assert.equal(blocked.fields.updateState.textContent, 'Kokoro 82M is installing. Update now works once it has finished.');
  assert.equal(blocked.fields.updateGo.disabled, true);
  assert.match(said(blocked.fields.updatePlan).join(' '), /start the Lab again with serve\.bat/);

  const current = harness({'/api/update/plan': {plan: {ok: true, newer: false, message: 'This is the newest version, 0.4.0.'}, blocked: null, how: 'window'}});
  await current.context.openUpdateNow();
  assert.equal(current.fields.updateState.textContent, 'This is the newest version, 0.4.0.');
  assert.equal(current.fields.updateGo.hidden, true);
  assert.equal(current.fields.updateClose.textContent, 'Close');

  const refused = harness({'/api/update/plan': {plan: {ok: false, problem: 'GitHub could not be reached (offline). Nothing was changed.'}, blocked: null, how: 'window'}});
  await refused.context.openUpdateNow();
  assert.equal(refused.fields.updateState.textContent, 'GitHub could not be reached (offline). Nothing was changed.');
  assert.equal(refused.fields.updateGo.hidden, true);
});

test('unsaved scene changes are asked about before the Lab closes, and a refusal from the service is said', async () => {
  const kept = harness({'/api/update/plan': {plan: PLAN, blocked: null, how: 'window'}}, {sceneDirty: true, confirm: false});
  await kept.context.openUpdateNow();
  await kept.context.startUpdateNow();
  assert.deepEqual(plain(kept.calls).map((call) => call[0]), ['/api/update/plan']);

  const refused = harness({'/api/update/plan': {plan: PLAN, blocked: null, how: 'window'},
    '/api/update/start': new Error('A scene batch is rendering. Wait for it to finish, or stop it in Scenes, then press Update now.')});
  await refused.context.openUpdateNow();
  await refused.context.startUpdateNow();
  assert.equal(refused.fields.updateState.textContent, 'The update did not start: A scene batch is rendering. Wait for it to finish, or stop it in Scenes, then press Update now.');
  assert.equal(refused.fields.updateGo.disabled, false);
  refused.context.closeUpdateNow();
  assert.equal(refused.fields.updateDialog.open, false);
});

test('after an update the note says so once, with an Install button for each model to install again', async () => {
  const notice = {state: 'updated', from: '0.4.0', to: '0.4.1', release_url: 'https://github.com/Fel101/fels-voice-lab/releases/tag/v0.4.1',
    advice: [{text: "Kokoro's pinned packages changed. Delete the .venv-kokoro folder and install it again.", models: [{engine: 'kokoro', name: 'Kokoro'}], folder: '.venv-kokoro'}],
    problems: []};
  const h = harness({'/api/update/notice': {notice}, '/api/update/notice/dismiss': {ok: true}, '/api/system/open-folder': {folder: 'C:\\Lab'}});
  await h.context.refreshUpdateNotice();
  assert.equal(h.fields.updateNotice.hidden, false);
  assert.equal(h.fields.updateNoticeTitle.textContent, 'Updated to version 0.4.1');
  assert.match(h.fields.updateNoticeText.textContent, /Some models need installing again/);
  assert.equal(h.fields.updateNoticeLink.hidden, false);
  assert.equal(h.fields.updateNoticeLink.href, notice.release_url);
  assert.deepEqual(said(h.fields.updateNoticeAdvice), [notice.advice[0].text + ' | Open the Lab’s folder | Install Kokoro']);
  const [folder, install] = h.fields.updateNoticeAdvice.children[0].children.slice(1);
  install.handlers.click();
  assert.deepEqual(h.installs, [['kokoro', false]]);
  await folder.handlers.click();
  assert.equal(h.fields.openLabFolderStatus.textContent, 'Opened C:\\Lab in File Explorer.');
  await h.context.dismissUpdateNotice();
  assert.equal(h.fields.updateNotice.hidden, true);
  assert.deepEqual(plain(h.calls).map((call) => call[0]), ['/api/update/notice', '/api/system/open-folder', '/api/update/notice/dismiss']);
});

test('a failed update says why and which version is still here; no notice shows nothing', async () => {
  const h = harness({'/api/update/notice': {notice: {state: 'failed', from: '0.4.0', to: '0.4.1',
    message: 'GitHub could not be reached (offline). Nothing was changed.'}}});
  await h.context.refreshUpdateNotice();
  assert.equal(h.fields.updateNoticeTitle.textContent, 'The update did not finish');
  assert.equal(h.fields.updateNoticeText.textContent, 'GitHub could not be reached (offline). Nothing was changed. This is still version 0.4.0.');
  assert.equal(h.fields.updateNoticeLink.hidden, true);

  const none = harness({'/api/update/notice': {notice: null}});
  await none.context.refreshUpdateNotice();
  assert.equal(none.fields.updateNotice.hidden, true);
});

test('Update now, its window, the note and Open the Lab’s folder are on the page once and wired', () => {
  for (const id of ['updateNow', 'updateNowHeader', 'updateDialog', 'updateGo', 'updateClose', 'updateNotice', 'updateNoticeClose',
    'openLabFolder']) {
    assert.equal((source.match(new RegExp(`id="${id}"`, 'g')) || []).length, 1, id);
  }
  for (const wiring of ["$('updateNow').addEventListener('click',openUpdateNow)", "$('updateNowHeader').addEventListener('click',openUpdateNow)",
    "$('updateGo').addEventListener('click',startUpdateNow)", "$('updateNoticeClose').addEventListener('click',dismissUpdateNotice)",
    "$('openLabFolder').addEventListener('click',openLabFolder)", 'refreshUpdateNotice();']) {
    assert.ok(source.includes(wiring), wiring);
  }
});
