"""The voices a program can speak with by name (GET /api/voices): the Cast's voices, and the preset voices of the models
that are installed. A voice's `name` goes back as `voice` in POST /api/speak, or POST /api/performance for a Cast voice.

A Cast voice is named after its character when it is the character's only voice, or its only approved one; any voice is
also "Character (Label)". A preset voice is named as its model names it (af_heart, Harper), or "model:voice" when that name is
taken. Each model's adapter says which voices it has (Adapter.named_voices). Nothing here loads a model: installed files are
read, and a model that reports its voices only once it is loaded (Qwen CustomVoice) lists them while it is loaded."""

from voice_lab import engine_adapters
from voice_lab import engine_checks
from voice_lab.output_license import made_from, model_licence

# The models whose clips take an acting direction ("quietly, as if tired"); cast_store.DIRECTION_ENGINES is the same set.
DIRECTION_MODELS = engine_adapters.engines_where("takes_direction")


def looks_like_kokoro(value):
    """Whether a `voice` is written as Kokoro voice ids (af_heart, or a blend af_heart,am_adam): such a request without an
    engine is Kokoro's, as it always was, even when no voice in the list has that name."""
    pieces = [piece.strip() for piece in value.split(",")]
    return all(len(piece) > 3 and piece[1] in "fm" and piece[2] == "_" and piece[0].isalpha() for piece in pieces)


def identity_languages(identity, adapter):
    """The languages a Cast voice speaks: those its preset voice was chosen with, those its adapter reads from its options
    (a Kokoro voice's own), else its model's."""
    options = identity.get("engine_options") or {}
    if identity["kind"] == "preset_voice":
        return list(options.get("supported_languages") or [options.get("language") or "English"])
    own = adapter.voice_languages(options) if adapter is not None else None
    if own:
        return list(own)
    return sorted(adapter.languages) if adapter is not None and adapter.languages else ["English"]


def not_installed(entry):
    """The sentence of a voice whose model is not installed: what to press to install it."""
    return f"{entry.get('name', entry['id'])} is not installed. Open Models & features and press Install on its card."


def cast_voices(characters, entries, installed):
    """The Cast's usable voices: not retired, not rejected, on a model this service runs. `installed` says whether a model's
    files are in place."""
    voices = []
    for character in characters:
        if character.get("retired"):
            continue
        usable = [
            identity
            for identity in character["identities"]
            if not identity.get("retired") and identity["status"] != "rejected" and identity["engine_id"] in entries
        ]
        approved = [identity for identity in usable if identity["status"] == "approved"]
        default = usable[0] if len(usable) == 1 else approved[0] if len(approved) == 1 else None
        for identity in usable:
            entry = entries[identity["engine_id"]]
            why = None
            missing = not installed(identity["engine_id"])
            if missing:
                why = not_installed(entry)
            elif identity["kind"] == "reference":
                recordings = {item["id"]: item for item in identity.get("references", [])}
                approved_recording = recordings.get(identity.get("approved_reference_id"))
                if approved_recording is None or approved_recording.get("retired"):
                    why = "It has no approved voice recording yet. Approve one in Cast."
            voices.append(
                {
                    "name": f"{character['name']} ({identity['label']})",
                    "kind": "cast",
                    "character": character["name"],
                    "label": identity["label"],
                    "identity_id": identity["id"],
                    "status": identity["status"],
                    "model": identity["engine_id"],
                    "model_name": entry.get("name", identity["engine_id"]),
                    "languages": identity_languages(identity, engine_adapters.get(identity["engine_id"])),
                    "takes_direction": identity["engine_id"] in DIRECTION_MODELS,
                    "licence": identity["licence"],
                    "made_from": identity.get("made_from") or made_from(identity["kind"]),
                    "ready": why is None,
                    "why_not": why,
                    "_short": character["name"] if identity is default else None,
                    "_missing_model": missing,
                }
            )
    return voices


