// Local WAV mastering and sound effects.
var lastEffect = null;
function audioToolResult(result) {
  var delivery = result.delivery, urls = [['audioToolPlayer','src','audio_url'],
    ['audioToolDownload','href','audio_url'],['audioToolReceipt','href','receipt_url']];
  urls.forEach(function(binding) {
    var item = $(binding[0]), value = delivery[binding[2]];
    if (value) item.setAttribute(binding[1],value); else item.removeAttribute(binding[1]);
    item.hidden = !value;
  });
  var note = [];
  if (delivery.source_precision_label) note.push('Source '+delivery.source_precision_label);
  if (delivery.saved_precision_label) note.push('Saved '+delivery.saved_precision_label);
  if (delivery.output_measurements) {
    var meter = delivery.output_measurements;
    if (meter.integrated_lufs !== null) note.push(meter.integrated_lufs+' LUFS');
    if (meter.true_peak_dbtp !== null) note.push(meter.true_peak_dbtp+' dBTP');
  }
  $('audioToolStatus').textContent = note.join(' · ') || 'Preview ready.';
}
function runAudioTool(button, action) {
  $(button).addEventListener('click',async function() {
    var buttons = ['makeSfx','makeMaster','addSfxBackground'];
    if (buttons.some(function(id) {return $(id).disabled;})) return;
    buttons.forEach(function(id) {$(id).disabled = true;});
    $('audioToolStatus').textContent = 'Working locally…';
    try { await action(); } catch (error) { $('audioToolStatus').textContent = error.message || String(error); }
    finally { buttons.forEach(function(id) {$(id).disabled = false;}); }
  });
}
runAudioTool('makeSfx',async function() {
  var result = await fetchJson('/api/audio-tools/sound',{description:$('sfxWords').value,seconds:Number($('sfxSeconds').value),seed:Number($('sfxSeed').value)});
  audioToolResult(result); lastEffect = result.delivery;
  var dubs = await fetchJson('/api/projects?kind=dub'), select = $('sfxDub');
  select.textContent = '';
  (dubs.projects || []).forEach(function(row) {option(select,row.id,row.title);});
  $('sfxDubRow').hidden = !select.options.length;
});
runAudioTool('addSfxBackground',async function() {
  if (!lastEffect || !$('sfxDub').value) throw new Error('Generate an effect and choose a timed dub.');
  await fetchJson('/api/audio-tools/'+lastEffect.id+'/dub-background',{scene_id:$('sfxDub').value});
  $('audioToolStatus').textContent = 'Saved the effect as the dub background.';
});
runAudioTool('makeMaster',async function() {
  audioToolResult(await productionUpload('/api/audio-tools/master',$('masterSource').files[0],{
    precision:$('masterPrecision').value,gain_db:Number($('masterGain').value),target_lufs:$('masterLoudness').value,
    ceiling_dbtp:Number($('masterPeak').value),limit:$('masterLimit').checked}));
});
