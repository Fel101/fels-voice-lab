"""Chapter auditions and verified audiobook packages, built from the selected scene takes."""

import csv
import itertools
import json
from math import ceil, log10
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import numpy as np
import soundfile as sf

from voice_lab import ffmpeg_tools
from voice_lab import synthetic_label

from voice_lab.book_language import original_line
from voice_lab.book_store import ACTIVE
from voice_lab.scene_selection import validate_selected_take
from voice_lab.scene_store import _path_lock, digest_json, new_id
from tools.scene_export import sha256_file, subtitle_text

RATE = 24_000
FORMATS = {"wav", "flac", "mp3", "m4b"}
MEDIA = {
    "wav": "audio/wav",
    "flac": "audio/flac",
    "m4b": "audio/mp4",
    "mp3": "audio/mpeg",
    "jpg": "image/jpeg",
    "json": "application/json",
    "cue": "text/plain; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "srt": "application/x-subrip",
    "vtt": "text/vtt; charset=utf-8",
}
LIMIT_SECONDS = 12 * 3600
CHAPTER_LIMIT_SECONDS = 90 * 60
COVER_LIMIT = 5 * 1024 * 1024
ID = re.compile(r"[a-f0-9]{32}")
SAFE_FILE = re.compile(r"[a-z0-9][a-z0-9.-]*\.(?:wav|flac|m4b|mp3|jpg|json|cue|csv|srt|vtt)")


def _run(tool, *args, timeout=3600):
    path = ffmpeg_tools.resolve(None, tool)
    if not path:
        raise ffmpeg_tools.FFmpegMissing("Open System & backups and choose Install FFmpeg to export audiobooks.")
    command = [path, *map(str, args)]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{tool} could not finish the audiobook operation.") from exc
    if result.returncode:
        last = result.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(f"{tool} could not produce the audiobook file: {last[-1][:250] if last else 'no details'}.")
    return result


def _ffmpeg(*args, timeout=3600):
    return _run("ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", *args, timeout=timeout)


def _probe(path, demux=None):
    result = _run(
        "ffprobe",
        "-v",
        "error",
        *(["-f", demux] if demux else []),
        "-protocol_whitelist",
        "file",
        "-show_streams",
        "-show_chapters",
        "-show_format",
        "-of",
        "json",
        path,
        timeout=45,
    )
    return json.loads(result.stdout)


def _db(value):
    return round(20 * log10(value), 2) if value > 0 else None


def _measure(path, peak, energy, frames):
    # FFmpeg accumulates the full book's gated loudness without holding it in memory.
    result = _run(
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-i",
        path,
        "-af",
        "ebur128=framelog=verbose:peak=true",
        "-f",
        "null",
        "-",
    )
    text = result.stderr.decode(errors="replace").rsplit("Summary:", 1)[-1]
    loud = re.search(r"I:\s+(-?[\d.]+) LUFS", text)
    true = re.search(r"Peak:\s+(-?[\d.]+) dBFS", text)
    return {
        "meter": "EBU R128 / PCM sample statistics",
        "integrated_lufs": float(loud[1]) if loud and float(loud[1]) > -70 else None,
        "sample_peak_dbfs": _db(peak),
        "true_peak_dbtp": float(true[1]) if true else None,
        "rms_dbfs": _db((energy / frames) ** 0.5) if frames else None,
    }


def attach_cover(books, book_id, revision, data):
    if not data or len(data) > COVER_LIMIT:
        raise ValueError("Choose a JPEG or PNG cover up to 5 MiB.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png_pipe"
    elif data.startswith(b"\xff\xd8\xff"):
        kind = "jpeg_pipe"
    else:
        raise ValueError("Choose a JPEG or PNG cover.")
    book = books.get(book_id)
    if revision != book["revision"]:
        raise ValueError("Reopen the saved book before changing its cover.")
    folder = books.root / book_id / "covers"
    folder.mkdir(parents=True, exist_ok=True)
    staging = folder.parent / "book-staging"
    staging.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging, prefix="cover-") as work:
        raw, jpg = Path(work) / "cover.input", Path(work) / "cover.jpg"
        raw.write_bytes(data)
        streams = _probe(raw, kind)["streams"]
        if len(streams) != 1 or not all(0 < streams[0].get(key, 0) <= 5000 for key in ("width", "height")):
            raise ValueError("Use a cover up to 5,000 pixels on each side.")
        _ffmpeg(
            "-protocol_whitelist",
            "file",
            "-f",
            kind,
            "-i",
            raw,
            "-frames:v",
            1,
            "-vf",
            "scale=1200:1200:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
            "-q:v",
            3,
            "-update",
            1,
            jpg,
            timeout=45,
        )
        key = sha256_file(jpg)
        dest = folder / (key + ".jpg")
        if not dest.exists():
            jpg.replace(dest)
    return books.set_cover(book_id, revision, {"sha256": key, "original_sha256": sha256_bytes(data)})


