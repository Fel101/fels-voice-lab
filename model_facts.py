"""What each model's card on the Models page says, in plain words, and what its three questions filter on (roadmap issue 3).

Every fact comes from the project's records, never from text in the page: the catalog (catalog/engines.json) for what a
model can do, its adapter (engine_adapters/) for where it runs, its voices, directions, languages and what its audio may
be used for, its installer for the graphics memory it needs (the minimum the installer itself refuses below), and the
language screen's evidence (docs/evidence/language-matrix.json) for how each language came out. Where a record says
nothing, the card says "not stated". GET /api/catalog gives each engine its `facts`; tests/test_model_facts.py checks
them against MODEL_CAPABILITIES.md."""

import importlib
import json
import math

import engine_adapters
import language_matrix

WORKING = {"supported", "worker_verified"}
EXPERIMENTAL = "experimental"
SCREEN_WORDS = {
    "understood": "Understood",
    "partial": "Partly understood",
    "not_understood": "Not understood",
    "inconclusive": "Inconclusive",
}
MARKUP_WORDS = {"square": "square brackets", "round": "round brackets"}


def _status(capabilities, key):
    status = (capabilities.get(key) or {}).get("status")
    return "yes" if status in WORKING else "experimental" if status == EXPERIMENTAL else None


def _join(items):
    items = list(items)
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def gpu_memory_gb(adapter):
    """The graphics memory, in whole GB, below which the model's installer refuses a card; None when it has no minimum."""
    if not adapter or not adapter.gpu_build:
        return None
    try:
        minimum = getattr(importlib.import_module(adapter.gpu_build), "GPU_MEMORY_GB", None)
    except Exception:  # noqa: BLE001 - a card's words must not depend on an installer that cannot load here
        return None
    # An 8 GB card reports a little under 8 GiB, so the installers ask for 7.5 to let one through: say 8.
    return math.ceil(minimum) if minimum else None


def runs(adapter):
    if adapter and adapter.cpu_only:
        return {"cpu": True, "gpu": False, "gpu_memory_gb": None,
                "text": "Runs without a graphics card: the Lab runs it on the processor (CPU)."}  # fmt: skip
    if adapter and adapter.gpu_only:
        memory = gpu_memory_gb(adapter)
        text = (
            f"Needs a graphics card with at least {memory} GB of memory."
            if memory
            else "Needs a graphics card. How much memory it needs is not stated."
        )
        return {"cpu": False, "gpu": True, "gpu_memory_gb": memory, "text": text}
    gpu = bool(adapter and adapter.uses_torch and adapter.gpu_build)
    text = "Runs with or without a graphics card." if gpu else "Runs without a graphics card."
    return {"cpu": True, "gpu": gpu, "gpu_memory_gb": None, "text": text}


def voices(entry, adapter):
    capabilities = entry.get("capabilities") or {}
    converts = bool(adapter and adapter.converts)
    clones = bool(adapter and adapter.reference_voices)
    presets = bool(adapter and (adapter.preset_voices or adapter.fixed_voices))
    design = _status(capabilities, "voice_design")
    parts = []
    if converts:
        parts.append("Changes the voice in a recording you give it into another voice (Voice conversion)")
    if clones:
        parts.append("Clones a voice from a recording")
    if presets:
        parts.append("Has preset voices")
    if design:
        parts.append("Makes a voice from a description" + (" (experimental)" if design == "experimental" else ""))
    text = _join([parts[0]] + [part[0].lower() + part[1:] for part in parts[1:]]) + "." if parts else "No voices of its own."
    if converts:
        text += " It does not speak text."
    elif not clones:
        text += " It does not clone a voice from a recording."
    return {"clones": clones, "presets": presets, "designs": bool(design), "converts": converts, "text": text}


def direction(entry, adapter):
    takes = bool(adapter and adapter.takes_direction)
    experimental = _status(entry.get("capabilities") or {}, "voice_direction") == "experimental"
    text = "Takes acting directions" + (" (experimental)." if experimental else ".") if takes else "Takes no acting directions."
    markup = MARKUP_WORDS.get(adapter.acted_markup) if adapter else None
    if markup:
        text += f" It acts some tags written in {markup} in its text instead of saying them."
    return {"takes": takes, "acts_tags": bool(markup), "text": text}


