"""Forced alignment of a known transcript to a clip, giving word times, in the isolated .venv-asr environment.

Whisper's cross-attention weights place each word of the text it is given on the audio. The words are
the ones supplied; only their times are estimated, so this is a forced aligner rather than a transcriber.
It uses the local multilingual faster-whisper copy (MIT code, MIT Whisper weights) and downloads nothing.
Times are seconds from the start of the clip, to the 20 ms resolution of the model's cross-attention frames.

    .venv-asr\\Scripts\\python.exe -I align_worker.py --model-dir hf-cache\\faster-whisper-tiny-d90ca5fe --manifest <items.json>

A manifest is a JSON list of {"id", "audio", "text", "language"} objects. The output is one JSON object on
stdout: {"items": [...]}, one entry per clip with "words" and "checks", or with "error" when that clip cannot
be aligned. One failing clip never stops the others.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ALIGNER = {"id": "faster-whisper-tiny-cross-attention", "method": "Whisper cross-attention, teacher-forced on the given text", "version": 1}
MAX_SECONDS = 30.0
WINDOW_SAMPLES = 400  # 25 ms energy windows at 16 kHz
HOP_SAMPLES = 160  # 10 ms hop
FRAME_SECONDS = HOP_SAMPLES / 16000
SPEECH_BELOW_PEAK_DB = 30.0  # frames this far below the clip's loudest frame count as silence
SILENCE_FLOOR_DB = -60.0  # a clip whose loudest frame is quieter than this has no speech at all
ONSET_GAP_MAX = 0.3  # seconds between the first word and the first speech frame
SILENT_WORD_MAX = 0.1  # share of words whose middle sits in silence
COVERAGE_RANGE = (0.7, 1.3)  # words span this share of the detected speech


def frame_db(audio):
    """Loudness of each 10 ms frame in dB, from 25 ms windows."""
    if audio.size < WINDOW_SAMPLES:
        audio = np.pad(audio, (0, WINDOW_SAMPLES - audio.size))
    windows = np.lib.stride_tricks.sliding_window_view(audio, WINDOW_SAMPLES)[::HOP_SAMPLES]
    rms = np.sqrt(np.mean(np.square(windows.astype(np.float64)), axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-9))


def speech_bounds(audio):
    """First and last 10 ms frames within SPEECH_BELOW_PEAK_DB of the clip's loudest frame, in seconds.
    A clip whose loudest frame is below SILENCE_FLOOR_DB has no speech at all."""
    db = frame_db(audio)
    peak = float(db.max())
    if peak < SILENCE_FLOOR_DB:
        return None, None
    speech = np.flatnonzero(db > peak - SPEECH_BELOW_PEAK_DB)
    if speech.size == 0:
        return None, None
    return float(speech[0] * FRAME_SECONDS), float((speech[-1] + 1) * FRAME_SECONDS)


def merge_punctuation(words):
    """Punctuation has no sound: attach it to the word before it instead of giving it a time of its own."""
    merged = []
    for word in words:
        if merged and not any(ch.isalnum() for ch in word["word"]):
            merged[-1]["word"] += word["word"]
        else:
            merged.append(dict(word))
    return merged


def snap_to_speech(words, audio):
    """Cross-attention gives leading and trailing silence to the first and last words; move those edges to the speech."""
    start, end = speech_bounds(audio)
    snapped = []
    if start is None or not words:
        return words, snapped
    if words[0]["start"] < start:
        words[0]["start"] = round(start, 3)
        snapped.append("first word start")
    if words[-1]["end"] > end:
        words[-1]["end"] = round(end, 3)
        snapped.append("last word end")
    for word in words:
        word["end"] = max(word["start"], word["end"])
    return words, snapped


def quality_checks(words, audio):
    """Heuristic checks against the audio's own energy. They flag clips for a person to review; they do not score speech."""
    db = frame_db(audio)
    peak = float(db.max())
    speech_start, speech_end = speech_bounds(audio)
    if not words or speech_start is None:
        return {"status": "review", "reason": "no speech energy or no words"}
    first_start = words[0]["start"]
    last_end = words[-1]["end"]
    onset_gap = abs(first_start - speech_start)
    silent = 0
    for word in words:
        # A word is silent only when every frame of its span is quiet. Plosive closures and quiet vowels have
        # quiet middles, so a midpoint test would call audible words silent.
        first = int(word["start"] / FRAME_SECONDS)
        last = max(int(word["end"] / FRAME_SECONDS), first) + 1
        span = db[min(first, db.size - 1):min(last, db.size)]
        if span.size == 0 or span.max() <= peak - SPEECH_BELOW_PEAK_DB:
            silent += 1
    silent_fraction = silent / len(words)
    coverage = (last_end - first_start) / max(speech_end - speech_start, FRAME_SECONDS)
    review = (onset_gap > ONSET_GAP_MAX or silent_fraction > SILENT_WORD_MAX
              or not COVERAGE_RANGE[0] <= coverage <= COVERAGE_RANGE[1])
    return {"status": "review" if review else "ok",
            "speech_start_seconds": round(speech_start, 3), "speech_end_seconds": round(speech_end, 3),
            "onset_gap_seconds": round(onset_gap, 3), "silent_word_fraction": round(silent_fraction, 3),
            "coverage": round(coverage, 3)}


