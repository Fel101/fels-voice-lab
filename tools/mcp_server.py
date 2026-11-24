"""Give Claude Code, or any other app that uses MCP, a voice: an MCP server for Fel's Voice Lab.

    claude mcp add --scope user --transport stdio voice-lab -- "<Lab>\\.venv\\Scripts\\python.exe" "<Lab>\\tools\\mcp_server.py"

The Lab's page shows that line with this computer's real paths (System & backups, Connect other apps), and
docs/CONNECT.md says how to add it to other apps. The app starts this file and talks to it over standard input and
output: JSON-RPC messages, one per line (MCP's stdio transport). It speaks both eras of the protocol: 2026-07-28, where
each request names its version and server/discover says what the server is, and the initialize handshake of 2025-11-25
and the three revisions before it. It calls the Lab's HTTP API on this computer for the app.

Its tools: lab_status, list_voices, speak and stop_speaking. None of them deletes, restores, installs or changes anything
in the library: a clip is spoken without keeping a take.

When the Lab is not running, the first call that needs it starts the Lab's service from this folder, with the library
the Lab's window opens, and that service stops when this server does. (--url, or --no-start, turns that off.) Clips go
to out/spoken/mcp-<date>-<time>-<process id>/ in the Lab's folder, which the Lab's cleanup removes once this server has
stopped and a day has passed, unless a call names a file to keep.

Standard library only, as tools/lab_cli.py is, so the Lab's own Python runs it and so does any Python 3.8 or later.
Standard output carries protocol messages only; what it says for people goes to standard error.
"""

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from voice_lab import lab_address  # noqa: E402 - beside this folder, standard library only
from voice_lab import speech_input  # noqa: E402
from app_version import APP_NAME, VERSION  # noqa: E402

SERVER_NAME = lab_address.MCP_NAME

# The protocol versions this server speaks, newest first. Modern ones (2026-07-28) carry the version in each request's
# _meta and need no handshake; legacy ones agree on it once with initialize, where a client that asks for one of them gets
# it and any other gets the newest (each revision's "Versioning and Compatibility" or lifecycle page).
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"
SERVER_INFO = {"name": SERVER_NAME, "title": APP_NAME, "version": VERSION}
LIST_TTL_MS = 3_600_000  # the tool list never changes while the server runs
MAX_BUNDLE_BYTES = 33 * 1024 * 1024 + 4096  # the Slate handoff upload limit
QUICK_SECONDS = 10  # health, voices, the queue and a cancel answer at once
START_SECONDS = 120  # a starting service imports its packages and opens the library
WAIT_SECONDS = 3600  # how long a clip waits in the Lab's line: the most the service allows
SPEAK_SECONDS = WAIT_SECONDS + 1800  # waiting, loading the model, and making up to 10,000 characters
PROGRESS_SECONDS = 10  # how often a call that asked for progress hears how it stands
MAX_TEXT = 10_000  # what one /api/speak call takes (scene_store.MAX_LINE_TEXT)
NOT_RUNNING_HINT = "Open Fel's Voice Lab on this computer, then try again."

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
UNSUPPORTED_VERSION = -32022  # 2026-07-28's UnsupportedProtocolVersionError
INSTRUCTIONS = (
    "Fel's Voice Lab speaks text aloud on this computer. Call list_voices for the voices, speak to say something, "
    "stop_speaking to stop. The first clip loads a model and can take a minute."
)


def log(message):
    print(f"voice-lab MCP: {message}", file=sys.stderr, flush=True)


class ToolError(Exception):
    """A tool could not do what it was asked. The message says why and what to do; it goes back as the tool's result."""


class Stopped(Exception):
    """stop_speaking, or the client, stopped this call."""


# ----------------------------------------------------------------------------------------------------- the Lab's API


