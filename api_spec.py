"""The Voice Lab's HTTP API, written down once.

Every route serve.py answers is a row in ROUTES below. The service's dispatch is built from this table (routes.py): serve.py
registers one handler for each row and will not start with a row nobody answers or a handler with no row, so a new route has
to be documented or marked internal on purpose. From the same table this module builds the OpenAPI document (openapi.json,
also served at GET /api/openapi.json) and the reference tables in docs/API.md; tools/build_api_docs.py writes both files.

A route is *documented* when scripts may rely on it: its shape changes only with a new version and a CHANGELOG line. It is
*internal* when it exists for the Lab's own pages (the pages, their scripts, appearance, pickers); it can change at any time
and is listed in docs/API.md and in openapi.json under x-internal-routes, but not described.

Standard library only, so a script or a test can import it without the Lab's model environments.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app_version import APP_NAME, VERSION

OPENAPI_VERSION = "3.1.0"
DEFAULT_URL = "http://127.0.0.1:8765"


@dataclass(frozen=True)
class Field:
    """A request field, a query-string parameter or a response header."""
    name: str
    type: str          # string, integer, number, boolean, object, or one of those with [] after it for a list
    doc: str
    required: bool = False


@dataclass(frozen=True)
class Route:
    method: str
    path: str                  # {name} marks a path parameter; a trailing /* is a prefix answered by one handler
    group: str
    summary: str               # one sentence, imperative
    internal: bool = False
    query: tuple = ()          # Field: query-string parameters
    body: tuple = ()           # Field: members of the JSON request body
    body_kind: str = "json"    # json, audio (a recording's bytes plus X-Reference-Metadata) or none
    reply: str = ""            # what the answer holds, in Markdown
    reply_kind: str = "json"   # json, wav, ndjson, csv, audio, html, javascript or file
    status: int = 200          # the success status
    headers: tuple = ()        # Field: response headers worth knowing
    errors: tuple = ()         # (status, when) pairs beyond the usual 400 and 404
    notes: str = ""            # more Markdown for the reference


def F(name, type, doc, required=False):
    return Field(name, type, doc, required)


def R(method, path, group, summary, **more):
    return Route(method, path, group, summary, **more)


ID = "[a-f0-9]{32}"
EXAMPLE_ID = "0123456789abcdef0123456789abcdef"

# Path parameters: (what it is, the pattern serve.py accepts, an example that fits it). tests/test_api_docs.py checks the
# examples against the patterns and the patterns against the routes serve.py really matches.
PATH_PARAMS = {
    "character_id": ("A character's id, from `GET /api/cast`.", ID, EXAMPLE_ID),
    "identity_id": ("A voice identity's id, from `GET /api/cast`.", ID, EXAMPLE_ID),
    "reference_id": ("A reference recording version's id, from `GET /api/cast`.", ID, EXAMPLE_ID),
    "take_id": ("A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`.", ID, EXAMPLE_ID),
    "scene_id": ("A scene's id, from `GET /api/scenes`.", ID, EXAMPLE_ID),
    "job_id": ("A job's id, from the call that started it.", ID, EXAMPLE_ID),
    "session_id": ("A blind listening session's id, from `GET /api/evaluations/blind-sessions`.", ID, EXAMPLE_ID),
    "item_id": ("A compared sample's id.", ID, EXAMPLE_ID),
    "run_id": ("A registered comparison run's id, from `GET /api/evaluations`.", "[A-Za-z0-9_-]{1,80}", "neutral-clone-1"),
    "export_id": ("An export package's folder name under `out/exports`, from `GET /api/exports`.",
                  "[A-Za-z0-9][A-Za-z0-9._-]{0,89}", "panel-20261025T120000Z-1a2b3c4d"),
    "engine_id": ("An engine id from `GET /api/catalog`.", "[a-z0-9][a-z0-9.-]{0,80}", "kokoro"),
    "preview_file": ("The file name in a preview answer's `audio_url`, without its `.wav`.", "[A-Za-z0-9._-]+", "preview"),
}

# The groups in the order the reference shows them, each with the sentence that opens its section.
GROUPS = (
    ("Service", "Is it running, what is loaded, which engines exist. None of these makes audio, except an engine check."),
    ("Speaking", "Turn text into audio. `speak` and `performance` answer with the finished WAV file in one response."),
    ("Cast", "Characters, the voices (identities) they speak with, the recordings that clone those voices, and "
             "pronunciations. Retiring blocks new use of an item and everything made from it; purging deletes it."),
    ("Takes", "Saved performances. Every `performance` call and every scene render saves a take with its text, settings, "
              "model and provenance. An edit of a take (a trim, a cut of the silence, fades, a gain) is saved as a new take "
              "that names its original."),
    ("Scenes", "Ordered scripts rendered line by line through the same performance call. The manifest's fields are in "
               "[scene-schema-v1.md](scenes/scene-schema-v1.md)."),
    ("Exports", "Delivery packages: a scene's selected takes, converted, checked and written to `out/exports`."),
    ("Comparisons", "Listening comparisons and blind A/B sessions. See "
                    "[evaluation-schema-v1.md](evaluation/evaluation-schema-v1.md)."),
    ("Conversion", "Voice conversion: say a recording again in another voice."),
    ("System", "The service's report, folder sizes, library backups and housekeeping."),
    ("Pages", "The Lab's own pages and the scripts they load."),
    ("Page helpers", "Data the Lab's pages read and write for themselves."),
    ("Drama", "The Drama studio page's job API."),
)

KOKORO_FIELDS = (
    F("voice", "string", "Kokoro: a voice name, or several joined with commas to blend them. Default `af_heart`. Phonon: one "
                         "voice name from `phonon_voices` of `GET /api/info`. Default `Harper`."),
    F("speed", "number", "Kokoro: speaking rate. Default 1.0."),
    F("language", "string", "Kokoro and Qwen: the language. Default `English`. Phonon speaks English only."),
)

# What an edit of a take takes. The preview and the save take the same fields; the save adds an idempotency key.
EDIT_FIELDS = (
    F("start_seconds", "number", "Where the kept part starts, in seconds from the start of the take. Default 0."),
    F("end_seconds", "number", "Where the kept part ends. Default the end of the take. It must be after `start_seconds`."),
    F("auto_trim", "boolean", "Cut the quiet before and after the speech inside that range. Default false."),
    F("silence_threshold_db", "number", "Auto-trim: anything quieter than this counts as silence, from -90 to -10 dB. Default -50."),
    F("keep_silence_ms", "number", "Auto-trim: how much of the silence to leave at each end, 0 to 2000 ms. Default 80."),
    F("fade_in_ms", "number", "A fade in over the start of the result, 0 to 5000 ms. Default 0."),
    F("fade_out_ms", "number", "A fade out over the end of the result, 0 to 5000 ms. Default 0."),
    F("gain_db", "number", "Make the result louder or quieter, -60 to 24 dB. A gain that would clip is refused. Default 0."),
)
EDIT_REFUSALS = ("A field is unknown, not a number (or not true or false) or out of range; `end_seconds` is not after "
                 "`start_seconds`; the edit changes nothing; the result would be under 0.05 seconds; the two fades are longer "
                 "than the result; auto-trim finds nothing louder than the threshold; a gain would clip the loudest sample (the "
                 "answer says how much the take can take); the take is retired; or its stored audio is missing or no longer "
                 "matches its checksum.")
EDIT_NOTES = ("An edit never changes the take it starts from. The steps run in this order: trim to `start_seconds` to "
              "`end_seconds`; auto-trim inside that range (quiet inside the speech is never cut); the two fades, on a "
              "raised-cosine curve; then the gain. A trim alone copies the samples exactly; a fade or a gain recalculates them and "
              "rounds back to 16 bits. The new take keeps the parent's text, voice, settings and model, starts as a "
              "`candidate`, and has its loudness and checks measured again. Its word and phoneme times, when the parent has "
              "them, move with the trim, and a word the trim cuts away leaves them. An edit of an edit is allowed. Retiring "
              "a take retires its edits, and purging it deletes them.")

ENGINE_NOT_INSTALLED = ("The engine could not load or run. When it is not installed and has a guided installer, `error` begins "
                        "by saying where to install it: Install on its Models & features card, or its launcher in the lab "
                        "folder with the arguments that choose the model: `setup-kokoro.bat`, `setup-phonon.bat`, "
                        "`setup-chatterbox.bat` (Chatterbox Turbo, or with `--original` Chatterbox 500M and Voice Conversion), "
                        "`setup-qwen.bat` with `--model` and the Qwen3-TTS model, or `setup-voxcpm.bat`.")

ROUTES = [
    # ---------------------------------------------------------------- Service
    R("GET", "/api/health", "Service", "Say whether the service is ready, and which version it is.",
      reply="`status` (`ready`, or `busy` while a model loads or generates), `busy`, `device`, `version`, `workers` (one entry "
            "per loaded engine), `last_worker_error` and `last_unload`."),
    R("GET", "/api/info", "Service", "Name the device, the loaded engines, the Kokoro and Phonon voices found and the free memory.",
      reply="`device`, `loaded` (the engines in memory), `voices` (Kokoro voice names), `kokoro_languages`, `phonon_voices` "
            "(the installed Phonon voices, each with `name`, `group`, `character`, `gender`, `age`, `locale` and "
            "`description`) and `free_ram_gb`."),
    R("GET", "/api/catalog", "Service", "List every engine the Lab knows, with its pinned checkpoint, capabilities and license.",
      reply="The engine catalog. An engine's `id` is the `engine` of the speaking calls; only engines whose "
            "`integration.service_api` is true answer them."),
    R("GET", "/api/engines/status", "Service",
      "Say which engines are installed and usable here, and what each check measured. Reads files and stored results only.",
      reply="`device` (the service's: `cuda` for a graphics card, or `cpu`), `busy`, `engines` (one row per catalog engine: "
            "`id`, `name`, `integrated`, `installation`, `state`, `check`, `check_outdated`, `min_free_ram_gb`, `device` and, "
            "for an integrated engine, `switch`, `device_choice` and `gpu`), `ram`, `install`, `speed` and `store_error`. "
            "`installation` holds `installed`, `missing` (the files not found), `environment`, `checkpoint` and, while files "
            "are missing from an engine that has a guided installer, `installer`: the launcher in the lab folder that installs "
            "it. `min_free_ram_gb` is the free RAM in GB the Lab wants before it loads the engine (`null` when none is set), "
            "and `ram` is this computer's `total_gb` and `free_gb`. An engine's `device` is the one its worker is asked for, "
            "and `check` is its last check there. `switch` is what its GPU/CPU switch shows (`gpu` or `cpu`), "
            "`device_choice` the choice kept for it (`null` when it follows the service), and `gpu` holds `choosable` (GPU "
            "can be picked), `reason` (why not), `build` (the PyTorch build in its environment: `cuda`, `rocm`, `cpu` or "
            "`null`) and `install` (Install puts in its GPU build). `install` is the latest install started from the Models "
            "page, or `null` when none has run since the service started: `engine`, `name`, `nvidia` (the GPU build), "
            "`state` (`running`, `finished` or `failed`), `started_at`, `ended_at`, `seconds`, `exit_code`, `lines` (the end "
            "of its output) and `log` (all of it, a path under the lab folder)."),
    R("POST", "/api/engines/{engine_id}/check", "Service",
      "Check that one engine can speak now: confirm its files, load it and speak one short clip.",
      body_kind="none",
      reply="`check`: `status` (`passed`, `failed` or `not_installed`), `device`, `ran_on`, `checked_at`, `reason`, "
            "`load_seconds`, `total_s`, `audio_seconds`, `real_time_factor` and, for a missing install, `missing`. Takes "
            "seconds to minutes; the result is kept and shows in `GET /api/engines/status`.",
      errors=((400, "The engine is unknown, or is not a speech engine."), (409, "The GPU worker is busy."))),
    R("POST", "/api/engines/{engine_id}/install", "Service",
      "Install an engine that has a guided installer (Kokoro, Phonon or Chatterbox Turbo): read its plan, or run the "
      "installer.",
      body=(F("dry_run", "boolean", "Only read the plan: what the installer would do and whether this computer can take it. "
                                    "Nothing is changed. Default false, which starts the install."),
            F("nvidia", "boolean", "Install the engine's GPU build for an NVIDIA graphics card (its installer's `--nvidia`). "
                                   "An engine installed with the CPU build of PyTorch can get it afterwards. Default false.")),
      reply="With `dry_run`, `plan`: `engine`, `nvidia`, `ok`, `paragraphs` (the plan, one string for each paragraph or "
            "numbered step) and `problems` (why it cannot go ahead; empty when `ok`). Otherwise `install`: the job that "
            "started, as in `GET /api/engines/status`, which is the route to poll for its progress. The job runs the script "
            "the engine's launcher runs, with `--yes`. While it runs the service refuses every other load, check and clip.",
      errors=((400, "The engine is unknown, has no installer (or no GPU build) in the app, or is installed already; or "
                    "`dry_run` or `nvidia` is not true or false."),
              (409, "An install is already running, or the GPU worker is busy."))),
    R("POST", "/api/engines/{engine_id}/device", "Service",
      "Run one engine on the graphics card or on the CPU from its next load, as the switch on its Models & features card does.",
      body=(F("device", "string", "`gpu` or `cpu`.", required=True),),
      reply="`device`: `engine`, `choice`, `device` (the one its worker is now asked for, `cuda` or `cpu`) and `unloaded` "
            "(true when the engine was loaded, so its next request loads it again). The choice is kept in "
            "`data/engines/devices.json`. An engine whose environment holds the CPU build of PyTorch runs on the CPU whatever "
            "is chosen, until its GPU build is installed.",
      errors=((400, "The engine is unknown or runs only on the CPU (Phonon, Drama 3), `device` is not `gpu` or `cpu`, or "
                    "`gpu` was asked of a service that runs on the CPU."),
              (409, "The GPU worker is busy."))),
    R("POST", "/api/load", "Service", "Load an engine into memory ahead of its first use.",
      body=(F("engine", "string", "An engine id. Default `kokoro`."),
            F("profile", "string", "Breeze only: its decode profile. Default `eager`.")),
      reply="`engine`, `loaded`, `load_seconds`, `sample_rate`, `memory`, `backend`, `checkpoint`, `checkpoint_revision`, "
            "`supported_speakers` and `supported_languages`.",
      errors=((409, "The GPU worker is busy."), (500, ENGINE_NOT_INSTALLED))),
    R("POST", "/api/unload", "Service", "Unload every engine and free the GPU.",
      body_kind="none", reply="`{\"ok\": true}`.", errors=((409, "The GPU worker is busy."),)),
    R("POST", "/api/prepare-reference", "Service", "Prepare a reference recording for an engine so its first clone is quick.",
      body=(F("engine", "string", "An engine id that clones from a recording.", True),
            F("path", "string", "A recording inside the Lab's reference storage.", True),
            F("reference_text", "string", "The exact transcript, which Breeze, Qwen Base and Drama 3 need."),
            F("exaggeration", "number", "Chatterbox: how strongly to act. Default 0.5."),
            F("profile", "string", "Breeze only: its decode profile."),
            F("xvec_only", "boolean", "Qwen Base only: use the speaker embedding alone, without a transcript.")),
      reply="What the engine reports about the prepared reference.", errors=((409, "The GPU worker is busy."),)),
    R("GET", "/api/performance/settings", "Service",
      "Describe the settings each engine's performance takes, and the languages its picker offers.",
      reply="`engines`: for each engine its controls (name, type, range, default), whether it takes a direction, and its "
            "`languages`; and `unscreened_languages`: per engine, the languages the language screen did not clear."),
    R("GET", "/api/openapi.json", "Service", "Return this API as an OpenAPI 3.1 document.",
      reply="The same document as `openapi.json` in the program folder, built from the route table."),

    # ---------------------------------------------------------------- Speaking
    R("POST", "/api/speak", "Speaking", "Speak text with one engine and answer with the finished WAV file.",
      body=(F("text", "string", "What to say, 1 to 1200 characters.", True),
            F("engine", "string", "An engine id from `GET /api/catalog` whose `integration.service_api` is true. "
                                  "Default `kokoro`."),
            *KOKORO_FIELDS,
            F("voice_description", "string", "Qwen VoiceDesign, VoxCPM2 and Breeze design mode: the voice to invent, in words."),
            F("mode", "string", "For engines with several modes: `design`, `clone`, `continuation`, `custom_voice` or, for "
                                "Breeze, `direction`. A checkpoint accepts the modes it supports."),
            F("reference_path", "string", "Cloning: a recording inside the Lab's reference storage."),
            F("reference_text", "string", "Cloning: the exact transcript of that recording."),
            F("direction", "string", "Engines with a direction control: how to deliver the line, up to 1000 characters."),
            F("seed", "integer", "Engines that take one: the random seed. Default 42."),
            F("temperature", "number", "Phonon: how much the delivery varies, 0 to 1. Default 0.3. The other engines that take "
                                       "a temperature list theirs under `capabilities` in `GET /api/catalog`."),
            F("duration_delta", "number", "Phonon: shift the pace, from -0.5 (faster) to 0.5 (slower). Default 0."),
            F("save_voice_as", "string", "Design mode only: keep the invented voice as a reusable reference under this "
                                         "name, letters, digits, `_` and `-`, up to two folders deep.")),
      reply="The whole 16-bit WAV file, in one response with a `Content-Length`. The call blocks until the clip is "
            "finished; there is no streaming and no partial answer. Other engine-specific fields are listed under each "
            "engine's `capabilities` in `GET /api/catalog`.",
      reply_kind="wav",
      headers=(F("X-Gen-Seconds", "string", "Seconds the engine took."),
               F("X-Audio-Seconds", "string", "Seconds of audio in the file."),
               F("X-First-Chunk", "string", "Seconds to the first audio, when the engine reports it; otherwise empty."),
               F("X-Generation-Mode", "string", "The mode the engine ran in, when it reports one."),
               F("X-Voice-Backend", "string", "The backend, when reported."),
               F("X-Model-Checkpoint", "string", "The checkpoint, when reported."),
               F("X-Checkpoint-Revision", "string", "The checkpoint's pinned revision, when reported."),
               F("X-Voice-Asset-Path", "string", "With `save_voice_as`: the file the voice was saved to.")),
      errors=((409, "`save_voice_as` names a voice that already exists, or the GPU worker is busy with another request. "
                    "One generation runs at a time; a second request is refused, not queued."),
              (413, "The request is over 64 KiB."), (500, ENGINE_NOT_INSTALLED),
              (504, "The engine did not answer in time.")),
      notes="Send no query string: `/api/speak?x=1` is a 404. Qwen, VoxCPM2 and Drama 3 refuse a field they do not take and "
            "name it in the 400. To keep what you generate as a Cast take, use "
            "[`POST /api/performance`](#post-api-performance)."),
    R("POST", "/api/stream", "Speaking", "Speak text with a Qwen3-TTS checkpoint and stream the audio as it is made.",
      body=(F("text", "string", "What to say, 1 to 1200 characters.", True),
            F("engine", "string", "A Qwen3-TTS engine id.", True),
            F("mode", "string", "`design`, `clone` or `custom_voice`, as the checkpoint supports."),
            F("voice_description", "string", "VoiceDesign: the voice to invent, in words."),
            F("reference_path", "string", "Base (clone): a recording inside the Lab's reference storage."),
            F("reference_text", "string", "Base (clone): the exact transcript."),
            F("speaker", "string", "CustomVoice: a speaker the loaded checkpoint reports."),
            F("language", "string", "The language. Default `English`."),
            F("save_voice_as", "string", "VoiceDesign only: keep the voice as a reusable reference under this name.")),
      reply="Newline-delimited JSON: one `start` line, `audio` lines (`sequence`, `sample_start`, `sample_count`, "
            "`sample_rate`, `sample_format` `pcm_s16le` and the PCM as base64 in `data`) and a closing `complete` line with "
            "the timings. A failure after the first line arrives as an `error` line, not a status code.",
      reply_kind="ndjson",
      notes="Use `POST /api/speak` when you want a file."),
    R("POST", "/api/performance", "Speaking", "Speak a line as a Cast voice and save it as a take.",
      body=(F("identity_id", "string", "The voice identity to speak with, from `GET /api/cast`.", True),
            F("text", "string", "The line, 1 to 1200 characters.", True),
            F("line_id", "string", "Your own label for the line, 1 to 100 characters. Takes are grouped by voice and line.",
              True),
            F("reference_version_id", "string", "The recording to clone from. Default: the identity's approved recording. "
                                                "Refused for a fixed or preset voice."),
            F("language", "string", "Default: the voice's own language, else `English`."),
            F("direction", "string", "How to deliver the line, up to 1000 characters. An engine with no verified "
                                     "direction control refuses a non-empty one."),
            F("settings", "object", "The engine's performance settings; see `GET /api/performance/settings`."),
            F("use_pronunciations", "boolean", "Apply the character's pronunciation dictionary to the spoken text. The take "
                                               "keeps both texts. Default false."),
            F("idempotency_key", "string", "1 to 200 characters. Repeating a call with the same key and the same inputs "
                                           "returns that take's audio without generating again."),
            F("identity_snapshot", "object", "Used by scene batches: the voice's `identity_type`, `engine_id` and "
                                             "`engine_options` as the scene saved them. The call is refused if the voice has "
                                             "changed since.")),
      reply="The take's 16-bit WAV file, in one response. The call blocks until the clip is finished and saved.",
      reply_kind="wav",
      headers=(F("X-Take-Id", "string", "The saved take's id."),
               F("X-Cast-Character", "string", "The character's name."),
               F("X-Cast-Identity", "string", "The voice's label."),
               F("X-Cast-Line-Id", "string", "The `line_id` you sent."),
               F("X-Gen-Seconds", "string", "Seconds the engine took."),
               F("X-Audio-Seconds", "string", "Seconds of audio in the file."),
               F("X-Voice-Backend", "string", "The backend."),
               F("X-Model-Checkpoint", "string", "The checkpoint."),
               F("X-Checkpoint-Revision", "string", "The checkpoint's pinned revision."),
               F("X-Generation-Mode", "string", "`clone`, `direction`, `custom_voice` or `fixed_voice`."),
               F("X-Idempotent-Replay", "string", "`true` when the answer is an earlier take for the same key; then only "
                                                  "`X-Take-Id` and the three `X-Cast-*` headers come with it.")),
      errors=((400, "The identity is rejected or retired, has no usable recording, the text or direction is too long, the "
                    "settings do not fit the engine, or `idempotency_key` was used before with different inputs or for an edit "
                    "of a take."),
              (404, "The identity or the recording does not exist."),
              (409, "The GPU worker is busy. One generation runs at a time; a second request is refused, not queued."),
              (504, "The engine did not answer in time.")),
      notes="See [Speak as a Cast voice](#speak-as-a-cast-voice) for how to find the ids and what a retry does."),
    R("POST", "/api/kokoro/phonemize", "Speaking", "Show the phonemes Kokoro would speak for a text.",
      body=(F("text", "string", "1 to 1200 characters.", True), F("language", "string", "Default `English`.")),
      reply="`language`, `language_name` and `segments`, each with its `graphemes` and `phonemes`.",
      notes="Kokoro's language rules live in its own environment, so its worker answers: Kokoro is loaded first when it is "
            "not loaded already, as for speaking, and the model loaded before it is unloaded. A text or a language it cannot "
            "take is refused with a 400 before anything loads."),

    # ---------------------------------------------------------------- Cast
    R("GET", "/api/cast", "Cast", "List every character with its voices and their recordings.",
      reply="`schema_version` and `characters`. Each character has `id`, `name`, `description`, `tags` and `identities`; each "
            "identity has `id`, `engine_id`, `label`, `status` (`candidate`, `approved` or `rejected`), `kind` (`reference`, "
            "`fixed_voice` or `preset_voice`), `engine_options`, `approved_reference_id`, `notes` and `references` (the "
            "recordings, newest version first, each with `id`, `version`, `transcript`, `origin`, `allowed_use`, "
            "`duration_seconds` and `audio_url`). Retired items say so."),
    R("POST", "/api/characters", "Cast", "Create a character.", status=201,
      body=(F("name", "string", "1 to 80 characters.", True), F("description", "string", "Up to 2000 characters."),
            F("tags", "string[]", "Up to 40 characters each.")),
      reply="`character`, as in `GET /api/cast`."),
    R("POST", "/api/characters/{character_id}", "Cast", "Rename a character or change its description.",
      body=(F("name", "string", "1 to 80 characters. Leave out to keep it."),
            F("description", "string", "Up to 2000 characters. Leave out to keep it.")),
      reply="`character`."),
    R("POST", "/api/characters/{character_id}/identities", "Cast", "Add a voice to a character.", status=201,
      body=(F("engine_id", "string", "A recording-cloning engine, `kokoro`, `phonon-31m-en` or the Qwen CustomVoice engine that "
                                     "the service exposes.", True),
            F("label", "string", "1 to 80 characters.", True),
            F("notes", "string", "Up to 2000 characters."),
            F("voice", "string", "Kokoro: a voice name, or several joined with commas to blend them. Default `af_heart`."),
            F("speaker", "string", "CustomVoice: a speaker the loaded checkpoint reports. Phonon: a voice name from "
                                   "`phonon_voices` of `GET /api/info`."),
            F("language", "string", "CustomVoice: a language the loaded checkpoint reports. Phonon: `English`, the default."),
            F("default_direction", "string", "CustomVoice: the delivery instruction to use when a line gives none.")),
      reply="`identity`. A new voice is a `candidate` until it is approved.",
      notes="A cloning voice has no recording yet: upload one with "
            "[`POST /api/identities/{identity_id}/references`](#post-api-identities-identity_id-references)."),
    R("POST", "/api/identities/{identity_id}/update", "Cast",
      "Rename a voice, change its notes or Kokoro voice, or approve or reject it.",
      body=(F("label", "string", "1 to 80 characters."), F("notes", "string", "Up to 2000 characters."),
            F("status", "string", "`candidate`, `approved` or `rejected`."),
            F("reference_version_id", "string", "The recording to approve; needed to approve a cloning voice."),
            F("engine_options", "object", "Kokoro: `{\"voice\": \"af_heart\"}`. Preset voices cannot change.")),
      reply="`identity`.",
      notes="Approving a cloning voice needs a recording with its exact transcript. Approving a voice sends another "
            "approved voice of the same character and engine back to `candidate`."),
    R("POST", "/api/identities/{identity_id}/references", "Cast", "Upload a recording for a cloning voice and save it as a "
                                                                  "reference version.",
      status=201, body_kind="audio",
      reply="`reference`: the saved version with its quality readings and `audio_url`.",
      errors=((413, "The recording is over 32 MiB."),),
      notes="Send the recording's bytes as the body (WAV, FLAC, Ogg, MP3 or M4A) and, in the `X-Reference-Metadata` header, "
            "base64url-encoded JSON: `{\"filename\": \"nell.wav\", \"transcript\": \"...\", \"origin\": \"...\", "
            "\"allowed_use\": \"...\", \"edits\": {\"start_seconds\": 0.5, \"end_seconds\": 9.0, \"target_sample_rate\": "
            "24000, \"channel\": \"mono\", \"noise_reduction\": {\"enabled\": false}}}`. `filename` sets the format; `edits` "
            "is optional. The saved reference is 0.5 to 60 seconds."),
    R("POST", "/api/identities/{identity_id}/references/preview", "Cast",
      "Process a recording the way an upload would, and return the result without saving anything.",
      body_kind="audio", reply_kind="wav",
      reply="The processed WAV. Its quality readings are in the `X-Reference-*` headers.",
      headers=(F("X-Reference-Duration", "string", "Seconds."), F("X-Reference-Sample-Rate", "string", "Hertz."),
               F("X-Reference-RMS", "string", "dBFS."),
               F("X-Reference-Quality", "string", "Base64url-encoded JSON of the quality assessment.")),
      errors=((413, "The recording is over 32 MiB."),),
      notes="Same body and `X-Reference-Metadata` header as the upload."),
    R("POST", "/api/identities/{identity_id}/references/from-take", "Cast",
      "Save a take as a new reference recording for a cloning voice.", status=201,
      body=(F("take_id", "string", "The take to copy.", True),
            F("transcript", "string", "Default: the take's spoken text."),
            F("edits", "object", "Trim, rate and channel edits, as for an upload."),
            F("origin", "string", "Default: a note naming the take."),
            F("allowed_use", "string", "Added to the take's own terms, which always carry over.")),
      reply="`reference`."),
    R("POST", "/api/references/{reference_id}/versions", "Cast",
      "Save a new version of a reference recording with a different transcript or trim.", status=201,
      body=(F("transcript", "string", "Up to 1200 characters. Default: the current one."),
            F("origin", "string", "Cannot be blank."), F("allowed_use", "string", "Cannot be blank."),
            F("start_seconds", "number", "Trim start in the source recording."),
            F("end_seconds", "number", "Trim end."),
            F("target_sample_rate", "integer", "8000 to 192000."),
            F("channel", "string", "`mono`, `left` or `right`."),
            F("noise_reduction", "object", "`enabled`, `profile_start_seconds`, `profile_end_seconds`, `strength`.")),
      reply="`reference`: the new version. The earlier one is kept as it was."),
    R("GET", "/api/references/{reference_id}/audio", "Cast", "Download a reference version's processed WAV.",
      reply_kind="wav", reply="The WAV.", headers=(F("X-Reference-Version", "string", "The version's id."),)),
    R("GET", "/api/references/{reference_id}/source", "Cast", "Download the recording a reference version was made from.",
      reply_kind="audio", reply="The recording as it was uploaded."),
    R("POST", "/api/references/{reference_id}/transcript-suggestion", "Cast",
      "Draft a transcript of a reference recording. Nothing is saved.", body_kind="none",
      reply="`suggestion`: the drafted text and how it was made. Check it before saving it with a new version.",
      errors=((409, "A transcription is already running."), (500, "The transcription failed."),
              (503, "No transcription model is installed."))),
    R("GET", "/api/pronunciations", "Cast", "List pronunciation entries.",
      query=(F("character_id", "string", "A character's entries together with the shared ones. Default: only the shared ones."),),
      reply="`pronunciations`: `id`, `character_id` (null when shared), `source`, `replacement`, `notes`."),
    R("POST", "/api/pronunciations", "Cast", "Add a pronunciation, or change the one with the same term.",
      body=(F("source", "string", "The written term, 1 to 100 characters.", True),
            F("replacement", "string", "How to spell it for the engine, 1 to 200 characters.", True),
            F("character_id", "string", "Omit for a shared entry."), F("notes", "string", "Up to 500 characters.")),
      reply="`pronunciation`."),
    R("POST", "/api/retirements", "Cast", "Retire a character, voice, recording or take: block new use of it and of "
                                          "everything made from it. Nothing is deleted.",
      status=201,
      body=(F("kind", "string", "`character`, `identity`, `reference` or `take`.", True),
            F("id", "string", "The item's id.", True), F("reason", "string", "Up to 300 characters.")),
      reply="`retirement` and `affected`: how many items of each kind it reaches."),
    R("POST", "/api/retirements/restore", "Cast", "Lift a retirement.",
      body=(F("kind", "string", "As for retiring.", True), F("id", "string", "The item's id.", True)),
      reply="`{\"restored\": true}`. Something retired because it came from a retired source stays blocked until that "
            "source is restored."),
    R("GET", "/api/purges/preview", "Cast", "Show what purging a retired item would delete.",
      query=(F("kind", "string", "`character`, `identity`, `reference` or `take`.", True),
             F("id", "string", "The item's id.", True)),
      reply="`would_purge` (counts by kind), `media_files` and `external_copies`: copies of the audio outside Cast storage, "
            "which a purge lists but never deletes.",
      errors=((400, "The item is not retired."),)),
    R("POST", "/api/purges", "Cast", "Permanently delete a retired item, everything made from it, and their audio.",
      body=(F("kind", "string", "As for retiring.", True), F("id", "string", "The item's id.", True),
            F("reason", "string", "Up to 300 characters."),
            F("confirm", "boolean", "Must be `true`; anything else is refused.", True)),
      reply="`batch_id`, `purged` (counts by kind), `media_files` and `external_copies`.",
      errors=((400, "The item is not retired, `confirm` is not `true`, or a file could not be removed (nothing is purged "
                    "then, so the call can be repeated)."),)),

    # ---------------------------------------------------------------- Takes
    R("GET", "/api/takes", "Takes", "List takes, oldest first.",
      query=(F("identity_id", "string", "Only this voice's takes."), F("line_id", "string", "Only takes for this line.")),
      reply="`takes`: each with `id`, `character_id`, `character_name`, `identity_id`, `identity_label`, `engine_id`, "
            "`reference_version_id`, `line_id`, `text`, `spoken_text`, `language`, `direction`, `settings`, `model`, "
            "`provenance`, `sample_rate`, `duration_seconds`, `status`, `rating`, `notes`, `created_at`, `loudness`, "
            "`waveform_checks`, `has_word_timings`, `has_phoneme_timings`, `parent_take_id` and `edit` (null unless the "
            "take is an edit of another take), `audio_url` and its retirement state."),
    R("GET", "/api/takes/{take_id}", "Takes", "Read one take.", reply="`take`, as in `GET /api/takes`."),
    R("GET", "/api/takes/{take_id}/audio", "Takes", "Download a take's WAV.", reply_kind="wav", reply="The WAV.",
      headers=(F("X-Take-Id", "string", "The take's id."),)),
    R("GET", "/api/takes/{take_id}/waveform", "Takes", "Draw a take: its peaks, its level and where the speech is.",
      query=(F("points", "integer", "How many slices to summarise the take in, 10 to 2000. Default 600."),),
      reply="`duration_seconds`, `sample_rate`, `points`, `peaks` (a `[min, max]` pair from -1 to 1 for each slice), "
            "`peak_dbfs` and `rms_dbfs` (null for silence), and `speech`: the part louder than -50 dB, with its "
            "`start_seconds` and `end_seconds`, or null when nothing is.",
      errors=((400, "`points` is not a whole number from 10 to 2000, or the stored audio is missing or has changed."),)),
    R("POST", "/api/takes/{take_id}/edit-preview", "Takes", "Hear an edit of a take without saving anything.",
      body=EDIT_FIELDS, reply_kind="wav", reply="The edited audio as a 16-bit mono WAV. Nothing is saved, and the take is not changed.",
      headers=(F("X-Audio-Seconds", "string", "Seconds of audio in the edited clip."),
               F("X-Edit-Start-Seconds", "string", "Where in the take the kept part starts."),
               F("X-Edit-End-Seconds", "string", "Where in the take the kept part ends."),
               F("X-Peak-Dbfs", "string", "The loudest sample of the edited clip in dBFS; empty for silence.")),
      errors=((400, EDIT_REFUSALS),), notes=EDIT_NOTES),
    R("POST", "/api/takes/{take_id}/edit", "Takes", "Save an edit of a take as a new take.", status=201,
      body=EDIT_FIELDS + (F("idempotency_key", "string", "1 to 200 characters. Repeating the call with the same key and the "
                                                         "same edit returns the take that was made instead of making another."),),
      reply="`take`, the new take as in `GET /api/takes` (its `parent_take_id` is this take and its `edit` says what was done), "
            "and `replayed`. A repeat with the same `idempotency_key` answers `200` with `replayed: true` and the take "
            "made the first time.",
      errors=((400, EDIT_REFUSALS + " An `idempotency_key` used before for a different edit, another take or a performance is "
                                    "refused too."),), notes=EDIT_NOTES),
    R("POST", "/api/takes/{take_id}/update", "Takes", "Mark a take selected, rejected or candidate, rate it, or note it.",
      body=(F("status", "string", "`candidate`, `selected` or `rejected`. Selecting a take returns the line's other "
                                  "selected take to `candidate`."),
            F("rating", "integer", "1 to 5."), F("notes", "string", "Up to 2000 characters.")),
      reply="`take`.", errors=((400, "The take is retired."),)),
    R("GET", "/api/takes/{take_id}/voice-match", "Takes",
      "Score how closely a take's voice matches its reference recording, with the optional speaker model.",
      reply="`voice_match`: `available`; when true, a `score`, a `band` (`likely_same`, `uncertain` or `likely_different`), "
            "the `reference_version_id` and `proxy: true`; when false, a `reason` (a preset voice has no recording to compare "
            "with, or the speaker model is not installed). A proxy for review, not an acceptance result."),

    # ---------------------------------------------------------------- Scenes
    R("GET", "/api/scenes", "Scenes", "List scenes.",
      reply="`schema_version` and `scenes`: every scene in full, the most recently changed first."),
    R("POST", "/api/scenes", "Scenes", "Create a scene from a manifest.", status=201,
      body=(F("name", "string", "1 to 120 characters. Default `Untitled scene`."),
            F("lines", "object[]", "1 to 500 lines, in order; see the scene schema.", True),
            F("schema_version", "integer", "Default 1.")),
      reply="`scene`, with its `scene_id`, `revision`, `checksum` and computed `segments`.",
      errors=((413, "The request is over 2 MiB."),),
      notes="Get a template from `GET /api/scenes/template.json` and fill in ids from `GET /api/cast`."),
    R("GET", "/api/scenes/template.json", "Scenes", "Download a scene manifest template.",
      reply="A manifest with placeholder ids."),
    R("GET", "/api/scenes/template.csv", "Scenes", "Download the scene CSV header and one sample row.", reply_kind="csv",
      reply="The CSV."),
    R("POST", "/api/scenes/import", "Scenes", "Create a scene from JSON or CSV text.", status=201,
      body=(F("format", "string", "`json` or `csv`.", True), F("content", "string", "The text, up to 2 MiB.", True)),
      reply="`scene`.", errors=((413, "The request is over 2 MiB."),)),
    R("GET", "/api/scenes/{scene_id}", "Scenes", "Read a scene.", reply="`scene`."),
    R("POST", "/api/scenes/{scene_id}", "Scenes", "Replace a scene's name and lines.",
      body=(F("name", "string", "1 to 120 characters."), F("lines", "object[]", "The full list of lines.", True),
            F("expected_revision", "integer", "The revision you last read. A different one is refused, so a stale edit "
                                              "cannot overwrite a newer one.")),
      reply="`scene` at its next revision.",
      errors=((400, "`expected_revision` is stale, or the manifest is invalid."), (413, "The request is over 2 MiB.")),
      notes="There is no PUT: a scene is updated with this POST."),
    R("GET", "/api/scenes/{scene_id}/export.json", "Scenes", "Download a scene as a manifest.", reply_kind="file",
      reply="The manifest as a JSON file."),
    R("GET", "/api/scenes/{scene_id}/export.csv", "Scenes", "Download a scene as CSV.", reply_kind="csv",
      reply="The CSV file."),
    R("GET", "/api/scenes/{scene_id}/segments", "Scenes", "Show how each line splits into synthesis segments.",
      query=(F("line_id", "string", "Only this line."),), reply="`segments`: `seg-001`, `seg-002` and so on, per line."),
    R("GET", "/api/scenes/{scene_id}/jobs", "Scenes", "List a scene's batch jobs, newest first.",
      reply="`jobs`, each as `GET /api/scenes/jobs/{job_id}` gives it."),
    R("POST", "/api/scenes/{scene_id}/jobs", "Scenes", "Queue a batch that renders the scene's lines as takes.", status=202,
      body=(F("line_ids", "string[]", "Only these lines. Default: every line."),
            F("segment_ids", "string[]", "Only these segments, as `line-id/seg-001`.")),
      reply="`job`. Jobs run one take at a time; poll `GET /api/scenes/jobs/{job_id}`."),
    R("GET", "/api/scenes/jobs/{job_id}", "Scenes", "Read a job's progress.",
      reply="`job`: its `status` (`queued`, `running`, `paused`, `completed`, `failed` or `cancelled`), the counts `total`, "
            "`queued`, `running`, `completed`, `failed` and `cancelled`, `progress` from 0 to 1, and `rows`: one per segment "
            "to render, with its `status`, `take_id`, `attempts` and `error`."),
    R("GET", "/api/scenes/jobs/{job_id}/manifest.json", "Scenes", "Download a finished job's result manifest.",
      reply_kind="file", reply="The manifest, only when its checksum verifies.",
      errors=((409, "The manifest's checksum does not match."),)),
    R("POST", "/api/scenes/jobs/{job_id}/pause", "Scenes", "Pause a job after the take in progress.", body_kind="none",
      reply="`job`."),
    R("POST", "/api/scenes/jobs/{job_id}/resume", "Scenes", "Resume a paused job.", body_kind="none", reply="`job`."),
    R("POST", "/api/scenes/jobs/{job_id}/cancel", "Scenes", "Cancel a job. A take already being made is not undone.",
      body_kind="none", reply="`job`."),
    R("POST", "/api/scenes/jobs/{job_id}/retry", "Scenes", "Queue the failed rows of a job again.", body_kind="none",
      reply="`job`."),
    R("POST", "/api/scenes/{scene_id}/select", "Scenes", "Choose which take a line, or one of its segments, uses.",
      body=(F("line_id", "string", "The line.", True), F("take_id", "string", "The take.", True),
            F("segment_id", "string", "For a line with several segments.")),
      reply="`scene`."),
    R("POST", "/api/scenes/{scene_id}/preview", "Scenes", "Assemble the selected takes into one preview.", status=201,
      body_kind="none",
      reply="`preview`: `audio_url` for the raw assembly, `mastered_audio_url` for the loudness-normalized copy, and the "
            "measurements.",
      errors=((400, "A line has no selected take, or its audio is missing."),)),
    R("GET", "/api/scenes/{scene_id}/previews/{preview_file}.wav", "Scenes", "Download a preview's WAV.", reply_kind="wav",
      reply="The WAV named by a preview answer's `audio_url` or `mastered_audio_url`."),

    # ---------------------------------------------------------------- Exports
    R("GET", "/api/exports", "Exports", "List export packages and say which formats FFmpeg can write.",
      query=(F("recheck", "string", "`1` asks FFmpeg again, after an install."),),
      reply="`exports` (newest first, each with its `name`, `status` and, when complete, its scene, formats and counts) and "
            "`ffmpeg` (`found`, `ready`, `version`, `formats` and a `message`)."),
    R("POST", "/api/exports/preview", "Exports", "Check an export request and report what it would write, writing nothing.",
      body=(F("scene_id", "string", "The scene to export.", True),
            F("formats", "string[]", "`wav`, `flac`, `ogg`, `mp3`, `m4a`. Default `[\"wav\"]`."),
            F("filename_template", "string", "Default `{line_id}`."), F("name", "string", "The package folder's name."),
            F("output_rate", "integer", "Sample rate in hertz."),
            F("trim_start_seconds", "number", "Cut from the start of each clip."),
            F("trim_end_seconds", "number", "Cut from the end of each clip."),
            F("fade_in_ms", "integer", "Fade in."), F("fade_out_ms", "integer", "Fade out."),
            F("loudness_lufs", "number", "Normalize each clip to this loudness."),
            F("loudness_true_peak", "number", "True-peak ceiling with `loudness_lufs`. Default -1."),
            F("limiter_dbfs", "number", "Peak limiter ceiling.")),
      reply="`dry_run` and `log`: the plan, line by line."),
    R("POST", "/api/exports", "Exports", "Write an export package. An existing package is never overwritten.", status=201,
      body=(F("scene_id", "string", "The scene to export.", True),
            F("formats", "string[]", "`wav`, `flac`, `ogg`, `mp3`, `m4a`. Default `[\"wav\"]`."),
            F("filename_template", "string", "Default `{line_id}`."), F("name", "string", "The package folder's name."),
            F("output_rate", "integer", "Sample rate in hertz."),
            F("trim_start_seconds", "number", "Cut from the start of each clip."),
            F("trim_end_seconds", "number", "Cut from the end of each clip."),
            F("fade_in_ms", "integer", "Fade in."), F("fade_out_ms", "integer", "Fade out."),
            F("loudness_lufs", "number", "Normalize each clip to this loudness."),
            F("loudness_true_peak", "number", "True-peak ceiling with `loudness_lufs`. Default -1."),
            F("limiter_dbfs", "number", "Peak limiter ceiling.")),
      reply="`ok`, `export` (the package's summary) and `log`.",
      errors=((409, "Another package is still being written."),)),
    R("POST", "/api/exports/{export_id}/verify", "Exports", "Check a written package against its manifest.",
      body_kind="none", reply="`ok`, `name` and `log`.", errors=((400, "The package is missing or does not verify."),)),

    # ---------------------------------------------------------------- Comparisons
    R("GET", "/api/evaluations/suites", "Comparisons", "List the named test suites.", reply="The suite definitions."),
    R("GET", "/api/evaluations", "Comparisons", "List registered runs.", reply="`runs`."),
    R("GET", "/api/evaluations/runs/{run_id}", "Comparisons", "Read a run with its samples, unblinded.", reply="`run`."),
    R("POST", "/api/evaluations/runs/import", "Comparisons", "Register a checksummed run manifest from `out/comparisons`.", status=201,
      body=(F("manifest_path", "string", "The manifest's path.", True),), reply="`run`."),
    R("POST", "/api/evaluations/cast-import", "Comparisons", "Make a run from saved Cast takes, to compare them.", status=201,
      body=(F("take_ids", "string[]", "The takes to compare.", True),
            F("title", "string", "Default `Saved voice comparison`.")),
      reply="`run`."),
    R("GET", "/api/evaluations/blind-sessions", "Comparisons", "List blind listening sessions.", reply="`sessions`."),
    R("POST", "/api/evaluations/blind-sessions", "Comparisons", "Start a blind A/B session over a run.", status=201,
      body=(F("run_id", "string", "The run to compare.", True),
            F("evaluator", "string", "Who is listening. Default `local listener`.")),
      reply="`session`: trials with A and B labels and opaque audio URLs only."),
    R("GET", "/api/evaluations/blind-sessions/{session_id}", "Comparisons", "Read a session. Models stay hidden until reveal.",
      reply="`session`."),
    R("POST", "/api/evaluations/blind-sessions/{session_id}/ratings", "Comparisons", "Save or revise the scores for a trial.",
      body=(F("trial_id", "string", "The trial.", True),
            F("ratings", "object", "`left` and `right`, each with integer scores 1 to 5 for `intelligibility`, "
                                   "`identity_consistency`, `acting_fit`, `naturalness` and `artifacts`; and an optional "
                                   "`preference` of `A`, `B`, `tie` or null.", True),
            F("notes", "string", "Free text.")),
      reply="`session`."),
    R("POST", "/api/evaluations/blind-sessions/{session_id}/reveal", "Comparisons",
      "Reveal which engine made which sample, once every trial is scored on both sides.", body_kind="none",
      reply="`session`, now revealed and locked."),
    R("GET", "/api/evaluations/blind-audio/{item_id}", "Comparisons", "Play one concealed audition copy.", reply_kind="wav",
      reply="The WAV."),
    R("GET", "/api/evaluations/items/{item_id}", "Comparisons", "Read the diagnostics for a sample, unblinded.",
      reply="`item`."),
    R("POST", "/api/evaluations/items/{item_id}/transcript", "Comparisons", "Attach a heard transcript to a sample.",
      body=(F("heard_text", "string", "What was heard.", True),
            F("evaluator", "object", "Who or what transcribed it: its `name` and `version`.", True)),
      reply="`item_id` and `diagnostics`, including the comparison with the expected text: word and character error rates "
            "and the differences."),

    # ---------------------------------------------------------------- Conversion
    R("POST", "/api/convert", "Conversion", "Say a recording again in another voice, with Chatterbox voice conversion.",
      status=201,
      body=(F("source_path", "string", "The performance to convert: a WAV, FLAC or Ogg file of up to 60 seconds in the Lab's "
                                       "reference, saved-voice, Cast, scene or conversion folders.", True),
            F("target_reference_path", "string", "The voice to convert to: a WAV, FLAC or Ogg recording of up to 10 seconds in "
                                                 "the reference, saved-voice or Cast reference folders.", True),
            F("target_reference_id", "string", "A Cast reference version; its recording must be the target file.")),
      reply="`conversion`: `conversion_id`, `audio_url`, `audio_path`, `manifest_path` and the manifest, which names both "
            "inputs by checksum.",
      errors=((409, "The GPU worker is busy."),)),

    # ---------------------------------------------------------------- System
    R("GET", "/api/system", "System", "Report the version, the system, the library, FFmpeg, disk space and engine states.",
      query=(F("recheck", "string", "`1` asks FFmpeg again."),),
      reply="`generated_at`, `app`, `system`, `library`, `ffmpeg`, `disk`, `engines`, `log`, `backups` and `diagnostics`, a "
            "plain-text version for a bug report with no scripts, transcripts or names in it."),
    R("GET", "/api/system/storage", "System", "Measure the library's folders and what a cleanup would remove.",
      reply="`library`, `other` and `cleanup`. Never walks the Drama model folder."),
    R("GET", "/api/system/backups", "System", "List backups, what a new one would include, and the running job.",
      reply="`folder`, `backups`, `plan`, `task` and `suggested_folder`."),
    R("POST", "/api/system/backups", "System", "Back up the library to one ZIP, in the background.", status=202,
      body=(F("include", "string[]", "The optional sections to add: `voices`, `exports`, `drama`. Default: all of them."),),
      reply="`task`: poll `GET /api/system/task`.", errors=((409, "A backup, check or restore is already running."),)),
    R("POST", "/api/system/backups/verify", "System", "Read a backup back in full and check every file, in the background.",
      status=202, body=(F("name", "string", "The backup's file name, from the list.", True),),
      reply="`task`.", errors=((409, "A backup, check or restore is already running."),)),
    R("POST", "/api/system/backups/restore", "System", "Restore a backup into a new folder inside the library, in the "
                                                       "background.",
      status=202,
      body=(F("name", "string", "The backup's file name.", True),
            F("folder", "string", "A new folder's name: letters, digits, spaces, dots, dashes and underscores.", True)),
      reply="`task`. The library in use is never touched.",
      errors=((409, "A backup, check or restore is already running."),)),
    R("GET", "/api/system/task", "System", "Say how the running backup, check or restore is getting on.",
      reply="`task`: its kind, state and progress, or null."),
    R("POST", "/api/system/cleanup", "System", "Remove old worker scratch folders, desktop sessions and service logs.",
      body_kind="none", reply="What was removed, and anything that could not be."),

    # ---------------------------------------------------------------- Pages (internal)
    *[R("GET", path, "Pages", summary, internal=True, reply_kind=kind)
      for path, summary, kind in (
          ("/", "The Lab's main page.", "html"), ("/index.html", "The Lab's main page.", "html"),
          ("/compare", "The Compare page.", "html"), ("/compare.html", "The Compare page.", "html"),
          ("/theme.js", "The appearance script.", "javascript"),
          ("/voice_workflows.js", "A page script.", "javascript"), ("/voice_library.js", "A page script.", "javascript"),
          ("/drama3-studio.js", "The Drama studio script.", "javascript"),
          ("/voice_lab_studio.js", "A page script.", "javascript"), ("/voice_lab_cast.js", "A page script.", "javascript"),
          ("/voice_lab_scenes.js", "A page script.", "javascript"),
          ("/voice_lab_cast_bindings.js", "A page script.", "javascript"),
          ("/voice_lab_take_edit.js", "A page script.", "javascript"),
          ("/voice_lab_generate.js", "A page script.", "javascript"), ("/voice_lab_system.js", "A page script.", "javascript"),
          ("/voice_lab_app.js", "A page script.", "javascript"), ("/voice_lab_drama.js", "A page script.", "javascript"),
          ("/out/*", "A file from the program's `out` folder, such as a finished voice conversion and its manifest.", "file"))],

    # ---------------------------------------------------------------- Page helpers (internal)
    R("GET", "/api/preferences", "Page helpers", "Read the saved page preferences: the theme, and whether the page checks for "
                                                 "updates when it opens.", internal=True,
      reply="`theme` and `check_updates`."),
    R("POST", "/api/preferences", "Page helpers", "Save one page preference or both; the other keeps its saved value.",
      internal=True,
      body=(F("theme", "string", "`system`, `light`, `dark` or `forest`."),
            F("check_updates", "boolean", "Ask GitHub for a newer release once a day when the page opens."))),
    R("GET", "/api/reference-audio", "Page helpers", "Play a recording from the reference screen by path.", internal=True,
      reply_kind="audio"),
    R("GET", "/api/references", "Page helpers", "List the recordings the reference screen offers.", internal=True),
    R("GET", "/api/voice-library", "Page helpers", "List the saved voices the voice picker offers.", internal=True),
    R("GET", "/api/conversion/sources", "Page helpers", "List the performances the conversion picker offers.", internal=True),
    R("GET", "/api/conversion/targets", "Page helpers", "List the voices the conversion picker offers.", internal=True),
    R("POST", "/api/system/use-library", "Page helpers", "Point the desktop app at a restored library the next time it "
                                                         "starts.",
      internal=True, body=(F("folder", "string", "A restored folder's name.", True),)),
    R("GET", "/api/updates", "Page helpers", "Say which version this is and what the last check for a newer release found, "
                                             "without asking GitHub.", internal=True,
      reply="`current`, `releases` (the releases page on GitHub), `check_on_open`, `due` (true when the page should check "
            "now: the once-a-day check is on and the last check is a day old, or an hour old if it failed) and `last` (the last "
            "check, as `POST /api/updates/check` answered it, or null)."),
    R("POST", "/api/updates/check", "Page helpers", "Ask GitHub which release is the newest and whether it is newer than this "
                                                    "version.", internal=True, body_kind="none",
      reply="`current`, `latest` (null while no release is published), `newer`, `message`, `checked_at` and `release` (its "
            "`tag`, `name`, `url`, `published_at` and `notes`).",
      errors=((502, "GitHub could not be reached, or refused to answer."),)),

    # ---------------------------------------------------------------- Drama (internal)
    R("GET", "/api/drama3/*", "Drama", "Everything below `/api/drama3/` that is read: `info`, and `jobs/{id}` with its "
                                       "`audio`, `metadata` and `package`.", internal=True),
    R("GET", "/api/drama3/info", "Drama", "The Drama runtime's capabilities, sources and jobs.", internal=True),
    R("GET", "/api/drama3/jobs/{job_id}", "Drama", "A Drama job, from an offset into its log.", internal=True),
    R("GET", "/api/drama3/jobs/{job_id}/audio", "Drama", "A Drama job's audio.", internal=True, reply_kind="wav"),
    R("GET", "/api/drama3/jobs/{job_id}/metadata", "Drama", "A Drama job's metadata file.", internal=True, reply_kind="file"),
    R("GET", "/api/drama3/jobs/{job_id}/package", "Drama", "A Drama job's package.", internal=True, reply_kind="file"),
    R("POST", "/api/drama3/jobs", "Drama", "Start a Drama job.", internal=True, status=202),
    R("POST", "/api/drama3/jobs/{job_id}/cancel", "Drama", "Cancel a Drama job.", internal=True),
]


# ------------------------------------------------------------------------------------------------ the table, read

def key(route):
    return f"{route.method} {route.path}"


def path_names(path):
    """The parameter names in a path template, in order."""
    return re.findall(r"\{([a-z_0-9]+)\}", path)


def shape(path):
    """The path with every parameter written {}: how the source scan spells a route."""
    return re.sub(r"\{[a-z_0-9]+\}", "{}", path)


def documented():
    return [route for route in ROUTES if not route.internal]


def internal():
    return [route for route in ROUTES if route.internal]


def anchor(route):
    """The id of the route's heading in docs/API.md."""
    return re.sub(r"[^a-z0-9_]+", "-", f"{route.method} {route.path}".lower()).strip("-")


