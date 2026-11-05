"""What this computer has that the Lab asks about, read without loading a model or importing a GPU library: whether an
NVIDIA driver is installed, the processor's name, the graphics cards Windows lists with their driver versions, for NVIDIA
cards the generation (CUDA compute capability) and driver version that decide which NVIDIA build of PyTorch can run on them,
and for AMD cards the chip (such as gfx1201) that decides which of AMD's ROCm code packages they need.

Standard library only, so the installers and setup.bat can use it before any package is installed. Every answer comes from
files, the registry, NVIDIA's own nvidia-smi and AMD's own OpenCL driver; a value that cannot be read is left out rather than
guessed.
"""

import json
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
AMD_TIMEOUT = 30
# What nvidia-smi is asked. A driver older than 2022 does not know compute_cap, so it is asked again without it.
SMI_FIELDS = (("name", "compute_cap", "driver_version", "memory.total"), ("name", "driver_version", "memory.total"))
# Settings that hide graphics cards from AMD's runtimes. The probe below runs without them, so it lists every card.
VISIBILITY = ("HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL")
# Asks AMD's OpenCL driver, which every AMD graphics driver for Windows installs, for its cards: the chip by its LLVM name
# (CL_DEVICE_NAME, such as gfx1201), the card's own name (CL_DEVICE_BOARD_NAME_AMD), its memory (for a chip built into the
# processor, the share of system memory it can use), whether it is built into the processor (CL_DEVICE_HOST_UNIFIED_MEMORY),
# and its PCI bus (CL_DEVICE_TOPOLOGY_AMD), which PyTorch reports too. It runs in a Python of its own, so a driver fault cannot
# stop the program that asks. It prints one JSON line.
AMD_PROBE = r"""
import ctypes, json
cl = ctypes.WinDLL("OpenCL.dll")
for name in ("clGetPlatformIDs", "clGetPlatformInfo", "clGetDeviceIDs", "clGetDeviceInfo"):
    getattr(cl, name).restype = ctypes.c_int32
cards = []
count = ctypes.c_uint32()
if cl.clGetPlatformIDs(0, None, ctypes.byref(count)) == 0 and count.value:
    platforms = (ctypes.c_void_p * count.value)()
    cl.clGetPlatformIDs(count.value, platforms, None)
    def info(function, handle, key, size=256):
        buffer, got = ctypes.create_string_buffer(size), ctypes.c_size_t()
        if function(ctypes.c_void_p(handle), ctypes.c_uint32(key), ctypes.c_size_t(size), buffer, ctypes.byref(got)):
            return None
        return buffer.raw[: got.value]
    def text(value):
        return value.split(b"\0")[0].decode("utf-8", "replace").strip() if value else None
    def number(value):
        return int.from_bytes(value, "little") if value else None
    for platform in platforms:
        if "Advanced Micro Devices" not in (text(info(cl.clGetPlatformInfo, platform, 0x0903)) or ""):
            continue
        found = ctypes.c_uint32()
        if cl.clGetDeviceIDs(ctypes.c_void_p(platform), ctypes.c_uint64(4), 0, None, ctypes.byref(found)) or not found.value:
            continue
        devices = (ctypes.c_void_p * found.value)()
        cl.clGetDeviceIDs(ctypes.c_void_p(platform), ctypes.c_uint64(4), found.value, devices, None)
        for device in devices:
            chip = (text(info(cl.clGetDeviceInfo, device, 0x102B)) or "").split(":")[0]
            topology = info(cl.clGetDeviceInfo, device, 0x4037, 24)
            unified = number(info(cl.clGetDeviceInfo, device, 0x1035, 4))
            cards.append({"name": text(info(cl.clGetDeviceInfo, device, 0x4038)) or chip, "chip": chip,
                          "memory_mib": (number(info(cl.clGetDeviceInfo, device, 0x101F, 8)) or 0) // 2**20 or None,
                          "integrated": None if unified is None else bool(unified),
                          "bus": topology[21] if topology and len(topology) == 24 and topology[0] == 1 else None})
print(json.dumps({"cards": cards}))
"""


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


def amd_listed(listed=None):
    """The AMD graphics cards Windows lists, as graphics_cards() gives them (its name and driver version); [] when none."""
    return [
        card
        for card in (graphics_cards() if listed is None else listed)
        if card["name"].upper().startswith("AMD ") or "RADEON" in card["name"].upper()
    ]


def amd_cards(*, run=subprocess.run, python=None, listed=None):
    """The AMD graphics cards as [{"name", "chip", "memory_mib", "integrated", "bus", "driver"}], in the order AMD's driver
    lists them: chip is the LLVM name of the card's chip, such as gfx1201 for a Radeon RX 9070 or AI PRO R9700 and gfx1103 for
    the Radeon 780M in a Ryzen processor; memory_mib its memory, for a chip in the processor the share of system memory it can
    use; integrated whether it is built into the processor; bus its PCI bus number, which PyTorch reports as pci_bus_id; and
    driver the version Windows lists for it. AMD's OpenCL driver answers, in a Python of its own (AMD_PROBE). [] when Windows
    lists no AMD card or the driver does not answer; a value it does not give is None."""
    if sys.platform != "win32":
        return []
    windows = amd_listed(listed)
    if not windows:
        return []
    env = {key: value for key, value in os.environ.items() if key.upper() not in VISIBILITY}
    try:
        done = run(
            [python or sys.executable, "-I", "-c", AMD_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=AMD_TIMEOUT,
            env=env,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = None
    for line in reversed((done.stdout or "").splitlines()):
        try:
            found = json.loads(line)
            break
        except ValueError:
            continue
    if done.returncode or not isinstance(found, dict) or not isinstance(found.get("cards"), list):
        return []
    drivers = {card["name"]: card.get("driver") for card in windows}
    cards = []
    for card in found["cards"]:
        if not isinstance(card, dict) or not re.fullmatch(r"gfx[0-9a-f]+", str(card.get("chip") or "")):
            continue
        cards.append(
            {
                "name": str(card.get("name") or card["chip"]),
                "chip": card["chip"],
                "memory_mib": card.get("memory_mib") if isinstance(card.get("memory_mib"), int) else None,
                "integrated": card.get("integrated") if isinstance(card.get("integrated"), bool) else None,
                "bus": card.get("bus") if isinstance(card.get("bus"), int) else None,
                "driver": drivers.get(card.get("name")),
            }
        )
    return cards


def capability_text(capability):
    return f"{capability[0]}.{capability[1]}"


def driver_text(driver):
    return f"{driver[0]}.{driver[1]:02d}"
