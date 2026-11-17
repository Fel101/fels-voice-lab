"""Run recipe steps with local stores and the approved provider, when selected."""

import tempfile
import uuid
from pathlib import Path

import audio_tools
import automation
import connector_profiles
import dub_translation
import engine_adapters
import speech_input
import synthetic_label
from local_speech_client import LocalClient


def operate(lab, store, step, current, cancellation):
    operation, options, value = step["operation"], step["settings"], current["value"]
    if cancellation.is_set():
        raise speech_input.InputCancelled("Cancelled before the next step.")
    proof, profile = {}, None
    if step.get("connector"):
        profile = connector_profiles.get(step["provider"], operation)
        if profile["revision"] != step["connector"]["revision"]:
            raise ValueError("The provider settings changed after this run was queued.")
    if operation == "speak":
        if profile:
            clip = connector_profiles.speak(profile, value, options["voice"], options.get("speed", 1))
            proof = {"revision": profile["revision"], "model": profile["model"], "destination": profile["destination"]}
        else:
            clip = LocalClient(lab.port).speak(value, options["voice"], options.get("speed", 1), cancellation)
        output = store.put(clip, ".wav")
    elif operation == "transcribe":
        path = store.asset(value)
        result = speech_input.run(
            lab.scenes.root, path.read_bytes(), path.suffix, speech_input.settings(options), cancelled=cancellation
        )
        output, proof = result["text"], {"language": result["language"], "model": result["model"]}
    elif operation == "translate":
        source = dub_translation.locale(options["source_language"])
        target = dub_translation.locale(options["target_language"])
        output = connector_profiles.translate(profile, value, source, target)
        revision = profile["revision"]
        proof = {"source_language": source, "target_language": target, "revision": revision}
    elif operation == "normalize":
        path = store.asset(value)
        with tempfile.TemporaryDirectory(dir=store.root) as scratch:
            result = audio_tools.master(Path(scratch), path.read_bytes(), options)
            clip = Path(scratch) / "out" / "audio-tools" / result["id"] / "audio.wav"
            output = store.put(clip.read_bytes(), ".wav")
        proof = {
            "source_precision": result["source_precision"],
            "saved_precision": result["saved_precision"],
            "output_measurements": result["output_measurements"],
        }
    elif operation == "convert":
        source = store.asset(value)
        if source.suffix not in {".wav", ".flac", ".ogg"}:
            raise ValueError("Voice conversion needs a WAV, FLAC or Ogg source.")
        kwargs = {
            "request_id": "automation-" + uuid.uuid4().hex,
            "what": "Automation voice conversion",
            "wait_seconds": 180,
            "priority": "low",
            "alive": lambda: not cancellation.is_set(),
        }
        with lab.engines.queued(**kwargs):
            identity = lab.cast.get_generation_identity(options["identity_id"])
            if identity["status"] != "approved" or not identity.get("reference"):
                raise ValueError("Choose an approved Cast reference for voice conversion.")
            target = identity["reference"]
            samples, rate, _ = lab.engines.convert(str(source), target["audio_path"])
        output = store.put(synthetic_label.wav_bytes(samples, rate, engine_id=engine_adapters.only("converts")), ".wav")
        proof = {"reference_id": target["id"], "reference_sha256": target["audio_sha256"], "watermark": "Perth"}
    else:
        scene = lab.scenes.get_scene(value)
        options = {**options}
        render = options.pop("render_missing", False)
        (job,) = lab.dub_jobs.enqueue(
            [{"scene_id": value, "render_missing": render, "export": {**options, "expected_revision": scene["revision"]}}]
        )
        output, proof = job["id"], {"scene_id": value, "scene_revision": scene["revision"]}
    return {"kind": step["output_kind"], "value": output, "provider": step["provider"], "receipt": proof}


def create_runner(lab):
    return automation.Runner(automation.Store(lab.scenes.root), lambda *args: operate(lab, *args))
