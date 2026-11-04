"""What this computer has that the Lab asks about, read without loading a model or importing a GPU library: whether an
NVIDIA driver is installed, the processor's name, and the graphics cards Windows lists with their driver versions.

Standard library only, so the installers and setup.bat can use it before any package is installed. Every answer comes from
files and the registry; a value that cannot be read is left out rather than guessed.
"""

import os
import platform
import sys
from pathlib import Path

# The device class Windows files display adapters under.
DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
PROCESSOR_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"


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
