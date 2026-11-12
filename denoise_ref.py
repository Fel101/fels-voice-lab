"""Denoise one reference clip for voice cloning, then check that the voice still matches.

The cleanup is the recipe that produced the reference clip in the Lab's Cast store,
which this tool reproduces sample for sample:

    ffmpeg -ss <start> -t <dur> -i in.wav -af "afftdn=nf=-25,aresample=24000,aformat=channel_layouts=mono" out.wav

Every run writes a new file (existing files are kept unless --overwrite is given) and a JSON
report next to it. The report compares the cleaned window with the same window before cleanup
using the proxy speaker similarity, and measures how much the quiet parts got quieter. A clip
at or under 5 seconds is refused, because Chatterbox Turbo rejects references that short.

    .venv\\Scripts\\python.exe denoise_ref.py clip.wav --start 8 --dur 25 --nf -25
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

import ffmpeg_tools

LAB = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = LAB / "out" / "denoise"
SAMPLE_RATE = 24_000
MIN_TURBO_SECONDS = 5.0
# Cleaning may not move the cleaned clip further than this from a target voice than the uncleaned clip was.
MAX_TARGET_LOSS = 0.02


class DenoiseError(RuntimeError):
    pass


def find_ffmpeg():
    found = ffmpeg_tools.find_ffmpeg()
    if found:
        return found
    raise DenoiseError("Cleaning a reference uses FFmpeg, which was not found on this PC. " + ffmpeg_tools.INSTALL_HINT)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_ffmpeg(ffmpeg, source, destination, *, start, dur, audio_filter):
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if start:
        command += ["-ss", f"{start:g}"]
    if dur:
        command += ["-t", f"{dur:g}"]
    command += ["-i", str(source), "-af", audio_filter, "-c:a", "pcm_s16le", str(destination)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode or not Path(destination).is_file():
        raise DenoiseError(f"ffmpeg failed: {result.stderr.strip() or result.returncode}")


def _quiet_floor_dbfs(path):
    """10th percentile of 100 ms frame RMS, in dBFS: a stand-in for the noise floor."""
    audio, rate = sf.read(str(path), dtype="float32", always_2d=False)
    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    frame = max(1, int(rate * 0.1))
    count = len(audio) // frame
    if count == 0:
        return float("nan")
    frames = audio[: count * frame].reshape(count, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-10
    return float(20 * np.log10(np.percentile(rms, 10)))


def denoise(source, out=None, *, start=0.0, dur=0.0, nf=-25.0, overwrite=False, strict=False,
            compare_to=None):
    """Write a cleaned reference window and return its report dict.

    ``verdict`` is ``pass``, ``warn``, or ``fail``. A clip is refused as too short at 5 s or less. With
    ``compare_to`` (a clean reference of the same speaker), the cleaned clip must still match that target,
    and cleaning must not move it away from the target by more than MAX_TARGET_LOSS. Without a target the
    identity is not checked and the verdict is at least ``warn``. Strict mode raises instead of writing a
    failing clip.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise DenoiseError(f"Input clip not found: {source}")
    if not -80 <= nf <= -20:
        raise DenoiseError("afftdn noise floor must be between -80 and -20 dB.")
    ffmpeg = find_ffmpeg()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = Path(out) if out else DEFAULT_OUT_DIR / f"{source.stem}-denoised-{stamp}.wav"
    destination = destination.resolve()
    if destination.exists() and not overwrite:
        raise DenoiseError(f"Refusing to replace an existing file (pass --overwrite to replace): {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    cleaned_filter = f"afftdn=nf={nf:g},aresample={SAMPLE_RATE},aformat=channel_layouts=mono"
    plain_filter = f"aresample={SAMPLE_RATE},aformat=channel_layouts=mono"
    with tempfile.TemporaryDirectory(prefix="denoise-", dir=str(destination.parent)) as scratch:
        plain = Path(scratch) / "plain.wav"
        cleaned = Path(scratch) / "cleaned.wav"
        _run_ffmpeg(ffmpeg, source, plain, start=start, dur=dur, audio_filter=plain_filter)
        _run_ffmpeg(ffmpeg, source, cleaned, start=start, dur=dur, audio_filter=cleaned_filter)

        import speaker_similarity as similarity_model

        seconds = sf.info(str(cleaned)).duration
        levels = {"pass": 0, "warn": 1, "fail": 2}
        worst, reasons = ["pass"], []

        def note(level, text):
            reasons.append(text)
            worst[0] = max(worst[0], level, key=levels.get)

        if seconds <= MIN_TURBO_SECONDS:
            note("fail", f"window is {seconds:.2f} s; Chatterbox Turbo needs more than {MIN_TURBO_SECONDS:g} s")
        # Noise removal itself moves the embedding, so the drift from the uncleaned window is reported
        # for information only. The identity decision compares against a clean target of the same speaker.
        drift = similarity_model.similarity(plain, cleaned)
        target = {"path": str(compare_to) if compare_to else None, "uncleaned": None, "cleaned": None,
                  "cleaned_band": None}
        if compare_to:
            raw_match = similarity_model.similarity(plain, compare_to)
            clean_match = similarity_model.similarity(cleaned, compare_to)
            target_band = similarity_model.band(clean_match)
            target.update(uncleaned=round(raw_match, 4), cleaned=round(clean_match, 4), cleaned_band=target_band)
            if target_band == "likely_different":
                note("fail", f"cleaned voice no longer matches {Path(compare_to).name} ({clean_match:.3f})")
            elif target_band == "uncertain":
                note("warn", f"cleaned voice match to {Path(compare_to).name} is uncertain ({clean_match:.3f})")
            if clean_match < raw_match - MAX_TARGET_LOSS:
                note("fail", f"cleaning moved the voice away from {Path(compare_to).name} "
                             f"({raw_match:.3f} raw, {clean_match:.3f} cleaned)")
        else:
            note("warn", "identity not checked: pass --compare-to with a clean reference of the same speaker")
        band = target["cleaned_band"]
        verdict = worst[0]
        floor_before, floor_after = _quiet_floor_dbfs(plain), _quiet_floor_dbfs(cleaned)
        if strict and verdict == "fail":
            raise DenoiseError("; ".join(reasons))
        # Only a clip that passed (or was allowed through) reaches its final name.
        os.replace(cleaned, destination)

    report = {
        "schema_version": 1,
        "source": str(source),
        "source_sha256": sha256_file(source),
        "output": str(destination),
        "output_sha256": sha256_file(destination),
        "window": {"start_seconds": float(start), "duration_seconds": round(seconds, 3)},
        "filter": cleaned_filter,
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "identity": {"proxy_drift_from_uncleaned_window": round(drift, 4),
                     "target": target,
                     "target_band": band,
                     "proxy_note": "Proxy speaker similarity (see speaker_similarity.py). Decisions use the target only."},
        "quiet_floor_dbfs": {"before": round(floor_before, 2), "after": round(floor_after, 2),
                             "reduction_db": round(floor_before - floor_after, 2)},
        "verdict": verdict,
        "reasons": reasons,
        "created_at": stamp,
    }
    destination.with_suffix(".json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input")
    parser.add_argument("--out", help="output WAV path (default: out/denoise/<name>-denoised-<UTC time>.wav)")
    parser.add_argument("--start", type=float, default=0.0, help="window start in seconds")
    parser.add_argument("--dur", type=float, default=0.0, help="window length in seconds (0 = to the end)")
    parser.add_argument("--nf", type=float, default=-25.0, help="afftdn noise floor in dB (proven default -25)")
    parser.add_argument("--compare-to", help="reference WAV to score the cleaned clip against")
    parser.add_argument("--overwrite", action="store_true", help="replace --out if it already exists")
    parser.add_argument("--strict", action="store_true", help="refuse to write a clip whose verdict is fail")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = denoise(args.input, args.out, start=args.start, dur=args.dur, nf=args.nf,
                         overwrite=args.overwrite, strict=args.strict, compare_to=args.compare_to)
    except DenoiseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 3 if report["verdict"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
