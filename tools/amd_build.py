"""AMD's ROCm build of PyTorch for the models the Lab runs only on a graphics card (the four Qwen3-TTS models, VoxCPM2 and
Breeze TTS 2), on a computer whose graphics card is an AMD Radeon.

AMD publishes PyTorch 2.12 with ROCm 7.14 for Windows and Python 3.12 on its own index (repo.amd.com/rocm/whl-multi-arch):
torch and torchaudio; the ROCm runtime they load (rocm-sdk-core and rocm-sdk-libraries, with rocm-bootstrap and rocm, two small
Python packages); and the code for each chip in packages of its own, torch's kernels (amd-torch-device-<chip>, plus
amd-torch-device-gfx11 or amd-torch-device-gfx12-0 for the chips of RDNA 3 and 4) and the ROCm libraries'
(rocm-sdk-device-<chip>). requirements/amd-rocm.txt pins every one of these by its SHA-256. For a model, the installer takes the
model's own list (requirements/qwen-nvidia.txt, say), puts AMD's torch and torchaudio in place of its NVIDIA builds, and adds
the runtime and the code for the card's chip, which AMD's OpenCL driver names (hardware.amd_cards). pip installs the wheels
first; rocm, which AMD publishes only as source, is built afterwards with the setuptools that the model's list has just
installed, so pip runs nothing the lists do not pin.

The Lab uses one AMD card. On a computer with more than one (a Ryzen processor with Radeon graphics beside a Radeon card, say)
that is the card HIP_VISIBLE_DEVICES or runtime.local.json's hip_visible_devices names, or else a separate card before one
built into the processor, then the one with the most memory. On Windows, AMD's OpenCL and HIP runtimes number the cards from
the same list in AMD's driver, so before PyTorch is installed a number in the setting is read in OpenCL's order. Once it is
installed, the installer asks PyTorch which number the card has, finding it by its PCI bus, and stops if a setting names
another. With no setting, it writes that number to runtime.local.json as hip_visible_devices, which the Lab's workers read
(common.py) before PyTorch starts.
"""

import hashlib
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

try:
    from tools import install_kokoro as base
except ImportError:  # run as a script, which puts tools\ first on the path and not the lab folder
    import install_kokoro as base

import hardware

InstallError = base.InstallError
ROOT = Path(__file__).resolve().parents[1]
LOCK = "requirements/amd-rocm.txt"
BUILD = "AMD's ROCm 7.14 build of PyTorch 2.12"
# The packages of a model's list that AMD's builds take the place of.
SWAPS = ("torch", "torchaudio")
# Published only as source: pip builds it once the wheels, setuptools among them, are installed.
SOURCE_PACKAGE = "rocm"
WHEELS = "requirements-amd.txt"
SOURCE = "requirements-amd-source.txt"
SETTINGS = "runtime.local.json"
SETTING = "hip_visible_devices"
VARIABLE = "HIP_VISIBLE_DEVICES"
# torch's kernels for a family of chips, which each chip of it needs beside its own: RDNA 3 and 3.5 (gfx11..) and RDNA 4
# (gfx120.).
FAMILIES = (("gfx11", "amd-torch-device-gfx11"), ("gfx120", "amd-torch-device-gfx12-0"))
_DEVICE = re.compile(r"(?:amd-torch-device|rocm-sdk-device)-")
_CHIP = re.compile(r"amd-torch-device-(gfx[0-9a-f]{4})")
# Measured from AMD's wheels: the shared ones (torch, torchaudio and the ROCm runtime) are 943 MB to download and take 3.24 GB
# unpacked. A chip's own packages add at most 672 MB and 0.91 GB for RDNA 3 and 3.5 (gfx1100, with the gfx11 family's
# kernels), 517 MB and 0.75 GB for RDNA 4 (455 MB and 0.69 GB for the R9700's gfx1201), and 94 MB and 0.12 GB for RDNA 1 and 2.
# Installed for the R9700 they took 4.17 GB, the bytecode pip compiles included, so the shared ones are counted at 3.5 GB.
SHARED_DOWNLOAD_MB = 950
SHARED_DISK_MB = 3500
CHIP_SIZES_MB = (("gfx11", 680, 920), ("gfx120", 520, 760), ("gfx10", 100, 120))
CHECK_TIMEOUT = 300
CARDS_TEXT = (
    "the Radeon RX 5000, 6000, 7000 and 9000 series, the Radeon PRO and AI PRO cards of the same chips, and the Radeon "
    "graphics in Ryzen processors"
)
# Lists the cards PyTorch sees, as it numbers them, with each one's PCI bus. It runs in the model's environment once the packages
# are in, with no setting that hides a card.
HIP_PROBE = """
import json, torch
devices = []
for index in range(torch.cuda.device_count()):
    properties = torch.cuda.get_device_properties(index)
    devices.append({"index": index, "name": properties.name, "chip": properties.gcnArchName.split(":")[0],
                    "bus": getattr(properties, "pci_bus_id", None), "memory_mib": properties.total_memory // 2**20})
print(json.dumps({"torch": torch.__version__, "hip": torch.version.hip, "devices": devices}))
"""