class Lab:
    """The Lab's service on this computer: found where lab_cli.py looks (lab_address.find_url), or started from this
    folder when none answers."""

    def __init__(self, root=ROOT, *, url=None, can_start=True, environ=None, run_folder=None, starter=subprocess.Popen):
        self.root = Path(root)
        self.given = url
        self.can_start = can_start and not url
        self.environ = os.environ if environ is None else environ
        self.run_folder = run_folder
        self.starter = starter
        self.url = None
        self.process = None  # the service this server started, if it did
        self.lock = threading.Lock()

    def find(self):
        """The address of a Lab that answers now, or None."""
        found, _source = lab_address.find_url(self.given, self.environ)
        for url in dict.fromkeys(item for item in (self.url, found) if item):
            if lab_address.is_lab(lab_address.ask_health(url)):
                self.url = url.rstrip("/")
                return self.url
        return None

    def ensure(self, call=None):
        """The address of a running Lab, starting this folder's service when none runs and that is allowed."""
        with self.lock:
            url = self.find()
            if url:
                return url
            if not self.can_start:
                where = self.given or lab_address.find_url(None, self.environ)[0]
                raise ToolError(f"Fel's Voice Lab is not running: nothing answers at {where}. {NOT_RUNNING_HINT}")
            if call is not None:
                call.state = "starting"
            return self.start(call)

    def python(self):
        windows = self.root / ".venv" / "Scripts" / "python.exe"
        other = self.root / ".venv" / "bin" / "python"
        return windows if os.name == "nt" else other

    def start(self, call=None):
        python = self.python()
        if not python.is_file() or not (self.root / "serve.py").is_file():
            raise ToolError(
                f"Fel's Voice Lab is not running, and it cannot be started from {self.root}: its Python is missing. "
                "Run Setup (or setup.bat) for the Lab first."
            )
        library = lab_address.window_library(self.root)
        command = [str(python), "-X", "utf8", str(self.root / "serve.py"), "--port", str(lab_address.window_port(self.root))]
        command += ["--state-root", str(library)]
        if os.name == "nt":
            command += ["--parent-pid", str(os.getpid())]  # it stops when this server does, however this server ends
        folder = self.run_folder() if callable(self.run_folder) else Path(self.run_folder or self.root / "out" / "spoken")
        folder.mkdir(parents=True, exist_ok=True)
        service_log = folder / "service.log"
        log(f"the Lab is not running; starting its service ({service_log})")
        with open(service_log, "w", encoding="utf-8") as output:
            self.process = self.starter(
                command,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=output,  # never this server's own output, which carries the protocol
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        wanted = lab_address.lab_id(self.root, library)
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if call is not None and call.cancelled.is_set():
                raise Stopped()
            if self.process.poll() is not None:
                raise ToolError(
                    f"Fel's Voice Lab could not be started: {last_lines(service_log)} {NOT_RUNNING_HINT}".replace("  ", " ")
                )
            candidates = [lab_address.find_url(None, self.environ)[0], f"http://127.0.0.1:{lab_address.window_port(self.root)}"]
            for url in dict.fromkeys(candidates):
                answer = lab_address.ask_health(url, timeout=1.0)
                if lab_address.is_lab(answer) and answer.get("lab_id") == wanted:
                    self.url = url.rstrip("/")
                    log(f"the Lab's service is running at {self.url}")
                    return self.url
            time.sleep(0.25)
        raise ToolError(f"Fel's Voice Lab did not start within {START_SECONDS} seconds. {NOT_RUNNING_HINT}")

    def stop(self):
        """Stop the service this server started. On Windows it stops by itself, and cleanly, within a second of this
        process ending (--parent-pid); there the process this server holds is only the environment's launcher."""
        process, self.process = self.process, None
        if process is None or process.poll() is not None or os.name == "nt":
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def call(self, method, path, body=None, *, headers=None, timeout=QUICK_SECONDS):
        """One request: (status, headers with lower-case names, bytes). An error answer raises ToolError with the
        service's sentence; nothing answering raises ToolError too, as does a Lab not found yet."""
        if not self.url:
            raise ToolError(f"Fel's Voice Lab has not been found yet. {NOT_RUNNING_HINT}")
        address = urllib.parse.urlsplit(self.url)
        connection = http.client.HTTPConnection(address.hostname, address.port or 80, timeout=timeout)
        if isinstance(body, bytes):
            data, sent = body, {"Content-Type": "application/octet-stream"}
        else:
            data = json.dumps(body).encode("utf-8") if body is not None else None
            sent = {"Content-Type": "application/json"} if data is not None else {}
        sent.update(headers or {})
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            payload = response.read()
            answer = {key.lower(): value for key, value in response.getheaders()}
        except TimeoutError:
            raise ToolError(f"Fel's Voice Lab did not answer within {timeout} seconds.") from None
        except (OSError, http.client.HTTPException) as exc:
            raise ToolError(f"Fel's Voice Lab stopped answering ({exc}). {NOT_RUNNING_HINT}") from None
        finally:
            connection.close()
        if response.status >= 400:
            raise LabRefused(response.status, payload)
        return response.status, answer, payload

    def get(self, path):
        return json.loads(self.call("GET", path)[2].decode("utf-8"))


class LabRefused(ToolError):
    def __init__(self, status, payload):
        try:
            answer = json.loads(payload.decode("utf-8"))
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        self.status = status
        self.code = answer.get("code")
        message = answer.get("error") or payload.decode("utf-8", "replace").strip()[:300] or f"status {status}"
        super().__init__(str(message))


def last_lines(path, count=3):
    try:
        lines = [line.strip() for line in Path(path).read_text("utf-8", "replace").splitlines() if line.strip()]
    except OSError:
        return "it wrote no log."
    return " ".join(lines[-count:]) or "it stopped without saying why."


# -------------------------------------------------------------------------------------------------------- playback


class Player:
    """Plays a WAV file on this computer's speakers until it ends or its call is stopped."""

    PLAYERS = (("afplay",), ("paplay",), ("aplay", "-q"))

    def play(self, path, seconds, stop):
        """Play the file; return True when it played to its end, False when `stop` (an Event) was set first."""
        if stop.is_set():
            return False
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            stopped = stop.wait(seconds + 0.3)
            if stopped:
                winsound.PlaySound(None, 0)
            return not stopped
        for player in self.PLAYERS:
            if shutil.which(player[0]):
                process = subprocess.Popen(
                    [*player, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                while process.poll() is None:
                    if stop.wait(0.1):
                        process.terminate()
                        return False
                return True
        raise ToolError("this computer has no player this server knows (afplay, paplay or aplay)")


# ------------------------------------------------------------------------------------------------------- the tools

NO_ARGUMENTS = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS = [
    {
        "name": "lab_status",
        "title": "Voice Lab status",
        "description": (
            "Say whether Fel's Voice Lab, the voice studio on this computer, is running, which voice models are "
            "installed, and what its queue is doing. It never starts the Lab; speak and list_voices do."
        ),
        "inputSchema": NO_ARGUMENTS,
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "list_voices",
        "title": "List voices",
        "description": (
            "List the voices Fel's Voice Lab can speak with now: the person's Cast (characters they made in the Lab) and "
            "the preset voices of the models installed. Each line gives the name to pass to speak, the model, the "
            "languages, and whether it takes an acting direction. Starts the Lab when it is not running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "description": "Only voices that speak this language, such as English."},
                "include_not_ready": {
                    "type": "boolean",
                    "description": "Also list voices that cannot speak yet, with the reason. Default false.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "speak",
        "title": "Speak aloud",
        "description": (
            "Say text aloud on this computer's speakers with Fel's Voice Lab, and save it as a WAV file. Use it when the "
            "person asks to hear something, or asked you to speak your answers. Give a voice name from list_voices, or "
            "leave voice out for the Lab's default voice. Up to 10,000 characters; long text is made in parts and joined. "
            "The first clip after the Lab starts also loads the voice's model, which takes from ten seconds to more than "
            "a minute: the call waits, it does not fail. Clips are spoken one after another, never over each other; "
            "stop_speaking stops one. Starts the Lab when it is not running. Answers with the file's path and length."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT, "description": "What to say."},
                "voice": {"type": "string", "description": "A voice's name from list_voices. Default: the Lab's default."},
                "direction": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": (
                        "How to say it, such as 'quietly, as if tired'. Only voices whose model takes acting directions "
                        "use it (list_voices says which); the others speak without it."
                    ),
                },
                "language": {
                    "type": "string",
                    "description": "The language to speak, such as English, for a voice that speaks several.",
                },
                "play": {"type": "boolean", "description": "false saves the clip without playing it. Default true."},
                "file": {
                    "type": "string",
                    "description": (
                        "A .wav path to save the clip to and keep. Without it the clip goes to the Lab's folder for these "
                        "clips, cleaned up a day after this server stops. An existing file is never replaced."
                    ),
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "stop_speaking",
        "title": "Stop speaking",
        "description": (
            "Stop what speak is saying: it stops the clip playing now and cancels the ones being made or waiting. "
            "Nothing else changes."
        ),
        "inputSchema": NO_ARGUMENTS,
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def local_tool(name, description, fields, required=()):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {key: {"type": kind, "description": note} for key, kind, note in fields},
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    }


TOOLS.extend(
    [
        local_tool(
            "set_voice",
            "Choose the default local voice for this client connection. Other clients keep their own settings.",
            [("voice", "string", "A ready voice name. An empty value clears it.")],
            ("voice",),
        ),
        local_tool(
            "describe_voice",
            "Describe a local model voice with its languages, direction controls and current readiness.",
            [("voice", "string", "A voice name from list_voices.")],
            ("voice",),
        ),
        local_tool(
            "transcribe",
            "Transcribe audio already selected in this client's workspace with local faster-whisper.",
            [
                ("file", "string", "A file within the configured workspace."),
                ("language", "string", "Optional language code."),
                ("translate", "boolean", "Return English words instead of the original language."),
            ],
            ("file",),
        ),
        local_tool(
            "preview_voice_candidate",
            "Preview a Voice Slate recording handoff and inspect its origin, permission and supported model choices.",
            [("file", "string", "Local Voice Slate handoff ZIP.")],
            ("file",),
        ),
        local_tool(
            "import_voice_candidate",
            "Import an explicitly reviewed Voice Slate handoff into Cast as a candidate pending approval.",
            [
                ("file", "string", "The same Voice Slate handoff ZIP."),
                ("character", "string", "An existing Cast character name or ID."),
                ("engine_id", "string", "An eligible engine from the preview."),
                ("sidecar_sha256", "string", "SHA-256 of the reviewed sidecar."),
                ("permission", "boolean", "True when the user has confirmed the allowed use."),
            ],
            ("file", "character", "engine_id", "sidecar_sha256", "permission"),
        ),
    ]
)
next(tool for tool in TOOLS if tool["name"] == "import_voice_candidate")["annotations"]["readOnlyHint"] = False
TOOLS[2]["inputSchema"]["properties"]["format"] = {
    "type": "string",
    "description": "wav, mp3, flac, opus or aac. Encoded files require play=false and a named voice.",
}

TOOL_NAMES = {tool["name"] for tool in TOOLS}


def check_arguments(name, arguments):
    """The arguments as the tool's schema allows them, or ToolError saying what is wrong."""
    if not isinstance(arguments, dict):
        raise ToolError("The arguments must be an object.")
    schema = next(tool for tool in TOOLS if tool["name"] == name)["inputSchema"]
    properties = schema["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        allowed = ", ".join(properties) or "none"
        raise ToolError(f"{name} takes no argument called {unknown[0]!r}. It takes: {allowed}.")
    for key in schema.get("required", ()):
        if key not in arguments:
            raise ToolError(f"{name} needs {key!r}.")
    for key, value in arguments.items():
        kind = properties[key]["type"]
        if kind == "string" and not isinstance(value, str):
            raise ToolError(f"{key} must be text.")
        if kind == "boolean" and not isinstance(value, bool):
            raise ToolError(f"{key} must be true or false.")
    return arguments


def slug(text, words=6):
    found = re.findall(r"[A-Za-z0-9]+", text)[:words]
    return "-".join(found).lower()[:40] or "clip"


def wav_seconds(path):
    with wave.open(str(path), "rb") as clip:
        return clip.getnframes() / float(clip.getframerate())


class Call:
    """One tools/call while it runs: stop_speaking and notifications/cancelled reach it here."""

    def __init__(self, rpc_id, name):
        self.rpc_id = rpc_id
        self.name = name
        self.cancelled = threading.Event()
        self.silent = False  # the client cancelled it, so no answer goes back (the specification's cancellation)
        self.lab_request = None  # the X-Request-Id of its clip, while the Lab has it
        self.state = "waiting"


class Tools:
    def __init__(self, lab, player, clips=None, *, workspace=None, scope=None):
        self.lab = lab
        self.player = player
        self.clips_root = Path(clips) if clips else ROOT / "out" / "spoken"
        self.workspace = Path(workspace).resolve(strict=True) if workspace is not None else None
        if self.workspace is not None and not self.workspace.is_dir():
            raise ValueError("The MCP workspace must be an existing directory.")
        self.voice_default = ""
        self.scope = scope or ""
        self.folder = None
        self.count = 0
        self.speaking = threading.Lock()  # one clip at a time, in the order the calls came
        self.calls = {}
        self.calls_lock = threading.Lock()
        lab.run_folder = self.run_folder

    def run_folder(self):
        """This server's folder for clips: the cleanup on System & backups knows its name (housekeeping.SPOKEN_NAME)."""
        if self.folder is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.folder = self.clips_root / f"mcp-{stamp}-{os.getpid()}"
            self.folder.mkdir(parents=True, exist_ok=True)
        return self.folder

    # ------------------------------------------------------------------------------------------------ lab_status

    def lab_status(self, call, arguments):
        url = self.lab.find()
        if url is None:
            where = self.lab.given or lab_address.find_url(None, self.lab.environ)[0]
            if self.lab.can_start:
                text = (
                    f"Fel's Voice Lab is not running (nothing answers at {where}). speak and list_voices start it, "
                    "which takes a few seconds."
                )
            else:
                text = f"Fel's Voice Lab is not running: nothing answers at {where}. {NOT_RUNNING_HINT}"
            return text, {"running": False, "url": where}
        health = self.lab.get("/api/health")
        status = self.lab.get("/api/engines/status")
        queue = self.lab.get("/api/queue")
        installed = [row["name"] for row in status.get("engines", []) if (row.get("installation") or {}).get("installed")]
        loaded = [row.get("engine") for row in health.get("workers") or [] if row.get("engine")]
        names = {row["id"]: row["name"] for row in status.get("engines", [])}
        device = {"cuda": "the graphics card", "cpu": "the processor"}.get(status.get("device"), status.get("device"))
        lines = [f"Fel's Voice Lab {health.get('version', '?')} is running at {url}, on {device}."]
        if self.lab.process is not None:
            lines.append("This server started it, and it stops when this server does.")
        lines.append("Installed models: " + (", ".join(installed) if installed else "none yet") + ".")
        if loaded:
            lines.append("Loaded now: " + ", ".join(names.get(engine, engine) for engine in loaded) + ".")
        running = queue.get("running")
        waiting = queue.get("waiting") or []
        doing = f"making a {running.get('what', 'request').lower()}" if running else "nothing running"
        lines.append(f"Queue: {doing}, {len(waiting)} waiting.")
        facts = {
            "running": True,
            "url": url,
            "version": health.get("version"),
            "device": status.get("device"),
            "started_by_this_server": self.lab.process is not None,
            "installed_models": installed,
            "loaded_models": [names.get(engine, engine) for engine in loaded],
            "queue": {"running": (running or {}).get("what"), "waiting": len(waiting)},
        }
        return " ".join(lines), facts

    # ----------------------------------------------------------------------------------------------- list_voices

    def voices(self, call):
        self.lab.ensure(call)
        return self.lab.get("/api/voices").get("voices", [])

    def list_voices(self, call, arguments):
        voices = self.voices(call)
        language = (arguments.get("language") or "").strip().casefold()
        if language:
            voices = [voice for voice in voices if any(language in item.casefold() for item in voice["languages"])]
        if not arguments.get("include_not_ready"):
            voices = [voice for voice in voices if voice["ready"]]
        if not voices:
            text = "No voice can speak" + (f" {arguments['language']}" if language else "") + " yet."
            text += " Install a model on the Lab's Models & features page, or add a voice in Cast."
            return text, {"voices": []}
        lines = [f"{len(voices)} voices. Pass the name before the dash to speak."]
        for voice in voices:
            what = f"Cast: {voice['character']}, {voice['label']}" if voice["kind"] == "cast" else voice["label"]
            notes = [voice["model_name"], ", ".join(voice["languages"])]
            if voice.get("takes_direction"):
                notes.append("takes directions")
            line = f"{voice['name']} - {what} ({'; '.join(notes)})"
            if not voice["ready"]:
                line += f". Not ready: {voice['why_not']}"
            lines.append(line)
        keep = ("name", "kind", "character", "label", "model", "model_name", "languages", "takes_direction", "ready")
        facts = [{key: voice[key] for key in keep if key in voice} for voice in voices]
        for fact, voice in zip(facts, voices):
            if not voice["ready"]:
                fact["why_not"] = voice["why_not"]
        return "\n".join(lines), {"voices": facts}

    def set_voice(self, call, arguments):
        value = arguments["voice"].strip()
        voice = self.named(call, value) if value else None
        if value and (voice is None or not voice.get("ready")):
            raise ToolError("Choose a ready voice from list_voices.")
        self.voice_default = voice["name"] if voice else ""
        return "Default voice set for this client.", {"voice": self.voice_default or None}

    def describe_voice(self, call, arguments):
        voice = self.named(call, arguments["voice"])
        if voice is None:
            raise ToolError("Choose a voice from list_voices.")
        keys = ("name", "model", "model_name", "languages", "takes_direction", "ready", "why_not", "kind")
        details = {key: voice[key] for key in keys if key in voice}
        return f"{voice['name']} uses {voice['model_name']}.", details

    def workspace_path(self, given):
        if self.workspace is None:
            raise ToolError("Start MCP with --workspace to select files for this client.")
        if not given or ":" in given[2:] or given.startswith(("//", chr(92) * 2)):
            raise ToolError("Choose a local file in the selected workspace.")
        candidate = Path(given)
        candidate = candidate if candidate.is_absolute() else self.workspace / candidate
        try:
            path = candidate.resolve()
        except (OSError, ValueError) as exc:
            raise ToolError("Choose a valid local file in the workspace.") from exc
        if path == self.workspace or self.workspace not in path.parents:
            raise ToolError("The file is outside the selected workspace.")
        return path

    def transcribe(self, call, arguments):
        path = self.workspace_path(arguments["file"])
        if not path.is_file() or path.stat().st_size > speech_input.LIMIT:
            raise ToolError("Choose an audio file in the selected workspace of at most 25 MiB.")
        call.state = "recognizing"
        try:
            suffix = speech_input.audio_suffix(path.name)
            options = speech_input.settings(arguments, translate=arguments.get("translate", False))
            with path.open("rb") as audio:
                raw = audio.read(speech_input.LIMIT + 1)
            result = speech_input.run(self.run_folder(), raw, suffix, options, cancelled=call.cancelled)
        except speech_input.InputCancelled as exc:
            raise Stopped() from exc
        except (ValueError, OSError) as exc:
            raise ToolError(str(exc)) from exc
        return result["text"], {
            "text": result["text"],
            "language": result["language"],
            "duration": result["duration"],
            "model": result["model"],
        }

    def _slate_file(self, given):
        path = self.workspace_path(given)
        if not path.is_file() or path.suffix.casefold() != ".zip":
            raise ToolError("Choose a Voice Slate ZIP in the selected workspace.")
        with path.open("rb") as file:
            raw = file.read(MAX_BUNDLE_BYTES + 1)
        if not 0 < len(raw) <= MAX_BUNDLE_BYTES:
            raise ToolError("The Voice Slate ZIP is empty or too large.")
        return raw

    def preview_voice_candidate(self, call, arguments):
        raw = self._slate_file(arguments["file"])
        self.lab.ensure(call)
        _, _, result = self.lab.call("POST", "/api/cast/slate-clip/preview", raw, timeout=45)
        facts = json.loads(result)
        return "Review the reference origin and allowed use before creating a Cast candidate.", facts

    def import_voice_candidate(self, call, arguments):
        check = arguments["sidecar_sha256"]
        if arguments["permission"] is not True or not re.fullmatch("[a-f0-9]{64}", check):
            raise ToolError("Preview this handoff and confirm its permitted use before import.")
        raw = self._slate_file(arguments["file"])
        self.lab.ensure(call)
        query = urllib.parse.urlencode({name: arguments[name] for name in ("character", "engine_id", "sidecar_sha256")})
        _, _, result = self.lab.call("POST", "/api/cast/slate-clip/import?" + query, raw, timeout=45)
        facts = json.loads(result)
        return "Reference imported as a Cast candidate.", facts

    # ----------------------------------------------------------------------------------------------------- speak

    def clip_path(self, arguments):
        given = arguments.get("file")
        suffix = "." + arguments.get("format", "wav")
        if given:
            path = self.workspace_path(given) if self.workspace is not None else Path(given).expanduser()
            if not path.is_absolute():
                path = Path(self.lab.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()) / path
            if path.suffix.lower() != suffix:
                raise ToolError(f"file must end in {suffix}: {path}")
            if path.exists():
                raise ToolError(f"{path} already exists, and speak never replaces a file. Name another file.")
            return path, True
        self.count += 1
        return self.run_folder() / f"{self.scope}{self.count:03d}-{slug(arguments['text'])}{suffix}", False

    def speak(self, call, arguments):
        arguments = {**arguments}
        if "voice" not in arguments and self.voice_default:
            arguments["voice"] = self.voice_default
        fmt = arguments.get("format", "wav")
        if fmt not in ("wav", "mp3", "flac", "opus", "aac"):
            raise ToolError("Choose WAV, MP3, FLAC, Opus or AAC.")
        if fmt != "wav" and (arguments.get("play", True) or not arguments.get("voice")):
            raise ToolError("Encoded output needs a named voice and play=false.")
        text = arguments["text"]
        if not text.strip():
            raise ToolError("text is empty: give the words to say.")
        if len(text) > MAX_TEXT:
            raise ToolError(f"text has {len(text):,} characters; speak takes up to {MAX_TEXT:,}. Send it in pieces.")
        path, kept = self.clip_path(arguments)
        call.state = "queued"
        while not self.speaking.acquire(timeout=0.2):  # the clip before this one is still being made or played
            if call.cancelled.is_set():
                raise Stopped()
        try:
            return self._speak(call, arguments, path, kept)
        finally:
            self.speaking.release()

    def _speak(self, call, arguments, path, kept):
        if call.cancelled.is_set():
            raise Stopped()
        self.lab.ensure(call)
        voice_name = (arguments.get("voice") or "").strip()
        direction = (arguments.get("direction") or "").strip()
        notes = []
        body = {"text": arguments["text"], "wait_seconds": WAIT_SECONDS}
        if voice_name:
            body["voice"] = voice_name
        if arguments.get("language"):
            body["language"] = arguments["language"]
        if direction:
            voice = self.named(call, voice_name) if voice_name else None
            if (voice is None and voice_name) or (voice is not None and voice.get("takes_direction")):
                body["direction"] = direction  # a name the Lab does not know is the Lab's to refuse
            else:
                who = voice["name"] if voice is not None else "The Lab's default voice"
                notes.append(f"{who} takes no acting direction, so it was spoken without one.")
        call.lab_request = f"mcp-{uuid.uuid4().hex}"
        call.state = "making"
        fmt = arguments.get("format", "wav")
        path_api = "/api/speak"
        if fmt != "wav":
            path_api = "/v1/audio/speech"
            body = {
                "input": arguments["text"],
                "voice": voice_name,
                "model": "tts-1",
                "instructions": direction,
                "response_format": fmt,
                "wait_seconds": WAIT_SECONDS,
            }
        try:
            _status, headers, data = self.lab.call(
                "POST", path_api, body, headers={"X-Request-Id": call.lab_request}, timeout=SPEAK_SECONDS
            )
        except LabRefused as exc:
            if exc.code == "cancelled" or call.cancelled.is_set():
                raise Stopped() from None
            raise ToolError(self.explain(exc, voice_name)) from None
        finally:
            call.lab_request = None
        if call.cancelled.is_set():
            raise Stopped()
        if fmt != "wav":
            if not data or not headers.get("content-type", "").startswith("audio/"):
                raise ToolError("Voice Lab did not return audio in the selected format.")
            save(path, data)
            return f"Saved {fmt.upper()} audio.", {
                "file": str(path),
                "format": fmt,
                "voice": voice_name,
                "seconds": float(headers.get("x-audio-seconds", "0")),
                "kept": kept,
                "played": False,
            }
        if not (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
            raise ToolError("The Lab's answer was not a WAV file, so nothing was saved.")
        save(path, data)
        seconds = wav_seconds(path)
        parts = int(headers.get("x-parts") or 1)
        facts = {"file": str(path), "seconds": round(seconds, 2), "voice": voice_name or None, "parts": parts}
        facts.update({"kept": kept, "played": False})
        said = f"as {voice_name}" if voice_name else "in the Lab's default voice"
        if arguments.get("play", True):
            call.state = "playing"
            try:
                facts["played"] = self.player.play(path, seconds, call.cancelled)
            except ToolError as exc:
                notes.append(f"It was not played: {exc}.")
            if not facts["played"] and call.cancelled.is_set():
                notes.append("Playback was stopped.")
            verb = "Spoke" if facts["played"] else "Made"
        else:
            verb = "Made"
        where = "Saved to" if kept else "The clip is"
        text = f"{verb} {seconds:.1f} s {said}" + (f", in {parts} parts" if parts > 1 else "") + f". {where} {path}."
        return " ".join([text, *notes]), facts

    def named(self, call, name):
        """The voice a name means in the Lab's list, or None when the Lab's own answer should say what is wrong."""
        wanted = name.casefold()
        for voice in self.voices(call):
            if wanted in {item.casefold() for item in [voice["name"], *voice.get("aliases", [])]}:
                return voice
        return None

    @staticmethod
    def explain(exc, voice_name):
        message = str(exc).replace("GET /api/voices lists", "list_voices lists")
        if exc.code == "model_not_installed" and not voice_name:
            message += " Or name another voice: list_voices lists them."
        if exc.code == "busy":
            message += " Try again in a moment."
        return message

    # --------------------------------------------------------------------------------------------- stop_speaking

    def stop_speaking(self, call, arguments):
        with self.calls_lock:
            speaking = [other for other in self.calls.values() if other.name == "speak" and other is not call]
        if not speaking:
            return "Nothing is being spoken.", {"stopped": 0}
        for other in speaking:
            self.stop(other)
        count = len(speaking)
        return ("Stopped." if count == 1 else f"Stopped {count} clips."), {"stopped": count}

    def stop(self, call, silent=False):
        call.silent = call.silent or silent
        call.cancelled.set()  # a clip that is playing stops at once (Player.play waits on it)
        request = call.lab_request
        if request and self.lab.url:
            for _attempt in range(3):  # the Lab may not have it in its line yet
                try:
                    self.lab.call("POST", f"/api/queue/{urllib.parse.quote(request)}/cancel")
                    return
                except LabRefused as exc:
                    if exc.status != 404 or call.lab_request is None:
                        return
                except ToolError:
                    return
                time.sleep(0.3)

    def stop_all(self):
        with self.calls_lock:
            calls = list(self.calls.values())
        for call in calls:
            self.stop(call, silent=True)


def save(path, data):
    """Write the clip under a temporary name and move it into place, so a failure leaves no half file."""
    path = Path(path)
    partial = path.with_name(path.name + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(data)
        os.replace(partial, path)
    except OSError as exc:
        raise ToolError(f"The clip could not be saved to {path}: {exc.strerror or exc}") from None
    finally:
        if partial.exists():
            partial.unlink()


# ---------------------------------------------------------------------------------------------------- the protocol


class Server:
    """MCP over newline-delimited JSON-RPC. A legacy client opens with initialize; a modern one (2026-07-28) names the
    version in each request's _meta and may ask server/discover first. Both get tools/list and tools/call, and the
    notifications cancelled (and initialized). Each tools/call runs on a thread of its own, so a stop or a cancel is heard
    while a clip is made."""

    def __init__(self, tools, output):
        self.tools = tools
        self.output = output
        self.writing = threading.Lock()
        self.threads = []

    def send(self, message):
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.writing:
            self.output.write(line.encode("utf-8"))
            self.output.flush()

    def answer(self, rpc_id, result, modern=False):
        if modern:  # 2026-07-28: every result says what it is, and who answered
            result = {"resultType": "complete", **result, "_meta": {META_SERVER: SERVER_INFO}}
        self.send({"jsonrpc": "2.0", "id": rpc_id, "result": result})

    def error(self, rpc_id, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": rpc_id, "error": error})

    def serve(self, stream):
        """Read messages until the client closes standard input, then stop what is running and return."""
        for raw in iter(stream.readline, b""):
            line = raw.strip()
            if line:
                self.handle(line)
        log("the client closed the connection; stopping")
        self.tools.stop_all()
        for thread in self.threads:
            thread.join(timeout=2)
        self.tools.lab.stop()

    def handle(self, line):
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self.error(None, PARSE_ERROR, "Parse error: each line must be one JSON-RPC message.")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self.error(None, INVALID_REQUEST, "Invalid request: send one JSON-RPC 2.0 object per line.")
        method = message.get("method")
        if not isinstance(method, str):
            return None  # a response to a request this server never sends, or something it does not know
        params = message.get("params") or {}
        if "id" not in message:
            return self.notification(method, params)
        rpc_id = message["id"]
        if method == "initialize":
            return self.answer(rpc_id, self.initialize(params))
        meta = params.get("_meta") if isinstance(params, dict) else None
        modern = isinstance(meta, dict) and META_VERSION in meta
        if modern and not self.modern_request_ok(rpc_id, meta):
            return None
        if method == "server/discover":
            return self.answer(rpc_id, self.discover(), modern=True)
        if method == "ping":
            return self.answer(rpc_id, {}, modern)
        if method == "tools/list":
            listed = {"tools": TOOLS}
            if modern:
                listed.update({"ttlMs": LIST_TTL_MS, "cacheScope": "public"})
            return self.answer(rpc_id, listed, modern)
        if method == "tools/call":
            return self.start_call(rpc_id, params, modern)
        return self.error(rpc_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def modern_request_ok(self, rpc_id, meta):
        """A 2026-07-28 request names a version this server speaks and the client's capabilities; otherwise the error
        the specification gives goes back, and False."""
        version = meta.get(META_VERSION)
        if version not in MODERN_VERSIONS:
            self.error(
                rpc_id,
                UNSUPPORTED_VERSION,
                "Unsupported protocol version",
                {"supported": list(PROTOCOL_VERSIONS), "requested": version},
            )
            return False
        if not isinstance(meta.get(META_CAPABILITIES), dict):
            self.error(rpc_id, INVALID_PARAMS, f"Invalid params: _meta needs {META_CAPABILITIES}.")
            return False
        return True

    def initialize(self, params):
        """The legacy handshake: the client's version when this server speaks it, else the newest legacy one."""
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        version = asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        client = (params.get("clientInfo") or {}).get("name", "a client") if isinstance(params, dict) else "a client"
        log(f"{client} connected (protocol {version})")
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }

    def discover(self):
        """2026-07-28's server/discover: the versions, capabilities and instructions, without a handshake."""
        return {
            "supportedVersions": list(PROTOCOL_VERSIONS),
            "capabilities": {"tools": {}},
            "instructions": INSTRUCTIONS,
            "ttlMs": LIST_TTL_MS,
            "cacheScope": "public",
        }

    def notification(self, method, params):
        if method == "notifications/cancelled" and isinstance(params, dict):
            with self.tools.calls_lock:
                call = self.tools.calls.get(json.dumps(params.get("requestId")))
            if call is not None:
                log(f"the client cancelled a {call.name} call" + (f": {params['reason']}" if params.get("reason") else ""))
                threading.Thread(target=self.tools.stop, args=(call, True), daemon=True).start()

    def start_call(self, rpc_id, params, modern=False):
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return self.error(rpc_id, INVALID_PARAMS, "tools/call needs the tool's name.")
        name = params["name"]
        if name not in TOOL_NAMES:
            return self.error(rpc_id, INVALID_PARAMS, f"Unknown tool: {name}")
        call = Call(rpc_id, name)
        key = json.dumps(rpc_id)
        with self.tools.calls_lock:
            self.tools.calls[key] = call
        token = (params.get("_meta") or {}).get("progressToken") if isinstance(params.get("_meta"), dict) else None
        arguments = params.get("arguments") or {}
        thread = threading.Thread(target=self.run_call, args=(call, key, arguments, token, modern), daemon=True)
        self.threads = [item for item in self.threads if item.is_alive()] + [thread]
        thread.start()
        return None

    def run_call(self, call, key, arguments, token, modern=False):
        ticking = threading.Event()
        if token is not None:
            threading.Thread(target=self.progress, args=(call, token, ticking), daemon=True).start()
        try:
            text, facts = getattr(self.tools, call.name)(call, check_arguments(call.name, arguments))
            result = {"content": [{"type": "text", "text": text}], "structuredContent": facts, "isError": False}
        except Stopped:
            result = {"content": [{"type": "text", "text": "Stopped before it was spoken."}], "isError": False}
            result["structuredContent"] = {"stopped": True}
        except ToolError as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # a fault here must not end the server; the client hears what it was
            log(f"{call.name} failed: {type(exc).__name__}: {exc}")
            result = {"content": [{"type": "text", "text": f"{call.name} failed: {type(exc).__name__}: {exc}"}]}
            result["isError"] = True
        finally:
            ticking.set()
            with self.tools.calls_lock:
                self.tools.calls.pop(key, None)
        if not call.silent:
            self.answer(call.rpc_id, result, modern)

    def progress(self, call, token, done):
        """While a call runs, say how it stands every PROGRESS_SECONDS, so the client knows it is not stuck."""
        count = 0
        while not done.wait(PROGRESS_SECONDS):
            count += 1
            # Only a clip the Lab is making asks the Lab how it stands: before then there may be no Lab to ask.
            if call.state == "making":
                message = self.making_message(call)
            else:
                message = {
                    "queued": "Waiting for the clip before this one to finish.",
                    "starting": "Starting Fel's Voice Lab.",
                    "playing": "Playing the clip.",
                }.get(call.state, "Working.")
            if done.is_set():
                return
            self.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progressToken": token, "progress": count, "message": message},
                }
            )

    def making_message(self, call):
        try:
            queue = self.tools.lab.get("/api/queue")
        except ToolError:
            return "The Lab is making the clip."
        waiting = [item.get("id") for item in queue.get("waiting") or []]
        if call.lab_request in waiting:
            ahead = waiting.index(call.lab_request) + (1 if queue.get("running") else 0)
            return f"Waiting in the Lab's line: {ahead} ahead."
        return "The Lab is making the clip (the first one also loads the voice's model)."


def main(argv=None):
    parser = argparse.ArgumentParser(description="An MCP server that gives apps a voice from Fel's Voice Lab.")
    parser.add_argument("--url", help="the Lab's address; then this server never starts the Lab itself")
    parser.add_argument("--no-start", action="store_true", help="do not start the Lab's service when it is not running")
    parser.add_argument("--workspace", type=Path, help="the directory allowed for selected file tools")
    parser.add_argument("--http", type=int, metavar="PORT", help="serve authenticated MCP on a loopback port")
    parser.add_argument("--token-file", type=Path, help="a local file for the HTTP connection token")
    parser.add_argument("--clips", type=Path, help="where clips go (default: out/spoken in the Lab's folder)")
    args = parser.parse_args(argv)
    output = sys.stdout.buffer
    sys.stdout = sys.stderr  # a stray print goes to the log, never into the protocol
    lab = Lab(url=args.url.rstrip("/") if args.url else None, can_start=not args.no_start)
    if args.http is not None:
        if args.workspace is None:
            parser.error("--http requires an explicit --workspace directory")
        from mcp_http import run

        return run(args, lab, parser)
    server = Server(Tools(lab, Player(), clips=args.clips, workspace=args.workspace), output)
    try:
        server.serve(sys.stdin.buffer)
    except KeyboardInterrupt:
        server.tools.stop_all()
        lab.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
