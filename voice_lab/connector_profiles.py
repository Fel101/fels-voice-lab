"""Versioned optional OpenAI-compatible providers configured by this Lab's operator."""

import hashlib
import ipaddress
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = 1
SOURCE = re.compile(r"provider-[a-z0-9][a-z0-9-]{0,39}")
SECRET = re.compile(r"[A-Z][A-Z0-9_]{2,79}")
MAX_RESPONSE = 25 * 1024 * 1024
SETTINGS = Path(__file__).resolve().parents[1] / "out" / "provider-profiles.json"
_saved_lock = threading.Lock()
_session_keys = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def configured_profiles():
    location = os.environ.get("VOICE_LAB_PROVIDER_FILE")
    path = Path(location) if location else SETTINGS
    if not location and not path.is_file():
        return {}
    if not path.is_absolute() or str(path).startswith(("//", chr(92) * 2)):
        raise ValueError("Choose a local absolute provider file path.")
    try:
        with path.open("rb") as handle:
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("Provider settings exceed the size limit.")
        saved = json.loads(raw)
        if saved["schema_version"] != VERSION or not isinstance(saved["providers"], list):
            raise ValueError("Use provider schema version 1.")
        if len(saved["providers"]) > 24:
            raise ValueError("Configure up to 24 providers.")
        profiles = {}
        for row in saved["providers"]:
            info = check(row, allow_session=not location)
            if info["id"] in profiles:
                raise ValueError("Each provider must have a unique ID.")
            profiles[info["id"]] = info
        return profiles
    except (TypeError, OSError, KeyError) as exc:
        raise ValueError("Check the local provider configuration file.") from exc


def save(row, key):
    if os.environ.get("VOICE_LAB_PROVIDER_FILE"):
        raise ValueError("This Lab uses a separate provider settings file. Manage those profiles in that file.")
    if not isinstance(row, dict) or set(row) - {"id", "base_url", "model", "capabilities"}:
        raise ValueError("Enter a provider name, address, model and what it can do.")
    if not isinstance(key, str) or len(key) > 512 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        raise ValueError("Enter a valid provider key, or leave it empty for a local server that needs no key.")
    with _saved_lock:
        rows = configured_profiles()
        ident = row.get("id")
        if not isinstance(ident, str):
            raise ValueError("Enter a name for this provider.")
        prior = rows.get(ident)
        declared = bool(key or (prior and prior["session_key"]))
        profile = check({**row, "session_key": declared}, allow_session=True)
        if profile["external"] and prior is None and not key:
            raise ValueError("Enter the provider's key before saving an HTTPS profile.")
        rows[profile["id"]] = profile
        if len(rows) > 24:
            raise ValueError("Save up to 24 provider profiles.")
        fields = ("id", "base_url", "model", "capabilities", "session_key")
        saved = {"schema_version": VERSION, "providers": [{field: value[field] for field in fields} for value in rows.values()]}
        from voice_lab.dubbing import write_json_atomic

        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(SETTINGS, saved)
        if key:
            _session_keys[(profile["id"], profile["revision"])] = key
        return public(profile)


def check(row, *, allow_session=False):
    fields = {"id", "base_url", "model", "capabilities", "credential_env"}
    if allow_session:
        fields.add("session_key")
    if not isinstance(row, dict) or set(row) - fields:
        raise ValueError("Invalid provider configuration.")
    ident, endpoint, model = row.get("id"), row.get("base_url"), row.get("model")
    allowed, secret = row.get("capabilities"), row.get("credential_env", "")
    session_key = row.get("session_key", False)
    if (
        not isinstance(ident, str)
        or not SOURCE.fullmatch(ident)
        or not isinstance(endpoint, str)
        or len(endpoint) > 400
        or not isinstance(model, str)
        or not 1 <= len(model) <= 120
        or not isinstance(allowed, list)
        or not allowed
        or any(mode not in ("speak", "translate") for mode in allowed)
        or not isinstance(secret, str)
        or type(session_key) is not bool
        or (secret and not SECRET.fullmatch(secret))
    ):
        raise ValueError("Each provider needs an ID, base URL, model, and speech or translation capability.")
    url = urllib.parse.urlsplit(endpoint)
    if (
        not url.hostname
        or url.username
        or url.password
        or url.fragment
        or url.query
        or not re.fullmatch(r"/[A-Za-z0-9/_-]*v1/?", url.path)
    ):
        raise ValueError("The provider base URL must name an explicit /v1 endpoint.")
    try:
        is_local = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        is_local = False
    if (
        url.scheme not in ("http", "https")
        or (not is_local and (url.scheme != "https" or not (secret or session_key)))
        or any(value in endpoint for value in ("\r", "\n", "\\"))
    ):
        raise ValueError("Enter an HTTPS address and a key for a provider outside this computer.")
    port = url.port
    profile = {
        "id": ident,
        "base_url": endpoint.rstrip("/"),
        "model": model,
        "capabilities": sorted(set(allowed)),
        "credential_env": secret,
        "session_key": session_key,
        "external": not is_local,
        "destination": url.hostname + (f":{port}" if port else ""),
    }
    profile["revision"] = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    return profile


def get(ident, operation=None):
    profile = configured_profiles().get(ident)
    if profile is None or (operation is not None and operation not in profile["capabilities"]):
        raise ValueError(f"The selected provider does not support {operation or 'this connection'}.")
    return profile


def public(profile):
    return {
        key: val
        for key, val in profile.items()
        if key in ("id", "capabilities", "destination", "external", "model", "revision")
    }


def exchange(profile, leaf, payload=None, *, opener=None, byte_limit=MAX_RESPONSE):
    headers = {}
    if profile["credential_env"] or profile["session_key"]:
        key = (
            os.environ.get(profile["credential_env"], "")
            if profile["credential_env"]
            else _session_keys.get((profile["id"], profile["revision"]), "")
        )
        if not key:
            raise ValueError("Open Automation and enter the key for this provider, then run the request again.")
        if any(ord(char) < 32 or ord(char) > 126 for char in key):
            raise ValueError("The configured provider credential is invalid.")
        headers["Authorization"] = "Bearer " + key
    encoded = json.dumps(payload).encode("utf-8") if payload is not None else None
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(profile["base_url"] + leaf, data=encoded, headers=headers)
    client = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with client.open(request, timeout=50) as reply:
            raw = reply.read(byte_limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ValueError("The configured provider did not complete the connection.") from exc
    if len(raw) > byte_limit:
        raise ValueError("The configured provider response exceeds the size limit.")
    return raw


def translate(profile, text, source_language, target_language):
    message = {
        "model": profile["model"],
        "stream": False,
        "temperature": 0,
        "messages": [
            {
                "role": "system",
                "content": f"Translate the supplied text from {source_language} to "
                f"{target_language}. Return only the translation.",
            },
            {"role": "user", "content": text},
        ],
    }
    try:
        answer = json.loads(exchange(profile, "/chat/completions", message, byte_limit=1_000_000))
        text = answer["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("The provider did not return translated text.")
        return text.strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("The provider did not return translated text.") from exc


def speak(profile, text, voice, speed):
    body = {"model": profile["model"], "input": text, "voice": voice, "speed": speed, "response_format": "wav"}
    audio = exchange(profile, "/audio/speech", body)
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise ValueError("The provider did not return a WAV.")
    return audio
