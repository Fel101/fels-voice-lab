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
  return {book:'Book', dub:'Timed scene', scene:'Scene', persona:'Voice file'}[kind] || 'Project';
}
function openProductionProject(kind, id) {
  if (kind !== 'book' && sceneDirty && !window.confirm('Open a saved project and discard unsaved scene edits?')) return;
  if (kind === 'book') {
    location.hash = '#books';
    refreshBooks(id).catch(bookError);
  } else {
    location.hash = '#scenes';
    refreshSceneList(id).catch(showSceneError);
  }
}
$('homeImportScript').addEventListener('click', function () {
  $('sceneScriptImport').open = true;
  requestAnimationFrame(function () { $('sceneImportFile').focus(); });
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
    $('homeStart').hidden = !!list.children.length;
    status.textContent = '';
  } catch (error) {
    status.textContent = 'Recent work could not be loaded. Open a workspace from the menu, or reload this page to try again.';
  }
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
    var units = project.kind === 'book' ? sceneCount(project.passages, 'passage', 'passages') : sceneCount(project.passages, 'line', 'lines');
    info.textContent = ' · ' + [projectKindName(project.kind), units, sceneCount(project.voices.length, 'voice', 'voices'),
      sceneCount(project.selected_takes, 'selected take', 'selected takes'), project.latest_job_status].filter(Boolean).join(' · ');
    line.append(title,info);
    [['Transcript',project.transcript],['Job',project.latest_job],
      ['Selected take',project.selected_take_ids.length?'/api/takes/'+project.selected_take_ids[0]+'/audio':'']]
      .forEach(function(item) {
        if(!item[1]) return;
        var link=document.createElement('a'); link.textContent=' '+item[0]; link.href=item[1]; line.appendChild(link);
      });
    if (project.exports && project.exports.length) {
      var receipt = document.createElement('a');
      receipt.href = project.exports[0]; receipt.textContent = ' Latest delivery';
      line.appendChild(receipt);
    }
    rows.appendChild(line);
    option(select, project.id, project.title+' ('+projectKindName(project.kind)+')');
  });
  if (Array.from(select.options).some(function(item) {return item.value===oldProject;})) select.value=oldProject;
  $('projectsStatus').textContent = productionProjects.length ? '' : 'No projects match this search.';
}
async function productionUpload(url, raw, options) {
  if (!raw) throw new Error('Choose a local file first.');
  var result = await fetch(url, {method:'POST',body:raw,headers:{'Content-Type':'application/octet-stream',
    'X-Reference-Metadata':encodeReferenceMetadata(options)}});
  var body = await result.json();
  if (!result.ok) throw new Error(body.error || 'The local service rejected the file.');
  return body;
}
async function productionExport() {
  var current = productionProjects.find(function(item) { return item.id === $('projectArchive').value; });
  if (!current) throw new Error('Choose a saved project to export.');
  $('projectsStatus').textContent = 'Preparing and checking the project file…';
  var result = await fetchJson('/api/portable/export', {
    kind: current.kind, id: current.id, include_media: $('projectArchiveMedia').checked,
    include_voices: $('projectArchiveVoices').checked, permission: $('archiveSharePermission').checked
  });
  $('projectDownload').href = result.archive.download;
  $('projectDownload').hidden = false;
  $('projectsStatus').textContent = 'Project file ready to download.';
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
  $('portableTitle').textContent = archiveReview.title+' · '+projectKindName(archiveReview.kind)+' · '+
    Object.keys(archiveReview.assets || {}).length+' verified assets'+(archiveReview.author?' · '+archiveReview.author:'');
  var parent = $('portableMappings');
  parent.textContent = '';
  archiveReview.voices.forEach(function(source) {
    var field = document.createElement('label');
    field.className = 'field';
    var model = (source.model && source.model.name) || sceneModelName(source.engine_id);
    var message = source.label + ' · ' + model;
    if (source.sample) {
      var kind = source.sample.source_type === 'designed' ? 'Designed voice' : 'Cloned from a voice recording';
      message += ' · ' + kind + ' · Source ' + source.sample.origin + ' · Allowed use ' + source.sample.allowed_use;
    } else if (source.model && source.model.allowed_use) message += ' · ' + source.model.allowed_use;
    if (!source.model_installed) message += ' · Install ' + model + ' on Models & features to speak with this voice.';
    field.textContent = message;
    var playable = source.audition || source.sample;
    if (playable) {
      var listen = document.createElement('button');
      listen.type = 'button'; listen.textContent = 'Hear the voice';
      listen.addEventListener('click',async function() {
        var chosen = reviewedFile;
        listen.disabled = true;
        try {
          var response = await fetch('/api/portable/voice-preview?asset='+encodeURIComponent(playable.asset),
            {method:'POST',body:chosen,headers:{'Content-Type':'application/octet-stream'}});
          if(!response.ok) throw new Error('This voice preview could not be read.');
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
      option(select,'','Choose an approved voice');
      productionIdentities().filter(function(row) {return row.voice.status==='approved';})
        .forEach(function(row) {option(select,row.voice.id,row.label);});
      field.appendChild(select);
      var candidate = document.createElement('button');
      candidate.type = 'button'; candidate.textContent = 'Import '+source.label+' as candidate';
      candidate.addEventListener('click',function() {importPortable(source.id).catch(projectsError);});
      field.appendChild(candidate);
    }
    parent.appendChild(field);
  });
  $('portablePermission').closest('label').hidden = !archiveReview.voices.some(function (source) { return source.sample; });
  $('portableMedia').closest('label').hidden = !(archiveReview.media || []).length;
  $('importPortable').textContent = archiveReview.kind === 'persona' ? 'Import as candidate' : 'Import reviewed project';
  $('portableReview').hidden = false;
  $('projectsStatus').textContent = '';
}
async function importPortable(candidate) {
  if (!archiveReview || reviewedFile !== $('portableFile').files[0]) throw new Error('Check this archive first.');
  var mappings = Object.fromEntries(Array.from($('portableMappings').querySelectorAll('select')).map(function(select) {
    return [select.dataset.sourceVoice, select.value];
  }));
  var body = {archive_sha256:archiveReview.archive_sha256,voices:mappings,include_media:$('portableMedia').checked,
    permission:$('portablePermission').checked,archive_voice_id:candidate};
  var result = await productionUpload('/api/portable/import',reviewedFile,body);
  if (result.imported.kind === 'persona') {
    await refreshCast();
    await refreshProjects();
    $('projectsStatus').textContent = 'Imported a Cast candidate. Review and approve it in Cast before using it.';
  } else {
    closeArchiveReview();
    await refreshProjects();
    $('projectsStatus').textContent = 'Imported as a new '+result.imported.kind+' project.';
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
