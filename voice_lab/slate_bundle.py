"""Read the exact kept WAV and sidecar in a Voice Slate handoff file."""

import io
import json
import re
import tempfile
import zipfile
from pathlib import Path

from voice_lab.recorder_clip import MAX_CLIP_BYTES, MAX_SIDECAR_BYTES, ClipRefused, _inside, load_clip

MAX_BUNDLE_BYTES = MAX_CLIP_BYTES + MAX_SIDECAR_BYTES + 4096
_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in "123456789¹²³"), *(f"LPT{i}" for i in "123456789¹²³")}


def read_bundle(raw, *, accept_asr_draft=False):
    limits = {"clip.wav": MAX_CLIP_BYTES, "clip.json": MAX_SIDECAR_BYTES}
    if not raw or len(raw) > MAX_BUNDLE_BYTES:
        raise ClipRefused("The Voice Slate handoff is empty or exceeds the upload limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
            members = bundle.infolist()
            if len(members) != len(limits) or {entry.filename for entry in members} != limits.keys():
                raise ClipRefused("A Voice Slate handoff needs exactly one kept WAV and its sidecar.")
            files = {}
            for entry in members:
                limit = limits[entry.filename]
                if entry.compress_type != zipfile.ZIP_STORED or entry.file_size > limit or entry.flag_bits & 1:
                    raise ClipRefused("The Voice Slate handoff contains a compressed, encrypted or oversized member.")
                with bundle.open(entry) as source:
                    files[entry.filename] = source.read(limit + 1)
                if len(files[entry.filename]) > limit:
                    raise ClipRefused("The Voice Slate handoff contains an oversized member.")
        try:
            sidecar = json.loads(files["clip.json"].decode("utf-8-sig"))
        except (ValueError, RecursionError) as exc:
            # Not UTF-8, not JSON, or a number with more digits than Python reads (a plain ValueError). Caught here, not
            # below, where ValueError would also catch load_clip's ClipRefused and hide its reasons.
            raise ClipRefused("Could not read the .voiceslate file or its sidecar.") from exc
        if not isinstance(sidecar, dict) or not isinstance(sidecar.get("export"), dict):
            raise ClipRefused("The Voice Slate sidecar has no export information.")
        if sidecar.get("schema") not in ("voice-recorder-clip-v1", "voice-recorder-clip-v2"):
            raise ClipRefused("This Voice Slate sidecar version is not supported.")
        name = sidecar["export"].get("file")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r'[^<>:"/\\|?*\x00-\x1f]{1,176}\.wav', name, re.I)
            or name.split(".", 1)[0].rstrip().upper() in _DEVICES
        ):
            raise ClipRefused("The sidecar must name a portable kept WAV file.")
        # Use fixed archive member names. Only the existing bridge's confined path check can name
        # a temporary WAV. There is no archive extraction or write to a user's source folder.
        with tempfile.TemporaryDirectory(prefix="voice-lab-slate-") as temp:
            speaker = sidecar.get("speaker")
            folder = Path(temp)
            if isinstance(speaker, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", speaker):
                folder = folder / speaker
            folder.mkdir(exist_ok=True)
            kept = _inside(folder, name, "export.file", bare=True)
            path = kept.with_suffix(".json")
            kept.write_bytes(files["clip.wav"])
            path.write_bytes(files["clip.json"])
            return load_clip(path, accept_asr_draft=accept_asr_draft)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, UnicodeError) as exc:
        raise ClipRefused("Could not read the .voiceslate file or its sidecar.") from exc
