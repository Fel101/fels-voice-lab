// Book projects group editable scene chapters and their selected takes.
var bookPreview = null, activeBook = null, bookDelivery = null, bookBusy = false;
var bookTimer = null, bookRead = 0;
var bookModelSpeed = {};
var bookFields = { title: 'bookTitle', author: 'bookAuthor', narrator: 'bookNarrator',
  genre: 'bookGenre', year: 'bookYear', description: 'bookDescription' };

function bookNote(message) {
  $('bookStatus').textContent = message;
}
function bookError(error) {
  bookNote(error.message || String(error));
}
function bookUrl() {
  return '/api/books/' + activeBook.id;
}
function bookAction(message, work) {
  if (bookBusy) return;
  bookBusy = true;
  $('books').setAttribute('aria-busy', 'true');
  bookNote(message);
  Promise.resolve().then(work).catch(bookError).finally(function () {
    bookBusy = false;
    $('books').setAttribute('aria-busy', 'false');
    bookImportReady();
  });
}
async function bookUpload(url, raw) {
  var response = await fetch(url, {method: 'POST', body: raw, headers: {'Content-Type': 'application/octet-stream'}});
  var result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The file could not be read.');
  return result;
}
function bookImportReady() {
  var voices = Array.from($('bookVoiceMap').querySelectorAll('select'));
  var mapped = voices.every(function (select) { return select.value; });
  $('bookCreate').disabled = bookBusy || !bookPreview || !mapped;
  if (!bookPreview) return;
  var models = Object.fromEntries(voices.map(function (select) {
    return [select.dataset.speaker, select.selectedOptions[0]?.dataset.model || ''];
  }));
  var passages = bookPreview.chapters.flatMap(function (chapter) { return chapter.lines; });
  var chapterGap = Number($('bookImportGap').value) / 1000;
  var paragraphGap = Number($('bookParagraphPause').value) / 1000;
  var listeningSeconds = (bookPreview.chapters.length - 1) * chapterGap;
  var renderSeconds = 0;
  var untested = Array.from(new Set(Object.values(models))).filter(function (model) {
    return model && !Object.hasOwn(bookModelSpeed, model);
  });
  var measured = mapped && !untested.length;
  passages.forEach(function (line) {
    var speechSeconds = (line.text.match(/[\p{L}\p{N}]+/gu) || []).length / 2.5;
    listeningSeconds += speechSeconds + (line.pause_before_ms || 0) / 1000
      + (line.pause_after_ms == null ? paragraphGap : line.pause_after_ms / 1000);
    if (measured) renderSeconds += speechSeconds * bookModelSpeed[models[line.speaker]];
  });
  $('bookImportSummary').textContent = bookCount(bookPreview.chapters.length, 'chapter') + ' · '
    + bookCount(passages.length, 'passage') + ' · About ' + bookDuration(listeningSeconds) + ' of audio'
    + (measured ? ' · About ' + bookDuration(renderSeconds) + ' to render here.'
      : mapped ? ' · Test ' + untested.map(sceneModelName).join(', ')
        + ' on Models & features, then preview again to see how long rendering takes here.'
        : ' · Choose voices to estimate render time.');
}
function bookCastPicker(name) {
  var select = document.createElement('select'), defaultId = sceneScriptGuess(name);
  select.dataset.speaker = name;
  option(select, '', 'Choose an approved Cast voice…');
  sceneScriptVoices().forEach(function (group) {
    var choices = document.createElement('optgroup');
    choices.label = group.character.name;
    group.voices.filter(function (voice) { return voice.status === 'approved'; }).forEach(function (voice) {
      var item = document.createElement('option');
      item.value = voice.id;
      item.textContent = voice.label + ' · ' + sceneModelName(voice.engine_id);
      item.dataset.model = voice.engine_id;
      choices.appendChild(item);
    });
    if (choices.childElementCount) select.appendChild(choices);
  });
  if (Array.from(select.options).some(function (item) { return item.value === defaultId; })) select.value = defaultId;
  select.addEventListener('change', bookImportReady);
  return select;
}
function paintBookPreview() {
  var voices = $('bookVoiceMap'), chapters = $('bookReviewChapter');
  voices.replaceChildren();
  chapters.replaceChildren();
  $('bookImportWarnings').replaceChildren();
  bookPreview.chapters.forEach(function (chapter, index) { option(chapters, index, chapter.title + ' · ' + bookCount(chapter.lines.length, 'passage')); });
  var roles = (bookPreview.narration ? [''] : []).concat(bookPreview.speakers);
  roles.forEach(function (role) { sceneLabel(role || 'Default narrator', bookCastPicker(role), voices, true); });
  bookPreview.warnings.forEach(function (warning) {
    var item = document.createElement('li');
    item.textContent = warning;
    $('bookImportWarnings').appendChild(item);
  });
  $('bookImportTitle').value = bookPreview.title;
  $('bookReview').hidden = false;
  showBookReviewChapter();
  bookImportReady();
}
function showBookReviewChapter() {
  var part = bookPreview && bookPreview.chapters[Number($('bookReviewChapter').value)];
  $('bookReviewText').value = part ? part.lines.map(function (line) {
    return (line.speaker ? line.speaker + ': ' : '') + line.text;
  }).join('\n\n') : '';
}
function bookPreviewInput(file, name) {
  return bookUpload('/api/books/import/preview?name=' + encodeURIComponent(name) + '&split='
    + encodeURIComponent($('bookSplit').value), file).then(function (result) {
    return refreshCast().then(function () {
      bookPreview = result.manuscript;
      bookModelSpeed = result.model_speed || {};
      paintBookPreview();
      bookNote('Review the extraction and assign your Cast voices.');
    });
  });
}
$('bookPreviewFile').addEventListener('click', function () {
  var file = $('bookFile').files[0];
  if (!file) return bookNote('Choose a manuscript file first.');
  bookAction('Extracting manuscript…', function () { return bookPreviewInput(file, file.name); });
});
$('bookPreviewPaste').addEventListener('click', function () {
  var text = $('bookPaste').value.trim();
  if (!text) return bookNote('Paste a manuscript first.');
  bookAction('Reading manuscript…', function () { return bookPreviewInput(new Blob([text]), 'Untitled manuscript.md'); });
});
['bookParagraphPause', 'bookImportGap'].forEach(function (id) {
  $(id).addEventListener('input', bookImportReady);
});
$('bookReviewChapter').addEventListener('change', showBookReviewChapter);
$('bookCreate').addEventListener('click', function () {
  bookAction('Creating chapter scenes…', async function () {
    var voices = Object.fromEntries(Array.from($('bookVoiceMap').querySelectorAll('select'), function (select) {
      return [select.dataset.speaker, select.value];
    }));
    var result = await fetchJson('/api/books', {metadata: {title: $('bookImportTitle').value.trim()},
      voices: voices, paragraph_pause_ms: Number($('bookParagraphPause').value), chapters: bookPreview.chapters,
      gap_ms: Number($('bookImportGap').value),
      source: {filename: bookPreview.filename, sha256: bookPreview.sha256}});
    bookPreview = null;
    $('bookReview').hidden = true;
    await refreshBooks(result.book.id);
    bookNote(result.warnings.join(' ') || 'Book saved.');
  });
});

