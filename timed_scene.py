"""Validate timed scene fields and assemble selected takes on the subtitle clock."""

from math import gcd
import re

import numpy as np
from scipy.signal import resample_poly

from subtitle_import import FORMATS, MAX_CUE_TEXT, MAX_SOURCE_MS, MAX_TIMELINE_MS, clock_time, cue_overlaps, subtitle_speech

SAMPLE_RATE = 24_000
CUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
ASSEMBLY = "Selected takes placed at cue starts, with overlaps mixed and overruns kept."
PROCESSING_STEPS = [
    "resampled takes to 24 kHz where needed",
    "placed takes on the subtitle clock",
    "mixed overlaps with peak protection",
    "wrote PCM 16-bit WAV",
]


def _timestamp(value, label):
    if type(value) is not int or not 0 <= value <= MAX_TIMELINE_MS:
        raise ValueError(f"{label} must be between 00:00.000 and 30:00.000. Edit the cue time and try again.")
    return value


def validate_timeline(value):
    if not isinstance(value, dict) or value.get("format") not in (*FORMATS, "transcript"):
        raise ValueError("Import SRT, WebVTT or a Voice Slate transcript to add a timeline.")
    duration = _timestamp(value.get("duration_ms"), "Timeline duration")
    if not duration:
        raise ValueError("Timeline duration must be positive.")
    fingerprint = value.get("source_sha256")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
        raise ValueError("Timeline source_sha256 must be a lowercase SHA-256 checksum.")
    result = {"format": value["format"], "duration_ms": duration, "source_sha256": fingerprint}
    if "source_offset_ms" in value:
        offset = value["source_offset_ms"]
        if type(offset) is not int or not 0 <= offset <= MAX_SOURCE_MS - duration:
            raise ValueError("The start in the source video is invalid. Import the original subtitles again.")
        result["source_offset_ms"] = offset
    if "source_cues" in value:
        sources = value["source_cues"]
        if not isinstance(sources, list) or len(sources) > 500:
            raise ValueError("A source track must be an array of at most 500 cues.")
        result["source_cues"] = []
        seen = set()
        for index, row in enumerate(sources, 1):
            line_id = row.get("line_id") if isinstance(row, dict) else None
            if not isinstance(line_id, str) or not CUE_ID.fullmatch(line_id) or line_id in seen:
                raise ValueError("Every source cue needs a unique line ID.")
            seen.add(line_id)
            label = f"Original subtitle {index}"
            cue = validate_cue(row, label)
            text = row.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_CUE_TEXT:
                raise ValueError(f"{label} has invalid text.")
            if cue["end_ms"] > duration:
                raise ValueError(f"{label} exceeds its source timeline.")
            result["source_cues"].append(
                {
                    "line_id": line_id,
                    "start_ms": cue["start_ms"],
                    "end_ms": cue["end_ms"],
                    "text": text,
                    "speaker": cue.get("speaker", ""),
                }
            )
    return result


