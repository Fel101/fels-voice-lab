# Third-party notices

Reviewed 20 October 2026, with Phonon added on 30 October 2026 and Chatterbox Turbo's installer and speaker encoder on
3 November 2026. The root MIT license covers the project's original code.
It does not replace the licenses below or grant rights to model weights, voices,
recordings, trademarks, scripts or generated outputs.

## Source included in this repository

| Local source | Origin and license | Retained notices / modifications |
|---|---|---|
| `breeze/loader.py`, `breeze/int8.py`, `breeze/native.py`, `breeze/runtime.py` | [Saganaki22/ComfyUI-Breeze-TTS-2](https://github.com/Saganaki22/ComfyUI-Breeze-TTS-2), Apache-2.0; Copyright 2026 saganaki22 | Local standalone loader/runtime integration; upstream notices remain in headers. `native.py` derives from BreezeBlue's model implementation and credits Sesame and The HuggingFace Inc. configuration code. |
| Breeze inference implementation underlying the preceding files; `breeze/vendor/t5gemma2_compat.py` | [breezeblue-ai/breeze-tts](https://github.com/breezeblue-ai/breeze-tts), Apache-2.0 | Inference-only adaptation, runtime compatibility and standalone serving. |
| `breeze/vendor/codec_config.py`, `breeze/vendor/codec_model.py` | [QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS), Apache-2.0 | Copyright 2026 The Qwen team, Alibaba Group and the HuggingFace Inc. team. Tokenizer subset; changed Transformers decorators and attention-mask arguments. |
| `fqwen_compat.py` | RoPE compatibility function adapted from qwen-tts-hf / Transformers; Apache-2.0 components | Reads both old `rope_theta` and newer `rope_parameters`. Copyright The Qwen team, Alibaba Group and The HuggingFace Inc. team. |
| `speaker_similarity.py` (the speaker encoder: trim, mel frames, partial windows and the LSTM's forward pass) | The inference path of [Chatterbox](https://github.com/resemble-ai/chatterbox)'s `voice_encoder`, MIT, Copyright 2025 Resemble AI, which is adapted from [Real-Time-Voice-Cloning](https://github.com/CorentinJ/Real-Time-Voice-Cloning), MIT, Copyright 2019 Corentin Jemine and the earlier authors its licence names. Its trim, STFT framing and Slaney mel filter bank follow [librosa](https://github.com/librosa/librosa) 0.11.0, ISC, Copyright 2013--2023 librosa development team. | Rewritten for NumPy alone, without PyTorch or librosa, to read Turbo's `ve.safetensors`; tests hold it to values that chatterbox-tts 0.1.7 and librosa 0.11.0 produce. Licence texts: `licenses/Chatterbox-LICENSE.txt`, `licenses/Real-Time-Voice-Cloning-LICENSE.txt` and `licenses/librosa-LICENSE.txt`. |

Full upstream license texts are retained under `licenses/`. The historical import
commit for the Breeze files was not recorded; this audit does not invent one.
License copies were verified against ComfyUI-Breeze commit
`3461ca6011b7e833147b2e7edb779da7a08c6259`, Breeze commit
`6eba901d1845353802652c708a4740fdfc5baa75` and Qwen commit
`022e286b98fbec7e1e916cb940cdf532cd9f488e`. These identify the notice sources,
not a claim that the local adaptations are identical to those revisions. The Chatterbox licence
comes from the chatterbox-tts 0.1.7 wheel, librosa's from its 0.11.0 wheel, and
Real-Time-Voice-Cloning's from its repository at commit
`865eba8826bb6d76721a9d828a546724779c23ab`, the last change to that file.

No upstream `NOTICE` file was found in those three source trees during this audit.
Preserve newly supplied upstream notices if updating these components.

## Optional runtime packages

Python packages are installed separately, not copied into this repository.
Core dependencies include NumPy (BSD-3-Clause), SciPy (BSD-3-Clause), SoundFile
(BSD-3-Clause, with libsndfile LGPL-2.1-or-later), and pyloudnorm (MIT). The desktop
shell uses pywebview (BSD-3-Clause), Python.NET (MIT), and Microsoft Edge WebView2
(Microsoft terms). They retain their own dependency licenses. This repository
does not redistribute Python, FFmpeg, WebView2 or a frozen executable.

`tools/install_kokoro.py` installs the 89 packages pinned in `requirements/kokoro-cpu.txt`
(PyTorch, Transformers, spaCy and their dependencies) into `.venv`. Nearly all are MIT, BSD
or Apache. phonemizer-fork and the espeak-ng library inside espeakng-loader are GPL-3.0,
num2words is LGPL, and certifi and tqdm are MPL-2.0. They are fetched from PyPI and GitHub
on the user's computer, not copied into this repository.

`tools/install_phonon.py` installs the one package pinned in `requirements/phonon.txt` into `.venv`: the `ptts` runtime
(MIT OR Apache-2.0), a Rust extension that needs only NumPy. It is fetched from PyPI on the user's computer, not copied
into this repository.

`tools/install_chatterbox.py` installs the 73 packages pinned in `requirements/chatterbox-cpu.txt` into an environment of
its own, `.venv-chatterbox`: `chatterbox-tts` (MIT, Resemble AI, with S3Gen decoder files that keep Apache-2.0 headers from
CosyVoice), its Perth watermarker `resemble-perth` (MIT), PyTorch 2.6 for the CPU, Transformers, librosa and their
dependencies. Nearly all are MIT, BSD or Apache. soxr and the libsndfile library inside SoundFile are LGPL-2.1, and certifi
and tqdm are MPL-2.0. pykakasi (GPL-3.0), which `chatterbox-tts` lists for its multilingual model, is not installed. They
are fetched from PyPI on the user's computer, not copied into this repository.

FFmpeg is an optional external tool. Its obligations depend on its exact build
and enabled components; do not bundle an arbitrary executable with this project.

## Optional models and outputs

| Model family | Weight terms and release implications |
|---|---|
| Kokoro 82M | [Apache-2.0 model](https://huggingface.co/hexgrad/Kokoro-82M). Preserve model/voice notices when redistributing weights. |
| Phonon 31M English | [Apache-2.0 model card](https://huggingface.co/gradium/phonon-31m-en) for the weights, tokenizer and default voice. The 37 preset voices come from [gradium/phonon-voices](https://huggingface.co/gradium/phonon-voices), which states **no** licence (no model card and no LICENSE file), so nothing establishes what audio made with them may be used for: ask Gradium before you publish it. The card asks that Phonon not be used to impersonate a real person or to deceive listeners about who is speaking, and that listeners be told when speech is synthetic where that matters to them. Gradium's larger 40M models are CC BY-NC 4.0 and are not integrated. |
| Qwen3-TTS | [Apache-2.0 checkpoints](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base). faster-Qwen is a separately installed runtime. |
| Chatterbox Original/Turbo/VC | [MIT upstream](https://github.com/resemble-ai/chatterbox). The Turbo weights are MIT by [their model card](https://huggingface.co/ResembleAI/chatterbox-turbo/blob/749d1c1a46eb10492095d68fbcf55691ccf137cd/README.md) at the pinned revision, which asks that the model not be used to do harm. Every clip carries Resemble AI's Perth watermark: keep the native behavior; no removal is implemented. |
| VoxCPM2 | [Apache-2.0 checkpoint](https://huggingface.co/openbmb/VoxCPM2). Runtime source and audio frontend components remain separate installations. |
| Breeze TTS 2 and derived checkpoints | [BreezeBlue Research and Non-Commercial License 1.1](https://huggingface.co/BreezeBlue/Breeze-TTS-2/blob/main/LICENSE), including self-hosted output restrictions. Its Apache source license does **not** make the weights or those outputs commercially unrestricted. |
| Drama 3, including Q4 conversion | [Fish Audio Community License 1.0](https://huggingface.co/fishaudio/drama-3/blob/e0a9218aa48888270f06ef1ed9ea67622c0e17d2/LICENSE). Commercial use is conditional, including combined affiliate revenue below USD 500,000; hosted speech/voice-cloning services need a separate agreement. Preserve “Built with Fish Audio” attribution and derivative notices. The model is not OSI open source. |

Weights, native runtimes, recordings, training data and generated samples are
not part of this repository. The Drama installer obtains the pinned
third-party runtime, model and their notices separately after explicit invocation, and
the Kokoro installer does the same for the pinned Kokoro checkpoint, its model card and its voices, the Phonon installer
for the pinned Phonon model and the 37 preset voices with their catalog, and the Chatterbox installer for the pinned
Chatterbox Turbo checkpoint and its model card.
The complete upstream license governs; this table is a practical inventory.
Reference consent, copyright, privacy and publicity rights still need review for
each voice and intended use. Converting a restricted voice through another model
does not establish new rights. See [Responsible use](docs/RESPONSIBLE_USE.md).
