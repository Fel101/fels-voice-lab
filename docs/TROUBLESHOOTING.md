# Troubleshooting

What runs on which computer, and what to do when setup, a model or an export stops. [Setup](SETUP.md) has the full detail of
every installer. If something here does not help, the end of this page says how to report it.

## What runs on which computer

The Lab checks how much memory is free before it loads a model and refuses to start one that would not fit, rather than let
Windows slow to a crawl. *Free* is what is left once Windows and your other programs have taken theirs, usually 4 to 6 GB less
than the computer has in all, more with a browser full of tabs open.

| Model | Free RAM it asks for | 8 GB computer | 16 GB computer | 32 GB computer | Disk | Installer |
|---|---|---|---|---|---|---|
| Phonon 31M English | 2 GB | Yes | Yes | Yes | 0.3 GB | `setup-phonon.bat` |
| Kokoro 82M | 3 GB | Yes | Yes | Yes | 1.7 GB | `setup-kokoro.bat` |
| Chatterbox Turbo 350M (cloning) | 8 GB | No | With little else open | Yes | 5 GB | `setup-chatterbox.bat` |
| Chatterbox 500M and Voice Conversion | 10 GB | No | Only just | Yes | 3.2 GB, and Turbo's environment | `setup-chatterbox.bat --original` |
| Qwen3-TTS (four models) | 10 GB, and a graphics card | No | Only just | Yes | 5.3 GB, and 2.5 or 4.5 GB a model | `setup-qwen.bat --model` (NVIDIA) |
| VoxCPM2 2B | 10 GB, and a graphics card with 7 GB | No | Only just | Yes | 10 GB | `setup-voxcpm.bat` (NVIDIA) |
| Breeze TTS 2 | 12 GB, and a graphics card with 10 GB | No | No | Yes | 12.5 GB | `setup-breeze.bat` (NVIDIA) |
| Drama 3 (35B, Q4) | 26 GB | No | No | With nearly everything closed | 21 GB | `setup-drama3.bat` |

