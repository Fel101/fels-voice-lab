"""Studio export panel: validation, busy handling, real package output, verification, and HTTP wiring."""

import io
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import export_panel
import ffmpeg_tools
import lipsync
import serve
from cast_store import CastStore
from tools import scene_export
from scene_store import SceneBatchRunner, SceneStore
from tests.phoneme_stub import stub_phonemes
from worker_pool import read_catalog

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def tone_wav(seconds=1.0, sample_rate=24_000, amplitude=0.1):
    time_axis = np.arange(round(seconds * sample_rate), dtype=np.float32) / sample_rate
    audio = amplitude * np.sin(2 * np.pi * 220 * time_axis)
    result = io.BytesIO()
    sf.write(result, audio, sample_rate, format="WAV", subtype="PCM_16")
    return result.getvalue()


class FakeEngines:
    def __init__(self):
        self.catalog = read_catalog()
        self.entries = {entry["id"]: entry for entry in self.catalog["engines"]}
        self.api_entries = {key: row for key, row in self.entries.items()
                            if row.get("integration", {}).get("service_api") is True}
        self.device = "cpu"

    def speak(self, req):
        time_axis = np.arange(12_000, dtype=np.float32) / 24_000
        audio = (0.1 * np.sin(2 * np.pi * 260 * time_axis)).astype(np.float32)
        return audio, 24_000, {"total_s": 0.5, "first_chunk_s": None, "mode": "clone",
                              "backend": "fake-qwen", "checkpoint": "Qwen/test", "checkpoint_revision": "test-revision"}


class ExportPanelBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-export-panel-")
        self.root = Path(self.temp.name)
        self.cast = CastStore(self.root)
        self.character = self.cast.create_character("Mira", "Warm and steady")
        self.identity = self.cast.create_identity(self.character["id"], "qwen3-tts-1.7b-base", "Mira Base")
        self.reference = self.cast.add_reference(self.identity["id"], "mira.wav", tone_wav(),
            transcript="A clear reference line.", origin="local test fixture", allowed_use="tests only",
            edits={"target_sample_rate": 24_000})
        self.cast.update_identity(self.identity["id"], {"status": "approved", "reference_version_id": self.reference["id"]})
        self.engines = FakeEngines()
        self.scenes = SceneStore(self.cast, self.engines.entries)
        self.runner = None
        self.server = None

    def tearDown(self):
        if self.runner:
            self.runner.close(timeout=5)
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.temp.cleanup()

    def scene_payload(self, name="Export panel scene"):
        return {"schema_version": 1, "name": name, "lines": [{
            "line_id": "line-001", "line_type": "dialogue", "character_id": self.character["id"],
            "identity_id": self.identity["id"], "reference_version_id": self.reference["id"],
            "text": "The road is clear.", "language": "English", "direction": "",
            "pause_before_ms": 0, "pause_after_ms": 75, "take_count": 1,
            "settings": {"seed": 42}, "use_pronunciations": False,
        }, {
            "line_id": "line-002", "line_type": "dialogue", "character_id": self.character["id"],
            "identity_id": self.identity["id"], "reference_version_id": self.reference["id"],
            "text": "Keep your voice down.", "language": "English", "direction": "",
            "pause_before_ms": 100, "pause_after_ms": 0, "take_count": 1,
            "settings": {"seed": 43}, "use_pronunciations": False,
        }]}

    def render_selected_scene(self):
        """Create, render, and select a two-line scene using the real batch runner."""
        base = self.start_server()
        scene = self.scenes.create_scene(self.scene_payload())
        self.runner = SceneBatchRunner(self.scenes, base)
        job = self.scenes.create_job(scene["scene_id"])
        self.runner.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = self.scenes.get_job(job["id"])
            if current["status"] == "completed":
                break
            time.sleep(0.025)
        else:
            self.fail("batch job did not complete")
        for row in current["rows"]:
            self.scenes.select_take(scene["scene_id"], row["line_id"], row["take_id"], row["segment_id"])
        return scene["scene_id"]

    def start_server(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1],
                                                              self.cast, self.scenes, None)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self.base


