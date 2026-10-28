"""Talk to a running Voice Lab service from a terminal or a script.

    python tools\\lab_cli.py health
    python tools\\lab_cli.py speak --text "Hello from the Lab." --out hello.wav
    python tools\\lab_cli.py cast
    python tools\\lab_cli.py perform --character Nell --text "We made it home." --line-id scene1-001 --out nell.wav

Each command makes one call to the service, which docs/API.md describes. The service is the one at --url, else the
VOICE_LAB_URL environment variable, else http://127.0.0.1:8765 (start it with serve.py; the desktop app picks a port of its
own). This file uses only the standard library, so any Python 3.8 or later runs it, with or without the Lab's packages.

Exit status: 0 when the call worked; 1 when the service refused it or failed (its message goes to standard error); 2 for a
mistake in the command line; 3 when nothing answered.
"""
import argparse
import http.client
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8765"
QUICK_SECONDS = 10           # health and cast answer at once
GENERATE_SECONDS = 900       # a first call also loads the model; a long line takes a while after that
# What the service says about a take, as the keys a --json answer uses.
FACTS = {"x-audio-seconds": "audio_seconds", "x-gen-seconds": "gen_seconds", "x-take-id": "take_id",
         "x-cast-character": "character", "x-cast-identity": "voice", "x-cast-line-id": "line_id",
         "x-voice-backend": "backend", "x-model-checkpoint": "checkpoint", "x-checkpoint-revision": "checkpoint_revision",
         "x-generation-mode": "mode", "x-idempotent-replay": "replay", "x-voice-asset-path": "voice_asset"}


class Failed(Exception):
    """The call did not work. The message is for the person at the terminal."""


class Refused(Failed):
    def __init__(self, status, message):
        super().__init__(f"the service answered {status}: {message}")
        self.status = status


class NoAnswer(Failed):
    pass


class Usage(Exception):
    pass


# ----------------------------------------------------------------------------------------------------------- the call

