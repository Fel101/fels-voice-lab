// Run with node --test tests/connect_ui.test.cjs. Connect other apps on System & backups: the address it shows, why it is not
// the known one when it is not, copying the address and the test command, and saving the window's port.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {pageSource} = require('./page_source.cjs');

const source = pageSource();
function sourceBetween(start, end) {
  return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));
}

const KNOWN = 'http://127.0.0.1:8765';
const INFO = {url: KNOWN, known_url: KNOWN, note: null, in_address_file: true,
  address_file: 'D:\\Profiles\\AppData\\Local\\FelsVoiceLabService\\service.json', window_port: 8765,
  test: 'curl.exe http://127.0.0.1:8765/api/health', guide: 'https://github.com/Fel101/fels-voice-lab/blob/v0.4.0/docs/API.md',
  connect_guide: 'https://github.com/Fel101/fels-voice-lab/blob/v0.4.0/docs/CONNECT.md', openai_base: 'http://127.0.0.1:8765/v1',
  mcp: {name: 'voice-lab', claude_code: 'claude mcp add --scope user --transport stdio voice-lab -- "D:\\Lab\\.venv\\Scripts\\python.exe" '
    + '"D:\\Lab\\tools\\mcp_server.py"', settings: '{\n  "mcpServers": {\n    "voice-lab": {}\n  }\n}'}};

function element(tag) {
  return {tag, textContent: '', className: '', hidden: false, value: '', href: '#', attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; }, select() {}};
}

function harness(replies, {clipboard = 'works', execCommand = false} = {}) {
  const calls = [];
  const copied = [];
  const fields = {};
  for (const id of ['connectUrl', 'connectNote', 'connectTest', 'connectGuide', 'connectStatus', 'connectPort',
    'connectPortStatus', 'connectMcp', 'connectMcpGuide', 'connectOpenAI']) {
    fields[id] = element(id);
  }
  const body = {children: [], appendChild(child) { this.children.push(child); }, removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }};
  const navigator = clipboard === 'none' ? {} : {clipboard: {writeText(text) {
    copied.push(text);
    return clipboard === 'works' ? Promise.resolve() : Promise.reject(new Error('NotAllowedError'));
  }}};
  const context = vm.createContext({
    $: (id) => fields[id],
    navigator,
    Number,
    document: {createElement: element, body, execCommand(name) { copied.push('execCommand:' + name); return execCommand; }},
    fetchJson(url, sent) {
      calls.push([url, sent]);
      const reply = replies[url];
      if (reply instanceof Error) return Promise.reject(reply);
      return Promise.resolve(typeof reply === 'function' ? reply(sent) : reply);
    },
  });
  vm.runInContext(sourceBetween('function systemNote(', 'function renderSystem('), context);
  vm.runInContext(sourceBetween('// Connect other apps:', '// Install speech recognition.'), context);
  return {context, fields, calls, copied, body};
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test('on the known port it shows the address, the test command and the guide, and no note', async () => {
  const {context, fields} = harness({'/api/system/connect': INFO});
  await context.refreshConnect();
  assert.equal(fields.connectUrl.textContent, KNOWN);
  assert.equal(fields.connectTest.textContent, 'curl.exe http://127.0.0.1:8765/api/health');
  assert.equal(fields.connectGuide.href, INFO.guide);
  assert.equal(fields.connectMcp.textContent, INFO.mcp.claude_code);
  assert.equal(fields.connectMcpGuide.href, INFO.connect_guide);
  assert.equal(fields.connectOpenAI.textContent, 'http://127.0.0.1:8765/v1');
  assert.equal(fields.connectPort.value, '8765');
  assert.equal(fields.connectNote.hidden, true);
});

test('a Lab moved off the known port says why and how programs still find it', async () => {
  const moved = {...INFO, url: 'http://127.0.0.1:50123', test: 'curl.exe http://127.0.0.1:50123/api/health',
    note: 'Another program is using port 8765, so this Lab listens on port 50123 instead.'};
  const {context, fields} = harness({'/api/system/connect': moved});
  await context.refreshConnect();
  assert.equal(fields.connectNote.hidden, false);
  assert.equal(fields.connectNote.textContent, 'Another program is using port 8765, so this Lab listens on port 50123 instead. '
    + 'Programs that read the address file (' + INFO.address_file + ') still find it; give any other program this address.');
  const chosen = harness({'/api/system/connect': {...moved, note: null, window_port: 0, in_address_file: false}});
  await chosen.context.refreshConnect();
  assert.match(chosen.fields.connectNote.textContent, /^The Lab’s window took a free port, as its setting below asks\. Give programs this address: the address file names another Lab that is running\.$/);
  assert.equal(chosen.fields.connectPort.value, '0');
  const asked = harness({'/api/system/connect': {...moved, note: null, window_port: 50123}});
  await asked.context.refreshConnect();
  assert.match(asked.fields.connectNote.textContent, /^The Lab’s window asks for port 50123, as its setting below says\. Programs that read/);
  const started = harness({'/api/system/connect': {...moved, note: null}});
  await started.context.refreshConnect();
  assert.match(started.fields.connectNote.textContent, /^This service was started on port 50123\. Programs that read/,
    'serve.py --port 50123: the window\'s setting, 8765, did not choose it');
});

