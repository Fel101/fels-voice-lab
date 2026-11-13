# Changelog

## Unreleased

Every NVIDIA build has now run on an NVIDIA card: on a laptop with a GeForce RTX 5070 Laptop GPU (8 GB), each model installed
and passed **Test** with no change to the Lab. The first try there failed only because the laptop had switched its NVIDIA card
off, and the installers did not notice, so they now ask NVIDIA's driver itself before they download anything.

### NVIDIA graphics cards

- **A switched-off card is caught before the download.** A laptop's power-saving, Eco or iGPU-only mode can switch its NVIDIA
  card off while the driver stays installed. Windows then still lists the card, `nvidia-smi` blames permissions, and the
  installers went ahead, downloaded every package and model file, and failed at the speech check. Each installer now asks the
  driver itself (`nvcuda.dll`, in a Python of its own) whether CUDA starts, and stops before any download when it does not,
  naming the card and saying where such a card is switched back on. The driver's answer also gives the card's generation and
  memory when `nvidia-smi` cannot.
- **PyTorch is asked to start CUDA after pip.** On NVIDIA's build each installer has the PyTorch it just installed start CUDA
  and list the card before it fetches the model files, and stops with PyTorch's own error, or when the build has no code for
  the card.
- **A memory check is never skipped.** VoxCPM2 and Breeze TTS 2 need 7 and 8 GB of graphics memory. When neither
  `nvidia-smi` nor the driver could say how much the card has, the check was skipped without a word; the installers now stop
  and say so.
- **Failures say what happened.** A failed check now shows the error it ended on first, leaves out the warning the sox package
  prints on every start, and keeps all of its output beside the check's clip (`install-check.log` and the like). The hint
  under it matches the error: no card PyTorch can use (with what to check on a laptop), a driver too old for CUDA 13.0, a
  build with no code for the card, or a card out of memory; it no longer says to update the driver for every error that
  mentions CUDA, and it offers the CPU build only for Kokoro and Chatterbox.
- **Breeze TTS 2 on an 8 GB card.** Its installer refused a card with less than 10 GB, yet Breeze passed **Test** on the
  laptop's 8 GB card with 7.4 GB in use. It now takes an 8 GB card, and its plan says that a long line may run out of memory
  there, as a 29-second take allocated 9.5 GB elsewhere. When a line does, Breeze says to split it into shorter lines instead
  of showing PyTorch's error.
- **Models & features notices a card switched on.** The service read the NVIDIA cards once and kept the answer until it
  restarted; it now reads them again after half a minute. A model that ran on the CPU because the card was not available
  when it loaded no longer blames its PyTorch.
- The installers, `setup.bat`, the guides and [MODEL_CAPABILITIES.md](MODEL_CAPABILITIES.md) give the laptop's results in
  place of "not yet tried on an NVIDIA card". The comments in `requirements/*-nvidia.txt` still say it, because changing
  those files would make `update.bat` ask for every NVIDIA model to be installed again.

### Fixes

- **Players can seek.** A `GET` that answers with audio now says `Accept-Ranges: bytes` and answers a `Range` request with
  `206` and that part of the file. Without it Chromium would not seek in a recording loaded by its address, so a scene line's
  player, the Compare page and the voice library could only play from the start. Recordings made by a `POST` are unchanged.
- **The conversion lists on a folder spelled another way.** The source and target lists for Voice conversion, and a
  conversion made from an uploaded target, answered `400` when the Lab's folder path was spelled otherwise than its resolved
  form, as with a short folder name such as `RUNNER~1`. That made three tests fail on GitHub's runner for 0.2.0. A Lab
  started with `serve.py` or the desktop app was not affected, because it resolves its own folder.
- **A clip that reaches full scale gets headroom.** A model can overshoot full scale by a sample or two, and Chatterbox 500M
  did in the new smoke test. Written as 16-bit, those samples were cut off, and the take checks flagged the take as clipped.
  Every clip a model makes, and every voice conversion, is now scaled to a -1 dBFS peak when its peak reaches full scale;
  quieter clips are left exactly as they are.
- **A voice card's report stays.** After **Retire voice**, **Restore voice** or deleting a voice for good on its card, the
  Cast library redrew its cards and wrote the voice count over the report at once, so the list of copies outside Cast that
  a delete leaves for you to remove never stayed on screen. The report now stays, above the count.
- The Clone page's note on Common Voice no longer says Cast cannot take MP3.

### Setup

