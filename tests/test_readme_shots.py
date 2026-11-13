"""tools/readme_shots.py: what it refuses before it starts anything, and that its cast and scene use no person's recording.

The pictures themselves need Kokoro, a browser and FFmpeg, so they are made by hand (the tool's docstring says how); these
tests run without any of them."""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import script_import  # noqa: E402
from tools import readme_shots  # noqa: E402


class StandInService:
    """Answers the cast's calls as the service would, and keeps them."""

    def __init__(self):
        self.calls = []

    def call(self, path, body=None, *, timeout=60, raw=False):
        self.calls.append((path, body))
        if path == "/api/characters":
            return {"character": {"id": "c-" + body["name"]}}
        if path.endswith("/identities"):
            return {"identity": {"id": "v-" + body["label"]}}
        return {"identity": {}}


class ReadmeShotsTests(unittest.TestCase):
    def run_main(self):
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            status = readme_shots.main(["--out", str(ROOT / "out" / "never-written")])
        return status, error.getvalue()

    def test_no_browser_stops_before_anything_starts(self):
        with mock.patch.object(readme_shots.flow, "BROWSER", None), mock.patch.object(readme_shots, "Service") as service:
            status, error = self.run_main()
        self.assertEqual(status, 2)
        self.assertIn("No Microsoft Edge or Google Chrome", error)
        service.assert_not_called()

    def test_no_ffmpeg_stops_before_anything_starts(self):
        with (
            mock.patch.object(readme_shots.flow, "BROWSER", "edge.exe"),
            mock.patch.object(readme_shots.ffmpeg_tools, "find_ffmpeg", return_value=None),
            mock.patch.object(readme_shots, "Service") as service,
        ):
            status, error = self.run_main()
        self.assertEqual(status, 2)
        self.assertIn("FFmpeg was not found", error)
        service.assert_not_called()

    def test_the_cast_is_kokoro_preset_voices_and_uploads_no_recording(self):
        service = StandInService()
        voices = readme_shots.cast(service)
        self.assertEqual(voices, {name: f"v-{label}" for name, label, _ in readme_shots.CHARACTERS})
        identities = [body for path, body in service.calls if path.endswith("/identities")]
        self.assertEqual([body["engine_id"] for body in identities], ["kokoro"] * len(readme_shots.CHARACTERS))
        self.assertEqual([body["voice"] for body in identities], [voice for _, _, voice in readme_shots.CHARACTERS])
        self.assertFalse([path for path, _ in service.calls if "/references" in path])
        approvals = [body for path, body in service.calls if path.endswith("/update")]
        self.assertEqual(approvals, [{"status": "approved"}] * len(readme_shots.CHARACTERS))

    def test_every_speaker_in_the_scene_has_a_voice(self):
        read = script_import.parse_script(readme_shots.SCRIPT, "text")
        self.assertEqual(
            sorted(speaker["name"] for speaker in read["speakers"]),
            sorted(name.upper() for name, _, _ in readme_shots.CHARACTERS),
        )
        self.assertEqual((len(read["lines"]), read["unread"], read["narration"]), (4, [], 0))


if __name__ == "__main__":
    unittest.main()
