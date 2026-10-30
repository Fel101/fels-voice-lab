"""Small, offline-safe helpers for the installed Phonon files, and the checks every Phonon request goes through.

Phonon 31M English speaks with one of 37 preset voices. They are the English entries of voices.jsonl in the pinned
gradium/phonon-voices snapshot, so the list is read from disk: it never needs the model, the runtime or the network. The pins
come from catalog/engines.json, the one place a revision is written down for the service (the installer reads its own copy,
catalog/phonon_snapshot.json, and a test keeps the two equal).
"""

import json
import math
from pathlib import Path

from common import LAB

PHONON_ENGINE_ID = "phonon-31m-en"
CATALOG = "catalog/engines.json"
MODEL_FILES = ("config.json", "default-voice.safetensors", "model.q8.gguf", "tokenizer.json")
VOICE_CATALOG = "voices.jsonl"
DEFAULT_VOICE = "Harper"
LANGUAGE = "English"
# Every spelling of English the picker and the Cast use. The voices are American, British or characters; none is a language choice.
_ENGLISH = {"english", "en", "en-us", "en-gb", "american english", "british english"}
GROUPS = ("American English", "British English", "Characters")
_GROUP_OF_LOCALE = {"en-us": "American English", "en-gb": "British English", "characters": "Characters"}
SEED_RANGE = (0, 2147483647)
TEMPERATURE_RANGE = (0.0, 1.0)
DURATION_DELTA_RANGE = (-0.5, 0.5)
DEFAULTS = {"seed": 42, "temperature": 0.3, "duration_delta": 0.0}
REQUEST_FIELDS = frozenset({"engine", "text", "voice", "language", *DEFAULTS})
NOT_INSTALLED = ("Phonon 31M is not installed (no voices were found). Open Models & features and press Install on its card, "
                 "or run setup-phonon.bat in the lab folder.")


def pins(root=None):
    """The catalog's pins for Phonon: the model and the voice catalog, each as (repository, revision)."""
    catalog = json.loads((Path(root or LAB) / CATALOG).read_text(encoding="utf-8"))
    entry = next(item for item in catalog["engines"] if item["id"] == PHONON_ENGINE_ID)
    backend = entry["backend"]
    return {"model": (entry["checkpoint"], entry["local_revision"]),
            "voices": (backend["voices_checkpoint"], backend["voices_revision"])}


def snapshot_dir(repo, revision, root=None):
    return Path(root or LAB) / "hf-cache" / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / revision


def snapshot_dirs(root=None):
    """The pinned model snapshot and the pinned voice-catalog snapshot, whether or not they are on disk."""
    found = pins(root)
    return snapshot_dir(*found["model"], root), snapshot_dir(*found["voices"], root)


def missing_files(root=None):
    """What the engine needs that is not in place, as paths below hf-cache/hub; empty when it can load."""
    model, voices = snapshot_dirs(root)
    wanted = [model / name for name in MODEL_FILES] + [voices / VOICE_CATALOG]
    return [path.relative_to(Path(root or LAB) / "hf-cache" / "hub").as_posix() for path in wanted if not path.is_file()]


def _group(record):
    return _GROUP_OF_LOCALE.get(str(record.get("locale") or ""), "American English")


