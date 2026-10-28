# Fel's Voice Lab

A local studio for character voices on Windows. Design or clone a voice, approve it as a character
in your Cast, act dialogue with it, render whole scenes as batches, compare takes blind, and export
production audio with a checksummed manifest. It runs on your own computer: the service listens only
on `127.0.0.1`, and the model workers run offline, so nothing is downloaded while you generate.

**An early release (0.1.0) for Windows 10/11 and Python 3.12.** It runs from source and has no
installer. Model runtimes and weights are separate installations, so a fresh checkout is not a
one-click setup of every model. It was built and checked on one Windows machine with an AMD GPU;
there are no NVIDIA, Linux or macOS results. Original application code is MIT; borrowed code and
optional models keep their own licenses. See [third-party terms](THIRD_PARTY_NOTICES.md).

## A look at it

The pictures below are made by the project's own browser test
([`tests/test_browser_flow.py`](tests/test_browser_flow.py)), which drives the real page in headless
Microsoft Edge or Google Chrome. Its engines are stand-ins that return a plain tone, so no model is
loaded and nobody's voice appears. Mira is a character that exists only in that test, every waveform
is a test tone, and the red note in the second picture is the automatic check noticing that a
half-second tone is shorter than its sentence.

### Cast library

![The Cast library with one approved voice card: its audition player, reference count and buttons](docs/images/cast-library.png)

Every saved voice is a card that shows how it was made, which model will speak for it, a saved
audition to play and how many saved reference versions it has. A reference version never changes
once saved, and keeps its transcript, origin and allowed use. Approving a voice records your
decision to use it; it cannot prove that you have the right to.

### Perform lines

![Perform lines: a character, a voice, a line of dialogue and the first of two takes, with its loudness and waveform check](docs/images/perform.png)

Pick a character and voice, write or paste a line, optionally add an acting instruction, and make a
set of takes on consecutive seeds. Every take is saved with the text, model, reference and settings
that produced it, shows its loudness and peak readings, and carries automatic waveform checks for
silence, clipping, long pauses and audio much shorter than its text. The checks never judge the
words, the likeness or the acting: you still listen.

### Edit a take

![The take editor open under a take: a waveform with the kept part tinted, trim times, silence, fades and gain](docs/images/take-editor.png)

**Edit take…** opens an editor under the take. Drag across the waveform or type times to trim, cut
the silence at both ends, add fades and a gain, listen with **Hear the edit**, then **Save as new
take**. The original is never changed, and the new take records what was done to it.

### Scenes and batch

![Scenes and batch: one line with its character, voice, language and settings, then the buttons, a finished batch job and the assembled scene](docs/images/scene.png)

A scene is a script of lines with stable ids, each with its own character, voice, reference version,
language, pauses and settings. **Render scene** makes the takes as a batch that runs one take at a
time, can be paused, cancelled and retried, and picks up again after a restart. You choose the take
for each line and assemble a preview. Undo, redo and the usual shortcuts work on the script.

### Export package

![The export panel after a package was written and verified](docs/images/export.png)

The export panel writes WAV, FLAC, Ogg, MP3 or AAC (M4A) line files, the raw scene mix, captions, a
transcript and a manifest of checksums into `out/exports/`, and never overwrites an existing package.
FFmpeg writes the files; the original Cast takes are copied byte for byte. **Verify** reads the
package back and checks every hash, the captions, and each file's codec and length.

## What it does

- **Model studio:** fixed voices, supported voice blends, descriptions, cloning, presets, playable
  reference details and model-specific controls.
- **Voice cloning:** browse for a recording or record one locally, listen, document its source and
  allowed use, save it as a named Cast candidate and generate a test line.
- **Cast and Perform:** voice cards with audio previews and model labels, a search box,
  immutable reference versions with transcripts and provenance, approvals, pronunciation, saved
  performances, seed variants, take review with loudness and peak readings, and a take editor that
  saves each edit as a new take.
- **Scenes and batch:** stable line ids, segmentation, sequential jobs with pause, resume, cancel
  and retry, stale-take protection, undo and redo, and previews.
- **Compare and export:** blind A/B listening on saved Cast takes, measurements, optional transcript
  and speaker proxies, packages in five formats with manifests and captions, and lip-sync tracks.
- **Drama studio:** up to sixteen reference speakers, inline cues, long scripts, incremental PCM
  audition, retained jobs and mixed-audio packages.
- **System and backups:** the version, FFmpeg, free disk space and model states in one place, a
  report to paste into a bug report, folder sizes with a cleanup, and a one-file library backup that
  is read back and checked when it is written and restored into a new folder without touching the
  library in use.
- **Retire and purge:** stop a voice, recording or take, and everything made from it, from being
  used again without deleting its audio, or delete it for good after a confirmation.
