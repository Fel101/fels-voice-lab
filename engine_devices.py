"""Where each model runs, the graphics card (GPU) or the processor (CPU), as chosen with the switch on its Models page card.

The service picks one device when it starts (common.pick_device): the graphics card when it finds an NVIDIA driver or an AMD
card that AMD's ROCm build of PyTorch has code for, or when runtime.local.json or --device says "cuda", and the CPU otherwise.
A service on the CPU keeps every model there. On a service with a graphics card, each model that can use one has a switch, and
its choice is kept in data/engines/devices.json beside the test results; it applies from the model's next load. A model nobody
has switched uses the card when its PyTorch can.

Which PyTorch a model's environment holds is read from the name of its dist-info folder, so nothing is imported: 2.12.0+cu130
is an NVIDIA (CUDA) build, 2.12.0+rocm7.14.0 an AMD (ROCm) build, and 2.12.0 or 2.12.0+cpu the CPU build, which is what PyPI
serves for Windows. A CPU build cannot use the card whatever the switch says, so the model is then asked for the CPU, and its
card offers the GPU build when the app can install one for this computer (engine_install.EngineInstaller.gpu_build).
"""

import json
import os
import re
import threading
from pathlib import Path

import engine_adapters

CHOICES = ("gpu", "cpu")
STORE_SCHEMA = 1
# Models that never use the graphics card in the Lab, and what their card says about it. Their workers are asked for the
# service's device as before: an adapter says why its engine does not (Phonon's ptts has no GPU code), and Drama 3
# (speech.cpp) chooses its own device.
CPU_ONLY = {
    **engine_adapters.table("cpu_only"),
    "drama-3": "Drama 3 runs on the CPU in the Lab; docs/SETUP.md describes its experimental GPU setting for short designs.",
}
# Models that run only on a graphics card in the Lab, and what their card says about the CPU. Their workers refuse the CPU.
GPU_ONLY = {
    **{
        engine: "Qwen3-TTS runs only on a graphics card in the Lab: its fast decoder records CUDA graphs."
        for engine in (
            "qwen3-tts-1.7b-voice-design",
            "qwen3-tts-1.7b-base",
            "qwen3-tts-0.6b-base",
            "qwen3-tts-1.7b-custom-voice",
        )
    },
    "voxcpm2": "VoxCPM2 runs only on a graphics card with at least 7 GB of memory.",
    "breeze-tts-2": "Breeze TTS 2 runs only on a graphics card in the Lab.",
}
NO_GPU = (
    "This service runs every model on the CPU: it found no NVIDIA graphics card driver and no AMD card that AMD's ROCm build "
    'of PyTorch has code for, or it was started for the CPU (--device cpu, or "device": "cpu" in runtime.local.json). '
    "docs/TROUBLESHOOTING.md (Graphics cards) says more."
)
_TORCH = re.compile(r"torch-(?P<version>[^-]+)\.dist-info", re.IGNORECASE)


def torch_build(root, entry):
    """(build, version) for the PyTorch in an engine's own environment: build is "cuda", "rocm" or "cpu", and both are None when
    the environment holds no PyTorch (or is not there). It reads folder names only."""
    site = (Path(root) / entry["runtime"]["python"]).parent.parent / "Lib" / "site-packages"
    try:
        names = os.listdir(site)
    except OSError:
        return None, None
    for name in names:
        match = _TORCH.fullmatch(name)
        if match:
            version = match["version"]
            local = version.partition("+")[2].lower()
            if local.startswith("cu"):
                return "cuda", version
            if local.startswith("rocm"):
                return "rocm", version
            return "cpu", version
    return None, None


