# Drama 3 character-production expansion — 2026-10-18

Fel's Voice Lab now includes a **Drama scene & narration studio**. It uses the existing pinned model and runtime. No additional weights, Python packages, graphics drivers or Windows timeout settings were installed or changed.

## Use

- Fel's Voice Lab: choose Drama 3, then expand **Open Drama scene & narration studio**.
- Save and approve reference voices in the normal Cast/voice-library workflow. Refresh saved voices in the studio, assign speaker slots, and insert matching speaker tags in the script.
- Insert acting cues at the cursor, or write them directly. They apply from that position until another cue changes the delivery. Experimental cues may be ignored or spoken.
- Choose **Generate & audition** to hear incoming audio. **Stop take** cancels the owned worker. Closing the studio dialog stops local listening while the retained job continues; recent takes reconnect to it.
- Completed takes offer WAV, model/cast receipt and ZIP downloads. Recent takes restore the script, cast and controls for a fresh generation. They do not inject previous audio tokens.

Production studio generation runs on **CPU**, including reference-free long scripts. Experimental short Vulkan design is an opt-in local setting (`drama_design_device`); the default is CPU. GPU cloning remains disabled following the earlier device-loss/Windows 0x116 failure. The model unloads after each studio job.

## Implemented milestones

| Milestone | Behavior |
|---|---|
| Acting controls | Cue chips, custom cues and speaker-tag insertion; separate plain-word preview. Pronunciation substitutions preserve cues and speaker tags. Captions/word checks exclude those controls. Existing preset/direction controls remain available. |
| Joint cast | Up to 16 distinct speaker slots, each assigned an approved saved reference and exact transcript. Lab source IDs pin the reference version. Referenced scenes reject unassigned speaking slots unless the user explicitly allows generated voices. |
| Longer scripts | Up to 8,000 characters in one native request, retaining the runtime's automatic chunk/context handling. App output is capped at ten minutes; the runtime also enforces its own token/context limits. Large reference casts can require shorter scripts. The regular per-line endpoints retain their existing bounds. |
| Streaming audition | Incremental PCM preview while synthesis runs; final WAV matches the preview transport. Polling supports reconnect and bounded chunks rather than retaining an open HTTP connection. Stop works during worker loading/prefill as well as after audio starts. |
| Languages | All 83 language codes in the installed model's pinned metadata, plus Auto. The Lab scene language choices are expanded too. Language labels do not translate text, force accents or reach the runtime. The [language screen](LANGUAGE_MATRIX.md) checks text-level intelligibility for 12 of them; no language has been perceptually validated. |
| Retained jobs | Script/cast/settings, source checksums, model/runtime receipts, audio checksum, cue-free transcript and ZIP. An OS ownership lock prevents another instance resetting an active job. A replacement instance marks abandoned jobs interrupted and preserves their files. |
| Quality screening | The existing audition suite adds inline delivery changes, short event duration and same-speaker turns. Flags cover unusually long events and possible spoken instructions. Studio completion includes quiet/clipping/extended-event flags. Voice identity and cue adherence still require listening. |

Joint cast output is one mixed mono recording. It does not include isolated speaker stems, reliable per-speaker timestamps, or automatic attachment to individual game dialogue lines. Use the established line-by-line generation/alignment workflow when those deliverables are required. Completed studio ZIPs contain audio, script, plain transcript, receipts and attribution; they do not fabricate timings.

## Previous-take editing research

Fish's [official model examples](https://huggingface.co/fishaudio/drama-3#editing-as-continuation) and [SGLang cookbook](https://sgl-project.github.io/sglang-omni/cookbook/fishaudio_drama3.html#editing-as-continuation) describe retakes conditioned on previous context/VQ tokens. That capability is different from restoring the same script, seed and reference for another generation. It does not promise a sample-identical unchanged region or word-level audio splicing.

The pinned [speech.cpp worker protocol](https://github.com/nyosegawa/speech.cpp/blob/v0.8.10/docs/worker.md) and [Drama options](https://github.com/nyosegawa/speech.cpp/blob/v0.8.10/docs/models/drama3.md#options) expose neither previous-take context input nor a generated VQ-history artifact. Its internal continuity between chunks of a single request is not a previous-take editing API. The studio advertises `editing.supported=false` and provides no misleading edit button.

The next backend milestone would retain generated VQ tokens with codec/model/cast checksums, add an explicit history input to a pinned native runtime, bound the combined context, and compare chained retakes against the official examples. It needs an isolated native build and reference-behavior tests. Re-encoding a WAV as another cloning reference would not establish equivalent editing. Keep the current runtime as a fallback and validate on CPU before any AMD GPU experiment.

Moving to the official CUDA-oriented serving stack is not established as a compatible shortcut for the AMD setup this was developed on. Its published int4 path uses GPTQ-Marlin with NVIDIA compute capability 8.0+, while the FP8 weight footprint alone is larger than the 32 GB GPU used here. See the [official serving requirements](https://sgl-project.github.io/sglang-omni/cookbook/fishaudio_drama3.html#int4-checkpoint). No speculative backend installation was performed.

## APIs and storage

Fel's Voice Lab base: `/api/drama3`.

- `GET /info`: capabilities, approved source choices, recent jobs.
- `POST /jobs`: `text`, `cast:[{speaker,source_id}]`, `settings`, optional `language`, `description`, `direction`, `allow_unassigned`.
- `GET /jobs/ID?offset=BYTES`: retained job and the next base64 PCM16LE mono 44.1 kHz preview block. Offsets must be even and cannot exceed the available bytes.
- `POST /jobs/ID/cancel`: request cancellation; partial previews remain unaccepted.
- `GET /jobs/ID/audio`, `/metadata`, `/package`: completed, checksum-verified artifacts only.

Resolve source IDs from `/info`; callers do not supply arbitrary filesystem paths. Lab jobs live under the selected state root's `out/drama3/studio/`. These stores are local and ignored by Git.

## Verification and activation

Focused regressions cover inline markup and pronunciation, immutable inputs, source visibility, long request transport, actual HTTP routes, preview/WAV correspondence, completed-only exports, ownership across instances, load-time cancellation, UI request construction and streamed audition/stop behavior.
Python backend changes require a server restart. Refreshing an already running old server is insufficient. Complete any active generation before restarting; no production console is force-stopped by this update.
