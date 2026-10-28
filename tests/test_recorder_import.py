"""Voice Slate bridge: a kept clip is validated, copied into Cast with its provenance, and nothing else is touched."""

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import recorder_import as ri
import recorder_identity
from cast_store import CastStore
from recorder_clip import ClipRefused, find_sidecars, load_clip
from reference_paths import resolve_reference_path
from tests import recorder_fixtures as fx


def tree_hashes(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(folder).rglob("*")) if path.is_file()}


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="voice-lab-recorder-")
        self.addCleanup(self.temp.cleanup)
        self.clips = Path(self.temp.name) / "voice-clips"
        self.lab = Path(self.temp.name) / "lab"
        self.store = CastStore(self.lab)

    def make(self, **kwargs):
        return fx.write_clip(self.clips, **kwargs)

    def register(self, sidecar, **kwargs):
        kwargs.setdefault("character", "Narrator")
        kwargs.setdefault("create_character", True)
        kwargs.setdefault("identity_check", False)
        return ri.import_clip(sidecar, store=self.store, **kwargs)

    def assertStoreUntouched(self):
        self.assertFalse((self.lab / "data").exists(), "a refused clip must not create or change the Cast store")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ri.main([str(item) for item in argv])
        return code, out.getvalue(), err.getvalue()


