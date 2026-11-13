"""Voice conversion: the source performances and target voices the page offers, a file chosen from this computer
(serve.save_conversion_upload), and the conversion itself (POST /api/convert), with its manifest."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import uuid

from cast_store import MAX_UPLOAD_BYTES
import engine_adapters
import synthetic_label
from worker_pool import WorkerError


def register(router, lab):
    """The Conversion rows of api_spec.ROUTES, and the lists the page reads."""
    serve, engines, cast = lab.serve, lab.engines, lab.cast

    @router.get("/api/conversion/sources")
    def conversion_sources(self, parsed, req):
        self._json(200, {"sources": serve._conversion_asset_rows(cast, target=False)})

    @router.get("/api/conversion/targets")
    def conversion_targets(self, parsed, req):
        self._json(200, {"targets": serve._conversion_asset_rows(cast, target=True)})

    @router.post("/api/conversion/uploads")
    def conversion_upload(self, parsed, req):
        body = self._read_body(MAX_UPLOAD_BYTES)
        try:
            metadata = self._reference_metadata()
        except ValueError as exc:
            raise ValueError("Send the file's name and role as base64url JSON in the X-Reference-Metadata header.") from exc
        self._json(201, {"upload": serve.save_conversion_upload(body, metadata)})

    @router.post("/api/convert")
    def convert(self, parsed, req):
        source_path, source_info = serve._conversion_audio_path(
            req.get("source_path"), "Source performance", target=False, cast=cast
        )
        target_path, target_info = serve._conversion_audio_path(
            req.get("target_reference_path"), "Target voice reference", target=True, cast=cast
        )
        if source_path == target_path:
            raise ValueError("Source performance and target voice reference must be separate files.")
        reference_id = req.get("target_reference_id")
        if reference_id:
            reference = cast.get_reference(reference_id)
            recorded_target = Path(cast.get_media_path("reference", reference_id)).resolve()
            if recorded_target != target_path:
                raise ValueError("Target reference path does not match the selected Cast reference version.")
            target_metadata = {
                "reference_version_id": reference["id"],
                "origin": reference.get("origin", ""),
                "allowed_use": reference.get("allowed_use", ""),
            }
        else:
            target_metadata = next(
                (row for row in serve._conversion_asset_rows(cast, target=True) if Path(row["path"]).resolve() == target_path),
                {},
            )
        try:
            wav, sample_rate, engine_info = engines.convert(str(source_path), str(target_path))
        except WorkerError:
            raise
        import numpy as np

        audio = np.asarray(wav, dtype=np.float32).reshape(-1)
        if not audio.size or not np.isfinite(audio).all():
            raise RuntimeError("Chatterbox voice conversion produced empty or invalid audio.")
        source_upload = serve._upload_record(source_path) or {}  # a file chosen from this computer: the name it had there
        conversion_id = uuid.uuid4().hex
        artifact_dir = serve.OUT / "conversions" / conversion_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        audio_path = artifact_dir / "converted.wav"
        manifest_path = artifact_dir / "manifest.json"
        wav_bytes = synthetic_label.wav_bytes(audio, int(sample_rate), engine_id=engine_adapters.only("converts"))
        with audio_path.open("xb") as output:
            output.write(wav_bytes)
        manifest = {
            "schema_version": 1,
            "conversion_id": conversion_id,
            "created_at_local": datetime.now().astimezone().isoformat(),
            "task": "voice_conversion",
            "watermark": "PerthImplicitWatermarker",
            "model": engine_info,
            "source_performance": {
                "path": str(source_path),
                "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "sample_rate_hz": source_info.samplerate,
                "duration_seconds": round(float(source_info.duration), 4),
                **(
                    {
                        "cast_take_id": next(
                            (
                                row.get("take_id")
                                for row in serve._conversion_asset_rows(cast)
                                if Path(row["path"]).resolve() == source_path
                            ),
                            None,
                        )
                    }
                ),
                **({"original_name": source_upload["original_name"]} if source_upload.get("original_name") else {}),
            },
            "target_voice_reference": {
                "path": str(target_path),
                "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                "sample_rate_hz": target_info.samplerate,
                "duration_seconds": round(float(target_info.duration), 4),
                **target_metadata,
            },
            "output": {
                "path": str(audio_path.resolve()),
                "sha256": hashlib.sha256(wav_bytes).hexdigest(),
                "sample_rate_hz": int(sample_rate),
                "duration_seconds": len(audio) / int(sample_rate),
            },
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        audio_url = "/out/" + str(audio_path.resolve().relative_to(serve.OUT.resolve())).replace("\\", "/")
        self._json(
            201,
            {
                "conversion": {
                    "conversion_id": conversion_id,
                    "audio_url": audio_url,
                    "audio_path": str(audio_path.resolve()),
                    "manifest_path": str(manifest_path.resolve()),
                    "manifest": manifest,
                }
            },
        )
