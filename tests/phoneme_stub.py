"""A stand-in for misaki's English rules, for tests of lip-sync plumbing.

misaki ships with Kokoro and is not part of the core install, so tests that build tracks from word times use this
instead. They check how a track is built from phonemes, not what misaki says; one test in test_lipsync.py runs misaki
itself when it is installed.
"""

KNOWN = {"hi": ["h", "I"], "there": ["ð", "ɛ", "ɹ"], "the": ["ð", "ə"], "road": ["ɹ", "O", "d"],
         "is": ["ɪ", "z"], "clear": ["k", "l", "ɪ", "ɹ"]}


def stub_phonemes(text):
    """[(word, [symbols])] like lipsync.english_word_phonemes: a few known words, one stop and one vowel otherwise."""
    return [(word, list(KNOWN.get(word.strip(".,").lower(), ["p", "ɑ"]))) for word in text.split()]
