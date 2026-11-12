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
| Qwen3-TTS (four models) | 10 GB, and a graphics card | No | Only just | Yes | 5.3 GB, and 2.5 or 4.5 GB a model | `setup-qwen.bat --model` (NVIDIA or AMD) |
| VoxCPM2 2B | 10 GB, and a graphics card with 7 GB | No | Only just | Yes | 10 GB | `setup-voxcpm.bat` (NVIDIA or AMD) |
| Breeze TTS 2 | 12 GB, and a graphics card with 10 GB | No | No | Yes | 12.5 GB | `setup-breeze.bat` (NVIDIA or AMD) |
| Drama 3 (35B, Q4) | 26 GB | No | No | With nearly everything closed | 21 GB | `setup-drama3.bat` |

The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 run only on a graphics card, an NVIDIA card or an AMD Radeon
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

**Setup could not get Python 3.12.** When the computer has no Python 3.12, `setup.bat` downloads the Lab's own (about 14 MB,
from nuget.org) into its `python` folder. If that download stopped, check the internet connection and run `setup.bat` again;
it keeps what is already done. If the file did not match its SHA-256, it was deleted: run setup again, and if it happens
again, a proxy or security program is changing downloads. Or install
[python-3.12.10-amd64.exe](https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe) yourself, the last 3.12
release with a Windows installer, and run `setup.bat` again: it uses an installed Python 3.12 and downloads nothing. The
newest Python on python.org is a later version, which the Lab does not support yet.

