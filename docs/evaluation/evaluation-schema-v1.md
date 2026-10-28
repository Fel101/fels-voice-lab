# Evaluation schema v1

Evaluation state is local to the selected service state root (`--state-root`). The SQLite store is `data/evaluations/evaluations.sqlite3` (ignored by Git); run WAVs and manifests are kept under `out/comparisons/<run-id>/` (also ignored). Back these up together while `serve.py` is stopped.

## Named suites

`evaluation_suites.py` exposes suite schema v1 at `GET /api/evaluations/suites`. The eleven entries include the Drama inline-acting/context showcase and cover neutral identity, four emotions, whisper/shout, native events, short barks, names/numbers, pronunciation traps, multilingual lines, paragraph continuity, and scene consistency. Each suite labels its scope as `common_capability` or `native_feature_demo`; a native event string or direction is not presumed to mean the same thing to every engine. Each case retains the exact text, language, direction, and note. A suite definition is a test recipe, not proof that an engine passed it.

The real runner currently executes `neutral-clone-three-line` only. It uses the same reference file and transcript for Breeze, Qwen Base, and Chatterbox Turbo. Breeze and Qwen receive recorded numeric seeds; Turbo has no effective seed field in this adapter. Numeric seed equality is not treated as equivalent randomness.

## Run manifests and artifacts

`results.json` has `schema_version: 1`, `run_id`, suite ID/version, checksum, reference path/transcript/hash, engine/backend/checkpoint identity as reported, exact conditions, timing/resource summaries, and one record per generated sample. The checksum is SHA-256 of canonical UTF-8 JSON with sorted keys and compact separators, excluding the `checksum` member itself.

Each sample records an opaque ID, case and repetition, engine ID, expected text, relative raw and listening-copy paths, separate SHA-256 hashes, sample rate/channels/duration, request and generation time, RTF, requested parameters, response headers, process and GPU memory observations, loudness-match settings, and advisory diagnostics. The store resolves both audio paths beneath the run folder and verifies their hashes before registering the manifest. It rejects duplicate sample keys, path escapes, missing files, checksum errors, and conflicting IDs. It does not overwrite an existing run.

Raw WAVs are never normalized in place. The audition copy applies per-clip EBU R128 gain toward -23 LUFS, with at most +12 dB gain and a -1 dBFS sample-peak ceiling; no compression is applied. Raw and audition paths remain separate in the manifest.

## Blind sessions and ratings

`POST /api/evaluations/blind-sessions` builds pairwise trials from the first shared repetition of each case. The response contains A/B labels, expected text, and opaque audio URLs only; it does not return engine IDs, metadata, or filename headers. Playback serves the loudness-matched copy. Pair orientation is balanced within each session and against earlier sessions for the same run; ties are randomized. A local operator could still inspect the database or call the explicitly unblinded run endpoint, so this is an honest local listening workflow rather than protection against a hostile administrator.

Run and suite manifests remain schema 1. The local evaluation database is now
schema 3. Schema 2 added `side_ratings` and preserved every original row in
`ratings`; it never guesses A/B scores from historical pair-level scores. Schema 3
renamed the folder that holds run audio from `out/m5` to `out/comparisons`: the
first open of an older database copies it into `migration-backups`, moves the
folder and rewrites the stored paths to match.

`POST /api/evaluations/blind-sessions/{session_id}/ratings` accepts
`{trial_id, ratings: {left: {intelligibility, identity_consistency, acting_fit,
naturalness, artifacts}, right: {the same five fields}, preference: "A" | "B" |
"tie" | null}, notes}`. Both sides need integer scores from 1 through 5; five
means fewer artifacts. Preference is optional. The response distinguishes
`rating_complete`, `rating_format: "per-side"` and `legacy_rating`. Old flat
score payloads can still be stored as legacy history, but cannot satisfy the new
reveal requirement. Ratings persist across restarts.

`POST /api/evaluations/blind-sessions/{session_id}/reveal` is enabled only after
both sides of every trial have scores. Mapping revelation locks the session.
Previously revealed historical sessions remain revealed and read-only. No
historical pair-level rating is relabeled as an opinion about either sample.

Automated smoke checks create explicitly named synthetic persistence-fixture ratings. These are not listener opinions and must not be included in subjective result summaries.

## Diagnostics

Waveform checks measure finite samples, sample peak, RMS, leading/trailing and total quiet-frame durations. They flag complete silence, clipping, and long silence; a short-duration heuristic is advisory. Transcript comparison normalizes case/punctuation, computes WER/CER and edit operations, and flags missing words, a strict-prefix possible truncation, and repeated short phrases. The transcript endpoint requires both evaluator name and version. Evaluation does not run ASR or speaker-similarity models automatically. The optional transcript-draft and voice-match tools are separate features that need their own installed assets, and their output is not an acceptance result. Human review remains necessary.

## API index

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/evaluations/suites` | Versioned named suite definitions |
| GET | `/api/evaluations` | Registered runs |
| GET | `/api/evaluations/runs/{run_id}` | Unblinded run manifest and sample details |
| POST | `/api/evaluations/runs/import` | Register a checksummed manifest below `out/comparisons/` |
| GET/POST | `/api/evaluations/blind-sessions` | List sessions / create a concealed A/B session |
| GET | `/api/evaluations/blind-sessions/{id}` | Read a session without its model mapping until reveal |
| POST | `/api/evaluations/blind-sessions/{id}/ratings` | Save or revise all five scores for a trial |
| POST | `/api/evaluations/blind-sessions/{id}/reveal` | Reveal after all trials are rated |
| GET | `/api/evaluations/blind-audio/{item_id}` | Play one concealed audition copy |
| GET | `/api/evaluations/items/{item_id}` | Read diagnostics for a known unblinded sample |
| POST | `/api/evaluations/items/{item_id}/transcript` | Attach a transcript with evaluator provenance |
