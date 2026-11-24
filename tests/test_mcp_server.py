"""The MCP server (tools/mcp_server.py): a whole conversation over its pipes against a stand-in service, and in this
process the parts a conversation cannot show quickly: stopping, cancelling, one clip at a time, progress, and starting
the Lab's service when none runs."""

import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))

import api_service  # noqa: E402
from voice_lab import api_spec  # noqa: E402
from voice_lab import housekeeping  # noqa: E402
from voice_lab import kokoro_support  # noqa: E402
from voice_lab import lab_address  # noqa: E402
import mcp_server  # noqa: E402
from voice_lab import phonon_support  # noqa: E402
from app_version import APP_NAME, VERSION  # noqa: E402
from voice_lab.worker_process import WorkerCancelled  # noqa: E402

SERVER = ROOT / "tools" / "mcp_server.py"


class StandInEngines(api_service.FakeEngines):
    """The stand-in engines, with the queue's cancel: a cancelled request that is being made answers 499."""

    def __init__(self):
        super().__init__()
        self.cancelled = []

    def cancel_request(self, request_id):
        self.cancelled.append(request_id)
        if self.gate is not None:
            self.gate.set()
        return "running"

    def speak(self, request):
        result = super().speak(request)
        if self.cancelled:
            raise WorkerCancelled("Stopped.")
        return result


class LabCase(unittest.TestCase):
    def setUp(self):
        self.engines = StandInEngines()
        self.service = api_service.Service(self, self.engines)
        self.engines.install_probe = lambda entry: {"installed": entry["id"] == "kokoro"}
        self.enterContext(mock.patch.object(kokoro_support, "discover_kokoro_voices", lambda: ["af_heart", "ef_dora"]))
        self.enterContext(mock.patch.object(phonon_support, "discover_phonon_voices", lambda: []))
        self.clips = Path(self.enterContext(tempfile.TemporaryDirectory()))


# ------------------------------------------------------------------------------------------- over the server's pipes


