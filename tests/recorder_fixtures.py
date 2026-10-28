"""Generated stand-ins for Voice Slate output: a speaker folder with a kept clip, its master and its sidecar.

Nothing here is a real recording. The "voice" is a seeded vowel-sequence synthesizer, enough for the speaker encoder
to give stable embeddings; the processing chain follows Voice Slate's order (high-pass, noise reduction with FFmpeg's
afftdn, gain, resample).
"""

import hashlib
import json
import subprocess
import tempfile
from math import gcd
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, lfilter, resample_poly, sosfiltfilt

KEPT_RATE = 24_000
MASTER_RATE = 48_000
STEM = "20261020-161204_mic_3f9a1c2e"


def synth_voice(seconds=12.0, rate=KEPT_RATE, f0=120.0, seed=0, formant_shift=1.0):
    """A deterministic speech-like signal: jittered glottal pulses through moving formants, syllables and pauses."""
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    f = f0 * (1 + 0.10 * np.sin(2 * np.pi * 0.35 * t + 1.0) + 0.03 * np.sin(2 * np.pi * 5.0 * t))
    pulses = (np.diff(np.floor(np.cumsum(f / rate)), prepend=0) > 0).astype(np.float64)
    source = lfilter([1.0], [1.0, -0.97], pulses) + 0.02 * rng.normal(size=n)
    vowels = [(730, 1090, 2440), (270, 2290, 3010), (300, 870, 2240), (530, 1840, 2480), (570, 840, 2410)]
    segment = int(0.28 * rate)
    out, position, index = np.zeros(n), 0, 0
    while position < n:
        end = min(n, position + segment)
        part = source[position:end].copy()
        for centre, width in zip((v * formant_shift for v in vowels[index % len(vowels)]), (80, 90, 120)):
            r = np.exp(-np.pi * width / rate)
            part = lfilter([1 - r], [1, -2 * r * np.cos(2 * np.pi * centre / rate), r * r], part)
        out[position:end] += part * np.hanning(len(part)) ** 0.6
        position = end - segment // 4
        index += 1
        if index % 6 == 0:
            position += int(0.25 * rate)
    out *= 0.6 + 0.4 * np.sin(2 * np.pi * 3.7 * t) ** 2
    for centre in (1.0, 3.3, 5.9, 8.4, 10.7):
        start, length = int(centre * rate), int(0.09 * rate)
        if start + length < n:
            out[start:start + length] += 0.08 * lfilter([1, -1], [1], rng.normal(size=length)) * np.hanning(length) * np.max(np.abs(out))
    return (out / (np.max(np.abs(out)) + 1e-9) * 0.7).astype(np.float32)