- **From a script:** a documented local HTTP API and a small command-line tool.
- **Other tools:** voice conversion, consent-aware import of clips kept by
  [Fel's Voice Slate](https://github.com/Fel101/fels-voice-slate), the companion recorder, and dataset
  preparation. Nothing here trains or fine-tunes a model.

## Models

Integrated families are Kokoro, Chatterbox Original/Turbo/VC, Breeze TTS 2, Qwen3-TTS
VoiceDesign/Base/CustomVoice, VoxCPM2 and Fish Audio Drama 3. *Integrated* means the adapter and the
interface exist. The **Models & features** page says whether a family is *installed* (its
environment and pinned files are on disk) and *usable* (a check on your device loaded it and it
spoke one sentence). Which languages and devices were actually tried is written down in
[capabilities and limits](MODEL_CAPABILITIES.md) and the [language screen](docs/LANGUAGE_MATRIX.md).

Drama studio and all Drama cloning use CPU after a local Vulkan device-loss incident. Experimental
short GPU design is an opt-in local configuration; do not enable GPU cloning. Previous-take editing,
speaker stems and native word timing are not exposed by the pinned backend. Built with Fish Audio.

Kokoro presets are licensed model speaker packs, not uploaded recordings. See
[voice assets and redistribution](docs/VOICE_ASSET_RIGHTS.md) before publishing recordings,
generated voices or model files with your project.

Voices belong to people. Clone only recordings you made yourself or whose speaker and rights holder
allow it, keep the transcript, origin and permission with each reference, and do not use the
software for deceptive impersonation or fraud. [Responsible use](docs/RESPONSIBLE_USE.md) says more.

## Start here

1. Read [setup](docs/SETUP.md), install Python 3.12 (64-bit), and run `setup.bat`.
2. Run `setup-kokoro.bat` to add Kokoro, a small model that speaks on an ordinary CPU (about 600 MB of
   downloads; it shows its plan and asks first). The core alone opens the studio and manages its stores,
   but it cannot synthesize without a model. [Setup](docs/SETUP.md) covers the other models.
3. Run `setup-desktop.bat` once. Use the **Fel's Voice Lab** desktop icon afterward. Its dedicated
   window runs without a console; closing it stops its backend.

FFmpeg is not bundled. Install it (`winget install Gyan.FFmpeg`) for scene export, MP3 and AAC
output, and M4A references. Choose **Light**, **Dark** or **System** from the header's **Theme**
menu; the choice is saved locally.

Browser mode, without the desktop window:

```powershell
.venv\Scripts\python.exe serve.py --port 8768
```

Open `http://127.0.0.1:8768/`. Closing a browser tab does not stop a foreground service. The desktop
launcher owns a separate backend on a free port and never closes independently started services.
Read [desktop and state behavior](docs/SETUP.md).

## Documentation

| Guide | Purpose |
|---|---|
| [Setup](docs/SETUP.md) | Environments, hardware, desktop launch and known installation limits |
| [User guide](docs/USER_GUIDE.md) | Design through delivery, stopping and backups |
| [Capabilities](MODEL_CAPABILITIES.md) | Integrated features versus trials and blocked models |
| [Language screen](docs/LANGUAGE_MATRIX.md) | Languages each engine was screened in, with word error rates and the hardware used |
| [Drama](docs/DRAMA3.md) / [production studio](docs/DRAMA3_EXPANSION.md) | Native runtime, cast, streaming and editing limits |
| [Voice Slate bridge](docs/RECORDER_BRIDGE.md) | Importing clips kept by Fel's Voice Slate: consent and identity checks |
| [Scene schema](docs/scenes/scene-schema-v1.md) | Scripts, stable IDs and selection rules |
| [HTTP API](docs/API.md) | Calls for scripts and other programs: speak, speak as a Cast voice, the command-line tool, and [`openapi.json`](openapi.json) |
| [Responsible use](docs/RESPONSIBLE_USE.md) | Recording/voice rights and output restrictions |
| [Changelog](CHANGELOG.md) | What is in this release, and its known limits |

## Verify

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
node --test tests/ui_workspace.test.cjs tests/drama3_studio.test.cjs tests/theme.test.cjs tests/voice_workflows.test.cjs tests/voice_library.test.cjs tests/compare_cast.test.cjs tests/cast_reference_ui.test.cjs tests/engine_status.test.cjs tests/scene_history.test.cjs tests/take_edit_ui.test.cjs
.venv\Scripts\python.exe tools/check_repo.py
```

The last command checks the repository's files: it fails on obvious secrets, personal home paths, recordings, databases, logs and
model files, and on Markdown links to pages that are not in the repository.
The browser flow test in `tests/test_browser_flow.py` drives the page in headless Microsoft Edge or Google Chrome
when one is installed, and is skipped otherwise. Set `VOICE_LAB_BROWSER` to a browser path, and
`VOICE_LAB_BROWSER_SHOTS` to a folder to keep a picture of the working part of the page at each stage;
the screenshots above, in `docs/images/`, were made that way.
See [contributing](CONTRIBUTING.md) and [security](SECURITY.md). Optional-model tests report skips when assets are absent;
passing contract tests do not establish voice quality on every device/language.

## License

[MIT](LICENSE) for original application code, with [Apache notices](NOTICE) for
adapted components. Model licensing is separate: Breeze weights/self-hosted outputs
are non-commercial, and Drama has conditional commercial and hosted-service terms.
Check the full upstream licenses and recording permissions for your use. No model
weights, voice recordings or generated samples are bundled. No affiliation with
or endorsement by the model publishers is implied.
