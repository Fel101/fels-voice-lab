"""Package selected Fel's Voice Lab takes and scenes without changing source recordings.

The exporter reads the Cast and Scenes databases in SQLite read-only mode. It copies
original take WAVs byte-for-byte, writes derived assets only below out/exports, and
records the exact scene selections and provenance needed for review/re-import.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePath
from contextlib import contextmanager

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ffmpeg_tools  # noqa: E402  finding FFmpeg and what this build can encode
import synthetic_label  # noqa: E402  the AI-generated speech label every delivered file carries
import word_timing  # noqa: E402  pure helpers only: phrase grouping and offsets; the exporter never writes Cast data
from fit import fit_status  # noqa: E402  slot status only; the stretch itself is not applied by the exporter
from lipsync import check_track, prime_for_takes, track_for_takes  # noqa: E402  mouth-shape tracks from stored word times; estimates
from output_license import license_profile  # noqa: E402
from scene_selection import validate_selected_take  # noqa: E402
from cast_store import RetirementReader


FORMATS = {"wav", "flac", "ogg", "mp3", "m4a"}
FORMAT_CONFIG = {
    "wav": (".wav", ["-c:a", "pcm_s16le"]),
    "flac": (".flac", ["-c:a", "flac"]),
    "ogg": (".ogg", ["-c:a", "libvorbis", "-q:a", "6"]),
    "mp3": (".mp3", ["-c:a", "libmp3lame", "-q:a", "2"]),  # variable bit rate, about 190 kbit/s
    "m4a": (".m4a", ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]),
}
# The codec ffprobe reports for each format's deliverables, checked when a package is verified.
FORMAT_CODEC = {"wav": "pcm_s16le", "flac": "flac", "ogg": "vorbis", "mp3": "mp3", "m4a": "aac"}
# The sample rates a lossy encoder accepts. FFmpeg stops with an error at any other, so the export checks first.
ENCODER_RATES = {
    "mp3": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
    "m4a": (7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000),
}
CSV_FIELDS = [
    "asset_type", "scene_id", "line_id", "segment_id", "character_id", "character_name",
    "identity_id", "take_id", "selected_take_ids_json", "engine_id", "checkpoint", "checkpoint_revision",
    "source_audio_sha256", "output_path", "output_sha256", "codec", "sample_rate",
    "channels", "duration_seconds", "reference_version_id", "reference_provenance_json",
    "model_json", "runtime_json", "take_settings_json", "license_json", "processing_steps_json", "text",
]


class ExportError(RuntimeError):
    pass


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_json(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@contextmanager
def read_only_db(path):
    path = Path(path).resolve()
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        yield db
    finally:
        db.close()


def under_root(root, relative):
    path = (Path(root) / Path(relative)).resolve()
    try:
        path.relative_to(Path(root).resolve())
    except ValueError as exc:
        raise ExportError(f"Stored media path escapes the project root: {relative!r}") from exc
    if not path.is_file():
        raise ExportError(f"Stored media file is missing: {relative}")
    return path


def safe_component(value, *, max_length=90):
    raw = str(value)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(" .-_").lower()
    if not cleaned or cleaned in {".", ".."}:
        cleaned = "item"
    if cleaned.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = f"item-{cleaned}"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[:max_length - 10].rstrip(' .-_')}-{suffix}"


def filename_from_template(template, *, line_id, character_name, take_id):
    if "/" in template or "\\" in template or ".." in template:
        raise ExportError("Filename template must be a single safe filename; path separators and '..' are rejected.")
    names = set(re.findall(r"\{([^{}]+)\}", template))
    if names - {"line_id", "character", "take_id"}:
        raise ExportError("Filename template placeholders are limited to {line_id}, {character}, and {take_id}.")
    try:
        rendered = template.format(
            line_id=safe_component(line_id),
            character=safe_component(character_name),
            take_id=str(take_id)[:8],
        )
    except (KeyError, ValueError) as exc:
        raise ExportError(f"Invalid filename template: {exc}") from exc
    if PurePath(rendered).name != rendered:
        raise ExportError("Filename template produced a path component.")
    return safe_component(rendered)


def probe_media(path, ffprobe):
    command = [
        ffprobe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels,duration:stream_tags=comment",
        "-show_entries", "format=duration:format_tags=comment", "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ExportError(f"ffprobe failed for {path}: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        duration = stream.get("duration") or data.get("format", {}).get("duration")
        # The label sits in the container's tags, except in Ogg, where Vorbis keeps it on the stream.
        comment = (data.get("format", {}).get("tags") or {}).get("comment") or (stream.get("tags") or {}).get("comment")
        return {
            "codec": stream.get("codec_name"),
            "sample_rate": int(stream["sample_rate"]),
            "channels": int(stream["channels"]),
            "duration_seconds": float(duration),
            "comment": comment,
        }
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ExportError(f"ffprobe returned incomplete audio metadata for {path}.") from exc


def load_scene(root, scene_id):
    scene_db = Path(root) / "data" / "scenes" / "scenes.sqlite3"
    if not scene_db.is_file():
        raise ExportError(f"Scene database is missing: {scene_db}")
    with read_only_db(scene_db) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT manifest_json,checksum FROM scenes WHERE id=?", (scene_id,)).fetchone()
    if row is None:
        raise ExportError(f"Scene not found: {scene_id}")
    scene = json.loads(row["manifest_json"])
    checksum = scene.pop("checksum", None)
    calculated = digest_json(scene)
    scene["checksum"] = checksum
    if checksum != row["checksum"] or checksum != calculated:
        raise ExportError(f"Scene manifest checksum mismatch for {scene_id}.")
    if scene.get("scene_id") != scene_id or not isinstance(scene.get("lines"), list):
        raise ExportError("Scene manifest has an invalid scene ID or line list.")
    return scene


def load_takes(root, scene):
    cast_db = Path(root) / "data" / "cast" / "cast.sqlite3"
    if not cast_db.is_file():
        raise ExportError(f"Cast database is missing: {cast_db}")
    selected = []
    with read_only_db(cast_db) as db:
        db.row_factory = sqlite3.Row
        # One reader for the whole scene. A Cast database from before schema 4 has no retirements table, so nothing in
        # it can be retired, and it must still export.
        retirements = RetirementReader(db) if db.execute("PRAGMA user_version").fetchone()[0] >= 4 else None
        for line in scene["lines"]:
            segments = line.get("segments") or []
            if not segments:
                raise ExportError(f"Scene line {line.get('line_id')} has no segments.")
            segment_map = line.get("selected_takes") or {}
            line_takes = []
            for segment in segments:
                segment_id = segment.get("id")
                take_id = segment_map.get(segment_id)
                if not take_id and len(segments) == 1:
                    take_id = line.get("selected_take_id")
                if not take_id:
                    raise ExportError(f"Scene line {line['line_id']}/{segment_id} has no selected take.")
                row = db.execute(
                    "SELECT t.*,c.name AS character_name,i.engine_id "
                    "FROM takes t JOIN characters c ON c.id=t.character_id "
                    "JOIN identities i ON i.id=t.identity_id WHERE t.id=?",
                    (take_id,),
                ).fetchone()
                if row is None:
                    raise ExportError(f"Selected take does not exist in Cast: {take_id}")
                take = dict(row)
                for source, target in (
                    ("settings_json", "settings"), ("model_json", "model"),
                    ("provenance_json", "provenance"),
                ):
                    take[target] = json.loads(take.pop(source))
                if retirements is not None and retirements.state("take", take_id):
                    raise ExportError(f"Selected take {take_id} is retired; choose another take before exporting.")
                if take["identity_id"] != line.get("identity_id"):
                    raise ExportError(
                        f"Selected take {take_id} for {line['line_id']} has the wrong voice identity."
                    )
                if take["character_id"] != line.get("character_id"):
                    raise ExportError(
                        f"Selected take {take_id} for {line['line_id']} has the wrong character."
                    )
                spoken = segment.get("text", line["text"])
                if line.get("use_pronunciations"):
                    pronunciations = db.execute("SELECT source,replacement FROM pronunciations WHERE character_id IS NULL OR character_id=? ORDER BY length(source) DESC,source COLLATE NOCASE",
                                                (line["character_id"],)).fetchall()
                    for pronunciation in pronunciations:
                        spoken = re.sub(r"(?<!\w)" + re.escape(pronunciation["source"]) + r"(?!\w)",
                                        lambda _match, value=pronunciation["replacement"]: value,
                                        spoken, flags=re.IGNORECASE)
                try:
                    validate_selected_take(scene["scene_id"], line, segment, take, spoken)
                except ValueError as exc:
                    raise ExportError(str(exc)) from exc
                media = under_root(root, take["audio_path"])
                actual_hash = sha256_file(media)
                if actual_hash != take["audio_sha256"]:
                    raise ExportError(f"Selected take {take_id} source hash does not match Cast.")
                take["media_path"] = media
                take["source_hash_verified"] = actual_hash
                take["segment_id"] = segment_id
                take["segment_text"] = segment.get("text", line["text"])
                take["probe"] = None
                take["word_timings"] = word_timing.stored_timings(take.pop("word_timings_json", ""))
                take["phoneme_timings"] = word_timing.stored_timings(take.pop("phoneme_timings_json", ""))
                line_takes.append(take)
            selected.append({"line": line, "takes": line_takes})
    return selected


def reference_provenance(root, take):
    provenance = take["provenance"]
    reference_id = take.get("reference_version_id") or provenance.get("reference_version_id")
    result = {
        "reference_version_id": reference_id,
        "origin": provenance.get("origin"),
        "allowed_use": provenance.get("allowed_use"),
        "source_sha256": provenance.get("source_sha256"),
        "reference_audio_sha256": provenance.get("reference_sha256"),
        "parent_reference_id": provenance.get("parent_reference_id"),
        "parent_take_id": provenance.get("parent_take_id"),
    }
    if reference_id:
        db_path = Path(root) / "data" / "cast" / "cast.sqlite3"
        with read_only_db(db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT id,source_sha256,audio_sha256,origin,allowed_use,parent_reference_id,parent_take_id "
                "FROM reference_versions WHERE id=?",
                (reference_id,),
            ).fetchone()
        if row:
            result["reference_record"] = dict(row)
    return result


def print_preflight(root, scene, selected, formats, include_character_id):
    print("PRE-EXPORT PROVENANCE REVIEW")
    print(f"Scene: {scene['scene_id']} revision {scene['revision']} ({scene['name']})")
    print(f"Selected lines: {len(selected)}; requested deliverable formats: {', '.join(formats)}")
    if include_character_id:
        count = sum(item["line"].get("character_id") == include_character_id for item in selected)
        print(f"Character pack: {include_character_id}; selected lines: {count}")
    profiles = {}
    for item in selected:
        for take in item["takes"]:
            profile = license_profile(take["engine_id"], take["model"], reference_provenance(root, take))
            profiles[(take["engine_id"], profile["status"], profile["output_use"])] = profile
    for (engine_id, _status, _output_use), profile in sorted(profiles.items()):
        print(
            f"- {engine_id}: {profile['status'].upper()}; "
            f"code={profile['code_license']}; weights={profile['weights_license']}; "
            f"output={profile['output_use']}"
        )
        if profile["status"] in {"restricted", "unknown"}:
            print(f"  Review: {profile['evidence']}")


def ffmpeg_filters(args, duration):
    filters = []
    trim_start = args.trim_start_seconds or 0.0
    trim_end = args.trim_end_seconds or 0.0
    if trim_start or trim_end:
        end = duration - trim_end
        if trim_start >= end:
            raise ExportError(
                f"Trim removes the whole clip ({trim_start:.3f}s start, {trim_end:.3f}s end, "
                f"{duration:.3f}s source)."
            )
        filters.append(f"atrim=start={trim_start:.6f}:end={end:.6f}")
        filters.append("asetpts=PTS-STARTPTS")
    output_duration = duration - trim_start - trim_end
    if args.fade_in_ms:
        fade = args.fade_in_ms / 1000.0
        if fade > output_duration:
            raise ExportError("Fade-in duration cannot exceed the trimmed clip duration.")
        filters.append(f"afade=t=in:st=0:d={fade:.6f}")
    if args.fade_out_ms:
        fade = args.fade_out_ms / 1000.0
        if fade > output_duration:
            raise ExportError("Fade-out duration cannot exceed the trimmed clip duration.")
        filters.append(f"afade=t=out:st={output_duration - fade:.6f}:d={fade:.6f}")
    if args.loudness_lufs is not None:
        filters.append(
            f"loudnorm=I={args.loudness_lufs:.2f}:TP={args.loudness_true_peak:.2f}:LRA=11"
        )
    if args.limiter_dbfs is not None:
        limit = 10 ** (args.limiter_dbfs / 20.0)
        filters.append(f"alimiter=limit={limit:.9f}:level=false")
    return filters, output_duration


def processing_steps(args, fmt, output_duration):
    steps = [f"FFmpeg transcode to {fmt.upper()}"]
    if args.output_rate:
        steps.append(f"resample to {args.output_rate} Hz")
    if args.trim_start_seconds:
        steps.append(f"trim {args.trim_start_seconds:.3f} seconds from start")
    if args.trim_end_seconds:
        steps.append(f"trim {args.trim_end_seconds:.3f} seconds from end")
    if args.fade_in_ms:
        steps.append(f"fade in over {args.fade_in_ms} ms")
    if args.fade_out_ms:
        steps.append(f"fade out over {args.fade_out_ms} ms")
    if args.loudness_lufs is not None:
        steps.append(
            f"FFmpeg loudnorm target {args.loudness_lufs:.2f} LUFS, "
            f"{args.loudness_true_peak:.2f} dBTP"
        )
    if args.limiter_dbfs is not None:
        steps.append(f"FFmpeg alimiter ceiling {args.limiter_dbfs:.2f} dBFS")
    return steps, output_duration


def transcode(ffmpeg, source, destination, fmt, args, source_probe):
    filters, output_duration = ffmpeg_filters(args, source_probe["duration_seconds"])
    extension, codec = FORMAT_CONFIG[fmt]
    if destination.suffix.lower() != extension:
        raise ExportError(f"Internal extension mismatch for requested format {fmt}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-hide_banner", "-nostdin", "-n", "-i", str(source)]
    if filters:
        command.extend(["-af", ",".join(filters)])
    if args.output_rate:
        command.extend(["-ar", str(args.output_rate)])
    command.extend(codec)
    command.extend(synthetic_label.ffmpeg_args())
    muxer = {"ogg": "ogg", "mp3": "mp3", "m4a": "ipod"}.get(fmt)
    if muxer:
        command.extend(["-f", muxer])
    command.append(str(destination))
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ExportError(
            f"FFmpeg failed for {source} -> {destination} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    if not destination.is_file() or destination.stat().st_size == 0:
        raise ExportError(f"FFmpeg reported success but did not create {destination}.")
    return output_duration


def copy_original(source, destination, expected_hash):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Path(source).open("rb") as src, destination.open("xb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    copied_hash = sha256_file(destination)
    if copied_hash != expected_hash or sha256_file(source) != expected_hash:
        raise ExportError(f"Original waveform hash changed during copy: {source}")
    return copied_hash


def read_audio_mono(path):
    audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    if not audio.size or not np.isfinite(audio).all():
        raise ExportError(f"Audio is empty or non-finite: {path}")
    mono = audio.mean(axis=1).astype(np.float32) if audio.shape[1] > 1 else audio[:, 0]
    return mono, int(sample_rate)


def resample_audio(audio, source_rate, target_rate):
    if source_rate == target_rate:
        return audio
    divisor = math.gcd(source_rate, target_rate)
    return resample_poly(audio, target_rate // divisor, source_rate // divisor).astype(np.float32)


def write_pcm16(path, audio, sample_rate):
    path.parent.mkdir(parents=True, exist_ok=True)
    synthetic_label.write_wav(path, audio, sample_rate)
    if not path.is_file() or path.stat().st_size == 0:
        raise ExportError(f"Could not write PCM WAV: {path}")


def format_timestamp(seconds, decimal=","):
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    whole_seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{decimal}{millis:03d}"


def subtitle_text(captions):
    srt_lines = []
    vtt_lines = ["WEBVTT", ""]
    for index, caption in enumerate(captions, 1):
        start = format_timestamp(caption["start_seconds"])
        end = format_timestamp(caption["end_seconds"])
        text = f"{caption['character_name']}: {caption['text']}" if caption.get("show_speaker", True) else caption["text"]
        srt_lines.extend([str(index), f"{start} --> {end}", text, ""])
        vtt_lines.extend([
            caption["line_id"],
            f"{format_timestamp(caption['start_seconds'], '.')}"
            f" --> {format_timestamp(caption['end_seconds'], '.')}",
            text, "",
        ])
    return "\n".join(srt_lines), "\n".join(vtt_lines)


def write_subtitles(path_srt, path_vtt, captions):
    srt, vtt = subtitle_text(captions)
    path_srt.write_text(srt, encoding="utf-8")
    path_vtt.write_text(vtt, encoding="utf-8")


def caption_cues_for_scene(timed_captions, selected):
    """Caption cues on the scene clock. A line whose takes all have word times is cut into phrases from those times;
    any other line keeps its one line-level caption. The phrases follow the scene mix's pauses and take order."""
    takes_by_line = {item["line"]["line_id"]: item["takes"] for item in selected}
    cues = []
    for caption in timed_captions:
        takes = takes_by_line[caption["line_id"]]
        timings = [take.get("word_timings") for take in takes]
        if not takes or not all(timing and timing.get("words") for timing in timings):
            cues.append({**caption, "show_speaker": True})
            continue
        joiner = word_timing.joiner_for(word_timing.whisper_code(takes[0].get("language")))
        words = word_timing.shifted_words(
            [(take["probe"]["duration_seconds"], timing["words"]) for take, timing in zip(takes, timings)])
        for index, phrase in enumerate(word_timing.word_phrases(words, joiner)):
            cues.append({**caption,
                         "start_seconds": caption["start_seconds"] + phrase["start_seconds"],
                         "end_seconds": caption["start_seconds"] + phrase["end_seconds"],
                         "text": phrase["text"], "show_speaker": index == 0})
    return cues


