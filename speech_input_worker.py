"""Isolated audio decode and faster-whisper inference for the speech-input API."""

import json
import sys

import av
import numpy as np
from faster_whisper import WhisperModel

RATE = 16_000
MAX_SAMPLES = RATE * 600


def decode(source, demuxer):
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    chunks, total = [], 0
    with av.open(source, mode="r", format=demuxer) as media:
        if not media.streams.audio:
            raise ValueError("The file contains no audio.")

        def get_frames():
            for frame in media.decode(audio=0):
                yield from resampler.resample(frame)
            yield from resampler.resample(None)

        for frame in get_frames():
            total += frame.samples
            if total > MAX_SAMPLES:
                raise ValueError("Local recognition accepts at most ten minutes per file.")
            chunks.append(frame.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError("The file contains no audio samples.")
    return np.concatenate(chunks).astype(np.float32) / 32768


def main(model_folder, source, demuxer, options):
    audio = decode(source, demuxer)
    model = WhisperModel(model_folder, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
    segments, info = model.transcribe(
        audio,
        task=options["task"],
        language=options["language"],
        initial_prompt=options["prompt"] or None,
        temperature=options["temperature"],
        beam_size=3,
        vad_filter=False,
        condition_on_previous_text=False,
        word_timestamps=options["word_timestamps"],
    )
    rows, words = [], []
    for part in segments:
        rows.append(
            {
                "id": part.id,
                "start": part.start,
                "end": part.end,
                "text": part.text,
                "seek": part.seek,
                "tokens": part.tokens,
                "temperature": part.temperature,
                "avg_logprob": part.avg_logprob,
                "compression_ratio": part.compression_ratio,
                "no_speech_prob": part.no_speech_prob,
            }
        )
        words.extend({"word": item.word.strip(), "start": item.start, "end": item.end} for item in part.words or ())
    result = {
        "task": options["task"],
        "language": info.language,
        "duration": len(audio) / RATE,
        "text": "".join(part["text"] for part in rows).strip(),
        "segments": rows,
    }
    if options["word_timestamps"]:
        result["words"] = words
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