def operation_id(route):
    return re.sub(r"[^a-z0-9]+", "_", f"{route.method} {route.path}".lower()).strip("_")


def groups_in_use(routes):
    seen = {route.group for route in routes}
    return [(name, intro) for name, intro in GROUPS if name in seen]


# ------------------------------------------------------------------------------------------------ OpenAPI

_MEDIA = {"json": "application/json", "wav": "audio/wav", "ndjson": "application/x-ndjson", "csv": "text/csv",
          "audio": "audio/*", "html": "text/html", "javascript": "text/javascript", "file": "application/octet-stream"}


def _schema(kind):
    if kind.endswith("[]"):
        return {"type": "array", "items": _schema(kind[:-2])}
    return {"type": kind}


def _fields_schema(fields):
    schema = {"type": "object", "properties": {item.name: {**_schema(item.type), "description": item.doc} for item in fields}}
    required = [item.name for item in fields if item.required]
    if required:
        schema["required"] = required
    return schema


def _error(meaning):
    return {"description": meaning, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def _operation(route):
    parameters = []
    for name in path_names(route.path):
        doc, pattern, example = PATH_PARAMS[name]
        parameters.append({"name": name, "in": "path", "required": True, "description": doc, "example": example,
                           "schema": {"type": "string", "pattern": f"^{pattern}$"}})
    for item in route.query:
        parameters.append({"name": item.name, "in": "query", "required": item.required, "description": item.doc,
                           "schema": _schema(item.type)})
    operation = {"operationId": operation_id(route), "summary": route.summary, "tags": [route.group]}
    if route.notes:
        operation["description"] = route.notes
    if route.body_kind == "audio":
        parameters.append({"name": "X-Reference-Metadata", "in": "header", "required": True,
                           "description": "Base64url-encoded JSON: `filename`, `transcript`, `origin`, `allowed_use` and "
                                          "optional `edits`.", "schema": {"type": "string"}})
        operation["requestBody"] = {"required": True, "content": {
            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
    elif route.body_kind == "json" and route.body:
        operation["requestBody"] = {"required": any(item.required for item in route.body),
                                    "content": {"application/json": {"schema": _fields_schema(route.body)}}}
    if parameters:
        operation["parameters"] = parameters
    media = _MEDIA[route.reply_kind]
    success = {"description": route.reply or "The answer."}
    if route.reply_kind == "json":
        success["content"] = {media: {"schema": {"type": "object"}}}
    else:
        success["content"] = {media: {"schema": {"type": "string", "format": "binary"}}}
    if route.headers:
        success["headers"] = {item.name: {"description": item.doc, "schema": {"type": "string"}} for item in route.headers}
    responses = {str(route.status): success}
    for status, meaning in route.errors:
        responses[str(status)] = _error(meaning)
    responses["default"] = {"$ref": "#/components/responses/Error"}
    operation["responses"] = responses
    return operation


def build_openapi():
    """The OpenAPI 3.1 document for the documented routes."""
    public = documented()
    paths = {}
    for route in public:
        paths.setdefault(route.path, {})[route.method.lower()] = _operation(route)
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": f"{APP_NAME} HTTP API",
            "version": VERSION,
            "summary": "The local service behind the Voice Lab's pages, for scripts and other programs.",
            "description": "Listens on 127.0.0.1 only. JSON in, JSON out, except where a call returns audio. Every error is "
                           "a JSON object with an `error` message. See docs/API.md for the conventions, the speaking calls "
                           "and the command-line tool.",
            "license": {"name": "MIT", "identifier": "MIT"},
        },
        "servers": [{"url": DEFAULT_URL, "description": "The default port. `serve.py --port` changes it."}],
        "tags": [{"name": name, "description": intro} for name, intro in groups_in_use(public)],
        "paths": paths,
        "components": {
            "schemas": {"Error": {"type": "object", "required": ["error"],
                                  "properties": {"error": {"type": "string", "description": "What went wrong."}}}},
            "responses": {"Error": {"description": "Any other failure: a JSON object with an `error` message.",
                                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}},
        },
        "x-internal-routes": sorted(key(route) for route in internal()),
    }


def openapi_text():
    return json.dumps(build_openapi(), indent=2, ensure_ascii=False) + "\n"


# ------------------------------------------------------------------------------------------------ the Markdown reference

def _cell(text):
    return text.replace("|", "\\|").replace("\n", " ")


def _table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_cell(str(value)) for value in row) + " |" for row in rows]
    return lines


