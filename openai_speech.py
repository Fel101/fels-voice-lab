"""Speech for apps that speak to OpenAI's speech API: what POST /v1/audio/speech takes, and the audio formats it answers in.

Many chat and assistant apps can send text to any server that answers like OpenAI's speech API (POST /v1/audio/speech with
model, input and voice). Here `voice` is a voice's name from GET /api/voices, `model` is a model's id when it names one
(anything else, such as tts-1, means the voice's own model), `instructions` is an acting direction for a voice whose model
takes one, and `speed` is the model's own speed control where it has one. The answer is in the format asked for: WAV, FLAC
and raw PCM (24 kHz, 16-bit, as OpenAI's is) need nothing more; MP3, Opus and AAC are encoded by FFmpeg. Like OpenAI's, the
default format is MP3. Nothing is kept in the library; every file with room for it carries the AI-generated label
(synthetic_label.py). A key in the request is accepted and ignored: the service stays on this computer, as every route does.
"""

import io
import subprocess

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

import engine_adapters
import ffmpeg_tools
import synthetic_label

DEFAULT_FORMAT = "mp3"
PCM_RATE = 24_000
MAX_INPUT = 10_000
SPEED_RANGE = (0.25, 4.0)
ENCODE_TIMEOUT = 300
# Each format's media type, and for the ones FFmpeg makes: its encoder (as ffmpeg_tools reports it, and as FFmpeg names
# it) and the options that make the file. OpenAI's Opus comes in an Ogg file, its AAC as an ADTS stream.
FORMATS = {
    "mp3": ("audio/mpeg", ("mp3", "libmp3lame"), ["-b:a", "128k", "-f", "mp3"]),
    "opus": ("audio/ogg", ("opus", "libopus"), ["-b:a", "64k", "-f", "ogg"]),
    "aac": ("audio/aac", ("aac", "aac"), ["-b:a", "128k", "-f", "adts"]),
    "flac": ("audio/flac", None, None),
    "wav": ("audio/wav", None, None),
    "pcm": ("audio/pcm", None, None),
}
# OpenAI's own voices. The Lab has none of them, so a request that names one is told where the Lab's names are.
OPENAI_VOICES = frozenset(
    {"alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer", "verse", "marin", "cedar"}
)


class NeedsFFmpeg(Exception):
    """A format FFmpeg makes was asked for, and this computer's FFmpeg cannot make it."""

    status_code = 503
    error_code = "ffmpeg_not_installed"


def parse(req):
    """The request as the Lab reads it: text, voice (a name), model, format, speed and instructions. ValueError says what is
    wrong with it."""
    if not isinstance(req, dict):
        raise ValueError("Send a JSON object.")
    text = req.get("input")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("input must be the text to speak.")
    if len(text) > MAX_INPUT:
        raise ValueError(f"input has {len(text):,} characters; the Lab takes up to {MAX_INPUT:,}. Send it in pieces.")
    voice = req.get("voice")
    if isinstance(voice, dict):
        voice = voice.get("id")
    if not isinstance(voice, str) or not voice.strip():
        raise ValueError("voice must be a voice's name; GET /v1/audio/voices lists them.")
    model = req.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("model must be text: a model's id from GET /v1/models, or any other name for the voice's own model.")
    response_format = req.get("response_format") or DEFAULT_FORMAT
    if response_format not in FORMATS:
        raise ValueError(f"response_format must be one of {', '.join(FORMATS)}.")
    stream_format = req.get("stream_format") or "audio"
    if stream_format != "audio":
        raise ValueError("stream_format must be audio: the Lab answers with the whole file, not with events.")
    speed = req.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not SPEED_RANGE[0] <= speed <= SPEED_RANGE[1]:
        raise ValueError("speed must be a number from 0.25 to 4.0.")
    instructions = req.get("instructions") or ""
    if not isinstance(instructions, str) or len(instructions) > 1000:
        raise ValueError("instructions must be text of 1,000 characters or fewer.")
    return {
        "text": text,
        "voice": voice.strip(),
        "model": model,
        "format": response_format,
        "speed": float(speed),
        "instructions": instructions.strip(),
    }


