# Fish Audio Drama 3

Drama 3 is integrated as `drama-3` in Fel's Voice Lab. Built with Fish Audio.

The official model has a 35B-parameter MoE backbone with approximately 3B active parameters per token. Its official SGLang/vLLM routes target larger CUDA cards. The Lab uses the community **speech.cpp 0.8.10 Windows Vulkan** runtime and the **Q4_K** conversion, which leaves the Python environments alone.

## Installed components

| Component | Pin |
|---|---|
| Original model | `fishaudio/drama-3`, source revision `e0a9218aa48888270f06ef1ed9ea67622c0e17d2` |
| GGUF conversion | `sakasegawa/drama-3-GGUF`, revision `9a01354d04ab6eb521bc9cdfc56ab6824c474af2` |
| File | `Drama-35B-A3B-v3-Q4_K.gguf`, 20,787,264,352 bytes |
| Model SHA-256 | `2a57b14c9b208a2ff833ab7305b920ca8f154ad807004a52d96a86a90a55523e` |
| Native runtime | `nyosegawa/speech.cpp` v0.8.10, Windows x64 Vulkan |
| Runtime archive SHA-256 | `2904dd54de7f18cf438338c28216ebffc205d29c6a2de3e645af40dd136b92f2` |
| Compute device | Experimental design only. On the development machine `Vulkan1` was the AMD Radeon AI PRO R9700 and `Vulkan0` its integrated 780M; the numbering differs from machine to machine. The default is CPU. |

Weights, portable executable/DLLs, license, NOTICE and installation receipt live under ignored `out/drama3/`. Preserve that directory when moving/backing up the installation.

To install, or to resume an interrupted download, press **Install** on its card in **Models & features**, or run this in the
Fel's Voice Lab folder ([SETUP.md](SETUP.md#drama-3-on-the-cpu) says what it does):

```powershell
setup-drama3.bat
```

The installer states the licence and its plan and asks first. It verifies the runtime archive and complete model hashes before
writing `out/drama3/installation.json`, then speaks a check sentence on the CPU. Downloads resume through the 32 MB pieces
already done. Partial download files never count as an installed model. The runtime also verifies the complete model hash before first load in each worker; cold start includes this disk read. Inference uses explicit local paths with update checks disabled.

## GPU stability policy (2026-10-18)

The first reference-conditioned Vulkan synthesis coincided with a Windows `0x116 VIDEO_TDR_FAILURE` reboot. The native log reported `device lost on Vulkan1` after successful CPU reference encoding. The exact driver/kernel cause is not established. **All cloning, including prepared references, now runs on CPU** in both applications. A mode switch unloads the previous native worker before loading the other device. Reference-free design can still run on a Vulkan GPU as an experimental opt-in (`drama_design_device` in the ignored `runtime.local.json`). The default is CPU, and GPU cloning remains disabled. CPU cloning needs at least 26 GB free system RAM and can take several minutes. There is no UI option to re-enable GPU cloning. No graphics driver or Windows timeout setting was changed.

## Character-production studio

The new cast, acting, long-form, streaming audition and 83-label language picker are documented in [DRAMA3_EXPANSION.md](DRAMA3_EXPANSION.md). All production studio jobs run on CPU and unload afterward. Previous-take editing remains unavailable in the pinned native backend.

## Fel's Voice Lab

Choose **Fish Audio Drama 3 (35B MoE / Q4_K)** in Model studio. Select design or clone. Cloning requires both a reference and its exact transcript. Cast identities use the same immutable-reference, Perform and scene/batch workflows as other cloning engines; their configured state-root directories reach the model worker.

Controls are seed, temperature, top-k, top-p and maximum seconds per chunk. Seed zero is valid. Voice descriptions and delivery cues become explicit bracket cues before the dialogue. They are experimental steering, not guaranteed separate instruction channels: a take may ignore or speak the cue. Check words and delivery before accepting it.

Examples:

```text
[sighs] Fine. We will wait here.
<|speaker:0|> The gate is closed. <|speaker:1|> Then we wait for sunrise.
```

The model supports up to 16 speaker indices. Unreferenced indices use model-selected voices. The original model panel exposes one cloning reference at a time; the new Drama production studio maps up to 16 saved reference voices to speaker slots. The native runtime supports encoded multi-speaker voice files through its CLI/shared wrapper.

Language follows the written text. Selecting a language does not translate text, force an accent or reach the runtime. Model studio accepts multilingual text. The scene picker lists the installed model's 83 language labels plus Auto, and the upstream model advertises 83 languages. The [language screen](LANGUAGE_MATRIX.md) checks the text in 12 of them: nine understood, Japanese partial, Korean not understood, Hindi inconclusive. The other 71 labels were never screened.

The native worker emits incremental PCM. The new Lab Drama production studio plays incremental PCM previews and retains the final WAV. The original simple generation endpoints still return complete WAVs. The selected speech.cpp worker does **not expose Fish's previous-take editing API**. No CFG, speed, repetition-penalty, inference-step or training control is advertised for this adapter. Reaching a duration/model cap is reported as a failed generation, rather than saving a truncated take as complete.

## Ownership and GPU release

The existing Lab residency lease remains in use. The Python adapter owns the native process in a Windows kill-on-close job. Unloading, cancellation, timeout or termination of that Python worker releases its native process and VRAM. Cached reference codes stay on disk. No model is preloaded at application startup.

## Verification

Automated contracts and Windows native-child termination are covered by `tests/test_drama3.py`; actual studio request construction is covered by `tests/ui_workspace.test.cjs`. `tools/verify_drama3.py` checks real design, speaker tags, event tags, isolated Cast HTTP cloning, saved provenance, source immutability and unload.
## License and references

The installed model and conversion include the **Fish Audio Community License 1.0** and derivative NOTICE. Commercial use has revenue and service conditions; the picker/receipts retain the attribution and conditions. Keep the actual license with any redistributed materials and review its full terms for the intended use.

- [Official Drama 3 model](https://huggingface.co/fishaudio/drama-3)
- [Official vLLM requirements](https://recipes.vllm.ai/fishaudio/drama-3)
- [Pinned GGUF conversion and attribution](https://huggingface.co/sakasegawa/drama-3-GGUF/tree/9a01354d04ab6eb521bc9cdfc56ab6824c474af2)
- [Pinned native runtime documentation](https://github.com/nyosegawa/speech.cpp/blob/v0.8.10/docs/models/drama3.md)