class RegistrationTests(BridgeCase):
    def test_a_valid_clip_is_registered_with_its_metadata_intact(self):
        sidecar = self.make()
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        wav = sidecar.with_suffix(".wav")
        report = self.register(sidecar)

        self.assertEqual(report["status"], "registered")
        ref = self.store.get_reference(report["reference_id"])
        self.assertEqual(ref["transcript"], meta["transcript"])
        self.assertEqual(ref["allowed_use"], meta["allowed_use"])
        self.assertTrue(ref["origin"].startswith(meta["origin"]), "the person's own origin text comes first")
        self.assertIn(meta["id"], ref["origin"], "Voice Slate's clip id travels with the voice")
        self.assertEqual(ref["source_name"], meta["export"]["file"])
        self.assertEqual(ref["source_sha256"], meta["export"]["sha256"])
        self.assertEqual((ref["sample_rate"], ref["channels"]), (24_000, 1))
        self.assertAlmostEqual(ref["duration_seconds"], meta["export"]["duration_seconds"], places=2)

        identity = self.store.get_identity(report["identity_id"])
        self.assertEqual((identity["engine_id"], identity["kind"], identity["status"]), ("turbo", "reference", "candidate"))
        self.assertIsNone(identity["approved_reference_id"], "approving a voice stays with the person")

        folder = Path(report["folder"])
        self.assertEqual((folder / "source.wav").read_bytes(), wav.read_bytes(), "the copy is the kept clip, byte for byte")
        copied, _ = sf.read(str(folder / "reference.wav"), dtype="int16")
        original, _ = sf.read(str(wav), dtype="int16")
        np.testing.assert_array_equal(copied, original)
        self.assertEqual((folder / "recorder-clip.json").read_bytes(), sidecar.read_bytes(),
                         "the sidecar is copied verbatim beside the audio")
        record = json.loads((folder / "recorder-import.json").read_text(encoding="utf-8"))
        self.assertNotIn(Path(self.temp.name).name, json.dumps(record), "the record holds no path from this computer")
        clip = record["recorder_clip"]
        self.assertEqual((clip["id"], clip["speaker"], clip["schema"]), (meta["id"], meta["speaker"], meta["schema"]))
        self.assertEqual((clip["export_sha256"], clip["transcript_status"], clip["consent_confirmed"]),
                         (meta["export"]["sha256"], "typed", True))
        self.assertEqual(clip["sidecar_sha256"], hashlib.sha256(sidecar.read_bytes()).hexdigest())
        self.assertEqual(record["voice_lab"]["reference_id"], report["reference_id"])
        self.assertFalse(list(self.lab.rglob("*.raw.wav")), "the master recording is never copied")
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["recorder-clip.json", "recorder-import.json", "reference.wav", "source.wav"])

    def test_the_worker_policy_accepts_the_imported_copy_and_still_refuses_the_recorder_folder(self):
        sidecar = self.make()
        original = sidecar.with_suffix(".wav")
        with self.assertRaisesRegex(ValueError, "must be an existing file under"):
            resolve_reference_path("turbo", original, extra_roots=())
        report = self.register(sidecar)
        roots = (self.store.data_root / "media" / "references",)
        accepted = resolve_reference_path("turbo", report["reference_audio"], extra_roots=roots)
        self.assertEqual(accepted, Path(report["reference_audio"]).resolve())
        with self.assertRaises(ValueError):
            resolve_reference_path("turbo", original, extra_roots=roots)
        generation = self.store.get_generation_identity(report["identity_id"], report["reference_id"])
        self.assertEqual(generation["reference"]["id"], report["reference_id"], "the voice is usable for performance takes")

    def test_the_clip_id_and_allowed_use_reach_the_provenance_of_takes(self):
        sidecar = self.make()
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        report = self.register(sidecar)
        generation = self.store.get_generation_identity(report["identity_id"], report["reference_id"])
        take = self.store.create_take(generation, line_id="line-1", text="Hello.", spoken_text="Hello.", language="en",
                                      direction="", settings={}, model={"backend": "test"},
                                      wav=np.full(2400, 0.1, np.float32), sample_rate=24_000, generation_info={})
        self.assertIn(meta["id"], take["provenance"]["origin"])
        self.assertEqual(take["provenance"]["allowed_use"], meta["allowed_use"])
        self.assertEqual(take["provenance"]["source_sha256"], meta["export"]["sha256"])

    def test_nothing_in_the_recorder_folder_changes(self):
        sidecar = self.make()
        before = tree_hashes(self.clips)
        self.register(sidecar, identity_check=True, embed=lambda samples, rate: np.array([1.0, 0.0]))
        self.assertEqual(tree_hashes(self.clips), before)

    def test_the_same_clip_twice_registers_once_and_a_corrected_transcript_makes_a_new_version(self):
        sidecar = self.make()
        first = self.register(sidecar)
        again = self.register(sidecar)
        self.assertEqual((again["status"], again["reference_id"]), ("already_registered", first["reference_id"]))
        self.assertEqual(len(self.store.get_identity(first["identity_id"])["references"]), 1)

        fx.edited(sidecar, lambda data: data.update(transcript="The morning ferry left the harbour as the fog lifted.",
                                                    transcript_status="edited"))
        corrected = self.register(sidecar)
        self.assertEqual((corrected["status"], corrected["reference_version"]), ("registered", 2))
        self.assertTrue(any("registered before" in note for note in corrected["warnings"]))
        references = self.store.get_identity(first["identity_id"])["references"]
        self.assertEqual(sorted(ref["transcript"] for ref in references),
                         ["The morning ferry left the harbour as the fog lifted.",
                          "The morning ferry left the harbour just as the fog began to lift."],
                         "the earlier version is kept: the store is append-only")

    def test_an_import_that_stopped_before_its_provenance_was_written_is_completed_by_running_it_again(self):
        sidecar = self.make()
        first = self.register(sidecar)
        folder = Path(first["folder"])
        for name in ("recorder-clip.json", "recorder-import.json"):
            (folder / name).unlink()
        completed = self.register(sidecar)
        self.assertEqual((completed["status"], completed["reference_id"]), ("completed", first["reference_id"]))
        self.assertEqual((folder / "recorder-clip.json").read_bytes(), sidecar.read_bytes())
        self.assertEqual(len(self.store.get_identity(first["identity_id"])["references"]), 1)

    def test_a_second_clip_joins_the_same_candidate_voice(self):
        first = self.register(self.make())
        second = self.register(self.make(stem="20261020-163000_mic_bbbb2222", clip_id="bbbb2222",
                                         kept=fx.synth_voice(9.0, seed=4)), create_character=False)
        self.assertEqual((second["character_id"], second["identity_id"]), (first["character_id"], first["identity_id"]))
        self.assertEqual(second["created"], ["reference"])
        self.assertEqual([ref["version"] for ref in self.store.get_identity(first["identity_id"])["references"]], [2, 1])

    def test_another_engine_gets_its_own_voice_and_a_non_cloning_engine_is_refused(self):
        sidecar = self.make()
        turbo = self.register(sidecar)
        qwen = self.register(sidecar, engine_id="qwen3-tts-1.7b-base", create_character=False)
        self.assertNotEqual(qwen["identity_id"], turbo["identity_id"])
        with self.assertRaisesRegex(ClipRefused, "not a reference-cloning engine"):
            self.register(sidecar, engine_id="kokoro")


