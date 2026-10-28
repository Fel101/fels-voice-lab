"""Reference transcript suggestions: availability, busy handling, HTTP wiring, and the real worker."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import transcript_suggest
import re

from tests.test_export_panel import ExportPanelBase, tone_wav

REAL_REFERENCE = ROOT / "data" / "cast" / "media" / "references" / "c75c8dd9e9f14af6869e11bc94f8e13b" / "reference.wav"
REAL_TRANSCRIPT = "I kept that little note you left beside the door."
WORKER_READY = transcript_suggest.ASR_PYTHON.is_file() and (transcript_suggest.ASR_MODEL_DIR / "model.bin").is_file()


def _words(text):
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text.lower().replace("’", "'"))
    return re.findall(r"[a-z0-9']+", text)


def wer_words(reference, hypothesis):
    """Word error rate: the edit distance between the word lists over the number of reference words."""
    ref, hyp = _words(reference), _words(hypothesis)
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / max(1, len(ref))


class SuggestionUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-asr-")
        self.audio = Path(self.temp.name) / "clip.wav"
        self.audio.write_bytes(tone_wav())

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_isolated_environment_is_reported_not_guessed(self):
        with mock.patch.object(transcript_suggest, "ASR_PYTHON", Path(self.temp.name) / "missing" / "python.exe"):
            with self.assertRaises(transcript_suggest.TranscriptUnavailable):
                transcript_suggest.suggest(self.audio)

    def test_missing_model_copy_is_reported(self):
        with mock.patch.object(transcript_suggest, "ASR_MODEL_DIR", Path(self.temp.name) / "no-model"):
            if not transcript_suggest.ASR_PYTHON.is_file():
                self.skipTest("the isolated environment is not installed")
            with self.assertRaises(transcript_suggest.TranscriptUnavailable):
                transcript_suggest.suggest(self.audio)

    def test_second_suggestion_while_one_runs_is_busy(self):
        model = Path(self.temp.name) / "busy-model"
        model.mkdir()
        (model / "model.bin").write_bytes(b"fixture")
        self.assertTrue(transcript_suggest._suggest_lock.acquire(blocking=False))
        try:
            with mock.patch.object(transcript_suggest, "ASR_PYTHON", Path(sys.executable)), \
                 mock.patch.object(transcript_suggest, "ASR_MODEL_DIR", model), \
                 self.assertRaises(transcript_suggest.TranscriptBusy):
                transcript_suggest.suggest(self.audio)
        finally:
            transcript_suggest._suggest_lock.release()

    def test_worker_output_is_returned_as_a_draft_with_its_model_label(self):
        fake = mock.Mock(returncode=0, stderr="", stdout='{"text": "Hello there.", "language": "en", '
                                                       '"language_probability": 0.99, "duration_seconds": 1.2}\n')
        model_dir = Path(self.temp.name) / "model"
        model_dir.mkdir()
        (model_dir / "model.bin").write_bytes(b"placeholder")
        with mock.patch.object(transcript_suggest, "ASR_PYTHON", Path(sys.executable)), \
             mock.patch.object(transcript_suggest, "ASR_MODEL_DIR", model_dir), \
             mock.patch.object(transcript_suggest.subprocess, "run", return_value=fake) as run:
            result = transcript_suggest.suggest(self.audio)
        self.assertEqual(result["text"], "Hello there.")
        self.assertTrue(result["draft"])
        self.assertEqual(result["model"], transcript_suggest.MODEL_LABEL)
        command = run.call_args.args[0]
        self.assertIn("-I", command, "the worker must run in isolated mode")
        self.assertIn(str(self.audio), command)


class SuggestionHttpTests(ExportPanelBase):
    def test_route_returns_a_draft_and_maps_failures_to_clear_statuses(self):
        base = self.start_server()
        url = f"{base}/api/references/{self.reference['id']}/transcript-suggestion"
        draft = {"text": "Hello there.", "language": "en", "language_probability": 0.99,
                 "duration_seconds": 1.2, "model": transcript_suggest.MODEL_LABEL, "draft": True}

        def post(target):
            request = urllib.request.Request(target, data=b"{}", method="POST",
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))

        with mock.patch.object(transcript_suggest, "suggest", return_value=draft):
            status, body = post(url)
        self.assertEqual(status, 200)
        self.assertEqual(body["suggestion"]["text"], "Hello there.")
        self.assertTrue(body["suggestion"]["draft"])

        cases = [
            (transcript_suggest.TranscriptBusy("busy"), 409),
            (transcript_suggest.TranscriptUnavailable("not set up"), 503),
            (RuntimeError("worker exited"), 500),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(transcript_suggest, "suggest", side_effect=error):
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        post(url)
                self.assertEqual(failure.exception.code, expected)

        with self.assertRaises(urllib.error.HTTPError) as unknown:
            post(f"{base}/api/references/{'0' * 32}/transcript-suggestion")
        self.assertEqual(unknown.exception.code, 404)


@unittest.skipUnless(WORKER_READY, "the isolated environment and model copy are not set up")
@unittest.skipUnless(REAL_REFERENCE.is_file(), "the local Cast reference used for this check is not present")
class RealWorkerTests(unittest.TestCase):
    def test_isolated_worker_drafts_a_close_transcript_for_a_real_reference(self):
        result = transcript_suggest.suggest(REAL_REFERENCE)
        self.assertTrue(result["draft"])
        self.assertLessEqual(wer_words(REAL_TRANSCRIPT, result["text"]), 0.2)


if __name__ == "__main__":
    unittest.main()
