"""Validated, checkpoint-specific controls for the installed Chatterbox TTS APIs, and where their pinned files are."""

import json
import math
from pathlib import Path

LAB = Path(__file__).resolve().parent
TURBO_ENGINE_ID = "turbo"
ORIGINAL_ENGINE_ID = "orig"
VC_ENGINE_ID = "chatterbox-vc"
# What ChatterboxTurboTTS.from_local reads from the snapshot. catalog/chatterbox_turbo_snapshot.json lists the same files with
# their checksums (plus the model card) for tools/install_chatterbox.py, and tests/test_install_chatterbox.py keeps them in step.
TURBO_FILES = ("added_tokens.json", "conds.pt", "merges.txt", "s3gen_meanflow.safetensors", "special_tokens_map.json",
               "t3_turbo_v1.safetensors", "tokenizer_config.json", "ve.safetensors", "vocab.json")
# What ChatterboxTTS.from_local (Chatterbox 500M) and ChatterboxVC.from_local read from the pinned ResembleAI/chatterbox
# snapshot, which catalog/chatterbox_snapshot.json lists with their checksums for install_chatterbox.py --original.
ORIGINAL_FILES = ("conds.pt", "s3gen.safetensors", "t3_cfg.safetensors", "tokenizer.json", "ve.safetensors")
VC_FILES = ("conds.pt", "s3gen.safetensors")


def snapshot_dir(engine_id, root=LAB):
    """The pinned hf-cache snapshot of a Chatterbox engine, at the revision catalog/engines.json names. Reads only the
    catalog."""
    catalog = json.loads((Path(root) / "catalog" / "engines.json").read_text(encoding="utf-8"))
    entry = next(item for item in catalog["engines"] if item["id"] == engine_id)
    return (Path(root) / "hf-cache" / "hub" / f"models--{entry['checkpoint'].replace('/', '--')}" / "snapshots"
            / entry["local_revision"])


def turbo_snapshot_dir(root=LAB):
    """The pinned hf-cache snapshot of Chatterbox Turbo."""
    return snapshot_dir(TURBO_ENGINE_ID, root)


def original_snapshot_dir(root=LAB):
    """The pinned hf-cache snapshot of ResembleAI/chatterbox, which Chatterbox 500M and Chatterbox VC load."""
    return snapshot_dir(ORIGINAL_ENGINE_ID, root)


CHATTERBOX_SAMPLING_FIELDS = {
    "orig": {"exaggeration", "cfg_weight", "temperature", "top_p", "repetition_penalty", "min_p"},
    "turbo": {"temperature", "top_k", "top_p", "repetition_penalty"},
}

CHATTERBOX_DEFAULTS = {
    "orig": {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8,
             "top_p": 1.0, "repetition_penalty": 1.2, "min_p": 0.05},
    "turbo": {"temperature": 0.8, "top_k": 1000, "top_p": 0.95, "repetition_penalty": 1.2},
}

_RANGES = {
    "exaggeration": (0.0, 2.0), "cfg_weight": (0.0, 1.0), "temperature": (0.05, 2.0),
    "top_p": (0.05, 1.0), "repetition_penalty": (0.5, 2.0), "min_p": (0.0, 1.0),
    "top_k": (1, 2048),
}


def normalize_chatterbox_sampling(engine_id, values):
    """Return all effective controls for one API variant; reject incompatible values."""
    if engine_id not in CHATTERBOX_DEFAULTS:
        raise ValueError(f"No Chatterbox TTS sampling schema for {engine_id!r}.")
    values = values or {}
    if not isinstance(values, dict):
        raise ValueError("Chatterbox sampling settings must be an object.")
    supported = CHATTERBOX_SAMPLING_FIELDS[engine_id]
    unsupported = set(values) - supported
    if unsupported:
        raise ValueError(f"Chatterbox {engine_id} does not support sampling parameter(s): "
                         f"{', '.join(sorted(unsupported))}.")
    result = dict(CHATTERBOX_DEFAULTS[engine_id])
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Chatterbox {key} must be a number.")
        numeric = float(value)
        low, high = _RANGES[key]
        if not math.isfinite(numeric) or not low <= numeric <= high:
            raise ValueError(f"Chatterbox {key} must be between {low} and {high}.")
        if key == "top_k" and not numeric.is_integer():
            raise ValueError("Chatterbox top_k must be a whole number.")
        result[key] = int(numeric) if key == "top_k" else numeric
    return result
