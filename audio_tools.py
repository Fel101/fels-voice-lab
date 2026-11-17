"""Create and measure local production audio without changing source recordings."""

import hashlib
import io
import math
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

import loudness
import synthetic_label
from scene_store import canonical_json, new_id

MAX_UPLOAD = 48 * 1024 * 1024
MAX_SAMPLES = 8_000_000
PRECISIONS = {"PCM_16": "16-bit PCM", "PCM_24": "24-bit PCM", "FLOAT": "32-bit floating point"}
SOUNDS = {
    "rain": ("rain", "drizzle", "shower"),
    "surf": ("surf", "sea", "ocean", "waves", "shore"),
    "wind": ("wind", "breeze", "gust"),
    "fire": ("fire", "crackle", "campfire"),
    "chime": ("chime", "bell"),
}


def number(value, label, low, high):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be between {low} and {high}.")
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        parsed = math.nan
    if not low <= parsed <= high:
        raise ValueError(f"{label} must be between {low} and {high}.")
    return parsed


def seed_value(value):
    seed = number(value, "Variation number", 0, 2**32 - 1)
    if not seed.is_integer():
        raise ValueError("Variation number must be a whole number.")
    return int(seed)


def read_audio(raw):
    if not raw or len(raw) > MAX_UPLOAD:
        raise ValueError("Choose a local WAV or FLAC up to 48 MiB.")
    try:
        with sf.SoundFile(io.BytesIO(raw)) as source:
            if source.format not in {"WAV", "WAVEX", "FLAC", "RF64"}:
                raise ValueError("Choose a local WAV or FLAC source.")
            if (
                source.channels not in (1, 2)
                or source.frames <= 0
                or source.frames * source.channels > MAX_SAMPLES
                or not 8000 <= source.samplerate <= 96000
            ):
                raise ValueError("Use mono or stereo audio at 8–96 kHz, with at most 8 million decoded samples.")
            subtype, rate = source.subtype, source.samplerate
            comment = source.comment or ""
            audio = source.read(dtype="float64", always_2d=True)
    except sf.LibsndfileError as exc:
        raise ValueError("The source audio could not be read.") from exc
    if not audio.size or not np.isfinite(audio).all():
        raise ValueError("The audio has no samples or contains invalid samples.")
    return audio, rate, subtype, comment


def publish(root, audio, rate, precision, receipt, *, synthetic=False, comment=""):
    if precision not in PRECISIONS:
        raise ValueError("Select PCM16, PCM24 or float32 WAV.")
    directory = Path(root) / "out" / "audio-tools"
    directory.mkdir(parents=True, exist_ok=True)
    ident = new_id()
    with tempfile.TemporaryDirectory(dir=directory, prefix=".audio-") as temporary:
        delivery = Path(temporary) / "delivery"
        delivery.mkdir()
        path = delivery / "audio.wav"
        if receipt["method"] == "procedural-sfx-v1":
            comment = "Synthetic sound effect: procedural effect made with Voice Lab."
        if comment:
            samples = np.asarray(audio)
            channels = 1 if samples.ndim == 1 else samples.shape[1]
            with sf.SoundFile(path, "w", rate, channels, subtype=precision, format="WAV") as result:
                result.comment = comment
                result.software = "Voice Lab"
                result.write(samples)
        elif synthetic:
            synthetic_label.write_wav(path, audio, rate, engine_id=receipt["method"], subtype=precision)
        else:
            sf.write(path, audio, rate, subtype=precision)
        info = sf.info(path)
        receipt.update(
            id=ident,
            saved_precision=info.subtype,
            saved_precision_label=PRECISIONS[info.subtype],
            channels=info.channels,
            sample_rate=info.samplerate,
            seconds=info.duration,
            output_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            output_measurements=loudness.measure_file(path),
            audio_url=f"/api/audio-tools/{ident}/audio.wav",
            receipt_url=f"/api/audio-tools/{ident}/receipt.json",
        )
        (delivery / "receipt.json").write_text(canonical_json(receipt) + "\n", encoding="utf-8")
        delivery.rename(directory / ident)
    return receipt


