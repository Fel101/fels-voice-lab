"""Measure word and phoneme timing against Kokoro's own predicted durations.

Kokoro reports when each word and phoneme is spoken (kokoro_timing.py). That is the model's own prediction, not a
human label, so it is the reference here and not the truth. The tool speaks English sentences from the Cast takes
with Kokoro, aligns the same audio with the Whisper aligner, and reports how far the aligned word times and the
phoneme times (shared out over each word, as lipsync.py does) sit from Kokoro's times.

    .venv\\Scripts\\python.exe tools\\timing_validation.py [--limit 40] [--publish]
"""

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import soundfile as sf  # noqa: E402

from common import pick_device  # noqa: E402  (sets the model cache and device selection before any model import)
import lipsync  # noqa: E402
import word_timing  # noqa: E402

EVIDENCE = ROOT / "docs" / "evidence" / "timing-validation.json"
REFERENCE = "Kokoro af_heart predicted durations at 25 ms frames: the synthesis model's own timing, not a human label"


def english_texts(limit):
    db = sqlite3.connect(f"file:{(ROOT / 'data' / 'cast' / 'cast.sqlite3').as_posix()}?mode=ro", uri=True)
    rows = db.execute("SELECT DISTINCT spoken_text FROM takes WHERE language='English' ORDER BY spoken_text").fetchall()
    db.close()
    texts = []
    for (raw,) in rows:
        text = word_timing.text_without_markup(raw)  # Kokoro speaks these, whichever engine's tags they carry
        if 12 <= len(text) <= 120 and text not in texts:
            texts.append(text)
    return texts[:limit]


def summarize(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return {"count": len(values), "median_ms": round(statistics.median(ordered) * 1000, 1),
            "p90_ms": round(p90 * 1000, 1)}


def kokoro_phonetic(timing):
    return [item for item in timing["phonemes"] if item["symbol"] not in lipsync.MARKERS]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=40, help="sentences to measure (from the Cast English takes)")
    parser.add_argument("--publish", action="store_true", help="write docs/evidence/timing-validation.json")
    args = parser.parse_args(argv)

    from worker_pool import EnginePool  # imported after the environment is set up
    texts = english_texts(args.limit)
    scratch = ROOT / "out" / "timing-validation" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    scratch.mkdir(parents=True, exist_ok=True)
    spoken = []
    pool = EnginePool(pick_device("auto"), idle_minutes=5)
    try:
        pool.load("kokoro")
        for index, text in enumerate(texts):
            wav, rate, info = pool.speak({"engine": "kokoro", "text": text, "voice": "af_heart"})
            path = scratch / f"utt-{index:03d}.wav"
            sf.write(str(path), wav, rate)
            spoken.append({"id": f"utt-{index:03d}", "text": text, "audio": str(path), "timing": info.get("timing"),
                           "timing_unavailable": info.get("timing_unavailable"), "duration": wav.size / rate})
    finally:
        pool.close()
    aligned = word_timing.run_aligner([{"id": item["id"], "audio": item["audio"], "text": item["text"],
                                        "language": "en"} for item in spoken], scratch)
    by_id = {item["id"]: item for item in aligned["items"]}

    word_start, word_end, word_end_no_final = [], [], []
    phone_a_start, phone_a_end, phone_b_start, phone_b_end = [], [], [], []
    utterances, skipped, sequence_mismatch = [], [], 0
    for item in spoken:
        if item["timing"] is None:
            skipped.append({"id": item["id"], "reason": item["timing_unavailable"] or "no Kokoro timing"})
            continue
        result = by_id.get(item["id"], {})
        if "error" in result or "words" not in result:
            skipped.append({"id": item["id"], "reason": result.get("error", "not aligned")})
            continue
        whisper = result["words"]
        kokoro_words = item["timing"]["words"]
        try:
            misaki_words = lipsync.english_word_phonemes(item["text"])
        except Exception as exc:  # pragma: no cover - reported, never silently dropped
            skipped.append({"id": item["id"], "reason": f"phonemes: {type(exc).__name__}"})
            continue
        if not (len(whisper) == len(kokoro_words) == len(misaki_words)):
            skipped.append({"id": item["id"], "reason": f"word counts differ: aligned {len(whisper)}, "
                                                        f"kokoro {len(kokoro_words)}, phonetic {len(misaki_words)}"})
            continue
        ds = [abs(w["start"] - k["start"]) for w, k in zip(whisper, kokoro_words)]
        de = [abs(w["end"] - k["end"]) for w, k in zip(whisper, kokoro_words)]
        word_start.extend(ds)
        word_end.extend(de)
        word_end_no_final.extend(de[:-1])
        reference = kokoro_phonetic(item["timing"])
        symbols = [s for _, sym in misaki_words for s in sym if s not in lipsync.MARKERS]
        if [p["symbol"] for p in reference] != symbols:
            sequence_mismatch += 1
            skipped.append({"id": item["id"], "reason": "phoneme sequences differ from misaki's"})
            continue
        spans_a = lipsync.distribute_words([(w["start"], w["end"]) for w in whisper], misaki_words)
        spans_b = lipsync.distribute_words([(k["start"], k["end"]) for k in kokoro_words], misaki_words)
        for (_, a0, a1), (_, b0, b1), ref in zip(spans_a, spans_b, reference):
            phone_a_start.append(abs(a0 - ref["start"]))
            phone_a_end.append(abs(a1 - ref["end"]))
            phone_b_start.append(abs(b0 - ref["start"]))
            phone_b_end.append(abs(b1 - ref["end"]))
        utterances.append({"id": item["id"], "words": len(whisper), "phonemes": len(reference),
                           "duration_seconds": round(item["duration"], 3)})

    report = {
        "schema": 1, "reference": REFERENCE,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "aligner": aligned.get("aligner"), "voice": "af_heart",
        "utterances_measured": len(utterances), "utterances_skipped": skipped,
        "sequence_mismatch": sequence_mismatch,
        "word_start_error": summarize(word_start), "word_end_error": summarize(word_end),
        "word_end_error_excluding_final_word": summarize(word_end_no_final),
        "phoneme_start_error_distributed_from_aligned_words": summarize(phone_a_start),
        "phoneme_end_error_distributed_from_aligned_words": summarize(phone_a_end),
        "phoneme_start_error_distributed_from_reference_words": summarize(phone_b_start),
        "phoneme_end_error_distributed_from_reference_words": summarize(phone_b_end),
        "utterances": utterances,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (scratch / "timing-validation.json").write_text(text, encoding="utf-8")
    if args.publish:
        EVIDENCE.write_text(text + "\n", encoding="utf-8")
    print(f"measured {len(utterances)} utterances; skipped {len(skipped)}; evidence in {scratch}")
    print(f"word start error: {report['word_start_error']}")
    print(f"word end error (excluding final word): {report['word_end_error_excluding_final_word']}")
    print(f"phoneme start error from aligned words: {report['phoneme_start_error_distributed_from_aligned_words']}")
    print(f"phoneme start error from reference words: {report['phoneme_start_error_distributed_from_reference_words']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