The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 run only on a graphics card, and the app installs them only for an NVIDIA one
([graphics cards](#graphics-cards)).

The RAM numbers are the Lab's own rule, a measured peak with room to spare where one was measured; they live in
[ram_needs.py](../ram_needs.py). Only one model is loaded at a time, so the largest one you use is what counts.

How fast, measured on one computer on its CPU (an AMD Ryzen 7 PRO 8845HS mini PC): Phonon makes a second of speech in about
0.2 s and Kokoro in about 0.3 s, so both are faster than real time. Chatterbox Turbo takes 2.5 to 3 s for each second of
speech. A graphics card makes the PyTorch models much faster; see [graphics cards](#graphics-cards).

To change a RAM need for your computer only, add it to `runtime.local.json` beside `serve.py`, for example
`{"min_free_ram_gb": {"turbo": 6}}`. A lower number lets the model start when memory is short, and Windows may then slow down
or the model's worker may stop.

## Setup stops

**`py -3.12` could not start.** The Lab needs Python 3.12 (64-bit). The newest Python on python.org is a later version, which
the Lab does not support yet. Install
[python-3.12.10-amd64.exe](https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe), the last 3.12 release with a
Windows installer, and run `setup.bat` again. Setup keeps what is already done.

**Windows asks before it runs `setup.bat`.** Files extracted from a ZIP downloaded from the internet carry a mark that makes
Windows ask before running them. Choose to run it, or, before you extract the ZIP, right-click it, choose **Properties**, tick
**Unblock** and press **OK**.

**The folder's path is too long.** PyTorch and the model caches have deep folders, and Windows stops at 259 characters unless
long paths are switched on. Move the Lab to a short path such as `C:\voice-lab`. Or switch long paths on: in PowerShell run
as administrator, `New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1
-PropertyType DWORD -Force`, then run the installer again.

**The folder is inside OneDrive.** OneDrive would upload gigabytes of packages and models and can lock the Lab's databases while
it syncs them. Move the folder somewhere OneDrive does not sync (often not the Desktop or Documents), such as `C:\voice-lab`.

**pip stopped, or a download stopped.** Usually the connection dropped or a proxy got in the way. Run the same installer again:
downloads resume where they stopped and packages already installed are kept. A file that does not match its published SHA-256
is deleted rather than kept, so a damaged download cannot slip through.

**"already has other versions of packages".** Each model has an environment of its own (`.venv-kokoro`, `.venv-phonon`,
`.venv-chatterbox`), and an installer never replaces a package it finds there at another version. If you did not put them there
on purpose, delete that model's folder and run its installer again.

**A message about a missing DLL** (`WinError 126`, for example `fbgemm.dll` or `c10.dll`). Install the
[Microsoft Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe) (x64), then run the installer again.

**A virus scanner holds a file.** Installers wait a few seconds for a file that is still being scanned. If one still says access
is denied, let the scan finish, or allow the Lab's folder in the scanner, and run the installer again.

**After updating from a version that put Kokoro and Phonon in `.venv`.** Models & features now says they are not installed:
they live in `.venv-kokoro` and `.venv-phonon`. Run `setup-kokoro.bat` and `setup-phonon.bat` (or press **Install** on their
cards). The model files already in `hf-cache` are kept, so only the packages download again.

## A model will not load

**"Only 5.2 GB RAM free; not loading turbo (needs 8 GB free)."** Close what holds memory (browsers with many tabs, games,
other AI tools), then try again. The Lab unloads one model before it loads the next, so you never need to close it. See the
table above for what fits your computer, and `min_free_ram_gb` to change a need.

**"not installed".** The card on **Models & features** lists the files that are missing, and its **Install** button installs
them. The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 install only for an NVIDIA graphics card and driver their build runs on
(VoxCPM2 needs a card with 7 GB of memory, Breeze one with 10 GB); on another computer their card says why it has no
**Install** ([Model runtimes](SETUP.md#model-runtimes)).

**"Installed, test out of date".** A package or a checkpoint changed since the last test. Press **Test**.

**A model loads but speaks badly.** Kokoro's Japanese and Mandarin voices need language packages its installer does not add;
Chatterbox keeps a line to a few sentences (give it 1,200 characters and it stops early, which the take's checks flag).

## Graphics cards

**NVIDIA.** Kokoro and the Chatterbox models can run on an NVIDIA card with a GPU build of PyTorch: `setup.bat` offers it when it
finds an NVIDIA driver, or run `setup-kokoro.bat --nvidia` and `setup-chatterbox.bat --nvidia`. The Qwen3-TTS models, VoxCPM2
and Breeze TTS 2 run only on a graphics card and have only an NVIDIA build, which **Install** on their cards puts in. Nobody
has tried any of this on an NVIDIA card yet, so a [hardware report](#reporting-a-problem) of how it went is welcome.

- Every NVIDIA build is CUDA 13.0 (PyTorch 2.12): a GeForce GTX 16 or RTX 20 series card or newer (RTX 50 included), and an
  NVIDIA driver of version 580 or later. On an older card, setup gives Kokoro and Chatterbox their CPU builds.
- **"needs a graphics card with at least 10 GB of memory".** VoxCPM2 needs 7 GB of graphics memory and Breeze TTS 2 10 GB, so
  their installers refuse a smaller card before they download anything. Breeze used 7.5 GB at most, too much for an 8 GB card
  once Windows and other programs have taken their share.
- **Models & features** has a **Runs on GPU | CPU** switch on each card. A model installed for the CPU offers **Install GPU
  build** when it is switched to GPU; GPU is greyed out, with the reason, where it cannot work.
- **"needs NVIDIA driver 580 or later".** The CUDA 13.0 builds need a 2025 driver. Update it from nvidia.com or the NVIDIA
  app, restart the Lab, and press **Install GPU build** on the model's card (or run its launcher with `--nvidia`, which
  replaces a CPU build of PyTorch in place). The installers read the card and the driver with NVIDIA's `nvidia-smi`; when it
  cannot say, they go by the driver version Windows lists.
- Going back from a GPU build to the CPU build takes deleting the model's folder (`.venv-kokoro` or `.venv-chatterbox`) and
  installing it again; the model files stay. The switch alone keeps a GPU build on the CPU.
- The service uses the card when it finds an NVIDIA driver. To keep everything on the CPU, put `{"device": "cpu"}` in
  `runtime.local.json`.

**AMD.** The Lab was built on an AMD card with ROCm builds of PyTorch, installed by hand into the models' environments; there is
no guided installer for them. Make a model's environment with a ROCm build of PyTorch yourself, then run its launcher with
`--checkpoint-only`, which fetches the model files, leaves the packages alone and runs the speech check ([Setup](SETUP.md)
lists each model's packages). If the service does not pick the card by itself, put `{"device": "cuda"}` in
`runtime.local.json` (ROCm answers to that name too).

## Export and audio

**An export will not start, or M4A references will not import.** Export packages and M4A imports need FFmpeg, which is not
bundled. Run `winget install Gyan.FFmpeg`, then press **Check FFmpeg again** in **System & backups**. The Lab finds it in
WinGet's folder even before Windows has refreshed its PATH. MP3 and AAC also need FFmpeg's `libmp3lame` and `aac` encoders,
which the usual full build has. Without FFmpeg, WAV, FLAC, Ogg and MP3 references still import.

**What a file says about itself.** Every clip the Lab makes is labelled inside the file as AI-generated speech made with the
Lab, in its comment tag; an export's package checks the label is still there when it is verified. Your own reference recordings
are never labelled. A tag is easy to strip, so it is a label, not a watermark.

## Updating

**`update.bat` says the Lab is running.** It names each program it found running from the Lab's folder. Close the Lab's window,
stop `serve.bat` (Ctrl+C in its window) and let any installer finish, then run it again.

**"Files git tracks were changed here", or "has commits that the release does not".** A folder made with `git clone` is only
moved forward to a release, never merged. Commit or stash your changes and run `update.bat` again, or update it with git yourself.

**Something is wrong after an update.** `update.bat --undo` puts the previous version back: from the copies it kept in
`out\update\<from>-to-<to>`, or with `git reset --keep` in a Git checkout. An update and an undo never change your library, the
models or `runtime.local.json`.

**"pinned packages changed".** A release can pin newer packages for a model, and the update names the models it affects. Delete
that model's environment folder and run its installer again; the model files are kept, so only the packages download again.

**Check for updates says GitHub could not be reached.** The check asks `api.github.com` and needs the internet; a firewall or a
proxy can stop it. Nothing else in the Lab needs GitHub once it is installed. "No release of the Lab has been published" is an
answer, not an error: there is nothing to update to yet.

## Reporting a problem

Open **System & backups**, press **Copy report for a bug report**, and paste it into a
[hardware report](https://github.com/Fel101/fels-voice-lab/issues/new?template=hardware_report.yml) or a
[bug report](https://github.com/Fel101/fels-voice-lab/issues/new?template=bug_report.md). The report names the version, Windows
and Python, the processor, graphics cards and memory, which models passed their test on which device and how fast, and what
FFmpeg can write. It holds no scripts, transcripts, voice names or recordings, and your home folder reads as `~`. Each installer
also leaves a small `installation.json` receipt in `out\kokoro`, `out\phonon` or `out\chatterbox` that is worth adding.