class ValidationTests(BridgeCase):
    def test_a_wav_whose_bytes_changed_is_refused_and_nothing_is_written(self):
        sidecar = self.make()
        wav = sidecar.with_suffix(".wav")
        data = bytearray(wav.read_bytes())
        data[-200] ^= 0x01
        wav.write_bytes(bytes(data))
        with self.assertRaisesRegex(ClipRefused, "SHA-256"):
            load_clip(sidecar)
        with self.assertRaisesRegex(ClipRefused, "SHA-256"):
            self.register(sidecar)
        self.assertStoreUntouched()

    def test_a_sidecar_without_consent_is_refused(self):
        changes = {"false": lambda s: s.update(consent_confirmed=False), "missing": lambda s: s.pop("consent_confirmed"),
                   "text": lambda s: s.update(consent_confirmed="true"), "number": lambda s: s.update(consent_confirmed=1)}
        for label, change in changes.items():
            with self.subTest(label):
                sidecar = self.make(stem=f"20261020-161204_mic_c{label}", clip_id=f"c{label}", change=change)
                with self.assertRaisesRegex(ClipRefused, "consent_confirmed"):
                    self.register(sidecar)
        self.assertStoreUntouched()

    def test_an_exported_file_outside_the_sidecars_folder_is_refused_even_when_its_hash_is_right(self):
        outside = Path(self.temp.name) / "outside.wav"
        outside.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(outside), fx.synth_voice(8.0), 24_000, subtype="PCM_16")
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        names = {"parent": "../../outside.wav", "subfolder": "masters/x.wav", "absolute": str(outside),
                 "drive": "C:\\Windows\\x.wav", "unc": "\\\\server\\share\\x.wav", "stream": "clip.wav:stream",
                 "dots": "..", "empty": ""}
        for label, name in names.items():
            with self.subTest(label):
                sidecar = self.make(stem=f"20261020-161204_mic_e{label}", clip_id=f"e{label}",
                                    change=lambda s, n=name: (s["export"].update(file=n, sha256=digest)))
                with self.assertRaisesRegex(ClipRefused, "export.file"):
                    self.register(sidecar)
        self.assertStoreUntouched()

    def test_a_master_outside_the_sidecars_folder_is_refused(self):
        outside = Path(self.temp.name) / "outside.raw.wav"
        outside.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(outside), fx.synth_voice(8.0), 24_000, subtype="PCM_24")
        names = {"parent": "../../outside.raw.wav", "up-and-down": "masters/../../../outside.raw.wav",
                 "absolute": str(outside), "drive": "C:/Windows/x.raw.wav", "unc": "//server/share/x.raw.wav"}
        for label, name in names.items():
            with self.subTest(label):
                sidecar = self.make(stem=f"20261020-161204_mic_m{label}", clip_id=f"m{label}"[:12],
                                    change=lambda s, n=name: s["master"].update(file=n))
                with self.assertRaisesRegex(ClipRefused, "master.file"):
                    self.register(sidecar)
        self.assertStoreUntouched()

    def test_a_link_that_points_out_of_the_folder_is_refused(self):
        outside = Path(self.temp.name) / "elsewhere"
        outside.mkdir(parents=True)
        sf.write(str(outside / "x.raw.wav"), fx.synth_voice(8.0), 24_000, subtype="PCM_24")
        tried = 0
        for kind in ("symlink", "junction"):
            sidecar = self.make(stem=f"20261020-161204_mic_{kind}", clip_id=kind)
            if kind == "symlink":
                link = sidecar.parent / "masters" / "link.raw.wav"
                try:
                    os.symlink(outside / "x.raw.wav", link)
                except (OSError, NotImplementedError):
                    continue
                name = "masters/link.raw.wav"
            else:                                   # a Windows directory junction needs no special privilege
                link = sidecar.parent / "outside-masters"
                made = os.name == "nt" and subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                                          capture_output=True).returncode == 0
                if not made:
                    continue
                self.addCleanup(os.rmdir, link)     # removes the junction itself, never what it points to
                name = "outside-masters/x.raw.wav"
            tried += 1
            fx.edited(sidecar, lambda data, name=name: data["master"].update(file=name))
            with self.subTest(kind), self.assertRaisesRegex(ClipRefused, "master.file"):
                load_clip(sidecar)
        if not tried:
            self.skipTest("this account can make neither symbolic links nor junctions")

    def test_a_missing_or_blank_transcript_origin_or_allowed_use_is_refused(self):
        for key in ("transcript", "origin", "allowed_use"):
            for label, value in (("blank", "   "), ("missing", None), ("number", 7)):
                with self.subTest(key=key, case=label):
                    def change(s, key=key, value=value):
                        s.pop(key) if value is None else s.update({key: value})
                    sidecar = self.make(stem=f"20261020-161204_mic_{key[:3]}{label}", clip_id=f"{key[:3]}{label}", change=change)
                    with self.assertRaisesRegex(ClipRefused, key):
                        self.register(sidecar)
        self.assertStoreUntouched()

    def test_an_asr_draft_transcript_is_refused_unless_it_is_accepted_on_purpose(self):
        sidecar = self.make(change=lambda s: s.update(transcript_status="asr_draft"))
        with self.assertRaisesRegex(ClipRefused, "asr_draft"):
            self.register(sidecar)
        self.assertStoreUntouched()
        report = self.register(sidecar, accept_asr_draft=True)
        self.assertTrue(any("asr_draft" in note for note in report["warnings"]))
        record = json.loads((Path(report["folder"]) / "recorder-import.json").read_text(encoding="utf-8"))
        self.assertEqual(record["recorder_clip"]["transcript_status"], "asr_draft", "the status is kept, not hidden")

    def test_the_schema_prefix_decides_what_is_read(self):
        def v1(data):
            data["schema"] = "voice-recorder-clip-v1"
            data.pop("chain")
            data.update(gain_db=5.1, denoise={"nf": -25.0})
        clip = load_clip(self.make(stem="20261020-161204_mic_old00001", clip_id="old00001", change=v1))
        self.assertEqual(clip.schema, "voice-recorder-clip-v1")
        self.assertEqual((clip.denoise["enabled"], clip.denoise["nf"]), (True, -25.0), "v1 keeps its denoise at top level")
        for label, value in (("other", "someone-elses-clip-v1"), ("blank", ""), ("number", 2)):
            with self.subTest(label):
                sidecar = self.make(stem=f"20261020-161204_mic_s{label}", clip_id=f"s{label}",
                                    change=lambda s, v=value: s.update(schema=v))
                with self.assertRaisesRegex(ClipRefused, "schema"):
                    load_clip(sidecar)
        missing = self.make(stem="20261020-161204_mic_snone", clip_id="snone", change=lambda s: s.pop("schema"))
        with self.assertRaisesRegex(ClipRefused, "schema"):
            load_clip(missing)

    def test_the_wav_must_be_the_format_the_sidecar_declares(self):
        sidecar = self.make(change=lambda s: s["export"].update(sample_rate=48_000))
        with self.assertRaisesRegex(ClipRefused, "sample_rate"):
            load_clip(sidecar)

    def test_a_turbo_reference_of_five_seconds_or_less_is_refused_before_anything_is_written(self):
        sidecar = self.make(kept=fx.synth_voice(4.5))
        with self.assertRaisesRegex(ClipRefused, "longer than 5 s"):
            self.register(sidecar)
        self.assertStoreUntouched()

    def test_text_the_store_would_refuse_is_caught_before_the_first_write(self):
        sidecar = self.make(change=lambda s: s.update(origin="x" * 990))
        with self.assertRaisesRegex(ClipRefused, "origin"):
            self.register(sidecar)
        silent = self.make(stem="20261020-161204_mic_silent01", clip_id="silent01", kept=np.zeros(24_000 * 8, np.float32))
        with self.assertRaisesRegex(ClipRefused, "silent"):
            self.register(silent)
        self.assertStoreUntouched()

    def test_the_character_must_exist_or_be_created_on_purpose(self):
        sidecar = self.make()
        with self.assertRaisesRegex(ClipRefused, "no Cast character is named"):
            self.register(sidecar, create_character=False)
        with self.assertRaisesRegex(ClipRefused, "has the id"):
            self.register(sidecar, character="a" * 32, create_character=True)
        self.assertStoreUntouched()
        self.store.create_character("Narrator")
        twin = self.store.create_character("narrator")
        with self.assertRaisesRegex(ClipRefused, "2 characters are named"):
            self.register(sidecar, create_character=False)
        report = self.register(sidecar, character=twin["id"], create_character=False)
        self.assertEqual(report["created"], ["identity", "reference"])

    def test_a_sidecar_that_is_not_json_or_is_enormous_is_refused(self):
        folder = self.clips / "my-voice"
        folder.mkdir(parents=True)
        bad = folder / "bad.json"
        bad.write_bytes(b"\xff\xfe not json")
        with self.assertRaisesRegex(ClipRefused, "UTF-8 JSON"):
            load_clip(bad)
        bad.write_text("[" * 100_000, encoding="utf-8")
        with self.assertRaisesRegex(ClipRefused, "UTF-8 JSON"):
            load_clip(bad)
        bad.write_text(" " * (1 << 20) + "{}", encoding="utf-8")
        with self.assertRaisesRegex(ClipRefused, "larger than 1 MiB"):
            load_clip(bad)


