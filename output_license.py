"""Output-licence profile for a take: what its engine's code, weights and outputs allow, and what the saved reference allows.

The Cast exporter writes this profile into every package, and saving a designed voice records it beside the new WAV.
It has its own module so that saving a design does not import the exporter and everything the exporter needs.
"""

import re

import engine_adapters

ENGINE_LICENSE_NOTES = {
    "breeze-tts-2": {
        "code_license": "Apache-2.0; retained upstream notices in licenses/ and THIRD_PARTY_NOTICES.md.",
        "weights_license": "BreezeBlue Research and Non-Commercial License 1.1; covers derived checkpoints.",
        "output_use": "Restricted: self-hosted outputs subject to research/non-commercial terms; review the complete model license.",
        "status": "restricted",
        "evidence": "THIRD_PARTY_NOTICES.md; pinned catalog/engines.json; complete upstream license."
    },
    "voxcpm2": {
        "code_license": "Apache-2.0 per the pinned upstream repository.",
        "weights_license": "Apache-2.0 per the pinned model card.",
        "output_use": "Unknown; review reference rights and intended distribution.",
        "status": "unknown",
        "evidence": "THIRD_PARTY_NOTICES.md; pinned catalog/engines.json; complete upstream license."
    },
    "drama-3": {
        "code_license": "MIT speech.cpp runtime, separately installed; runtime dependencies retain their own terms.",
        "weights_license": "Fish Audio Community License 1.0, including Q4 derivative.",
        "output_use": "Conditional commercial use, aggregate affiliate revenue below USD 500,000; hosted TTS/voice-cloning services need separate agreement. Built with Fish Audio. Review full model and reference terms.",
        "status": "restricted",
        "evidence": "https://huggingface.co/fishaudio/drama-3/blob/e0a9218aa48888270f06ef1ed9ea67622c0e17d2/LICENSE; derivative NOTICE."
    }
}
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
