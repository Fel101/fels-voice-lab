"""Where FFmpeg is, what it can write, and the one place that reads audio with it.

Nothing here is bundled. FFmpeg is looked for on PATH first and then in the folder WinGet unpacks it into, so the
answer is the same whether the service started from a shell or from the desktop shortcut. What a build can encode is
read from the build itself, and the answer for the usual FFmpeg is kept for a minute so a page that asks often does not
start a process for every request.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import soundfile as sf

WINGET_FOLDER = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
                 / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe")
INSTALL_HINT = "Install FFmpeg (for example, in a terminal: winget install Gyan.FFmpeg) and try again."
CACHE_SECONDS = 60
PROBE_TIMEOUT = 20
DECODE_TIMEOUT = 120
# The encoder each package format needs, named as the keys of a status report's "encoders".
FORMAT_ENCODER = {"wav": None, "flac": "flac", "ogg": "vorbis", "mp3": "mp3", "m4a": "aac"}
FORMAT_LABEL = {"wav": "WAV", "flac": "FLAC", "ogg": "Ogg Vorbis", "mp3": "MP3", "m4a": "AAC (M4A)"}
_ENCODER_NAME = {"mp3": "libmp3lame", "aac": "aac", "vorbis": "libvorbis", "flac": "flac"}
_LISTING = re.compile(r"^\s*[AVS][.FSXBD]{5}\s+(\S+)", re.MULTILINE)
_status_lock = threading.Lock()
_status_cache = {"at": 0.0, "value": None}


class FFmpegMissing(ValueError):
    """FFmpeg, or the part of it a job needs, is not on this PC. The message says what to do about it."""


def _find(tool):
    found = shutil.which(tool)
    if found:
        return found
    if WINGET_FOLDER.is_dir():
        matches = sorted(WINGET_FOLDER.glob(f"ffmpeg-*/bin/{tool}.exe"))
        if matches:
            return str(matches[-1])
    return None


def find_ffmpeg():
    return _find("ffmpeg")


def find_ffprobe():
    return _find("ffprobe")


def resolve(choice, tool):
    """The program for `tool`: the one a caller named (a path or a PATH name), else the usual search."""
    return shutil.which(choice) if choice else _find(tool)


def _listing(ffmpeg, option):
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", option], capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(_LISTING.findall(result.stdout)) if result.returncode == 0 else set()


def capabilities(ffmpeg, ffprobe):
    """What these two programs are and which package formats this FFmpeg can encode; both may be None."""
    if not ffmpeg:
        return {"found": False, "ffmpeg": None, "ffprobe": ffprobe, "version": None, "encoders": {}, "decoders": {},
                "message": "FFmpeg was not found on this PC. " + INSTALL_HINT}
    version = None
    try:
        first = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=PROBE_TIMEOUT).stdout
        match = re.match(r"ffmpeg version (\S+)", first)
        version = match.group(1) if match else None
    except (OSError, subprocess.SubprocessError):
        pass
    encoders, decoders = _listing(ffmpeg, "-encoders"), _listing(ffmpeg, "-decoders")
    return {
        "found": True, "ffmpeg": ffmpeg, "ffprobe": ffprobe, "version": version,
        "encoders": {key: name in encoders for key, name in _ENCODER_NAME.items()},
        "decoders": {"aac": "aac" in decoders},
        "message": None if ffprobe else "FFmpeg was found but ffprobe was not, and writing a package needs both. " + INSTALL_HINT,
    }


def status(refresh=False):
    """capabilities() of the FFmpeg the lab would use, remembered for a minute unless refresh is set."""
    with _status_lock:
        now = time.monotonic()
        if refresh or _status_cache["value"] is None or now - _status_cache["at"] > CACHE_SECONDS:
            _status_cache.update(at=now, value=capabilities(find_ffmpeg(), find_ffprobe()))
        return dict(_status_cache["value"])


def forget():
    """Drop the remembered answer, for the tests."""
    with _status_lock:
        _status_cache.update(at=0.0, value=None)


def writable_formats(report):
    """Which package formats can be written now, by format name."""
    ready = bool(report["found"] and report["ffprobe"])
    return {name: ready and (encoder is None or bool(report["encoders"].get(encoder)))
            for name, encoder in FORMAT_ENCODER.items()}


def cannot_write(formats, report):
    """Why a package in these formats cannot be written with this FFmpeg, or None when it can."""
    if not report["found"] or not report["ffprobe"]:
        return report["message"]
    missing = [FORMAT_LABEL[name] for name in formats
               if FORMAT_ENCODER[name] and not report["encoders"].get(FORMAT_ENCODER[name])]
    if missing:
        return (f"This FFmpeg build cannot write {' or '.join(missing)}: its encoder is not included. "
                "Choose other formats, or install a full FFmpeg build.")
    return None


def decode_audio(source_bytes, suffix, max_seconds):
    """Read the first audio stream of an MP4-family file (M4A) into float32 samples at the file's own sample rate.

    The input format is fixed instead of guessed, and only plain files may be opened, so a file that merely has the
    right extension cannot make FFmpeg follow a playlist or a network address. At most max_seconds + 1 seconds are
    decoded; the caller rejects anything longer. Returns (samples shaped [frames, channels], sample_rate)."""
    ffmpeg = find_ffmpeg()
    kind = suffix.lstrip(".").upper()
    if not ffmpeg:
        raise FFmpegMissing(f"{kind} files are read with FFmpeg, which was not found on this PC. {INSTALL_HINT} "
                            "Or convert the file to WAV, FLAC, OGG or MP3.")
    with tempfile.TemporaryDirectory(prefix="voice-lab-decode-") as folder:
        source, target = Path(folder) / ("input" + suffix), Path(folder) / "decoded.wav"
        source.write_bytes(source_bytes)
        command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-protocol_whitelist", "file",
                   "-f", "mov", "-i", str(source), "-map", "0:a:0", "-vn", "-t", f"{max_seconds + 1:g}",
                   "-c:a", "pcm_f32le", "-f", "wav", str(target)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=DECODE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("FFmpeg took too long to read this file.") from exc
        except OSError as exc:
            raise ValueError(f"FFmpeg could not be started: {exc}") from exc
        if result.returncode or not target.is_file() or target.stat().st_size == 0:
            reason = (result.stderr.strip().splitlines() or ["it gave no reason"])[-1][:200]
            raise ValueError(f"FFmpeg could not read this {kind} file: {reason}")
        try:
            audio, rate = sf.read(target, dtype="float32", always_2d=True)
        except Exception as exc:
            raise ValueError(f"Could not read the audio FFmpeg produced: {exc}") from exc
    return audio, int(rate)
