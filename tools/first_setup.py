"""The rest of a first setup, once setup.bat has made .venv: the desktop window, then the speech models you choose.

    .venv\\Scripts\\python.exe tools\\first_setup.py
    .venv\\Scripts\\python.exe tools\\first_setup.py --models recommended --yes
    .venv\\Scripts\\python.exe tools\\first_setup.py --models kokoro chatterbox --nvidia
    .venv\\Scripts\\python.exe tools\\first_setup.py --models none --no-desktop

setup.bat runs it with whatever was typed after setup.bat. It installs the desktop window and its shortcut (what
setup-desktop.bat does), then offers the guided model installers: Kokoro and Phonon, which speak in preset voices on any
computer, and Chatterbox Turbo, which clones a voice from a recording and wants more memory. It prints the plan of every model it
is about to install (each installer's own --dry-run), asks once, and then runs those installers one after another with --yes. A
model that is already installed is left alone, and running setup.bat again after an interruption continues where it stopped.
setup-kokoro.bat, setup-phonon.bat and setup-chatterbox.bat stay for adding a model later, and so does the Install button on
Models & features.
"""

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import common  # noqa: E402  (points the model cache at this folder before anything else is imported)
import hardware  # noqa: E402
import engine_checks  # noqa: E402

# What each model is called here, its catalog id and installer, and whether a GPU build of PyTorch exists for it.
MODELS = {
    "kokoro": {"engine": "kokoro", "script": "tools/install_kokoro.py", "nvidia": True},
    "phonon": {"engine": "phonon-31m-en", "script": "tools/install_phonon.py", "nvidia": False},
    "chatterbox": {"engine": "turbo", "script": "tools/install_chatterbox.py", "nvidia": True},
}
CHOICES = {"recommended": ("kokoro", "phonon"), "all": ("kokoro", "phonon", "chatterbox"), "none": ()}
# For the models with an NVIDIA build: the name setup gives them, and their environment and launcher, for switching one to the
# graphics card later.
GPU_MODELS = {
    "kokoro": ("Kokoro", ".venv-kokoro", "setup-kokoro.bat"),
    "chatterbox": ("Chatterbox Turbo", ".venv-chatterbox", "setup-chatterbox.bat"),
}
# Below this much RAM in all, Chatterbox Turbo is unlikely ever to find the free memory it asks for.
CLONING_TOTAL_RAM_GB = 15
DRY_RUN_DONE = "Checks passed."
WIDTH = 110


class SetupStopped(Exception):
    """Setup cannot go on; the message says why and what to do."""


def say(text=""):
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def paragraph(text):
    say(textwrap.fill(text, width=WIDTH, break_on_hyphens=False))


def catalog_entries(root=ROOT):
    engines = json.loads((Path(root) / "catalog" / "engines.json").read_text(encoding="utf-8"))["engines"]
    return {entry["id"]: entry for entry in engines}


def installed(names, root=ROOT, probe=engine_checks.installation):
    """The models among `names` whose environment and files are already in place."""
    entries = catalog_entries(root)
    return [name for name in names if probe(entries[MODELS[name]["engine"]], root)["installed"]]


def model_sizes(root=ROOT):
    """{model: (bytes to download, bytes on disk)} for a first install of each guided model on the CPU, from the numbers its
    installer plans with: its packages, and the model files it fetches by default."""
    from tools import install_chatterbox as ic, install_kokoro as ik, install_phonon as ip

    root = Path(root)
    kokoro = ik.load_manifest(root / ik.MANIFEST)
    kokoro_files = sum(item["size"] for item in ik.checkpoint_files(kokoro, ik.choose_voices(kokoro, ["supported"])))
    phonon_files = sum(item["size"] for repo in ip.load_manifest(root / ip.MANIFEST)["repos"] for item in repo["files"])
    turbo_files = sum(item["size"] for item in ik.load_manifest(root / ic.MANIFEST)["files"])
    return {
        "kokoro": (ik.PACKAGES_DOWNLOAD_MB * 10**6 + kokoro_files, ik.PACKAGES_DISK_MB * 10**6 + kokoro_files),
        "phonon": (ip.PACKAGES_DOWNLOAD_MB * 10**6 + phonon_files, ip.PACKAGES_DISK_MB * 10**6 + phonon_files),
        "chatterbox": (ic.PACKAGES_DOWNLOAD_MB * 10**6 + turbo_files, ic.PACKAGES_DISK_MB * 10**6 + turbo_files),
    }


