# Fel's Voice Lab guide

Updated 25 October 2026. Start with [setup](SETUP.md) and
[model capabilities](../MODEL_CAPABILITIES.md). Models work offline after their
packages and pinned assets have been installed. The version you are running is
at the foot of every page and in `/api/health`; `python serve.py --version` prints it.

## The first time

A Lab with no speech model installed says so at the top of **Model studio**, and offers the model it can install, with how
much it downloads and how much disk it takes. Its **Install** button starts the same install as that model's card in **Models
& features**: it shows what it will do first, and nothing changes until you press **Install now**. While it runs, **Show
progress** opens its window again. When it finishes, the studio is ready to speak: the model is chosen, a sentence is in the
box and **Generate** has the focus.

## Appearance and navigation

The header's **Theme** menu offers **Light**, **Dark** (neutral grey), **Forest** (dark green), and **System**. System
follows the operating system (Light, or Dark when Windows is in dark mode); an explicit choice overrides it. The choice is
saved in the selected app state folder and survives closing/reopening the desktop
window, whose local port can change. Compare uses the same preference when opened.

Choose **Voice cloning** to upload or record a voice, save it under a name and
hear a test straight away. **Cast library** is where you edit a voice's recordings and approve
them; **Perform lines** makes repeatable dialogue and acting. On narrow screens the
navigation wraps above the workspace and sample cards stack. While a model is busy, **Model worker** at the foot of the
navigation says what it is doing and what waits for it; see [One thing at a time](#one-thing-at-a-time).

## Clone your own voice

1. Open **Voice cloning**, type a **Name for this voice** and choose a **Model** that clones.
   Press **Install** on a model's card in **Models & features** to add one.
   Chatterbox Turbo clones on an ordinary CPU (or run `setup-chatterbox.bat`,
   [setup](SETUP.md#chatterbox-turbo-on-the-cpu)); Chatterbox 500M and Drama 3,
   which needs 26 GB of free RAM, clone on the CPU too, and the Qwen3-TTS Base
   models, VoxCPM2 and Breeze TTS 2 on a graphics card, NVIDIA's or AMD's.
2. Choose **Upload your voice — browse files**, **Record from the microphone**, or a saved
   voice recording. Uploaded WAV, FLAC, OGG, MP3 and M4A files stay on this PC. The
   file you upload is kept untouched as the source, and the voice recording is made from
   it as a WAV, so an MP3 or M4A is never re-encoded. M4A files are read with FFmpeg
   (see [setup](SETUP.md)); without it the page says so and suggests converting the file.
   Use one clear
   speaker without music; a clean 6–30 second passage is a useful starting point.
   The direct workflow accepts 0.5–60 seconds and up to 32 MB; Turbo requires
   more than 5 seconds. Use Cast to trim a longer recording before cloning.
3. Play the source preview. Enter the **Exact words in the recording** as heard,
   **Where it comes from** and **Who may use it, and how**. Voice cloning needs the
   words for every model except VoxCPM2, which can clone without them. Confirm that you own
   the recording or have permission to clone it. The checkbox records your decision;
   it does not establish rights by itself.
4. Enter **New words to speak**. These are the new performance, not the words of the
   recording. Add an **Acting direction** when the model takes one.
   Choose **Save the voice and hear it**, or **Save the recording only**.
5. Listen to the result against the source. The voice and its recording are saved in
   Cast, not yet approved, and the test is saved in Perform lines as a take. A saved
   recording never changes: an edit makes a new version of it. Open **Review and edit
   in Cast** to edit the recording or approve it. Nothing is approved automatically.

The preview also shows a **quality score** (0–100: good, review or poor) and plain warnings for clipping, a high
noise floor, reverberant decay, steady tones such as hum or music in the quiet parts, and a second voice. A warning
means listen before saving; it does not block the save, and the score is not stored with the recording. See
[REFERENCE_SCREEN.md](REFERENCE_SCREEN.md) for what each check measures and where it is weak.

Microphone recording needs the browser's permission and an available input device.
Choose **Stop recording** when finished; recording stops automatically at about
59 seconds. If microphone capture is unavailable, upload a recording instead.
If the test fails after saving, the saved recording stays, so you can try again.
Fish cloning runs on CPU and its first test can take several minutes. Chatterbox Turbo on a
CPU takes 10 to 30 seconds to load and then two to three times as long as the line it speaks.

A voice recording gives the model the voice, and can also carry its accent, its way of
speaking and the sound of the room into the result. It is not a trained replacement
model. The exact words help the models that read a recording together with its text;
wrong words can lower the quality. Choosing another recording can change the voice you
get even when the new dialogue stays the same.

Use your own recording or one whose speaker and rights holder allow the intended
cloning and distribution. A downloadable dataset, public video or existing picker
entry is not blanket permission. Model terms still apply: Breeze self-hosted
outputs are non-commercial; Fish has commercial and hosted-service conditions.
Read [voice asset rights](VOICE_ASSET_RIGHTS.md) before publishing voice recordings
or a starter voice pack.

## Design, cast and perform

**Model studio** auditions text with a model's own settings. Kokoro speaks in its
preset voices, one or several blended equally (**Kokoro voices**). Its list holds
licensed Kokoro voice files, not uploaded WAVs; choosing several averages them
equally. Kokoro cannot take a new voice from a WAV or clone a voice recording.
Upload that recording in **Voice cloning** and choose a model that clones instead.
Qwen CustomVoice speaks in the model's own preset voices (**Preset voice**).
Phonon speaks English in 37 fixed preset voices, grouped as American English, British
English and Characters, with a seed, a temperature and a pace shift; it takes no acting direction
and no voice recording, and its voices can be added to a character in **Cast library** as
preset voices. The same seed gives nearly the same clip, not the same file.
Gradium states no licence for those 37 voices, so ask them before you publish audio
made with one. Models that design voices take a description; models that clone need a voice
recording you may use and, where they ask for them, its exact words. Unsupported controls
are withheld or rejected, not silently advertised as effective.

While **Generate** works, the status line counts the seconds, and the first request
of a session also loads and warms the model. **Stop waiting** gives the page back at once.
If the model had started that line it finishes it, and your next request waits until it
does; **Stop** under **Model worker** ends it at once. A request stopped either way adds nothing to Recent clips, but a
voice you asked to save as a reusable asset is still saved when the model finishes, unless **Stop** ended it.

A list of voice recordings shows a playable preview, and under **Source, use notes and
recording details** the kind of source, the length, where it comes from and who may use
it. A recording from Cast also names its character, its voice and its version. **Allowed
use: Unknown** means nobody has written down who may use it; notes that are there were
written by a person, and the app does not check that they are right. Older generated
fixtures may have sparse notes or duplicate audio. Use **Upload / record a new voice** to
add a recording, and **Refresh saved recordings** after changing them outside the Lab.

Saved voice recordings are grouped as **Saved character voices** or **Local recordings and
designs**. The list shows each voice's preferred version: the approved one, or the newest
when none is approved. **All recording versions** shows the others. This filter does not
silently replace a recording you have already chosen.

For Chatterbox, **Clone the chosen voice recording** switches between cloning the saved
recording you chose and the model's default voice. It is no longer limited to the old
`af_heart` test clip. The recording gives the voice; the dialogue field gives what the
model should say.

Every recent studio result has **Save as a reusable Cast voice**. This opens Voice
cloning with the audio, proposed transcript and source/model terms carried into
the form. Name it, listen, correct the proposed words and record permission before
saving. Qwen VoiceDesign output becomes a voice recording for Qwen Base to clone,
because the design and cloning checkpoints are separate. Saving a preset voice's audio
for another model to clone does not change the preset.

**Cast library** opens with named saved-voice cards. Search by name, character
role or model, and play each card's saved audition.
**Created with** identifies the model that produced the audition when recorded.
**Generate dialogue with** appears when the saved voice uses another model,
such as a Qwen VoiceDesign sample reused through Qwen Base.

Choose **Use in Model studio** to select its voice recording, preset voice or blend of
preset voices; **Perform lines** to select the saved voice for new dialogue; or **Edit
voice** to open the editor for the character, its voices and its recordings. A preset voice
or blend can have a playable audition without a voice recording. A missing audition or
recording is reported rather than replaced with invented audio. Opening a card
does not generate speech or approve the voice.

In the editor, record or import a voice recording, look at its waveform and edits, enter
its **Exact words in the recording**, **Where it comes from** and **Who may use it, and
how**, then **Approve this version**. A saved version never changes: a new recording or
edit makes another version. The file you imported is kept apart from the edited versions. Voice Slate clip imports are documented in
[the bridge guide](RECORDER_BRIDGE.md).

**Perform lines** reuses a Cast voice, the exact script and the acting settings its model
has. Make several takes, listen, then rate, reject or select them; each take keeps a record
of its model, voice recording and settings. The **Line name** under **Details** is filled
in for you: takes for the same line name are listed together, and exports name the line by
it. **Takes to make** sets how many takes **Make takes** makes,
from 1 to 8 (3 by default), each on the next seed after the one you entered; it appears for
models that take a seed, and the others make one take. The takes are made one after
another and the status line counts the seconds on the current one. **Stop after this take**
lets the take in progress finish and skips the rest; the takes already made stay saved.
Each new take also gets automatic waveform
checks: silence, samples at full scale, a pause of at least one second inside the take,
or audio much shorter than its text. Leading and trailing quiet and ordinary sentence
pauses are not flagged. Under them each card shows the take's **loudness** in LUFS (ITU-R BS.1770, the
reading a loudness meter gives), its **sample peak** in dBFS and its **true peak** in dBTP, which is the
highest point of the signal as a converter would play it, so it can be above every stored sample. A true peak
above -1 dBTP gets a note, because MP3, AAC and Ogg export can clip there. A take under 0.4 seconds, a silent one
and one below the -70 LUFS gate have no loudness figure and the card says which. The numbers are measured when a
take is saved, and for takes made earlier the first time the service starts after the update. The checks and the
numbers never judge the words, the voice or the acting, so listen
before you select. Takes saved before this check existed show that no record exists.
Pronunciation rules and saved performances
keep written dialogue separate from the model's spoken spelling. A voice recording
made from a take keeps the restrictions of its source. Preset voices do not
silently become cloned audition WAVs.

**Edit take…** is a button on every take in Perform lines. It opens an editor under the take, with the whole take drawn as
a waveform, the part you keep tinted, and a line under the waveform where the speech is (anything louder than -50 dB). Drag
across the waveform to choose the part to keep, or type **Start** and **End** in seconds. **Cut the silence at both ends**
removes the quiet before and after the speech inside that range. It counts anything quieter than **Quieter than** (-50 dB
unless you change it) as silence, leaves **Silence to keep** (80 ms) at each end, and never touches a pause inside the
speech. **Fade in** and **Fade out** turn the ends down over the milliseconds you give. **Gain** makes the result louder
or quieter, from -60 to 24 dB; a gain that would clip is refused, and the message says how much that take can take.
**Hear the edit** plays the result and saves nothing. **Save as new take** saves it as a new take that names its
original. The steps run in that order: trim, silence, fades, gain. A trim alone copies the audio exactly, and an edit may not
leave less than 0.05 seconds. Edits of edits are allowed.

The original is never changed, so you can listen to both, select the one you prefer and retire the other. An edited take
keeps the original's text, voice, settings and model, and its card says what was done. Its loudness and waveform checks are
measured again on the new audio, so a trim that leaves too little speech gets the usual flag. Its word times and Kokoro
phoneme times, when the original has them, move with the trim, and a word the trim removes leaves the record; fades and
gain change no time. Retiring a take blocks its edits too, and purging it deletes them. A scene can choose an edited take
like any other take for its line. An exported package names the take it used and does not list the edit steps; look the take
up in Cast to see them. This is not the Drama model's own previous-take editing, which the model does not offer.

**Scenes & batch** imports a script (plain text, Fountain, JSON or CSV) or starts from Perform. The script is a table, one row per line: its number, character,
the start of its text, its voice and model, and its status. **Open** on a row shows the line's takes, then its full form
(character, voice, text, language, pauses and settings) and its line name, which exports and the API use. The status says where
the line stands:

- **Not rendered**: it has no take yet.
- **Waiting** and **Rendering**: a batch has it.
- **Failed**: the last batch to try it failed, and the reason is under the status.
- **Choose a take**: it has a take that fits, and none is chosen. Open the row and press **Use this take**, or press **Use the
  newest take where none is chosen** above the table to do it for every such line at once.
- **Done**: its chosen take fits the line as it is now.
- **Out of date**: its text, voice, language, pronunciations or settings changed after its take was made. The old takes stay
  in Cast, marked with what changed.
- **Not saved yet**: the line has edits you have not saved.

**Render** on a row renders that line. Tick lines and press **Render ticked** to render several, **Render all** for every
line, or **Render what is missing or out of date** for the lines that are not rendered, failed or out of date. Each saves the
scene first. The filters show one character's lines, or the lines with one status; **Tick all shown** ticks what they show,
and **Render ticked** renders the ticked lines that are shown. A long line is split into parts, and its row opens to show each
part with its own takes and **Render this part**. **Show the parts of long lines** shows how each line splits before you
render.

A batch renders one take at a time, and **Pause**, **Resume**, **Cancel after this line**, **Stop now** and **Try the failed
lines again** work on it. **Stop now** cancels the batch and also stops the line it is rendering, so its model loads again
for the next request. A batch renders all of one model's lines before it loads
the next model, so a scene that switches between voices on two models loads each model once; while it runs, the status line
says the order, such as "Kokoro 82M: 7 of 12, then Qwen3-TTS 1.7B Base: 8". The scene is still assembled in script order. A
take whose waveform checks flag something says so under it, and **Takes from this batch** lists one batch's takes with
the same checks as Perform, each with **Use this take**. **Build the scene from the chosen takes** puts the chosen takes and
the pauses together in script order, as **Scene, exactly as rendered** and **Scene, evened out in loudness**; a timing target
is advisory.
See [the scene schema](scenes/scene-schema-v1.md).

**Import a script from text or Fountain** takes a script you already have, from a `.txt` or `.fountain` file or pasted
in. Plain text has one speech per line, written `NAME: words`. A direction in brackets or parentheses at the start of the
words, or after the name (`MARA (quietly): words`), becomes the line's acting direction. Fountain is the screenplay format,
with each name in capitals above its lines: a parenthetical is the direction of the words after it, extensions such as
(V.O.) and (CONT'D) come off the names, and the title page's Title names the scene. Scene headings, transitions, notes,
sections and the boneyard are left out. **Read the script** says how many lines and speakers it found, and lists by line
number the lines it could not read, such as a name with no words or a direction on its own.

Then each speaker gets a voice. A speaker with the same name as a Cast character starts on that character's approved voice;
the others start on **Choose a voice…**, and **Create the scene** stays off until every speaker has a voice or **Leave these
lines out**. Lines with no name (plain text) and action (Fountain) are left out unless you choose a voice to read them as
narration. Each line gets what a new line gets: the voice's default acting direction when the script gives none, 250 ms
after it and one take. A direction for a model that takes none is left out. Once the scene is made, the panel lists every
line it left out and why. The [HTTP API](API.md#post-api-scenes-import) imports a script the same way, and
[a preview route](API.md#post-api-scenes-import-preview) reads one without making anything.

Undo (Ctrl+Z) and redo (Ctrl+Y or Ctrl+Shift+Z) reverse script edits, including typing, line changes, moves and
removals. Saving, opening another scene, importing, starting from Perform and choosing a take (which reloads the saved
scene) clear the undo history. Ctrl+S saves the scene, and Alt+Up or Alt+Down moves the line you are in. In a text box,
Ctrl+Z is that box's own undo. The shortcuts only work while Scenes & batch is open.

**Export package** uses a saved scene and its chosen takes. **Check what goes in** first,
choose WAV, FLAC, Ogg, MP3 or AAC (M4A) and any trims, fades and loudness, then **Create package**
and **Check the chosen package**. Copies of the takes, the manifests and the captions keep where
each take came from. A caption without word times is not timed to the words. Existing exports are
never silently overwritten.

FFmpeg writes every format, so the panel checks it when you open it. Without FFmpeg it says so and
switches off every button that needs it; with an FFmpeg build that lacks one encoder, only that format
is switched off. Either way it offers **Install FFmpeg**, which puts FFmpeg in the Lab's own folder and
switches the formats on when it is done, with no restart. **Refresh list** checks again, which is how you
pick up an FFmpeg you have just installed yourself. MP3 is written as variable bit rate (about 190 kbit/s) and AAC at 192 kbit/s. MP3 takes only
8, 11.025, 12, 16, 22.05, 24, 32, 44.1 and 48 kHz, and AAC only 7.35, 8, 11.025, 12, 16, 22.05, 24, 32,
44.1, 48, 64, 88.2 and 96 kHz; an export that would need another rate is refused before anything is written. FFmpeg records the encoder's delay in the file, so a player that
honours it plays exactly the take's length; one that does not adds a few tens of milliseconds of silence at
the start, and captions follow the take, not that silence.
Verifying a package checks each file's codec as well as its hash.

## One thing at a time

The model worker does one thing at a time: one load, clip, take, test or install. What you ask for while it is busy waits
its turn, and **Model worker**, at the foot of the navigation, says what runs and for how long, and where each of your
waiting requests stands, such as "Waiting: 2 ahead of you (a scene batch is rendering)". **Cancel** takes your request out
of the line. **Stop** ends what runs at once by ending its model's process, so the next request loads the model again. An
install and an unload cannot be stopped; an install runs until it is done. Your requests go ahead of a scene batch's
next line: **Make takes** during a batch makes its take after the line being rendered, and the batch then carries on.

## Compare and diagnose

**Compare & rate** can now use your saved Cast performances, including different
voices produced by the same model. Search **Compare saved performances**, play
their previews, select 2–20 takes, name the comparison and choose **Create
comparison from selected takes**. Every selected take must have a partner with
identical written/spoken text, language and acting direction. If not, generate
the same test line in Perform first; different languages remain separate cases.

The import verifies source hashes, preserves Cast metadata and inherited use
terms, and creates separate loudness-matched listening copies. It does not load
a model or assign ratings. Choose the new evaluation run, enter an evaluator label
and choose **Create blind A/B session**. Give **Sample A** and **Sample B** their
own five scores for words, identity, acting, naturalness and artifacts; an overall
A/B/tie preference is optional. Choose **Save A and B ratings** for each trial.
**Reveal model mapping and ratings** becomes available after both sides of every
trial are rated. Old pair-level scores remain visible as legacy history; they do
not supply either sample's scores. Revealed mappings identify
the saved character/voice/take, including two voices on the same model. Ratings persist;
revealed sessions are read-only.

This auditions voices. Different voice recordings, voices or model settings do not
make it a controlled model benchmark. Saved evaluation runs remain
available. Raw audio is preserved. Automatic
loudness/clipping/transcript checks do not approve speaker likeness or acting.
Optional transcript suggestions are drafts;
a person still checks their exactness before saving.

## Other voice tools

**Check voice match** is a local speaker-encoder cosine proxy, from the speaker encoder
that comes with Chatterbox Turbo (installing Turbo installs it). Its two bands
(likely the same voice from 0.847, likely a different one below 0.796) were measured
on one target voice, not all speakers or languages; the statistics of the clips they
were measured from are in [catalog/speaker_similarity.json](../catalog/speaker_similarity.json).
A missing encoder must not be treated as a voice match.

**Voice conversion** keeps the **Performance to convert** apart from the **Voice to convert
it to**, keeps a record of each conversion, and preserves Chatterbox's native watermark. It is
not training. Its two lists offer recordings already in the Lab's folders. To use a file from
this computer instead, choose it under the list: **Use a recording from this computer** for
the performance (WAV, FLAC, OGG, MP3 or M4A, up to 60 seconds; its voice is replaced, so it
needs no permission), or **Use a voice recording from this computer** for the voice to convert
to (up to 10 seconds). For a voice recording, fill in **Where it comes from** and **Who may
use it, and how** and tick that you own the recording or have permission to clone the voice
first, as Voice cloning asks; the tick clears after
each file. The file is then kept, and chosen in its list under its own name. Every
conversion's manifest records the original name of each file, and where the voice
recording comes from and who may use it. The Lab keeps these files as mono WAVs in
`out\conversions\uploads\source` and `out\conversions\uploads\target`, named by the
SHA-256 of the file you chose, so the same file chosen twice is kept once, each with
a small JSON of its name and terms beside it. Backups leave out `out\conversions`,
so copy that folder yourself if you want to keep them; deleting a recording there
(with its JSON) takes it off the list.
Dataset preparation is a provenance/manifest workbench; no training, fine-tuning,
automatic voice improvement or unrestricted output rights are implied.

## Drama scene and narration studio

Select Drama 3 and open **Drama scene & narration studio**. Assign up to sixteen
approved voice recordings with their exact words, insert speaker tags and acting
directions, then audition incremental audio. Scripts can contain up to 8,000 characters;
the worker's context, duration and ten-minute audio caps still apply. Select one
of 83 language labels plus Auto. The label is metadata: it does not translate text or reach the model, and [the language screen](LANGUAGE_MATRIX.md) records which languages each model was checked in.

Completed jobs retain mixed WAV, script, cue-free transcript, model/cast receipts
and ZIP downloads. Reopen recent jobs to restore settings for a fresh generation.
There are no isolated stems, reliable word/speaker timing, previous-take VQ editing
or automatic conversion of joint audio into separately timed game dialogue.
The model may ignore an acting direction or speak it. All studio jobs and cloning run on CPU after a
local Vulkan driver-loss incident. See [Drama details](DRAMA3.md).

## Retire, restore and delete for good

**Retire** stops new use without deleting anything. Retire a voice in the Cast library,
a recording version with **Retire this recording** in the Cast editor, or a take in
Perform lines. A retired item still plays for review, but Perform, Scenes, Compare,
voice conversion, the Drama studio, the lists of voice recordings and exports refuse it. Retiring
a voice also blocks its recordings and takes. Retiring a recording also blocks its
takes and any versions or recordings made from it. Retiring a take blocks the edits made from it. Retiring a voice or a recording
clears its approval, and restoring never approves it again.

**Restore** lifts the item's own retirement. An item still blocked through the voice,
recording or take it came from stays blocked until that source is restored. The Cast
library hides retired voices unless you tick **Show retired voices**, where you can
restore them or delete them for good.

**Delete this voice for good…** (on a retired voice in the Cast library), **Delete this recording for good…** (on a
retired recording in the Cast editor) and **Delete this take for good…** (on a retired take) permanently delete the
item, everything made from it, and their audio from Cast storage. It cannot be undone, and the confirmation lists what will go.
Cast keeps a tombstone with the time, the reason and the audio's checksums, but not the
audio. Copies outside Cast storage, such as exported packages and comparison copies under
`out/`, are listed afterwards and are never deleted. Remove them yourself. Back up
the Cast folders before you delete anything you might need later.

Limits: loose files in the `reference` and `data/voices` folders are not Cast records, so
retire or delete them in their folders. Character-level retirement is available through
the local API but has no button yet. Make a [library backup](#back-up-and-restore-the-library) first.

## Check which models can speak

Open **Models & features** to see each model's state. **Integrated** means this build contains
its adapter. **Installed** means its Python environment and pinned checkpoint files are on disk;
the page checks this without loading a model. **Usable** means a test on the current device
loaded the model and spoke one short sentence. A test describes the model as it was when it ran, so it
records the model pin and the packages installed in the model's environment. If either changes, the card
reads **test out of date** until you press **Test again**. Tests saved by an older build have no record
of either and read as out of date once.

Press **Test** on a card to run that test. The first test loads the model, which can
take a minute or more, and other model requests are refused until it finishes. A failed test shows its
reason. Results are saved per device in `data/engines/checks.json` under the state root, so a
test on the GPU does not mark the CPU as usable. Each model has a Python environment of its own, so installing one never marks
another's test out of date.

When the service uses a graphics card, each card has a **Runs on GPU | CPU** switch. It chooses where that model runs from its
next load, and the card's state and test are then those of that device. A model installed with the CPU build of PyTorch runs
on the CPU; switch it to GPU and its card offers **Install GPU build**, which replaces PyTorch in that model's environment with
the GPU build for this computer's card, [NVIDIA's](SETUP.md#nvidia-graphics-cards) or [AMD's](SETUP.md#amd-radeon-cards). GPU
is greyed out, with the reason under it, for Phonon and Drama 3 (they run on the CPU) and where the graphics card or its driver
cannot run the model's GPU build. The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 run only on a graphics card, so their switch
stays on GPU and CPU is greyed out instead; **Install** always gives them the GPU build for this computer's card. On a model
that is not installed yet, the switch chooses which build **Install** puts in.

Every card says how much free RAM the Lab wants before it loads that model, and the top of the section says how much RAM
this computer has and how much is free right now. The Lab refuses to load a model when less than that is free. A card turns
red when the whole computer has less RAM than the model needs, or when less is free at the moment: close other programs, and
then press **Test**. The figure is the Lab's own rule for loading. For Kokoro, Phonon and Chatterbox Turbo it comes from a
measured peak with room to spare; for the others it is not a measurement of what the model uses. `runtime.local.json` can change it for one
computer ([hardware and state settings](SETUP.md#hardware-and-state-settings)).

A model that is not installed says *Not installed yet* and how much **Install** downloads and how much disk it uses. The files
it is missing, the installer that adds them, and where the output of an install that stopped was kept are under **Details**
on its card, so the card itself shows no paths. **Install** opens a window with what the installer will do and whether this
computer can take it, and **Install now** runs it and follows it in the same
window, with the end of its output ([setting up](SETUP.md) has a section on each installer). **Hide** closes the window and
leaves the install running; **Show progress** on the card opens it again, and **Show output** shows the end of one that
stopped. The cards stay where they are while it runs. The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 install only for a
graphics card their build runs on, an NVIDIA card and driver or an AMD Radeon whose chip AMD's build has code for, and VoxCPM2
and Breeze only on a card with enough memory; on another computer their cards gather under **more models need manual setup**
and say why the app cannot install them there.

**Measured speed on this computer** lists each model, where it **Runs on**, and its **Typical time per second of
speech**: the median of the seconds it took to generate divided by the seconds of audio, from the clips and tests this
computer made. It records speed only; precision is not measured.

Language choices are checked the same way. A language marked **(unscreened)** in a picker is one the
[language screen](LANGUAGE_MATRIX.md) did not clear for that model: the recognizer heard it as partial or
not understood. The app keeps those choices so existing scenes still open, but listen before relying on one.

## Word times and captions

Run `.venv\Scripts\python.exe align_takes.py --all` to record word times for every Cast take that has none.
The run takes about a minute for 150 takes and writes nothing else. Scene exports then write captions as word
phrases wherever every take of a line has word times; any other line keeps one caption. The times are
approximate and have not been checked against trusted timings, so listen before relying on a caption's timing.
[Alignment notes](ALIGNMENT.md) describe the limits. A take made by editing another take starts with its original's word
times, moved with the trim, so it needs no run; if the original had none, the run records the edit's own, from the full line
of text, which fits badly when the trim cut words away.

Each scene line that has a target duration also gets a fit status in the export manifest: `within_slot`,
`stretched` (a bounded stretch would fit it; listen first) or `needs_rerender`. The export keeps the take as
rendered, so re-render a line when its status says so.

## Lip-sync tracks

Each exported scene line that has phoneme times also gets a mouth-shape track: a JSON file under `lipsync/` in the
package. Takes that Kokoro made after the schema 7 update carry their own phoneme times, in any language Kokoro
speaks. Other English takes use their aligned words, and the phoneme times inside each word are estimates. The line's
`lip_sync` entry in its character pack says whether a track was written, and a line without one says why. Restart the
desktop app after updating, because the Cast database moves to schema 7. A take made by editing a Kokoro take keeps its
phoneme times, moved with the trim. Nothing in the app plays the tracks yet.
[Lip-sync notes](LIPSYNC.md) describe the format, the mouth shapes and the checks.

## Use the Lab from a script

The pages talk to a local web service, and a script or another program can too: `POST /api/speak` turns text into one
finished WAV file, and `POST /api/performance` speaks a line as one of your Cast voices and saves it as a take. Start
`serve.py` (the desktop app picks a port of its own) and try `tools\lab_cli.py health`, `speak` or `perform`. The
[HTTP API page](API.md) lists every call, and [`openapi.json`](../openapi.json) describes them for tools that read OpenAPI.
While the model worker is busy, a request that needs it is refused with 409 at once, unless its JSON body has
`wait_seconds`: then it waits in line, `GET /api/queue` shows where it stands, and `POST /api/queue/{request_id}/cancel`
takes it out of the line or stops it.
The same page shows how a script [edits a saved take](API.md#edit-a-saved-take): look, hear, save.

## Stop and back up

**Unload models** releases the current worker/GPU lease. Another service with a
loaded model can cause HTTP 409; unload from its owning app or wait for its idle
timeout. Never delete an ownership lock file to bypass a running owner.

Closing the desktop window stops only its own service and the workers it started. Stop a
foreground/browser service with its terminal. Stop all services sharing a state
root before copying its `data/cast`, `data/scenes`, `data/evaluations`,
`out/comparisons` and `out/scenes` directories together. `out/comparisons` holds
the comparison audio and manifests used by the evaluation database. Retain needed
`data/voices`, Drama jobs, export packages and source recordings too. Back up the
installation's model caches, environments and native receipts separately if
retaining offline use.

Recent simple studio clips are browser-session history rather than the durable
Cast store. The desktop shell uses a private WebView session; download those
clips or save them to Cast before closing. Saved voice recordings, takes, scenes and
Drama job history remain on disk. Logs and manifests can include private scripts
or paths; do not upload them without review.

### System: the report, FFmpeg, disk space and storage

**System & backups**, last in the list on the left, shows what this PC is running without loading a model: the version,
Python and Windows, the device and free memory, the library folder with the schema number of each database and how much
is in it, and the name of the service log. It says whether FFmpeg is found and which of WAV, FLAC, Ogg, MP3 and AAC
this build can write, and whether it is the Lab's own. While none writes every format, **Install FFmpeg** says what it
would download and how much disk it uses, and installs it with the page open: the button is busy until it ends, and the
line under it follows the download. **Check FFmpeg again** looks again after you install one yourself, with no restart. It shows the free space
on the library's drive and the program's drive, flagging either under 5 GB, and counts the models by state.

**Copy report for a bug report** puts the same facts on the clipboard as plain text, with the processor, the graphics cards
and total memory, where each model's last test ran and how fast, and the speed of recent clips. **Show the report** shows
exactly what will be copied. The text holds no scripts, transcripts, names of characters or voices, or recordings, and your
home folder reads as `~`. Read it before you share it: a path can still carry a folder name you chose. The
[hardware report](https://github.com/Fel101/fels-voice-lab/issues/new?template=hardware_report.yml) on GitHub asks for it.

**Updates** says which version this is. **Check for updates** asks GitHub for the newest release and, when it is newer, links
to what changed in it; the request carries nothing about you or your library. Tick **Check when the Lab opens** to have the page
ask by itself, at most once a day; a newer version then also shows in the page header. Updating is `update.bat`'s job, with the
Lab closed: [Updating the Lab](SETUP.md#updating-the-lab) says what it does and how `update.bat --undo` goes back.

**Measure folders** adds the size of each library folder (and whether a backup holds it) and of the backups, voice
conversions, logs, worker scratch folders and desktop sessions. It waits for the button because it walks those folders,
and it never walks the Drama model folder. **Clean up old scratch folders and logs** runs the sweep described below and
nothing else.

### Back up and restore the library

**System & backups, Back up now** writes one ZIP into `backups/` inside the library folder, named
`voice-lab-library-<date>-<time>.zip`. It holds the Cast, Scenes and Comparisons databases with their audio, and the
appearance setting. Tick **Saved voices**, **Export packages** and **Drama studio jobs** to add those. It leaves out model
files and environments, logs, voice conversions (`out/conversions`), the `migration-backups` copies, lock and temporary files, and
anything that is a link.

The databases are copied with SQLite's own online backup, so each copy is consistent while the service runs. A take that
is half way through saving is the one thing that is not covered, so let a render finish first, or stop the service. The
ZIP is written under a temporary name, read back, every file compared with the checksum in the backup's manifest and each
database put through SQLite's integrity check, and only then given its name. A backup that does not check out is
discarded, and the page says why. There must be room for the library plus 64 MB. A backup cut short by the service
stopping leaves a `.part` file or a `.backup-` folder, which the next backup removes once it is a day old.

The page lists the newest 25 backups. **Check** reads one in full again, which is worth doing after you copy it to another
drive. Keep a copy on another drive: a backup beside the library does not survive that drive failing. It is not
encrypted, so it holds your recordings and scripts the way the library does.

**Restore** never changes the library you are using. It unpacks into a new folder, `restored/<name>` inside the library
folder, checks every file and database, opens the stores (which moves a database from an older structure forward in the
copy, with the usual safety copy beside it), looks for the audio the database names, and only then gives the folder its
name. A folder that already holds anything is refused, and a backup made by a newer version of the app is refused with a
note to update. If the library was already missing some audio when the backup was made, the restore says how many files.
If the backup itself lost audio it fails, and nothing is left behind. On the page, one backup, check or restore runs at a
time.

To use the restored library, press **Open the restored library next time**. It writes `state_root` into
`desktop.local.json` beside the app, keeping any other setting there, and the desktop app opens that library when you
close and reopen it. To go back, set `state_root` to the old folder, or delete that line to return to this project's own
folder. To look at a restored library without switching, stop the service and start it with `--state-root "<folder>"`.

The same from a terminal:

```powershell
.venv\Scripts\python.exe tools\backup_library.py create
.venv\Scripts\python.exe tools\backup_library.py create --to D:\Backups --without exports drama
.venv\Scripts\python.exe tools\backup_library.py verify backups\voice-lab-library-20261025-120000.zip
.venv\Scripts\python.exe tools\backup_library.py restore backups\voice-lab-library-20261025-120000.zip D:\VoiceLab-restored --use
.venv\Scripts\python.exe tools\backup_library.py use D:\VoiceLab-restored
```

`create` backs up the library the desktop app opens, or the one `--state-root` names, and `--to` chooses the folder.
`verify` exits with status 1 and lists what is wrong when a backup does not check out. `restore` takes a folder that does
not exist yet or is empty, and `--use` or a later `use` points the desktop app at it. `use` with the folder you came from
switches back. Nothing here schedules backups or merges a backup into a library that already holds work.

### The service log, cleanup and safety copies

Each run of the service writes `out/logs/service-<date>-<time>-<id>.log`, and the start-up
line names the file. It has a line for each action and each failure (generating, saving,
deleting, importing, exporting) and for any request that is refused, fails or takes a second
or more. A line gives the time, a request ID, the request, the status, the seconds, the
model and, for a failure, the error and the worker log it names. The ID also comes back to
the page in the `X-Request-Id` header, and a script or assistant that sends its own
`X-Request-Id` (letters, digits, `.`, `_` and `-`) finds that ID in the log. The log never
holds script text, the words of voice recordings or request bodies, and the query part of an address
is dropped. An error message or a path can still carry a fragment of a name, so read the log
before you share it. One run's log stops growing at about 6 MB.

When the service starts it clears away what past runs left in `out/`, in the background, and
never in Cast, Scenes or exports: the temporary-audio folder of a worker that has exited
(`out/workers/<engine>-<id>`) once it is an hour old and empty, or a week old if it still
holds something; desktop sessions (`out/desktop/session-*`) beyond the newest ten that are
more than a day old and not running; and the logs of runs beyond the newest thirty that are
more than a week old. `python tools/cleanup_runtime.py` lists what it would remove, and
`--apply` removes it. Start the service with `--no-cleanup` to keep everything.

Before the Cast, Scenes or Evaluations database moves to a newer structure, the service
copies it into a `migration-backups` folder beside it (the newest three copies of each are
kept) and stops without changing anything if it cannot make the copy. A new database needs
no copy. To go back to an older build, stop the service and put the copy in place of the
database file.
