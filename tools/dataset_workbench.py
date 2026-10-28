#!/usr/bin/env python3
"""Build a provenance-preserving, local TTS training-dataset snapshot.

This tool never copies, rewrites, or deletes source audio. It writes manifests
and measurements under out/datasets. Training remains a separate gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import sys
import unicodedata
import wave
from datetime import datetime
from pathlib import Path
from typing import Any


TOOL_VERSION = "1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_MODELS = {
    "qwen3-tts-1.7b-base": {
        "format": "qwen3_tts_base_raw_jsonl_v1",
        "single_speaker": True,
        "shared_reference_required": True,
    },
    "qwen3-tts-0.6b-base": {
        "format": "qwen3_tts_base_raw_jsonl_v1",
        "single_speaker": True,
        "shared_reference_required": True,
    },
    "voxcpm2": {
        "format": "voxcpm_audio_text_jsonl_v1",
        "single_speaker": False,
        "shared_reference_required": False,
    },
}
RIGHTS_STATUSES = {"approved", "unknown", "restricted"}
SOURCE_KINDS = {"self_recorded", "licensed_dataset", "cast_reference", "cast_take", "other"}
DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


class DatasetError(ValueError):
    """An invalid input or unsafe dataset path."""


def _reject_extra_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise DatasetError(f"{label}: unsupported field(s): {', '.join(extra)}")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative_audio(root: Path, raw: Any, label: str) -> tuple[Path, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise DatasetError(f"{label}: audio path must be a non-empty relative path")
    rel = Path(raw)
    if rel.is_absolute() or any(part in {"..", ""} for part in rel.parts):
        raise DatasetError(f"{label}: audio path must stay under --audio-root: {raw!r}")
    try:
        resolved = (root / rel).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DatasetError(f"{label}: cannot resolve audio file {raw!r}: {exc}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise DatasetError(f"{label}: resolved audio path escapes --audio-root: {raw!r}") from exc
    if not resolved.is_file():
        raise DatasetError(f"{label}: audio path is not a file: {raw!r}")
    if resolved.suffix.lower() != ".wav":
        raise DatasetError(f"{label}: only PCM WAV input is inspected/exported; got {raw!r}")
    return resolved, resolved.relative_to(root).as_posix()


def _decode_pcm(block: bytes, width: int) -> list[int]:
    if width == 1:
        return [sample - 128 for sample in block]
    if width == 2:
        return [row[0] for row in struct.iter_unpack("<h", block)]
    if width == 3:
        values: list[int] = []
        for index in range(0, len(block), 3):
            sample = block[index] | (block[index + 1] << 8) | (block[index + 2] << 16)
            if sample & 0x800000:
                sample -= 1 << 24
            values.append(sample)
        return values
    if width == 4:
        return [row[0] for row in struct.iter_unpack("<i", block)]
    raise DatasetError(f"unsupported PCM sample width: {width} bytes")


def inspect_wav(path: Path) -> dict[str, Any]:
    """Measure PCM WAV duration and simple signal-health indicators without loading it all."""
    try:
        before = path.stat()
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            width = audio.getsampwidth()
            declared_frames = audio.getnframes()
            if audio.getcomptype() != "NONE":
                raise DatasetError(f"{path}: compressed WAV is not supported by the offline inspector")
            if not 1 <= channels <= 2:
                raise DatasetError(f"{path}: expected one or two PCM channels; found {channels}")
            if width not in {1, 2, 3, 4}:
                raise DatasetError(f"{path}: unsupported PCM sample width: {width} bytes")
            if sample_rate <= 0 or declared_frames <= 0:
                raise DatasetError(f"{path}: WAV has no sample rate or audio frames")
            frame_bytes = channels * width
            scale = 1 << (8 * width - 1)
            silence_threshold = max(1, int(scale * 0.001))  # approximately -60 dBFS
            total_frames = total_samples = silent_frames = clipped_samples = peak = 0
            sum_squares = sum_samples = 0
            while True:
                raw = audio.readframes(max(1, sample_rate))
                if not raw:
                    break
                if len(raw) % frame_bytes:
                    raise DatasetError(f"{path}: truncated PCM frame at byte {total_samples * width}")
                values = _decode_pcm(raw, width)
                frame_count = len(values) // channels
                for offset in range(0, len(values), channels):
                    frame_values = values[offset : offset + channels]
                    frame_peak = max(abs(value) for value in frame_values)
                    if frame_peak < silence_threshold:
                        silent_frames += 1
                    for value in frame_values:
                        magnitude = abs(value)
                        peak = max(peak, magnitude)
                        clipped_samples += magnitude >= scale - 1
                        sum_squares += value * value
                        sum_samples += value
                        total_samples += 1
                total_frames += frame_count
        after = path.stat()
    except DatasetError:
        raise
    except (OSError, EOFError, wave.Error, struct.error) as exc:
        raise DatasetError(f"{path}: cannot inspect PCM WAV: {exc}") from exc
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise DatasetError(f"{path}: source changed while it was being inspected")
    if total_frames != declared_frames or not total_samples:
        raise DatasetError(f"{path}: truncated WAV; header declares {declared_frames} frames, read {total_frames}")
    rms = math.sqrt(sum_squares / total_samples) / scale
    dc = (sum_samples / total_samples) / scale
    db = lambda value: round(20 * math.log10(max(value, 1e-6)), 2)
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "sample_width_bytes": width,
        "frames": total_frames,
        "duration_seconds": round(total_frames / sample_rate, 6),
        "peak_dbfs": db(peak / scale),
        "rms_dbfs": db(rms),
        "dc_offset": round(dc, 6),
        "near_silent_frame_percent": round(100 * silent_frames / total_frames, 3),
        "clipped_sample_percent": round(100 * clipped_samples / total_samples, 6),
    }


def _validate_rights(value: Any, label: str) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, dict):
        raise DatasetError(f"{label}: provenance and rights object is required")
    kind = value.get("kind")
    origin = value.get("origin")
    source_id = value.get("source_id")
    status = value.get("rights_status")
    use_scope = value.get("use_scope", [])
    evidence = value.get("evidence")
    if not isinstance(kind, str) or kind not in SOURCE_KINDS:
        raise DatasetError(f"{label}: provenance.kind must be one of {sorted(SOURCE_KINDS)}")
    if not all(isinstance(item, str) and item.strip() for item in (origin, source_id)):
        raise DatasetError(f"{label}: provenance.origin and source_id are required")
    if not isinstance(status, str) or status not in RIGHTS_STATUSES:
        raise DatasetError(f"{label}: rights_status must be one of {sorted(RIGHTS_STATUSES)}")
    if not isinstance(use_scope, list) or not all(isinstance(item, str) for item in use_scope):
        raise DatasetError(f"{label}: use_scope must be a list of strings")
    if not isinstance(evidence, str) or not evidence.strip():
        raise DatasetError(f"{label}: rights evidence note is required (a local provenance note is enough)")
    license_name = value.get("license", "unknown")
    if not isinstance(license_name, str):
        raise DatasetError(f"{label}: provenance.license must be a string")
    _reject_extra_keys(value, {"kind", "origin", "source_id", "rights_status", "use_scope", "evidence", "license"}, f"{label}.provenance")
    clean = {
        "kind": kind,
        "origin": origin.strip(),
        "source_id": source_id.strip(),
        "rights_status": status,
        "use_scope": sorted(set(use_scope)),
        "evidence": evidence.strip(),
        "license": license_name.strip() or "unknown",
    }
    approved = status == "approved" and "local_model_training" in clean["use_scope"]
    return clean, approved


def _normalized_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _audio_warnings(metrics: dict[str, Any]) -> list[str]:
    warnings = []
    if metrics["duration_seconds"] < 0.3:
        warnings.append("duration_below_0.3_seconds")
    if metrics["duration_seconds"] > 30.0:
        warnings.append("duration_above_30_seconds_may_raise_memory_use")
    if metrics["channels"] != 1:
        warnings.append("audio_is_not_mono; no downmix was applied")
    if metrics["near_silent_frame_percent"] == 100:
        warnings.append("audio_is_silent_at_minus_60_dbfs_threshold")
    elif metrics["near_silent_frame_percent"] >= 20:
        warnings.append("near_silence_at_least_20_percent")
    if metrics["clipped_sample_percent"] > 0:
        warnings.append("one_or_more_samples_at_pcm_limit")
    if abs(metrics["dc_offset"]) >= 0.01:
        warnings.append("dc_offset_at_least_0.01")
    return warnings


def _assign_splits(items: list[dict[str, Any]], strategy: str, seed: int, fraction: float) -> tuple[dict[str, str], dict[str, Any]]:
    if strategy not in {"text", "speaker"}:
        raise DatasetError("split strategy must be 'text' or 'speaker'")
    if not 0 < fraction < 0.5:
        raise DatasetError("validation fraction must be greater than 0 and less than 0.5")
    key_field = "speaker_id" if strategy == "speaker" else "normalized_text"
    groups: dict[str, list[str]] = {}
    for item in items:
        group = item[key_field] if strategy == "speaker" else _normalized_text(item["text"])
        groups.setdefault(group, []).append(item["id"])
    group_keys = list(groups)
    if len(group_keys) < 2:
        assignment = {item_id: "train" for ids in groups.values() for item_id in ids}
        return assignment, {"strategy": strategy, "seed": seed, "group_count": len(group_keys), "train_groups": len(group_keys), "validation_groups": 0}
    validation_count = min(len(group_keys) - 1, max(1, round(len(group_keys) * fraction)))
    ranked = sorted(group_keys, key=lambda key: hashlib.sha256(f"{seed}\0{strategy}\0{key}".encode("utf-8")).digest())
    validation_groups = set(ranked[:validation_count])
    assignment = {
        item_id: ("validation" if key in validation_groups else "train")
        for key, ids in groups.items()
        for item_id in ids
    }
    return assignment, {
        "strategy": strategy,
        "seed": seed,
        "requested_validation_fraction": fraction,
        "group_count": len(group_keys),
        "train_groups": len(group_keys) - validation_count,
        "validation_groups": validation_count,
    }


def prepare_dataset(
    manifest_path: Path,
    audio_root: Path,
    output_root: Path,
    *,
    validation_fraction: float = 0.2,
    seed: int = 42,
    split_strategy: str = "text",
    enforce_output_scope: bool = False,
) -> dict[str, Any]:
    """Validate and snapshot a manifest; export raw model JSONL only with explicit training rights."""
    manifest_path = manifest_path.resolve(strict=True)
    audio_root = audio_root.resolve(strict=True)
    output_root = output_root.resolve()
    if not manifest_path.is_file() or not audio_root.is_dir():
        raise DatasetError("manifest must be a file and --audio-root must be a directory")
    if enforce_output_scope:
        allowed = (PROJECT_ROOT / "out" / "datasets").resolve()
        try:
            output_root.relative_to(allowed)
        except ValueError as exc:
            raise DatasetError(f"output root must stay under {allowed}") from exc
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read JSON manifest: {exc}") from exc
    if not isinstance(document, dict) or isinstance(document.get("schema_version"), bool) or document.get("schema_version") != 1:
        raise DatasetError("manifest must be a JSON object with schema_version: 1")
    dataset_id = document.get("dataset_id")
    target_model = document.get("target_model")
    if not isinstance(dataset_id, str) or not DATASET_ID_RE.fullmatch(dataset_id):
        raise DatasetError("dataset_id must be a lowercase slug (letters, digits, dot, underscore, hyphen)")
    if not isinstance(target_model, str) or target_model not in SUPPORTED_MODELS:
        raise DatasetError(f"target_model must be one of {sorted(SUPPORTED_MODELS)}")
    _reject_extra_keys(document, {"schema_version", "dataset_id", "target_model", "items", "shared_reference"}, "manifest")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise DatasetError("split seed must be a non-negative integer")
    if not isinstance(validation_fraction, (int, float)) or not math.isfinite(validation_fraction) or not 0 < validation_fraction < 0.5:
        raise DatasetError("validation fraction must be greater than 0 and less than 0.5")
    raw_items = document.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise DatasetError("manifest items must be a non-empty array")

    prepared_items: list[dict[str, Any]] = []
    rights_clear = True
    audio_cache: dict[Path, dict[str, Any]] = {}
    audio_hash_to_id: dict[str, str] = {}
    item_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        label = f"items[{index}]"
        if not isinstance(raw_item, dict):
            raise DatasetError(f"{label}: item must be an object")
        _reject_extra_keys(raw_item, {"id", "audio", "text", "speaker_id", "provenance"}, label)
        item_id = raw_item.get("id")
        speaker_id = raw_item.get("speaker_id")
        text = raw_item.get("text")
        if not isinstance(item_id, str) or not ITEM_ID_RE.fullmatch(item_id) or item_id in item_ids:
            raise DatasetError(f"{label}: id must be unique and use letters, digits, dot, underscore, hyphen")
        if not isinstance(speaker_id, str) or not speaker_id.strip():
            raise DatasetError(f"{label}: speaker_id is required")
        if not isinstance(text, str) or not text.strip():
            raise DatasetError(f"{label}: transcript text is required")
        item_ids.add(item_id)
        provenance, approved = _validate_rights(raw_item.get("provenance"), label)
        rights_clear &= approved
        source_path, relative_path = _safe_relative_audio(audio_root, raw_item.get("audio"), label)
        if source_path not in audio_cache:
            before_hash = _sha256_file(source_path)
            metrics = inspect_wav(source_path)
            after_hash = _sha256_file(source_path)
            if before_hash != after_hash:
                raise DatasetError(f"{label}: source changed while it was being hashed")
            audio_cache[source_path] = {"sha256": before_hash, "bytes": source_path.stat().st_size, "metrics": metrics}
        file_hash = audio_cache[source_path]["sha256"]
        if file_hash in audio_hash_to_id:
            raise DatasetError(f"{label}: duplicate audio content also appears as item {audio_hash_to_id[file_hash]!r}")
        audio_hash_to_id[file_hash] = item_id
        metrics = audio_cache[source_path]["metrics"]
        audio_warnings = _audio_warnings(metrics)
        prepared_items.append({
            "id": item_id,
            "audio": relative_path,
            "audio_sha256": file_hash,
            "audio_bytes": audio_cache[source_path]["bytes"],
            "text": text,
            "speaker_id": speaker_id.strip(),
            "provenance": provenance,
            "audio_metrics": metrics,
            "audio_warnings": audio_warnings,
        })

    model_spec = SUPPORTED_MODELS[target_model]
    speakers = sorted({item["speaker_id"] for item in prepared_items})
    if model_spec["single_speaker"] and len(speakers) != 1:
        raise DatasetError(f"{target_model} official fine-tuning format is single-speaker; found {len(speakers)} speakers")

    reference = document.get("shared_reference")
    prepared_reference = None
    if reference is not None:
        if not isinstance(reference, dict):
            raise DatasetError("shared_reference must be an object")
        _reject_extra_keys(reference, {"audio", "speaker_id", "provenance"}, "shared_reference")
        reference_path, reference_relative = _safe_relative_audio(audio_root, reference.get("audio"), "shared_reference")
        reference_provenance, reference_approved = _validate_rights(reference.get("provenance"), "shared_reference")
        rights_clear &= reference_approved
        reference_speaker = reference.get("speaker_id")
        if not isinstance(reference_speaker, str) or not reference_speaker.strip():
            raise DatasetError("shared_reference.speaker_id is required")
        if any(speaker != reference_speaker.strip() for speaker in speakers):
            raise DatasetError("shared_reference.speaker_id must match every training item when one shared reference is used")
        if reference_path not in audio_cache:
            before_hash = _sha256_file(reference_path)
            metrics = inspect_wav(reference_path)
            after_hash = _sha256_file(reference_path)
            if before_hash != after_hash:
                raise DatasetError("shared reference changed while it was being hashed")
            audio_cache[reference_path] = {"sha256": before_hash, "bytes": reference_path.stat().st_size, "metrics": metrics}
        reference_metrics = audio_cache[reference_path]["metrics"]
        prepared_reference = {
            "audio": reference_relative,
            "audio_sha256": audio_cache[reference_path]["sha256"],
            "audio_bytes": audio_cache[reference_path]["bytes"],
            "speaker_id": reference_speaker.strip(),
            "provenance": reference_provenance,
            "audio_metrics": reference_metrics,
            "audio_warnings": _audio_warnings(reference_metrics),
        }
    elif model_spec["shared_reference_required"]:
        raise DatasetError(f"{target_model} requires one shared_reference with independent provenance and rights")

    assignment, split_info = _assign_splits(prepared_items, split_strategy, seed, float(validation_fraction))
    for item in prepared_items:
        item["split"] = assignment[item["id"]]
    split_counts = {name: sum(item["split"] == name for item in prepared_items) for name in ("train", "validation")}
    blockers = []
    if not rights_clear:
        blockers.append("Every clip and shared reference needs rights_status=approved and local_model_training in use_scope.")
    if not split_counts["validation"]:
        blockers.append("At least two distinct split groups are needed to create a held-out validation set.")
    if any("audio_is_silent_at_minus_60_dbfs_threshold" in item["audio_warnings"] for item in prepared_items):
        blockers.append("One or more clips are silent at the workbench threshold; correct or remove them before export.")
    if any("audio_is_not_mono; no downmix was applied" in item["audio_warnings"] for item in prepared_items):
        blockers.append("One or more clips are not mono; explicitly prepare mono files before model export.")
    if prepared_reference and "audio_is_silent_at_minus_60_dbfs_threshold" in prepared_reference["audio_warnings"]:
        blockers.append("The shared reference is silent at the workbench threshold; correct it before export.")
    if prepared_reference and "audio_is_not_mono; no downmix was applied" in prepared_reference["audio_warnings"]:
        blockers.append("The shared reference is not mono; explicitly prepare a mono reference before model export.")
    can_export = not blockers

    identity = {
        "tool_version": TOOL_VERSION,
        "dataset_id": dataset_id,
        "source_audio_root": str(audio_root),
        "target_model": target_model,
        "format": model_spec["format"],
        "split": {**split_info, "validation_fraction": float(validation_fraction)},
        "speakers": speakers,
        "shared_reference": prepared_reference,
        "items": prepared_items,
    }
    version_hash = hashlib.sha256(_canonical_json(identity)).hexdigest()
    version = version_hash[:20]
    bundle_dir = output_root / dataset_id / version
    manifest_lock = {"schema_version": 1, "dataset_version": version, "dataset_sha256": version_hash, **identity}
    audit = {
        "tool_version": TOOL_VERSION,
        "dataset_id": dataset_id,
        "dataset_version": version,
        "dataset_sha256": version_hash,
        "generated_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "target_model": target_model,
        "training_format": model_spec["format"],
        "source_audio_root": str(audio_root),
        "source_audio_copied": False,
        "item_count": len(prepared_items),
        "split_counts": split_counts,
        "split": manifest_lock["split"],
        "speaker_count": len(speakers),
        "duration_seconds_total": round(sum(item["audio_metrics"]["duration_seconds"] for item in prepared_items), 6),
        "rights_clear_for_local_training": rights_clear,
        "model_manifests_exported": can_export,
        "training_enabled": False,
        "blockers": blockers,
        "warnings": [
            {"item_id": item["id"], "warning": warning}
            for item in prepared_items for warning in item["audio_warnings"]
        ],
        "audio_checks": [
            {"item_id": item["id"], "relative_path": item["audio"], "sha256": item["audio_sha256"], **item["audio_metrics"]}
            for item in prepared_items
        ],
    }
    if prepared_reference:
        audit["warnings"].extend({"item_id": "shared_reference", "warning": warning} for warning in prepared_reference["audio_warnings"])
        audit["audio_checks"].append({
            "item_id": "shared_reference", "relative_path": prepared_reference["audio"],
            "sha256": prepared_reference["audio_sha256"], **prepared_reference["audio_metrics"],
        })

    export_contents: dict[str, str] = {}
    if can_export:
        train_rows = []
        validation_rows = []
        for item in prepared_items:
            absolute_audio = str((audio_root / Path(item["audio"])).resolve())
            row = {"audio": absolute_audio, "text": item["text"]}
            if prepared_reference:
                row["ref_audio"] = str((audio_root / Path(prepared_reference["audio"])).resolve())
            (train_rows if item["split"] == "train" else validation_rows).append(row)
        for filename, rows in (("train.jsonl", train_rows), ("validation.jsonl", validation_rows)):
            export_contents[filename] = "".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
            )

    if bundle_dir.exists():
        try:
            existing = json.loads((bundle_dir / "dataset.lock.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatasetError(f"content-addressed output exists but its lock file is unreadable: {bundle_dir}") from exc
        comparable_keys = ("dataset_id", "source_audio_root", "target_model", "format", "split", "speakers", "shared_reference", "items")
        if existing.get("dataset_sha256") != version_hash or any(existing.get(key) != manifest_lock.get(key) for key in comparable_keys):
            raise DatasetError(f"content-address collision or modified output; preserve and inspect {bundle_dir}")
        expected_artifacts = {"audit.json", *export_contents.keys()}
        recorded_artifacts = existing.get("artifact_sha256", {})
        if not isinstance(recorded_artifacts, dict) or set(recorded_artifacts) != expected_artifacts:
            raise DatasetError(f"versioned dataset artifact index is incomplete or invalid: {bundle_dir}")
        for name, expected_hash in recorded_artifacts.items():
            if Path(name).name != name:
                raise DatasetError(f"invalid artifact path in versioned dataset lock: {name!r}")
            artifact = bundle_dir / name
            if not artifact.is_file() or _sha256_file(artifact) != expected_hash:
                raise DatasetError(f"versioned dataset artifact changed or is missing: {artifact}")
        audit["output_reused"] = True
        return {"bundle_dir": str(bundle_dir), "audit": audit, "manifest_lock": existing}

    bundle_dir.mkdir(parents=True, exist_ok=False)
    audit["output_reused"] = False
    (bundle_dir / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for filename, contents in export_contents.items():
        (bundle_dir / filename).write_text(contents, encoding="utf-8", newline="\n")
    manifest_lock["artifact_sha256"] = {
        filename: _sha256_file(bundle_dir / filename)
        for filename in ("audit.json", *export_contents.keys())
    }
    (bundle_dir / "dataset.lock.json").write_text(json.dumps(manifest_lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"bundle_dir": str(bundle_dir), "audit": audit, "manifest_lock": manifest_lock}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="audit, version, split, and export one explicit local manifest")
    prepare.add_argument("--manifest", required=True, type=Path, help="JSON manifest using schema v1")
    prepare.add_argument("--audio-root", required=True, type=Path, help="root for manifest-relative WAV paths")
    prepare.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "out" / "datasets")
    prepare.add_argument("--validation-fraction", type=float, default=0.2)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--split-strategy", choices=("text", "speaker"), default="text")
    args = parser.parse_args(argv)
    output_root = args.output_root if args.output_root.is_absolute() else PROJECT_ROOT / args.output_root
    try:
        result = prepare_dataset(
            args.manifest, args.audio_root, output_root,
            validation_fraction=args.validation_fraction, seed=args.seed,
            split_strategy=args.split_strategy, enforce_output_scope=True,
        )
    except (DatasetError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    audit = result["audit"]
    print(json.dumps({
        "status": "ready_for_export" if audit["model_manifests_exported"] else "audit_saved_export_blocked",
        "bundle_dir": result["bundle_dir"],
        "dataset_version": audit["dataset_version"],
        "split_counts": audit["split_counts"],
        "model_manifests_exported": audit["model_manifests_exported"],
        "blockers": audit["blockers"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
