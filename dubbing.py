"""Immutable local media and verified deliveries for Voice Lab's timed scenes."""

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import ffmpeg_tools
from scene_store import _path_lock, canonical_json, digest_json
from timed_scene import SAMPLE_RATE
from tools.scene_export import source_caption_text, subtitle_text

MAX_MEDIA_BYTES = 512 * 1024 * 1024
MAX_SECONDS = 1800
FORMATS = {
    ".mp4": "mov",
    ".mov": "mov",
    ".m4a": "mov",
    ".mkv": "matroska",
    ".webm": "matroska",
    ".wav": "wav",
    ".mp3": "mp3",
    ".flac": "flac",
    ".ogg": "ogg",
}
LANG_TAGS = {
    "en": "eng",
    "es": "spa",
    "fr": "fra",
    "de": "deu",
    "it": "ita",
    "pt": "por",
    "ja": "jpn",
    "ko": "kor",
    "zh": "zho",
    "ru": "rus",
    "ar": "ara",
    "hi": "hin",
    "nl": "nld",
}
TYPES = {
    "mp4": "video/mp4",
    "wav": "audio/wav",
    "txt": "text/plain; charset=utf-8",
    "srt": "application/x-subrip",
    "ass": "text/plain; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "json": "application/json",
}
RANGE_CHUNK = 256 * 1024