def master(root, raw, settings):
    audio, rate, source_precision, comment = read_audio(raw)
    source = loudness.measure(audio, rate)
    gain = number(settings.get("gain_db", 0), "Gain (dB)", -36, 24)
    target = settings.get("target_lufs")
    if target is not None and target != "":
        target = number(target, "Target loudness (LUFS)", -36, -9)
        if source["integrated_lufs"] is None:
            raise ValueError("This source has no integrated loudness reading.")
        gain += target - source["integrated_lufs"]
    ceiling = number(settings.get("ceiling_dbtp", -1), "True-peak ceiling (dBTP)", -12, -0.1)
    limit = settings.get("limit", True)
    if type(limit) is not bool:
        raise ValueError("Limiter must be true or false.")
    audio *= 10 ** (gain / 20)
    reduction = 0.0
    if limit:
        limit_amplitude = 10 ** ((ceiling - 0.03) / 20)
        # Soft-knee limiting leaves samples below the knee untouched. Verify the rebuilt peak afterwards.
        knee = limit_amplitude * 0.83
        amplitude = np.abs(audio)
        excess = np.maximum(amplitude - knee, 0)
        limited = np.minimum(amplitude, knee) + (limit_amplitude - knee) * np.tanh(excess / (limit_amplitude - knee))
        audio = np.copysign(limited, audio)
        peak = loudness.measure(audio, rate)["true_peak_dbtp"]
        if peak is not None and peak > ceiling - 0.02:
            reduction = ceiling - 0.03 - peak
            audio *= 10 ** (reduction / 20)
    precision = settings.get("precision", "PCM_24")
    if precision != "FLOAT" and np.max(np.abs(audio)) >= 1:
        raise ValueError("The result would clip in PCM. Reduce gain or enable the true-peak limiter.")
    return publish(
        root,
        audio,
        rate,
        precision,
        {
            "method": "mastering",
            "source_precision": source_precision,
            "source_precision_label": PRECISIONS.get(source_precision, source_precision),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "source_measurements": source,
            "gain_db": round(gain, 3),
            "target_lufs": target if target not in (None, "") else None,
            "limiter": limit,
            "ceiling_dbtp": ceiling if limit else None,
            "true_peak_trim_db": round(reduction, 3),
        },
        comment=comment if comment.startswith(("AI-generated speech:", "Synthetic sound effect:")) else "",
    )


def _sound(kind, timeline, random, rate):
    noise = random.normal(0, 1, len(timeline))
    if kind in {"rain", "surf", "wind"}:
        low, high = {"rain": (650, 8800), "surf": (55, 2400), "wind": (70, 640)}[kind]
        filtered = sosfilt(butter(3, (low, high), fs=rate, btype="bandpass", output="sos"), noise)
        if kind == "rain":
            return filtered * 0.32
        if kind == "surf":
            swell = 0.28 + 0.72 * (0.5 + 0.5 * np.sin(2 * np.pi * timeline / 5.4 - 1.3)) ** 2
        else:
            swell = 0.4 + 0.3 * np.sin(timeline * 1.1) + 0.24 * np.sin(timeline * 2.7)
        return filtered * swell
    if kind == "fire":
        bed = 0.2 * sosfilt(butter(2, 230, fs=rate, output="sos"), noise)
        for event in random.integers(0, len(timeline), max(1, len(timeline) // 2600)):
            width = min(len(timeline) - event, int(0.055 * rate))
            decay = np.exp(-np.arange(width) / (rate * 0.008))
            bed[event : event + width] += random.uniform(0.08, 0.44) * noise[event : event + width] * decay
        return bed
    result = np.zeros_like(timeline)
    for freq, level, decay in ((523.25, 0.56, 1.3), (783.99, 0.26, 2.1), (1046.5, 0.17, 2.9), (1872, 0.055, 5.9)):
        phase = random.uniform(-0.08, 0.08)
        result += level * np.sin(2 * np.pi * freq * timeline + phase) * np.exp(-decay * timeline)
    return result


def sound_effect(root, settings):
    import re

    description = settings.get("description", "")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 180:
        raise ValueError("Enter a short sound description.")
    tokens = set(re.findall("[a-z]+", description.casefold()))
    matched = [name for name, words in SOUNDS.items() if tokens.intersection(words)]
    if len(matched) != 1:
        raise ValueError("Describe one supported sound: rain, surf, wind, fire or a chime.")
    duration = number(settings.get("seconds", 5), "Duration", 0.4, 180)
    seed = seed_value(settings.get("seed", 42))
    rate = 24000
    timeline = np.arange(round(rate * duration)) / rate
    kind = matched[0]
    audio = _sound(kind, timeline, np.random.default_rng(seed), rate)
    attack = 0.008 if kind == "chime" else 0.12
    audio *= np.minimum(timeline / attack, 1) * np.minimum((duration - timeline) / 0.14, 1)
    audio *= 0.6 / max(np.max(np.abs(audio)), 1e-8)
    return publish(
        root,
        audio,
        rate,
        "PCM_24",
        {"method": "procedural-sfx-v1", "preset": kind, "description": description, "seed": seed, "synthetic": True},
        synthetic=True,
    )
