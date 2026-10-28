"""Lip-sync tracks: a mouth shape (viseme) for every phoneme, with times, for a game to play back.

Two timing sources feed the same track format:

- "kokoro-durations": Kokoro's own predicted phoneme durations, stored with each take that Kokoro makes
  (kokoro_timing.py). They place every phoneme on the model's 25 ms frames. They are the model's prediction, not a
  human label.
- "word-distribution": an aligned English word span (word_timing.py) shared out over the word's phonemes in proportion
  to rough phoneme lengths. The phoneme times are estimates.

Aligned English takes get their phonemes from misaki's grapheme-to-phoneme rules (Apache-2.0, installed with Kokoro),
which use the same symbols as Kokoro's input. misaki is optional: without it an aligned take gets no track and the
reason says so, while Kokoro takes are unaffected. A take with neither Kokoro's times nor English word times gets no track.
"""

from kokoro_timing import FRAME_SECONDS

FORMAT = "voice-lab-lipsync-v1"
VISEMES = ("rest", "closed", "teeth", "tongue_th", "tongue", "sibilant", "velar",
           "open_wide", "open_mid", "smile", "round")
# Symbols that carry no sound of their own: spacing, punctuation, stress and length marks, and pitch arrows.
MARKERS = frozenset(" .,;:!?—…\"()“”ˈˌːʰʲ↓→↗↘̃")
SYMBOL_VISEME = {
    "p": "closed", "b": "closed", "m": "closed", "β": "closed",
    "f": "teeth", "v": "teeth",
    "θ": "tongue_th", "ð": "tongue_th",
    "t": "tongue", "d": "tongue", "n": "tongue", "l": "tongue", "ɾ": "tongue", "ɹ": "tongue", "r": "tongue",
    "ɖ": "tongue", "ɳ": "tongue", "ɻ": "tongue", "ʈ": "tongue", "ɲ": "tongue",
    "s": "sibilant", "z": "sibilant", "ʃ": "sibilant", "ʒ": "sibilant", "ʧ": "sibilant", "ʤ": "sibilant",
    "ʦ": "sibilant", "ʣ": "sibilant", "ʥ": "sibilant", "ʨ": "sibilant", "ɕ": "sibilant", "ʂ": "sibilant",
    "k": "velar", "g": "velar", "ɡ": "velar", "ŋ": "velar", "h": "velar", "ɟ": "velar", "ç": "velar",
    "x": "velar", "χ": "velar", "ɣ": "velar", "ʁ": "velar", "ʔ": "velar", "q": "velar", "ɴ": "velar",
    "w": "round", "ɥ": "round", "ɰ": "round", "ʋ": "round",
    "j": "smile", "ʎ": "smile", "ʝ": "smile", "ɪ": "smile", "i": "smile", "ɨ": "smile",
    "ɑ": "open_wide", "a": "open_wide", "æ": "open_wide", "ʌ": "open_wide", "ɒ": "open_wide", "ɐ": "open_wide",
    "ɛ": "open_mid", "e": "open_mid", "ɜ": "open_mid", "ɚ": "open_mid", "ə": "open_mid", "ᵊ": "open_mid",
    "ᵻ": "open_mid",
    "ɔ": "round", "o": "round", "ʊ": "round", "u": "round", "ø": "round", "œ": "round", "ɤ": "round", "ɯ": "round",
    # misaki's single-letter diphthongs: A eɪ, I aɪ, O oʊ, Q əʊ, W aʊ, Y ɔɪ.
    "A": "open_mid", "I": "open_wide", "O": "round", "Q": "round", "W": "open_wide", "Y": "round",
    # Uncommon in English output. misaki writes the flap ɾ as T, so T is a tongue-tip sound.
    "T": "tongue", "S": "sibilant", "c": "velar", "y": "smile", "ɸ": "closed", "ɽ": "tongue", "ᵝ": "closed",
    "ꭧ": "open_mid",
}
VOWELS = frozenset("ɑæʌɒɐɛeɜɚəᵊᵻɪiɨɔoʊuøœɤɯaAIOQWY")
AFFRICATES = frozenset("ʧʤ")
SONORANTS = frozenset("lmnŋɹrwjɾ")
FRICATIVES = frozenset("fvθðszʃʒh")
STOPS = frozenset("pbtdkgɡ")
_G2P = None


