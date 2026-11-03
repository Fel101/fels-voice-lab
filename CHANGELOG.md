# Changelog

## 0.1.0 — 2026-10-28

The first public release, for Windows 10/11 and Python 3.12. It runs from source, and model runtimes are installed
separately ([setup](docs/SETUP.md)). What was and was not checked for each model is in
[capabilities and limits](MODEL_CAPABILITIES.md). The limits of this release are listed at the end.

### Voices and Cast

- **Cloning a voice.** Browse for a recording or record one in the page, listen to the source and the result, write down its
  origin and allowed use, save it as a named Cast candidate and generate a saved test line. A reference can be WAV, FLAC,
  Ogg, MP3 or M4A (M4A needs FFmpeg). The original file is kept byte for byte as the source and the working copy is a WAV.
  Reference pickers show duration, provenance, use notes and a playable preview, and Kokoro's licensed fixed presets stay
  distinct from uploaded recordings.
- **Cast library.** Named voice cards with audio previews, the model that made each voice and the model that will speak for
  it, a search box, and fixed voices and speaker blends beside cloned ones. A reference is an immutable version
  with a transcript, origin, allowed use and approval. Pronunciation dictionaries and saved performances are kept with the
  character. [Voice asset rights](docs/VOICE_ASSET_RIGHTS.md) and [responsible use](docs/RESPONSIBLE_USE.md) say what a
  person has to arrange for themselves.
- **Reference screen.** The preview shows a score from 0 to 100 (good, review, poor) and plain warnings for clipping, a high
  noise floor, reverberant decay, steady tones such as hum or music in the quiet parts, and a second voice. It is advisory
  and never blocks a save ([docs/REFERENCE_SCREEN.md](docs/REFERENCE_SCREEN.md)).
- **Helpers around a reference.** Optional transcript drafts from a local speech recognizer; a person checks the text before
  saving it. Gated cleaning (`denoise_ref.py`): a cleaned clip that moves further from a same-speaker reference than the raw
  clip did gets a fail verdict, and strict mode refuses to write it. A voice match on takes uses Chatterbox Turbo's speaker
  encoder, which the Lab runs with NumPy, so it needs only the `ve.safetensors` that `setup-chatterbox.bat` fetches and no
  PyTorch in `.venv`; it is a cosine proxy, not an identity verdict. Its two bands, 0.847 and 0.796, were measured on 128
  clips of one target voice and ship in `catalog/speaker_similarity.json`, so nothing has to be measured first.
- **Retire and purge.** Retiring a voice, recording or take blocks new use of it and of everything made from it, without
  deleting audio. Perform, Scenes, Compare, voice conversion, the Drama studio, pickers and exports refuse retired items,
  which still play for review. Restoring lifts an item's own retirement and never re-approves a voice. Purging deletes a
  retired item, everything made from it and their Cast audio after an explicit confirmation, and leaves a tombstone with the
  checksums.
