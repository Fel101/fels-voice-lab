"""Shared helpers for the voice lab.

Import this module before anything that touches Hugging Face: it points the model cache at
voice-lab/hf-cache so every downloaded model lives inside this folder (delete the folder and
they are all gone; it never touches the global Hugging Face cache other speech tools use).
"""

import ctypes
import json
import os
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(LAB / "hf-cache"))
# Machine-specific device choices belong in an ignored local file, never a public default.
_settings_path = LAB / "runtime.local.json"
_settings = json.loads(_settings_path.read_text(encoding="utf-8")) if _settings_path.is_file() else {}
if "hip_visible_devices" in _settings:
    os.environ.setdefault("HIP_VISIBLE_DEVICES", str(_settings["hip_visible_devices"]))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

def torch_runtime():
    """Keep the broker usable without installing a GPU/model stack."""
    import torch
    if _settings.get("disable_cudnn", False):
        torch.backends.cudnn.enabled = False
    return torch


def skip_miopen(device):
    """On AMD's ROCm build of PyTorch, run convolutions and LSTMs with PyTorch's own kernels instead of MIOpen's, which ROCm
    puts behind torch.backends.cudnn. MIOpen tunes its kernels for every input length it has not met before: on a Radeon AI
    PRO R9700, Kokoro took 11 to 53 s over each sentence of a new length with it, and 0.6 to 0.9 s without it. Nothing changes
    on the CPU or on an NVIDIA build."""
    if device == "cuda":
        torch = torch_runtime()
        if getattr(torch.version, "hip", None):
            torch.backends.cudnn.enabled = False


def runtime_setting(name, default=None):
    return _settings.get(name, default)

OUT = LAB / "out"
OUT.mkdir(exist_ok=True)

from lines import REFERENCE_TEXT, TAG_RE, strip_tags  # noqa: E402,F401  (re-exported for the other scripts)


def _ram_gb():
    """(total, free) physical RAM in GB (Windows), without needing psutil."""
    if os.name != "nt":
        page = os.sysconf("SC_PAGE_SIZE")
        return os.sysconf("SC_PHYS_PAGES") * page / 2**30, os.sysconf("SC_AVPHYS_PAGES") * page / 2**30
    class MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    stat = MemStatus()
    stat.dwLength = ctypes.sizeof(MemStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat.ullTotalPhys / 2**30, stat.ullAvailPhys / 2**30


def free_ram_gb():
    """Free physical RAM in GB (Windows), without needing psutil."""
    return _ram_gb()[1]


def ram_report():
    """This computer's total and free RAM in GB, for the Models page to set beside each model's free-RAM need."""
    total, free = _ram_gb()
    return {"total_gb": round(total, 1), "free_gb": round(free, 1)}


def require_ram(min_gb, what):
    free = free_ram_gb()
    if free < min_gb:
        raise MemoryError(f"Only {free:.1f} GB RAM free; not loading {what} (needs {min_gb} GB free). "
                          "Close something heavy (a program with a large model loaded) and retry.")


DEVICES = ("auto", "cpu", "cuda")


def pick_device(want="auto", *, nvidia_present=None, amd_usable=None):
    """The device the service asks its workers for: "cpu", or "cuda" for a graphics card (NVIDIA, or AMD through a ROCm build
    of PyTorch, which answers to the same name). "auto" takes runtime.local.json's "device" when it sets one; otherwise a
    graphics card when this computer has an NVIDIA driver, an AMD card whose chip AMD's ROCm build of PyTorch has code for
    (tools/amd_build.py), or this environment's PyTorch can see one, and the CPU when not.

    The service's .venv holds no PyTorch, so the graphics card is what usually decides. A worker whose own PyTorch cannot use
    the card (the CPU build the installers put in unless told --nvidia) runs on the CPU anyway: see
    worker_runtime.usable_device."""
    if want == "auto":
        want = runtime_setting("device", "auto")
        if want not in DEVICES:
            raise ValueError(f'runtime.local.json: "device" must be one of {", ".join(DEVICES)}, not {want!r}.')
    if want != "auto":
        return want
    if nvidia_present is None:
        from hardware import nvidia_driver_present as nvidia_present
    if nvidia_present():
        return "cuda"
    if amd_usable is None:
        try:
            from tools.amd_build import usable_card as amd_usable
        except ImportError:  # a script started from elsewhere, without the lab folder on its path
            amd_usable = bool
    if amd_usable():
        return "cuda"
    try:
        torch = torch_runtime()
    except ModuleNotFoundError as exc:
        if exc.name != "torch":
            raise
        return "cpu"
    return "cuda" if torch.cuda.is_available() else "cpu"


def sync(device):
    if device == "cuda":
        torch = torch_runtime()
        torch.cuda.synchronize()


def to_numpy(audio):
    if hasattr(audio, "detach"):
        audio = audio.detach().float().cpu().numpy()
    return np.asarray(audio, dtype=np.float32).reshape(-1)


def save_wav(path, wav, sr):
    sf.write(str(path), to_numpy(wav), int(sr))


def release_gpu():
    import gc
    gc.collect()
    try:
        torch = torch_runtime()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


class Timer:
    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.t0