def sha256_bytes(data):
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _chosen(books, scene):
    cast = books.scenes.cast
    if "timeline" in scene:
        raise ValueError("Chapter scenes use sequential narration. Open a timed scene in dubbing.")
    recent = books.scenes.list_jobs(scene["scene_id"])
    if recent and (recent[0]["status"] in ACTIVE or recent[0]["status"] in {"failed", "paused"}):
        raise ValueError(f"{scene['name']} has a running, paused or failed chapter job. Finish it before assembling.")
    ids = [take_id for line in scene["lines"] for take_id in line["selected_takes"].values()]
    takes = {take["id"]: take for take in cast.get_takes(ids)}
    if len(takes) != len(set(ids)):
        raise ValueError(f"{scene['name']} has a missing take. Choose a current take first.")
    order, total = [], 0
    pronunciations, verified = {}, {}
    for line in scene["lines"]:
        if line["use_pronunciations"] and line["character_id"] not in pronunciations:
            pronunciations[line["character_id"]] = cast.list_pronunciations(line["character_id"])
        for segment in line["segments"]:
            take = takes.get(line["selected_takes"].get(segment["id"]))
            if not take or take["retired"]:
                raise ValueError(f"Render and select takes for every passage in {scene['name']}.")
            spoken = (
                cast.apply_pronunciations(segment["text"], line["character_id"], pronunciations[line["character_id"]])
                if line["use_pronunciations"]
                else segment["text"]
            )
            validate_selected_take(scene["scene_id"], line, segment, take, spoken)
            if take["id"] not in verified:
                item = cast.get_media_path("take", take["id"])
                if sha256_file(item) != take["audio_sha256"]:
                    raise ValueError(f"A selected take in {scene['name']} failed its saved checksum.")
                audio = sf.info(item)
                verified[take["id"]] = {
                    "id": take["id"],
                    "audio_sha256": take["audio_sha256"],
                    "samples": audio.frames,
                    "rate": audio.samplerate,
                    "model": take.get("model") or {},
                    "licence": take["licence"],
                    "reference_id": take["reference_version_id"],
                }
            stamp = verified[take["id"]]
            total += stamp["samples"] / stamp["rate"]
            order.append({**stamp, "line_id": line["line_id"], "segment_id": segment["id"]})
        total += (line["pause_before_ms"] + line["pause_after_ms"]) / 1000
    if total > CHAPTER_LIMIT_SECONDS:
        raise ValueError(f"{scene['name']} exceeds 90 minutes. Split it into shorter chapters.")
    return order


def _captions(scene, order, raw_rate, final_frames):
    cursor, captions = 0, []
    for line, values in zip(scene["lines"], itertools.groupby(order, key=lambda row: row["line_id"]), strict=True):
        key, parts = values
        if key != line["line_id"]:
            raise ValueError("Selected audio does not match the chapter passage order.")
        cursor += round(raw_rate * line["pause_before_ms"] / 1000)
        start = cursor
        for part in parts:
            cursor += ceil(part["samples"] * raw_rate / part["rate"])
        end = min(final_frames / RATE, cursor / raw_rate)
        text = original_line(line)
        if end > start / raw_rate:
            captions.append(
                {
                    "line_id": line["line_id"],
                    "text": text,
                    "start_seconds": start / raw_rate,
                    "end_seconds": end,
                    "cue": True,
                    "show_speaker": False,
                }
            )
        cursor += round(raw_rate * line["pause_after_ms"] / 1000)
    return captions


def _read_manifest(path):
    receipt = json.loads(path.read_text(encoding="utf-8"))
    expected = receipt.pop("checksum", None)
    if expected != digest_json(receipt):
        raise ValueError("The audiobook manifest failed its checksum.")
    return {**receipt, "checksum": expected}


def _write_manifest(books, path, body):
    receipt = {**body, "checksum": digest_json(body)}
    books.scenes._atomic_json(path, receipt)
    return receipt


