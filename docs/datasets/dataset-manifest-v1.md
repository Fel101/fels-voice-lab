# Local TTS dataset manifest v1

Use this manifest only for recordings that have been explicitly selected for local model training. The workbench does not discover or import Cast audio automatically. It reads source files in place, creates no audio copies, and does not contact a model service.

## Manifest shape

Save one UTF-8 JSON object with `schema_version: 1`, a lowercase `dataset_id`, a `target_model` of `qwen3-tts-1.7b-base`, `qwen3-tts-0.6b-base`, or `voxcpm2`, and an `items` array. Each item needs a stable `id`, root-relative PCM WAV path, exact transcript, speaker ID, and provenance/rights note:

```json
{
  "schema_version": 1,
  "dataset_id": "mara-season-one",
  "target_model": "qwen3-tts-1.7b-base",
  "items": [
    {
      "id": "line-001-take-01",
      "audio": "audio/line-001-take-01.wav",
      "text": "The bridge is closed until sunrise.",
      "speaker_id": "mara",
      "provenance": {
        "kind": "self_recorded",
        "origin": "Voice session 2026-09-30",
        "source_id": "session-20260930-mara",
        "rights_status": "approved",
        "use_scope": ["local_model_training"],
        "evidence": "Speaker consent record stored in the local project archive.",
        "license": "private local recording"
      }
    }
  ],
  "shared_reference": {
    "audio": "audio/mara-reference.wav",
    "speaker_id": "mara",
    "provenance": {
      "kind": "self_recorded",
      "origin": "Voice session 2026-09-30",
      "source_id": "session-20260930-mara-reference",
      "rights_status": "approved",
      "use_scope": ["local_model_training"],
      "evidence": "Speaker consent record stored in the local project archive.",
      "license": "private local recording"
    }
  }
}
```

Allowed `provenance.kind` values are `self_recorded`, `licensed_dataset`, `cast_reference`, `cast_take`, and `other`. `rights_status` can be `approved`, `unknown`, or `restricted`. An export is written only when every training clip and shared reference is `approved` for `local_model_training`, the target model's speaker/reference rules pass, both splits exist, and there are no silent or non-mono clips. Unknown or restricted rights still produce an audit snapshot, but no model training manifests.

For its conservative single-speaker export, the workbench requires one `shared_reference` for all Qwen Base rows. The upstream JSONL requires a `ref_audio` field and recommends keeping it identical across the dataset. VoxCPM2's pinned direct training loader accepts `audio` and `text`, with optional `ref_audio`; the optional top-level shared reference maps to that column. The workbench does not export the separate VoxCPM Docker WebUI's `audio_path` schema.

## Prepare a version

From the voice-lab project directory, with the target model declared in the manifest:

```powershell
.\.venv\Scripts\python.exe tools\dataset_workbench.py prepare `
  --manifest data\datasets\mara-season-one\manifest.json `
  --audio-root data\datasets\mara-season-one `
  --validation-fraction 0.2 --seed 42 --split-strategy text
```

Output is content-addressed under `out/datasets/<dataset-id>/<version>/`. `dataset.lock.json` records audio hashes, transcripts, provenance, signal measurements, deterministic split assignment, and hashes of exported files. `audit.json` records blockers and warnings. When export is allowed, `train.jsonl` and `validation.jsonl` use absolute local audio paths and the target model's raw direct-loader columns. The WAVs remain where they were. Paths in the generated JSONL are local metadata; keep the bundle private.

The default split groups identical normalized transcripts together to avoid leaking repeated lines across train and validation. Use `--split-strategy speaker` to hold whole speakers out for a multi-speaker VoxCPM2 dataset. Fewer than two groups cannot produce a validation set and are not exported. The split is deterministic for a given manifest, strategy, fraction, and seed.

## Checks and limits

The dependency-free PCM WAV reader records sample rate, channels, bit width, frames, duration, peak/RMS dBFS, DC offset, near-silence percentage, and clipped-sample percentage. It warns about clips shorter than 0.3 seconds, longer than 30 seconds, clipping, DC offset, and near-silence. Silent and non-mono files block export; no audio is normalized, resampled, downmixed, trimmed, denoised, transcribed, or rewritten.

These checks are signal-level screening, not a speech detector, SNR/noise classifier, transcript verifier, or quality rating. Review every warning and listen to the selected clips before a training run. The raw export does not encode the selected model's audio codes; Qwen's official `prepare_data.py` remains a separate gated pre-processing step.

Dataset snapshot versioning hashes the selected source files and manifest inputs. Re-running identical inputs reuses the version only after checking the recorded artifact hashes. If a version folder or its lock/audit files are modified or incomplete, preserve it, inspect it, and use a new dataset ID or corrected source manifest; do not edit the recorded version in place.

The workbench is implemented at [`tools/dataset_workbench.py`](../../tools/dataset_workbench.py), with synthetic, temporary-file-only tests at [`tests/test_dataset_workbench.py`](../../tests/test_dataset_workbench.py). No user's Cast references or performances were selected as a dataset while the workbench was built.
