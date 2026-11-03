"""How much free RAM, in GB, the Lab wants before it loads each engine: the one table every check reads.

serve.py checks it before it starts a worker and shows it on each card of the Models page, and the worker checks it again
before it loads the model. Each number is the Lab's own rule, a measured peak with room to spare where one was measured, not
a promise about what a model uses.

runtime.local.json can change a number for one computer, such as {"min_free_ram_gb": {"kokoro": 1}}. A need of 0 turns the
check off for that engine.
"""

from common import runtime_setting
from phonon_support import PHONON_ENGINE_ID

DEFAULT_MIN_FREE_RAM_GB = {
    # Measured on the CPU, one worker speaking a long line. Kokoro held 1.2 GB once loaded and peaked at 1.8 GB (3.2 GB
    # committed) on 1,019 characters; Phonon peaked at about 0.5 GB on 1,200; Chatterbox Turbo peaked at 6.1 GB while it
    # loaded and held 5.0 GB after 1,200, so 8 GB leaves about 2 GB to spare.
    "kokoro": 3, PHONON_ENGINE_ID: 2,
    "turbo": 8, "orig": 10, "chatterbox-vc": 10, "breeze-tts-2": 12, "voxcpm2": 10, "drama-3": 26,
    "qwen3-tts-1.7b-voice-design": 10, "qwen3-tts-1.7b-base": 10, "qwen3-tts-0.6b-base": 10, "qwen3-tts-1.7b-custom-voice": 10,
}


def with_overrides(defaults, overrides):
    """The defaults with runtime.local.json's "min_free_ram_gb" laid over them. An engine the Lab does not know, or a need that
    is not a number of 0 or more, is refused with a message that names it."""
    if overrides is None:
        return dict(defaults)
    if not isinstance(overrides, dict):
        raise ValueError('runtime.local.json: "min_free_ram_gb" must map engines to GB, such as {"kokoro": 1}.')
    needs = dict(defaults)
    for engine, need in overrides.items():
        if engine not in defaults:
            raise ValueError(f'runtime.local.json: "min_free_ram_gb" names {engine!r}, which is not one of the Lab\'s engines '
                             f'({", ".join(sorted(defaults))}).')
        if isinstance(need, bool) or not isinstance(need, (int, float)) or not need >= 0:
            raise ValueError(f'runtime.local.json: "min_free_ram_gb" for {engine} must be a number of GB, 0 or more.')
        needs[engine] = need
    return needs


MIN_FREE_RAM_GB = with_overrides(DEFAULT_MIN_FREE_RAM_GB, runtime_setting("min_free_ram_gb"))
