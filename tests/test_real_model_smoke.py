"""The real-model smoke tool (tools/real_model_smoke.py): which models it tries, how it judges a clip, and a whole run against a
stand-in service that answers as the Lab's does. No model is loaded here; the tool itself is what loads them."""

import io
import json
import re
import sys
import tempfile
import threading
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import synthetic_label  # noqa: E402
from tools import real_model_smoke as smoke  # noqa: E402

RATE = 24_000


def voice(seconds=6.0, amplitude=0.3, rate=RATE):
    """A stand-in for speech: a tone whose loudness moves like syllables, never silent and never at full scale."""
    time = np.arange(round(seconds * rate)) / rate
    return (amplitude * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * time)) * np.sin(2 * np.pi * 220 * time)).astype(np.float32)


def labelled(audio, engine="kokoro", rate=RATE):
    return synthetic_label.wav_bytes(audio, rate, engine_id=engine)


STATUS = {
    "engines": [
        {"id": "kokoro", "state": "passed"},
        {"id": "phonon-31m-en", "state": "not_installed"},
        {"id": "turbo", "state": "not_checked"},
        {"id": "chatterbox-vc", "state": "conversion_task"},
        {"id": "drama-3", "state": "not_integrated"},
    ]
}


class ChoosingTests(unittest.TestCase):
    def test_all_installed_tries_every_installed_model_that_speaks(self):
        self.assertEqual(smoke.choose(STATUS, [], all_installed=True), ["kokoro", "turbo"])

    def test_named_models_are_tried_as_named_even_when_not_installed(self):
        self.assertEqual(smoke.choose(STATUS, ["phonon-31m-en", "kokoro"], all_installed=False), ["phonon-31m-en", "kokoro"])

    def test_an_unknown_model_or_one_that_only_converts_is_a_mistake(self):
        with self.assertRaisesRegex(smoke.Usage, "Unknown model: kokro. The models are: kokoro, "):
            smoke.choose(STATUS, ["kokro"], all_installed=False)
        with self.assertRaisesRegex(smoke.Usage, "chatterbox-vc converts recordings and does not speak text"):
            smoke.choose(STATUS, ["chatterbox-vc"], all_installed=False)

    def test_the_command_line_needs_models_or_all_installed_but_not_both(self):
        for argv in ([], ["kokoro", "--all-installed"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
                smoke.main(argv)
            self.assertEqual(raised.exception.code, 2)


class WorkflowTests(unittest.TestCase):
    """.github/workflows/real-model.yml first runs on GitHub when the project is published, so its commands are read here."""

    def test_each_tool_the_workflow_runs_takes_the_options_it_is_given(self):
        from tools import install_kokoro

        workflow = (ROOT / ".github" / "workflows" / "real-model.yml").read_text(encoding="utf-8")
        commands = re.findall(r"run: \.venv\\Scripts\\python\.exe tools\\(\w+)\.py (.*)", workflow)
        self.assertEqual([name for name, _ in commands], ["install_kokoro", "real_model_smoke"])
        parsers = {"install_kokoro": install_kokoro.build_parser(), "real_model_smoke": smoke.build_parser()}
        for name, arguments in commands:
            with self.subTest(name):
                parsed = parsers[name].parse_args(arguments.split())
                self.assertTrue(parsed.yes if name == "install_kokoro" else parsed.models == ["kokoro"])
        self.assertIn("--voices af_heart", workflow, "the voice the smoke request asks for")
        self.assertNotIn("--gpu", workflow, "the runner has no graphics card")
        self.assertIn("path: out/smoke/", workflow)


class JudgingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-smoke-")
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def judge(self, data, engine="kokoro", sample_rate=RATE):
        path = self.folder / "clip.wav"
        path.write_bytes(data)
        return smoke.judge(path, engine=engine, sample_rate=sample_rate)

    def test_a_labelled_clip_of_the_right_length_and_rate_passes(self):
        verdict = self.judge(labelled(voice()))
        self.assertEqual(verdict["problems"], [])
        self.assertEqual((verdict["seconds"], verdict["sample_rate"]), (6.0, RATE))
        self.assertTrue(synthetic_label.is_labelled(verdict["label"]))
        self.assertLess(verdict["peak_dbfs"], 0)

    def test_the_sentence_has_a_sensible_length(self):
        low, high = smoke.expected_seconds()
        self.assertEqual(len(smoke.SENTENCE.split()), 17)
        self.assertAlmostEqual(low, 17 / 6)
        self.assertAlmostEqual(high, 17 / 1.2 + 3)

    def test_each_fault_is_named(self):
        quiet_gap = np.concatenate([voice(2.0), np.zeros(2 * RATE, np.float32), voice(2.0)])
        cases = {
            "it is at 24000 Hz, not the model's 48000 Hz": (labelled(voice()), "kokoro", 48000),
            "the model did not say its sample rate": (labelled(voice()), "kokoro", None),
            "it lasts 1.00 s, outside 2.8 to 17.2 s for this sentence": (labelled(voice(1.0)), "kokoro", RATE),
            "it is too short for its words": (labelled(voice(1.0)), "kokoro", RATE),
            "it lasts 30.00 s": (labelled(voice(30.0)), "kokoro", RATE),
            "generated silence": (labelled(np.zeros(6 * RATE, np.float32)), "kokoro", RATE),
            "it is silent": (labelled(np.zeros(6 * RATE, np.float32)), "kokoro", RATE),
            "it is clipped (samples at full scale)": (labelled(np.sign(voice()).astype(np.float32)), "kokoro", RATE),
            "the file carries no AI-generated speech label": (self.plain(voice()), "kokoro", RATE),
            "its label does not name Kokoro 82M": (labelled(voice(), engine="turbo"), "kokoro", RATE),
            "the answer is not audio the Lab can read": (b'{"error": "not a WAV"}', "kokoro", RATE),
        }
        for words, (data, engine, rate) in cases.items():
            with self.subTest(words):
                problems = self.judge(data, engine, rate)["problems"]
                self.assertTrue(any(problem.startswith(words) for problem in problems), problems)
        verdict = self.judge(labelled(quiet_gap))
        self.assertEqual(verdict["problems"], [], "a pause is noted, never failing")
        self.assertEqual(verdict["notes"], ["a pause of 2.0 s inside it"])

    @staticmethod
    def plain(audio):
        buffer = io.BytesIO()
        sf.write(buffer, audio, RATE, format="WAV", subtype="PCM_16")
        return buffer.getvalue()


class StandIn(BaseHTTPRequestHandler):
    """Answers as the Lab's service does for the calls the tool makes. Kokoro speaks; Turbo's Test fails and it says nothing."""

    calls = []

    def log_message(self, *args):
        pass

    def answer(self, status, body, kind="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.calls.append(("GET", self.path, None))
        if self.path == "/api/health":
            return self.answer(200, {"status": "ready", "version": "9.9.9", "device": "cpu"})
        if self.path == "/api/engines/status":
            return self.answer(200, STATUS)
        return self.answer(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.calls.append(("POST", self.path, body))
        if self.path == "/api/engines/kokoro/check":
            return self.answer(200, {"check": {"status": "passed", "ran_on": "cpu", "load_seconds": 2.25}})
        if self.path == "/api/engines/turbo/check":
            return self.answer(200, {"check": {"status": "failed", "ran_on": "cpu", "reason": "generated silence"}})
        if self.path == "/api/load":
            return self.answer(200, {"engine": body["engine"], "sample_rate": RATE, "load_seconds": 2.25})
        if self.path == "/api/speak" and body["engine"] == "kokoro":
            return self.answer(200, labelled(voice()), "audio/wav", [("X-Gen-Seconds", "1.5")])
        if self.path == "/api/speak":
            return self.answer(500, {"error": "Worker exited during generation."})
        if self.path == "/api/unload":
            return self.answer(200, {"ok": True})
        return self.answer(404, {"error": "not found"})


class RunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-lab-smoke-run-")
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        StandIn.calls = []
        self.stopped = []
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        test = self

        class FakeService:
            def __init__(self, folder, *, device=None):
                self.folder, self.device, self.base = folder, device, base

            def start(self):
                return smoke.lab_cli.get_json(self.base, "/api/health", 5)

            def stop(self):
                test.stopped.append(self.device)

        self.service = FakeService

    def run_smoke(self, models=(), all_installed=False):
        said = []
        code = smoke.run(Namespace(models=list(models), all_installed=all_installed, device="cpu"), out=self.out,
                         service_factory=self.service, say=said.append, now=lambda: datetime(2026, 11, 12, 9, 30))  # fmt: skip
        return code, "\n".join(said), self.out / "2026-11-12-093000"

    def test_a_model_that_speaks_passes_and_its_clip_is_kept(self):
        code, said, folder = self.run_smoke(["kokoro"])
        self.assertEqual(code, 0, said)
        speak = [body for method, path, body in StandIn.calls if path == "/api/speak"]
        self.assertEqual(speak, [{"engine": "kokoro", "text": smoke.SENTENCE, "voice": "af_heart"}], "the Test's own request")
        self.assertEqual(sf.info(folder / "kokoro.wav").samplerate, RATE)
        report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
        self.assertEqual((report["version"], report["sentence"]), ("9.9.9", smoke.SENTENCE))
        row = report["models"][0]
        self.assertEqual((row["test"], row["load_seconds"], row["speak_seconds"], row["seconds"]), ("passed", 2.25, 1.5, 6.0))
        self.assertIn("Kokoro 82M  passed  cpu", said)
        self.assertIn("Listen to them", said)
        self.assertEqual(self.stopped, ["cpu"])
        self.assertIn(("POST", "/api/unload", None), StandIn.calls, "each model is unloaded before the next")

    def test_all_installed_tries_each_and_fails_for_the_one_that_does_not_speak(self):
        code, said, folder = self.run_smoke(all_installed=True)
        self.assertEqual(code, 1)
        checked = [path for method, path, body in StandIn.calls if path.endswith("/check")]
        self.assertEqual(checked, ["/api/engines/kokoro/check", "/api/engines/turbo/check"])
        self.assertIn("Chatterbox Turbo 350M: its Test failed: generated silence.", said)
        self.assertIn(
            "Chatterbox Turbo 350M: it did not speak: the service answered 500: Worker exited during generation.\n", said
        )
        self.assertEqual([row["engine"] for row in json.loads((folder / "report.json").read_text("utf-8"))["models"]],
                         ["kokoro", "turbo"])  # fmt: skip
        self.assertEqual(self.stopped, ["cpu"], "the service is stopped even when a model fails")

    def test_a_named_model_that_is_not_installed_fails_with_what_is_missing(self):
        def not_installed(handler):
            return handler.answer(200, {"check": {"status": "not_installed", "missing": [".venv-phonon"]}})

        original = StandIn.do_POST

        def do_post(handler):
            if handler.path == "/api/engines/phonon-31m-en/check":
                handler.rfile.read(int(handler.headers.get("Content-Length") or 0))
                return not_installed(handler)
            return original(handler)

        StandIn.do_POST = do_post
        self.addCleanup(setattr, StandIn, "do_POST", original)
        code, said, _folder = self.run_smoke(["phonon-31m-en"])
        self.assertEqual(code, 1)
        self.assertIn("it is not installed: .venv-phonon not found.", said)

    def test_no_model_installed_is_said_plainly(self):
        empty = {"engines": [{"id": "kokoro", "state": "not_installed"}]}
        original = smoke.lab_cli.get_json

        def get_json(base, path, timeout):
            return empty if path == "/api/engines/status" else original(base, path, timeout)

        smoke.lab_cli.get_json = get_json
        self.addCleanup(setattr, smoke.lab_cli, "get_json", original)
        with self.assertRaisesRegex(smoke.Usage, "No model is installed"):
            self.run_smoke(all_installed=True)
        self.assertEqual(self.stopped, ["cpu"])


if __name__ == "__main__":
    unittest.main()
