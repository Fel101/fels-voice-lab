// System panel: what this PC is running, FFmpeg, disk and storage, library backups, and a report to copy for a bug report.
// The server decides every fact (and redacts the report); the page shows what it is told and never guesses from the browser.
var systemTaskTimer=null,backupPlan=null,suggestedFolder='';
function formatBytes(count){
  if(count===null||count===undefined)return 'unknown';
  var units=['bytes','KB','MB','GB','TB'],size=Number(count),index=0;
  while(size>=1024&&index<units.length-1){size/=1024;index++;}
  return (index===0?String(Math.round(size)):size.toFixed(1))+' '+units[index];
}
function whenText(iso){var date=new Date(iso);return isNaN(date.getTime())?String(iso||'an unknown time'):date.toLocaleString();}
function systemFacts(report){
  var schemas=report.library.schemas,counts=report.library.counts,schemaText=Object.keys(schemas).map(function(key){return key+' '+(schemas[key]===null?'unreadable':schemas[key]);}).join(', ');
  var countText=[['characters','character','characters'],['identities','voice identity','voice identities'],['references','reference','references'],['takes','take','takes'],['scenes','scene','scenes']].filter(function(row){
    return counts[row[0]]!==null&&counts[row[0]]!==undefined;}).map(function(row){return plural(counts[row[0]],row[1],row[2]);}).join(', ');
  var source=report.system.python_source,python=report.system.python+(source==="the Lab's own"?' (the Lab\'s own)':source?' (from '+source+')':'');
  var rows=[['Version',report.app.name+' '+report.app.version],['Python and system',python+', '+report.system.platform],
    ['Device',report.system.device+', '+report.system.free_ram_gb+' GB of memory free'],['Library folder',report.library.path],
    ['Database schemas',schemaText||'no databases yet'],['Library contents',countText||'nothing yet']];
  if(report.log&&report.log.file)rows.push(['Service log',report.log.file]);
  return rows;
}
function ffmpegSummary(ffmpeg){
  if(!ffmpeg||!ffmpeg.found)return {ok:false,text:(ffmpeg&&ffmpeg.message)||'FFmpeg was not found on this PC.'};
  var names={wav:'WAV',flac:'FLAC',ogg:'Ogg',mp3:'MP3',m4a:'AAC'},formats=Object.keys(names).map(function(key){return names[key]+' '+(ffmpeg.formats&&ffmpeg.formats[key]?'yes':'no');}).join(', ');
  return {ok:!!ffmpeg.ready&&!ffmpeg.message,text:'FFmpeg '+(ffmpeg.version||'')+' found'+(ffmpeg.ready?'':', but ffprobe was not')+'. Can write: '+formats+'.'+(ffmpeg.message?' '+ffmpeg.message:'')};
}
function diskSummary(label,row){
  if(!row)return {low:false,text:label+': the free space could not be read.'};
  return {low:!!row.low,text:label+': '+formatBytes(row.free_bytes)+' free of '+formatBytes(row.total_bytes)+(row.low?'. That is low; a backup and new takes need room.':'.')};
}
function modelsSummary(engines){
  var words={usable:'usable',not_checked:'installed but not tested',check_outdated:'test out of date',check_failed:'test failed',not_installed:'not installed',conversion_task:'conversion tools'};
  var parts=Object.keys(engines.counts).filter(function(key){return engines.counts[key];}).map(function(key){return engines.counts[key]+' '+(words[key]||key);});
  return (parts.length?parts.join(', '):'No models are integrated')+' (device '+engines.device+').';
}
function plural(count,one,many){return count+' '+(count===1?one:many);}
function systemNote(id,text,flagged){var node=$(id);node.textContent=text;node.className=flagged?'meta take-checks flagged':'meta';}
function renderSystem(report){
  var facts=$('systemFacts');facts.textContent='';
  systemFacts(report).forEach(function(row){var term=document.createElement('dt'),value=document.createElement('dd');term.textContent=row[0];value.textContent=row[1];facts.appendChild(term);facts.appendChild(value);});
  var ffmpeg=ffmpegSummary(report.ffmpeg);systemNote('systemFfmpeg',ffmpeg.text,!ffmpeg.ok);
  var library=diskSummary('Library drive',report.disk.library),install=diskSummary('Program drive',report.disk.install);
  systemNote('systemDisk',library.text+' '+install.text,library.low||install.low);
  $('systemModels').textContent=modelsSummary(report.engines);
  $('systemDiagnostics').textContent=report.diagnostics||'';
  $('systemCopy').disabled=!report.diagnostics;
}
function refreshSystem(recheck){
  $('systemError').textContent='';
  return fetchJson('/api/system'+(recheck?'?recheck=1':'')).then(renderSystem).catch(function(e){$('systemError').textContent='Could not read the system report: '+e.message;});
}
function copyDiagnostics(){
  var text=$('systemDiagnostics').textContent||'',done=function(){$('systemCopied').textContent='Copied. It holds no scripts, names or recordings, and your home folder reads as ~. Read it under Show the report before you share it.';},
    fallback=function(){var box=document.createElement('textarea');box.value=text;box.setAttribute('readonly','');document.body.appendChild(box);box.select();
      try{if(document.execCommand('copy'))done();else $('systemCopied').textContent='Your browser blocked copying. Open Show the report, select the text and copy it yourself.';}catch(e){$('systemCopied').textContent='Your browser blocked copying. Open Show the report, select the text and copy it yourself.';}
      document.body.removeChild(box);};
  if(navigator.clipboard&&navigator.clipboard.writeText)return navigator.clipboard.writeText(text).then(done,fallback);
  fallback();return Promise.resolve();
}
function storageRows(storage){
  var rows=storage.library.map(function(row){return {label:row.label,bytes:row.bytes,backup:row.optional?'optional':'always'};});
  return rows.concat(storage.other.map(function(row){return {label:row.label,bytes:row.bytes,backup:'no'};}));
}
function cleanupText(cleanup){
  if(!cleanup.count)return 'Nothing to clean up: no stopped-worker folders, old desktop sessions or old logs are waiting.';
  var kinds={worker:['worker scratch folder','worker scratch folders'],desktop:['old desktop session','old desktop sessions'],log:['old service log','old service logs']};
  var parts=Object.keys(kinds).filter(function(key){return cleanup.by_kind[key].count;}).map(function(key){return plural(cleanup.by_kind[key].count,kinds[key][0],kinds[key][1]);});
  return 'Cleanup would remove '+parts.join(', ')+' ('+formatBytes(cleanup.bytes)+'). It never touches Cast, Scenes, exports or backups.';
}
function renderStorage(storage){
  var body=$('systemStorage');body.textContent='';
  storageRows(storage).forEach(function(row){
    var tr=document.createElement('tr');[row.label,formatBytes(row.bytes),row.backup==='always'?'always':row.backup==='optional'?'if ticked':'no'].forEach(function(text){
      var cell=document.createElement('td');cell.textContent=text;tr.appendChild(cell);});body.appendChild(tr);});
  $('systemCleanup').textContent=cleanupText(storage.cleanup);
  $('systemClean').disabled=!storage.cleanup.count;
}
function measureStorage(){
  $('systemError').textContent='';$('systemMeasure').disabled=true;$('systemStatus').textContent='Measuring the folders…';
  return fetchJson('/api/system/storage').then(function(storage){renderStorage(storage);$('systemStatus').textContent='';})
    .catch(function(e){$('systemStatus').textContent='';$('systemError').textContent='Could not measure the folders: '+e.message;}).then(function(){$('systemMeasure').disabled=false;});
}
function runCleanup(){
  if(!window.confirm('Remove the stopped-worker folders, old desktop sessions and old service logs listed above? Cast, Scenes, exports and backups are not touched.'))return Promise.resolve();
  $('systemClean').disabled=true;$('systemError').textContent='';
  return fetchJson('/api/system/cleanup',{}).then(function(result){
    var message='Removed '+plural(result.removed,'item','items')+' ('+formatBytes(result.bytes)+')'+(result.errors.length?'; '+result.errors.length+' could not be removed.':'.');
    return measureStorage().then(function(){$('systemStatus').textContent=message;});
  }).catch(function(e){$('systemError').textContent='Cleanup did not finish: '+e.message;});
}
function taskPercent(task){return task.status==='running'&&task.bytes_total?Math.min(100,Math.round(100*task.bytes_done/task.bytes_total)):null;}
function taskMessage(task){
  var result=task.result||{},warnings=result.warnings&&result.warnings.length?' '+result.warnings.join(' '):'';
  if(task.status==='running'){
    var percent=taskPercent(task)||0;
    return task.label+': '+percent+'% ('+task.files_done+' of '+task.files_total+' files, '+formatBytes(task.bytes_done)+' of '+formatBytes(task.bytes_total)+').';
  }
  if(task.status==='failed')return task.error;
  if(task.status!=='done')return '';
  if(task.kind==='backup')return 'Backup saved as '+result.name+' ('+formatBytes(result.bytes)+', '+plural(result.files,'file','files')+'). It was read back and every file matched its checksum.'+warnings;
  if(task.kind==='check')return result.ok?result.name+' passed: every file matches its checksum and each database passes SQLite’s integrity check.':result.name+' did not pass. '+result.problems.join(' ');
  if(task.kind==='restore')return 'Restored to '+result.state_root+' ('+plural(result.files,'file','files')+', '+plural(result.counts.takes,'take','takes')+'). Every file matched its checksum and the databases opened. Your current library was not touched.'+warnings;
  return '';
}
function backupSections(plan){
  var box=$('backupSections');box.textContent='';var legend=document.createElement('legend');legend.textContent='Also include';box.appendChild(legend);
  plan.sections.filter(function(row){return row.optional;}).forEach(function(row){
    var label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.name='backupSection';input.value=row.key;input.checked=true;
    input.addEventListener('change',showBackupEstimate);
    label.appendChild(input);label.appendChild(document.createTextNode(' '+row.label+' ('+formatBytes(row.bytes)+')'));box.appendChild(label);});
  showBackupEstimate();
}
function chosenBackupSections(){return [].slice.call(document.querySelectorAll('input[name=backupSection]')).filter(function(input){return input.checked;}).map(function(input){return input.value;});}
function showBackupEstimate(){
  if(!backupPlan)return;
  var chosen=chosenBackupSections(),total=0;
  backupPlan.sections.forEach(function(row){if(!row.optional||chosen.indexOf(row.key)>=0)total+=row.bytes;});
  $('backupSize').textContent='What is ticked holds about '+formatBytes(total)+' before compression. The Cast, Scenes and Comparisons folders and the appearance setting are always included.';
}
function backupRow(backup,busy){
  var article=document.createElement('article'),title=document.createElement('strong'),detail=document.createElement('p'),row=document.createElement('div');
  title.textContent=backup.name;article.appendChild(title);detail.className='meta';
  if(backup.problem){detail.textContent='This file does not read as a backup: '+backup.problem;article.appendChild(detail);}
  else{
    detail.textContent=formatBytes(backup.bytes)+', '+plural(backup.files,'file','files')+', made '+whenText(backup.created_at)+' by version '+(backup.app_version||'unknown')+
      (backup.skipped?'; '+plural(backup.skipped,'item','items')+' left out':'')+(backup.missing_media?'; '+plural(backup.missing_media,'media file was','media files were')+' already missing from the library':'')+'.';
    article.appendChild(detail);
    var holds=document.createElement('p');holds.className='meta';
    holds.textContent='Holds: '+Object.keys(backup.sections||{}).map(function(key){return backup.sections[key].label;}).join(', ')+'.';article.appendChild(holds);
  }
  row.className='row';
  var check=document.createElement('button');check.type='button';check.textContent='Check';check.disabled=!!busy;
  check.addEventListener('click',function(){startBackupTask('/api/system/backups/verify',{name:backup.name});});row.appendChild(check);
  if(!backup.problem){var restore=document.createElement('button');restore.type='button';restore.textContent='Restore…';restore.disabled=!!busy;
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
  if(!result.backups.length){var none=document.createElement('p');none.className='meta';none.textContent='No backups yet.';list.appendChild(none);}
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
  }).catch(function(e){$('systemError').textContent='Could not list the backups: '+e.message;});
}
function pollBackupTask(){
  return fetchJson('/api/system/task').then(function(result){
    if(result.task.status==='running'){renderTask(result.task);systemTaskTimer=setTimeout(pollBackupTask,1000);return result;}
    return refreshBackups();
  }).catch(function(e){$('systemError').textContent='Could not read the job status: '+e.message;});
}
function startBackupTask(url,body){
  $('systemError').textContent='';
  return fetchJson(url,body).then(function(){return refreshBackups();}).catch(function(e){$('systemError').textContent=e.message;});
}
function startBackup(){return startBackupTask('/api/system/backups',{include:chosenBackupSections()});}
function startRestore(){
  var name=$('restoreBox').dataset.backup,folder=$('restoreFolder').value.trim();
  if(!folder){$('systemError').textContent='Type a name for the new folder.';return Promise.resolve();}
  $('restoreBox').hidden=true;
  return startBackupTask('/api/system/backups/restore',{name:name,folder:folder});
}
function useRestoredLibrary(){
  if(!window.confirm('Open this restored library the next time the desktop app starts? Your current library stays where it is, and you can switch back.'))return Promise.resolve();
  return fetchJson('/api/system/use-library',{folder:$('restoreUse').dataset.folder}).then(function(result){
    $('backupStatus').textContent='Next start: the desktop app opens '+result.state_root+'. Close and reopen Fel’s Voice Lab to switch. To go back, set state_root in '+result.settings_file+(result.previous?' to '+result.previous:' to "."')+' (or delete that line).';
  }).catch(function(e){$('systemError').textContent=e.message;});
}
// Updates: the service asks GitHub only when Check for updates is pressed, or once a day when the page opens if that is on.
// A newer release shows here and in the header; the update itself is update.bat's job, with the Lab closed.
function updateMessage(result){
  if(result.error)return 'The last check failed: '+result.error;
  return result.message+(result.checked_at?' Checked '+whenText(result.checked_at)+'.':'');
}
function renderUpdate(result){
  var newer=!!(result.newer&&result.release&&!result.error),link=$('updateRelease'),badge=$('updateBadge');
  systemNote('updateStatus',updateMessage(result),!!result.error);
  link.hidden=!newer;badge.hidden=!newer;
  if(newer){link.href=result.release.url;link.textContent='What’s new in '+result.latest;badge.textContent='Version '+result.latest+' is available';}
  return result;
}
function checkForUpdates(quiet){
  $('updateCheck').disabled=true;
  if(!quiet)systemNote('updateStatus','Asking GitHub…',false);
  return fetchJson('/api/updates/check',{}).then(renderUpdate).catch(function(e){
    if(!quiet)systemNote('updateStatus','Could not check for updates: '+e.message,true);
  }).then(function(){$('updateCheck').disabled=false;});
}
function refreshUpdates(){
  return fetchJson('/api/updates').then(function(state){
    $('updateAuto').checked=!!state.check_on_open;
    if(state.last)renderUpdate(state.last);
    if(state.due)return checkForUpdates(true);
  }).catch(function(e){systemNote('updateStatus','Could not read the update status: '+e.message,true);});
}
function saveUpdateChoice(){
  var box=$('updateAuto'),on=box.checked;
  return fetchJson('/api/preferences',{check_updates:on}).then(function(){if(on)return checkForUpdates(false);}).catch(function(e){
    box.checked=!on;systemNote('updateStatus','That choice could not be saved: '+e.message,true);
  });
}
function refreshSystemPage(){return Promise.all([refreshSystem(false),refreshBackups()]);}
$('systemRefresh').addEventListener('click',function(){refreshSystemPage();});
$('systemCopy').addEventListener('click',function(){copyDiagnostics();});
$('systemFfmpegCheck').addEventListener('click',function(){refreshSystem(true);});
$('systemMeasure').addEventListener('click',measureStorage);
$('systemClean').addEventListener('click',runCleanup);
$('backupCreate').addEventListener('click',startBackup);
$('restoreGo').addEventListener('click',startRestore);
$('restoreCancel').addEventListener('click',function(){$('restoreBox').hidden=true;});
$('restoreUse').addEventListener('click',useRestoredLibrary);
$('updateCheck').addEventListener('click',function(){checkForUpdates(false);});
$('updateAuto').addEventListener('change',saveUpdateChoice);
refreshUpdates();
