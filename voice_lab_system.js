// System panel: what this PC is running, FFmpeg, disk and storage, library backups, and a report to copy for a bug report.
// The server decides every fact (and redacts the report); the page shows what it is told and never guesses from the browser.
var systemTaskTimer=null,backupPlan=null,suggestedFolder='';
function formatBytes(count){
  if(count===null||count===undefined)return LabText.t('system.unknown');
  var units=['bytes','KB','MB','GB','TB'],size=Number(count),index=0;
  while(size>=1024&&index<units.length-1){size/=1024;index++;}
  return (index===0?String(Math.round(size)):size.toFixed(1))+' '+units[index];
}
function whenText(iso){var date=new Date(iso);return isNaN(date.getTime())?String(iso||LabText.t('system.an_unknown_time')):LabText.dateTime(date);}
function systemFacts(report){
  var schemas=report.library.schemas,counts=report.library.counts,schemaText=Object.keys(schemas).map(function(key){return key+' '+(schemas[key]===null?LabText.t('system.unreadable'):schemas[key]);}).join(', ');
  var countText=[
    counts.characters == null ? '' : LabText.plural('system.character_count', counts.characters),
    counts.identities == null ? '' : LabText.plural('system.voice_count', counts.identities),
    counts.references == null ? '' : LabText.plural('system.voice_recording_count', counts.references),
    counts.takes == null ? '' : LabText.plural('system.take_count', counts.takes),
    counts.scenes == null ? '' : LabText.plural('system.scene_count', counts.scenes),
    counts.dubs == null ? '' : LabText.plural('system.dub_count', counts.dubs),
    counts.narrations == null ? '' : LabText.plural('system.narration_count', counts.narrations),
    counts.projects == null ? '' : LabText.plural('system.project_count', counts.projects),
    counts.audio_tools == null ? '' : LabText.plural('system.audio_tool_file_count', counts.audio_tools),
    counts.recipes == null ? '' : LabText.plural('system.recipe_count', counts.recipes)
  ].filter(Boolean).join(', ');
  var source=report.system.python_source,python=report.system.python+(source==="the Lab's own"?LabText.t('system.the_lab_s_own_2'):source?LabText.t('system.from', {source: source}):'');
  var rows=[[LabText.t('system.version'),report.app.name+' '+report.app.version],[LabText.t('system.python_and_system'),python+', '+report.system.platform],
    [LabText.t('system.device'),LabText.t('system.free_memory', {device:report.system.device, free_gb:report.system.free_ram_gb})],[LabText.t('system.library_folder'),report.library.path],
    [LabText.t('system.database_schemas'),schemaText||LabText.t('system.no_databases_yet')],[LabText.t('system.library_contents'),countText||LabText.t('system.nothing_yet')]];
  if(report.log&&report.log.file)rows.push([LabText.t('system.service_log'),report.log.file]);
  return rows;
}
function ffmpegSummary(ffmpeg){
  if(!ffmpeg||!ffmpeg.found)return {ok:false,text:(ffmpeg&&ffmpeg.message)||LabText.t('system.ffmpeg_was_not_found_on')};
  var names={wav:'WAV',flac:'FLAC',ogg:'Ogg',mp3:'MP3',m4a:'AAC'},formats=Object.keys(names).map(function(key){return names[key]+' '+(ffmpeg.formats&&ffmpeg.formats[key]?LabText.t('system.yes'):LabText.t('system.no'));}).join(', ');
  return {ok:!!ffmpeg.ready&&!ffmpeg.message,text:((!ffmpeg.ready ? LabText.t('system.ffmpeg_without_probe', {version: ffmpeg.version||'', formats: formats}) : LabText.t('system.ffmpeg_found_can_write', {version: ffmpeg.version||'', formats: formats})) + (ffmpeg.message?' '+ffmpeg.message:''))};
}
function diskSummary(label,row){
  if(!row)return {low:false,text:LabText.t('system.the_free_space_could_not', {drive: label})};
  var capacity = {drive: label, free: formatBytes(row.free_bytes), total: formatBytes(row.total_bytes)};
  return {low: !!row.low, text: row.low ? LabText.t('system.disk_space_low', capacity) : LabText.t('system.disk_space_available', capacity)};
}
function modelsSummary(engines){
  var words={usable:LabText.t('system.usable'),not_checked:LabText.t('system.installed_but_not_tested'),check_outdated:LabText.t('system.test_out_of_date'),check_failed:LabText.t('system.test_failed'),not_installed:LabText.t('system.not_installed'),conversion_task:LabText.t('system.conversion_tools')};
  var parts=Object.keys(engines.counts).filter(function(key){return engines.counts[key];}).map(function(key){return engines.counts[key]+' '+(words[key]||key);});
  return parts.length ? LabText.t('system.model_summary', {models: parts.join(', '), device: engines.device}) : LabText.t('system.no_models_are_integrated', {device: engines.device});
}
function systemNote(id,text,flagged){var node=$(id);node.textContent=text;node.className=flagged?'meta take-checks flagged':'meta';}
function renderSystem(report){
  var facts=$('systemFacts');facts.textContent='';
  systemFacts(report).forEach(function(row){var term=document.createElement('dt'),value=document.createElement('dd');term.textContent=row[0];value.textContent=row[1];facts.appendChild(term);facts.appendChild(value);});
  var ffmpeg=ffmpegSummary(report.ffmpeg);systemNote('systemFfmpeg',ffmpeg.text,!ffmpeg.ok);renderFfmpegOffer('system',report.ffmpeg);
  var library=diskSummary(LabText.t('system.library_drive'),report.disk.library),install=diskSummary(LabText.t('system.program_drive'),report.disk.install);
  systemNote('systemDisk',library.text+' '+install.text,library.low||install.low);
  $('systemModels').textContent=modelsSummary(report.engines);
  $('systemDiagnostics').textContent=report.diagnostics||'';
  $('systemCopy').disabled=!report.diagnostics;
}
function refreshSystem(recheck){
  $('systemError').textContent='';
  return fetchJson('/api/system'+(recheck?'?recheck=1':'')).then(renderSystem).catch(function(e){$('systemError').textContent=LabText.t('system.could_not_read_the_system', {message: e.message});});
}
function copyDiagnostics(){
  var text=$('systemDiagnostics').textContent||'',done=function(){$('systemCopied').textContent=LabText.t('system.copied_it_holds_no_scripts');},
    fallback=function(){var box=document.createElement('textarea');box.value=text;box.setAttribute('readonly','');document.body.appendChild(box);box.select();
      try{if(document.execCommand('copy'))done();else $('systemCopied').textContent=LabText.t('system.your_browser_blocked_copying_open');}catch(e){$('systemCopied').textContent=LabText.t('system.your_browser_blocked_copying_open');}
      document.body.removeChild(box);};
  if(navigator.clipboard&&navigator.clipboard.writeText)return navigator.clipboard.writeText(text).then(done,fallback);
  fallback();return Promise.resolve();
}
function storageLabel(key, label){return LabText.label('system.storage_section.'+key, label);}
function storageRows(storage){
  var rows=storage.library.map(function(row){return {label:storageLabel(row.key,row.label),bytes:row.bytes,backup:row.optional?'optional':'always'};});
  return rows.concat(storage.other.map(function(row){return {label:storageLabel(row.key,row.label),bytes:row.bytes,backup:'no'};}));
}
function cleanupText(cleanup){
  if(!cleanup.count)return LabText.t('system.nothing_to_clean_up_no');
  var counts=cleanup.by_kind;
  var parts=[
    counts.worker?.count ? LabText.plural('system.worker_scratch_folder_count', counts.worker.count) : '',
    counts.desktop?.count ? LabText.plural('system.old_desktop_session_count', counts.desktop.count) : '',
    counts.log?.count ? LabText.plural('system.old_service_log_count', counts.log.count) : '',
    counts.spoken?.count ? LabText.plural('system.folder_of_old_clips_for_other_apps_count', counts.spoken.count) : ''
  ].filter(Boolean);
  return LabText.t('system.cleanup_would_remove_it_never', {cleanup_items: parts.join(', '), bytes: formatBytes(cleanup.bytes)});
}
function renderStorage(storage){
  var body=$('systemStorage');body.textContent='';
  storageRows(storage).forEach(function(row){
    var tr=document.createElement('tr');[row.label,formatBytes(row.bytes),row.backup==='always'?LabText.t('system.always'):row.backup==='optional'?LabText.t('system.if_ticked'):LabText.t('system.no')].forEach(function(text){
      var cell=document.createElement('td');cell.textContent=text;tr.appendChild(cell);});body.appendChild(tr);});
  $('systemCleanup').textContent=cleanupText(storage.cleanup);
  $('systemClean').disabled=!storage.cleanup.count;
}
function measureStorage(){
  $('systemError').textContent='';$('systemMeasure').disabled=true;$('systemStatus').textContent=LabText.t('system.measuring_the_folders');
  return fetchJson('/api/system/storage').then(function(storage){renderStorage(storage);$('systemStatus').textContent='';})
    .catch(function(e){$('systemStatus').textContent='';$('systemError').textContent=LabText.t('system.could_not_measure_the_folders', {message: e.message});}).then(function(){$('systemMeasure').disabled=false;});
}
function runCleanup(){
  if(!window.confirm(LabText.t('system.remove_the_stopped_worker_folders')))return Promise.resolve();
  $('systemClean').disabled=true;$('systemError').textContent='';
  return fetchJson('/api/system/cleanup',{}).then(function(result){
    var message=LabText.t('system.removed', {items: LabText.plural('system.item_count', result.removed), bytes: formatBytes(result.bytes), failures: result.errors.length?LabText.t('system.cleanup_unremoved', {count: result.errors.length}):'.'});
    return measureStorage().then(function(){$('systemStatus').textContent=message;});
  }).catch(function(e){$('systemError').textContent=LabText.t('system.cleanup_did_not_finish', {message: e.message});});
}
function taskPercent(task){return task.status==='running'&&task.bytes_total?Math.min(100,Math.round(100*task.bytes_done/task.bytes_total)):null;}
function taskMessage(task){
  var result=task.result||{},warnings=result.warnings&&result.warnings.length?' '+result.warnings.join(' '):'';
  if(task.status==='running'){
    var percent=taskPercent(task)||0;
    return LabText.t('system.task_progress', {
      task: task.label, percent: percent, files_done: task.files_done, files_total: task.files_total,
      bytes_done: formatBytes(task.bytes_done), bytes_total: formatBytes(task.bytes_total)
    });
  }
  if(task.status==='failed')return task.error;
  if(task.status!=='done')return '';
  if(task.kind==='backup')return LabText.t('system.backup_saved_as_it_was', {name: result.name, bytes: formatBytes(result.bytes), files: LabText.plural('system.file_count', result.files), warnings: warnings});
  if(task.kind==='check')return result.ok
    ? LabText.t('system.passed_every_file_matches_its', {name: result.name})
    : LabText.t('system.did_not_pass', {name: result.name, problems: result.problems.join(' ')});
  if(task.kind==='restore')return LabText.t('system.restored_to_every_file_matched', {state_root: result.state_root, files: LabText.plural('system.file_count', result.files), takes: LabText.plural('system.take_count', result.counts.takes), warnings: warnings});
  return '';
}
function backupSections(plan){
  var box=$('backupSections');box.textContent='';var legend=document.createElement('legend');legend.textContent=LabText.t('system.also_include');box.appendChild(legend);
  plan.sections.filter(function(row){return row.optional;}).forEach(function(row){
    var label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.name='backupSection';input.value=row.key;input.checked=true;
    input.addEventListener('change',showBackupEstimate);
    label.appendChild(input);label.appendChild(document.createTextNode(' '+storageLabel(row.key,row.label)+' ('+formatBytes(row.bytes)+')'));box.appendChild(label);});
  showBackupEstimate();
}
function chosenBackupSections(){return [].slice.call(document.querySelectorAll('input[name=backupSection]')).filter(function(input){return input.checked;}).map(function(input){return input.value;});}
function showBackupEstimate(){
  if(!backupPlan)return;
  var chosen=chosenBackupSections(),total=0;
  backupPlan.sections.forEach(function(row){if(!row.optional||chosen.indexOf(row.key)>=0)total+=row.bytes;});
  $('backupSize').textContent=LabText.t('system.what_is_ticked_holds_about', {bytes: formatBytes(total)});
}
function backupRow(backup,busy){
  var article=document.createElement('article'),title=document.createElement('strong'),detail=document.createElement('p'),row=document.createElement('div');
  title.textContent=backup.name;article.appendChild(title);detail.className='meta';
  if(backup.problem){detail.textContent=LabText.t('system.this_file_does_not_read', {problem: backup.problem});article.appendChild(detail);}
  else{
    detail.textContent=(LabText.t('system.backup_summary', {backup_size: formatBytes(backup.bytes), files: LabText.plural('system.file_count', backup.files), date: whenText(backup.created_at), version: backup.app_version||LabText.t('system.version_unknown')})
      + (backup.skipped ? LabText.plural('system.backup_excluded', backup.skipped) : '')
      + (backup.missing_media ? LabText.plural('system.media_missing_count', backup.missing_media) : '') + '.');
    article.appendChild(detail);
    var holds=document.createElement('p');holds.className='meta';
    holds.textContent=LabText.t('system.holds', {sections: Object.keys(backup.sections||{}).map(function(key){return storageLabel(key,backup.sections[key].label);}).join(', ')});article.appendChild(holds);
  }
  row.className='row';
  var check=document.createElement('button');check.type='button';check.textContent=LabText.t('system.check');check.disabled=!!busy;
  check.addEventListener('click',function(){startBackupTask('/api/system/backups/verify',{name:backup.name});});row.appendChild(check);
  if(!backup.problem){var restore=document.createElement('button');restore.type='button';restore.textContent=LabText.t('system.restore_2');restore.disabled=!!busy;
    restore.addEventListener('click',function(){showRestoreForm(backup.name);});row.appendChild(restore);}
  article.appendChild(row);return article;
}
function showRestoreForm(name){
  $('restoreName').textContent=name;$('restoreFolder').value=suggestedFolder;$('restoreBox').hidden=false;$('restoreBox').dataset.backup=name;$('restoreFolder').focus();
}
function renderBackups(result){
  $('backupFolder').textContent=result.folder;suggestedFolder=result.suggested_folder||'';backupPlan=result.plan;
  if(!document.querySelector('input[name=backupSection]'))backupSections(result.plan);else showBackupEstimate();
  var list=$('backupList');list.textContent='';
  if(!result.backups.length){var none=document.createElement('p');none.className='meta';none.textContent=LabText.t('system.no_backups_yet');list.appendChild(none);}
  result.backups.forEach(function(backup){list.appendChild(backupRow(backup,result.task.status==='running'));});
  renderTask(result.task);
}
function renderTask(task){
  var bar=$('backupProgress'),percent=taskPercent(task);
  bar.hidden=percent===null;if(percent!==null)bar.value=percent;
  $('backupStatus').textContent=taskMessage(task);
  $('backupStatus').className=task.status==='failed'||(task.status==='done'&&task.kind==='check'&&!task.result.ok)?'meta take-checks flagged':'meta';
  var busy=task.status==='running';
  ['backupCreate','restoreGo'].forEach(function(id){$(id).disabled=busy;});
  var restored=task.status==='done'&&task.kind==='restore';
  $('restoreUse').hidden=!restored;$('restoreHint').hidden=!restored;
  if(restored){$('restoreUse').dataset.folder=task.result.folder;$('restoreCommand').textContent='.venv\\Scripts\\python.exe serve.py --state-root "'+task.result.state_root+'"';}
}
// The full list (and the library walk behind its sizes) is read when the page opens and when a job ends; while one runs only its small status is polled.
function refreshBackups(){
  clearTimeout(systemTaskTimer);
  return fetchJson('/api/system/backups').then(function(result){
    renderBackups(result);
    if(result.task.status==='running')systemTaskTimer=setTimeout(pollBackupTask,1000);
    return result;
  }).catch(function(e){$('systemError').textContent=LabText.t('system.could_not_list_the_backups', {message: e.message});});
}
function pollBackupTask(){
  return fetchJson('/api/system/task').then(function(result){
    if(result.task.status==='running'){renderTask(result.task);systemTaskTimer=setTimeout(pollBackupTask,1000);return result;}
    return refreshBackups();
  }).catch(function(e){$('systemError').textContent=LabText.t('system.could_not_read_the_job', {message: e.message});});
}
function startBackupTask(url,body){
  $('systemError').textContent='';
  return fetchJson(url,body).then(function(){return refreshBackups();}).catch(function(e){$('systemError').textContent=e.message;});
}
function startBackup(){return startBackupTask('/api/system/backups',{include:chosenBackupSections()});}
function startRestore(){
  var name=$('restoreBox').dataset.backup,folder=$('restoreFolder').value.trim();
  if(!folder){$('systemError').textContent=LabText.t('system.type_a_name_for_the');return Promise.resolve();}
  $('restoreBox').hidden=true;
  return startBackupTask('/api/system/backups/restore',{name:name,folder:folder});
}
function useRestoredLibrary(){
  if(!window.confirm(LabText.t('system.open_this_restored_library_the')))return Promise.resolve();
  return fetchJson('/api/system/use-library',{folder:$('restoreUse').dataset.folder}).then(function(result){
    $('backupStatus').textContent=LabText.t('system.restore_next_start', {state_root: result.state_root, settings_file: result.settings_file, previous: result.previous || '"."'});
  }).catch(function(e){$('systemError').textContent=e.message;});
}
// Updates: the service asks GitHub only when Check for updates is pressed, or once a day when the page opens if that is on.
// A newer release shows here and in the header, each with Update now.
function updateMessage(result){
  if(result.error)return LabText.t('system.the_last_check_failed', {error: result.error});
  return result.message+(result.checked_at?LabText.t('system.checked', {date: whenText(result.checked_at)}):'');
}
function renderUpdate(result){
  var newer=!!(result.newer&&result.release&&!result.error),link=$('updateRelease'),badge=$('updateBadge');
  systemNote('updateStatus',updateMessage(result),!!result.error);
  link.hidden=!newer;badge.hidden=!newer;$('updateNow').hidden=!newer;$('updateNowHeader').hidden=!newer;
  if(newer){link.href=result.release.url;link.textContent=LabText.t('system.what_s_new_in', {latest: result.latest});badge.textContent=LabText.t('system.version_is_available', {latest: result.latest});}
  return result;
}
// Update now: first the plan update.bat would show (POST /api/update/plan), in a window over the page; on yes the Lab closes,
// an updater in a window of its own updates it, and the Lab opens again (POST /api/update/start). The update itself is
// tools/update.py's, as update.bat's is. Afterwards the page says once what the update said (GET /api/update/notice).
var updateNowState={closing:false};
function updateNowParts(){return {box:$('updateDialog'),title:$('updateTitle'),state:$('updateState'),plan:$('updatePlan'),go:$('updateGo'),close:$('updateClose')};}
function updateLine(parent,text,className){var line=document.createElement('p');line.className=className||'meta';line.textContent=text;parent.appendChild(line);return line;}
function updateComesBack(how){
  return how==='window'
    ?LabText.t('system.the_lab_closes_a_window')
    :LabText.t('system.the_lab_closes_and_a');
}
function renderUpdatePlan(parts,result){
  var plan=result.plan||{};
  parts.plan.textContent='';parts.close.textContent=LabText.t('system.close');parts.go.hidden=true;
  if(!plan.ok){parts.state.textContent=plan.problem||LabText.t('system.the_update_cannot_go_ahead');return;}
  if(!plan.newer){parts.state.textContent=plan.message;return;}
  parts.title.textContent=LabText.t('system.update_from_to', {from: plan.from, to: plan.to});
  parts.state.textContent=result.blocked||LabText.t('system.this_is_what_it_will');
  (plan.steps||[]).forEach(function(text,index){updateLine(parts.plan,(index+1)+'. '+text);});
  (plan.notes||[]).forEach(function(text){updateLine(parts.plan,text);});
  updateLine(parts.plan,updateComesBack(result.how));
  if(plan.release&&plan.release.url){
    var link=document.createElement('a');link.href=plan.release.url;link.target='_blank';link.rel='noopener noreferrer';
    link.textContent=LabText.t('system.what_s_new_in_2', {to: plan.to});updateLine(parts.plan,'').appendChild(link);
  }
  parts.close.textContent=LabText.t('system.cancel');parts.go.hidden=false;parts.go.disabled=!!result.blocked;
}
function openUpdateNow(){
  var parts=updateNowParts();
  updateNowState={closing:false};
  parts.title.textContent=LabText.t('system.update_now');parts.plan.textContent='';
  parts.state.textContent=LabText.t('system.reading_what_the_update_will');
  parts.go.hidden=true;parts.go.disabled=false;parts.close.disabled=false;parts.close.textContent=LabText.t('system.cancel');
  if(!parts.box.open&&parts.box.showModal)parts.box.showModal();
  return fetchJson('/api/update/plan',{}).then(function(result){if(parts.box.open)renderUpdatePlan(parts,result);}).catch(function(e){
    parts.state.textContent=LabText.t('system.could_not_read_what_the', {message: e.message});parts.close.textContent=LabText.t('system.close');
  });
}
function startUpdateNow(){
  var parts=updateNowParts();
  if(typeof sceneDirty!=='undefined'&&sceneDirty&&!window.confirm(LabText.t('system.the_scene_has_changes_that')))return Promise.resolve();
  parts.go.disabled=true;parts.close.disabled=true;parts.state.textContent=LabText.t('system.closing_the_lab_for_the');
  return fetchJson('/api/update/start',{}).then(function(result){
    updateNowState.closing=true;parts.go.hidden=true;parts.plan.textContent='';
    parts.state.textContent=updateComesBack(result.how);
  }).catch(function(e){parts.state.textContent=LabText.t('system.the_update_did_not_start', {message: e.message});parts.go.disabled=false;parts.close.disabled=false;});
}
function closeUpdateNow(){var box=$('updateDialog');if(!updateNowState.closing&&box.open&&box.close)box.close();}
// The note an update leaves: shown once, at the top of every page, until Close this note.
function renderUpdateNotice(notice){
  var box=$('updateNotice'),title=$('updateNoticeTitle'),text=$('updateNoticeText'),list=$('updateNoticeAdvice'),link=$('updateNoticeLink');
  list.textContent='';link.hidden=true;box.hidden=!notice;
  if(!notice)return;
  var advice=notice.advice||[];
  if(notice.state==='updated'){
    title.textContent=LabText.t('system.updated_to_version', {to: notice.to});
    text.textContent=advice.length?LabText.t('system.your_voices_scenes_and_settings'):LabText.t('system.your_voices_scenes_models_and');
    if(notice.release_url){link.href=notice.release_url;link.textContent=LabText.t('system.what_changed_in', {to: notice.to});link.hidden=false;}
  }else if(notice.state==='undone'){
    title.textContent=LabText.t('system.back_to_version', {from: notice.from});
    text.textContent=advice.length?LabText.t('system.the_update_was_undone_some'):LabText.t('system.the_update_was_undone_your');
  }else if(notice.state==='current'){
    title.textContent=LabText.t('system.nothing_to_update');text.textContent=notice.message||LabText.t('system.this_is_the_newest_version');
  }else{
    title.textContent=LabText.t('system.the_update_did_not_finish');
    text.textContent=LabText.t('system.this_is_still_version', {release_note: notice.message||'', from: notice.from});
  }
  (notice.problems||[]).forEach(function(problem){var item=document.createElement('li');item.className='warn';item.textContent=problem;list.appendChild(item);});
  advice.forEach(function(entry){
    var item=document.createElement('li');item.appendChild(document.createTextNode(entry.text));
    if(entry.folder){
      var folder=document.createElement('button');folder.type='button';folder.textContent=LabText.t('system.open_the_lab_s_folder_2');
      folder.addEventListener('click',openLabFolder);item.appendChild(folder);
    }
    (entry.models||[]).forEach(function(model){
      var install=document.createElement('button');install.type='button';install.textContent=LabText.t('system.install', {name: model.name});
      install.addEventListener('click',function(){location.hash='#models';planInstall(model.engine,(modelCards[model.engine]||{}).installGpu);});
      item.appendChild(install);
    });
    list.appendChild(item);
  });
}
function refreshUpdateNotice(){return fetchJson('/api/update/notice').then(function(result){renderUpdateNotice(result.notice);}).catch(function(){});}
function dismissUpdateNotice(){$('updateNotice').hidden=true;return fetchJson('/api/update/notice/dismiss',{}).catch(function(){});}
function openLabFolder(){
  return fetchJson('/api/system/open-folder',{}).then(function(result){systemNote('openLabFolderStatus',LabText.t('system.opened_in_file_explorer', {folder: result.folder}),false);})
    .catch(function(e){systemNote('openLabFolderStatus',e.message,true);});
}
function checkForUpdates(quiet){
  $('updateCheck').disabled=true;
  if(!quiet)systemNote('updateStatus',LabText.t('system.asking_github'),false);
  return fetchJson('/api/updates/check',{}).then(renderUpdate).catch(function(e){
    if(!quiet)systemNote('updateStatus',LabText.t('system.could_not_check_for_updates', {message: e.message}),true);
  }).then(function(){$('updateCheck').disabled=false;});
}
function refreshUpdates(){
  return fetchJson('/api/updates').then(function(state){
    $('updateAuto').checked=!!state.check_on_open;
    if(state.last)renderUpdate(state.last);
    if(state.due)return checkForUpdates(true);
  }).catch(function(e){systemNote('updateStatus',LabText.t('system.could_not_read_the_update', {message: e.message}),true);});
}
function saveUpdateChoice(){
  var box=$('updateAuto'),on=box.checked;
  return fetchJson('/api/preferences',{check_updates:on}).then(function(){if(on)return checkForUpdates(false);}).catch(function(e){
    box.checked=!on;systemNote('updateStatus',LabText.t('system.that_choice_could_not_be', {message: e.message}),true);
  });
}
// Install FFmpeg: the button here and the one on the export panel start the same install (POST /api/ffmpeg/install), which the
// service runs as tools/install_ffmpeg.py. The page follows it (GET /api/ffmpeg/install) and asks FFmpeg again when it ends.
var ffmpegJob=null,ffmpegTimer=null,ffmpegOffers={system:null,export:null},ffmpegError='';
var FFMPEG_VIEWS={system:['systemFfmpegInstall','systemFfmpegProgress'],export:['exportFfmpegInstall','exportFfmpegProgress']};
// In megabytes of a million bytes, as the installer's own progress lines count them, so the offer and the download agree.
function ffmpegMegabytes(bytes){ return Math.round(bytes/1e6)+' MB'; }
function ffmpegOfferText(offer){
  return LabText.t('system.install_ffmpeg_puts_ffmpeg_in', {version: offer.version, builder: offer.builder, license: offer.license, download_size: ffmpegMegabytes(offer.download_bytes), installed_size: ffmpegMegabytes(offer.disk_bytes)});
}
function ffmpegJobText(job){
  var lines=(job.lines||[]).map(function(line){return line.trim();}).filter(Boolean);
  if(job.state==='running')return LabText.t('system.installing_ffmpeg_it_cannot_be', {progress: lines.at(-1)||LabText.t('system.install_starting')});
  if(job.state==='finished')return LabText.t('system.ffmpeg_is_installed');
  var errors=lines.filter(function(line){return /^error:/.test(line);}).map(function(line){return line.replace(/^error:\s*/,'');});
  return LabText.t('system.ffmpeg_was_not_installed_press', {error: errors.join(' ')||lines.slice(-1)[0]||LabText.t('system.install_stopped_without_reason')});
}
function renderFfmpegOffer(where,report){
  if(report!==undefined)ffmpegOffers[where]=report&&report.offer||null;
  var ids=FFMPEG_VIEWS[where],button=$(ids[0]),note=$(ids[1]),offer=ffmpegOffers[where],job=ffmpegJob,running=!!job&&job.state==='running';
  button.hidden=!offer&&!running;
  button.disabled=running;
  button.textContent=running?LabText.t('system.installing_ffmpeg'):LabText.t('system.install_ffmpeg');
  var text=ffmpegError||(job&&(running||job.state==='failed'||!offer)?ffmpegJobText(job):offer?ffmpegOfferText(offer):'');
  note.textContent=text;note.hidden=!text;
  note.className=ffmpegError||(job&&job.state==='failed')?'meta take-checks flagged':'meta';
}
function renderFfmpegJob(job){
  var was=!!ffmpegJob&&ffmpegJob.state==='running';
  ffmpegJob=job||null;
  renderFfmpegOffer('system');renderFfmpegOffer('export');
  clearTimeout(ffmpegTimer);
  if(ffmpegJob&&ffmpegJob.state==='running'){ffmpegTimer=setTimeout(pollFfmpegInstall,1000);}
  else if(was){refreshSystem(true);if(typeof refreshExports==='function')refreshExports(undefined,true);}
}
function pollFfmpegInstall(){
  return fetchJson('/api/ffmpeg/install').then(function(result){ffmpegError='';renderFfmpegJob(result.install);}).catch(function(e){
    ffmpegError=LabText.t('system.could_not_follow_the_ffmpeg', {message: e.message});renderFfmpegJob(null);});
}
function startFfmpegInstall(){
  ffmpegError='';
  return fetchJson('/api/ffmpeg/install',{}).then(function(result){renderFfmpegJob(result.install);}).catch(function(e){
    ffmpegError=LabText.t('system.ffmpeg_install_error', {message: e.message});renderFfmpegOffer('system');renderFfmpegOffer('export');});
}
// Connect other apps: where programs on this computer reach this service, a command that tests it, and the port the Lab's
// window asks for (desktop.local.json). The address file and the known port are lab_address.py's.
var connectInfo=null;
function connectNoteText(info){
  if(info.url===info.known_url)return '';
  // Without a note from the service, the port was chosen: by the window's setting below, or when the service was started.
  var port=Number(info.url.split(':').pop());
  var why=info.note||(info.window_port===0?LabText.t('system.the_lab_s_window_took')
    :port===info.window_port?LabText.t('system.the_lab_s_window_asks', {port: port})
    :LabText.t('system.this_service_was_started_on', {port: port}));
  return why+' '+(info.in_address_file?LabText.t('system.programs_that_read_the_address', {address_file: info.address_file})
    :LabText.t('system.give_programs_this_address_the'));
}
function renderConnect(info){
  connectInfo=info;
  $('connectUrl').textContent=info.url;
  $('connectTest').textContent=info.test;
  $('connectGuide').href=info.guide;
  $('connectMcp').textContent=info.mcp.claude_code;
  $('connectMcpGuide').href=info.connect_guide;
  $('connectOpenAI').textContent=info.openai_base;
  var note=connectNoteText(info);$('connectNote').textContent=note;$('connectNote').hidden=!note;
  $('connectPort').value=String(info.window_port);
}
function refreshConnect(){
  return fetchJson('/api/system/connect').then(renderConnect).catch(function(e){systemNote('connectStatus',LabText.t('system.could_not_read_the_lab', {message: e.message}),true);});
}
function copyText(text){
  var fallback=function(){var box=document.createElement('textarea');box.value=text;box.setAttribute('readonly','');document.body.appendChild(box);box.select();
    var copied=false;try{copied=document.execCommand('copy');}catch(e){copied=false;}document.body.removeChild(box);return copied;};
  if(navigator.clipboard&&navigator.clipboard.writeText)return navigator.clipboard.writeText(text).then(function(){return true;},fallback);
  return Promise.resolve(fallback());
}
// What each Copy button copies, and what the note says once it has.
var CONNECT_COPIES={
  url:function(info){return [info.url,LabText.t('system.copied', {url: info.url})];},
  test:function(info){return [info.test,LabText.t('system.copied_the_test_command_paste')];},
  mcp:function(info){return [info.mcp.claude_code,LabText.t('system.copied_the_claude_code_line')];},
  openai:function(info){return [info.openai_base,LabText.t('system.copied_openai_address', {address: info.openai_base})];},
  settings:function(info){return [info.mcp.settings,LabText.t('system.copied_the_settings_paste_them')];}
};
function copyConnect(which){
  if(!connectInfo)return Promise.resolve();
  var chosen=CONNECT_COPIES[which](connectInfo);
  return copyText(chosen[0]).then(function(copied){
    systemNote('connectStatus',!copied?LabText.t('system.your_browser_blocked_copying_select'):chosen[1],!copied);
  });
}
// The settings for other MCP apps are not shown on the page, so a browser that blocks copying is answered with them.
function copyMcpSettings(){
  return copyConnect('settings').then(function(){
    if(connectInfo&&/blocked copying/.test($('connectStatus').textContent))
      systemNote('connectStatus',LabText.t('system.your_browser_blocked_copying_these', {replace: connectInfo.mcp.settings.replace(/\s+/g,' ')}),true);
  });
}
function saveConnectPort(){
  var text=$('connectPort').value.trim(),port=Number(text);
  if(!text||!Number.isInteger(port)){systemNote('connectPortStatus',LabText.t('system.type_a_whole_number_0'),true);return Promise.resolve();}
  return fetchJson('/api/system/connect/port',{port:port}).then(function(result){
    systemNote('connectPortStatus',(result.window_port===0 ? LabText.t('system.window_free_port') : LabText.t('system.window_specific_port', {port: result.window_port})),false);
  }).catch(function(e){systemNote('connectPortStatus',e.message,true);});
}
// Install speech recognition.
var asrInstallTimer = null;
async function refreshAsrInstall() {
  clearTimeout(asrInstallTimer);
  try {
    var status = await fetchJson('/api/asr/install');
    var running = status.install && status.install.state === 'running';
    var ready = status.speech_recognition.ready;
    $('systemAsrInstall').hidden = ready && !running;
    $('systemAsrInstall').disabled = !!running;
    $('systemAsrState').textContent = running
      ? LabText.t('system.installing_speech_recognition_close_the')
      : ready ? LabText.t('system.speech_recognition_is_ready_audio')
        : status.install && status.install.state === 'failed' ? LabText.t('system.the_install_stopped_choose_install')
          : LabText.t('system.install_speech_recognition_to_draft');
    if (running) asrInstallTimer = setTimeout(refreshAsrInstall, 1200);
  } catch (_) {
    $('systemAsrInstall').disabled = false;
    $('systemAsrState').textContent = LabText.t('system.could_not_check_speech_recognition');
  }
}
$('systemAsrInstall').addEventListener('click', async function () {
  var button = $('systemAsrInstall');
  button.disabled = true;
  try {
    var response = await fetchJson('/api/asr/install',{dry_run:true});
    var plan = response.plan;
    if (!plan.ok) throw new Error(plan.problems.join(' ') || LabText.t('system.check_the_available_disk_space'));
    if (!window.confirm(LabText.t('system.install_speech_recognition_this_downloads'))) { button.disabled = false; return; }
    await fetchJson('/api/asr/install',{});
    await refreshAsrInstall();
  } catch (error) {
    button.disabled = false;
    $('systemAsrState').textContent = error.message;
  }
});
function refreshSystemPage(){return Promise.all([refreshSystem(false),refreshBackups(),pollFfmpegInstall(),refreshAsrInstall(),refreshConnect()]);}
$('systemRefresh').addEventListener('click',function(){refreshSystemPage();});
$('systemCopy').addEventListener('click',function(){copyDiagnostics();});
$('systemFfmpegCheck').addEventListener('click',function(){refreshSystem(true);});
$('systemFfmpegInstall').addEventListener('click',startFfmpegInstall);
$('exportFfmpegInstall').addEventListener('click',startFfmpegInstall);
$('systemMeasure').addEventListener('click',measureStorage);
$('systemClean').addEventListener('click',runCleanup);
$('backupCreate').addEventListener('click',startBackup);
$('restoreGo').addEventListener('click',startRestore);
$('restoreCancel').addEventListener('click',function(){$('restoreBox').hidden=true;});
$('restoreUse').addEventListener('click',useRestoredLibrary);
$('updateCheck').addEventListener('click',function(){checkForUpdates(false);});
$('updateAuto').addEventListener('change',saveUpdateChoice);
$('updateNow').addEventListener('click',openUpdateNow);
$('updateNowHeader').addEventListener('click',openUpdateNow);
$('updateGo').addEventListener('click',startUpdateNow);
$('updateClose').addEventListener('click',closeUpdateNow);
$('updateDialog').addEventListener('cancel',function(event){if(updateNowState.closing)event.preventDefault();});
$('updateNoticeClose').addEventListener('click',dismissUpdateNotice);
$('openLabFolder').addEventListener('click',openLabFolder);
$('connectCopy').addEventListener('click',function(){copyConnect('url');});
$('connectCopyTest').addEventListener('click',function(){copyConnect('test');});
$('connectCopyMcp').addEventListener('click',function(){copyConnect('mcp');});
$('connectCopyMcpSettings').addEventListener('click',function(){copyMcpSettings();});
$('connectCopyOpenAI').addEventListener('click',function(){copyConnect('openai');});
$('connectPortSave').addEventListener('click',saveConnectPort);
refreshUpdates();
refreshUpdateNotice();