class CommandLineTests(BridgeCase):
    def test_a_dry_run_lists_and_validates_and_writes_nothing(self):
        good = self.make()
        self.make(stem="20261020-163000_mic_bbbb2222", clip_id="bbbb2222", change=lambda s: s.update(consent_confirmed=False))
        (good.parent.parent / "speakers.json").write_text("{}", encoding="utf-8")
        (good.parent / "notes.json").write_text('{"schema": "something-else"}', encoding="utf-8")
        before = tree_hashes(self.clips)
        code, out, _ = self.run_cli(good.parent, "--character", "Narrator", "--create-character", "--store-root", self.lab)
        self.assertEqual(code, 0)
        self.assertIn("[ok] 20261020-161204_mic_3f9a1c2e.json", out)
        self.assertIn("[REFUSED] 20261020-163000_mic_bbbb2222.json", out)
        self.assertIn("would use: character 'Narrator' (new)", out)
        self.assertIn("Nothing was imported", out)
        self.assertEqual(tree_hashes(self.clips), before)
        self.assertStoreUntouched()
        self.assertEqual([path.name for path in find_sidecars(good.parent)],
                         ["20261020-161204_mic_3f9a1c2e.json", "20261020-163000_mic_bbbb2222.json"])

    def test_registering_from_a_folder_needs_the_clips_to_be_named(self):
        good = self.make()
        code, _, err = self.run_cli(good.parent, "--register", "--character", "Narrator", "--create-character",
                                    "--store-root", self.lab)
        self.assertEqual(code, 2)
        self.assertIn("--clip", err)
        self.assertStoreUntouched()
        code, _, err = self.run_cli(good, "--register", "--store-root", self.lab)
        self.assertEqual((code, "--character" in err), (2, True))

    def test_register_imports_only_the_named_clip(self):
        first = self.make()
        self.make(stem="20261020-163000_mic_bbbb2222", clip_id="bbbb2222", kept=fx.synth_voice(9.0, seed=4))
        code, out, _ = self.run_cli(first.parent, "--register", "--character", "Narrator", "--create-character",
                                    "--clip", "3f9a1c2e", "--no-identity-check", "--store-root", self.lab)
        self.assertEqual(code, 0, out)
        references = [ref for char in self.store.list_cast() for ident in char["identities"] for ref in ident["references"]]
        self.assertEqual([ref["source_name"] for ref in references], ["20261020-161204_mic_3f9a1c2e.wav"])
        code, out, _ = self.run_cli(first.parent, "--register", "--character", "Narrator", "--all", "--no-identity-check",
                                    "--store-root", self.lab)
        self.assertEqual(code, 0, out)
        self.assertIn("[already_registered]", out)
        self.assertIn("[registered]", out)

    def test_json_output_and_exit_codes_say_what_happened(self):
        good = self.make()
        refused = self.make(stem="20261020-163000_mic_bbbb2222", clip_id="bbbb2222",
                            change=lambda s: s.update(consent_confirmed=False))
        code, out, _ = self.run_cli(good, "--json", "--store-root", self.lab)
        entries = json.loads(out)
        self.assertEqual((code, entries[0]["status"], entries[0]["clip"]["id"]), (0, "valid", "3f9a1c2e"))
        code, out, _ = self.run_cli(refused, "--json", "--store-root", self.lab)
        entries = json.loads(out)
        self.assertEqual((code, entries[0]["status"]), (2, "refused"))
        self.assertIn("consent_confirmed", entries[0]["problems"][0])
        code, _, err = self.run_cli(self.clips, "--store-root", self.lab)
        self.assertEqual(code, 2)
        self.assertIn("speaker folders", err)

    def test_a_wav_path_finds_its_sidecar(self):
        good = self.make()
        code, out, _ = self.run_cli(good.with_suffix(".wav"), "--store-root", self.lab)
        self.assertEqual(code, 0)
        self.assertIn("3f9a1c2e", out)


