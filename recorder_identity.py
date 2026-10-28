"""Did Voice Slate's processing change the voice? Compare a kept clip with its untouched master.

Voice Slate keeps the original recording beside every kept clip but cannot tell whether noise reduction (or anything
else in its chain) changed how the voice sounds; its sidecar says ``identity_checked: false``. This compares the
speaker embedding of the kept clip with that of the master, using the speaker encoder voice-lab already has
(speaker_similarity.py: Chatterbox's encoder, CPU, local weights only), and flags a clip that moved too far.

What the score is, and is not:

- It measures Voice Slate's processing alone, with no TTS model involved. Cloning the clip and scoring the clone
  (speaker_similarity.voice_match) also measures that model's own drift.
- It is a *change* check. Removing noise changes the embedding of a noisy recording whether or not the voice got worse
  (an earlier design study of the denoiser found this and dropped the same comparison as a pass/fail identity gate). A flag therefore
  means "listen to it". ``against`` (a clean recording of the same voice) shows which way the clip moved.
- The encoder is not level-invariant (it reads unnormalised power-mel), so both sides are brought to one speech level
  first; without that, a 20 dB gain change alone scores 0.75 to 0.81 and Voice Slate applies up to +36 dB.
- The sidecar lists the cuts made on the master, so the master is cut the same way before comparing: the same
  material, not different words.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundfile as sf

RESULT_SCHEMA = "voice-lab-identity-check-v1"
# Chosen from the measurements in docs/RECORDER_BRIDGE.md: untouched-voice processing (high-pass, gain, limiter,
# resampling) scored 0.945 to 0.99 there, every afftdn setting Voice Slate offers from nf -50 up scored 0.76 to 0.92.
DEFAULT_MIN_SIMILARITY = 0.90
TARGET_TOLERANCE = 0.02          # as denoise_ref.MAX_TARGET_LOSS: closer or further than this counts as a move
MATCH_LEVEL_DB = -20.0           # both sides' voiced RMS; Voice Slate's own Normalize target
SILENT_DB = -70.0
FRAME_SECONDS = 0.02
MIN_CUT_SECONDS = 0.020          # Voice Slate ignores shorter cut selections
MAX_JOIN_FADE_MS = 50.0
MAX_MASTER_SECONDS = 600.0
ENCODER_LABEL = "chatterbox-turbo ve.safetensors (speaker encoder, CPU)"
READING = ("A change check, not a damage check: noise reduction can move a noisy recording's speaker embedding toward "
           "the clean voice as well as away from it. A flag means listen to the master and the clip; pass `against` "
           "(a clean recording of the same voice) to see which way the clip moved.")


class Unavailable(RuntimeError):
    """The check cannot run for a reason that belongs in the report, not in a traceback."""


def _mono(samples):
    samples = np.asarray(samples, dtype=np.float32)
    return samples if samples.ndim == 1 else samples.mean(axis=1, dtype=np.float32)


def _frame_rms(samples, rate, seconds):
    frame = max(1, int(rate * seconds))
    usable = len(samples) // frame * frame
    if usable == 0:
        return np.array([math.sqrt(float(np.mean(samples.astype(np.float64) ** 2)))]) if len(samples) else np.zeros(1)
    return np.sqrt(np.mean(samples[:usable].astype(np.float64).reshape(-1, frame) ** 2, axis=1))


def voiced_rms_db(samples, rate):
    """RMS in dBFS of the voiced part: 20 ms frames within 30 dB of the loudest frame (as Voice Slate measures it)."""
    rms = _frame_rms(_mono(samples), rate, FRAME_SECONDS)
    db = 20 * np.log10(rms + 1e-12)
    voiced = db > db.max() - 30.0
    return 10 * math.log10(float(np.mean(rms[voiced] ** 2)) + 1e-24)


def quiet_floor_db(samples, rate):
    """10th percentile of 100 ms frame RMS in dBFS: a stand-in for the noise floor."""
    return float(20 * np.log10(np.percentile(_frame_rms(_mono(samples), rate, 0.1), 10) + 1e-12))


def match_level(samples, rate):
    """The samples scaled so their voiced RMS is MATCH_LEVEL_DB. Silence cannot be matched."""
    samples = _mono(samples)
    level = voiced_rms_db(samples, rate)
    if level < SILENT_DB:
        raise Unavailable(f"the audio is silent (voiced level {level:.0f} dBFS)")
    return (samples * 10 ** ((MATCH_LEVEL_DB - level) / 20)).astype(np.float32)


def aligned_master(master, rate, sidecar, kept_seconds):
    """The master with the sidecar's cuts and silences applied, so it holds the same material as the kept clip.

    Returns ``(samples, mode, note)``. ``mode`` is ``edit_aligned``, or ``whole_master`` when there is nothing to align
    or the edit list does not reproduce the clip's length; ``note`` then says why. Fades and region gains change only
    level, which is matched afterwards. Joins are not crossfaded: the embedding does not hear ten milliseconds.
    """
    n = len(master)
    cuts, silences = [], []
    try:
        for edit in sidecar.get("edits") or []:
            op = edit.get("op", "cut")
            if op not in ("cut", "silence"):
                continue
            start, end = float(edit["start_seconds"]), float(edit["end_seconds"])
            lo, hi = max(0, round(start * rate)), min(n, round(end * rate))
            if hi > lo and (op == "silence" or hi - lo >= MIN_CUT_SECONDS * rate):
                (cuts if op == "cut" else silences).append((lo, hi))
        fade_ms = float(sidecar.get("join_fade_ms", 10))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return master, "whole_master", "the sidecar's edit list could not be read, so the whole master was compared"
    merged = []
    for lo, hi in sorted(cuts):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    segments, position = [], 0
    for lo, hi in merged:
        if lo > position:
            segments.append((position, lo))
        position = hi
    if position < n:
        segments.append((position, n))
    if not segments:
        return master, "whole_master", "the sidecar's cuts remove the whole master, so the whole master was compared"
    fade = round(min(max(fade_ms, 0.0), MAX_JOIN_FADE_MS) / 1000 * rate) if math.isfinite(fade_ms) else 0
    material = sum(hi - lo for lo, hi in segments)
    expected = (material - (len(segments) - 1) * fade) / rate          # the clip's length once the joins are crossfaded
    if abs(expected - kept_seconds) > 0.05 + 0.012 * len(merged):      # cut points snap to zero crossings, 5 ms a side
        return master, "whole_master", (f"the sidecar's edits give {expected:.2f} s but the clip is "
                                         f"{kept_seconds:.2f} s, so the whole master was compared")
    if not merged and not silences:
        return master, "whole_master", ""
    work = master.copy()
    for lo, hi in silences:
        work[lo:hi] = 0.0
    return np.concatenate([work[lo:hi] for lo, hi in segments]), "edit_aligned", \
        f"{material / rate:.2f} s of the {n / rate:.2f} s master"


def _read_master(path):
    """The master as mono float samples, its rate, and what was found about it. Raises Unavailable."""
    if not path.is_file():
        raise Unavailable(f"the master {path.name} is not in the sidecar's folder")
    try:
        info = sf.info(str(path))
        if not info.samplerate or info.frames / info.samplerate > MAX_MASTER_SECONDS:
            raise Unavailable(f"the master is longer than {MAX_MASTER_SECONDS:g} s")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        samples, rate = sf.read(str(path), dtype="float32", always_2d=True)
    except (RuntimeError, OSError) as exc:
        raise Unavailable(f"the master is not readable audio ({exc})") from None
    found = {"file": path.name, "sha256": digest.hexdigest(), "sample_rate": int(rate), "channels": samples.shape[1],
             "seconds": round(len(samples) / rate, 3)}
    return _mono(samples), int(rate), found


def _processing(clip):
    chain = clip.data.get("chain")
    if not isinstance(chain, dict):
        return {}
    keep = (bool, int, float, str, type(None))
    return {name: {key: value for key, value in block.items() if isinstance(value, keep)}
            for name, block in chain.items() if isinstance(block, dict)}


def _default_embed(samples, rate):
    import speaker_similarity

    return speaker_similarity.embedding_from_samples(samples, rate)


def check_identity(clip, *, min_similarity=DEFAULT_MIN_SIMILARITY, against=None, embed=None, now=None):
    """Score how far the kept clip's voice fingerprint is from its master's, and flag it below ``min_similarity``.

    ``clip`` is a validated recorder_clip.RecorderClip. ``against`` is an optional clean recording of the same voice:
    the report then also says whether the clip is closer to it or further from it than the master was. ``embed`` is
    ``(samples, rate) -> unit vector`` and defaults to voice-lab's speaker encoder. The report is a plain dict;
    nothing is written (see store_result), and nothing is downloaded.
    """
    if not 0.0 < float(min_similarity) <= 1.0:
        raise ValueError("The similarity threshold must be above 0 and at most 1.")
    embed = embed or _default_embed
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    result = {
        "schema": RESULT_SCHEMA, "checked_at": stamp, "status": "unavailable", "verdict": None, "score": None,
        "threshold": float(min_similarity),
        "clip": {"id": clip.id, "speaker": clip.speaker, "export_sha256": clip.export_sha256,
                 "sidecar_sha256": clip.sidecar_sha256},
        "noise_reduction": clip.denoise, "processing": _processing(clip),
        "method": {"encoder": ENCODER_LABEL, "similarity": "cosine similarity of unit speaker embeddings",
                   "levels_matched_to_dbfs": MATCH_LEVEL_DB},
        "master": None, "target": None, "reasons": [], "reading": READING,
    }
    try:
        master, rate, found = _read_master(clip.master_path)
        declared = clip.data["master"]
        found["matches_sidecar"], differences = True, []
        for key, label in (("sample_rate", "sample rate"), ("channels", "channel count")):
            if declared.get(key) != found[key]:
                differences.append(f"{label} {found[key]} (sidecar says {declared.get(key)})")
        claimed = clip.data.get("master_seconds")
        if isinstance(claimed, (int, float)) and not isinstance(claimed, bool) and abs(claimed - found["seconds"]) > 0.05:
            differences.append(f"length {found['seconds']} s (sidecar says {claimed})")
        if differences:
            found["matches_sidecar"] = False
        result["master"] = found
        kept = sf.read(io.BytesIO(clip.wav_bytes), dtype="float32", always_2d=True)
        kept_samples, kept_rate = _mono(kept[0]), int(kept[1])
        reference, mode, note = aligned_master(master, rate, clip.data, len(kept_samples) / kept_rate)
        speech, floor = voiced_rms_db(master, rate), quiet_floor_db(master, rate)
        found["noise"] = {"speech_dbfs": round(speech, 1), "noise_floor_dbfs": round(floor, 1),
                          "speech_over_noise_db": round(speech - floor, 1)}
        result["method"].update(master_reference=mode, master_reference_note=note)
        kept_vector = embed(match_level(kept_samples, kept_rate), kept_rate)
        reference_vector = embed(match_level(reference, rate), rate)
        target_vector = None
        if against is not None:
            target_path = Path(against).expanduser().resolve()
            try:
                audio, target_rate = sf.read(str(target_path), dtype="float32", always_2d=True)
            except (RuntimeError, OSError) as exc:
                raise Unavailable(f"the `against` recording is not readable audio ({exc})") from None
            target_vector = embed(match_level(_mono(audio), int(target_rate)), int(target_rate))
            result["target"] = {"file": target_path.name}
    except Unavailable as exc:
        result["reasons"].append(f"The identity check could not run: {exc}.")
        return result
    except (FileNotFoundError, ImportError) as exc:
        result["reasons"].append(f"The identity check could not run: the speaker encoder is not available ({exc}).")
        return result

    score = float(np.dot(kept_vector, reference_vector))
    result.update(status="checked", score=round(score, 4), verdict="pass" if score >= min_similarity else "flag")
    reasons = result["reasons"]
    if not found["matches_sidecar"]:
        result["verdict"] = "flag"
        reasons.append("The master does not match its sidecar: " + "; ".join(differences) + ". It may not be the "
                       "recording this clip was made from, so the score below says little about the processing.")
    denoise = clip.denoise
    nf_text = f"afftdn nf={denoise['nf']:g}" if denoise["nf"] is not None else "noise reduction"
    if score < min_similarity:
        if denoise["enabled"]:
            reasons.append(f"Noise reduction was on ({nf_text}) and the clip's voice fingerprint is further from the "
                           f"master's than {min_similarity:.2f} ({score:.3f}). Listen to the master and the clip "
                           "before using it as a reference.")
        else:
            reasons.append(f"No noise reduction was applied, yet the clip's voice fingerprint moved ({score:.3f}, "
                           f"threshold {min_similarity:.2f}). Check that the master belongs to this clip, and look at "
                           "the high-pass, hum and limiter settings.")
        highpass = result["processing"].get("highpass", {})
        if highpass.get("enabled") is True and isinstance(highpass.get("hz"), (int, float)) and highpass["hz"] >= 150:
            reasons.append(f"The high-pass at {highpass['hz']:g} Hz also removes voice energy.")
    elif denoise["enabled"]:
        reasons.append(f"Noise reduction was on ({nf_text}) and the fingerprint stayed within the threshold "
                       f"({score:.3f}).")
    noise = found["noise"]["speech_over_noise_db"]
    if denoise["enabled"] and score < min_similarity and noise >= 30:
        reasons.append(f"The master's noise floor is about {noise:.0f} dB below the speech, so there was little "
                       "noise to remove.")
    if target_vector is not None:
        before, after = float(np.dot(reference_vector, target_vector)), float(np.dot(kept_vector, target_vector))
        change = after - before
        direction = "toward" if change > TARGET_TOLERANCE else "away" if change < -TARGET_TOLERANCE else "unchanged"
        result["target"].update(master_to_target=round(before, 4), clip_to_target=round(after, 4),
                                change=round(change, 4), direction=direction, tolerance=TARGET_TOLERANCE)
        reasons.append({"toward": f"Against {result['target']['file']} the clip is closer to that voice than the "
                                  f"master was ({after:.3f} against {before:.3f}).",
                        "away": f"Against {result['target']['file']} the clip is further from that voice than the "
                                f"master was ({after:.3f} against {before:.3f}): the processing moved the voice away.",
                        "unchanged": f"Against {result['target']['file']} the clip is about as close to that voice "
                                     f"as the master was ({after:.3f} against {before:.3f})."}[direction])
    return result


def _reference_folder(store, reference_id):
    """The folder holding a reference's source audio: where its provenance and identity checks are kept."""
    return store.get_media_path("reference_source", reference_id).parent


def store_result(store, reference_id, result):
    """Write a check result into voice-lab's own store, next to the reference it belongs to. Never overwrites.

    Voice Slate's sidecar is not touched; each check is a new ``identity-check-<UTC time>-<nn>.json`` file, so a later
    check with another threshold or encoder adds to the history instead of replacing it. File names sort by age.
    """
    folder = _reference_folder(store, reference_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    payload = (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    for attempt in range(1, 100):
        path = folder / f"identity-check-{stamp}-{attempt:02d}.json"
        try:
            with path.open("xb") as handle:
                handle.write(payload)
        except FileExistsError:
            continue
        return path
    raise RuntimeError("Could not find a free identity-check file name.")


def load_results(store, reference_id):
    """Every stored identity check for a reference, oldest first."""
    results = []
    for path in sorted(_reference_folder(store, reference_id).glob("identity-check-*.json")):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return results