def lock_packages(root=ROOT):
    """The packages requirements/amd-rocm.txt pins, every chip's included."""
    return base.read_lock(Path(root) / LOCK)


def chips(root=ROOT, packages=None):
    """The chips the list has code for, such as gfx1201: those with both their torch and their ROCm library packages."""
    keys = {item["key"] for item in (lock_packages(root) if packages is None else packages)}
    found = [match[1] for key in keys if (match := _CHIP.fullmatch(key)) and f"rocm-sdk-device-{match[1]}" in keys]
    return sorted(found)


def chip_packages(chip):
    """The packages that hold the code for one chip: torch's kernels for its family, if it has one, then its own."""
    family = [name for prefix, name in FAMILIES if chip.startswith(prefix)]
    return [*family, f"amd-torch-device-{chip}", f"rocm-sdk-device-{chip}"]


def chip_sizes_mb(chip):
    """(download, disk) in MB for the packages of one chip, rounded up from AMD's wheels."""
    return next(((download, disk) for prefix, download, disk in CHIP_SIZES_MB if chip.startswith(prefix)), (100, 120))


def is_device_package(key):
    return bool(_DEVICE.match(key))


def pinned_packages(root, model_lock, chip):
    """A model's packages for a card with this chip: its own list with AMD's torch and torchaudio in place of the NVIDIA builds,
    then the ROCm runtime and the chip's code."""
    model = base.read_lock(Path(root) / model_lock)
    wanted = set(chip_packages(chip))
    amd = [item for item in lock_packages(root) if not is_device_package(item["key"]) or item["key"] in wanted]
    builds = {item["key"]: item for item in amd if item["key"] in SWAPS}
    packages = [builds.get(item["key"], item) for item in model]
    present = {item["key"] for item in packages}
    return packages + [item for item in amd if item["key"] not in present and item["key"] not in SWAPS]


def write_requirements(root, model_lock, chip, folder):
    """Write the two lists pip installs for an AMD card into folder, and return their paths: requirements-amd.txt, every wheel
    (the model's list with AMD's torch and torchaudio, the ROCm runtime and the chip's code), and requirements-amd-source.txt,
    the one package AMD publishes only as source."""
    lines = base.lock_lines(Path(root) / model_lock)
    amd = base.lock_lines(Path(root) / LOCK)
    wanted = set(chip_packages(chip))
    for key in SWAPS:
        if key in lines:
            lines[key] = amd[key]
    for key, line in amd.items():
        if key not in SWAPS and key != SOURCE_PACKAGE and (not is_device_package(key) or key in wanted):
            lines.setdefault(key, line)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    wheels, source = folder / WHEELS, folder / SOURCE
    wheels.write_text(
        f"# {model_lock} with {BUILD} from {LOCK}, for the {chip} chip, written by the installer.\n"
        + "\n".join(line for key, line in lines.items() if key != SOURCE_PACKAGE)
        + "\n",
        encoding="utf-8",
    )
    source.write_text(
        f"# The package from {LOCK} that AMD publishes only as source, written by the installer.\n{amd[SOURCE_PACKAGE]}\n",
        encoding="utf-8",
    )
    return wheels, source


