"""Read and validate a Voice Slate kept clip before voice-lab copies or compares anything.

Voice Slate writes <speaker>/<name>.wav (24 kHz, mono, 16-bit), <name>.json (the sidecar) and the untouched original
in <speaker>/masters/<name>.raw.wav; the format is described in Voice Slate's docs\\clip-format.md. Nothing here
writes: the sidecar and the WAV are each read once, the WAV is hashed, and what was checked is exactly what is later
copied, so the file cannot change between the check and the copy.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import soundfile as sf

SCHEMA_PREFIX = "voice-recorder-clip-"
MAX_SIDECAR_BYTES = 1 << 20
MAX_CLIP_BYTES = 32 * 1024 * 1024         # the cast store's own upload limit; the store stays the authority
EXPORT_RATE, EXPORT_CHANNELS, EXPORT_SUBTYPE = 24_000, 1, "PCM_16"
RECORDER_MIN_SECONDS, RECORDER_MAX_SECONDS = 6.0, 40.0
TRANSCRIPT_STATUSES = {"typed", "read_from_script", "asr_draft", "edited"}
CLIP_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class ClipRefused(ValueError):
    """The clip failed validation. ``problems`` lists every reason found, in plain words."""

    def __init__(self, problems):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class RecorderClip:
    """A kept clip that passed validation, with the exact bytes that were checked."""

    sidecar_path: Path
    sidecar_bytes: bytes
    sidecar_sha256: str
    data: dict
    wav_path: Path
    wav_bytes: bytes
    export_sha256: str
    master_path: Path
    duration_seconds: float
    warnings: tuple = field(default_factory=tuple)

    @property
    def id(self):
        return self.data["id"]

    @property
    def speaker(self):
        return str(self.data.get("speaker") or self.sidecar_path.parent.name)

    @property
    def schema(self):
        return self.data["schema"]

    @property
    def created(self):
        return str(self.data.get("created") or "")

    @property
    def source(self):
        return str(self.data.get("source") or "")

    @property
    def transcript(self):
        return self.data["transcript"].strip()

    @property
    def transcript_status(self):
        return str(self.data.get("transcript_status") or "")

    @property
    def origin(self):
        return self.data["origin"].strip()

    @property
    def allowed_use(self):
        return self.data["allowed_use"].strip()

    @property
    def flags(self):
        flags = self.data.get("flags")
        return [flag for flag in flags if isinstance(flag, str)] if isinstance(flags, list) else []

    @property
    def denoise(self):
        """What Voice Slate says about noise reduction, whichever schema wrote it.

        v2 keeps it in ``chain.denoise``; v1 had top-level ``denoise``. The ``cleaned`` flag counts too, so a clip is
        only reported as not denoised when nothing in the sidecar says otherwise.
        """
        chain = self.data.get("chain")
        block = chain.get("denoise") if isinstance(chain, dict) else None
        legacy = self.data.get("denoise")
        enabled, nf, checked = False, None, None
        if isinstance(block, dict):
            enabled, nf, checked = block.get("enabled") is True, block.get("nf"), block.get("identity_checked")
        elif legacy:
            enabled, nf = True, legacy.get("nf") if isinstance(legacy, dict) else None
        enabled = enabled or "cleaned" in self.flags
        return {"enabled": enabled, "nf": nf if isinstance(nf, (int, float)) and not isinstance(nf, bool) else None,
                "recorder_identity_checked": checked}

    def summary(self):
        denoise = self.denoise
        return {"id": self.id, "speaker": self.speaker, "file": self.wav_path.name, "schema": self.schema,
                "seconds": round(self.duration_seconds, 3), "created": self.created, "source": self.source,
                "transcript": self.transcript, "transcript_status": self.transcript_status,
                "origin": self.origin, "allowed_use": self.allowed_use, "flags": self.flags,
                "noise_reduction": denoise, "export_sha256": self.export_sha256,
                "sidecar_sha256": self.sidecar_sha256, "warnings": list(self.warnings)}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _inside(folder, name, what, *, bare=False):
    """The path a sidecar names, if it stays inside the sidecar's own folder. A sidecar can come from anywhere."""
    if not isinstance(name, str) or not name.strip():
        raise ClipRefused(f"{what} is missing")
    text = name.replace("\\", "/")
    posix = PurePosixPath(text)
    # ':' covers drive letters and NTFS alternate data streams; a leading slash covers rooted and UNC paths.
    if any(ord(ch) < 32 for ch in name) or ":" in name or posix.is_absolute():
        raise ClipRefused(f"{what} is not a plain relative path ({name!r})")
    if not posix.parts or any(part in (".", "..") for part in posix.parts):
        raise ClipRefused(f"{what} climbs out of the sidecar's folder ({name!r})")
    if bare and len(posix.parts) != 1:
        raise ClipRefused(f"{what} must be a file name in the sidecar's folder ({name!r})")
    root = folder.resolve()
    target = (root / Path(*posix.parts)).resolve()       # resolves links, so a link out of the folder is caught too
    if root not in target.parents:
        raise ClipRefused(f"{what} names a file outside the sidecar's folder ({name!r})")
    return target


