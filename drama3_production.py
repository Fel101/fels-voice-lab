"""Shared Drama studio contracts and retained jobs; no model loads at import time."""

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import threading
import time
import uuid
import wave
import zipfile

MAX_TEXT = 8000
MAX_AUDIO_SECONDS = 600
CUES = ["whispers", "laughs", "sighs", "gasps", "coughs", "speaks softly", "excited", "sad"]
LANGUAGES = dict(
    zip(
        "af am ar as az be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu he hi hr ht hu hy id is it ja jv ka kk km kn ko la lt lv mi ml mn mr ms my ne nl nn no pa pl ps pt ro ru sa sd si sk sl sn sq sr sv sw ta te th tl tr uk ur vi yi yo zh".split(),
        [
            "Afrikaans",
            "Amharic",
            "Arabic",
            "Assamese",
            "Azerbaijani",
            "Belarusian",
            "Bulgarian",
            "Bengali",
            "Tibetan",
            "Breton",
            "Bosnian",
            "Catalan",
            "Czech",
            "Welsh",
            "Danish",
            "German",
            "Greek",
            "English",
            "Spanish",
            "Estonian",
            "Basque",
            "Persian",
            "Finnish",
            "Faroese",
            "French",
            "Galician",
            "Gujarati",
            "Hebrew",
            "Hindi",
            "Croatian",
            "Haitian Creole",
            "Hungarian",
            "Armenian",
            "Indonesian",
            "Icelandic",
            "Italian",
            "Japanese",
            "Javanese",
            "Georgian",
            "Kazakh",
            "Khmer",
            "Kannada",
            "Korean",
            "Latin",
            "Lithuanian",
            "Latvian",
            "Maori",
            "Malayalam",
            "Mongolian",
            "Marathi",
            "Malay",
            "Burmese",
            "Nepali",
            "Dutch",
            "Norwegian Nynorsk",
            "Norwegian",
            "Punjabi",
            "Polish",
            "Pashto",
            "Portuguese",
            "Romanian",
            "Russian",
            "Sanskrit",
            "Sindhi",
            "Sinhala",
            "Slovak",
            "Slovenian",
            "Shona",
            "Albanian",
            "Serbian",
            "Swedish",
            "Swahili",
            "Tamil",
            "Telugu",
            "Thai",
            "Filipino",
            "Turkish",
            "Ukrainian",
            "Urdu",
            "Vietnamese",
            "Yiddish",
            "Yoruba",
            "Chinese",
        ],
    )
)
TOKEN = re.compile(r"<\|speaker:(\d+)\|>|\[([^\[\]\r\n]{1,160})\]")
ID = re.compile(r"^[a-f0-9]{32}$")