- **No Python to install.** On a computer with no Python 3.12, `setup.bat` now downloads one for the Lab alone: Python
  3.12.10 from nuget.org, where python.org publishes it as a package (about 14 MB), checked against its SHA-256 and unpacked
  into the Lab's `python` folder. It installs nothing into Windows, and every environment the Lab makes is built on it. A
  computer with Python 3.12 keeps using it and downloads nothing. **System & backups** says which Python the Lab runs on.
  Keep the `python` folder. After moving a Lab set up this way, delete its `.venv` folders and run `setup.bat` again
  ([Moving the Lab](docs/SETUP.md#moving-the-lab)).
- **Setup.exe.** Each release now has `FelsVoiceLab-Setup-<version>.exe`. It installs for you alone, with no administrator
  prompt, into `%LOCALAPPDATA%\FelsVoiceLab` (it warns about a folder OneDrive syncs), carries the Lab's own Python and the
  packages of the core and the desktop window so it needs no internet, sets both up, adds a Start menu shortcut (and a
  desktop one if ticked) and opens the Lab. Models install from **Models & features** as before. Uninstalling in Settings,
  Apps asks whether to keep your library and keeps it unless you say no. The installed folder holds the release's files, so
  `update.bat` updates it like an unpacked ZIP. It is not signed yet: Windows SmartScreen warns, and
  [Verify a download](SECURITY.md#verify-a-download) says how to check the file against `SHA256SUMS` and its GitHub
  attestation. Built by `tools/build_installer.py` with Inno Setup; `tools/setup_core.py` and `tools/setup_desktop.py` take
  `--wheels`, and the desktop one `--no-shortcut`.
- **FFmpeg from a button.** Scene export, reference cleaning and M4A references need FFmpeg, which meant leaving the Lab to
  run `winget`. **Install FFmpeg**, on **System & backups** and on a scene's export panel while no FFmpeg writes every
  format, now downloads FFmpeg 9.0.2 (Gyan Doshi's essentials build, GPL v3, about 115 MB) from its GitHub release, checks
  it against its SHA-256, tries that it writes every export format, and puts it in the Lab's `ffmpeg` folder. The page
  follows the download and uses FFmpeg as soon as it is in place, with no restart; a download that stops is continued by
  pressing the button again. `setup.bat` offers it when it finds no FFmpeg (`--ffmpeg` installs it unasked), and
  `tools/install_ffmpeg.py` does it from a terminal. An FFmpeg you installed yourself still works; the Lab's own comes
  first, and `update.bat` leaves it alone ([FFmpeg](docs/SETUP.md#ffmpeg)). New routes: `GET` and
  `POST /api/ffmpeg/install`. The `ffmpeg` part of `GET /api/exports` and `GET /api/system` gains `own` and `offer`.
- **The first run installs a model from the studio.** A Lab with no speech model said to go to **Models & features** and press
  Install there. The note on **Model studio** now has the **Install** button itself, with how much the model downloads and
  how much disk it takes, and starts the same install as the model's card. When the install it started finishes, the studio
  is ready to speak: the model chosen, a sentence in the box and the focus on **Generate**. The README starts with a
  [Quick start](README.md#quick-start) with measured times, and [Setup](docs/SETUP.md) with the quick way.
  `GET /api/engines/status` gives each model that is not installed an `install_size`, from its installer's own numbers.
- **A model card in plain words.** A card for a model that is not installed read "Missing:" and a list of file paths and
  snapshot folders. It now reads *Not installed yet* and "Install downloads about 0.6 GB and uses 1.7 GB of disk."; the
  missing files, the installer's name and the log of an install that stopped are under **Details** on the card, and
  `GET /api/engines/status` still lists the files as before.

### Scenes

- **A script becomes a scene.** **Import a script from text or Fountain** takes a `.txt` or `.fountain` file, or a pasted
  script. Plain text has one speech per line, `NAME: words`, with a direction in brackets or parentheses as the acting
  direction; Fountain's character cues, dialogue and parentheticals become lines, its extensions come off the names, its
  title names the scene, and its headings, transitions, notes and boneyard are left out. **Read the script** counts the
  lines and speakers and lists, by line number, what it could not read. Each speaker then gets a voice, set already when a
  Cast character has the same name, or is left out; **Create the scene** stays off while a speaker has neither, and the
  service refuses it too. Lines with no name and Fountain action are narration, spoken only when a narrator voice is
  chosen. After the import the panel lists every line left out and why, with any acting direction left out because the
  voice's model takes none. `POST /api/scenes/import` takes `text` and `fountain` with a `speakers` map, and
  `POST /api/scenes/import/preview` reads a script without making anything.
- **Scenes as a table.** A scene opens as a table, one row per line: a tick box, its number, character, the start of its
  text, its voice and model, its status, and **Play** (its chosen take), **Render** and **Open**. **Open** shows the line's
  takes, each with **Use this take**, and the line's full form, which is built only then, so a 300-line scene opens in about
  a tenth of a second. A line split into parts lists each part with its own takes and **Render this part**. The statuses are
  not rendered, waiting, rendering, failed (with the reason), choose a take, done, and out of date (with what changed after
  the take was made); a row with unsaved edits says so. Above the table: **Tick all shown**, **Render ticked**, **Render
  all**, **Render what is missing or out of date**, **Use the newest take where none is chosen**, and filters by character
  and by status. The box for typing line ids to render is gone; a line's name is in its details. Rendered takes are still
  listed by batch, under **Takes from this batch**.
- **Two scene routes.** `GET /api/scenes/{scene_id}/lines` says where every line and segment stands, with its takes, in one
  answer. `POST /api/scenes/{scene_id}/choose-newest` chooses the newest take that fits for every segment without one, in one
  new revision.
- **Saving a long scene is quicker.** Saving read each line's voice, reference and chosen take on its own: 1.8 s for a
  300-line scene with chosen takes. It reads each voice once now, and the takes together: 0.13 s.
- **A batch says its order.** A batch has always rendered all of one model's lines before it loads the next model (it sorts
  its lines by model, then reference, then script order), but nothing said so and no test held it. While a batch runs, the
  status line now names the model rendering and the ones to come, with how many lines each has, and a test fails if a scene
  that alternates two models makes them take turns. The scene is still assembled in script order. Each row of
  `GET /api/scenes/jobs/{job_id}` gains `engine_id`, and the job gains `models`.

### Waiting for the model

- **Requests wait in line instead of being turned away.** The model worker still does one thing at a time, and a request
  that needed it while it was busy was refused, so **Make takes** during a scene batch failed with "busy". The page's
  requests now wait their turn, ahead of the batch's next line: the take comes after the line being rendered, and the batch
  carries on. At most 20 requests wait.
- **Model worker.** At the foot of the navigation, while the worker is busy: what runs and for how long, with **Stop**, and
  where each of the page's waiting requests stands, such as "Waiting: 2 ahead of you (a scene batch is rendering)", with
  **Cancel**. **Stop** ends the model's process, so a long clip or take ends within moments and the next request loads the
  model again. An install and an unload cannot be stopped.
- **Stop now for a batch.** **Cancel after this line** is what **Cancel** did: the line being rendered finishes. **Stop now**
  also stops that line, and marks it cancelled.
- **For scripts.** A model route's JSON body takes `wait_seconds` (up to 3600) and `wait_priority` (`low` waits behind the
  others, as a batch's lines do); without `wait_seconds` a busy worker still answers 409 at once. `GET /api/queue` says what
  runs and what waits, `POST /api/queue/{request_id}/cancel` takes a request out of the line or stops it, and the cancelled
  request answers 499. A request's id is the `X-Request-Id` it sends. `POST /api/scenes/jobs/{job_id}/cancel` takes
  `stop_current`.

### Plain words on the page

- **One word for one thing.** The page used several words for one thing, and some that only a programmer would know.
  Labels, messages and the [user guide](docs/USER_GUIDE.md) now keep to five: a *voice recording* is the recording a model
  clones (it was a reference, identity reference, prompt reference or local reference), an *acting direction* tells a model
  how to say a line (delivery instruction, acting instruction, delivery cue), a *voice* is a character's voice in Cast
  (voice identity, voice candidate), a *model* is what speaks (engine, backend), and a long line is split into *parts*
  (segments). 109 labels changed, among them **Record from the microphone**, **Exact words in the recording**, **Where it
  comes from**, **Who may use it, and how**, **Save the voice and hear it**, **Save the recording only**, **Takes to
  make**, **Make takes**, **Model settings**, **Show the parts of long lines**, **Build the scene from the chosen takes**
  and **Try the failed lines again**. The Breeze TTS 2, Qwen3-TTS and VoxCPM2 panels are headed by the model's name, and
  Scenes & batch by the menu's. Element ids, API fields, file formats and stored data are unchanged, and the guide's
  screenshots are new.
- **Delete for good, everywhere.** Cast's **Purge voice audio…** and **Purge take…** are **Delete this voice for
  good…** and **Delete this take for good…**, like the recording's **Delete this recording for good…**, and the report
  after one counts voices, voice recordings and takes, where it said identities and references.
- **Perform's line name is under Details.** **Stable line ID** sat between the voice and the dialogue, a box most people
  never need. It is **Line name** now, under **Details**, and still filled in for you; a scene's line says the same.
- **The speed table says what it measures.** **Measured speed on this service**, with its *Device* and *Median real-time
  factor* columns, is **Measured speed on this computer**, with **Runs on** and **Typical time per second of speech**, and
  the figure has its unit ("0.25 s").
- The library backup names its first part *Cast voices, voice recordings and takes*.

### Releases

- **A release is published only after its checks pass.** 0.2.0 was published by hand while three of its tests failed on
  GitHub's runner. Pushing a version's tag now runs the checks every push gets on the tagged commit, and only when they pass
  does GitHub publish the release, with this changelog's section as its notes (`tools/release_notes.py`). A tag that is not
  `v` and the version in `app_version.py` is refused, as `update.bat` would refuse it.
- **A real model speaks.** Every other test runs without loading a model. `tools/real_model_smoke.py` starts the service,
  runs a model's **Test**, has it say one sentence and checks the clip: its sample rate, its length for the sentence, that it
  is audible and not clipped, and that the AI-generated speech label inside it names the model. `--all-installed` tries
  every installed model and lists each one's load time; the clips are kept in `out\smoke\` for a person to listen to. A new
  weekly workflow runs it with Kokoro on a Windows runner with no Python, after `setup.bat` and Kokoro's installer
  ([CONTRIBUTING.md](CONTRIBUTING.md#making-a-release)).
- **The page's JavaScript is checked.** The page's scripts share one global scope, and nothing caught a misspelt function
  name in a branch no test reaches. TypeScript's checker now reads them as one program (`npm run check`, after `npm ci`),
  in every CI run; it found no misspelt names, and a dozen spots now say their types where the checker could not tell.
  TypeScript is the one development tool in `package.json`, pinned in `package-lock.json`; the Lab itself needs no Node.

## 0.2.0 — 2026-11-10

Every model now installs from its card on the Models page, and each card has a GPU/CPU switch. The models that run on a
graphics card get AMD's ROCm build on an AMD Radeon as well as NVIDIA's CUDA build. Voice conversion takes recordings from
this computer, and `update.bat` names every model whose packages or files an update changes. A 0.1.0 folder updates with
`update.bat`.

### Models & features

- **A GPU/CPU switch on every card.** On a computer whose graphics card the service uses, each model's card has a **Runs on
  GPU | CPU** switch. The choice is kept per model in `data/engines/devices.json` and applies from the model's next load: a
  loaded model is unloaded when it is switched. A model that is not installed yet installs the build its switch shows, so
  **Install** puts in the NVIDIA build when GPU is chosen and the card can run it. A model installed with the CPU build of
  PyTorch runs on the CPU until its GPU build is installed, and its card then offers **Install GPU build**, which replaces
  PyTorch in its environment and nothing else. GPU is greyed out, with the reason under it, where it cannot work: Phonon and
  Drama 3 run on the CPU, and a card or driver the build cannot run on is named. A service that runs everything on the CPU
  shows no switches and says so once. Each card's tests and its state line are those of the device it runs on now. New route:
  `POST /api/engines/{engine_id}/device`; `POST /api/engines/{engine_id}/install` takes `nvidia`.
- **The install window.** **Install** opens the installer's plan in a window over the page, and **Install now** follows the
  install there, with its output, until it finishes; **Hide** leaves it running and **Show progress** on the card opens it
  again. The cards no longer widen or grow while a plan is open or an install runs, so the page stays where it is: a card's
  text stays the same for the whole install, and its buttons sit at its foot.
- **Chatterbox on RTX 50 cards.** `setup-chatterbox.bat --nvidia` now installs the CUDA 13.0 builds of PyTorch 2.12 (the same
  file as Kokoro's NVIDIA build) and torchaudio 2.11 instead of PyTorch 2.6 with CUDA 12.6, which has no code for RTX 50 series
  cards. Chatterbox Turbo was checked to load, speak and clone a voice with PyTorch 2.12 and torchaudio 2.11 (their CPU builds).
  The build needs a GeForce GTX 16 or RTX 20 series card or newer and an NVIDIA driver of version 580 or later, as Kokoro's
  does, so `setup.bat` now gives both models their NVIDIA builds on an RTX 50 card. The CPU build is unchanged. Neither NVIDIA
  build has run on an NVIDIA card yet.
- **Every model installs from its card.** The nine models that had no **Install** button have one now, and a launcher in the
  lab folder; each installer prints its plan and asks first, pins every package and file by its SHA-256, continues an
  interrupted download, and ends with a speech check.
  - **Chatterbox 500M and Voice Conversion:** `setup-chatterbox.bat --original` adds their 3.2 GB checkpoint to Chatterbox
    Turbo's environment, for the CPU or, with `--gpu`, a graphics card. On one 8-core CPU the 500M loaded in 41 s and spoke
    1.9 s of audio in 16.5 s.
  - **The four Qwen3-TTS models:** `setup-qwen.bat --model <model>` installs 66 packages into `.venv-fqwen`, which the four
    share, and one model's checkpoint. The Base models' check clones the clip Kokoro's installer makes.
  - **VoxCPM2:** `setup-voxcpm.bat` makes `.venv-voxcpm` with 62 packages and fetches the checkpoint and the 25 source files
    of VoxCPM its inference imports, at a pinned commit. Its worker no longer needs the Qwen environment's packages; a
    `.venv-voxcpm` without PyTorch still borrows them.
  - **Breeze TTS 2:** `setup-breeze.bat` installs 41 packages and the 7.7 GB BF16 checkpoint. Its plan says first that the
    weights are for research and non-commercial use only.
  - **Drama 3:** `setup-drama3.bat` replaces running `tools/install_drama3.py` by hand. Its plan states the Fish Audio
    Community License first, it refuses a computer with less than 26 GB of RAM, it fetches the 20.8 GB model in 32 MB pieces
    six at a time, and it ends with a speech check on the CPU (on one 8-core CPU: loaded in 28 s, 2.3 s of audio in 9 s).

  The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 run only on a graphics card. On a computer with an NVIDIA driver they get
  the CUDA 13.0 builds of PyTorch 2.12 and torchaudio 2.11 that Chatterbox's NVIDIA build uses, and on one with an AMD Radeon
  AMD's ROCm build (below). On a computer whose card or driver neither build can run on, their card has no **Install** and
  says why. None of the three has run on an NVIDIA card yet.
- **The graphics-card models install on an AMD Radeon too.** On a computer without an NVIDIA driver whose graphics card is an
  AMD Radeon, **Install** on the cards of the four Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 (and their launchers) puts in
  AMD's ROCm 7.14 builds of PyTorch 2.12 and torchaudio 2.11 from AMD's own index, in place of the CUDA builds, with the ROCm
  runtime and the code for the card's chip; before, the environment had to be made by hand and the launcher run with
  `--checkpoint-only`. The installer asks AMD's OpenCL driver which chip the card has, and
  [requirements/amd-rocm.txt](requirements/amd-rocm.txt) pins every file by its SHA-256 for 20 chips: the Radeon RX 5000,
  6000, 7000 and 9000 series, the Radeon PRO and AI PRO cards built on them, and the Radeon graphics in Ryzen processors. On a
  computer with two AMD cards, such as a Ryzen processor's Radeon graphics beside a Radeon card, the Lab uses the separate card,
  and the installer writes the number PyTorch gives it to `runtime.local.json` (`hip_visible_devices`), which the models'
  workers read before PyTorch starts. The service now uses the graphics card on such a computer without being told. Checked on
  an AMD mini PC with a Radeon AI PRO R9700 beside the Ryzen 7 PRO 8845HS's Radeon 780M: every installer chose the R9700 and
  passed its speech check there (Qwen3-TTS 1.7B VoiceDesign spoke 1.9 s in 1.3 s, VoxCPM2 4.6 s in 36 s on its first take,
  Breeze 3.0 s in 14.5 s), and Qwen3-TTS 1.7B CustomVoice installed from its card and passed **Test** on the R9700.
- **Kokoro and Chatterbox run on an AMD Radeon too.** Their GPU build is now the one for this computer's card. On a computer
  without an NVIDIA driver whose card is an AMD Radeon, **Install** or **Install GPU build** with the switch on GPU, the question
  `setup.bat` asks, and the new `--gpu` of `setup-kokoro.bat` and `setup-chatterbox.bat` put in AMD's ROCm builds of PyTorch
  2.12 (and torchaudio 2.11 for Chatterbox) in place of the CPU builds, with the ROCm runtime and the code for the card's chip.
  In an environment that holds the CPU build only those packages change. `--nvidia` still asks for NVIDIA's build alone, and
  the Models page now asks every installer for its GPU build with `--gpu`. On AMD's build, Kokoro, the Chatterbox models and
  Voice Conversion leave AMD's MIOpen library off: it tuned its kernels for every new length of sentence, which took Kokoro 11
  to 53 s a sentence on the R9700, against 0.6 to 0.9 s without it. Checked on the R9700: Kokoro installed from its card in
  377 s, Chatterbox Turbo switched from its CPU build in 174 s and cloned a voice in its speech check (2.5 s of audio in 2.0 s),
  Chatterbox 500M and Voice Conversion passed theirs, and **Test** passed for Kokoro (2.7 s in 0.5 s), Turbo (2.1 s in 1.8 s)
  and 500M (2.6 s in 6.6 s).
- **Voice conversion takes files from this computer.** Under each of its two lists, a picker takes a WAV, FLAC, OGG, MP3
  or M4A file (read as Cast imports read them, M4A through FFmpeg) and chooses it in the list under its own name: a source
  performance of up to 60 seconds, or a target voice of up to 10, which needs the permission tick, origin and allowed use
  that Voice cloning asks for. The files are kept as mono WAVs named by their SHA-256 in `out\conversions\uploads`, and each
  conversion's manifest records their original names and the target's terms. New route: `POST /api/conversion/uploads`.
- **update.bat knows every model.** After an update it now names the pinned packages and files of all eleven installable
  models, not only Kokoro's, Phonon's and Chatterbox Turbo's. An environment is named once, with the command of each model
  installed in it (`--gpu` for a GPU build of Kokoro or Chatterbox, where it used to say `--nvidia`), and only for a list its
  build of PyTorch came from: a change to `requirements/amd-rocm.txt` names only AMD installs. A model counts as installed
  when its checkpoint is on disk at any revision, and its files are named only when its installer would fetch some; Drama 3
  when its receipt differs from its installer's pins.
- **MIOpen off for Qwen3-TTS and Breeze TTS 2 on AMD's build too.** Measured on the R9700 with each model's Lab worker:
  Qwen3-TTS's first take after a load took 18 to 25 s with MIOpen and 3 s without it, and its later takes were 15 to 30%
  slower with it even once its kernels were cached; Breeze took 35 to 150% longer over each take of a new length with MIOpen
  and allocated the same graphics memory (9.5 GB at most for a 29 s take). Breeze now loads in about 30 s instead of 45 to 60,
  and its **Test** spoke the check sentence, 3.0 s, in 9.3 s, where its install check had taken 14.5. VoxCPM2 already ran
  without MIOpen.
- **Graphics memory checked before a download.** The installers now read each NVIDIA card's memory from `nvidia-smi`, and
  VoxCPM2's and Breeze's refuse a card with less than 7 and 10 GB before they download anything. Breeze used up to 7.5 GB, so
  an 8 GB card is too small for it.
- **VoxCPM2 takes that run on.** VoxCPM2 sometimes keeps going after its text ends, up to a length cap of about a second a
  text token, and the voice description in front of the text raised that cap: the Models page's check sentence with seed 42
  ran on to 20.8 s on the R9700. The Lab now caps a take by the spoken text alone and makes a take that reaches the cap again
  from a seed derived from the requested one, up to three tries, rather than from the next seed, which Perform gives its next
  take. The take's generation details record the seed its audio came from and every try; when all three run on, the
  generation fails instead of saving a take that ran on. Checked on the R9700: seed 42 ran on in its first try, stopped at
  11.2 s, and gave a 4.6 s take at its second; seeds 7 and 1234 were unchanged.

### Checked

- In this release's checkout, with an environment built from `requirements/core.txt` and no model installed, CI's steps
  pass: `ruff check .`, `ruff format --check .`, 1660 Python tests (the 17 skipped need model packages and files, FFmpeg
  encoders or link privileges that environment lacks), 203 Node tests, `tools/check_repo.py` and
  `tools/build_api_docs.py --check`.
- The AMD builds were checked on one AMD mini PC with a Radeon AI PRO R9700, as each item above says. The NVIDIA builds of
  every model ran only against stand-ins for `nvidia-smi` and PyTorch: no NVIDIA card was available.
- `update.bat` from 0.1.0 to this release ran only in tests, against stand-in GitHub answers and downloads, because the
  release did not exist yet.

## 0.1.0 — 2026-11-04

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
  codec, length and label with ffprobe.
- **Labelled as synthetic.** Every audio file the Lab makes says inside it that it is AI-generated speech: takes, edited
  takes, `/api/speak` answers, conversions, scene mixes, Drama takes, blind-listening copies and every file of an export, in
  each of the five formats. The comment tag names the model and the Lab and carries the IPTC digital source type for media made
  by a trained model, and the Lab is the file's software (`synthetic_label.py`). Reference recordings, people's own voices,
  are never labelled. A tag can be stripped, so it is a label and not a watermark; Chatterbox's own Perth watermark is
  unchanged.
- **Word times and fit.** `align_takes.py` aligns Cast takes that have no word times, in one batch, with the local
  faster-whisper tiny model (MIT code and weights; nothing is downloaded); see [docs/ALIGNMENT.md](docs/ALIGNMENT.md).
  Captions are written as word phrases wherever every take of a line has word times, and as one caption per line otherwise.
  Boundary accuracy is not measured, so treat word times as approximate. `fit.py` is a bounded time-stretch (15 % either way
  by default) whose status (`within_slot`, `stretched`, `needs_rerender`) the export records against each line's target
  duration; the export does not stretch the audio.
- **Lip-sync tracks.** The export writes one mouth-shape track per line that has phoneme times, at
  `lipsync/<character>/<line>.json` ([docs/LIPSYNC.md](docs/LIPSYNC.md)). Kokoro takes store the model's own predicted
  phoneme times and the export prefers them, in any language Kokoro speaks. Other English takes share their aligned words
  over estimated phoneme times with misaki's English rules, which the export reads from Kokoro's environment in one call;
  other languages get a stated reason instead of a track. Against
  Kokoro's own predicted timing on 40 sentences, the median word-start error is 35 ms and the median phoneme-start error is
  39 ms (12.5 ms with exact word times, as a control). That shows agreement with Kokoro's prediction, not accuracy against
  real speech, and the mouth-shape mapping has not been checked against a face or by ear.

### Models

- **Integrated families:** Kokoro, Phonon 31M English, Chatterbox Original/Turbo/VC, Breeze TTS 2, Qwen3-TTS
  VoiceDesign/Base/CustomVoice, VoxCPM2 and Fish Audio Drama 3. The catalog records each pin; a catalog entry means an
  integration exists, not that the package, model or hardware is present.
- **One environment per model.** Every model runs in a Python environment of its own, and the service's `.venv` holds only the
  core packages, which no installer changes: Kokoro in `.venv-kokoro`, Phonon in `.venv-phonon` and the three Chatterbox
  engines in `.venv-chatterbox`, beside the Breeze, Qwen and VoxCPM2 environments that were already separate. Each pinned list
  repeats the core packages at the same versions. Installing one model no longer marks another model's test out of date, and
  no two models can share a PyTorch. A Kokoro or Phonon installed into `.venv` by an earlier build is not found any more: run
  its installer again, which keeps the model files already in `hf-cache`. The installers share one set of helpers for
  making and reading an environment.
- **Kokoro install.** `setup-kokoro.bat`, or `tools/install_kokoro.py`, installs Kokoro for the CPU into `.venv-kokoro`: 96 pinned
  packages with the CPU build of PyTorch, each file checked against a SHA-256 in `requirements/kokoro-cpu.txt`; the pinned
  checkpoint and 41 voices from Hugging Face, resumable and checked against the SHA-256 in `catalog/kokoro_snapshot.json`; then a
  speech check that loads the model without a network and also makes `reference/af_heart.wav`, the synthetic clip the cloning
  models' tests clone. About 0.6 GB to download and 1.7 GB on disk. It shows its plan and asks first, runs only with the lab's
  own `.venv`, and refuses to replace a package that is installed at another version (`--checkpoint-only` adds just the model
  files to a `.venv-kokoro` you made yourself). Run again, it installs only what is missing. A studio page that was open during
  the install reads the new voices when you press Generate, so it needs no reload. The phoneme preview and the lip-sync
  tracks of aligned takes reach Kokoro's English rules in `.venv-kokoro` (the preview through Kokoro's worker, the exporter in
  one call for a whole package), and the Kokoro worker makes the default reference clip when it loads and the clip is missing.
- **Phonon.** Phonon 31M English (Gradium) speaks English in 37 preset voices, 12 American, 13 British and 12 characters, on the
  CPU through the numpy-only `ptts` runtime, with no PyTorch. It takes a seed, a temperature and a pace shift (`duration_delta`,
  -0.5 to 0.5), and cannot clone, design a voice or take a direction. Its voices are reusable Cast identities, so Perform lines,
  scenes and batches speak them. `setup-phonon.bat`, or `tools/install_phonon.py`, installs the 8 packages pinned in
  `requirements/phonon.txt` (`ptts` and the core packages) into `.venv-phonon`, and the pinned model and voice files from Hugging
  Face, each checked against a SHA-256 (`catalog/phonon_snapshot.json`), then runs a speech check without a network: about
  0.14 GB to download and 0.3 GB on disk. It follows the Kokoro installer's rules (it shows its plan and asks first, runs only
  with the lab's own `.venv`, refuses to replace a package at another version, and has `--checkpoint-only`), and the
  **Install** button on its card runs it. The weights are Apache-2.0, but the repository that holds the 37 voices states no
  licence, so the installer's plan, the page and the docs say to ask Gradium before you publish audio made with one.
- **Chatterbox Turbo install.** `setup-chatterbox.bat`, or `tools/install_chatterbox.py`, installs Chatterbox Turbo 350M, the
  model that clones a voice from a recording of more than five seconds, for the CPU. It makes an environment of its own,
  `.venv-chatterbox`, from the lab's Python 3.12, as every model has: Chatterbox needs PyTorch 2.6, and Kokoro has PyTorch 2.12.
  There it installs 73 packages pinned by SHA-256 in `requirements/chatterbox-cpu.txt` (without the Gradio demos and
  the multilingual text packages Turbo never imports), fetches the ten files of the pinned Turbo snapshot that Turbo loads
  (resumable, each checked against `catalog/chatterbox_turbo_snapshot.json`), and checks without a network that Turbo speaks in
  its built-in voice and clones that recording: about 3.4 GB to download and 5 GB on disk. It follows the Kokoro installer's
  rules (it shows its plan and asks first, runs only with the lab's own `.venv`, refuses to replace a package at another
  version, and has `--checkpoint-only` for a `.venv-chatterbox` you built yourself), and the **Install** button on the Turbo
  card runs it. The code and the weights are MIT, and every clip carries Resemble AI's Perth
  watermark. Turbo needs 8 GB of free RAM to load: on one 8-core CPU it peaked at 6.1 GB while loading and held 5.0 GB after a
  line of 1,200 characters.
- **Chatterbox's environment.** Chatterbox Turbo, Original and VC now run in `.venv-chatterbox`, not `.venv`. Turbo loads the
  files of its pinned snapshot directly, so it never asks the Hugging Face Hub, and without `reference/af_heart.wav` its worker
  warms up and its Models page test speak in the built-in voice instead of reaching for Kokoro, which that environment does not
  have. A call that clones without a `reference_path` while that clip is missing is refused at once, with what to do. A
  Chatterbox set up by hand in `.venv` is not found any more: run the installer, or build `.venv-chatterbox` with your own
  packages and use `--checkpoint-only`.
- **NVIDIA graphics cards (not yet tried on one).** `setup-kokoro.bat --nvidia` installs the CUDA 13.0 build of PyTorch 2.12
  (GeForce GTX 16 and RTX 20 series or newer, RTX 50 included, with a driver of version 580 or later), and
  `setup-chatterbox.bat --nvidia` the CUDA 12.6 builds of PyTorch 2.6 and torchaudio (up to the RTX 40 series), each pinned by
  SHA-256 in `requirements/kokoro-nvidia.txt` and `requirements/chatterbox-nvidia.txt` and checked by a speech check on the
  card. Before anything is downloaded, each reads the card and the driver with NVIDIA's `nvidia-smi` and refuses a build that
  cannot run there: no NVIDIA driver, a card older than the GTX 16 series or a driver older than 580 for Kokoro, an RTX 50
  series card for Chatterbox. `setup.bat` offers them when it finds a driver, asks once, and gives each model the build that
  runs on the card: on an RTX 50 card, Kokoro's NVIDIA build and Chatterbox's CPU build. The service asks its workers for the
  card when an NVIDIA driver is installed (or `"device"` in `runtime.local.json`, or `--device cuda`, says so), and a worker
  whose PyTorch cannot use the card runs on the CPU instead of failing: a CPU build, or a CUDA build with no code for the card,
  which PyTorch itself reports as available. Its card on Models & features says it ran on the CPU. A hardware report template
  for GitHub asks for the Lab's copied report, which now names the processor, the graphics cards, total RAM and how fast each
  model's test ran on which device.
- **Models & features** reports three states for each family: integrated, installed (its environment and pinned checkpoint
  files are on disk) and usable (a test on this device passed). **Test** loads the family on the service device,
  speaks one short sentence and reports the time it took; missing files are reported without loading anything, and a
  missing Kokoro points to Install on its card and to `setup-kokoro.bat`, as does the error of speaking before it is
  installed. A model without a guided installer that is missing files says so, and points to the setup guide. Results are
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
  Kokoro asks for 3 GB: one worker held 1.2 GB once loaded and peaked at 1.8 GB on a line of about 1,000 characters. The
  numbers live in one table, `ram_needs.py`, and `min_free_ram_gb` in `runtime.local.json` changes them for one computer.
- **First run.** A studio with no model installed says so at the top of Model studio and links to Models & features. The
  model pickers in Model studio and Voice cloning mark each model that is not installed, and Model studio says what to do
  about the one you picked. Voice cloning starts on a cloning model this computer has installed, or else on one the Lab can
  install for you, keeps one you chose, and says when the chosen one is not installed. On Models & features the models the
  app cannot install yet are folded under "More models", with a count, and the glossary sits under "What these words mean".
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

- **One setup.** `setup.bat` sets up the core, then the desktop window, then the speech models you choose: Kokoro and Phonon
  (recommended), those and Chatterbox Turbo, or none. It prints every chosen installer's plan, asks once, and runs them one
  after another; a model already installed is left alone, and running it again after an interruption continues. Typed after
  it, `--models`, `--yes`, `--nvidia` and `--no-desktop` answer the questions (`tools/first_setup.py`). `setup-kokoro.bat`,
  `setup-phonon.bat`, `setup-chatterbox.bat` and `setup-desktop.bat` stay for doing one part later.
- **Updates.** **Check for updates** on System & backups asks GitHub for the newest release and says whether it is newer, with
  a link to its notes; the request carries nothing about the person or the library. It asks by itself only when **Check when the
  Lab opens** is ticked, at most once a day, and then shows a newer version in the page header too. `update.bat` updates the
  folder with the Lab closed: it shows the plan and asks first (`--check`, `--dry-run`, `--yes`). A ZIP install gets the
  release's files from GitHub, keeps a copy of every file it replaces or removes, puts them back if a write fails, and never
  touches the library, the models, the `.venv` environments, `runtime.local.json` or `out\`. A Git checkout moves to the
  release's tag as a fast-forward. `update.bat --undo` goes back to the version before. The new version then updates the core
  and desktop packages when their lists changed, and names any installed model whose pinned packages or files changed
  (`updates.py`, `tools/update.py`; [Updating the Lab](docs/SETUP.md#updating-the-lab)).
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
- **Local only, from a browser too.** Another website's page may open the Lab's pages in a tab and nothing more: any other
  request it makes is refused by its `Sec-Fetch-Site`, including the plain GETs, such as an `<audio>`, that carry no `Origin`.
  A path in a request is checked against the Lab's folders before Windows opens it, so `/api/reference-audio` and `/out/`
  never reach for a network share. Every answer says that another site may not frame it (`X-Frame-Options`,
  `frame-ancestors`) or read it as another type (`nosniff`).
- **Every request is answered.** An error a route does not handle comes back as a `500` that names it, and a Drama job that
  does not exist as a `404`, instead of a closed connection that a page can only report as "Failed to fetch".
- **Core install.** The core install works without `librosa` and `misaki`; both are imported only where they are used.
  `setup.bat` checks that Python 3.12 starts, and gives the link to the 3.12.10 installer when it does not. A setup that was
  interrupted is finished by running it again; an existing `.venv` that cannot load the core says what to do instead of
  showing a traceback, and is still never changed. Setting up inside a folder OneDrive syncs gets a warning first.
- **Version.** The one version number lives in `app_version.py`. The page footer, `/api/health`, the service log and
  `serve.py --version` all read it.

### For scripts and other programs

- **HTTP API.** [docs/API.md](docs/API.md) and `openapi.json` (OpenAPI 3.1) document 87 routes; 34 more serve the Lab's own
  pages and are internal and may change. Both are generated from one table (`api_spec.py`) by `tools/build_api_docs.py`, and
  the service's dispatch is built from the same table (`routes.py`): `serve.py` registers one function for each row, and will
  not start with a row nobody answers or a function with no row. Errors a handler does not answer itself are answered the same
  way on every route. `POST /api/speak` is a blocking call that answers one finished WAV;
  `POST /api/performance` speaks a line as a Cast voice, saves it as a take and can be repeated safely with an
  `idempotency_key`. A running service answers `GET /api/openapi.json`. A request refused for its size, or a small one
  refused as coming from another website's page, is read through first, so a client that is still sending gets its `413`
  or `403`.
- **Command line.** `tools/lab_cli.py` calls a running service from a terminal or a script: `health`, `speak`, `perform` and
  `cast`. It needs only the Python standard library. Its exit status is 0 when the call worked, 1 when the service refused it
  or failed, 2 for a mistake in the command line and 3 when nothing answered.

### Checked

- In a clean checkout, with an environment built from `requirements/core.txt`, CI's steps pass: `ruff check .`,
  `ruff format --check .`, 1432 Python tests (the skipped ones need model packages and files, FFmpeg encoders or link
  privileges that environment lacks), 195 Node tests and `tools/check_repo.py`. A full test run no longer leaves a desktop
  session or a service log in the real `out/` folder, nor sweeps it: the desktop test keeps both in a temporary folder and
  skips the startup sweep. The worker tests still pass audio through `out/workers`, the one place a worker may write, and
  remove what they put there.
- With an environment for each model, Kokoro and Phonon were installed from a fresh copy that had only the core (Windows 11,
  CPU only, pip's cache warm): Kokoro's installer made `.venv-kokoro` and installed 96 packages in 309 seconds, spoke its check
  sentence and made the default reference clip; Phonon's made `.venv-phonon` in 67 seconds. The studio started from that copy
  passed **Test** for both (real-time factors of 0.28 and 0.17), spoke through `/api/speak`, and showed phonemes through
  Kokoro's worker, refusing an empty text, an unknown language and a missing language package with a `400` before anything
  loaded. `setup.bat --models recommended --yes --no-desktop` then found both installed and left them alone, and with
  `--models all` it printed Chatterbox's real plan and asked before installing anything.
- The label survives in every export format: real WAV, FLAC, Ogg, MP3 and AAC files written by FFmpeg 8.1.1 carry it, and
  ffprobe reads it back. The demo in the README was spoken by this build through `/api/speak`.
- Updates, in a copy of this build made like a ZIP install: **Check for updates** and `update.bat --check` asked GitHub's real
  API and said that no release is published yet (the repository was still private then). `update.bat` refused to run while
  that copy's service was running and named its process, and it passes the updater's exit code on (0, 1, 2 and 130 were
  tried). The page's header showed a newer release, from a stand-in answer, at phone width. Updating from a
  ZIP and from a Git checkout, `--undo`, and putting files back after a write fails ran only in tests, against stand-in
  GitHub answers and downloads and Git repositories the tests made.
- The NVIDIA card checks (which build runs on which card, the driver version, and the worker falling back to the CPU) ran
  only against stand-ins for `nvidia-smi` and PyTorch: no NVIDIA card was available.
- The browser flow test drives the page in headless Microsoft Edge against stand-in engines: it clones a voice, performs a
  line, edits a take, edits and renders a scene, exports and verifies a package, and makes, checks and restores a backup.
  It also takes the screenshots in the README.
- Real packages were written with FFmpeg 8.1.1 and read back by the verifier, and a real MP3 and a real M4A were imported.
- The Kokoro installer was run from an empty folder (Python 3.12.10, Windows 11, CPU only), when it still installed into `.venv`.
  After `setup_core.py` it installed its packages and the checkpoint with its 41 voices in 296 seconds with pip's cache warm, and
  the speech check passed. A speech recognizer read
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

- **Platform.** Windows 10/11 only. Built and checked on one machine with an AMD Radeon AI PRO R9700. The `--nvidia` builds
  are pinned and checked by hash but have not been run on an NVIDIA card, and there are no Linux or macOS results. Only Kokoro,
  Phonon, Chatterbox Turbo and Drama 3 have installers, and VoxCPM2 needs pinned source and a ROCm overlay that this repository
  does not install. Nothing here trains or fine-tunes a model.
- **Updates.** This is the first release, so there was nothing to update from: `update.bat` has only run against stand-ins,
  GitHub's answers and downloads and Git repositories made by its tests. The first real update will be the one from this
  version. It trusts
  GitHub and this repository as the first download did: it takes files only from github.com over HTTPS and checks that a
  release's `app_version.py` matches its tag, but releases are not signed.
- **The synthetic label** is a tag in the file's metadata, which an editor or a converter can drop. Takes, packages and
  clips made before it existed carry none, and a package exported before it is verified without one.
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
- **Chatterbox Turbo.** On the CPU it is slow: two and a half to three times as long as the speech it makes on one 8-core
  CPU. Its `--nvidia` build has not been tried on an NVIDIA card, and does not support RTX 50 series cards, because the PyTorch
  2.6 that `chatterbox-tts` 0.1.7 asks for has no CUDA build new enough. It speaks English, and given 1,200 characters it stopped after 30 to 36 seconds of audio,
  so keep lines to a few sentences. Chatterbox Original and VC share its environment, but no installer fetches their
  checkpoint. The installer was run on one Windows 11 computer; a missing Visual C++ runtime, a folder path near the
  259-character limit and a network that blocks PyPI or Hugging Face were not tried for real, and its tests cover them with
  stand-ins.
- **Install button and RAM figures.** Only the Kokoro, Phonon and Chatterbox Turbo cards have an Install button, and they have
  no Stop button. Their progress is the installer's own output, so a quiet step, such as pip unpacking PyTorch, can show
  nothing new for minutes. The RAM on a card is the Lab's own rule for loading a model. Only Kokoro's, Phonon's and Chatterbox
  Turbo's come from a measured peak; the others are not a measurement of what the model uses.
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
