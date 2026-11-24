"""Run the four sequences of calls the other apps of Fel's Studio make to Voice Lab, as docs/CONNECT.md writes them down (For
Fel's other apps), against a running Lab, and say whether each one works.

    .venv\\Scripts\\python.exe tools\\suite_api_check.py
    .venv\\Scripts\\python.exe tools\\suite_api_check.py --url http://127.0.0.1:8775 --to http://127.0.0.1:8776

1. Speak with a voice by name: POST /v1/audio/speech, then POST /api/speak.
2. Design a voice from words and keep it as a Cast voice: POST /api/characters/from-design, then speak with it by name.
3. A Cast voice cloned from a recording that a program uploads: POST /api/characters, /api/characters/{id}/identities,
   /api/identities/{id}/references and /api/identities/{id}/update, then speak with it. The recording is a clip the Lab
   speaks first with a preset voice, so no person's voice is used.
4. A voice file out of one Lab and into another: a take of the designed voice (POST /api/performance), POST
   /api/portable/export and GET /api/portable/{id}.zip; then POST /api/portable/preview and /import on the other Lab (--to,
   else the same Lab), the approval, and a clip in the imported voice.

Every answer is also held to the fields the suite API promises (api_spec.py, tests/suite_api_<N>.json).

It needs a Lab with a preset voice (Kokoro), a model that designs voices (Qwen3-TTS VoiceDesign) and one that clones from a
recording (Qwen3-TTS Base, or Chatterbox Turbo). The first calls load those models, which can take minutes. It makes characters named "Suite check"
and a short code, and retires and purges them at the end unless --keep is given; the voice file it exports stays in the
Lab's out/portable folder. It unloads nothing and changes nothing else. Exit status 0 when all four sequences work.

Standard library only: tests/test_suite_api.py runs the same sequences against a stand-in Lab.
"""

import argparse
import base64
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api_spec  # noqa: E402 - beside this folder, standard library only
import lab_address  # noqa: E402
from app_version import SUITE_API  # noqa: E402

# How long a model call waits in line for the model worker: the first one loads its model.
WAIT_SECONDS = 1800
SPEAK_LINE = "Hello from the suite check of Fel's Studio."
DESIGN_WORDS = "A calm, warm voice of a woman in her thirties, unhurried, speaking clearly in a quiet room."
RECORDING_LINE = "Every program on this computer can ask the Lab for a voice, and this recording shows how one is cloned."
AUDITION_LINE = "This is how the voice sounds in a voice file."
OCTETS = {"Content-Type": "application/octet-stream"}


class CheckFailed(Exception):
    """A sequence did not work: the sentence says where."""


class Answer:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def json(self):
        return json.loads(self.body.decode("utf-8"))


def metadata_header(fields):
    """The X-Reference-Metadata header: the fields as base64url-encoded JSON."""
    return base64.urlsafe_b64encode(json.dumps(fields).encode("utf-8")).decode("ascii").rstrip("=")


class Lab:
    """One Lab's HTTP API, called as a program on this computer calls it. `calls` keeps each call's route, as api_spec names
    it, and its JSON answer, for answer_problems()."""

    def __init__(self, url, timeout=WAIT_SECONDS + 120):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.calls = []
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this computer: no proxy

    def call(self, method, route, *, path=None, body=None, data=None, headers=None, expect=200):
        """Call `route` (a path from api_spec, its {parameters} filled from `path`) with a JSON `body` or raw `data`. The
        answer, or CheckFailed when its status is not `expect`."""
        target = self.url + route.format(
            **{name: urllib.parse.quote(str(value), safe="") for name, value in (path or {}).items()}
        )
        sent = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            sent["Content-Type"] = "application/json"
        request = urllib.request.Request(target, data=data, method=method, headers=sent)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                answer = Answer(response.status, response.headers, response.read())
        except urllib.error.HTTPError as error:
            answer = Answer(error.code, error.headers, error.read())
        except OSError as error:
            raise CheckFailed(f"{method} {route} reached no Lab at {self.url}: {error}.") from error
        parsed = None
        if (answer.headers.get("Content-Type") or "").startswith("application/json"):
            try:
                parsed = answer.json
            except ValueError:
                parsed = None
        self.calls.append((f"{method} {route}", answer.status, parsed))
        if answer.status != expect:
            said = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(said, dict):  # the OpenAI-style routes' shape
                said = said.get("message")
            raise CheckFailed(
                f"{method} {route} answered {answer.status}" + (f": {said}" if said else ".") + f" (expected {expect})"
            )
        return answer


def wav(answer, route):
    if not (answer.body[:4] == b"RIFF" and answer.body[8:12] == b"WAVE"):
        raise CheckFailed(f"{route} answered something that is not a WAV file.")
    return answer.body