class ConversationTests(LabCase):
    def start_server(self):
        environ = {**os.environ, "VOICE_LAB_ADDRESS_FILE": str(self.clips / "service.json")}
        process = subprocess.Popen(
            [sys.executable, str(SERVER), "--url", self.service.base, "--clips", str(self.clips)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environ,
        )
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.stdout.close)
        self.addCleanup(lambda: process.stdin.closed or process.stdin.close())
        self.addCleanup(lambda: process.poll() is None and process.kill())
        lines = queue.Queue()
        self.raw = []

        def read():
            for line in iter(process.stdout.readline, b""):
                self.raw.append(line)
                lines.put(line)

        threading.Thread(target=read, daemon=True).start()
        return process, lines

    def send(self, process, message):
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    def ask(self, process, lines, message):
        self.send(process, message)
        answer = json.loads(lines.get(timeout=30))
        self.assertEqual((answer["jsonrpc"], answer.get("id")), ("2.0", message.get("id")))
        return answer

    def test_a_whole_conversation_starts_lists_the_tools_speaks_and_leaves_the_clip(self):
        process, lines = self.start_server()
        hello = self.ask(
            process,
            lines,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            },
        )
        self.assertEqual(hello["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(hello["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertEqual(hello["result"]["serverInfo"], {"name": "voice-lab", "title": APP_NAME, "version": VERSION})
        self.send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = self.ask(process, lines, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            [
                "lab_status",
                "list_voices",
                "speak",
                "stop_speaking",
                "set_voice",
                "describe_voice",
                "transcribe",
                "preview_voice_candidate",
                "import_voice_candidate",
            ],
        )
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertGreater(len(tool["description"]), 60, "a description written for the model that calls it")
        self.assertIn(
            "ten seconds to more than a minute", next(tool for tool in tools if tool["name"] == "speak")["description"]
        )

        voices = self.ask(
            process,
            lines,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_voices", "arguments": {}}},
        )["result"]
        self.assertFalse(voices["isError"])
        self.assertEqual([voice["name"] for voice in voices["structuredContent"]["voices"]], ["af_heart", "ef_dora"])
        self.assertIn("af_heart - Heart · American English (Kokoro 82M; American English)", voices["content"][0]["text"])

        spoken = self.ask(
            process,
            lines,
            {
                "jsonrpc": "2.0",
                "id": "speak-1",
                "method": "tools/call",
                "params": {"name": "speak", "arguments": {"text": "Hello from the Lab.", "voice": "af_heart", "play": False}},
            },
        )["result"]
        self.assertFalse(spoken["isError"], spoken)
        clip = Path(spoken["structuredContent"]["file"])
        self.assertTrue(clip.is_file())
        self.assertEqual(clip.parent.parent, self.clips)
        self.assertRegex(clip.parent.name, housekeeping.SPOKEN_NAME.pattern)
        self.assertEqual(clip.name, "001-hello-from-the-lab.wav")
        with wave.open(str(clip), "rb") as audio:
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 0.1, places=2)
        self.assertEqual(spoken["content"][0]["text"], f"Made 0.1 s as af_heart. The clip is {clip}.")
        self.assertEqual(self.engines.requests[-1]["voice"], "af_heart")
        self.assertEqual(self.engines.requests[-1]["engine"], "kokoro")

        process.stdin.close()
        self.assertEqual(process.wait(timeout=15), 0, "closing standard input ends the server")
        for line in self.raw:
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0", "standard output carries protocol messages only")
        self.assertIn(b"test connected (protocol 2025-11-25)", process.stderr.read())

    def test_versions_errors_and_refusals_answer_as_the_protocol_says(self):
        process, lines = self.start_server()
        older = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        self.assertEqual(self.ask(process, lines, older)["result"]["protocolVersion"], "2025-06-18")
        unknown = {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
        self.assertEqual(self.ask(process, lines, unknown)["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(self.ask(process, lines, {"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"], {})
        self.assertEqual(
            self.ask(process, lines, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})["error"]["code"], -32601
        )
        process.stdin.write(b"this is not json\n")
        process.stdin.flush()
        broken = json.loads(lines.get(timeout=30))
        self.assertEqual((broken["id"], broken["error"]["code"]), (None, -32700))
        call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "delete_everything", "arguments": {}}}
        self.assertEqual(
            self.ask(process, lines, call)["error"], {"code": -32602, "message": "Unknown tool: delete_everything"}
        )

        nobody = {"name": "speak", "arguments": {"text": "Hello.", "voice": "Nobody", "play": False}}
        result = self.ask(process, lines, {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": nobody})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["content"][0]["text"], "There is no voice called 'Nobody'. list_voices lists the voices that can speak."
        )
        wrong = {"name": "speak", "arguments": {"text": "Hello.", "loud": True}}
        result = self.ask(process, lines, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": wrong})["result"]
        self.assertEqual(
            (result["isError"], result["content"][0]["text"]),
            (True, "speak takes no argument called 'loud'. It takes: text, voice, direction, language, play, file, format."),
        )
        kept = self.clips / "kept" / "hello.wav"
        to_file = {"name": "speak", "arguments": {"text": "Hello.", "play": False, "file": str(kept)}}
        result = self.ask(process, lines, {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": to_file})["result"]
        self.assertEqual(result["structuredContent"]["file"], str(kept))
        self.assertTrue(result["structuredContent"]["kept"])
        self.assertIn(f"Saved to {kept}.", result["content"][0]["text"])
        result = self.ask(process, lines, {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": to_file})["result"]
        self.assertEqual(
            (result["isError"], result["content"][0]["text"]),
            (True, f"{kept} already exists, and speak never replaces a file. Name another file."),
        )
        process.stdin.close()
        self.assertEqual(process.wait(timeout=15), 0)


# ------------------------------------------------------------------------------------------------------ in process


class RecordingPlayer:
    """Plays nothing: it keeps when each clip started and ended, and holds each for `seconds` unless it is stopped."""

    def __init__(self, seconds=0.0):
        self.seconds = seconds
        self.played = []
        self.started = threading.Event()

    def play(self, path, seconds, stop):
        begun = time.monotonic()
        self.started.set()
        stopped = stop.wait(self.seconds)
        self.played.append((Path(path).name, round(seconds, 2), begun, time.monotonic(), not stopped))
        return not stopped


class Wire:
    """The server's standard output, as messages, in order."""

    def __init__(self):
        self.messages = []
        self.arrived = threading.Condition()

    def write(self, data):
        with self.arrived:
            self.messages.append(json.loads(data.decode("utf-8")))
            self.arrived.notify_all()

    def flush(self):
        pass

    def answer(self, rpc_id, timeout=15):
        deadline = time.monotonic() + timeout
        with self.arrived:
            while True:
                for message in self.messages:
                    if message.get("id") == rpc_id and ("result" in message or "error" in message):
                        return message
                left = deadline - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no answer to {rpc_id!r}: {self.messages}")
                self.arrived.wait(left)


class InProcessCase(LabCase):
    def setUp(self):
        super().setUp()
        self.player = RecordingPlayer()
        self.lab = mcp_server.Lab(url=self.service.base, environ={"VOICE_LAB_ADDRESS_FILE": str(self.clips / "none.json")})
        self.tools = mcp_server.Tools(self.lab, self.player, clips=self.clips)
        self.wire = Wire()
        self.server = mcp_server.Server(self.tools, self.wire)

    def call(self, rpc_id, name, arguments=None, meta=None):
        params = {"name": name, "arguments": arguments or {}}
        if meta:
            params["_meta"] = meta
        self.server.handle(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": params}).encode())

    def result(self, rpc_id):
        return self.wire.answer(rpc_id)["result"]


class SpeakingTests(InProcessCase):
    def test_a_clip_is_played_aloud_by_default_and_the_answer_says_how_long_it_was(self):
        self.call(1, "speak", {"text": "Hello there.", "voice": "af_heart"})
        result = self.result(1)
        self.assertEqual([item[:2] for item in self.player.played], [("001-hello-there.wav", 0.1)])
        self.assertTrue(result["structuredContent"]["played"])
        self.assertEqual(
            result["content"][0]["text"], f"Spoke 0.1 s as af_heart. The clip is {result['structuredContent']['file']}."
        )
        self.assertNotIn("wait_seconds", self.engines.requests[-1], "the service takes it; it never reaches the model")

    def test_two_calls_never_talk_over_each_other_and_keep_their_order(self):
        self.player.seconds = 0.3
        self.call(1, "speak", {"text": "First."})
        self.assertTrue(self.player.started.wait(10))
        self.call(2, "speak", {"text": "Second."})
        self.result(1), self.result(2)
        (first, _, _, first_end, _), (second, _, second_start, _, _) = self.player.played
        self.assertEqual((first, second), ("001-first.wav", "002-second.wav"))
        self.assertGreaterEqual(second_start, first_end)

    def test_stop_speaking_stops_the_clip_being_made_and_the_lab_hears_the_cancel(self):
        self.engines.gate = threading.Event()
        self.call(1, "speak", {"text": "A long story."})
        self.assertTrue(self.engines.started.wait(10))
        self.call(2, "stop_speaking")
        self.assertEqual(self.result(2)["content"][0]["text"], "Stopped.")
        self.assertEqual(self.result(1), {"content": [{"type": "text", "text": "Stopped before it was spoken."}],
                                          "isError": False, "structuredContent": {"stopped": True}})  # fmt: skip
        self.assertEqual(len(self.engines.cancelled), 1)
        self.assertRegex(self.engines.cancelled[0], r"^mcp-[0-9a-f]{32}$")
        self.assertEqual(self.player.played, [], "a stopped clip is never played")
        self.call(3, "stop_speaking")
        self.assertEqual(self.result(3)["content"][0]["text"], "Nothing is being spoken.")

    def test_stop_speaking_stops_a_clip_that_is_playing(self):
        self.player.seconds = 10
        self.call(1, "speak", {"text": "Listen."})
        self.assertTrue(self.player.started.wait(10))
        self.call(2, "stop_speaking")
        result = self.result(1)
        self.assertFalse(result["structuredContent"]["played"])
        self.assertIn("Playback was stopped.", result["content"][0]["text"])
        self.assertLess(self.player.played[0][3] - self.player.played[0][2], 5)

    def test_a_call_the_client_cancels_gets_no_answer_and_its_clip_is_cancelled(self):
        self.engines.gate = threading.Event()
        self.call("c1", "speak", {"text": "Never mind."})
        self.assertTrue(self.engines.started.wait(10))
        cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "c1", "reason": "the user"}}
        self.server.handle(json.dumps(cancel).encode())
        deadline = time.monotonic() + 10
        while not self.engines.cancelled and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.engines.cancelled), 1)
        self.call(2, "lab_status")
        self.result(2)
        time.sleep(0.3)
        self.assertFalse([message for message in self.wire.messages if message.get("id") == "c1"])

    def test_a_direction_goes_only_to_a_voice_that_takes_one(self):
        self.call(1, "speak", {"text": "Hello.", "voice": "af_heart", "direction": "softly", "play": False})
        self.assertIn("af_heart takes no acting direction, so it was spoken without one.", self.result(1)["content"][0]["text"])
        self.assertNotIn("direction", self.engines.requests[-1])
        self.call(2, "speak", {"text": "Hello.", "direction": "softly", "play": False})
        self.assertIn("The Lab's default voice takes no acting direction", self.result(2)["content"][0]["text"])

    def test_how_a_call_stands_before_the_lab_is_found_asks_nothing_of_the_lab(self):
        # The progress thread once asked the Lab's queue on every tick, so a tick before the Lab's address was known
        # stopped it with a TypeError and the client heard nothing more.
        self.enterContext(mock.patch.object(mcp_server, "PROGRESS_SECONDS", 0.01))
        lab = mcp_server.Lab(url=self.service.base, environ={"VOICE_LAB_ADDRESS_FILE": str(self.clips / "none.json")})
        with self.assertRaisesRegex(mcp_server.ToolError, "has not been found yet"):
            lab.get("/api/queue")
        server = mcp_server.Server(mcp_server.Tools(lab, self.player, clips=self.clips), self.wire)
        call, done = mcp_server.Call(1, "speak"), threading.Event()
        call.state = "starting"
        thread = threading.Thread(target=server.progress, args=(call, "p0", done))
        thread.start()
        deadline = time.monotonic() + 10
        while len([item for item in self.wire.messages if item.get("method") == "notifications/progress"]) < 3:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        call.state = "making"  # the Lab is still not found: making_message says so in general words
        time.sleep(0.05)
        done.set()
        thread.join(5)
        messages = [item["params"]["message"] for item in self.wire.messages if item.get("method") == "notifications/progress"]
        self.assertEqual(messages[:3], ["Starting Fel's Voice Lab."] * 3)
        self.assertIn("The Lab is making the clip.", messages)

    def test_a_long_call_hears_how_it_stands(self):
        self.enterContext(mock.patch.object(mcp_server, "PROGRESS_SECONDS", 0.05))
        self.engines.gate = threading.Event()
        self.call(1, "speak", {"text": "Wait for it.", "play": False}, meta={"progressToken": "p1"})
        self.assertTrue(self.engines.started.wait(10))
        time.sleep(0.3)
        self.engines.gate.set()
        self.result(1)
        progress = [message["params"] for message in self.wire.messages if message.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(progress), 2)
        self.assertEqual({item["progressToken"] for item in progress}, {"p1"})
        counts = [item["progress"] for item in progress]
        self.assertEqual(counts, sorted(set(counts)), "progress only increases")
        self.assertTrue(any("The Lab is making the clip" in item["message"] for item in progress), progress)

    def test_text_that_is_empty_or_too_long_is_refused_with_what_to_do(self):
        self.call(1, "speak", {"text": "   "})
        self.assertEqual(self.result(1)["content"][0]["text"], "text is empty: give the words to say.")
        self.call(2, "speak", {"text": "a" * 10_001})
        self.assertEqual(
            self.result(2)["content"][0]["text"], "text has 10,001 characters; speak takes up to 10,000. Send it in pieces."
        )
        self.assertEqual(self.engines.requests, [])

    def test_status_and_voices_read_the_lab_and_change_nothing(self):
        self.call(1, "lab_status")
        result = self.result(1)
        self.assertTrue(result["structuredContent"]["running"])
        self.assertIn(f"is running at {self.service.base}, on the processor.", result["content"][0]["text"])
        self.assertIn("Queue: nothing running, 0 waiting.", result["content"][0]["text"])
        self.call(2, "list_voices", {"language": "spanish"})
        self.assertEqual([voice["name"] for voice in self.result(2)["structuredContent"]["voices"]], ["ef_dora"])
        self.call(3, "list_voices", {"language": "Klingon"})
        self.assertEqual(
            self.result(3)["content"][0]["text"],
            "No voice can speak Klingon yet. Install a model on the Lab's Models & features page, or add a voice in Cast.",
        )
        self.assertEqual(self.engines.requests, [])


class ModernProtocolTests(InProcessCase):
    """2026-07-28: no handshake; each request names its version and the client's capabilities in _meta."""

    META = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "modern", "version": "1"},
    }

    def request(self, rpc_id, method, params=None, meta=None):
        params = dict(params or {})
        params["_meta"] = dict(self.META if meta is None else meta)
        self.server.handle(json.dumps({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}).encode())
        return self.wire.answer(rpc_id)

    def test_discover_says_what_the_server_speaks_and_is_without_a_handshake(self):
        result = self.request("d", "server/discover")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"], ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"])
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["_meta"], {"io.modelcontextprotocol/serverInfo": {"name": "voice-lab", "title": APP_NAME,
                                                                                    "version": VERSION}})  # fmt: skip
        self.assertEqual((result["ttlMs"], result["cacheScope"]), (3_600_000, "public"))

    def test_tools_are_listed_and_called_with_the_fields_the_revision_asks_for(self):
        listed = self.request(1, "tools/list")["result"]
        self.assertEqual(listed["resultType"], "complete")
        self.assertEqual(
            [tool["name"] for tool in listed["tools"]],
            [
                "lab_status",
                "list_voices",
                "speak",
                "stop_speaking",
                "set_voice",
                "describe_voice",
                "transcribe",
                "preview_voice_candidate",
                "import_voice_candidate",
            ],
        )
        self.assertEqual((listed["ttlMs"], listed["cacheScope"]), (3_600_000, "public"))
        spoken = self.request(2, "tools/call", {"name": "speak", "arguments": {"text": "Hello.", "play": False}})["result"]
        self.assertEqual(spoken["resultType"], "complete")
        self.assertFalse(spoken["isError"])
        self.assertTrue(Path(spoken["structuredContent"]["file"]).is_file())
        self.assertIn("io.modelcontextprotocol/serverInfo", spoken["_meta"])

    def test_a_version_it_does_not_speak_or_a_request_without_capabilities_is_refused_as_the_revision_says(self):
        error = self.request(1, "tools/list", meta={**self.META, "io.modelcontextprotocol/protocolVersion": "2099-01-01"})
        self.assertEqual(error["error"]["code"], -32022)
        self.assertEqual(error["error"]["data"]["requested"], "2099-01-01")
        self.assertIn("2026-07-28", error["error"]["data"]["supported"])
        meta = {key: value for key, value in self.META.items() if not key.endswith("clientCapabilities")}
        self.assertEqual(self.request(2, "tools/list", meta=meta)["error"]["code"], -32602)

    def test_a_legacy_client_never_sees_the_modern_fields_and_initialize_stays_legacy(self):
        self.server.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"protocolVersion": "2026-07-28"}}).encode())  # fmt: skip
        self.assertEqual(self.wire.answer(1)["result"]["protocolVersion"], "2025-11-25")
        self.server.handle(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode())
        self.assertEqual(set(self.wire.answer(2)["result"]), {"tools"})