def trimmed_captions(captions, duration, trim_start=0.0, trim_end=0.0):
    end = duration - trim_start - trim_end
    if end <= 0 or trim_start < 0 or trim_end < 0:
        raise ExportError("Invalid scene caption trim interval.")
    result = []
    for caption in captions:
        start = max(0.0, caption["start_seconds"] - trim_start)
        stop = min(end, caption["end_seconds"] - trim_start)
        if stop > start:
            result.append({**caption, "start_seconds": start, "end_seconds": stop})
    return result


def verify_caption_sets(export_root, manifest):
    """Check caption clocks, text, and explicit associations with delivery audio."""
    scene_mix = manifest["scene_mix"]
    sets = manifest.get("caption_sets")
    transforms = manifest.get("transforms", {})
    trim_start = transforms.get("trim_start_seconds", 0.0) or 0.0
    trim_end = transforms.get("trim_end_seconds", 0.0) or 0.0
    if not sets:
        if trim_start or trim_end:
            raise ExportError("Legacy trimmed export has no captions associated with delivered audio; re-export to a new package.")
        sets = [{"audio_path": scene_mix["path"], "srt_path": "scene/scene.srt", "vtt_path": "scene/scene.vtt",
                 "trim_start_seconds": 0.0, "trim_end_seconds": 0.0}]
    else:
        expected = {r["output_path"] for r in manifest["assets"] if r["asset_type"].startswith("scene_mix_")}
        if len(sets) != len(expected) or {item["audio_path"] for item in sets} != expected:
            raise ExportError("Caption associations do not cover each scene audio variant exactly once.")
    cues = scene_mix.get("caption_cues", scene_mix["lines"])
    for item in sets:
        raw = item["audio_path"] == scene_mix["path"]
        start, end = (0.0, 0.0) if raw else (trim_start, trim_end)
        if item.get("trim_start_seconds", 0.0) != start or item.get("trim_end_seconds", 0.0) != end:
            raise ExportError("Caption trim mapping does not match the associated audio transform.")
        expected_srt, expected_vtt = subtitle_text(trimmed_captions(cues, scene_mix["duration_seconds"], start, end))
        for field, expected_text in (("srt_path", expected_srt), ("vtt_path", expected_vtt)):
            path = under_root(export_root, item[field])
            if not path.is_file() or path.read_text(encoding="utf-8") != expected_text:
                raise ExportError(f"Caption timing/text mismatch for {item['audio_path']}: {item[field]}")