class StoredCheckTests(BridgeCase):
    def test_registering_stores_the_identity_check_in_voice_lab_and_never_in_the_sidecar(self):
        sidecar = self.make()
        sidecar_bytes = sidecar.read_bytes()
        report = self.register(sidecar, identity_check=True, embed=lambda samples, rate: np.array([0.6, 0.8]))
        self.assertEqual(report["identity_check"]["status"], "checked")
        stored = Path(report["identity_check_file"])
        self.assertEqual(stored.parent, Path(report["folder"]))
        self.assertTrue(stored.name.startswith("identity-check-"))
        self.assertEqual(json.loads(stored.read_text(encoding="utf-8"))["score"], report["identity_check"]["score"])
        self.assertEqual(sidecar.read_bytes(), sidecar_bytes, "Voice Slate's sidecar is never written to")
        self.assertEqual(len(recorder_identity.load_results(self.store, report["reference_id"])), 1)
        again = self.register(sidecar, identity_check=True, embed=lambda samples, rate: np.array([0.6, 0.8]))
        self.assertEqual(again["status"], "already_registered")
        self.assertEqual(len(recorder_identity.load_results(self.store, report["reference_id"])), 2,
                         "a later check adds to the history instead of replacing it")


if __name__ == "__main__":
    unittest.main()
