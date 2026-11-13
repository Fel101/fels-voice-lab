"""Word times for Cast takes, from forced alignment of each take's spoken text.

A take's word times are a derived record stored beside the take. They never change the take audio, and each
record names the aligner that produced it. align_takes runs the aligner once for a whole batch, so a scene
pays for one model load. Captions use these times only as a view: a line without word times keeps its
line-level caption, and the export says which one it used.
"""

import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import engine_adapters

LAB = Path(__file__).resolve().parent
ALIGN_PYTHON = LAB / ".venv-asr" / "Scripts" / "python.exe"
ALIGN_WORKER = LAB / "align_worker.py"
ALIGN_MODEL_DIR = LAB / "hf-cache" / "faster-whisper-tiny-d90ca5fe"
ALIGN_TIMEOUT_SECONDS = 1800
SCHEMA = 1
UNKNOWN_ALIGNER = {"id": "unknown"}
# Whisper's language codes for the app's language labels. A label without a code is not aligned.
WHISPER_CODE = {
    "English": "en", "American English": "en", "British English": "en", "Spanish": "es", "French": "fr",
    "German": "de", "Italian": "it", "Portuguese": "pt", "Brazilian Portuguese": "pt", "Russian": "ru",
    "Chinese": "zh", "Mandarin Chinese": "zh", "Japanese": "ja", "Korean": "ko", "Hindi": "hi", "Arabic": "ar",
}
# Scripts that do not put spaces between words; their phrases join without a space.
UNSPACED_CODES = {"zh", "ja", "th", "lo", "my", "km"}
# Markup is removed only where the engine acts on it instead of saying it. Everything else in a line is spoken, so it
# stays in the text that is aligned: Kokoro, Qwen, VoxCPM and Chatterbox Original read bracketed or parenthesised words aloud.
SPEAKER_MARKER = re.compile(r"<\|speaker:\d+\|>")  # no engine says it
SQUARE_TAG = re.compile(r"\[[^\[\]\n]*\]")
ROUND_TAG = re.compile(r"\([^()\n]*\)")
MARKUP = {"square": SQUARE_TAG, "round": ROUND_TAG}
ENGINE_MARKUP = {
    "drama-3": (SQUARE_TAG,),      # [whispers] and other delivery cues, with <|speaker:N|> markers
    # Chatterbox Turbo's [laugh] [chuckle] [cough], and the other engines whose adapters say (engine_adapters/)
    **{engine: (MARKUP[kind],) for engine, kind in engine_adapters.table("acted_markup").items()},
}
SENTENCE_END = (".", "!", "?", "。", "！", "？")
PHRASE_MAX_CHARS = 42
PHRASE_MAX_SECONDS = 6.0


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _without(text, patterns):
    text = str(text or "")
    for pattern in patterns:
        text = pattern.sub(" ", text)
    return " ".join(text.split())


def speech_text(spoken_text, engine_id=None):
    """The words an engine speaks: speaker markers, and the tag syntax that engine acts on, removed; spacing tidied.
    An engine not listed in ENGINE_MARKUP acts on no tags, so every other word in its text is kept."""
    return _without(spoken_text, (SPEAKER_MARKER, *ENGINE_MARKUP.get(engine_id, ())))


def text_without_markup(text):
    """The text with every markup syntax the app's engines act on removed, for text that a different engine will speak."""
    return _without(text, (SPEAKER_MARKER, SQUARE_TAG, ROUND_TAG))


def whisper_code(language_label):
    return WHISPER_CODE.get(str(language_label or "").strip())


def joiner_for(code):
    return "" if code in UNSPACED_CODES else " "


def stored_timings(raw):
    """A take that was never aligned has an empty value; that means not aligned, not aligned with no words."""
    if not raw:
        return None
    return json.loads(raw)


