"""Installed, usable and measured state for the model engines shown on the Models page.

Reading state never loads a model: files are checked by name and results come from a small
JSON store. Only EnginePool.check, which the Models page starts on request, loads a worker.
"""

import hashlib
import json
import os
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from chatterbox_support import ORIGINAL_ENGINE_ID, ORIGINAL_FILES, TURBO_ENGINE_ID, TURBO_FILES, VC_ENGINE_ID, VC_FILES
from kokoro_support import discover_kokoro_voices
from phonon_support import PHONON_ENGINE_ID, VOICE_CATALOG, default_voice, voice_names
from qwen_support import QWEN_FILES, QWEN_LAUNCHER, QWEN_MODELS

LAB = Path(__file__).resolve().parent
CHECK_TEXT = "This is a short check of the voice engine."
CHECK_VOICE = "A clear, calm adult narrator."
CHECK_MIN_SECONDS = 0.5
CHECK_MIN_PEAK = 0.001
SPEED_SAMPLES = 20
STORE_SCHEMA = 1
WEIGHT_SUFFIXES = (".safetensors", ".pth", ".pt", ".bin", ".gguf")
# VoxCPM2 reads its pinned snapshot from the top of hf-cache; the other Hugging Face engines use hf-cache/hub.
TOP_LEVEL_CACHE = {"voxcpm2"}
# Source directories that a worker adds to its import path: VoxCPM2's pinned source files, which tools/install_voxcpm.py fetches.
EXTRA_DIRECTORIES = {"voxcpm2": ("out/voxcpm2/source/src",)}
# Environments whose packages an engine may borrow when its own lacks them: a .venv-voxcpm made by hand without PyTorch reads
# .venv-fqwen's, read-only (engine_worker.py adds them to its import path), as the Lab first ran VoxCPM2 on an AMD card.
BORROWED_PACKAGES = {"voxcpm2": ".venv-fqwen/Scripts/python.exe"}
DRAMA_RECEIPT = "out/drama3/installation.json"
# A launcher in the lab folder that puts an engine's files in place, with the arguments that choose its checkpoint.
# installation() names it while files are missing.
GUIDED_INSTALLERS = {"kokoro": "setup-kokoro.bat", PHONON_ENGINE_ID: "setup-phonon.bat", TURBO_ENGINE_ID: "setup-chatterbox.bat",
                     ORIGINAL_ENGINE_ID: "setup-chatterbox.bat --original", VC_ENGINE_ID: "setup-chatterbox.bat --original",
                     **{engine: f"{QWEN_LAUNCHER} --model {model}" for engine, model in QWEN_MODELS.items()},
                     "voxcpm2": "setup-voxcpm.bat", "breeze-tts-2": "setup-breeze.bat"}
# Python packages an engine's environment must hold besides the interpreter; installation() looks for their dist-info folders.
# Kokoro and Phonon each have an environment of their own (.venv-kokoro, .venv-phonon), the three Chatterbox engines share
# .venv-chatterbox, and the four Qwen3-TTS engines .venv-fqwen; each installer makes its environment before it installs anything
# into it.
REQUIRED_PACKAGES = {"kokoro": ("kokoro",), PHONON_ENGINE_ID: ("ptts",),
                     **{engine: ("chatterbox-tts",) for engine in (TURBO_ENGINE_ID, ORIGINAL_ENGINE_ID, VC_ENGINE_ID)},
                     **{engine: ("faster-qwen3-tts",) for engine in QWEN_MODELS},
                     "voxcpm2": ("torch", "transformers", "librosa"), "breeze-tts-2": ("transformers", "accelerate")}
# Files an engine's snapshot must hold, beyond having a weight file: the engine cannot load with any of these missing.
REQUIRED_FILES = {TURBO_ENGINE_ID: TURBO_FILES, ORIGINAL_ENGINE_ID: ORIGINAL_FILES, VC_ENGINE_ID: VC_FILES,
                  **{engine: QWEN_FILES for engine in QWEN_MODELS},
                  "voxcpm2": ("model.safetensors", "audiovae.pth", "config.json", "tokenizer.json")}
