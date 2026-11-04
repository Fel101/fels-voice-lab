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
| Chatterbox 500M and Voice Conversion | 10 GB | No | Only just | Yes | not measured | by hand |
| Qwen3-TTS (four checkpoints) | 10 GB | No | Only just | Yes | not measured | by hand |
| VoxCPM2 2B | 10 GB, and a graphics card with 8 GB | No | Only just | Yes | not measured | by hand |
| Breeze TTS 2 | 12 GB | No | No | Yes | not measured | by hand |
| Drama 3 (35B, Q4) | 26 GB | No | No | With nearly everything closed | 21 GB | `tools\install_drama3.py` |

The RAM numbers are the Lab's own rule, a measured peak with room to spare where one was measured; they live in
[ram_needs.py](../ram_needs.py). Only one model is loaded at a time, so the largest one you use is what counts.

How fast, measured on one computer on its CPU (an AMD Ryzen 7 PRO 8845HS laptop): Phonon makes a second of speech in about
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

**"not installed".** The card on **Models & features** lists the files that are missing. Kokoro, Phonon and Chatterbox Turbo
have an **Install** button; the other models are set up by hand ([Model runtimes](SETUP.md#model-runtimes)).

**"Installed, test out of date".** A package or a checkpoint changed since the last test. Press **Test**.

**A model loads but speaks badly.** Kokoro's Japanese and Mandarin voices need language packages its installer does not add;
Chatterbox keeps a line to a few sentences (give it 1,200 characters and it stops early, which the take's checks flag).

## Graphics cards

**NVIDIA.** Kokoro and Chatterbox Turbo can run on an NVIDIA card with a GPU build of PyTorch: `setup.bat` offers it when it finds
an NVIDIA driver, or run `setup-kokoro.bat --nvidia` and `setup-chatterbox.bat --nvidia`. Nobody has tried this on an NVIDIA
card yet, so a [hardware report](#reporting-a-problem) of how it went is welcome.

- Kokoro's build is CUDA 13.0: a GeForce GTX 16 or RTX 20 series card or newer (RTX 50 included), and an NVIDIA driver of version
  580 or later.
- Chatterbox's is CUDA 12.6, the newest that the PyTorch 2.6 it needs comes in: cards up to the RTX 40 series. It does not run
  on RTX 50 cards.
- To switch a model between its CPU and GPU builds, delete its folder (`.venv-kokoro` or `.venv-chatterbox`) and run its
  installer with or without `--nvidia`. The model files stay.
- The service uses the card when it finds an NVIDIA driver. A model whose own PyTorch is the CPU build still runs, on the CPU,
  and its card on Models & features says so. To keep everything on the CPU, put `{"device": "cpu"}` in `runtime.local.json`.

**AMD.** The Lab was built on an AMD card with ROCm builds of PyTorch, installed by hand into the models' environments; there is
no guided installer for them. If the service does not pick the card by itself, put `{"device": "cuda"}` in
`runtime.local.json` (ROCm answers to that name too).

## Export and audio

**An export will not start, or M4A references will not import.** Export packages and M4A imports need FFmpeg, which is not
bundled. Run `winget install Gyan.FFmpeg`, then press **Check FFmpeg again** in **System & backups**. The Lab finds it in
WinGet's folder even before Windows has refreshed its PATH. MP3 and AAC also need FFmpeg's `libmp3lame` and `aac` encoders,
which the usual full build has. Without FFmpeg, WAV, FLAC, Ogg and MP3 references still import.

**What a file says about itself.** Every clip the Lab makes is labelled inside the file as AI-generated speech made with the
Lab, in its comment tag; an export's package checks the label is still there when it is verified. Your own reference recordings
are never labelled. A tag is easy to strip, so it is a label, not a watermark.

## Reporting a problem

Open **System & backups**, press **Copy report for a bug report**, and paste it into a
[hardware report](https://github.com/Fel101/fels-voice-lab/issues/new?template=hardware_report.yml) or a
[bug report](https://github.com/Fel101/fels-voice-lab/issues/new?template=bug_report.md). The report names the version, Windows
and Python, the processor, graphics cards and memory, which models passed their test on which device and how fast, and what
FFmpeg can write. It holds no scripts, transcripts, voice names or recordings, and your home folder reads as `~`. Each installer
also leaves a small `installation.json` receipt in `out\kokoro`, `out\phonon` or `out\chatterbox` that is worth adding.