def _evidence():
    try:
        report = json.loads(language_matrix.EVIDENCE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    names = {column["code"]: column["label"] for column in report.get("columns", [])}
    return {row["engine"]: row.get("cells", {}) for row in report.get("engines", [])}, names


def _language(label, names):
    """The language a picker label speaks, for the page's language question: American and British English are English."""
    code = language_matrix.COLUMN_OF_LABEL.get(label)
    return names.get(code.split("-")[0], label) if code else label


def languages(entry, adapter, cells, names):
    engine_id = entry["id"]
    if adapter and adapter.converts:
        words = "Keeps the language of the recording it is given."
        return {"offered": [], "filter": [], "summary": words, "text": ""}
    offered = sorted(adapter.languages if adapter and adapter.languages else {"English"})
    # "Auto" is no language, and a bare "English" beside "American English" is the same picker entry under another name.
    offered = [
        label
        for label in offered
        if label != "Auto"
        and not any(
            other != label and other.endswith(" " + label) and language_matrix.COLUMN_OF_LABEL.get(other) for other in offered
        )
    ]
    metadata = engine_id in language_matrix.METADATA_LANGUAGE_ENGINES
    rows, by_status, unscreened = [], {}, []
    for label in offered:
        code = language_matrix.COLUMN_OF_LABEL.get(label)
        status = (cells.get(code) or {}).get("status") if code else None
        screen = status if status in SCREEN_WORDS else "not_screened"
        rows.append({"label": label, "language": _language(label, names), "screen": screen})
        if screen == "not_screened":
            unscreened.append(label)
        else:
            by_status.setdefault(screen, []).append(label)
    sentences = []
    if metadata:
        sentences.append("A line's language is only a note: the model speaks the language its text is written in.")
    for status, words in SCREEN_WORDS.items():
        if by_status.get(status):
            where = (
                ""
                if any(by_status.get(earlier) for earlier in list(SCREEN_WORDS)[: list(SCREEN_WORDS).index(status)])
                else " in the Lab's language screen"
            )
            sentences.append(f"{words}{where}: {_join(by_status[status])}.")
    if unscreened:
        if metadata or len(unscreened) > 12:
            sentences.append(f"Never screened: {len(unscreened)} more languages it takes.")
        else:
            sentences.append(
                f"Never screened: {_join(unscreened)}."
                if by_status
                else f"{_join(unscreened)}, not yet checked by the Lab's language screen."
            )
    extra = sorted(
        names.get(code, code)
        for code, cell in cells.items()
        if cell.get("status") == "understood" and not any(row["language"] == names.get(code.split("-")[0]) for row in rows)
    )
    if extra and not metadata:
        sentences.append(f"The screen also understood {_join(extra)}, which its language list does not offer yet.")
    if adapter and adapter.upstream_languages:
        sentences.append(f"Its makers list {len(adapter.upstream_languages)} languages.")
    # The card's line and the language question name what a model takes, except what the screen did not understand. A model
    # that takes any label as a note needs the screen to have heard the language: understood, or partly.
    kept = {"understood", "partial"} if metadata else {"understood", "partial", "not_screened"}
    main = sorted({row["language"] for row in rows if row["screen"] in kept - {"partial"}})
    partly = sorted({row["language"] for row in rows if row["screen"] == "partial"} - set(main))
    summary = (
        "Languages: " + (_join(main) or "none the screen understood") + (f"; partly {_join(partly)}" if partly else "") + "."
    )
    return {"offered": rows, "filter": sorted(set(main) | set(partly)), "summary": summary, "text": " ".join(sentences)}


def output_use(adapter):
    profile = (adapter.output_license if adapter else None) or {}
    words = adapter.output_use_words if adapter else None
    if words:
        return {"status": profile.get("status", "unknown"), "text": words}
    if profile.get("status") == "restricted":
        return {
            "status": "restricted",
            "text": "What its audio may be used for: restricted by its licence (THIRD_PARTY_NOTICES.md).",
        }
    return {"status": "not_stated", "text": "What its audio may be used for: not stated."}


def facts(entry, evidence=None):
    """A catalog entry's card facts: first_model, runs, voices, direction, languages and output, each with its `text`."""
    adapter = engine_adapters.get(entry["id"])
    cells, names = evidence if evidence is not None else _evidence()
    return {
        "first_model": bool(adapter and adapter.first_model),
        "runs": runs(adapter),
        "voices": voices(entry, adapter),
        "direction": direction(entry, adapter),
        "languages": languages(entry, adapter, cells.get(entry["id"], {}), names),
        "output": output_use(adapter),
    }


def catalog_with_facts(catalog):
    """The catalog as GET /api/catalog answers it: each engine with its facts."""
    evidence = _evidence()
    return {**catalog, "engines": [{**entry, "facts": facts(entry, evidence)} for entry in catalog.get("engines", [])]}
