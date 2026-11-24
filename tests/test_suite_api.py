"""The Lab's suite API (contract 11 of Fel's Studio): the routes NPC Forge, Altera and other programs rely on keep their fields.

tests/suite_api_<SUITE_API>.json holds what those routes take and answer, as api_spec.py says. A change to a field of one of
them fails here until the snapshot is written again by tools/build_api_docs.py, which writes an addition and refuses a change
that would break a program written for the snapshot: that needs a new SUITE_API. tools/suite_api_check.py runs the four
sequences of docs/CONNECT.md against two stand-in Labs, and every answer is held to its promised fields, so a field renamed in
the code fails too. POST /api/characters/from-design, the one route made for this, is tested on its own.
"""

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_service  # noqa: E402
from voice_lab import api_spec  # noqa: E402
from voice_lab import kokoro_support  # noqa: E402
from voice_lab import voice_names  # noqa: E402
from voice_lab.route_groups import cast as cast_routes  # noqa: E402
from tools import build_api_docs, suite_api_check  # noqa: E402

SNAPSHOT = ROOT / "tests" / f"suite_api_{api_spec.SUITE_API}.json"
DESIGNER, SPEAKER, SMALL_SPEAKER = "qwen3-tts-1.7b-voice-design", "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base"
# What contract 11 lists, and the route Lab step 4 added for designing a voice in one call.
CONTRACT_ROUTES = {
    "GET /api/health",
    "GET /api/voices",
    "POST /v1/audio/speech",
    "POST /api/speak",
    "POST /api/characters",
    "POST /api/characters/{character_id}/identities",
    "POST /api/identities/{identity_id}/references",
    "POST /api/portable/export",
    "POST /api/portable/preview",
    "POST /api/portable/import",
    "POST /api/unload",
    "POST /api/characters/from-design",
}


def frozen():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def snapshot_of(routes):
    with mock.patch.object(api_spec, "ROUTES", routes):
        return api_spec.suite_snapshot()


def changed(key, **fields):
    """ROUTES with the row `key` changed."""
    return [replace(route, **fields) if api_spec.key(route) == key else route for route in api_spec.ROUTES]


def row(key):
    return next(route for route in api_spec.ROUTES if api_spec.key(route) == key)


