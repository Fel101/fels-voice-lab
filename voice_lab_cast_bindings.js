// Voice Lab bindings: transcript drafts, cast and reference controls, and performance buttons.

$('castCreateFirst').addEventListener('click',function(event){
  event.preventDefault();
  $('castEditor').open=true;
  $('characterName').focus();
});
// Transcript draft for the selected saved reference version. It only fills the form; saving stays a separate, deliberate step.
$('suggestTranscript').addEventListener('click',function(){
  var id=$('referenceVersion').value;
  if(!id){$('referenceStatus').textContent=LabText.t('cast.choose_a_saved_recording_version');return;}
  if($('referenceTranscript').value.trim()&&!window.confirm(LabText.t('cast.replace_the_transcript_in_this')))return;
  var button=this;button.disabled=true;$('referenceStatus').textContent=LabText.t('cast.listening_to_the_saved_clip');
  fetchJson('/api/references/'+id+'/transcript-suggestion',{}).then(function(result){
    var draft=result.suggestion;
    if(!draft.text){$('referenceStatus').textContent=LabText.t('cast.no_speech_was_recognised_in');return;}
    $('referenceTranscript').value=draft.text;
    $('referenceStatus').textContent=LabText.t('cast.draft_from_detected_check_every', {model: draft.model, language: draft.language});
  }).catch(apiFailure).then(function(){button.disabled=false;});
});
function apiFailure(error){$('referenceStatus').textContent=error.message||String(error);}
$('createCharacter').addEventListener('click',function(){fetchJson('/api/characters',{name:$('characterName').value,description:$('characterDescription').value,tags:[]})
  .then(function(result){$('characterName').value='';$('referenceStatus').textContent=LabText.t('cast.character_saved_locally');return refreshCast(result.character.id);}).catch(apiFailure);});
$('saveCharacter').addEventListener('click',function(){var c=activeCharacter();if(!c)return;fetchJson('/api/characters/'+c.id,{name:c.name,description:$('characterNotes').value})
  .then(function(){ $('referenceStatus').textContent=LabText.t('cast.character_notes_saved');return refreshCast(c.id,$('identitySelect').value);}).catch(apiFailure);});
$('characterSelect').addEventListener('change',function(){renderActiveCharacter();});
$('identityEngine').addEventListener('change',updateIdentityEngineKind);
$('loadCustomVoiceIdentity').addEventListener('click',loadCustomVoiceIdentityOptions);
['phononVoice','phononIdentityVoice'].forEach(function(id){$(id).addEventListener('change',showPhononVoiceInfo);});
$('createIdentity').addEventListener('click',function(){var c=activeCharacter();if(!c){apiFailure(new Error(LabText.t('cast.create_or_open_a_character_2')));return;}
  var engine=$('identityEngine').value,body={engine_id:engine,label:$('identityLabel').value,notes:$('identityNotes').value};
  if(engine==='kokoro'){body.voice=selectedValues($('fixedVoice')).join(',');}
  if(engine==='qwen3-tts-1.7b-custom-voice'){
    if(!$('presetSpeaker').value||!$('presetLanguage').value){apiFailure(new Error(LabText.t('cast.load_customvoice_then_choose_its')));return;}
    body.speaker=$('presetSpeaker').value;body.language=$('presetLanguage').value;body.default_direction=$('presetDirection').value;
  }
  if(engine==='phonon-31m-en'){
    if(!$('phononIdentityVoice').value){apiFailure(new Error(LabText.t('cast.install_phonon_in_models_features')));return;}
    body.speaker=$('phononIdentityVoice').value;body.language='English';
  }
  fetchJson('/api/characters/'+c.id+'/identities',body)
    .then(function(result){return refreshCast(c.id,result.identity.id);}).catch(apiFailure);});
$('identitySelect').addEventListener('change',renderActiveIdentity);
$('referenceVersion').addEventListener('change',function(){loadSavedReference(selectedReference(activeIdentity(),$('referenceVersion')));});
$('referenceFile').addEventListener('change',function(){var file=this.files&&this.files[0];if(!file)return;loadReferenceSource(file,file).then(function(){ $('referenceStatus').textContent=LabText.t('cast.source_preview_ready_trim_or');previewReferenceClip();})
  .catch(function(error){$('referenceStatus').textContent=LabText.t('cast.file_preview_failed', {message: error.message});});});