def resample(samples, rate_in, rate_out):
    if rate_in == rate_out:
        return np.asarray(samples, dtype=np.float32)
    g = gcd(rate_in, rate_out)
    return resample_poly(np.asarray(samples, dtype=np.float64), rate_out // g, rate_in // g).astype(np.float32)


def voiced_db(samples, rate):
    frame = int(rate * 0.02)
    rms = np.sqrt(np.mean(samples[:len(samples) // frame * frame].astype(np.float64).reshape(-1, frame) ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-12)
    return 10 * np.log10(np.mean(rms[db > db.max() - 30] ** 2) + 1e-24)


def noisy_master(voice, rate=MASTER_RATE, snr_db=25.0, seed=1, level_db=-32.0):
    """The voice at the master's rate with pink noise added, scaled to a microphone-like level."""
    x = resample(voice, KEPT_RATE, rate)
    rng = np.random.default_rng(seed)
    spectrum = np.fft.rfft(rng.normal(size=len(x)))
    freqs = np.fft.rfftfreq(len(x), 1 / rate)
    freqs[0] = freqs[1]
    noise = np.fft.irfft(spectrum / np.sqrt(freqs), len(x))
    noise *= 10 ** (voiced_db(x, rate) / 20) / np.sqrt(np.mean(noise ** 2)) * 10 ** (-snr_db / 20)
    mixed = (x + noise).astype(np.float32)
    return (mixed * 10 ** ((level_db - voiced_db(mixed, rate)) / 20)).astype(np.float32)


def afftdn(samples, rate, filter_text, ffmpeg):
    """Run FFmpeg's afftdn the way Voice Slate does: float WAV in, float WAV out, same length."""
    with tempfile.TemporaryDirectory(prefix="recorder-fixture-") as tmp:
        source, cleaned = Path(tmp) / "in.wav", Path(tmp) / "out.wav"
        sf.write(str(source), samples, rate, subtype="FLOAT", format="WAV")
        done = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
                               "-af", filter_text, "-c:a", "pcm_f32le", str(cleaned)], capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError(done.stderr)
        out, _ = sf.read(str(cleaned), dtype="float32")
    return np.pad(out, (0, max(0, len(samples) - len(out))))[:len(samples)]


def keep(master, rate=MASTER_RATE, *, filter_text=None, ffmpeg=None, gain_db=None, cuts=(), highpass_hz=80.0):
    """The kept clip (24 kHz, float) Voice Slate would make: cuts, high-pass, optional afftdn, level, resample."""
    x = master
    if cuts:
        keep_ranges, position = [], 0
        for start, end in sorted((round(a * rate), round(b * rate)) for a, b in cuts):
            if start > position:
                keep_ranges.append((position, start))
            position = end
        if position < len(x):
            keep_ranges.append((position, len(x)))
        x = np.concatenate([x[a:b] for a, b in keep_ranges])
    x = sosfiltfilt(butter(4, highpass_hz, btype="highpass", fs=rate, output="sos"), x).astype(np.float32)
    if filter_text:
        x = afftdn(x, rate, filter_text, ffmpeg)
    gain = gain_db if gain_db is not None else -20.0 - voiced_db(x, rate)
    x = np.clip(x * 10 ** (gain / 20), -0.89, 0.89).astype(np.float32)
    y = resample(x, rate, KEPT_RATE)
    return (np.round(np.clip(y, -1, 1) * 32767) / 32767).astype(np.float32)


def sidecar_template(stem=STEM, clip_id="3f9a1c2e", speaker="my-voice", seconds=12.0, master_seconds=12.0,
                     master_rate=MASTER_RATE, sha256="", edits=(), nf=None):
    denoise = {"enabled": nf is not None, "nf": nf if nf is not None else -25.0}
    if nf is not None:
        denoise.update(filter=f"afftdn=nf={nf:g}", identity_checked=False)
    return {
        "schema": "voice-recorder-clip-v2", "software": {"name": "voice-slate", "version": "0.2.0"},
        "id": clip_id, "created": "2026-10-20T16:12:04+01:00", "speaker": speaker, "source": "mic",
        "device": {"name": "Studio Microphone", "hostapi": "Windows WASAPI", "native_rate": master_rate, "channels": 1},
        "export": {"file": f"{stem}.wav", "sample_rate": KEPT_RATE, "channels": 1, "bit_depth": 16,
                   "duration_seconds": round(seconds, 3), "sha256": sha256},
        "master": {"file": f"masters/{stem}.raw.wav", "sample_rate": master_rate, "bit_depth": 24, "channels": 1},
        "measurements": {"peak_dbfs": -3.1, "rms_dbfs": -22.4, "clipped_fraction": 0.0, "silence_fraction": 0.21},
        "transcript": "The morning ferry left the harbour just as the fog began to lift.",
        "transcript_status": "typed", "origin": "Recorded by me at my desk",
        "allowed_use": "Personal narration voice, not shared", "consent_confirmed": True, "source_url": None,
        "master_seconds": round(master_seconds, 3), "edits": list(edits),
        "join_fade_ms": 10.0,
        "chain": {"highpass": {"enabled": True, "hz": 80.0}, "hum": {"enabled": False, "hz": 60.0, "harmonics": 3},
                  "denoise": denoise, "level": {"gain_db": 5.1}, "limiter": {"enabled": True, "ceiling_db": -1.0}},
        "needs_transcript_review": False, "notes": "",
        "flags": ["filtered", "level_changed", "limited"] + (["cleaned"] if nf is not None else []),
    }


def write_clip(root, *, speaker="my-voice", stem=STEM, clip_id="3f9a1c2e", kept=None, master=None,
               master_rate=MASTER_RATE, edits=(), nf=None, change=None):
    """Write <root>/<speaker>/{masters/<stem>.raw.wav, <stem>.wav, <stem>.json} and return the sidecar's path.

    ``kept`` (24 kHz) and ``master`` default to the same synthetic voice. ``change(sidecar)`` may alter the sidecar
    dict before it is written; the WAV's SHA-256 is filled in first, so a test can then break it on purpose.
    """
    folder = Path(root) / speaker
    (folder / "masters").mkdir(parents=True, exist_ok=True)
    if kept is None:
        kept = synth_voice(8.0)
    if master is None:
        master = resample(synth_voice(8.0), KEPT_RATE, master_rate)
    sf.write(str(folder / "masters" / f"{stem}.raw.wav"), master, master_rate, subtype="PCM_24", format="WAV")
    wav = folder / f"{stem}.wav"
    sf.write(str(wav), kept, KEPT_RATE, subtype="PCM_16", format="WAV")
    sidecar = sidecar_template(stem, clip_id, speaker, len(kept) / KEPT_RATE, len(master) / master_rate, master_rate,
                               hashlib.sha256(wav.read_bytes()).hexdigest(), edits, nf)
    if change:
        change(sidecar)
    path = folder / f"{stem}.json"
    path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def edited(path, change):
    """Rewrite a sidecar on disk with ``change(dict)`` applied (the WAV is left alone)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    change(data)
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