def line_fit(duration_seconds, target_ms):
    """Whether a line's take fits its target duration: within the slot, stretchable within the bound, or needing a
    re-render. Advisory only: the scene mix does not stretch any audio, so the deliverables keep the take as rendered."""
    if not target_ms:
        return None
    return fit_status(duration_seconds, target_ms / 1000.0)


def make_scene_mix(root, selected, output_path):
    output_rate = selected[0]["takes"][0]["probe"]["sample_rate"]
    parts = []
    line_results = []
    current_sample = 0
    for item in selected:
        line = item["line"]
        rate = output_rate
        before = int(line.get("pause_before_ms") or 0)
        after = int(line.get("pause_after_ms") or 0)
        if before:
            silence = round(rate * before / 1000)
            parts.append(np.zeros(silence, dtype=np.float32))
            current_sample += silence
        line_start_sample = current_sample
        line_audio = []
        for take in item["takes"]:
            audio, source_rate = read_audio_mono(take["media_path"])
            audio = resample_audio(audio, source_rate, rate)
            parts.append(audio)
            line_audio.append(audio)
            current_sample += len(audio)
        line_samples = sum(len(part) for part in line_audio)
        character_name = item["takes"][0]["character_name"]
        line_results.append({
            "line_id": line["line_id"],
            "character_id": line["character_id"],
            "identity_id": line["identity_id"],
            "character_name": character_name,
            "text": line["text"],
            "selected_take_ids": [take["id"] for take in item["takes"]],
            "segment_ids": [take["segment_id"] for take in item["takes"]],
            "start_seconds": line_start_sample / rate,
            "end_seconds": (line_start_sample + line_samples) / rate,
            "duration_seconds": line_samples / rate,
            "pause_before_ms": before,
            "pause_after_ms": after,
            "target_duration_ms": line.get("target_duration_ms"),
            "fit": line_fit(line_samples / rate, line.get("target_duration_ms")),
        })
        if after:
            silence = round(rate * after / 1000)
            parts.append(np.zeros(silence, dtype=np.float32))
            current_sample += silence
    mix = np.concatenate(parts) if parts else np.array([], dtype=np.float32)
    if not mix.size:
        raise ExportError("Scene has no selected audio.")
    write_pcm16(output_path, mix, output_rate)
    return {
        "sample_rate": output_rate,
        "channels": 1,
        "duration_seconds": len(mix) / output_rate,
        "lines": line_results,
        "assembly": "selected take segments in scene order with explicit configured silence; no crossfades or mastering",
        "processing_steps": ["resampled segments to first selected take sample rate where needed", "concatenated segments", "inserted scene pauses", "wrote PCM 16-bit WAV"],
    }