def write_json_atomic(path, value):
    temp = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(canonical_json(value) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _run(command, timeout=240, *, data=None, cwd=None):
    try:
        result = subprocess.run(command, input=data, capture_output=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("FFmpeg or ffprobe took too long to finish.") from exc
    except OSError as exc:
        raise ValueError(f"Could not start the media tool: {exc}") from exc
    if result.returncode:
        reason = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no details"]
        raise ValueError(f"The media tool could not finish: {reason[0][:360]}")
    return result.stdout


def _tool(name):
    path = ffmpeg_tools.find_ffmpeg() if name == "ffmpeg" else ffmpeg_tools.find_ffprobe()
    if not path:
        raise ValueError("Install FFmpeg and ffprobe in System & backups to work with dubbing media.")
    return path


def _options(kind):
    return ["-protocol_whitelist", "file", "-f", kind, *(["-enable_drefs", "0"] if kind == "mov" else [])]


def _ffmpeg(*args, cwd=None, data=None):
    return _run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", *map(str, args)], cwd=cwd, data=data)


def change_tempo(audio, speed):
    raw = _ffmpeg(
        "-f",
        "f32le",
        "-ar",
        SAMPLE_RATE,
        "-ac",
        1,
        "-i",
        "pipe:0",
        "-af",
        f"atempo={speed:.8f},apad",
        "-t",
        f"{len(audio) / SAMPLE_RATE / speed:.8f}",
        "-f",
        "f32le",
        "pipe:1",
        data=audio.astype("<f4", copy=False).tobytes(),
    )
    return np.frombuffer(raw, dtype="<f4").copy()


def _probe(path, kind):
    raw = _run(
        [_tool("ffprobe"), "-v", "error", *_options(kind), "-show_streams", "-show_format", "-of", "json", str(path)], 30
    )
    try:
        result = json.loads(raw)
        seconds = float(result["format"]["duration"])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("The media has no readable duration.") from exc
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_SECONDS + 0.1:
        raise ValueError("Dubbing media must be between 0 and 30 minutes.")
    return seconds, result["streams"]


def _audio_visual(streams):
    video = next(
        (row for row in streams if row["codec_type"] == "video" and not row.get("disposition", {}).get("attached_pic")), None
    )
    audio = next((row for row in streams if row["codec_type"] == "audio"), None)
    return video, audio


def _prepare(source, folder, kind):
    seconds, streams = _probe(source, kind)
    original_video, original_audio = _audio_visual(streams)
    if not original_video and not original_audio:
        raise ValueError("The file has no playable video or audio.")
    if original_video and (original_video.get("width", 0) * original_video.get("height", 0) > 3840 * 2160):
        raise ValueError("Dubbing supports video up to 4K in the initial workspace.")
    base = [*_options(kind), "-i", source, "-map_metadata", "-1", "-map_chapters", "-1"]
    filename = "original.mp4" if original_video else "original.wav"
    video_copy = bool(original_video and original_video["codec_name"] == "h264" and original_video.get("pix_fmt") == "yuv420p")
    args = []
    if original_video:
        args = (
            ["-map", f"0:{original_video['index']}", "-c:v", "copy"]
            if video_copy
            else [
                "-map",
                f"0:{original_video['index']}",
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-vf",
                "scale=ceil(iw/2)*2:ceil(ih/2)*2",
                "-pix_fmt",
                "yuv420p",
            ]
        )
    if original_audio:
        args += [
            "-map",
            f"0:{original_audio['index']}",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-c:a",
            "aac" if original_video else "pcm_s16le",
        ]
    if original_video:
        args += ["-movflags", "+faststart"]
    _ffmpeg(*base, *args, "-t", f"{seconds:.6f}", folder / filename)
    working = folder / filename
    _, preview_streams = _probe(working, "mov" if original_video else "wav")
    normalized_video, _ = _audio_visual(preview_streams)
    # A source with a shorter picture than its audio still uses the picture as the video delivery clock.
    duration = float(normalized_video.get("duration", seconds)) if normalized_video else seconds
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Could not measure the picture duration.")
    waveform = []
    if original_audio:
        pcm = _ffmpeg(
            *_options(kind),
            "-i",
            source,
            "-map",
            f"0:{original_audio['index']}",
            "-t",
            f"{duration:.6f}",
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "pipe:1",
        )
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        if samples.size:
            waveform = [round(int(np.abs(chunk).max()) / 32768, 3) for chunk in np.array_split(samples, min(480, len(samples)))]
    return {
        "preview": filename,
        "preview_sha256": sha256(working),
        "duration_ms": round(duration * 1000),
        "video": bool(original_video),
        "audio": bool(original_audio),
        "picture_copied": video_copy,
        "waveform": waveform,
    }


def _directory(scenes, scene_id):
    scene = scenes.get_scene(scene_id)
    if not scene.get("timeline"):
        raise ValueError("Open a timed scene imported from subtitles or a Voice Slate transcript.")
    return scenes.root / "out" / "dubs" / scene_id, scene


def _read(folder):
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8")) if folder.is_dir() else None


def attach(scenes, scene_id, reader, length, name, *, background=False, expected_source_sha=None):
    folder, scene = _directory(scenes, scene_id)
    suffix = Path(name).suffix.lower()
    if suffix not in FORMATS or len(name) > 255 or not 0 < length <= MAX_MEDIA_BYTES:
        raise ValueError("Choose a supported local media file up to 512 MiB.")
    final = folder / ("background" if background else "source")
    attached = _read(final)
    staging = folder / "dubbing-staging"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging, prefix="upload-") as temporary:
        stage = Path(temporary) / "asset"
        stage.mkdir()
        source = stage / ("upload" + suffix)
        digest = hashlib.sha256()
        with source.open("wb") as output:
            remaining = length
            while remaining:
                block = reader.read(min(RANGE_CHUNK, remaining))
                if not block:
                    raise ValueError("Upload ended before all the media bytes arrived.")
                output.write(block)
                digest.update(block)
                remaining -= len(block)
        checksum = digest.hexdigest()
        if expected_source_sha is not None and expected_source_sha != checksum:
            raise ValueError("The watched media changed during copying. Resubmit the manifest once the file has settled.")
        identity = scene.get("source_media") if scene["timeline"]["format"] == "transcript" else None
        if not background and identity and checksum != identity["sha256"]:
            raise ValueError("Choose the same media file you transcribed in Voice Slate.")
        if attached:
            if checksum == attached["sha256"]:
                return attached
            raise ValueError("This scene already has a different file of this kind. Create a new scene to change it.")
        prepared = _prepare(source, stage, FORMATS[suffix])
        if background and (prepared["video"] or not prepared["audio"]):
            raise ValueError("Choose an aligned, audio-only background stem.")
        result = {
            **prepared,
            "name": name,
            "size_bytes": length,
            "sha256": checksum,
            "original_file": source.name,
            "transcript_source_verified": bool(identity and not background),
        }
        write_json_atomic(stage / "manifest.json", result)
        try:
            stage.rename(final)
        except OSError:
            winner = _read(final)
            if not winner:
                raise
            if winner["sha256"] != checksum:
                raise ValueError("A different file was attached while this one was loading.")
            return winner
    return result


def _caption_rows(scene, placements=None):
    clock = {row["line_id"]: row for row in placements or []}
    rows = []
    for line in scene["lines"]:
        cue = line["cue"]
        placement = clock.get(line["line_id"])
        if placements is not None and not placement:
            continue
        start = placement["start_sample"] / SAMPLE_RATE if placement else cue["start_ms"] / 1000
        end = placement["end_sample"] / SAMPLE_RATE if placement else cue["end_ms"] / 1000
        rows.append(
            {
                "line_id": line["line_id"],
                "text": line["text"],
                "start_seconds": start,
                "end_seconds": end,
                "cue": True,
                "show_speaker": False,
            }
        )
    return sorted(rows, key=lambda row: row["start_seconds"])


def captions(scene, placements=None):
    from dub_translation import source_text

    rows = _caption_rows(scene, placements)
    lines = {line["line_id"]: line for line in scene["lines"]}
    target = subtitle_text(rows)
    bilingual = subtitle_text(
        [
            {**row, "text": "\n".join(dict.fromkeys(filter(None, (row["text"], source_text(scene, lines[row["line_id"]])))))}
            for row in rows
        ]
    )
    data = {
        "target.txt": "\n".join(row["text"] for row in rows) + "\n",
        "target.srt": target[0],
        "target.vtt": target[1],
        "bilingual.srt": bilingual[0],
        "bilingual.vtt": bilingual[1],
    }
    from dub_caption_styles import write as write_ass

    data.update({name + ".ass": write_ass(scene, rows, name)[0] for name in ("target", "bilingual")})
    original = source_caption_text(scene)
    if original:
        data.update({"source.srt": original[0], "source.vtt": original[1]})
    return data


def _delivery_manifests(deliveries):
    for path in deliveries.glob("[0-9a-f]*"):
        try:
            manifest = _read(path)
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get("id") == path.name:
            yield manifest


def status(scenes, scene_id):

    folder, scene = _directory(scenes, scene_id)
    outputs = sorted(
        _delivery_manifests(folder / "deliveries"), key=lambda row: (row["scene_revision"], row["finished_ns"]), reverse=True
    )
    output = outputs[0] if outputs else None
    return {
        "source": _read(folder / "source"),
        "background": _read(folder / "background"),
        "delivery": output,
        "deliveries": [
            {
                "id": row["id"],
                "scene_revision": row["scene_revision"],
                "target_language": row["target_language"],
                "finished_ns": row["finished_ns"],
            }
            for row in outputs[:50]
        ],
        "scene_revision": scene["revision"],
    }


def path_for(scenes, scene_id, name, delivery_id=None):
    folder, _ = _directory(scenes, scene_id)
    if delivery_id:
        target = folder / "deliveries" / delivery_id
        manifest = _read(target)
        allowed = ["manifest.json", *manifest["files"]] if manifest else []
    else:
        target = folder / "source"
        manifest = _read(target)
        allowed = [manifest["preview"]] if manifest else []
    if name not in allowed or not (target / name).is_file():
        raise KeyError("Dubbing file not found.")
    return target / name


def _attachment_checks(folder, asset):
    for name, expected in ((asset["preview"], asset["preview_sha256"]), (asset["original_file"], asset["sha256"])):
        path = folder / name
        if not path.is_file() or sha256(path) != expected:
            raise ValueError("Attached media failed its saved SHA-256. Restore the original attachment files.")


def _mix_output(stage, seconds, source_bed, gain, duck, dialogue_hash):
    dialogue = stage / "dialogue.wav"
    if sha256(dialogue) != dialogue_hash:
        raise ValueError("The selected-take mix failed its checksum.")
    base = ["-i", dialogue]
    if source_bed:
        _ffmpeg(
            "-i",
            source_bed,
            "-af",
            f"volume={gain:g}dB,apad",
            "-t",
            f"{seconds:.6f}",
            "-c:a",
            "pcm_s16le",
            stage / "background.wav",
        )
        base += ["-i", stage / "background.wav"]
        background = "[1:a]"
        speech = "[0:a]"
        filters = ""
        if duck:
            filters = (
                "[0:a]asplit[dialogue][side];[1:a][side]sidechaincompress=threshold=0.018:ratio=5:attack=15:release=250[bed];"
            )
            background, speech = "[bed]", "[dialogue]"
        base += [
            "-filter_complex",
            filters + f"{background}{speech}amix=inputs=2:duration=longest:normalize=0,"
            "alimiter=limit=0.891:latency=1,apad[mix]",
            "-map",
            "[mix]",
        ]
    else:
        base += ["-af", "alimiter=limit=0.891:latency=1,apad"]
    _ffmpeg(*base, "-t", f"{seconds:.6f}", "-ar", "48000", "-c:a", "pcm_s16le", stage / "dub.wav")


def _container_language(tag):
    return LANG_TAGS.get(tag.split("-")[0], tag if len(tag) == 3 else "und")


@dataclass(frozen=True)
class DubOptions:
    language: str
    keep_original: bool
    bed_path: Path | None
    bed_sha: str | None
    gain: float
    duck: bool
    burn: str | None
    alternates: list


def _mux(stage, source_path, source, options, seconds):
    command = ["-i", source_path, "-i", stage / "dub.wav", "-i", stage / "target.srt"]
    mapping = ["-map", "0:v:0", "-map", "1:a:0"]
    language = _container_language(options.language)
    flags = [
        "-metadata:s:a:0",
        f"title=Dubbed ({options.language})",
        "-metadata:s:a:0",
        f"language={language}",
        "-disposition:a:0",
        "default",
    ]
    tracks, subtitles = 1, 1
    has_original = bool(options.keep_original and source["audio"])
    if has_original:
        mapping += ["-map", "0:a:0"]
        flags += ["-metadata:s:a:1", "title=Original", "-disposition:a:1", "0"]
        tracks += 1
    caption_maps = ["-map", "2:s:0"]
    flags += ["-metadata:s:s:0", f"language={language}", "-disposition:s:0", "0"]
    for index, (folder, receipt) in enumerate(options.alternates):
        input_idx = 3 + 2 * index
        command += ["-i", folder / "dub.wav", "-i", folder / "target.srt"]
        mapping += ["-map", f"{input_idx}:a:0"]
        caption_maps += ["-map", f"{input_idx + 1}:s:0"]
        tag = receipt["target_language"]
        alt = _container_language(tag)
        flags += [
            f"-metadata:s:a:{tracks}",
            f"title=Dubbed ({tag})",
            f"-metadata:s:a:{tracks}",
            f"language={alt}",
            f"-disposition:a:{tracks}",
            "0",
            f"-metadata:s:s:{subtitles}",
            f"language={alt}",
            f"-disposition:s:{subtitles}",
            "0",
        ]
        tracks += 1
        subtitles += 1
    if options.burn:
        flags += ["-vf", f"ass={options.burn}.ass", "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p"]
    else:
        flags += ["-c:v", "copy"]
    _ffmpeg(
        *command,
        *mapping,
        *caption_maps,
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:s",
        "mov_text",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        *flags,
        "-movflags",
        "+faststart",
        "-t",
        f"{seconds:.6f}",
        stage / "dub.mp4",
        cwd=stage,
    )
    actual, streams = _probe(stage / "dub.mp4", "mov")
    expected = ["video"] + ["audio"] * tracks + ["subtitle"] * subtitles
    if [row["codec_type"] for row in streams] != expected or abs(actual - seconds) > 0.15:
        raise ValueError("Exported video did not pass its stream and duration check.")
    if [bool(row["disposition"]["default"]) for row in streams[1 : 1 + tracks]] != [True] + [False] * (tracks - 1):
        raise ValueError("Exported video does not default to the primary dubbed audio track.")
    return [
        {
            "type": row["codec_type"],
            "codec": row["codec_name"],
            "language": row.get("tags", {}).get("language"),
            "default": bool(row["disposition"]["default"]),
        }
        for row in streams
    ]


def _format_options(scenes, scene, folder, source, request):
    from dub_translation import locale

    lang = locale(request.get("target_language"))
    keep_original = request.get("keep_original", False)
    manual = request.get("background", False)
    gain = request.get("background_gain_db", 0)
    duck = request.get("duck", False)
    burn = request.get("burn")
    if any(type(flag) is not bool for flag in (keep_original, manual, duck)) or (
        type(gain) not in (int, float) or not -30 <= gain <= 6 or burn not in (None, "target", "bilingual", "karaoke")
    ):
        raise ValueError("Invalid original track, background, gain or subtitle option.")
    alt_ids = request.get("additional_deliveries", [])
    if (
        not isinstance(alt_ids, list)
        or len(alt_ids) > 8
        or any(
            not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident)
            for ident in alt_ids
        )
        or len(set(alt_ids)) != len(alt_ids)
    ):
        raise ValueError("Choose up to eight distinct alternate deliveries.")
    if (burn or keep_original or alt_ids) and not (source and source["video"]):
        raise ValueError("Original or alternate tracks and burned subtitles require attached video.")
    if keep_original and not source["audio"]:
        raise ValueError("The attached video has no original audio.")
    bed_path = bed_sha = None
    if manual:
        bed = _read(folder / "background")
        if not bed:
            raise ValueError("Attach an aligned audio-only background stem before selecting it.")
        _attachment_checks(folder / "background", bed)
        bed_path = folder / "background" / bed["preview"]
        bed_sha = bed["sha256"]
    alternates, used = [], {lang}
    for ident in alt_ids:
        verify(scenes, scene["scene_id"], ident)
        path = folder / "deliveries" / ident
        receipt = _read(path)
        tag = receipt["target_language"]
        if (
            tag in used
            or receipt["source_sha256"] != source["sha256"]
            or abs(receipt["duration_seconds"] - source["duration_ms"] / 1000) > 0.01
            or not {"dub.wav", "target.srt"} <= receipt["files"].keys()
        ):
            raise ValueError("Alternate deliveries must have distinct languages, matching source and captions.")
        used.add(tag)
        alternates.append((path, receipt))
    return DubOptions(lang, keep_original, bed_path, bed_sha, gain, bool(bed_path and duck), burn, alternates)


