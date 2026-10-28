"""Engine checks: installed, usable and measured state for the Models page.

Every test uses a fake worker or a temporary tree. No model is loaded, and nothing is written under the real out/ or data/ folders.
"""

import json
import sys
import tempfile
import threading
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

import api_spec
import engine_checks
import reference_paths
import serve
import worker_pool
from cast_store import CastStore
from engine_checks import (EngineCheckStore, audible, check_record, engine_fingerprint, installation,
                           smoke_request, status_payload)
from worker_pool import EnginePool, WorkerBusy, read_catalog
from worker_process import WorkerRequestError, WorkerTimeout, WorkerUnavailable

CATALOG = {entry["id"]: entry for entry in read_catalog()["engines"]}
POINTER = "Kokoro 82M is not installed. To install it, run setup-kokoro.bat in the lab folder, then try again."


def installed_probe(entry):
    return {"installed": True, "missing": [], "environment": entry["runtime"]["python"], "checkpoint": "fake"}


def missing_probe(entry):
    return {"installed": False, "missing": [entry["runtime"]["python"]], "environment": entry["runtime"]["python"],
            "checkpoint": "fake"}


def pin_one(entry):
    return "pin-1"


def pin_two(entry):
    return "pin-2"


class FakeWorker:
    """Answers the worker protocol in-process: load reports speakers, generate writes a short tone."""

    def __init__(self, *, load_error=None, silent=False, device=None, speakers=("Ryan",)):
        self.load_error = load_error
        self.silent = silent
        self.device = device
        self.speakers = list(speakers)
        self.calls = []
        self.alive = False
        self.pid = None
        self.log_path = Path(tempfile.gettempdir()) / "voice-lab-engine-check-tests.log"

    def start(self):
        self.alive = True

    def close(self, force=False):
        self.alive = False

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "load":
            if self.load_error is not None:
                raise self.load_error
            return {"model_loaded": True, "sample_rate": 24000, "load_seconds": 0.25, "pid": 4242,
                    "supported_speakers": self.speakers, "supported_languages": ["English"]}
        if method == "prepare_reference":
            return {"prepared": True}
        if method == "generate":
            samples = np.zeros(24000, np.float32) if self.silent else (
                0.2 * np.sin(np.arange(24000, dtype=np.float32) * 0.05)).astype(np.float32)
            output = Path(params["output_file"])
            output.parent.mkdir(parents=True, exist_ok=True)
            np.save(output, samples, allow_pickle=False)
            info = {"total_s": 0.05}
            if self.device:
                info["device"] = self.device
            return {"audio_file": str(output), "sample_rate": 24000, "sample_count": samples.size, "info": info}
        if method == "unload":
            return {"model_loaded": False}
        raise AssertionError(f"Unexpected worker call {method!r}")

    def generated(self):
        return [params for method, params in self.calls if method == "generate"]