def script(text):
    """Validate model-only markup, and separate it from captions/word checks."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"Use a nonempty script up to {MAX_TEXT} characters.")
    rest = TOKEN.sub("", text)
    if any(c in rest for c in "[]<>"):
        raise ValueError("Use balanced [plain delivery cues] and <|speaker:0|> through <|speaker:15|> only.")
    if any(int(m.group(1)) > 15 or str(int(m.group(1))) != m.group(1) for m in TOKEN.finditer(text) if m.group(1) is not None):
        raise ValueError("Speaker indices must be 0–15.")
    plain = " ".join(TOKEN.sub(" ", text).split())
    if not plain and not any(m.group(2) in ("laughs", "sighs", "gasps", "coughs") for m in TOKEN.finditer(text)):
        raise ValueError("Add spoken words or a vocal event to the script.")
    speakers = set()
    current = 0
    cursor = 0
    for match in TOKEN.finditer(text):
        if TOKEN.sub("", text[cursor : match.start()]).strip():
            speakers.add(current)
        if match.group(1) is not None:
            current = int(match.group(1))
        elif match.group(2) in ("laughs", "sighs", "gasps", "coughs"):
            speakers.add(current)
        cursor = match.end()
    if text[cursor:].strip():
        speakers.add(current)
    return {
        "text": text.strip(),
        "plain_text": plain,
        "speakers": sorted(speakers),
        "cues": [m.group(2) for m in TOKEN.finditer(text) if m.group(2)],
    }


def protected_transform(text, transform):
    parts = []
    cursor = 0
    for match in TOKEN.finditer(text):
        parts.extend((transform(text[cursor : match.start()]), match.group()))
        cursor = match.end()
    return "".join(parts) + transform(text[cursor:])


def capabilities():
    return {
        "schema": 1,
        "languages": [{"code": "auto", "label": "Auto"}, *[{"code": k, "label": v} for k, v in LANGUAGES.items()]],
        "cues": CUES,
        "max_text": MAX_TEXT,
        "max_speakers": 16,
        "max_audio_seconds": MAX_AUDIO_SECONDS,
        "streaming": True,
        "cloning_device": "cpu",
        "studio_device": "cpu",
        "editing": {
            "supported": False,
            "reason": "The pinned speech.cpp worker has no previous-take VQ context input. See docs/DRAMA3_EXPANSION.md.",
        },
    }


class Cancelled(RuntimeError):
    pass


class Studio:
    """One retained production job at a time. Runner owns normal app residency gates."""

    def __init__(self, root, sources, resolve, runner, prepare=None, authorize=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.sources = sources
        self.resolve = resolve
        self.runner = runner
        self.prepare = prepare
        self.authorize = authorize or (lambda row: None)
        from local_ownership import LocalOwnership

        self.ownership = LocalOwnership(self.root / "runner.lock")
        self.lock = threading.RLock()
        self.active = None
        self.events = {}
        self.closed = False
        # A process restart cannot leave an old job looking live.
        if self.ownership.acquire():
            try:
                for p in self.root.glob("*/job.json"):
                    data = json.loads(p.read_text("utf-8"))
                    if data["status"] in ("queued", "running", "cancelling"):
                        data.update(
                            status="interrupted", error="The application stopped; start a new take. Original files retained."
                        )
                        self._save(p.parent, data)
            finally:
                self.ownership.release()

    def _directory(self, job_id):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise ValueError("Invalid Drama job id.")
        path = self.root / job_id
        if not (path / "job.json").is_file():
            raise KeyError("Drama job not found.")
        return path

    def _save(self, directory, data):
        temp = directory / ("job-" + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(directory / "job.json")

    def list(self):
        with self.lock:
            result = []
            for p in sorted(self.root.glob("*/job.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                row = json.loads(p.read_text("utf-8"))
                try:
                    self.authorize(row)
                except Exception as exc:
                    if isinstance(exc, PermissionError) or getattr(exc, "status", None) in (403, 404):
                        continue
                    raise
                result.append(row)
                if len(result) >= 20:
                    break
            return result

    def start(self, body):
        from drama3_runtime import controls

        if not isinstance(body, dict) or set(body) - {
            "text",
            "cast",
            "settings",
            "language",
            "allow_unassigned",
            "description",
            "direction",
        }:
            raise ValueError("Unknown Drama studio field.")
        parsed = script(body.get("text"))
        language = body.get("language", "auto")
        if language not in ("auto", *LANGUAGES):
            raise ValueError("Choose a language listed by the installed Drama model.")
        slots = body.get("cast", [])
        if not isinstance(slots, list) or len(slots) > 16:
            raise ValueError("Use at most 16 cast slots.")
        if not isinstance(body.get("allow_unassigned", False), bool):
            raise ValueError("allow_unassigned must be true or false.")
        references = []
        seen = set()
        for slot in slots:
            if not isinstance(slot, dict) or set(slot) != {"speaker", "source_id"}:
                raise ValueError("A cast slot needs speaker and source_id.")
            speaker = slot["speaker"]
            if isinstance(speaker, bool) or not isinstance(speaker, int) or not 0 <= speaker <= 15 or speaker in seen:
                raise ValueError("Cast slots must have unique speaker indices 0–15.")
            seen.add(speaker)
            ref = dict(self.resolve(slot["source_id"]))
            ref["speaker"] = speaker
            if not isinstance(ref.get("text"), str) or not ref["text"].strip():
                raise ValueError("Every cast reference needs its exact transcript.")
            path = Path(ref["path"])
            try:
                with wave.open(str(path), "rb") as wav:
                    duration = wav.getnframes() / wav.getframerate()
            except (wave.Error, EOFError) as exc:
                raise ValueError("Use a saved PCM WAV reference in the Drama studio.") from exc
            if not 0.5 <= duration <= 60:
                raise ValueError("Each cast reference must last 0.5–60 seconds.")
            ref["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            references.append(ref)
        if references and set(parsed["speakers"]) - seen and not body.get("allow_unassigned", False):
            raise ValueError("Assign every speaking slot, or explicitly allow model-selected unassigned voices.")
        for key in ("description", "direction"):
            value = body.get(key, "")
            if not isinstance(value, str) or len(value) > 1000 or any(c in value for c in "[]<>\r\n"):
                raise ValueError("Global voice/delivery descriptions must be plain text up to 1000 characters.")
        pinned_cast = [
            {"speaker": ref["speaker"], "source_id": ref.get("source_id", slot["source_id"])}
            for ref, slot in zip(references, slots)
        ]
        clean = {
            **body,
            "text": parsed["text"],
            "cast": pinned_cast,
            "settings": controls(body.get("settings")),
            "language": language,
        }
        if len(json.dumps({"request": clean, "references": references}, ensure_ascii=False).encode("utf-8")) > 55000:
            raise ValueError("The script and reference transcripts together are too large; shorten them or reduce the cast.")
        if self.prepare:
            clean["_runtime_binding"] = self.prepare(clean, references)
        with self.lock:
            if self.closed:
                raise RuntimeError("Drama studio is shutting down.")
            if self.active:
                raise RuntimeError("A Drama studio take is already running; stop it or wait.")
            if not self.ownership.acquire():
                raise RuntimeError("Another application instance owns this Drama studio job; wait or stop it there.")
            job_id = uuid.uuid4().hex
            try:
                directory = self.root / job_id
                directory.mkdir()
                row = {
                    "id": job_id,
                    "status": "queued",
                    "created": time.time(),
                    "request": clean,
                    "script": parsed,
                    "references": references,
                    "human_review": "not performed",
                }
                self._save(directory, row)
                event = threading.Event()
                self.events[job_id] = event
                self.active = job_id
                thread = threading.Thread(target=self._run, args=(directory, row, references, event), daemon=True)
                self.thread = thread
                thread.start()
            except BaseException:
                self.active = None
                self.events.pop(job_id, None)
                self.ownership.release()
                raise
            return dict(row)

    def _run(self, directory, row, references, cancel):
        started = time.monotonic()
        count = 0
        first = None
        finished = threading.Event()

        def watch_cancel():
            while not finished.wait(0.1):
                if (directory / "cancel.request").exists():
                    cancel.set()
                    return

        monitor = threading.Thread(target=watch_cancel, daemon=True)
        monitor.start()
        try:
            with self.lock:
                row["status"] = "running"
                self._save(directory, row)
            with (directory / "preview.pcm").open("wb") as stream:

                def emit(pcm, rate):
                    nonlocal count, first
                    if cancel.is_set():
                        raise Cancelled("Take stopped.")
                    if rate != 44100 or len(pcm) % 2:
                        raise RuntimeError("Invalid native preview format.")
                    count += len(pcm) // 2
                    if count > 44100 * MAX_AUDIO_SECONDS:
                        raise RuntimeError("Take exceeded the ten-minute studio audio limit; shorten the script.")
                    if first is None:
                        first = round(time.monotonic() - started, 3)
                    stream.write(pcm)
                    stream.flush()

                info = self.runner(row["request"], references, directory, emit, cancel)
            if cancel.is_set():
                raise Cancelled("Take stopped.")
            for ref in references:
                if hashlib.sha256(Path(ref["path"]).read_bytes()).hexdigest() != ref["sha256"]:
                    raise RuntimeError("A source reference changed during generation; result was not accepted.")
            path = directory / "take.wav"
            with wave.open(str(path), "rb") as wav:
                if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                    raise RuntimeError("Completed audio must be mono PCM16 WAV.")
                rate = wav.getframerate()
                raw = wav.readframes(wav.getnframes())
            import numpy as np

            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            if rate != 44100 or len(audio) != count or not count:
                raise RuntimeError("Completed WAV does not match streamed audio.")
            flags = []
            duration = count / rate
            if not np.isfinite(audio).all():
                raise RuntimeError("Completed audio contains invalid samples.")
            if float(np.max(np.abs(audio))) < 0.001:
                flags.append("silent_or_very_quiet")
            if np.mean(np.abs(audio) >= 0.999) > 0.001:
                flags.append("possible_clipping")
            if row["script"]["cues"] and duration > max(12, len(row["script"]["plain_text"].split()) * 2):
                flags.append("extended_event_or_pause")
            row.update(
                status="complete",
                model=info,
                first_audio_seconds=first,
                wall_seconds=round(time.monotonic() - started, 3),
                audio_seconds=duration,
                audio_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                quality={
                    "flags": flags,
                    "word_accuracy": "not checked",
                    "speaker_identity": "requires listening",
                    "cue_adherence": "requires listening",
                },
            )
            # Native cast audio is a mixed take. Do not invent speaker stems or word timings.
            (directory / "transcript.txt").write_text(row["script"]["plain_text"], encoding="utf-8")
        except Exception as exc:
            user_cancel = (directory / "cancel.request").exists() or self.closed
            row.update(status="cancelled" if user_cancel else "failed", error=str(exc))
        finally:
            finished.set()
            monitor.join(1)
            with self.lock:
                if cancel.is_set() and row["status"] == "complete":
                    row.update(status="cancelled", error="Take stopped before final acceptance.")
                try:
                    self._save(directory, row)
                finally:
                    self.active = None
                    self.events.pop(row["id"], None)
                    self.ownership.release()

    def get(self, job_id, offset=0):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset % 2:
            raise ValueError("Audio offset must be a nonnegative even byte count.")
        with self.lock:
            directory = self._directory(job_id)
            row = json.loads((directory / "job.json").read_text("utf-8"))
            self.authorize(row)
            if row["status"] in ("queued", "running") and (directory / "cancel.request").exists():
                row["status"] = "cancelling"
        path = directory / "preview.pcm"
        size = path.stat().st_size if path.exists() else 0
        if offset > size:
            raise ValueError("Audio offset exceeds available preview.")
        pcm = b""
        if size > offset:
            with path.open("rb") as stream:
                stream.seek(offset)
                pcm = stream.read(176400)
        return {
            "job": row,
            "pcm": base64.b64encode(pcm).decode(),
            "offset": offset + len(pcm),
            "available_bytes": size,
            "sample_rate": 44100,
        }

    def cancel(self, job_id):
        with self.lock:
            directory = self._directory(job_id)
            row = json.loads((directory / "job.json").read_text("utf-8"))
            self.authorize(row)
            if row["status"] in ("queued", "running", "cancelling"):
                (directory / "cancel.request").touch()
                if job_id in self.events:
                    self.events[job_id].set()
                row["status"] = "cancelling"
            return row

    def artifact(self, job_id, kind):
        directory = self._directory(job_id)
        row = self.get(job_id)["job"]
        if row["status"] != "complete":
            raise ValueError("Only complete takes can be exported; partial previews are not accepted takes.")
        wav = (directory / "take.wav").read_bytes()
        if hashlib.sha256(wav).hexdigest() != row["audio_sha256"]:
            raise ValueError("Saved take checksum changed.")
        if kind == "audio":
            return wav, "audio/wav"
        if kind == "metadata":
            return json.dumps(row, ensure_ascii=False, indent=2).encode(), "application/json"
        if kind != "package":
            raise ValueError("Unknown Drama artifact.")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("take.wav", wav)
            archive.writestr("take.json", json.dumps(row, ensure_ascii=False, indent=2))
            archive.writestr("script.txt", row["request"]["text"])
            archive.writestr("transcript.txt", row["script"]["plain_text"])
            archive.writestr(
                "README.txt",
                "Built with Fish Audio. Fish Audio Community License 1.0 applies. Mixed cast take; no isolated stems or word timings. Human listening approval is separate.\nhttps://huggingface.co/fishaudio/drama-3/blob/main/LICENSE\n",
            )
        return output.getvalue(), "application/zip"

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.events.values():
                event.set()
        if getattr(self, "thread", None):
            self.thread.join(15)
