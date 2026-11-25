"""The licence ratings and detailed output terms for models, Cast voices and takes.

The Cast exporter writes this profile into every package, and saving a designed voice records it beside the new WAV.
It has its own module so that saving a design does not import the exporter and everything the exporter needs.
"""

import re
from functools import lru_cache

from voice_lab import engine_adapters
from voice_lab.worker_pool import read_catalog

LICENCE_RATINGS = ("yes", "conditions", "unclear", "no")
# How a designed recording's origin begins (reference_catalog.design_provenance writes it). No person is in such a recording,
# so its terms are those of the model that designed it.
DESIGN_ORIGIN = "Original synthetic voice design via "


@lru_cache(maxsize=1)
def _model_entries():
    return {entry["id"]: entry for entry in read_catalog()["engines"]}


def catalog_licence(entry, *, preset=False):
    """A catalog entry's licence rating and reason, or with preset=True its preset voices', where the catalog rates them apart."""
    if preset and entry.get("preset_licence"):
        return entry["preset_licence"]
    return entry.get("licence") or {"rating": "unclear", "reason": "The model has no licence rating in the Lab catalog."}


def model_licence(engine_id, *, preset=False):
    return catalog_licence(_model_entries().get(engine_id, {}), preset=preset)


def worst_licence(*licences):
    """Keep the most restrictive rating and explain every distinct reason for it."""
    rating = max((item["rating"] for item in licences), key=LICENCE_RATINGS.index)
    reasons = dict.fromkeys(item["reason"] for item in licences if item["rating"] == rating)
    return {"rating": rating, "reason": " ".join(reasons)}


def designer_of(origin):
    """The model id a designed recording's origin names, or None for any other recording."""
    if not isinstance(origin, str) or not origin.startswith(DESIGN_ORIGIN):
        return None
    return origin[len(DESIGN_ORIGIN):].split(";", 1)[0].strip() or None


def recording_licence(rating=None, origin=None):
    """A recording's own rating: the one saved in Cast, else its design model's for a designed recording, else unclear."""
    if rating:
        return {"rating": rating, "reason": "The recording's allowed use was rated in Cast."}
    designer = designer_of(origin)
    if designer:
        model = model_licence(designer)
        name = _model_entries().get(designer, {}).get("name", designer)
        return {"rating": model["rating"], "reason": f"The recording was designed with {name}: {model['reason']}"}
    return {"rating": "unclear", "reason": "The recording's allowed use has no rating yet. Review it and save a rating in Cast."}


def voice_licence(engine_id, kind, recording_rating=None, recording_origin=None):
    model = model_licence(engine_id, preset=kind in {"preset_voice", "fixed_voice"})
    if kind != "reference":
        return model
    return worst_licence(model, recording_licence(recording_rating, recording_origin))


def take_licence(take, recording_rating, recording_origin=None):
    kind = "reference" if take["reference_version_id"] else "preset_voice"
    return voice_licence(take["engine_id"], kind, recording_rating, recording_origin)

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