- **Voice Slate clips.** Consent-aware import of clips kept by [Fel's Voice Slate](https://github.com/Fel101/fels-voice-slate),
  one clip at a time, with identity checks against the clip's master ([docs/RECORDER_BRIDGE.md](docs/RECORDER_BRIDGE.md)).

### Performing and reviewing takes

- **Perform lines.** Choose a character and voice, write a line, add an acting instruction where the model supports one, and
  make 1 to 8 takes (3 by default) on consecutive seeds; a model without a seed control makes one. Every take is saved with
  its text, voice, reference, settings and model. **Stop after this take** lets the take in progress finish and skips the
  rest. The studio and Perform count the seconds while they work, and **Stop waiting** in the studio hands the page back at
  once while the engine finishes the line.
- **Checks on every take.** Automatic waveform checks (silence, samples at full scale, a pause of a second or more inside the
  take, audio much shorter than its text) and loudness readings: integrated loudness (LUFS, ITU-R BS.1770 as `pyloudnorm`
  computes it), sample peak (dBFS) and true peak (dBTP, read at four times the sample rate), with a note when a true peak
  above -1 dBTP could clip in MP3, AAC or Ogg export. They are made when the take is saved and kept with it. They describe
  the audio only; nothing in them rates the words, the voice or the acting.
- **Edit a take.** **Edit take…** opens an editor under the take: the take drawn as a waveform with the kept part tinted and
  the speech underlined, a trim by dragging across the waveform or typing times, **Cut the silence at both ends** (anything
  quieter than -50 dB unless you change it, 80 ms left at each end, pauses inside the speech untouched), fades in and out,
  and a gain from -60 to 24 dB. **Hear the edit** plays the result and saves nothing; **Save as new take** saves a new
  candidate that names its original, and the original is never changed. The steps run in a fixed order (trim, silence,
  fades, gain). A trim alone copies the samples exactly, a gain that would clip is refused with the most the take can take,
  and a result under 0.05 s is refused. An edit is a take like any other: it keeps the original's text, voice, settings and
  model, its checks are measured again on the new audio, and a scene line can choose it and the exporter loads it. Word and
  phoneme times move with the trim. Retiring a take retires its edits, and purging it deletes them.
- **Compare and rate.** Selected Cast takes, including different voices from one model, go into checksummed blind sessions
  with separate listening copies. Pairing needs identical text, spoken text, language and direction. Each sample gets its own
  A/B score, there is an optional preference, and a revealed session stays locked.

### Scenes, batch and export

- **Scenes.** A script of lines with stable ids, each with its own character, voice, reference version, language, pauses,
  target duration and settings, imported from JSON or CSV or started from the current performer
  ([scene schema](docs/scenes/scene-schema-v1.md)). Undo and redo cover field changes, settings, identity changes, line adds,
  moves, duplicates, removals and edits to the advanced JSON: Ctrl+Z, Ctrl+Y or Ctrl+Shift+Z, Ctrl+S to save, and Alt+Up or
  Alt+Down to move the line you are in.
- **Batch.** Jobs render one take at a time, can be paused, resumed, cancelled and retried, and resume after a restart. A
  selected take that no longer matches its line is refused. Previews assemble the selected takes.
- **Export.** Packages are written only under `out/exports/` in the state root, and an existing package is never
  overwritten. A package holds the line files in the formats you choose (WAV, FLAC, Ogg, MP3 at about 190 kbit/s variable
  rate, and AAC in M4A at 192 kbit/s), the raw scene mix, captions, a transcript and a manifest of checksums. Original Cast
  takes are copied byte for byte. Before anything is written the export checks that this FFmpeg build has the encoder and
  takes the sample rate, and says so in words if not. **Verify** reads the package back: hashes, captions, and each file's
  codec and length with ffprobe.
- **Word times and fit.** `align_takes.py` aligns Cast takes that have no word times, in one batch, with the local
  faster-whisper tiny model (MIT code and weights; nothing is downloaded); see [docs/ALIGNMENT.md](docs/ALIGNMENT.md).
  Captions are written as word phrases wherever every take of a line has word times, and as one caption per line otherwise.
  Boundary accuracy is not measured, so treat word times as approximate. `fit.py` is a bounded time-stretch (15 % either way
  by default) whose status (`within_slot`, `stretched`, `needs_rerender`) the export records against each line's target
  duration; the export does not stretch the audio.
- **Lip-sync tracks.** The export writes one mouth-shape track per line that has phoneme times, at
  `lipsync/<character>/<line>.json` ([docs/LIPSYNC.md](docs/LIPSYNC.md)). Kokoro takes store the model's own predicted
  phoneme times and the export prefers them, in any language Kokoro speaks. Other English takes share their aligned words
  over estimated phoneme times with misaki's English rules; other languages get a stated reason instead of a track. Against
  Kokoro's own predicted timing on 40 sentences, the median word-start error is 35 ms and the median phoneme-start error is
  39 ms (12.5 ms with exact word times, as a control). That shows agreement with Kokoro's prediction, not accuracy against
  real speech, and the mouth-shape mapping has not been checked against a face or by ear.

### Models

- **Integrated families:** Kokoro, Phonon 31M English, Chatterbox Original/Turbo/VC, Breeze TTS 2, Qwen3-TTS
  VoiceDesign/Base/CustomVoice, VoxCPM2 and Fish Audio Drama 3. The catalog records each pin; a catalog entry means an
  integration exists, not that the package, model or hardware is present.
- **Kokoro install.** `setup-kokoro.bat`, or `tools/install_kokoro.py`, installs Kokoro for the CPU: 89 pinned packages with
  the CPU build of PyTorch, each file checked against a SHA-256 in `requirements/kokoro-cpu.txt`; the pinned checkpoint and 41
  voices from Hugging Face, resumable and checked against the SHA-256 in `catalog/kokoro_snapshot.json`; then a speech check that
  loads the model without a network. About 570 MB to download and 1.4 GB on disk. It shows its plan and asks first, runs only
  with the lab's own `.venv`, and refuses to replace a package that is installed at another version, so an environment with its
  own PyTorch keeps it (`--checkpoint-only` adds just the model files). Run again, it installs only what is missing. A studio
  page that was open during the install reads the new voices when you press Generate, so it needs no reload.
- **Phonon.** Phonon 31M English (Gradium) speaks English in 37 preset voices, 12 American, 13 British and 12 characters, on the
  CPU through the numpy-only `ptts` runtime, with no PyTorch. It takes a seed, a temperature and a pace shift (`duration_delta`,
  -0.5 to 0.5), and cannot clone, design a voice or take a direction. Its voices are reusable Cast identities, so Perform lines,
  scenes and batches speak them. `setup-phonon.bat`, or `tools/install_phonon.py`, installs the one package pinned in
  `requirements/phonon.txt` and the pinned model and voice files from Hugging Face, each checked against a SHA-256
  (`catalog/phonon_snapshot.json`), then runs a speech check without a network: about 82 MB to download and 90 MB on disk. It
  follows the Kokoro installer's rules (it shows its plan and asks first, runs only with the lab's own `.venv`, refuses to
  replace `ptts` at another version, and has `--checkpoint-only`), and the **Install** button on its card runs it. The weights
  are Apache-2.0, but the repository that holds the 37 voices states no licence, so the installer's plan, the page and the docs
  say to ask Gradium before you publish audio made with one. Installing `ptts` changes `.venv`, so the Models page marks the
  stored tests of the other models there as out of date.