def viseme_for(symbol):
    """The mouth shape for one phoneme symbol, or None for a symbol that makes no sound of its own. A symbol missing
    from the table gets the neutral shape rather than failing the track; tests keep the table complete."""
    if symbol in MARKERS:
        return None
    return SYMBOL_VISEME.get(symbol, "open_mid")


def weight(symbol):
    """Rough relative length of a phoneme, used to share a word's time among its phonemes."""
    if symbol in MARKERS:
        return 0.0
    if symbol in VOWELS:
        return 1.6
    if symbol in AFFRICATES:
        return 1.2
    if symbol in SONORANTS:
        return 1.0
    if symbol in FRICATIVES:
        return 0.9
    if symbol in STOPS:
        return 0.7
    return 1.0


def english_word_phonemes(text):
    """[(word, [phoneme symbols])] for the text's words, using misaki's English rules. Only tokens with letters or
    digits count, which matches the words the aligner returns."""
    global _G2P
    from misaki import en
    if _G2P is None:
        _G2P = en.G2P(trf=False, british=False)
    _phonemes, tokens = _G2P(text)
    return [(token.text, list(token.phonemes or "")) for token in tokens if any(c.isalnum() for c in token.text)]


def distribute(start, end, symbols):
    """Share one word's span over its phoneme symbols in proportion to their weights. Markers take no time."""
    total = sum(weight(symbol) for symbol in symbols)
    if total <= 0 or end <= start:
        return []
    spans, cursor = [], start
    for symbol in symbols:
        share = weight(symbol)
        if share <= 0:
            continue
        length = (end - start) * share / total
        spans.append((symbol, cursor, cursor + length))
        cursor += length
    return spans


def distribute_words(word_spans, word_symbols):
    """Phoneme spans for every word. word_spans are (start, end) per word, aligned one to one with word_symbols."""
    if len(word_spans) != len(word_symbols):
        raise ValueError(f"{len(word_spans)} aligned words but {len(word_symbols)} phonetic words; the times cannot be matched")
    spans = []
    for (start, end), (_word, symbols) in zip(word_spans, word_symbols):
        spans.extend(distribute(start, end, symbols))
    return spans


def _add_key(keys, viseme, start, end):
    if end - start <= 1e-9:
        return
    if keys and keys[-1]["viseme"] == viseme and abs(keys[-1]["end"] - start) < 1e-6:
        keys[-1]["end"] = round(end, 3)
    else:
        keys.append({"start": round(start, 3), "end": round(end, 3), "viseme": viseme})


def build_track(phoneme_spans, duration, *, timing_source, frame_seconds=None, word_source=None):
    """A lip-sync track: contiguous keys from 0 to the clip's duration, with rest between sounds. phoneme_spans are
    (symbol, start, end) in seconds. Overlaps are cut so the keys never overlap, and any part of a span outside the
    clip (before 0 or after its end, as a trim leaves some) is cut away."""
    duration = max(float(duration), 0.0)
    keys, cursor, unknown = [], 0.0, 0
    for symbol, start, end in sorted(phoneme_spans, key=lambda item: item[1]):
        viseme = viseme_for(symbol)
        if viseme is None:
            continue
        if symbol not in SYMBOL_VISEME:
            unknown += 1
        start = min(max(start, cursor), duration)
        end = min(max(end, start), duration)
        _add_key(keys, "rest", cursor, start)
        _add_key(keys, viseme, start, end)
        cursor = end
    _add_key(keys, "rest", cursor, duration)
    if not keys:
        keys = [{"start": 0.0, "end": round(duration, 3), "viseme": "rest"}]
    return {
        "schema": 1, "format": FORMAT, "visemes": list(VISEMES), "duration_seconds": round(duration, 3),
        "timing": {"source": timing_source, "frame_seconds": frame_seconds, "word_source": word_source},
        "keys": keys, "checks": {"unmapped_symbols": unknown},
    }