**The Lab stopped starting after its folder was moved or renamed.** A console that says `No Python at` a path in the old
folder means the environments were built on the Lab's own Python, which moved. Move the folder back, or follow
[Moving the Lab](SETUP.md#moving-the-lab): delete the `.venv` folders and run `setup.bat` again.

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
them. The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 install only for a graphics card their build runs on, an NVIDIA card and
driver or an AMD Radeon whose chip AMD's build has code for (VoxCPM2 needs a card with 7 GB of memory, Breeze one with 10 GB);
on another computer their card says why it has no **Install** ([Model runtimes](SETUP.md#model-runtimes)).

**"Installed, test out of date".** A package or a checkpoint changed since the last test. Press **Test**.

**A model loads but speaks badly.** Kokoro's Japanese and Mandarin voices need language packages its installer does not add;
Chatterbox keeps a line to a few sentences (give it 1,200 characters and it stops early, which the take's checks flag).

## Graphics cards

**NVIDIA.** Kokoro and the Chatterbox models can run on an NVIDIA card with a GPU build of PyTorch: `setup.bat` offers it when it
finds an NVIDIA driver, or run `setup-kokoro.bat --gpu` and `setup-chatterbox.bat --gpu` (`--nvidia` asks for NVIDIA's build
alone). The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 run only on a graphics card; on an NVIDIA one, **Install** on their cards
puts in their NVIDIA build. Every NVIDIA build was checked on a laptop with a GeForce RTX 5070 Laptop GPU (8 GB), so a
[hardware report](#reporting-a-problem) from another NVIDIA card is welcome.

- **"CUDA cannot use the ...", "No CUDA GPUs are available" or "CUDA graphs require CUDA device".** The NVIDIA driver is
  installed, but the card is not available. On a laptop it is usually switched off: by a power-saving, Eco or iGPU-only
  mode in Windows or in the maker's app (GIGABYTE Control Center, Armoury Crate, Lenovo Vantage, MSI Center), or in the
  BIOS. Device Manager then shows the card greyed out or with Code 45, and `nvidia-smi` blames permissions, which are not
  the problem. Switch the card on (restart if asked) and install again: the files already downloaded are kept. A
  `CUDA_VISIBLE_DEVICES` of `-1` hides the card in the same way. The installers check this before they download anything,
  and again once PyTorch is in place; **Models & features** reads the card again within half a minute of it being
  switched on. While the card is off, a model already installed runs on the CPU, and its card says so.
- **The first load on an RTX 50 card is slow.** Loads on the RTX 5070 Laptop GPU took 26 to 84 seconds, much of it PyTorch's
  CUDA 13.0 build preparing code for the card. Once a model is loaded, it is quick.

- Every NVIDIA build is CUDA 13.0 (PyTorch 2.12): a GeForce GTX 16 or RTX 20 series card or newer (RTX 50 included), and an
  NVIDIA driver of version 580 or later. On an older card, setup gives Kokoro and Chatterbox their CPU builds.
- **"needs a graphics card with at least 10 GB of memory".** VoxCPM2 needs 7 GB of graphics memory and Breeze TTS 2 10 GB, so
  their installers refuse a smaller card before they download anything. Breeze used 7.5 GB at most for the check sentence, too
  much for most 8 GB cards once Windows and other programs have taken their share (on a laptop whose NVIDIA card drives no
  display, **Test** passed at 7.4 of 8 GB), and a 29-second take allocated 9.5 GB: on a card with 10 or 12 GB, keep
  Breeze's lines short. When neither `nvidia-smi` nor the driver can say how much memory the card has, they stop and say so.
- **Models & features** has a **Runs on GPU | CPU** switch on each card. A model installed for the CPU offers **Install GPU
  build** when it is switched to GPU; GPU is greyed out, with the reason, where it cannot work.
- **"needs NVIDIA driver 580 or later".** The CUDA 13.0 builds need a 2025 driver. Update it from nvidia.com or the NVIDIA
  app, restart the Lab, and press **Install GPU build** on the model's card (or run its launcher with `--gpu`, which
  replaces a CPU build of PyTorch in place). The installers read the card and the driver with NVIDIA's `nvidia-smi` and ask
  the driver itself (`nvcuda.dll`) whether CUDA starts; when `nvidia-smi` cannot say, they go by the driver's answer and the
  driver version Windows lists.
- Going back from a GPU build to the CPU build takes deleting the model's folder (`.venv-kokoro` or `.venv-chatterbox`) and
  installing it again; the model files stay. The switch alone keeps a GPU build on the CPU.
- The service uses the card when it finds an NVIDIA driver. To keep everything on the CPU, put `{"device": "cpu"}` in
  `runtime.local.json`.

**AMD.** Every model that runs on a graphics card in the Lab runs on an AMD Radeon card with AMD's ROCm build of PyTorch. On a
computer without an NVIDIA driver, **Install** on the cards of the Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 (or their
launchers) puts it in; Kokoro and the Chatterbox models get it from **Install** or **Install GPU build** with their switch on
GPU, from the question `setup.bat` asks, or from `setup-kokoro.bat --gpu` and `setup-chatterbox.bat --gpu`.
[Setup](SETUP.md#amd-radeon-cards) says what it installs. The Lab was checked on a Radeon AI PRO R9700, so a
[hardware report](#reporting-a-problem) from another AMD card is welcome.

- **"has no code for the ...".** AMD's build has code for the chips of the Radeon RX 5000, 6000, 7000 and 9000 series, the
  Radeon PRO and AI PRO cards built on them, and the Radeon graphics in Ryzen processors. An older card (a Radeon RX 500 or
  Vega, say) cannot run it, so the models that need a graphics card have no **Install** there, and Kokoro and Chatterbox keep
  their CPU builds.
- **The first sentence after a load is slow.** On an AMD card, the first sentence a model speaks after it loads takes 10 to 20
  seconds while the card's kernels load; the next ones are quick. The Models page's load speaks a warm-up line for that.
- **"AMD's driver did not say which chip it has".** The installers ask AMD's OpenCL driver, which comes with the graphics
  driver. Install or update AMD Software: Adrenalin Edition (PRO Edition for a workstation card) from amd.com, restart the Lab
  and try again. The same goes for a speech check that fails with a HIP error.
- **Two AMD cards**, such as a Ryzen processor's Radeon graphics beside a Radeon card: the Lab uses the separate card, and the
  first install writes the number PyTorch gives it to `runtime.local.json` as `"hip_visible_devices"`. To use the other card,
  change that number (or set `HIP_VISIBLE_DEVICES`) and run each model's launcher again: it adds the code for that card's chip.
  An installer stops, and says which number to give, when the setting names a card other than the one it installed for.
- The service uses the card when it finds one that AMD's build has code for. `{"device": "cpu"}` in `runtime.local.json`
  keeps everything on the CPU.

## Export and audio

**An export will not start, a reference will not clean, or M4A references will not import.** These need FFmpeg, which is not
bundled. Press **Install FFmpeg** in **System & backups** (or on the export panel): the Lab downloads its own copy into its
`ffmpeg` folder and uses it at once, with no restart. If it says the download stopped, connect the computer to the internet
and press it again; it continues where it stopped. If you would rather install FFmpeg yourself, run
`winget install Gyan.FFmpeg`, then press **Check FFmpeg again**: the Lab finds it in WinGet's folder even before Windows has
refreshed its PATH. MP3 and AAC also need FFmpeg's `libmp3lame` and `aac` encoders, which the usual full build and the Lab's
own have. Without FFmpeg, WAV, FLAC, Ogg and MP3 references still import.

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

**"pinned packages changed".** A release can pin newer packages for a model, and the update names each environment it affects,
with the models installed in it. Delete that folder, then press **Install** on each model's card or run the command it names
(with `--gpu` where it says so); the model files are kept, so only the packages download again.

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