class Aligner:
    """One loaded model; align_item aligns one manifest entry and never raises for a bad clip."""

    def __init__(self, model, decode_audio, pad_or_trim, tokenizer_class):
        self.model = model
        self.decode_audio = decode_audio
        self.pad_or_trim = pad_or_trim
        self.tokenizer_class = tokenizer_class

    def align_item(self, item):
        try:
            return self._align(item)
        except Exception as exc:  # one unreadable clip must not stop the batch
            detail = (str(exc).strip().splitlines() or [""])[0]
            return {"id": item.get("id"), "error": f"{type(exc).__name__}: {detail}"[:400]}

    def _align(self, item):
        text = str(item.get("text", "")).strip()
        if not text:
            raise ValueError("the clip has no text to align")
        audio = self.decode_audio(item["audio"], sampling_rate=16000)
        seconds = audio.size / 16000
        if seconds > MAX_SECONDS:
            raise ValueError(f"the clip is {seconds:.1f} s; alignment takes clips up to {MAX_SECONDS:.0f} s")
        features = self.model.feature_extractor(audio)
        num_frames = features.shape[-1]
        encoder_output = self.model.encode(self.pad_or_trim(features))
        tokenizer = self.tokenizer_class(self.model.hf_tokenizer, self.model.model.is_multilingual,
                                         task="transcribe", language=item["language"])
        text_tokens = tokenizer.encode(text)
        if not text_tokens:
            raise ValueError("the text produced no tokens")
        timings = self.model.find_alignment(tokenizer, [text_tokens], encoder_output, num_frames)[0]
        words = [{"word": timing["word"].strip(), "start": round(float(timing["start"]), 3),
                  "end": round(float(timing["end"]), 3), "probability": round(float(timing["probability"]), 3)}
                 for timing in timings if timing["word"].strip()]
        words, snapped = snap_to_speech(merge_punctuation(words), audio)
        if not words:
            raise ValueError("no word times were produced")
        checks = quality_checks(words, audio)
        checks["snapped_edges"] = snapped
        return {"id": item["id"], "duration_seconds": round(seconds, 3), "words": words, "checks": checks}


def main(argv=None):
    # The parent reads this worker's stdout as UTF-8; a Windows pipe defaults to the ANSI code page.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", required=True, help="local faster-whisper model folder (no downloads)")
    parser.add_argument("--manifest", required=True, help="JSON list of {id, audio, text, language}")
    args = parser.parse_args(argv)

    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio, pad_or_trim
    from faster_whisper.tokenizer import Tokenizer

    model = WhisperModel(args.model_dir, device="cpu", compute_type="int8", local_files_only=True)
    aligner = Aligner(model, decode_audio, pad_or_trim, Tokenizer)
    items = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    results = [aligner.align_item(item) for item in items]
    json.dump({"aligner": ALIGNER, "items": results}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