# ------------------------------------------------------------------------------------------ when the Lab is not running


class FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class StartingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / ".venv" / "Scripts").mkdir(parents=True)
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        (self.root / ".venv" / "bin" / "python").write_bytes(b"")
        (self.root / "serve.py").write_text("", "utf-8")
        self.environ = {"VOICE_LAB_ADDRESS_FILE": str(self.root / "service.json"), "FELS_SUITE_DIR": str(self.root / "suite")}
        self.started = []
        self.answers = {}
        self.enterContext(mock.patch.object(lab_address, "ask_health", lambda url, timeout=2.0: self.answers.get(url)))

    def starter(self, exit_code=None, say=""):
        def start(command, **options):
            self.started.append((command, options))
            options["stdout"].write(say)
            if exit_code is None:
                library = Path(command[command.index("--state-root") + 1])
                port = command[command.index("--port") + 1]
                self.answers[f"http://127.0.0.1:{port}"] = {"app": APP_NAME, "lab_id": lab_address.lab_id(self.root, library)}
            return FakeProcess(exit_code)

        return start

    def lab(self, **options):
        return mcp_server.Lab(self.root, environ=self.environ, run_folder=self.root / "out" / "spoken" / "run", **options)

    def test_a_closed_lab_is_started_with_the_windows_library_and_port_and_stops_with_this_server(self):
        (self.root / "desktop.local.json").write_text(json.dumps({"state_root": "library", "port": 8790}), "utf-8")
        lab = self.lab(starter=self.starter())
        self.assertEqual(lab.ensure(), "http://127.0.0.1:8790")
        [(command, options)] = self.started
        python = self.root / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        expected = [str(python), "-X", "utf8", str(self.root / "serve.py"), "--port", "8790"]
        expected += ["--state-root", str(self.root / "library")]
        if os.name == "nt":
            expected += ["--parent-pid", str(os.getpid())]
        self.assertEqual(command, expected)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(Path(options["stdout"].name), self.root / "out" / "spoken" / "run" / "service.log")
        self.assertEqual(options["cwd"], str(self.root))
        self.assertEqual(lab.ensure(), "http://127.0.0.1:8790", "once running, it is found, not started again")
        self.assertEqual(len(self.started), 1)

    def test_a_service_that_cannot_start_says_why(self):
        lab = self.lab(starter=self.starter(exit_code=1, say="Traceback\nModuleNotFoundError: No module named 'numpy'\n"))
        with self.assertRaises(mcp_server.ToolError) as caught:
            lab.ensure()
        self.assertEqual(
            str(caught.exception),
            "Fel's Voice Lab could not be started: Traceback ModuleNotFoundError: No module named 'numpy' "
            "Open Fel's Voice Lab on this computer, then try again.",
        )

    def test_without_its_python_or_when_told_not_to_it_says_how_to_open_the_lab(self):
        (self.root / ".venv" / "Scripts" / "python.exe").unlink()
        (self.root / ".venv" / "bin" / "python").unlink()
        with self.assertRaises(mcp_server.ToolError) as caught:
            self.lab(starter=self.starter()).ensure()
        self.assertIn("its Python is missing. Run Setup (or setup.bat) for the Lab first.", str(caught.exception))
        with self.assertRaises(mcp_server.ToolError) as caught:
            self.lab(can_start=False, starter=self.starter()).ensure()
        self.assertEqual(
            str(caught.exception),
            "Fel's Voice Lab is not running: nothing answers at http://127.0.0.1:8765. "
            "Open Fel's Voice Lab on this computer, then try again.",
        )
        self.assertEqual(self.started, [])

    def test_status_never_starts_the_lab(self):
        tools = mcp_server.Tools(self.lab(starter=self.starter()), RecordingPlayer(), clips=self.root / "clips")
        text, facts = tools.lab_status(mcp_server.Call(1, "lab_status"), {})
        self.assertEqual(facts, {"running": False, "url": "http://127.0.0.1:8765"})
        self.assertEqual(
            text,
            "Fel's Voice Lab is not running (nothing answers at http://127.0.0.1:8765). speak and list_voices start it, "
            "which takes a few seconds.",
        )
        self.assertEqual(self.started, [])


