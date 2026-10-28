"""Owned synthetic audio for path/contract checks; never ship private WAVs."""
import os
from pathlib import Path
import tempfile
import wave

import numpy as np


def speech_between_silences(rate=24_000, before=0.4, speech=1.2, after=0.4, amplitude=0.3):
    """Float samples: `before` seconds of silence, `speech` seconds of a 220 Hz tone and `after` seconds of silence. The tone
    stands in for speech wherever a test needs to know exactly where the speech is."""
    tone = amplitude * np.sin(2 * np.pi * 220 * np.arange(int(round(speech * rate))) / rate)
    return np.concatenate((np.zeros(int(round(before * rate))), tone, np.zeros(int(round(after * rate))))).astype(np.float32)


def owned_reference(testcase, root):
    directory = Path(root) / "reference"
    directory.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="contract-fixture-", suffix=".wav", dir=directory)
    os.close(fd)
    path = Path(name)
    testcase.addCleanup(lambda: path.unlink(missing_ok=True))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * 24000 * 6)
    return path
