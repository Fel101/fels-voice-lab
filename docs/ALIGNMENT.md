# Word timing and alignment

Status: 23 October 2026. Word times are a derived record stored beside each Cast take. They support
captions and review. They are not a verified timing standard yet (see Limits).

## What exists

- `align_takes.py` aligns Cast takes that have no record (`--all`, `--take-id`, and `--redo` to re-align).
  It runs `align_worker.py` once for the whole batch in the isolated `.venv-asr` environment and stores the
  record in `takes.word_timings_json` (Cast schema 6). It writes nothing else. Take audio is never changed.
- Scene export writes `scene.srt` and `scene.vtt` with word phrases wherever every take of a line has word
  times. A line without them keeps one line caption. The manifest records the caption cues the files were
  built from, and the verifier checks those cues.
- `fit.py` is a bounded time-stretch (librosa phase vocoder, 15 % either way by default). It returns one of
  three statuses: `within_slot`, `stretched` (listen before accepting), or `needs_rerender`. The scene export
  records each line's status against its target duration. It does not stretch the audio. librosa is optional and
  only the stretch needs it; the status check that the exporter uses runs without it.

## Edited takes

A take made by editing another take (a trim, a cut of the silence, fades, a gain; see the guide) gets its word record from
its original, not from the aligner, so it needs no run. `derive_word_record` in `take_edit.py` moves every word earlier by
the start of the kept part and cuts it at the new ends. A word the trim leaves less than 0.04 s of, and less than half of, is
dropped, and the record's `text` then follows the words that are left. Fades and gain change no time. `audio_sha256` and
`duration_seconds` are those of the new audio, and a `derived_from` block names the original take, the kept part and how many
words were dropped or shortened. The times are as approximate as the original's.

An original that was never aligned, or whose alignment failed, gives an edit with no record, and `align_takes.py` finds it like
any other take without one. It aligns the take's full spoken text, though, so an edit that cut words away from such a take
fits badly: the checks will flag it. `--take-id ID --redo` aligns an edit's own audio instead of keeping the moved times.

## Choosing the aligner (licence check, 23 October 2026)

| Candidate | Code licence | Weights licence | Verdict |
|---|---|---|---|
| faster-whisper cross-attention, local `tiny` model | MIT (the installed package's METADATA says `License: MIT`) | Whisper weights are MIT per OpenAI's release; confirm the model card before redistributing model files | **Chosen.** Offline, multilingual, and the weights are already on disk, so nothing is downloaded. |
| stable-ts `align()` | MIT (summary of the package) | Whisper weights, as above | Not used. Same method as the chosen one, with a second package to maintain. |
| torchaudio `MMS_FA` (Meta MMS) | BSD-2-Clause (torchaudio 2.11.0, read from its licence file) | CC-BY-NC 4.0 (torchaudio documentation and Hugging Face ports) | Excluded: non-commercial weights. |
| WhisperX | BSD-2 (third-party summary; confirm the LICENSE file) | Default alignment models are per-language wav2vec2 checkpoints whose licences differ | Not adopted without a licence check for each model. |
| facebook `wav2vec2-base-960h` | Not checked for this model; torchaudio's own code is BSD-2-Clause | Apache-2.0 (Hugging Face card) | Candidate for a stronger English aligner later. English only; needs a download approval. |
| Montreal Forced Aligner | MIT per an aggregator (confirm the LICENSE file) | Several pretrained acoustic models are CC BY 4.0, which requires attribution; check each language | Candidate for later. Heavy to install on Windows, and each model needs its own licence check; needs a download approval. |

The chosen aligner is the smallest Whisper model. Its word boundaries are approximate. A stronger aligner is the
next step if lip-sync or phoneme timing is needed, and it needs the download approval noted above.

## How a take is aligned

The transcript the engine was given is teacher-forced against the take's audio. Only markup the engine acts on
instead of saying is removed first: `<|speaker:N|>` markers for every engine, `[laugh]`-style tags for Chatterbox
Turbo and Drama, and `(laugh)`-style tags for Breeze. Any other word in the text, including words in brackets or
parentheses, is spoken by the other engines, so it stays in the transcript. Whisper's cross-attention places each
word, to the 20 ms frame. Then:

- Punctuation joins the word before it and gets no time of its own.
- If the first word starts before the speech, its start moves to the speech onset; likewise the last word's end.
  Speech is any 10 ms frame within 30 dB of the clip's loudest frame. A clip quieter than -60 dBFS has no speech.
- Clips longer than 30 s are refused. Each clip is an error entry, and the batch continues.

## Checks

A take is marked `review` when any of these holds, and `ok` otherwise:

- its first word starts more than 0.3 s from the speech onset (after edge snapping this is 0 by construction);
- a word is silent across its whole span (every frame more than 30 dB below the loudest frame);
- the words span less than 70 % or more than 130 % of the detected speech.

These checks catch gross errors. They do not measure boundary accuracy.

## What was measured on the local Cast takes (23 October 2026)

- 150 takes were aligned in one batch in 54 seconds, with no errors.
- An earlier rule counted a word as silent when its midpoint was quiet. That flagged 70 audible takes, because
  plosive closures and quiet vowels have quiet midpoints. The rule now asks whether the whole span is silent.
  All 150 takes then pass the checks.
- Word durations: median 260 ms; 1.1 % of words are under 40 ms.
- Word probabilities: median 0.96. Some function words score near zero, which is a weak point of this model.
- **Not measured:** boundary accuracy. There are no ground-truth timings here. A proxy based on energy dips gave a
  median distance of 70 ms. A random point in the search window would give about 50 ms, so that proxy does not
  show that the boundaries are accurate. Treat word times as approximate until a person listens or a reference
  is available.

## Limits

- Not a checked basis for lip-sync or phoneme timing. The lip-sync tracks ([LIPSYNC.md](LIPSYNC.md)) share these
  word times over phonemes, and their accuracy against real speech is unmeasured.
- Only the app's own language labels map to Whisper codes (`word_timing.WHISPER_CODE`). A take in another
  language gets an error record and no word times. Drama's 83 labels are mostly not mapped yet.
- Single-word re-rendering is not implemented. The word times are the data it would need.
- The fit status is advisory in exports. A stretched line is not stretched in the deliverables until that is
  added and listened to, and phase-vocoder stretches can smear consonants or shift timbre.
- Chinese and Japanese words come out as groups of characters, so cue text is only as fine as the tokenizer.

## Reproduce

```bash
.venv-asr\Scripts\python.exe -I align_worker.py --model-dir hf-cache\faster-whisper-tiny-d90ca5fe --manifest <items.json>
.venv\Scripts\python.exe align_takes.py --all
```

The first command aligns any clips listed in a manifest. The second aligns the Cast takes that have no record.
