// Run with node --test tests/updates_ui.test.cjs. The Updates part of System & backups: what it shows for each answer the
// service gives, and that it asks GitHub only when told or when the once-a-day check is due.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {pageSource} = require('./page_source.cjs');

const source = pageSource();
function sourceBetween(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}

const RELEASE = {tag: 'v0.2.0', name: "Fel's Voice Lab 0.2.0", url: 'https://github.com/Fel101/fels-voice-lab/releases/tag/v0.2.0'};

function harness(replies) {
  const calls = [];
  const fields = {};
  for (const id of ['updateStatus', 'updateCheck', 'updateRelease', 'updateBadge', 'updateAuto']) {
    fields[id] = {textContent: '', className: '', hidden: true, disabled: false, checked: false, href: '#'};
  }
  const context = vm.createContext({
    $: (id) => fields[id],
    whenText: (iso) => 'at ' + iso,
    fetchJson(url, body) {
      calls.push([url, body]);
      const reply = replies[url];
      if (reply instanceof Error) return Promise.reject(reply);
      return Promise.resolve(typeof reply === 'function' ? reply(body) : reply);
    },
  });
  vm.runInContext(sourceBetween('function systemNote(', 'function refreshSystem('), context);
  vm.runInContext(sourceBetween('// Updates:', 'function refreshSystemPage('), context);
  return {context, fields, calls};
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
