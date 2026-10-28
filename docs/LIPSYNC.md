# Lip-sync tracks

Status: 23 October 2026. Experimental. A track is data for a game to play back. Nothing in the app plays one yet, and
no one has checked a track against a face or against listening.

## What a track is

Each scene line that has phoneme times gets one JSON file in the export package:

- `lipsync/<character folder>/<line>.json`. `manifest.json` lists it as an asset of type `lip_sync_track`, with its
  hash, so `verify` checks it like any other file and also checks its keys.
- `character-packs/<character folder>/character-pack.json` gives each line a `lip_sync` entry. Its `status` is
  `written` (with `timing_source` and `path`) or `not_written` (with a `reason`, such as a take with no word times, or
  an aligned take in a language with no phonemes here).
- The format is `voice-lab-lipsync-v1`: `duration_seconds`; `visemes` (the eleven mouth shapes below); `keys`, a list
  of `{start, end, viseme}` in seconds that runs in order from 0 to the duration with no gaps or overlaps; `timing`
  (where the times came from, below); and `checks.unmapped_symbols`.

Keys are hard cuts. A game should blend between them.

## Mouth shapes

| Mouth shape | Sounds |
|---|---|
| `rest` | no sound: gaps between sounds, spaces, stress and length marks |
| `closed` | p, b, m |
| `teeth` | f, v |
| `tongue_th` | th (θ, ð) |
| `tongue` | t, d, n, l, r |
| `sibilant` | s, z, sh, zh, ch, j |
| `velar` | k, g, ng, h |
| `open_wide` | ah (ɑ), a (æ), and the diphthongs aɪ and aʊ |
| `open_mid` | eh (ɛ), uh (ə), and the diphthong eɪ |
| `smile` | y (j), ee (i), ih (ɪ) |
| `round` | w, oh (oʊ), aw (ɔ), oo (u, ʊ) |

For aligned takes, each sound's share of its word's time follows a rough length: vowels 1.6, affricates 1.2, sonorants
1.0, fricatives 0.9, stops 0.7. Stress and length marks take no time.

## Where the times come from

- **Kokoro takes** store Kokoro's own phoneme times with the take, when it is made. They are the durations the model
  predicts, placed on its 25 ms frames. They are kept in the Cast database (`takes.phoneme_timings_json`, schema 7),
  not in the engine record. The export uses them as they are, in any language Kokoro speaks. Kokoro's English
  pipelines also give word times; its other pipelines give phoneme times only.
- **Other English takes** use their stored forced alignment (see [ALIGNMENT.md](ALIGNMENT.md)). Each word's time is
  shared out over its phonemes from misaki's English rules, which use the same symbols Kokoro does. The phoneme times
  inside a word are estimates. misaki comes with Kokoro and is not part of the core install: without it an aligned
  take gets no track (`lip_sync: not_written`, with the reason), and Kokoro takes are unaffected.
- **Takes made before schema 7** have no stored Kokoro times, so they keep the aligned track. Only takes made from now
  on store them.
- **Edited takes** (a trim, a cut of the silence, fades, a gain) keep their original's phoneme times, moved earlier by the
  start of the kept part and cut at its ends, so an edited Kokoro take still gets its own track. A sound the trim leaves
  almost nothing of is dropped. Fades and gain change no time. The times are the model's frame times moved by the trim,
  so they no longer fall on whole 25 ms frames.
- **A line that joins takes** places each take by its own source, one after another on one timeline. The track's
  `timing.source` is that source, or `mixed` when the takes differ.
- **Length and trims**: the track runs to the delivered line's length, the sum of its takes minus the trims. The start
  trim moves every key earlier and cuts what falls before 0.

## How Kokoro's stored times were checked

Three real Kokoro takes, made in a temporary Cast store on 23 October 2026, passed these checks: an English sentence
(4.0 s), a two-line English text (3.25 s) and a Spanish sentence (2.875 s).

