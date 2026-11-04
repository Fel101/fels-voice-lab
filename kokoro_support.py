"""Small, offline-safe helpers for the installed Kokoro pipeline."""

from pathlib import Path
import re
from importlib.machinery import PathFinder
from importlib.util import find_spec

from common import LAB


KOKORO_REPO = "hexgrad/Kokoro-82M"
KOKORO_REPO_CACHE = LAB / "hf-cache" / "hub" / "models--hexgrad--Kokoro-82M"
# Kokoro's packages live in its own environment (tools/install_kokoro.py makes it), not in the service's .venv.
KOKORO_SITE_PACKAGES = LAB / ".venv-kokoro" / "Lib" / "site-packages"
KOKORO_LANGUAGES = {
    "a": {"name": "American English", "aliases": ("en-us", "english (us)", "american english")},
    "b": {"name": "British English", "aliases": ("en-gb", "english (uk)", "british english")},
    "e": {"name": "Spanish", "aliases": ("es", "spanish")},
    "f": {"name": "French", "aliases": ("fr", "fr-fr", "french")},
    "h": {"name": "Hindi", "aliases": ("hi", "hindi")},
    "i": {"name": "Italian", "aliases": ("it", "italian")},
    "j": {"name": "Japanese", "aliases": ("ja", "japanese")},
    "p": {"name": "Brazilian Portuguese", "aliases": ("pt-br", "portuguese", "brazilian portuguese")},
    "z": {"name": "Mandarin Chinese", "aliases": ("zh", "mandarin", "mandarin chinese", "chinese")},
}
_LANGUAGE_LOOKUP = {alias.casefold(): code for code, data in KOKORO_LANGUAGES.items()
                    for alias in (code, data["name"], *data["aliases"])}
_LANGUAGE_LOOKUP.update({"english": "a", "en": "a"})
_VOICE_RE = re.compile(r"[a-z][fm]_[a-z][a-z0-9_]*\Z")
_OPTIONAL_FRONTEND_MODULES = {"j": "pyopenjtalk", "z": "ordered_set"}


def kokoro_snapshot_dir() -> Path:
    """Return the locally cached Kokoro snapshot without consulting the Hub."""
    snapshots = KOKORO_REPO_CACHE / "snapshots"
    ref = KOKORO_REPO_CACHE / "refs" / "main"
    if ref.is_file():
        revision = ref.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-f0-9]{40}", revision):
            candidate = snapshots / revision
            if candidate.is_dir():
                return candidate
    candidates = sorted((path for path in snapshots.iterdir() if path.is_dir()),
                        key=lambda path: path.stat().st_mtime, reverse=True) if snapshots.is_dir() else []
    return candidates[0] if candidates else snapshots / "missing-local-snapshot"


def discover_kokoro_voices() -> list[str]:
    """Discover voice packs present in the selected local snapshot, not all upstream voices."""
    voices_dir = kokoro_snapshot_dir() / "voices"
    if not voices_dir.is_dir():
        return []
    return sorted(path.stem for path in voices_dir.glob("*.pt") if _VOICE_RE.fullmatch(path.stem))


def normalize_kokoro_voices(value, available=None) -> list[str]:
    """Validate a single cached speaker or a comma-separated equal-weight blend."""
    if isinstance(value, (list, tuple)):
        pieces = list(value)
    elif isinstance(value, str):
        pieces = value.split(",")
    else:
        raise ValueError("Kokoro voice must be a fixed voice ID or a comma-separated equal blend.")
    voices = [str(piece).strip() for piece in pieces]
    if not voices or len(voices) > 8 or any(not _VOICE_RE.fullmatch(voice) for voice in voices):
        raise ValueError("Choose 1–8 valid Kokoro voice IDs; separate blend voices with commas.")
    if len(set(voices)) != len(voices):
        raise ValueError("A Kokoro blend cannot include the same voice more than once.")
    if available is None:
        available = discover_kokoro_voices()
    missing = [voice for voice in voices if voice not in available]
    if missing:
        raise ValueError("Kokoro voice pack(s) are not cached locally: " + ", ".join(missing) +
                         ". Add them with: setup-kokoro.bat --voices " + " ".join(missing))
    return sorted(voices)


def _module_available(name):
    """Whether Kokoro can import a module: in this Python (inside Kokoro's worker), or in Kokoro's own environment (from the
    service, whose .venv does not hold Kokoro). Nothing is imported."""
    return find_spec(name) is not None or PathFinder.find_spec(name, [str(KOKORO_SITE_PACKAGES)]) is not None


def resolve_kokoro_language(language, voices) -> str:
    """Map a language label/code to the installed Kokoro G2P pipeline.

    Legacy requests say just `English`; infer British English when all selected voices
    are British. Explicit non-English language choices are never inferred from voice ID.
    """
    raw = "English" if language is None or str(language).strip() == "" else str(language).strip()
    key = raw.casefold()
    if key not in _LANGUAGE_LOOKUP:
        choices = ", ".join(f"{code} ({info['name']})" for code, info in KOKORO_LANGUAGES.items())
        raise ValueError(f"Unsupported Kokoro language {raw!r}. Choose one of: {choices}.")
    code = _LANGUAGE_LOOKUP[key]
    if key in {"english", "en"} and voices and all(voice.startswith("b") for voice in voices):
        return "b"
    missing = _OPTIONAL_FRONTEND_MODULES.get(code)
    if missing and not _module_available(missing):
        raise ValueError(f"Kokoro {KOKORO_LANGUAGES[code]['name']} frontend is unavailable: "
                         f"Python module {missing!r} is missing. Install the matching Misaki language extra "
                         "in an isolated environment, then try again.")
    return code


def kokoro_languages() -> list[dict]:
    result = []
    for code, item in KOKORO_LANGUAGES.items():
        missing = _OPTIONAL_FRONTEND_MODULES.get(code)
        available = not missing or _module_available(missing)
        result.append({"code": code, "name": item["name"], "aliases": list(item["aliases"]),
                       "available": available,
                       "missing_module": None if available else missing})
    return result


def phonemize_kokoro(text: str, language: str = "English") -> dict:
    """Use Kokoro's installed language frontend without loading a speech model or voice pack."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Text must contain at least one non-whitespace character.")
    if len(text) > 1200:
        raise ValueError("Keep it under 1200 characters.")
    from kokoro import KPipeline

    code = resolve_kokoro_language(language, [])
    # Quiet pipelines produce exactly the grapheme/phoneme segments used by inference.
    pipeline = KPipeline(lang_code=code, repo_id=KOKORO_REPO, model=False, device="cpu")
    segments = [{"graphemes": graphemes, "phonemes": phonemes}
                for graphemes, phonemes, _audio in pipeline(text.strip(), voice=None, split_pattern=r"\n+")]
    if not segments:
        raise ValueError("Kokoro produced no phoneme segments for this text.")
    return {"language": code, "language_name": KOKORO_LANGUAGES[code]["name"], "segments": segments}
