"""Use the running Lab's speech and cancellation routes on this computer."""

import http.client
import json
import threading
import uuid

import speech_input

MAX_RESPONSE = 32 * 1024 * 1024


class LocalClient:
    def __init__(self, port):
        self.port = port

    def request(self, path, payload=None, *, key=None, limit=MAX_RESPONSE):
        wire = http.client.HTTPConnection("127.0.0.1", self.port, timeout=520)
        headers = {"X-Request-Id": key} if key else {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            wire.request(
                "GET" if payload is None else "POST",
                path,
                body=None if payload is None else json.dumps(payload).encode(),
                headers=headers,
            )
            answer = wire.getresponse()
            raw = answer.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("The automation response exceeds the local limit.")
            if answer.status >= 400:
                error = json.loads(raw).get("error", "The local request failed.")
                raise ValueError(error.get("message", "") if isinstance(error, dict) else error)
            return raw
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError("The Voice Lab service stopped answering.") from exc
        finally:
            wire.close()

    def speak(self, text, voice, speed, cancellation):
        return self.cancellable(
            "/v1/audio/speech",
            {"model": "tts-1", "voice": voice, "input": text, "speed": speed, "response_format": "wav", "wait_seconds": 180},
            cancellation,
        )

    def cancellable(self, path, body, cancellation):
        key = "automation-" + uuid.uuid4().hex
        done = threading.Event()

        def stop_when_requested():
            while not done.wait(0.2):
                if cancellation.is_set():
                    for _ in range(3):
                        try:
                            result = json.loads(self.request(f"/api/queue/{key}/cancel", {}))
                            if result.get("cancelled") or done.wait(0.15):
                                break
                        except ValueError:
                            if done.wait(0.15):
                                break
                    return

        listener = threading.Thread(target=stop_when_requested, daemon=True)
        listener.start()
        try:
            if cancellation.is_set():
                raise speech_input.InputCancelled("Cancelled before the clip started.")
            return self.request(path, body, key=key)
        finally:
            done.set()