def run_preflight(args):
    root = Path(args.project_root).resolve()
    scene = load_scene(root, args.scene_id)
    selected = load_takes(root, scene)
    character_ids = {item["line"].get("character_id") for item in selected}
    if args.character_id and args.character_id not in character_ids:
        raise ExportError(f"Character {args.character_id} has no selected lines in scene {args.scene_id}.")
    if args.character_id and not any(
        item["line"].get("character_id") == args.character_id for item in selected
    ):
        raise ExportError("Selected character pack has no lines.")
    output_stems = set()
    for item in selected:
        take = item["takes"][0]
        stem = filename_from_template(
            args.filename_template, line_id=item["line"]["line_id"],
            character_name=take["character_name"], take_id=take["id"],
        )
        key = (item["line"]["character_id"], stem)
        if key in output_stems:
            raise ExportError(
                f"Filename template collides for multiple lines ({stem}); include {{line_id}} or {{take_id}}."
            )
        output_stems.add(key)
    args.ffmpeg = ffmpeg_tools.resolve(args.ffmpeg, "ffmpeg")
    args.ffprobe = ffmpeg_tools.resolve(args.ffprobe, "ffprobe")
    formats = args.formats or ["wav"]
    unknown = sorted(set(formats) - FORMATS)
    if unknown:
        raise ExportError(f"Unsupported format(s): {', '.join(unknown)}")
    problem = ffmpeg_tools.cannot_write(formats, ffmpeg_tools.capabilities(args.ffmpeg, args.ffprobe))
    if problem:
        raise ExportError(problem)
    for item in selected:
        for take in item["takes"]:
            take["probe"] = probe_media(take["media_path"], args.ffprobe)
            if take["probe"]["sample_rate"] != int(take["sample_rate"]):
                raise ExportError(
                    f"Take {take['id']} sample rate differs from its Cast record "
                    f"({take['probe']['sample_rate']} vs {take['sample_rate']})."
                )
    if args.output_rate is not None and not 8000 <= args.output_rate <= 192000:
        raise ExportError("--output-rate must be between 8000 and 192000 Hz.")
    rates = {args.output_rate} if args.output_rate else {item["takes"][0]["probe"]["sample_rate"] for item in selected}
    for fmt, allowed in ENCODER_RATES.items():
        unsupported = sorted(rate for rate in rates if fmt in formats and rate not in allowed)
        if unsupported:
            raise ExportError(
                f"{ffmpeg_tools.FORMAT_LABEL[fmt]} cannot be written at {', '.join(f'{rate} Hz' for rate in unsupported)}. "
                f"Set the sample rate to one of {', '.join(str(rate) for rate in allowed)} Hz, or leave "
                f"{ffmpeg_tools.FORMAT_LABEL[fmt]} out.")
    if args.loudness_true_peak is not None and args.loudness_lufs is None:
        raise ExportError("--loudness-true-peak requires --loudness-lufs.")
    if args.loudness_lufs is not None and args.loudness_true_peak is None:
        args.loudness_true_peak = -1.0
    for name in ("trim_start_seconds", "trim_end_seconds"):
        value = getattr(args, name)
        if value is not None and value < 0:
            raise ExportError(f"--{name.replace('_', '-')} must be zero or greater.")
    if args.fade_in_ms < 0 or args.fade_out_ms < 0:
        raise ExportError("Fade lengths must be zero or greater.")
    if args.loudness_lufs is not None and not -70 <= args.loudness_lufs <= -5:
        raise ExportError("--loudness-lufs must be between -70 and -5 LUFS.")
    if args.loudness_true_peak is not None and not -9 <= args.loudness_true_peak <= 0:
        raise ExportError("--loudness-true-peak must be between -9 and 0 dBTP.")
    if args.limiter_dbfs is not None and not -24 <= args.limiter_dbfs <= 0:
        raise ExportError("--limiter-dbfs must be between -24 and 0 dBFS.")
    if args.trim_start_seconds or args.trim_end_seconds or args.fade_in_ms or args.fade_out_ms or args.loudness_lufs is not None or args.limiter_dbfs is not None:
        # Validate duration bounds for every assembled line before creating output files.
        for item in selected:
            duration = sum(t["probe"]["duration_seconds"] for t in item["takes"])
            ffmpeg_filters(args, duration)
    return root, scene, selected


