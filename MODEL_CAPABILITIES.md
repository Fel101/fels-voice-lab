# Model capabilities

Current integration inventory, 5 November 2026. “Integrated” means the adapter and
UI exist; it does not mean weights/packages are included in this repository.
On the Models page, *installed* means the environment and pinned checkpoint files are on disk, and *usable* means a check on this device loaded the model and spoke one sentence, and neither the model pin nor the packages installed in its environment have changed since. The catalog never claims usability by itself.
The catalog records pins and runtime paths. Listener approval is distinct from
transport, timing, checksum and contract verification.

Language and hardware claims point at the evidence behind them. The
[language screen](docs/LANGUAGE_MATRIX.md) records, for each engine and language, whether a
speech recognizer read the generated sentence back within a stated word error rate, and the
device it ran on. That is a screen, not a listening test. Each device-specific claim below
names the hardware it was checked on. The NVIDIA results come from one laptop with a GeForce RTX 5070 Laptop GPU (8 GB),
with the Models page's **Test**, warm; this project has no Linux results.

| Model | Integrated capabilities | Limits |
|---|---|---|
| Kokoro 82M | Cached fixed voices, equal blends, speed, locale frontends and phoneme preview | No cloning/design/acting direction. Optional Japanese/Mandarin frontends may be absent. Screen: understood in American and British English, Spanish and French; Hindi, Italian and Brazilian Portuguese were not screened because the development snapshot lacked their voice packs, which `setup-kokoro.bat` fetches. It runs in its own `.venv-kokoro`; `--gpu` takes the GPU build of PyTorch for this computer's card: NVIDIA's CUDA build, with which it passed **Test** on the RTX 5070 Laptop GPU (2.7 s of audio in 0.19 s), or AMD's ROCm build, with which it passed **Test** on the R9700 (2.7 s of audio in 0.5 s). Complete WAV output. |
| Phonon 31M English | 37 fixed English preset voices (12 American, 13 British, 12 characters), seed, temperature and a pace shift; runs on the CPU through the numpy-only `ptts` runtime, no PyTorch; the voices are reusable Cast identities | English only. No cloning, design or direction: the open checkpoint has no speaker encoder. `setup-phonon.bat` installs it in its own `.venv-phonon`. The weights are Apache-2.0, but the repository of the 37 voices states no licence, so ask Gradium before you publish audio made with one. A seed repeats a clip closely, not byte for byte. Not in the language screen: a trial on 30 October 2026, which is not part of this repository, had a speech recognizer read back 44 clips in four voices at a mean word error rate of 3.8% (Kokoro 3.5% on the same sentences); there has been no listening test. Complete WAV output. |
| Chatterbox Turbo | Reference cloning, native vocal-event tags, variant-specific sampling, a built-in voice | No effective exaggeration/CFG acting controls. Reference must exceed five seconds. English, complete WAV; native Perth watermark retained. `setup-chatterbox.bat` installs it for the CPU in its own `.venv-chatterbox` (PyTorch 2.6). With `--gpu` it takes GPU builds of PyTorch 2.12 and torchaudio 2.11 for this computer's card instead: NVIDIA's CUDA 13.0 builds (GTX 16 and RTX 20 series or newer, RTX 50 included), with which it passed **Test** on the RTX 5070 Laptop GPU (2.4 s of audio in 1.9 s), or AMD's ROCm builds, with which it passed **Test** on the R9700 (2.1 s of audio in 1.8 s). On one 8-core CPU it took 2.5 to 3 times as long as the speech it made, peaked at 6.1 GB of RAM while it loaded, and stopped after 30 to 36 s of audio when given 1,200 characters. Without `reference/af_heart.wav` the Models page test speaks in the built-in voice; the installer's check clones. Its English was screened on the GPU of the development machine. |
| Chatterbox Original | Cloning, exaggeration, CFG weight and supported sampling | No arbitrary text direction or Turbo event controls; complete WAV and native watermark. Runs in `.venv-chatterbox`; `setup-chatterbox.bat --original` installs its checkpoint, for the CPU or, with `--gpu`, this computer's NVIDIA card or AMD card. On one 8-core CPU it loaded in 41 s and spoke 1.9 s of audio in 16.5 s; with AMD's build on the R9700, 2.6 s in 6.6 s; with NVIDIA's on the RTX 5070 Laptop GPU, 2.1 s in 4.0 s. |
| Chatterbox VC | Separate source performance / target identity, provenance | Conversion rather than new scripted speech. Native watermark retained. Runs in `.venv-chatterbox`; `setup-chatterbox.bat --original` installs its checkpoint with Chatterbox 500M's. On one 8-core CPU it converted a 1.9 s line in 13 s once loaded; with AMD's build on the R9700, a 2 s line in 2.0 s. |
| Breeze TTS 2 | Description design, cloning, directed performance, CFG/temperature, seed and eager/graph profiles | Screen: English and Chinese understood ([language screen](docs/LANGUAGE_MATRIX.md)); the picker offers English only. No public native stream transport. Code Apache-2.0; weights and self-hosted outputs non-commercial. `setup-breeze.bat` installs it for a card with at least 10 GB of memory: NVIDIA's build, which passed **Test** even on the 8 GB RTX 5070 Laptop GPU its installer refuses (2.6 s in 17 s, 7.4 GB in use, a short line only), or AMD's ROCm build, with which it passed its speech check on the R9700 and used at most 7.5 GB (a 29 s take allocated 9.5 GB). On the R9700 it takes about three times as long as the speech it makes. |
| Qwen 1.7B VoiceDesign | Description design, language/sampling/seed, native generator transport | Descriptions do not guarantee accents/emotion. Screen: eight of ten picker languages understood; Italian and Portuguese partial and marked unscreened in the picker. Design streaming passed local GPU acceptance. `setup-qwen.bat --model 1.7b-voice-design` installs it for an NVIDIA card, with which it passed **Test** on the RTX 5070 Laptop GPU (1.9 s in 0.96 s), or with AMD's ROCm build, which passed its speech check on the R9700. |
| Qwen 1.7B / 0.6B Base | Reference/transcript cloning, local prompt cache, language/sampling/seed, streaming adapter | Acting instructions experimental; native-stream real acceptance remains pending for these checkpoints. The screen clones the bundled English reference for every language: 1.7B understood in eight of ten, Japanese partial, Portuguese not understood; 0.6B understood in nine of ten, Portuguese not understood. Japanese and Portuguese on 1.7B and Portuguese on 0.6B are marked unscreened in the picker. `setup-qwen.bat --model 1.7b-base` or `--model 0.6b-base` installs one for an NVIDIA card (both passed **Test** on the RTX 5070 Laptop GPU, at about twice real time) or an AMD card with AMD's ROCm build; its speech check clones `reference/af_heart.wav`, which Kokoro's installer makes. |
| Qwen 1.7B CustomVoice | Direct native preset identities, discovered speakers/languages, direction and sampling | No arbitrary cloning. Audition WAVs are not conditioning references. The screen uses one preset speaker for every language: six of ten understood; French, Italian, Japanese and Portuguese partial, and marked unscreened in the picker. Streaming adapter exists; checkpoint-specific real-stream acceptance pending. `setup-qwen.bat --model 1.7b-custom-voice` installs it for an NVIDIA card, with which it passed **Test** on the RTX 5070 Laptop GPU (2.6 s in 1.3 s), or with AMD's ROCm build, which passed its speech check and **Test** on the R9700. |
| VoxCPM2 | Design, reference cloning, transcript continuation, CFG, inference steps and seed; 48 kHz | Screened on R9700/ROCm: understood in English, Spanish, French, German, Italian, Russian, Chinese and Arabic; Japanese and Korean partial; Portuguese not understood; Hindi inconclusive. The picker allows English only, and the upstream card lists 30 languages, 18 of which were never screened. Clone direction experimental; no public native stream. `setup-voxcpm.bat` installs it, with VoxCPM's source files, for a card with at least 7 GB of memory: NVIDIA's build, which passed **Test** on the RTX 5070 Laptop GPU (4.0 s in 9.7 s, 5.7 GB in use), or AMD's ROCm build, which passed its speech check on the R9700. A take that runs on past its text, as the Models page's check sentence did with seed 42 on the R9700, is capped by the spoken text and made again from a derived seed, up to three tries, after which the generation fails. |
| Fish Drama 3 | Description/cues, cloning, sixteen reference slots, 8,000-character joint scripts, 83 language labels + Auto (metadata: the runtime does not receive them), incremental PCM audition, cancel/history/ZIP | Studio/cloning CPU only; short GPU design experimental opt-in (screened on Vulkan1). Text-level screen: nine of twelve screened languages understood; Japanese partial; Korean not understood; Hindi inconclusive; 71 labels never screened. Cues may be ignored/spoken. Mixed mono audio, no stems/word timing. No previous-take VQ editing in speech.cpp 0.8.10. Fish terms/attribution apply. `setup-drama3.bat` installs it (a 20.8 GB download) on a computer with at least 26 GB of RAM. |