def check_track(track):
    """Raise ValueError unless the track is well formed: the known format and mouth shapes, and keys that run in order,
    touch one another, and cover 0 to the duration."""
    if track.get("format") != FORMAT:
        raise ValueError(f"format is {track.get('format')!r}, not {FORMAT!r}")
    keys, duration = track.get("keys") or [], track.get("duration_seconds")
    if not keys or keys[0]["start"] != 0 or abs(keys[-1]["end"] - duration) > 1e-6:
        raise ValueError("the keys do not cover 0 to the duration")
    for before, after in zip(keys, keys[1:]):
        if abs(before["end"] - after["start"]) > 1e-6:
            raise ValueError(f"keys do not touch at {before['end']} seconds")
    unknown = sorted({key["viseme"] for key in keys} - set(VISEMES))
    if unknown:
        raise ValueError(f"unknown mouth shapes {unknown}")


KOKORO = "kokoro-durations"
DISTRIBUTED = "word-distribution"
WORD_SOURCE = {KOKORO: "kokoro-durations", DISTRIBUTED: "whisper-cross-attention"}


def take_phoneme_spans(take):
    """(source, spans) for one take, with spans as (symbol, start, end) in seconds from the take's own start. A take
    that Kokoro made carries Kokoro's phoneme times, used as they are, whatever its language. Any other take shares its
    aligned English words out over misaki's phonemes. Raises ValueError with the reason when neither is available."""
    timing = take.get("phoneme_timings")
    if timing:
        return KOKORO, [(item["symbol"], item["start"], item["end"]) for item in timing["phonemes"]]
    record = take.get("word_timings")
    if record is None:
        raise ValueError("a take of this line has no word times yet; run align_takes.py")
    if record.get("language") != "en":
        raise ValueError("no phonemes for this language yet")
    if record.get("error"):
        raise ValueError(f"a take of this line could not be aligned: {record['error']}")
    words = [(word["start"], word["end"]) for word in record["words"]]
    try:
        phonemes = english_word_phonemes(record["text"])
    except ImportError as exc:  # misaki comes with Kokoro; an environment without it still exports, minus this track
        raise ValueError("the English phoneme rules (misaki) are not installed in this environment") from exc
    return DISTRIBUTED, distribute_words(words, phonemes)


def track_for_takes(takes, *, trim_start=0.0, trim_end=0.0):
    """(track, reason) for one scene line, on the delivered line's timeline. Each take's phoneme times move by the takes
    before it, then earlier by the start trim. No track is made, and the reason says why, when a take has neither
    Kokoro's times nor usable English word times."""
    if not takes:
        return None, "the line has no takes"
    durations = [take["probe"]["duration_seconds"] for take in takes]
    duration = sum(durations) - trim_start - trim_end
    if duration <= 0:
        return None, "the trim removes the whole line"
    spans, sources, offset = [], [], 0.0
    for take, seconds in zip(takes, durations):
        try:
            source, take_spans = take_phoneme_spans(take)
        except ValueError as exc:
            return None, str(exc)
        shift = offset - trim_start
        spans.extend((symbol, start + shift, end + shift) for symbol, start, end in take_spans)
        sources.append(source)
        offset += seconds
    source = sources[0] if len(set(sources)) == 1 else "mixed"
    return build_track(spans, duration, timing_source=source,
                       frame_seconds=FRAME_SECONDS if KOKORO in sources else None,
                       word_source=WORD_SOURCE.get(source, "mixed")), None