$('previewReference').addEventListener('click',previewReferenceClip);
 $('noiseReductionEnabled').addEventListener('change',syncNoiseReductionFields);
 $('noiseReductionStrength').addEventListener('input',syncNoiseReductionFields);
['trimStart','trimEnd','referenceChannel','referenceRate'].forEach(function(id){$(id).addEventListener('input',function(){if(referenceBuffer){try{var r=activeTrim();drawReferenceWave(referenceBuffer,r.start,r.end);}catch(_){}}});});
$('micStart').addEventListener('click',beginMicCapture);$('micStop').addEventListener('click',stopMicCapture);
$('saveReference').addEventListener('click',saveUploadedReference);$('saveReferenceVersion').addEventListener('click',saveEditedReferenceVersion);
$('approveIdentity').addEventListener('click',function(){var ref=$('approveReference').value;if(!ref){apiFailure(new Error(LabText.t('cast.choose_a_saved_recording_version_2')));return;}
  saveIdentityUpdate({status:'approved',reference_version_id:ref}).then(function(){return refreshCast($('characterSelect').value,$('identitySelect').value);})
    .then(function(){$('referenceStatus').textContent=LabText.t('cast.the_voice_now_uses_the');}).catch(apiFailure);});
$('referenceVersion').addEventListener('change',updateReferenceRetirement);
$('retireReference').addEventListener('click',function(){var identity=activeIdentity(),ref=selectedReferenceVersion();if(!identity||!ref)return;retireAction('reference',ref.id,LabText.t('cast.this_recording_version')).then(function(message){if(message)$('referenceStatus').textContent=message;return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('restoreReference').addEventListener('click',function(){var identity=activeIdentity(),ref=selectedReferenceVersion();if(!identity||!ref)return;restoreAction('reference',ref.id,'this recording version').then(function(message){$('referenceStatus').textContent=message;return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('purgeReference').addEventListener('click',function(){var identity=activeIdentity(),ref=selectedReferenceVersion();if(!identity||!ref)return;purgeAction('reference',ref.id,LabText.t('cast.this_recording_version_and_everything')).then(function(message){if(message)$('referenceStatus').textContent=message;return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('approveFixedIdentity').addEventListener('click',function(){var identity=activeIdentity();if(!identity)return;saveIdentityUpdate({status:'approved'}).then(function(){return refreshCast(identity.character_id,identity.id);}).catch(apiFailure);});
$('performCharacter').addEventListener('change',function(){renderPerformanceCharacter();loadCurrentTakes();});
$('performIdentity').addEventListener('change',function(){renderPerformanceIdentity();loadCurrentTakes();});
$('performReference').addEventListener('change',loadCurrentTakes);$('lineId').addEventListener('change',loadCurrentTakes);
$('performLanguage').addEventListener('change',function(){var identity=performanceIdentity();if(identity&&identity.kind==='preset_voice'){
  $('performSupport').textContent=LabText.t('cast.checkpoint_preset_no_voice_recording', {speaker: identity.engine_options.speaker, language: this.value});
}});
$('performGenerate').addEventListener('click',generatePerformanceSet);$('performStop').addEventListener('click',stopPerformanceSet);
$('refreshTakes').addEventListener('click',loadCurrentTakes);
$('dramaMode').addEventListener('change',refresh);
$('actingPreset').addEventListener('change',function(){if(this.value){$('performDirection').value=this.value;}});
$('savePronunciation').addEventListener('click',function(){var c=performanceCharacter();if(!c){$('performError').textContent=LabText.t('cast.choose_a_character_first');return;}
  fetchJson('/api/pronunciations',{character_id:c.id,source:$('pronunciationSource').value,replacement:$('pronunciationReplacement').value})
    .then(function(){$('pronunciationSource').value='';$('pronunciationReplacement').value='';loadPronunciations(c.id);}).catch(function(error){$('performError').textContent=error.message;});});