class SnapshotTests(unittest.TestCase):
    def test_the_snapshot_is_what_the_route_table_gives(self):
        breaking, added = api_spec.suite_changes(frozen(), api_spec.suite_snapshot())
        self.assertEqual(
            breaking,
            [],
            f"suite API {api_spec.SUITE_API} would break for the other apps of Fel's Studio. Keep "
            "the old names and types, or raise SUITE_API (tools/build_api_docs.py says how).",
        )
        self.assertEqual(added, [], f"{SNAPSHOT.name} is out of date; run tools/build_api_docs.py to add these.")
        self.assertEqual(
            SNAPSHOT.read_text(encoding="utf-8"),
            build_api_docs.snapshot_text(),
            f"{SNAPSHOT.name} is out of date; run tools/build_api_docs.py",
        )

    def test_the_suite_api_holds_what_contract_11_lists(self):
        marked = {api_spec.key(route) for route in api_spec.suite_routes()}
        self.assertLessEqual(CONTRACT_ROUTES, marked)
        self.assertEqual(set(frozen()["routes"]), marked)
        self.assertEqual(frozen()["suite_api"], api_spec.SUITE_API)
        self.assertTrue(all(not route.internal for route in api_spec.suite_routes()), "a suite route is documented")

    def test_answer_fields_are_written_one_way(self):
        name = re.compile(r"[a-z_0-9]+(?:\[\])?(?:\.[a-z_0-9]+(?:\[\])?)*\Z")
        for route in api_spec.suite_routes():
            with self.subTest(api_spec.key(route)):
                self.assertEqual(bool(route.answer), route.reply_kind == "json", "a JSON answer lists its fields")
                names = [field for field, _ in route.answer]
                self.assertEqual(len(names), len(set(names)))
                for field, kind in route.answer:
                    self.assertRegex(field, name)
                    base, _, null = kind.partition("|")
                    self.assertIn(base, api_spec.ANSWER_TYPES)
                    self.assertIn(null, ("", "null"))
                    # A field is a leaf or the start of others, never both.
                    self.assertFalse(any(other.startswith(field + ".") or other.startswith(field + "[]") for other in names))

    def test_renaming_a_field_breaks_the_suite_api(self):
        voices = row("GET /api/voices")
        renamed_answer = tuple(
            ("voices[].reason", kind) if name == "voices[].why_not" else (name, kind) for name, kind in voices.answer
        )
        speech = row("POST /v1/audio/speech")
        renamed_body = tuple(replace(field, name="text") if field.name == "input" else field for field in speech.body)
        upload = row("POST /api/identities/{identity_id}/references")
        renamed_metadata = tuple(
            replace(field, name="terms") if field.name == "allowed_use" else field for field in upload.metadata
        )
        cases = {
            "an answer field": (
                changed("GET /api/voices", answer=renamed_answer),
                "GET /api/voices no longer answers voices[].why_not.",
            ),
            "a request field": (
                changed("POST /v1/audio/speech", body=renamed_body),
                "POST /v1/audio/speech no longer takes the field input.",
            ),
            "a metadata field": (
                changed("POST /api/identities/{identity_id}/references", metadata=renamed_metadata),
                "POST /api/identities/{identity_id}/references no longer takes the X-Reference-Metadata field allowed_use.",
            ),
            "a header": (
                changed("POST /api/performance", headers=()),
                "POST /api/performance no longer sends the header X-Take-Id.",
            ),
            "a type": (
                changed("GET /api/health", answer=(("fels.api", "string"),) + row("GET /api/health").answer[:-1]),
                "GET /api/health answers fels.api as string instead of integer.",
            ),
            "a route left out": (changed("POST /api/unload", suite=False), "POST /api/unload is no longer in the suite API."),
            "a status": (changed("POST /api/characters", status=200), "POST /api/characters answers 200 instead of 201."),
            "a field now needed": (
                changed(
                    "POST /api/characters/from-design",
                    body=tuple(
                        replace(field, required=True) if field.name == "text" else field
                        for field in row("POST /api/characters/from-design").body
                    ),
                ),
                "POST /api/characters/from-design needs text now.",
            ),
        }
        for label, (routes, sentence) in cases.items():
            with self.subTest(label):
                breaking, _added = api_spec.suite_changes(frozen(), snapshot_of(routes))
                self.assertIn(sentence, breaking)

    def test_an_addition_keeps_the_number(self):
        voices = row("GET /api/voices")
        routes = changed("GET /api/voices", answer=voices.answer + (("voices[].summary", "object"),))
        routes = [
            replace(route, body=route.body + (api_spec.F("pace", "number", "New."),))
            if api_spec.key(route) == "POST /api/speak"
            else route
            for route in routes
        ]
        routes = [
            replace(route, suite=True, answer=(("ok", "boolean"),)) if api_spec.key(route) == "POST /api/load" else route
            for route in routes
        ]
        breaking, added = api_spec.suite_changes(frozen(), snapshot_of(routes))
        self.assertEqual(breaking, [])
        self.assertEqual(
            sorted(added),
            sorted(
                [
                    "GET /api/voices answers a new field, voices[].summary.",
                    "POST /api/speak takes a new field, pace.",
                    "POST /api/load is new in the suite API.",
                ]
            ),
        )
        needed = [
            replace(route, body=route.body + (api_spec.F("pace", "number", "New.", True),))
            if api_spec.key(route) == "POST /api/speak"
            else route
            for route in api_spec.ROUTES
        ]
        self.assertEqual(api_spec.suite_changes(frozen(), snapshot_of(needed))[0], ["POST /api/speak needs a new field, pace."])

    def test_the_tool_adds_to_the_snapshot_and_refuses_to_break_it(self):
        renamed = changed(
            "GET /api/voices",
            answer=tuple(
                ("voices[].reason", kind) if name == "voices[].why_not" else (name, kind)
                for name, kind in row("GET /api/voices").answer
            ),
        )
        added = changed("GET /api/voices", answer=row("GET /api/voices").answer + (("voices[].summary", "object"),))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            folder = Path(temp)
            kept = folder / SNAPSHOT.name
            kept.write_text(SNAPSHOT.read_text(encoding="utf-8"), encoding="utf-8")
            unchanged = kept.read_text(encoding="utf-8")
            with (
                mock.patch.object(build_api_docs, "SNAPSHOTS", folder),
                mock.patch.object(build_api_docs, "OPENAPI", folder / "openapi.json"),
                mock.patch.object(build_api_docs, "GUIDE", folder / "API.md"),
                mock.patch.object(build_api_docs, "guide_text", lambda current: current),
            ):
                (folder / "API.md").write_text("guide\n", encoding="utf-8")
                with mock.patch.object(api_spec, "ROUTES", renamed), contextlib.redirect_stdout(io.StringIO()) as said:
                    self.assertEqual(build_api_docs.main(["--check"]), 1)
                    self.assertEqual(build_api_docs.main([]), 1)
                self.assertEqual(kept.read_text(encoding="utf-8"), unchanged, "a breaking change is not written")
                self.assertIn("GET /api/voices no longer answers voices[].why_not.", said.getvalue())
                self.assertIn(f"raise SUITE_API in app_version.py to {api_spec.SUITE_API + 1}", said.getvalue())
                self.assertIn(f"suite_api_{api_spec.SUITE_API + 1}.json", said.getvalue())
                self.assertTrue((folder / "openapi.json").is_file(), "the other files are written")
                with mock.patch.object(api_spec, "ROUTES", added), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(build_api_docs.main([]), 0)
                self.assertIn('"voices[].summary": "object"', kept.read_text(encoding="utf-8"))
                # A new number writes a new file beside the old one.
                with (
                    mock.patch.object(api_spec, "ROUTES", renamed),
                    mock.patch.object(api_spec, "SUITE_API", api_spec.SUITE_API + 1),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(build_api_docs.main([]), 0)
                    following = json.loads((folder / f"suite_api_{api_spec.SUITE_API}.json").read_text(encoding="utf-8"))
                self.assertIn("voices[].reason", following["routes"]["GET /api/voices"]["answer"]["fields"])
                self.assertIn('"voices[].summary": "object"', kept.read_text(encoding="utf-8"), "the old file stays")

    def test_openapi_json_says_which_routes_are_the_suite_api(self):
        spec = api_spec.build_openapi()
        self.assertEqual(spec["info"]["x-fels-suite-api"], api_spec.SUITE_API)
        marked = {
            f"{method.upper()} {path}"
            for path, methods in spec["paths"].items()
            for method, operation in methods.items()
            if operation.get("x-fels-suite-api") == api_spec.SUITE_API
        }
        self.assertEqual(marked, set(frozen()["routes"]))
        voices = spec["paths"]["/api/voices"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        item = voices["properties"]["voices"]["items"]["properties"]
        self.assertEqual(item["why_not"], {"type": ["string", "null"]})
        self.assertEqual(item["aliases"], {"type": "array", "items": {"type": "string"}})


class DesignEngines(api_service.FakeEngines):
    """The stand-in engines, with clips long enough to be a voice recording (`seconds`), the models the four sequences need
    installed (`installed`), and an unload."""

    def __init__(self, seconds=3.0):
        super().__init__()
        self.seconds = seconds
        self.installed = {"kokoro", DESIGNER, SPEAKER}
        self.install_probe = lambda entry: {"installed": entry["id"] in self.installed}
        self.unloads = 0

    def speak(self, request):
        _samples, rate, info = super().speak(request)
        axis = np.arange(round(self.seconds * rate), dtype=np.float32) / rate
        return (
            (0.1 * np.sin(2 * np.pi * 180 * axis)).astype(np.float32),
            rate,
            {**info, "checkpoint": "Qwen/stand-in", "checkpoint_revision": "stand-in"},
        )

    def unload(self):
        self.unloads += 1


class StandInLabTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(kokoro_support, "discover_kokoro_voices", lambda: ["af_heart", "ef_dora"]))

    def lab(self, **engines):
        return api_service.Service(self, DesignEngines(**engines))

    @staticmethod
    def characters(service):
        return [character for character in service.cast.list_cast() if not character.get("retired")]


class SequenceTests(StandInLabTest):
    def test_the_four_sequences_run_as_written_and_every_answer_has_its_fields(self):
        first, second = self.lab(), self.lab()
        lab, other = suite_api_check.Lab(first.base), suite_api_check.Lab(second.base)
        said = []
        self.assertTrue(suite_api_check.run(lab, other, say=said.append), "\n".join(said))
        self.assertEqual(len([line for line in said if ": works (" in line]), 4, said)
        lab.call("POST", "/api/unload", body={})
        self.assertEqual(first.engines.unloads, 1)
        self.assertEqual(suite_api_check.answer_problems([lab, other], everything=True), [])
        # It cleans up after itself in both Labs: what it made is purged.
        self.assertEqual((first.cast.list_cast(), second.cast.list_cast()), ([], []))
        # The designed voice was designed, then cloned by the Base model; the imported one speaks in the other Lab.
        design = [request for request in first.engines.requests if request.get("mode") == "design"]
        self.assertEqual(len(design), 1)
        self.assertEqual(design[0]["engine"], DESIGNER)
        self.assertTrue(any(request.get("engine") == SPEAKER for request in second.engines.requests))

    def test_with_keep_and_one_lab_the_voices_stay(self):
        service = self.lab()
        lab = suite_api_check.Lab(service.base)
        said = []
        self.assertTrue(suite_api_check.run(lab, keep=True, say=said.append), "\n".join(said))
        self.assertIn("4. A voice file out of the Lab and back in: works", "\n".join(said))
        names = sorted(character["name"] for character in self.characters(service))
        self.assertEqual(len(names), 3)
        self.assertTrue(all(name.startswith("Suite check ") for name in names))

    def test_a_field_renamed_in_the_code_fails_the_check(self):
        public = voice_names.public

        def renamed(voices):
            return [
                {("reason" if key == "why_not" else key): value for key, value in voice.items()} for voice in public(voices)
            ]

        service = self.lab()
        lab = suite_api_check.Lab(service.base)
        said = []
        with mock.patch.object(voice_names, "public", renamed):
            self.assertFalse(suite_api_check.run(lab, say=said.append))
        self.assertIn("Answer fields: GET /api/voices never answered voices[].why_not.", said)

    def test_a_lab_without_a_design_model_says_which_to_install(self):
        service = self.lab()
        service.engines.installed.discard(DESIGNER)
        said = []
        self.assertFalse(suite_api_check.run(suite_api_check.Lab(service.base), say=said.append))
        text = "\n".join(said)
        self.assertIn("2. Design a voice from words and keep it in Cast: failed.", text)
        self.assertIn("Qwen3-TTS 1.7B VoiceDesign is not installed", text)
        self.assertIn("4. A voice file out of the Lab and back in: failed. It needs the designed voice of sequence 2.", text)
        self.assertIn("3. A Cast voice from an uploaded recording: works", text)

    def test_something_that_is_not_the_lab_is_named(self):
        service = self.lab()
        said = []
        with mock.patch(
            "voice_lab.route_groups.engines.fels_suite.health_block", lambda *args: {"app": "npc-forge", "api": 45}
        ):
            self.assertFalse(suite_api_check.run(suite_api_check.Lab(service.base), say=said.append))
        self.assertIn("is not Voice Lab", said[0])


class FromDesignTests(StandInLabTest):
    def design(self, service, **fields):
        body = {"name": "Maya", "voice_description": "A bright, quick voice.", **fields}
        status, headers, raw = service.request("POST", "/api/characters/from-design", body)
        return status, json.loads(raw)

    def test_it_designs_keeps_and_approves_the_voice_in_one_call(self):
        service = self.lab()
        status, answer = self.design(service, text="Good morning, everyone.", seed=7, language="English", wait_seconds=30)
        self.assertEqual(status, 201, answer)
        voice = answer["voice"]
        self.assertEqual(
            (voice["name"], voice["kind"], voice["model"], voice["ready"], voice["status"]),
            ("Maya", "cast", SPEAKER, True, "approved"),
        )
        self.assertEqual((answer["engine"], answer["clone_engine"]), (DESIGNER, SPEAKER))
        # No person is in a designed voice's recording, so its badge comes from the models, both Apache-2.0 here.
        self.assertEqual(voice["licence"]["rating"], "yes")
        self.assertIn("The recording was designed with Qwen3-TTS 1.7B VoiceDesign", voice["licence"]["reason"])
        # The model was asked to design, with only the fields a design takes.
        self.assertEqual(
            service.engines.requests,
            [
                {
                    "engine": DESIGNER,
                    "mode": "design",
                    "text": "Good morning, everyone.",
                    "voice_description": "A bright, quick voice.",
                    "language": "English",
                    "seed": 7,
                }
            ],
        )
        identity = service.cast.get_identity(voice["identity_id"])
        self.assertEqual((identity["label"], identity["approved_reference_id"]), ("Designed", answer["reference_id"]))
        self.assertIn("A bright, quick voice.", identity["notes"])
        reference = identity["references"][0]
        self.assertEqual(reference["transcript"], "Good morning, everyone.")
        self.assertTrue(reference["origin"].startswith(f"Original synthetic voice design via {DESIGNER}; Qwen/stand-in"))
        self.assertIn("Apache-2.0", reference["allowed_use"])
        self.assertEqual(answer["character_id"], identity["character_id"])
        # It speaks by its name.
        status, headers, _raw = service.request(
            "POST", "/v1/audio/speech", {"voice": "Maya", "input": "Hello.", "response_format": "wav"}
        )
        self.assertEqual(status, 200)

    def test_without_a_line_it_says_the_default_one(self):
        service = self.lab()
        status, answer = self.design(service)
        self.assertEqual(status, 201, answer)
        self.assertEqual(service.engines.requests[0]["text"], cast_routes.DESIGN_LINE)
        self.assertNotIn("seed", service.engines.requests[0])

    def test_a_name_in_use_is_refused_before_anything_is_spoken(self):
        service = self.lab()
        service.cast.create_character("maya", "")
        status, answer = self.design(service)
        self.assertEqual((status, answer["code"]), (409, "already_exists"))
        self.assertIn("There is already a character called Maya", answer["error"])
        self.assertEqual(service.engines.requests, [])
        # A retired character's name is free again.
        retired = service.cast.list_cast()[0]
        service.cast.retire("character", retired["id"])
        self.assertEqual(self.design(service)[0], 201)

    def test_a_model_that_is_not_installed_is_named(self):
        service = self.lab()
        service.engines.installed.discard(SPEAKER)
        status, answer = self.design(service)
        self.assertEqual((status, answer["code"]), (503, "model_not_installed"))
        self.assertIn(
            "A voice designed with Qwen3-TTS 1.7B VoiceDesign is spoken afterwards by a model that clones", answer["error"]
        )
        self.assertIn("press Install on the card of Qwen3-TTS 1.7B Base.", answer["error"])
        # Another model that clones, whose clips anyone may use, speaks it when no Base checkpoint is installed.
        service.engines.installed.add("turbo")
        status, answer = self.design(service, name="Wren")
        self.assertEqual((status, answer["clone_engine"]), (201, "turbo"), answer)
        service.engines.installed.add(SMALL_SPEAKER)
        status, answer = self.design(service)
        self.assertEqual((status, answer["clone_engine"]), (201, SMALL_SPEAKER))
        status, answer = self.design(service, name="Tobin", clone_engine="voxcpm2")
        self.assertEqual(status, 503)
        self.assertIn("VoxCPM2 2B is not installed", answer["error"])
        service.engines.installed.discard(DESIGNER)
        status, answer = self.design(service, name="Tobin")
        self.assertEqual(status, 503)
        self.assertIn("Qwen3-TTS 1.7B VoiceDesign is not installed", answer["error"])
        self.assertEqual(len(self.characters(service)), 2)

    def test_a_request_it_cannot_take_is_refused_with_a_reason(self):
        service = self.lab()
        cases = {
            "no name": ({"name": None}, "name must be text"),
            "a long line": ({"text": "x" * 401}, "text must be text of 1 to 400 characters"),
            "a long description": ({"voice_description": "x" * 1001}, "voice_description must be text of 1 to 1,000"),
            "not a design model": ({"engine": "kokoro"}, "engine must be a model that designs voices"),
            "not a cloning model": ({"clone_engine": "kokoro"}, "clone_engine must be a model that clones"),
        }
        for label, (fields, words) in cases.items():
            with self.subTest(label):
                status, answer = self.design(service, **fields)
                self.assertEqual(status, 400, answer)
                self.assertIn(words, answer["error"])
        self.assertEqual(service.engines.requests, [])
        self.assertEqual(self.characters(service), [])

    def test_a_clip_too_short_for_a_voice_recording_keeps_nothing(self):
        service = self.lab(seconds=0.2)
        status, answer = self.design(service)
        self.assertEqual(status, 400, answer)
        self.assertEqual(len(service.engines.requests), 1)
        self.assertEqual(service.cast.list_cast(), [])

    def test_a_failure_while_saving_takes_the_new_character_back(self):
        service = self.lab()
        with mock.patch.object(service.cast, "update_identity", side_effect=ValueError("The approval failed.")):
            status, answer = self.design(service)
        self.assertEqual((status, answer["error"]), (400, "The approval failed."))
        self.assertEqual(service.cast.list_cast(), [])

    def test_every_design_model_has_models_to_speak_it(self):
        from voice_lab import engine_adapters
        from voice_lab.cast_store import REFERENCE_ENGINES

        self.assertEqual(cast_routes.DESIGNERS, engine_adapters.engines_where("saves_designs"))
        self.assertIn(engine_adapters.DEFAULT_DESIGNER, cast_routes.DESIGNERS)
        for designer in cast_routes.DESIGNERS:
            with self.subTest(designer):
                speakers = engine_adapters.get(designer).design_speakers
                self.assertTrue(speakers)
                self.assertLessEqual(set(speakers), REFERENCE_ENGINES)


if __name__ == "__main__":
    unittest.main()
