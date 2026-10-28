# Third-party notices

Reviewed 20 October 2026. The root MIT license covers the project's original code.
It does not replace the licenses below or grant rights to model weights, voices,
recordings, trademarks, scripts or generated outputs.

## Source included in this repository

| Local source | Origin and license | Retained notices / modifications |
|---|---|---|
| `breeze/loader.py`, `breeze/int8.py`, `breeze/native.py`, `breeze/runtime.py` | [Saganaki22/ComfyUI-Breeze-TTS-2](https://github.com/Saganaki22/ComfyUI-Breeze-TTS-2), Apache-2.0; Copyright 2026 saganaki22 | Local standalone loader/runtime integration; upstream notices remain in headers. `native.py` derives from BreezeBlue's model implementation and credits Sesame and The HuggingFace Inc. configuration code. |
| Breeze inference implementation underlying the preceding files; `breeze/vendor/t5gemma2_compat.py` | [breezeblue-ai/breeze-tts](https://github.com/breezeblue-ai/breeze-tts), Apache-2.0 | Inference-only adaptation, runtime compatibility and standalone serving. |
| `breeze/vendor/codec_config.py`, `breeze/vendor/codec_model.py` | [QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS), Apache-2.0 | Copyright 2026 The Qwen team, Alibaba Group and the HuggingFace Inc. team. Tokenizer subset; changed Transformers decorators and attention-mask arguments. |
| `fqwen_compat.py` | RoPE compatibility function adapted from qwen-tts-hf / Transformers; Apache-2.0 components | Reads both old `rope_theta` and newer `rope_parameters`. Copyright The Qwen team, Alibaba Group and The HuggingFace Inc. team. |

Full upstream license texts are retained under `licenses/`. The historical import
commit for the Breeze files was not recorded; this audit does not invent one.
License copies were verified against ComfyUI-Breeze commit
`3461ca6011b7e833147b2e7edb779da7a08c6259`, Breeze commit
`6eba901d1845353802652c708a4740fdfc5baa75` and Qwen commit
`022e286b98fbec7e1e916cb940cdf532cd9f488e`. These identify the notice sources,
not a claim that the local adaptations are identical to those revisions.

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

FFmpeg is an optional external tool. Its obligations depend on its exact build
and enabled components; do not bundle an arbitrary executable with this project.

## Optional models and outputs

| Model family | Weight terms and release implications |
|---|---|
| Kokoro 82M | [Apache-2.0 model](https://huggingface.co/hexgrad/Kokoro-82M). Preserve model/voice notices when redistributing weights. |
| Qwen3-TTS | [Apache-2.0 checkpoints](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base). faster-Qwen is a separately installed runtime. |
| Chatterbox Original/Turbo/VC | [MIT upstream](https://github.com/resemble-ai/chatterbox). Keep the native Perth watermark behavior; no removal is implemented. |
| VoxCPM2 | [Apache-2.0 checkpoint](https://huggingface.co/openbmb/VoxCPM2). Runtime source and audio frontend components remain separate installations. |
| Breeze TTS 2 and derived checkpoints | [BreezeBlue Research and Non-Commercial License 1.1](https://huggingface.co/BreezeBlue/Breeze-TTS-2/blob/main/LICENSE), including self-hosted output restrictions. Its Apache source license does **not** make the weights or those outputs commercially unrestricted. |
| Drama 3, including Q4 conversion | [Fish Audio Community License 1.0](https://huggingface.co/fishaudio/drama-3/blob/e0a9218aa48888270f06ef1ed9ea67622c0e17d2/LICENSE). Commercial use is conditional, including combined affiliate revenue below USD 500,000; hosted speech/voice-cloning services need a separate agreement. Preserve “Built with Fish Audio” attribution and derivative notices. The model is not OSI open source. |

Weights, native runtimes, recordings, training data and generated samples are
not part of this repository. The Drama installer obtains the pinned
third-party runtime, model and their notices separately after explicit invocation, and
the Kokoro installer does the same for the pinned Kokoro checkpoint, its model card and its voices.
The complete upstream license governs; this table is a practical inventory.
Reference consent, copyright, privacy and publicity rights still need review for
each voice and intended use. Converting a restricted voice through another model
does not establish new rights. See [Responsible use](docs/RESPONSIBLE_USE.md).
