"""Thin wrappers so Kokoro, Phonon and the two Chatterbox models look the same to the test scripts.

Each engine's speak() returns (wav float32 numpy, sample_rate, info dict). info always has
"total_s" (wall time to finish the clip) and, where the engine streams, "first_chunk_s"
(time until the first sentence of audio existed - what you would wait before she starts talking).
"""

import time

from common import LAB, REFERENCE_TEXT, Timer, skip_miopen, sync, to_numpy
import synthetic_label

import numpy as np


class Kokoro:
    """Kokoro-82M: tiny fixed voices, locale-aware G2P, equal-weight voice blending."""
    label = "Kokoro"
    sr = 24000

    def __init__(self, device):
        from kokoro import KModel
        from kokoro_support import KOKORO_REPO, kokoro_snapshot_dir

        self.device = device
        skip_miopen(device)
        self.snapshot = kokoro_snapshot_dir()
        config_path = self.snapshot / "config.json"
        weights_path = self.snapshot / "kokoro-v1_0.pth"
        if not config_path.is_file() or not weights_path.is_file():
            raise RuntimeError(f"Kokoro local checkpoint is incomplete at {self.snapshot}; "
                               "restore its cached snapshot before loading the worker.")
        # Give KModel the cached files explicitly. This avoids its default Hub lookup and
        # prevents selecting a voice or language from triggering an implicit download.
        self.model = KModel(repo_id=KOKORO_REPO, config=str(config_path), model=str(weights_path)).to(device).eval()
        self.pipes = {}

    def _pipeline(self, language):
        from kokoro import KPipeline
        from kokoro_support import KOKORO_REPO

        if language not in self.pipes:
            # Kokoro documents one language pipeline per shared KModel.
            self.pipes[language] = KPipeline(lang_code=language, repo_id=KOKORO_REPO,
                                             model=self.model, device=self.device)
        return self.pipes[language]

    def speak(self, text, voice="af_heart", speed=1.0, language="English"):
        import torch
        from kokoro_support import (KOKORO_LANGUAGES, discover_kokoro_voices,
                                    normalize_kokoro_voices, resolve_kokoro_language)
        from kokoro_timing import timing_for_chunks

        voices = normalize_kokoro_voices(voice, discover_kokoro_voices())
        language_code = resolve_kokoro_language(language, voices)
        pipeline = self._pipeline(language_code)
        packs = [pipeline.load_single_voice(str(self.snapshot / "voices" / f"{item}.pt")) for item in voices]
        voice_pack = packs[0] if len(packs) == 1 else torch.mean(torch.stack(packs), dim=0)
        t0 = time.perf_counter()
        first = None
        parts = []
        phoneme_segments = []
        timed_chunks = []
        for result in pipeline(text, voice=voice_pack, speed=speed, split_pattern=r"\n+"):
            graphemes, phonemes, audio = result
            phoneme_segments.append({"graphemes": graphemes, "phonemes": phonemes})
            if audio is None:
                continue
            if first is None:
                sync(self.device)
                first = time.perf_counter() - t0
            offset = sum(len(part) for part in parts) / self.sr
            parts.append(to_numpy(audio))
            durations = None if result.pred_dur is None else result.pred_dur.tolist()
            timed_chunks.append({"phonemes": phonemes, "tokens": result.tokens, "durations": durations, "offset": offset})
        sync(self.device)
        wav = np.concatenate(parts) if parts else np.zeros(0, np.float32)
        info = {"first_chunk_s": first, "total_s": time.perf_counter() - t0,
                "language": language_code, "language_name": KOKORO_LANGUAGES[language_code]["name"],
                "voice": ",".join(voices), "speed": float(speed), "phoneme_segments": phoneme_segments}
        # Kokoro's durations place every phoneme. They are kept only when every chunk can be placed exactly.
        timing, reason = timing_for_chunks(timed_chunks, self.model.vocab)
        if timing is not None:
            info["timing"] = timing
        else:
            info["timing_unavailable"] = reason
        if len(voices) > 1:
            info["voice_blend"] = {"method": "equal_arithmetic_mean", "voices": voices,
                                   "weights": [round(1 / len(voices), 8)] * len(voices)}
        return wav, self.sr, info