- **Chatterbox Turbo install.** `setup-chatterbox.bat`, or `tools/install_chatterbox.py`, installs Chatterbox Turbo 350M, the
  model that clones a voice from a recording of more than five seconds, for the CPU. It makes an environment of its own,
  `.venv-chatterbox`, from the lab's Python 3.12, because Chatterbox needs PyTorch 2.6 and Kokoro's installer puts PyTorch 2.12
  in `.venv`. There it installs 73 packages pinned by SHA-256 in `requirements/chatterbox-cpu.txt` (without the Gradio demos and
  the multilingual text packages Turbo never imports), fetches the ten files of the pinned Turbo snapshot that Turbo loads
  (resumable, each checked against `catalog/chatterbox_turbo_snapshot.json`), and checks without a network that Turbo speaks in
  its built-in voice and clones that recording: about 3.4 GB to download and 5 GB on disk. It follows the Kokoro installer's
  rules (it shows its plan and asks first, runs only with the lab's own `.venv`, refuses to replace a package at another
  version, and has `--checkpoint-only` for a `.venv-chatterbox` you built yourself, with a GPU PyTorch say), and the
  **Install** button on the Turbo card runs it. The code and the weights are MIT, and every clip carries Resemble AI's Perth
  watermark. Turbo needs 8 GB of free RAM to load: on one 8-core CPU it peaked at 6.1 GB while loading and held 5.0 GB after a
  line of 1,200 characters.
