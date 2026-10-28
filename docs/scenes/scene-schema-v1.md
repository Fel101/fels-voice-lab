# Scene manifest schema v1

Scenes are ordered scripts whose lines point to existing cast voice identities. This reference documents the current script-schema-v1 contract in `scene_store.py`, including later identity and model integrations. JSON is UTF-8; CSV import/export uses UTF-8 with a BOM for spreadsheet compatibility. API paths are local to the voice-lab service (for example `http://127.0.0.1:8768`).

## Minimal JSON

Get `/api/scenes/template.json` for a template. Replace the placeholders with IDs from `/api/cast` before creating a scene:

```json
{
  "schema_version": 1,
  "name": "The Last Signal",
  "lines": [
    {
      "line_id": "scene1-001",
      "line_type": "dialogue",
      "character_id": "<character-id>",
      "identity_id": "<identity-id>",
      "reference_version_id": "<reference-version-id-or-null>",
      "text": "The exact words to speak.",
      "language": "English",
      "direction": "Quiet, guarded, with a pause before the last word.",
      "pause_before_ms": 0,
      "pause_after_ms": 250,
      "take_count": 1,
      "target_duration_ms": null,
      "settings": {},
      "use_pronunciations": false
    }
  ]
}
```

`schema_version` defaults to 1 when omitted. The service resolves `character_id`, `engine_id`, and the approved/current `reference_version_id` from the identity if omitted. A supplied character or engine must match that identity. Persisted/exported scenes also contain `scene_id`, `revision`, `checksum`, generated `segments`, `selected_take_id`, and `selected_takes`; those are managed fields and should not be manually edited. `selected_take_id` is the compatibility field for a one-segment line. Multi-segment selections live in `selected_takes`, keyed by segment ID.

## Fields and bounds

| Field | Meaning and validation |
|---|---|
| `name` | Required in stored scene; 1–120 characters. Defaults to “Untitled scene” on create. |
| `lines` | Ordered list, 1–500 entries. Order is playback/export order. |
| `line_id` | Stable unique ID, 1–64 characters; starts with a letter/digit, then letters, digits, `.`, `_`, `:`, or `-`. |
| `line_type` | `dialogue` or `narration`; defaults to `dialogue`. |
| `character_id`, `identity_id` | Must identify the same existing cast character and its generation identity. Identity must have a valid approved reference where that engine requires one. |
| `engine_id` | Optional input consistency check; must match the chosen identity. |
| `reference_version_id` | Optional; resolved/validated by the cast store. The selected version is fixed into the stored scene. |
| `text` | Required nonblank exact speech text, at most 10,000 characters. |
| `language` | Defaults to `English`; case-insensitive input is normalized to the supported spelling. The current scene picker accepts American/British English, Spanish, French, Hindi, Italian and Brazilian Portuguese for Kokoro (plus English alias); English for Chatterbox, Breeze and the current VoxCPM2 scene contract; Auto plus Chinese, English, French, German, Italian, Japanese, Korean, Portuguese, Russian and Spanish for Qwen; and Auto plus the 83 pinned Drama language labels. Native CustomVoice identities use their discovered language list. This allowlist does not claim equal quality in every language. |
| `direction` | Optional string up to 1,000 characters. Nonempty directions are rejected for engines without an exposed text-direction control; Qwen Base and VoxCPM2 direction remain experimental. |
| `pause_before_ms`, `pause_after_ms` | Integers from 0 to 10,000; default 0. |
| `take_count` | Integer 1–10; default 1. Takes are synthesized sequentially. |
| `target_duration_ms` | Optional integer 1–120,000. Saved as a timing target only; it does not change the generated duration. |
| `settings` | Optional JSON object, at most 4,096 serialized characters. Engine/API validation still applies when rendered. |
| `use_pronunciations` | Optional boolean, default false. Uses the character's saved pronunciation dictionary. |
| selected-take fields | Optional references to existing takes for the same identity; normally set using `/select` after rendering. |

Line text is segmented at sentence boundaries where possible to a maximum of 1,200 characters per synthesis call. The splitter protects common abbreviations, balanced event/markup spans, and pronunciation dictionary phrases. Segments are assigned `seg-001`, `seg-002`, etc. Review `GET /segments` output before rendering long lines. Segmentation does not promise continuous prosody across calls.

