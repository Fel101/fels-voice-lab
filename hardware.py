"""What this computer has that the Lab asks about, read without loading a model or importing a GPU library: whether an
NVIDIA driver is installed, the processor's name, the graphics cards Windows lists with their driver versions, and for NVIDIA
cards the generation (CUDA compute capability) and driver version that decide which NVIDIA build of PyTorch can run on them.

Standard library only, so the installers and setup.bat can use it before any package is installed. Every answer comes from
files, the registry and NVIDIA's own nvidia-smi; a value that cannot be read is left out rather than guessed.
"""

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The device class Windows files display adapters under.
DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
PROCESSOR_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SMI_TIMEOUT = 20
# What nvidia-smi is asked. A driver older than 2022 does not know compute_cap, so it is asked again without it.
SMI_FIELDS = (("name", "compute_cap", "driver_version", "memory.total"), ("name", "driver_version", "memory.total"))


def nvidia_driver_present(system_root=None):
    """Whether an NVIDIA display driver is installed: it puts nvcuda.dll in System32. It says nothing of the card's age, nor of
    whether a model's PyTorch can use it."""
    if sys.platform != "win32":
        return False
    root = Path(system_root or os.environ.get("SystemRoot", r"C:\Windows"))
    return (root / "System32" / "nvcuda.dll").is_file()


def _registry_value(key, name):
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as opened:
            value = winreg.QueryValueEx(opened, name)[0]
    except (ImportError, OSError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def processor_name():
    """The processor as Windows names it, such as "AMD Ryzen 7 PRO 8845HS w/ Radeon 780M Graphics"."""
    return _registry_value(PROCESSOR_KEY, "ProcessorNameString") or platform.processor() or None


def graphics_cards():
    """[{"name", "driver"}] for each graphics card Windows lists, once each; [] when none can be read. Windows' own virtual
    adapters (Microsoft Remote Display Adapter, Microsoft Basic Display Adapter) are left out: no model runs on them."""
    try:
        import winreg
    except ImportError:
        return []
    cards = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS) as adapters:
            for index in range(winreg.QueryInfoKey(adapters)[0]):
                subkey = winreg.EnumKey(adapters, index)
                if not subkey.isdigit():
                    continue
                name = _registry_value(rf"{DISPLAY_CLASS}\{subkey}", "DriverDesc")
                card = {"name": name, "driver": _registry_value(rf"{DISPLAY_CLASS}\{subkey}", "DriverVersion")}
                if name and not name.startswith("Microsoft ") and card not in cards:
                    cards.append(card)
    except OSError:
        return cards
    return cards


def version_pair(text):
    """A "major.minor" number such as 12.0 or 581.80 as a pair of ints, (12, 0) or (581, 80); None for [N/A] or anything else."""
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", text or "")
    return (int(match[1]), int(match[2])) if match else None


def memory_mib(text):
    """nvidia-smi's memory.total, such as "8151 MiB", as a number of MiB; None for [N/A] or anything else."""
    match = re.fullmatch(r"\s*(\d+)\s*MiB\s*", text or "")
    return int(match[1]) if match else None


def nvidia_driver_version(windows_version):
    """NVIDIA's own driver version from the one Windows lists: 32.0.15.8180 is driver 581.80, read from the last five digits.
    None when the text is not in that form."""
    parts = (windows_version or "").strip().split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return None
    digits = (parts[2] + parts[3].zfill(4))[-5:]
    return (int(digits[:3]), int(digits[3:])) if len(digits) == 5 else None


def nvidia_smi_path(system_root=None, program_files=None):
    """nvidia-smi.exe, which NVIDIA's driver puts in System32 (older drivers put it in Program Files\\NVIDIA Corporation\\NVSMI),
    or None when there is none."""
    candidates = [
        Path(system_root or os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe",
        Path(program_files or os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "NVIDIA Corporation"
        / "NVSMI"
        / "nvidia-smi.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("nvidia-smi")
    return Path(found) if found else None


def _smi_cards(smi, run):
    """The cards nvidia-smi lists, or None when it does not answer."""
    for fields in SMI_FIELDS:
        try:
            done = run(
                [str(smi), f"--query-gpu={','.join(fields)}", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SMI_TIMEOUT,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode == 0:
            break
    else:
        return None
    cards = []
    for line in (done.stdout or "").splitlines():
        values = [value.strip() for value in line.rsplit(",", len(fields) - 1)]
        if len(values) != len(fields) or not values[0]:
            continue
        found = dict(zip(fields, values))
        cards.append(
            {
                "name": found["name"],
                "capability": version_pair(found.get("compute_cap")),
                "driver": version_pair(found["driver_version"]),
                "memory_mib": memory_mib(found["memory.total"]),
            }
        )
    return cards


def nvidia_cards(*, run=subprocess.run, smi=None, listed=None):
    """The NVIDIA graphics cards as [{"name", "capability", "driver", "memory_mib"}]. capability is the card's CUDA compute
    capability as a pair, such as (12, 0) for a GeForce RTX 50 series card and (8, 9) for an RTX 40, driver is NVIDIA's driver
    version as a pair, such as (581, 80), and memory_mib its graphics memory (an "8 GB" card has about 8150). nvidia-smi answers
    them when it is there; without it, the names and driver versions Windows lists are used, and the capability and memory are
    None. [] when this computer has no NVIDIA card."""
    smi = nvidia_smi_path() if smi is None else smi
    if smi:
        cards = _smi_cards(smi, run)
        if cards is not None:
            return cards
    return [
        {"name": card["name"], "capability": None, "driver": nvidia_driver_version(card.get("driver")), "memory_mib": None}
        for card in (graphics_cards() if listed is None else listed)
        if "nvidia" in card["name"].lower()
    ]


def capability_text(capability):
    return f"{capability[0]}.{capability[1]}"


def driver_text(driver):
    return f"{driver[0]}.{driver[1]:02d}"
