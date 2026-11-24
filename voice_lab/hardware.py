"""What this computer has that the Lab asks about, read without loading a model or importing a GPU library: whether an
NVIDIA driver is installed, the processor's name, the graphics cards Windows lists with their driver versions, for NVIDIA
cards the generation (CUDA compute capability) and driver version that decide which NVIDIA build of PyTorch can run on them,
and whether NVIDIA's driver can start CUDA on them at all, and for AMD cards the chip (such as gfx1201) that decides which of
AMD's ROCm code packages they need.

Standard library only, so the installers and setup.bat can use it before any package is installed. Every answer comes from
files, the registry, NVIDIA's own nvidia-smi and CUDA driver, and AMD's own OpenCL driver; a value that cannot be read is left
out rather than guessed.
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
CUDA_TIMEOUT = 30
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
# Asks NVIDIA's CUDA driver itself (nvcuda.dll) whether CUDA can start and, when it can, each card's name, generation (compute
# capability: attributes 75 and 76) and memory, and which CUDA version the driver supports. A laptop's power-saving, Eco or
# iGPU-only mode can switch the NVIDIA card off: Windows then still lists the card and its driver, nvidia-smi says only that it
# has "insufficient permissions", and cuInit says CUDA_ERROR_NO_DEVICE, which is what every model's PyTorch then meets. It runs
# in a Python of its own, so a driver fault cannot stop the program that asks. It prints one JSON line.
CUDA_PROBE = r"""
import ctypes, json
try:
    cuda = ctypes.WinDLL("nvcuda.dll")
except OSError as exc:
    print(json.dumps({"error": f"nvcuda.dll could not be loaded ({exc})", "code": None, "driver_cuda": None, "cards": []}))
    raise SystemExit
for name in ("cuInit", "cuDriverGetVersion", "cuDeviceGetCount", "cuDeviceGet", "cuDeviceGetName", "cuDeviceTotalMem_v2",
             "cuDeviceGetAttribute", "cuGetErrorName", "cuGetErrorString"):
    getattr(cuda, name).restype = ctypes.c_int