def export(scenes, scene_id, request):
    """Publish each successful delivery atomically. Existing deliveries survive a failed attempt."""
    folder, scene = _directory(scenes, scene_id)
    if request.get("expected_revision") != scene["revision"]:
        raise ValueError("Reload the saved scene revision before exporting.")
    source = _read(folder / "source")
    source_identity = scene.get("source_media") if scene["timeline"]["format"] == "transcript" else None
    if source and source_identity and source["sha256"] != source_identity.get("sha256"):
        raise ValueError("The attached file does not match the saved Voice Slate source identity.")
    if source:
        _attachment_checks(folder / "source", source)
    args = _format_options(scenes, scene, folder, source, request)
    take_ids = [
        take_id for line in scene["lines"] if not line.get("dub", {}).get("mute") for take_id in line["selected_takes"].values()
    ]
    licences = {take["id"]: take["licence"] for take in scenes.cast.get_takes(take_ids)}
    fingerprint = digest_json(
        [
            3,
            scene["checksum"],
            source and source["sha256"],
            args.bed_sha,
            args.language,
            args.keep_original,
            args.gain if args.bed_path else None,
            args.duck,
            args.burn,
            [receipt["checksum"] for _, receipt in args.alternates],
            licences,
        ]
    )
    deliveries = folder / "deliveries"
    deliveries.mkdir(parents=True, exist_ok=True)
    (folder / "dubbing-staging").mkdir(parents=True, exist_ok=True)
    with _path_lock(deliveries):
        for existing in _delivery_manifests(deliveries):
            if existing.get("fingerprint") == fingerprint:
                try:
                    verify(scenes, scene_id, existing["id"])
                except (OSError, ValueError, KeyError):
                    continue
                return existing
        return _publish(scenes, scene, folder, source, args, fingerprint, licences)