def device_facts(engine, *, service_device, installed, build, choice, gpu_install):
    """What the Models page shows on an engine's switch, and which device its worker is asked for.

    `build` is torch_build()'s first value, `choice` the stored "gpu", "cpu" or None, and `gpu_install` (ok, reason) says
    whether the app can install a GPU build of the engine on this computer. Returns:
      device       the device its worker is asked for ("cuda" or "cpu"; a CPU-only engine keeps the service's device),
      switch       what the switch shows, "gpu" or "cpu",
      choice       the stored choice, or None,
      gpu          {choosable, reason, build, install, cpu}: whether GPU can be picked on the switch, why not, the
                   installed PyTorch build, whether Install puts in (or would add) the GPU build, and, for a model that runs
                   only on a graphics card, why CPU cannot be picked (None for the others).
    """
    install_ok, install_reason = gpu_install
    if engine in GPU_ONLY:
        # The CPU is never an option: the switch stays on GPU, and Install always puts in the GPU build.
        usable = service_device == "cuda" and ((installed and build != "cpu") or install_ok)
        return {
            "device": "cuda" if service_device == "cuda" else service_device,
            "switch": "gpu",
            "choice": None,
            "gpu": {
                "choosable": usable,
                "reason": None if usable else (install_reason if service_device == "cuda" else NO_GPU),
                "build": build,
                "install": bool(service_device == "cuda" and install_ok and (not installed or build == "cpu")),
                "cpu": GPU_ONLY[engine],
            },
        }
    if engine in CPU_ONLY:
        return {
            "device": service_device,
            "switch": "cpu",
            "choice": None,
            "gpu": {"choosable": False, "reason": CPU_ONLY[engine], "build": build, "install": False, "cpu": None},
        }
    if service_device != "cuda":
        return {
            "device": "cpu",
            "switch": "cpu",
            "choice": choice,
            "gpu": {"choosable": False, "reason": NO_GPU, "build": build, "install": False, "cpu": None},
        }
    if installed and build != "cpu":
        # A GPU build, or one this function cannot read (a model set up by hand): the worker finds out for itself and falls
        # back to the CPU with a note when its PyTorch cannot use the card (worker_runtime.usable_device).
        choosable, reason, default = True, None, "gpu"
    else:
        # Not installed, or installed with the CPU build: the card can use the graphics card only through a GPU build that the
        # app can install here. A model that is not installed yet gets that build by default; a CPU build stays on the CPU
        # until someone switches it.
        choosable, reason = install_ok, (None if install_ok else install_reason)
        default = "gpu" if install_ok and not installed else "cpu"
    switch = choice if choice == "cpu" or (choice == "gpu" and choosable) else default
    device = "cuda" if switch == "gpu" and installed and build != "cpu" else "cpu"
    return {
        "device": device,
        "switch": switch,
        "choice": choice,
        "gpu": {
            "choosable": choosable,
            "reason": reason,
            "build": build,
            "install": bool(switch == "gpu" and install_ok and (not installed or build == "cpu")),
            "cpu": None,
        },
    }


class EngineDeviceStore:
    """The device chosen for each engine on its card, kept as one small JSON file. A file that cannot be read counts as no
    choices, and a failed write leaves the choice in memory until the service stops, with the error kept for the page."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.write_error = None
        self._choices = self._read()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        devices = data.get("devices") if isinstance(data, dict) and data.get("schema") == STORE_SCHEMA else None
        if not isinstance(devices, dict):
            return {}
        return {key: value for key, value in devices.items() if isinstance(key, str) and value in CHOICES}

    def get(self, engine):
        with self._lock:
            return self._choices.get(engine)

    def set(self, engine, choice):
        """Keep `choice` ("gpu" or "cpu") for an engine; None forgets it, so the engine follows the service again."""
        if choice is not None and choice not in CHOICES:
            raise ValueError("Choose gpu or cpu.")
        with self._lock:
            if choice is None:
                self._choices.pop(engine, None)
            else:
                self._choices[engine] = choice
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(self.path.name + ".tmp")
                temporary.write_text(
                    json.dumps({"schema": STORE_SCHEMA, "devices": self._choices}, indent=2, sort_keys=True), encoding="utf-8"
                )
                os.replace(temporary, self.path)
                self.write_error = None
            except OSError as exc:
                self.write_error = f"{type(exc).__name__}: {exc}"