def build_from_source(root, environment, requirements, *, runner=subprocess.run):
    """pip install the package AMD publishes only as source, checked against its SHA-256 and built with the setuptools already
    installed in the environment (from the model's list), so pip fetches nothing to build it with."""
    command = [
        str(base.env_python(root, environment)),
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "--no-deps",
        "--require-hashes",
        "--no-build-isolation",
        f"--no-binary={SOURCE_PACKAGE}",
        "-r",
        str(requirements),
    ]
    code = runner(command, cwd=root, env={**os.environ, "PYTHONUTF8": "1"}).returncode
    if code:
        raise InstallError(
            f"pip stopped with exit code {code} while it built {SOURCE_PACKAGE}, the part of AMD's ROCm runtime that AMD "
            "publishes as source. Read its message above, fix the cause, and run the installer again: it continues where it "
            "stopped."
        )


def install_packages(root, environment, model_lock, chip, folder, *, runner=subprocess.run):
    """Install a model's packages for an AMD card into its environment: the wheels, then the source package."""
    wheels, source = write_requirements(root, model_lock, chip, folder)
    base.pip_install(root, environment, wheels, runner=runner)
    build_from_source(root, environment, source, runner=runner)


def visible_setting(root=ROOT, environ=None):
    """(where, number) for a setting that names the card the Lab uses: HIP_VISIBLE_DEVICES, or runtime.local.json's
    hip_visible_devices, and the first card number it gives. (None, None) when neither is set, and (where, None) when the
    setting is not a number."""
    environ = os.environ if environ is None else environ
    value, where = environ.get(VARIABLE), VARIABLE
    if value is None or not str(value).strip():
        where = SETTINGS
        try:
            settings = json.loads((Path(root) / SETTINGS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            settings = {}
        value = settings.get(SETTING) if isinstance(settings, dict) else None
        if value is None or not str(value).strip():
            return None, None
    first = str(value).split(",")[0].strip()
    return where, int(first) if first.isdigit() else None


def choose_card(cards, chip_list, *, visible=None):
    """The AMD card the Lab is to use: the one a setting names (visible, its number in the order AMD's driver lists the cards),
    or else of the cards whose chip the build has code for, a separate card before one built into the processor, then the one
    with the most memory. None when there is no such card."""
    if visible is not None:
        return cards[visible] if 0 <= visible < len(cards) else None
    usable = [(position, card) for position, card in enumerate(cards) if card["chip"] in chip_list]
    if not usable:
        return None
    return min(usable, key=lambda pair: (pair[1].get("integrated") is not False, -(pair[1].get("memory_mib") or 0), pair[0]))[1]


def card_text(card):
    """The card as the plan names it, such as "AMD Radeon AI PRO R9700 (gfx1201)"."""
    built_in = ", built into the processor" if card.get("integrated") else ""
    return f"{card['name']} ({card['chip']}{built_in})"


def card_problems(cards, *, model, root=ROOT, memory_gb=None, environ=None):
    """Why model cannot run on this computer's AMD cards with AMD's build, as sentences: a setting names a card that is not
    there, no card has a chip the build has code for, or the card it would use has less memory than memory_gb GB. An empty
    list when it can. The Models page asks this too before it offers Install."""
    chip_list = chips(root)
    if not cards:
        return [
            f"Windows lists an AMD graphics card, but AMD's driver did not say which chip it has, so the Lab cannot tell "
            f"whether {BUILD} has code for it. Update or reinstall the AMD graphics driver (AMD Software: Adrenalin Edition, "
            "or PRO Edition for a workstation card) and run the installer again."
        ]
    where, visible = visible_setting(root, environ)
    if where and visible is None:
        return [
            f"{where} does not give a card number, so the Lab cannot tell which AMD card to use. Set it to a number, or "
            "remove it, and run the installer again."
        ]
    card = choose_card(cards, chip_list, visible=visible)
    if visible is not None and card is None:
        names = ", ".join(f"{position} {item['name']}" for position, item in enumerate(cards)) or "none"
        return [
            f"{where} names card {visible}, and AMD's driver lists these: {names}. Correct it or remove it, and run the "
            "installer again."
        ]
    if card is None or card["chip"] not in chip_list:
        shown = ", ".join(card_text(item) for item in ([card] if card else cards))
        return [f"{BUILD} has no code for the {shown}. It has code for {CARDS_TEXT}: {', '.join(chip_list)}."]
    problems = []
    if memory_gb and card.get("memory_mib") and card["memory_mib"] < memory_gb * 1024:
        problems.append(
            f"{model} needs a graphics card with at least {memory_gb} GB of memory, and the {card['name']} has "
            f"{card['memory_mib'] / 1024:.1f} GB."
        )
    return problems


def no_card_text(model, problems):
    """What an installer says on a computer whose AMD cards AMD's build cannot run on: there is no build to plan for them, so
    only the reasons, from card_problems."""
    return f"This computer cannot take {model} yet:\n" + "\n".join(
        textwrap.fill(problem, width=110, break_on_hyphens=False, initial_indent="  - ", subsequent_indent="    ")
        for problem in problems
    )


def chosen_card(cards, root=ROOT, environ=None):
    """The card the installer installs for, as card_problems judges it: None when card_problems finds no card to use."""
    chip_list = chips(root)
    card = choose_card(cards, chip_list, visible=visible_setting(root, environ)[1])
    return card if card is not None and card["chip"] in chip_list else None


def usable_card(root=ROOT, cards=None):
    """Whether this computer has an AMD card the Lab can use through AMD's build: the one chosen_card picks. The service asks
    this when it starts, to run the models on the graphics card."""
    if cards is None:
        if not hardware.amd_listed():
            return False
        cards = hardware.amd_cards()
    return chosen_card(cards, root) is not None


def graphics(*, nvidia_present=None, amd_cards=None):
    """Which build an installer for a model that runs only on a graphics card puts in: ("nvidia", []) on a computer with an
    NVIDIA driver, ("amd", cards) on one whose graphics cards Windows lists as AMD's, with the cards AMD's driver gives
    (hardware.amd_cards, which can be []), and (None, []) on one with neither."""
    if nvidia_present if nvidia_present is not None else base.nvidia_driver_present():
        return "nvidia", []
    if amd_cards is None:
        if not hardware.amd_listed():
            return None, []
        amd_cards = hardware.amd_cards()
    return "amd", list(amd_cards)


def sizes_mb(chip, other_download_mb, other_disk_mb):
    """(download, disk) in MB for a model's packages with AMD's build: its other packages, the shared ones, and the chip's."""
    download, disk = chip_sizes_mb(chip)
    return other_download_mb + SHARED_DOWNLOAD_MB + download, other_disk_mb + SHARED_DISK_MB + disk


def plan_note(card, cards):
    """The plan's paragraph about AMD's build: what it needs, that it is AMD's, how far it has been tried, and which card the
    Lab uses on a computer with more than one."""
    note = (
        f"{BUILD} needs a recent AMD graphics driver (AMD Software: Adrenalin Edition, or PRO Edition for a workstation "
        "card). Its packages carry AMD's ROCm runtime and libraries, under AMD's own licence terms. The Lab was checked with "
        "it on an AMD Radeon AI PRO R9700 (gfx1201); on other chips it has not been tried yet: if it works, or does not, a "
        "hardware report on GitHub helps the next person."
    )
    if len(cards) > 1:
        note += (
            f" This computer has {len(cards)} AMD graphics cards ({', '.join(item['name'] for item in cards)}), and the Lab "
            f"uses the {card['name']}: once PyTorch is in, the installer notes the number PyTorch gives it in {SETTINGS} "
            f'("{SETTING}"), unless {VARIABLE} or that file already names a card.'
        )
    return note


def hip_devices(root, environment, *, runner=subprocess.run):
    """The graphics cards PyTorch in the environment lists, numbered as it numbers them, each with its PCI bus."""
    env = {key: value for key, value in os.environ.items() if key.upper() not in hardware.VISIBILITY}
    try:
        done = runner(
            [str(base.env_python(root, environment)), "-I", "-c", HIP_PROBE],
            cwd=root,
            env={**env, "PYTHONUTF8": "1"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"PyTorch did not list the graphics cards within {CHECK_TIMEOUT // 60} minutes.") from exc
    found = base.last_json_line(done.stdout or "")
    if done.returncode or not isinstance(found, dict) or not isinstance(found.get("devices"), list):
        output = (done.stdout or "") + (done.stderr or "")
        raise InstallError(
            f"The packages are in {environment}, but AMD's PyTorch could not list the graphics cards:\n"
            + base.check_output(output)
            + failure_hint(output)
        )
    return found["devices"]


def hip_number(devices, card):
    """The number PyTorch gives the card: the one on the card's PCI bus, or else the first with its chip and name."""
    if card.get("bus") is not None:
        on_bus = [device for device in devices if device.get("bus") == card["bus"] and device.get("chip") == card["chip"]]
        if len(on_bus) == 1:
            return on_bus[0]["index"]
    same = [device for device in devices if device.get("chip") == card["chip"] and device.get("name") == card["name"]]
    return same[0]["index"] if same else None


def remember_number(root, number):
    """Write hip_visible_devices into runtime.local.json, keeping everything else the file holds."""
    path = Path(root) / SETTINGS
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise InstallError(
            f"{SETTINGS} cannot be read ({exc}), so the Lab cannot note which AMD card to use. Correct the file "
            "or delete it, and run the installer again."
        ) from exc
    if not isinstance(settings, dict):
        raise InstallError(
            f"{SETTINGS} does not hold a JSON object. Correct the file or delete it, and run the installer again."
        )
    settings[SETTING] = str(number)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def settle_number(root, devices, card, *, environ=None):
    """The number PyTorch gives the card the packages were installed for, which the speech check and the Lab's workers use, and a
    sentence for the installer's output (None when there is nothing to say). With more than one card and no setting that names
    one, the number is written to runtime.local.json; a setting that names another card stops the installer."""
    number = hip_number(devices, card)
    if number is None:
        listed = ", ".join(f"{device['index']} {device['name']} ({device['chip']})" for device in devices) or "none"
        raise InstallError(
            f"AMD's PyTorch does not list the {card_text(card)}, which the packages were installed for. It "
            f"lists: {listed}. Update the AMD graphics driver (AMD Software: Adrenalin Edition, or PRO Edition "
            "for a workstation card) and run the installer again."
        )
    where, visible = visible_setting(root, environ)
    if visible is not None:
        if visible != number:
            named = next((device["name"] for device in devices if device.get("index") == visible), None)
            which = f"the {named} in PyTorch's numbering" if named else "a card PyTorch does not list"
            raise InstallError(
                f"{where} names card {visible}, {which}, and the packages were installed for the {card['name']}, "
                f"card {number} in that numbering. Set it to {number}, or remove it, and run the installer again."
            )
        return number, None
    if len(devices) > 1:
        remember_number(root, number)
        return number, (
            f"This computer has {len(devices)} AMD graphics cards, and the Lab uses the {card['name']}: "
            f'{SETTINGS} now says "{SETTING}": "{number}".'
        )
    return number, None


def receipt_fields(root, card, number):
    """What an installation record adds about the build: "cuda" for NVIDIA's, and for AMD's the list's SHA-256 and the card,
    its chip, its driver and the number PyTorch gives it."""
    if not card:
        return {"build": "cuda"}
    return {
        "build": "rocm",
        "amd_lock_sha256": hashlib.sha256((Path(root) / LOCK).read_bytes()).hexdigest(),
        "gpu": {"name": card["name"], "chip": card["chip"], "driver": card.get("driver"), "hip_visible_devices": number},
    }


def failure_hint(output):
    """A sentence for the most likely cause of a failure of AMD's PyTorch on the card, from what it printed."""
    lowered = output.lower()
    if "out of memory" in lowered:
        return (
            "\nThe graphics card ran out of memory. Close other programs that use it (games, or another model loaded in the "
            "Lab) and run the installer again."
        )
    if re.search(r"\bhip|rocm|no kernel image|invalid device function", lowered):
        return (
            "\nPyTorch could not use the AMD graphics card. Update the graphics driver (AMD Software: Adrenalin Edition, or "
            "PRO Edition for a workstation card) and run the installer again."
        )
    return ""
