"""Check the files of this repository before they are published.

It fails on a private key or provider token, a personal Windows home folder in a path, a recording, model, database, log or
program file, a file in a folder that holds private runtime state, a file over 2 MB, an image outside docs/ and assets/, and a
Markdown link to a page the repository does not contain. It reads the files Git would publish (tracked, and untracked ones that are
not ignored), or every file under the folder when it is not a Git checkout. It never prints the text it matched.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".html", ".js", ".cjs", ".json", ".md", ".txt", ".bat", ".ps1", ".yml", ".svg", ".toml", ".iss"}
TEXT_NAMES = {"LICENSE", "NOTICE", ".gitignore", ".gitattributes"}
FORBIDDEN_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".mp4", ".safetensors", ".gguf", ".pth", ".pt",
                      ".bin", ".sqlite3", ".db", ".log", ".exe", ".dll", ".zip", ".7z"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}
IMAGE_FOLDERS = ("assets/", "docs/")
# The one kind of audio the repository may hold: a demo the Lab made from a model's preset voices, in docs/demo, beside a
# manifest (the same name, .json) that records its SHA-256 and says no person's recording was used. Anything else is refused.
DEMO_FOLDER = "docs/demo/"
DEMO_SUFFIXES = {".mp3"}
PRIVATE_FOLDERS = {"out", "data", "reference", "hf-cache", "dist", "build"}
LARGEST_BYTES = 2 * 1024 * 1024
RULES = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "provider_token": re.compile(r"\b(?:hf_[a-zA-Z0-9]{25,}|gh[pousr]_[a-zA-Z0-9]{30,}|sk-[a-zA-Z0-9_-]{25,})\b"),
    "windows_user_home": re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!Public(?:[\\/]|$)|<|YOUR)[a-zA-Z0-9_.-]+[\\/]", re.I),
}
# An inline Markdown link, not an image: [text](target) or [text](target "title").
MARKDOWN_LINK = re.compile(r'(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)')


def git_listing(root):
    """The paths Git would publish, or None when `root` is not a Git checkout or Git cannot be run."""
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    done = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                          capture_output=True, timeout=120)
    if done.returncode:
        return None
    return sorted({item.decode("utf-8") for item in done.stdout.split(b"\0") if item})


def file_names(root):
    names = git_listing(root)
    if names is None:
        names = sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                       if path.is_file() and ".git" not in path.relative_to(root).parts)
    # A tracked file that was deleted from the folder is not published; a link that points nowhere still is.
    return [name for name in names if (root / name).is_file() or (root / name).is_symlink()]


def dead_links(root, shipped):
    """Relative links in the Markdown whose target is not in the repository, so the published pages would point at nothing.
    Links inside fenced code blocks are examples, and web or anchor links are skipped."""
    findings = []
    for name in sorted(shipped):
        if not name.endswith(".md"):
            continue
        fenced = False
        for number, line in enumerate((Path(root) / name).read_text("utf-8-sig").splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            for match in MARKDOWN_LINK.finditer(line):
                target = match.group(1)
                if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                    continue
                relative = unquote(target.split("#", 1)[0].split("?", 1)[0])
                if not relative:
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), relative))
                if resolved in shipped or any(item.startswith(resolved.rstrip("/") + "/") for item in shipped):
                    continue
                findings.append({"path": name, "rule": "dead_link", "line": number, "target": target})
    return findings


def declared_demo(root, name):
    """Whether a published file is a synthetic demo that its manifest vouches for: in docs/demo, an MP3 whose manifest names it,
    matches its SHA-256 and states that it is synthetic."""
    path = Path(root) / name
    if not name.startswith(DEMO_FOLDER) or path.suffix.lower() not in DEMO_SUFFIXES:
        return False
    try:
        manifest = json.loads(path.with_suffix(".json").read_text("utf-8"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return False
    return (isinstance(manifest, dict) and manifest.get("file") == path.name and manifest.get("sha256") == digest
            and str(manifest.get("label", "")).startswith("AI-generated speech:") and bool(manifest.get("synthetic")))


def audit(root=ROOT, deny=()):
    """The published file names and what is wrong with them. `deny` lists words that must not appear in any text file;
    a finding says which entry of `deny` matched (its position), never the word."""
    root = Path(root).resolve()
    names = file_names(root)
    findings = []
    for name in names:
        path = root / name
        if path.is_symlink():
            findings.append({"path": name, "rule": "linked_file"})
            continue
        suffix = path.suffix.lower()
        if any(part in PRIVATE_FOLDERS or part.startswith(".venv") for part in Path(name).parts[:-1]):
            findings.append({"path": name, "rule": "private_folder"})
        if declared_demo(root, name):
            pass
        elif (suffix in FORBIDDEN_SUFFIXES or name.endswith(".local.json")
                or path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example")):
            findings.append({"path": name, "rule": "private_or_binary_file"})
        elif suffix in IMAGE_SUFFIXES and not name.startswith(IMAGE_FOLDERS):
            findings.append({"path": name, "rule": "image_outside_docs"})
        if path.stat().st_size > LARGEST_BYTES:
            findings.append({"path": name, "rule": "large_file"})
        if suffix in TEXT_SUFFIXES or name in TEXT_NAMES:
            text = path.read_text("utf-8-sig", errors="replace")
            for rule, pattern in RULES.items():
                for match in pattern.finditer(text):
                    # Never print the matching value into the report.
                    findings.append({"path": name, "rule": rule, "line": text.count("\n", 0, match.start()) + 1})
            lowered = text.lower()
            for position, word in enumerate(deny):
                start = lowered.find(word)
                while start != -1:
                    findings.append({"path": name, "rule": "denied_text", "line": text.count("\n", 0, start) + 1, "entry": position})
                    start = lowered.find(word, start + len(word))
    findings.extend(dead_links(root, set(names)))
    return names, findings


def read_deny_file(path):
    lines = (line.strip().lower() for line in Path(path).read_text("utf-8-sig").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="the folder to check (default: this repository)")
    parser.add_argument("--deny-file", type=Path,
                        help="a text file with one word or phrase per line that must not appear anywhere (a name, an e-mail "
                             "address); keep it outside the repository. Matches are reported by position, never printed.")
    args = parser.parse_args(argv)
    deny = read_deny_file(args.deny_file) if args.deny_file else []
    names, findings = audit(args.root, deny)
    print(json.dumps({"files": len(names), "findings": findings}, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