class TempTree(unittest.TestCase):
    """Give each test a private project root, and keep worker output and the reference clip inside it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-engine-checks-")
        self.root = Path(self.temp.name)
        for target, value in ((worker_pool, "OUT"), (worker_pool, "LAB"), (reference_paths, "LAB")):
            patcher = mock.patch.object(target, value, self.root / "out" if value == "OUT" else self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        reference = self.root / "reference" / "af_heart.wav"
        reference.parent.mkdir(parents=True)
        sf.write(reference, np.zeros(6 * 24000, np.float32), 24000, subtype="PCM_16")

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, content=b"x"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def pool(self, worker, *, store=None, probe=installed_probe):
        return EnginePool("cpu", 0, worker_factory=lambda entry: worker, idle_poll_seconds=0.1,
                          check_store=store, install_probe=probe)


class InstallationTests(TempTree):
    def hf_snapshot(self, engine):
        entry = CATALOG[engine]
        base = "hf-cache" if engine == "voxcpm2" else "hf-cache/hub"
        return f"{base}/models--{entry['checkpoint'].replace('/', '--')}/snapshots/{entry['local_revision']}"

    def environment(self, engine):
        self.write(CATALOG[engine]["runtime"]["python"])

    def test_installed_needs_the_environment_and_a_weight_file(self):
        self.environment("kokoro")
        self.write(f"{self.hf_snapshot('kokoro')}/kokoro-v1_0.pth")
        result = installation(CATALOG["kokoro"], self.root)
        self.assertTrue(result["installed"])
        self.assertEqual(result["missing"], [])

    def test_missing_environment_and_snapshot_are_named(self):
        result = installation(CATALOG["kokoro"], self.root)
        self.assertFalse(result["installed"])
        self.assertIn(CATALOG["kokoro"]["runtime"]["python"], result["missing"])
        self.assertIn(self.hf_snapshot("kokoro"), result["missing"])

    def test_snapshot_without_weights_is_not_installed(self):
        self.environment("kokoro")
        self.write(f"{self.hf_snapshot('kokoro')}/config.json")
        result = installation(CATALOG["kokoro"], self.root)
        self.assertFalse(result["installed"])
        self.assertIn(f"{self.hf_snapshot('kokoro')} (no weight file)", result["missing"])

    def test_a_missing_kokoro_names_the_launcher_that_installs_it(self):
        self.assertEqual(installation(CATALOG["kokoro"], self.root)["installer"], "setup-kokoro.bat")
        self.environment("kokoro")
        self.write(f"{self.hf_snapshot('kokoro')}/config.json")
        result = installation(CATALOG["kokoro"], self.root)
        self.assertFalse(result["installed"])
        self.assertEqual(result["installer"], "setup-kokoro.bat")

    def test_an_installed_engine_names_no_launcher(self):
        self.environment("kokoro")
        self.write(f"{self.hf_snapshot('kokoro')}/kokoro-v1_0.pth")
        self.assertNotIn("installer", installation(CATALOG["kokoro"], self.root))

    def test_engines_without_a_guided_installer_name_none(self):
        for engine, entry in CATALOG.items():
            if entry["integration"].get("service_api") is True and engine not in engine_checks.GUIDED_INSTALLERS:
                with self.subTest(engine=engine):
                    self.assertFalse(installation(entry, self.root)["installed"])
                    self.assertNotIn("installer", installation(entry, self.root))

    def test_every_launcher_named_is_in_the_lab_folder_and_belongs_to_an_integrated_engine(self):
        self.assertEqual(set(engine_checks.GUIDED_INSTALLERS), {"kokoro"})
        for engine, launcher in engine_checks.GUIDED_INSTALLERS.items():
            with self.subTest(engine=engine):
                self.assertIs(CATALOG[engine]["integration"].get("service_api"), True)
                self.assertTrue((ROOT / launcher).is_file(), f"{launcher} is not in the lab folder")

    def test_voxcpm_reads_the_top_level_cache_and_needs_its_source_and_overlay(self):
        self.environment("voxcpm2")
        self.write(f"{self.hf_snapshot('voxcpm2')}/model.safetensors")
        result = installation(CATALOG["voxcpm2"], self.root)
        self.assertIn("out/voxcpm2/source/src", result["missing"])
        self.assertIn(".venv-fqwen/Lib/site-packages", result["missing"])
        (self.root / "out/voxcpm2/source/src").mkdir(parents=True)
        (self.root / ".venv-fqwen/Lib/site-packages").mkdir(parents=True)
        self.assertTrue(installation(CATALOG["voxcpm2"], self.root)["installed"])

    def test_drama_needs_the_receipt_and_the_files_it_names(self):
        self.environment("drama-3")
        self.assertEqual(installation(CATALOG["drama-3"], self.root)["missing"], ["out/drama3/installation.json"])
        model = self.root / "out/drama3/models/Drama.gguf"
        runtime = self.root / "out/drama3/runtime/speech.exe"
        receipt = self.root / "out/drama3/installation.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"model_path": str(model), "runtime_path": str(runtime)}), encoding="utf-8")
        self.assertEqual(installation(CATALOG["drama-3"], self.root)["missing"], [str(model), str(runtime)])
        self.write("out/drama3/models/Drama.gguf")
        self.write("out/drama3/runtime/speech.exe")
        self.assertTrue(installation(CATALOG["drama-3"], self.root)["installed"])

    def test_breeze_needs_its_bf16_weight_file(self):
        self.write(CATALOG["breeze-tts-2"]["runtime"]["python"])
        result = installation(CATALOG["breeze-tts-2"], self.root)
        self.assertIn("breeze/models (Breeze-TTS-2-bf16.safetensors)", result["missing"])
        self.write("breeze/models/breezetts2/example/Breeze-TTS-2-bf16.safetensors")
        self.assertTrue(installation(CATALOG["breeze-tts-2"], self.root)["installed"])


class InstallPointerTests(unittest.TestCase):
    def test_a_missing_kokoro_is_told_the_launcher_to_run(self):
        self.assertEqual(engine_checks.install_pointer(CATALOG["kokoro"], missing_probe), POINTER)

    def test_an_installed_kokoro_has_nothing_to_point_at(self):
        self.assertIsNone(engine_checks.install_pointer(CATALOG["kokoro"], installed_probe))

    def test_an_engine_without_a_launcher_has_nothing_to_point_at_and_is_not_probed(self):
        asked = []

        def probe(entry):
            asked.append(entry["id"])
            return missing_probe(entry)

        for engine, entry in CATALOG.items():
            if engine not in engine_checks.GUIDED_INSTALLERS:
                with self.subTest(engine=engine):
                    self.assertIsNone(engine_checks.install_pointer(entry, probe))
        self.assertEqual(asked, [])

    def test_an_entry_without_a_name_is_called_by_its_id(self):
        nameless = {key: value for key, value in CATALOG["kokoro"].items() if key != "name"}
        self.assertTrue(engine_checks.install_pointer(nameless, missing_probe).startswith("kokoro is not installed."))

    def test_the_api_reference_and_the_setup_guide_name_every_launcher(self):
        setup = (ROOT / "docs" / "SETUP.md").read_text(encoding="utf-8")
        for launcher in engine_checks.GUIDED_INSTALLERS.values():
            with self.subTest(launcher=launcher):
                self.assertIn(launcher, api_spec.ENGINE_NOT_INSTALLED)
                self.assertIn(launcher, setup)


class FingerprintTests(TempTree):
    def site_packages(self, environment=".venv"):
        site = self.root / environment / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        return site

    def test_it_is_stable_until_a_package_is_added_or_removed(self):
        site = self.site_packages()
        (site / "torch-2.12.0.dist-info").mkdir()
        first = engine_fingerprint(CATALOG["kokoro"], self.root)
        self.assertEqual(engine_fingerprint(CATALOG["kokoro"], self.root), first)
        (site / "numpy-2.0.0.dist-info").mkdir()
        added = engine_fingerprint(CATALOG["kokoro"], self.root)
        self.assertNotEqual(added, first)
        (site / "numpy-2.0.0.dist-info").rmdir()
        self.assertEqual(engine_fingerprint(CATALOG["kokoro"], self.root), first)

    def test_a_package_version_change_counts_but_unrelated_files_do_not(self):
        site = self.site_packages()
        (site / "torch-2.12.0.dist-info").mkdir()
        before = engine_fingerprint(CATALOG["kokoro"], self.root)
        (site / "__pycache__").mkdir()
        (site / "notes.txt").write_text("not a package", encoding="utf-8")
        self.assertEqual(engine_fingerprint(CATALOG["kokoro"], self.root), before)
        (site / "torch-2.12.0.dist-info").rename(site / "torch-2.13.0.dist-info")
        self.assertNotEqual(engine_fingerprint(CATALOG["kokoro"], self.root), before)

    def test_a_change_to_the_catalog_pin_counts(self):
        self.site_packages()
        before = engine_fingerprint(CATALOG["kokoro"], self.root)
        moved = dict(CATALOG["kokoro"], local_revision="a-newer-snapshot")
        self.assertNotEqual(engine_fingerprint(moved, self.root), before)

    def test_an_engine_reads_its_own_environment_and_its_overlay(self):
        self.site_packages(".venv")
        before = engine_fingerprint(CATALOG["voxcpm2"], self.root)
        (self.site_packages(".venv-fqwen") / "torch-2.12.0.dist-info").mkdir()
        self.assertNotEqual(engine_fingerprint(CATALOG["voxcpm2"], self.root), before)  # the overlay it imports from
        before_kokoro = engine_fingerprint(CATALOG["kokoro"], self.root)
        (self.site_packages(".venv-fqwen") / "extra-1.0.dist-info").mkdir()
        self.assertEqual(engine_fingerprint(CATALOG["kokoro"], self.root), before_kokoro)  # a different environment

    def test_a_missing_environment_still_gives_a_fingerprint(self):
        self.assertEqual(len(engine_fingerprint(CATALOG["kokoro"], self.root)), 16)


class SmokeRequestTests(unittest.TestCase):
    def test_kokoro_prefers_af_heart_and_falls_back_to_a_cached_voice(self):
        with mock.patch.object(engine_checks, "discover_kokoro_voices", return_value=["bf_alice", "af_heart"]):
            self.assertEqual(smoke_request("kokoro")["voice"], "af_heart")
        with mock.patch.object(engine_checks, "discover_kokoro_voices", return_value=["bf_alice"]):
            self.assertEqual(smoke_request("kokoro")["voice"], "bf_alice")

    def test_custom_voice_uses_the_first_speaker_the_checkpoint_reports(self):
        request = smoke_request("qwen3-tts-1.7b-custom-voice", {"supported_speakers": ["Vivian", "Ryan"]})
        self.assertEqual(request["speaker"], "Vivian")
        with self.assertRaisesRegex(ValueError, "no speakers"):
            smoke_request("qwen3-tts-1.7b-custom-voice", {"supported_speakers": []})

    def test_unknown_engines_have_no_speech_check(self):
        with self.assertRaisesRegex(ValueError, "No speech check"):
            smoke_request("not-an-engine")

    def test_audio_must_be_long_enough_and_not_silent(self):
        self.assertIn("at least", audible(np.zeros(100, np.float32), 24000))
        self.assertEqual(audible(np.zeros(24000, np.float32), 24000), "generated silence")
        self.assertIsNone(audible(np.full(24000, 0.5, np.float32), 24000))


class CheckStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-check-store-")
        self.path = Path(self.temp.name) / "engines" / "checks.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_checks_survive_a_reload_and_stay_per_device(self):
        store = EngineCheckStore(self.path)
        store.record_check("kokoro", "cpu", check_record(status="passed", device="cpu", total_s=0.5, audio_seconds=2.0))
        store.record_check("kokoro", "cuda", check_record(status="failed", device="cuda", reason="no GPU"))
        checks = EngineCheckStore(self.path).checks_for("kokoro")
        self.assertEqual(checks["cpu"]["status"], "passed")
        self.assertEqual(checks["cpu"]["real_time_factor"], 0.25)
        self.assertEqual(checks["cuda"]["reason"], "no GPU")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_speed_rows_use_the_median_and_ignore_invalid_clips(self):
        store = EngineCheckStore(self.path)
        store.record_clip("kokoro", "cpu", 0.5, 2.0)
        store.record_clip("kokoro", "cpu", 2.0, 2.0)
        store.record_clip("kokoro", "cpu", 1.0, 4.0)
        store.record_clip("kokoro", "cpu", 0, 2.0)
        store.record_clip("kokoro", "cpu", 1.0, 0)
        store.record_clip("kokoro", "cpu", 0.5, 0.0000417)
        (row,) = store.speed_rows()
        self.assertEqual(row["clips"], 3)
        self.assertEqual(row["median_real_time_factor"], 0.25)
        self.assertEqual(row["last_audio_seconds"], 4.0)

    def test_only_the_latest_clips_are_kept(self):
        store = EngineCheckStore(self.path)
        for index in range(engine_checks.SPEED_SAMPLES + 5):
            store.record_clip("turbo", "cpu", 1.0, 1.0 + index)
        (row,) = store.speed_rows()
        self.assertEqual(row["clips"], engine_checks.SPEED_SAMPLES)
        self.assertEqual(row["last_audio_seconds"], 1.0 + engine_checks.SPEED_SAMPLES + 4)

    def test_corrupt_file_is_set_aside_and_the_store_starts_empty(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = EngineCheckStore(self.path)
        self.assertEqual(store.checks_for("kokoro"), {})
        self.assertTrue(self.path.with_name("checks.json.corrupt").is_file())

    def test_a_failed_save_is_reported_without_failing_the_clip(self):
        blocker = Path(self.temp.name) / "blocked"
        blocker.write_text("a file where a folder should be", encoding="utf-8")
        store = EngineCheckStore(blocker / "checks.json")
        store.record_clip("kokoro", "cpu", 0.5, 1.0)
        self.assertIsNotNone(store.write_error)
        self.assertEqual(store.speed_rows()[0]["clips"], 1)


class StatusPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-status-")
        self.store = EngineCheckStore(Path(self.temp.name) / "checks.json")
        self.store.record_check("kokoro", "cpu", check_record(status="passed", device="cpu", total_s=0.5, audio_seconds=2,
                                                              fingerprint="pin-1"))
        self.store.record_check("kokoro", "cuda", check_record(status="failed", device="cuda", reason="boom",
                                                               fingerprint="pin-1"))

    def tearDown(self):
        self.temp.cleanup()

    def rows(self, device, probe=installed_probe, fingerprint_of=pin_one):
        payload = status_payload(read_catalog_entries(), device=device, busy=False, store=self.store, probe=probe,
                                 fingerprint_of=fingerprint_of)
        return {row["id"]: row for row in payload["engines"]}, payload

    def test_usable_is_judged_on_the_service_device_only(self):
        self.assertEqual(self.rows("cpu")[0]["kokoro"]["state"], "usable")
        self.assertEqual(self.rows("cuda")[0]["kokoro"]["state"], "check_failed")
        self.assertEqual(self.rows("vulkan")[0]["kokoro"]["state"], "not_checked")

    def test_missing_files_win_over_an_earlier_passing_check(self):
        rows, _ = self.rows("cpu", probe=missing_probe)
        self.assertEqual(rows["kokoro"]["state"], "not_installed")
        self.assertEqual(rows["kokoro"]["check"]["status"], "passed")

    def test_a_missing_install_carries_its_launcher_into_the_rows_the_page_reads(self):
        empty_lab = Path(self.temp.name) / "empty-lab"
        rows, _ = self.rows("cpu", probe=lambda entry: installation(entry, empty_lab))
        self.assertEqual(rows["kokoro"]["state"], "not_installed")
        self.assertEqual(rows["kokoro"]["installation"]["installer"], "setup-kokoro.bat")
        self.assertEqual(rows["turbo"]["state"], "not_installed")
        self.assertNotIn("installer", rows["turbo"]["installation"])

    def test_conversion_is_reported_as_a_task_not_a_speech_check(self):
        rows, _ = self.rows("cpu")
        self.assertEqual(rows["chatterbox-vc"]["state"], "conversion_task")

    def test_a_check_made_against_another_pin_or_package_set_is_out_of_date(self):
        rows, _ = self.rows("cpu", fingerprint_of=pin_two)
        self.assertEqual(rows["kokoro"]["state"], "check_outdated")
        self.assertTrue(rows["kokoro"]["check_outdated"])
        self.assertEqual(rows["kokoro"]["check"]["status"], "passed")  # kept, so the page can say when it was made
        self.assertEqual(self.rows("cuda", fingerprint_of=pin_two)[0]["kokoro"]["state"], "check_outdated")  # failures too
        rows, _ = self.rows("cpu")
        self.assertEqual(rows["kokoro"]["state"], "usable")
        self.assertFalse(rows["kokoro"]["check_outdated"])

    def test_a_check_recorded_before_fingerprints_existed_is_out_of_date(self):
        self.store.record_check("turbo", "cpu", check_record(status="passed", device="cpu", total_s=0.5, audio_seconds=2))
        rows, _ = self.rows("cpu")
        self.assertEqual(rows["turbo"]["state"], "check_outdated")

    def test_missing_files_and_conversion_still_win_over_an_out_of_date_check(self):
        rows, _ = self.rows("cpu", probe=missing_probe, fingerprint_of=pin_two)
        self.assertEqual(rows["kokoro"]["state"], "not_installed")
        rows, _ = self.rows("cpu", fingerprint_of=pin_two)
        self.assertEqual(rows["chatterbox-vc"]["state"], "conversion_task")
        self.assertFalse(rows["chatterbox-vc"]["check_outdated"])

    def test_engines_that_were_never_checked_do_not_compute_a_fingerprint(self):
        asked = []

        def watching(entry):
            asked.append(entry["id"])
            return "pin-1"

        self.rows("cpu", fingerprint_of=watching)
        self.assertEqual(asked, ["kokoro"])  # the only engine with a stored check on this device

    def test_speed_and_busy_come_from_the_store_and_the_service(self):
        self.store.record_clip("kokoro", "cpu", 0.5, 2.0)
        payload = status_payload(read_catalog_entries(), device="cpu", busy=True, store=self.store, probe=installed_probe)
        self.assertTrue(payload["busy"])
        self.assertEqual(payload["speed"][0]["engine"], "kokoro")
        self.assertIsNone(payload["store_error"])


def read_catalog_entries():
    return dict(CATALOG)


class FailureReasonTests(unittest.TestCase):
    def test_reason_keeps_the_first_line_with_its_log_path(self):
        error = WorkerUnavailable("Worker timed out during generate after 300s. Log: C:/logs/turbo.log\n"
                                  "  3%|  27/1000 [02:59<1:48:11, 6.67s/it]")
        self.assertEqual(engine_checks.failure_reason(error),
                         "WorkerUnavailable: Worker timed out during generate after 300s. Log: C:/logs/turbo.log")

    def test_reason_is_bounded_and_names_an_empty_error(self):
        self.assertEqual(engine_checks.failure_reason(WorkerUnavailable("")), "WorkerUnavailable: ")
        self.assertEqual(len(engine_checks.failure_reason(WorkerUnavailable("x" * 1000))), 400)


class EngineCheckTests(TempTree):
    def test_check_passes_and_records_the_clip_for_the_speed_table(self):
        store = EngineCheckStore(self.root / "data" / "engines" / "checks.json")
        worker = FakeWorker()
        pool = self.pool(worker, store=store)
        record = pool.check("kokoro")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["device"], "cpu")
        self.assertEqual(record["ran_on"], "cpu")
        self.assertEqual(record["total_s"], 0.05)
        self.assertAlmostEqual(record["audio_seconds"], 1.0)
        self.assertEqual(store.checks_for("kokoro")["cpu"]["status"], "passed")
        state = pool.engine_status()
        kokoro = {row["id"]: row for row in state["engines"]}["kokoro"]
        self.assertEqual(kokoro["state"], "usable")
        self.assertEqual(state["speed"][0]["clips"], 1)

    def test_a_check_is_bound_to_the_engine_it_ran_against_and_expires_when_a_package_changes(self):
        store = EngineCheckStore(self.root / "checks.json")
        pool = self.pool(FakeWorker(), store=store)
        site = self.root / ".venv" / "Lib" / "site-packages"
        (site / "torch-2.12.0.dist-info").mkdir(parents=True)

        def kokoro_row():
            return {row["id"]: row for row in pool.engine_status()["engines"]}["kokoro"]

        record = pool.check("kokoro")
        self.assertEqual(record["fingerprint"], engine_fingerprint(CATALOG["kokoro"], self.root))
        self.assertEqual(kokoro_row()["state"], "usable")
        (site / "torch-2.13.0.dist-info").mkdir()  # a package arrives after the check
        self.assertEqual(kokoro_row()["state"], "check_outdated")
        pool.check("kokoro")
        self.assertEqual(kokoro_row()["state"], "usable")

    def test_a_failed_check_carries_the_fingerprint_too(self):
        store = EngineCheckStore(self.root / "checks.json")
        worker = FakeWorker(load_error=WorkerUnavailable("Configured Python environment is missing"))
        record = self.pool(worker, store=store).check("kokoro")
        self.assertEqual(record["fingerprint"], engine_fingerprint(CATALOG["kokoro"], self.root))

    def test_every_speech_engine_passes_its_own_speak_validation(self):
        for engine in ("kokoro", "turbo", "orig", "breeze-tts-2", "qwen3-tts-1.7b-voice-design",
                       "qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base", "qwen3-tts-1.7b-custom-voice",
                       "voxcpm2", "drama-3"):
            with self.subTest(engine=engine):
                worker = FakeWorker()
                record = self.pool(worker).check(engine)
                self.assertEqual(record["status"], "passed", record.get("reason"))
                self.assertEqual(len(worker.generated()), 1)

    def test_conversion_has_no_speech_check(self):
        worker = FakeWorker()
        with self.assertRaisesRegex(ValueError, "Voice conversion"):
            self.pool(worker).check("chatterbox-vc")
        with self.assertRaisesRegex(ValueError, "Unknown engine"):
            self.pool(worker).check("not-an-engine")
        self.assertEqual(worker.calls, [])

    def test_missing_files_answer_without_starting_a_worker(self):
        store = EngineCheckStore(self.root / "checks.json")
        worker = FakeWorker()
        record = self.pool(worker, store=store, probe=missing_probe).check("kokoro")
        self.assertEqual(record["status"], "not_installed")
        self.assertEqual(worker.calls, [])
        self.assertEqual(store.checks_for("kokoro"), {})

    def test_a_load_failure_is_a_failed_check_with_its_reason(self):
        store = EngineCheckStore(self.root / "checks.json")
        worker = FakeWorker(load_error=WorkerUnavailable("Configured Python environment is missing"))
        record = self.pool(worker, store=store).check("kokoro")
        self.assertEqual(record["status"], "failed")
        self.assertIn("WorkerUnavailable", record["reason"])
        self.assertEqual(store.checks_for("kokoro")["cpu"]["status"], "failed")

    def test_silent_output_fails_the_check(self):
        record = self.pool(FakeWorker(silent=True)).check("kokoro")
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["reason"], "generated silence")

    def test_a_busy_service_refuses_the_check_and_records_nothing(self):
        store = EngineCheckStore(self.root / "checks.json")
        pool = self.pool(FakeWorker(), store=store)
        self.assertTrue(pool._gpu_job.acquire(blocking=False))
        try:
            with self.assertRaises(WorkerBusy):
                pool.check("kokoro")
        finally:
            pool._gpu_job.release()
        self.assertEqual(store.checks_for("kokoro"), {})

    def test_custom_voice_check_uses_the_speaker_the_checkpoint_reported(self):
        worker = FakeWorker(speakers=("Vivian", "Ryan"))
        self.assertEqual(self.pool(worker).check("qwen3-tts-1.7b-custom-voice")["status"], "passed")
        self.assertEqual(worker.generated()[0]["speaker"], "Vivian")

    def test_clip_device_follows_the_device_the_worker_reports(self):
        store = EngineCheckStore(self.root / "checks.json")
        worker = FakeWorker(device="Vulkan1")
        record = self.pool(worker, store=store).check("drama-3")
        self.assertEqual(record["device"], "cpu")
        self.assertEqual(record["ran_on"], "Vulkan1")
        self.assertEqual(store.speed_rows()[0]["device"], "Vulkan1")


class FailedLoadTests(TempTree):
    """A worker that cannot load an engine that is not installed gets the launcher's name in front of its error."""

    WORKER_ERROR = "Worker load failed: ModuleNotFoundError: No module named 'kokoro'. Log: kokoro.log"

    def failing_pool(self, error, *, probe=missing_probe):
        return self.pool(FakeWorker(load_error=error), probe=probe)

    def test_speaking_and_loading_a_missing_kokoro_lead_with_the_launcher(self):
        actions = {"speak": lambda pool: pool.speak({"engine": "kokoro", "text": "Hello there."}),
                   "load": lambda pool: pool.load("kokoro")}
        for name, action in actions.items():
            with self.subTest(action=name):
                error = WorkerRequestError(self.WORKER_ERROR)
                pool = self.failing_pool(error)
                with self.assertRaises(WorkerRequestError) as raised:
                    action(pool)
                self.assertEqual(str(raised.exception), f"{POINTER} {self.WORKER_ERROR}")
                self.assertIs(raised.exception.__cause__, error)
                self.assertEqual(str(error), self.WORKER_ERROR)  # the worker's own error is not rewritten
                self.assertEqual(pool.health()["last_worker_error"]["message"], "worker failed to start/load")
                self.assertEqual(pool.loaded, [])

    def test_the_kind_and_status_of_the_worker_error_are_kept(self):
        for error, status in ((WorkerRequestError(self.WORKER_ERROR), 500),
                              (WorkerUnavailable("Worker exited during load (code 1). Log: kokoro.log"), 500),
                              (WorkerTimeout("Worker timed out during load after 600s. Log: kokoro.log"), 504)):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as raised:
                    self.failing_pool(error).load("kokoro")
                self.assertEqual(raised.exception.status_code, status)
                self.assertTrue(str(raised.exception).startswith(POINTER))

    def test_a_kokoro_that_is_installed_keeps_its_error_as_it_was(self):
        error = WorkerRequestError(self.WORKER_ERROR)
        with self.assertRaises(WorkerRequestError) as raised:
            self.failing_pool(error, probe=installed_probe).load("kokoro")
        self.assertIs(raised.exception, error)
        self.assertEqual(str(raised.exception), self.WORKER_ERROR)

    def test_an_engine_without_a_launcher_keeps_its_error_and_is_not_probed(self):
        asked = []

        def probe(entry):
            asked.append(entry["id"])
            return missing_probe(entry)

        error = WorkerRequestError("Worker load failed: ModuleNotFoundError: No module named 'chatterbox'. Log: turbo.log")
        with self.assertRaises(WorkerRequestError) as raised:
            self.failing_pool(error, probe=probe).load("turbo")
        self.assertIs(raised.exception, error)
        self.assertEqual(asked, [])

    def test_a_missing_environment_gets_the_pointer_before_any_worker_starts(self):
        error = WorkerUnavailable("Configured Python environment is missing for kokoro: .venv/Scripts/python.exe")

        def no_environment(entry):
            raise error

        pool = EnginePool("cpu", 0, worker_factory=no_environment, idle_poll_seconds=0.1, install_probe=missing_probe)
        self.addCleanup(pool.close)
        with self.assertRaises(WorkerUnavailable) as raised:
            pool.load("kokoro")
        self.assertEqual(str(raised.exception), f"{POINTER} {error}")

    def test_a_refusal_that_is_not_a_failed_load_is_not_mistaken_for_a_missing_install(self):
        asked = []

        def probe(entry):
            asked.append(entry["id"])
            return missing_probe(entry)

        def too_little_memory(key):
            raise MemoryError("Only 1.0 GB RAM free; not loading kokoro.")

        worker = FakeWorker()
        pool = EnginePool("cpu", 0, worker_factory=lambda entry: worker, idle_poll_seconds=0.1, install_probe=probe,
                          resource_check=too_little_memory)
        self.addCleanup(pool.close)
        with self.assertRaisesRegex(MemoryError, "^Only 1.0 GB"):
            pool.load("kokoro")
        self.assertTrue(pool._gpu_job.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(WorkerBusy, "^The GPU worker is busy"):
                pool.speak({"engine": "kokoro", "text": "Hello there."})
        finally:
            pool._gpu_job.release()
        self.assertEqual(asked, [])

    def test_a_probe_that_fails_does_not_hide_the_load_error(self):
        def broken(entry):
            raise OSError("the folder cannot be read")

        error = WorkerRequestError(self.WORKER_ERROR)
        with self.assertRaises(WorkerRequestError) as raised:
            self.failing_pool(error, probe=broken).load("kokoro")
        self.assertIs(raised.exception, error)

    def test_the_next_try_after_the_install_starts_a_fresh_worker(self):
        worker = FakeWorker(load_error=WorkerRequestError(self.WORKER_ERROR))
        pool = self.pool(worker, probe=missing_probe)
        with self.assertRaises(WorkerRequestError):
            pool.load("kokoro")
        worker.load_error = None  # the launcher has run
        self.assertTrue(pool.load("kokoro")["loaded"])
        self.assertEqual(pool.loaded, ["kokoro"])


class EngineStatusHttpTests(TempTree):
    def setUp(self):
        super().setUp()
        self.cast = CastStore(self.root)
        self.store = EngineCheckStore(self.root / "data" / "engines" / "checks.json")
        self.worker = FakeWorker()
        self.engines = self.pool(self.worker, store=self.store)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
        evaluations = serve.EvaluationStore(self.root / "evaluations.sqlite3", lab_root=self.root)
        self.server.RequestHandlerClass = serve.make_handler(self.engines, self.server.server_address[1], self.cast,
                                                             evaluation_store=evaluations)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")

    def test_check_then_status_reports_the_engine_usable(self):
        status, body = self.call("POST", "/api/engines/kokoro/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["check"]["status"], "passed")
        status, body = self.call("GET", "/api/engines/status")
        self.assertEqual(status, 200)
        kokoro = {row["id"]: row for row in body["engines"]}["kokoro"]
        self.assertEqual(kokoro["state"], "usable")
        self.assertEqual(body["device"], "cpu")
        self.assertTrue(body["speed"])

    def test_dotted_engine_ids_are_routed(self):
        status, body = self.call("POST", "/api/engines/qwen3-tts-1.7b-voice-design/check", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["check"]["status"], "passed")

    def test_task_and_unknown_engines_are_bad_requests(self):
        self.assertEqual(self.call("POST", "/api/engines/chatterbox-vc/check", {})[0], 400)
        self.assertEqual(self.call("POST", "/api/engines/not-an-engine/check", {})[0], 400)

    def test_a_busy_service_answers_409(self):
        self.assertTrue(self.engines._gpu_job.acquire(blocking=False))
        try:
            self.assertEqual(self.call("POST", "/api/engines/kokoro/check", {})[0], 409)
        finally:
            self.engines._gpu_job.release()

    def test_speaking_or_loading_before_kokoro_is_installed_leads_with_the_launcher(self):
        self.worker.load_error = WorkerRequestError(FailedLoadTests.WORKER_ERROR)
        self.engines.install_probe = lambda entry: installation(entry, self.root)  # nothing is installed in the private root
        for path, body in (("/api/speak", {"engine": "kokoro", "text": "Hello there."}), ("/api/load", {"engine": "kokoro"})):
            with self.subTest(path=path):
                status, answer = self.call("POST", path, body)
                self.assertEqual(status, 500)
                self.assertEqual(answer["error"], f"{POINTER} {FailedLoadTests.WORKER_ERROR}")

    def test_a_failed_load_of_an_installed_kokoro_keeps_its_own_error(self):
        self.worker.load_error = WorkerRequestError(FailedLoadTests.WORKER_ERROR)
        entry = CATALOG["kokoro"]
        self.write(entry["runtime"]["python"])
        self.write(f"hf-cache/hub/models--hexgrad--Kokoro-82M/snapshots/{entry['local_revision']}/kokoro-v1_0.pth")
        self.engines.install_probe = lambda entry: installation(entry, self.root)
        status, answer = self.call("POST", "/api/speak", {"engine": "kokoro", "text": "Hello there."})
        self.assertEqual((status, answer["error"]), (500, FailedLoadTests.WORKER_ERROR))


if __name__ == "__main__":
    unittest.main()