class HousekeepingTests(unittest.TestCase):
    def test_an_mcp_servers_clips_go_a_day_after_it_stopped(self):
        out = Path(self.enterContext(tempfile.TemporaryDirectory()))
        spoken = out / "spoken"
        names = ["mcp-20261101-101010-111", "mcp-20261101-101010-222", "mcp-20261114-101010-333", "kept-by-hand"]
        for name in names:
            (spoken / name).mkdir(parents=True)
            (spoken / name / "001-hello.wav").write_bytes(b"RIFF")
        now = time.time()
        for name in names[:2] + names[3:]:
            os.utime(spoken / name, (now - 3 * 86400, now - 3 * 86400))
        alive = {222}.__contains__
        items = housekeeping.plan(out, now=now, pid_alive=alive)
        self.assertEqual([(item.kind, item.path.name) for item in items], [("spoken", "mcp-20261101-101010-111")])
        self.assertEqual(housekeeping.summarize(items)["by_kind"]["spoken"], {"count": 1, "bytes": 4})
        self.assertEqual(housekeeping.apply(items, out)["removed"], 1)
        self.assertEqual(sorted(path.name for path in spoken.iterdir()), sorted(names[1:]))


class SettingsTests(unittest.TestCase):
    def test_the_lines_for_claude_code_and_other_apps_name_this_labs_python_and_server(self):
        settings = lab_address.mcp_settings(Path("D:/My Lab"), Path("D:/My Lab/.venv/Scripts/python.exe"))
        script = Path("D:/My Lab/tools/mcp_server.py")
        python = Path("D:/My Lab/.venv/Scripts/python.exe")
        self.assertEqual(
            settings["claude_code"], f'claude mcp add --scope user --transport stdio voice-lab -- "{python}" "{script}"'
        )
        self.assertEqual(
            json.loads(settings["settings"]), {"mcpServers": {"voice-lab": {"command": str(python), "args": [str(script)]}}}
        )

    def test_the_windows_library_is_its_setting_or_the_lab_folder(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.assertEqual(lab_address.window_library(root), root)
        elsewhere = root.parent / "Elsewhere"
        (root / "desktop.local.json").write_text(json.dumps({"state_root": str(elsewhere)}), "utf-8")
        self.assertEqual(lab_address.window_library(root), elsewhere)
        (root / "desktop.local.json").write_text(json.dumps({"state_root": "library"}), "utf-8")
        self.assertEqual(lab_address.window_library(root), root / "library")
        (root / "desktop.local.json").write_text("not json", "utf-8")
        self.assertEqual(lab_address.window_library(root), root)

    def test_the_output_stream_gets_whole_lines_of_json_with_no_newline_inside(self):
        output = io.BytesIO()
        server = mcp_server.Server(None, output)
        server.answer(1, {"content": [{"type": "text", "text": "two\nlines"}]})
        self.assertEqual(output.getvalue().count(b"\n"), 1)
        self.assertEqual(json.loads(output.getvalue())["result"]["content"][0]["text"], "two\nlines")


class RouteTests(unittest.TestCase):
    def test_every_lab_path_the_server_calls_is_one_of_the_labs_routes(self):
        """design_voice once called a route that had moved to a later branch, and every call to it failed."""
        routes = {route.path for route in api_spec.ROUTES}
        source = (ROOT / "tools" / "mcp_server.py").read_text(encoding="utf-8")
        named = set(re.findall(r'"(/(?:api|v1)/[A-Za-z0-9/_-]*)', source))
        self.assertGreater(len(named), 5)
        for path in sorted(named):
            with self.subTest(path=path):
                self.assertTrue(any(route.startswith(path) for route in routes), f"no route starts {path}")


if __name__ == "__main__":
    unittest.main()