def ready_voice(lab, identity_id):
    """The voice /api/voices lists for a Cast voice, by its identity's id."""
    for voice in lab.call("GET", "/api/voices").json["voices"]:
        if voice.get("identity_id") == identity_id:
            return voice
    raise CheckFailed("GET /api/voices does not list the new voice.")


def speak(lab, voice_name, text):
    clip = lab.call(
        "POST",
        "/v1/audio/speech",
        body={"voice": voice_name, "input": text, "response_format": "wav", "wait_seconds": WAIT_SECONDS},
    )
    return wav(clip, "POST /v1/audio/speech")


def recognise(lab):
    """The Lab's version, after checking that `lab` is a Voice Lab with this suite API."""
    fels = lab.call("GET", "/api/health").json.get("fels") or {}
    if fels.get("app") != "voice-lab":
        raise CheckFailed(f"{lab.url} is not Voice Lab: its /api/health has no fels block naming voice-lab.")
    if fels.get("api") != SUITE_API:
        raise CheckFailed(
            f"{lab.url} is Voice Lab {fels.get('version')} with suite API {fels.get('api')}; this check is "
            f"written for suite API {SUITE_API}. Run the check of that Lab's own folder."
        )
    return fels.get("version")


def speak_by_name(lab, wanted=None):
    """Sequence 1: a voice by name from GET /api/voices, through POST /v1/audio/speech and POST /api/speak."""
    voices = [voice for voice in lab.call("GET", "/api/voices").json["voices"] if voice["ready"]]
    if wanted:
        found = [
            voice for voice in voices if wanted.casefold() in {name.casefold() for name in (voice["name"], *voice["aliases"])}
        ]
        if not found:
            raise CheckFailed(f"No ready voice is called {wanted!r}. GET /api/voices lists them.")
        voice = found[0]
    else:
        presets = sorted(
            (voice for voice in voices if voice["kind"] == "preset"),
            key=lambda voice: (voice.get("voice_id") != "af_heart", voice["model"] != "kokoro"),
        )
        if not presets:
            raise CheckFailed(
                "No preset voice is ready. Install Kokoro 82M in Models & features, or name a voice with --voice."
            )
        voice = presets[0]
    speak(lab, voice["name"], SPEAK_LINE)
    body = {"text": SPEAK_LINE, "voice": voice["name"], "wait_seconds": WAIT_SECONDS}
    if voice["takes_direction"]:
        body["direction"] = "Cheerfully, as if greeting a friend."
    wav(lab.call("POST", "/api/speak", body=body), "POST /api/speak")
    return voice


def design_voice(lab, name, designer=None, speaker=None):
    """Sequence 2: POST /api/characters/from-design, then a clip in the new voice by its name."""
    body = {"name": name, "voice_description": DESIGN_WORDS, "wait_seconds": WAIT_SECONDS}
    if designer:
        body["engine"] = designer
    if speaker:
        body["clone_engine"] = speaker
    made = lab.call("POST", "/api/characters/from-design", body=body, expect=201).json
    if not made["voice"]["ready"]:
        raise CheckFailed(f"The designed voice is not ready: {made['voice']['why_not']}")
    speak(lab, made["voice"]["name"], SPEAK_LINE)
    return made


def clone_recording(lab, name, recorder, speaker, made):
    """Sequence 3: a recording a program uploads (here, a clip of the preset voice `recorder`), as a Cast voice. The new
    character's id goes into `made` as soon as it exists."""
    recording = speak(lab, recorder["name"], RECORDING_LINE)
    character = lab.call(
        "POST", "/api/characters", body={"name": name, "description": "Made by the suite check."}, expect=201
    ).json["character"]
    made.append(character["id"])
    identity = lab.call(
        "POST",
        "/api/characters/{character_id}/identities",
        path={"character_id": character["id"]},
        body={"engine_id": speaker, "label": "Recorded"},
        expect=201,
    ).json["identity"]
    terms = {
        "filename": "recording.wav",
        "transcript": RECORDING_LINE,
        "origin": f"Spoken by the preset voice {recorder['name']} ({recorder['model_name']}) for the suite check; "
        "no person's recording.",
        "allowed_use": "Testing Voice Lab only.",
    }
    reference = lab.call(
        "POST",
        "/api/identities/{identity_id}/references",
        path={"identity_id": identity["id"]},
        data=recording,
        headers={**OCTETS, "X-Reference-Metadata": metadata_header(terms)},
        expect=201,
    ).json["reference"]
    lab.call(
        "POST",
        "/api/identities/{identity_id}/update",
        path={"identity_id": identity["id"]},
        body={"status": "approved", "reference_version_id": reference["id"]},
    )
    voice = ready_voice(lab, identity["id"])
    if not voice["ready"]:
        raise CheckFailed(f"The cloned voice is not ready: {voice['why_not']}")
    speak(lab, voice["name"], SPEAK_LINE)
    return voice


