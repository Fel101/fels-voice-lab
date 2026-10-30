# Voice presets, references and redistribution

Reviewed 21 October 2026, with Phonon added on 30 October 2026. This distinguishes the application's MIT source code
from the model files and audio used with it. A voice appearing in a local picker
does not mean it is cleared for a public download or a commercial project.

## Kokoro's fixed voices and blends

The fixed-voice list discovers cached `.pt` speaker/style tensors from
`hexgrad/Kokoro-82M`. These are upstream presets, not recordings supplied by the
application's author. The upstream model is Apache-2.0 and explicitly supports
commercial deployment. See the [model card](https://huggingface.co/hexgrad/Kokoro-82M)
and [voice catalogue](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md).

Using those preset names in the application is appropriate. Redistributing the
actual model or voice tensors is permitted under Apache-2.0 with its conditions:
include the license, preserve relevant notices, mark modified files and retain
any applicable upstream NOTICE. Keep the model card and its training-data
attributions with a separately distributed model pack. The application does not
own those upstream assets or receive exclusive rights to the voice identities.
The [Apache license](https://www.apache.org/licenses/LICENSE-2.0), especially
sections 4, 6 and 7, covers redistribution, trademarks and warranty limits.

An equal blend averages selected Kokoro tensors; it does not create a new
unrestricted license or establish exclusive ownership. Kokoro does not turn a
WAV upload into a new native `.pt` preset. New recorded or designed voices belong
in the reference library and are used by a model that supports cloning.

This repository contains no model weights and no voice audio. Keeping names and
integration code in it is different from bundling a model pack or an audio
demonstration.

## Phonon's preset voices

The 37 English preset voices come from `voices.jsonl` and its voice files in the pinned `gradium/phonon-voices` snapshot. Like
Kokoro's, they are upstream presets, not recordings supplied by the application's author. The model repository,
`gradium/phonon-31m-en`, states Apache-2.0 for its weights, tokenizer and default voice. **The voices repository states no
licence at all: it has no model card and no LICENSE file.** Nothing in either repository says what audio made with those voices
may be used for, so treat that as unknown: ask Gradium before you publish a recording that uses one, and do not redistribute the
voice files with your project. The model card also asks that Phonon not be used to impersonate a real person, and that listeners
be told when speech is synthetic where that matters to them.

## Where the picker finds audio

The reference picker looks in three places and labels each one, because where a file sits is not evidence of where it came
from:

| Source | What it means | Publication decision |
|---|---|---|
| Local reference folder (`reference/`) | Files you put there. It includes `af_heart.wav`, a synthetic Kokoro test reference that `engines.ensure_reference()` can generate from `af_heart` and the application's test text, and then reuses. That WAV has no complete per-file generation receipt, and its name is not evidence of a recorded person's permission. | Review a file's generation record and planned use before choosing it as a public demo. The folder is not part of this repository (Git ignores `reference/`). |
| Saved designed voice (`data/voices/`) | Audio written when you save a designed voice. It is generated under the engine's own terms, and Breeze's are non-commercial. | Keep the engine's restrictions with the file, and do not present saved designs as unrestricted starter voices. |
| Cast reference | A saved recording or generated audio with versioned origin and allowed-use notes. A prepared copy and its original source can appear separately. | Follow the saved terms for that exact version and its source chain. A human approval records a decision; it does not prove ownership. |
| Any other file | A filename or containing directory alone does not establish an engine, a speaker's permission or an allowed use. | Show unknown provenance until a matching generation record or rights record establishes it. |

Audit what your own library holds before you share any of it; this repository includes none of it, and no private speaker
names, recordings or consent documents.

The [Qwen VoiceDesign checkpoint](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign)
and [VoxCPM2 checkpoint](https://huggingface.co/openbmb/VoxCPM2) identify their
weight license as Apache-2.0. That does not clear an independently supplied
reference recording or a recognizable person's identity.

## Restrictions that follow generated voices

Breeze's self-hosted weights and outputs have research/non-commercial terms.
Commercial use needs a separate written license. Non-commercial illustrative
samples are allowed with acknowledgement and compliance with the license; that
permission does not override a particular recording's narrower saved terms.
See the [Breeze license, sections 3–5](https://huggingface.co/BreezeBlue/Breeze-TTS-2/blob/main/LICENSE).

Fish Drama 3 permits personal testing and conditional commercial use. Its
community commercial permission uses combined affiliate revenue below
USD 500,000; specified hosted services require a separate agreement. Preserve
the applicable Fish attribution and notices. Distributing application source
for people to run locally is different from operating a self-service hosted
voice-generation service. See the [Fish Community License 1.0](https://huggingface.co/fishaudio/drama-3/blob/main/LICENSE).

Cloning a restricted reference through Qwen, Fish or another engine does not
erase its original restrictions. A newly generated voice can still resemble a
real person; model permission alone does not resolve recording, privacy,
publicity or impersonation rights.

## Choosing references in the application

A useful voice browser should show a human-readable name, recorded/generated
source, engine, exact transcript when required, duration, version, origin,
allowed-use notes and an audio preview. Imported audio should be copied into
managed local storage with the user's rights notes, rather than allowing an
arbitrary path to become an approved voice. Unknown rights should stay visible
as unknown. Do not relabel imported or generated voices as MIT merely because
the application code is MIT.

For a public starter collection, create a separate reviewed manifest containing
each asset's hash, generation model/revision, prompt or source recording,
transcript, source-chain terms and intended distribution permission. Keep private
consent evidence local.

See also [responsible use](RESPONSIBLE_USE.md) and the
[third-party notices](../THIRD_PARTY_NOTICES.md). This is a practical provenance
review, not a guarantee about every jurisdiction or a disputed person's rights.