class ExportValidationTests(ExportPanelBase):
    def test_bad_requests_are_rejected_before_any_package_folder_exists(self):
        scene_id = "a" * 32
        bad_requests = [
            {"scene_id": "../../etc"},
            {"scene_id": scene_id, "formats": []},
            {"scene_id": scene_id, "formats": ["wma"]},
            {"scene_id": scene_id, "formats": ["wav", "wav"]},
            {"scene_id": scene_id, "name": "../escape"},
            {"scene_id": scene_id, "output_rate": True},
            {"scene_id": scene_id, "output_rate": 44100.5},
            {"scene_id": scene_id, "trim_start_seconds": float("nan")},
            {"scene_id": scene_id, "filename_template": "x" * 121},
        ]
        for req in bad_requests:
            with self.subTest(req=req):
                with self.assertRaises(ValueError):
                    export_panel.run(self.root, req)
        self.assertFalse((self.root / "out" / "exports").exists())

    def test_the_formats_the_panel_offers_are_the_formats_the_exporter_writes(self):
        offered = set(export_panel.FORMATS)
        self.assertEqual(offered, scene_export.FORMATS)
        self.assertEqual(offered, set(scene_export.FORMAT_CONFIG))
        self.assertEqual(offered, set(scene_export.FORMAT_CODEC))
        self.assertEqual(offered, set(ffmpeg_tools.FORMAT_ENCODER))
        self.assertEqual(offered, set(ffmpeg_tools.FORMAT_LABEL))

    def test_the_ffmpeg_report_says_what_is_missing(self):
        full = {"found": True, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "version": "9.0", "decoders": {"aac": True},
                "encoders": {"mp3": True, "aac": True, "vorbis": True, "flac": True}, "message": None}
        no_mp3 = {**full, "encoders": {**full["encoders"], "mp3": False}}
        cases = {
            "everything": (full, True, True, None),
            "no mp3 encoder": (no_mp3, True, False, "MP3"),
            "no ffprobe": ({**full, "ffprobe": None, "message": "FFmpeg was found but ffprobe was not."}, False, False, "ffprobe"),
            "no ffmpeg": (ffmpeg_tools.capabilities(None, None), False, False, "not found"),
        }
        for label, (status, ready, every_format, words) in cases.items():
            with self.subTest(label), mock.patch.object(ffmpeg_tools, "status", return_value=status):
                report = export_panel.ffmpeg_report()
                self.assertEqual(sorted(report["formats"]), sorted(export_panel.FORMATS))
                self.assertEqual((report["ready"], all(report["formats"].values())), (ready, every_format))
                if words is None:
                    self.assertIsNone(report["message"])
                else:
                    self.assertIn(words, report["message"])
        with mock.patch.object(ffmpeg_tools, "status", return_value=no_mp3):
            formats = export_panel.ffmpeg_report()["formats"]
        self.assertEqual([name for name, usable in formats.items() if not usable], ["mp3"])

    def test_a_missing_ffmpeg_or_encoder_is_reported_in_plain_words_before_anything_is_written(self):
        scene_id = self.render_selected_scene()
        with mock.patch.object(ffmpeg_tools, "resolve", return_value=None):
            with self.assertRaisesRegex(ValueError, "FFmpeg was not found.*winget install Gyan.FFmpeg"):
                export_panel.run(self.root, {"scene_id": scene_id}, dry_run=True)
        no_mp3 = {"found": True, "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "version": "9.0", "decoders": {},
                  "encoders": {"mp3": False, "aac": True, "vorbis": True, "flac": True}, "message": None}
        with mock.patch.object(ffmpeg_tools, "resolve", side_effect=lambda choice, tool: tool), \
                mock.patch.object(ffmpeg_tools, "capabilities", return_value=no_mp3):
            with self.assertRaisesRegex(ValueError, "cannot write MP3"):
                export_panel.run(self.root, {"scene_id": scene_id, "formats": ["wav", "mp3"]}, dry_run=True)
        self.assertFalse((self.root / "out" / "exports").exists())

    def test_the_export_list_carries_the_ffmpeg_report_and_a_page_can_ask_again(self):
        self.start_server()
        seen = []

        def status(refresh=False):
            seen.append(refresh)
            return ffmpeg_tools.capabilities(None, None)

        with mock.patch.object(ffmpeg_tools, "status", status):
            for path in ("/api/exports", "/api/exports?recheck=1"):
                with urllib.request.urlopen(self.base + path, timeout=10) as response:
                    listed = json.loads(response.read().decode("utf-8"))
                self.assertEqual(listed["exports"], [])
                self.assertFalse(listed["ffmpeg"]["ready"])
                self.assertIn("not found", listed["ffmpeg"]["message"])
        self.assertEqual(seen, [False, True])

    def test_preview_request_with_unknown_scene_fails_cleanly(self):
        with self.assertRaisesRegex(ValueError, "Scene not found"):
            export_panel.run(self.root, {"scene_id": "b" * 32}, dry_run=True)

    def test_verify_and_lookup_refuse_traversal_and_missing_folders(self):
        for name in ["../out", "..", "nested/dir", "missing-package"]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    export_panel.verify(self.root, name)

    def test_listing_reports_incomplete_packages_without_reading_them(self):
        self.assertEqual(export_panel.list_exports(self.root), [])
        broken = self.root / "out" / "exports" / "broken-run"
        broken.mkdir(parents=True)
        (broken / "INCOMPLETE.txt").write_text("Export stopped with OSError: disk full\n", encoding="utf-8")
        rows = export_panel.list_exports(self.root)
        self.assertEqual([(row["name"], row["status"]) for row in rows], [("broken-run", "incomplete")])

    def test_busy_export_lock_reports_busy_instead_of_running_a_second_write(self):
        self.assertTrue(export_panel._export_lock.acquire(blocking=False))
        try:
            with self.assertRaises(export_panel.ExportBusy):
                export_panel.run(self.root, {"scene_id": "c" * 32})
        finally:
            export_panel._export_lock.release()


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required for real package output")
class ExportPackageTests(ExportPanelBase):
    def test_preview_writes_nothing_and_a_real_package_verifies_and_refuses_overwrite(self):
        scene_id = self.render_selected_scene()
        preview = export_panel.run(self.root, {"scene_id": scene_id, "formats": ["wav"]}, dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertIn("PRE-EXPORT PROVENANCE REVIEW", preview["log"])
        self.assertFalse((self.root / "out" / "exports").exists())

        req = {"scene_id": scene_id, "formats": ["wav", "flac"], "name": "panel-test",
               "filename_template": "{line_id}", "trim_end_seconds": 0.0, "fade_out_ms": 20}
        created = export_panel.run(self.root, req)
        self.assertTrue(created["ok"])
        self.assertEqual(created["export"]["status"], "complete")
        self.assertEqual(created["export"]["line_count"], 2)
        folder = created["export"]["name"]
        self.assertEqual(folder, scene_export.safe_component("panel-test"))
        self.assertEqual(created["export"]["path"], f"out/exports/{folder}")
        package = self.root / "out" / "exports" / folder
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["formats"], ["wav", "flac"])
        self.assertEqual(manifest["transforms"]["fade_out_ms"], 20)
        self.assertTrue((package / "deliverables" / "flac").is_dir())

        listed = export_panel.list_exports(self.root)
        self.assertEqual([(row["name"], row["status"]) for row in listed], [(folder, "complete")])

        verified = export_panel.verify(self.root, folder)
        self.assertTrue(verified["ok"])
        self.assertIn("Export verification passed", verified["log"])

        with self.assertRaisesRegex(ValueError, "already exists"):
            export_panel.run(self.root, req)
        self.assertTrue(export_panel.verify(self.root, folder)["ok"], "a refused overwrite must leave the package intact")

    def test_studio_routes_return_preview_create_list_and_verify_over_http(self):
        scene_id = self.render_selected_scene()

        def call(method, path, body=None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            request = urllib.request.Request(self.base + path, data=data, method=method,
                                             headers={"Content-Type": "application/json"} if data else {})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8"))

        status, preview = call("POST", "/api/exports/preview", {"scene_id": scene_id})
        self.assertEqual(status, 200)
        self.assertTrue(preview["dry_run"])

        status, created = call("POST", "/api/exports", {"scene_id": scene_id, "name": "http-panel-test"})
        self.assertEqual(status, 201)
        folder = created["export"]["name"]
        self.assertEqual(folder, scene_export.safe_component("http-panel-test"))

        status, listed = call("GET", "/api/exports")
        self.assertEqual(status, 200)
        self.assertEqual([row["name"] for row in listed["exports"]], [folder])

        status, verified = call("POST", f"/api/exports/{folder}/verify", {})
        self.assertEqual(status, 200)
        self.assertTrue(verified["ok"])

        with self.assertRaises(urllib.error.HTTPError) as duplicate:
            call("POST", "/api/exports", {"scene_id": scene_id, "name": "http-panel-test"})
        self.assertEqual(duplicate.exception.code, 400)

        with self.assertRaises(urllib.error.HTTPError) as traversal:
            call("POST", "/api/exports/..%2Fsecret/verify", {})
        self.assertEqual(traversal.exception.code, 404)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required for real package output")
class LossyExportTests(ExportPanelBase):
    def setUp(self):
        super().setUp()
        report = ffmpeg_tools.status(refresh=True)
        if not (report["encoders"].get("mp3") and report["encoders"].get("aac")):
            self.skipTest("this FFmpeg build has no MP3 or AAC encoder")

    def test_mp3_and_aac_packages_are_written_probed_and_verified(self):
        scene_id = self.render_selected_scene()
        created = export_panel.run(self.root, {"scene_id": scene_id, "formats": ["mp3", "m4a"], "name": "lossy-test"})
        package = self.root / "out" / "exports" / created["export"]["name"]
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["formats"], ["mp3", "m4a"])
        codecs = {"line_deliverable_mp3": "mp3", "line_deliverable_m4a": "aac", "scene_mix_mp3": "mp3", "scene_mix_m4a": "aac"}
        found = {}
        for asset in manifest["assets"]:
            if asset["asset_type"] in codecs:
                found.setdefault(asset["asset_type"], []).append(asset)
                self.assertEqual(asset["codec"], codecs[asset["asset_type"]], asset["output_path"])
                self.assertEqual(asset["sample_rate"], 24_000)
                self.assertTrue((package / asset["output_path"]).is_file())
        self.assertEqual({kind: len(items) for kind, items in found.items()},
                         {"line_deliverable_mp3": 2, "line_deliverable_m4a": 2, "scene_mix_mp3": 1, "scene_mix_m4a": 1})
        for name in ("scene-mix.mp3", "scene-mix.m4a", "scene-mix-mp3.srt", "scene-mix-m4a.vtt"):
            self.assertTrue((package / "scene" / name).is_file(), name)
        # A take is half a second; the encoder adds a little padding, so the length is close, not equal.
        self.assertAlmostEqual(found["line_deliverable_mp3"][0]["duration_seconds"], 0.5, delta=0.1)
        self.assertTrue(export_panel.verify(self.root, created["export"]["name"])["ok"])

    def test_a_deliverable_that_is_not_the_codec_it_claims_fails_verification(self):
        scene_id = self.render_selected_scene()
        created = export_panel.run(self.root, {"scene_id": scene_id, "formats": ["mp3", "wav"], "name": "swap-test"})
        package = self.root / "out" / "exports" / created["export"]["name"]
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        line_mp3 = next(item for item in manifest["assets"] if item["asset_type"] == "line_deliverable_mp3")
        line_wav = next(item for item in manifest["assets"]
                        if item["asset_type"] == "line_deliverable_wav" and item["line_id"] == line_mp3["line_id"])
        # Put the WAV's bytes under the MP3's name and keep the manifest honest about the new hash, so only the codec is wrong.
        (package / line_mp3["output_path"]).write_bytes((package / line_wav["output_path"]).read_bytes())
        line_mp3["output_sha256"] = scene_export.sha256_file(package / line_mp3["output_path"])
        manifest.pop("checksum")
        manifest["checksum"] = scene_export.digest_json(manifest)
        (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "MP3 deliverable codec mismatch"):
            export_panel.verify(self.root, created["export"]["name"])

    def test_a_sample_rate_a_lossy_encoder_cannot_write_is_refused_before_any_file_exists(self):
        scene_id = self.render_selected_scene()
        for fmt, words in (("mp3", "MP3 cannot be written at 50000 Hz"), ("m4a", r"AAC \(M4A\) cannot be written at 50000 Hz")):
            with self.subTest(fmt):
                with self.assertRaisesRegex(ValueError, words):
                    export_panel.run(self.root, {"scene_id": scene_id, "formats": [fmt], "output_rate": 50_000}, dry_run=True)
        self.assertTrue(export_panel.run(self.root, {"scene_id": scene_id, "formats": ["wav"], "output_rate": 50_000},
                                         dry_run=True)["dry_run"], "WAV has no such limit")
        self.assertFalse((self.root / "out" / "exports").exists())


