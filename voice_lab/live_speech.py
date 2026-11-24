"""Short-lived local speaking sessions. PCM uploads and the event cursor survive reconnects."""

import hashlib
import io
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field

from voice_lab import speech_input

SAMPLE_RATE = 16000
FRAME_BYTES = 2
MAX_TURN = SAMPLE_RATE * FRAME_BYTES * 30
MAX_CHUNK = SAMPLE_RATE * FRAME_BYTES * 3
SESSION_LIMIT = 24
MAX_CHUNKS = 1500
IDLE = 900


@dataclass
class Session:
    options: dict
    touched: float
    audio: bytearray = field(default_factory=bytearray)
    chunks: dict = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=100))
    next_event: int = 1
    state: str = "open"
    running: bool = False
    cancelled: threading.Event = field(default_factory=threading.Event)
    playback: threading.Event = field(default_factory=threading.Event)


class Sessions:
    def __init__(self, state_root):
        self.root = state_root
        self.rows = {}
        self.lock = threading.RLock()

    def prune(self):
        now = time.monotonic()
        for key, value in list(self.rows.items()):
            if not value.running and now - value.touched > IDLE:
                del self.rows[key]

    def create(self, req):
        options = speech_input.settings(req)
        with self.lock:
            self.prune()
            if len(self.rows) >= SESSION_LIMIT:
                reclaim = next((key for key, row in self.rows.items() if row.state == "cancelled" and not row.running), None)
                if reclaim is None:
                    raise ValueError("Too many local live sessions. Cancel an existing session or let it expire.")
                del self.rows[reclaim]
            key = uuid.uuid4().hex
            row = Session(options, time.monotonic())
            self.rows[key] = row
            self.event(row, "session.ready", sample_rate=SAMPLE_RATE, format="pcm_s16le")
            return {"id": key, **self.replay(key, 0)}

    def get(self, key):
        self.prune()
        try:
            row = self.rows[key]
        except KeyError:
            raise KeyError("This live session expired or the service restarted. Open a new session.") from None
        row.touched = time.monotonic()
        return row

    @staticmethod
    def event(row, event, **details):
        row.events.append({"id": row.next_event, "type": event, **details})
        row.next_event += 1

    def replay(self, key, after):
        with self.lock:
            row = self.get(key)
            if after < 0 or after >= row.next_event or (row.events and after < row.events[0]["id"] - 1):
                raise ValueError("The event cursor is outside this session's replay window.")
            return {
                "state": row.state,
                "next_offset": len(row.audio) // 2,
                "cursor": row.next_event - 1,
                "events": [event for event in row.events if event["id"] > after],
            }

    def push(self, key, offset, raw):
        if not raw or len(raw) > MAX_CHUNK or len(raw) % 2 or offset < 0:
            raise ValueError("Send at most three seconds of 16 kHz signed 16-bit mono PCM.")
        fingerprint = hashlib.sha256(raw).digest()
        with self.lock:
            row = self.get(key)
            if row.state != "open":
                raise ValueError("This live audio turn is closed.")
            next_offset = len(row.audio) // 2
            if offset < next_offset and row.chunks.get(offset) == fingerprint:
                return {"next_offset": next_offset, "replayed": True}
            if offset != next_offset or len(row.audio) + len(raw) > MAX_TURN or len(row.chunks) >= MAX_CHUNKS:
                raise ValueError("Use the next sample offset and at most 1,500 chunks or 30 seconds per turn.")
            row.chunks[offset] = fingerprint
            row.audio.extend(raw)
            return {"next_offset": len(row.audio) // 2, "replayed": False}

    def recognize(self, key, final):
        with self.lock:
            row = self.get(key)
            if row.state == "complete" and final:
                return self.replay(key, row.next_event - 2)
            if row.state != "open" or row.running or len(row.audio) < SAMPLE_RATE:
                raise ValueError("Send at least half a second of audio and wait for any current recognition first.")
            row.running = True
            if final:
                row.state = "finalizing"
            value = bytes(row.audio)
            length = len(value) // 2
        try:
            clip = io.BytesIO()
            with wave.open(clip, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(FRAME_BYTES)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(value)
            result = speech_input.run(self.root, clip.getvalue(), ".wav", row.options, cancelled=row.cancelled)
            with self.lock:
                if row.cancelled.is_set():
                    raise speech_input.InputCancelled("The session was cancelled.")
                if final:
                    row.state = "complete"
                self.event(
                    row,
                    "transcript.final" if final else "transcript.partial",
                    text=result["text"],
                    language=result["language"],
                    samples=length,
                )
                return self.replay(key, row.next_event - 2)
        finally:
            with self.lock:
                row.running = False
                if row.state == "finalizing":
                    row.state = "open"

    def cancel(self, key):
        with self.lock:
            row = self.get(key)
            if row.state != "cancelled":
                row.state = "cancelled"
                row.cancelled.set()
                row.playback.set()
                row.audio.clear()
                row.chunks.clear()
                self.event(row, "session.cancelled")
            return self.replay(key, row.next_event - 2)