class Phonon:
    """Phonon 31M English: 37 preset voices, no cloning, on the CPU through the ptts runtime (numpy only, no PyTorch).

    The model and the voice catalog load from their pinned snapshots in hf-cache, so nothing reaches the network.
    """
    label = "Phonon"
    sr = 24000

    def __init__(self, device):
        import ptts
        from phonon_support import missing_files, pins, snapshot_dirs, voice_names

        # ptts runs on the CPU only, whatever device the service chose for the models that can use a GPU.
        self.device = "cpu"
        missing = missing_files()
        if missing:
            raise RuntimeError(f"Phonon local files are incomplete: {', '.join(missing)} is missing from hf-cache/hub. "
                               "Run setup-phonon.bat to put them back.")
        model_dir, voices_dir = snapshot_dirs()
        self.tts = ptts.TTS(model=str(model_dir), voice_catalog=str(voices_dir), lang="en", quant="q8", device="cpu")
        self.sr = int(self.tts.sample_rate)
        self.version = ptts.__version__
        self.pins = pins()
        self.voices = voice_names()
        self._delta = None

    def speak(self, text, voice=None, seed=42, temperature=0.3, duration_delta=0.0):
        from phonon_support import LANGUAGE, PHONON_ENGINE_ID, resolve_voice

        voice = resolve_voice(voice, self.voices)
        if duration_delta != self._delta:
            # No condition at all is the model's own pace; a shift moves it by a few tenths either way.
            self.tts.set_conditions({"duration_delta": float(duration_delta)} if duration_delta else {})
            self._delta = duration_delta
        t0 = time.perf_counter()
        wav = self.tts.synth(text, voice=voice, seed=int(seed), temperature=float(temperature))
        total = time.perf_counter() - t0
        (model, model_revision), (voices, voices_revision) = self.pins["model"], self.pins["voices"]
        info = {"total_s": total, "device": self.device, "backend": "ptts", "backend_version": self.version,
                "checkpoint": model, "checkpoint_revision": model_revision, "voices_checkpoint": voices,
                "voices_revision": voices_revision, "engine_id": PHONON_ENGINE_ID, "language": LANGUAGE, "speaker": voice,
                "seed": int(seed), "temperature": float(temperature), "duration_delta": float(duration_delta)}
        return np.asarray(wav, dtype=np.float32).reshape(-1), self.sr, info


class ChatterboxTurbo:
    """Chatterbox Turbo (350M): understands [laugh] [chuckle] [cough]. It ignores exaggeration/cfg
    (its own code says so), so the only controls are the reference voice and the tags.

    The voice is prepared once and reused; passing audio_prompt_path to generate() would redo the
    voice analysis on every clip, which is not what a running service would do.

    It loads the files of the pinned snapshot directly (from_local), so no Hub lookup or download can happen, and it does not
    need the ten-step decoder that from_pretrained would also want.
    """
    label = "Chatterbox Turbo"

    def __init__(self, device):
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        from chatterbox_support import TURBO_FILES, turbo_snapshot_dir

        self.device = device
        skip_miopen(device)
        snapshot = turbo_snapshot_dir()
        missing = [name for name in TURBO_FILES if not (snapshot / name).is_file()]
        if missing:
            raise RuntimeError(f"Chatterbox Turbo local files are incomplete: {', '.join(missing)} is missing from {snapshot}. "
                               "Run setup-chatterbox.bat to put them back.")
        self.model = ChatterboxTurboTTS.from_local(snapshot, device)
        self.sr = self.model.sr
        self._builtin = self.model.conds      # None when the download ships no default voice
        self._ref = None

    def has_builtin_voice(self):
        return self._builtin is not None

    def prepare_reference(self, ref_wav):
        """Load and retain one speaker reference for subsequent generate calls."""
        self._use_voice(ref_wav)
        return {"prepared": True, "reference": str(ref_wav)}

    def _use_voice(self, ref_wav):
        if ref_wav:
            if str(ref_wav) != self._ref:
                self.model.prepare_conditionals(str(ref_wav))
                self._ref = str(ref_wav)
        else:
            if self._builtin is None:
                raise RuntimeError("This Chatterbox Turbo download has no built-in voice; give it a reference clip.")
            self.model.conds = self._builtin
            self._ref = None

    def speak(self, text, ref_wav=None, **sampling):
        self._use_voice(ref_wav)
        with Timer() as t:
            wav = self.model.generate(text, **sampling)
            sync(self.device)
        return to_numpy(wav), self.sr, {"total_s": t.seconds, "sampling": dict(sampling)}


