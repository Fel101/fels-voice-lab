"""Draft transcripts with faster-whisper: one reference clip, or a manifest of clips for the language screen.

This runs only in the isolated .venv-asr environment, never in the main lab environment,
and it reads a local model folder without downloading anything. It prints one JSON object
on stdout. The text is a draft: a person must check every word before it becomes an
exact reference transcript.

    .venv-asr\\Scripts\\python.exe -I asr_worker.py --model-dir hf-cache\\faster-whisper-tiny-d90ca5fe --audio <clip.wav>
    .venv-asr\\Scripts\\python.exe -I asr_worker.py --model-dir hf-cache\\faster-whisper-tiny-d90ca5fe --manifest <clips.json>

A manifest is a JSON list of {"id", "audio", "language"} objects. Each clip is transcribed with
its language forced, so a wrong guess shows up as word errors instead of a silent detection.
"""

import argparse
import json
import sys
from pathlib import Path


def transcribe_manifest(model, items):
    """Transcribe each clip with its language forced; returns one {id, text} per clip, in order."""
    results = []
    for item in items:
        segments, _ = model.transcribe(item["audio"], language=item["language"], beam_size=5,
                                       vad_filter=False, condition_on_previous_text=False)
        results.append({"id": item["id"], "text": " ".join(segment.text.strip() for segment in segments).strip()})
    return results


def main(argv=None):
    # The parent reads this worker's stdout as UTF-8. A Windows pipe defaults to the ANSI code page,
    # which cannot hold most non-Latin transcripts, so the encoding is set explicitly here.
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", required=True, help="local faster-whisper model folder (no downloads)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--audio", help="reference clip to transcribe")
    source.add_argument("--manifest", help="JSON list of {id, audio, language} clips to transcribe")
    args = parser.parse_args(argv)

    from faster_whisper import WhisperModel

    model = WhisperModel(args.model_dir, device="cpu", compute_type="int8", local_files_only=True)
    if args.manifest:
        items = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        json.dump({"items": transcribe_manifest(model, items)}, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    segments, info = model.transcribe(args.audio, beam_size=5, vad_filter=False,
                                      condition_on_previous_text=False)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    json.dump({
        "text": text,
        "language": info.language,
        "language_probability": round(float(info.language_probability), 3),
        "duration_seconds": round(float(info.duration), 3),
    }, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
