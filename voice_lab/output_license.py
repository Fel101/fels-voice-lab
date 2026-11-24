"""The licence ratings and detailed output terms for models, Cast voices and takes.

The Cast exporter writes this profile into every package, and saving a designed voice records it beside the new WAV.
It has its own module so that saving a design does not import the exporter and everything the exporter needs.
"""

import re
from functools import lru_cache

from voice_lab import engine_adapters
from voice_lab.worker_pool import read_catalog

LICENCE_RATINGS = ("yes", "conditions", "unclear", "no")


@lru_cache(maxsize=1)
def _model_entries():
    return {entry["id"]: entry for entry in read_catalog()["engines"]}


def model_licence(engine_id, *, preset=False):
    entry = _model_entries().get(engine_id, {})
    if preset and entry.get("preset_licence"):
        return entry["preset_licence"]
    return entry.get("licence") or {"rating": "unclear", "reason": "The model has no licence rating in the Lab catalog."}


def worst_licence(*licences):
    """Keep the most restrictive rating and explain every distinct reason for it."""
    rating = max((item["rating"] for item in licences), key=LICENCE_RATINGS.index)
    reasons = dict.fromkeys(item["reason"] for item in licences if item["rating"] == rating)
    return {"rating": rating, "reason": " ".join(reasons)}


def voice_licence(engine_id, kind, recording_rating=None):
    model = model_licence(engine_id, preset=kind in {"preset_voice", "fixed_voice"})
    if kind != "reference":
        return model
    recording = {
        "rating": recording_rating or "unclear",
        "reason": ("The recording's allowed use was rated in Cast." if recording_rating else
                   "The recording's allowed use has no rating yet. Review it and save a rating in Cast."),
    }
    return worst_licence(model, recording)


def take_licence(take, recording_rating):
    kind = "reference" if take["reference_version_id"] else "preset_voice"
    return voice_licence(take["engine_id"], kind, recording_rating)

ENGINE_LICENSE_NOTES = {}
# The engines whose adapters say (engine_adapters/).
ENGINE_LICENSE_NOTES.update(engine_adapters.table("output_license"))


def license_profile(engine_id, model, provenance):
    profile = dict(ENGINE_LICENSE_NOTES.get(engine_id, {
        "code_license": "Unknown in the local inventory.",
        "weights_license": "Unknown in the local inventory.",
        "output_use": "Unknown; review the model, reference, and subject rights before redistribution.",
        "status": "unknown",
        "evidence": "No verified local license record for this engine.",
    }))
    # Keep catalog-provided checkpoint terms visible, but do not mistake them for
    # code or output rights.
    if model.get("license") and profile["weights_license"].startswith("Unknown"):
        profile["weights_license"] = model["license"]
    allowed_use = provenance.get("allowed_use")
    if allowed_use:
        profile["recorded_reference_allowed_use"] = allowed_use
        use_key = re.sub(r"[^a-z]+", " ", str(allowed_use).lower())
        if any(term in use_key for term in (
            "private audition", "no redistribution", "not for redistribution",
            "non commercial", "noncommercial", "internal use only", "personal use only",
        )):
            profile["status"] = "restricted"
            profile["output_use"] = f"Restricted by saved reference provenance: {allowed_use}"
    else:
        profile["recorded_reference_allowed_use"] = "Unknown; no reference/output-use term was recorded."
        if profile["status"] != "restricted":
            profile["status"] = "unknown"
    return profile
