"""The label inside every audio file of speech the Lab makes: that it is synthetic, made by an AI model, and what made it.

An export package's manifest says where each clip came from, but a file that leaves the package carries only what is inside
it. So every WAV the Lab writes of a take, an edited take, a performance, a voice conversion or a scene mix, and every file the
exporter encodes (WAV, FLAC, Ogg, MP3 or AAC), holds a comment saying it is AI-generated speech, with the IPTC digital source
type for media made by a trained model, and names the Lab as the software that made it. Reference recordings, the voices of
real people that the clones start from, are never labelled.

A tag is easy to strip, so it is a label and not a watermark. Chatterbox's clips also carry Resemble AI's Perth watermark,
which is inside the sound itself. The EU AI Act (Article 50) asks providers of systems that generate synthetic audio to mark
it in a machine-readable way; whether it applies to you, and what else it asks, is for your own advice.
"""

import io
import os
import json
from pathlib import Path

import numpy as np
import soundfile as sf

from app_version import APP_NAME, VERSION

SOFTWARE = f"{APP_NAME} {VERSION}"
# IPTC's controlled vocabulary for how a picture or a sound came to be: this term means made by a trained AI model.
SOURCE_TYPE = "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
PREFIX = "AI-generated speech"
CATALOG = Path(__file__).resolve().parent / "catalog" / "engines.json"
_names = {}


def engine_name(engine_id):
    """The catalog's name for an engine, such as "Kokoro 82M"; its id when the catalog does not know it."""
    if not _names:
        try:
            engines = json.loads(CATALOG.read_text(encoding="utf-8"))["engines"]
            _names.update({entry["id"]: entry.get("name", entry["id"]) for entry in engines})
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return _names.get(engine_id, str(engine_id))


def comment(engine_id=None):
    """The words of the label. Without an engine (a scene mix, a package's delivery of several lines) it names none."""
    made = f" by {engine_name(engine_id)}" if engine_id else ""
    return (
        f"{PREFIX}: a synthetic voice made{made} with {SOFTWARE}, not a recording of a person speaking. "
        f"IPTC digital source type: {SOURCE_TYPE}"
    )


def is_labelled(text):
    return isinstance(text, str) and text.startswith(PREFIX + ":")


def write_wav(target, audio, sample_rate, *, engine_id=None, subtype="PCM_16"):
    """Write a WAV (a path, or a file object such as BytesIO) carrying the label in its INFO comment, with the Lab as its
    software."""
    samples = np.asarray(audio)
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    with sf.SoundFile(target, "w", int(sample_rate), channels, subtype=subtype, format="WAV") as out:
        out.comment = comment(engine_id)
        out.software = SOFTWARE
        out.write(samples)


def wav_bytes(audio, sample_rate, *, engine_id=None, subtype="PCM_16"):
    buffer = io.BytesIO()
    write_wav(buffer, audio, sample_rate, engine_id=engine_id, subtype=subtype)
    return buffer.getvalue()


def label_file(path, *, engine_id=None):
    """Label a 16-bit WAV that was written piece by piece while it streamed (a Drama take): its samples are read and written
    back unchanged, with the label."""
    path = Path(path)
    samples, rate = sf.read(path, dtype="int16")
    temporary = path.with_name(path.name + ".labelled")
    write_wav(temporary, samples, rate, engine_id=engine_id)
    os.replace(temporary, path)


def ffmpeg_args(engine_id=None):
    """FFmpeg options that put the label in a file it encodes. comment and encoded_by survive in all five formats the exporter
    writes: WAV INFO, FLAC and Ogg Vorbis comments, MP3 ID3 frames and the AAC/M4A atoms (which keep the comment only)."""
    return ["-metadata", f"comment={comment(engine_id)}", "-metadata", f"encoded_by={SOFTWARE}"]