function bookStopAudio() {
  var audio = /** @type {HTMLAudioElement} */ ($('bookAudition'));
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  audio.hidden = true;
}
function refreshBooks(preferred) {
  return fetchJson('/api/books').then(function (result) {
    var select = $('bookSelect'), current = preferred || (activeBook && activeBook.id);
    select.replaceChildren();
    option(select, '', 'Choose a saved book…');
    result.books.forEach(function (book) { option(select, book.id, book.title + ' · ' + bookCount(book.chapters, 'chapter')); });
    if (result.books.some(function (book) { return book.id === current; })) select.value = current;
    else if (result.books.length) select.value = result.books[0].id;
    return openBook(select.value, true);
  });
}
function openBook(id, useFields) {
  window.clearTimeout(bookTimer);
  var read = ++bookRead;
  if (!id) {
    bookStopAudio();
    activeBook = bookDelivery = null;
    $('bookProject').hidden = true;
    return Promise.resolve();
  }
  return fetchJson('/api/books/' + id).then(function (result) {
    if (read !== bookRead) return;
    var previous = activeBook && activeBook.id;
    activeBook = result.book;
    bookDelivery = result.delivery;
    if (previous !== id) bookStopAudio();
    renderBook(useFields || previous !== id);
    if (location.hash === '#books' && activeBook.chapter_status.some(function (chapter) {
      return ['queued', 'running', 'pausing', 'cancelling'].includes(chapter.status);
    })) {
      bookTimer = window.setTimeout(function () {
        if (location.hash === '#books' && !bookBusy) openBook(id, false).catch(bookError);
        else if (location.hash === '#books') bookTimer = window.setTimeout(function () {
          if (location.hash === '#books') openBook(id, false).catch(bookError);
        }, 2500);
      }, 1750);
    }
  });
}
function bookCount(count, unit) {
  return count + ' ' + unit + (count === 1 ? '' : 's');
}
function bookDuration(seconds) {
  if (seconds < 60) return bookCount(Math.max(1, Math.round(seconds)), 'second');
  var minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return bookCount(minutes, 'minute');
  var hours = bookCount(Math.floor(minutes / 60), 'hour');
  return minutes % 60 ? hours + ' ' + bookCount(minutes % 60, 'minute') : hours;
}
var bookFormats = ['wav', 'flac', 'mp3', 'm4b'];
function selectedBookFormats() {
  return bookFormats.filter(function (name) {
    return $('book' + name[0].toUpperCase() + name.slice(1)).checked;
  });
}
function renderBookExportTiming() {
  if (!activeBook) return;
  var formats = selectedBookFormats();
  // Rough allowance for assembly, disk I/O, and the encoders that the user chose.
  var encoding = {wav: 0.005, flac: 0.01, mp3: 0.025, m4b: 0.045};
  var pace = formats.reduce(function (sum, format) { return sum + encoding[format]; }, 0.015);
  var seconds = 3 + 0.15 * activeBook.chapters.length + pace * activeBook.estimated_seconds;
  $('bookExport').disabled = !formats.length;
  $('bookExportTiming').textContent = formats.length
    ? 'Allow about ' + bookDuration(seconds) + ' for export. The time depends on your computer. '
      + 'Once started, the export cannot be stopped. The Lab finishes writing and checking the package.'
    : 'Choose at least one audio format to export.';
}
function renderBook(fields) {
  $('bookProject').hidden = !activeBook;
  if (!activeBook) return;
  var art = /** @type {HTMLImageElement} */ ($('bookCoverPreview'));
  art.hidden = !activeBook.cover;
  if (activeBook.cover) {
    var url = bookUrl() + '/cover.jpg?v=' + activeBook.cover.sha256;
    if (art.getAttribute('src') !== url) art.src = url;
  } else {
    art.removeAttribute('src');
  }
  if (fields) {
    Object.entries(bookFields).forEach(function ([key, id]) { $(id).value = activeBook.metadata[key] || ''; });
    $('bookGap').value = activeBook.gap_ms;
    $('bookPronounce').value = activeBook.pronunciations.map(function (row) { return row.source + ' = ' + row.replacement; }).join('\n');
  }
  $('bookEstimate').textContent = 'Estimated listening time · About ' + bookDuration(activeBook.estimated_seconds);
  renderBookExportTiming();
  var table = $('bookChapters');
  table.replaceChildren();
  activeBook.chapter_status.forEach(function (chapter, index) {
    var tr = document.createElement('tr');
    var title = document.createElement('th');
    title.scope = 'row';
    title.textContent = (index + 1) + '. ' + chapter.title;
    var state = document.createElement('td'), actions = document.createElement('td');
    state.textContent = chapter.selected + '/' + chapter.segments + ' selected · ' + chapter.status.replaceAll('_', ' ')
      + ' · about ' + bookDuration(chapter.estimated_seconds);
    actions.appendChild(sceneButton('Edit', 'Edit chapter ' + chapter.title, function () {
      if (sceneDirty && !window.confirm('Discard unsaved edits to the open scene?')) return;
      location.hash = '#scenes';
      refreshSceneList(chapter.id).catch(bookError);
    }));
    var running = ['queued', 'running', 'pausing', 'cancelling'].includes(chapter.status);
    if (!running) {
      var renderLabel = chapter.status === 'paused' ? 'Resume' : 'Render missing';
      actions.appendChild(sceneButton(renderLabel, renderLabel + ' for ' + chapter.title,
        function () { queueBook([chapter.id], false); }));
      if (chapter.status === 'review_takes' || chapter.status === 'ready') {
        actions.appendChild(sceneButton('Use newest takes', 'Use newest takes for ' + chapter.title, function () {
          bookChapterAction(chapter.id, '/choose', 'Selecting the chapter takes…');
        }));
      }
      if (chapter.status === 'ready') {
        actions.appendChild(sceneButton('Audition', 'Audition ' + chapter.title, function () {
          bookAction('Assembling the chapter audition…', async function () {
            var result = await fetchJson(bookUrl() + '/chapters/' + chapter.id + '/audition', {});
            var audio = /** @type {HTMLAudioElement} */ ($('bookAudition'));
            audio.src = result.audio_url;
            audio.hidden = false;
            bookNote('Audition ready · ' + chapter.title);
          });
        }));
        actions.appendChild(sceneButton('Rerender', 'Rerender ' + chapter.title, function () { queueBook([chapter.id], true); }));
      }
    }
    if (chapter.job && (running || chapter.status === 'paused')) {
      var verb = chapter.status === 'running' ? 'pause' : 'cancel';
      if (!['pausing', 'cancelling'].includes(chapter.status)) actions.appendChild(sceneButton(
        verb === 'pause' ? 'Pause' : 'Cancel', (verb === 'pause' ? 'Pause' : 'Cancel') + ' ' + chapter.title, function () {
          bookAction('Updating the chapter job…', async function () {
            await fetchJson('/api/scenes/jobs/' + chapter.job.id + '/' + verb, {});
            await openBook(activeBook.id, false);
            bookNote('Chapter job updated.');
          });
        }));
    }
    if (index > 0) actions.appendChild(sceneButton('Move earlier', 'Move earlier · ' + chapter.title, function () {
      var order = activeBook.chapters.slice();
      [order[index - 1], order[index]] = [order[index], order[index - 1]];
      saveBook({chapters: order});
    }));
    tr.append(title, state, actions);
    table.appendChild(tr);
  });
  paintBookDelivery();
}
function paintBookDelivery() {
  $('bookVerify').disabled = !bookDelivery;
  $('bookPlayM4b').disabled = !bookDelivery || !bookDelivery.files['book.m4b'];
  var data = bookDelivery;
  if (!data) {
    $('bookDownloads').replaceChildren();
    $('bookDeliverySummary').textContent = '';
    return;
  }
  var levels = data.loudness;
  $('bookDeliverySummary').textContent = 'Last export · ' + bookDuration(data.duration_seconds)
    + (levels.integrated_lufs == null ? '' : ' · ' + levels.integrated_lufs + ' LUFS')
    + (levels.true_peak_dbtp == null ? '' : ' · ' + levels.true_peak_dbtp + ' dBTP');
  var downloads = $('bookDownloads');
  downloads.replaceChildren();
  Object.keys(data.files).concat('manifest.json').forEach(function (name) {
    var link = document.createElement('a');
    link.href = bookUrl() + '/exports/' + data.id + '/' + encodeURIComponent(name);
    link.download = name;
    link.textContent = name;
    downloads.appendChild(link);
  });
}
function bookChapterAction(id, suffix, note) {
  bookAction(note, async function () {
    await fetchJson(bookUrl() + '/chapters/' + id + suffix, {});
    await openBook(activeBook.id, false);
    bookNote('Chapter updated.');
  });
}
function queueBook(chapters, rerender) {
  bookAction('Checking the render queue…', async function () {
    var data = await fetchJson(bookUrl() + '/queue', {chapters: chapters || undefined, rerender: rerender});
    await openBook(activeBook.id, false);
    bookNote(data.jobs.length ? 'Queued chapter jobs are saved.' : 'Every requested chapter already has current audio.');
  });
}
function saveBook(changes) {
  bookAction('Saving book details…', async function () {
    await fetchJson(bookUrl(), {expected_revision: activeBook.revision, ...changes});
    await refreshBooks(activeBook.id);
    bookNote('Book details saved.');
  });
}
$('bookSelect').addEventListener('change', function () { openBook(this.value, true).catch(bookError); });
$('bookQueue').addEventListener('click', function () { queueBook(null, false); });
$('bookSave').addEventListener('click', function () {
  try {
    var info = Object.fromEntries(Object.entries(bookFields).map(function ([key, id]) { return [key, $(id).value.trim()]; }));
    var rules = Object.create(null);
    $('bookPronounce').value.split('\n').filter(function (line) { return line.trim(); }).forEach(function (line) {
      var separator = line.indexOf('=');
      var word = line.slice(0, separator).trim(), spoken = line.slice(separator + 1).trim();
      if (separator < 0 || !word || !spoken) throw new Error('Add one pronunciation per line, such as harbor = har-bor.');
      rules[word] = spoken;
    });
    saveBook({metadata: info, gap_ms: Number($('bookGap').value), pronunciations: rules});
  } catch (error) { bookError(error); }
});
$('bookUploadCover').addEventListener('click', function () {
  var file = $('bookCover').files[0];
  if (!file) return bookNote('Choose a JPEG or PNG cover first.');
  bookAction('Saving cover…', async function () {
    await bookUpload(bookUrl() + '/cover?revision=' + activeBook.revision, file);
    $('bookCover').value = '';
    await openBook(activeBook.id, true);
    bookNote('Cover saved.');
  });
});
bookFormats.forEach(function (name) {
  $('book' + name[0].toUpperCase() + name.slice(1)).addEventListener('change', renderBookExportTiming);
});
$('bookExport').addEventListener('click', function () {
  var formats = selectedBookFormats();
  if (!formats.length) return;
  bookAction('Building and checking the audiobook. Downloads appear when the files pass their checks.', async function () {
    await fetchJson(bookUrl() + '/exports', {expected_revision: activeBook.revision, formats: formats});
    await openBook(activeBook.id, false);
    bookNote('Verified audiobook package ready.');
  });
});
$('bookVerify').addEventListener('click', function () {
  if (!bookDelivery) return;
  bookAction('Checking audiobook files…', async function () {
    var result = await fetchJson(bookUrl() + '/exports/' + bookDelivery.id + '/verify', {});
    bookNote(bookCount(result.files, 'file') + ' and ' + bookCount(result.chapters, 'chapter') + ' verified.');
  });
});

$('bookPlayM4b').addEventListener('click', function () {
  if (!bookDelivery || !bookDelivery.files['book.m4b']) return;
  var audio = /** @type {HTMLAudioElement} */ ($('bookAudition'));
  audio.src = bookUrl() + '/exports/' + bookDelivery.id + '/book.m4b';
  audio.hidden = false;
  audio.play().catch(bookError);
});
