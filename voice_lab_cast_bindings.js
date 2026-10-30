// Voice Lab bindings: transcript drafts, cast and reference controls, and performance buttons.
// Transcript draft for the selected saved reference version. It only fills the form; saving stays a separate, deliberate step.
$('suggestTranscript').addEventListener('click',function(){
  var id=$('referenceVersion').value;
  if(!id){$('referenceStatus').textContent='Choose a saved reference version first, then suggest its transcript.';return;}
  if($('referenceTranscript').value.trim()&&!window.confirm('Replace the transcript in this form with a draft from the audio?'))return;
  var button=this;button.disabled=true;$('referenceStatus').textContent='Listening to the saved clip for a draft transcript…';
  fetchJson('/api/references/'+id+'/transcript-suggestion',{}).then(function(result){
    var draft=result.suggestion;
    if(!draft.text){$('referenceStatus').textContent='No speech was recognised in this clip. Type the transcript yourself.';return;}
    $('referenceTranscript').value=draft.text;
    $('referenceStatus').textContent='Draft from '+draft.model+' (detected '+draft.language+'). Check every word against the recording and correct it. Nothing is saved or marked exact until you save the reference.';
  }).catch(apiFailure).then(function(){button.disabled=false;});
});
function apiFailure(error){$('referenceStatus').textContent=error.message||String(error);}
$('createCharacter').addEventListener('click',function(){fetchJson('/api/characters',{name:$('characterName').value,description:$('characterDescription').value,tags:[]})
  .then(function(result){$('characterName').value='';$('referenceStatus').textContent='Character saved locally.';return refreshCast(result.character.id);}).catch(apiFailure);});
$('saveCharacter').addEventListener('click',function(){var c=activeCharacter();if(!c)return;fetchJson('/api/characters/'+c.id,{name:c.name,description:$('characterNotes').value})
  .then(function(){ $('referenceStatus').textContent='Character notes saved.';return refreshCast(c.id,$('identitySelect').value);}).catch(apiFailure);});
$('characterSelect').addEventListener('change',function(){renderActiveCharacter();});
$('identityEngine').addEventListener('change',updateIdentityEngineKind);
$('loadCustomVoiceIdentity').addEventListener('click',loadCustomVoiceIdentityOptions);
['phononVoice','phononIdentityVoice'].forEach(function(id){$(id).addEventListener('change',showPhononVoiceInfo);});
$('createIdentity').addEventListener('click',function(){var c=activeCharacter();if(!c){apiFailure(new Error('Create or open a character first.'));return;}
  var engine=$('identityEngine').value,options={},body={engine_id:engine,label:$('identityLabel').value,engine_options:options,notes:$('identityNotes').value};
  if(engine==='kokoro'){options.voice=selectedValues($('fixedVoice')).join(',');}
  if(engine==='qwen3-tts-1.7b-custom-voice'){
    if(!$('presetSpeaker').value||!$('presetLanguage').value){apiFailure(new Error('Load CustomVoice, then choose its speaker and language.'));return;}
    body.speaker=$('presetSpeaker').value;body.language=$('presetLanguage').value;body.default_direction=$('presetDirection').value;
  }
  if(engine==='phonon-31m-en'){
    if(!$('phononIdentityVoice').value){apiFailure(new Error('Install Phonon in Models & features, then choose its voice.'));return;}
    body.speaker=$('phononIdentityVoice').value;body.language='English';
  }
  fetchJson('/api/characters/'+c.id+'/identities',body)
    .then(function(result){return refreshCast(c.id,result.identity.id);}).catch(apiFailure);});
$('identitySelect').addEventListener('change',renderActiveIdentity);
$('referenceVersion').addEventListener('change',function(){loadSavedReference(selectedReference(activeIdentity(),$('referenceVersion')));});
$('referenceFile').addEventListener('change',function(){var file=this.files&&this.files[0];if(!file)return;loadReferenceSource(file,file).then(function(){ $('referenceStatus').textContent='Source preview ready. Trim or downmix, listen to the processed preview, and enter an exact transcript.';previewReferenceClip();})
  .catch(function(error){$('referenceStatus').textContent='File preview failed: '+error.message;});});
