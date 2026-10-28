from __future__ import annotations

import json
import io
import struct
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path

from tools.dataset_workbench import DatasetError, PROJECT_ROOT, inspect_wav, main, prepare_dataset


def write_wave(path: Path, frequency: int, *, samples: int = 8000, rate: int = 16000, amplitude: int = 9000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values = []
    for index in range(samples):
        # A deterministic tone keeps each fixture's content hash distinct.
        value = int(amplitude * ((index * frequency) % rate) / rate)
        if index % 2:
            value = -value
        values.append(value)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack(f"<{len(values)}h", *values))


def approved_provenance(source_id: str) -> dict:
    return {
        "kind": "self_recorded",
        "origin": "synthetic unit-test fixture",
        "source_id": source_id,
        "rights_status": "approved",
        "use_scope": ["local_model_training"],
        "evidence": "Test-owned generated waveform; not a user recording.",
        "license": "test-only",
    }


class DatasetWorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audio_root = self.root / "audio"
        self.output_root = self.root / "bundles"
        self.audio_root.mkdir()
        self.items = []
        for index, text in enumerate(("A door opens.", "The train is late.", "We can leave now.", "Another door opens.")):
            relative = f"takes/take-{index}.wav"
            write_wave(self.audio_root / relative, 250 + index * 71)
            self.items.append({
                "id": f"take-{index}",
                "audio": relative,
                "text": text,
                "speaker_id": "actor-a",
                "provenance": approved_provenance(f"recording-{index}"),
            })
        write_wave(self.audio_root / "refs/actor-a.wav", 887)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_manifest(self, *, model: str = "qwen3-tts-1.7b-base", rights: str = "approved") -> Path:
        items = json.loads(json.dumps(self.items))
        if rights != "approved":
            items[0]["provenance"]["rights_status"] = rights
            if rights == "approved":
                items[0]["provenance"]["use_scope"] = ["local_model_training"]
        doc = {
            "schema_version": 1,
            "dataset_id": "dataset-test-voice",
            "target_model": model,
            "items": items,
            "shared_reference": {
                "audio": "refs/actor-a.wav",
                "speaker_id": "actor-a",
                "provenance": approved_provenance("reference-actor-a"),
            } if model.startswith("qwen") else None,
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def test_qwen_export_contains_raw_train_validation_and_reusable_reference(self) -> None:
        result = prepare_dataset(self.write_manifest(), self.audio_root, self.output_root, seed=31)
        bundle = Path(result["bundle_dir"])
        self.assertTrue(result["audit"]["model_manifests_exported"])
        self.assertEqual(result["audit"]["split_counts"]["train"] + result["audit"]["split_counts"]["validation"], 4)
        train_rows = [json.loads(line) for line in (bundle / "train.jsonl").read_text(encoding="utf-8").splitlines()]
        validation_rows = [json.loads(line) for line in (bundle / "validation.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(train_rows and validation_rows)
        self.assertEqual(set(train_rows[0]), {"audio", "text", "ref_audio"})
        self.assertEqual(train_rows[0]["ref_audio"], str((self.audio_root / "refs/actor-a.wav").resolve()))
        self.assertEqual(sum(map(len, (train_rows, validation_rows))), 4)
        self.assertFalse(list(bundle.rglob("*.wav")), "the workbench must not copy source audio")

    def test_same_inputs_reuse_the_content_addressed_version(self) -> None:
        first = prepare_dataset(self.write_manifest(), self.audio_root, self.output_root, seed=11)
        second = prepare_dataset(self.write_manifest(), self.audio_root, self.output_root, seed=11)
        self.assertEqual(first["audit"]["dataset_sha256"], second["audit"]["dataset_sha256"])
        self.assertTrue(second["audit"]["output_reused"])

    def test_modified_versioned_output_is_detected(self) -> None:
        first = prepare_dataset(self.write_manifest(), self.audio_root, self.output_root)
        bundle = Path(first["bundle_dir"])
        (bundle / "train.jsonl").write_text("modified\n", encoding="utf-8")
        with self.assertRaisesRegex(DatasetError, "artifact changed"):
            prepare_dataset(self.write_manifest(), self.audio_root, self.output_root)

    def test_cli_prepare_smoke_uses_scoped_output_and_prints_version(self) -> None:
        allowed_output = PROJECT_ROOT / "out" / "datasets"
        allowed_output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=allowed_output) as output_root:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main([
                    "prepare", "--manifest", str(self.write_manifest()),
                    "--audio-root", str(self.audio_root), "--output-root", output_root,
                ])
            result = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "ready_for_export")
            self.assertTrue((Path(result["bundle_dir"]) / "dataset.lock.json").is_file())
            self.assertEqual(len(result["dataset_version"]), 20)

    def test_rights_unknown_keeps_audit_but_blocks_model_manifests(self) -> None:
        result = prepare_dataset(self.write_manifest(rights="unknown"), self.audio_root, self.output_root)
        bundle = Path(result["bundle_dir"])
        self.assertFalse(result["audit"]["rights_clear_for_local_training"])
        self.assertFalse(result["audit"]["model_manifests_exported"])
        self.assertFalse((bundle / "train.jsonl").exists())
        self.assertTrue((bundle / "audit.json").is_file())

    def test_qwen_rejects_multiple_speakers(self) -> None:
        path = self.write_manifest()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["items"][1]["speaker_id"] = "actor-b"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(DatasetError, "single-speaker"):
            prepare_dataset(path, self.audio_root, self.output_root)

    def test_qwen_shared_reference_must_match_speaker(self) -> None:
        path = self.write_manifest()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["shared_reference"]["speaker_id"] = "actor-b"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(DatasetError, "must match every training item"):
            prepare_dataset(path, self.audio_root, self.output_root)

    def test_voxcpm_export_uses_pinned_audio_text_jsonl_and_speaker_holdout(self) -> None:
        doc = {
            "schema_version": 1,
            "dataset_id": "dataset-vox-test",
            "target_model": "voxcpm2",
            "items": json.loads(json.dumps(self.items)),
        }
        for index, item in enumerate(doc["items"]):
            item["speaker_id"] = f"actor-{index}"
        path = self.root / "voxcpm.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        result = prepare_dataset(path, self.audio_root, self.output_root, split_strategy="speaker", seed=0)
        bundle = Path(result["bundle_dir"])
        self.assertTrue(result["audit"]["model_manifests_exported"])
        train = [json.loads(line) for line in (bundle / "train.jsonl").read_text(encoding="utf-8").splitlines()]
        validation = [json.loads(line) for line in (bundle / "validation.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(set(train[0]), {"audio", "text"})
        train_audio = {row["audio"] for row in train}
        validation_audio = {row["audio"] for row in validation}
        self.assertFalse(train_audio & validation_audio)

    def test_path_traversal_is_rejected_before_output(self) -> None:
        path = self.write_manifest()
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["items"][0]["audio"] = "../outside.wav"
        path.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(DatasetError, "stay under --audio-root"):
            prepare_dataset(path, self.audio_root, self.output_root)
        self.assertFalse(self.output_root.exists())

    def test_audio_signal_checks_report_silence_and_clipping(self) -> None:
        silent = self.audio_root / "silent.wav"
        with wave.open(str(silent), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(struct.pack("<800h", *([0] * 800)))
        metrics = inspect_wav(silent)
        self.assertEqual(metrics["near_silent_frame_percent"], 100.0)
        self.assertEqual(metrics["duration_seconds"], 0.05)
        clipped = self.audio_root / "clipped.wav"
        with wave.open(str(clipped), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(struct.pack("<800h", *([-32768, 32767] * 400)))
        clipped_metrics = inspect_wav(clipped)
        self.assertEqual(clipped_metrics["clipped_sample_percent"], 100.0)

    def test_one_text_group_cannot_fabricate_a_validation_split(self) -> None:
        path = self.write_manifest()
        doc = json.loads(path.read_text(encoding="utf-8"))
        for item in doc["items"]:
            item["text"] = "Repeated line."
        path.write_text(json.dumps(doc), encoding="utf-8")
        result = prepare_dataset(path, self.audio_root, self.output_root)
        self.assertFalse(result["audit"]["model_manifests_exported"])
        self.assertEqual(result["audit"]["split_counts"]["validation"], 0)
        self.assertIn("held-out validation", result["audit"]["blockers"][0])


if __name__ == "__main__":
    unittest.main()
