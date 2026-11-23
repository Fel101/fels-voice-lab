# Fel's Voice Slate clips in Cast

A kept clip from [Fel's Voice Slate](https://github.com/Fel101/fels-voice-slate) (`<speaker>\<name>.wav`, 24 kHz mono, with
its `<name>.json` sidecar and the untouched original in `masters\`) can become a voice recording in Cast with its
transcript, origin and allowed use intact. It arrives as a candidate: nothing is approved, and nothing is imported until you
press **Import as candidate**. The clip format is in Voice Slate's `docs\clip-format.md`.

There are three ways in:

| From | How | When |
|---|---|---|
| **Send to Voice Lab** in Voice Slate | The clip waits in Voice Lab's inbox, at the top of **Cast library** | Voice Slate 1.0 and later, with Voice Lab installed |
| **Save for Voice Lab** in Voice Slate | You pick the saved file in **Cast library → Import from Voice Slate** | Voice Slate 1.0 and later |
| The command line | `recorder_import.py` reads Voice Slate's folder, and can compare a clip with its master | Any version of Voice Slate |

Voice Slate 0.7 and earlier have neither action: use the command line.

## Send to Voice Lab: the inbox

In Voice Slate, open a kept clip and choose **Send to Voice Lab**. Voice Slate writes one `.voiceslate` file into Voice
Lab's inbox, and Voice Lab need not be running. Voice Slate offers the action only when the inbox exists, which Voice Lab
makes the first time it starts; otherwise Voice Slate offers **Save for Voice Lab** instead.

In Voice Lab, **Cast library** shows "1 clip from Voice Slate" at the top, and the **Cast library** link in the menu shows
how many wait. The page looks when it gets focus and every ten seconds while it is shown, so a clip you just sent appears
within ten seconds of switching back to Voice Lab. Each clip shows its speaker, length, when it was sent and its
transcript.

- **Preview** opens the form under the clip: the file, its transcript, origin, allowed use and any warnings, a character
  name (the speaker's, to start with; a new name creates the character) and the model to clone with. If the transcript is
  the speech recognizer's unchecked draft, you have to tick that you understand before **Import as candidate**.
- **Import as candidate** adds the recording to that character's voice as a candidate. Review it and approve it in Cast.
  The file then goes to the Recycle Bin.
- **Dismiss** sends the file to the Recycle Bin without importing it.

A file that fails a check stays in the list with the reason and can only be dismissed: another kind of file, one larger
than a clip can be, or a clip whose check fails (a changed WAV, a missing consent statement). Keep the clip again in Voice
Slate and send it once more.

The inbox is `inbox\voice-lab\` in the folder of Fel's Studio, `%LOCALAPPDATA%\Fels\` unless `FELS_SUITE_DIR` names
another ([Connect other apps](CONNECT.md#the-other-apps-of-fels-studio)). You never need to open it: the page lists what is
in it. A `.voiceslate` file you copy there yourself is treated the same way. Programs can
use the same list through `GET /api/inbox`, `POST /api/inbox/{inbox_file}/import` and `POST /api/inbox/{inbox_file}/dismiss`
([API](API.md#get-api-inbox)).

## Save for Voice Lab: the file box

Open a kept clip in Voice Slate and choose **Save for Voice Lab** from the clip actions. This creates one `.voiceslate` file
with the exact kept WAV and exact sidecar, wherever you save it. In Voice Lab, open **Cast library → Import from Voice
Slate** and pick the file. The same form opens as for a clip in the inbox. The file you picked stays where it is.

Both ways keep the speaker's master and any release document in Voice Slate. The file keeps the release pointer from the
sidecar, and the comparison with the master is not run: import the Voice Slate folder from the command line for that.

The `.voiceslate` file is an uncompressed ZIP with two fixed member names, `clip.wav` and `clip.json`. The reader checks the
sidecar, the WAV's hash, the consent statement and the chosen model's needs before anything reaches Cast.

Inspired by [VoiceStudio's portable voices](https://github.com/debpalash/VoiceStudio/blob/main/docs/persona-format.md) by
Palash Debnath (`debpalash`) and the VoiceStudio contributors. This file preserves Voice Slate’s existing records and uses
Voice Lab’s own recorder bridge.

## Use it from the command line

Run from this folder. Voice Slate keeps its clips in `~\Recordings\voice-clips` (since Voice Slate 0.5), one folder per
speaker, unless its Settings name another folder. A copy older than 0.5 kept them in `voice-clips` beside the program, and
a copy that still has recordings there keeps using it.

```powershell
# 1. List and check a speaker's clips. Writes nothing.
.venv\Scripts\python.exe recorder_import.py $HOME\Recordings\voice-clips\my-voice

# 2. Also compare each clip with its master (local encoder, no download).
.venv\Scripts\python.exe recorder_import.py $HOME\Recordings\voice-clips\my-voice --check-identity

# 3. Import one clip as a candidate voice for a Cast character (--create-character if "Narrator" does not exist yet).
.venv\Scripts\python.exe recorder_import.py $HOME\Recordings\voice-clips\my-voice --clip 3f9a1c2e --register --character "Narrator" --create-character
```

`--register` also runs the identity check and stores it. Other options: `--engine` (default `turbo`), `--label`, `--all`
(every valid clip in a folder), `--accept-asr-draft`, `--no-identity-check`, `--against clean.wav`, `--min-similarity 0.90`,
`--json`. Open Cast afterwards, audition the new reference and approve it there: the import never approves anything.

## What is checked before anything is copied

The sidecar's schema starts with `voice-recorder-clip-` (v1 and v2; the name comes from Voice Slate's first name and
stays the same); `consent_confirmed` is exactly `true`; it has a
transcript, origin and allowed use; `export.file` is a plain file name and `master.file` a relative path, both inside the
sidecar's folder (links and junctions out of it are caught); the WAV's SHA-256 equals `export.sha256`; the WAV is the format
the sidecar declares. A transcript Voice Slate marks `asr_draft` (the speech recognizer's draft, nobody corrected it) is
refused unless you pass `--accept-asr-draft`. Also refused, before the first write: a clip of 5 s or less for `turbo`, text
longer than the Cast store keeps, a silent clip. The WAV is read once, hashed, and those same bytes are what get copied.
`consent_confirmed` is the speaker's own statement in Voice Slate, not a verified release.

## Where it ends up

`CastStore.add_reference` does the copying, so the clip is in `data/cast/media/references/<reference id>/`, a folder the
workers already accept; Voice Slate's folder is not added to any allowlist and nothing in it changes. The master is never
copied. The reference keeps the transcript, the allowed use verbatim, and the origin text followed by a note with
Voice Slate's clip id (`[Voice Slate clip 3f9a1c2e; speaker my-voice; mic; kept ...; sha256 ...]`). Takes made from it
carry that origin and allowed use in their provenance. Beside the audio:

| File | What it is |
|---|---|
| `source.wav`, `reference.wav` | The kept clip as imported, and the Cast reference made from it (same samples). |
| `recorder-clip.json` | Voice Slate's sidecar, byte for byte. |
| `recorder-import.json` | Where it came from, hashes, transcript status, consent flag, noise-reduction settings, warnings. |
| `identity-check-<time>-<nn>.json` | One file per identity check. Never overwritten, so re-checking keeps the history. |

Importing the same audio with the same transcript, origin and allowed use again changes nothing. If one of those changed (a
transcript corrected in Voice Slate), the clip becomes a new reference version and the old one stays. Clips from one speaker
join one voice per engine, named `Voice Slate: <speaker>`.

## The identity check

Voice Slate cannot tell whether noise reduction changed the voice (`identity_checked: false`). `recorder_identity.py`
compares the speaker embedding of the kept clip with that of its master, using the encoder `speaker_similarity.py` already
loads (cosine similarity, CPU, local weights; nothing is downloaded). A score below the threshold (default **0.90**) is
flagged, and the report says whether noise reduction was on. Two things make the comparison fair: the encoder reads
unnormalised levels, so both sides are brought to the same speech level first (a 20 dB gain change alone otherwise scores
0.75 to 0.81), and the master is cut where the sidecar says the clip was cut, so both hold the same material.

**What the score means.** It is a *change* check on Voice Slate's processing, with no TTS model in it. It is not a damage
check: removing noise changes the embedding of a noisy recording even when the result is closer to the real voice
(an earlier design study of the denoiser found this and dropped the same comparison as a pass/fail gate). A flag means listen to the
master and the clip. `--against <clean recording of the same voice>` adds which way it moved: closer to that voice, about the
same, or further away.

**How it differs from cloning the clip and comparing the clone** (`speaker_similarity.voice_match` on a take). That number
mixes the reference's quality, what Voice Slate did to it, and the TTS model's own drift: clones of one voice scored 0.77 to
0.97 when the bands were measured. This check scores the same recording before and after Voice Slate's chain, so a low value
points at the clip, not the engine. Use it first, to decide whether a clip is worth cloning from; use the clone comparison
afterwards, to judge an engine. The two bands (0.847 and 0.796, in `catalog/speaker_similarity.json`) answer "same voice or
another?" for clones of one target and are not used here: a clip can stay "likely the same voice" at 0.85 and still sound
different.

**Measured** (five Kokoro speech clips with synthetic noise; levels matched; not your recordings):

| What happened to the master | Score |
|---|---|
| High-pass 80 Hz, gain, limiter, resample | 0.94 to 0.99 |
| Hum notch at 60 Hz and two harmonics | about 0.95 |
| High-pass at 300 Hz (Voice Slate's maximum) | 0.81 to 0.88 |
| `afftdn` noise floor -80 to -60 dB | 0.94 to 0.97 |
| `afftdn` -50 dB | 0.90 to 0.95 |
| `afftdn` -40 dB | 0.83 to 0.88 |
| `afftdn` -30 to -20 dB (Voice Slate's default is -25) | 0.79 to 0.88 |
| `afftdn` -20 dB with `nr=60` (over-denoised, not offered by Voice Slate) | 0.57 to 0.69 |
| A master of a different voice | 0.69 to 0.87 |

So Voice Slate's default noise floor already moves the voice more than anything else in its chain; the setting is an
absolute level applied before Voice Slate's gain, so a quiet master is hit harder. If a clip is flagged and its master was
already clean (the report gives the speech-to-noise gap), re-keep it without noise reduction.

The check says "not checked" instead of guessing when the master is missing or unreadable, silent, or the encoder weights are
absent. A master that disagrees with its sidecar (rate, channels, length) is flagged: it may not belong to the clip. The
sidecar has no hash for the master, so the report records the master's SHA-256 at check time.

## Tests

`tests\test_recorder_import.py`, `tests\test_recorder_identity.py`, `tests\test_slate_bundle.py` and
`tests\test_inbox.py` use generated audio only. `test_inbox.py` makes its `.voiceslate` file by hand from the suite
contract's rules and hands it over as Voice Slate will; the page's own test is in `tests\test_browser_flow.py`. The identity tests need the
Chatterbox Turbo encoder weights (`setup-chatterbox.bat` fetches them); the ones that clean a clip also need FFmpeg (found as `denoise_ref.find_ffmpeg()` does). They skip
what is missing and say so.