def _message(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            return value["error"]
    except ValueError:
        pass
    return raw.decode("utf-8", "replace").strip()[:300] or "no message"


def call(base, method, path, *, body=None, timeout):
    """One request. Returns (headers, bytes); headers are lower-case names with text the service percent-encoded decoded."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data is not None else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # the service is on this computer: no proxy
    try:
        with opener.open(request, timeout=timeout) as response:
            return read_headers(response.headers), response.read()
    except urllib.error.HTTPError as error:
        raise Refused(error.code, _message(error.read())) from None
    except (urllib.error.URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise NoAnswer(f"no answer from {base} within {timeout:g} seconds (--timeout changes this)") from None
        raise NoAnswer(f"no answer from {base}: {reason}. Is the service running?") from None
    except http.client.HTTPException as error:
        raise NoAnswer(f"the connection to {base} ended before the answer was complete ({error})") from None


def read_headers(headers):
    result = {key.lower(): value for key, value in headers.items()}
    marker = result.get("x-metadata-encoding", "")
    if marker.startswith("percent-encoded UTF-8:"):
        for name in marker.split(":", 1)[1].split(","):
            name = name.strip().lower()
            if name in result:
                result[name] = urllib.parse.unquote(result[name])
    return result


def get_json(base, path, timeout):
    return json.loads(call(base, "GET", path, timeout=timeout)[1].decode("utf-8"))


def save(path, data):
    """Write the file under a temporary name and move it into place, so a failure leaves no half file."""
    path = Path(path)
    partial = path.with_name(path.name + ".part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(data)
        os.replace(partial, path)
    except OSError as error:
        raise Failed(f"cannot write {path}: {error.strerror or error}") from None
    finally:
        if partial.exists():
            partial.unlink()


def save_wav(path, data):
    if not (data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
        raise Failed("the service's answer is not a WAV file, so nothing was written")
    save(path, data)


# ------------------------------------------------------------------------------------------------------- what to send

def parse_pairs(items):
    """KEY=VALUE pairs; a value that is valid JSON (7, 0.9, true, {"a": 1}) is read as that, anything else as text."""
    result = {}
    for item in items or ():
        key, separator, value = item.partition("=")
        if not key or not separator:
            raise Usage(f"{item!r} is not KEY=VALUE")
        try:
            result[key] = json.loads(value)
        except ValueError:
            result[key] = value
    return result


def read_text(args):
    if args.text is not None:
        return args.text
    if args.text_file:
        raw = sys.stdin.read() if args.text_file == "-" else Path(args.text_file).read_text(encoding="utf-8-sig")
        return raw.strip()
    return None


def read_request_file(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise Usage(f"cannot read the request file {path}: {error}") from None
    if not isinstance(value, dict):
        raise Usage(f"the request file {path} must hold a JSON object")
    return value


def resolve_voice(base, character, voice, timeout):
    """The identity id for a character's name, and its voice's label when it has several."""
    characters = get_json(base, "/api/cast", timeout).get("characters", [])
    named = [item for item in characters if item["name"].casefold() == character.casefold() and not item.get("retired")]
    if not named:
        known = ", ".join(sorted(item["name"] for item in characters if not item.get("retired"))) or "none yet"
        raise Usage(f"there is no character called {character!r}. Characters: {known}")
    if len(named) > 1:
        raise Usage(f"more than one character is called {character!r}; give --identity-id instead")
    usable = [item for item in named[0]["identities"] if not item.get("retired") and item["status"] != "rejected"]
    if not usable:
        raise Usage(f"{named[0]['name']} has no voice that can be used")
    if voice:
        chosen = [item for item in usable if item["label"].casefold() == voice.casefold()]
    else:
        chosen = usable if len(usable) == 1 else [item for item in usable if item["status"] == "approved"]
    if len(chosen) != 1:
        labels = "; ".join(f"{item['label']} ({item['engine_id']}, {item['status']})" for item in usable)
        raise Usage(f"{named[0]['name']} has these voices: {labels}. Choose one with --voice LABEL")
    return chosen[0]["id"]


# ------------------------------------------------------------------------------------------------------- the commands

def summarize(path, data, headers):
    facts = {}
    for header, key in FACTS.items():
        value = headers.get(header)
        if value:
            facts[key] = value
    for key in ("audio_seconds", "gen_seconds"):
        if key in facts:
            try:
                facts[key] = float(facts[key])
            except ValueError:
                del facts[key]
    if "replay" in facts:
        facts["replay"] = facts["replay"] == "true"
    return {"file": str(path), "bytes": len(data), **facts}


def say_wrote(result):
    details = []
    if "audio_seconds" in result:
        details.append(f"{result['audio_seconds']:.2f} s of audio")
    if "character" in result:
        details.append(f"as {result['character']}" + (f" ({result['voice']})" if "voice" in result else ""))
    if "take_id" in result:
        details.append(f"take {result['take_id']}" + (", an earlier take replayed" if result.get("replay") else ""))
    if "gen_seconds" in result and not result.get("replay"):
        details.append(f"made in {result['gen_seconds']:.2f} s")
    print(f"Wrote {result['file']}" + (": " + ", ".join(details) if details else f" ({result['bytes']} bytes)"))


def finish(args, path, data, headers):
    result = summarize(path, data, headers)
    if args.json:
        print(json.dumps(result))
    else:
        say_wrote(result)
    return 0


def health(args, base):
    deadline = time.monotonic() + args.wait
    while True:
        try:
            info = get_json(base, "/api/health", args.timeout)
            break
        except NoAnswer:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
    if args.json:
        print(json.dumps(info))
    else:
        print(f"Voice Lab {info.get('version', '?')} is {info.get('status', '?')} at {base} (device: {info.get('device', '?')})")
    return 0


def speak(args, base):
    body = read_request_file(args.request_file) if args.request_file else {}
    text = read_text(args)
    if text is not None:
        body["text"] = text
    for key in ("engine", "voice", "speed", "language"):
        if getattr(args, key) is not None:
            body[key] = getattr(args, key)
    body.update(parse_pairs(args.set))
    if not str(body.get("text", "")).strip():
        raise Usage("give --text or --text-file (or a request file with a text)")
    headers, data = call(base, "POST", "/api/speak", body=body, timeout=args.timeout)
    save_wav(args.out, data)
    return finish(args, args.out, data, headers)


def perform(args, base):
    text = read_text(args)
    if text is None or not text.strip():
        raise Usage("give --text or --text-file")
    if bool(args.identity_id) == bool(args.character):
        raise Usage("name the voice with --character NAME (and --voice LABEL), or with --identity-id")
    body = {"identity_id": args.identity_id or resolve_voice(base, args.character, args.voice, min(args.timeout, QUICK_SECONDS)),
            "line_id": args.line_id, "text": text}
    for key in ("reference_version_id", "direction", "language", "idempotency_key"):
        if getattr(args, key) is not None:
            body[key] = getattr(args, key)
    if args.setting:
        body["settings"] = parse_pairs(args.setting)
    if args.pronunciations:
        body["use_pronunciations"] = True
    headers, data = call(base, "POST", "/api/performance", body=body, timeout=args.timeout)
    save_wav(args.out, data)
    return finish(args, args.out, data, headers)


def cast(args, base):
    data = get_json(base, "/api/cast", args.timeout)
    if args.json:
        print(json.dumps(data))
        return 0
    if not data.get("characters"):
        print("The Cast is empty.")
    for character in data.get("characters", []):
        print(character["name"] + ("  (retired)" if character.get("retired") else ""))
        for identity in character["identities"]:
            notes = [identity["engine_id"], identity["status"]] + (["retired"] if identity.get("retired") else [])
            print(f"  {identity['label']}  ({', '.join(notes)})  identity {identity['id']}")
    return 0


# --------------------------------------------------------------------------------------------------------- the parser

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS,
                        help=f"where the service listens (default: $VOICE_LAB_URL, else {DEFAULT_URL})")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS, metavar="SECONDS",
                        help=f"how long to wait for an answer (default: {QUICK_SECONDS} for health and cast, "
                             f"{GENERATE_SECONDS} for speak and perform)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     parents=[common])
    commands = parser.add_subparsers(dest="command", required=True, metavar="{health,speak,perform,cast}")

    def add(name, handler, summary):
        sub = commands.add_parser(name, help=summary, description=summary, parents=[common])
        sub.set_defaults(handler=handler)
        sub.add_argument("--json", action="store_true", help="print one JSON object instead of a sentence")
        return sub

    sub = add("health", health, "Ask whether the service is up.")
    sub.add_argument("--wait", type=float, default=0, metavar="SECONDS", help="keep trying this long until it answers")

    def text_arguments(sub):
        sub.add_argument("--text", help="what to say, 1 to 1200 characters")
        sub.add_argument("--text-file", metavar="FILE", help="read the text from a file (- for standard input)")
        sub.add_argument("--out", required=True, metavar="FILE", help="where to write the WAV file")

    sub = add("speak", speak, "Speak text with one engine and write the WAV file (POST /api/speak).")
    text_arguments(sub)
    sub.add_argument("--engine", help="an engine id from the catalog (the service's default is kokoro)")
    sub.add_argument("--voice", help="Kokoro: a voice name, or several joined with commas")
    sub.add_argument("--speed", type=float, help="Kokoro: speaking rate")
    sub.add_argument("--language", help="the language, where the engine takes one")
    sub.add_argument("--set", action="append", metavar="KEY=VALUE",
                     help="any other field of the request, for example --set seed=7; repeatable")
    sub.add_argument("--request-file", metavar="FILE", help="a JSON file holding the whole request body")

    sub = add("perform", perform, "Speak a line as a Cast voice, save the take and write the WAV file (POST /api/performance).")
    text_arguments(sub)
    sub.add_argument("--character", metavar="NAME", help="the character, by name")
    sub.add_argument("--voice", metavar="LABEL", help="the voice's label, when the character has more than one")
    sub.add_argument("--identity-id", metavar="ID", help="the voice by id instead (see the cast command)")
    sub.add_argument("--line-id", default="cli", metavar="ID", help="your label for the line (default: cli)")
    sub.add_argument("--reference-version-id", metavar="ID", help="the recording to clone from, if not the approved one")
    sub.add_argument("--direction", help="how to deliver the line")
    sub.add_argument("--language", help="the language")
    sub.add_argument("--setting", action="append", metavar="KEY=VALUE", help="an engine setting; repeatable")
    sub.add_argument("--pronunciations", action="store_true", help="apply the character's pronunciation dictionary")
    sub.add_argument("--idempotency-key", metavar="KEY", help="a repeat with the same key returns the same take")

    add("cast", cast, "List the characters and voices with their ids.")
    return parser


def normalize_url(url):
    if not url.lower().startswith(("http://", "https://")):
        raise Usage(f"{url!r} is not a web address; it should look like {DEFAULT_URL}")
    return url.rstrip("/")


def timeout_for(args):
    """--timeout when it was given, else how long this command may wait: a read answers at once, speaking takes a while."""
    given = getattr(args, "timeout", None)
    if given is not None:
        return given
    return QUICK_SECONDS if args.command in ("health", "cast") else GENERATE_SECONDS


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        base = normalize_url(getattr(args, "url", None) or os.environ.get("VOICE_LAB_URL") or DEFAULT_URL)
        args.timeout = timeout_for(args)
        return args.handler(args, base)
    except Usage as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except NoAnswer as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except Failed as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