def carry_voice_file(lab, other, identity_id, made, into=None):
    """Sequence 4: the designed voice as a voice file, out of `lab` and into `other`, as a new character named as in the file,
    or added to a new character named `into` (the way into the same Lab, where the file's name is taken). The characters it
    makes go into `made`. Its voice as other's GET /api/voices lists it."""
    take = lab.call(
        "POST",
        "/api/performance",
        body={
            "identity_id": identity_id,
            "text": AUDITION_LINE,
            "line_id": "suite-check-audition",
            "wait_seconds": WAIT_SECONDS,
        },
    )
    wav(take, "POST /api/performance")
    if not take.headers.get("X-Take-Id"):
        raise CheckFailed("POST /api/performance kept no take (no X-Take-Id).")
    archive = lab.call("POST", "/api/portable/export", body={"kind": "persona", "identity_id": identity_id}, expect=201).json[
        "archive"
    ]
    if archive["download"] != f"/api/portable/{archive['id']}.zip":
        raise CheckFailed(f"POST /api/portable/export names an unexpected download: {archive['download']}.")
    raw = lab.call("GET", "/api/portable/{delivery_id}.zip", path={"delivery_id": archive["id"]}).body
    preview = other.call("POST", "/api/portable/preview", data=raw, headers=OCTETS).json["archive"]
    terms = {"archive_sha256": preview["archive_sha256"], "permission": True}
    if into:
        character = other.call("POST", "/api/characters", body={"name": into}, expect=201).json["character"]
        made.append(character["id"])
        terms["character_id"] = character["id"]
    imported = other.call(
        "POST", "/api/portable/import", data=raw, headers={**OCTETS, "X-Reference-Metadata": metadata_header(terms)}, expect=201
    ).json["imported"]
    identity = imported["identity"]
    if identity["character_id"] not in made:
        made.append(identity["character_id"])
    if identity["status"] != "candidate" or not identity["references"]:
        raise CheckFailed("The imported voice is not a candidate with its recording.")
    other.call(
        "POST",
        "/api/identities/{identity_id}/update",
        path={"identity_id": identity["id"]},
        body={"status": "approved", "reference_version_id": identity["references"][0]["id"]},
    )
    voice = ready_voice(other, identity["id"])
    if voice["ready"]:
        speak(other, voice["name"], SPEAK_LINE)
    return voice


def _value_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "string[]" if value and all(isinstance(item, str) for item in value) else "array"
    return "object"


def _fits(value, kind):
    base, _, null = kind.partition("|")
    found = _value_type(value)
    if found == "null":
        return bool(null)
    if base == "number":
        return found in ("number", "integer")
    if base == "array":
        return found in ("array", "string[]")
    if base == "string[]":
        return found == "string[]" or value == []
    return found == base


def values_at(answer, name):
    """Every value the dotted field `name` (api_spec's notation: `voices[].name`) has in a JSON answer."""
    found = [answer]
    for part in name.split("."):
        listed = part.endswith("[]")
        field = part[:-2] if listed else part
        found = [item[field] for item in found if isinstance(item, dict) and field in item]
        if listed:
            found = [member for item in found if isinstance(item, list) for member in item]
    return found


def answer_problems(labs, everything=False, complete=True):
    """Sentences for each promised answer field (api_spec's suite routes) that a recorded answer gave with another type, or,
    when the calls were `complete` (every sequence ran), that no answer of its route gave at all. With `everything`, a
    suite route no call reached is one too."""
    promised = {api_spec.key(route): route.answer for route in api_spec.suite_routes()}
    seen, problems = {}, []
    for lab in labs:
        for route, status, answer in lab.calls:
            if route not in promised or not 200 <= status < 300:
                continue
            seen.setdefault(route, set())
            for name, kind in promised[route]:
                values = values_at(answer, name)
                if values:
                    seen[route].add(name)
                for value in values:
                    if not _fits(value, kind):
                        problems.append(f"{route} answered {name} as {_value_type(value)}, not {kind}.")
                        break
    for route, fields in promised.items():
        if not complete:
            break
        if route not in seen:
            if everything:
                problems.append(f"No call reached {route}.")
            continue
        problems += [f"{route} never answered {name}." for name, _ in fields if name not in seen[route]]
    return problems