version = ctypes.c_int()
driver = None
if cuda.cuDriverGetVersion(ctypes.byref(version)) == 0 and version.value:
    driver = list(divmod(version.value // 10, 100))
code, cards = cuda.cuInit(0), []
if code == 0:
    count = ctypes.c_int()
    code = cuda.cuDeviceGetCount(ctypes.byref(count))
    for ordinal in range(count.value if code == 0 else 0):
        device, major, minor = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        name, memory = ctypes.create_string_buffer(256), ctypes.c_size_t()
        if cuda.cuDeviceGet(ctypes.byref(device), ordinal) or cuda.cuDeviceGetName(name, 256, device):
            continue
        known = cuda.cuDeviceTotalMem_v2(ctypes.byref(memory), device) == 0
        generation = (cuda.cuDeviceGetAttribute(ctypes.byref(major), 75, device) == 0
                      and cuda.cuDeviceGetAttribute(ctypes.byref(minor), 76, device) == 0)
        cards.append({"name": name.value.decode("utf-8", "replace").strip(),
                      "capability": [major.value, minor.value] if generation else None,
                      "memory_mib": memory.value // 2**20 if known and memory.value else None})
    if code == 0 and not cards:
        code = 100
error = None
if code:
    label, words = ctypes.c_char_p(), ctypes.c_char_p()
    cuda.cuGetErrorName(code, ctypes.byref(label))
    cuda.cuGetErrorString(code, ctypes.byref(words))
    label = (label.value or b"").decode("utf-8", "replace") or f"CUDA error {code}"
    words = (words.value or b"").decode("utf-8", "replace")
    error = f"{label}: {words}" if words else label
print(json.dumps({"error": error, "code": code or None, "driver_cuda": driver, "cards": cards}))
"""


def _last_json(text):
    """The last line of a probe's output that is JSON, or None."""
    for line in reversed((text or "").splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


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


def cuda_probe(*, run=subprocess.run, python=None, system_root=None):
    """What NVIDIA's CUDA driver itself says (CUDA_PROBE), as {"error", "code", "driver_cuda", "cards"}: error is None when CUDA
    starts, and otherwise the driver's own name and words for why it cannot, such as "CUDA_ERROR_NO_DEVICE: no CUDA-capable
    device is detected"; code is CUDA's number for it (100 for that one); driver_cuda the newest CUDA version the driver runs,
    as a pair such as (13, 4); and cards [{"name", "capability", "memory_mib"}] as nvidia_cards gives them. The probe runs with
    this program's environment, so a CUDA_VISIBLE_DEVICES that hides every card counts, as it would for a model. None when there
    is no NVIDIA driver to ask, or the probe crashed, hung or printed nothing it could read."""
    if not nvidia_driver_present(system_root):
        return None
    try:
        done = run(
            [python or sys.executable, "-I", "-c", CUDA_PROBE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CUDA_TIMEOUT,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = _last_json(done.stdout)
    if done.returncode or not isinstance(found, dict) or not isinstance(found.get("cards"), list):
        return None

    def pair(value):
        ok = isinstance(value, list) and len(value) == 2 and all(isinstance(part, int) for part in value)
        return tuple(value) if ok else None

    cards = [
        {
            "name": card["name"],
            "capability": pair(card.get("capability")),
            "memory_mib": card.get("memory_mib") if isinstance(card.get("memory_mib"), int) else None,
        }
        for card in found["cards"]
        if isinstance(card, dict) and isinstance(card.get("name"), str) and card["name"]
    ]
    error = found.get("error") if isinstance(found.get("error"), str) and found.get("error") else None
    code = found.get("code") if isinstance(found.get("code"), int) else None
    if error is None and not cards:
        error, code = "CUDA_ERROR_NO_DEVICE: the driver lists no CUDA device", 100
    return {"error": error, "code": code, "driver_cuda": pair(found.get("driver_cuda")), "cards": [] if error else cards}


def nvidia_cards(*, run=subprocess.run, smi=None, listed=None, probe=None):
    """The NVIDIA graphics cards as [{"name", "capability", "driver", "memory_mib"}]. capability is the card's CUDA compute
    capability as a pair, such as (12, 0) for a GeForce RTX 50 series card and (8, 9) for an RTX 40, driver is NVIDIA's driver
    version as a pair, such as (581, 80), and memory_mib its graphics memory (an "8 GB" card has about 8150). nvidia-smi answers
    them when it is there. NVIDIA's CUDA driver is asked too (cuda_probe; probe is its answer, or False not to ask): when
    nvidia-smi does not answer, the driver gives the generation and memory, and the driver version comes from Windows' list.
    When the driver says CUDA cannot start, as when a laptop's NVIDIA card is switched off, every card carries "unavailable",
    the driver's own words, because no model's PyTorch can use it then; Windows keeps listing such a card. With neither answer,
    the names and driver versions Windows lists are used, and the capability and memory are None. [] when this computer has no
    NVIDIA card."""
    smi = nvidia_smi_path() if smi is None else smi
    cards = _smi_cards(smi, run) if smi else None
    cuda = cuda_probe(run=run) if probe is None else probe or None
    windows = [
        {"name": card["name"], "capability": None, "driver": nvidia_driver_version(card.get("driver")), "memory_mib": None}
        for card in (graphics_cards() if listed is None else listed)
        if "nvidia" in card["name"].lower()
    ]
    if cuda and cuda["error"]:
        named = cards or windows or [{"name": "NVIDIA graphics card", "capability": None, "driver": None, "memory_mib": None}]
        return [{**card, "unavailable": cuda["error"]} for card in named]
    if cards is None and cuda:
        drivers = {card["name"]: card["driver"] for card in windows}
        first = windows[0]["driver"] if windows else None
        return [{**card, "driver": drivers.get(card["name"], first)} for card in cuda["cards"]]
    if cards is None:
        return windows
    # An nvidia-smi from before 2022 does not know compute_cap: the driver fills in what it leaves out, card by card.
    answered = {card["name"]: card for card in (cuda or {}).get("cards", [])}
    return [
        {**card, **{key: answered[card["name"]][key] for key in ("capability", "memory_mib") if card[key] is None}}
        if card["name"] in answered
        else card
        for card in cards
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
    found = _last_json(done.stdout)
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