BREEZE_WEIGHTS = "Breeze-TTS-2-bf16.safetensors"
# Where Breeze's loader looks for its files (breeze/loader.py: the models folder beside it, then the repository's name), and the
# ones it cannot load without.
BREEZE_FOLDER = "breeze/models/breezetts2/drbaph_Breeze-TTS-2-comfyui"
BREEZE_FILES = (BREEZE_WEIGHTS, "config.json", "tokenizer.json", "audio_tokenizer/model.safetensors")
DESIGN_ENGINES = {"breeze-tts-2", "qwen3-tts-1.7b-voice-design", "voxcpm2", "drama-3"}
CLONE_ENGINES = {"qwen3-tts-1.7b-base", "qwen3-tts-0.6b-base"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round(value, digits):
    return None if value is None else round(float(value), digits)


def _relative(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _drama_missing(root):
    receipt = root / DRAMA_RECEIPT
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
        paths = [data["model_path"], data["runtime_path"]]
    except FileNotFoundError:
        return [DRAMA_RECEIPT]
    except (OSError, ValueError, KeyError, TypeError):
        return [f"{DRAMA_RECEIPT} (unreadable)"]
    return [path for path in map(str, paths) if not Path(path).is_file()]


def _has_package(root, environment, name):
    """Whether the environment of this interpreter holds a package, judged from its dist-info folder."""
    site = (root / environment).parent.parent / "Lib" / "site-packages"
    prefix = name.lower().replace("-", "_") + "-"
    return any(item.lower().startswith(prefix) for item in _dist_infos(site))


def installation(entry, root=LAB):
    """Report whether an engine's Python environment and pinned checkpoint are on disk.

    This checks file presence only. Checksums and the real load are left to the worker, so a
    passing installation check is not a claim that the engine can speak. While files are missing, an engine that has a
    guided installer also reports its launcher as `installer`, so the Models page can say what to run.
    """
    root = Path(root)
    engine = entry["id"]
    environment = entry["runtime"]["python"]
    missing = [] if (root / environment).is_file() else [environment]
    if not missing:
        borrowed = BORROWED_PACKAGES.get(engine)
        missing.extend(f"{package} (Python package)" for package in REQUIRED_PACKAGES.get(engine, ())
                       if not _has_package(root, environment, package)
                       and not (borrowed and not _has_package(root, environment, "torch")
                                and _has_package(root, borrowed, package)))
    if engine == "drama-3":
        checkpoint = "out/drama3"
        missing.extend(_drama_missing(root))
    elif engine == "breeze-tts-2":
        checkpoint = BREEZE_FOLDER
        missing.extend(f"{BREEZE_FOLDER}/{name}" for name in BREEZE_FILES if not (root / BREEZE_FOLDER / name).is_file())
    else:
        cache = root / "hf-cache" if engine in TOP_LEVEL_CACHE else root / "hf-cache" / "hub"
        repo = entry["checkpoint"].replace("/", "--")
        snapshot = cache / f"models--{repo}" / "snapshots" / entry["local_revision"]
        checkpoint = _relative(snapshot, root)
        if not snapshot.is_dir():
            missing.append(checkpoint)
        elif not any(item.is_file() and item.suffix in WEIGHT_SUFFIXES for item in snapshot.iterdir()):
            missing.append(f"{checkpoint} (no weight file)")
        else:
            missing.extend(f"{checkpoint}/{name}" for name in REQUIRED_FILES.get(engine, ()) if not (snapshot / name).is_file())
        voices = entry.get("backend", {}).get("voices_checkpoint")
        if voices:
            # A second pinned repository holds the preset voices the model speaks with; without its catalog nothing can be chosen.
            folder = cache / f"models--{voices.replace('/', '--')}" / "snapshots" / entry["backend"]["voices_revision"]
            if not (folder / VOICE_CATALOG).is_file():
                missing.append(f"{_relative(folder, root)} ({VOICE_CATALOG})")
    for directory in EXTRA_DIRECTORIES.get(engine, ()):
        if not (root / directory).is_dir():
            missing.append(directory)
    result = {"installed": not missing, "missing": missing, "environment": environment, "checkpoint": checkpoint}
    if missing and engine in GUIDED_INSTALLERS:
        result["installer"] = GUIDED_INSTALLERS[engine]
    return result


def install_pointer(entry, probe):
    """Say how to install an engine that is missing files: an engine with a guided installer is pointed at its Install button
    and launcher, any other at the setup guide. None when its files are in place.

    `probe` is installation() or a stand-in for it. EnginePool leads the error of a failed load with this sentence, since that
    error is the first thing someone who has not installed the engine sees. A retry is enough afterwards: the studio's Generate
    reads the installed voices again when its list is empty.
    """
    if probe(entry)["installed"]:
        return None
    name = entry.get("name", entry["id"])
    launcher = GUIDED_INSTALLERS.get(entry["id"])
    if launcher is None:
        return (f"{name} is not installed, and the Lab cannot install it for you yet. Models & features lists the files it "
                "needs, and the Model runtimes section of docs/SETUP.md says how to set it up.")
    return (f"{name} is not installed. Open Models & features and press Install on its card, or run {launcher} in the lab "
            "folder, then try again.")


def _dist_infos(site_packages):
    """The installed packages in one site-packages folder: its *.dist-info folder names."""
    if not site_packages.is_dir():
        return []
    return sorted(item.name for item in site_packages.iterdir() if item.name.endswith(".dist-info"))


def engine_fingerprint(entry, root=LAB):
    """What a stored check is a statement about: the catalog's pin for the engine, and the packages installed in the
    environment it runs in (plus the packages it borrows from another environment: BORROWED_PACKAGES).

    A check says the engine spoke when it was made. After a dependency pin, a package or a checkpoint snapshot changes,
    that result no longer describes the engine, so the Models page asks for a new check instead of repeating the old one.
    It reads folder names only and loads nothing.
    """
    root = Path(root)
    pinned = {key: entry.get(key) for key in ("checkpoint", "local_revision", "backend", "runtime", "precision")}
    environment = (root / entry["runtime"]["python"]).parent.parent
    packages = _dist_infos(environment / "Lib" / "site-packages")
    for site in sorted(environment.glob("lib/python*/site-packages")):
        packages += _dist_infos(site)
    borrowed = BORROWED_PACKAGES.get(entry["id"])
    if borrowed and not _has_package(root, entry["runtime"]["python"], "torch"):
        # The engine imports the borrowed packages, so a change there changes it too.
        packages += _dist_infos((root / borrowed).parent.parent / "Lib" / "site-packages")
    payload = json.dumps({"pinned": pinned, "packages": packages}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def smoke_request(engine, loaded=None, *, root=LAB):
    """The shortest request that uses an engine's normal path, so a passing check predicts Perform."""
    base = {"engine": engine, "text": CHECK_TEXT}
    if engine == "kokoro":
        voices = discover_kokoro_voices()
        return {**base, "voice": "af_heart" if not voices or "af_heart" in voices else voices[0]}
    if engine == PHONON_ENGINE_ID:
        return {**base, "voice": default_voice(voice_names(root))}
    if engine == TURBO_ENGINE_ID and not (Path(root) / "reference" / "af_heart.wav").is_file():
        # Turbo clones the default reference when there is one (Kokoro makes it). Its own environment cannot make it, so
        # without it the check speaks in the built-in voice: the installer's speech check is the one that clones.
        return {**base, "clone": False}
    if engine in {TURBO_ENGINE_ID, "orig"}:
        # Turbo clones the default reference and Original uses its built-in voice, as a default call does.
        return base
    if engine in DESIGN_ENGINES:
        return {**base, "mode": "design", "voice_description": CHECK_VOICE}
    if engine in CLONE_ENGINES:
        # x-vector conditioning needs no transcript, and the bundled reference has none.
        return {**base, "reference_path": str(Path(root) / "reference" / "af_heart.wav"), "xvec_only": True}
    if engine == "qwen3-tts-1.7b-custom-voice":
        speakers = (loaded or {}).get("supported_speakers") or []
        if not speakers:
            raise ValueError("The CustomVoice checkpoint reported no speakers to check.")
        return {**base, "speaker": speakers[0]}
    raise ValueError(f"No speech check is defined for {engine!r}.")


def audible(samples, sample_rate):
    """Return why generated audio does not count as speech, or None when it passes."""
    seconds = samples.size / sample_rate
    if seconds < CHECK_MIN_SECONDS:
        return f"generated {seconds:.2f} s of audio; at least {CHECK_MIN_SECONDS} s is expected"
    if float(np.max(np.abs(samples))) < CHECK_MIN_PEAK:
        return "generated silence"
    return None


def clip_device(info, default):
    """Use the device a worker reports for its clip, or the service device when it reports none."""
    device = info.get("device") if isinstance(info, dict) else None
    return device if isinstance(device, str) and device else default


def failure_reason(exc):
    """Keep one line for the Models page: the error type and the first line of its message.

    The first line carries a worker's log path. Progress-bar output that follows it is dropped.
    """
    lines = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {lines[0] if lines else ''}"[:400]


def check_record(*, status, device, ran_on=None, reason=None, load_seconds=None, total_s=None, audio_seconds=None,
                 fingerprint=None):
    factor = round(total_s / audio_seconds, 3) if total_s and audio_seconds else None
    return {"status": status, "device": device, "ran_on": ran_on or device, "checked_at": utc_now(),
            "reason": reason, "load_seconds": _round(load_seconds, 3), "total_s": _round(total_s, 3),
            "audio_seconds": _round(audio_seconds, 3), "real_time_factor": factor, "fingerprint": fingerprint}


class EngineCheckStore:
    """Check results and clip timings per engine and device, kept as one small JSON file.

    Writes are best effort: a failed save never fails a clip, and the in-memory copy still
    answers the Models page until the process ends.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.write_error = None
        self._data = self._read()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError):
            self._set_aside()
            return self._empty()
        if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:
            self._set_aside()
            return self._empty()
        data.setdefault("checks", {})
        data.setdefault("speed", {})
        return data

    @staticmethod
    def _empty():
        return {"schema": STORE_SCHEMA, "checks": {}, "speed": {}}

    def _set_aside(self):
        try:
            os.replace(self.path, self.path.with_name(self.path.name + ".corrupt"))
        except OSError:
            pass

    def _write(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + ".tmp")
            temporary.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(temporary, self.path)
            self.write_error = None
        except OSError as exc:
            self.write_error = f"{type(exc).__name__}: {exc}"

    def record_check(self, engine, device, record):
        with self._lock:
            self._data["checks"].setdefault(engine, {})[device] = record
            self._write()
        return record

    def record_clip(self, engine, device, total_s, audio_seconds):
        # Clips under a millisecond carry no usable timing, and would round to zero audio seconds.
        if not (total_s > 0 and audio_seconds >= 0.001):
            return
        sample = {"total_s": round(float(total_s), 4), "audio_seconds": round(float(audio_seconds), 6), "at": utc_now()}
        with self._lock:
            samples = self._data["speed"].setdefault(engine, {}).setdefault(device, [])
            samples.append(sample)
            del samples[:-SPEED_SAMPLES]
            self._write()

    def checks_for(self, engine):
        with self._lock:
            return {device: dict(record) for device, record in self._data["checks"].get(engine, {}).items()}

    def speed_rows(self):
        rows = []
        with self._lock:
            for engine, by_device in sorted(self._data["speed"].items()):
                for device, samples in sorted(by_device.items()):
                    factors = [item["total_s"] / item["audio_seconds"] for item in samples if item["audio_seconds"] > 0]
                    if not factors:
                        continue
                    last = samples[-1]
                    rows.append({"engine": engine, "device": device, "clips": len(samples),
                                 "median_real_time_factor": round(statistics.median(factors), 3),
                                 "last_total_s": last["total_s"], "last_audio_seconds": last["audio_seconds"],
                                 "last_measured_at": last["at"]})
        return rows


def _state(integrated, installed, task_only, current, outdated=False):
    if not integrated:
        return "not_integrated"
    if not installed:
        return "not_installed"
    if task_only:
        return "conversion_task"
    if current is None:
        return "not_checked"
    if outdated:
        return "check_outdated"
    return "usable" if current.get("status") == "passed" else "check_failed"


def current_check(checks, device):
    """The stored check for the device an engine runs on now. A check asked of the graphics card that ran on the CPU (a CPU
    build of PyTorch falls back so) is a check on the CPU, as the ones made before each model had its own switch were."""
    current = checks.get(device)
    if current is None and device == "cpu":
        fallen = checks.get("cuda")
        if fallen is not None and fallen.get("ran_on") == "cpu":
            return fallen
    return current


def status_payload(entries, *, device, busy, store, probe, fingerprint_of=engine_fingerprint, ram_needs=None, ram=None,
                   device_of=None):
    """One row per catalog engine: integrated, installed and usable on this device, plus the speed table.

    A stored check counts only while its fingerprint matches the engine's current one. Checks made before fingerprints
    were recorded have none, so they read as out of date until the engine is checked again. `ram_needs` maps an engine to the
    free RAM in GB the Lab wants before it loads it, and `ram` is this computer's total and free RAM; the Models page sets one
    beside the other. `device_of(entry, installed)` gives an engine's GPU/CPU switch (engine_devices.device_facts): the row's
    `device` is then the one its worker is asked for, and its check is the one made there; without it every engine runs on
    the service's device."""
    ram_needs = ram_needs or {}
    rows = []
    for entry in entries.values():
        integration = entry.get("integration", {})
        integrated = integration.get("service_api") is True
        task_only = integration.get("task_only") is True
        install = probe(entry) if integrated else {"installed": False, "missing": [],
                                                    "environment": entry.get("runtime", {}).get("python"),
                                                    "checkpoint": None}
        facts = device_of(entry, install["installed"]) if device_of is not None and integrated else None
        engine_device = facts["device"] if facts else device
        checks = store.checks_for(entry["id"]) if store is not None else {}
        current = current_check(checks, engine_device)
        outdated = (current is not None and integrated and install["installed"] and not task_only
                    and current.get("fingerprint") != fingerprint_of(entry))
        row = {"id": entry["id"], "name": entry.get("name", entry["id"]), "integrated": integrated,
               "task_only": task_only, "installation": install,
               "state": _state(integrated, install["installed"], task_only, current, outdated),
               "check": current, "check_outdated": outdated, "checks": checks,
               "min_free_ram_gb": ram_needs.get(entry["id"]), "device": engine_device}
        if facts:
            row.update(switch=facts["switch"], device_choice=facts["choice"], gpu=facts["gpu"])
            if facts["gpu"].get("cpu") and not facts["gpu"]["install"] and not install["installed"]:
                # A model that runs only on a graphics card, and whose GPU build the app cannot install on this computer: the
                # card offers no Install and names no launcher that would refuse, and its switch note says why.
                row["installation"] = {key: value for key, value in install.items() if key != "installer"}
        rows.append(row)
    return {"device": device, "busy": bool(busy), "engines": rows, "ram": ram,
            "speed": store.speed_rows() if store is not None else [],
            "store_error": store.write_error if store is not None else None}