def _section(route):
    lines = [f'<a id="{anchor(route)}"></a>', "", f"#### `{route.method} {route.path}`", "", route.summary, ""]
    names = path_names(route.path)
    if names:
        lines += _table(["Path parameter", "Meaning"], [(f"`{name}`", PATH_PARAMS[name][0]) for name in names]) + [""]
    if route.query:
        lines += _table(["Query parameter", "Type", "Meaning"],
                        [(f"`{item.name}`" + (" (required)" if item.required else ""), item.type, item.doc)
                         for item in route.query]) + [""]
    if route.body_kind == "audio":
        lines += ["**Request:** the recording's bytes, and an `X-Reference-Metadata` header.", ""]
    elif route.body and route.body_kind == "json":
        lines += _table(["Request field", "Type", "Meaning"],
                        [(f"`{item.name}`" + (" (required)" if item.required else ""), item.type, item.doc)
                         for item in route.body]) + [""]
    reply = {"json": "JSON", "wav": "`audio/wav`", "ndjson": "`application/x-ndjson`", "csv": "`text/csv`",
             "audio": "audio", "html": "HTML", "javascript": "JavaScript", "file": "a file"}[route.reply_kind]
    lines += [f"**Answer:** `{route.status}`, {reply}." + (f" {route.reply}" if route.reply else ""), ""]
    if route.headers:
        lines += _table(["Answer header", "Meaning"], [(f"`{item.name}`", item.doc) for item in route.headers]) + [""]
    if route.errors:
        lines += _table(["Also answers", "When"], [(f"`{status}`", meaning) for status, meaning in route.errors]) + [""]
    if route.notes:
        lines += [route.notes, ""]
    return lines


def render_reference():
    """The reference part of docs/API.md: every documented route, by group, then the internal routes."""
    public = documented()
    lines = []
    for name, intro in groups_in_use(public):
        rows = [route for route in public if route.group == name]
        lines += [f"### {name}", "", intro, ""]
        lines += _table(["Method", "Path", "What it does"],
                        [(route.method, f"[`{route.path}`](#{anchor(route)})", route.summary) for route in rows]) + [""]
        for route in rows:
            lines += _section(route)
    lines += ["### Internal routes", "",
              "These serve the Lab's own pages. They can change at any time and are not part of the API.", ""]
    lines += _table(["Method", "Path", "Used for"],
                    [(route.method, f"`{route.path}`", route.summary) for route in internal()]) + [""]
    return "\n".join(lines).rstrip("\n") + "\n"
