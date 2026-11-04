# Setup

This is an early Windows release that runs from source, not a bundled
installer with every model. Python 3.12 x64 is the supported starting point.
`setup.bat` sets up the core, the desktop window and the speech models you choose, in one go. Three models have guided
installers, [Kokoro](#kokoro-on-the-cpu), [Phonon](#phonon-on-the-cpu) and [Chatterbox Turbo](#chatterbox-turbo-on-the-cpu),
and all three speak on an ordinary CPU; Chatterbox Turbo is the one that clones a voice from a recording. Each has a launcher
of its own too (`setup-kokoro.bat`, `setup-phonon.bat`, `setup-chatterbox.bat`) for adding it later. A core installation
without a model opens the studio and its stores, but cannot speak until one is installed.

Every model runs in a Python environment of its own (`.venv-kokoro`, `.venv-phonon`, `.venv-chatterbox` and so on), apart from
the service's `.venv`, so no installer can change the packages another model or the service depends on.
[Troubleshooting](TROUBLESHOOTING.md) has a table of what runs on an 8, 16 or 32 GB computer.

## Core and desktop

Download the newest release's ZIP from the repository's Releases page on GitHub and extract it, or clone the repository, into a
writable folder that OneDrive does not sync, such as `C:\voice-lab`. Either way, `update.bat` brings it up to date later
([Updating the Lab](#updating-the-lab)). Windows often syncs the
Desktop and Documents folders to OneDrive, which would upload the gigabytes of packages and
models the Lab installs and can lock its databases while it syncs them; `setup.bat` warns
when the folder is inside OneDrive.

Install Python 3.12 (64-bit) with its launcher. The newer Python that python.org offers
first does not work with the Lab yet: 3.12.10 is the last 3.12 release with a Windows
installer ([python-3.12.10-amd64.exe](https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe)),
and `setup.bat` gives that link when `py -3.12` cannot start.

Run `setup.bat`. It does three things, in order:

1. **The core**, `.venv`: the service's own packages from [requirements/core.txt](../requirements/core.txt).
2. **The desktop window**, `.venv-desktop`: pywebview, and the **Fel's Voice Lab** shortcut on your Desktop. It uses the installed
   [Microsoft WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/). If WebView2 is missing, the window cannot
   open: install it from Microsoft and start the Lab again. The app does not silently change your browser or download a runtime.
   `setup-desktop.bat` does this step on its own.
3. **The speech models you choose.** It asks: Kokoro and Phonon (recommended: about 0.8 GB to download and 2 GB on disk), those
   and Chatterbox Turbo (about 4 GB to download and 7 GB on disk; Turbo wants a computer with 16 GB of RAM or more), or none for
   now. It prints the plan of every model it is about to install, the installers' own `--dry-run`, asks once, and then runs
   them one after another. On a computer with an NVIDIA driver it also offers the [GPU builds](#nvidia-graphics-cards).

Typed after `setup.bat`, `--models recommended` (or `all`, `none`, or names such as `kokoro chatterbox`) chooses without the
question, `--yes` accepts the plans, `--nvidia` takes the GPU builds and `--no-desktop` skips the window; `--help` lists them. A
model that is already installed is left as it is.

If `setup.bat` is interrupted, run it again: it finishes the job. An existing `.venv` that it did not leave unfinished is checked
and preserved rather than repaired or upgraded automatically. If its import check fails, inspect the error and restore its
requirements in a separate environment, or, when nothing else has been installed into it, delete `.venv` and run `setup.bat`
again. Never replace working AMD/model pins by running a generic `pip install --upgrade`.

The shortcut starts a dedicated window and hidden backend on a free loopback
port. Closing that window cancels its work and stops its backend/children.
Closing just an inner studio panel does not close the app. Another instance
started through a terminal or by another program remains independent.
Logs are under `out/desktop/session-*/backend.log` and `out/desktop/launcher.log`.
No model preloads; the normal idle unload timeout remains ten minutes.

The shortcut is named **Fel's Voice Lab**. Rerunning desktop setup renames the
old **Voice Lab** shortcut only when it belongs to this installation. Project
folder names and existing voice libraries stay at their current paths.

Choose Light, Dark, Forest or System in the header. Appearance is saved under
`data/ui/preferences.json` inside the selected `--state-root` (the project folder
by default), rather than being tied to a browser port. This local file is not part of
the repository. Separate state roots keep separate theme preferences.

For browser/API mode:

```powershell
.venv\Scripts\python.exe serve.py --port 8768 --device cpu
```

Open `http://127.0.0.1:8768/`. The default CLI port remains 8765 for compatibility;
choose a free port explicitly if another app uses it. In browser mode the
terminal controls the service lifetime; closing a browser tab does not stop it.
`serve.bat` is the foreground fallback.

## Kokoro on the CPU

Kokoro is the quickest way to hear the studio speak: a small model that runs on an ordinary computer. `setup.bat` offers it; to add
it later, run

```powershell
setup-kokoro.bat
```

or `.venv\Scripts\python.exe tools\install_kokoro.py`. It prints its plan and asks before it changes anything.
`--dry-run` shows the plan and runs the checks without changing anything, `--yes` accepts the plan without asking, and `--nvidia`
takes the [GPU build](#nvidia-graphics-cards) of PyTorch.

The studio can run the same installer for you. Until it has run, the Kokoro card on the **Models & features** page reads
*Not installed*, lists the files that are missing and has an **Install** button. **Install** shows the installer's own plan,
the text `--dry-run` prints, with the reasons it would refuse if this computer cannot take it. **Install now** then runs the
installer, and the card shows the end of its output. While it runs nothing else loads a model or speaks, and a page that is
reloaded shows the progress again. There is no Stop button: closing the studio stops the install, and running it again
continues where it stopped. The whole output of each run is kept in `out\install`. When it ends the card reads *Installed,
not tested*; press **Test**. A studio with no model installed says so at the top of **Model studio** and links to that page, and
its model picker marks the models that are not installed. Speaking before the install has run fails with an error that begins by
saying Kokoro is not installed and where to install it; once it has run, **Generate** reads the new voices by itself, so a studio
page that was already open needs no reload.

Kokoro gets a Python environment of its own, `.venv-kokoro`, made from the same Python 3.12 that `setup.bat` used. It installs
into this folder, in `.venv-kokoro`, `hf-cache` and `out\kokoro`:

1. **96 pinned Python packages** from [requirements/kokoro-cpu.txt](../requirements/kokoro-cpu.txt), among them the CPU build of
   PyTorch, Transformers and spaCy, and the core's own NumPy, SciPy and SoundFile at the same versions: 275 MB to download and
   about 1.3 GB installed. pip checks every file against the SHA-256 in that list. The versions are the ones Kokoro was checked
   with on the development machine, except PyTorch, whose CPU build comes from PyPI where the development machine uses a ROCm
   build. pip also keeps its downloads in its usual cache in your user profile.
2. **The Kokoro checkpoint** at the pinned revision of `hexgrad/Kokoro-82M` on Hugging Face, with its model card and 41 voices
   (about 350 MB). Every file is checked against the SHA-256 in [catalog/kokoro_snapshot.json](../catalog/kokoro_snapshot.json).
   An interrupted download resumes where it stopped, and a file that fails its check is deleted rather than kept.
3. **A speech check.** A fresh process in `.venv-kokoro` loads Kokoro without using the network and speaks the sentence the
   Models page uses. The clip stays in `out\kokoro\install-check.wav` beside a small receipt of what was installed. The same
   process makes `reference\af_heart.wav`, the synthetic clip the cloning models' tests clone, when it is not there yet.

Run it again to continue after an interruption or to repair a damaged file: it installs only what is missing. It stops before it
changes anything when it is not run with this folder's own Python, when a package it pins is already in `.venv-kokoro` at another
version, when this folder's path is too long for Windows' 259-character limit (move the lab to a path such as `C:\voice-lab`, or
switch on long paths in Windows), or when the disk is too full. If you made `.venv-kokoro` yourself (with a GPU build of PyTorch
of your own, say), `--checkpoint-only` fetches the model files and leaves its packages alone.

The phoneme preview on the studio page and the lip-sync tracks of aligned takes use Kokoro's English rules (misaki), which live
in `.venv-kokoro`; the Lab asks Kokoro's environment for them, so they work once Kokoro is installed.

`--voices` chooses the voices: `supported` (the default: the 41 voices that work with these packages, in American and British
English, Spanish, French, Hindi, Italian and Brazilian Portuguese), `english` (28), `all` (54), or voice IDs such as `bm_george`.
The 13 Japanese and Mandarin Chinese voices also need Misaki language packages that the installer does not add. More voices can
be added later with the same command.

What it needs here, measured on one computer with only the CPU: one Kokoro worker holds about 1.2 GB once loaded and peaked at
about 1.8 GB while speaking a line of about 1,000 characters (58 seconds of audio, made in 17 seconds). The Lab asks for 3 GB of
free RAM before it loads Kokoro; [`runtime.local.json`](#hardware-and-state-settings) can change that for one computer.

Licences: the Kokoro weights are Apache-2.0. Most of the packages are MIT, BSD or Apache. phonemizer-fork and the espeak-ng
library inside espeakng-loader are GPL-3.0, num2words is LGPL, and certifi and tqdm are MPL-2.0. They are installed on your
computer at your request and are not part of this repository.

To remove Kokoro, delete `.venv-kokoro`, `hf-cache\hub\models--hexgrad--Kokoro-82M` and `out\kokoro`.

If an earlier version of the Lab installed Kokoro into `.venv`, the Lab now looks for it in `.venv-kokoro`, and Models & features
says it is not installed until you run `setup-kokoro.bat` again: it keeps the model files already in `hf-cache` and fetches only
the packages. Kokoro's packages left in `.venv` (about 1 GB) do nothing there any more; to clear them, delete `.venv` and run
`setup.bat`, which makes it again with the core packages only.

The recipe was checked from an empty folder on one Windows 11 computer with Python 3.12.10, using only the CPU: the packages and
checkpoint installed, a speech recognizer read the check clip back as the check sentence, and the studio started from that folder
passed **Test**. With its own `.venv-kokoro`, it was checked again from a fresh copy that had only the core: the installer made
the environment and installed the 96 packages in 309 seconds (pip's cache was warm), the speech check spoke 2.7 s of audio in
0.8 s after a 12 s load and made the default reference clip, and the studio passed **Test** (a real-time factor of 0.28) and
showed phonemes through Kokoro's worker. If the speech check fails with a message about a missing DLL, install the
[Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) (x64) and run the installer again.
Other Windows setups have not been tried.

## Phonon on the CPU

Phonon 31M English is a very small speech model: its checkpoint is 65 MB, and it runs on an ordinary computer without a graphics card
and without PyTorch. It speaks English in 37 preset voices (12 American, 13 British and 12 characters, among them a demon, an ogre and
a snow queen) and takes a seed, a temperature and a pace shift. It cannot clone a recording, design a voice or take a direction.
`setup.bat` offers it; to add it later, run

```powershell
setup-phonon.bat
```

or `.venv\Scripts\python.exe tools\install_phonon.py`. It prints its plan and asks before it changes anything. `--dry-run` shows the
plan and runs the checks without changing anything, and `--yes` accepts the plan without asking. The **Install** button on the Phonon
card of **Models & features** runs the same installer, and works the way [Kokoro's does](#kokoro-on-the-cpu).

Phonon gets a Python environment of its own, `.venv-phonon`, made from the same Python 3.12 that `setup.bat` used, so installing it
changes no other model's packages and leaves their tests as they were. It installs into this folder, in `.venv-phonon`, `hf-cache`
and `out\phonon`:

1. **8 pinned Python packages** from [requirements/phonon.txt](../requirements/phonon.txt): the `ptts` runtime, a 7 MB wheel that
   needs only NumPy, and the core's own NumPy, SciPy and SoundFile at the same versions. There is no PyTorch. That is 60 MB to
   download and 240 MB on disk, and pip checks every wheel against the SHA-256 in that file.
2. **The Phonon files** at pinned revisions: the 65 MB model with its tokenizer, configuration and default voice from
   `gradium/phonon-31m-en`, and the 37 English preset voices with their catalog from `gradium/phonon-voices` (10 MB). That is 75 MB to
   download. Every file is checked against the SHA-256 in [catalog/phonon_snapshot.json](../catalog/phonon_snapshot.json). An
   interrupted download resumes where it stopped, and a file that fails its check is deleted rather than kept.
3. **A speech check.** A fresh process in `.venv-phonon` loads Phonon without using the network and speaks the sentence the Models
   page uses. The clip stays in `out\phonon\install-check.wav` beside a small receipt of what was installed.

Run it again to continue after an interruption or to repair a damaged file: it installs only what is missing. It stops before it
changes anything when it is not run with this folder's own Python, when a package it pins is already in `.venv-phonon` at another
version, when this folder's path is too long for Windows' 259-character limit (move the lab to a path such as `C:\voice-lab`, or
switch on long paths in Windows), or when the disk is too full. If you made `.venv-phonon` yourself, `--checkpoint-only` fetches
the model files and leaves its packages alone.

The studio's picker groups the voices as American English, British English and Characters, and **Cast library** can add any of them
as an identity. The larger Phonon models of the family (40M) are licensed CC BY-NC 4.0 and are not offered.

Licences: the model's weights, tokenizer and default voice are Apache-2.0 (its model card says so), and the `ptts` runtime is MIT OR
Apache-2.0. The 37 preset voices come from a separate Gradium repository that has no model card and no LICENSE file, so nothing says
what audio made with them may be used for. Ask Gradium before you publish a recording that uses one. The model card also asks that
Phonon not be used to impersonate a real person, and that listeners be told when speech is synthetic where that matters to them.

What it does here, measured on one computer with only the CPU: it loads in under half a second, and a clip takes about a fifth of its
own length to make (a real-time factor of 0.12 to 0.36 across the 37 voices). One worker holds about 150 MB when loaded and peaked at
about 510 MB while speaking a line of 1,200 characters; the Lab asks for 2 GB of free RAM before it loads it. A pace shift of -0.5 made
one 18-word sentence 22% shorter and +0.5 made it 19% longer.

A seed gives nearly the same clip, not the same file: two clips from one seed differ by about a millionth of full scale, and running
with one thread instead of all of them moved the end of a clip by one 80 ms frame. Do not expect an identical clip on another computer.

To remove Phonon, delete `.venv-phonon`, `hf-cache\hub\models--gradium--phonon-31m-en`, `hf-cache\hub\models--gradium--phonon-voices`
and `out\phonon`. If an earlier version installed `ptts` into `.venv`, run `setup-phonon.bat` again, which makes `.venv-phonon`
and keeps the model files; `ptts` left in `.venv` does nothing there.

The recipe was checked on one Windows 11 computer with Python 3.12.10, using only the CPU, from a copy of the Lab that had only the
core packages: **Install** on the Models page ran the installer in 30 seconds and the card read *Installed, not tested*; **Test**
passed (2.6 s of audio in 0.3 s); and all 37 voices spoke, each a different clip. With its own `.venv-phonon`, the installer ran
again from a fresh copy in 67 seconds and the studio started from it passed **Test** (a real-time factor of 0.17). If the speech check fails with a message about a
missing DLL, install the [Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) (x64) and run the
installer again. Other Windows setups have not been tried.

## Chatterbox Turbo on the CPU

Chatterbox Turbo 350M, from Resemble AI, clones a voice: give it a recording of more than five seconds and it speaks new English
lines in that voice. It also performs tags such as `[laugh]`, `[chuckle]` and `[cough]` written into the text. It runs on an
ordinary computer without a graphics card, though more slowly than it speaks (see below). `setup.bat` offers it; to add it later,
run

```powershell
setup-chatterbox.bat
```

or `.venv\Scripts\python.exe tools\install_chatterbox.py`. It prints its plan and asks before it changes anything. `--dry-run` shows
the plan and runs the checks without changing anything, `--yes` accepts the plan without asking, and `--nvidia` takes the
[GPU builds](#nvidia-graphics-cards) of PyTorch and torchaudio. The **Install** button on the Chatterbox Turbo card of
**Models & features** runs the same installer, and works the way [Kokoro's does](#kokoro-on-the-cpu).

Chatterbox gets a Python environment of its own, `.venv-chatterbox`, as every model does: it needs PyTorch 2.6, and Kokoro's
`.venv-kokoro` has PyTorch 2.12, so the two could not share one. The installer makes it from the same Python 3.12 that `setup.bat`
used, and installs into this folder, in `.venv-chatterbox`, `hf-cache` and `out\chatterbox`:

1. **73 pinned Python packages** from [requirements/chatterbox-cpu.txt](../requirements/chatterbox-cpu.txt), among them PyTorch 2.6
   for the CPU, Transformers, librosa and `chatterbox-tts` 0.1.7 itself: 400 MB to download and 2.0 GB installed. pip checks every
   file against the SHA-256 in that list. They are the versions pip resolved for `chatterbox-tts` 0.1.7 with the core's NumPy and
   SoundFile, less what Turbo never imports: the Gradio demo apps, the Japanese and Chinese text packages of Chatterbox's
   multilingual model, and a developer tool. setuptools stays at 81.0.0, because Chatterbox's watermarker imports `pkg_resources`,
   which later releases dropped. pip also keeps its downloads in its usual cache in your user profile.
2. **The Turbo checkpoint** at the pinned revision (749d1c1) of `ResembleAI/chatterbox-turbo` on Hugging Face: the 1.9 GB language
   model, the 1.1 GB speech decoder, the speaker encoder, the built-in voice, the tokenizer and the model card, 3.0 GB in all. The
   repository's ten-step decoder, another 1.1 GB that Turbo does not use, is left out. Every file is checked against the SHA-256 in
   [catalog/chatterbox_turbo_snapshot.json](../catalog/chatterbox_turbo_snapshot.json). An interrupted download resumes where it
   stopped, and a file that fails its check is deleted rather than kept.
3. **A speech check that clones.** A fresh process in `.venv-chatterbox` loads Turbo without using the network, speaks a paragraph
   in its built-in voice, clones that recording, and speaks the Models page's check sentence in the cloned voice. Both clips stay
   in `out\chatterbox` (`install-reference.wav` and `install-check.wav`) beside a small receipt of what was installed, which also
   records how the voice match scored the clone against its reference.

Run it again to continue after an interruption or to repair a damaged file: it installs only what is missing. It stops before it
changes anything when it is not run with this folder's own Python, when a package it pins is already in `.venv-chatterbox` at
another version, when this folder's path is too long for Windows' 259-character limit (move the lab to a path such as
`C:\voice-lab`, or switch on long paths in Windows), or when the disk is too full: a first install needs about 6 GB free. To run
Chatterbox on an NVIDIA card, use `--nvidia` (see [NVIDIA graphics cards](#nvidia-graphics-cards)); for any other GPU build, make
`.venv-chatterbox` yourself with PyTorch and `chatterbox-tts`, then run the installer with `--checkpoint-only`: it fetches the
model files and leaves your packages alone. Only the CPU recipe has been tried.

To clone a voice, open **Voice cloning**, choose Chatterbox Turbo, record or choose a recording longer than five seconds, write
down its words, where it came from and what it may be used for, and press **Save voice and generate test**. **Test** on the Models
page speaks in the built-in voice, unless `reference\af_heart.wav` exists (the synthetic clip Kokoro's installer and Kokoro's first
load make), which it then clones. A call to
`/api/speak` that clones must name its `reference_path` unless that clip exists; with `clone` set to false it uses the built-in
voice.

Chatterbox Original (500M) and Voice Conversion run in the same `.venv-chatterbox`, so after this install they need only their own
checkpoint, `ResembleAI/chatterbox` at revision 5bb1f6e in `hf-cache\hub`, which no installer fetches yet.

Licences: the Chatterbox code (`chatterbox-tts`) and the Turbo weights are MIT (Resemble AI; the model card says so), and a few
of its decoder files keep Apache-2.0 headers from CosyVoice. Every clip Chatterbox makes carries Resemble AI's Perth watermark, a
mark you cannot hear that detection tools can find; the Lab does not remove it. Most of the packages are MIT, BSD or Apache. soxr
and the libsndfile library inside SoundFile are LGPL-2.1, and certifi and tqdm are MPL-2.0. They are installed on your computer at
your request and are not part of this repository. The model card asks that it not be used to do harm; read
[responsible use](RESPONSIBLE_USE.md) before you clone anyone's voice.

What it does here, measured on one computer with only the CPU (an 8-core AMD Ryzen 7 PRO 8845HS): it loads in 10 to 30 seconds
(the first load after installing is the slow one, and the Models page's load includes a short warm-up line), prepares a voice
from a recording in one to three seconds, and takes two and a half to three times as long as the speech it makes (a real-time
factor of 2.5 to 3). One worker holds 3.3 GB when loaded, peaked at 6.1 GB while it loaded (the weights are read whole
before they are copied into the model), and held 5.0 GB after speaking a line of 1,200 characters; the Lab asks for 8 GB of free
RAM before it loads it ([`min_free_ram_gb`](#hardware-and-state-settings) changes that for one computer). Keep lines to a few
sentences: given 1,200 characters, it stopped after 30 to 36 seconds of audio, and the
take's checks flag audio much shorter than its text.

To remove Chatterbox, delete `.venv-chatterbox`, `hf-cache\hub\models--ResembleAI--chatterbox-turbo` and `out\chatterbox`. The voice
match then says it is not installed.

If you set Chatterbox up in `.venv` by hand before this installer existed, the Lab now looks for it in `.venv-chatterbox`. Run the
installer, which keeps the checkpoint files already in `hf-cache` and fetches only what is missing, or build `.venv-chatterbox`
yourself with the packages you had (a GPU build of PyTorch, say) and run it with `--checkpoint-only`.

The recipe was checked on one Windows 11 computer with Python 3.12.10, using only the CPU, from fresh clones of the repository
after `setup.bat`. `setup-chatterbox.bat` made `.venv-chatterbox` and installed the 73 packages (pip's cache was warm), and was
stopped part way through the download. Run again, it kept the packages, resumed the half-fetched file where it stopped, and
finished in 252 seconds; its speech check spoke in the built-in voice and cloned it. In the studio started from that folder,
**Test** passed (2.4 s of audio in 6.6 s), and the calls the Voice cloning page makes saved a voice from an 11.5-second recording
and spoke a test line in it: the voice match scored that take 0.92 against the recording and 0.78 against Turbo's built-in
voice. In a second fresh clone, **Install** on the Turbo card ran the installer in 308 seconds with the model files already in
`hf-cache`, and **Test** then passed. If the speech check fails with a message about a missing DLL, install the
[Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) (x64) and run the installer again. Other
Windows setups have not been tried.

## NVIDIA graphics cards

Kokoro and Chatterbox Turbo can run on an NVIDIA graphics card with a GPU (CUDA) build of PyTorch instead of the CPU one. **Nobody
has tried this on an NVIDIA card yet**: the builds are pinned by their SHA-256 and the installers check them, but whether they
load and speak on your card is what a [hardware report](TROUBLESHOOTING.md#reporting-a-problem) would tell the next person.

```powershell
setup-kokoro.bat --nvidia
setup-chatterbox.bat --nvidia
```

`setup.bat` offers the same when it finds an NVIDIA driver. Before anything is downloaded, each installer asks NVIDIA's own
`nvidia-smi` which card and driver this computer has, and refuses a build that cannot run there: without a driver
(`System32\nvcuda.dll`), on a card older or newer than the build has code for, or, for Kokoro, with a driver older than 580.
Each then runs its speech check on the card. `setup.bat` asks once for both models and gives each the build that runs on the
card: on an RTX 50 series card, Kokoro gets its NVIDIA build and Chatterbox Turbo its CPU build, and setup says so.

| Model | PyTorch build | Cards | Download, beyond the CPU install |
|---|---|---|---|
| Kokoro | 2.12.0, CUDA 13.0 ([requirements/kokoro-nvidia.txt](../requirements/kokoro-nvidia.txt)) | GeForce GTX 16 and RTX 20 series or newer, RTX 50 included, with an NVIDIA driver of version 580 or later | about 1.8 GB |
| Chatterbox Turbo | 2.6.0 with torchaudio, CUDA 12.6 ([requirements/chatterbox-nvidia.txt](../requirements/chatterbox-nvidia.txt)) | up to the RTX 40 series; not RTX 50 cards, which need a newer CUDA than PyTorch 2.6 has | about 2.3 GB |

The CUDA builds carry NVIDIA's CUDA libraries inside them, under NVIDIA's own licence terms; like the other packages they are
installed on your computer at your request and are not part of this repository.

When the service finds an NVIDIA driver it asks its workers for the card (`cuda`). A model whose own PyTorch cannot use the card
runs on the CPU anyway, and its card on Models & features says it ran on the CPU: the CPU build (installed without `--nvidia`),
or a CUDA build with no code for this card, such as PyTorch 2.6 on an RTX 50 series card, which reports the card as available
and then could not run anything on it. To switch a model between the two, delete its environment folder (`.venv-kokoro` or
`.venv-chatterbox`) and run its installer again with or without `--nvidia`; the model files in `hf-cache` stay.
`"device": "cpu"` in `runtime.local.json` keeps every model on the CPU.

## Model runtimes

The [catalog](../catalog/engines.json) records exact checkpoint revisions and
interpreter paths used by the adapters. A catalog entry means integration exists,
not that the corresponding package, model, language frontend or hardware is present. After installing a family, open **Models & features**: it shows whether the environment and checkpoint files are present, and **Test** loads the model and speaks one sentence on the service device. Each card also says how much free RAM the Lab wants before it loads that model, and it refuses to load the model when less is free ([`min_free_ram_gb`](#hardware-and-state-settings) changes that for one computer). A model without a guided installer that is missing files says so when it is asked to speak, and points here.

Two packages are optional and not part of the core install: `misaki` (English phoneme rules for the lip-sync
tracks of aligned takes; it comes with Kokoro, in `.venv-kokoro`, and the exporter asks Kokoro's environment for them) and
`librosa` (the time-stretch in `fit.py`, which the exporter does not use). The app, Cast and scene export run without them.

| Family | Runtime path | Setup status |
|---|---|---|
| Kokoro | `.venv-kokoro/Scripts/python.exe` | `setup-kokoro.bat` installs the pinned runtime and checkpoint ([above](#kokoro-on-the-cpu)), for the CPU or, with `--nvidia`, an NVIDIA card. |
| Phonon 31M English | `.venv-phonon/Scripts/python.exe` | `setup-phonon.bat` installs the pinned CPU runtime and model files ([above](#phonon-on-the-cpu)). |
| Chatterbox Original/Turbo/VC | `.venv-chatterbox/Scripts/python.exe` | `setup-chatterbox.bat` installs the pinned CPU runtime and Turbo's checkpoint ([above](#chatterbox-turbo-on-the-cpu)). Original and VC use the same environment and need the `ResembleAI/chatterbox` checkpoint, which no installer fetches yet. A GPU build of PyTorch is yours to install there. |
| Breeze | `.venv-breeze/Scripts/python.exe` | Separate environment, BF16 standalone wrapper. Needs the pinned checkpoint and text/audio tokenizer files. Review its non-commercial model license first. |
| faster-Qwen | `.venv-fqwen/Scripts/python.exe` | Separate environment; faster-qwen3-tts 0.4.0, qwen-tts-hf 0.1.1.post1, Transformers 5.15.1 were tested locally. Needs exact catalog snapshots. Stock qwen-tts is a different comparison runtime. |
| VoxCPM2 | Dedicated catalog environment and pinned source/ROCm overlay | Needs a GPU with at least 8 GB of memory (it peaked at 5.6 GB). Tested only on one AMD Radeon AI PRO R9700 with ROCm. It reads pinned source under `out/voxcpm2/source/src` and the Qwen environment's packages, so it is not a portable automatic installation. |
| Drama 3 | Core environment plus separate speech.cpp | Pinned native download installer below; studio and cloning use CPU. |

Do not install a model's dependencies into the core environment. Runtime dependency conflicts are why every model has an
environment of its own and the workers are separate processes. This release does not claim Linux or macOS support, tested NVIDIA
support (the `--nvidia` builds are untried) or reproducible builds of all optional model environments. Public installer recipes
for the other families are not written yet.

For Drama, first read its [model license](https://huggingface.co/fishaudio/drama-3/blob/e0a9218aa48888270f06ef1ed9ea67622c0e17d2/LICENSE),
then explicitly run:

```powershell
.venv\Scripts\python.exe tools/install_drama3.py
```

This downloads a 20.8 GB Q4 model and portable speech.cpp 0.8.10 runtime, verifies
hashes, and retains license/derivative notices under `out/drama3`. Allow additional
space for the runtime archive and temporary/resumable download. Studio inference
needs at least 26 GB free RAM and may take several minutes. No model download
occurs during ordinary generation. Keep the installer receipt with the model.

## Hardware and state settings

`runtime.local.json` optionally supplies `device`, `hip_visible_devices`,
`disable_cudnn` and `drama_design_device`; copy/edit the provided example for your own hardware only. `device` is `auto` (the
default: the graphics card when there is an NVIDIA driver, or when the service's own PyTorch can see one), `cpu` or `cuda`
(also the name a ROCm build of PyTorch answers to on an AMD card); `--device` on `serve.py` or `desktop.py` chooses for one
run. A model whose PyTorch cannot use the card runs on the CPU whatever is chosen. `drama_design_device` (default `cpu`) selects the device for single-line Drama design only; `Vulkan1` is the experimental local GPU path. Drama cloning and studio synthesis always use CPU.
Environment `HIP_VISIBLE_DEVICES` takes precedence. The public default does not
hide GPU 0. `--device cpu` avoids GPU selection.

`min_free_ram_gb` changes how much free RAM the Lab wants before it loads an engine, on this computer only, for example
`{"min_free_ram_gb": {"kokoro": 2}}`; 0 turns the check off for that engine. The defaults, in `ram_needs.py`, are the numbers
the Models page shows. A lower number lets a model start on a computer that is short of memory, and Windows may then slow down
or the worker may stop. A name the Lab does not know, or a number below 0, stops the service with a message that says so.

`desktop.local.json` optionally contains `{"state_root":"relative/or/absolute/folder"}`.
Its default is this project directory. A different state root changes the Cast,
Scenes and Evaluation stores; do not move a database without its media. Model
caches and worker logs stay under the source installation. **System & backups**
(and `tools\backup_library.py use`) writes this file when you choose to open a restored library:
it keeps any other setting in it and never overwrites a file it cannot read. See
[back up and restore the library](USER_GUIDE.md#back-up-and-restore-the-library).

`ffmpeg` and `ffprobe` are needed for scene export and transcoding, for importing M4A references, and for
MP3 and AAC export (the build needs its `libmp3lame` and `aac` encoders; the usual full build has both). The lab
finds them on PATH or in the folder WinGet unpacks them into (`winget install Gyan.FFmpeg`), so a desktop
shortcut works before PATH has been refreshed. Neither tool is bundled. Without them WAV, FLAC, OGG and MP3
references still import, and the export panel and the import errors say what is missing. Transcript drafts optionally
use an isolated `.venv-asr` environment built from `requirements/asr-faster-whisper.txt` and a local copy of the Systran faster-whisper-tiny snapshot (revision d90ca5fe) in `hf-cache/faster-whisper-tiny-d90ca5fe`; `align_takes.py` uses the same pair for word times ([alignment notes](ALIGNMENT.md)).
The UI reports unavailable if they are absent. Speaker matching (the voice match on
takes, the second-voice warning in the reference screen, and the identity checks on
cleaned and imported clips) needs only Chatterbox Turbo's speaker encoder, `ve.safetensors`
in the pinned Turbo snapshot in `hf-cache`, which `setup-chatterbox.bat` fetches. The Lab runs
that encoder itself with NumPy, so neither PyTorch nor Chatterbox has to be in `.venv`; its
tests hold it to embeddings that Chatterbox made. The two bands that label a score
ship in [catalog/speaker_similarity.json](../catalog/speaker_similarity.json), so
nothing needs measuring first. The encoder is not required to start; without it those
checks say they did not run.

The app's default local-only boundary and optional-model limits are described
in [SECURITY.md](../SECURITY.md) and [model capabilities](../MODEL_CAPABILITIES.md).

## Updating the Lab

**System & backups** has a **Check for updates** button. It asks GitHub which release of the Lab is the newest and says whether
it is newer than yours, with a link to what changed. It sends nothing about you or your library; GitHub sees only that this
computer asked. The Lab never asks by itself unless you tick **Check when the Lab opens**, and then at most once a day, with a
note in the page header when a newer version is out.

To update, close the Lab and run `update.bat` in its folder:

```powershell
update.bat            # shows what will change, asks, then updates
update.bat --check    # only says whether there is a newer release
update.bat --dry-run  # shows the plan and changes nothing
update.bat --undo     # puts back the version the last update replaced
```

It refuses while the Lab, `serve.bat` or an installer is running from the folder. In a folder that came from a ZIP it downloads
the new release and the one you have from GitHub, replaces the files the Lab ships, removes the ones the new version dropped and
leaves every other file alone: your library, the models, the `.venv` environments, `runtime.local.json` and `out\` are never
touched. It copies every file it replaces or removes to `out\update\<from>-to-<to>` first, which is what `--undo` puts back,
and if a file cannot be written it puts back what it had already changed. A shipped file you had changed yourself is replaced
too, and your copy stays in that folder. In a folder made with `git clone` it moves the checkout to the release's tag as a
fast-forward, and refuses if you have changed tracked files or made commits of your own.

Then the new version finishes the job: it updates the core packages in `.venv` (and the desktop window's) when their lists
changed, and names any installed model whose pinned packages or model files changed. For those, delete the model's environment
folder and run its installer again; the model files already in `hf-cache` are kept.

## Removing the Lab

The Lab has no installer and writes nothing to the registry, so it does not appear in Settings, Apps. Nearly everything it
adds sits in its own folder, and deleting that folder removes it. In this order:

1. **Close the Lab.** Its window stops its own backend. A service started by hand stops when its console closes. Windows
   will not delete files that a running program holds.
2. **Keep what you want.** Your voices, takes and scenes live in the library, which is this folder unless `desktop.local.json`
   sets a `state_root` (see above). On **System & backups**, press **Back up now** (tick **Saved voices** and **Export
   packages** to include those), then copy the ZIP out of `backups\`. That folder is inside the one you are about to delete.
   [Back up and restore the library](USER_GUIDE.md#back-up-and-restore-the-library) has the details.
3. **Delete the shortcut.** `setup.bat` (or `setup-desktop.bat`) put **Fel's Voice Lab** on your Desktop, outside the folder, and
   it would otherwise be left pointing at nothing.
4. **Delete the Lab folder.** The environments (`.venv`, `.venv-desktop`, `.venv-kokoro`, `.venv-phonon`, `.venv-chatterbox` and
   any other `.venv-` folder), `hf-cache`, `data` and `out` go with it. A library that `state_root` moved elsewhere stays where it
   is: delete that folder too if you want it gone.

Three things are outside the folder:

- **pip's download cache,** `%LOCALAPPDATA%\pip\Cache`, which can hold a gigabyte or more of PyTorch downloads. pip shares it
  with every other Python project on the computer, so clearing it only makes the next download fresh. Run
  `.venv\Scripts\python.exe -m pip cache purge` before step 4, or delete that folder afterwards.
- **Programs you installed separately:** Python 3.12, FFmpeg (`winget uninstall Gyan.FFmpeg`) and the Microsoft Visual C++
  Redistributable, which other programs use too. Remove them from Settings, Apps when nothing else needs them.
- **[Fel's Voice Slate](https://github.com/Fel101/fels-voice-slate),** the companion recorder, which is a separate program in
  its own folder.

The desktop window runs in private mode and deletes its temporary browser data when it closes, so it leaves nothing behind. To
remove one model and keep the Lab, see the end of [Kokoro on the CPU](#kokoro-on-the-cpu), [Phonon on the CPU](#phonon-on-the-cpu) or
[Chatterbox Turbo on the CPU](#chatterbox-turbo-on-the-cpu).