- **Chatterbox's environment.** Chatterbox Turbo, Original and VC now run in `.venv-chatterbox`, not `.venv`. Turbo loads the
  files of its pinned snapshot directly, so it never asks the Hugging Face Hub, and without `reference/af_heart.wav` its worker
  warms up and its Models page test speak in the built-in voice instead of reaching for Kokoro, which that environment does not
  have. A call that clones without a `reference_path` while that clip is missing is refused at once, with what to do. A
  Chatterbox set up by hand in `.venv` is not found any more: run the installer, or build `.venv-chatterbox` with your own
  packages and use `--checkpoint-only`.
- **Models & features** reports three states for each family: integrated, installed (its environment and pinned checkpoint
  files are on disk) and usable (a test on this device passed). **Test** loads the family on the service device,
  speaks one short sentence and reports the time it took; missing files are reported without loading anything, and a
  missing Kokoro points to Install on its card and to `setup-kokoro.bat`, as does the error of speaking before it is
  installed. Results are
  kept per device. A stored test carries a fingerprint of the model pin and the packages installed in its environment, and
  reads "test out of date" when either changes. A speed table shows the median real-time factor per family and device from
  clips and tests this service generated.
- **Install from the page.** A Kokoro, Phonon or Chatterbox Turbo card that is not installed has an **Install** button. It shows
  the installer's own plan (the `--dry-run` of `tools/install_kokoro.py`, `tools/install_phonon.py` or
  `tools/install_chatterbox.py`, with the reasons it would refuse), and **Install now** runs the installer that
  `setup-kokoro.bat`, `setup-phonon.bat` or `setup-chatterbox.bat` runs, with the lab's own Python and the end of its output
  on the card. While it runs nothing else loads a model or speaks, a page
  that is reloaded picks the progress up again, and the whole output is kept in `out/install`. There is no Stop button:
  closing the studio stops the install, and running it again continues where it stopped. The other cards say that the app
  cannot install that model yet. (`POST /api/engines/{engine_id}/install`; `GET /api/engines/status` reports the install.)
- **RAM on each card.** Every card says how much free RAM the Lab wants before it loads that model, and the section says how
  much RAM this computer has. A card turns red when the whole computer has less than that or when less is free right now.
- **First run.** A studio with no model installed says so at the top of Model studio and links to Models & features. The
  model pickers in Model studio and Voice cloning mark each model that is not installed, and Model studio says what to do
  about the one you picked. On Models & features the models the app cannot install yet are folded under "More models", with a
  count, and the glossary sits under "What these words mean".
- **Themes.** **Dark** is now neutral grey. The earlier dark green theme is still there as **Forest**, and System follows
  Windows to Light or the neutral Dark. Compare uses the same choice. Its primary button had white text on the light accent in
  both dark themes (1.8 to 1 contrast); it is dark text now, as on the other pages. A test keeps every theme on both pages
  and checks that text and buttons stay readable (4.5 to 1 or better) in each.