def unknown_voice(name):
    """What to say about a voice the Lab does not have."""
    if name.casefold() in OPENAI_VOICES:
        return (
            f"{name} is one of OpenAI's own voices, which the Lab does not have. Choose one of the Lab's: GET /v1/audio/voices "
            "lists them."
        )
    return f"There is no voice called {name!r}. GET /v1/audio/voices lists the voices that can speak."


def model_speed(voice, speed):
    """The speed to send the voice's model: None for its own pace (speed 1), else the speed, when the model has a speed
    control that reaches it. ValueError says what the model can do."""
    if speed == 1.0:
        return None
    adapter = engine_adapters.get(voice["model"])
    control = (getattr(adapter, "control_ui", None) or {}).get("speed") if adapter is not None else None
    if not control:
        raise ValueError(
            f"{voice['model_name']} has no speed control, so {voice['name']} speaks at its own pace. Send speed 1."
        )
    low, high = control[1], control[2]
    if not low <= speed <= high:
        raise ValueError(f"{voice['model_name']} speaks at {low:g} to {high:g} times its pace. Send a speed in that range.")
    return speed


def check_format(response_format, report=None):
    """Raise NeedsFFmpeg when the format is made by FFmpeg and this computer's FFmpeg cannot make it. Checked before the
    clip is made, so nobody waits for a clip that cannot be sent."""
    _type, encoder, _options = FORMATS[response_format]
    if encoder is None:
        return
    report = report or ffmpeg_tools.status()
    label = "Opus" if response_format == "opus" else response_format.upper()
    if not report.get("found"):
        raise NeedsFFmpeg(
            f"{label} is made with FFmpeg, which was not found on this computer. Press Install FFmpeg on System & backups, "
            "or ask for response_format wav, flac or pcm."
        )
    if not (report.get("encoders") or {}).get(encoder[0]):
        raise NeedsFFmpeg(
            f"This computer's FFmpeg cannot make {label}: its encoder ({encoder[1]}) is not included. Press Install FFmpeg "
            "on System & backups for one that can, or ask for response_format wav, flac or pcm."
        )


def encode(wav, response_format, engine_id=None):
    """The labelled WAV the Lab made, as (bytes, media type) in the format asked for."""
    media_type, encoder, options = FORMATS[response_format]
    if response_format == "wav":
        return wav, media_type
    samples, rate = sf.read(io.BytesIO(wav), dtype="float32")
    if response_format == "pcm":
        if rate != PCM_RATE:
            divisor = np.gcd(int(rate), PCM_RATE)
            samples = resample_poly(samples, PCM_RATE // divisor, int(rate) // divisor).astype(np.float32)
        return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes(), media_type
    if response_format == "flac":
        buffer = io.BytesIO()
        with sf.SoundFile(buffer, "w", int(rate), 1 if samples.ndim == 1 else samples.shape[1], format="FLAC",
                          subtype="PCM_16") as out:  # fmt: skip
            out.comment = synthetic_label.comment(engine_id)
            out.software = synthetic_label.SOFTWARE
            out.write(samples)
        return buffer.getvalue(), media_type
    ffmpeg = ffmpeg_tools.find_ffmpeg()
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-map", "0:a:0"]
    command += ["-c:a", encoder[1], *synthetic_label.ffmpeg_args(engine_id), *options, "pipe:1"]
    try:
        result = subprocess.run(command, input=wav, capture_output=True, timeout=ENCODE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("FFmpeg took too long to encode the clip.") from exc
    if result.returncode != 0 or not result.stdout:
        reason = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no reason given"]
        raise RuntimeError(f"FFmpeg could not encode the clip as {response_format}: {reason[0]}")
    return result.stdout, media_type
