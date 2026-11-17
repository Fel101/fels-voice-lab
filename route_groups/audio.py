"""Local sound effects and WAV mastering."""

import re

import audio_tools


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