class ExportTakeLoadingTests(ExportPanelBase):
    def test_one_retirement_reader_serves_the_whole_scene(self):
        scene_id = self.render_selected_scene()
        built = []
        real = scene_export.RetirementReader

        def counting(db):
            built.append(db)
            return real(db)

        with mock.patch.object(scene_export, "RetirementReader", counting):
            selected = scene_export.load_takes(self.root, scene_export.load_scene(self.root, scene_id))
        self.assertEqual([item["line"]["line_id"] for item in selected], ["line-001", "line-002"])
        self.assertEqual(len(built), 1)

    def test_a_cast_database_from_before_retirement_still_loads_for_export(self):
        scene_id = self.render_selected_scene()
        db = sqlite3.connect(self.cast.db_path)
        try:
            db.execute("DROP INDEX takes_parent")
            for column in ("waveform_checks_json", "word_timings_json", "phoneme_timings_json", "loudness_json", "edit_json",
                           "parent_take_id"):
                db.execute(f"ALTER TABLE takes DROP COLUMN {column}")
            for table in ("purged_items", "purge_batches", "retirements"):
                db.execute(f"DROP TABLE {table}")
            db.execute("PRAGMA user_version=3")
            db.commit()
        finally:
            db.close()
        selected = scene_export.load_takes(self.root, scene_export.load_scene(self.root, scene_id))
        self.assertEqual(len(selected), 2)
        self.assertEqual({take["word_timings"] for item in selected for take in item["takes"]}, {None})

    def test_a_retired_take_still_stops_the_export(self):
        scene_id = self.render_selected_scene()
        scene = self.scenes.get_scene(scene_id)
        take_id = next(iter(next(line for line in scene["lines"] if line["line_id"] == "line-002")["selected_takes"].values()))
        self.cast.retire("take", take_id, reason="Bad take.")
        with self.assertRaisesRegex(scene_export.ExportError, "retired"):
            scene_export.load_takes(self.root, scene_export.load_scene(self.root, scene_id))


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required for real package output")
class WordCaptionExportTests(ExportPanelBase):
    def test_a_line_with_word_times_gets_phrase_captions_and_the_package_verifies(self):
        scene_id = self.render_selected_scene()
        scene = self.scenes.get_scene(scene_id)
        takes = {line["line_id"]: next(iter(line["selected_takes"].values())) for line in scene["lines"]}
        spans = [("The", 0.0, 0.2), ("road", 0.2, 0.45), ("is", 0.45, 0.6), ("clear.", 0.6, 0.95)]
        self.cast.set_word_timings(takes["line-001"], {
            "schema": 1, "aligner": {"id": "test"}, "duration_seconds": 1.0, "checks": {"status": "ok"},
            "words": [{"word": word, "start": start, "end": end, "probability": 0.9} for word, start, end in spans]})
        created = export_panel.run(self.root, {"scene_id": scene_id, "formats": ["wav"], "name": "word-captions",
                                               "filename_template": "{line_id}", "trim_end_seconds": 0.0})
        self.assertTrue(created["ok"])
        folder = created["export"]["name"]
        package = self.root / "out" / "exports" / folder
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        cues = manifest["scene_mix"]["caption_cues"]
        self.assertEqual([cue["text"] for cue in cues], ["The road is clear.", "Keep your voice down."])
        line_one = next(line for line in manifest["scene_mix"]["lines"] if line["line_id"] == "line-001")
        self.assertAlmostEqual(cues[0]["start_seconds"], line_one["start_seconds"], places=3)
        self.assertAlmostEqual(cues[0]["end_seconds"], line_one["start_seconds"] + 0.95, places=3)
        srt = (package / "scene" / "scene.srt").read_text(encoding="utf-8")
        self.assertIn("Mira: The road is clear.", srt)
        self.assertIn("Mira: Keep your voice down.", srt)
        self.assertTrue(export_panel.verify(self.root, folder)["ok"])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg and ffprobe are required for real package output")