def sizes_text(names, sizes):
    download = sum(sizes[name][0] for name in names)
    disk = sum(sizes[name][1] for name in names)
    return f"About {download / 1e9:.1f} GB to download and {disk / 1e9:.1f} GB on disk."


def menu_text(total_ram_gb, sizes):
    """The question asked when setup is run without --models."""
    cloning = "Chatterbox Turbo wants 8 GB of free RAM before it loads, so it needs a computer with 16 GB or more."
    if total_ram_gb:
        cloning += f" This computer has {total_ram_gb:.0f} GB" + (
            ", which is probably too little for it." if total_ram_gb < CLONING_TOTAL_RAM_GB else "."
        )
    lines = [
        "Which speech models should setup install now?",
        "  1  Kokoro and Phonon (recommended): speech in 78 preset voices, on any computer.",
        f"     {sizes_text(CHOICES['recommended'], sizes)}",
        "  2  Kokoro, Phonon and Chatterbox Turbo, which also clones a voice from a recording of more than five seconds.",
        f"     {sizes_text(CHOICES['all'], sizes)}",
        "  3  None now. Add them later with the Install buttons on Models & features, or with setup-kokoro.bat,",
        "     setup-phonon.bat and setup-chatterbox.bat.",
        textwrap.fill(cloning, width=WIDTH, break_on_hyphens=False),
    ]
    return "\n".join(lines)


def ask_models(total_ram_gb, *, ask=input):
    say(menu_text(total_ram_gb, model_sizes()))
    while True:
        try:
            answer = ask("Type 1, 2 or 3 and press Enter (Enter alone means 1): ").strip()
        except EOFError:
            answer = "3"
        if answer in ("", "1"):
            return list(CHOICES["recommended"])
        if answer == "2":
            return list(CHOICES["all"])
        if answer == "3":
            return []
        say("That is not one of the choices.")


def parse_models(values):
    """--models takes recommended, all, none, or model names (kokoro, phonon, chatterbox)."""
    chosen = []
    for value in values:
        for name in CHOICES.get(value, (value,)):
            if name not in MODELS:
                raise SetupStopped(
                    f"There is no model called {name!r}. Use recommended, all, none, or the names {', '.join(MODELS)}."
                )
            if name not in chosen:
                chosen.append(name)
    return chosen


def ask_yes(question, *, ask=input):
    try:
        return ask(question).strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def card_problems(name, cards):
    """Why a model's NVIDIA build would not run on these NVIDIA cards, from its own installer's check; [] when it would."""
    from tools import install_chatterbox as ic, install_kokoro as ik

    return {"kokoro": ik, "chatterbox": ic}[name].card_problems(cards)


def gpu_choice(gpu_models, cards):
    """(models whose NVIDIA build runs on these cards, {model: the paragraph that says why another gets its CPU build})."""
    able, refused = [], {}
    for name in gpu_models:
        problems = card_problems(name, cards)
        if not problems:
            able.append(name)
            continue
        label, environment, launcher = GPU_MODELS[name]
        text = " ".join(problems) + f" So setup installs {label}'s CPU build, which runs on the processor."
        if any("needs NVIDIA driver" in problem for problem in problems):
            text += (
                f" To move it to the graphics card later, update the driver from nvidia.com or the NVIDIA app, delete the "
                f"{environment} folder and run {launcher} --nvidia."
            )
        refused[name] = text
    return able, refused


def installer_command(name, *options, python=sys.executable, nvidia=False):
    command = [str(python), str(ROOT / MODELS[name]["script"]), *options]
    if nvidia and MODELS[name]["nvidia"]:
        command.append("--nvidia")
    return command


