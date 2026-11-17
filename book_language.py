"""Project pronunciation rules and their authored manuscript text."""

import re


def pronunciations(value):
    if not isinstance(value, dict) or len(value) > 100:
        raise ValueError("Enter up to 100 project pronunciations.")
    seen, result = set(), []
    for word, replacement in value.items():
        if (
            not isinstance(word, str)
            or not isinstance(replacement, str)
            or not word.strip()
            or not replacement.strip()
            or len(word) > 120
            or len(replacement) > 200
        ):
            raise ValueError("A project pronunciation needs a source and replacement phrase.")
        folded = word.strip().casefold()
        if folded in seen:
            raise ValueError("Enter each pronunciation source only once.")
        seen.add(folded)
        result.append({"source": word.strip(), "replacement": replacement.strip()})
    return sorted(result, key=lambda row: (-len(row["source"]), row["source"].casefold()))


def original_line(line):
    return (
        line["manuscript_text"] if line.get("manuscript_spoken") == line["text"] and "manuscript_text" in line else line["text"]
    )


def spoken_line(text, rules):
    if not rules:
        return text
    words = {row["source"].casefold(): row["replacement"] for row in rules}
    phrase = "|".join(re.escape(row["source"]) for row in rules)
    return re.sub(r"(?<!\w)(?:" + phrase + r")(?!\w)", lambda match: words.get(match[0].casefold(), match[0]), text, flags=re.I)