## CSV

`GET /api/scenes/template.csv` returns the exact header and one sample row. Fields are:

```text
schema_version,scene_name,line_id,line_type,character_id,identity_id,engine_id,reference_version_id,text,language,direction,pause_before_ms,pause_after_ms,take_count,selected_take_id,selected_takes_json,target_duration_ms,settings_json,use_pronunciations
```

`selected_takes_json` and `settings_json` are JSON objects encoded in their CSV cells. Blank integer cells use the defaults. IDs must already exist in the local cast. CSV rows preserve input order. JSON and CSV imports are limited to 2 MiB per HTTP request.

## API workflow

| Method and path | Purpose |
|---|---|
| `GET /api/cast` | Discover local characters and identities. |
| `GET /api/scenes`, `POST /api/scenes` | List or create a scene (`POST` body is a manifest). |
| `GET /api/scenes/template.json`, `GET /api/scenes/template.csv` | Download templates. |
| `POST /api/scenes/import` | Import `{ "format": "json"|"csv", "content": "..." }`. |
| `GET /api/scenes/{scene_id}` | Read scene and its revision/checksum. |
| `POST /api/scenes/{scene_id}` | Replace/update scene (the service has no PUT); pass `expected_revision` to detect stale edits. |
| `GET /api/scenes/{scene_id}/export.json`, `/export.csv` | Export ordered manifest/rows. |
| `GET /api/scenes/{scene_id}/segments?line_id=...` | Inspect the computed segments. |
| `POST /api/scenes/{scene_id}/jobs` | Queue all rows, or send `line_ids` / `segment_ids` to rerender selected work. Segment selector format is `line-id/seg-001`. |
| `GET /api/scenes/{scene_id}/jobs`, `GET /api/scenes/jobs/{job_id}` | Read scene job list or detailed progress. |
| `GET /api/scenes/jobs/{job_id}/manifest.json` | Read the result manifest only when its checksum verifies; mismatch returns HTTP 409. |
| `POST /api/scenes/jobs/{job_id}/pause`, `/resume`, `/cancel`, `/retry` | Control a batch or retry failed rows. |
| `POST /api/scenes/{scene_id}/select` | Select a take using `line_id`, `take_id`, and (for segmented lines) `segment_id`. |
| `POST /api/scenes/{scene_id}/preview` | Assemble the currently selected takes in scene order. |

The queue renders one take at a time through `/api/performance`, keeping the existing GPU scheduler and take provenance. A persisted request key prevents duplicate take creation when the process exits after audio is saved but before a batch row is checkpointed. On restart, an interrupted active job is re-queued; completed rows remain completed. Paused jobs remain paused until resumed. Cancellation stops future rows and cannot undo a generation already in flight.

Scene preview requires a selected valid take for every segment and rejects an incomplete/failed latest batch or a missing/hash-invalid WAV. It writes two separate files with explicit silence and no crossfades: the raw assembly (`audio_path`) and a scene-level loudness-normalized copy (`mastered_audio_path`). The latter uses the existing `pyloudnorm` runtime dependency, targets -18 LUFS, caps boost at +12 dB, and caps sample peak at -1 dBFS. The sidecar JSON includes `mastering` measurements and both SHA-256 hashes. There is no compression, limiter dynamics, or per-line leveling; raw takes are never modified. This is an audition mix, not a final mastering chain or native multi-speaker generation.

## Storage and recovery

Stop `serve.py` before taking a coordinated backup of `data/cast/`, `data/scenes/`, and `out/scenes/`. These must come from the same snapshot: scenes/jobs point at cast identities and take media. Restore all three together, retain virtual environments, cached model files, reference recordings, and historical outputs, then start `.venv\Scripts\python.exe serve.py`. If the job-manifest endpoint returns 409, keep the files intact and restore a matching backup rather than editing the signed-by-checksum data by hand. Cast schema v3 includes native preset identities and the earlier unique take request keys; the service performs its normal migration when opening an older supported database. Script schema v1 is separate from the Cast database version.