All supported character engines reuse immutable Cast identities through Perform
and scene batches, subject to each mode's constraints. Pronunciation and saved
performances forward only supported controls. A duration/context cap failure is
not an accepted complete take. Qwen streaming `/api/stream` and Drama polling are
separate transports; `/api/speak` returns a complete WAV.

## Shared tools

Cast provenance and versioning; line variants and human ratings; scene queue and
stale-selection validation; blind A/B comparisons; export dry-run/verification;
scene export panel over the package exporter; optional local ASR draft suggestions; a proxy voice match (speaker-encoder cosine, not an identity verdict; Chatterbox Turbo's encoder, which the Lab runs with NumPy from the `ve.safetensors` that `setup-chatterbox.bat` fetches); gated reference cleaning (needs FFmpeg; a fail verdict when cleaning moves a clip away from a same-speaker reference); a pre-save reference screen (a 0–100 score with warnings for clipping, noise, reverb, steady tones and a second voice; advisory, not a listening test; [docs/REFERENCE_SCREEN.md](docs/REFERENCE_SCREEN.md)); source/target voice
conversion; Voice Slate clip import; dataset audit/manifests. None grants voice rights
or automatically proves acting/identity quality. Dataset preparation is not training.

Word times: `align_takes.py` records forced-alignment word times for Cast takes, and captions use them where
recorded. The aligner is faster-whisper's cross-attention method with the local tiny model; see
[docs/ALIGNMENT.md](docs/ALIGNMENT.md) for the licence check and the limits, which include unmeasured boundary
accuracy. `fit.py` gives a bounded time-stretch status (`within_slot`, `stretched`, `needs_rerender`). The
export records the status and does not stretch the audio.

Lip-sync tracks: the export writes one mouth-shape track per line that has phoneme times. Kokoro takes store the
model's own predicted phoneme times with the take, in any language Kokoro speaks. Other English takes share their
aligned words over estimated phoneme times. The mouth-shape mapping has not been checked against a face or by ear;
see [docs/LIPSYNC.md](docs/LIPSYNC.md).

## Trials and blocked candidates

Chatterbox Multilingual V3, MOSS-VoiceGenerator and MOSS-TTSD had isolated local
trials but are not ordinary integrated engines. IndexTTS 2.5, CosyVoice 3 and Fish
S2 Pro stopped at their documented Windows/AMD/dependency gates. No compatibility
or licensing claim is transferred between those models and an integrated family.

The local trial reports remain historical developer evidence and are not part of
this repository. Public users should follow [setup](docs/SETUP.md), not reproduce old
personal paths or replace pinned GPU packages from an environment inventory.

## Rights and stability

Read [third-party/model terms](THIRD_PARTY_NOTICES.md) and [responsible use](docs/RESPONSIBLE_USE.md).
Original MIT application code does not supersede model or reference restrictions.
Drama GPU cloning remains disabled following an observed Vulkan device loss and
Windows VIDEO_TDR_FAILURE; no driver/timeout workaround is supplied. Human review
of words, likeness, acting and intended-language quality remains required.