def run_aligner(items, scratch_dir=None):
    """Run align_worker.py in the isolated .venv-asr environment, once, for every item."""
    scratch = Path(scratch_dir) if scratch_dir else LAB / "out" / "word-timings"
    scratch.mkdir(parents=True, exist_ok=True)
    manifest = scratch / f"align-{uuid.uuid4().hex[:12]}.json"
    manifest.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    completed = subprocess.run(
        [str(ALIGN_PYTHON), "-I", str(ALIGN_WORKER), "--model-dir", str(ALIGN_MODEL_DIR), "--manifest", str(manifest)],
        cwd=str(LAB), capture_output=True, timeout=ALIGN_TIMEOUT_SECONDS, check=False)
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise RuntimeError("The aligner failed: " + (detail[-1] if detail else f"exit {completed.returncode}"))
    lines = [line for line in completed.stdout.decode("utf-8").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("The aligner produced no result.")
    return json.loads(lines[-1])


def _error_record(take, reason, language=None, text=None, aligner=None):
    return {"schema": SCHEMA, "aligner": aligner or UNKNOWN_ALIGNER, "audio_sha256": take.get("audio_sha256"),
            "language": language, "text": text, "aligned_at": utc_now(), "error": reason}


def align_takes(takes, *, run_worker=None, scratch_dir=None):
    """Align every take in one aligner call and return {take_id: record}. A take that cannot be aligned gets
    an error record rather than words, so a later run can tell "failed" from "never tried"."""
    run_worker = run_worker or run_aligner
    by_id = {take["id"]: take for take in takes}
    records, items = {}, []
    for take in takes:
        code = whisper_code(take.get("language"))
        text = speech_text(take.get("spoken_text"), take.get("engine_id"))
        if code is None:
            records[take["id"]] = _error_record(take, "the language has no Whisper code", take.get("language"), text)
        elif not text:
            records[take["id"]] = _error_record(take, "the take has no speech text", take.get("language"), text)
        else:
            items.append({"id": take["id"], "audio": str(take["media_path"]), "text": text, "language": code})
    if items:
        output = run_worker(items, scratch_dir)
        aligner = output.get("aligner") or UNKNOWN_ALIGNER
        results = {item["id"]: item for item in output.get("items", [])}
        for item in items:
            take = by_id[item["id"]]
            result = results.get(item["id"], {"error": "the aligner returned no result for this take"})
            if result.get("error"):
                records[item["id"]] = _error_record(take, result["error"], item["language"], item["text"], aligner)
            else:
                records[item["id"]] = {
                    "schema": SCHEMA, "aligner": aligner, "audio_sha256": take.get("audio_sha256"),
                    "language": item["language"], "text": item["text"], "aligned_at": utc_now(),
                    "duration_seconds": result["duration_seconds"], "words": result["words"],
                    "checks": result["checks"],
                }
    return records


def shifted_words(segments):
    """Word times for one line. Each segment is (duration_seconds, words) for one take, in the order the line plays
    them; each take's words move by the durations of the takes before it, as the scene mix places them."""
    shifted, offset = [], 0.0
    for duration, words in segments:
        shifted.extend({"word": word["word"], "start": word["start"] + offset, "end": word["end"] + offset,
                        "probability": word.get("probability")} for word in words)
        offset += float(duration)
    return shifted


def word_phrases(words, joiner):
    """Group aligned words into caption phrases: at most PHRASE_MAX_CHARS characters and PHRASE_MAX_SECONDS seconds,
    always ending a phrase after a sentence mark. Phrase text is the words joined as the language joins them."""
    phrases, current = [], []

    def close():
        if current:
            phrases.append({"start_seconds": round(current[0]["start"], 3),
                            "end_seconds": round(max(current[-1]["end"], current[0]["start"]), 3),
                            "text": joiner.join(word["word"] for word in current)})
            current.clear()

    for word in words:
        if current:
            text = joiner.join([item["word"] for item in current] + [word["word"]])
            span = word["end"] - current[0]["start"]
            if len(text) > PHRASE_MAX_CHARS or span > PHRASE_MAX_SECONDS:
                close()
        current.append(word)
        if word["word"].endswith(SENTENCE_END):
            close()
    close()
    return phrases