$('previewReference').addEventListener('click',previewReferenceClip);
 $('noiseReductionEnabled').addEventListener('change',syncNoiseReductionFields);
 $('noiseReductionStrength').addEventListener('input',syncNoiseReductionFields);
['trimStart','trimEnd','referenceChannel','referenceRate'].forEach(function(id){$(id).addEventListener('input',function(){if(referenceBuffer){try{var r=activeTrim();drawReferenceWave(referenceBuffer,r.start,r.end);}catch(_){}}});});
$('micStart').addEventListener('click',beginMicCapture);$('micStop').addEventListener('click',stopMicCapture);
$('saveReference').addEventListener('click',saveUploadedReference);$('saveReferenceVersion').addEventListener('click',saveEditedReferenceVersion);
$('approveIdentity').addEventListener('click',function(){var ref=$('approveReference').value;if(!ref){apiFailure(new Error('Choose a saved reference version to approve.'));return;}
  saveIdentityUpdate({status:'approved',reference_version_id:ref}).then(function(){return refreshCast($('characterSelect').value,$('identitySelect').value);})
    .then(function(){$('referenceStatus').textContent='Voice identity now approves the selected immutable reference version.';}).catch(apiFailure);});
$('referenceVersion').addEventListener('change',updateReferenceRetirement);
$('retireReference').addEventListener('click',function(){var identity=activeIdentity(),ref=selectedReferenceVersion();if(!identity||!ref)return;retireAction('reference',ref.id,'this recording version').then(function(message){if(message)$('referenceStatus').textContent=message;return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('restoreReference').addEventListener('click',function(){var identity=activeIdentity(),ref=selectedReferenceVersion();if(!identity||!ref)return;restoreAction('reference',ref.id,'this recording version').then(function(message){$('referenceStatus').textContent=message;return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('purgeReference').addEventListener('click',function(){var identity=activeIdentity(),ref=selectedReferenceVersion();if(!identity||!ref)return;purgeAction('reference',ref.id,'this recording version and everything made from it').then(function(message){if(message)$('referenceStatus').textContent=message;return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('approveFixedIdentity').addEventListener('click',function(){var identity=activeIdentity();if(!identity)return;saveIdentityUpdate({status:'approved'}).then(function(){return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('performCharacter').addEventListener('change',function(){renderPerformanceCharacter();loadCurrentTakes();});
$('performIdentity').addEventListener('change',function(){renderPerformanceIdentity();loadCurrentTakes();});
$('performReference').addEventListener('change',loadCurrentTakes);$('lineId').addEventListener('change',loadCurrentTakes);
$('performLanguage').addEventListener('change',function(){var identity=performanceIdentity();if(identity&&identity.kind==='preset_voice'){
  $('performSupport').textContent='Checkpoint preset: '+identity.engine_options.speaker+' / '+this.value+'. No reference recording is used.';
}});
$('performGenerate').addEventListener('click',generatePerformanceSet);$('performStop').addEventListener('click',stopPerformanceSet);
$('refreshTakes').addEventListener('click',loadCurrentTakes);
$('dramaMode').addEventListener('change',refresh);
$('actingPreset').addEventListener('change',function(){if(this.value){$('performDirection').value=this.value;}});
$('savePronunciation').addEventListener('click',function(){var c=performanceCharacter();if(!c){$('performError').textContent='Choose a character first.';return;}
  fetchJson('/api/pronunciations',{character_id:c.id,source:$('pronunciationSource').value,replacement:$('pronunciationReplacement').value})
    .then(function(){$('pronunciationSource').value='';$('pronunciationReplacement').value='';loadPronunciations(c.id);}).catch(function(error){$('performError').textContent=error.message;});});