- Each clip is a whole number of 600-sample frames at 24 kHz. Its last phoneme ends one frame before the clip ends,
  where Kokoro's end-of-sequence frame sits. The two-line text was split at its newline into two chunks, and the
  second chunk's times land on the right frame, so the chunk offsets are correct.
- Every stored phoneme time is a whole number of frames.
- The stored symbols are the phonemes Kokoro was given, in order, and the stored record reads back exactly. Each take's
  record is 1.7 to 3.8 KB.

These checks show that the times follow the audio Kokoro made. They do not show that the times match a human
speaker's mouth movement.

## How aligned times compare with Kokoro

The reference is Kokoro's own predicted durations (25 ms frames) for 40 English sentences from the Cast takes. This
measures the aligned path, which is used for takes without Kokoro's times. It shows how closely the aligner's word
times, shared over phonemes, agree with Kokoro's timing. The reference is not a human label, so it does not show
accuracy against a real speaker. The full record is in [evidence/timing-validation.json](evidence/timing-validation.json).

| Measure | Count | Median | 90th percentile |
|---|---|---|---|
| Word start, aligner against Kokoro | 341 | 35 ms | 90 ms |
| Word end, all words | 341 | 60 ms | 280 ms |
| Word end, excluding the final word | 301 | 50 ms | 105 ms |
| Phoneme start, shared out from aligned words | 1,145 | 39 ms | 98.5 ms |
| Phoneme end, shared out from aligned words | 1,145 | 37.6 ms | 98.7 ms |
| Phoneme start, shared out from Kokoro's words (control) | 1,145 | 12.5 ms | 78.4 ms |
| Phoneme end, shared out from Kokoro's words (control) | 1,145 | 7.2 ms | 81.7 ms |

No sentence was skipped, and every sentence's phonemes matched misaki's sequence. The control rows show that sharing a
word's time over its phonemes adds a median 12 ms of error even when the word boundaries are exact. Most of the phoneme
error comes from the aligner's word boundaries. The final word's end is the weakest boundary, so the figure that
excludes it is the better guide. A rerun on 23 October left the reference figures unchanged and moved the aligned-word
figures by a few milliseconds (phoneme start median 37.8 ms, against 39.3 ms in the published record).

Coverage: all 150 stored English Cast takes produce a track that passes `check_track`, with no refusals. That checks
the structure only.

To measure again:

```bash
.venv\Scripts\python.exe tools\timing_validation.py --limit 40 --publish
```

The tool speaks the sentences with Kokoro through the engine pool, aligns the audio in `.venv-asr`, and writes the
report beside the run. `--publish` also copies it to `docs/evidence/timing-validation.json`.

## Limits

- **Aligned takes are English only.** Their phonemes come from misaki's English rules. A take in another language gets
  a track only if Kokoro made it, because only then does it carry its own phoneme times.
- **Kokoro's times are the model's.** They say when the model places each sound, not when a speaker's mouth moves.
  The mouth shapes for Kokoro's non-English sounds are mapped from the full symbol set (a test covers the set) but
  have not been checked by eye or by ear.
- **Phonemes follow the dictionary and the model, not the audio.** A word said with a different vowel or an accent
  gets the shapes of the phonemes Kokoro was given.
- **Takes made before schema 7** have no stored Kokoro times. Takes from other engines always use the aligned path.
- **Word times are approximate** for aligned takes ([ALIGNMENT.md](ALIGNMENT.md)). Boundary accuracy against real
  speech is unmeasured.
- **Eleven shapes** with no jaw or lip height, no emphasis, no smoothing between sounds, and no eye or head motion.
- **No stretch in the tracks.** The fit stretch is not applied to deliverables, so a track matches the audio that is
  exported.
- **Words that do not line up** (numbers, symbols or abbreviations that the dictionary splits differently from the
  aligner) get no track when the take is aligned, with the reason "N aligned words but M phonetic words". None of the
  Cast takes hit this.

## Next steps

1. A stronger English aligner, if the word boundaries limit the aligned takes. ALIGNMENT.md lists the candidates and
   their licences; each needs a download approval.
2. Playback in the game: blend the shapes, add a rest pose, and check the result on the actual characters.