def _asset_record(*, root, asset_type, scene, line=None, take=None, output_path, output_hash,
                  media, processing_steps, source_hash=None, license_data=None):
    model = (take or {}).get("model", {})
    engine_id = (take or {}).get("engine_id") or ""
    reference = reference_provenance(root, take) if take else {}
    license_data = license_data or license_profile(engine_id, model, reference)
    return {
        "asset_type": asset_type,
        "scene_id": scene["scene_id"],
        "scene_revision": scene["revision"],
        "line_id": line.get("line_id") if line else None,
        "segment_id": take.get("segment_id") if take else None,
        "character_id": (take or {}).get("character_id") or (line or {}).get("character_id"),
        "character_name": (take or {}).get("character_name"),
        "identity_id": (take or {}).get("identity_id") or (line or {}).get("identity_id"),
        "take_id": (take or {}).get("id"),
        "selected_take_ids": (
            [t["id"] for t in line["_takes"]] if line and line.get("_takes")
            else [take["id"]] if take else []
        ),
        "engine_id": engine_id,
        "checkpoint": model.get("checkpoint") or model.get("model_id") or model.get("model"),
        "checkpoint_revision": model.get("checkpoint_revision") or model.get("revision"),
        "model": model,
        "runtime": model.get("runtime") or model.get("backend") or {},
        "take_settings": (take or {}).get("settings"),
        "reference_provenance": reference,
        "license": license_data,
        "source_audio_sha256": source_hash,
        "output_path": str(output_path),
        "output_sha256": output_hash,
        "codec": media.get("codec"),
        "sample_rate": media.get("sample_rate"),
        "channels": media.get("channels"),
        "duration_seconds": media.get("duration_seconds"),
        "processing_steps": processing_steps,
        "text": line.get("text") if line else None,
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_manifest_csv(path, records):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            writer.writerow({
                **row,
                "reference_provenance_json": json.dumps(row.get("reference_provenance"), ensure_ascii=False, sort_keys=True),
                "model_json": json.dumps(row.get("model"), ensure_ascii=False, sort_keys=True),
                "runtime_json": json.dumps(row.get("runtime"), ensure_ascii=False, sort_keys=True),
                "take_settings_json": json.dumps(row.get("take_settings"), ensure_ascii=False, sort_keys=True),
                "selected_take_ids_json": json.dumps(row.get("selected_take_ids", []), ensure_ascii=False),
                "license_json": json.dumps(row.get("license"), ensure_ascii=False, sort_keys=True),
                "processing_steps_json": json.dumps(row.get("processing_steps"), ensure_ascii=False),
                "text": (row.get("text") or "").replace("\r", " ").replace("\n", " "),
            })


def export_package(args):
    root, scene, selected = run_preflight(args)
    formats = args.formats or ["wav"]
    if len(formats) != len(set(formats)):
        raise ExportError("Each --format may be specified only once.")
    print_preflight(root, scene, selected, formats, args.character_id)
    name = args.name or (
        datetime.now(timezone.utc).strftime("export-%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    )
    if "/" in name or "\\" in name or name in {".", ".."} or ".." in name:
        raise ExportError("--name must be a single safe folder name without path traversal.")
    name = safe_component(name)
    destination = (root / "out" / "exports" / name).resolve()
    try:
        destination.relative_to((root / "out" / "exports").resolve())
    except ValueError as exc:
        raise ExportError("Export destination escapes out/exports.") from exc
    if destination.exists():
        raise ExportError(f"Export destination already exists; refusing to overwrite: {destination}")
    if args.dry_run:
        print(f"Dry run; no files written. Destination would be: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    print(f"Export destination: {destination}")
    records = []
    selected_map = []
    captions = []
    transcripts = []
    line_audio_sources = []
    line_outputs = {}
    take_original_paths = {}
    try:
        char_folders = {}
        # The phoneme rules for aligned English takes live in Kokoro's environment: one call there covers every line.
        prime_for_takes([take for item in selected for take in item["takes"]])
        for item in selected:
            line = item["line"]
            take = item["takes"][0]
            char_id = line["character_id"]
            char_name = take["character_name"]
            char_folder = char_folders.setdefault(
                char_id, safe_component(f"{char_name}-{char_id[:8]}")
            )
            stub = filename_from_template(
                args.filename_template,
                line_id=line["line_id"], character_name=char_name, take_id=take["id"],
            )
            segment_paths = []
            for seg_take in item["takes"]:
                original_path = destination / "original-takes" / "characters" / char_folder / (
                    f"{stub}__{safe_component(seg_take['segment_id'], max_length=45)}__"
                    f"{seg_take['id'][:8]}.wav"
                )
                copied_hash = copy_original(
                    seg_take["media_path"], original_path, seg_take["audio_sha256"]
                )
                take_original_paths[seg_take["id"]] = original_path
                segment_probe = probe_media(original_path, args.ffprobe)
                records.append(_asset_record(
                    root=root,
                    asset_type="original_take_wav", scene=scene, line=line, take=seg_take,
                    output_path=original_path.relative_to(destination).as_posix(),
                    output_hash=copied_hash, media=segment_probe,
                    processing_steps=["byte-for-byte copy of immutable Cast take WAV"],
                    source_hash=seg_take["audio_sha256"],
                ))
                segment_paths.append(seg_take["media_path"])
            if len(item["takes"]) == 1:
                line_source = item["takes"][0]["media_path"]
                source_probe = item["takes"][0]["probe"]
                line_source_steps = ["selected take WAV used as the one-line source"]
            else:
                source_rate = item["takes"][0]["probe"]["sample_rate"]
                line_parts = []
                for seg_take in item["takes"]:
                    audio, rate = read_audio_mono(seg_take["media_path"])
                    line_parts.append(resample_audio(audio, rate, source_rate))
                assembled = np.concatenate(line_parts)
                line_source = destination / "derived" / "line-sources" / "characters" / char_folder / f"{stub}.wav"
                write_pcm16(line_source, assembled, source_rate)
                source_probe = probe_media(line_source, args.ffprobe)
                line_source_steps = ["concatenated selected segments without added pauses", "resampled segments to first segment rate where needed", "wrote PCM 16-bit WAV"]
                records.append(_asset_record(
                    root=root,
                    asset_type="line_source_assembly", scene=scene, line=line, take=take,
                    output_path=line_source.relative_to(destination).as_posix(),
                    output_hash=sha256_file(line_source), media=source_probe,
                    processing_steps=line_source_steps,
                    source_hash=None,
                ))
            line_audio_sources.append((item, line_source, source_probe, char_folder, stub))
            start_take = item["takes"][0]
            line_outputs[line["line_id"]] = {
                "line_id": line["line_id"], "character_id": char_id, "character_name": char_name,
                "identity_id": line["identity_id"], "text": line["text"],
                "selected_take_ids": [t["id"] for t in item["takes"]],
                "segment_ids": [t["segment_id"] for t in item["takes"]],
                "original_take_paths": [
                    take_original_paths[t["id"]].relative_to(destination).as_posix()
                    for t in item["takes"]
                ],
                "deliverables": {},
            }
            track, track_reason = track_for_takes(
                item["takes"], trim_start=args.trim_start_seconds, trim_end=args.trim_end_seconds)
            if track is not None:
                track_path = destination / "lipsync" / char_folder / f"{stub}.json"
                write_json(track_path, track)
                track_output = track_path.relative_to(destination).as_posix()
                records.append(_asset_record(
                    root=root, asset_type="lip_sync_track", scene=scene, line=line, take=start_take,
                    output_path=track_output, output_hash=sha256_file(track_path),
                    media={"duration_seconds": track["duration_seconds"]},
                    processing_steps=[f"phoneme times: {track['timing']['source']}; moved to match the export trims"],
                ))
                line_outputs[line["line_id"]]["lip_sync"] = {
                    "status": "written", "timing_source": track["timing"]["source"], "path": track_output}
            else:
                line_outputs[line["line_id"]]["lip_sync"] = {"status": "not_written", "reason": track_reason}
            for fmt in formats:
                extension, _ = FORMAT_CONFIG[fmt]
                output_name = f"{stub}{extension}"
                out_path = destination / "deliverables" / fmt / "characters" / char_folder / output_name
                output_duration = transcode(args.ffmpeg, line_source, out_path, fmt, args, source_probe)
                media = probe_media(out_path, args.ffprobe)
                proc_steps, _ = processing_steps(args, fmt, output_duration)
                line_record = dict(line)
                line_record["_takes"] = item["takes"]
                records.append(_asset_record(
                    root=root,
                    asset_type=f"line_deliverable_{fmt}", scene=scene, line=line_record, take=start_take,
                    output_path=out_path.relative_to(destination).as_posix(),
                    output_hash=sha256_file(out_path), media=media,
                    processing_steps=line_source_steps + proc_steps,
                    source_hash=start_take["audio_sha256"] if len(item["takes"]) == 1 else None,
                ))
                line_outputs[line["line_id"]]["deliverables"][fmt] = out_path.relative_to(destination).as_posix()
            selected_map.append({
                "line_id": line["line_id"],
                "character_id": char_id,
                "identity_id": line["identity_id"],
                "selected_takes": {
                    t["segment_id"]: t["id"] for t in item["takes"]
                },
            })
            captions.append({
                "line_id": line["line_id"],
                "character_name": char_name,
                "text": line["text"],
            })
            transcripts.append(f"{char_name}\t{line['line_id']}\t{line['text']}")

        scene_dir = destination / "scene"
        scene_mix_path = scene_dir / "scene-mix-raw.wav"
        scene_timing = make_scene_mix(root, selected, scene_mix_path)
        scene_probe = probe_media(scene_mix_path, args.ffprobe)
        scene_license_profiles = {}
        for item in selected:
            first = item["takes"][0]
            scene_license_profiles[first["engine_id"]] = license_profile(
                first["engine_id"], first["model"], reference_provenance(root, first)
            )
        scene_record = _asset_record(
            root=root,
            asset_type="scene_mix_raw_wav", scene=scene, line=None, take=None,
            output_path=scene_mix_path.relative_to(destination).as_posix(),
            output_hash=sha256_file(scene_mix_path), media=scene_probe,
            processing_steps=scene_timing["processing_steps"],
        )
        scene_record["selected_take_ids"] = [
            take["id"] for item in selected for take in item["takes"]
        ]
        scene_record["line_selections"] = selected_map
        scene_record["license_profiles"] = scene_license_profiles
        records.append(scene_record)
        for fmt in formats:
            extension, _ = FORMAT_CONFIG[fmt]
            out_path = scene_dir / f"scene-mix{extension}"
            output_duration = transcode(args.ffmpeg, scene_mix_path, out_path, fmt, args, scene_probe)
            media = probe_media(out_path, args.ffprobe)
            proc_steps, _ = processing_steps(args, fmt, output_duration)
            record = _asset_record(
                root=root,
                asset_type=f"scene_mix_{fmt}", scene=scene, line=None, take=None,
                output_path=out_path.relative_to(destination).as_posix(),
                output_hash=sha256_file(out_path), media=media,
                processing_steps=["source: raw scene mix"] + proc_steps,
            )
            record["selected_take_ids"] = scene_record["selected_take_ids"]
            record["line_selections"] = selected_map
            record["license_profiles"] = scene_license_profiles
            records.append(record)

        # The scene mix is built from exact selected takes. Use its measured segment
        # lengths and explicit pauses as the line-level caption clock.
        caption_map = {caption["line_id"]: caption for caption in captions}
        timed_captions = []
        for measured in scene_timing["lines"]:
            caption = caption_map[measured["line_id"]]
            timed_captions.append({**caption, **measured})
        cue_captions = caption_cues_for_scene(timed_captions, selected)
        scene_timing["caption_cues"] = cue_captions
        write_subtitles(scene_dir / "scene.srt", scene_dir / "scene.vtt", cue_captions)
        caption_sets = [{"audio_path": scene_mix_path.relative_to(destination).as_posix(),
                         "srt_path": "scene/scene.srt", "vtt_path": "scene/scene.vtt",
                         "trim_start_seconds": 0.0, "trim_end_seconds": 0.0}]
        for fmt in formats:
            extension, _ = FORMAT_CONFIG[fmt]
            srt, vtt = scene_dir / f"scene-mix-{fmt}.srt", scene_dir / f"scene-mix-{fmt}.vtt"
            adjusted = trimmed_captions(cue_captions, scene_timing["duration_seconds"],
                                        args.trim_start_seconds, args.trim_end_seconds)
            write_subtitles(srt, vtt, adjusted)
            caption_sets.append({"audio_path": f"scene/scene-mix{extension}",
                "srt_path": srt.relative_to(destination).as_posix(), "vtt_path": vtt.relative_to(destination).as_posix(),
                "trim_start_seconds": args.trim_start_seconds, "trim_end_seconds": args.trim_end_seconds})
        (destination / "transcript.txt").write_text("\n".join(transcripts) + "\n", encoding="utf-8")

        # Save a portable, checksummed source-scene snapshot plus a compact exact
        # selection index. Verification reparses this as an import/reselection pass.
        write_json(destination / "scene_manifest.json", scene)
        selection_payload = {
            "schema_version": 1,
            "scene_id": scene["scene_id"],
            "scene_revision": scene["revision"],
            "scene_checksum": scene["checksum"],
            "lines": selected_map,
        }
        selection_payload["checksum"] = digest_json(selection_payload)
        write_json(destination / "selection_manifest.json", selection_payload)
        for char_id, char_folder in char_folders.items():
            selected_lines = [
                line_outputs[line["line_id"]] for line in
                (item["line"] for item in selected)
                if line.get("character_id") == char_id
            ]
            char_meta = next(
                item["takes"][0] for item in selected
                if item["line"]["character_id"] == char_id
            )
            pack = {
                "schema_version": 1,
                "scene_id": scene["scene_id"],
                "character_id": char_id,
                "character_name": char_meta["character_name"],
                "identity_ids": sorted({line["identity_id"] for line in selected_lines}),
                "line_count": len(selected_lines),
                "lines": selected_lines,
                "license_profiles": {
                    item["takes"][0]["engine_id"]: license_profile(
                        item["takes"][0]["engine_id"], item["takes"][0]["model"],
                        reference_provenance(root, item["takes"][0])
                    )
                    for item in selected if item["line"]["character_id"] == char_id
                },
            }
            pack_path = destination / "character-packs" / char_folder / "character-pack.json"
            write_json(pack_path, pack)
            pack_transcript = [
                f"{char_meta['character_name']}\t{line['line_id']}\t{line['text']}"
                for line in selected_lines
                for item in selected if item["line"]["line_id"] == line["line_id"]
            ]
            pack_path.with_name("transcript.txt").write_text(
                "\n".join(pack_transcript) + "\n", encoding="utf-8"
            )

        manifest = {
            "schema_version": 1,
            "export_id": name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scene": {
                "scene_id": scene["scene_id"], "scene_revision": scene["revision"],
                "scene_checksum": scene["checksum"], "name": scene["name"],
            },
            "selection": selected_map,
            "formats": formats,
            "caption_sets": caption_sets,
            "filename_template": args.filename_template,
            "transforms": {
                "sample_rate_hz": args.output_rate,
                "trim_start_seconds": args.trim_start_seconds,
                "trim_end_seconds": args.trim_end_seconds,
                "fade_in_ms": args.fade_in_ms,
                "fade_out_ms": args.fade_out_ms,
                "loudness_lufs": args.loudness_lufs,
                "loudness_true_peak_dbtp": args.loudness_true_peak,
                "limiter_dbfs": args.limiter_dbfs,
            },
            # Every delivered file and the scene mix carry this comment (synthetic_label.py); verify checks it is there.
            "synthetic_label": synthetic_label.comment(),
            "timing": {
                "source": "sample-count durations from decoded selected segments, validated with ffprobe, plus scene pauses",
                "caption_granularity": "word phrases where every take of a line has stored word times; one caption per line otherwise",
                "word_timing": "stored forced-alignment records from align_takes.py; their checks are in each take's word_timings",
                "lip_sync": "per-line mouth-shape tracks: Kokoro takes use their stored phoneme times; other English takes use aligned words shared over estimated phoneme times",
            },
            "scene_mix": {
                **scene_timing,
                "path": scene_mix_path.relative_to(destination).as_posix(),
                "sha256": sha256_file(scene_mix_path),
            },
            "character_packs": [
                {
                    "character_id": char_id,
                    "folder": char_folder,
                    "line_count": sum(item["line"]["character_id"] == char_id for item in selected),
                }
                for char_id, char_folder in char_folders.items()
            ],
            "assets": records,
            "source_project": str(root),
        }
        manifest["checksum"] = digest_json(manifest)
        write_json(destination / "manifest.json", manifest)
        write_manifest_csv(destination / "manifest.csv", records)
        print(f"Export complete: {destination}")
        print(f"Scene lines: {len(selected)}; original take WAVs: {sum(len(i['takes']) for i in selected)}")
        print(f"Deliverables: {len(selected) * len(formats)} line files + {len(formats)} scene mixes")
        print(f"Scene mix: {scene_mix_path} ({scene_probe['duration_seconds']:.3f}s, {scene_probe['sample_rate']} Hz)")
        for char_id, char_folder in char_folders.items():
            count = sum(item["line"]["character_id"] == char_id for item in selected)
            print(f"Character pack: {destination / 'character-packs' / char_folder} ({count} lines)")
    except Exception as exc:
        marker = destination / "INCOMPLETE.txt"
        marker.write_text(f"Export stopped with {type(exc).__name__}: {exc}\n", encoding="utf-8")
        raise


def verify_export(args):
    root = Path(args.project_root).resolve()
    export_root = Path(args.export_dir).expanduser().resolve()
    manifest_path = export_root / "manifest.json"
    selection_path = export_root / "selection_manifest.json"
    scene_path = export_root / "scene_manifest.json"
    if not manifest_path.is_file() or not selection_path.is_file() or not scene_path.is_file():
        raise ExportError("Export is missing manifest.json, selection_manifest.json, or scene_manifest.json.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_checksum = manifest.pop("checksum", None)
    if manifest_checksum != digest_json(manifest):
        raise ExportError("Export manifest checksum mismatch.")
    manifest["checksum"] = manifest_checksum
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    scene = json.loads(scene_path.read_text(encoding="utf-8"))
    expected_checksum = selection.pop("checksum", None)
    if expected_checksum != digest_json(selection):
        raise ExportError("Export selection manifest checksum mismatch.")
    selection["checksum"] = expected_checksum
    scene_checksum = scene.pop("checksum", None)
    if scene_checksum != digest_json(scene):
        raise ExportError("Re-imported scene snapshot checksum mismatch.")
    scene["checksum"] = scene_checksum
    load_takes(root, scene)  # Validate current selection inputs, not just file hashes.
    if scene["scene_id"] != selection["scene_id"] or scene["revision"] != selection["scene_revision"]:
        raise ExportError("Selection snapshot does not match the scene manifest.")
    imported = []
    for line in scene["lines"]:
        takes = line.get("selected_takes") or {}
        if not takes and len(line.get("segments", [])) == 1 and line.get("selected_take_id"):
            takes = {line["segments"][0]["id"]: line["selected_take_id"]}
        imported.append({
            "line_id": line["line_id"], "character_id": line["character_id"],
            "identity_id": line["identity_id"], "selected_takes": takes,
        })
    if imported != selection["lines"] or imported != manifest["selection"]:
        raise ExportError("Re-imported scene selections differ from the exported selected-take mapping.")
    take_ids = [
        take_id for line in imported for take_id in line["selected_takes"].values()
    ]
    with read_only_db(root / "data" / "cast" / "cast.sqlite3") as db:
        db.row_factory = sqlite3.Row
        for take_id in take_ids:
            row = db.execute("SELECT audio_path,audio_sha256 FROM takes WHERE id=?", (take_id,)).fetchone()
            if row is None:
                raise ExportError(f"Re-imported selection references missing Cast take {take_id}.")
            source = under_root(root, row["audio_path"])
            actual = sha256_file(source)
            if actual != row["audio_sha256"]:
                raise ExportError(f"Original Cast waveform changed after export: {take_id}")
            matching = [r for r in manifest["assets"] if r.get("take_id") == take_id and r["asset_type"] == "original_take_wav"]
            if not matching:
                raise ExportError(f"Export is missing a byte-preserved original for selected take {take_id}.")
            for record in matching:
                if record.get("source_audio_sha256") != actual:
                    raise ExportError(f"Manifest source hash does not match selected take {take_id}.")
                copy_path = (export_root / record["output_path"]).resolve()
                if not copy_path.is_file() or sha256_file(copy_path) != actual:
                    raise ExportError(f"Original export hash does not match selected take {take_id}.")
    ffprobe = ffmpeg_tools.find_ffprobe()
    if ffprobe is None and any(item["asset_type"].startswith(("line_deliverable_", "scene_mix_")) for item in manifest["assets"]):
        raise ExportError("Checking a package's audio needs ffprobe, which comes with FFmpeg. " + ffmpeg_tools.INSTALL_HINT)
    checked = 0
    for record in manifest["assets"]:
        asset_path = (export_root / record["output_path"]).resolve() if record.get("output_path") else None
        if asset_path is None:
            continue
        try:
            asset_path.relative_to(export_root)
        except ValueError as exc:
            raise ExportError(f"Export path escapes the package: {record['output_path']}") from exc
        if not asset_path.is_file() or sha256_file(asset_path) != record["output_sha256"]:
            raise ExportError(f"Export asset is missing or hash-invalid: {record['output_path']}")
        if record["asset_type"] == "lip_sync_track":
            try:
                check_track(json.loads(asset_path.read_text(encoding="utf-8")))
            except ValueError as exc:
                raise ExportError(f"Lip-sync track is malformed: {record['output_path']} ({exc})") from exc
        if record["asset_type"].startswith(("line_deliverable_", "scene_mix_")):
            media = probe_media(asset_path, ffprobe)
            expected_ext = record["asset_type"].rsplit("_", 1)[-1]
            if expected_ext in FORMAT_CODEC:
                if media["codec"] != FORMAT_CODEC[expected_ext]:
                    raise ExportError(f"{ffmpeg_tools.FORMAT_LABEL[expected_ext]} deliverable codec mismatch: "
                                      f"{record['output_path']} ({media['codec']})")
                if abs(media["duration_seconds"] - record["duration_seconds"]) > 0.03:
                    raise ExportError(f"Duration changed after export: {record['output_path']}")
                # A package exported before files were labelled says nothing about a label, and is checked without one.
                if manifest.get("synthetic_label") and not synthetic_label.is_labelled(media["comment"]):
                    raise ExportError(f"The AI-generated speech label is missing from {record['output_path']}.")
        if record["asset_type"].startswith(("original_take_wav", "line_deliverable_", "scene_mix_")):
            if not record.get("license"):
                raise ExportError(f"Audio asset is missing license/provenance classification: {record['output_path']}")
        checked += 1
    line_ids = [line["line_id"] for line in scene["lines"]]
    srt_path, vtt_path = export_root / "scene" / "scene.srt", export_root / "scene" / "scene.vtt"
    if not srt_path.is_file() or not vtt_path.is_file():
        raise ExportError("Scene subtitles are missing.")
    srt_blocks = [block for block in srt_path.read_text(encoding="utf-8").strip().split("\n\n") if block.strip()]
    vtt_blocks = [
        block for block in vtt_path.read_text(encoding="utf-8").split("\n\n")[1:]
        if block.strip()
    ]
    vtt_ids = [block.splitlines()[0].strip() for block in vtt_blocks if block.splitlines()]
    if len(srt_blocks) != len(line_ids) or len(vtt_ids) != len(line_ids) or vtt_ids != line_ids:
        raise ExportError("SRT/VTT cue count or stable line-ID mapping does not match the scene.")
    verify_caption_sets(export_root, manifest)
    transcript_path = export_root / "transcript.txt"
    if not transcript_path.is_file() or len(transcript_path.read_text(encoding="utf-8").splitlines()) != len(line_ids):
        raise ExportError("Transcript line count does not match the scene.")
    for pack_info in manifest.get("character_packs", []):
        pack_path = export_root / "character-packs" / pack_info["folder"] / "character-pack.json"
        if not pack_path.is_file():
            raise ExportError(f"Character pack is missing: {pack_path}")
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
        expected_count = sum(line["character_id"] == pack_info["character_id"] for line in imported)
        if pack["line_count"] != expected_count or len(pack["lines"]) != expected_count:
            raise ExportError(f"Character pack line count mismatch: {pack_path}")
    print("Export verification passed")
    print(f"Re-imported selected scene: {scene['scene_id']} revision {scene['revision']}")
    print(f"Selected line/segment takes reproduced: {len(take_ids)}")
    print(f"Assets hash-checked: {checked}; original take WAVs match Cast and copied hashes.")
    print(f"Subtitles/transcript verified: {len(line_ids)} line-level cues; no word timing.")
    print("Deliverable codecs (WAV, FLAC, Ogg, MP3, AAC), durations and the AI-generated speech label were verified with "
          "ffprobe where present.")


def add_processing_arguments(parser):
    parser.add_argument("--output-rate", type=int, help="Explicit deliverable sample rate, 8000–192000 Hz.")
    parser.add_argument("--loudness-lufs", type=float, help="Explicit FFmpeg loudnorm target, -70 through -5 LUFS.")
    parser.add_argument("--loudness-true-peak", type=float, help="Loudnorm true-peak target; requires --loudness-lufs (defaults to -1 dBTP).")
    parser.add_argument("--limiter-dbfs", type=float, help="Explicit FFmpeg limiter ceiling, -24 through 0 dBFS.")
    parser.add_argument("--trim-start-seconds", type=float, default=0.0, help="Explicitly trim this amount from each line's start.")
    parser.add_argument("--trim-end-seconds", type=float, default=0.0, help="Explicitly trim this amount from each line's end.")
    parser.add_argument("--fade-in-ms", type=int, default=0, help="Explicit line-level fade-in length.")
    parser.add_argument("--fade-out-ms", type=int, default=0, help="Explicit line-level fade-out length.")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(ROOT), help="Fel's Voice Lab root (defaults to this project).")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export", help="Export a selected scene and optional character pack.")
    export.add_argument("--scene-id", required=True)
    export.add_argument("--character-id", help="Cast character to report in the preflight; packs are written for every scene character.")
    export.add_argument("--format", dest="formats", action="append", choices=sorted(FORMATS), help="Deliverable format; repeat for multiple. Original WAVs are always preserved.")
    export.add_argument("--name", help="Safe, unique folder name below out/exports; an existing folder is never overwritten.")
    export.add_argument("--filename-template", default="{line_id}", help="Line file name; placeholders: {line_id}, {character}, {take_id}.")
    export.add_argument("--dry-run", action="store_true", help="Show provenance and destination without writing files.")
    export.add_argument("--ffmpeg", help="FFmpeg executable or PATH name.")
    export.add_argument("--ffprobe", help="FFprobe executable or PATH name.")
    add_processing_arguments(export)
    verify = subparsers.add_parser("verify", help="Verify package hashes, source-waveform preservation, ffprobe metadata, and scene re-import.")
    verify.add_argument("export_dir")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            export_package(args)
        else:
            verify_export(args)
    except (ExportError, OSError, sqlite3.Error, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