def preset_voices(entries, installed, loaded_info=None):
    """The models' own voices, as each model's adapter names them (Adapter.named_voices): Kokoro's and Phonon's installed
    voices, and the speakers of a loaded Qwen CustomVoice."""
    voices = []
    for model, entry in entries.items():
        adapter = engine_adapters.get(model)
        if adapter is None:
            continue
        loaded = loaded_info(model) if loaded_info is not None else None
        ready = installed(model)
        for voice in adapter.named_voices(loaded):
            voices.append(
                {
                    "name": voice["voice_id"],
                    "kind": "preset",
                    "label": voice["label"],
                    "voice_id": voice["voice_id"],
                    "model": model,
                    "model_name": entry.get("name", model),
                    "languages": list(voice["languages"]),
                    "takes_direction": bool(adapter.takes_direction),
                    "licence": model_licence(model, preset=True),
                    "made_from": "model",
                    "ready": ready,
                    "why_not": None if ready else not_installed(entry),
                    "_request": {"engine": model, **voice["request"]},
                    "_missing_model": not ready,
                }
            )
    return voices


def model_installed(engines, model, probe=None):
    """Whether a model this service runs has its files installed, as its card in Models & features says."""
    probe = probe or getattr(engines, "install_probe", None) or engine_checks.installation
    try:
        return bool(probe(engines.api_entries[model])["installed"])
    except Exception:
        return False


def list_voices(cast, engines, probe=None):
    """Every voice a program can name, Cast voices first, each with the one name to send back."""
    entries = engines.api_entries
    known = {}

    def installed(model):
        if model not in known:
            known[model] = model_installed(engines, model, probe)
        return known[model]

    cast_list = cast_voices(cast.list_cast(), entries, installed)
    presets = preset_voices(entries, installed, getattr(engines, "loaded_model_info", None))

    def counted(names):
        counts = {}
        for name in names:
            counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
        return counts

    # A character's name goes to its default voice only when no other Cast voice could be meant by it.
    cast_counts = counted(name for voice in cast_list for name in {voice["name"], voice["_short"]} - {None})
    taken = set()
    for voice in cast_list:
        short = voice.pop("_short")
        voice["aliases"] = [voice["name"]]
        if short is not None and cast_counts[short.casefold()] == 1:
            voice["name"] = short
        taken.update(name.casefold() for name in (voice["name"], *voice["aliases"]))
    # A preset voice keeps its model's name for it unless a Cast voice or another model's voice has it.
    preset_counts = counted(voice["name"] for voice in presets)
    for voice in presets:
        qualified = f"{voice['model']}:{voice['voice_id']}"
        if voice["name"].casefold() in taken or preset_counts[voice["name"].casefold()] > 1:
            voice["name"], voice["aliases"] = qualified, []
        else:
            voice["aliases"] = [qualified]
    return cast_list + presets


def public(voices):
    """The list as GET /api/voices answers it."""
    return [{key: value for key, value in voice.items() if not key.startswith("_")} for voice in voices]


def find(name, voices):
    """The one voice a name means, matched without regard to case. KeyError when none has it, ValueError when several do."""
    wanted = name.strip().casefold()
    found = [voice for voice in voices if wanted in {item.casefold() for item in (voice["name"], *voice["aliases"])}]
    if not found:
        raise KeyError(f"There is no voice called {name.strip()!r}. GET /api/voices lists the voices that can speak.")
    if len(found) > 1:
        names = ", ".join(voice["name"] for voice in found)
        raise ValueError(f"More than one voice is called {name.strip()!r}: {names}. Send one of those names instead.")
    return found[0]


def speak_request(voice, req):
    """The request /api/speak sends to the model for a preset voice named in `voice`: the model, the voice in the model's own
    field, and the voice's language unless the request names one."""
    asked = {key: value for key, value in req.items() if key != "voice"}
    fields = dict(voice["_request"])
    if asked.get("language"):
        fields.pop("language", None)
    return {**asked, **fields}
