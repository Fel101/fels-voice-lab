"""Local sound effects and WAV mastering."""

import json
import re

from voice_lab import audio_tools
from voice_lab import dubbing
from voice_lab.portable_projects import _id


def _artifact(root, ident, name):
    if not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{32}", ident):
        raise KeyError("Audio delivery not found.")
    path = root / "out" / "audio-tools" / ident / name
    if not path.is_file():
        raise KeyError("Audio delivery not found.")
    return path


def register(router, lab):
    @router.post("/api/audio-tools/sound")
    def sound(self, parsed, req):
        self._json(201, {"delivery": audio_tools.sound_effect(lab.scenes.root, req)})

    @router.post("/api/audio-tools/master")
    def master(self, parsed, req):
        raw = self._read_body(audio_tools.MAX_UPLOAD)
        self._json(201, {"delivery": audio_tools.master(lab.scenes.root, raw, self._reference_metadata())})

    @router.get("/api/audio-tools/{delivery_id}/audio.wav")
    def audio(self, parsed, req, delivery_id):
        self._send_path(_artifact(lab.scenes.root, delivery_id, "audio.wav"), "audio/wav")

    @router.get("/api/audio-tools/{delivery_id}/receipt.json")
    def receipt(self, parsed, req, delivery_id):
        self._send_path(_artifact(lab.scenes.root, delivery_id, "receipt.json"), "application/json")

    @router.post("/api/audio-tools/{delivery_id}/dub-background")
    def use_background(self, parsed, req, delivery_id):
        path = _artifact(lab.scenes.root, delivery_id, "audio.wav")
        info = json.loads(path.with_name("receipt.json").read_text(encoding="utf-8"))
        if info.get("method") != "procedural-sfx-v1":
            raise ValueError("Choose a generated sound effect for the dub background.")
        key = _id(req.get("scene_id"))
        with path.open("rb") as source:
            result = dubbing.attach(
                lab.scenes,
                key,
                source,
                path.stat().st_size,
                info["preset"] + ".wav",
                background=True,
                expected_source_sha=info["output_sha256"],
            )
        self._json(201, {"background": result})