def _read_sidecar(path):
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != ".json" or not path.is_file():
        raise ClipRefused(f"{path} is not a sidecar (.json) file")
    if path.stat().st_size > MAX_SIDECAR_BYTES:
        raise ClipRefused("the sidecar is larger than 1 MiB")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ClipRefused("the sidecar is not valid UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise ClipRefused("the sidecar is not a JSON object")
    return path, raw, data


def find_sidecars(folder):
    """The clip sidecars directly inside one speaker's folder: ``*.json`` files whose schema is Voice Slate's."""
    found = []
    for path in sorted(Path(folder).glob("*.json")):
        try:
            if path.stat().st_size > MAX_SIDECAR_BYTES:
                continue
            data = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            continue
        if isinstance(data, dict) and str(data.get("schema", "")).startswith(SCHEMA_PREFIX):
            found.append(path)
    return found


def load_clip(sidecar, *, accept_asr_draft=False):
    """Validate one kept clip from its sidecar and return it, or raise ClipRefused listing what is wrong.

    Checked: the schema starts with ``voice-recorder-clip-``; ``consent_confirmed`` is true; there is a transcript,
    origin and allowed use; ``export.file`` is a plain file name and ``master.file`` a relative path, both inside the
    sidecar's folder; the WAV's SHA-256 equals ``export.sha256``; and the WAV is in the format the sidecar declares.
    An ``asr_draft`` transcript (the speech recognizer's unreviewed guess) is refused unless ``accept_asr_draft``.
    """
    path, raw, data = _read_sidecar(sidecar)
    problems, warnings = [], []
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
        raise ClipRefused(f"the schema is {schema!r}, not a {SCHEMA_PREFIX}* sidecar")
    if data.get("consent_confirmed") is not True:
        problems.append("consent_confirmed is not true; Voice Slate only writes a sidecar for a clip the speaker "
                        "confirmed they may use")
    for key, label in (("transcript", "a transcript"), ("origin", "an origin"), ("allowed_use", "an allowed use")):
        if not isinstance(data.get(key), str) or not data[key].strip():
            problems.append(f"the sidecar has no {key}; voice-lab needs {label} to keep with the voice")
    clip_id = data.get("id")
    if not isinstance(clip_id, str) or not CLIP_ID_RE.fullmatch(clip_id):
        problems.append(f"the sidecar has no usable clip id ({clip_id!r})")
    status = data.get("transcript_status")
    if status == "asr_draft":
        if accept_asr_draft:
            warnings.append("the transcript is the speech recognizer's unreviewed draft (asr_draft); "
                            "it was accepted as the reference transcript on request")
        else:
            problems.append("the transcript is the speech recognizer's unreviewed draft (asr_draft); correct it in "
                            "Voice Slate and keep the clip again, or accept the draft explicitly")
    elif status not in TRANSCRIPT_STATUSES:
        warnings.append(f"transcript_status {status!r} is not one Voice Slate documents")

    export, master = data.get("export"), data.get("master")
    wav_path = master_path = None
    declared = {}
    if not isinstance(export, dict) or not isinstance(master, dict):
        problems.append("the sidecar has no export and master blocks")
    else:
        try:
            wav_path = _inside(path.parent, export.get("file"), "export.file", bare=True)
            if wav_path.suffix.lower() != ".wav":
                problems.append(f"export.file is not a .wav file ({export.get('file')!r})")
        except ClipRefused as exc:
            problems.extend(exc.problems)
        try:
            master_path = _inside(path.parent, master.get("file"), "master.file")
            if wav_path is not None and master_path == wav_path:
                problems.append("master.file is the kept clip itself")
        except ClipRefused as exc:
            problems.extend(exc.problems)
        declared = {key: export.get(key) for key in ("sample_rate", "channels", "bit_depth", "duration_seconds")}
        sha = export.get("sha256")
        if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha.lower()):
            problems.append("export.sha256 is missing or is not a SHA-256, so the clip cannot be checked against it")
    if problems:
        raise ClipRefused(problems)

    if not wav_path.is_file():
        raise ClipRefused(f"the kept clip {export['file']!r} is not next to its sidecar")
    if wav_path.stat().st_size > MAX_CLIP_BYTES:
        raise ClipRefused("the kept clip is larger than 32 MiB")
    wav_bytes = wav_path.read_bytes()
    actual = sha256_bytes(wav_bytes)
    if actual != export["sha256"].lower():
        raise ClipRefused(f"the WAV's SHA-256 ({actual[:12]}...) is not export.sha256 ({export['sha256'][:12]}...): "
                          "the file changed after Voice Slate kept it")
    try:
        info = sf.info(io.BytesIO(wav_bytes))
    except (RuntimeError, OSError) as exc:
        raise ClipRefused(f"the kept clip is not readable audio ({exc})") from None
    seconds = info.frames / info.samplerate if info.samplerate else 0.0
    bits = {"PCM_16": 16, "PCM_24": 24, "PCM_32": 32}.get(info.subtype)
    for label, actual_value, claimed in (("sample_rate", info.samplerate, declared["sample_rate"]),
                                         ("channels", info.channels, declared["channels"]),
                                         ("bit_depth", bits, declared["bit_depth"])):
        if claimed != actual_value:
            problems.append(f"the WAV has {label} {actual_value!r} but export.{label} says {claimed!r}")
    if info.format != "WAV":
        problems.append(f"the kept clip is {info.format}, not WAV")
    if problems:
        raise ClipRefused(problems)

    if (info.samplerate, info.channels, info.subtype) != (EXPORT_RATE, EXPORT_CHANNELS, EXPORT_SUBTYPE):
        warnings.append(f"the clip is {info.samplerate} Hz, {info.channels} channel(s), {info.subtype}; "
                        "Voice Slate normally writes 24000 Hz mono PCM_16")
    claimed_seconds = declared["duration_seconds"]
    if isinstance(claimed_seconds, (int, float)) and abs(claimed_seconds - seconds) > 0.05:
        warnings.append(f"the clip is {seconds:.2f} s but export.duration_seconds says {claimed_seconds}")
    if not RECORDER_MIN_SECONDS <= seconds <= RECORDER_MAX_SECONDS:
        warnings.append(f"the clip is {seconds:.1f} s; Voice Slate keeps clips of "
                        f"{RECORDER_MIN_SECONDS:g} to {RECORDER_MAX_SECONDS:g} s")
    if wav_path.stem != path.stem:
        warnings.append(f"the sidecar is {path.name} but its clip is {wav_path.name}; a clip's files share a name")
    if isinstance(data.get("speaker"), str) and data["speaker"] != path.parent.name:
        warnings.append(f"the sidecar says speaker {data['speaker']!r} but it sits in the folder {path.parent.name!r}")
    flags = data.get("flags") if isinstance(data.get("flags"), list) else []
    for flag, text in (("clipped", "the recording clipped"),
                       ("interrupted", "the recording device stopped while recording")):
        if flag in flags:
            warnings.append(f"{text} (flag {flag!r})")
    if data.get("source") not in (None, "mic"):
        warnings.append(f"the clip came from {data.get('source')!r}, not a microphone; its origin is the person's "
                        "own statement")
    return RecorderClip(sidecar_path=path, sidecar_bytes=raw, sidecar_sha256=sha256_bytes(raw), data=data,
                        wav_path=wav_path, wav_bytes=wav_bytes, export_sha256=actual, master_path=master_path,
                        duration_seconds=seconds, warnings=tuple(warnings))
