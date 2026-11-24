"""The reference sentence and the vocal-tag helper. No heavy imports, so any Python can read them."""

import re

# Tags Chatterbox Turbo understands. Engines that cannot perform them get the text without.
TAG_RE = re.compile(r"\[(?:laugh|chuckle|cough)\]\s*", re.I)

# About 14 seconds of neutral speech; Chatterbox wants a reference clip of roughly 10 s.
REFERENCE_TEXT = ("Hi, it's so good to see you. I've been thinking about our conversation from yesterday, and "
                  "honestly, I couldn't stop smiling. Tell me everything about your day, and don't leave out "
                  "the small details, because those are my favorite part.")


def strip_tags(text):
    return TAG_RE.sub("", text).strip()
