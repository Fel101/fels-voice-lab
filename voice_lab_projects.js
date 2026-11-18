// Search saved projects and review portable archives before importing.
var productionProjects = [], archiveReview = null, reviewedFile = null;
var projectRead = 0, voiceSampleUrls = [], productionCast = [];
function projectsError(error) { $('projectsStatus').textContent = error.message || String(error); }
function productionIdentities() {
  return productionCast.flatMap(function(character) {
    return character.identities.filter(function(voice) {return !character.retired && !voice.retired;})
      .map(function(voice) {return {voice:voice, label:character.name+' · '+voice.label};});
  });
}
function projectKindName(kind) {
  return {book: LabText.t('projects.kind.book'), dub: LabText.t('projects.kind.dub'), scene: LabText.t('projects.kind.scene'), persona: LabText.t('projects.kind.persona')}[kind] || LabText.t('projects.archive_project');
}
function openProductionProject(kind, id) {
  if (kind !== 'book' && sceneDirty && !window.confirm(LabText.t('projects.discard_edits'))) return;
  if (kind === 'book') {
    location.hash = '#books';
    refreshBooks(id).catch(bookError);
  } else {
    location.hash = '#scenes';
    refreshSceneList(id, $('sceneEditor').value).catch(showSceneError);
  }
}
['homeImportScript', 'homeImportMore'].forEach(function (id) {
  $(id).addEventListener('click', function () {
    $('sceneScriptImport').open = true;
    requestAnimationFrame(function () { $('sceneImportFile').focus(); });
  });
});
async function refreshHome() {
  var list = $('homeRecent'), status = $('homeStatus');
  try {
    var result = await fetchJson('/api/projects/recent');
    list.replaceChildren();
    result.projects.forEach(function (project) {
      var row = document.createElement('li'), link = document.createElement('a'), kind = document.createElement('span');
      link.href = project.kind === 'book' ? '#books' : '#scenes';
      link.textContent = project.title;
      link.addEventListener('click', function(event) {
        event.preventDefault();
        openProductionProject(project.kind, project.id);
      });
      kind.className = 'meta';
      kind.textContent = projectKindName(project.kind);
      row.append(link, kind);
      list.appendChild(row);
    });
    $('homeRecentHeading').hidden = list.hidden = !list.children.length;
    $('homeNewWork').hidden = !list.children.length;
    $('homeStart').hidden = !!list.children.length;
    status.textContent = '';
  } catch (error) {
    status.textContent = LabText.t('home.recent_work_could_not_be');
  }
}
function projectJobStatus(status) {
  return {
    queued: LabText.t('projects.job_status.queued'),
    running: LabText.t('projects.job_status.running'),
    paused: LabText.t('projects.job_status.paused'),
    failed: LabText.t('projects.job_status.failed'),
    cancelled: LabText.t('projects.job_status.cancelled'),
    completed: LabText.t('projects.job_status.completed')
  }[status] || status;
}
async function refreshProjects() {
  var read = ++projectRead;
  var query = new URLSearchParams({q:$('projectsQuery').value,kind:$('projectsKind').value});
  var [result,cast] = await Promise.all([fetchJson('/api/projects?'+query.toString()),fetchJson('/api/cast')]);
  if (read !== projectRead) return;
  productionProjects = result.projects || []; productionCast = cast.characters || [];
  var rows = $('projectRows'), select = $('projectArchive');
  var oldProject = select.value;
  rows.textContent = ''; select.textContent = '';
  productionProjects.forEach(function(project) {
    var line = document.createElement('li'), title = document.createElement('a'), info = document.createElement('span');
    title.href = project.kind === 'book' ? '#books' : '#scenes';
    title.textContent = project.title;
    title.addEventListener('click', function(event) {event.preventDefault(); openProductionProject(project.kind,project.id);});
    var units = project.kind === 'book' ? LabText.plural('projects.passage_count', project.passages) : LabText.plural('projects.line_count', project.passages);
    info.textContent = ' · ' + [projectKindName(project.kind), units, LabText.plural('projects.voice_count', project.voices.length),
      LabText.plural('projects.take_count', project.selected_takes), projectJobStatus(project.latest_job_status)].filter(Boolean).join(' · ');
    line.append(title,info);
    [[LabText.t('projects.transcript'),project.transcript],[LabText.t('projects.job'),project.latest_job],
      [LabText.t('projects.selected_take'),project.selected_take_ids.length?'/api/takes/'+project.selected_take_ids[0]+'/audio':'']]
      .forEach(function(item) {
        if(!item[1]) return;
        var link=document.createElement('a'); link.textContent=' '+item[0]; link.href=item[1]; line.appendChild(link);
      });
    if (project.exports && project.exports.length) {
      var receipt = document.createElement('a');
      receipt.href = project.exports[0]; receipt.textContent = LabText.t('projects.latest_delivery');
      line.appendChild(receipt);
    }
    rows.appendChild(line);
    option(select, project.id, LabText.t('projects.archive_option', {title: project.title, kind: projectKindName(project.kind)}));
  });
  if (Array.from(select.options).some(function(item) {return item.value===oldProject;})) select.value=oldProject;
  $('projectsStatus').textContent = productionProjects.length ? '' : LabText.t('projects.no_results');
}
async function productionUpload(url, raw, options) {
  if (!raw) throw new Error(LabText.t('projects.choose_file'));
  var result = await fetch(url, {method:'POST',body:raw,headers:{'Content-Type':'application/octet-stream',
    'X-Reference-Metadata':encodeReferenceMetadata(options)}});
  var body = await result.json();
  if (!result.ok) throw new Error(LabText.error(body, LabText.t('projects.file_rejected')));
  return body;
}
async function productionExport() {
  var current = productionProjects.find(function(item) { return item.id === $('projectArchive').value; });
  if (!current) throw new Error(LabText.t('projects.choose_export'));
  $('projectsStatus').textContent = LabText.t('projects.preparing_archive');
  var result = await fetchJson('/api/portable/export', {
    kind: current.kind, id: current.id, include_media: $('projectArchiveMedia').checked,
    include_voices: $('projectArchiveVoices').checked, permission: $('archiveSharePermission').checked
  });
  $('projectDownload').href = result.archive.download;
  $('projectDownload').hidden = false;
  $('projectsStatus').textContent = LabText.t('projects.download_ready');
  $('archiveSharePermission').checked = false;
}
function openVoiceFileImport() {
  $('portableImportPanel').open = true;
  requestAnimationFrame(function () { $('portableFile').focus(); });
}
$('castImportVoiceFile').addEventListener('click', openVoiceFileImport);
function closeArchiveReview() {
  archiveReview = null; reviewedFile = null;
  voiceSampleUrls.forEach(function(url) {URL.revokeObjectURL(url);}); voiceSampleUrls = [];
  $('portableReview').hidden = true;
  $('portableMedia').checked = false; $('portablePermission').checked = false;
}
async function reviewPortable() {
  closeArchiveReview();
  var file = $('portableFile').files[0];
  var result = await productionUpload('/api/portable/preview',file,{});
  if (file !== $('portableFile').files[0]) return;
  archiveReview = result.archive; reviewedFile = file;
  $('portableTitle').textContent = LabText.plural('projects.review_title', Object.keys(archiveReview.assets || {}).length, {title: archiveReview.title, kind: projectKindName(archiveReview.kind)})+(archiveReview.author?' · '+archiveReview.author:'');
  var parent = $('portableMappings');
  parent.textContent = '';
  archiveReview.voices.forEach(function(source) {
    var field = document.createElement('label');
    field.className = 'field';
    var model = (source.model && source.model.name) || sceneModelName(source.engine_id);
    var message = source.label + ' · ' + model;
    if (source.sample) {
      var kind = source.sample.source_type === 'designed' ? LabText.t('projects.voice_designed') : LabText.t('projects.voice_cloned');
      message += ' · ' + LabText.t('projects.source_terms', {kind: kind, origin: source.sample.origin, terms: source.sample.allowed_use});
    } else if (source.model && source.model.allowed_use) message += ' · ' + source.model.allowed_use;
    if (!source.model_installed) message += ' · ' + LabText.t('projects.install_model', {model: model});
    field.textContent = message;
    var playable = source.audition || source.sample;
    if (playable) {
      var listen = document.createElement('button');
      listen.type = 'button'; listen.textContent = LabText.t('projects.hear_voice');
      listen.addEventListener('click',async function() {
        var chosen = reviewedFile;
        listen.disabled = true;
        try {
          var response = await fetch('/api/portable/voice-preview?asset='+encodeURIComponent(playable.asset),
            {method:'POST',body:chosen,headers:{'Content-Type':'application/octet-stream'}});
          if(!response.ok) throw new Error(LabText.t('projects.preview_unreadable'));
          var blob = await response.blob();
          if (chosen !== reviewedFile) return;
          var player = document.createElement('audio'), url = URL.createObjectURL(blob);
          voiceSampleUrls.push(url);
          player.controls = true; player.src = url; listen.replaceWith(player);
        } catch(error) {projectsError(error); listen.disabled = false;}
      });
      field.appendChild(listen);
    }
    if (archiveReview.kind !== 'persona') {
      var select = document.createElement('select');
      select.dataset.sourceVoice=source.id;
      option(select,'',LabText.t('projects.choose_voice'));
      productionIdentities().filter(function(row) {return row.voice.status==='approved';})
        .forEach(function(row) {option(select,row.voice.id,row.label);});
      field.appendChild(select);
      var candidate = document.createElement('button');
      candidate.type = 'button'; candidate.textContent = LabText.t('projects.candidate_named', {name: source.label});
      candidate.addEventListener('click',function() {importPortable(source.id).catch(projectsError);});
      field.appendChild(candidate);
    }
    parent.appendChild(field);
  });
  $('portablePermission').closest('label').hidden = !archiveReview.voices.some(function (source) { return source.sample; });
  $('portableMedia').closest('label').hidden = !(archiveReview.media || []).length;
  $('importPortable').textContent = archiveReview.kind === 'persona' ? LabText.t('projects.import_candidate') : LabText.t('projects.import_project');
  $('portableReview').hidden = false;
  $('projectsStatus').textContent = '';
}
function importedProjectKind(kind) {
  return {book: LabText.t('projects.import_kind.book'), dub: LabText.t('projects.import_kind.dub'), scene: LabText.t('projects.import_kind.scene')}[kind] || kind;
}
async function importPortable(candidate) {
  if (!archiveReview || reviewedFile !== $('portableFile').files[0]) throw new Error(LabText.t('projects.check_first'));
  var mappings = Object.fromEntries(Array.from($('portableMappings').querySelectorAll('select')).map(function(select) {
    return [select.dataset.sourceVoice, select.value];
  }));
  var body = {archive_sha256:archiveReview.archive_sha256,voices:mappings,include_media:$('portableMedia').checked,
    permission:$('portablePermission').checked,archive_voice_id:candidate};
  var result = await productionUpload('/api/portable/import',reviewedFile,body);
  if (result.imported.kind === 'persona') {
    await refreshCast();
    await refreshProjects();
    $('projectsStatus').textContent = LabText.t('projects.candidate_saved');
  } else {
    closeArchiveReview();
    await refreshProjects();
    $('projectsStatus').textContent = LabText.t('projects.project_saved', {kind: importedProjectKind(result.imported.kind)});
  }
}
$('searchProjects').addEventListener('click',function() {refreshProjects().catch(projectsError);});
$('projectsQuery').addEventListener('keydown',function(event) {
  if(event.key==='Enter') refreshProjects().catch(projectsError);
});
$('projectsKind').addEventListener('change',function() {refreshProjects().catch(projectsError);});
$('exportProjectArchive').addEventListener('click',function() {productionExport().catch(projectsError);});
$('portableFile').addEventListener('change',closeArchiveReview);
$('previewPortable').addEventListener('click',function() {reviewPortable().catch(projectsError);});
$('importPortable').addEventListener('click',function() {importPortable('').catch(projectsError);});
