"""Exact phoneme and word times from Kokoro's own predicted durations.

Kokoro's model predicts how many 25 ms frames (40 per second) each input symbol lasts. The model input is
[BOS, known symbols..., EOS]: characters with no vocabulary entry are dropped before it is built, so the durations
line up with the known characters in order. This module maps those durations back to the phoneme string and to
the pipeline's word tokens. It imports no model code, so it can be tested without loading Kokoro.
"""

FRAME_SECONDS = 0.025  # 600 samples per frame at 24 kHz


def phoneme_frames(phonemes, durations, vocab):
    """Frame span of every phoneme character the model received, as {character_index: (start_frame, end_frame)}.
    durations[0] is BOS (silence before the first symbol) and durations[-1] is EOS; the rest follow the known symbols."""
    known = [index for index, char in enumerate(phonemes) if char in vocab]
    if len(durations) != len(known) + 2:
        raise ValueError(f"expected {len(known) + 2} durations for {len(known)} known symbols; got {len(durations)}")
    spans, cursor = {}, int(durations[0])
    for order, index in enumerate(known):
        length = int(durations[1 + order])
        spans[index] = (cursor, cursor + length)
        cursor += length
    return spans


def token_spans(phonemes, tokens):
    """Where each token's phoneme symbols sit in the phoneme string, in order; None for a token that cannot be found
    there. A token marked as followed by whitespace also consumes the one space character after it."""
    spans, cursor = [], 0
    for token in tokens:
        symbols = getattr(token, "phonemes", None) or ""
        if symbols and phonemes.startswith(symbols, cursor):
            spans.append((cursor, cursor + len(symbols)))
            cursor += len(symbols)
        else:
            spans.append(None)
        if getattr(token, "whitespace", False) and phonemes[cursor:cursor + 1] == " ":
            cursor += 1
    return spans


def kokoro_timing(phonemes, tokens, durations, vocab, offset_seconds=0.0):
    """Word and phoneme times, in seconds from the start of the whole clip. offset_seconds places a chunk that does not
    start at zero. Words are the tokens with letters or digits; punctuation only adds its time to the phoneme list."""
    frames = phoneme_frames(phonemes, durations, vocab)
    words, unplaced = [], 0
    for token, span in zip(tokens, token_spans(phonemes, tokens)):
        text = getattr(token, "text", "") or ""
        if not any(char.isalnum() for char in text):
            continue
        known = [frames[index] for index in range(*span) if index in frames] if span else []
        if not known:
            unplaced += 1
            continue
        words.append({"word": text,
                      "start": round(offset_seconds + known[0][0] * FRAME_SECONDS, 3),
                      "end": round(offset_seconds + known[-1][1] * FRAME_SECONDS, 3)})
    phoneme_list = [{"symbol": phonemes[index], "start": round(offset_seconds + start * FRAME_SECONDS, 3),
                     "end": round(offset_seconds + end * FRAME_SECONDS, 3)}
                    for index, (start, end) in sorted(frames.items())]
    return {"frame_seconds": FRAME_SECONDS, "words": words, "phonemes": phoneme_list, "unplaced_words": unplaced}


def timing_for_chunks(chunks, vocab):
    """(timing, reason) for a whole clip. Each chunk is {"phonemes", "tokens", "durations", "offset"} for one pipeline
    chunk: the phoneme string, the pipeline's tokens, the model's durations, and where the chunk starts in the clip.
    The timing is refused, with the reason, when a chunk has no durations or does not match its phonemes. A chunk
    without tokens (Kokoro's non-English pipelines give none) keeps its exact phoneme times but no word times."""
    if not chunks:
        return None, "the clip has no audio"
    words, phonemes, unplaced, words_available = [], [], 0, True
    for chunk in chunks:
        if chunk["durations"] is None:
            return None, "the model gave no durations for a chunk of this clip"
        tokens = chunk["tokens"]
        if tokens is None:
            tokens, words_available = [], False
        try:
            part = kokoro_timing(chunk["phonemes"], tokens, chunk["durations"], vocab, chunk["offset"])
        except ValueError as exc:
            return None, str(exc)
        words.extend(part["words"])
        phonemes.extend(part["phonemes"])
        unplaced += part["unplaced_words"]
    return {"source": "kokoro-durations", "frame_seconds": FRAME_SECONDS, "words_available": words_available,
            "words": words, "phonemes": phonemes, "unplaced_words": unplaced}, None