def clean_up(lab, character_ids):
    """Retire and purge the characters the check made, with what was made from them."""
    for character_id in character_ids:
        lab.call(
            "POST",
            "/api/retirements",
            body={"kind": "character", "id": character_id, "reason": "Made by the suite check."},
            expect=201,
        )
        lab.call(
            "POST",
            "/api/purges",
            body={"kind": "character", "id": character_id, "confirm": True, "reason": "Made by the suite check."},
        )


def run(lab, other=None, *, voice=None, designer=None, speaker=None, keep=False, say=print):
    """Run the four sequences against `lab` (and `other` for the voice file's way in) and say how each went. True when all
    four work and every answer has its promised fields."""
    other = other or lab
    code = secrets.token_hex(2)
    made = {lab: [], other: []}
    results = []

    def step(number, title, work):
        started = time.monotonic()
        try:
            detail = work()
        except CheckFailed as failure:
            say(f"{number}. {title}: failed. {failure}")
            results.append(False)
            return None
        say(f"{number}. {title}: works ({time.monotonic() - started:.1f} s). {detail[0]}")
        results.append(True)
        return detail[1]

    try:
        version = recognise(lab)
        if other is not lab:
            recognise(other)
    except CheckFailed as failure:
        say(str(failure))
        return False
    say(
        f"Voice Lab {version} at {lab.url}, suite API {SUITE_API}."
        + (f" The voice file goes into {other.url}." if other is not lab else "")
    )

    def first():
        found = speak_by_name(lab, voice)
        return f"{found['name']} ({found['model_name']}).", found

    preset = step(1, "Speak with a voice by name", first)

    def second():
        answer = design_voice(lab, f"Suite check {code} designed", designer, speaker)
        made[lab].append(answer["character_id"])
        return (f"{answer['voice']['name']}, designed by {answer['engine']} and spoken by {answer['clone_engine']}.", answer)

    designed = step(2, "Design a voice from words and keep it in Cast", second)

    def third():
        if preset is None:
            raise CheckFailed("It needs the preset voice of sequence 1, to make the recording.")
        cloner = speaker or (designed["clone_engine"] if designed else "qwen3-tts-1.7b-base")
        found = clone_recording(lab, f"Suite check {code} cloned", preset, cloner, made[lab])
        return f"{found['name']}, cloned by {cloner}.", found

    step(3, "A Cast voice from an uploaded recording", third)

    def fourth():
        if designed is None:
            raise CheckFailed("It needs the designed voice of sequence 2.")
        # Into the same Lab, the file's character name is taken already, so the copy goes to a character of its own.
        into = f"Suite check {code} imported" if other is lab else None
        found = carry_voice_file(lab, other, designed["voice"]["identity_id"], made[other], into)
        spoken = "spoke in it." if found["ready"] else f"cannot speak it: {found['why_not']}"
        return f"{found['name']} arrived as a candidate, was approved, and the Lab {spoken}", found

    step(4, "A voice file into another Lab" if other is not lab else "A voice file out of the Lab and back in", fourth)

    # A field only some answers carry (a Cast voice's `character`) is missed for certain only when every sequence ran.
    problems = answer_problems([lab] if other is lab else [lab, other], complete=all(results))
    for problem in problems:
        say(f"Answer fields: {problem}")
    if not keep:
        for where, ids in made.items():
            try:
                clean_up(where, ids)
            except CheckFailed as failure:
                say(f"Clean-up: {failure} Remove the Suite check characters in Cast by hand.")
        count = sum(len(ids) for ids in made.values())
        if count:
            say(f"Cleaned up: {count} Suite check character{'s' if count != 1 else ''} retired and purged.")
    worked = all(results) and not problems
    say("All four sequences work." if worked else "Not everything works; see above.")
    return worked


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--url",
        help="the Lab to check (default: $VOICE_LAB_URL, else the address file a running Lab "
        "writes, else http://127.0.0.1:8765)",
    )
    parser.add_argument("--to", help="another Lab, to carry the voice file into (default: the same Lab)")
    parser.add_argument("--voice", help="the voice of sequence 1 (default: a ready preset voice, Kokoro's af_heart first)")
    parser.add_argument("--design-engine", help="the model that designs the voice (default: the Lab's, Qwen3-TTS VoiceDesign)")
    parser.add_argument("--clone-engine", help="the model that speaks the designed and the cloned voice (default: the Lab's)")
    parser.add_argument("--keep", action="store_true", help="keep the Suite check characters instead of purging them")
    args = parser.parse_args(argv)
    url, _source = lab_address.find_url(args.url, os.environ, default=lab_address.KNOWN_URL)
    lab = Lab(url)
    other = Lab(args.to) if args.to else None
    worked = run(lab, other, voice=args.voice, designer=args.design_engine, speaker=args.clone_engine, keep=args.keep)
    return 0 if worked else 1


if __name__ == "__main__":
    sys.exit(main())