def audition(books, book_id, scene_id):
    scene = books.chapter(book_id, scene_id)
    order = _chosen(books, scene)

    # A book-wide pronunciation edit can save the current rules on every chapter. Reuse audio
    # where the normalized script, pauses, voices, settings and selected audio are still identical.
    production = {key: value for key, value in scene.items() if key not in {"checksum", "revision", "book_pronunciations"}}
    fingerprint = digest_json([2, production, order])
    ident = fingerprint[:32]
    cache = books.root / book_id / "auditions"
    cache.mkdir(parents=True, exist_ok=True)
    final = cache / ident
    with _path_lock(cache):
        if final.is_dir():
            receipt = _read_manifest(final / "manifest.json")
            if receipt["fingerprint"] != fingerprint or sha256_file(final / "chapter.wav") != receipt["sha256"]:
                raise ValueError("This cached chapter audition failed its checksum.")
            return {**receipt, "scene_revision": scene["revision"]}, final / "chapter.wav"
        staging = cache.parent / "book-staging"
        staging.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging, prefix="audition-") as path:
            folder = Path(path) / "ready"
            folder.mkdir()
            preview = books.scenes.assemble_preview(scene_id, output_dir=Path(path) / "preview")
            if preview["scene_checksum"] != scene["checksum"]:
                raise ValueError("The chapter changed during assembly. Reopen it and try again.")
            out = folder / "chapter.wav"
            if preview["sample_rate"] == RATE:
                shutil.copyfile(preview["audio_path"], out)
            else:
                _ffmpeg(
                    "-i", preview["audio_path"], "-ar", RATE, "-ac", 1, "-c:a", "pcm_s16le", *synthetic_label.ffmpeg_args(), out
                )
            frames = sf.info(out).frames
            captions = _captions(scene, order, preview["sample_rate"], frames)
            receipt = _write_manifest(
                books,
                folder / "manifest.json",
                {
                    "id": ident,
                    "chapter_id": scene_id,
                    "chapter_title": scene["name"],
                    "scene_revision": scene["revision"],
                    "fingerprint": fingerprint,
                    "frames": frames,
                    "sample_rate": RATE,
                    "sha256": sha256_file(out),
                    "takes": order,
                    "captions": captions,
                },
            )
            folder.rename(final)
    return receipt, final / "chapter.wav"


def _join(path, selected, gap):
    offsets, captions, cursor, peak, energy = [], [], 0, 0.0, 0.0
    with sf.SoundFile(path, "w", RATE, channels=1, subtype="PCM_16") as audio:
        audio.comment = synthetic_label.comment()
        audio.software = synthetic_label.SOFTWARE
        for index, (receipt, file) in enumerate(selected, 1):
            start = cursor
            with sf.SoundFile(file) as chapter:
                for samples in chapter.blocks(blocksize=RATE * 10, dtype="int16"):
                    audio.write(samples)
                    units = samples.astype(np.float64) / 32768
                    peak = max(peak, float(np.max(np.abs(units))))
                    energy += float(np.dot(units, units))
                    cursor += len(samples)
            if cursor - start != receipt["frames"]:
                raise ValueError("Cached chapter audio length changed before export.")
            speech_end = cursor
            if index < len(selected) and gap:
                audio.write(np.zeros(gap, dtype=np.int16))
                cursor += gap
            offsets.append(
                {
                    "index": index,
                    "title": receipt["chapter_title"],
                    "chapter_id": receipt["chapter_id"],
                    "cache_id": receipt["id"],
                    "chapter_sha256": receipt["sha256"],
                    "start_frame": start,
                    "speech_end_frame": speech_end,
                    "end_frame": cursor,
                    "takes": receipt["takes"],
                }
            )
            for row in receipt["captions"]:
                captions.append(
                    {
                        **row,
                        "line_id": f"ch-{index}-{row['line_id']}",
                        "start_seconds": row["start_seconds"] + start / RATE,
                        "end_seconds": row["end_seconds"] + start / RATE,
                    }
                )
    return offsets, captions, _measure(path, peak, energy, cursor)


def _metadata_text(value):
    return re.sub(r"([\\=;#\n])", r"\\\1", value.replace("\r", ""))


