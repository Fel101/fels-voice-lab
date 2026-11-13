/* The line for the model worker, on the page: each model request the page sends waits its turn under an id the page chose,
   and the Model worker panel in the side bar says what runs and what waits for it, with Stop and Cancel. */
(function (global) {
  'use strict';
  // The routes that use the model worker (api_spec.WAITING_ROUTES). The page's own requests to them always wait in line.
  var WORKER_ROUTES = [/^\/api\/(speak|stream|performance|load|unload|convert|prepare-reference|kokoro\/phonemize)$/,
    /^\/api\/engines\/[^/]+\/check$/];
  var WAIT_SECONDS = 3600;
  // What the panel says the worker is busy with, for each kind of work GET /api/queue names.
  var DOING = {'Clip':'a clip is being made', 'Live clip':'a live clip is being made', 'Take':'a take is being made',
    'Load':'a model is loading', 'Unload':'the model is unloading', 'Model test':'a model is being tested',
    'Voice conversion':'a voice is being converted', 'Voice recording':'a voice recording is being prepared',
    'Pronunciation preview':'a pronunciation is being previewed', 'Drama studio take':'a Drama studio take is being made',
    'Device switch':'the model is moving to another device', 'Idle unload':'an idle model is unloading'};
  var send = global.fetch, mine = {}, count = 0, timer = null, last = null, failure = '', failureUntil = 0, quiet = 0;

  /** @param {string} id @returns {any} */
  function el(id) { return global.document && global.document.getElementById(id); }
  function workerRoute(url) {
    var path = String(url).split('?')[0].replace(/^https?:\/\/[^/]+/, '');
    return WORKER_ROUTES.some(function (pattern) { return pattern.test(path); });
  }
  function newId() {
    count += 1;
    return 'page-' + Date.now().toString(36) + '-' + count + '-' + Math.random().toString(36).slice(2, 8);
  }
  // A worker request's fetch options, with wait_seconds in its JSON body and its id in X-Request-Id; null when it has no
  // JSON object to add to. A request that already says how long to wait, or names its own id, keeps them.
  function inLine(init) {
    if (!init || String(init.method || 'GET').toUpperCase() !== 'POST' || typeof init.body !== 'string') return null;
    var body;
    try { body = JSON.parse(init.body); } catch (error) { return null; }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return null;
    if (!Object.prototype.hasOwnProperty.call(body, 'wait_seconds')) body.wait_seconds = WAIT_SECONDS;
    /** @type {Record<string, string>} */
    var headers = {}, id = null;
    if (init.headers && typeof init.headers.forEach === 'function') {
      init.headers.forEach(function (value, name) { headers[name] = value; });
    } else { Object.assign(headers, init.headers || {}); }
    Object.keys(headers).forEach(function (name) { if (name.toLowerCase() === 'x-request-id') id = headers[name]; });
    if (!id) { id = newId(); headers['X-Request-Id'] = id; }
    return {id:id, init:Object.assign({}, init, {headers:headers, body:JSON.stringify(body)})};
  }
  function fetchInLine(url, init) {
    var request = workerRoute(url) ? inLine(init) : null;
    if (!request) return send.call(global, url, init);
    mine[request.id] = true;
    soon();
    var answer = send.call(global, url, request.init);
    var settled = function () { delete mine[request.id]; soon(); };
    answer.then(settled, settled);
    return answer;
  }

  function doing(running) {
    if (running.priority === 'low') return 'a scene batch is rendering';
    if (running.detail) return running.detail; // an install: "Kokoro 82M is being installed"
    return DOING[running.what] || running.what + ' is running';
  }
  function modelName(engine) {
    var entry = ((global.catalog && global.catalog.engines) || []).find(function (item) { return item.id === engine; });
    return entry ? entry.name : engine;
  }
  // The panel's lines for a GET /api/queue answer: what runs, with Stop when it can be stopped; each of this page's waiting
  // requests, with Cancel; and how many others wait. `own` holds the ids of this page's requests.
  function describe(state, own) {
    if (!state) return [];
    var running = state.running, waiting = state.waiting || [], lines = [], others = 0;
    if (running) {
      lines.push({id:running.id, action:running.can_stop ? 'stop' : null,
        text:'Now: ' + running.what + (running.engine ? ' · ' + modelName(running.engine) : '') + ' · '
          + Math.round(running.seconds) + ' s' + (own[running.id] ? ' (yours)' : '')});
    }
    waiting.forEach(function (item, index) {
      if (!own[item.id]) { others += 1; return; }
      var ahead = index + (running ? 1 : 0);
      lines.push({id:item.id, action:'cancel', text:ahead === 0 ? 'Waiting: next in line'
        : 'Waiting: ' + ahead + ' ahead of you' + (running ? ' (' + doing(running) + ')' : '')});
    });
    if (others) lines.push({id:'others', action:null, text:others + (others === 1 ? ' more request waits' : ' more requests wait')});
    return lines;
  }
  var BUTTONS = {stop:['Stop', 'Stopping…'], cancel:['Cancel', 'Cancelling…']};
  function act(item, button) {
    button.disabled = true; button.textContent = BUTTONS[item.action][1]; failure = '';
    var failed = function (message) { failure = message; failureUntil = Date.now() + 10000; };
    send.call(global, '/api/queue/' + encodeURIComponent(item.id) + '/cancel', {method:'POST'}).then(function (response) {
      if (response.ok || response.status === 404) return null; // a 404: it finished, or left the line, meanwhile
      return response.json().then(function (answer) { failed(answer.error || 'Could not stop it.'); });
    }).catch(function (error) { failed(error.message); }).then(soon);
  }
  function render(state) {
    var panel = el('queuePanel'), list = el('queueItems');
    if (!panel || !list) return;
    if (failure && Date.now() > failureUntil) failure = '';
    var lines = describe(state, mine), signature = lines.map(function (item) { return item.id + ':' + item.action; }).join('|');
    // A batch leaves the worker free for a moment between its lines, so the panel hides at the second answer in a row with
    // nothing in it, not the first.
    quiet = lines.length ? 0 : quiet + 1;
    if (quiet === 1 && !failure) return;
    panel.hidden = !lines.length && !failure;
    // Rebuild only when the lines change, so a button keeps its focus while the seconds count.
    if (list.dataset.signature !== signature) {
      list.dataset.signature = signature;
      list.textContent = '';
      lines.forEach(function (item) {
        var row = global.document.createElement('p'), text = global.document.createElement('span');
        row.className = 'queue-item'; text.className = 'queue-text'; row.appendChild(text);
        if (item.action) {
          var button = global.document.createElement('button');
          button.type = 'button'; button.textContent = BUTTONS[item.action][0];
          button.addEventListener('click', function () { act(item, button); });
          row.appendChild(button);
        }
        if (item.action === 'stop') {
          var note = global.document.createElement('span');
          note.className = 'meta'; note.textContent = 'Stop ends it now; the model loads again for the next request.';
          row.appendChild(note);
        }
        list.appendChild(row);
      });
    }
    Array.prototype.forEach.call(list.querySelectorAll('.queue-text'), function (text, index) {
      text.textContent = lines[index].text;
    });
    if (el('queueError')) el('queueError').textContent = failure;
  }
  function busy() { return Object.keys(mine).length > 0 || !!(last && (last.running || (last.waiting || []).length)); }
  function schedule(delay) { clearTimeout(timer); timer = setTimeout(poll, delay); }
  function soon() { schedule(250); }
  // Every second while something runs or waits, every eight seconds otherwise, and not while the page is out of sight.
  function poll() {
    if (global.document && global.document.hidden) { schedule(2000); return; }
    send.call(global, '/api/queue').then(function (response) { return response.ok ? response.json() : null; })
      .then(function (state) { last = state; render(state); }).catch(function () {})
      .then(function () { schedule(busy() ? 1000 : 8000); });
  }

  global.fetch = fetchInLine;
  global.VoiceLabQueue = {describe:describe, inLine:inLine, workerRoute:workerRoute, poll:poll, pending:function () {
    return Object.keys(mine);
  }};
  schedule(0);
})(window);
