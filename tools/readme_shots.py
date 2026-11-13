"""The README's pictures, from the real page and the real service, with Kokoro's preset voices.

    .venv\\Scripts\\python.exe tools\\readme_shots.py                 writes docs\\images\\*.png
    .venv\\Scripts\\python.exe tools\\readme_shots.py --out <folder>

The browser test (tests/test_browser_flow.py) keeps pictures too, but its models are stand-ins that return tones, so its
waveforms, lengths and loudness readings are not a real model's. This starts serve.py on a library of its own in a temporary
folder, gives two characters one of Kokoro 82M's preset voices each (no person's recording is used or cloned), and drives the
page in headless Microsoft Edge or Google Chrome with the browser test's driver: it performs a line, opens the take editor,
looks at the Cast library, imports a short script as a scene, renders it, and exports it. It keeps a picture at each
of those, under the names the README uses.

Needs Kokoro installed, a Chromium-family browser (VOICE_LAB_BROWSER names one) and FFmpeg for the export picture. Close the
Lab first: this starts a service of its own, and two would load models side by side. The temporary library is removed at the
end, and the Lab's own library is not touched.

Exit status: 0 when every picture was taken; 1 when the page did not get there (the message says where); 2 when something it
needs is missing; 3 when the service did not start.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import ffmpeg_tools  # noqa: E402
import test_browser_flow as flow  # noqa: E402  (the DevTools driver the browser test uses)
from tools import lab_cli  # noqa: E402
from tools.real_model_smoke import NO_WINDOW, START_SECONDS, NotStarted, free_port  # noqa: E402

PICTURES = ("perform", "take-editor", "cast-library", "scene", "export")
# Two characters and their Kokoro voices, and the line Perform speaks.
CHARACTERS = (("Mira", "Mira, warm", "af_heart"), ("Tobin", "Tobin, dry", "bm_george"))
LINE = "I kept the porch light on. I hoped you'd find your way back."
# The scene, as a plain-text script (script_import.py reads NAME: line).
SCRIPT = """MIRA: I kept the porch light on. I hoped you'd find your way back.
TOBIN: The road was longer than I said it would be.
MIRA: It always is. Come in, the kettle's still warm.
TOBIN: Then I'll stay until it isn't.
"""
SPEAK_SECONDS = 600  # the first take loads the model


class Problem(Exception):
    """Something the tool needs is missing (exit status 2)."""


class Service:
    """serve.py on a free port and its own library, with its output in folder/service.log. It stops with this process."""

    def __init__(self, library, folder):
        self.library, self.folder = Path(library), Path(folder)
        self.process, self.base = None, None

    def start(self):
        python = ROOT / ".venv" / "Scripts" / "python.exe"
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        command = [str(python if python.is_file() else sys.executable), str(ROOT / "serve.py"), "--port", str(port),
                   "--parent-pid", str(os.getpid()), "--idle-minutes", "0", "--no-cleanup",
                   "--state-root", str(self.library)]  # fmt: skip
        self.log = self.folder / "service.log"
        with self.log.open("wb") as log:
            self.process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                            stderr=subprocess.STDOUT, creationflags=NO_WINDOW)  # fmt: skip
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise NotStarted(f"The service stopped as it started (exit code {self.process.returncode}). See {self.log}.")
            try:
                return lab_cli.get_json(self.base, "/api/health", 5)
            except lab_cli.Failed:
                time.sleep(1)
        raise NotStarted(f"The service did not answer within {START_SECONDS} s. See {self.log}.")

    def call(self, path, body=None, *, timeout=60, raw=False):
        answer = lab_cli.call(self.base, "POST", path, body=body, timeout=timeout)[1]
        return answer if raw else json.loads(answer.decode("utf-8"))

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        try:
            lab_cli.call(self.base, "POST", "/api/unload", timeout=120)
        except lab_cli.Failed:
            pass
        self.process.terminate()
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            self.process.kill()


def cast(service):
    """The two characters, each with an approved Kokoro voice. Returns {character name: identity id}."""
    voices = {}
    for name, label, voice in CHARACTERS:
        character = service.call("/api/characters", {"name": name})["character"]
        identity = service.call(
            f"/api/characters/{character['id']}/identities", {"engine_id": "kokoro", "label": label, "voice": voice}
        )["identity"]
        service.call(f"/api/identities/{identity['id']}/update", {"status": "approved"})
        voices[name] = identity["id"]
    return voices


def settle(page):
    """Wait until every player on screen has read its recording, so a picture shows lengths rather than 0:00. Players that
    the page makes with preload="none" (so a long list does not fetch every clip) are asked to read theirs first."""
    page.evaluate(
        "[...document.querySelectorAll('audio')].filter((a) => a.offsetParent && a.getAttribute('src') && a.readyState < 1)"
        ".forEach((a) => { a.preload = 'metadata'; a.load(); })"
    )
    page.wait_until(
        "[...document.querySelectorAll('audio')].filter((a) => a.offsetParent && a.getAttribute('src'))"
        ".every((a) => a.readyState >= 1)",
        timeout=30,
    )


def picture(page, name, selector, to=None):
    settle(page)
    page.screenshot(name, selector, to=to)


def pictures(page, service, voices):
    """Drive the page to each picture. Each wait names what it waited for when it fails."""
    page.open(service.base + "/#perform")

    # Perform: a take of one line from Mira's voice.
    page.wait_until("[...document.querySelectorAll('#performCharacter option')].some((o) => o.textContent === 'Mira')")
    page.choose("#performCharacter", "Mira")
    page.wait_until("document.getElementById('performIdentity').value !== ''")
    page.set_value("#performText", LINE)
    if page.evaluate("!document.getElementById('variantCount').parentElement.hidden"):
        page.set_value("#variantCount", "2")  # a model that varies from take to take; Kokoro makes one
    page.click("#performGenerate")
    page.wait_until("document.getElementById('performStatus').textContent.startsWith('Takes made.')", timeout=SPEAK_SECONDS)
    page.wait_until("document.querySelectorAll('#takeList audio').length >= 1")
    picture(page, "perform", "#perform", to="#takeList article")

    # The take editor on the first take: trim the ends, fade, a little quieter, and hear it.
    page.evaluate(
        "[...document.querySelectorAll('#takeList article button')].find((b) => b.textContent === 'Edit take…').click()"
    )
    hear = "[...document.querySelectorAll('#takeList .take-edit button')].find((b) => b.textContent === 'Hear the edit' && !b.disabled)"
    page.wait_until("!!" + hear)
    page.wait_until("document.querySelector('#takeList .take-edit-speech').textContent.startsWith('Speech from')")
    # Keep the speech the editor found, with a little room each side, so the picture shows the kept part.
    speech = page.evaluate("document.querySelector('#takeList .take-edit-speech').textContent")
    found = re.search(r"Speech from ([0-9.]+) to ([0-9.]+) s", speech)
    start, end = (max(0.0, float(found.group(1)) - 0.1), float(found.group(2)) + 0.15) if found else (0.0, None)
    edits = [("start_seconds", f"{start:.2f}"), ("fade_in_ms", "20"), ("fade_out_ms", "120"), ("gain_db", "-1.5")]
    if end is not None:
        edits.append(("end_seconds", f"{end:.2f}"))
    for field, value in edits:
        page.set_value(f"#takeList .take-edit [data-edit-field={field}]", value)
    page.evaluate(hear + ".click()")
    page.wait_until("document.querySelector('#takeList .take-edit [role=status]').textContent.startsWith('The edited clip is')")
    picture(page, "take-editor", "#takeList article:has(.take-edit)")

    # The Cast library: each card plays one of its voice's takes (Tobin's made here, as Perform made Mira's).
    service.call(
        "/api/performance",
        {"identity_id": voices["Tobin"], "text": "The road was longer than I said it would be.", "line_id": "readme:tobin"},
        timeout=SPEAK_SECONDS,
        raw=True,
    )
    page.show("cast")
    page.click("#refreshVoices")
    page.wait_until("document.querySelector('#voiceGallery article audio') !== null")
    picture(page, "cast-library", "#cast", to="#voiceGallery")

    # A scene from a short script, rendered line by line, the newest takes chosen, and assembled.
    scene = service.call(
        "/api/scenes/import",
        {
            "format": "text",
            "content": SCRIPT,
            "name": "The porch light",
            "speakers": {name.upper(): {"identity_id": identity} for name, identity in voices.items()},
        },
    )["scene"]
    page.show("scenes")
    page.click("#sceneRefresh")  # the scene was imported behind the page's back
    page.wait_until(
        f"[...document.getElementById('sceneSelect').options].some((o) => o.value === {json.dumps(scene['scene_id'])})"
    )
    page.set_value("#sceneSelect", scene["scene_id"])
    page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line').length === 4")
    page.click("#sceneStart")
    page.wait_until(
        "document.querySelectorAll('#sceneLineEditor .scene-line[data-status=\"choose_take\"]').length === 4",
        timeout=SPEAK_SECONDS,
    )
    page.click("#sceneChooseNewest")
    page.wait_until("document.querySelectorAll('#sceneLineEditor .scene-line[data-status=\"done\"]').length === 4")
    page.click("#scenePreview")
    page.wait_until("document.getElementById('scenePreviewPlayer').getAttribute('src')", timeout=120)
    page.click('button[aria-label="Line 1 details"]')
    picture(page, "scene", "label:has(#sceneName)", to="#scenePreviewPlayer")

    # The export package, written and verified.
    page.evaluate("document.getElementById('sceneExport').open = true")
    page.wait_until("document.getElementById('exportCreate') && !document.getElementById('exportCreate').disabled")
    page.click("#exportCreate")
    page.wait_until("document.getElementById('exportStatus').textContent.startsWith('Package written to')", timeout=180)
    page.wait_until("document.getElementById('exportSelect').value !== ''")
    page.click("#exportVerify")
    page.wait_until("document.getElementById('exportStatus').textContent.startsWith('Verification passed')", timeout=180)
    picture(page, "export", "#sceneExport")
    if page.problems:
        raise AssertionError("The page reported errors: " + "; ".join(map(str, page.problems)))


def run(out):
    if flow.BROWSER is None:
        raise Problem("No Microsoft Edge or Google Chrome was found. Set VOICE_LAB_BROWSER to one.")
    if not (ffmpeg_tools.find_ffmpeg() and ffmpeg_tools.find_ffprobe()):
        raise Problem("FFmpeg was not found: the export picture needs it. Install it from the System page.")
    work = Path(tempfile.mkdtemp(prefix="voice-lab-shots-"))
    service, page = Service(work / "library", work), None
    (work / "library").mkdir()
    try:
        service.start()
        status = {row["id"]: row for row in lab_cli.get_json(service.base, "/api/engines/status", 30).get("engines", [])}
        if status.get("kokoro", {}).get("state") in (None, "not_installed", "not_integrated"):
            raise Problem("Kokoro is not installed. Install it from its card in Models & features first.")
        voices = cast(service)
        flow.SHOTS = Path(out)
        page = flow.DevToolsPage(flow.BROWSER)
        pictures(page, service, voices)
    finally:
        if page is not None:
            page.close()
        service.stop()
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "images", help="where the pictures go")
    args = parser.parse_args(argv)
    try:
        run(args.out)
    except Problem as exc:
        print(exc, file=sys.stderr)
        return 2
    except NotStarted as exc:
        print(exc, file=sys.stderr)
        return 3
    except (AssertionError, RuntimeError) as exc:
        print(f"The page did not get there: {exc}", file=sys.stderr)
        return 1
    print("Pictures in " + str(args.out) + ": " + ", ".join(f"{name}.png" for name in PICTURES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