test('a service that cannot be asked says so', async () => {
  const {context, fields} = harness({'/api/system/connect': new Error('Failed to fetch')});
  await context.refreshConnect();
  assert.equal(fields.connectStatus.textContent, 'Could not read the Lab’s address: Failed to fetch');
  assert.match(fields.connectStatus.className, /flagged/);
});

test('the address and the test command are copied, and a browser that blocks copying is told about', async () => {
  const {context, fields, copied} = harness({'/api/system/connect': INFO});
  await context.refreshConnect();
  await context.copyConnect('url');
  assert.equal(fields.connectStatus.textContent, 'Copied http://127.0.0.1:8765.');
  await context.copyConnect('test');
  assert.deepEqual(copied, [KNOWN, INFO.test]);
  assert.match(fields.connectStatus.textContent, /^Copied the test command\. Paste it into a command window/);
  const old = harness({'/api/system/connect': INFO}, {clipboard: 'none', execCommand: true});
  await old.context.refreshConnect();
  await old.context.copyConnect('url');
  assert.deepEqual(old.copied, ['execCommand:copy']);
  assert.equal(old.body.children.length, 0, 'the helper box is taken away again');
  assert.equal(old.fields.connectStatus.textContent, 'Copied http://127.0.0.1:8765.');
  const blocked = harness({'/api/system/connect': INFO}, {clipboard: 'refused', execCommand: false});
  await blocked.context.refreshConnect();
  await blocked.context.copyConnect('test');
  assert.equal(blocked.fields.connectStatus.textContent, 'Your browser blocked copying. Select the text above and copy it yourself.');
  assert.match(blocked.fields.connectStatus.className, /flagged/);
});

test('the window’s port is saved for the next time it opens, and only a whole number is sent', async () => {
  const {context, fields, calls} = harness({'/api/system/connect/port': (sent) => ({window_port: sent.port})});
  fields.connectPort.value = '0';
  await context.saveConnectPort();
  assert.deepEqual(plain(calls), [['/api/system/connect/port', {port: 0}]]);
  assert.equal(fields.connectPortStatus.textContent, 'Saved. The window takes any free port the next time it opens.');
  fields.connectPort.value = ' 9123 ';
  await context.saveConnectPort();
  assert.equal(fields.connectPortStatus.textContent, 'Saved. The window asks for port 9123 the next time it opens.');
  for (const typed of ['', 'abc', '80.5']) {
    fields.connectPort.value = typed;
    await context.saveConnectPort();
    assert.equal(fields.connectPortStatus.textContent, 'Type a whole number: 0, or 1024 to 65535.');
  }
  assert.equal(calls.length, 2, 'nothing is sent for those');
  const refused = harness({'/api/system/connect/port': new Error('The port must be 0, for any free port, or a number from 1024 to 65535.')});
  refused.fields.connectPort.value = '80';
  await refused.context.saveConnectPort();
  assert.equal(refused.fields.connectPortStatus.textContent, 'The port must be 0, for any free port, or a number from 1024 to 65535.');
  assert.match(refused.fields.connectPortStatus.className, /flagged/);
});

test('the Claude Code line and the settings for other MCP apps are copied, and blocked copying shows the settings', async () => {
  const {context, fields, copied} = harness({'/api/system/connect': INFO});
  await context.refreshConnect();
  await context.copyConnect('mcp');
  assert.match(fields.connectStatus.textContent, /^Copied the Claude Code line\. Paste it into a command window, then start a new Claude Code session/);
  await context.copyMcpSettings();
  assert.deepEqual(copied, [INFO.mcp.claude_code, INFO.mcp.settings]);
  assert.match(fields.connectStatus.textContent, /^Copied the settings\. Paste them into the app’s MCP settings/);
  await context.copyConnect('openai');
  assert.equal(copied.at(-1), 'http://127.0.0.1:8765/v1');
  assert.match(fields.connectStatus.textContent, /^Copied http:\/\/127\.0\.0\.1:8765\/v1\. In the app, choose OpenAI for speech/);
  const blocked = harness({'/api/system/connect': INFO}, {clipboard: 'refused', execCommand: false});
  await blocked.context.refreshConnect();
  await blocked.context.copyMcpSettings();
  assert.equal(blocked.fields.connectStatus.textContent,
    'Your browser blocked copying. These are the settings: { "mcpServers": { "voice-lab": {} } }');
  assert.match(blocked.fields.connectStatus.className, /flagged/);
});