class ChatterboxOriginal:
    """Original Chatterbox (500M): built-in default voice or a reference clip, plus the
    exaggeration and cfg_weight controls (higher exaggeration = more dramatic, lower cfg = slower pace).

    Like Turbo it loads the files of the pinned snapshot directly (from_local), so no Hub lookup can happen."""
    label = "Chatterbox"

    def __init__(self, device):
        from chatterbox.tts import ChatterboxTTS
        from chatterbox_support import ORIGINAL_FILES, original_snapshot_dir

        self.device = device
        skip_miopen(device)
        snapshot = original_snapshot_dir()
        missing = [name for name in ORIGINAL_FILES if not (snapshot / name).is_file()]
        if missing:
            raise RuntimeError(f"Chatterbox 500M local files are incomplete: {', '.join(missing)} is missing from {snapshot}. "
                               "Run setup-chatterbox.bat --original to put them back.")
        self.model = ChatterboxTTS.from_local(snapshot, device)
        self.sr = self.model.sr
        self._builtin = self.model.conds
        self._ref = None
        self._ref_exaggeration = None

    def has_builtin_voice(self):
        return self._builtin is not None

    def prepare_reference(self, ref_wav, exaggeration=0.5):
        """Load and retain one speaker reference with the requested conditioning strength."""
        self._use_voice(ref_wav, exaggeration)
        return {"prepared": True, "reference": str(ref_wav), "exaggeration": float(exaggeration)}

    def _use_voice(self, ref_wav, exaggeration):
        if ref_wav:
            if str(ref_wav) != self._ref or float(exaggeration) != self._ref_exaggeration:
                self.model.prepare_conditionals(str(ref_wav), exaggeration=exaggeration)
                self._ref = str(ref_wav)
                self._ref_exaggeration = float(exaggeration)
        else:
            self.model.conds = self._builtin
            self._ref = None
            self._ref_exaggeration = None

    def speak(self, text, ref_wav=None, exaggeration=0.5, cfg_weight=0.5, **sampling):
        self._use_voice(ref_wav, exaggeration)
        effective = {"exaggeration": float(exaggeration), "cfg_weight": float(cfg_weight), **sampling}
        with Timer() as t:
            wav = self.model.generate(text, **effective)
            sync(self.device)
        return to_numpy(wav), self.sr, {"total_s": t.seconds, "sampling": effective}


REFERENCE_DIR = LAB / "reference"
REFERENCE_WAV = REFERENCE_DIR / "af_heart.wav"


def build_engine(key, device):
    """The engine behind one of the worker keys: kokoro, phonon-31m-en, turbo (Chatterbox Turbo) or orig (Chatterbox Original)."""
    return {"kokoro": Kokoro, "phonon-31m-en": Phonon, "turbo": ChatterboxTurbo, "orig": ChatterboxOriginal}[key](device)


def ensure_reference(kokoro=None):
    """A ~14 s clip of Kokoro's af_heart, the default reference voice that the cloning models' checks clone.

    Synthetic fixture with no human source recording. Model terms and applicable identity rights still apply. It needs Kokoro,
    so the Kokoro worker and Kokoro's installer make it, with the model they have already loaded (`kokoro`); without one it
    loads Kokoro on the CPU.
    """
    if REFERENCE_WAV.exists():
        return
    REFERENCE_DIR.mkdir(exist_ok=True)
    print("Making the reference voice clip (Kokoro af_heart) ...", flush=True)
    eng = kokoro or build_engine("kokoro", "cpu")
    wav, sr, _ = eng.speak(REFERENCE_TEXT, voice="af_heart")
    # Kokoro made it, so it carries the AI-generated speech label like every other clip the Lab makes.
    synthetic_label.write_wav(REFERENCE_WAV, to_numpy(wav), sr, engine_id="kokoro")
    print(f"  reference clip: {len(wav) / sr:.1f} s", flush=True)