def show_plan(name, *, nvidia, runner=subprocess.run):
    """Print one installer's plan (its --dry-run). Raises SetupStopped when the installer says this computer cannot take it."""
    done = runner(
        installer_command(name, "--dry-run", nvidia=nvidia),
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = ((done.stdout or "") + (done.stderr or "")).rstrip().splitlines()
    say("\n".join(line for line in lines if not line.startswith(DRY_RUN_DONE)).rstrip())
    say("")
    if done.returncode:
        raise SetupStopped(
            f"The {name} installer cannot run here yet; what it needs is above. Fix that and run setup.bat again: "
            "what is already installed is kept."
        )


def run_installer(name, *, nvidia, runner=subprocess.run):
    say(f"=== Installing {name} ===")
    if runner(installer_command(name, "--yes", nvidia=nvidia), cwd=ROOT).returncode:
        raise SetupStopped(
            f"The {name} installer stopped; its message is above. Run setup.bat again to continue: what is "
            "already installed or downloaded is kept."
        )
    say("")


def set_up_desktop():
    """What setup-desktop.bat does: pywebview in .venv-desktop and the desktop shortcut. A failure is reported, not fatal."""
    say("=== The desktop window ===")
    try:
        from tools import setup_desktop

        setup_desktop.main()
    except (OSError, subprocess.CalledProcessError) as exc:
        say(
            f"The desktop window could not be set up ({exc}). The Lab still runs in a browser with serve.bat; "
            "run setup-desktop.bat to try the window again."
        )
        return False
    say("")
    return True


def run(
    args,
    *,
    ask=input,
    interactive=None,
    runner=subprocess.run,
    probe=engine_checks.installation,
    ram=common.ram_report,
    nvidia_present=hardware.nvidia_driver_present,
    cards=hardware.nvidia_cards,
    desktop=set_up_desktop,
):
    interactive = sys.stdin.isatty() if interactive is None else interactive
    desktop_ready = desktop() if not args.no_desktop else None
    if args.models is not None:
        chosen = parse_models(args.models)
    elif interactive:
        chosen = ask_models(ram().get("total_gb"), ask=ask)
    else:
        chosen = []
        paragraph(
            "No models were chosen: this is not an interactive window, so there is nobody to ask. Add --models "
            "recommended (or all) to install them."
        )
    already = installed(chosen, probe=probe)
    for name in already:
        say(f"{name} is already installed; setup leaves it as it is.")
    todo = [name for name in chosen if name not in already]
    gpu_models = [name for name in todo if MODELS[name]["nvidia"]]
    offer = bool(gpu_models) and not args.nvidia and args.models is None and interactive and nvidia_present()
    gpu = []
    if gpu_models and (args.nvidia or offer):
        # Each model's installer says whether its NVIDIA build runs on this card: Chatterbox's has no code for an RTX 50
        # series card, and Kokoro's needs a 2025 driver. Those models get their CPU builds and the rest can use the card.
        able, refused = gpu_choice(gpu_models, cards())
        if offer and able:
            names = " and ".join(GPU_MODELS[name][0] for name in able)
            paragraph(
                f"This computer has an NVIDIA graphics driver. {names} can run on the graphics card with an NVIDIA build of "
                f"PyTorch instead of the CPU one: a larger download (about 2 GB{' each' if len(able) > 1 else ''}), and the "
                "Lab has not been tried on an NVIDIA card yet, so a report of how it went is welcome."
            )
        for text in refused.values():
            paragraph(text)
        if args.nvidia:
            gpu = able
        elif (
            offer
            and able
            and ask_yes("Install the NVIDIA builds? Type yes for them, or press Enter for the CPU builds: ", ask=ask)
        ):
            gpu = able
    if todo:
        say("")
        for name in todo:
            show_plan(name, nvidia=name in gpu, runner=runner)
        if not args.yes:
            if not interactive:
                raise SetupStopped(
                    "This is not an interactive window, so there is nobody to ask. Nothing was installed. Add "
                    "--yes to accept the plans."
                )
            if not ask_yes(
                f"Install {' and '.join(todo)} now, as planned above? Type yes to install, or press Enter to stop: ", ask=ask
            ):
                say("No model was installed. Install them later from Models & features, or run setup.bat again.")
                todo = []
        for name in todo:
            run_installer(name, nvidia=name in gpu, runner=runner)
    say("Setup is done.")
    if desktop_ready:
        say("Start the Lab with the Fel's Voice Lab icon on your Desktop.")
    else:
        say("Start the Lab with serve.bat, then open the address it prints.")
    if not chosen and not already:
        say("It cannot speak until a model is installed: Models & features has an Install button for each.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--models",
        nargs="+",
        metavar="MODEL",
        help="recommended (Kokoro and Phonon), all (adds Chatterbox Turbo), none, or names: kokoro phonon chatterbox",
    )
    parser.add_argument("--yes", action="store_true", help="install the chosen models without asking")
    parser.add_argument(
        "--nvidia",
        action="store_true",
        help="install the NVIDIA (CUDA) build of PyTorch for Kokoro and Chatterbox, each where it runs on this card; untested "
        "by the Lab",
    )
    parser.add_argument("--no-desktop", action="store_true", help="skip the desktop window and its shortcut")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except SetupStopped as exc:
        say("")
        paragraph(str(exc))
        return 1
    except KeyboardInterrupt:
        say("\nStopped. Run setup.bat again to continue; what is already installed is kept.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