def validate_cue(value, line_id):
    label = line_id
    if isinstance(value, dict) and type(value.get("start_ms")) is int and 0 <= value["start_ms"] <= MAX_TIMELINE_MS:
        label += f" ({clock_time(value['start_ms'])})"
    if not isinstance(value, dict):
        raise ValueError(f"{label} needs a start and end time. Open the line and enter its cue times.")
    start = _timestamp(value.get("start_ms"), f"{label} start")
    end = _timestamp(value.get("end_ms"), f"{label} end")
    if start >= end:
        raise ValueError(f"{label} must end after it starts. Open the line and set a later end time.")
    cue = {"start_ms": start, "end_ms": end}
    if any(key in value for key in ("source_start_ms", "source_end_ms", "source_text")):
        source_start = _timestamp(value.get("source_start_ms"), f"{label} source start")
        source_end = _timestamp(value.get("source_end_ms"), f"{label} source end")
        if source_start >= source_end:
            raise ValueError(f"{label} source must end after it starts.")
        text = value.get("source_text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CUE_TEXT:
            raise ValueError(f"{label} has invalid source text.")
        cue.update(source_start_ms=source_start, source_end_ms=source_end, source_text=text)
    for key, limit in (("source_id", 200), ("speaker", 60)):
        if key in value:
            field = value[key]
            if not isinstance(field, str) or len(field) > limit:
                raise ValueError(f"{label} {key} must be text of up to {limit} characters.")
            cue[key] = field
    for key in ("source_line_ids", "replaces"):
        if key in value:
            ids = value[key]
            if (
                not isinstance(ids, list)
                or len(ids) > 500
                or any(not isinstance(item, str) or not CUE_ID.fullmatch(item) for item in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError(f"{label} {key} must be a list of unique cue IDs.")
            cue[key] = ids
    return cue


def source_track(scene):
    """Recover the source clock for timed scenes saved before source tracks were stored separately."""
    timeline = scene["timeline"]
    if "source_cues" in timeline:
        return timeline["source_cues"]
    rows = []
    for line in scene["lines"]:
        cue = line.get("cue")
        if not cue or "source_text" not in cue:
            continue
        text, speaker = subtitle_speech(cue["source_text"], timeline["format"])
        rows.append(
            {
                "line_id": line["line_id"],
                "start_ms": cue["source_start_ms"],
                "end_ms": cue["source_end_ms"],
                "text": text,
                "speaker": speaker,
            }
        )
    return rows


def resample_take(audio, rate):
    if rate == SAMPLE_RATE:
        return audio
    divisor = gcd(rate, SAMPLE_RATE)
    return resample_poly(audio, SAMPLE_RATE // divisor, rate // divisor).astype(np.float32)


def validate_dub_settings(value):
    if not isinstance(value, dict):
        raise ValueError("Dubbing controls must be an object.")
    fit = value.get("fit", "natural")
    speed = value.get("speed", 1)
    gain = value.get("gain_db", 0)
    mute = value.get("mute", False)
    if fit not in ("natural", "fit") or type(speed) not in (float, int) or not 0.8 <= speed <= 1.35:
        raise ValueError("Choose natural or bounded fit with speed from 0.8× to 1.35×.")
    if type(gain) not in (float, int) or not -30 <= gain <= 6 or type(mute) is not bool:
        raise ValueError("Cue gain must be between −30 and +6 dB and mute must be true or false.")
    return {"fit": fit, "speed": speed, "gain_db": gain, "mute": mute}


def _dub_parts(line, parts, slot, warnings, label):
    from dubbing import change_tempo

    controls = validate_dub_settings(line.get("dub", {}))
    original = sum(len(part) for part in parts)
    speed = controls["speed"]
    if controls["fit"] == "fit" and slot:
        speed = min(1.35, max(speed, original / max(slot - SAMPLE_RATE // 100, 1)))
    if speed != 1 and original:
        parts = [change_tempo(np.concatenate(parts), speed)]
    gain = 10 ** (controls["gain_db"] / 20)
    if gain != 1:
        parts = [part * gain for part in parts]
    if controls["fit"] == "fit" and sum(len(part) for part in parts) > slot:
        warnings.append(
            {
                "code": "fit_limit",
                "message": f"{label} still runs long at the speed limit. Shorten the words or move the cue end.",
            }
        )
    return parts, {**controls, "applied_speed": speed, "original_samples": original}


def mix(timeline, tracks, *, dub=False):
    """Mix each line's sequential take segments at its exact cue start on a common sample grid."""
    samples_per_ms = SAMPLE_RATE // 1000
    max_samples = MAX_TIMELINE_MS * samples_per_ms
    end = timeline["duration_ms"] * samples_per_ms
    audio = np.zeros(end, dtype=np.float32)
    placements, warnings, adjustments = [], [], []
    for number, (line, parts) in enumerate(tracks, 1):
        cue = line["cue"]
        start = cue["start_ms"] * samples_per_ms
        slot_end = cue["end_ms"] * samples_per_ms
        label = f"Line {number} ({clock_time(cue['start_ms'])})"
        if dub and line.get("dub", {}).get("mute"):
            adjustments.append({"line_id": line["line_id"], **validate_dub_settings(line["dub"])})
            continue
        if dub:
            parts, controls = _dub_parts(line, parts, slot_end - start, warnings, label)
            adjustments.append({"line_id": line["line_id"], **controls})
        amount = sum(len(part) for part in parts)
        if amount == 0 or start + amount > max_samples:
            raise ValueError(f"{label} has no audio or runs beyond the scene limit. Choose a shorter take or move it earlier.")
        placements.append((line["line_id"], start, start + amount, label))
        end = max(end, slot_end, start + amount)
        if end > len(audio):
            expanded = np.zeros(min(max_samples, max(end, len(audio) * 2)), dtype=np.float32)
            expanded[: len(audio)] = audio
            audio = expanded
        offset = start
        for part in parts:
            stop = offset + len(part)
            audio[offset:stop] += part
            offset = stop
        if start + amount > slot_end:
            overrun = (start + amount - slot_end) / SAMPLE_RATE
            duration = f"{overrun:.1f} s" if overrun >= 0.05 else "less than 0.1 s"
            warnings.append({"code": "overrun", "message": f"{label} runs {duration} past its cue."})
    if not placements and not adjustments:
        raise ValueError("A timed scene has no selected audio. Render the lines and choose their takes.")
    warnings.extend(cue_overlaps((start, stop, label) for _, start, stop, label in placements))
    audio = audio[:end]
    peak = max(float(audio.max()), -float(audio.min()))
    gain = min(1.0, (32767 / 32768) / peak) if peak else 1.0
    if gain < 1.0:
        audio *= gain
        warnings.append({"code": "peak_reduced", "message": "Mix level reduced to avoid clipping."})
    return audio, {
        "mode": "dub" if dub else "timed_cues",
        **({"adjustments": adjustments} if dub else {}),
        "gain": gain,
        "warnings": warnings,
        **(
            {"source_offset_ms": timeline["source_offset_ms"], "video_start": clock_time(timeline["source_offset_ms"])}
            if "source_offset_ms" in timeline
            else {}
        ),
        "placements": [
            {"line_id": line_id, "start_sample": start, "end_sample": stop} for line_id, start, stop, _ in placements
        ],
    }