class LipSyncExportTests(ExportPanelBase):
    SPANS = [("The", 0.0, 0.2), ("road", 0.2, 0.45), ("is", 0.45, 0.6), ("clear.", 0.6, 0.95)]

    def setUp(self):
        super().setUp()
        # These tests check the export around a track, so the phoneme rules (misaki, optional) are a fixed stand-in.
        patcher = mock.patch.object(lipsync, "english_word_phonemes", stub_phonemes)
        patcher.start()
        self.addCleanup(patcher.stop)

    def export_with_line_one_timed(self, trim_start):
        scene_id = self.render_selected_scene()
        scene = self.scenes.get_scene(scene_id)
        line_one = next(line for line in scene["lines"] if line["line_id"] == "line-001")
        self.cast.set_word_timings(next(iter(line_one["selected_takes"].values())), {
            "schema": 1, "aligner": {"id": "test"}, "language": "en", "text": "The road is clear.",
            "duration_seconds": 1.0, "checks": {"status": "ok"},
            "words": [{"word": word, "start": start, "end": end, "probability": 0.9} for word, start, end in self.SPANS]})
        created = export_panel.run(self.root, {"scene_id": scene_id, "formats": ["wav"], "name": "lip-sync",
                                               "filename_template": "{line_id}", "trim_start_seconds": trim_start,
                                               "trim_end_seconds": 0.0})
        self.assertTrue(created["ok"])
        folder = created["export"]["name"]
        package = self.root / "out" / "exports" / folder
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        return folder, package, manifest

    def track_and_deliverable(self, package, manifest):
        records = [record for record in manifest["assets"] if record["asset_type"] == "lip_sync_track"]
        self.assertEqual([record["line_id"] for record in records], ["line-001"])
        track = json.loads((package / records[0]["output_path"]).read_text(encoding="utf-8"))
        deliverable = next(record for record in manifest["assets"]
                           if record["asset_type"] == "line_deliverable_wav" and record["line_id"] == "line-001")
        return track, deliverable

    def test_a_line_with_word_times_gets_a_lip_sync_track_that_matches_its_deliverable(self):
        folder, package, manifest = self.export_with_line_one_timed(trim_start=0.0)
        track, deliverable = self.track_and_deliverable(package, manifest)
        lipsync.check_track(track)
        self.assertEqual(track["timing"]["source"], "word-distribution")
        self.assertAlmostEqual(track["duration_seconds"], deliverable["duration_seconds"], places=2)
        statuses = {}
        for pack_info in manifest["character_packs"]:
            pack_path = package / "character-packs" / pack_info["folder"] / "character-pack.json"
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
            statuses.update({line["line_id"]: line["lip_sync"]["status"] for line in pack["lines"]})
        self.assertEqual(statuses, {"line-001": "written", "line-002": "not_written"})
        self.assertTrue(export_panel.verify(self.root, folder)["ok"])

    def test_the_lip_sync_track_moves_with_the_export_trim(self):
        folder, package, manifest = self.export_with_line_one_timed(trim_start=0.1)
        track, deliverable = self.track_and_deliverable(package, manifest)
        lipsync.check_track(track)
        self.assertAlmostEqual(track["duration_seconds"], deliverable["duration_seconds"], places=2)
        first_tongue = next(key for key in track["keys"] if key["viseme"] == "tongue")
        self.assertAlmostEqual(first_tongue["start"], 0.1, places=2)  # "road" starts at 0.2 s, less the 0.1 s trim
        self.assertTrue(export_panel.verify(self.root, folder)["ok"])

    def test_a_kokoro_take_with_stored_times_exports_those_times(self):
        scene_id = self.render_selected_scene()
        scene = self.scenes.get_scene(scene_id)
        line_one = next(line for line in scene["lines"] if line["line_id"] == "line-001")
        timing = {"source": "kokoro-durations", "frame_seconds": 0.025, "words": [], "unplaced_words": 0,
                  "phonemes": [{"symbol": "p", "start": 0.1, "end": 0.2}, {"symbol": "ɑ", "start": 0.2, "end": 0.5}]}
        db = sqlite3.connect(self.cast.db_path)
        try:
            db.execute("UPDATE takes SET phoneme_timings_json=? WHERE id=?",
                       (json.dumps(timing), next(iter(line_one["selected_takes"].values()))))
            db.commit()
        finally:
            db.close()
        created = export_panel.run(self.root, {"scene_id": scene_id, "formats": ["wav"], "name": "kokoro-times",
                                               "filename_template": "{line_id}", "trim_end_seconds": 0.0})
        self.assertTrue(created["ok"])
        folder = created["export"]["name"]
        package = self.root / "out" / "exports" / folder
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        track, _deliverable = self.track_and_deliverable(package, manifest)
        lipsync.check_track(track)
        self.assertEqual(track["timing"]["source"], "kokoro-durations")
        self.assertEqual(track["keys"][1:3], [{"start": 0.1, "end": 0.2, "viseme": "closed"},
                                              {"start": 0.2, "end": 0.5, "viseme": "open_wide"}])
        self.assertTrue(export_panel.verify(self.root, folder)["ok"])


if __name__ == "__main__":
    unittest.main()