def discover_phonon_voices(root=None):
    """The English preset voices whose files are installed: dicts with name, group, gender, age, locale and description.

    American voices come first, then British, then the characters; each group is in name order."""
    _model, voices = snapshot_dirs(root)
    try:
        lines = (voices / VOICE_CATALOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    base = voices.resolve()
    found = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("language") != "en":
            continue
        name, filename = record.get("name"), record.get("filename")
        if not isinstance(name, str) or not name.strip() or not isinstance(filename, str):
            continue
        path = (voices / filename).resolve()
        if base not in path.parents or not path.is_file():
            continue
        group = _group(record)
        found.append({"name": name, "group": group, "character": group == "Characters", "gender": record.get("gender"),
                      "age": record.get("age"), "locale": record.get("locale"), "description": record.get("description") or ""})
    return sorted(found, key=lambda item: (GROUPS.index(item["group"]), item["name"].casefold()))


def voice_names(root=None):
    return [item["name"] for item in discover_phonon_voices(root)]


def default_voice(names):
    if not names:
        raise ValueError(NOT_INSTALLED)
    return DEFAULT_VOICE if DEFAULT_VOICE in names else names[0]


def resolve_voice(value, names):
    """The installed voice a request means, matched without regard to case; the default voice when none is named."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default_voice(names)
    if not isinstance(value, str):
        raise ValueError("A Phonon voice is chosen by its name.")
    if not names:
        raise ValueError(NOT_INSTALLED)
    found = {name.casefold(): name for name in names}.get(value.strip().casefold())
    if found is None:
        shown = ", ".join(names[:4])
        raise ValueError(f"Phonon has no installed voice named {value.strip()!r}. {len(names)} are installed, for example "
                         f"{shown}; GET /api/info lists them all.")
    return found


def normalize_language(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return LANGUAGE
    if not isinstance(value, str) or value.strip().casefold() not in _ENGLISH:
        raise ValueError(f"Phonon 31M speaks English only, so {value!r} is not available. Its voices are American, British "
                         "and characters.")
    return LANGUAGE


def _number(source, key, whole=False):
    low, high = {"seed": SEED_RANGE, "temperature": TEMPERATURE_RANGE, "duration_delta": DURATION_DELTA_RANGE}[key]
    value = source.get(key)
    if value is None:
        value = DEFAULTS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Phonon {key} must be a number.")
    if whole and int(value) != value:
        raise ValueError(f"Phonon {key} must be a whole number.")
    if not low <= value <= high:
        raise ValueError(f"Phonon {key} must be between {low:g} and {high:g}.")
    return int(value) if whole else float(value)


def normalize_settings(source):
    """The seed, temperature and duration shift of a request, defaults filled in and ranges checked."""
    return {"seed": _number(source, "seed", whole=True), "temperature": _number(source, "temperature"),
            "duration_delta": _number(source, "duration_delta")}


def identity_options(capabilities, speaker, language, controls, root=None):
    """The options a Phonon preset-voice identity keeps for good: the pinned model and voice catalog, the voice chosen, and the
    voices and language it was chosen from. `controls` is performance_controls() for the engine."""
    voices = discover_phonon_voices(root)
    if not voices:
        raise ValueError(NOT_INSTALLED)
    if not isinstance(speaker, str) or not speaker.strip():
        raise ValueError("Choose a Phonon voice for this identity.")
    chosen = resolve_voice(speaker, [item["name"] for item in voices])
    detail = next(item for item in voices if item["name"] == chosen)
    found = pins(root)
    return {"checkpoint": found["model"][0], "checkpoint_revision": found["model"][1],
            "voices_checkpoint": found["voices"][0], "voices_revision": found["voices"][1],
            "speaker": chosen, "language": normalize_language(language),
            "supported_speakers": [item["name"] for item in voices], "supported_languages": [LANGUAGE],
            "default_direction": "", "settings_schema": controls["controls"],
            "capability_snapshot": {"voice_direction": controls["direction"],
                                    "sampling_controls": capabilities.get("sampling_controls", {}),
                                    "acting_controls": capabilities.get("acting_controls", [])},
            "voice_details": {key: detail[key] for key in ("group", "gender", "age", "locale", "description")}}


def check_request(req, root=None):
    """Validate a speak request for Phonon and return what the worker needs: voice, language, seed, temperature, duration_delta."""
    unsupported = set(req) - REQUEST_FIELDS
    if unsupported:
        raise ValueError(f"Unsupported Phonon parameter(s): {', '.join(sorted(unsupported))}. "
                         f"It takes voice, seed, temperature, duration_delta and language.")
    return {"voice": resolve_voice(req.get("voice"), voice_names(root)), "language": normalize_language(req.get("language")),
            **normalize_settings(req)}