- **Removing the Lab.** [docs/SETUP.md](docs/SETUP.md#removing-the-lab) says what to back up, what to delete (the folder and
  the Desktop shortcut) and what lives outside the folder (pip's cache and the programs installed separately).
- **Language screen.** It speaks each language an engine claims, has a speech recognizer write the words back, and records a
  word error rate for each engine and language (character error rate for Chinese and Japanese). The run covered 75 claimed
  cells from 216 generated clips on an AMD Radeon AI PRO R9700: 56 understood, 10 partial, 4 not understood, 2 inconclusive
  and 3 unavailable. Picker entries that are partial or not understood stay available and are marked "(unscreened)". It is a
  screen, not a listening test: nobody listened to those clips, and it does not judge accent, voice quality or likeness
  ([docs/LANGUAGE_MATRIX.md](docs/LANGUAGE_MATRIX.md)).
- **Regression check.** `evaluation_regression.py` speaks one evaluation suite through one engine and compares it with a saved
  baseline: whether each clip spoke, word error, the speaker proxy for clone engines, and clip length. Run it before a
  dependency or checkpoint pin changes ([docs/REGRESSION.md](docs/REGRESSION.md)).
- **Drama 3 (Fish Audio).** Design and cloning through a pinned native speech.cpp runtime that `tools/install_drama3.py`
  installs after you read its license (a 20.8 GB model download, started explicitly). A studio for up to sixteen reference
  speakers with inline cues and long joint scripts, incremental PCM audition, cancel, retained job history and mixed-audio
  packages. The studio and cloning use the CPU.
- **VoxCPM2** refuses a GPU with under 8 GB of memory; it peaked at 5.6 GB when measured.

### Library, service and desktop

- **Desktop.** `setup-desktop.bat` creates a **Fel's Voice Lab** shortcut that opens a dedicated window over a hidden backend
  on a free loopback port. Closing the window cancels its work and stops the backend and its children, and never closes a
  service that was started separately. The Light, Dark, Forest and System themes are saved with the library.
- **System & backups.** One page for the version, Python and Windows, the device and free memory, the library folder with
  the schema number of each database and how much is in it, FFmpeg and which formats this build can write, free space
  (flagged under 5 GB), and the models counted by state. Nothing on it loads a model. **Copy report for a bug report** puts
  those facts on the clipboard as plain text with no scripts, transcripts, names or recordings, and the home folder reads as
  `~`. **Measure folders** sizes each library folder on request, and **Clean up old scratch folders and logs** runs the
  start-up sweep on demand.
- **Library backup.** **Back up now** writes one ZIP into `backups/`: the Cast, Scenes and Comparisons databases with their
  audio and the appearance setting, plus saved voices, export packages and Drama studio jobs when ticked. Databases go in as
  SQLite online-backup copies, so they are consistent while the service runs. The ZIP is written under a temporary name,
  read back, each file compared with its checksum and each database put through SQLite's integrity check, and only then
  renamed; one that does not check out is discarded with the reason. **Check** reads a backup in full again. **Restore**
  unpacks into a new folder in the library, never into one that holds anything, verifies as it goes and removes everything
  it made if anything fails; the library in use is never touched. **Open the restored library next time** writes the state
  root into `desktop.local.json`. `tools/backup_library.py` does the same from a terminal.
- **Safety.** Before the Cast, Scenes or Evaluations database moves to a newer schema, the service copies it (SQLite's online
  backup) into `migration-backups`, keeps the newest three, and stops without changing anything if the copy fails. A library
  moves to the newest schema (Cast schema 9) when it is first opened, and an older build refuses a library at a newer one.
- **Service log.** Each run writes `out/logs/service-<date>-<time>-<id>.log`: a line for every action and every failure with
  a request id, the status, the seconds, the engine and the error. Script text, request bodies and query strings are never
  written. The id comes back in the `X-Request-Id` header. A log rotates at 2 MB and keeps two older parts.
- **Housekeeping.** A worker that stops removes its temporary-audio folder. At start the service clears what past runs left
  in `out/` (stopped workers' folders, old desktop sessions, old logs) in the background, and never touches Cast, Scenes or
  exports. `tools/cleanup_runtime.py` lists what it would remove and `serve.py --no-cleanup` skips the sweep.
- **Waiting connections.** The service keeps up to 128 connections waiting for it, not Python's 5. A page asks for its ten
  scripts side by side, and Windows refuses the connection that finds the queue full, so a page opened while the service
  was busy for a moment would come up without one of them.
- **Core install.** The core install works without `librosa` and `misaki`; both are imported only where they are used.
- **Version.** The one version number lives in `app_version.py`. The page footer, `/api/health`, the service log and
  `serve.py --version` all read it.

### For scripts and other programs

- **HTTP API.** [docs/API.md](docs/API.md) and `openapi.json` (OpenAPI 3.1) document 87 routes; 34 more serve the Lab's own
  pages and are internal and may change. Both are generated from one table (`api_spec.py`) by `tools/build_api_docs.py`, and
  a test fails when the table and `serve.py` disagree. `POST /api/speak` is a blocking call that answers one finished WAV;
  `POST /api/performance` speaks a line as a Cast voice, saves it as a take and can be repeated safely with an
  `idempotency_key`. A running service answers `GET /api/openapi.json`. A request refused for its size, or a small one
  refused as coming from another website's page, is read through first, so a client that is still sending gets its `413`
  or `403`.
- **Command line.** `tools/lab_cli.py` calls a running service from a terminal or a script: `health`, `speak`, `perform` and
  `cast`. It needs only the Python standard library. Its exit status is 0 when the call worked, 1 when the service refused it
  or failed, 2 for a mistake in the command line and 3 when nothing answered.

### Checked

- In a clean checkout, with an environment built from `requirements/core.txt`, CI's steps pass: `ruff check .`, 1308 Python
  tests (17 skipped, for model packages and files, FFmpeg encoders and link privileges that environment lacks), 185 Node
  tests and `tools/check_repo.py`.
- The browser flow test drives the page in headless Microsoft Edge against stand-in engines: it clones a voice, performs a
  line, edits a take, edits and renders a scene, exports and verifies a package, and makes, checks and restores a backup.
  It also takes the screenshots in the README.
- Real packages were written with FFmpeg 8.1.1 and read back by the verifier, and a real MP3 and a real M4A were imported.
- The Kokoro installer was run from an empty folder (Python 3.12.10, Windows 11, CPU only). After `setup_core.py` it installed the
  89 packages and the checkpoint with its 41 voices in 296 seconds with pip's cache warm, and the speech check passed. A speech recognizer read
  the check clip back as the check sentence, one sentence in each of six languages came back intelligible, and the studio
  started from that folder passed **Test** and spoke through `/api/speak` with an American voice, a British voice
  and a blend. A download stopped at 62 MB resumed from its partial file and matched its hash, a damaged voice file was
  fetched again, and a package installed at another version was refused.
- The **Install** button on the Kokoro card was pressed in a studio started from a fresh copy of the repository after
  `setup_core.py` (Windows 11, CPU only, pip's cache warm). It showed the installer's plan, ran it with the newest lines of its
  output following along on the card, and finished in 277 seconds with the card reading *Installed, not tested on CPU*. A page
  reloaded in the middle picked the install up again, and **Test** then loaded the model and spoke one sentence (2.7 s of
  audio in 0.8 s).
- Phonon was installed with the **Install** button of a studio started from a fresh copy of the repository that had only the core
  packages (Windows 11, CPU only, pip's cache warm). The installer showed its plan and finished in 30 seconds, and **Test** then
  passed (2.6 s of audio in 0.3 s). On the real model all 37 voices spoke through `/api/speak`, each as a different clip; a Cast
  identity made a Perform take; and a two-line scene rendered as a batch and assembled a preview. An unknown voice, reference
  audio, speed, a direction, a temperature or pace outside its range, another language and a fractional seed were each refused
  with a `400` that says why. A pace shift of -0.5 and +0.5 changed one sentence's length by -22% and +19%. A seed repeats a clip
  to within rounding and not byte for byte (differences near a millionth of full scale), and one thread instead of all of them
  moved the end of a clip by one 80 ms frame.
- Chatterbox Turbo was installed from fresh clones of the repository after `setup.bat` (Windows 11, CPU only, pip's cache
  warm). `setup-chatterbox.bat` made `.venv-chatterbox` and installed the 73 packages; stopped during the download and run
  again, it kept them, resumed the half-fetched file and finished in 252 seconds, and its speech check spoke in the built-in
  voice and cloned it. The studio started from that folder passed **Test** (2.4 s of audio in 6.6 s); the calls the Voice
  cloning page makes saved a voice from an 11.5-second recording and spoke a test line in it, which the voice match scored
  0.92 against that recording and 0.78 against the built-in voice. The worker peaked at 6.1 GB. In a second fresh clone,
  **Install** on the Turbo card ran the installer in 308 seconds with the model files already in `hf-cache`, and **Test**
  passed. The NumPy speaker encoder gave the same embeddings as Chatterbox's own to within 2e-7 on speech clips.
- The new Models & features layout, the first-run banner and the Dark and Forest themes were looked at in a browser at phone
  width. The Light theme was not changed.

### Known limits

- **Platform.** Windows 10/11 only. Built and checked on one machine with an AMD Radeon AI PRO R9700; there are no NVIDIA,
  Linux or macOS results. Only Kokoro, Phonon, Chatterbox Turbo and Drama 3 have installers, and VoxCPM2 needs pinned source
  and a ROCm overlay that this repository does not install. Nothing here trains or fine-tunes a model.
- **Kokoro install.** It was run on one Windows 11 computer. A computer without the Microsoft Visual C++ runtime, a folder
  path near Windows' 259-character limit and a network that blocks PyPI or Hugging Face were not tried for real; the installer
  stops with a message in each case, and its tests cover them with stand-ins. The Japanese and Mandarin voices need Misaki
  language packages that it does not add.
- **Phonon.** English only, preset voices only, and CPU only. The repository of its 37 voices states no licence, so what
  audio made with them may be used for is unknown. A seed repeats a clip closely, not byte for byte, and not across
  computers. It is not in the language screen (a trial read 44 clips back at a mean word error rate of 3.8%), nobody has
  listened to it as a test, and it was run on one Windows 11 computer. A missing Visual C++ runtime, a folder path near the
  259-character limit and a network that blocks PyPI or Hugging Face were not tried for real; the installer's tests, and the
  Kokoro installer's for the download code the two share, cover them with stand-ins.
- **Chatterbox Turbo.** The installer sets it up for the CPU only, where it is slow: two and a half to three times as long as
  the speech it makes on one 8-core CPU. A GPU build of PyTorch is yours to install in `.venv-chatterbox`, and that has not
  been tried with this installer. It speaks English, and given 1,200 characters it stopped after 30 to 36 seconds of audio,
  so keep lines to a few sentences. Chatterbox Original and VC share its environment, but no installer fetches their
  checkpoint. The installer was run on one Windows 11 computer; a missing Visual C++ runtime, a folder path near the
  259-character limit and a network that blocks PyPI or Hugging Face were not tried for real, and its tests cover them with
  stand-ins.
- **Install button and RAM figures.** Only the Kokoro, Phonon and Chatterbox Turbo cards have an Install button, and they have
  no Stop button. Their progress is the installer's own output, so a quiet step, such as pip unpacking PyTorch, can show
  nothing new for minutes. The RAM on a card is the Lab's own rule for loading a model, not a measurement of what the model
  uses.
- **What the numbers mean.** The language screen is not a listening test. Take checks and loudness readings describe audio
  and nothing else, a voice match is a proxy whose bands come from one target voice, word-time boundary accuracy is
  unmeasured, and the lip-sync mouth shapes have not been checked against a face or by ear.
- **Drama.** GPU cloning is off after a Vulkan device loss and a Windows VIDEO_TDR_FAILURE, and no driver workaround is
  supplied. The pinned backend has no previous-take editing, speaker stems or native word timing. Voice conversion has no
  speech check, because it needs source audio.
- **Editing a take.** An export manifest names an edited take but not the steps that made it. `align_takes.py` on an edit that
  cuts words from a take that had no word times aligns the whole text against the shorter audio. An open edit panel is lost
  when the take list reloads, and the waveform is not redrawn on a theme change until the next interaction.
- **Scenes and loudness.** Undo stops at the last save, take choice or reload, and the history is not kept after the page
  closes. Compare and the Scenes page do not show loudness, and nothing is normalised by the readings.
- **Backups.** They are not scheduled, not encrypted and cannot be deleted from the page. A restore does not merge into a
  library that already holds work, and a take that is half way through saving when a backup starts is not in it.
- **Service.** **Stop waiting** cannot interrupt the engine, only the page's wait. The service log has no viewer in the page.
  A stored check does not notice a checkpoint file replaced in place under the same name (Breeze and Drama have no
  revision-named folder).
- **Retirement.** It does not reach loose files in `reference/` and `data/voices`, and there is no character-level button.
