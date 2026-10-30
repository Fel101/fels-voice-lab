# HTTP API

The Voice Lab is one small web service on your own computer. Its pages talk to it, and so can a script or another program.
This page says how. [`openapi.json`](../openapi.json) describes the same calls for tools that read OpenAPI, and a running
service answers `GET /api/openapi.json` with it.

[The basics](#the-basics) · [Quick start](#quick-start) · [Speak text](#speak-text) ·
[Speak as a Cast voice](#speak-as-a-cast-voice) · [Command-line tool](#command-line-tool) · [Reference](#reference) ·
[Keeping this page true](#keeping-this-page-true)

## The basics

**Address.** `http://127.0.0.1:8765` when you start `serve.py`; `serve.py --port 8768` picks another. The desktop app starts
its own copy on a free port that it does not announce, so for scripts start `serve.py` yourself and use its address.

**Local only.** The service listens on 127.0.0.1. A request whose `Host` is not `127.0.0.1` or `localhost`, or that a web page
from another site made (it carries a foreign `Origin`), gets `403`. A script sends no `Origin` and is fine. There is no
sign-in and no key: anything on this computer can call the service, so do not forward or proxy its port.

**JSON.** Send a JSON object with `Content-Type: application/json`. A body may be up to 64 KiB; scene calls take 2 MiB and
reference recordings 32 MiB. Answers are JSON, except where a call returns audio, CSV or a file.

**Errors.** A failure is a JSON object, `{"error": "what went wrong"}`, with a status: `400` the request was refused (the
message says why), `403` not local, `404` nothing there, `409` busy, or already exists, `413` too large, `500` the service
failed, `504` an engine did not answer in time. A body over its limit is read through before the `413` goes out, so a client
that is still sending gets the answer; one that declares more than 512 MiB is not read, and its client may see the connection
reset instead.

**One generation at a time.** The GPU worker does one load or generation at once. A second speaking request while one runs
is refused with `409`, not queued: wait, then send it again. Scene batches queue their own work and take one turn at a time.

**Request ids.** Every answer carries `X-Request-Id`. Send your own (letters, digits, `.`, `_` and `-`, up to 64 characters)
and the same id is in the service log, so your logs and the Lab's can be matched.

**Header text.** A header value that is not plain ASCII, such as a character's name, is percent-encoded as UTF-8, and
`X-Metadata-Encoding` lists which headers were.

**Which routes you can rely on.** The routes in the [reference](#reference) are *documented*: they change only with a new
version and a line in the [changelog](../CHANGELOG.md). The running version is `version` in `GET /api/health`. The
[internal routes](#internal-routes) serve the Lab's own pages and can change at any time.

## Quick start

Check that the service is up:

```powershell
curl.exe -s http://127.0.0.1:8765/api/health
```

Speak a sentence with a built-in voice and keep the file. From PowerShell:

```powershell
$body = @{ text = "Hello from the Lab." } | ConvertTo-Json
Invoke-WebRequest -Uri http://127.0.0.1:8765/api/speak -Method Post -ContentType "application/json" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -OutFile hello.wav
```

From Python, with nothing installed:

```python
import json
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8765/api/speak",
    data=json.dumps({"text": "Hello from the Lab."}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=600) as response:
    with open("hello.wav", "wb") as output:
        output.write(response.read())
```

Or with the [command-line tool](#command-line-tool):

```powershell
.venv\Scripts\python.exe tools\lab_cli.py speak --text "Hello from the Lab." --out hello.wav
```

## Speak text

[`POST /api/speak`](#post-api-speak) is the plain call: text in, one WAV file out.

- It is **blocking**. The service sends nothing until the clip is finished, then sends the whole 16-bit WAV file in one
  response with a `Content-Length`. There are no chunks, no partial file and nothing to poll. Set your client's timeout for
  the longest clip you expect: the first call for an engine also loads its model, which can take a minute or more.
- `text` (1 to 1200 characters) is the one field every engine needs. `engine` picks the model (default `kokoro`); the rest
  are that engine's own controls. The reference lists the common ones, `GET /api/catalog` lists what each engine supports,
  and `GET /api/performance/settings` describes its performance controls.
- The answer's headers say how long it took (`X-Gen-Seconds`), how long the clip is (`X-Audio-Seconds`) and which checkpoint
  made it.
- Nothing is saved except what you ask for with `save_voice_as`. To keep a take with its provenance, use the next call.
- [`POST /api/stream`](#post-api-stream) is the separate call for audio as it is made, for Qwen3-TTS. `/api/speak` stays a
  blocking whole-file call; streaming never changes it.

## Speak as a Cast voice

[`POST /api/performance`](#post-api-performance) speaks one line with a voice from your Cast and saves the result as a take,
with its text, settings, model and where the voice came from. Like `/api/speak` it answers with the WAV in one blocking
response.

**1. Find the voice.** `GET /api/cast` lists the characters, their voices (identities) and each voice's recordings. Use the
identity's `id`. A voice that clones a recording must be `approved`, or you must name a `reference_version_id`; a Kokoro voice
or a preset voice needs neither.

**2. Ask for the line.**

```json
{
  "identity_id": "0123456789abcdef0123456789abcdef",
  "line_id": "scene1-001",
  "text": "We made it home.",
  "idempotency_key": "scene1-001-attempt-1"
}
```

`line_id` is your own label, 1 to 100 characters. Takes for the same voice and line are grouped under it, which is how the
Perform page and the scenes find alternatives. The other fields are optional: `reference_version_id`, `language`,
`direction`, `settings`, `use_pronunciations`.

**3. Use the answer.** The body is the WAV. `X-Take-Id` names the saved take: read it with `GET /api/takes/{take_id}`,
download it again from `GET /api/takes/{take_id}/audio`, mark it with `POST /api/takes/{take_id}/update`, or choose it for a
scene line. The other headers are `X-Cast-Character`, `X-Cast-Identity`, `X-Cast-Line-Id`, `X-Audio-Seconds`,
`X-Gen-Seconds`, `X-Voice-Backend`, `X-Model-Checkpoint`, `X-Checkpoint-Revision` and `X-Generation-Mode`.

**4. Retry safely.** `idempotency_key` is any text you choose, up to 200 characters. If the connection drops after the
service saved the take, send the same call with the same key and you get that take back, with `X-Idempotent-Replay: true`,
instead of a second generation. A key used again with different inputs is refused with `400`.

**5. Expect `409` when it is busy.** One generation runs at a time, so a call that arrives during another is refused. Wait a
moment and send it again, with the same `idempotency_key`.

From the command line, the same call by name:

```powershell
.venv\Scripts\python.exe tools\lab_cli.py perform --character Nell --text "We made it home." --line-id scene1-001 --out nell.wav
```

## Edit a saved take

A take can be trimmed, cleared of the silence at its ends, faded, and made louder or quieter, and the result saved as a new
take. The original is never changed.

**1. Look at the take.** [`GET /api/takes/{take_id}/waveform`](#get-api-takes-take_id-waveform) answers the take's peaks, its
levels and where the speech is: enough to draw it or to choose a trim.

**2. Hear the edit.** [`POST /api/takes/{take_id}/edit-preview`](#post-api-takes-take_id-edit-preview) takes the edit as JSON,
for example `{"start_seconds": 0.3, "end_seconds": 1.8, "fade_out_ms": 40, "gain_db": -2}`, and answers the edited WAV.
Nothing is saved. Fields you leave out take their defaults, and a field the service does not know is refused, so a misspelt
one is never ignored silently.

**3. Save it.** [`POST /api/takes/{take_id}/edit`](#post-api-takes-take_id-edit) takes the same JSON, with an optional
`idempotency_key`, and answers `201` with the new take. Its `parent_take_id` is the take you edited and its `edit` says what
was done. It is a `candidate` for the same line with the same text, voice, settings and model; its loudness and checks are
measured again on the new audio, and its word and phoneme times, when the original has them, move with the trim. If the
connection drops, send the call again with the same key and you get that take back, with `200` and `replayed: true`.

The steps run in a fixed order: trim, then auto-trim, then the fades, then the gain. A trim alone copies the samples exactly.
An edit of an edit is allowed. Retiring a take retires its edits, and purging it deletes them. None of these calls reaches an
engine, so they work while the service is generating.

## Command-line tool

`tools/lab_cli.py` calls the service from a terminal or a script. It needs only Python 3.8 or later, not the Lab's packages.

```text
lab_cli.py [--url URL] [--timeout SECONDS] health | speak | perform | cast
```

| Command | What it does |
|---|---|
| `health [--wait SECONDS] [--json]` | Ask `GET /api/health`. With `--wait` it keeps trying until the service answers, for a script that has just started it. |
| `speak --text TEXT --out FILE` | `POST /api/speak`. Also `--text-file FILE`, `--engine`, `--voice`, `--speed`, `--language`, `--set KEY=VALUE` (repeatable; the value is read as JSON when it can be, else as text) and `--request-file FILE` (a whole request body). |
| `perform --text TEXT --out FILE` | `POST /api/performance`. Name the voice with `--character NAME` (and `--voice LABEL` when the character has more than one), or give `--identity-id`. Also `--line-id` (default `cli`), `--reference-version-id`, `--direction`, `--language`, `--setting KEY=VALUE`, `--pronunciations` and `--idempotency-key`. |
| `cast` | List the characters and voices with their ids (`--json` for the raw answer). |

`--url` is where the service listens (default the `VOICE_LAB_URL` environment variable, else `http://127.0.0.1:8765`).
`--timeout` is how long to wait for an answer: 10 seconds for `health` and `cast`, 900 for `speak` and `perform`. `--json`
prints what the call returned as one JSON object instead of a sentence. The WAV is written under a temporary name and moved
into place only when it has arrived whole, so a failed call leaves no half file.

The exit status is `0` when the call worked, `1` when the service refused it or failed (the message is the service's own,
on standard error), `2` for a mistake in the command line, and `3` when nothing answered.

## Reference

Every documented route, by group, then the internal routes. `{name}` in a path is a value you fill in.

<!-- BEGIN GENERATED REFERENCE: edit api_spec.py, then run tools/build_api_docs.py -->

### Service

Is it running, what is loaded, which engines exist. None of these makes audio, except an engine check.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/health`](#get-api-health) | Say whether the service is ready, and which version it is. |
| GET | [`/api/info`](#get-api-info) | Name the device, the loaded engines, the Kokoro voices found and the free memory. |
| GET | [`/api/catalog`](#get-api-catalog) | List every engine the Lab knows, with its pinned checkpoint, capabilities and license. |
| GET | [`/api/engines/status`](#get-api-engines-status) | Say which engines are installed and usable here, and what each check measured. Reads files and stored results only. |
| POST | [`/api/engines/{engine_id}/check`](#post-api-engines-engine_id-check) | Check that one engine can speak now: confirm its files, load it and speak one short clip. |
| POST | [`/api/engines/{engine_id}/install`](#post-api-engines-engine_id-install) | Install an engine that has a guided installer (Kokoro): read its plan, or run the installer. |
| POST | [`/api/load`](#post-api-load) | Load an engine into memory ahead of its first use. |
| POST | [`/api/unload`](#post-api-unload) | Unload every engine and free the GPU. |
| POST | [`/api/prepare-reference`](#post-api-prepare-reference) | Prepare a reference recording for an engine so its first clone is quick. |
| GET | [`/api/performance/settings`](#get-api-performance-settings) | Describe the settings each engine's performance takes, and the languages its picker offers. |
| GET | [`/api/openapi.json`](#get-api-openapi-json) | Return this API as an OpenAPI 3.1 document. |

<a id="get-api-health"></a>

#### `GET /api/health`

Say whether the service is ready, and which version it is.

**Answer:** `200`, JSON. `status` (`ready`, or `busy` while a model loads or generates), `busy`, `device`, `version`, `workers` (one entry per loaded engine), `last_worker_error` and `last_unload`.

<a id="get-api-info"></a>

#### `GET /api/info`

Name the device, the loaded engines, the Kokoro voices found and the free memory.

**Answer:** `200`, JSON. `device`, `loaded` (the engines in memory), `voices` (Kokoro voice names), `kokoro_languages` and `free_ram_gb`.

<a id="get-api-catalog"></a>

#### `GET /api/catalog`

List every engine the Lab knows, with its pinned checkpoint, capabilities and license.

**Answer:** `200`, JSON. The engine catalog. An engine's `id` is the `engine` of the speaking calls; only engines whose `integration.service_api` is true answer them.

<a id="get-api-engines-status"></a>

#### `GET /api/engines/status`

Say which engines are installed and usable here, and what each check measured. Reads files and stored results only.

**Answer:** `200`, JSON. `device`, `busy`, `engines` (one row per catalog engine: `id`, `name`, `integrated`, `installation`, `state`, `check`, `check_outdated`, `min_free_ram_gb`), `ram`, `install`, `speed` and `store_error`. `installation` holds `installed`, `missing` (the files not found), `environment`, `checkpoint` and, while files are missing from an engine that has a guided installer, `installer`: the launcher in the lab folder that installs it. `min_free_ram_gb` is the free RAM in GB the Lab wants before it loads the engine (`null` when none is set), and `ram` is this computer's `total_gb` and `free_gb`. `install` is the latest install started from the Models page, or `null` when none has run since the service started: `engine`, `name`, `state` (`running`, `finished` or `failed`), `started_at`, `ended_at`, `seconds`, `exit_code`, `lines` (the end of its output) and `log` (all of it, a path under the lab folder).

<a id="post-api-engines-engine_id-check"></a>

#### `POST /api/engines/{engine_id}/check`

Check that one engine can speak now: confirm its files, load it and speak one short clip.

| Path parameter | Meaning |
|---|---|
| `engine_id` | An engine id from `GET /api/catalog`. |

**Answer:** `200`, JSON. `check`: `status` (`passed`, `failed` or `not_installed`), `device`, `ran_on`, `checked_at`, `reason`, `load_seconds`, `total_s`, `audio_seconds`, `real_time_factor` and, for a missing install, `missing`. Takes seconds to minutes; the result is kept and shows in `GET /api/engines/status`.

| Also answers | When |
|---|---|
| `400` | The engine is unknown, or is not a speech engine. |
| `409` | The GPU worker is busy. |

<a id="post-api-engines-engine_id-install"></a>

#### `POST /api/engines/{engine_id}/install`

Install an engine that has a guided installer (Kokoro): read its plan, or run the installer.

| Path parameter | Meaning |
|---|---|
| `engine_id` | An engine id from `GET /api/catalog`. |

| Request field | Type | Meaning |
|---|---|---|
| `dry_run` | boolean | Only read the plan: what the installer would do and whether this computer can take it. Nothing is changed. Default false, which starts the install. |

**Answer:** `200`, JSON. With `dry_run`, `plan`: `engine`, `ok`, `paragraphs` (the plan, one string for each paragraph or numbered step) and `problems` (why it cannot go ahead; empty when `ok`). Otherwise `install`: the job that started, as in `GET /api/engines/status`, which is the route to poll for its progress. The job runs the script the engine's launcher runs, with `--yes`. While it runs the service refuses every other load, check and clip.

| Also answers | When |
|---|---|
| `400` | The engine is unknown, has no installer in the app, or is installed already; or `dry_run` is not true or false. |
| `409` | An install is already running, or the GPU worker is busy. |

<a id="post-api-load"></a>

#### `POST /api/load`

Load an engine into memory ahead of its first use.

| Request field | Type | Meaning |
|---|---|---|
| `engine` | string | An engine id. Default `kokoro`. |
| `profile` | string | Breeze only: its decode profile. Default `eager`. |

**Answer:** `200`, JSON. `engine`, `loaded`, `load_seconds`, `sample_rate`, `memory`, `backend`, `checkpoint`, `checkpoint_revision`, `supported_speakers` and `supported_languages`.

| Also answers | When |
|---|---|
| `409` | The GPU worker is busy. |
| `500` | The engine could not load or run. When it is not installed and has a launcher in the lab folder (Kokoro: `setup-kokoro.bat`), `error` begins by naming the launcher. |

<a id="post-api-unload"></a>

#### `POST /api/unload`

Unload every engine and free the GPU.

**Answer:** `200`, JSON. `{"ok": true}`.

| Also answers | When |
|---|---|
| `409` | The GPU worker is busy. |

<a id="post-api-prepare-reference"></a>

#### `POST /api/prepare-reference`

Prepare a reference recording for an engine so its first clone is quick.

| Request field | Type | Meaning |
|---|---|---|
| `engine` (required) | string | An engine id that clones from a recording. |
| `path` (required) | string | A recording inside the Lab's reference storage. |
| `reference_text` | string | The exact transcript, which Breeze, Qwen Base and Drama 3 need. |
| `exaggeration` | number | Chatterbox: how strongly to act. Default 0.5. |
| `profile` | string | Breeze only: its decode profile. |
| `xvec_only` | boolean | Qwen Base only: use the speaker embedding alone, without a transcript. |

**Answer:** `200`, JSON. What the engine reports about the prepared reference.

| Also answers | When |
|---|---|
| `409` | The GPU worker is busy. |

<a id="get-api-performance-settings"></a>

#### `GET /api/performance/settings`

Describe the settings each engine's performance takes, and the languages its picker offers.

**Answer:** `200`, JSON. `engines`: for each engine its controls (name, type, range, default), whether it takes a direction, and its `languages`; and `unscreened_languages`: per engine, the languages the language screen did not clear.

<a id="get-api-openapi-json"></a>

#### `GET /api/openapi.json`

Return this API as an OpenAPI 3.1 document.

**Answer:** `200`, JSON. The same document as `openapi.json` in the program folder, built from the route table.

### Speaking

Turn text into audio. `speak` and `performance` answer with the finished WAV file in one response.

| Method | Path | What it does |
|---|---|---|
| POST | [`/api/speak`](#post-api-speak) | Speak text with one engine and answer with the finished WAV file. |
| POST | [`/api/stream`](#post-api-stream) | Speak text with a Qwen3-TTS checkpoint and stream the audio as it is made. |
| POST | [`/api/performance`](#post-api-performance) | Speak a line as a Cast voice and save it as a take. |
| POST | [`/api/kokoro/phonemize`](#post-api-kokoro-phonemize) | Show the phonemes Kokoro would speak for a text, without loading a model. |

<a id="post-api-speak"></a>

#### `POST /api/speak`

Speak text with one engine and answer with the finished WAV file.

| Request field | Type | Meaning |
|---|---|---|
| `text` (required) | string | What to say, 1 to 1200 characters. |
| `engine` | string | An engine id from `GET /api/catalog` whose `integration.service_api` is true. Default `kokoro`. |
| `voice` | string | Kokoro: a voice name, or several joined with commas to blend them. Default `af_heart`. |
| `speed` | number | Kokoro: speaking rate. Default 1.0. |
| `language` | string | Kokoro and Qwen: the language. Default `English`. |
| `voice_description` | string | Qwen VoiceDesign, VoxCPM2 and Breeze design mode: the voice to invent, in words. |
| `mode` | string | For engines with several modes: `design`, `clone`, `continuation`, `custom_voice` or, for Breeze, `direction`. A checkpoint accepts the modes it supports. |
| `reference_path` | string | Cloning: a recording inside the Lab's reference storage. |
| `reference_text` | string | Cloning: the exact transcript of that recording. |
| `direction` | string | Engines with a direction control: how to deliver the line, up to 1000 characters. |
| `seed` | integer | Engines that take one: the random seed. Default 42. |
| `save_voice_as` | string | Design mode only: keep the invented voice as a reusable reference under this name, letters, digits, `_` and `-`, up to two folders deep. |

**Answer:** `200`, `audio/wav`. The whole 16-bit WAV file, in one response with a `Content-Length`. The call blocks until the clip is finished; there is no streaming and no partial answer. Other engine-specific fields are listed under each engine's `capabilities` in `GET /api/catalog`.

| Answer header | Meaning |
|---|---|
| `X-Gen-Seconds` | Seconds the engine took. |
| `X-Audio-Seconds` | Seconds of audio in the file. |
| `X-First-Chunk` | Seconds to the first audio, when the engine reports it; otherwise empty. |
| `X-Generation-Mode` | The mode the engine ran in, when it reports one. |
| `X-Voice-Backend` | The backend, when reported. |
| `X-Model-Checkpoint` | The checkpoint, when reported. |
| `X-Checkpoint-Revision` | The checkpoint's pinned revision, when reported. |
| `X-Voice-Asset-Path` | With `save_voice_as`: the file the voice was saved to. |

| Also answers | When |
|---|---|
| `409` | `save_voice_as` names a voice that already exists, or the GPU worker is busy with another request. One generation runs at a time; a second request is refused, not queued. |
| `413` | The request is over 64 KiB. |
| `500` | The engine could not load or run. When it is not installed and has a launcher in the lab folder (Kokoro: `setup-kokoro.bat`), `error` begins by naming the launcher. |
| `504` | The engine did not answer in time. |

Send no query string: `/api/speak?x=1` is a 404. Qwen, VoxCPM2 and Drama 3 refuse a field they do not take and name it in the 400. To keep what you generate as a Cast take, use [`POST /api/performance`](#post-api-performance).

<a id="post-api-stream"></a>

#### `POST /api/stream`

Speak text with a Qwen3-TTS checkpoint and stream the audio as it is made.

| Request field | Type | Meaning |
|---|---|---|
| `text` (required) | string | What to say, 1 to 1200 characters. |
| `engine` (required) | string | A Qwen3-TTS engine id. |
| `mode` | string | `design`, `clone` or `custom_voice`, as the checkpoint supports. |
| `voice_description` | string | VoiceDesign: the voice to invent, in words. |
| `reference_path` | string | Base (clone): a recording inside the Lab's reference storage. |
| `reference_text` | string | Base (clone): the exact transcript. |
| `speaker` | string | CustomVoice: a speaker the loaded checkpoint reports. |
| `language` | string | The language. Default `English`. |
| `save_voice_as` | string | VoiceDesign only: keep the voice as a reusable reference under this name. |

**Answer:** `200`, `application/x-ndjson`. Newline-delimited JSON: one `start` line, `audio` lines (`sequence`, `sample_start`, `sample_count`, `sample_rate`, `sample_format` `pcm_s16le` and the PCM as base64 in `data`) and a closing `complete` line with the timings. A failure after the first line arrives as an `error` line, not a status code.

Use `POST /api/speak` when you want a file.

<a id="post-api-performance"></a>

#### `POST /api/performance`

Speak a line as a Cast voice and save it as a take.

| Request field | Type | Meaning |
|---|---|---|
| `identity_id` (required) | string | The voice identity to speak with, from `GET /api/cast`. |
| `text` (required) | string | The line, 1 to 1200 characters. |
| `line_id` (required) | string | Your own label for the line, 1 to 100 characters. Takes are grouped by voice and line. |
| `reference_version_id` | string | The recording to clone from. Default: the identity's approved recording. Refused for a fixed or preset voice. |
| `language` | string | Default: the voice's own language, else `English`. |
| `direction` | string | How to deliver the line, up to 1000 characters. An engine with no verified direction control refuses a non-empty one. |
| `settings` | object | The engine's performance settings; see `GET /api/performance/settings`. |
| `use_pronunciations` | boolean | Apply the character's pronunciation dictionary to the spoken text. The take keeps both texts. Default false. |
| `idempotency_key` | string | 1 to 200 characters. Repeating a call with the same key and the same inputs returns that take's audio without generating again. |
| `identity_snapshot` | object | Used by scene batches: the voice's `identity_type`, `engine_id` and `engine_options` as the scene saved them. The call is refused if the voice has changed since. |

**Answer:** `200`, `audio/wav`. The take's 16-bit WAV file, in one response. The call blocks until the clip is finished and saved.

| Answer header | Meaning |
|---|---|
| `X-Take-Id` | The saved take's id. |
| `X-Cast-Character` | The character's name. |
| `X-Cast-Identity` | The voice's label. |
| `X-Cast-Line-Id` | The `line_id` you sent. |
| `X-Gen-Seconds` | Seconds the engine took. |
| `X-Audio-Seconds` | Seconds of audio in the file. |
| `X-Voice-Backend` | The backend. |
| `X-Model-Checkpoint` | The checkpoint. |
| `X-Checkpoint-Revision` | The checkpoint's pinned revision. |
| `X-Generation-Mode` | `clone`, `direction`, `custom_voice` or `fixed_voice`. |
| `X-Idempotent-Replay` | `true` when the answer is an earlier take for the same key; then only `X-Take-Id` and the three `X-Cast-*` headers come with it. |

| Also answers | When |
|---|---|
| `400` | The identity is rejected or retired, has no usable recording, the text or direction is too long, the settings do not fit the engine, or `idempotency_key` was used before with different inputs or for an edit of a take. |
| `404` | The identity or the recording does not exist. |
| `409` | The GPU worker is busy. One generation runs at a time; a second request is refused, not queued. |
| `504` | The engine did not answer in time. |

See [Speak as a Cast voice](#speak-as-a-cast-voice) for how to find the ids and what a retry does.

<a id="post-api-kokoro-phonemize"></a>

#### `POST /api/kokoro/phonemize`

Show the phonemes Kokoro would speak for a text, without loading a model.

| Request field | Type | Meaning |
|---|---|---|
| `text` (required) | string | 1 to 1200 characters. |
| `language` | string | Default `English`. |

**Answer:** `200`, JSON. `language`, `language_name` and `segments`, each with its `graphemes` and `phonemes`.

### Cast

Characters, the voices (identities) they speak with, the recordings that clone those voices, and pronunciations. Retiring blocks new use of an item and everything made from it; purging deletes it.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/cast`](#get-api-cast) | List every character with its voices and their recordings. |
| POST | [`/api/characters`](#post-api-characters) | Create a character. |
| POST | [`/api/characters/{character_id}`](#post-api-characters-character_id) | Rename a character or change its description. |
| POST | [`/api/characters/{character_id}/identities`](#post-api-characters-character_id-identities) | Add a voice to a character. |
| POST | [`/api/identities/{identity_id}/update`](#post-api-identities-identity_id-update) | Rename a voice, change its notes or Kokoro voice, or approve or reject it. |
| POST | [`/api/identities/{identity_id}/references`](#post-api-identities-identity_id-references) | Upload a recording for a cloning voice and save it as a reference version. |
| POST | [`/api/identities/{identity_id}/references/preview`](#post-api-identities-identity_id-references-preview) | Process a recording the way an upload would, and return the result without saving anything. |
| POST | [`/api/identities/{identity_id}/references/from-take`](#post-api-identities-identity_id-references-from-take) | Save a take as a new reference recording for a cloning voice. |
| POST | [`/api/references/{reference_id}/versions`](#post-api-references-reference_id-versions) | Save a new version of a reference recording with a different transcript or trim. |
| GET | [`/api/references/{reference_id}/audio`](#get-api-references-reference_id-audio) | Download a reference version's processed WAV. |
| GET | [`/api/references/{reference_id}/source`](#get-api-references-reference_id-source) | Download the recording a reference version was made from. |
| POST | [`/api/references/{reference_id}/transcript-suggestion`](#post-api-references-reference_id-transcript-suggestion) | Draft a transcript of a reference recording. Nothing is saved. |
| GET | [`/api/pronunciations`](#get-api-pronunciations) | List pronunciation entries. |
| POST | [`/api/pronunciations`](#post-api-pronunciations) | Add a pronunciation, or change the one with the same term. |
| POST | [`/api/retirements`](#post-api-retirements) | Retire a character, voice, recording or take: block new use of it and of everything made from it. Nothing is deleted. |
| POST | [`/api/retirements/restore`](#post-api-retirements-restore) | Lift a retirement. |
| GET | [`/api/purges/preview`](#get-api-purges-preview) | Show what purging a retired item would delete. |
| POST | [`/api/purges`](#post-api-purges) | Permanently delete a retired item, everything made from it, and their audio. |

<a id="get-api-cast"></a>

#### `GET /api/cast`

List every character with its voices and their recordings.

**Answer:** `200`, JSON. `schema_version` and `characters`. Each character has `id`, `name`, `description`, `tags` and `identities`; each identity has `id`, `engine_id`, `label`, `status` (`candidate`, `approved` or `rejected`), `kind` (`reference`, `fixed_voice` or `preset_voice`), `engine_options`, `approved_reference_id`, `notes` and `references` (the recordings, newest version first, each with `id`, `version`, `transcript`, `origin`, `allowed_use`, `duration_seconds` and `audio_url`). Retired items say so.

<a id="post-api-characters"></a>

#### `POST /api/characters`

Create a character.

| Request field | Type | Meaning |
|---|---|---|
| `name` (required) | string | 1 to 80 characters. |
| `description` | string | Up to 2000 characters. |
| `tags` | string[] | Up to 40 characters each. |

**Answer:** `201`, JSON. `character`, as in `GET /api/cast`.

<a id="post-api-characters-character_id"></a>

#### `POST /api/characters/{character_id}`

Rename a character or change its description.

| Path parameter | Meaning |
|---|---|
| `character_id` | A character's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `name` | string | 1 to 80 characters. Leave out to keep it. |
| `description` | string | Up to 2000 characters. Leave out to keep it. |

**Answer:** `200`, JSON. `character`.

<a id="post-api-characters-character_id-identities"></a>

#### `POST /api/characters/{character_id}/identities`

Add a voice to a character.

| Path parameter | Meaning |
|---|---|
| `character_id` | A character's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `engine_id` (required) | string | A recording-cloning engine, `kokoro` or the Qwen CustomVoice engine that the service exposes. |
| `label` (required) | string | 1 to 80 characters. |
| `notes` | string | Up to 2000 characters. |
| `voice` | string | Kokoro: a voice name, or several joined with commas to blend them. Default `af_heart`. |
| `speaker` | string | CustomVoice: a speaker the loaded checkpoint reports. |
| `language` | string | CustomVoice: a language the loaded checkpoint reports. |
| `default_direction` | string | CustomVoice: the delivery instruction to use when a line gives none. |

**Answer:** `201`, JSON. `identity`. A new voice is a `candidate` until it is approved.

A cloning voice has no recording yet: upload one with [`POST /api/identities/{identity_id}/references`](#post-api-identities-identity_id-references).

<a id="post-api-identities-identity_id-update"></a>

#### `POST /api/identities/{identity_id}/update`

Rename a voice, change its notes or Kokoro voice, or approve or reject it.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `label` | string | 1 to 80 characters. |
| `notes` | string | Up to 2000 characters. |
| `status` | string | `candidate`, `approved` or `rejected`. |
| `reference_version_id` | string | The recording to approve; needed to approve a cloning voice. |
| `engine_options` | object | Kokoro: `{"voice": "af_heart"}`. Preset voices cannot change. |

**Answer:** `200`, JSON. `identity`.

Approving a cloning voice needs a recording with its exact transcript. Approving a voice sends another approved voice of the same character and engine back to `candidate`.

<a id="post-api-identities-identity_id-references"></a>

#### `POST /api/identities/{identity_id}/references`

Upload a recording for a cloning voice and save it as a reference version.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

**Request:** the recording's bytes, and an `X-Reference-Metadata` header.

**Answer:** `201`, JSON. `reference`: the saved version with its quality readings and `audio_url`.

| Also answers | When |
|---|---|
| `413` | The recording is over 32 MiB. |

Send the recording's bytes as the body (WAV, FLAC, Ogg, MP3 or M4A) and, in the `X-Reference-Metadata` header, base64url-encoded JSON: `{"filename": "nell.wav", "transcript": "...", "origin": "...", "allowed_use": "...", "edits": {"start_seconds": 0.5, "end_seconds": 9.0, "target_sample_rate": 24000, "channel": "mono", "noise_reduction": {"enabled": false}}}`. `filename` sets the format; `edits` is optional. The saved reference is 0.5 to 60 seconds.

<a id="post-api-identities-identity_id-references-preview"></a>

#### `POST /api/identities/{identity_id}/references/preview`

Process a recording the way an upload would, and return the result without saving anything.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

**Request:** the recording's bytes, and an `X-Reference-Metadata` header.

**Answer:** `200`, `audio/wav`. The processed WAV. Its quality readings are in the `X-Reference-*` headers.

| Answer header | Meaning |
|---|---|
| `X-Reference-Duration` | Seconds. |
| `X-Reference-Sample-Rate` | Hertz. |
| `X-Reference-RMS` | dBFS. |
| `X-Reference-Quality` | Base64url-encoded JSON of the quality assessment. |

| Also answers | When |
|---|---|
| `413` | The recording is over 32 MiB. |

Same body and `X-Reference-Metadata` header as the upload.

<a id="post-api-identities-identity_id-references-from-take"></a>

#### `POST /api/identities/{identity_id}/references/from-take`

Save a take as a new reference recording for a cloning voice.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `take_id` (required) | string | The take to copy. |
| `transcript` | string | Default: the take's spoken text. |
| `edits` | object | Trim, rate and channel edits, as for an upload. |
| `origin` | string | Default: a note naming the take. |
| `allowed_use` | string | Added to the take's own terms, which always carry over. |

**Answer:** `201`, JSON. `reference`.

<a id="post-api-references-reference_id-versions"></a>

#### `POST /api/references/{reference_id}/versions`

Save a new version of a reference recording with a different transcript or trim.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `transcript` | string | Up to 1200 characters. Default: the current one. |
| `origin` | string | Cannot be blank. |
| `allowed_use` | string | Cannot be blank. |
| `start_seconds` | number | Trim start in the source recording. |
| `end_seconds` | number | Trim end. |
| `target_sample_rate` | integer | 8000 to 192000. |
| `channel` | string | `mono`, `left` or `right`. |
| `noise_reduction` | object | `enabled`, `profile_start_seconds`, `profile_end_seconds`, `strength`. |

**Answer:** `201`, JSON. `reference`: the new version. The earlier one is kept as it was.

<a id="get-api-references-reference_id-audio"></a>

#### `GET /api/references/{reference_id}/audio`

Download a reference version's processed WAV.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

**Answer:** `200`, `audio/wav`. The WAV.

| Answer header | Meaning |
|---|---|
| `X-Reference-Version` | The version's id. |

<a id="get-api-references-reference_id-source"></a>

#### `GET /api/references/{reference_id}/source`

Download the recording a reference version was made from.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

**Answer:** `200`, audio. The recording as it was uploaded.

<a id="post-api-references-reference_id-transcript-suggestion"></a>

#### `POST /api/references/{reference_id}/transcript-suggestion`

Draft a transcript of a reference recording. Nothing is saved.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

**Answer:** `200`, JSON. `suggestion`: the drafted text and how it was made. Check it before saving it with a new version.

| Also answers | When |
|---|---|
| `409` | A transcription is already running. |
| `500` | The transcription failed. |
| `503` | No transcription model is installed. |

<a id="get-api-pronunciations"></a>

#### `GET /api/pronunciations`

List pronunciation entries.

| Query parameter | Type | Meaning |
|---|---|---|
| `character_id` | string | A character's entries together with the shared ones. Default: only the shared ones. |

**Answer:** `200`, JSON. `pronunciations`: `id`, `character_id` (null when shared), `source`, `replacement`, `notes`.

<a id="post-api-pronunciations"></a>

#### `POST /api/pronunciations`

Add a pronunciation, or change the one with the same term.

| Request field | Type | Meaning |
|---|---|---|
| `source` (required) | string | The written term, 1 to 100 characters. |
| `replacement` (required) | string | How to spell it for the engine, 1 to 200 characters. |
| `character_id` | string | Omit for a shared entry. |
| `notes` | string | Up to 500 characters. |

**Answer:** `200`, JSON. `pronunciation`.

<a id="post-api-retirements"></a>

#### `POST /api/retirements`

Retire a character, voice, recording or take: block new use of it and of everything made from it. Nothing is deleted.

| Request field | Type | Meaning |
|---|---|---|
| `kind` (required) | string | `character`, `identity`, `reference` or `take`. |
| `id` (required) | string | The item's id. |
| `reason` | string | Up to 300 characters. |

**Answer:** `201`, JSON. `retirement` and `affected`: how many items of each kind it reaches.

<a id="post-api-retirements-restore"></a>

#### `POST /api/retirements/restore`

Lift a retirement.

| Request field | Type | Meaning |
|---|---|---|
| `kind` (required) | string | As for retiring. |
| `id` (required) | string | The item's id. |

**Answer:** `200`, JSON. `{"restored": true}`. Something retired because it came from a retired source stays blocked until that source is restored.

<a id="get-api-purges-preview"></a>

#### `GET /api/purges/preview`

Show what purging a retired item would delete.

| Query parameter | Type | Meaning |
|---|---|---|
| `kind` (required) | string | `character`, `identity`, `reference` or `take`. |
| `id` (required) | string | The item's id. |

**Answer:** `200`, JSON. `would_purge` (counts by kind), `media_files` and `external_copies`: copies of the audio outside Cast storage, which a purge lists but never deletes.

| Also answers | When |
|---|---|
| `400` | The item is not retired. |

<a id="post-api-purges"></a>

#### `POST /api/purges`

Permanently delete a retired item, everything made from it, and their audio.

| Request field | Type | Meaning |
|---|---|---|
| `kind` (required) | string | As for retiring. |
| `id` (required) | string | The item's id. |
| `reason` | string | Up to 300 characters. |
| `confirm` (required) | boolean | Must be `true`; anything else is refused. |

**Answer:** `200`, JSON. `batch_id`, `purged` (counts by kind), `media_files` and `external_copies`.

| Also answers | When |
|---|---|
| `400` | The item is not retired, `confirm` is not `true`, or a file could not be removed (nothing is purged then, so the call can be repeated). |

### Takes

Saved performances. Every `performance` call and every scene render saves a take with its text, settings, model and provenance. An edit of a take (a trim, a cut of the silence, fades, a gain) is saved as a new take that names its original.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/takes`](#get-api-takes) | List takes, oldest first. |
| GET | [`/api/takes/{take_id}`](#get-api-takes-take_id) | Read one take. |
| GET | [`/api/takes/{take_id}/audio`](#get-api-takes-take_id-audio) | Download a take's WAV. |
| GET | [`/api/takes/{take_id}/waveform`](#get-api-takes-take_id-waveform) | Draw a take: its peaks, its level and where the speech is. |
| POST | [`/api/takes/{take_id}/edit-preview`](#post-api-takes-take_id-edit-preview) | Hear an edit of a take without saving anything. |
| POST | [`/api/takes/{take_id}/edit`](#post-api-takes-take_id-edit) | Save an edit of a take as a new take. |
| POST | [`/api/takes/{take_id}/update`](#post-api-takes-take_id-update) | Mark a take selected, rejected or candidate, rate it, or note it. |
| GET | [`/api/takes/{take_id}/voice-match`](#get-api-takes-take_id-voice-match) | Score how closely a take's voice matches its reference recording, with the optional speaker model. |

<a id="get-api-takes"></a>

#### `GET /api/takes`

List takes, oldest first.

| Query parameter | Type | Meaning |
|---|---|---|
| `identity_id` | string | Only this voice's takes. |
| `line_id` | string | Only takes for this line. |

**Answer:** `200`, JSON. `takes`: each with `id`, `character_id`, `character_name`, `identity_id`, `identity_label`, `engine_id`, `reference_version_id`, `line_id`, `text`, `spoken_text`, `language`, `direction`, `settings`, `model`, `provenance`, `sample_rate`, `duration_seconds`, `status`, `rating`, `notes`, `created_at`, `loudness`, `waveform_checks`, `has_word_timings`, `has_phoneme_timings`, `parent_take_id` and `edit` (null unless the take is an edit of another take), `audio_url` and its retirement state.

<a id="get-api-takes-take_id"></a>

#### `GET /api/takes/{take_id}`

Read one take.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

**Answer:** `200`, JSON. `take`, as in `GET /api/takes`.

<a id="get-api-takes-take_id-audio"></a>

#### `GET /api/takes/{take_id}/audio`

Download a take's WAV.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

**Answer:** `200`, `audio/wav`. The WAV.

| Answer header | Meaning |
|---|---|
| `X-Take-Id` | The take's id. |

<a id="get-api-takes-take_id-waveform"></a>

#### `GET /api/takes/{take_id}/waveform`

Draw a take: its peaks, its level and where the speech is.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `points` | integer | How many slices to summarise the take in, 10 to 2000. Default 600. |

**Answer:** `200`, JSON. `duration_seconds`, `sample_rate`, `points`, `peaks` (a `[min, max]` pair from -1 to 1 for each slice), `peak_dbfs` and `rms_dbfs` (null for silence), and `speech`: the part louder than -50 dB, with its `start_seconds` and `end_seconds`, or null when nothing is.

| Also answers | When |
|---|---|
| `400` | `points` is not a whole number from 10 to 2000, or the stored audio is missing or has changed. |

<a id="post-api-takes-take_id-edit-preview"></a>

#### `POST /api/takes/{take_id}/edit-preview`

Hear an edit of a take without saving anything.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Request field | Type | Meaning |
|---|---|---|
| `start_seconds` | number | Where the kept part starts, in seconds from the start of the take. Default 0. |
| `end_seconds` | number | Where the kept part ends. Default the end of the take. It must be after `start_seconds`. |
| `auto_trim` | boolean | Cut the quiet before and after the speech inside that range. Default false. |
| `silence_threshold_db` | number | Auto-trim: anything quieter than this counts as silence, from -90 to -10 dB. Default -50. |
| `keep_silence_ms` | number | Auto-trim: how much of the silence to leave at each end, 0 to 2000 ms. Default 80. |
| `fade_in_ms` | number | A fade in over the start of the result, 0 to 5000 ms. Default 0. |
| `fade_out_ms` | number | A fade out over the end of the result, 0 to 5000 ms. Default 0. |
| `gain_db` | number | Make the result louder or quieter, -60 to 24 dB. A gain that would clip is refused. Default 0. |

**Answer:** `200`, `audio/wav`. The edited audio as a 16-bit mono WAV. Nothing is saved, and the take is not changed.

| Answer header | Meaning |
|---|---|
| `X-Audio-Seconds` | Seconds of audio in the edited clip. |
| `X-Edit-Start-Seconds` | Where in the take the kept part starts. |
| `X-Edit-End-Seconds` | Where in the take the kept part ends. |
| `X-Peak-Dbfs` | The loudest sample of the edited clip in dBFS; empty for silence. |

| Also answers | When |
|---|---|
| `400` | A field is unknown, not a number (or not true or false) or out of range; `end_seconds` is not after `start_seconds`; the edit changes nothing; the result would be under 0.05 seconds; the two fades are longer than the result; auto-trim finds nothing louder than the threshold; a gain would clip the loudest sample (the answer says how much the take can take); the take is retired; or its stored audio is missing or no longer matches its checksum. |

An edit never changes the take it starts from. The steps run in this order: trim to `start_seconds` to `end_seconds`; auto-trim inside that range (quiet inside the speech is never cut); the two fades, on a raised-cosine curve; then the gain. A trim alone copies the samples exactly; a fade or a gain recalculates them and rounds back to 16 bits. The new take keeps the parent's text, voice, settings and model, starts as a `candidate`, and has its loudness and checks measured again. Its word and phoneme times, when the parent has them, move with the trim, and a word the trim cuts away leaves them. An edit of an edit is allowed. Retiring a take retires its edits, and purging it deletes them.

<a id="post-api-takes-take_id-edit"></a>

#### `POST /api/takes/{take_id}/edit`

Save an edit of a take as a new take.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Request field | Type | Meaning |
|---|---|---|
| `start_seconds` | number | Where the kept part starts, in seconds from the start of the take. Default 0. |
| `end_seconds` | number | Where the kept part ends. Default the end of the take. It must be after `start_seconds`. |
| `auto_trim` | boolean | Cut the quiet before and after the speech inside that range. Default false. |
| `silence_threshold_db` | number | Auto-trim: anything quieter than this counts as silence, from -90 to -10 dB. Default -50. |
| `keep_silence_ms` | number | Auto-trim: how much of the silence to leave at each end, 0 to 2000 ms. Default 80. |
| `fade_in_ms` | number | A fade in over the start of the result, 0 to 5000 ms. Default 0. |
| `fade_out_ms` | number | A fade out over the end of the result, 0 to 5000 ms. Default 0. |
| `gain_db` | number | Make the result louder or quieter, -60 to 24 dB. A gain that would clip is refused. Default 0. |
| `idempotency_key` | string | 1 to 200 characters. Repeating the call with the same key and the same edit returns the take that was made instead of making another. |

**Answer:** `201`, JSON. `take`, the new take as in `GET /api/takes` (its `parent_take_id` is this take and its `edit` says what was done), and `replayed`. A repeat with the same `idempotency_key` answers `200` with `replayed: true` and the take made the first time.

| Also answers | When |
|---|---|
| `400` | A field is unknown, not a number (or not true or false) or out of range; `end_seconds` is not after `start_seconds`; the edit changes nothing; the result would be under 0.05 seconds; the two fades are longer than the result; auto-trim finds nothing louder than the threshold; a gain would clip the loudest sample (the answer says how much the take can take); the take is retired; or its stored audio is missing or no longer matches its checksum. An `idempotency_key` used before for a different edit, another take or a performance is refused too. |

An edit never changes the take it starts from. The steps run in this order: trim to `start_seconds` to `end_seconds`; auto-trim inside that range (quiet inside the speech is never cut); the two fades, on a raised-cosine curve; then the gain. A trim alone copies the samples exactly; a fade or a gain recalculates them and rounds back to 16 bits. The new take keeps the parent's text, voice, settings and model, starts as a `candidate`, and has its loudness and checks measured again. Its word and phoneme times, when the parent has them, move with the trim, and a word the trim cuts away leaves them. An edit of an edit is allowed. Retiring a take retires its edits, and purging it deletes them.

<a id="post-api-takes-take_id-update"></a>

#### `POST /api/takes/{take_id}/update`

Mark a take selected, rejected or candidate, rate it, or note it.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Request field | Type | Meaning |
|---|---|---|
| `status` | string | `candidate`, `selected` or `rejected`. Selecting a take returns the line's other selected take to `candidate`. |
| `rating` | integer | 1 to 5. |
| `notes` | string | Up to 2000 characters. |

**Answer:** `200`, JSON. `take`.

| Also answers | When |
|---|---|
| `400` | The take is retired. |

<a id="get-api-takes-take_id-voice-match"></a>

#### `GET /api/takes/{take_id}/voice-match`

Score how closely a take's voice matches its reference recording, with the optional speaker model.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

**Answer:** `200`, JSON. `voice_match`: `available`; when true, a `score`, a `band` (`likely_same`, `uncertain` or `likely_different`), the `reference_version_id` and `proxy: true`; when false, a `reason` (a preset voice has no recording to compare with, or the speaker model is not installed). A proxy for review, not an acceptance result.

### Scenes

Ordered scripts rendered line by line through the same performance call. The manifest's fields are in [scene-schema-v1.md](scenes/scene-schema-v1.md).

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/scenes`](#get-api-scenes) | List scenes. |
| POST | [`/api/scenes`](#post-api-scenes) | Create a scene from a manifest. |
| GET | [`/api/scenes/template.json`](#get-api-scenes-template-json) | Download a scene manifest template. |
| GET | [`/api/scenes/template.csv`](#get-api-scenes-template-csv) | Download the scene CSV header and one sample row. |
| POST | [`/api/scenes/import`](#post-api-scenes-import) | Create a scene from JSON or CSV text. |
| GET | [`/api/scenes/{scene_id}`](#get-api-scenes-scene_id) | Read a scene. |
| POST | [`/api/scenes/{scene_id}`](#post-api-scenes-scene_id) | Replace a scene's name and lines. |
| GET | [`/api/scenes/{scene_id}/export.json`](#get-api-scenes-scene_id-export-json) | Download a scene as a manifest. |
| GET | [`/api/scenes/{scene_id}/export.csv`](#get-api-scenes-scene_id-export-csv) | Download a scene as CSV. |
| GET | [`/api/scenes/{scene_id}/segments`](#get-api-scenes-scene_id-segments) | Show how each line splits into synthesis segments. |
| GET | [`/api/scenes/{scene_id}/jobs`](#get-api-scenes-scene_id-jobs) | List a scene's batch jobs, newest first. |
| POST | [`/api/scenes/{scene_id}/jobs`](#post-api-scenes-scene_id-jobs) | Queue a batch that renders the scene's lines as takes. |
| GET | [`/api/scenes/jobs/{job_id}`](#get-api-scenes-jobs-job_id) | Read a job's progress. |
| GET | [`/api/scenes/jobs/{job_id}/manifest.json`](#get-api-scenes-jobs-job_id-manifest-json) | Download a finished job's result manifest. |
| POST | [`/api/scenes/jobs/{job_id}/pause`](#post-api-scenes-jobs-job_id-pause) | Pause a job after the take in progress. |
| POST | [`/api/scenes/jobs/{job_id}/resume`](#post-api-scenes-jobs-job_id-resume) | Resume a paused job. |
| POST | [`/api/scenes/jobs/{job_id}/cancel`](#post-api-scenes-jobs-job_id-cancel) | Cancel a job. A take already being made is not undone. |
| POST | [`/api/scenes/jobs/{job_id}/retry`](#post-api-scenes-jobs-job_id-retry) | Queue the failed rows of a job again. |
| POST | [`/api/scenes/{scene_id}/select`](#post-api-scenes-scene_id-select) | Choose which take a line, or one of its segments, uses. |
| POST | [`/api/scenes/{scene_id}/preview`](#post-api-scenes-scene_id-preview) | Assemble the selected takes into one preview. |
| GET | [`/api/scenes/{scene_id}/previews/{preview_file}.wav`](#get-api-scenes-scene_id-previews-preview_file-wav) | Download a preview's WAV. |

<a id="get-api-scenes"></a>

#### `GET /api/scenes`

List scenes.

**Answer:** `200`, JSON. `schema_version` and `scenes`: every scene in full, the most recently changed first.

<a id="post-api-scenes"></a>

#### `POST /api/scenes`

Create a scene from a manifest.

| Request field | Type | Meaning |
|---|---|---|
| `name` | string | 1 to 120 characters. Default `Untitled scene`. |
| `lines` (required) | object[] | 1 to 500 lines, in order; see the scene schema. |
| `schema_version` | integer | Default 1. |

**Answer:** `201`, JSON. `scene`, with its `scene_id`, `revision`, `checksum` and computed `segments`.

| Also answers | When |
|---|---|
| `413` | The request is over 2 MiB. |

Get a template from `GET /api/scenes/template.json` and fill in ids from `GET /api/cast`.

<a id="get-api-scenes-template-json"></a>

#### `GET /api/scenes/template.json`

Download a scene manifest template.

**Answer:** `200`, JSON. A manifest with placeholder ids.

<a id="get-api-scenes-template-csv"></a>

#### `GET /api/scenes/template.csv`

Download the scene CSV header and one sample row.

**Answer:** `200`, `text/csv`. The CSV.

<a id="post-api-scenes-import"></a>

#### `POST /api/scenes/import`

Create a scene from JSON or CSV text.

| Request field | Type | Meaning |
|---|---|---|
| `format` (required) | string | `json` or `csv`. |
| `content` (required) | string | The text, up to 2 MiB. |

**Answer:** `201`, JSON. `scene`.

| Also answers | When |
|---|---|
| `413` | The request is over 2 MiB. |

<a id="get-api-scenes-scene_id"></a>

#### `GET /api/scenes/{scene_id}`

Read a scene.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, JSON. `scene`.

<a id="post-api-scenes-scene_id"></a>

#### `POST /api/scenes/{scene_id}`

Replace a scene's name and lines.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `name` | string | 1 to 120 characters. |
| `lines` (required) | object[] | The full list of lines. |
| `expected_revision` | integer | The revision you last read. A different one is refused, so a stale edit cannot overwrite a newer one. |

**Answer:** `200`, JSON. `scene` at its next revision.

| Also answers | When |
|---|---|
| `400` | `expected_revision` is stale, or the manifest is invalid. |
| `413` | The request is over 2 MiB. |

There is no PUT: a scene is updated with this POST.

<a id="get-api-scenes-scene_id-export-json"></a>

#### `GET /api/scenes/{scene_id}/export.json`

Download a scene as a manifest.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, a file. The manifest as a JSON file.

<a id="get-api-scenes-scene_id-export-csv"></a>

#### `GET /api/scenes/{scene_id}/export.csv`

Download a scene as CSV.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, `text/csv`. The CSV file.

<a id="get-api-scenes-scene_id-segments"></a>

#### `GET /api/scenes/{scene_id}/segments`

Show how each line splits into synthesis segments.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `line_id` | string | Only this line. |

**Answer:** `200`, JSON. `segments`: `seg-001`, `seg-002` and so on, per line.

<a id="get-api-scenes-scene_id-jobs"></a>

#### `GET /api/scenes/{scene_id}/jobs`

List a scene's batch jobs, newest first.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, JSON. `jobs`, each as `GET /api/scenes/jobs/{job_id}` gives it.

<a id="post-api-scenes-scene_id-jobs"></a>

#### `POST /api/scenes/{scene_id}/jobs`

Queue a batch that renders the scene's lines as takes.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `line_ids` | string[] | Only these lines. Default: every line. |
| `segment_ids` | string[] | Only these segments, as `line-id/seg-001`. |

**Answer:** `202`, JSON. `job`. Jobs run one take at a time; poll `GET /api/scenes/jobs/{job_id}`.

<a id="get-api-scenes-jobs-job_id"></a>

#### `GET /api/scenes/jobs/{job_id}`

Read a job's progress.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`: its `status` (`queued`, `running`, `paused`, `completed`, `failed` or `cancelled`), the counts `total`, `queued`, `running`, `completed`, `failed` and `cancelled`, `progress` from 0 to 1, and `rows`: one per segment to render, with its `status`, `take_id`, `attempts` and `error`.

<a id="get-api-scenes-jobs-job_id-manifest-json"></a>

#### `GET /api/scenes/jobs/{job_id}/manifest.json`

Download a finished job's result manifest.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, a file. The manifest, only when its checksum verifies.

| Also answers | When |
|---|---|
| `409` | The manifest's checksum does not match. |

<a id="post-api-scenes-jobs-job_id-pause"></a>

#### `POST /api/scenes/jobs/{job_id}/pause`

Pause a job after the take in progress.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-jobs-job_id-resume"></a>

#### `POST /api/scenes/jobs/{job_id}/resume`

Resume a paused job.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-jobs-job_id-cancel"></a>

#### `POST /api/scenes/jobs/{job_id}/cancel`

Cancel a job. A take already being made is not undone.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-jobs-job_id-retry"></a>

#### `POST /api/scenes/jobs/{job_id}/retry`

Queue the failed rows of a job again.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-scene_id-select"></a>

#### `POST /api/scenes/{scene_id}/select`

Choose which take a line, or one of its segments, uses.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `line_id` (required) | string | The line. |
| `take_id` (required) | string | The take. |
| `segment_id` | string | For a line with several segments. |

**Answer:** `200`, JSON. `scene`.

<a id="post-api-scenes-scene_id-preview"></a>

#### `POST /api/scenes/{scene_id}/preview`

Assemble the selected takes into one preview.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `201`, JSON. `preview`: `audio_url` for the raw assembly, `mastered_audio_url` for the loudness-normalized copy, and the measurements.

| Also answers | When |
|---|---|
| `400` | A line has no selected take, or its audio is missing. |

<a id="get-api-scenes-scene_id-previews-preview_file-wav"></a>

#### `GET /api/scenes/{scene_id}/previews/{preview_file}.wav`

Download a preview's WAV.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |
| `preview_file` | The file name in a preview answer's `audio_url`, without its `.wav`. |

**Answer:** `200`, `audio/wav`. The WAV named by a preview answer's `audio_url` or `mastered_audio_url`.

### Exports

Delivery packages: a scene's selected takes, converted, checked and written to `out/exports`.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/exports`](#get-api-exports) | List export packages and say which formats FFmpeg can write. |
| POST | [`/api/exports/preview`](#post-api-exports-preview) | Check an export request and report what it would write, writing nothing. |
| POST | [`/api/exports`](#post-api-exports) | Write an export package. An existing package is never overwritten. |
| POST | [`/api/exports/{export_id}/verify`](#post-api-exports-export_id-verify) | Check a written package against its manifest. |

<a id="get-api-exports"></a>

#### `GET /api/exports`

List export packages and say which formats FFmpeg can write.

| Query parameter | Type | Meaning |
|---|---|---|
| `recheck` | string | `1` asks FFmpeg again, after an install. |

**Answer:** `200`, JSON. `exports` (newest first, each with its `name`, `status` and, when complete, its scene, formats and counts) and `ffmpeg` (`found`, `ready`, `version`, `formats` and a `message`).

<a id="post-api-exports-preview"></a>

#### `POST /api/exports/preview`

Check an export request and report what it would write, writing nothing.

| Request field | Type | Meaning |
|---|---|---|
| `scene_id` (required) | string | The scene to export. |
| `formats` | string[] | `wav`, `flac`, `ogg`, `mp3`, `m4a`. Default `["wav"]`. |
| `filename_template` | string | Default `{line_id}`. |
| `name` | string | The package folder's name. |
| `output_rate` | integer | Sample rate in hertz. |
| `trim_start_seconds` | number | Cut from the start of each clip. |
| `trim_end_seconds` | number | Cut from the end of each clip. |
| `fade_in_ms` | integer | Fade in. |
| `fade_out_ms` | integer | Fade out. |
| `loudness_lufs` | number | Normalize each clip to this loudness. |
| `loudness_true_peak` | number | True-peak ceiling with `loudness_lufs`. Default -1. |
| `limiter_dbfs` | number | Peak limiter ceiling. |

**Answer:** `200`, JSON. `dry_run` and `log`: the plan, line by line.

<a id="post-api-exports"></a>

#### `POST /api/exports`

Write an export package. An existing package is never overwritten.

| Request field | Type | Meaning |
|---|---|---|
| `scene_id` (required) | string | The scene to export. |
| `formats` | string[] | `wav`, `flac`, `ogg`, `mp3`, `m4a`. Default `["wav"]`. |
| `filename_template` | string | Default `{line_id}`. |
| `name` | string | The package folder's name. |
| `output_rate` | integer | Sample rate in hertz. |
| `trim_start_seconds` | number | Cut from the start of each clip. |
| `trim_end_seconds` | number | Cut from the end of each clip. |
| `fade_in_ms` | integer | Fade in. |
| `fade_out_ms` | integer | Fade out. |
| `loudness_lufs` | number | Normalize each clip to this loudness. |
| `loudness_true_peak` | number | True-peak ceiling with `loudness_lufs`. Default -1. |
| `limiter_dbfs` | number | Peak limiter ceiling. |

**Answer:** `201`, JSON. `ok`, `export` (the package's summary) and `log`.

| Also answers | When |
|---|---|
| `409` | Another package is still being written. |

<a id="post-api-exports-export_id-verify"></a>

#### `POST /api/exports/{export_id}/verify`

Check a written package against its manifest.

| Path parameter | Meaning |
|---|---|
| `export_id` | An export package's folder name under `out/exports`, from `GET /api/exports`. |

**Answer:** `200`, JSON. `ok`, `name` and `log`.

| Also answers | When |
|---|---|
| `400` | The package is missing or does not verify. |

### Comparisons

Listening comparisons and blind A/B sessions. See [evaluation-schema-v1.md](evaluation/evaluation-schema-v1.md).

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/evaluations/suites`](#get-api-evaluations-suites) | List the named test suites. |
| GET | [`/api/evaluations`](#get-api-evaluations) | List registered runs. |
| GET | [`/api/evaluations/runs/{run_id}`](#get-api-evaluations-runs-run_id) | Read a run with its samples, unblinded. |
| POST | [`/api/evaluations/runs/import`](#post-api-evaluations-runs-import) | Register a checksummed run manifest from `out/comparisons`. |
| POST | [`/api/evaluations/cast-import`](#post-api-evaluations-cast-import) | Make a run from saved Cast takes, to compare them. |
| GET | [`/api/evaluations/blind-sessions`](#get-api-evaluations-blind-sessions) | List blind listening sessions. |
| POST | [`/api/evaluations/blind-sessions`](#post-api-evaluations-blind-sessions) | Start a blind A/B session over a run. |
| GET | [`/api/evaluations/blind-sessions/{session_id}`](#get-api-evaluations-blind-sessions-session_id) | Read a session. Models stay hidden until reveal. |
| POST | [`/api/evaluations/blind-sessions/{session_id}/ratings`](#post-api-evaluations-blind-sessions-session_id-ratings) | Save or revise the scores for a trial. |
| POST | [`/api/evaluations/blind-sessions/{session_id}/reveal`](#post-api-evaluations-blind-sessions-session_id-reveal) | Reveal which engine made which sample, once every trial is scored on both sides. |
| GET | [`/api/evaluations/blind-audio/{item_id}`](#get-api-evaluations-blind-audio-item_id) | Play one concealed audition copy. |
| GET | [`/api/evaluations/items/{item_id}`](#get-api-evaluations-items-item_id) | Read the diagnostics for a sample, unblinded. |
| POST | [`/api/evaluations/items/{item_id}/transcript`](#post-api-evaluations-items-item_id-transcript) | Attach a heard transcript to a sample. |

<a id="get-api-evaluations-suites"></a>

#### `GET /api/evaluations/suites`

List the named test suites.

**Answer:** `200`, JSON. The suite definitions.

<a id="get-api-evaluations"></a>

#### `GET /api/evaluations`

List registered runs.

**Answer:** `200`, JSON. `runs`.

<a id="get-api-evaluations-runs-run_id"></a>

#### `GET /api/evaluations/runs/{run_id}`

Read a run with its samples, unblinded.

| Path parameter | Meaning |
|---|---|
| `run_id` | A registered comparison run's id, from `GET /api/evaluations`. |

**Answer:** `200`, JSON. `run`.

<a id="post-api-evaluations-runs-import"></a>

#### `POST /api/evaluations/runs/import`

Register a checksummed run manifest from `out/comparisons`.

| Request field | Type | Meaning |
|---|---|---|
| `manifest_path` (required) | string | The manifest's path. |

**Answer:** `201`, JSON. `run`.

<a id="post-api-evaluations-cast-import"></a>

#### `POST /api/evaluations/cast-import`

Make a run from saved Cast takes, to compare them.

| Request field | Type | Meaning |
|---|---|---|
| `take_ids` (required) | string[] | The takes to compare. |
| `title` | string | Default `Saved voice comparison`. |

**Answer:** `201`, JSON. `run`.

<a id="get-api-evaluations-blind-sessions"></a>

#### `GET /api/evaluations/blind-sessions`

List blind listening sessions.

**Answer:** `200`, JSON. `sessions`.

<a id="post-api-evaluations-blind-sessions"></a>

#### `POST /api/evaluations/blind-sessions`

Start a blind A/B session over a run.

| Request field | Type | Meaning |
|---|---|---|
| `run_id` (required) | string | The run to compare. |
| `evaluator` | string | Who is listening. Default `local listener`. |

**Answer:** `201`, JSON. `session`: trials with A and B labels and opaque audio URLs only.

<a id="get-api-evaluations-blind-sessions-session_id"></a>

#### `GET /api/evaluations/blind-sessions/{session_id}`

Read a session. Models stay hidden until reveal.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

**Answer:** `200`, JSON. `session`.

<a id="post-api-evaluations-blind-sessions-session_id-ratings"></a>

#### `POST /api/evaluations/blind-sessions/{session_id}/ratings`

Save or revise the scores for a trial.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

| Request field | Type | Meaning |
|---|---|---|
| `trial_id` (required) | string | The trial. |
| `ratings` (required) | object | `left` and `right`, each with integer scores 1 to 5 for `intelligibility`, `identity_consistency`, `acting_fit`, `naturalness` and `artifacts`; and an optional `preference` of `A`, `B`, `tie` or null. |
| `notes` | string | Free text. |

**Answer:** `200`, JSON. `session`.

<a id="post-api-evaluations-blind-sessions-session_id-reveal"></a>

#### `POST /api/evaluations/blind-sessions/{session_id}/reveal`

Reveal which engine made which sample, once every trial is scored on both sides.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

**Answer:** `200`, JSON. `session`, now revealed and locked.

<a id="get-api-evaluations-blind-audio-item_id"></a>

#### `GET /api/evaluations/blind-audio/{item_id}`

Play one concealed audition copy.

| Path parameter | Meaning |
|---|---|
| `item_id` | A compared sample's id. |

**Answer:** `200`, `audio/wav`. The WAV.

<a id="get-api-evaluations-items-item_id"></a>

#### `GET /api/evaluations/items/{item_id}`

Read the diagnostics for a sample, unblinded.

| Path parameter | Meaning |
|---|---|
| `item_id` | A compared sample's id. |

**Answer:** `200`, JSON. `item`.

<a id="post-api-evaluations-items-item_id-transcript"></a>

#### `POST /api/evaluations/items/{item_id}/transcript`

Attach a heard transcript to a sample.

| Path parameter | Meaning |
|---|---|
| `item_id` | A compared sample's id. |

| Request field | Type | Meaning |
|---|---|---|
| `heard_text` (required) | string | What was heard. |
| `evaluator` (required) | object | Who or what transcribed it: its `name` and `version`. |

**Answer:** `200`, JSON. `item_id` and `diagnostics`, including the comparison with the expected text: word and character error rates and the differences.

### Conversion

Voice conversion: say a recording again in another voice.

| Method | Path | What it does |
|---|---|---|
| POST | [`/api/convert`](#post-api-convert) | Say a recording again in another voice, with Chatterbox voice conversion. |

<a id="post-api-convert"></a>

#### `POST /api/convert`

Say a recording again in another voice, with Chatterbox voice conversion.

| Request field | Type | Meaning |
|---|---|---|
| `source_path` (required) | string | The performance to convert: a WAV, FLAC or Ogg file of up to 60 seconds in the Lab's reference, saved-voice, Cast, scene or conversion folders. |
| `target_reference_path` (required) | string | The voice to convert to: a WAV, FLAC or Ogg recording of up to 10 seconds in the reference, saved-voice or Cast reference folders. |
| `target_reference_id` | string | A Cast reference version; its recording must be the target file. |

**Answer:** `201`, JSON. `conversion`: `conversion_id`, `audio_url`, `audio_path`, `manifest_path` and the manifest, which names both inputs by checksum.

| Also answers | When |
|---|---|
| `409` | The GPU worker is busy. |

### System

The service's report, folder sizes, library backups and housekeeping.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/system`](#get-api-system) | Report the version, the system, the library, FFmpeg, disk space and engine states. |
| GET | [`/api/system/storage`](#get-api-system-storage) | Measure the library's folders and what a cleanup would remove. |
| GET | [`/api/system/backups`](#get-api-system-backups) | List backups, what a new one would include, and the running job. |
| POST | [`/api/system/backups`](#post-api-system-backups) | Back up the library to one ZIP, in the background. |
| POST | [`/api/system/backups/verify`](#post-api-system-backups-verify) | Read a backup back in full and check every file, in the background. |
| POST | [`/api/system/backups/restore`](#post-api-system-backups-restore) | Restore a backup into a new folder inside the library, in the background. |
| GET | [`/api/system/task`](#get-api-system-task) | Say how the running backup, check or restore is getting on. |
| POST | [`/api/system/cleanup`](#post-api-system-cleanup) | Remove old worker scratch folders, desktop sessions and service logs. |

<a id="get-api-system"></a>

#### `GET /api/system`

Report the version, the system, the library, FFmpeg, disk space and engine states.

| Query parameter | Type | Meaning |
|---|---|---|
| `recheck` | string | `1` asks FFmpeg again. |

**Answer:** `200`, JSON. `generated_at`, `app`, `system`, `library`, `ffmpeg`, `disk`, `engines`, `log`, `backups` and `diagnostics`, a plain-text version for a bug report with no scripts, transcripts or names in it.

<a id="get-api-system-storage"></a>

#### `GET /api/system/storage`

Measure the library's folders and what a cleanup would remove.

**Answer:** `200`, JSON. `library`, `other` and `cleanup`. Never walks the Drama model folder.

<a id="get-api-system-backups"></a>

#### `GET /api/system/backups`

List backups, what a new one would include, and the running job.

**Answer:** `200`, JSON. `folder`, `backups`, `plan`, `task` and `suggested_folder`.

<a id="post-api-system-backups"></a>

#### `POST /api/system/backups`

Back up the library to one ZIP, in the background.

| Request field | Type | Meaning |
|---|---|---|
| `include` | string[] | The optional sections to add: `voices`, `exports`, `drama`. Default: all of them. |

**Answer:** `202`, JSON. `task`: poll `GET /api/system/task`.

| Also answers | When |
|---|---|
| `409` | A backup, check or restore is already running. |

<a id="post-api-system-backups-verify"></a>

#### `POST /api/system/backups/verify`

Read a backup back in full and check every file, in the background.

| Request field | Type | Meaning |
|---|---|---|
| `name` (required) | string | The backup's file name, from the list. |

**Answer:** `202`, JSON. `task`.

| Also answers | When |
|---|---|
| `409` | A backup, check or restore is already running. |

<a id="post-api-system-backups-restore"></a>

#### `POST /api/system/backups/restore`

Restore a backup into a new folder inside the library, in the background.

| Request field | Type | Meaning |
|---|---|---|
| `name` (required) | string | The backup's file name. |
| `folder` (required) | string | A new folder's name: letters, digits, spaces, dots, dashes and underscores. |

**Answer:** `202`, JSON. `task`. The library in use is never touched.

| Also answers | When |
|---|---|
| `409` | A backup, check or restore is already running. |

<a id="get-api-system-task"></a>

#### `GET /api/system/task`

Say how the running backup, check or restore is getting on.

**Answer:** `200`, JSON. `task`: its kind, state and progress, or null.

<a id="post-api-system-cleanup"></a>

#### `POST /api/system/cleanup`

Remove old worker scratch folders, desktop sessions and service logs.

**Answer:** `200`, JSON. What was removed, and anything that could not be.

### Internal routes

These serve the Lab's own pages. They can change at any time and are not part of the API.

| Method | Path | Used for |
|---|---|---|
| GET | `/` | The Lab's main page. |
| GET | `/index.html` | The Lab's main page. |
| GET | `/compare` | The Compare page. |
| GET | `/compare.html` | The Compare page. |
| GET | `/theme.js` | The appearance script. |
| GET | `/voice_workflows.js` | A page script. |
| GET | `/voice_library.js` | A page script. |
| GET | `/drama3-studio.js` | The Drama studio script. |
| GET | `/voice_lab_studio.js` | A page script. |
| GET | `/voice_lab_cast.js` | A page script. |
| GET | `/voice_lab_scenes.js` | A page script. |
| GET | `/voice_lab_cast_bindings.js` | A page script. |
| GET | `/voice_lab_take_edit.js` | A page script. |
| GET | `/voice_lab_generate.js` | A page script. |
| GET | `/voice_lab_system.js` | A page script. |
| GET | `/voice_lab_app.js` | A page script. |
| GET | `/voice_lab_drama.js` | A page script. |
| GET | `/out/*` | A file from the program's `out` folder, such as a finished voice conversion and its manifest. |
| GET | `/api/preferences` | Read the saved appearance choice. |
| POST | `/api/preferences` | Save the appearance choice. |
| GET | `/api/reference-audio` | Play a recording from the reference screen by path. |
| GET | `/api/references` | List the recordings the reference screen offers. |
| GET | `/api/voice-library` | List the saved voices the voice picker offers. |
| GET | `/api/conversion/sources` | List the performances the conversion picker offers. |
| GET | `/api/conversion/targets` | List the voices the conversion picker offers. |
| POST | `/api/system/use-library` | Point the desktop app at a restored library the next time it starts. |
| GET | `/api/drama3/*` | Everything below `/api/drama3/` that is read: `info`, and `jobs/{id}` with its `audio`, `metadata` and `package`. |
| GET | `/api/drama3/info` | The Drama runtime's capabilities, sources and jobs. |
| GET | `/api/drama3/jobs/{job_id}` | A Drama job, from an offset into its log. |
| GET | `/api/drama3/jobs/{job_id}/audio` | A Drama job's audio. |
| GET | `/api/drama3/jobs/{job_id}/metadata` | A Drama job's metadata file. |
| GET | `/api/drama3/jobs/{job_id}/package` | A Drama job's package. |
| POST | `/api/drama3/jobs` | Start a Drama job. |
| POST | `/api/drama3/jobs/{job_id}/cancel` | Cancel a Drama job. |

<!-- END GENERATED REFERENCE -->

## Keeping this page true

Every route `serve.py` answers has a row in `api_spec.py`, either documented or marked `internal=True`.
`tests/test_api_docs.py` reads `serve.py` and fails when it answers a route that has no row, when a row names a route it no
longer answers, when a path parameter's pattern differs from the one `serve.py` matches, and when `openapi.json` or the
reference above is not what the table gives. After you change the table, write both again:

```powershell
.venv\Scripts\python.exe tools\build_api_docs.py
```

Only the reference between the two marker lines is generated; the rest of this page is written by hand.