def _ffmetadata(path, book, chapters):
    info = book["metadata"]
    tags = {
        "title": info["title"],
        "album": info["title"],
        "artist": info["author"],
        "composer": info["narrator"],
        "genre": info["genre"] or "Audiobook",
        "date": info["year"],
        "description": info["description"],
        "media_type": "2",
    }
    lines = [";FFMETADATA1", *(f"{name}={_metadata_text(value)}" for name, value in tags.items() if value)]
    for row in chapters:
        lines.extend(
            [
                "[CHAPTER]",
                f"TIMEBASE=1/{RATE}",
                f"START={row['start_frame']}",
                f"END={row['end_frame']}",
                f"title={_metadata_text(row['title'])}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _verify_m4b(path, metadata, chapters, with_cover):
    result = _probe(path)
    streams = result["streams"]
    tracks = [row for row in streams if row["codec_type"] == "audio"]
    art = [row for row in streams if row.get("disposition", {}).get("attached_pic")]
    tags = result["format"].get("tags", {})
    if (
        len(tracks) != 1
        or tracks[0]["codec_name"] != "aac"
        or int(tracks[0]["sample_rate"]) != RATE
        or tracks[0]["channels"] != 1
        or len(art) != int(with_cover)
        or tags.get("title") != metadata["title"]
        or (metadata["author"] and tags.get("artist") != metadata["author"])
    ):
        raise ValueError("The M4B did not preserve its title, cover or expected audio stream.")
    saved = result["chapters"]
    if len(saved) != len(chapters):
        raise ValueError("The M4B chapter count did not match the book.")
    for actual, expected in zip(saved, chapters, strict=True):
        if (
            actual.get("tags", {}).get("title") != expected["title"]
            or abs(float(actual["start_time"]) - expected["start_frame"] / RATE) > 0.0015
            or abs(float(actual["end_time"]) - expected["end_frame"] / RATE) > 0.0015
        ):
            raise ValueError(f"Embedded chapter timing did not match {expected['title']}.")
    if abs(float(result["format"]["duration"]) - chapters[-1]["end_frame"] / RATE) > 0.07:
        raise ValueError("The M4B audio duration did not match the chapter clock.")
    return {
        "codec": "aac",
        "attached_cover": bool(art),
        "title": tags["title"],
        "duration_seconds": float(result["format"]["duration"]),
    }


def _publish_formats(folder, pcm, book, offsets, selected, formats, cover):
    meta = pcm.parent / "book.ffmetadata"
    _ffmetadata(meta, book, offsets)
    if "wav" in formats:
        shutil.copyfile(pcm, folder / "book.wav")
    for index, (_, audio) in enumerate(selected, 1):
        if "wav" in formats:
            shutil.copyfile(audio, folder / f"chapter-{index:03d}.wav")
        if "flac" in formats:
            _ffmpeg("-i", audio, "-c:a", "flac", *synthetic_label.ffmpeg_args(), folder / f"chapter-{index:03d}.flac")
    if "mp3" in formats:
        _ffmpeg(
            "-i",
            pcm,
            "-f",
            "ffmetadata",
            "-i",
            meta,
            "-map",
            "0:a",
            "-map_metadata",
            1,
            "-c:a",
            "libmp3lame",
            "-q:a",
            3,
            folder / "book.mp3",
        )
    if "m4b" in formats:
        args = ["-i", pcm, "-f", "ffmetadata", "-i", meta]
        if cover:
            args += ["-i", cover]
        args += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac", "-b:a", "80k"]
        if cover:
            args += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        _ffmpeg(*args, "-movflags", "+faststart", "-f", "ipod", *synthetic_label.ffmpeg_args(), folder / "book.m4b")
        return _verify_m4b(folder / "book.m4b", book["metadata"], offsets, bool(cover))
    return None


def _cues(folder, book, chapters, formats):
    with (folder / "chapters.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["chapter", "title", "start_frame", "speech_end_frame", "end_frame", "sample_rate"])
        writer.writerows(
            [row["index"], row["title"], row["start_frame"], row["speech_end_frame"], row["end_frame"], RATE]
            for row in chapters
        )
    if "mp3" in formats or "wav" in formats:
        file, kind = ("book.mp3", "MP3") if "mp3" in formats else ("book.wav", "WAVE")
        safe = lambda text: " ".join(text.replace('"', "'").split())  # noqa: E731
        lines = [f'TITLE "{safe(book["metadata"]["title"])}"', f'FILE "{file}" {kind}']
        for row in chapters[:99]:
            count = round(row["start_frame"] * 75 / RATE)
            minute, rest = divmod(count, 4500)
            second, frame = divmod(rest, 75)
            lines += [
                f"  TRACK {row['index']:02d} AUDIO",
                f'    TITLE "{safe(row["title"])}"',
                f"    INDEX 01 {minute:02d}:{second:02d}:{frame:02d}",
            ]
        # Classic CUE sheets can address at most 99 tracks; the frame-exact CSV covers every chapter.
        if len(chapters) <= 99:
            (folder / "book.cue").write_text("\n".join(lines) + "\n", encoding="utf-8")


def export(books, book_id, request):
    book = books.get(book_id)
    if book["revision"] != request.get("expected_revision"):
        raise ValueError("Reopen the saved book before exporting.")
    formats = request.get("formats", ["wav", "flac", "mp3", "m4b"])
    if (
        not isinstance(formats, list)
        or not formats
        or any(not isinstance(item, str) or item not in FORMATS for item in formats)
    ):
        raise ValueError("Choose WAV, FLAC, MP3 or M4B.")
    formats = sorted(set(formats))
    cover = None
    parent = books.root / book_id
    if book["cover"]:
        pin = book["cover"]["sha256"]
        cover = parent / "covers" / (pin + ".jpg")
        if not cover.is_file() or sha256_file(cover) != pin:
            raise ValueError("The saved cover image failed its checksum.")
    deliveries = parent / "deliveries"
    deliveries.mkdir(parents=True, exist_ok=True)
    with _path_lock(deliveries):
        selected = [audition(books, book_id, scene_id) for scene_id in book["chapters"]]
        gap = book["gap_ms"] * RATE // 1000
        if sum(item["frames"] for item, _ in selected) + (len(selected) - 1) * gap > LIMIT_SECONDS * RATE:
            raise ValueError("This export exceeds 12 hours. Produce the manuscript in volumes.")
        if books.get(book_id)["revision"] != book["revision"] or any(
            books.chapter(book_id, item["chapter_id"])["revision"] != item["scene_revision"] for item, _ in selected
        ):
            raise ValueError("The book changed during assembly. Reopen it and try again.")
        fingerprint = digest_json([1, book, [item["id"] for item, _ in selected], formats])
        for path in deliveries.glob("*/manifest.json"):
            try:
                receipt = _read_manifest(path)
                if receipt["fingerprint"] == fingerprint:
                    verify(books, book_id, receipt["id"])
                    return receipt
            except (OSError, ValueError, KeyError):
                continue
        staging = parent / "book-staging"
        staging.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging, prefix="export-") as temporary:
            root = Path(temporary)
            ready = root / "delivery"
            ready.mkdir()
            pcm = root / "whole.wav"
            offsets, captions, levels = _join(pcm, selected, gap)
            if cover:
                shutil.copyfile(cover, ready / "cover.jpg")
            media = _publish_formats(ready, pcm, book, offsets, selected, formats, cover)
            srt, vtt = subtitle_text(captions)
            (ready / "book.srt").write_text(srt, encoding="utf-8")
            (ready / "book.vtt").write_text(vtt, encoding="utf-8")
            _cues(ready, book, offsets, formats)
            ident = new_id()
            receipt = _write_manifest(
                books,
                ready / "manifest.json",
                {
                    "schema_version": 1,
                    "id": ident,
                    "book_id": book_id,
                    "book_revision": book["revision"],
                    "metadata": book["metadata"],
                    "sample_rate": RATE,
                    "duration_seconds": offsets[-1]["end_frame"] / RATE,
                    "formats": formats,
                    "chapters": offsets,
                    "loudness": levels,
                    "m4b": media,
                    "caption_clock": "passage audio",
                    "fingerprint": fingerprint,
                    "finished_ns": time.time_ns(),
                    "files": {asset.name: sha256_file(asset) for asset in ready.iterdir()},
                },
            )
            ready.rename(deliveries / ident)
        return receipt


def latest(books, book_id):
    books.get(book_id)
    found = []
    for path in (books.root / book_id / "deliveries").glob("*/manifest.json"):
        try:
            found.append(_read_manifest(path))
        except (ValueError, OSError):
            continue
    return max(found, key=lambda row: row["finished_ns"], default=None)


def path_for(books, book_id, artifact, key, filename):
    books.get(book_id)
    if not ID.fullmatch(key) or not SAFE_FILE.fullmatch(filename):
        raise KeyError("Audiobook file not found.")
    folder = books.root / book_id / artifact / key
    if not folder.is_dir():
        raise KeyError("Audiobook file not found.")
    receipt = _read_manifest(folder / "manifest.json")
    allowed = {"chapter.wav"} if artifact == "auditions" else set(receipt["files"])
    if filename not in {*allowed, "manifest.json"} or not (folder / filename).is_file():
        raise KeyError("Audiobook file not found.")
    return folder / filename


def verify(books, book_id, delivery_id):
    path = path_for(books, book_id, "deliveries", delivery_id, "manifest.json")
    receipt = _read_manifest(path)
    for name, expected in receipt["files"].items():
        if not SAFE_FILE.fullmatch(name) or not (path.parent / name).is_file() or sha256_file(path.parent / name) != expected:
            raise ValueError(f"Audiobook file {name} failed its SHA-256.")
    if receipt["m4b"]:
        _verify_m4b(path.parent / "book.m4b", receipt["metadata"], receipt["chapters"], "cover.jpg" in receipt["files"])
    return {"ok": True, "files": len(receipt["files"]), "chapters": len(receipt["chapters"])}