def _dialogue(scenes, scene, source, options, stage):
    with tempfile.TemporaryDirectory(dir=stage.parent, prefix="preview-") as preview_dir:
        preview = scenes.assemble_preview(scene["scene_id"], dub=True, output_dir=Path(preview_dir))
        if preview["scene_checksum"] != scene["checksum"]:
            raise ValueError("The scene changed during export. Reload and try again.")
        shutil.move(preview["audio_path"], stage / "dialogue.wav")
    if source and source["video"]:
        limit = source["duration_ms"] * SAMPLE_RATE // 1000
        if any(row["end_sample"] > limit for row in preview["timing"]["placements"]):
            raise ValueError("Spoken audio extends beyond the picture. Fit the cue, shorten its text or move it earlier.")
        return source["duration_ms"] / 1000, preview
    return max(preview["duration_seconds"], (source or {}).get("duration_ms", 0) / 1000), preview


def _publish(scenes, scene, folder, source, options, fingerprint, licences):
    from dub_caption_styles import generated_words, write as write_ass

    scene_id = scene["scene_id"]
    export_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(dir=folder / "dubbing-staging", prefix="export-") as temporary:
        stage = Path(temporary) / "delivery"
        stage.mkdir()
        seconds, preview = _dialogue(scenes, scene, source, options, stage)
        if {line["take_id"]: line["licence"] for line in preview["lines"]} != licences:
            raise ValueError("The take licence ratings changed during export. Try the export again.")
        _mix_output(stage, seconds, options.bed_path, options.gain, options.duck, preview["audio_sha256"])
        duration, wav_streams = _probe(stage / "dub.wav", "wav")
        if abs(duration - seconds) > 0.01 or len(wav_streams) != 1 or wav_streams[0]["codec_name"] != "pcm_s16le":
            raise ValueError("Exported WAV did not pass the codec and duration check.")
        placements = preview["timing"]["placements"]
        sidecars = captions(scene, placements)
        karaoke, estimated = write_ass(
            scene, _caption_rows(scene, placements), "karaoke", generated_words(scenes.cast, preview)
        )
        sidecars["karaoke.ass"] = karaoke
        for name, text in sidecars.items():
            if text.strip():
                (stage / name).write_text(text, encoding="utf-8", newline="\n")
        streams = []
        if source and source["video"]:
            if not placements:
                raise ValueError("Unmute at least one cue before exporting dubbed video.")
            streams = _mux(stage, folder / "source" / source["preview"], source, options, seconds)
        manifest = {
            "schema_version": 1,
            "id": export_id,
            "scene_id": scene_id,
            "scene_revision": scene["revision"],
            "scene_checksum": scene["checksum"],
            "source_sha256": source and source["sha256"],
            "source_offset_ms": scene["timeline"].get("source_offset_ms", 0),
            "background_sha256": options.bed_sha,
            "background_gain_db": options.gain if options.bed_path else None,
            "duck": options.duck,
            "target_language": options.language,
            "duration_seconds": seconds,
            "picture_copied": bool(source and source["video"] and source["picture_copied"] and not options.burn),
            "burn": options.burn,
            "streams": streams,
            "takes": preview["lines"],
            "timing": preview["timing"],
            "additional_deliveries": [receipt["id"] for _, receipt in options.alternates],
            "karaoke_estimated_cues": estimated,
            "files": {path.name: sha256(path) for path in stage.iterdir()},
            "finished_ns": time.time_ns(),
            "fingerprint": fingerprint,
        }
        manifest["checksum"] = digest_json(manifest)
        write_json_atomic(stage / "manifest.json", manifest)
        stage.rename(folder / "deliveries" / export_id)
    return manifest


def verify(scenes, scene_id, delivery_id):
    folder, _ = _directory(scenes, scene_id)
    folder = folder / "deliveries" / delivery_id
    manifest = _read(folder)
    if not manifest:
        raise KeyError("Dubbing delivery not found.")
    checksum = manifest.pop("checksum", None)
    if checksum != digest_json(manifest):
        raise ValueError("The delivery manifest failed its checksum.")
    for name, expected in manifest["files"].items():
        if Path(name).name != name or not (folder / name).is_file() or sha256(folder / name) != expected:
            raise ValueError(f"Delivery file {name} failed its SHA-256.")
    return {"ok": True, "id": delivery_id, "files": len(manifest["files"])}
