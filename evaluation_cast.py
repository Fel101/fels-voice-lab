"""Snapshot saved Cast performances into the existing blind-listening workflow."""

from __future__ import annotations

import io
import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone

import numpy as np
import pyloudnorm as pyln
import soundfile as sf

from evaluation_diagnostics import analyze_audio
from evaluation_store import digest_bytes, digest_json
from runtime_folders import COMPARISONS_DIR
import synthetic_label


TAKE_ID = re.compile(r"[a-f0-9]{32}\Z")
MAX_TAKES = 20
MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def _listening_copy(content, destination):
    audio, rate = sf.read(io.BytesIO(content), dtype="float32", always_2d=True)
    if (not len(audio) or audio.shape[1] > 2 or len(audio) / rate > 600 or
            not np.isfinite(audio).all()):
        raise ValueError("Selected performance audio must be finite mono/stereo audio up to 10 minutes.")
    peak = float(np.max(np.abs(audio)))
    input_lufs, target_gain = None, 1.0
    if len(audio) / rate >= 0.4 and peak > 0:
        measured = float(pyln.Meter(rate).integrated_loudness(audio))
        if math.isfinite(measured):
            input_lufs = measured
            target_gain = 10 ** ((-23.0 - measured) / 20)
    ceiling = 10 ** (-1.0 / 20)
    gain = min(target_gain, 10 ** (12.0 / 20), ceiling / peak if peak else 1.0)
    # Labelled as AI-generated, but with no engine named: these copies are for blind listening.
    synthetic_label.write_wav(destination, np.clip(audio * gain, -ceiling, ceiling), rate)
    return {
        "method": "per-clip EBU R128 gain match with -1 dBFS sample-peak ceiling",
        "target_lufs": -23.0, "input_lufs": input_lufs,
        "applied_gain_db": 20 * math.log10(gain), "peak_ceiling_dbfs": -1.0,
        "compression": False,
        "note": "LUFS unavailable for silence or clips shorter than 400 ms; peak ceiling still applies."
        if input_lufs is None else "Gain is capped at +12 dB and by the sample-peak ceiling.",
    }


def import_cast_takes(cast, evaluations, take_ids, *, title="Saved voice comparison"):
    """Copy selected, hash-verified takes; never synthesize, change Cast or invent ratings.

    Comparison keys identify candidates, allowing different voices/takes from the
    same engine. The actual engine and inherited reference terms stay in metadata.
    """
    if (not isinstance(take_ids, list) or not 2 <= len(take_ids) <= MAX_TAKES or
            any(not isinstance(value, str) or not TAKE_ID.fullmatch(value) for value in take_ids)):
        raise ValueError("Select 2–20 saved performance take IDs.")
    if len(set(take_ids)) != len(take_ids):
        raise ValueError("Select each performance only once.")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        raise ValueError("Comparison title must contain 1–120 characters.")
    if cast.root.resolve() != evaluations.lab_root.resolve():
        raise ValueError("Cast and evaluation storage must use the same selected state root.")
    root = evaluations.lab_root.resolve()
    comparisons_root = (evaluations.out_root / COMPARISONS_DIR).resolve()
    if root not in comparisons_root.parents:
        raise ValueError("Evaluation output must remain inside the selected state root.")
    records, total_bytes = [], 0
    for take_id in take_ids:
        take = cast.get_take(take_id)
        if take["retired"]:
            raise ValueError(f"Take {take_id} is retired and cannot be compared; choose another take.")
        source = cast.get_media_path("take", take_id).resolve()
        takes_root = (cast.data_root / "media" / "takes").resolve()
        if takes_root not in source.parents or not source.is_file():
            raise ValueError("A selected performance is outside managed Cast take storage.")
        size = source.stat().st_size
        total_bytes += size
        if size > MAX_AUDIO_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("Selected audio exceeds the comparison import size limit.")
        content = source.read_bytes()
        if digest_bytes(content) != take.get("audio_sha256"):
            raise ValueError("A selected performance failed its saved audio checksum; source unchanged.")
        condition = (take["text"], take["spoken_text"], take["language"], take["direction"])
        records.append((take, content, condition))
    counts = Counter(condition for _, _, condition in records)
    if any(counts[condition] < 2 for _, _, condition in records):
        raise ValueError("Every selected take needs a partner with identical text, spoken text, language and direction. Generate a shared test line in Perform first.")

    run_id = "cast-" + uuid.uuid4().hex
    run_dir = comparisons_root / run_id
    raw_dir, listening_dir = run_dir / "raw", run_dir / "listening"
    raw_dir.mkdir(parents=True, exist_ok=False)
    listening_dir.mkdir()
    created_files = []
    try:
        conditions, samples, labels = {}, [], {}
        for take, content, condition in records:
            case_id = conditions.setdefault(condition, f"shared-line-{len(conditions) + 1:02d}")
            candidate_id = "candidate-" + take["id"]
            label = (f"{take['character_name']} / {take['identity_label']} / "
                     f"{take['engine_id']} / take {take['id'][:8]}")
            labels[candidate_id] = label
            raw_path = raw_dir / (take["id"] + ".wav")
            listening_path = listening_dir / (take["id"] + ".wav")
            created_files.extend((raw_path, listening_path))
            with raw_path.open("xb") as output:
                output.write(content)
            mastering = _listening_copy(content, listening_path)
            samples.append({
                "item_id": uuid.uuid4().hex, "case_id": case_id, "repetition": 0,
                "engine_id": candidate_id, "expected_text": take["text"],
                "raw_path": raw_path.relative_to(run_dir).as_posix(),
                "raw_sha256": take["audio_sha256"],
                "listening_path": listening_path.relative_to(run_dir).as_posix(),
                "listening_sha256": digest_bytes(listening_path.read_bytes()),
                "metadata": {"source": "cast_performance", "candidate_label": label,
                    "take_id": take["id"], "character_id": take["character_id"],
                    "identity_id": take["identity_id"], "engine_id": take["engine_id"],
                    "source_audio_sha256": take["audio_sha256"], "spoken_text": take["spoken_text"],
                    "language": take["language"], "direction": take["direction"],
                    "settings": take["settings"], "model": take["model"],
                    "provenance": take["provenance"], "loudness_match": mastering},
                "diagnostics": analyze_audio(raw_path, expected_text=take["text"]),
            })
        manifest = {"schema_version": 1, "run_id": run_id, "title": title.strip(),
            "suite_id": "saved-cast-candidates", "suite_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "scope": "candidate_voice_audition", "candidate_labels": labels,
            "comparison_note": "Shared text/language/direction; voices, references, settings and engines may differ. This is a candidate audition, not a controlled model benchmark.",
            "conditions": [{"case_id": case_id, "text": condition[0], "spoken_text": condition[1],
                            "language": condition[2], "direction": condition[3]}
                           for condition, case_id in conditions.items()],
            "summary": {"cast_import": {"candidates": len(samples), "shared_lines": len(conditions)}},
            "samples": samples}
        manifest["checksum"] = digest_json(manifest)
        manifest_path = run_dir / "results.json"
        created_files.append(manifest_path)
        with manifest_path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        return evaluations.register_run_manifest(manifest_path)
    except Exception:
        # These are the explicit files and empty directories created by this call.
        # Never traverse or recursively remove a user-selected directory.
        for target in reversed(created_files):
            target.unlink(missing_ok=True)
        for folder in (listening_dir, raw_dir, run_dir):
            if folder.exists():
                folder.rmdir()
        raise
